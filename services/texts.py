"""
services/texts.py — все редактируемые тексты бота.

Владелец может через /admin -> «Редактирование» изменить любой текст из
реестра TEXTS (с форматированием Telegram, цитатами, премиум-эмодзи и
до 2 фото). Значения по умолчанию — из ТЗ. Динамические части (списки
рестников, дней рождения и т.п.) бот формирует сам и добавляет ПОД
редактируемым текстом.

В шаблонах можно использовать подстановки вида {role}, {date}, {link} —
список доступных подстановок показывается при редактировании.
"""

from __future__ import annotations

import html
import logging
from typing import Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InputMediaPhoto, Message

import config
from config import db

logger = logging.getLogger(__name__)
texts_col = db["texts"]


class Raw(str):
    """Значение подстановки, которое НЕ нужно экранировать (готовый HTML)."""


SECTIONS = {
    "join": "Первые этапы вступления",
    "member": "Панель участника",
    "admin": "Панель админа",
    "tech": "Техническая панель",
}

_ABOUT_TAIL = {"member": "Вы в обычном режиме.", "admin": "Вы в административном режиме."}

# key: (section, название кнопки, доступные подстановки, текст по умолчанию)
TEXTS: dict[str, tuple[str, str, str, str]] = {
    # ------------------------------------------------------------ вступление
    "start_welcome": ("join", "Приветствие /start", "",
                      "Приветствуем! Это бот проекта «Moons Eye». Нажмите кнопку ниже, чтобы подать заявку на вступление."),
    "closed_notice": ("join", "Набор закрыт", "",
                      "На данный момент набор в наш проект закрыт. Загляните позже."),
    "wait_mode_notice": ("join", "Режим ожидания (бронь)", "",
                         "Приветствуем!\n\nОднако, сейчас мы находимся в подготовке к следующей Фазе. "
                         "Вы можете поставить бронь на роль и вступить тогда, когда наступит новая Фаза набора!"),
    "forced_wait": ("join", "Принудительное ожидание волны", "",
                    "Вы переведены в режим ожидания следующей волны набора. Подача анкеты станет возможной при следующей волне."),
    "sub_required": ("join", "Нет подписки на каналы", "",
                     "Без подписки на наши каналы регистрация и дальнейшее использование бота недоступны."),
    "role_prompt": ("join", "Запрос роли", "",
                    "Отлично! Напишите имя персонажа (роль), которую хотите занять. Нужна одна роль из списка."),
    "role_multiple": ("join", "Введено несколько ролей", "", "Пожалуйста, введите только одну роль."),
    "role_unknown": ("join", "Роль не найдена", "",
                     "Такой роли нет в списке. Пожалуйста, введите одно имя из списка ещё раз."),
    "role_taken": ("join", "Роль занята", "{role}",
                   "Роль «{role}» уже занята. Пожалуйста, выберите другую из свободных."),
    "role_error": ("join", "Не удалось распознать роль (сбой)", "",
                   "Не удалось обработать ответ. Пожалуйста, напишите роль ещё раз чуть позже."),
    "birthday_prompt": ("join", "Запрос даты рождения", "",
                        "Введите вашу дату рождения в формате ДД.ММ (например, 24.05)."),
    "birthday_invalid": ("join", "Дата рождения неверна", "",
                         "Такой даты не существует. Введите дату рождения в формате ДД.ММ (например, 24.05)."),
    "username_prompt": ("join", "Запрос юзернейма", "",
                        "Отправьте ваш юзернейм в формате @user."),
    "username_missing": ("join", "Нет юзернейма", "",
                         "У вас не установлен юзернейм. Установите его в настройках Telegram на время принятия в чат — "
                         "у вас есть 30 минут, иначе заявка будет отменена. После этого отправьте его в формате @user."),
    "username_wrong": ("join", "Неверный юзернейм", "",
                       "Это не ваш юзернейм. Отправьте свой настоящий юзернейм в формате @user."),
    "username_expired": ("join", "Время на юзернейм вышло", "",
                         "Время на установку юзернейма истекло, заявка отменена. Чтобы начать заново, нажмите /start."),
    "agreement": ("join", "Пользовательское соглашение", "{link}",
                  "Убедитесь, что прочли пользовательское соглашение: {link}"),
    "application_sent": ("join", "Заявка отправлена", "", "Ваша заявка на рассмотрении."),
    "pending_notice": ("join", "Заявка уже на рассмотрении", "",
                       "Ваша заявка уже находится на рассмотрении. Ожидайте решения администрации."),
    "app_approved": ("join", "Заявка одобрена", "{link}",
                     "Ваша заявка одобрена! Подайте заявку на вступление по ссылке (действует 5 часов): {link}"),
    "app_rejected": ("join", "Заявка отклонена", "",
                     "К сожалению, ваша заявка была отклонена администрацией."),
    "invite_expired": ("join", "Ссылка истекла", "", "Время ожидания истекло. Подайте анкету заново."),
    "raid_notice": ("join", "Рейдерский переход", "",
                    "Замечен рейдорский переход. Мы выдаём Вам предупреждение: следующая подача заявки будет "
                    "возможна при следующей волне набора."),
    "ban_notice": ("join", "Бан (при /start)", "",
                   "Вы находитесь в чёрном списке проекта и не можете пользоваться ботом."),
    "no_slots": ("join", "Места в фазе закончились", "",
                 "К сожалению, места в текущей Фазе набора закончились. Дождитесь следующей."),
    "reservation_done": ("join", "Бронь принята", "{role}",
                         "Ваша бронь на роль «{role}» принята. Мы уведомим вас о начале новой Фазы набора."),
    "reservation_started": ("join", "Началась новая Фаза (бронь)", "{role}",
                            "Началась новая Фаза набора! Ваша анкета на роль «{role}» передана администрации на рассмотрение."),
    "reservation_exists": ("join", "Бронь уже стоит", "{role}",
                           "У вас уже стоит бронь на роль «{role}». Дождитесь начала новой Фазы набора."),
    "reservation_cancelled": ("join", "Бронь отменена", "", "Ваша бронь отменена."),
    "reservation_annulled": ("join", "Бронь аннулирована", "{role}",
                             "Ваша бронь на роль «{role}» аннулирована: роль стала недоступна."),
    # ------------------------------------------------------- панель участника
    "menu_greeting": ("member", "Меню: приветствие", "{role}", "Приветствую вновь, {role}."),
    "sub_lost_dm": ("member", "Отписка от канала (ЛС)", "",
                    "Функции бота стали недоступными, пока вы не подпишетесь обратно на наши каналы. "
                    "В общем чате вы заглушены до подписки."),
    "birthdays_header": ("member", "Дни рождения: заголовок", "", "Дни рождения участников:"),
    "members_header": ("member", "Участники: заголовок", "", "Участники чата:"),
    "rest_header": ("member", "Рест: текст над списком", "",
                    "Рест – время, во время которого участник может отдохнуть от чата."),
    "rest_none": ("member", "Рест: никого нет", "", "<i>сейчас никто не в ресте…</i>"),
    "rest_full": ("member", "Рест: мест нет", "", "все места заняты, рест взять невозможно"),
    "rest_prompt": ("member", "Рест: запрос даты", "",
                    "Рест – время, во время которого вы сможете отдохнуть от чата. Максимальный его срок – 2 недели. "
                    "Напишите, до какого срока вы бы хотели взять рест в формате ДД.ММ (например, до 24.05. "
                    "Вписать нужно только дату).\n!Предупреждаем, что во время реста вы не сможете писать в группу."),
    "rest_taken_dm": ("member", "Рест взят (ЛС)", "{date}", "Вы взяли рест до {date}. Это действие нельзя отменить."),
    "rest_over_dm": ("member", "Рест закончился (ЛС)", "",
                     "Ваш рест подошел к концу. Теперь вы не можете взять рест в течение 7-ми дней."),
    "rest_cancelled_dm": ("member", "Рест прерван владельцем (ЛС)", "",
                          "Ваш рест был прерван. Подробности можно уточнить у Поддержки. "
                          "Вам запрещено брать рест в течение 7-ми дней."),
    "complaint_intro": ("member", "Жалобная: описание", "",
                        "Жалобная предназначена для сообщений о нарушениях со стороны других участников чата. "
                        "Жалоба должна быть по существу и содержать не менее 50-ти слов. На администратора жалоба "
                        "уходит только владельцу."),
    "complaint_prompt": ("member", "Жалоба: запрос текста", "",
                         "Опишите вашу жалобу. Жалоба должна содержать не менее 50-ти слов."),
    "complaint_sent": ("member", "Жалоба отправлена", "", "Ваша жалоба на рассмотрении."),
    "support_intro": ("member", "Поддержка: описание", "",
                      "Поддержка предназначена для вопросов, жалоб и других вещей, которые помогают улучшать наше "
                      "качество работы."),
    "support_topic_prompt": ("member", "Поддержка: запрос темы", "",
                             "Выберите тему вашего письма (например, чат, жалоба и так далее). "
                             "Тема письма имеет лимит в 10 слов."),
    "support_body_prompt": ("member", "Поддержка: запрос текста", "",
                            "Опишите свою проблему или вопрос, вам ответит один из администраторов. "
                            "Длина вашего письма должна быть не меньше 20-ти слов."),
    "support_sent": ("member", "Поддержка: письмо отправлено", "",
                     "Ваше письмо успешно отправлено. Время ожидания ответа от 5-ти минут до 24-х часов."),
    "about_member": ("member", "О боте (участник)", "", f"{config.BOT_INFO_TEXT} {_ABOUT_TAIL['member']}"),
    # ---------------------------------------------------------- панель админа
    "admin_greeting": ("admin", "Админ-панель: приветствие", "{role}", "Приветствую вновь, {role}-сама."),
    "admins_header": ("admin", "Админы: заголовок", "", "Действующие администраторы:"),
    "edit_intro": ("admin", "Редактирование: вступление", "",
                   "Выберите раздел, тексты которого хотите отредактировать."),
    "addmember_intro": ("tech", "Добавить участника: текст", "",
                        "Если участник был в чате до моего появления, присвойте ему роль."),
    "addmember_form": ("tech", "Добавить участника: форма", "",
                       "Отправьте анкету в формате:\nУчастник: [id], Роль: [роль], День рождения: [ДД.ММ]"),
    "resters_header": ("admin", "Рестники: заголовок", "", "Рестники:"),
    "activity_header": ("admin", "Активность: заголовок", "", "Активность в чате:"),
    "moderation_header": ("admin", "Модерирование: заголовок", "", "Статистика нарушений и наказаний:"),
    "purges_header": ("tech", "Чистки: заголовок", "", "Данные о чистке:"),
    "poll_intro": ("admin", "Опросник: вступление", "", "Проведите опрос среди участников и/или админов."),
    "poll_form": ("admin", "Опросник: форма", "",
                  "Отправьте анкету в формате:\nЗаголовок: [текст], варианты ответов: 1. [текст] 2. [текст] 3. [текст]. "
                  "Викторина: да/нет. Время опроса: [например, 3 дня]\n"
                  "Для викторины можно добавить в конце: Правильный ответ: [номер]."),
    "broadcast_choose": ("admin", "Рассылка: выбор типа", "", "Выберите тип рассылки:"),
    "broadcast_members": ("admin", "Рассылка участникам", "",
                          "Введите свой текст, чтобы разослать его всем участникам. За день можно сделать не более "
                          "5-ти рассылок. К тексту можно добавить до 5-ти фото."),
    "broadcast_unreg": ("admin", "Рассылка незарегистрированным", "",
                        "Введите свой текст, чтобы разослать его всем незарегистрированным в боте пользователям. "
                        "За день можно сделать не более 5-ти рассылок. К тексту можно добавить до 5-ти фото."),
    "broadcast_all": ("admin", "Рассылка всем", "",
                      "Введите свой текст, чтобы разослать его всем: участникам чата и тем, кто не зарегистрирован "
                      "в боте. За день можно сделать не более 5-ти рассылок. К тексту можно добавить до 5-ти фото."),
    "broadcast_chat": ("admin", "Рассылка в чат", "",
                       "Введите свой текст, чтобы выслать его в общий чат. За день можно сделать не более "
                       "5-ти рассылок. К тексту можно добавить до 5-ти фото."),
    "slots_request": ("tech", "Запрос числа мест", "", "Новая Фаза / Волна.\nУкажите доступное кол-во мест."),
    "about_admin": ("admin", "О боте (админ)", "", f"{config.BOT_INFO_TEXT} {_ABOUT_TAIL['admin']}"),

    # --- Техническая панель: тексты технических функций (ночной режим, добавление участника, вступление) ---
    "night_header": ("tech", "Ночной режим: заголовок панели", "",
                     "🌙 Ночной режим\n\nВ начале и в конце ночного режима бот пишет об этом в основной чат."),
    "night_edit_prompt": ("tech", "Ночной режим: запрос часов", "",
                          "Введите новые часы начала и окончания ночного режима по МСК, например: 23-5 или "
                          "22:00-06:00. Время только целыми часами."),
    "night_on": ("tech", "Ночной режим: включился (в чат)", "{hours}",
                 "🌙 Включён ночной режим ({hours}): вложения запрещены всем, кроме админов."),
    "night_off": ("tech", "Ночной режим: закончился (в чат)", "{hours}",
                  "☀️ Ночной режим окончен ({hours}): вложения снова разрешены всем."),
    "member_added_m": ("tech", "Добавлен вручную: объявление в чат (мужская роль)", "{role}",
                       "Добавлен новый Участник – {role}."),
    "member_added_f": ("tech", "Добавлен вручную: объявление в чат (женская роль)", "{role}",
                       "Добавлена новая Участница – {role}."),
    "member_joined": ("tech", "Вступление: объявление в чат о новом участнике", "{role}",
                      "Новый участник – {role}!"),
    "captcha_text": ("tech", "Вступление: просьба пройти капчу (в чат)", "{role}",
                     "{role}, пожалуйста, пройдите капчу в течение 5 минут, нажав кнопку ниже."),
}


