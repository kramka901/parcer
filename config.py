import os
import sys
import re
from pathlib import Path
from typing import Any

# Добавляем корневую директорию проекта в sys.path для надёжного импорта модулей
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# Безопасный импорт python-dotenv
try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Список возможных путей и имён .env файла (актуально для Windows, где часто создают .env.txt или запускают из другой папки)
_CANDIDATE_ENV_PATHS = [
    BASE_DIR / ".env",
    BASE_DIR / ".env.txt",
    BASE_DIR / "env",
    BASE_DIR / ".env.local",
    Path.cwd() / ".env",
    Path.cwd() / ".env.txt",
    Path.cwd() / "env",
    BASE_DIR.parent / ".env",
    BASE_DIR.parent / ".env.txt",
]
if os.getenv("ENV_FILE"):
    _CANDIDATE_ENV_PATHS.insert(0, Path(os.environ["ENV_FILE"]))

ENV_PATH = BASE_DIR / ".env"
for cand in _CANDIDATE_ENV_PATHS:
    if cand.is_file():
        ENV_PATH = cand
        break

_DUMMY_TOKEN_VALUES = {
    "",
    "ваш_токен_бота_здесь",
    "your_bot_token_here",
    "your_token_here",
    "token_here",
    "insert_token_here",
    "none",
    "null"
}

_TOKEN_KEYS = [
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
    "TELEGRAM_TOKEN",
    "TOKEN",
    "TG_TOKEN",
    "TG_BOT_TOKEN",
    "TELEGRAM_API_TOKEN",
    "BOT_API_TOKEN",
    "API_TOKEN",
]

def _parse_value(val: str) -> str:
    """Удаляет пробелы, внешние кавычки и инлайн-комментарии"""
    val = val.strip()
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        return val[1:-1].strip()
    # Если нет кавычек, отделяем комментарий #
    return val.split("#")[0].strip().strip("'\"")

def _load_all_env_variables() -> str:
    """
    Универсальная загрузка переменных окружения из .env файла.
    1. Пробует использовать python-dotenv для всех найденных путей.
    2. Проводит собственное парсирование с поддержкой различных кодировок Windows
       (utf-8-sig, utf-8, utf-16, cp1251) и вариантов оформления (.env, .env.txt, без префикса).
    3. Заносит ВСЕ найденные переменные в os.environ.
    4. Возвращает найденный валидный Telegram Bot Token.
    """
    # 1. Попытка стандартной загрузки через python-dotenv
    if load_dotenv:
        for cand in _CANDIDATE_ENV_PATHS:
            if cand.is_file():
                try:
                    load_dotenv(cand, override=True)
                except Exception:
                    pass

    # 2. Прямой парсер файлов (гарантирует чтение даже без python-dotenv или при нестандартной кодировке)
    parsed_vars = {}
    raw_token_found = ""

    for cand in _CANDIDATE_ENV_PATHS:
        if not cand.is_file():
            continue

        for encoding in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "cp1251", "latin1"):
            try:
                with open(cand, "r", encoding=encoding) as f:
                    content = f.read()

                if not content or not content.strip():
                    continue

                for line in content.splitlines():
                    clean_line = line.strip().lstrip("\ufeff")
                    if not clean_line or clean_line.startswith("#"):
                        continue

                    if clean_line.startswith("export "):
                        clean_line = clean_line[7:].strip()

                    if "=" in clean_line:
                        var_name, var_val = clean_line.split("=", 1)
                        var_name = var_name.strip()
                        var_val = _parse_value(var_val)
                        if var_name:
                            parsed_vars[var_name] = var_val
                            # Помещаем в os.environ, если ещё нет или если текущее значение пустое
                            curr = os.getenv(var_name, "").strip()
                            if not curr or curr.lower() in _DUMMY_TOKEN_VALUES:
                                os.environ[var_name] = var_val

                    # Проверка, если пользователь вставил сырой токен Telegram на отдельной строке
                    raw_match = re.search(r'\b\d{8,11}:[A-Za-z0-9_-]{30,}\b', clean_line)
                    if raw_match and not raw_token_found:
                        raw_token_found = raw_match.group(0).strip()

                # Если файл успешно прочитан без ошибок декодирования, прерываем цикл кодировок
                break
            except Exception:
                continue

    # 3. Поиск токена среди известных имён переменных
    for key in _TOKEN_KEYS:
        val = (os.getenv(key) or os.getenv(key.lower()) or parsed_vars.get(key) or parsed_vars.get(key.lower()) or "").strip().strip("'\"")
        if val and val.lower() not in _DUMMY_TOKEN_VALUES:
            os.environ["TELEGRAM_BOT_TOKEN"] = val
            return val

    # 4. Если токен найден через регулярное выражение
    if raw_token_found:
        os.environ["TELEGRAM_BOT_TOKEN"] = raw_token_found
        return raw_token_found

    return ""

