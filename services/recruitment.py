"""
services/recruitment.py — набор в чат: Фазы, Волны, режим ожидания, брони.

Правила (ТЗ + ответы владельца)
------------------------------
* Всего 4 Фазы (I–IV), по две в Волне: Волна 1 = Фазы I–II, Волна 2 = Фазы III–IV.
  После IV Фазы происходит сброс (снова Фаза I / Волна 1).
* Фаза заканчивается, когда в чат вошло заданное владельцем число людей.
  Вход засчитывается в момент захода (member update); вручную добавленные
  участники считаются; не прошедшие капчу — не считаются (откат);
  повторный вход того же id не считается; выходы количество не меняют.
* После фазы — «режим ожидания» (свойство ОТКРЫТОГО набора): заявки нельзя,
  можно ставить бронь на роль. Ожидание: 1 неделя до новой Фазы, 2 недели до
  новой Волны. Новая Фаза открывается в 00:00 МСК.
* В 12:00 МСК последнего дня бот повторно спрашивает у владельца число мест
  (ждёт до открытия ≥ 12 часов); нет ответа — шаблон (Волна 1: 10, Волна 2: 15).
* Уже одобренные анкеты продолжают действовать. Брони удерживают места
  новой Фазы; при бане или занятии роли иным путём бронь аннулируется.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database import methods as db
from services import pure, texts
from services.texts import Raw
from services.utils import doc_user_line, esc, notify_admins, send

logger = logging.getLogger(__name__)

ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def wave_of(phase: int) -> int:
    return 1 if phase in (1, 2) else 2


# ---------------------------------------------------------------------------
# Анкеты
# ---------------------------------------------------------------------------

def application_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="Принять", callback_data=f"app_accept:{user_id}")
    kb.button(text="Отклонить", callback_data=f"app_reject:{user_id}")
    kb.button(text="Бан", callback_data=f"app_ban:{user_id}")
    kb.adjust(3)
    return kb.as_markup()


def application_text(user: dict) -> str:
    return (
        f"Новая заявка! Дата подачи: {pure.fmt_msk(user.get('application_at') or db.now())}\n\n"
        f"Роль: {esc(user['role_key'])}\nДень рождения: {esc(user['birthday'])}\n\n"
        f"Юзернейм: {doc_user_line(user)}"
    )


async def send_application(bot: Bot, user: dict) -> None:
    await notify_admins(bot, application_text(user), reply_markup=application_keyboard(user["_id"]))


# ---------------------------------------------------------------------------
# Свободные места
# ---------------------------------------------------------------------------

async def has_free_slot() -> bool:
    rec = await db.get_recruitment()
    if rec["slots"] <= 0:
        return False
    return rec["joined"] + await db.reserved_outstanding() < rec["slots"]


# ---------------------------------------------------------------------------
# Входы в фазу
# ---------------------------------------------------------------------------

async def register_join(bot: Bot, user_id: int) -> None:
    """Засчитать вход в счётчик текущей Фазы. Вызывается ТОЛЬКО для обычного
    входа по приглашению (после одобренной анкеты) — для ручного добавления
    владельцем используется register_manual_add(), который счётчик НЕ трогает
    в принципе, вне зависимости от состояния набора."""
    rec = await db.get_recruitment()
    if rec["wait_mode"] or rec["status"] != "open":
        return  # фазы сейчас нет — нечего засчитывать
    if not await db.add_joined(user_id):
        return  # повторный вход
    rec = await db.get_recruitment()
    if rec["slots"] > 0 and rec["joined"] >= rec["slots"]:
        await end_phase(bot, closed_by=user_id)


async def register_manual_add(user_id: int) -> None:
    """ТЗ: «при добавлении участника вручную... юзер минует сам режим
    ожидания и спокойно зачисляется в чат и базу данных» — то есть ручное
    добавление НИКОГДА не расходует места текущей Фазы и не влияет на счётчик
    набора, вне зависимости от открыт сейчас набор, закрыт или идёт ожидание.
    Эта функция намеренно ничего не делает с recruitment — оставлена как явная
    точка входа, чтобы не завязываться на побочный эффект register_join()."""
    return


async def rollback_join(bot: Bot, user_id: int) -> None:
    """Не прошёл капчу -> вход не засчитывается. Если именно он закрыл фазу — вернуть её."""
    await db.remove_joined(user_id)
    rec = await db.get_recruitment()
    if rec["wait_mode"] and rec.get("closed_by_user") == user_id:
        await db.set_recruitment({
            "wait_mode": False, "wait_until": None, "next_phase": None, "next_slots": None,
            "awaiting_slots": False, "closed_by_user": None,
        })
        await send(bot, config.OWNER_ID, "Участник не прошёл капчу — Фаза возвращена в набор (вход не засчитан).")


async def end_phase(bot: Bot, closed_by: Optional[int] = None) -> None:
    rec = await db.get_recruitment()
    phase = rec["phase"]
    nxt = phase + 1 if phase < 4 else 1
    new_wave = wave_of(nxt) != rec["wave"]
    days = config.WAVE_WAIT_DAYS if new_wave else config.PHASE_WAIT_DAYS
    wait_until = pure.next_midnight_msk(db.now(), days)
    await db.set_recruitment({
        "wait_mode": True, "wait_until": wait_until, "next_phase": nxt, "next_slots": None,
        "awaiting_slots": True, "reminder_sent": False, "closed_by_user": closed_by,
    })
    await ask_owner_for_slots(bot, nxt, intro=(
        f"Фаза {ROMAN[phase]} завершена (набрано {rec['joined']} из {rec['slots']}). "
        f"Режим ожидания до {pure.fmt_msk(wait_until)} МСК: юзеры могут ставить бронь на роли.\n\n"
    ))


async def ask_owner_for_slots(bot: Bot, next_phase: int, intro: str = "") -> None:
    wave = wave_of(next_phase)
    extra = (f"\n\nБудущая Фаза {ROMAN[next_phase]} | Волна {wave}. "
             f"Максимум мест: {config.WAVE_MAX_SLOTS[wave]}. Ответьте числом.")
    tpl, _ = await texts.load("slots_request")
    await send(bot, config.OWNER_ID, esc(intro) + texts.render(tpl) + extra)


async def handle_owner_slots_answer(bot: Bot, number: int) -> Optional[str]:
    """Ответ владельца числом на запрос мест. Вернуть текст ответа или None (запроса нет)."""
    rec = await db.get_recruitment()
    if not rec.get("awaiting_slots") or not rec.get("wait_mode"):
        return None
    wave = wave_of(rec.get("next_phase") or 1)
    cap = config.WAVE_MAX_SLOTS[wave]
    if number < 1 or number > cap:
        return f"Для Волны {wave} можно указать от 1 до {cap} мест. Отправьте число ещё раз."
    await db.set_recruitment({"next_slots": number, "awaiting_slots": False})
    return f"Принято: в следующей Фазе будет {number} мест."


# ---------------------------------------------------------------------------
# Открытие набора
# ---------------------------------------------------------------------------

async def open_recruitment(bot: Bot, slots: int) -> str:
    """Кнопка «открыть набор» (числом мест ответил владелец)."""
    rec = await db.get_recruitment()
    if rec["wait_mode"]:
        await db.set_recruitment({"status": "open"})
        return "Набор открыт. Идёт режим ожидания: новая Фаза начнётся по расписанию."
    cap = config.WAVE_MAX_SLOTS[rec["wave"]]
    if slots < 1 or slots > cap:
        return f"Для Волны {rec['wave']} можно указать от 1 до {cap} мест."
    fields = {"status": "open", "slots": slots, "ever_opened": True}
    if not rec.get("ever_opened"):
        fields.update({"phase": 1, "wave": 1, "joined": 0, "joined_ids": []})
    await db.set_recruitment(fields)
    return f"Набор открыт. Фаза {ROMAN[rec['phase'] if rec.get('ever_opened') else 1]}, мест: {slots}."


async def open_new_phase(bot: Bot) -> None:
    rec = await db.get_recruitment()
    phase = rec.get("next_phase") or 1
    wave = wave_of(phase)
    used_template = rec.get("next_slots") is None
    slots = rec["next_slots"] if not used_template else config.WAVE_TEMPLATE_SLOTS[wave]
    prev_wave = rec["wave"]
    await db.set_recruitment({
        "phase": phase, "wave": wave, "slots": slots, "joined": 0, "joined_ids": [],
        "wait_mode": False, "wait_until": None, "awaiting_slots": False, "reminder_sent": False,
        "next_slots": None, "next_phase": None, "closed_by_user": None, "status": "open", "ever_opened": True,
    })
    if wave != prev_wave:
        # новая Волна: все «ждущие следующей волны» и отмеченные за рейд снова могут подавать анкеты
        await db.users.update_many(
            {"in_chat": False, "status": {"$in": [db.ST_WAITING, db.ST_REJECTED]}},
            {"$set": {"status": db.ST_NEW, "rejection_count": 0, "raid_flag": False}},
        )
        await db.users.update_many({"in_chat": False, "raid_flag": True}, {"$set": {"raid_flag": False}})
    if used_template:
        await send(bot, config.OWNER_ID,
                   f"Владелец не ответил — бот самостоятельно ввёл кол-во мест по шаблону: {slots}.")
    await send(bot, config.OWNER_ID, f"Началась Фаза {ROMAN[phase]} | Волна {wave}. Мест: {slots}.")
    await convert_reservations(bot)


async def tick(bot: Bot) -> None:
    """Вызывается планировщиком: напоминание владельцу и открытие новой Фазы."""
    rec = await db.get_recruitment()
    if not rec.get("wait_mode") or not rec.get("wait_until"):
        return
    now = db.now()
    wait_until = rec["wait_until"]
    if rec.get("next_slots") is None and not rec.get("reminder_sent") and now >= wait_until - dt.timedelta(hours=12):
        await db.set_recruitment({"reminder_sent": True, "awaiting_slots": True})
        await ask_owner_for_slots(bot, rec.get("next_phase") or 1,
                                  intro="Напоминание: новая Фаза начнётся в 00:00 МСК. Если не ответите, "
                                        "будет применён шаблон.\n\n")
    if now >= wait_until and rec["status"] == "open":
        await open_new_phase(bot)


# ---------------------------------------------------------------------------
# Брони
# ---------------------------------------------------------------------------

async def annul_reservation(bot: Bot, user_id: int, notify: bool = False) -> None:
    res = await db.get_reservation(user_id)
    if not res:
        return
    await db.delete_reservation(user_id)
    user = await db.get_user(user_id)
    if user and not user.get("in_chat") and user.get("status") == db.ST_RESERVED:
        await db.set_fields(user_id, {"status": db.ST_NEW, "role_key": None, "birthday": None})
    if notify:
        await texts.send_key(bot, user_id, "reservation_annulled", role=res["role_key"])


async def annul_role_reservation(bot: Bot, role_key: str) -> None:
    """Роль заняли иным путём (например, вручную) — бронь аннулируется."""
    res = await db.reservation_by_role(role_key)
    if res:
        await annul_reservation(bot, res["user_id"], notify=True)


async def convert_reservations(bot: Bot) -> None:
    """Новая Фаза: анкеты забронировавших автоматически уходят админам."""
    for res in await db.all_reservations():
        uid = res["user_id"]
        user = await db.get_user(uid)
        if not user:
            await db.delete_reservation(uid)
            continue
        if await db.role_taken(res["role_key"], exclude_user=uid) and not await db.reservation_by_role(res["role_key"]):
            await annul_reservation(bot, uid, notify=True)
            continue
        await db.delete_reservation(uid)
        await db.set_fields(uid, {
            "status": db.ST_PENDING, "from_reservation": True, "application_at": db.now(),
            "role_key": res["role_key"], "birthday": res["birthday"], "gender": res["gender"],
        })
        fresh = await db.get_user(uid)
        await send_application(bot, fresh)
        await texts.send_key(bot, uid, "reservation_started", role=res["role_key"])


async def status_text() -> str:
    rec = await db.get_recruitment()
    if not rec.get("ever_opened") and rec["status"] == "closed":
        joined = "статус набора нужно сделать «открытым»"
    else:
        joined = str(rec["joined"])
    state = "открыто" if rec["status"] == "open" else "закрыто"
    tail = ""
    if rec.get("wait_mode") and rec.get("wait_until"):
        tail = f"\n\nРежим ожидания до {pure.fmt_msk(rec['wait_until'])} МСК"
    return (
        f"Статус набора: {state}\n\nФаза {ROMAN.get(rec['phase'], rec['phase'])}  |  Волна {rec['wave']}\n\n"
        f"Мест: {rec['slots']}\n\nВошло: {joined}{tail}"
    )
