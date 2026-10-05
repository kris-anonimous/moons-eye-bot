"""
config.py — конфигурация бота Moons Eye.

Секреты и ID берутся из переменных окружения (на Hugging Face Spaces:
Settings -> Variables and secrets). В коде ничего не хардкодится.
"""

import os

from motor.motor_asyncio import AsyncIOMotorClient

# --------------------------------------------------------------------------
# Окружение
# --------------------------------------------------------------------------
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
GEMINI_API_KEY: str = os.environ["GEMINI_API_KEY"]
MONGO_URI: str = os.environ["MONGO_URI"]
OWNER_ID: int = int(os.environ["OWNER_ID"])
MAIN_CHAT_ID: int = int(os.environ["MAIN_CHAT_ID"])
LOGS_CHAT_ID: int = int(os.environ["LOGS_CHAT_ID"])
REQUIRED_CHANNEL_IDS: list[int] = [
    int(x) for x in os.environ.get("REQUIRED_CHANNEL_IDS", "").split(",") if x.strip()
]
AGREEMENT_URL: str = os.environ.get("AGREEMENT_URL", "https://telegra.ph/")

# Модель Gemini. Старые модели (1.5, 2.0) отключены Google — при 404 смените
# значение переменной окружения GEMINI_MODEL на актуальную Flash-модель.
GEMINI_MODEL: str = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_FALLBACK_MODELS: list[str] = [
    m for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-flash-latest").split(",") if m.strip()
]
# Лимит запросов к Gemini в минуту (бесплатный тариф ограничен)
GEMINI_MAX_CALLS_PER_MINUTE: int = int(os.environ.get("GEMINI_MAX_CALLS_PER_MINUTE", "10"))

WEB_SERVER_HOST = "0.0.0.0"
WEB_SERVER_PORT = int(os.environ.get("PORT", "7860"))
WEBHOOK_PATH = "/webhook"
BASE_WEBHOOK_URL: str = os.environ.get("BASE_WEBHOOK_URL", "")

# --------------------------------------------------------------------------
# Константы бизнес-логики (из ТЗ и ответов владельца)
# --------------------------------------------------------------------------
CAPTCHA_TIMEOUT_MINUTES = 5              # отсчёт от входа в чат
INVITE_LINK_TIMEOUT_HOURS = 5            # отсчёт от создания ссылки-приглашения
USERNAME_WAIT_MINUTES = 30               # отсчёт от обнаружения, что юзернейма нет

MSK_OFFSET_HOURS = 3                     # МСК = UTC+3, без перехода на летнее время

NIGHT_MODE_START_HOUR = 23               # МСК
NIGHT_MODE_END_HOUR = 5                  # МСК

MAX_REST_DAYS = 14
REST_BAN_DAYS = 7
MAX_CONCURRENT_RESTERS = 5
REST_INACTIVITY_KICK_DAYS = 3

COMPLAINT_MIN_WORDS = 50
COMPLAINT_MUTE_THRESHOLD = 10            # «более 10» уникальных жалобщиков -> мут
COMPLAINT_BAN_THRESHOLD = 20             # «более 20» уникальных жалобщиков -> бан
SUPPORT_TOPIC_MAX_WORDS = 10
SUPPORT_BODY_MIN_WORDS = 20

MAX_ADMINS = 2                           # не считая владельца
MIN_DAYS_IN_CHAT_FOR_ADMIN = 14

WEEKLY_NORM_DEFAULT = 140
MONTHLY_NORM_DEFAULT = 560               # = 4 недельных
ADMIN_WEEKLY_NORM = 80
ADMIN_MONTHLY_NORM = 320
INACTIVITY_KICK_DAYS = 3                 # отображается в /admin как есть (это значение из ТЗ)
WEEKLY_PURGE_MIN_PARTICIPANTS = 10
MONTHLY_PURGE_MIN_PARTICIPANTS = 30
WEEKLY_TOP_THRESHOLD = 1000
MONTHLY_TOP_THRESHOLD = 5000
WEEKLY_PURGE_TIME_DEFAULT = "18:00"      # МСК, суббота
MONTHLY_PURGE_TIME_DEFAULT = "19:00"     # МСК, последний день месяца

WAVE_MAX_SLOTS = {1: 10, 2: 20}          # максимум, который может указать владелец
WAVE_TEMPLATE_SLOTS = {1: 10, 2: 15}     # шаблон, если владелец не ответил
PHASE_WAIT_DAYS = 7                      # режим ожидания новой фазы
WAVE_WAIT_DAYS = 14                      # режим ожидания новой волны
MAX_REJECTIONS = 3                       # всего попыток анкеты: 1 + ещё 2