# ---------------------------------------------------------------------------
# Загрузка / подстановка
# ---------------------------------------------------------------------------

async def load(key: str) -> tuple[str, list[str]]:
    default = TEXTS[key][3]
    doc = await texts_col.find_one({"_id": key})
    if not doc:
        return default, []
    return (doc.get("text") or default), list(doc.get("photos") or [])


def render(template: str, **values) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v) if isinstance(v, Raw) else html.escape(str(v)))
    return template


async def text_of(key: str, **values) -> str:
    template, _ = await load(key)
    return render(template, **values)


# ---------------------------------------------------------------------------
# Отправка с фото
# ---------------------------------------------------------------------------

async def send_with_photos(bot: Bot, chat_id: int, text: str, photos: list[str],
                           markup: Optional[InlineKeyboardMarkup] = None) -> Optional[Message]:
    text = text[:4096]
    try:
        if not photos:
            return await bot.send_message(chat_id, text, reply_markup=markup)
        if len(photos) == 1:
            if len(text) <= 1024:
                return await bot.send_photo(chat_id, photos[0], caption=text, reply_markup=markup)
            await bot.send_photo(chat_id, photos[0])
            return await bot.send_message(chat_id, text, reply_markup=markup)
        await bot.send_media_group(chat_id, [InputMediaPhoto(media=p) for p in photos])
        return await bot.send_message(chat_id, text, reply_markup=markup)
    except (TelegramBadRequest, TelegramForbiddenError) as e:
        logger.warning("Не удалось отправить сообщение %s: %s", chat_id, e)
        return None


