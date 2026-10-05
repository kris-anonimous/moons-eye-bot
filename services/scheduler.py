"""
services/scheduler.py — все фоновые задачи (APScheduler, таймзона UTC,
время вычисляется через МСК = UTC+3 внутри задач).
"""

from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatPermissions
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from database import methods as db
from services import pure, recruitment, texts
from services.utils import (
    assign_role_tag, check_subscriptions, doc_user_line, esc, kick_member,
    main_chat, notify_admins, role_of, send, sync_restrictions,
)

logger = logging.getLogger(__name__)


def _cron_msk(hour: int, minute: int = 0, **kw) -> CronTrigger:
    """CronTrigger в терминах UTC-часа, эквивалентного заданному часу по МСК."""
    return CronTrigger(hour=(hour - config.MSK_OFFSET_HOURS) % 24, minute=minute, **kw)


# ---------------------------------------------------------------------------
# КАПЧА
# ---------------------------------------------------------------------------

async def job_invite_expiry(bot: Bot):
    """ТЗ: «если юзер не подаёт заявку на вступление в течение 5 часов,
    ссылка сбрасывается» — это должно происходить, даже если человек вообще
    ни разу не кликнул по ссылке (а не только в момент попытки входа)."""
    now = db.now()
    async for u in db.users.find({
        "status": db.ST_APPROVED, "invite_deadline": {"$ne": None, "$lte": now},
    }):
        if u.get("invite_link"):
            try:
                await bot.revoke_chat_invite_link(config.MAIN_CHAT_ID, u["invite_link"])
            except Exception:  # noqa: BLE001
                pass
        await db.set_fields(u["_id"], {"status": db.ST_REJECTED, "invite_link": None, "invite_deadline": None})
        await texts.send_key(bot, u["_id"], "invite_expired")


async def job_captcha(bot: Bot):
    now = db.now()
    async for u in db.users.find({"captcha_deadline": {"$ne": None, "$lte": now}}):
        user_id = u["_id"]
        for chat_id, mid in u.get("captcha_message_ids", []):
            try:
                await bot.delete_message(chat_id, mid)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        await kick_member(bot, user_id)
        await recruitment.rollback_join(bot, user_id)
        await db.erase_user(user_id)
        await notify_admins(
            bot, f"{doc_user_line(u)} не прошёл(а) капчу за 5 минут — исключён(а) и удалён(а) из базы данных."
        )


# ---------------------------------------------------------------------------
# НОЧНОЙ РЕЖИМ
# ---------------------------------------------------------------------------

def _no_media_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True, can_send_audios=False, can_send_documents=False, can_send_photos=False,
        can_send_videos=False, can_send_video_notes=False, can_send_voice_notes=False,
        can_send_polls=True, can_send_other_messages=False, can_add_web_page_previews=False,
    )


async def job_night_on(bot: Bot):
    try:
        await bot.set_chat_permissions(config.MAIN_CHAT_ID, permissions=_no_media_permissions())
        await main_chat(bot, "🌙 Включён ночной режим (23:00–05:00 МСК): вложения запрещены всем, кроме админов.")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось включить ночной режим: %s", e)


async def job_night_off(bot: Bot):
    try:
        full = ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True, can_send_photos=True,
            can_send_videos=True, can_send_video_notes=True, can_send_voice_notes=True,
            can_send_polls=True, can_send_other_messages=True, can_add_web_page_previews=True,
        )
        await bot.set_chat_permissions(config.MAIN_CHAT_ID, permissions=full)
        await main_chat(bot, "☀️ Ночной режим выключен: вложения снова разрешены всем.")
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось выключить ночной режим: %s", e)


# ---------------------------------------------------------------------------
# ДНИ РОЖДЕНИЯ
# ---------------------------------------------------------------------------

