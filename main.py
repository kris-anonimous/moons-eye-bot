"""
main.py — точка входа. FastAPI-сервер (нужен Hugging Face Spaces) + aiogram
в режиме вебхука. Состояния анкет хранятся в MongoDB (MongoStorage), поэтому
перезапуск/пересборка Space не обнуляет заполняемые пользователями анкеты.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import ChatMemberUpdated, Update
from fastapi import FastAPI, Request, Response

import config
from database import methods as db
from database.storage import MongoStorage
from handlers import admin, chat_moderation, menu, registration
from services import pure
from services.scheduler import setup_scheduler
from services.utils import esc, gform, main_chat, notify_owner, role_of, sync_restrictions, tg_user_line

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MongoStorage())

# Порядок важен: более специфичные роутеры — раньше catch-all в chat_moderation.
dp.include_router(registration.router)
dp.include_router(menu.router)
dp.include_router(admin.router)
dp.include_router(chat_moderation.router)
dp.message.outer_middleware(chat_moderation.DmRaidGuardMiddleware())


@dp.my_chat_member()
async def on_bot_membership_changed(update: ChatMemberUpdated):
    """Блокировка/разблокировка (и перезапуск) бота пользователем в личке."""
    if update.chat.type != "private":
        return
    user_id = update.chat.id
    new_status, old_status = update.new_chat_member.status, update.old_chat_member.status

    if new_status == "kicked":  # заблокировал бота
        user = await db.get_user(user_id)
        in_system = bool(user and user.get("in_chat") and user.get("has_bot_access"))
        report = (
            f"Блокировка!\n\nВремя: {pure.fmt_msk(db.now())} МСК\n\n"
            f"Юзер: {tg_user_line(update.from_user)}\n\n"
            f"В системе: {'да, ' + esc(role_of(user)) if in_system else 'нет'}"
        )
        await notify_owner(bot, report)
        if user:
            await db.set_fields(user_id, {"blocked_bot": True})
        if in_system:
            await sync_restrictions(bot, user_id)
            await main_chat(
                bot,
                f"{gform(user, 'Уважаемый', 'Уважаемая')} {esc(role_of(user))}, просим не блокировать нашего бота, иначе вы теряете "
                f"доступ как к чату, так и к его составляющим. Заглушение будет снято после разблокировки бота.",
            )

    elif old_status == "kicked" and new_status == "member":  # разблокировал/перезапустил
        user = await db.get_user(user_id)
        if user and user.get("blocked_bot"):
            await db.set_fields(user_id, {"blocked_bot": False})
            if user.get("in_chat") and user.get("has_bot_access"):
                await sync_restrictions(bot, user_id)


scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scheduler
    if config.BASE_WEBHOOK_URL:
        webhook_url = f"{config.BASE_WEBHOOK_URL}{config.WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url, allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=True)
        logger.info("Вебхук установлен: %s", webhook_url)
    else:
        logger.warning("BASE_WEBHOOK_URL не задан — вебхук не установлен. Задайте переменную окружения.")

    await bot.set_my_commands([
        {"command": "start", "description": "Начать работу с ботом"},
        {"command": "menu", "description": "Меню участника"},
        {"command": "admin", "description": "Панель администратора"},
        {"command": "everyone", "description": "Созыв всех участников (только админы)"},
    ])

    scheduler = await setup_scheduler(bot)
    scheduler.start()
    dp["scheduler"] = scheduler  # доступен в хендлерах как параметр `scheduler: AsyncIOScheduler`
    logger.info("Планировщик запущен.")

    yield

    scheduler.shutdown(wait=False)
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.post(config.WEBHOOK_PATH)
async def webhook_handler(request: Request):
    data = await request.json()
    update = Update.model_validate(data, context={"bot": bot})
    await dp.feed_update(bot, update)
    return Response(status_code=200)


@app.get("/")
async def health_check():
    return {"status": "ok", "bot": "moons_eye_bot"}


if __name__ == "__main__":
    uvicorn.run(app, host=config.WEB_SERVER_HOST, port=config.WEB_SERVER_PORT)