async def show(target: Union[Message, CallbackQuery], key: str, markup: Optional[InlineKeyboardMarkup] = None,
               extra: str = "", **values) -> Optional[Message]:
    """Показать экран: текст из реестра (+ динамическая часть `extra` под ним)."""
    template, photos = await load(key)
    text = render(template, **values) + extra
    if isinstance(target, CallbackQuery):
        msg = target.message
        if not photos and not msg.photo:
            try:
                return await msg.edit_text(text[:4096], reply_markup=markup)
            except TelegramBadRequest:
                pass
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        return await send_with_photos(msg.bot, msg.chat.id, text, photos, markup)
    return await send_with_photos(target.bot, target.chat.id, text, photos, markup)


async def send_key(bot: Bot, chat_id: int, key: str, markup: Optional[InlineKeyboardMarkup] = None,
                   extra: str = "", *, to_log: bool = True, **values) -> Optional[Message]:
    """Отправить текст из реестра в личку/чат (с копией в логи)."""
    template, photos = await load(key)
    text = render(template, **values) + extra
    msg = await send_with_photos(bot, chat_id, text, photos, markup)
    if to_log and chat_id != config.LOGS_CHAT_ID:
        try:
            await bot.send_message(config.LOGS_CHAT_ID, f"<i>→ ЛС {chat_id}</i>\n{text[:3900]}", disable_notification=True)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
    return msg


