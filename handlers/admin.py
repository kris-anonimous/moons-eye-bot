"""
handlers/admin.py — /admin.

Разделы: Админы, Редактирование, Дни рождения, Добавить участника,
Рестники, Участники, Вывод активности, Статус набора, Модерирование,
Чистки, Опросник, Рассылка, О боте.
Разделы, доступные только владельцу, помечены и не добавляются в
клавиатуру обычным админам.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import methods as db
from handlers.menu import birthdays_block, members_block
from roles import ROLE_KEYS, get_role_gender
from services import pure, recruitment, texts
from services.texts import SECTIONS, TEXTS
from services.utils import assign_role_tag, doc_user_line, esc, gform, log, notify_admins, role_of, send, sync_restrictions

router = Router(name="admin")
ROMAN = recruitment.ROMAN


def is_privileged(user: dict) -> bool:
    return bool(user.get("is_admin") or user.get("is_owner"))


async def require_admin(source, bot: Bot) -> dict | None:
    user_id = source.from_user.id
    if user_id == config.OWNER_ID:
        return await db.ensure_owner(source.from_user.username, source.from_user.full_name)

    user = await db.get_user(user_id)
    if user and is_privileged(user):
        return user

    if isinstance(source, Message):
        if user is None:
            await source.answer("Вы не можете вызвать эту команду.")
            await notify_admins(bot, f"Пользователь {source.from_user.id} (не в базе данных) "
                                     f"попытался вызвать /admin в {pure.fmt_msk(db.now())}.")
        else:
            await source.answer("У вас нет прав администратора для вызова этой команды.")
            await notify_admins(bot, f"{doc_user_line(user)} попытался(ась) вызвать /admin в "
                                     f"{pure.fmt_msk(db.now())}.")
    return None


def admin_panel_kb(owner: bool):
    kb = InlineKeyboardBuilder()
    kb.button(text="Админы", callback_data="adm_admins")
    if owner:
        kb.button(text="Редактирование", callback_data="adm_edit")
    kb.button(text="Дни рождения", callback_data="adm_birthdays")
    if owner:
        kb.button(text="Добавить участника", callback_data="adm_addmember")
    kb.button(text="Рестники", callback_data="adm_resters")
    kb.button(text="Участники", callback_data="adm_members")
    kb.button(text="Вывод активности", callback_data="adm_activity")
    if owner:
        kb.button(text="Статус набора", callback_data="adm_recruitment")
    kb.button(text="Модерирование", callback_data="adm_moderation")
    if owner:
        kb.button(text="Чистки", callback_data="adm_purges")
        kb.button(text="Опросник", callback_data="adm_poll")
        kb.button(text="Рассылка", callback_data="adm_broadcast")
    kb.button(text="О боте", callback_data="adm_about")
    kb.adjust(2)
    return kb.as_markup()


@router.message(Command("admin"))
async def cmd_admin(message: Message, bot: Bot):
    user = await require_admin(message, bot)
    if not user:
        return
    await texts.show(message, "admin_greeting", markup=admin_panel_kb(user.get("is_owner", False)), role=role_of(user))


@router.callback_query(F.data == "adm_root")
async def cb_adm_root(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user:
        await callback.answer()
        return
    await texts.show(callback, "admin_greeting", markup=admin_panel_kb(user.get("is_owner", False)), role=role_of(user))
    await callback.answer()


def adm_back_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data="adm_root")
    return kb.as_markup()


# ---------------------------------------------------------------------------
# АДМИНЫ
# ---------------------------------------------------------------------------

class AssignStates(StatesGroup):
    waiting_new = State()
    waiting_dismiss = State()


@router.callback_query(F.data == "adm_admins")
async def cb_adm_admins(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user:
        await callback.answer()
        return
    lines = [f"\n{esc(role_of(a))} — {'владелец' if a.get('is_owner') else 'администратор'}"
             for a in await db.admins_list()]
    kb = InlineKeyboardBuilder()
    if user.get("is_owner"):
        if await db.count_admins() < config.MAX_ADMINS:
            kb.button(text="Назначить админа", callback_data="adm_assign_start")
        kb.button(text="Разжаловать админа", callback_data="adm_dismiss_start")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "admins_header", markup=kb.as_markup(), extra="".join(lines))
    await callback.answer()


@router.callback_query(F.data == "adm_assign_start")
async def cb_assign_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    if await db.count_admins() >= config.MAX_ADMINS:
        await callback.answer(f"Уже назначено максимум администраторов ({config.MAX_ADMINS}).", show_alert=True)
        return
    await state.set_state(AssignStates.waiting_new)
    await callback.message.answer("Отправьте ID пользователя, которого хотите назначить администратором.")
    await callback.answer()


@router.message(AssignStates.waiting_new)
async def process_assign(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    if await db.count_admins() >= config.MAX_ADMINS:
        await message.answer(f"Уже назначено максимум администраторов ({config.MAX_ADMINS}).")
        return

    target = await db.get_user(target_id)
    if not target or not target.get("in_chat") or not target.get("has_bot_access"):
        await message.answer("Этот пользователь не зарегистрирован в базе данных бота или не состоит в чате.")
        return
    joined_at = target.get("joined_chat_at")
    if not joined_at or (db.now() - joined_at).days < config.MIN_DAYS_IN_CHAT_FOR_ADMIN:
        await message.answer(f"Пользователь должен состоять в чате не менее {config.MIN_DAYS_IN_CHAT_FOR_ADMIN} дней.")
        return

    await db.set_fields(target_id, {"is_admin": True})
    try:
        await bot.promote_chat_member(
            config.MAIN_CHAT_ID, target_id,
            can_manage_chat=False, can_change_info=False, can_delete_messages=False, can_invite_users=False,
            can_restrict_members=False, can_pin_messages=False, can_promote_members=False,
            can_manage_topics=False, can_manage_video_chats=True,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        await message.answer(f"Права выданы в базе, но не удалось применить их в Telegram: {e}")

    await message.answer("Администратор успешно назначен.")
    await send(bot, target_id, "Вам выданы права администратора бота. Вызовите /admin, чтобы открыть панель.")
    _actor = await db.get_user(message.from_user.id)
    await log(bot, f"{esc(role_of(_actor))} {gform(_actor, 'назначил', 'назначила')} администратором "
                   f"{doc_user_line(target)}.")


@router.callback_query(F.data == "adm_dismiss_start")
async def cb_dismiss_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    await state.set_state(AssignStates.waiting_dismiss)
    await callback.message.answer("Отправьте ID действующего администратора, которого хотите разжаловать.")
    await callback.answer()


@router.message(AssignStates.waiting_dismiss)
async def process_dismiss(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("ID должен быть числом.")
        return
    await db.set_fields(target_id, {"is_admin": False})
    try:
        await bot.promote_chat_member(config.MAIN_CHAT_ID, target_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        pass
    await message.answer("Права администратора сняты.")
    await send(bot, target_id, "С вас сняты права администратора бота.")
    _actor = await db.get_user(message.from_user.id)
    await log(bot, f"{esc(role_of(_actor))} {gform(_actor, 'снял', 'сняла')} права администратора "
                   f"с пользователя id {target_id}.")


# ---------------------------------------------------------------------------
# РЕДАКТИРОВАНИЕ
# ---------------------------------------------------------------------------

class EditStates(StatesGroup):
    waiting_text = State()


@router.callback_query(F.data == "adm_edit")
async def cb_adm_edit(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for sec, label in SECTIONS.items():
        kb.button(text=label, callback_data=f"edit_sec:{sec}")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "edit_intro", markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("edit_sec:"))
async def cb_edit_section(callback: CallbackQuery):
    section = callback.data.split(":")[1]
    kb = InlineKeyboardBuilder()
    for key, (sec, label, *_r) in TEXTS.items():
        if sec == section:
            kb.button(text=label, callback_data=f"edit_key:{key}")
    kb.button(text="Сбросить все тексты раздела", callback_data=f"edit_reset_sec:{section}")
    kb.button(text="Назад", callback_data="adm_edit")
    kb.adjust(1)
    await callback.message.edit_text(f"{SECTIONS[section]} — выберите текст:", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("edit_reset_sec:"))
async def cb_edit_reset_section(callback: CallbackQuery):
    await texts.reset_texts(section=callback.data.split(":")[1])
    await callback.answer("Все тексты раздела сброшены до значений по умолчанию.", show_alert=True)


@router.callback_query(F.data.startswith("edit_key:"))
async def cb_edit_key(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    _, label, placeholders, default = TEXTS[key]
    current, photos = await texts.load(key)
    await state.set_state(EditStates.waiting_text)
    await state.update_data(key=key)

    kb = InlineKeyboardBuilder()
    for i in range(len(photos)):
        kb.button(text=f"Удалить фото {i + 1}", callback_data=f"edit_delphoto:{key}:{i}")
    if len(photos) > 1:
        kb.button(text="Удалить все фото", callback_data=f"edit_delallphotos:{key}")
    kb.button(text="Сбросить этот текст", callback_data=f"edit_reset_key:{key}")
    kb.adjust(1)

    ph_note = f"\n\nПодстановки: {placeholders}" if placeholders else ""
    photo_note = f"\n\nФото сейчас: {len(photos)}/{config.MAX_TEXT_PHOTOS}" if config.MAX_TEXT_PHOTOS else ""
    await callback.message.answer(
        f"Текущий текст «{label}»:\n\n{current}{ph_note}{photo_note}\n\n"
        f"Отправьте новый текст (форматирование Telegram поддерживается) — можно прямо с фото "
        f"(до {config.MAX_TEXT_PHOTOS}, каждое отдельным сообщением).",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_reset_key:"))
async def cb_edit_reset_key(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    await texts.reset_texts(key=key)
    await state.clear()
    await callback.answer("Текст сброшен до значения по умолчанию.", show_alert=True)


@router.callback_query(F.data.startswith("edit_delphoto:"))
async def cb_edit_delphoto(callback: CallbackQuery):
    _, key, idx = callback.data.split(":")
    await texts.remove_photo(key, int(idx))
    await callback.answer("Фото удалено.")


@router.callback_query(F.data.startswith("edit_delallphotos:"))
async def cb_edit_delallphotos(callback: CallbackQuery):
    key = callback.data.split(":", 1)[1]
    await texts.clear_photos_of(key)
    await callback.answer("Все фото у этого текста удалены.", show_alert=True)


@router.message(EditStates.waiting_text)
async def process_edit_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    key = data["key"]
    body = message.html_text or message.caption_html or message.text or message.caption or ""
    if body.strip():
        await texts.set_text(key, body)
    if message.photo:
        ok = await texts.add_photo(key, message.photo[-1].file_id)
        if not ok:
            await message.answer(f"Уже добавлено максимум фото ({config.MAX_TEXT_PHOTOS}) — сначала удалите лишнее.")
            return
    await message.answer(f"Текст «{TEXTS[key][1]}» обновлён.")
    await log(bot, f"Владелец отредактировал текст «{esc(TEXTS[key][1])}».")


# ---------------------------------------------------------------------------
# ДНИ РОЖДЕНИЯ / УЧАСТНИКИ (переиспользуем блоки из menu.py)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm_birthdays")
async def cb_adm_birthdays(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    await texts.show(callback, "birthdays_header", markup=adm_back_kb(), extra=await birthdays_block())
    await callback.answer()


@router.callback_query(F.data == "adm_members")
async def cb_adm_members(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    await texts.show(callback, "members_header", markup=adm_back_kb(), extra=await members_block())
    await callback.answer()


# ---------------------------------------------------------------------------
# ДОБАВИТЬ УЧАСТНИКА
# ---------------------------------------------------------------------------

class AddMemberStates(StatesGroup):
    waiting_form = State()


@router.callback_query(F.data == "adm_addmember")
async def cb_addmember(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Добавить участника", callback_data="addmember_go")
    await texts.show(callback, "addmember_intro", markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "addmember_go")
async def cb_addmember_go(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    await state.set_state(AddMemberStates.waiting_form)
    await texts.send_key(bot, callback.from_user.id, "addmember_form")
    await callback.answer()


@router.message(AddMemberStates.waiting_form)
async def process_addmember(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    parsed = pure.parse_add_member(message.text or "")
    if not parsed:
        await message.answer("Неверный формат. Пример: Участник: 123456789, Роль: Наруто, День рождения: 10.10")
        return
    target_id, role, birthday = parsed
    if role not in ROLE_KEYS:
        await message.answer("Такой роли нет в списке персонажей.")
        return
    if await db.role_taken(role):
        await message.answer("Эта роль уже занята.")
        return
    if not pure.parse_dd_mm(birthday, pure.to_msk(db.now()).year):
        await message.answer("Некорректная дата рождения.")
        return

    existing = await db.get_user(target_id)
    if not existing:
        await db.ensure_user(target_id, None, "Добавлен(а) вручную")
    await db.set_fields(target_id, {
        "role_key": role, "gender": get_role_gender(role), "birthday": birthday,
        "in_chat": True, "has_bot_access": True, "status": db.ST_MEMBER,
        "joined_chat_at": db.now(), "last_message_at": db.now(),
    })
    await db.mark_ever_member(target_id)
    await recruitment.register_manual_add(target_id)
    await assign_role_tag(bot, target_id, role)

    await message.answer("Участник успешно принят.")
    fresh = await db.get_user(target_id)
    await notify_admins(
        bot,
        f"Добавлен новый участник! Дата: {pure.fmt_msk(db.now(), with_time=False)}\n\n"
        f"Роль: {esc(role)}\n\nДень Рождения: {esc(birthday)}\n\nЮзернейм: {doc_user_line(fresh)}",
    )
    await send(bot, config.MAIN_CHAT_ID, f"{gform(role, 'Добавлен новый Участник', 'Добавлена новая Участница')} – {esc(role)}.")


# ---------------------------------------------------------------------------
# РЕСТНИКИ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm_resters")
async def cb_resters(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    resters, banned = await db.active_resters(), await db.rest_banned_users()
    kb = InlineKeyboardBuilder()
    for r in resters:
        kb.button(text=f"{r.get('role_key')} (рест)", callback_data=f"rester_info:{r['_id']}")
    for b in banned:
        kb.button(text=f"{b.get('role_key')} (запрет)", callback_data=f"rester_ban_info:{b['_id']}")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    extra = (f"\n\n{', '.join(esc(r.get('role_key','—')) for r in resters) or 'сейчас никто не в ресте…'}"
             f"\n\nЗапрет на рест:\n{', '.join(esc(b.get('role_key','—')) for b in banned) or 'нет ограничений'}")
    await texts.show(callback, "resters_header", markup=kb.as_markup(), extra=extra)
    await callback.answer()


@router.callback_query(F.data.startswith("rester_info:"))
async def cb_rester_info(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user:
        await callback.answer()
        return
    r = await db.get_user(int(callback.data.split(":")[1]))
    if not r:
        await callback.answer("Участник уже вышел из чата.", show_alert=True)
        return
    rest = r["rest"]
    days_in = (db.now() - rest["rest_start"]).days
    days_left = max((rest["rest_end"] - db.now()).days, 0)
    kb = InlineKeyboardBuilder()
    if user.get("is_owner"):
        kb.button(text="Отменить рест", callback_data=f"rester_cancel:{r['_id']}")
    kb.button(text="Назад", callback_data="adm_resters")
    kb.adjust(1)
    await callback.message.edit_text(
        f"Рестник: {esc(r.get('role_key'))}\n\n"
        f"Рест с {pure.fmt_msk(rest['rest_start'], False)} по {pure.fmt_msk(rest['rest_end'], False)};\n"
        f"Дней в ресте: {days_in};\nВыйдет через: {days_left} дн.;\n"
        f"Всего {gform(r, 'брал', 'брала')} рестов: {rest['rest_count']}",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rester_cancel:"))
async def cb_rester_cancel(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    target_id = int(callback.data.split(":")[1])
    target = await db.get_user(target_id)
    if not target:
        await callback.answer()
        return
    await db.end_rest(target_id)
    await sync_restrictions(bot, target_id)
    await assign_role_tag(bot, target_id, target["role_key"], resting=False)
    await texts.send_key(bot, target_id, "rest_cancelled_dm")
    gw = "его" if target.get("gender") != "ж" else "её"
    await send(bot, config.MAIN_CHAT_ID,
              f"{esc(target['role_key'])} выходит из реста досрочно. Узнайте, как прошёл {gw} отдых!")
    await callback.answer("Рест отменён.")


@router.callback_query(F.data.startswith("rester_ban_info:"))
async def cb_rester_ban_info(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user:
        await callback.answer()
        return
    r = await db.get_user(int(callback.data.split(":")[1]))
    if not r:
        await callback.answer("Участник уже вышел из чата.", show_alert=True)
        return
    rest = r["rest"]
    days_left = max((rest["rest_ban_until"] - db.now()).days, 0)
    kb = InlineKeyboardBuilder()
    limit = rest.get("rest_count", 1) + 1
    if user.get("is_owner") and rest.get("rest_ban_extensions", 0) < limit:
        kb.button(text="Продлить запрет", callback_data=f"rester_extend:{r['_id']}")
    kb.button(text="Назад", callback_data="adm_resters")
    kb.adjust(1)
    ban_days = (rest["rest_ban_until"] - rest["rest_ban_start"]).days if rest.get("rest_ban_start") else config.REST_BAN_DAYS
    rest_period = ""
    if rest.get("last_start") and rest.get("last_end"):
        rest_period = (f"{gform(r, 'Был', 'Была')} в ресте с {pure.fmt_msk(rest['last_start'], False)} по "
                       f"{pure.fmt_msk(rest['last_end'], False)};\n\n")
    await callback.message.edit_text(
        f"Вышедший: {esc(r.get('role_key'))}\n\n"
        f"{rest_period}"
        f"Дней в запрете: {ban_days};\n\n"
        f"Дней до возможности брать рест: {days_left};\n"
        f"Всего {gform(r, 'брал', 'брала')} рестов: {rest['rest_count']}",
        reply_markup=kb.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rester_extend:"))
async def cb_rester_extend(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    ok = await db.extend_rest_ban(int(callback.data.split(":")[1]))
    await callback.answer("Запрет продлён на 7 дней." if ok else "Лимит продлений исчерпан.", show_alert=not ok)


# ---------------------------------------------------------------------------
# ВЫВОД АКТИВНОСТИ
# ---------------------------------------------------------------------------

class ActivityStates(StatesGroup):
    waiting_target = State()


async def activity_block() -> str:
    lines = []
    for label, period in [("день", 1), ("неделю", 7), ("месяц", "month")]:
        top = await db.top_active(period, limit=3)
        lines.append(f"\nТоп-3 за {label}:")
        lines += [f"{i}. {esc(role_of(u))} — {c}" for i, (u, c) in enumerate(top, 1)] or ["(пусто)"]
    top_all = await db.top_active(None, limit=3)
    lines.append("\nТоп-3 за всё время:")
    lines += [f"{i}. {esc(role_of(u))} — {c}" for i, (u, c) in enumerate(top_all, 1)]
    return "\n".join(lines)


async def period_stat_report(period, label: str) -> str:
    """Ответ на /weekly_stat, /monthly_stat, /all_stat: сообщения за период +
    топ-3 самых активных БЕЗ порога по количеству сообщений (в отличие от
    отчёта о чистке, где третье место показывается только при достаточной
    активности) — именно так это описано в ТЗ. period: 7 / "month" / None."""
    total, _, _ = await db.totals(period)
    top = await db.top_active(period, limit=3)
    lines = [f"Статистика сообщений за {label}: {total}", "", f"3 самых активных участника за {label}:"]
    lines += [f"{i}. {esc(role_of(u))} — {c}" for i, (u, c) in enumerate(top, 1)] or ["(пока нет данных)"]
    return "\n".join(lines)


@router.message(Command("weekly_stat"), F.chat.type == "private", F.from_user.id == config.OWNER_ID)
async def cmd_weekly_stat(message: Message):
    await message.answer(await period_stat_report(7, "неделю"))


@router.message(Command("monthly_stat"), F.chat.type == "private", F.from_user.id == config.OWNER_ID)
async def cmd_monthly_stat(message: Message):
    await message.answer(await period_stat_report("month", "месяц"))


@router.message(Command("all_stat"), F.chat.type == "private", F.from_user.id == config.OWNER_ID)
async def cmd_all_stat(message: Message):
    await message.answer(await period_stat_report(None, "всё время"))


@router.callback_query(F.data == "adm_activity")
async def cb_activity(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Частная статистика", callback_data="activity_private")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "activity_header", markup=kb.as_markup(), extra=await activity_block())
    await callback.answer()


@router.callback_query(F.data == "activity_private")
async def cb_activity_private(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ActivityStates.waiting_target)
    await callback.message.answer("Впишите роль или id того, чью статистику вы хотите посмотреть:")
    await callback.answer()


@router.message(ActivityStates.waiting_target)
async def process_activity_target(message: Message, state: FSMContext):
    await state.clear()
    query = (message.text or "").strip()
    user_doc = await db.find_by_role(query) if query in ROLE_KEYS else None
    target_id = None
    if user_doc is None:
        try:
            target_id = int(query)
            user_doc = await db.get_user(target_id)
        except ValueError:
            await message.answer("Введите корректную роль или числовой id.")
            return
    else:
        target_id = user_doc["_id"]

    label = "ваша статистика" if target_id == message.from_user.id else (
        role_of(user_doc) if user_doc and user_doc.get("in_chat") else "неизвестно"
    )
    stat = await db.get_stat(target_id)
    total = db.window_count(stat, None)
    first_seen = (user_doc or {}).get("first_seen") or (stat or {}).get("first_seen")

    text = f"Роль: {esc(label)}\n\nВсего сообщений: {total}\n\n"
    if first_seen:
        text += f"Первое появление: {pure.fmt_msk(first_seen, False)}\n\n"
    if user_doc and user_doc.get("in_chat"):
        weekly, monthly = db.window_count(stat, 7), db.window_count(stat, "month")
        s = await db.get_purge_settings()
        text += f"Набрал ли недельную норму сообщений: {'да' if weekly >= s['weekly_norm'] else 'нет'}\n\n"
        text += f"Набрал ли месячную норму сообщений: {'да' if monthly >= s['monthly_norm'] else 'нет'}"

    await message.answer(text)


# ---------------------------------------------------------------------------
# СТАТУС НАБОРА
# ---------------------------------------------------------------------------

class RecruitStates(StatesGroup):
    waiting_slots = State()


@router.callback_query(F.data == "adm_recruitment")
async def cb_recruitment(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Открыть набор", callback_data="rec_open")
    kb.button(text="Закрыть набор", callback_data="rec_close")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await callback.message.edit_text(await recruitment.status_text(), reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "rec_open")
async def cb_rec_open(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    rec = await db.get_recruitment()
    if rec.get("wait_mode"):
        msg = await recruitment.open_recruitment(bot, 0)
        await callback.message.answer(msg)
        await callback.answer()
        return
    await state.set_state(RecruitStates.waiting_slots)
    cap = config.WAVE_MAX_SLOTS[rec["wave"] if rec.get("ever_opened") else 1]
    await callback.message.answer(f"Новая Фаза / Волна.\nУкажите доступное кол-во мест (максимум {cap}):")
    await callback.answer()


@router.message(RecruitStates.waiting_slots)
async def process_rec_slots(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    try:
        slots = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число.")
        return
    reply = await recruitment.open_recruitment(bot, slots)
    await message.answer(reply)
    await log(bot, f"Владелец открыл набор: {esc(reply)}")


@router.callback_query(F.data == "rec_close")
async def cb_rec_close(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    await db.set_recruitment({"status": "closed"})
    await callback.message.answer("Набор закрыт.")
    await log(bot, "Владелец закрыл набор.")
    await callback.answer()


# ---------------------------------------------------------------------------
# МОДЕРИРОВАНИЕ
# ---------------------------------------------------------------------------

async def moderation_block() -> str:
    lines = []
    for label, days in [("день", 1), ("неделю", 7), ("месяц", "month")]:
        viol = await db.violations_since(days if isinstance(days, int) else None)
        if days == "month":
            # фильтруем по календарному месяцу отдельно, т.к. violations_since
            # принимает только «последние N дней» либо «всё время»
            from services.pure import to_msk
            first = to_msk(db.now()).date().replace(day=1)
            viol = [v for v in viol if to_msk(v["at"]).date() >= first]
        lines.append(f"\nНарушений за {label}: {len(viol)}")
    viol_all = await db.violations_since(None)
    lines.append(f"\nНарушений за всё время: {len(viol_all)}")
    lines.append(f"\nВсего варнов сейчас в силе: {sum(u.get('warns', 0) for u in await db.members())}")
    return "\n".join(lines)


@router.callback_query(F.data == "adm_moderation")
async def cb_moderation(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Частная статистика", callback_data="moderation_private")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "moderation_header", markup=kb.as_markup(), extra=await moderation_block())
    await callback.answer()


class ModPrivateStates(StatesGroup):
    waiting = State()


@router.callback_query(F.data == "moderation_private")
async def cb_moderation_private(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ModPrivateStates.waiting)
    await callback.message.answer("Впишите роль участника:")
    await callback.answer()


@router.message(ModPrivateStates.waiting)
async def process_moderation_private(message: Message, state: FSMContext):
    await state.clear()
    role = (message.text or "").strip()
    user_doc = await db.find_by_role(role)
    if not user_doc:
        await message.answer("Участник с такой ролью не найден.")
        return
    rules = await db.rules_of_user(user_doc["_id"])
    await message.answer(
        f"Роль: {esc(role)}\n\n"
        f"Нарушения по: {', '.join(rules) if rules else 'нарушений нет'}\n\n"
        f"Варны: {user_doc.get('warns', 0)} (до бана: 4)\n"
        f"Устные предупреждения: {user_doc.get('oral_warns', 0)} (до варна: 3)"
    )


# ---------------------------------------------------------------------------
# ЧИСТКИ
# ---------------------------------------------------------------------------

class PurgeStates(StatesGroup):
    weekly_norm = State()
    monthly_norm = State()
    weekly_time = State()
    monthly_time = State()


async def purges_block() -> str:
    s = await db.get_purge_settings()
    return (
        f"\n\nНедельная: проводится в {esc(s['weekly_time'])} в субботу по МСК, строго от "
        f"{config.WEEKLY_PURGE_MIN_PARTICIPANTS} и более участников, не считая админов.\n\n"
        f"Месячная: проводится в {esc(s['monthly_time'])} по МСК последнего числа месяца, строго от "
        f"{config.MONTHLY_PURGE_MIN_PARTICIPANTS} и более участников, не считая админов.\n\n"
        f"Норма сообщений в неделю: {s['weekly_norm']}\n\nНорма сообщений в месяц: {s['monthly_norm']}\n\n"
        f"Дней неактивности до бана: {config.INACTIVITY_KICK_DAYS}\n\n"
        f"Дней неактивности после реста до бана: {config.REST_INACTIVITY_KICK_DAYS}\n\n"
        f"Режим чистки: {'включён' if s['enabled'] else 'выключен'}"
    )


@router.callback_query(F.data == "adm_purges")
async def cb_purges(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    s = await db.get_purge_settings()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Режим чистки ({'выкл' if s['enabled'] else 'вкл'})", callback_data="purge_toggle")
    kb.button(text="Изменить недельную норму", callback_data="purge_edit_weekly")
    kb.button(text="Изменить месячную норму", callback_data="purge_edit_monthly")
    kb.button(text="Изменить время чисток", callback_data="purge_edit_time")
    kb.button(text="Статистика чисток", callback_data="purge_stats")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "purges_header", markup=kb.as_markup(), extra=await purges_block())
    await callback.answer()


@router.callback_query(F.data == "purge_toggle")
async def cb_purge_toggle(callback: CallbackQuery):
    s = await db.get_purge_settings()
    await db.set_purge_settings({"enabled": not s["enabled"]})
    await callback.answer("Режим чистки переключён.")
    await log(callback.bot, f"Режим чистки переключён на: {'выкл' if s['enabled'] else 'вкл'}.")


@router.callback_query(F.data == "purge_edit_weekly")
async def cb_purge_weekly(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PurgeStates.weekly_norm)
    await callback.message.answer("Введите новую недельную норму сообщений (число):")
    await callback.answer()


@router.message(PurgeStates.weekly_norm)
async def process_purge_weekly(message: Message, state: FSMContext):
    await state.clear()
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число.")
        return
    await db.set_purge_settings({"weekly_norm": value})
    await message.answer(f"Недельная норма установлена: {value}")
    await log(message.bot, f"Недельная норма сообщений изменена на {value}.")


@router.callback_query(F.data == "purge_edit_monthly")
async def cb_purge_monthly(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PurgeStates.monthly_norm)
    await callback.message.answer("Введите новую месячную норму сообщений (число):")
    await callback.answer()


@router.message(PurgeStates.monthly_norm)
async def process_purge_monthly(message: Message, state: FSMContext):
    await state.clear()
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите число.")
        return
    await db.set_purge_settings({"monthly_norm": value})
    await message.answer(f"Месячная норма установлена: {value}")
    await log(message.bot, f"Месячная норма сообщений изменена на {value}.")


@router.callback_query(F.data == "purge_edit_time")
async def cb_purge_time(callback: CallbackQuery, state: FSMContext):
    await state.set_state(PurgeStates.weekly_time)
    await callback.message.answer("Введите время недельной чистки по МСК в формате ЧЧ:ММ (например, 18:00):")
    await callback.answer()


@router.message(PurgeStates.weekly_time)
async def process_purge_time_weekly(message: Message, state: FSMContext, scheduler):
    parsed = pure.parse_hhmm((message.text or "").strip())
    if not parsed:
        await message.answer("Неверный формат. Пример: 18:00")
        return
    await db.set_purge_settings({"weekly_time": f"{parsed[0]:02d}:{parsed[1]:02d}"})
    from services.scheduler import reschedule_purge_job
    reschedule_purge_job(scheduler, "weekly", parsed[0], parsed[1])
    await state.set_state(PurgeStates.monthly_time)
    await message.answer("Применено сразу. Теперь время месячной чистки по МСК (ЧЧ:ММ):")


@router.message(PurgeStates.monthly_time)
async def process_purge_time_monthly(message: Message, state: FSMContext, scheduler):
    await state.clear()
    parsed = pure.parse_hhmm((message.text or "").strip())
    if not parsed:
        await message.answer("Неверный формат. Пример: 19:00")
        return
    await db.set_purge_settings({"monthly_time": f"{parsed[0]:02d}:{parsed[1]:02d}"})
    from services.scheduler import reschedule_purge_job
    reschedule_purge_job(scheduler, "monthly", parsed[0], parsed[1])
    await message.answer("Время сохранено и применено сразу, без перезапуска бота.")
    await log(message.bot, "Время чисток изменено.")


@router.callback_query(F.data == "purge_stats")
async def cb_purge_stats(callback: CallbackQuery):
    s = await db.get_purge_stats()
    await callback.message.answer(
        f"Статистика чисток:\n\nПроведенных чисток: {s.get('conducted', 0)}\n"
        f"Пропущенных чисток: {s.get('skipped', 0)}"
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# ОПРОСНИК
# ---------------------------------------------------------------------------

class PollStates(StatesGroup):
    waiting_form = State()


@router.callback_query(F.data == "adm_poll")
async def cb_poll(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    await state.set_state(PollStates.waiting_form)
    text = (await texts.text_of("poll_intro")) + "\n\n" + (await texts.text_of("poll_form"))
    await callback.message.answer(text)
    await callback.answer()


@router.message(PollStates.waiting_form)
async def process_poll_form(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    parsed = pure.parse_poll_form(message.text or "")
    if not parsed:
        await message.answer(
            "Не удалось разобрать анкету. Пример:\nЗаголовок: Любимый арк, варианты ответов: "
            "1. Чунин Экзамен 2. Поиск Цунаде 3. Спасение Гаары. Викторина: нет. Время опроса: 3 дня"
        )
        return

    expires_at = db.now() + parsed["duration"]
    poll_id = await db.create_poll({
        "title": parsed["title"], "options": parsed["options"], "is_quiz": parsed["is_quiz"],
        "created_at": db.now(), "expires_at": expires_at, "votes": {}, "messages": {}, "tg_polls": {},
        "closed": False, "correct": parsed["correct"],
    })

    poll_doc = await db.get_poll(poll_id)
    sent = sum([await send_poll_to_member(bot, u["_id"], poll_doc) for u in await db.members(with_role=True)])
    await message.answer(f"Опрос создан и разослан {sent} участникам.")


async def send_poll_to_member(bot: Bot, user_id: int, poll: dict) -> bool:
    try:
        msg = await bot.send_poll(
            user_id, question=poll["title"], options=poll["options"], is_anonymous=False,
            type="quiz" if poll["is_quiz"] else "regular",
            correct_option_id=poll["correct"] if poll["is_quiz"] else None,
        )
        await db.set_poll_message(str(poll["_id"]), user_id, msg.message_id, msg.poll.id)
        return True
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


@router.poll_answer()
async def on_poll_answer(poll_answer, bot: Bot):
    poll = await db.find_poll_by_tg(poll_answer.poll_id)
    if not poll:
        return
    option = poll_answer.option_ids[0] if poll_answer.option_ids else None
    if option is None:
        return
    await db.record_vote(poll["_id"], poll_answer.user.id, option)
    # ТЗ: «Опросник должен удаляться у тех, кто уже выбрал свой вариант ответа» —
    # не дожидаясь общего закрытия опроса по истечении срока.
    mid = poll.get("messages", {}).get(str(poll_answer.user.id))
    if mid:
        try:
            await bot.delete_message(poll_answer.user.id, mid)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass


# ---------------------------------------------------------------------------
# РАССЫЛКА
# ---------------------------------------------------------------------------

class BroadcastStates(StatesGroup):
    waiting_text = State()
    confirming = State()


BROADCAST_KEYS = {"members": "broadcast_members", "unregistered": "broadcast_unreg",
                  "all": "broadcast_all", "chat": "broadcast_chat"}


@router.callback_query(F.data == "adm_broadcast")
async def cb_broadcast(callback: CallbackQuery, bot: Bot):
    user = await require_admin(callback, bot)
    if not user or not user.get("is_owner"):
        await callback.answer("Доступно только владельцу.", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="Только участникам", callback_data="bc_kind:members")
    kb.button(text="Только незарегистрированным", callback_data="bc_kind:unregistered")
    kb.button(text="Только в чат", callback_data="bc_kind:chat")
    kb.button(text="Всем", callback_data="bc_kind:all")
    kb.button(text="Назад", callback_data="adm_root")
    kb.adjust(1)
    await texts.show(callback, "broadcast_choose", markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("bc_kind:"))
async def cb_bc_kind(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.split(":")[1]
    if await db.broadcasts_today(kind) >= config.MAX_BROADCASTS_PER_DAY:
        await callback.answer(f"За сегодня уже достигнут лимит в {config.MAX_BROADCASTS_PER_DAY} рассылок этого типа.", show_alert=True)
        return
    await state.set_state(BroadcastStates.waiting_text)
    await state.update_data(kind=kind, photos=[])
    await texts.show(callback, BROADCAST_KEYS[kind])
    await callback.answer()


@router.message(BroadcastStates.waiting_text)
async def process_broadcast_text(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = list(data.get("photos", []))
    if message.photo:
        if len(photos) >= config.MAX_BROADCAST_PHOTOS:
            await message.answer(f"Уже добавлено максимум фото ({config.MAX_BROADCAST_PHOTOS}).")
            return
        photos.append(message.photo[-1].file_id)
        body = message.html_text or message.caption_html or data.get("text", "")
        await state.update_data(photos=photos, text=body)
        await message.answer(f"Фото добавлено ({len(photos)}/{config.MAX_BROADCAST_PHOTOS}). "
                             f"Пришлите ещё фото или отправьте текст без фото, чтобы перейти к предпросмотру.")
        return

    text = message.html_text or data.get("text", "") or ""
    await state.update_data(text=text)
    await state.set_state(BroadcastStates.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="bc_send")
    kb.button(text="Редактировать", callback_data="bc_edit")
    kb.button(text="Отменить", callback_data="bc_cancel")
    kb.button(text="Назад", callback_data="bc_back")
    kb.adjust(1)
    await message.answer(f"Предпросмотр рассылки ({len(photos)} фото):\n\n{text}", reply_markup=kb.as_markup())


@router.callback_query(F.data == "bc_edit", BroadcastStates.confirming)
async def cb_bc_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BroadcastStates.waiting_text)
    await callback.message.answer("Отправьте новый текст рассылки.")
    await callback.answer()


@router.callback_query(F.data == "bc_back")
async def cb_bc_back(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb_broadcast(callback, bot)


@router.callback_query(F.data == "bc_cancel")
async def cb_bc_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.answer("Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "bc_send", BroadcastStates.confirming)
async def cb_bc_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    kind, text, photos = data["kind"], data.get("text", ""), data.get("photos", [])
    await state.clear()

    async def deliver(chat_id: int):
        await texts.send_with_photos(bot, chat_id, text, photos)

    sent_count = 0
    if kind == "chat":
        await deliver(config.MAIN_CHAT_ID)
        sent_count = 1
    else:
        targets: set[int] = set()
        if kind in ("members", "all"):
            targets |= {u["_id"] for u in await db.members()}
        if kind in ("unregistered", "all"):
            targets |= {u["_id"] for u in await db.unregistered_users()}
        for uid in targets:
            await deliver(uid)
        sent_count = len(targets)

    await db.inc_broadcasts_today(kind)
    kind_label = {"chat": "в общий чат", "members": "участникам", "unregistered": "незарегистрированным",
                  "all": "всем"}[kind]
    await log(bot, f"Рассылка ({kind_label}, получателей: {sent_count}):\n\n{text}")
    await callback.message.answer("Рассылка выполнена.")
    await callback.answer()


# ---------------------------------------------------------------------------
# О БОТЕ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "adm_about")
async def cb_about(callback: CallbackQuery, bot: Bot):
    if not await require_admin(callback, bot):
        await callback.answer()
        return
    await texts.show(callback, "about_admin", markup=adm_back_kb())
    await callback.answer()