def _safe_int(val_str: Any, default: int) -> int:
    """Безопасное преобразование строки в int с очисткой от комментариев"""
    if val_str is None:
        return default
    try:
        clean = str(val_str).split("#")[0].strip().strip("'\"")
        return int(clean)
    except (ValueError, TypeError):
        return default

# Загрузка переменных окружения и токена бота
TELEGRAM_BOT_TOKEN = _load_all_env_variables()

def get_env_diagnostic_info() -> str:
    """Возвращает сводку о состоянии .env для удобной отладки"""
    token_status = "✅ НАЙДЕН" if TELEGRAM_BOT_TOKEN else "❌ НЕ НАЙДЕН"
    masked_token = f"{TELEGRAM_BOT_TOKEN[:8]}...{TELEGRAM_BOT_TOKEN[-4:]}" if TELEGRAM_BOT_TOKEN else "ОТСУТСТВУЕТ"
    env_exists = "Да" if ENV_PATH.is_file() else "Нет"
    return (
        f"Файл окружения: {ENV_PATH} (существует: {env_exists})\n"
        f"Статус токена: {token_status} ({masked_token})\n"
        f"Библиотека python-dotenv: {'Доступна' if load_dotenv else 'Не установлена (использован встроенный парсер)'}"
    )

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
USER_BACKUP_FILE = BASE_DIR / "user_preferences.json.bak"
CHAT_DATA_FILE = BASE_DIR / "chats_preferences.json"
SEEN_SUBSTITUTIONS_FILE = BASE_DIR / "seen_substitutions.json"
TIMETABLE_CACHE_FILE = BASE_DIR / "timetable_cache.json"
SUBSTITUTIONS_CACHE_FILE = BASE_DIR / "substitutions_cache.json"
TEACHERS_DATA_FILE = BASE_DIR / "teachers.json"
ADMINS_FILE = BASE_DIR / "admins.json"

# Начальное базовое количество пользователей (сохранение для 97 пользователей)
INITIAL_USER_COUNT = _safe_int(os.getenv("INITIAL_USER_COUNT"), 97)

# Начальные ID администраторов
DEFAULT_ADMIN_IDS = [1637426603]
env_admin_ids = (os.getenv("ADMIN_USER_IDS", "") or os.getenv("ADMIN_IDS", "")).strip()
if env_admin_ids:
    clean_admin_ids = env_admin_ids.split("#")[0].strip().strip("'\"[]")
    for aid_str in clean_admin_ids.split(","):
        aid_str = aid_str.strip()
        if aid_str.isdigit():
            aid = int(aid_str)
            if aid not in DEFAULT_ADMIN_IDS:
                DEFAULT_ADMIN_IDS.append(aid)

# Настройки сети и прокси (опционально, если EduPage блокирует VPN)
EDUPAGE_PROXY = os.getenv("EDUPAGE_PROXY", "").strip()
REQUEST_TIMEOUT = _safe_int(os.getenv("REQUEST_TIMEOUT"), 10)

# Интервал автоматической проверки новых замен (в секундах, по умолчанию 3 минуты)
SUBSTITUTION_CHECK_INTERVAL = _safe_int(os.getenv("SUBSTITUTION_CHECK_INTERVAL"), 180)

# Дни недели на русском
WEEKDAYS_RU = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

WEEKDAYS_SHORT_RU = ["Пн", "Вт", "Ср", "Чт", "Пт"]
