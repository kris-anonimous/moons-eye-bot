"""
handlers/registration.py

/start -> проверка бана/владельца/участника -> (для новых) проверка подписки
-> выбор роли (локально + Gemini как страховка) -> дата рождения -> юзернейм
-> согласие -> анкета админам -> приём/отклонение/бан -> invite-ссылка
(join request) -> проверка «тот ли юзер» -> капча 5 минут -> тег роли.

Плюс: бронь роли в режиме ожидания, посещения/блокировка (логи владельцу),
выход участника (с формой «роль + причина») и разбор решения владельца по
числу мест новой Фазы (текстовые сообщения в личке боту).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatJoinRequest, ChatMemberUpdated, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import methods as db
from roles import get_role_gender
from services import gemini_service, pure, recruitment, texts
from services.utils import (
    assign_role_tag, ban_everywhere, check_subscriptions, doc_user_line, esc,
    main_chat, notify_admins, notify_owner, role_of, sync_restrictions, tg_user_line, twink_guess,
)

logger = logging.getLogger(__name__)
router = Router(name="registration")


class RegStates(StatesGroup):
    waiting_role = State()
    waiting_birthday = State()
    waiting_username = State()


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.is_bot:
        return  # боты не участвуют в регистрации/анкетировании
    await state.clear()
    user_id = message.from_user.id
    before, doc = await db.ensure_user(user_id, message.from_user.username, message.from_user.full_name)

    # Анкета о посещении по ТЗ приходит только владельцу (и в лог), и только
    # при первом контакте с ботом или при блокировке/перезапуске — не на
    # каждый повторный /start уже известного боту человека.
    is_formal = bool(doc.get("is_owner") or doc.get("is_admin"))
    was_blocked = bool(before and before.get("blocked_bot"))
    first_contact = before is None
    if not is_formal and (first_contact or was_blocked):
        await notify_owner(
            bot,
            f"Новое посещение!\n\nВремя посещения: {pure.fmt_msk(db.now())} МСК\n\n"
            f"Юзер: {tg_user_line(message.from_user)}\n\n"
            f"Похож ли на твинк: {twink_guess(user_id)}\n\n"
            f"Был ли в системе/есть ли в системе: {'да' if before else 'нет'}\n\n"
            f"Блокировал ли бота: {'да' if was_blocked else 'нет'}",
        )
        if was_blocked:
            await db.set_fields(user_id, {"blocked_bot": False})

    if doc.get("bot_banned"):
        await texts.send_key(bot, user_id, "ban_notice")
        return

    if user_id == config.OWNER_ID:
        doc = await db.ensure_owner(message.from_user.username, message.from_user.full_name)

    if doc.get("in_chat") and doc.get("has_bot_access"):
        from handlers.menu import send_main_menu
        await send_main_menu(message, doc)
        return

    await run_entry_flow(message, doc)


async def run_entry_flow(message: Message, doc: dict) -> None:
    user_id = message.from_user.id
    rec = await db.get_recruitment()

    if rec["status"] == "closed":
        await texts.show(message, "closed_notice")
        return

    if doc.get("status") == db.ST_PENDING:
        await texts.show(message, "pending_notice")
        return

    if doc.get("status") == db.ST_APPROVED and doc.get("invite_link"):
        await texts.show(message, "app_approved", link=texts.Raw(esc(doc["invite_link"])))
        return

    if rec.get("wait_mode"):
        reservation = await db.get_reservation(user_id)
        if reservation:
            kb = InlineKeyboardBuilder()
            kb.button(text="Отменить бронь", callback_data="reservation_cancel")
            await texts.show(message, "reservation_exists", markup=kb.as_markup(), role=reservation["role_key"])
            return
        if doc.get("status") == db.ST_WAITING:
            await texts.show(message, "forced_wait")
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="Поставить бронь", callback_data="reserve_start")
        await texts.show(message, "wait_mode_notice", markup=kb.as_markup())
        return

    if doc.get("status") == db.ST_WAITING:
        await texts.show(message, "forced_wait")
        return

    if not await recruitment.has_free_slot():
        await texts.show(message, "no_slots")
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="Подать заявку на вступление", callback_data="apply_start")
    await texts.show(message, "start_welcome", markup=kb.as_markup())


# ---------------------------------------------------------------------------
# Начало анкеты
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "apply_start")
async def cb_apply_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if not await check_subscriptions(bot, user_id):
        await texts.show(callback, "sub_required")
        await callback.answer()
        return
    if not await recruitment.has_free_slot():
        await texts.show(callback, "no_slots")
        await callback.answer()
        return
    await state.set_state(RegStates.waiting_role)
    await state.update_data(reservation_mode=False)
    await texts.show(callback, "role_prompt")
    await callback.answer()


@router.callback_query(F.data == "reserve_start")
async def cb_reserve_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if not await check_subscriptions(bot, user_id):
        await texts.show(callback, "sub_required")
        await callback.answer()
        return
    if await db.get_reservation(user_id):
        await callback.answer("Бронь уже стоит.", show_alert=True)
        return
    await state.set_state(RegStates.waiting_role)
    await state.update_data(reservation_mode=True)
    await texts.show(callback, "role_prompt")
    await callback.answer()


@router.callback_query(F.data == "reservation_cancel")
async def cb_reservation_cancel(callback: CallbackQuery, bot: Bot):
    user_id = callback.from_user.id
    if not await db.get_reservation(user_id):
        await callback.answer()
        return
    await recruitment.annul_reservation(bot, user_id)
    await texts.show(callback, "reservation_cancelled")
    await callback.answer()


# ---------------------------------------------------------------------------
# Роль
# ---------------------------------------------------------------------------

@router.message(RegStates.waiting_role)
async def process_role(message: Message, state: FSMContext):
    text = message.text or ""
    local = pure.match_roles_local(text)

    if len(local) > 1:
        await texts.show(message, "role_multiple")
        return
    if len(local) == 1:
        role = local[0]
    else:
        result = await gemini_service.match_role(text)
        if result == "MULTIPLE":
            await texts.show(message, "role_multiple")
            return
        if result == "ERROR":
            await texts.show(message, "role_error")
            return
        if result is None:
            await texts.show(message, "role_unknown")
            return
        role = result

    if await db.role_taken(role, exclude_user=message.from_user.id):
        await texts.show(message, "role_taken", role=role)
        return

    await state.update_data(role_key=role, gender=get_role_gender(role))
    await state.set_state(RegStates.waiting_birthday)
    await texts.show(message, "birthday_prompt")


# ---------------------------------------------------------------------------
# Дата рождения
# ---------------------------------------------------------------------------

@router.message(RegStates.waiting_birthday)
async def process_birthday(message: Message, state: FSMContext):
    year = pure.to_msk(db.now()).year
    parsed = pure.parse_dd_mm((message.text or "").strip(), year)
    if not parsed:
        await texts.show(message, "birthday_invalid")
        return
    await state.update_data(birthday=pure.format_dd_mm(*parsed))
    await state.set_state(RegStates.waiting_username)
    await texts.show(message, "username_prompt")


# ---------------------------------------------------------------------------
# Юзернейм
# ---------------------------------------------------------------------------

@router.message(RegStates.waiting_username)
async def process_username(message: Message, state: FSMContext):
    real = message.from_user.username
    data = await state.get_data()

    if not real:
        deadline = data.get("username_deadline")
        if deadline is None:
            deadline = (dt.datetime.utcnow() + dt.timedelta(minutes=config.USERNAME_WAIT_MINUTES)).isoformat()
            await state.update_data(username_deadline=deadline)
            await texts.show(message, "username_missing")
            return
        if dt.datetime.utcnow() > dt.datetime.fromisoformat(deadline):
            await state.clear()
            await texts.show(message, "username_expired")
            return
        await texts.show(message, "username_missing")
        return

    text = (message.text or "").strip()
    if text.lstrip("@").lower() != real.lower():
        await texts.show(message, "username_wrong")
        return

    await state.update_data(username=real)
    kb = InlineKeyboardBuilder()
    kb.button(text="Принять", callback_data="agreement_accept")
    await texts.show(message, "agreement", markup=kb.as_markup(), link=texts.Raw(esc(config.AGREEMENT_URL)))


# ---------------------------------------------------------------------------
# Согласие -> анкета
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "agreement_accept")
async def cb_agreement_accept(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = callback.from_user.id
    role, gender, birthday = data.get("role_key"), data.get("gender"), data.get("birthday")
    reservation_mode = data.get("reservation_mode", False)
    await state.clear()

    if not role or not birthday:
        await callback.answer("Анкета устарела, начните заново через /start.", show_alert=True)
        return

    if await db.role_taken(role, exclude_user=user_id):
        await texts.show(callback, "role_taken", role=role)
        await callback.answer()
        return

    await db.set_fields(user_id, {
        "role_key": role, "gender": gender, "birthday": birthday,
        "agreement_accepted_at": db.now(),
    })

    if reservation_mode:
        await db.set_fields(user_id, {"status": db.ST_RESERVED})
        await db.create_reservation({
            "role_key": role, "user_id": user_id, "birthday": birthday, "gender": gender, "created_at": db.now(),
        })
        await notify_admins(
            bot,
            f"Поставлена бронь на: {esc(role)}, время: {pure.fmt_msk(db.now())}\n\n"
            f"ID: {doc_user_line(await db.get_user(user_id))}",
        )
        await texts.show(callback, "reservation_done", role=role)
        await callback.answer()
        return

    await db.set_fields(user_id, {"status": db.ST_PENDING, "application_at": db.now()})
    user = await db.get_user(user_id)
    await recruitment.send_application(bot, user)
    await texts.show(callback, "application_sent")
    await callback.answer()


# ---------------------------------------------------------------------------
# Решение администрации
# ---------------------------------------------------------------------------

async def _already_in_chat(bot: Bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(config.MAIN_CHAT_ID, user_id)
        return m.status in ("member", "restricted", "administrator", "creator")
    except (TelegramBadRequest, TelegramForbiddenError):
        return False


@router.callback_query(F.data.startswith("app_accept:"))
async def cb_app_accept(callback: CallbackQuery, bot: Bot):
    user_id = int(callback.data.split(":")[1])
    user = await db.get_user(user_id)
    if not user or user.get("status") not in (db.ST_PENDING,):
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    if await db.role_taken(user["role_key"], exclude_user=user_id):
        await callback.answer("Роль уже занята — анкету нужно отклонить.", show_alert=True)
        return

    await db.set_fields(user_id, {"status": db.ST_APPROVED})

    if await _already_in_chat(bot, user_id):
        await finalize_join(bot, user_id)
    else:
        link = await issue_invite_link(bot, user_id)
        if not link:
            await callback.answer("Не удалось создать ссылку-приглашение (проверьте права бота в чате).", show_alert=True)
            return

    try:
        await callback.message.edit_text(callback.message.text + "\n\n✅ Принято.")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("app_reject:"))
async def cb_app_reject(callback: CallbackQuery, bot: Bot):
    user_id = int(callback.data.split(":")[1])
    user = await db.get_user(user_id)
    if not user or user.get("status") != db.ST_PENDING:
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return

    new_count = user.get("rejection_count", 0) + 1
    await db.set_fields(user_id, {"role_key": None, "birthday": None, "rejection_count": new_count})

    if new_count >= config.MAX_REJECTIONS:
        await db.set_fields(user_id, {"status": db.ST_WAITING})
        await texts.send_key(bot, user_id, "forced_wait")
    else:
        await db.set_fields(user_id, {"status": db.ST_REJECTED})
        await texts.send_key(bot, user_id, "app_rejected")

    try:
        await callback.message.edit_text(callback.message.text + "\n\n❌ Отклонено.")
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("app_ban:"))
async def cb_app_ban(callback: CallbackQuery, bot: Bot):
    user_id = int(callback.data.split(":")[1])
    await ban_everywhere(bot, user_id)  # уже выставляет bot_banned/status/in_chat/has_bot_access
    await recruitment.annul_reservation(bot, user_id)
    await db.set_fields(user_id, {"role_key": None})
    try:
        await callback.message.edit_text(callback.message.text + "\n\n⛔ Забанен.")
    except TelegramBadRequest:
        pass
    await callback.answer()


# ---------------------------------------------------------------------------
# Заявка на вступление в чат (join request) — проверка «тот ли юзер»
# ---------------------------------------------------------------------------

@router.chat_join_request(F.chat.id == config.MAIN_CHAT_ID)
async def _revoke_link(bot: Bot, link: Optional[str]) -> None:
    if not link:
        return
    try:
        await bot.revoke_chat_invite_link(config.MAIN_CHAT_ID, link)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.info("Не удалось отозвать ссылку-приглашение: %s", e)


async def issue_invite_link(bot: Bot, user_id: int) -> Optional[str]:
    """Создать новую персональную ссылку-приглашение для одобренной анкеты
    и отправить её пользователю. Используется и при первом одобрении, и
    при повторной выдаче после рейдерского перехода по старой ссылке."""
    try:
        link = await bot.create_chat_invite_link(
            config.MAIN_CHAT_ID, member_limit=1, creates_join_request=True,
            expire_date=dt.datetime.utcnow() + dt.timedelta(hours=config.INVITE_LINK_TIMEOUT_HOURS),
        )
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось создать инвайт-ссылку: %s", e)
        return None
    await db.set_fields(user_id, {
        "invite_link": link.invite_link,
        "invite_deadline": db.now() + dt.timedelta(hours=config.INVITE_LINK_TIMEOUT_HOURS),
    })
    await texts.send_key(bot, user_id, "app_approved", link=texts.Raw(esc(link.invite_link)))
    return link.invite_link


@router.chat_join_request(F.chat.id == config.MAIN_CHAT_ID)
async def on_join_request(request: ChatJoinRequest, bot: Bot):
    user_id = request.from_user.id
    link = request.invite_link.invite_link if request.invite_link else None
    owner = await db.find_by_invite_link(link) if link else None

    if owner is None or owner["_id"] != user_id or owner.get("status") != db.ST_APPROVED:
        # ТЗ: «заявка отклоняется, а ссылка сбрасывается» — ссылку отзываем
        # сразу, а настоящему одобренному заявителю (если он есть) сразу же
        # выписываем новую, чтобы рейд не оставил его без доступа.
        try:
            await request.decline()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await _revoke_link(bot, link)
        if owner and owner.get("status") == db.ST_APPROVED:
            await issue_invite_link(bot, owner["_id"])
        await db.ensure_user(user_id, request.from_user.username, request.from_user.full_name)
        await db.set_fields(user_id, {"raid_flag": True, "status": db.ST_WAITING})
        await texts.send_key(bot, user_id, "raid_notice")
        await notify_admins(bot, f"Рейдерский переход по чужой ссылке: {tg_user_line(request.from_user)}.")
        return

    if owner.get("invite_deadline") and db.now() > owner["invite_deadline"]:
        try:
            await request.decline()
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await _revoke_link(bot, link)
        await db.set_fields(user_id, {"status": db.ST_REJECTED, "invite_link": None, "invite_deadline": None})
        await texts.send_key(bot, user_id, "invite_expired")
        return

    try:
        await request.approve()
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось одобрить заявку на вступление: %s", e)


# ---------------------------------------------------------------------------
# Фактический вход/выход участника
# ---------------------------------------------------------------------------

async def finalize_join(bot: Bot, user_id: int) -> None:
    user = await db.get_user(user_id)
    if not user:
        return
    old_link = user.get("invite_link")
    await db.set_fields(user_id, {
        "in_chat": True, "has_bot_access": True, "status": db.ST_MEMBER,
        "joined_chat_at": db.now(), "last_message_at": db.now(),
        "invite_link": None, "invite_deadline": None,
        "captcha_deadline": db.now() + dt.timedelta(minutes=config.CAPTCHA_TIMEOUT_MINUTES),
        "from_reservation": False,
    })
    if old_link:
        try:
            await bot.revoke_chat_invite_link(config.MAIN_CHAT_ID, old_link)
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            logger.info("Не удалось отозвать ссылку-приглашение (возможно, уже неактивна): %s", e)
    await db.mark_ever_member(user_id)
    await recruitment.register_join(bot, user_id)
    await recruitment.annul_role_reservation(bot, user["role_key"])

    role = user["role_key"]
    await assign_role_tag(bot, user_id, role)

    from services.utils import send_everyone
    await send_everyone(bot, f"Новый участник – {esc(role)}!")

    open_polls = await db.open_polls()
    if open_polls:
        from handlers.admin import send_poll_to_member
        for poll in open_polls:
            await send_poll_to_member(bot, user_id, poll)

    kb = InlineKeyboardBuilder()
    kb.button(text="Я не робот", callback_data=f"captcha_pass:{user_id}")
    msg = await main_chat(
        bot, f"{esc(role)}, пожалуйста, пройдите капчу в течение 5 минут, нажав кнопку ниже.",
        reply_markup=kb.as_markup(),
    )
    if msg:
        await db.set_fields(user_id, {"captcha_message_ids": [[config.MAIN_CHAT_ID, msg.message_id]]})

    await sync_restrictions(bot, user_id)


@router.chat_member(F.chat.id == config.MAIN_CHAT_ID)
async def on_chat_member_update(update: ChatMemberUpdated, bot: Bot):
    if update.new_chat_member.user.is_bot:
        return  # сторонние боты в чате не регистрируются как участники
    old_status, new_status = update.old_chat_member.status, update.new_chat_member.status
    user_id = update.new_chat_member.user.id
    joined = old_status in ("left", "kicked") and new_status in ("member", "restricted")
    left = old_status in ("member", "restricted", "administrator") and new_status in ("left", "kicked")

    if joined:
        user = await db.get_user(user_id)
        if user and user.get("status") == db.ST_APPROVED:
            await finalize_join(bot, user_id)
        else:
            await db.ensure_group_member(
                user_id, update.new_chat_member.user.username, update.new_chat_member.user.full_name,
            )
            await db.mark_ever_member(user_id)
        return

    if left:
        user = await db.get_user(user_id)
        if not user or not user.get("in_chat"):
            return  # уже обработано внутренней логикой бота (кик/бан из наказания)

        role = role_of(user)
        if new_status == "kicked":
            await main_chat(bot, f"{esc(role)} покидает наше сообщество. Подробности можно узнать у поддержки.")
            await notify_admins(bot, f"{esc(role)} исключён(а) из чата в {pure.fmt_msk(db.now())}.")
        else:
            reason = await db.pop_leave_note(user_id)
            if reason:
                await main_chat(bot, f"{esc(role)} покидает наше сообщество. Причина: {esc(reason)}")
                await notify_admins(bot, f"{esc(role)} покинул(а) чат в {pure.fmt_msk(db.now())} по причине: {esc(reason)}")
            else:
                await main_chat(bot, f"{esc(role)} покидает наше сообщество. Причина неизвестна.")
                await notify_admins(bot, f"{esc(role)} покинул(а) чат в {pure.fmt_msk(db.now())} по неизвестной причине.")
        await db.erase_user(user_id)


@router.callback_query(F.data.startswith("captcha_pass:"))
async def cb_captcha_pass(callback: CallbackQuery, bot: Bot):
    target_id = int(callback.data.split(":")[1])
    if callback.from_user.id != target_id:
        await callback.answer("Эта капча не для вас.", show_alert=True)
        return

    user = await db.get_user(target_id)
    if user:
        for chat_id, mid in user.get("captcha_message_ids", []):
            try:
                await bot.delete_message(chat_id, mid)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

    await db.set_fields(target_id, {"captcha_deadline": None, "captcha_message_ids": []})
    await sync_restrictions(bot, target_id)
    await callback.answer("Капча пройдена!")


# ---------------------------------------------------------------------------
# Ответ владельца числом мест (личка боту, вне FSM)
# ---------------------------------------------------------------------------

@router.message(F.chat.type == "private", F.from_user.id == config.OWNER_ID, F.text.regexp(r"^\s*\d+\s*$"))
async def owner_number_reply(message: Message, bot: Bot):
    reply = await recruitment.handle_owner_slots_answer(bot, int(message.text.strip()))
    if reply:
        await message.answer(reply)
