"""
services/bot_commands.py — меню команд Telegram (setMyCommands) в зависимости от чата и статуса пользователя.

Что видит человек в списке «/» :
  * личка, посторонний (нет в системе)     — /start
  * личка, участник                         — /menu, /start
  * личка, админ и владелец                 — /admin, /menu, /start
  * общий чат                               — /everyone только у админов и владельца бота, у остальных пусто
  * остальные чаты (например, чат логов)    — ничего

Работает через области действия (scope): общая для всех личек, личная для каждого участника/админа
(BotCommandScopeChat с chat_id = id пользователя) и персональная в основном чате для админов бота
(BotCommandScopeChatMember). Списки выставляются при /start, вступлении, назначении/снятии админа и при
запуске бота; у стёртых из базы удаляются.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeChat, BotCommandScopeChatMember, BotCommandScopeDefault,
)

import config
from database import methods as db

logger = logging.getLogger(__name__)

COMMANDS_OUTSIDER = [BotCommand(command="start", description="Начать работу с ботом")]
COMMANDS_MEMBER = [
    BotCommand(command="menu", description="Меню участника"),
    BotCommand(command="start", description="Перезапустить бота"),
]
COMMANDS_ADMIN = [
    BotCommand(command="admin", description="Панель администратора"),
    BotCommand(command="menu", description="Меню участника"),
    BotCommand(command="start", description="Перезапустить бота"),
]
COMMANDS_CHAT = [BotCommand(command="everyone", description="Созвать всех участников")]


async def _call(coro) -> None:
    """Выполнить вызов Bot API; одна повторная попытка при лимите, остальные ошибки — в лог."""
    try:
        await coro
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 1)
        logger.warning("Лимит Telegram при настройке команд — пропускаю одну операцию.")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        # Например, «chat not found»: человек ещё ни разу не писал боту — для него команды не нужны.
        logger.debug("Не удалось настроить команды: %s", e)
    except Exception:  # noqa: BLE001
        logger.exception("Ошибка при настройке команд")


async def setup_global(bot: Bot) -> None:
    """Общие списки: убираем старый общий набор, ставим наборы для личек и основного чата."""
    await _call(bot.delete_my_commands(scope=BotCommandScopeDefault()))
    await _call(bot.delete_my_commands(scope=BotCommandScopeAllGroupChats()))
    await _call(bot.set_my_commands(COMMANDS_OUTSIDER, scope=BotCommandScopeAllPrivateChats()))
    # В основном чате общего списка нет: /everyone показывается только админам (см. refresh_user_commands).
    await _call(bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=config.MAIN_CHAT_ID)))


def commands_for(user_id: int, user: Optional[dict]) -> Optional[list[BotCommand]]:
    """Личный список команд по статусу; None — личного списка нет (посторонний видит общий)."""
    if user_id == config.OWNER_ID or (user and (user.get("is_owner") or user.get("is_admin"))):
        return COMMANDS_ADMIN
    if user and user.get("in_chat") and user.get("has_bot_access"):
        return COMMANDS_MEMBER
    return None


async def refresh_user_commands(bot: Bot, user_id: int) -> None:
    """Привести личный список команд пользователя в соответствие с его статусом в базе."""
    user = await db.get_user(user_id)
    cmds = commands_for(user_id, user)
    scope = BotCommandScopeChat(chat_id=user_id)
    chat_scope = BotCommandScopeChatMember(chat_id=config.MAIN_CHAT_ID, user_id=user_id)
    if cmds is None:
        await _call(bot.delete_my_commands(scope=scope))
    else:
        await _call(bot.set_my_commands(cmds, scope=scope))
    if cmds is COMMANDS_ADMIN:
        await _call(bot.set_my_commands(COMMANDS_CHAT, scope=chat_scope))  # /everyone — только админам
    else:
        await _call(bot.delete_my_commands(scope=chat_scope))


async def resync_all(bot: Bot) -> None:
    """При запуске: выставить личные списки всем участникам, админам и владельцу."""
    ids = {config.OWNER_ID}
    ids.update(d["_id"] for d in await db.admins_list())
    ids.update(d["_id"] for d in await db.members(with_role=True))
    for user_id in ids:
        await refresh_user_commands(bot, user_id)
        await asyncio.sleep(0.1)  # не упираться в лимиты Telegram


async def drain_cleanup(bot: Bot) -> None:
    """Убрать личные списки у тех, кого стёрли из базы (вышел, исключён, забанен)."""
    while db.pending_command_cleanup:
        user_id = db.pending_command_cleanup.pop()
        await refresh_user_commands(bot, user_id)
        await asyncio.sleep(0.1)