# ---------------------------------------------------------------------------
# Редактирование (используется в админ-панели)
# ---------------------------------------------------------------------------

async def set_text(key: str, text: str) -> None:
    await texts_col.update_one({"_id": key}, {"$set": {"text": text}}, upsert=True)


async def add_photo(key: str, file_id: str) -> bool:
    _, photos = await load(key)
    if len(photos) >= config.MAX_TEXT_PHOTOS:
        return False
    await texts_col.update_one({"_id": key}, {"$push": {"photos": file_id}}, upsert=True)
    return True


async def remove_photo(key: str, index: int) -> None:
    _, photos = await load(key)
    if 0 <= index < len(photos):
        photos.pop(index)
        await texts_col.update_one({"_id": key}, {"$set": {"photos": photos}}, upsert=True)


async def clear_photos(section: Optional[str] = None) -> None:
    keys = [k for k, v in TEXTS.items() if section is None or v[0] == section]
    await texts_col.update_many({"_id": {"$in": keys}}, {"$set": {"photos": []}})


async def clear_photos_of(key: str) -> None:
    await texts_col.update_one({"_id": key}, {"$set": {"photos": []}}, upsert=True)


async def reset_texts(section: Optional[str] = None, key: Optional[str] = None) -> None:
    if key:
        await texts_col.delete_one({"_id": key})
        return
    keys = [k for k, v in TEXTS.items() if section is None or v[0] == section]
    await texts_col.delete_many({"_id": {"$in": keys}})
