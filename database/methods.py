"""
database/methods.py — весь слой доступа к MongoDB (Motor).

Коллекции
---------
users         — все, кто запускал бота. Запись «участника» стирается целиком
                при выходе/исключении (кроме жалоб и агрегатов статистики).
msg_stats     — счётчики сообщений по дням для каждого id (переживают выход:
                нужны для «частной статистики» по ушедшим, см. ТЗ).
violations    — журнал нарушений (при выходе user_id обнуляется, агрегаты остаются).
complaints    — жалобы (по ТЗ/ответам владельца сохраняются после выхода).
support       — обращения в поддержку.
polls         — опросники.
recruitment   — состояние набора (фазы/волны/режим ожидания).
reservations  — брони ролей в режиме ожидания.
ever_members  — id, которые когда-либо были в чате (для отчёта о посещении).
leave_notes   — заметки «Роль/Причина» перед выходом (живут 30 минут).
inactivity_kicks, purge_stats, purge_settings, broadcasts_log, settings, texts, fsm.

Состояния пользователя (users.status)
-------------------------------------
new            — просто запустил бота
pending        — анкета на рассмотрении
approved       — анкета одобрена, ждём вход по ссылке
rejected       — анкета отклонена (можно подать снова, пока не исчерпан лимит)
waiting_wave   — принудительно ждёт следующей волны (3 отказа / рейд-переход)
reserved       — поставил бронь на роль в режиме ожидания
member         — участник чата
banned         — в чёрном списке бота (бан из анкеты / вечный бан)
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from bson import ObjectId
from pymongo import ReturnDocument

import config
from config import db
from services.pure import msk_date_str, to_msk

users = db["users"]
msg_stats = db["msg_stats"]
violations_col = db["violations"]
complaints_col = db["complaints"]
support_col = db["support"]
polls_col = db["polls"]
recruitment_col = db["recruitment"]
reservations_col = db["reservations"]
ever_members_col = db["ever_members"]
leave_notes_col = db["leave_notes"]
inactivity_col = db["inactivity_kicks"]
purge_stats_col = db["purge_stats"]
purge_settings_col = db["purge_settings"]
broadcasts_col = db["broadcasts_log"]
settings_col = db["settings"]

ST_NEW, ST_PENDING, ST_APPROVED, ST_REJECTED = "new", "pending", "approved", "rejected"
ST_WAITING, ST_RESERVED, ST_MEMBER, ST_BANNED = "waiting_wave", "reserved", "member", "banned"


def now() -> dt.datetime:
    return dt.datetime.utcnow()


# ---------------------------------------------------------------------------
# ПОЛЬЗОВАТЕЛИ
# ---------------------------------------------------------------------------

def _new_user_doc(user_id: int, username: Optional[str], full_name: str) -> dict:
    return {
        "_id": user_id, "username": username, "full_name": full_name,
        "first_seen": now(), "status": ST_NEW,
        "in_chat": False, "has_bot_access": False,
        "role_key": None, "gender": None, "birthday": None,
        "is_admin": False, "is_owner": False,
        "joined_chat_at": None, "last_message_at": None,
        "invite_link": None, "invite_deadline": None,
        "rejection_count": 0, "raid_flag": False,
        "bot_banned": False, "blocked_bot": False, "ever_blocked": False,
        "warns": 0, "oral_warns": 0, "muted_until": None, "purge_deficits": 0,
        "everyone_abuse_count": 0,
        "captcha_deadline": None, "captcha_message_ids": [],
        "subscribed_ok": True, "complaint_muted": False,
        "username_deadline": None, "agreement_accepted_at": None,
        "from_reservation": False, "application_at": None,
        "rest": {
            "resting": False, "rest_start": None, "rest_end": None, "rest_count": 0,
            "last_start": None, "last_end": None, "ended_at": None,
            "rest_ban_start": None, "rest_ban_until": None, "rest_ban_extensions": 0,
        },
    }


async def ensure_user(user_id: int, username: Optional[str], full_name: str) -> tuple[Optional[dict], dict]:
    """Создать/обновить запись. Возвращает (запись ДО обновления или None, запись ПОСЛЕ)."""
    before = await users.find_one({"_id": user_id})
    if before is None:
        doc = _new_user_doc(user_id, username, full_name)
        await users.insert_one(doc)
        return None, doc
    await users.update_one({"_id": user_id}, {"$set": {"username": username, "full_name": full_name}})
    after = dict(before, username=username, full_name=full_name)
    return before, after


async def ensure_owner(username: Optional[str], full_name: str) -> dict:
    _, doc = await ensure_user(config.OWNER_ID, username, full_name)
    if not doc.get("is_owner") or not doc.get("in_chat"):
        await users.update_one({"_id": config.OWNER_ID}, {"$set": {
            "is_owner": True, "in_chat": True, "has_bot_access": True, "status": ST_MEMBER,
            "joined_chat_at": doc.get("joined_chat_at") or doc.get("first_seen") or now(),
        }})
        doc = await users.find_one({"_id": config.OWNER_ID})
    return doc


async def ensure_group_member(user_id: int, username: Optional[str], full_name: str) -> dict:
    """Человек в чате, но без доступа к боту (был до бота / вошёл по ссылке владельца)."""
    _, doc = await ensure_user(user_id, username, full_name)
    if not doc.get("in_chat"):
        await users.update_one({"_id": user_id}, {"$set": {
            "in_chat": True, "status": ST_MEMBER, "joined_chat_at": doc.get("joined_chat_at") or now(),
            "has_bot_access": bool(doc.get("role_key") and doc.get("has_bot_access")),
        }})
        doc = await users.find_one({"_id": user_id})
    return doc


async def get_user(user_id: int) -> Optional[dict]:
    return await users.find_one({"_id": user_id})


async def set_fields(user_id: int, fields: dict) -> None:
    await users.update_one({"_id": user_id}, {"$set": fields})


async def erase_user(user_id: int) -> None:
    """Полностью стереть участника (кроме жалоб и агрегатов статистики)."""
    await users.delete_one({"_id": user_id})
    await reservations_col.delete_many({"user_id": user_id})
    await leave_notes_col.delete_many({"_id": user_id})
    await violations_col.update_many({"user_id": user_id}, {"$set": {"user_id": None, "role": None}})


async def mark_ever_member(user_id: int) -> None:
    await ever_members_col.update_one({"_id": user_id}, {"$set": {"at": now()}}, upsert=True)


async def was_ever_member(user_id: int) -> bool:
    return await ever_members_col.find_one({"_id": user_id}) is not None


async def members(with_role: bool = False) -> list[dict]:
    docs = [d async for d in users.find({"in_chat": True})]
    return [d for d in docs if d.get("role_key") or d.get("is_owner")] if with_role else docs


async def members_sorted() -> list[dict]:
    """Строго по дате регистрации в чате (ТЗ не выделяет владельца отдельно —
    он просто обычно оказывается первым, т.к. раньше всех в чате)."""
    docs = await members(with_role=True)
    docs.sort(key=lambda d: d.get("joined_chat_at") or now())
    return docs


async def admins_list() -> list[dict]:
    return [d async for d in users.find({"$or": [{"is_admin": True}, {"is_owner": True}]})]


async def count_admins() -> int:
    return await users.count_documents({"is_admin": True})


async def find_by_role(role_key: str) -> Optional[dict]:
    return await users.find_one({"role_key": role_key, "in_chat": True})


async def find_by_invite_link(link: str) -> Optional[dict]:
    return await users.find_one({"invite_link": link})


async def unregistered_users() -> list[dict]:
    """Просто активировавшие бота: не в чате (не зарегистрированы)."""
    return [d async for d in users.find({"in_chat": False})]


# ---------------------------------------------------------------------------
# РОЛИ
# ---------------------------------------------------------------------------

async def taken_roles(exclude_user: Optional[int] = None) -> set[str]:
    """Роли, занятые участниками, поданными/одобренными анкетами и бронями."""
    taken: set[str] = set()
    query = {"role_key": {"$ne": None}, "$or": [
        {"in_chat": True}, {"status": {"$in": [ST_PENDING, ST_APPROVED]}},
    ]}
    async for d in users.find(query):
        if d["_id"] != exclude_user:
            taken.add(d["role_key"])
    async for r in reservations_col.find({}):
        if r["user_id"] != exclude_user:
            taken.add(r["role_key"])
    return taken


async def role_taken(role_key: str, exclude_user: Optional[int] = None) -> bool:
    return role_key in await taken_roles(exclude_user)


# ---------------------------------------------------------------------------
# СТАТИСТИКА СООБЩЕНИЙ
# ---------------------------------------------------------------------------

async def record_message(user_id: int) -> None:
    t = now()
    day = msk_date_str(t)
    await msg_stats.update_one(
        {"_id": user_id},
        {"$inc": {"total": 1, f"days.{day}": 1}, "$setOnInsert": {"first_seen": t}},
        upsert=True,
    )
    await users.update_one({"_id": user_id}, {"$set": {"last_message_at": t}})


def window_count(stat: Optional[dict], period) -> int:
    """Сообщения за период по МСК (включая сегодня).
    period: int — последние N календарных дней; None — всё время;
    "month" — с 1-го числа текущего календарного месяца (то же окно,
    в которое попадает месячная чистка — она стартует последним числом
    месяца, поэтому "месяц" здесь и там должен значить одно и то же,
    а не произвольные последние 30 дней)."""
    if not stat:
        return 0
    day_map = stat.get("days", {})
    if period is None:
        return stat.get("total", 0)
    today = to_msk(now()).date()
    if period == "month":
        first = today.replace(day=1)
        days_elapsed = (today - first).days + 1
        return sum(day_map.get((today - dt.timedelta(days=i)).isoformat(), 0) for i in range(days_elapsed))
    return sum(day_map.get((today - dt.timedelta(days=i)).isoformat(), 0) for i in range(period))


async def get_stat(user_id: int) -> Optional[dict]:
    return await msg_stats.find_one({"_id": user_id})


async def top_active(period, limit: int = 3) -> list[tuple[dict, int]]:
    """Топ среди текущих участников: [(user_doc, count)]. period — см. window_count."""
    result = []
    for u in await members(with_role=True):
        result.append((u, window_count(await get_stat(u["_id"]), period)))
    result.sort(key=lambda x: x[1], reverse=True)
    return result[:limit]


async def totals(period) -> tuple[int, int, int]:
    """(всего, зарегистрированные участники, вышедшие) за период. period — см. window_count."""
    member_ids = {u["_id"] for u in await members()}
    total = member_total = 0
    async for s in msg_stats.find({}):
        c = window_count(s, period)
        total += c
        if s["_id"] in member_ids:
            member_total += c
    return total, member_total, total - member_total


# ---------------------------------------------------------------------------
# РЕСТ
# ---------------------------------------------------------------------------

async def active_resters() -> list[dict]:
    return [d async for d in users.find({"rest.resting": True})]


async def rest_banned_users() -> list[dict]:
    return [d async for d in users.find({"rest.rest_ban_until": {"$ne": None}, "rest.resting": False})]


async def can_take_rest() -> bool:
    return await users.count_documents({"rest.resting": True}) < config.MAX_CONCURRENT_RESTERS


async def start_rest(user_id: int, end_dt: dt.datetime) -> None:
    await users.update_one({"_id": user_id}, {
        "$set": {"rest.resting": True, "rest.rest_start": now(), "rest.rest_end": end_dt},
        "$inc": {"rest.rest_count": 1},
    })


async def end_rest(user_id: int) -> None:
    u = await get_user(user_id)
    t = now()
    await users.update_one({"_id": user_id}, {"$set": {
        "rest.resting": False, "rest.last_start": u["rest"]["rest_start"], "rest.last_end": t,
        "rest.rest_start": None, "rest.rest_end": None, "rest.ended_at": t,
        "rest.rest_ban_start": t, "rest.rest_ban_until": t + dt.timedelta(days=config.REST_BAN_DAYS),
        "rest.rest_ban_extensions": 0,
    }})


async def clear_rest_ban(user_id: int) -> None:
    await users.update_one({"_id": user_id}, {"$set": {
        "rest.rest_ban_until": None, "rest.rest_ban_start": None, "rest.rest_ban_extensions": 0,
    }})


async def extend_rest_ban(user_id: int) -> bool:
    """Лимит продлений растёт с каждым новым запретом: при первом запрете —
    2 продления, при втором — 3, и так далее (rest_count + 1)."""
    u = await get_user(user_id)
    limit = u["rest"].get("rest_count", 1) + 1
    if u["rest"].get("rest_ban_extensions", 0) >= limit:
        return False
    until = u["rest"]["rest_ban_until"] or now()
    await users.update_one({"_id": user_id}, {
        "$set": {"rest.rest_ban_until": until + dt.timedelta(days=config.REST_BAN_DAYS)},
        "$inc": {"rest.rest_ban_extensions": 1},
    })
    return True


# ---------------------------------------------------------------------------
# ЖАЛОБЫ
# ---------------------------------------------------------------------------

async def add_complaint(from_user: int, target_user: int, text: str) -> int:
    """Сохранить жалобу. Возвращает число УНИКАЛЬНЫХ жалобщиков на цель."""
    await complaints_col.insert_one({
        "from_user": from_user, "target_user": target_user, "text": text, "created_at": now(),
    })
    return len(await complaints_col.distinct("from_user", {"target_user": target_user}))


async def complainers_of(target_user: int) -> set[int]:
    return set(await complaints_col.distinct("from_user", {"target_user": target_user}))


# ---------------------------------------------------------------------------
# ПОДДЕРЖКА
# ---------------------------------------------------------------------------

async def create_ticket(user_id: int, topic: str, body: str) -> str:
    res = await support_col.insert_one({
        "user_id": user_id, "topic": topic, "body": body, "created_at": now(),
        "status": "open", "reply": None, "replied_by": None, "replied_at": None,
    })
    return str(res.inserted_id)


async def get_ticket(ticket_id: str) -> Optional[dict]:
    return await support_col.find_one({"_id": ObjectId(ticket_id)})


async def answer_ticket(ticket_id: str, admin_id: int, text: str) -> None:
    await support_col.update_one({"_id": ObjectId(ticket_id)}, {"$set": {
        "status": "answered", "reply": text, "replied_by": admin_id, "replied_at": now(),
    }})


# ---------------------------------------------------------------------------
# НАБОР
# ---------------------------------------------------------------------------

async def get_recruitment() -> dict:
    doc = await recruitment_col.find_one({"_id": "state"})
    if doc is None:
        doc = {
            "_id": "state", "status": "closed", "phase": 1, "wave": 1, "slots": 0, "joined": 0,
            "joined_ids": [], "wait_mode": False, "wait_until": None, "next_phase": None,
            "next_slots": None, "awaiting_slots": False, "reminder_sent": False,
            "ever_opened": False, "closed_by_user": None,
        }
        await recruitment_col.insert_one(doc)
    return doc


async def set_recruitment(fields: dict) -> None:
    await recruitment_col.update_one({"_id": "state"}, {"$set": fields}, upsert=True)


async def add_joined(user_id: int) -> bool:
    """Засчитать вход в фазу. Повторный вход того же id не считается."""
    res = await recruitment_col.update_one(
        {"_id": "state", "joined_ids": {"$ne": user_id}},
        {"$inc": {"joined": 1}, "$push": {"joined_ids": user_id}},
    )
    return res.modified_count > 0


async def remove_joined(user_id: int) -> None:
    await recruitment_col.update_one(
        {"_id": "state", "joined_ids": user_id},
        {"$inc": {"joined": -1}, "$pull": {"joined_ids": user_id}},
    )


# --- брони ---

async def create_reservation(doc: dict) -> None:
    await reservations_col.insert_one(doc)


async def get_reservation(user_id: int) -> Optional[dict]:
    return await reservations_col.find_one({"user_id": user_id})


async def reservation_by_role(role_key: str) -> Optional[dict]:
    return await reservations_col.find_one({"role_key": role_key})


async def delete_reservation(user_id: int) -> None:
    await reservations_col.delete_many({"user_id": user_id})


async def all_reservations() -> list[dict]:
    return [d async for d in reservations_col.find({}).sort("created_at", 1)]


async def reserved_outstanding() -> int:
    """Сколько мест фазы удерживают анкеты, пришедшие из брони и ещё не вошедшие."""
    return await users.count_documents({
        "from_reservation": True, "in_chat": False, "status": {"$in": [ST_PENDING, ST_APPROVED]},
    })


# ---------------------------------------------------------------------------
# РАССЫЛКИ
# ---------------------------------------------------------------------------

async def broadcasts_today(kind: str) -> int:
    doc = await broadcasts_col.find_one({"_id": f"{kind}:{msk_date_str(now())}"})
    return doc["count"] if doc else 0


async def inc_broadcasts_today(kind: str) -> None:
    await broadcasts_col.update_one({"_id": f"{kind}:{msk_date_str(now())}"}, {"$inc": {"count": 1}}, upsert=True)


# ---------------------------------------------------------------------------
# ЧИСТКИ
# ---------------------------------------------------------------------------

async def get_purge_settings() -> dict:
    doc = await purge_settings_col.find_one({"_id": "state"})
    if doc is None:
        doc = {
            "_id": "state", "enabled": True,
            "weekly_norm": config.WEEKLY_NORM_DEFAULT, "monthly_norm": config.MONTHLY_NORM_DEFAULT,
            "weekly_time": config.WEEKLY_PURGE_TIME_DEFAULT, "monthly_time": config.MONTHLY_PURGE_TIME_DEFAULT,
        }
        await purge_settings_col.insert_one(doc)
    return doc


async def set_purge_settings(fields: dict) -> None:
    await purge_settings_col.update_one({"_id": "state"}, {"$set": fields}, upsert=True)


async def get_night_settings() -> dict:
    """Настройки ночного режима: enabled, start/end (час по МСК), active — применён ли режим сейчас."""
    doc = await purge_settings_col.find_one({"_id": "night"})
    if doc is None:
        doc = {"_id": "night", "enabled": True, "start": config.NIGHT_MODE_START_HOUR,
               "end": config.NIGHT_MODE_END_HOUR, "active": None}
        await purge_settings_col.insert_one(doc)
    return doc


async def set_night_settings(fields: dict) -> None:
    await purge_settings_col.update_one({"_id": "night"}, {"$set": fields}, upsert=True)


async def record_purge(conducted: bool) -> None:
    await purge_stats_col.update_one(
        {"_id": "state"}, {"$inc": {"conducted": 1 if conducted else 0, "skipped": 0 if conducted else 1}}, upsert=True,
    )


async def get_purge_stats() -> dict:
    return await purge_stats_col.find_one({"_id": "state"}) or {"conducted": 0, "skipped": 0}


async def add_inactivity_kick(role: Optional[str]) -> None:
    await inactivity_col.insert_one({"role": role, "at": now()})


async def inactivity_kicks_since(days: int) -> list[str]:
    since = now() - dt.timedelta(days=days)
    return [d["role"] async for d in inactivity_col.find({"at": {"$gte": since}}) if d.get("role")]


# ---------------------------------------------------------------------------
# МОДЕРАЦИЯ
# ---------------------------------------------------------------------------

async def add_oral_warns(user_id: int, count: int = 1) -> tuple[int, int]:
    """Вернёт (устных после, сколько технических варнов добавилось). 3 устных = 1 варн."""
    u = await get_user(user_id)
    total = u.get("oral_warns", 0) + count
    new_warns = total // 3
    await users.update_one({"_id": user_id}, {"$set": {"oral_warns": total % 3}})
    if new_warns:
        await add_warns(user_id, new_warns)
    return total % 3, new_warns


async def add_warns(user_id: int, count: int = 1) -> int:
    u = await get_user(user_id)
    total = u.get("warns", 0) + count
    await users.update_one({"_id": user_id}, {"$set": {"warns": total}})
    return total


async def remove_warn(user_id: int) -> int:
    u = await get_user(user_id)
    total = max(0, u.get("warns", 0) - 1)
    await users.update_one({"_id": user_id}, {"$set": {"warns": total}})
    return total


async def inc_everyone_abuse(user_id: int) -> int:
    doc = await users.find_one_and_update(
        {"_id": user_id}, {"$inc": {"everyone_abuse_count": 1}}, return_document=ReturnDocument.AFTER,
    )
    return doc.get("everyone_abuse_count", 0) if doc else 0


async def add_violation(user_id: int, role: Optional[str], rule: str, code: str, label: str) -> None:
    await violations_col.insert_one({
        "user_id": user_id, "role": role, "rule": rule, "code": code, "label": label, "at": now(),
    })


async def violations_since(days: Optional[int]) -> list[dict]:
    query = {} if days is None else {"at": {"$gte": now() - dt.timedelta(days=days)}}
    return [d async for d in violations_col.find(query)]


async def rules_of_user(user_id: int) -> list[str]:
    return sorted({d["rule"] async for d in violations_col.find({"user_id": user_id})})


# ---------------------------------------------------------------------------
# ОПРОСЫ
# ---------------------------------------------------------------------------

async def create_poll(doc: dict) -> str:
    res = await polls_col.insert_one(doc)
    return str(res.inserted_id)


async def get_poll(poll_id: str) -> Optional[dict]:
    return await polls_col.find_one({"_id": ObjectId(poll_id)})


async def open_polls() -> list[dict]:
    return [d async for d in polls_col.find({"closed": False})]


async def set_poll_message(poll_id: str, user_id: int, message_id: int, tg_poll_id: str) -> None:
    await polls_col.update_one({"_id": ObjectId(poll_id)}, {"$set": {
        f"messages.{user_id}": message_id, f"tg_polls.{tg_poll_id}": user_id,
    }})


async def find_poll_by_tg(tg_poll_id: str) -> Optional[dict]:
    return await polls_col.find_one({f"tg_polls.{tg_poll_id}": {"$exists": True}, "closed": False})


async def record_vote(poll_id, user_id: int, option: int) -> None:
    await polls_col.update_one({"_id": poll_id}, {"$set": {f"votes.{user_id}": option}})


async def close_poll(poll_id) -> None:
    await polls_col.update_one({"_id": poll_id}, {"$set": {"closed": True}})


# ---------------------------------------------------------------------------
# ЗАМЕТКИ О ВЫХОДЕ, НАСТРОЙКИ
# ---------------------------------------------------------------------------

async def save_leave_note(user_id: int, role: str, reason: str) -> None:
    await leave_notes_col.update_one(
        {"_id": user_id}, {"$set": {"role": role, "reason": reason, "at": now()}}, upsert=True,
    )


async def pop_leave_note(user_id: int) -> Optional[str]:
    note = await leave_notes_col.find_one_and_delete({"_id": user_id})
    if note and now() - note["at"] <= dt.timedelta(minutes=config.LEAVE_NOTE_MINUTES):
        return note["reason"]
    return None


async def get_setting(key: str, default=None):
    doc = await settings_col.find_one({"_id": key})
    return doc["value"] if doc else default


async def set_setting(key: str, value) -> None:
    await settings_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)
