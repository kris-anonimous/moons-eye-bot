"""
services/punishments.py — единая система наказаний (по образцу IRIS BOT).

Лестница: устный варн (3 шт. = 1 варн) -> варн (4 варна = исключение) ->
бан (исключение, вернуться можно) -> вечный бан (все площадки, без возврата).
Модерация «слепа» только к владельцу; админы наказываются как все.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from aiogram import Bot

import config
from database import methods as db
from services import pure, recruitment
from services.utils import (
    ban_everywhere, esc, kick_member, log, main_chat, notify_admins, role_of, sync_restrictions,
)

logger = logging.getLogger(__name__)

LABELS = {
    "oral": "устный варн", "oral2": "устный варн 2 шт.", "oral3": "устный варн 3 шт.",
    "warn": "варн", "warn2": "варн 2 шт.", "warn3": "варн 3 шт.",
    "ban": "бан", "perm": "вечный бан",
}


def label_of(code: str, mute_seconds: Optional[int] = None) -> str:
    if code == "mute":
        return f"мут {pure.human_duration(dt.timedelta(seconds=mute_seconds or 3600))}"
    return LABELS.get(code, code)


async def remove_participant(bot: Bot, user_id: int, permanent: bool) -> None:
    """Убрать участника из чата и стереть его данные."""
    await recruitment.annul_reservation(bot, user_id)
    await kick_member(bot, user_id)
    await db.erase_user(user_id)
    if permanent:
        # ban_everywhere сам ставит bot_banned=True/status=banned — вызываем
        # его ПОСЛЕ erase_user, иначе стирание сотрёт только что выставленный флаг.
        await ban_everywhere(bot, user_id)


async def apply_penalty(bot: Bot, user_id: int, rule: str, code: str, *, mute_seconds: Optional[int] = None,
                        announce: bool = True, reason: str = "") -> bool:
    """Выдать наказание. Возвращает False, если цель — владелец или её нет в базе."""
    user = await db.get_user(user_id)
    if not user or user.get("is_owner"):
        return False
    role = role_of(user)
    label = label_of(code, mute_seconds)
    tail = ""
    remove: Optional[bool] = None  # None — не удалять, False — кик, True — вечный бан

    if code in ("oral", "oral2", "oral3"):
        await db.add_oral_warns(user_id, {"oral": 1, "oral2": 2, "oral3": 3}[code])
    elif code in ("warn", "warn2", "warn3"):
        count = {"warn": 1, "warn2": 2, "warn3": 3}[code]
        total = await db.add_warns(user_id, count)
    elif code == "mute":
        until = db.now() + dt.timedelta(seconds=mute_seconds or 3600)
        await db.set_fields(user_id, {"muted_until": until})
        await sync_restrictions(bot, user_id)
    elif code == "ban":
        remove = False
    elif code == "perm":
        remove = True

    # 4 варна = исключение
    fresh = await db.get_user(user_id)
    if remove is None and fresh and fresh.get("warns", 0) >= 4:
        remove = False
        tail = " → исключение (4 варна)"

    await db.add_violation(user_id, role, rule, code, label)

    if announce:
        await main_chat(bot, f"Нарушение по {esc(rule)}\nНаказание: {esc(label)}{esc(tail)}")
    await notify_admins(
        bot,
        f"Нарушение!\n\nРоль: {esc(role)};\n\nПравило: {esc(rule)};\n\n"
        f"Наказание: {esc(label)}{esc(tail)};\n\nВремя: {pure.fmt_msk(db.now())}"
        + (f"\n\nПричина: {esc(reason)}" if reason else ""),
        log_it=True,
    )

    if remove is not None:
        await remove_participant(bot, user_id, permanent=remove)
    return True


async def undo_mute(bot: Bot, user_id: int) -> None:
    await db.set_fields(user_id, {"muted_until": None, "complaint_muted": False})
    await sync_restrictions(bot, user_id)


async def undo_warn(user_id: int) -> int:
    return await db.remove_warn(user_id)


async def undo_ban(bot: Bot, user_id: int) -> bool:
    """«Разбан» (кроме вечного бана — его снять нельзя)."""
    doc = await db.get_user(user_id)
    if doc and doc.get("bot_banned"):
        return False
    try:
        await bot.unban_chat_member(config.MAIN_CHAT_ID, user_id, only_if_banned=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось разбанить %s: %s", user_id, e)
        return False
    return True
