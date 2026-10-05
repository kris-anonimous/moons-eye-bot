"""
handlers/chat_moderation.py

- /everyone (созыв) с защитой от злоупотребления не-админами.
- Антиспам/рейд-детект (в памяти процесса).
- Простые правила модерации — по паттернам (2.0, 2.2, 2.3, 2.4).
- Контекстные правила — через Gemini (текст: 0.x/1.x/2.1/3.1–3.3; медиа: 3.0/3.5).
- Ручные наказания: ответ админа/владельца реплаем в формате «Нарушение по
  X / Наказание: Y» — как задано в ТЗ, а не отдельными командами-словами.
- Форма выхода «Роль / Причина» — сохраняется, используется при факте выхода.
- Всё, что пишут боту в личку не по делу (не команда, не часть анкеты),
  пересылается в чат логов (кроме владельца).
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from collections import defaultdict, deque

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message
from aiogram import BaseMiddleware

import config
from database import methods as db
from services import gemini_service, pure, punishments
from services.utils import (
    ban_everywhere, esc, is_main_thread, log, notify_admins, role_of, send, send_everyone,
)

logger = logging.getLogger(__name__)
router = Router(name="chat_moderation")

# --- состояние антиспама/рейд-детекта — в памяти процесса (сознательно,
# см. README: это счётчики "прямо сейчас", не часть истории нарушений) ---
_recent: dict[int, deque] = defaultdict(lambda: deque(maxlen=30))

EMPTY_RE = re.compile(r"^[\W\d_\s]{1,3}$|^(.)\1{4,}$")
_recent_dm: dict[int, deque] = defaultdict(lambda: deque(maxlen=30))

# Единая последовательность последних сообщений в ОБЩЕМ чате (не по одному
# юзеру, а по чату целиком) — нужна, чтобы честно ловить именно ПОДРЯД идущие
# сообщения одного человека (правило 2.3 «лесенка» и 2.4 «за раз»), а не
# просто «много коротких сообщений за N секунд», которое даёт ложные
# срабатывания на обычную быструю переписку нескольких разных людей.
_chat_sequence: deque = deque(maxlen=30)  # [(user_id, is_sticker_gif_or_audio), ...]


def _consecutive_run(user_id: int, media_only: bool = False) -> int:
    """Сколько подряд (с конца) сообщений в чате принадлежат этому юзеру
    (и, если media_only=True, являются стикером/GIF/аудио) без прерывания
    сообщением другого человека (или другого типа — для media_only)."""
    count = 0
    for uid, is_media in reversed(_chat_sequence):
        if uid != user_id or (media_only and not is_media):
            break
        count += 1
    return count


class DmRaidGuardMiddleware(BaseMiddleware):
    """Рейд/спам-атака НА САМОГО БОТА в личке (правило 2.0: «рейдерские...
    атаки бота/чат спамом» — ТЗ явно упоминает бота отдельно от чата).
    Регистрируется как outer-middleware в main.py — то есть проверяет
    ВСЕ личные сообщения раньше любых обработчиков (в т.ч. FSM-анкет),
    и при обнаружении спама просто не передаёт событие дальше."""

    async def __call__(self, handler, event: Message, data: dict):
        user = event.from_user
        if event.chat.type != "private" or not user or user.id == config.OWNER_ID:
            return await handler(event, data)

        now = dt.datetime.utcnow()
        dq = _recent_dm[user.id]
        dq.append(now)
        recent_window = [t for t in dq if (now - t).total_seconds() <= config.RAID_SECONDS]
        if len(recent_window) < config.RAID_MESSAGES:
            return await handler(event, data)

        bot = data["bot"]
        db_user = await db.get_user(user.id)
        if db_user and db_user.get("in_chat") and db_user.get("has_bot_access"):
            await punishments.apply_penalty(bot, user.id, "2.0", "perm", reason="спам-атака на бота в личке")
        else:
            await ban_everywhere(bot, user.id)
            await notify_admins(bot, f"Спам-атака на бота в личке: пользователь {user.id} заблокирован на всех площадках.")
        return None  # дальше не передаём — ни один обработчик это сообщение не увидит


# ---------------------------------------------------------------------------
# /everyone
# ---------------------------------------------------------------------------

@router.message(Command("everyone"), F.chat.id == config.MAIN_CHAT_ID)
async def cmd_everyone(message: Message, bot: Bot):
    if not is_main_thread(message) or message.from_user.is_bot:
        return
    user_id = message.from_user.id
    user = await db.get_user(user_id)
    allowed = bool(user and (user.get("is_admin") or user.get("is_owner")))

    if not allowed:
        count = await db.inc_everyone_abuse(user_id)
        role = role_of(user)
        if count > 3:
            await punishments.apply_penalty(bot, user_id, "2.5", "mute", mute_seconds=86400,
                                            reason="повторный спам /everyone")
            await send(bot, user_id, f"Уважаемый(ая) {esc(role)}, просим Вас не использовать эту команду, "
                                     f"если у Вас нет прав админа. При повторном нарушении Вы получите "
                                     f"предупреждение (варн).")
        else:
            await db.add_oral_warns(user_id, 1)
            warn = await message.answer("У вас нет прав админа для использования этой команды")
            import asyncio
            asyncio.create_task(_delete_later(bot, warn.chat.id, warn.message_id, 300))
        return

    text_after = message.text.partition(" ")[2] if " " in (message.text or "") else ""
    await send_everyone(bot, text_after)


async def _delete_later(bot: Bot, chat_id: int, message_id: int, seconds: int):
    import asyncio
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass


# ---------------------------------------------------------------------------
# Выход из чата — форма «Роль / Причина»
# ---------------------------------------------------------------------------

async def maybe_save_leave_note(message: Message, user: dict) -> None:
    reason = pure.parse_leave_note(message.text or "", user.get("role_key", ""))
    if reason:
        await db.save_leave_note(message.from_user.id, user["role_key"], reason)


# ---------------------------------------------------------------------------
# Основной обработчик сообщений в общем чате
# ---------------------------------------------------------------------------

MEDIA_FILTER = F.photo | F.video | F.sticker | F.animation | F.audio | F.voice | F.document


# ---------------------------------------------------------------------------
# Ручные наказания (ответ админа/владельца реплаем на сообщение нарушителя)
# ---------------------------------------------------------------------------

UNDO_WORDS = {"размут": "mute", "снять варн": "warn", "разбан": "ban"}
_MANUAL_RE = re.compile(r"(?i)^\s*нарушение\s+по")


def _looks_manual(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in UNDO_WORDS or bool(_MANUAL_RE.match(t))


@router.message(F.chat.id == config.MAIN_CHAT_ID, F.reply_to_message, F.text.func(_looks_manual))
async def on_manual_moderation(message: Message, bot: Bot):
    if not is_main_thread(message):
        return
    actor = await db.get_user(message.from_user.id)
    if not actor or not actor.get("is_owner"):
        return  # ручные наказания может выдавать только владелец (и сам бот — автоматически)
    target_id = message.reply_to_message.from_user.id
    target = await db.get_user(target_id)
    if target and target.get("is_owner"):
        return
    text = (message.text or "").strip()
    low = text.lower()

    if low in UNDO_WORDS:
        kind = UNDO_WORDS[low]
        if kind == "mute":
            await punishments.undo_mute(bot, target_id)
        elif kind == "warn":
            await punishments.undo_warn(target_id)
        elif kind == "ban":
            await punishments.undo_ban(bot, target_id)
        await log(bot, f"{esc(low.capitalize())} применено к {esc(role_of(target, str(target_id)))} "
                       f"владельцем ({esc(role_of(actor))}).")
        return

    parsed = pure.parse_punishment_message(text)
    if not parsed:
        return
    rule, label = parsed
    penalty = pure.penalty_from_text(label)
    if not penalty:
        return
    code, mute_seconds = penalty
    await punishments.apply_penalty(bot, target_id, rule, code, mute_seconds=mute_seconds, announce=False)
    await log(bot, f"Наказание выдано вручную владельцем ({esc(role_of(actor))}).")



@router.message(F.chat.id == config.MAIN_CHAT_ID, F.text | MEDIA_FILTER)
async def on_group_message(message: Message, bot: Bot):
    if not is_main_thread(message):
        return
    if message.from_user.is_bot:
        return  # сторонние боты в чате не участвуют ни в статистике, ни в модерации
    user_id = message.from_user.id
    now = dt.datetime.utcnow()
    dq = _recent[user_id]
    dq.append(now)

    user = await db.get_user(user_id)
    is_member = bool(user and user.get("in_chat") and user.get("has_bot_access"))

    # --- рейд/спам-детект (для незарегистрированных или неучастников) ---
    recent_window = [t for t in dq if (now - t).total_seconds() <= config.RAID_SECONDS]
    if len(recent_window) >= config.RAID_MESSAGES:
        if is_member:
            await punishments.apply_penalty(bot, user_id, "2.0", "perm", reason="спам/рейдерская атака")
        else:
            await ban_everywhere(bot, user_id)
            await notify_admins(bot, f"Рейдерская атака: пользователь {user_id} заблокирован на всех площадках.")
        return

    if not is_member:
        return

    if user["rest"]["resting"]:
        try:
            await message.delete()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        return

    text = message.text or message.caption or ""
    if text:
        await maybe_save_leave_note(message, user)

    await db.record_message(user_id)

    is_2_4_media = bool(message.sticker or message.animation or message.audio)
    _chat_sequence.append((user_id, is_2_4_media))

    # --- 2.3: СООБЩЕНИЯ ЛЕСЕНКОЙ — более 7-ми сообщений ПОДРЯД от одного и
    # того же человека, без прерывания другими участниками (любого типа
    # контента, не только коротких) ---
    if _consecutive_run(user_id) > 7:
        await punishments.apply_penalty(bot, user_id, "2.3", "oral", reason="сообщения лесенкой")
        return

    # --- 2.4: более 3-х стикеров/GIF/аудио ПОДРЯД от одного человека, без
    # прерывания сообщением другого типа (именно этих типов — ТЗ не включает
    # сюда фото, видео, войсы и произвольные документы) ---
    if is_2_4_media and _consecutive_run(user_id, media_only=True) > 3:
        await punishments.apply_penalty(bot, user_id, "2.4", "warn", reason="спам стикерами/GIF/музыкой")
        return

    if message.content_type != "text":
        if message.photo or message.sticker or message.video or message.animation:
            await _check_media(bot, message, user)
        return

    if not text:
        return

    # --- 2.2: пустые/бессмысленные сообщения ---
    if EMPTY_RE.match(text.strip()):
        await punishments.apply_penalty(bot, user_id, "2.2", "oral", reason="пустое сообщение")
        return

    # --- 2.0: спам командами (3+ подряд) ---
    if text.startswith("/"):
        recent_cmds = [t for t in dq if (now - t).total_seconds() <= config.COMMAND_SPAM_SECONDS]
        if len(recent_cmds) >= config.COMMAND_SPAM_COUNT:
            await punishments.apply_penalty(bot, user_id, "2.0", "perm", reason="спам командами")
        return

    # --- контекстные правила через Gemini (не чаще, чем стоит) ---
    if pure.count_words(text) >= 3:
        context = user.get("last_texts", [])
        result = await gemini_service.analyze_text(text, context)
        await db.set_fields(user_id, {"last_texts": (context + [text])[-10:]})
        if result:
            rule = result["rule"]
            code = config.MODERATION_RULES[rule]["penalty"]
            await punishments.apply_penalty(bot, user_id, rule, code, reason=result.get("reason", ""))


async def _check_media(bot: Bot, message: Message, user: dict) -> None:
    """Проверка изображения через Gemini. Для видео и GIF (animation) Telegram
    присылает готовую картинку-превью (thumbnail) — именно её мы и проверяем,
    а не покадрово весь ролик. Это значит: если что-то недопустимое появляется
    не на превью, а глубже в видео, эта проверка его не поймает — ловит только
    то, что видно на стоп-кадре."""
    file_id, mime = None, "image/jpeg"
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.sticker:
        if not message.sticker.is_animated and not message.sticker.is_video:
            file_id, mime = message.sticker.file_id, "image/webp"
        elif message.sticker.thumbnail:
            file_id = message.sticker.thumbnail.file_id
    elif message.video and message.video.thumbnail:
        file_id = message.video.thumbnail.file_id
    elif message.animation and message.animation.thumbnail:
        file_id = message.animation.thumbnail.file_id
    if not file_id:
        return
    try:
        buf = await bot.download(file_id)
        data = buf.read()
    except Exception:  # noqa: BLE001
        return
    result = await gemini_service.analyze_media(data, mime)
    if not result:
        return

    is_sticker_or_gif = bool(message.sticker or message.animation)
    if result.get("extreme"):
        # ТЗ: «педофилия/порно/нацизм/расчленёнка/нагота — варн 2 шт. или
        # вечный бан, по тяжести». Gemini уже разделяет «extreme» (самое
        # тяжёлое: CSAM/нацизм/расчленёнка) от простой наготы (ниже, nsfw) —
        # поэтому extreme идёт сразу вечным баном, без промежуточного варна.
        await punishments.apply_penalty(bot, message.from_user.id, "3.5", "perm", reason="крайний контент (CSAM/нацизм/расчленёнка)")
    elif result.get("nsfw") and is_sticker_or_gif:
        await punishments.apply_penalty(bot, message.from_user.id, "3.5", "warn2", reason="нагота в стикере/GIF")
    elif result.get("nsfw"):
        has_spoiler = bool(getattr(message, "has_media_spoiler", False))
        caption = (message.caption or "").lower()
        if not has_spoiler and "18+" not in caption:
            await punishments.apply_penalty(bot, message.from_user.id, "3.0", "warn3", reason="NSFW без спойлера/18+")


# ---------------------------------------------------------------------------
# Проверка подписок при первом сообщении в личке (для непринятых)
# ---------------------------------------------------------------------------

@router.message(F.chat.type == "private")
async def catch_all_private(message: Message, bot: Bot):
    if message.from_user.id == config.OWNER_ID:
        return
    if message.text and message.text.startswith("/"):
        return
    from services.utils import tg_user_line
    await log(
        bot,
        f"Сообщение боту в личке от {tg_user_line(message.from_user)}:\n"
        f"{esc(message.text or message.caption or '[медиа]')}",
    )
