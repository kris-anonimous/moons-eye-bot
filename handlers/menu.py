"""
handlers/menu.py — панель участника (/menu, и /start для тех, кто уже в чате).

У админов те же кнопки, но без «Поддержка»/«Жалобная»/«взять рест»
(ТЗ: кнопки остаются видимыми, но без возможности взаимодействия —
чтобы было видно, как выглядит раздел после редактирования).
"""

from __future__ import annotations

import datetime as dt

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import methods as db
from services import pure, texts
from services.utils import assign_role_tag, esc, gform, main_chat, notify_admins, role_of, sync_restrictions

router = Router(name="menu")

MONTHS_RU = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
             "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]


class ComplaintStates(StatesGroup):
    choosing_target = State()
    writing = State()
    confirming = State()


class SupportStates(StatesGroup):
    writing_topic = State()
    writing_body = State()
    confirming = State()


class SupportReplyStates(StatesGroup):
    writing = State()


class RestStates(StatesGroup):
    entering_date = State()


def is_limited(user: dict) -> bool:
    return bool(user.get("is_admin") or user.get("is_owner"))


def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="Дни рождения", callback_data="menu_birthdays")
    kb.button(text="Участники", callback_data="menu_members")
    kb.button(text="Рест", callback_data="menu_rest")
    kb.button(text="Жалобная", callback_data="menu_complaints")
    kb.button(text="Поддержка", callback_data="menu_support")
    kb.button(text="О боте", callback_data="menu_about")
    kb.adjust(2)
    return kb.as_markup()


async def send_main_menu(message: Message, user: dict):
    await texts.show(message, "menu_greeting", markup=main_menu_kb(), role=role_of(user))


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("in_chat") or not user.get("has_bot_access"):
        await message.answer("Эта команда доступна только участникам чата.")
        return
    await send_main_menu(message, user)