async def job_birthdays(bot: Bot):
    today = pure.to_msk(db.now()).date()
    in_two = today + dt.timedelta(days=2)
    members = await db.members(with_role=True)
    for m in members:
        bday = m.get("birthday")
        if not bday:
            continue
        day, month = (int(x) for x in bday.split("."))
        if (day, month) == (in_two.day, in_two.month):
            for other in members:
                if other["_id"] == m["_id"]:
                    continue
                await send(bot, other["_id"],
                          f"Через 2 дня у {esc(m['role_key'])} будет красная дата. "
                          f"Не забудьте поздравить именинника с важным днём…")
        if (day, month) == (today.day, today.month):
            un = f", @{m['username']}" if m.get("username") else ""
            from services.utils import send_everyone
            await send_everyone(bot, f"Поздравим {esc(m['role_key'])}{esc(un)} с днём рождения! "
                                      f"Желаем славно отметить этот важный день.")


# ---------------------------------------------------------------------------
# РЕСТ
# ---------------------------------------------------------------------------

async def job_rest_expiry(bot: Bot):
    now = db.now()
    for u in await db.active_resters():
        if u["rest"]["rest_end"] and u["rest"]["rest_end"] <= now:
            await db.end_rest(u["_id"])
            await sync_restrictions(bot, u["_id"])
            await assign_role_tag(bot, u["_id"], u["role_key"], resting=False)
            gw = "его" if u.get("gender") != "ж" else "её"
            await main_chat(bot, f"{esc(u['role_key'])} выходит из реста. Узнайте, как прошёл {gw} отдых!")
            await texts.send_key(bot, u["_id"], "rest_over_dm")
            await notify_admins(bot, f"{esc(u['role_key'])} ({doc_user_line(u)}) вышел(а) из реста. "
                                     f"Запрет на рест активен в течение 7-ми дней.")


async def job_rest_ban_expiry(bot: Bot):
    now = db.now()
    for u in await db.rest_banned_users():
        if u["rest"]["rest_ban_until"] and u["rest"]["rest_ban_until"] <= now:
            await db.clear_rest_ban(u["_id"])


async def job_rest_inactivity(bot: Bot):
    now = db.now()
    for u in await db.rest_banned_users():
        started = u["rest"].get("rest_ban_start")
        if not started or now - started < dt.timedelta(days=config.REST_INACTIVITY_KICK_DAYS):
            continue
        last = u.get("last_message_at")
        if last and last >= started:
            continue
        role = role_of(u)
        await kick_member(bot, u["_id"])
        await db.erase_user(u["_id"])
        await db.add_inactivity_kick(role)
        await main_chat(bot, f"{esc(role)} покидает наше сообщество. Причина: неактивность после реста.")
        await notify_admins(bot, f"{esc(role)} исключён(а) в {pure.fmt_msk(now)} за неактивность "
                                 f"в течение 3-х дней после реста.")


# ---------------------------------------------------------------------------
# ОБЫЧНАЯ НЕАКТИВНОСТЬ (вне чисток) — «удалять участников, неактивных более 4-х дней»
# ---------------------------------------------------------------------------

async def job_inactivity(bot: Bot):
    now = db.now()
    # ТЗ в двух местах называет разные числа: в отображаемых настройках чисток —
    # "дней неактивности до бана: 3", а в описании самого правила — "неактивных
    # более 4-х дней". Реконструкция: 3 — это "запас", а кикаем строго когда
    # неактивность УЖЕ СТРОГО БОЛЬШЕ 4 дней, то есть порог = INACTIVITY_KICK_DAYS + 1.
    threshold = now - dt.timedelta(days=config.INACTIVITY_KICK_DAYS + 1)
    for u in await db.members(with_role=True):
        if u.get("is_admin") or u.get("is_owner") or u["rest"]["resting"]:
            continue
        last = u.get("last_message_at") or u.get("joined_chat_at")
        if last and last < threshold:
            role = role_of(u)
            await kick_member(bot, u["_id"])
            await db.erase_user(u["_id"])
            await db.add_inactivity_kick(role)
            await main_chat(bot, f"{esc(role)} покидает наше сообщество. Причина: неактивность.")
            await notify_admins(bot, f"{esc(role)} исключён(а) в {pure.fmt_msk(now)} за неактивность "
                                     f"более {config.INACTIVITY_KICK_DAYS}-х дней.")


# ---------------------------------------------------------------------------
# ЧИСТКИ
# ---------------------------------------------------------------------------

