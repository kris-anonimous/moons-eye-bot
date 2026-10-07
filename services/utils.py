"""
services/utils.py — общие функции для всех обработчиков.

* send()/main_chat()/notify_admins() — отправка сообщений с копией в чат логов
  (ТЗ: «все действия бота логируются»).
* set_member_tag() — тег участника (Bot API 9.5, setChatMemberTag).
* sync_restrictions() — единая точка, решающая, можно ли участнику писать
  (капча, рест, мут, отписка от канала, блокировка бота).
* build_everyone() — созыв «по смайликам».
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods.base import TelegramMethod
from aiogram.types import ChatPermissions, Message

import config
from database import methods as db
from roles import get_role_gender
from services import pure

logger = logging.getLogger(__name__)
esc = html.escape

EMOJIS = ["🌙", "⭐", "🔥", "🌸", "⚡", "🍃", "💧", "🌊", "🗻", "🦊", "🐍", "🦅", "🌑", "☀️", "🍀", "🎴", "🗡", "🌀", "❄️", "🌪"]


def aware(t: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """naive UTC -> aware UTC (aiogram иначе трактует naive как локальное время)."""
    return t.replace(tzinfo=dt.timezone.utc) if t is not None and t.tzinfo is None else t


# ---------------------------------------------------------------------------
# Отправка
# ---------------------------------------------------------------------------

async def _raw_send(bot: Bot, chat_id: int, text: str, **kw) -> Optional[Message]:
    for attempt in range(2):
        try:
            return await bot.send_message(chat_id, text[:4096], **kw)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning("Не удалось отправить сообщение в %s: %s", chat_id, e)
            return None
    return None


async def log(bot: Bot, text: str, **kw) -> Optional[Message]:
    return await _raw_send(bot, config.LOGS_CHAT_ID, text, **kw)


async def send(bot: Bot, chat_id: int, text: str, *, to_log: bool = True, **kw) -> Optional[Message]:
    """Отправить сообщение и продублировать его в чат логов."""
    msg = await _raw_send(bot, chat_id, text, **kw)
    if to_log and chat_id != config.LOGS_CHAT_ID:
        label = "общий чат" if chat_id == config.MAIN_CHAT_ID else f"ЛС {chat_id}"
        await log(bot, f"<i>→ {label}</i>\n{text}", disable_notification=True)
    return msg


async def main_chat(bot: Bot, text: str, **kw) -> Optional[Message]:
    return await send(bot, config.MAIN_CHAT_ID, text, **kw)


async def notify_admins(bot: Bot, text: str, reply_markup=None, *, include_owner: bool = True,
                        log_it: bool = True) -> None:
    """Личное сообщение всем админам (и владельцу) + одна копия в чат логов."""
    for a in await db.admins_list():
        if a.get("is_owner") and not include_owner:
            continue
        await _raw_send(bot, a["_id"], text, reply_markup=reply_markup)
    if log_it:
        await log(bot, text, disable_notification=True)


async def notify_owner(bot: Bot, text: str, reply_markup=None) -> None:
    """Личное сообщение ТОЛЬКО владельцу + копия в чат логов — для отчётов
    о посещении и блокировке бота, которые по ТЗ не идут остальным админам."""
    await _raw_send(bot, config.OWNER_ID, text, reply_markup=reply_markup)
    await log(bot, text, disable_notification=True)


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def tg_user_line(user) -> str:
    """Имя, @юз и id для aiogram.types.User."""
    un = f"@{user.username}" if user.username else "без юзернейма"
    return f"{esc(user.full_name)}, {esc(un)}, id {user.id}"


def doc_user_line(u: dict) -> str:
    un = f"@{u['username']}" if u.get("username") else "без юзернейма"
    return f"{esc(u.get('full_name') or '—')}, {esc(un)}, id {u['_id']}"


def role_of(u: Optional[dict], fallback: str = "участник") -> str:
    if not u:
        return fallback
    return u.get("role_key") or ("Владелец" if u.get("is_owner") else fallback)


def gender_of(who) -> str:
    """Пол персонажа: who — запись пользователя (dict) или ключ роли (str). По умолчанию «м»."""
    if isinstance(who, dict):
        g = who.get("gender")
        if g in ("м", "ж"):
            return g
        role_key = who.get("role_key")
        return get_role_gender(role_key) if role_key else "м"
    if isinstance(who, str) and who:
        return get_role_gender(who)
    return "м"


def gform(who, male: str, female: str) -> str:
    """Форма слова по полу роли: gform(user, 'исключён', 'исключена')."""
    return female if gender_of(who) == "ж" else male


def is_main_thread(message: Message) -> bool:
    """Бот работает только в главной ветке чата (не в темах форума)."""
    return not message.message_thread_id or not message.is_topic_message


def is_night_msk(t: Optional[dt.datetime] = None) -> bool:
    h = pure.to_msk(t or db.now()).hour
    return pure.night_window_active(config.NIGHT_MODE_START_HOUR, config.NIGHT_MODE_END_HOUR, h)


# ---------------------------------------------------------------------------
# Теги участников
# ---------------------------------------------------------------------------

class _SetChatMemberTag(TelegramMethod[bool]):
    """Запасной вариант, если в установленной версии aiogram нет метода."""
    __returning__ = bool
    __api_method__ = "setChatMemberTag"
    chat_id: int
    user_id: int
    tag: Optional[str] = None


async def set_member_tag(bot: Bot, user_id: int, tag: str) -> bool:
    try:
        if hasattr(bot, "set_chat_member_tag"):
            await bot.set_chat_member_tag(config.MAIN_CHAT_ID, user_id, tag)
        else:
            await bot(_SetChatMemberTag(chat_id=config.MAIN_CHAT_ID, user_id=user_id, tag=tag))
        return True
    except Exception as e:  # noqa: BLE001 — тег не должен ронять основную логику
        logger.warning("Не удалось выдать тег %r пользователю %s: %s", tag, user_id, e)
        return False


async def assign_role_tag(bot: Bot, user_id: int, role: str, resting: bool = False) -> bool:
    return await set_member_tag(bot, user_id, pure.build_tag_text(role, resting, config.TAG_MAX_LEN))


# ---------------------------------------------------------------------------
# Права и ограничения
# ---------------------------------------------------------------------------

async def default_permissions(bot: Bot) -> ChatPermissions:
    try:
        chat = await bot.get_chat(config.MAIN_CHAT_ID)
        if chat.permissions is not None:
            return ChatPermissions(**chat.permissions.model_dump(exclude_none=True))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось получить права чата: %s", e)
    return ChatPermissions(
        can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
        can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True,
        can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True,
    )


async def sync_restrictions(bot: Bot, user_id: int) -> None:
    """Привести права участника в соответствие с его состоянием.

    Писать нельзя, если: не пройдена капча, участник в ресте, активен мут,
    нет подписки на каналы, участник заблокировал бота.
    Иначе — выставляются текущие права чата по умолчанию (в т.ч. ночные).
    """
    u = await db.get_user(user_id)
    if not u or u.get("is_owner"):
        return
    muted_until = u.get("muted_until")
    mute_active = bool(muted_until and muted_until > db.now())
    hard = bool(
        u.get("captcha_deadline") or u["rest"]["resting"]
        or not u.get("subscribed_ok", True) or u.get("blocked_bot")
    )
    try:
        if hard or mute_active:
            await bot.restrict_chat_member(
                config.MAIN_CHAT_ID, user_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=None if hard else aware(muted_until),
            )
        else:
            await bot.restrict_chat_member(config.MAIN_CHAT_ID, user_id, permissions=await default_permissions(bot))
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.info("Не удалось изменить права %s (возможно, это админ): %s", user_id, e)


async def kick_member(bot: Bot, user_id: int) -> None:
    """Исключение: пользователь может вступить снова (бан + разбан)."""
    try:
        await bot.ban_chat_member(config.MAIN_CHAT_ID, user_id)
        await bot.unban_chat_member(config.MAIN_CHAT_ID, user_id, only_if_banned=True)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось исключить %s: %s", user_id, e)


async def ban_everywhere(bot: Bot, user_id: int) -> None:
    """Бан на всех площадках: общий чат, обязательные каналы и чёрный список
    САМОГО бота — обязательно создаёт/обновляет запись пользователя с
    bot_banned=True, иначе незарегистрированный рейдер мог бы потом просто
    нажать /start и бот обработал бы его как нового человека."""
    for chat_id in [config.MAIN_CHAT_ID, *config.REQUIRED_CHANNEL_IDS]:
        try:
            await bot.ban_chat_member(chat_id, user_id)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.warning("Не удалось забанить %s в %s: %s", user_id, chat_id, e)
    await db.ensure_user(user_id, None, "—")
    await db.set_fields(user_id, {
        "bot_banned": True, "status": db.ST_BANNED, "in_chat": False, "has_bot_access": False,
    })


# ---------------------------------------------------------------------------
# /everyone
# ---------------------------------------------------------------------------

def build_everyone(members: list[dict], text: str = "") -> list[str]:
    """Созыв «по смайликам»: каждый участник — ссылка-упоминание на эмодзи."""
    mentions = [
        f'<a href="tg://user?id={m["_id"]}">{EMOJIS[i % len(EMOJIS)]}</a>' for i, m in enumerate(members)
    ]
    chunks = ["".join(mentions[i:i + 40]) for i in range(0, len(mentions), 40)] or [""]
    chunks[0] = f"{chunks[0]}\n{text}".strip()
    return chunks


async def send_everyone(bot: Bot, text: str) -> None:
    members = await db.members()
    for chunk in build_everyone(members, text):
        await main_chat(bot, chunk)


# ---------------------------------------------------------------------------
# Подписки
# ---------------------------------------------------------------------------

async def check_subscriptions(bot: Bot, user_id: int) -> bool:
    for channel_id in config.REQUIRED_CHANNEL_IDS:
        try:
            m = await bot.get_chat_member(channel_id, user_id)
            if m.status in ("left", "kicked"):
                return False
        except (TelegramBadRequest, TelegramForbiddenError):
            return False
    return True


def twink_guess(user_id: int) -> str:
    days = pure.estimate_account_age_days(user_id, db.now(), config.USER_ID_DATE_ANCHORS)
    return "да" if days is not None and days < config.TWINK_MAX_AGE_DAYS else "нет"