@router.callback_query(F.data == "menu_root")
async def cb_menu_root(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    await texts.show(callback, "menu_greeting", markup=main_menu_kb(), role=role_of(user))
    await callback.answer()


def back_kb(target: str = "menu_root"):
    kb = InlineKeyboardBuilder()
    kb.button(text="Назад", callback_data=target)
    return kb.as_markup()


# ---------------------------------------------------------------------------
# ДНИ РОЖДЕНИЯ
# ---------------------------------------------------------------------------

async def birthdays_block() -> str:
    members = await db.members(with_role=True)
    by_month: dict[int, list[tuple[int, str]]] = {m: [] for m in range(1, 13)}
    for u in members:
        bday = u.get("birthday")
        if not bday:
            continue
        d, m = (int(x) for x in bday.split("."))
        by_month[m].append((d, u.get("role_key", "—")))

    lines = []
    for month in range(1, 13):
        entries = sorted(by_month[month])
        if not entries:
            continue
        lines.append(f"\n{MONTHS_RU[month - 1]}:")
        lines.extend(f"{d:02d} – {esc(role)}" for d, role in entries)
    return "\n".join(lines) if lines else "\n\n(пока никто не указал дату рождения)"


@router.callback_query(F.data == "menu_birthdays")
async def cb_birthdays(callback: CallbackQuery):
    await texts.show(callback, "birthdays_header", markup=back_kb(), extra=await birthdays_block())
    await callback.answer()


# ---------------------------------------------------------------------------
# УЧАСТНИКИ
# ---------------------------------------------------------------------------

async def members_block() -> str:
    lines = []
    for m in await db.members_sorted():
        role = esc(m.get("role_key", "—"))
        if m.get("is_owner"):
            lines.append(f"\n{role} – владелец")
        elif m.get("is_admin"):
            lines.append(f"\n{role} – администратор")
        else:
            lines.append(f"\n{role}")
    return "".join(lines) if lines else "\n\n(участников пока нет)"


@router.callback_query(F.data == "menu_members")
async def cb_members(callback: CallbackQuery):
    await texts.show(callback, "members_header", markup=back_kb(), extra=await members_block())
    await callback.answer()


# ---------------------------------------------------------------------------
# РЕСТ
# ---------------------------------------------------------------------------

async def rest_block() -> str:
    resters = await db.active_resters()
    banned = await db.rest_banned_users()
    lines = ["\n"]
    if resters:
        lines += [f"{esc(r.get('role_key','—'))} — до {pure.fmt_msk(r['rest']['rest_end'], False)}" for r in resters]
    else:
        lines.append("<i>сейчас никто не в ресте…</i>")
    lines.append("")
    if banned:
        lines += [f"{esc(b.get('role_key','—'))} — запрет до {pure.fmt_msk(b['rest']['rest_ban_until'], False)}"
                  for b in banned]
    return "\n".join(lines)


@router.callback_query(F.data == "menu_rest")
async def cb_rest(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    tail = ""
    if not is_limited(user):
        if user["rest"]["resting"]:
            pass
        elif user["rest"].get("rest_ban_until"):
            pass
        elif not await db.can_take_rest():
            tail = "\n\nвсе места заняты, рест взять невозможно"
        else:
            kb.button(text="Взять рест", callback_data="rest_take")
    kb.button(text="Назад", callback_data="menu_root")
    kb.adjust(1)
    await texts.show(callback, "rest_header", markup=kb.as_markup(), extra=await rest_block() + tail)
    await callback.answer()


@router.callback_query(F.data == "rest_take")
async def cb_rest_take(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if is_limited(user):
        await callback.answer()
        return
    await state.set_state(RestStates.entering_date)
    await texts.send_key(callback.bot, callback.from_user.id, "rest_prompt")
    await callback.answer()


@router.message(RestStates.entering_date)
async def process_rest_date(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    year = pure.to_msk(db.now()).year
    parsed = pure.parse_dd_mm((message.text or "").strip(), year)
    if not parsed:
        await message.answer("Некорректная дата. Попробуйте снова: /menu → Рест → Взять рест.")
        return
    day, month = parsed
    today = pure.to_msk(db.now()).date()
    try:
        target = dt.date(today.year, month, day)
        if target <= today:
            target = dt.date(today.year + 1, month, day)
    except ValueError:
        await message.answer("Такой даты не существует.")
        return

    max_date = today + dt.timedelta(days=config.MAX_REST_DAYS)
    if target > max_date:
        target = max_date

    if not await db.can_take_rest():
        await message.answer("Все места заняты, рест взять невозможно.")
        return

    user = await db.get_user(message.from_user.id)
    end_dt = dt.datetime.combine(target, dt.time(0, 0)) - dt.timedelta(hours=config.MSK_OFFSET_HOURS)
    await db.start_rest(message.from_user.id, end_dt)
    await sync_restrictions(bot, message.from_user.id)
    await assign_role_tag(bot, message.from_user.id, user["role_key"], resting=True)

    date_str = pure.format_dd_mm(target.day, target.month)
    await texts.send_key(bot, message.from_user.id, "rest_taken_dm", date=date_str)
    gw = "его" if user.get("gender") != "ж" else "её"
    await main_chat(bot, f"{esc(user['role_key'])} берёт рест до {date_str}. Просьба не беспокоить {gw}!")
    await notify_admins(bot, f"{esc(user['role_key'])} берёт рест до {date_str}")


# ---------------------------------------------------------------------------
# ЖАЛОБНАЯ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu_complaints")
async def cb_complaints(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    if not is_limited(user):
        kb.button(text="Написать жалобу", callback_data="complaint_write")
    kb.button(text="Назад", callback_data="menu_root")
    kb.adjust(1)
    await texts.show(callback, "complaint_intro", markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "complaint_write")
async def cb_complaint_write(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if is_limited(user):
        await callback.answer()
        return
    members = [m for m in await db.members(with_role=True)
               if m["_id"] != callback.from_user.id and not m.get("is_owner")]
    kb = InlineKeyboardBuilder()
    for m in members:
        label = m.get("role_key", "—") + (" (админ — жалоба уйдёт только владельцу)" if m.get("is_admin") else "")
        kb.button(text=label, callback_data=f"complaint_target:{m['_id']}")
    kb.button(text="Назад", callback_data="menu_complaints")
    kb.adjust(1)
    await state.set_state(ComplaintStates.choosing_target)
    await callback.message.answer("На кого вы хотите пожаловаться?", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("complaint_target:"), ComplaintStates.choosing_target)
async def cb_complaint_target(callback: CallbackQuery, state: FSMContext):
    await state.update_data(target_id=int(callback.data.split(":")[1]))
    await state.set_state(ComplaintStates.writing)
    await texts.send_key(callback.bot, callback.from_user.id, "complaint_prompt")
    await callback.answer()


@router.message(ComplaintStates.writing)
async def process_complaint_text(message: Message, state: FSMContext):
    text = message.text or ""
    if pure.count_words(text) < config.COMPLAINT_MIN_WORDS:
        await message.answer(f"Жалоба должна содержать не менее {config.COMPLAINT_MIN_WORDS}-ти слов. Попробуйте снова.")
        return
    data = await state.get_data()
    target = await db.get_user(data["target_id"])
    if not target:
        await state.clear()
        await message.answer("Этот участник уже покинул чат.")
        return
    user = await db.get_user(message.from_user.id)
    await state.update_data(text=text)
    await state.set_state(ComplaintStates.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="complaint_send")
    kb.button(text="Редактировать", callback_data="complaint_edit")
    kb.button(text="Отменить", callback_data="complaint_cancel")
    kb.adjust(1)
    await message.answer(
        f"Ваша жалоба:\n\nВаша роль: {esc(role_of(user))}\nЖалоба на: {esc(role_of(target))}\n\n"
        f"Содержание жалобы: {esc(text)}",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "complaint_edit", ComplaintStates.confirming)
async def cb_complaint_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ComplaintStates.writing)
    await texts.send_key(callback.bot, callback.from_user.id, "complaint_prompt")
    await callback.answer()


@router.callback_query(F.data == "complaint_cancel", ComplaintStates.confirming)
async def cb_complaint_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    await texts.show(callback, "menu_greeting", markup=main_menu_kb(), role=role_of(user))
    await callback.answer()


@router.callback_query(F.data == "complaint_send", ComplaintStates.confirming)
async def cb_complaint_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_id, text = data["target_id"], data["text"]
    user, target = await db.get_user(callback.from_user.id), await db.get_user(target_id)
    await state.clear()

    if not target or target.get("is_owner"):
        await callback.message.answer("Эта жалоба больше не может быть отправлена.")
        await callback.answer()
        return

    unique = await db.add_complaint(callback.from_user.id, target_id, text)
    await texts.send_key(bot, callback.from_user.id, "complaint_sent")

    report = (f"Новая жалоба.\n\nОт: {esc(role_of(user))}\n\nНа: {esc(role_of(target))}\n\n"
              f"Содержание жалобы: <i>{esc(text)}</i>")

    if target.get("is_admin"):
        from services.utils import send as raw_send
        await raw_send(bot, config.OWNER_ID, report)
    else:
        await notify_admins(bot, report)
        eligible = [m for m in await db.members(with_role=True)
                    if not m.get("is_owner") and not m.get("is_admin") and m["_id"] != target_id]
        if unique > config.COMPLAINT_BAN_THRESHOLD or unique >= len(eligible):
            from services import punishments
            await punishments.remove_participant(bot, target_id, permanent=False)
            await main_chat(bot, f"{esc(role_of(target))} {gform(target, 'исключён', 'исключена')} из-за большого количества жалоб.")
        elif unique > config.COMPLAINT_MUTE_THRESHOLD and not target.get("complaint_muted"):
            until = db.now() + dt.timedelta(hours=24)
            await db.set_fields(target_id, {"muted_until": until, "complaint_muted": True})
            await sync_restrictions(bot, target_id)
            await main_chat(bot, f"{esc(role_of(target))} имеет много жалоб, {gform(target, 'участник заглушен', 'участница заглушена')} на 24 часа")

    await callback.answer()


# ---------------------------------------------------------------------------
# ПОДДЕРЖКА
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu_support")
async def cb_support(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    kb = InlineKeyboardBuilder()
    if not is_limited(user):
        kb.button(text="Написать в поддержку", callback_data="support_write")
    kb.button(text="Назад", callback_data="menu_root")
    kb.adjust(1)
    await texts.show(callback, "support_intro", markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "support_write")
async def cb_support_write(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user(callback.from_user.id)
    if is_limited(user):
        await callback.answer()
        return
    await state.set_state(SupportStates.writing_topic)
    await texts.send_key(callback.bot, callback.from_user.id, "support_topic_prompt")
    await callback.answer()


@router.message(SupportStates.writing_topic)
async def process_support_topic(message: Message, state: FSMContext):
    text = message.text or ""
    if pure.count_words(text) > config.SUPPORT_TOPIC_MAX_WORDS:
        await message.answer(f"Тема письма не должна превышать {config.SUPPORT_TOPIC_MAX_WORDS} слов. Попробуйте снова.")
        return
    await state.update_data(topic=text)
    await state.set_state(SupportStates.writing_body)
    await texts.send_key(message.bot, message.from_user.id, "support_body_prompt")


@router.message(SupportStates.writing_body)
async def process_support_body(message: Message, state: FSMContext):
    text = message.text or ""
    if pure.count_words(text) < config.SUPPORT_BODY_MIN_WORDS:
        await message.answer(f"Письмо должно содержать не менее {config.SUPPORT_BODY_MIN_WORDS} слов. Попробуйте снова.")
        return
    data = await state.get_data()
    await state.update_data(body=text)
    await state.set_state(SupportStates.confirming)

    kb = InlineKeyboardBuilder()
    kb.button(text="Отправить", callback_data="support_send")
    kb.button(text="Редактировать", callback_data="support_edit")
    kb.button(text="Отменить", callback_data="support_cancel")
    kb.adjust(1)
    await message.answer(
        f"Ваше письмо:\n\nТема письма: {esc(data['topic'])}\n\n<i>{esc(text)}</i>", reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "support_edit", SupportStates.confirming)
async def cb_support_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportStates.writing_body)
    await texts.send_key(callback.bot, callback.from_user.id, "support_body_prompt")
    await callback.answer()


@router.callback_query(F.data == "support_cancel", SupportStates.confirming)
async def cb_support_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    await texts.show(callback, "menu_greeting", markup=main_menu_kb(), role=role_of(user))
    await callback.answer()


@router.callback_query(F.data == "support_send", SupportStates.confirming)
async def cb_support_send(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user = await db.get_user(callback.from_user.id)
    ticket_id = await db.create_ticket(callback.from_user.id, data["topic"], data["body"])
    await state.clear()
    await texts.send_key(bot, callback.from_user.id, "support_sent")

    kb = InlineKeyboardBuilder()
    kb.button(text="Ответить", callback_data=f"support_reply:{ticket_id}")
    text = f"Письмо в поддержку от {esc(role_of(user))}:\n\n<i>{esc(data['body'])}</i>\n\nТема: {esc(data['topic'])}"
    await notify_admins(bot, text, reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("support_reply:"))
async def cb_support_reply_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportReplyStates.writing)
    await state.update_data(ticket_id=callback.data.split(":")[1])
    await callback.message.answer("Напишите ваш ответ на обращение в поддержку.")
    await callback.answer()


@router.message(SupportReplyStates.writing)
async def process_support_reply(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    ticket = await db.get_ticket(data["ticket_id"])
    if not ticket:
        await message.answer("Обращение не найдено (возможно, уже обработано).")
        await state.clear()
        return

    await db.answer_ticket(data["ticket_id"], message.from_user.id, message.text or "")
    await state.clear()

    admin = await db.get_user(message.from_user.id)
    wait = db.now() - ticket["created_at"]
    admin_role = "Владелец" if message.from_user.id == config.OWNER_ID else role_of(admin, "Администратор")
    text = (f"Ответ на ваше письмо в поддержку. Время ожидания: {pure.human_wait(wait)}.\n\n"
            f"Админ: {esc(admin_role)}\n\n<i>{esc(message.text or '')}</i>")
    from services.utils import send as raw_send
    await raw_send(bot, ticket["user_id"], text)
    await message.answer("Ответ отправлен.")


# ---------------------------------------------------------------------------
# О БОТЕ
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu_about")
async def cb_about(callback: CallbackQuery):
    await texts.show(callback, "about_member", markup=back_kb())
    await callback.answer()