async def _run_purge(bot: Bot, kind: str):
    settings = await db.get_purge_settings()
    if not settings.get("enabled", True):
        await db.record_purge(conducted=False)
        return

    days = 7 if kind == "weekly" else 30
    period = 7 if kind == "weekly" else "month"
    min_needed = config.WEEKLY_PURGE_MIN_PARTICIPANTS if kind == "weekly" else config.MONTHLY_PURGE_MIN_PARTICIPANTS
    norm = settings["weekly_norm"] if kind == "weekly" else settings["monthly_norm"]
    threshold = config.WEEKLY_TOP_THRESHOLD if kind == "weekly" else config.MONTHLY_TOP_THRESHOLD

    plain = [u for u in await db.members(with_role=True) if not u.get("is_admin") and not u.get("is_owner")]
    already_kicked = await db.inactivity_kicks_since(days)

    if len(plain) < min_needed:
        await db.record_purge(conducted=False)
        report = (f"Отчёт о чистке:\n\nТип чистки: {'недельная' if kind == 'weekly' else 'месячная'}\n\n"
                  f"Чистка не проводилась: недостаточно участников ({len(plain)} из {min_needed}).\n\n"
                  f"Выбыли за неактив во время недели/месяца: "
                  f"{', '.join(already_kicked) if already_kicked else '—'}")
        await notify_admins(bot, report)
        return

    try:
        await bot.set_chat_permissions(config.MAIN_CHAT_ID, permissions=ChatPermissions(can_send_messages=False))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    passed, deficits, kicked_deficit = [], [], []
    for u in plain:
        stat = await db.get_stat(u["_id"])
        count = db.window_count(stat, period)
        if count < norm:
            deficits.append(u)
            u_deficits = u.get("purge_deficits", 0) + 1
            await db.set_fields(u["_id"], {"purge_deficits": u_deficits})
            if u_deficits >= 2:
                kicked_deficit.append(u)
                await kick_member(bot, u["_id"])
                await db.erase_user(u["_id"])
                await notify_admins(bot, f"{esc(role_of(u))} исключён(а): повторный недобор нормы сообщений.")
            else:
                await db.add_warns(u["_id"], 1)
                await db.add_violation(u["_id"], role_of(u), "чистка", "warn", "недобор")
        else:
            passed.append(u)
            await db.set_fields(u["_id"], {"purge_deficits": 0})

    try:
        from services.utils import default_permissions
        await bot.set_chat_permissions(config.MAIN_CHAT_ID, permissions=await default_permissions(bot))
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    from services.utils import send_everyone
    await send_everyone(bot, "чистка завершена")
    for u in passed:
        await send(bot, u["_id"], "Вы успешно прошли чистку.")

    top = await db.top_active(period, limit=3)
    top_block = ""
    if len(top) == 3 and top[2][1] > threshold:
        lines = [f"{i}-е место: {esc(role_of(u))}; {c}" for i, (u, c) in enumerate(top, 1)]
        top_block = f"\n\n3 самых активных участника {'недели' if kind == 'weekly' else 'месяца'}:\n" + "\n".join(lines)

    total, _, _ = await db.totals(period)
    report = (
        f"Отчёт о чистке:\n\nТип чистки: {'недельная' if kind == 'weekly' else 'месячная'}\n\n"
        f"Выбыли: {', '.join(esc(role_of(u)) for u in kicked_deficit) or '—'}\n\n"
        f"Выбыли за неактив во время недели/месяца: {', '.join(already_kicked) if already_kicked else '—'}\n\n"
        f"Недобрали: {', '.join(esc(role_of(u)) for u in deficits) or '—'}\n\n"
        f"Сообщений за {'неделю' if kind == 'weekly' else 'месяц'}: {total}"
        f"{top_block}"
    )
    await notify_admins(bot, report)
    await db.record_purge(conducted=True)


async def job_weekly_purge(bot: Bot):
    await _run_purge(bot, "weekly")


async def job_monthly_purge(bot: Bot):
    import calendar
    today = pure.to_msk(db.now()).date()
    if today.day != calendar.monthrange(today.year, today.month)[1]:
        return
    await _run_purge(bot, "monthly")


# ---------------------------------------------------------------------------
# ОПРОСЫ
# ---------------------------------------------------------------------------

