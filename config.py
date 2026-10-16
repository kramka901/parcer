import os
from pathlib import Path
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Настройки школы Tallinna Mustjõe Gümnaasium
MJG_MAIN_URL = "https://www.mjg.ee"
EDUPAGE_SUBDOMAIN = "mustjoe"
EDUPAGE_BASE_URL = f"https://{EDUPAGE_SUBDOMAIN}.edupage.org"
SUBSTITUTION_URL = f"{EDUPAGE_BASE_URL}/substitution/"
TIMETABLE_URL = f"{EDUPAGE_BASE_URL}/timetable/"
TT_VIEWER_RPC_URL = f"{EDUPAGE_BASE_URL}/timetable/server/ttviewer.js?__func=getTTViewerData"
REGULAR_TT_RPC_URL = f"{EDUPAGE_BASE_URL}/timetable/server/regulartt.js?__func=regularttGetData"

# Кэширование (в секундах)
TIMETABLE_CACHE_TTL = 3600  # 1 час для базового расписания
SUBSTITUTION_CACHE_TTL = 300  # 5 минут для замен

# Файлы сохранения данных и кэша
USER_DATA_FILE = BASE_DIR / "user_preferences.json"
SEEN_SUBSTITUTIONS_FILE = BASE_DIR / "seen_substitutions.json"
TIMETABLE_CACHE_FILE = BASE_DIR / "timetable_cache.json"
SUBSTITUTIONS_CACHE_FILE = BASE_DIR / "substitutions_cache.json"
TEACHERS_DATA_FILE = BASE_DIR / "teachers.json"
ADMINS_FILE = BASE_DIR / "admins.json"

# Начальные ID администраторов
DEFAULT_ADMIN_IDS = [1637426603]
env_admin_ids = os.getenv("ADMIN_USER_IDS", "").strip()
if env_admin_ids:
    for aid_str in env_admin_ids.split(","):
        aid_str = aid_str.strip()
        if aid_str.isdigit():
            aid = int(aid_str)
            if aid not in DEFAULT_ADMIN_IDS:
                DEFAULT_ADMIN_IDS.append(aid)

# Настройки сети и прокси (опционально, если EduPage блокирует VPN)
EDUPAGE_PROXY = os.getenv("EDUPAGE_PROXY", "").strip()
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))

# Интервал автоматической проверки новых замен (в секундах, по умолчанию 3 минуты)
SUBSTITUTION_CHECK_INTERVAL = int(os.getenv("SUBSTITUTION_CHECK_INTERVAL", "180"))

# Дни недели на русском
WEEKDAYS_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница"
]

WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт"]