MAX_BROADCASTS_PER_DAY = 5
MAX_BROADCAST_PHOTOS = 5
MAX_TEXT_PHOTOS = 2

RAID_MESSAGES = 5
RAID_SECONDS = 10
COMMAND_SPAM_COUNT = 3
COMMAND_SPAM_SECONDS = 5

LEAVE_NOTE_MINUTES = 30
TAG_MAX_LEN = 16                         # лимит Telegram на тег участника

BOT_INFO_TEXT = (
    "Бот для обработки, хранения и передачи информации. "
    "Является запатентованным имуществом проекта «Moons Eye» с 20 октября 2025 года."
)

# Примерные точки «id аккаунта -> дата создания» для оценки «похож ли на твинк».
# Telegram не отдаёт дату регистрации, но id растут со временем. Значения
# ПРИБЛИЗИТЕЛЬНЫЕ — при желании поправьте под свои наблюдения.
USER_ID_DATE_ANCHORS = [
    (100_000_000, "2015-02-01"),
    (500_000_000, "2018-05-01"),
    (1_000_000_000, "2019-12-01"),
    (2_000_000_000, "2020-12-01"),
    (5_000_000_000, "2021-11-01"),
    (6_000_000_000, "2023-01-01"),
    (7_000_000_000, "2024-02-01"),
    (8_000_000_000, "2025-01-01"),
]
TWINK_MAX_AGE_DAYS = 90


# Роли вынесены в roles.py (там нет зависимостей от окружения — удобно для тестов)
from roles import ROLES, ROLE_KEYS, get_role_gender  # noqa: E402,F401


# --------------------------------------------------------------------------
# Правила модерации
# penalty-коды: mute, oral, oral2, oral3, warn, warn2, warn3, ban, perm
# --------------------------------------------------------------------------
MODERATION_RULES: dict[str, dict] = {
    "0.0": {"text": "Слив данных человека/группы лиц без согласия", "penalty": "perm"},
    "0.1": {"text": "Неадекватное поведение, давящая атмосфера в чате", "penalty": "ban"},
    "0.2": {"text": "Клевета на участников, администрацию", "penalty": "warn"},
    "0.3": {"text": "Обсуждение участников в негативном ключе", "penalty": "warn"},
    "0.4": {"text": "Намеренный поиск дыр в правилах", "penalty": "perm"},
    "0.5": {"text": "Общение на другом языке / отказ от русского", "penalty": "warn"},
    "1.0": {"text": "Защита исключённого нарушителя", "penalty": "perm"},
    "1.1": {"text": "Разжигание конфликтов", "penalty": "warn"},
    "1.2": {"text": "Осуждение по вере/ориентации/нации/взглядам", "penalty": "ban"},
    "1.3": {"text": "Реклама, вредоносные ссылки и файлы", "penalty": "warn"},
    "1.4": {"text": "Оскорбление администрации", "penalty": "warn"},
    "1.5": {"text": "Оскорбление участников", "penalty": "warn"},
    "2.0": {"text": "Спам командами, рейдерские атаки", "penalty": "perm"},
    "2.1": {"text": "Большие бессмысленные тексты и пасты", "penalty": "warn"},
    "2.2": {"text": "Пустые/бессмысленные сообщения", "penalty": "oral"},
    "2.3": {"text": "Сообщения лесенкой", "penalty": "oral"},
    "2.4": {"text": "Более 3 стикеров/GIF/аудио за раз", "penalty": "warn"},
    "2.5": {"text": "/everyone без прав более 3 раз", "penalty": "oral3"},
    "3.0": {"text": "NSFW без спойлера и пометки 18+", "penalty": "warn3"},
    "3.1": {"text": "Обсуждение интимных тем без цензуры", "penalty": "warn"},
    "3.2": {"text": "Откровенные порнографические/биологические описания", "penalty": "warn2"},
    "3.3": {"text": "Навязчивое непристойное обращение", "penalty": "warn2"},
    "3.4": {"text": "Скримеры/неприятные звуки без предупреждения", "penalty": "oral2"},
    # у 3.5 нет единого кода наказания в этой таблице — наказание выбирается по
    # тяжести прямо в handlers/chat_moderation.py._check_media (warn2 или perm)
    "3.5": {"text": "Педофилия/порно/нацизм/расчленёнка/нагота в стикерах и GIF", "penalty": "warn2"},
}

# Правила, которые определяет Gemini по тексту / по картинке
AI_TEXT_RULES = ["0.0", "0.1", "0.2", "0.3", "0.5", "1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "3.1", "3.2", "3.3"]
AI_MEDIA_RULES = ["3.0", "3.5"]

client = AsyncIOMotorClient(MONGO_URI)
db = client["moons_eye_bot"]