async def job_polls(bot: Bot):
    now = db.now()
    for p in await db.open_polls():
        if p["expires_at"] > now:
            continue
        await db.close_poll(p["_id"])
        votes = p.get("votes", {})
        options = p["options"]
        total = len(votes)
        stat_lines = []
        for i, opt in enumerate(options):
            c = sum(1 for v in votes.values() if v == i)
            pct = (c / total * 100) if total else 0
            stat_lines.append(f"за вариант {i + 1} («{esc(opt)}») — {pct:.1f}%")

        for uid_str, mid in p.get("messages", {}).items():
            try:
                await bot.delete_message(int(uid_str), mid)
            except (TelegramBadRequest, TelegramForbiddenError):
                pass

        rows = []
        for u in await db.members(with_role=True):
            uid = str(u["_id"])
            v = votes.get(uid)
            rows.append(f"{esc(role_of(u))}: {'вариант ' + str(v + 1) if v is not None else 'не голосовал(а)'}")

        text = (f"Опрос «{esc(p['title'])}» завершён.\n\n" + "\n".join(stat_lines) +
                "\n\nГолоса участников:\n" + "\n".join(rows))
        await send(bot, config.OWNER_ID, text)


# ---------------------------------------------------------------------------
# НАБОР
# ---------------------------------------------------------------------------

async def job_recruitment(bot: Bot):
    await recruitment.tick(bot)


# ---------------------------------------------------------------------------
# ПОДПИСКИ
# ---------------------------------------------------------------------------

async def job_subscriptions(bot: Bot):
    for m in await db.members(with_role=True):
        ok = await check_subscriptions(bot, m["_id"])
        was_ok = m.get("subscribed_ok", True)
        if ok == was_ok:
            continue
        await db.set_fields(m["_id"], {"subscribed_ok": ok})
        await sync_restrictions(bot, m["_id"])
        if not ok:
            await texts.send_key(bot, m["_id"], "sub_lost_dm")


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

async def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = await db.get_purge_settings()
    weekly_h, weekly_m = (int(x) for x in settings["weekly_time"].split(":"))
    monthly_h, monthly_m = (int(x) for x in settings["monthly_time"].split(":"))

    s = AsyncIOScheduler(timezone="UTC")
    s.add_job(job_captcha, "interval", minutes=1, args=[bot], max_instances=1)
    s.add_job(job_invite_expiry, "interval", minutes=5, args=[bot], max_instances=1)
    s.add_job(job_night_on, _cron_msk(config.NIGHT_MODE_START_HOUR), args=[bot])
    s.add_job(job_night_off, _cron_msk(config.NIGHT_MODE_END_HOUR), args=[bot])
    s.add_job(job_birthdays, _cron_msk(0), args=[bot])
    s.add_job(job_rest_expiry, "interval", minutes=5, args=[bot], max_instances=1)
    s.add_job(job_rest_ban_expiry, "interval", minutes=30, args=[bot], max_instances=1)
    s.add_job(job_rest_inactivity, "interval", hours=1, args=[bot], max_instances=1)
    s.add_job(job_inactivity, "interval", hours=1, args=[bot], max_instances=1)
    s.add_job(job_weekly_purge, _cron_msk(weekly_h, weekly_m, day_of_week="sat"),
              args=[bot], id="weekly_purge", replace_existing=True)
    s.add_job(job_monthly_purge, _cron_msk(monthly_h, monthly_m),
              args=[bot], id="monthly_purge", replace_existing=True)
    s.add_job(job_polls, "interval", minutes=1, args=[bot], max_instances=1)
    s.add_job(job_recruitment, "interval", minutes=10, args=[bot], max_instances=1)
    s.add_job(job_subscriptions, "interval", minutes=20, args=[bot], max_instances=1)
    return s


def reschedule_purge_job(scheduler: AsyncIOScheduler, kind: str, hour: int, minute: int) -> None:
    """Применить новое время чистки СРАЗУ, без перезапуска бота (вызывается
    из админ-панели после изменения времени)."""
    job_id = "weekly_purge" if kind == "weekly" else "monthly_purge"
    kwargs = {"day_of_week": "sat"} if kind == "weekly" else {}
    scheduler.reschedule_job(job_id, trigger=_cron_msk(hour, minute, **kwargs))
