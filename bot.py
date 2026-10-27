import os
import sys
import re
import json
import logging
import asyncio
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden, Conflict, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from config import (
    TELEGRAM_BOT_TOKEN,
    USER_DATA_FILE,
    USER_BACKUP_FILE,
    CHAT_DATA_FILE,
    INITIAL_USER_COUNT,
    SEEN_SUBSTITUTIONS_FILE,
    ADMINS_FILE,
    DEFAULT_ADMIN_IDS,
    SUBSTITUTION_CHECK_INTERVAL,
    WEEKDAYS_SHORT_RU,
    get_env_diagnostic_info
)
from parser import MJGParser

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация парсера
parser = MJGParser()


async def pcall(func, *args, **kwargs):
    """
    Запускает синхронную (сетевую/тяжёлую) функцию парсера в отдельном потоке,
    чтобы не блокировать event loop бота. Благодаря этому кнопки отвечают
    мгновенно, даже если EduPage отвечает медленно или недоступен.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


# ================= Управление администраторами =================

class AdminManager:
    """Управление списком администраторов бота (сохранение в admins.json)"""

    def __init__(self, filepath=ADMINS_FILE, initial_admins=None):
        self.filepath = filepath
        self.admins: set = set(initial_admins or DEFAULT_ADMIN_IDS)
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    for a in data.get("admins", []):
                        try:
                            self.admins.add(int(a))
                        except (ValueError, TypeError):
                            pass
            except Exception as e:
                logger.error(f"Ошибка загрузки admins.json: {e}")
        else:
            self._save()

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({"admins": sorted(list(self.admins))}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения admins.json: {e}")

    def is_admin(self, user_id: int) -> bool:
        return int(user_id) in self.admins

    def add_admin(self, user_id: int) -> bool:
        uid = int(user_id)
        if uid not in self.admins:
            self.admins.add(uid)
            self._save()
            return True
        return False

    def remove_admin(self, user_id: int) -> bool:
        uid = int(user_id)
        if len(self.admins) <= 1:
            return False  # Нельзя удалить последнего администратора
        if uid in self.admins:
            self.admins.remove(uid)
            self._save()
            return True
        return False

    def get_admins(self) -> List[int]:
        return sorted(list(self.admins))


admin_manager = AdminManager()


# ================= Управление пользовательскими данными =================

class UserPreferences:
    """Управление сохранёнными настройками пользователей (роль: ученик/учитель, класс, уведомления, подгруппы)"""

    def __init__(self, filepath=USER_DATA_FILE, backup_filepath=USER_BACKUP_FILE):
        self.filepath = filepath
        self.backup_filepath = backup_filepath
        self._data: Dict[str, Dict[str, Any]] = {}
        self._target_count: int = INITIAL_USER_COUNT
        self._load()

    def _load(self):
        loaded = False
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                    loaded = True
            except Exception as e:
                logger.error(f"Ошибка чтения настроек пользователей: {e}")

        if not loaded and os.path.exists(self.backup_filepath):
            try:
                with open(self.backup_filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                    loaded = True
            except Exception as e:
                logger.error(f"Ошибка чтения резервной копии настроек: {e}")

        if not loaded:
            self._data = {}

        meta = self._data.get("_meta", {})
        if "target_count" in meta:
            try:
                self._target_count = int(meta["target_count"])
            except (ValueError, TypeError):
                self._target_count = INITIAL_USER_COUNT
        else:
            self._target_count = INITIAL_USER_COUNT

    def _save(self):
        try:
            self._data.setdefault("_meta", {})["target_count"] = self._target_count
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            try:
                with open(self.backup_filepath, "w", encoding="utf-8") as bf:
                    json.dump(self._data, bf, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек пользователей: {e}")

    def get_user(self, user_id: Any) -> Dict[str, Any]:
        return self._data.get(str(user_id), {})

    def has_role(self, user_id: Any) -> bool:
        """Проверяет, выбрал ли пользователь уже роль и сущность"""
        user = self.get_user(user_id)
        role = user.get("role")
        if role == "teacher" and user.get("teacher_name"):
            return True
        if (role == "student" or not role) and user.get("class_name"):
            return True
        return False

    def get_role(self, user_id: Any) -> Optional[str]:
        """Возвращает 'teacher', 'student' или None"""
        user = self.get_user(user_id)
        role = user.get("role")
        if not role:
            if user.get("teacher_name"):
                return "teacher"
            if user.get("class_name"):
                return "student"
        return role

    def get_class(self, user_id: Any) -> Optional[str]:
        return self.get_user(user_id).get("class_name")

    def get_teacher(self, user_id: Any) -> Optional[str]:
        return self.get_user(user_id).get("teacher_name")

    def set_student(self, user_id: Any, class_name: str):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["role"] = "student"
        self._data[uid]["class_name"] = class_name
        self._data[uid]["is_active"] = True
        self._data[uid].pop("inactive_reason", None)
        self._data[uid].pop("teacher_name", None)
        if "notifications" not in self._data[uid]:
            self._data[uid]["notifications"] = True
        self._save()

    def set_teacher(self, user_id: Any, teacher_name: str):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["role"] = "teacher"
        self._data[uid]["teacher_name"] = teacher_name
        self._data[uid]["is_active"] = True
        self._data[uid].pop("inactive_reason", None)
        self._data[uid].pop("class_name", None)
        if "notifications" not in self._data[uid]:
            self._data[uid]["notifications"] = True
        self._save()

    def reset_role(self, user_id: Any):
        uid = str(user_id)
        if uid in self._data:
            self._data[uid].pop("role", None)
            self._data[uid].pop("class_name", None)
            self._data[uid].pop("teacher_name", None)
            self._save()

    def is_notifications_enabled(self, user_id: Any) -> bool:
        return self.get_user(user_id).get("notifications", True)

    def set_notifications(self, user_id: Any, enabled: bool):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["notifications"] = enabled
        self._save()

    def toggle_notifications(self, user_id: Any) -> bool:
        current = self.is_notifications_enabled(user_id)
        new_val = not current
        self.set_notifications(user_id, new_val)
        return new_val

    # Настройки подгрупп
    def get_subgroup_filter(self, user_id: Any) -> Dict[str, str]:
        user = self.get_user(user_id)
        filt = {}
        if user.get("subgroup_gender"):
            filt["gender"] = user["subgroup_gender"]
        if user.get("subgroup_academic"):
            filt["academic"] = user["subgroup_academic"]
        return filt

    def set_gender_filter(self, user_id: Any, gender: Optional[str]):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        if gender in ("boys", "girls"):
            self._data[uid]["subgroup_gender"] = gender
        else:
            self._data[uid].pop("subgroup_gender", None)
        self._save()

    def set_academic_filter(self, user_id: Any, academic: Optional[str]):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        if academic in ("1", "2"):
            self._data[uid]["subgroup_academic"] = academic
        else:
            self._data[uid].pop("subgroup_academic", None)
        self._save()

    def get_all_users(self) -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self._data.items() if not k.startswith("_")}

    def get_active_users_count(self) -> int:
        inactive_count = sum(
            1 for k, v in self._data.items()
            if not k.startswith("_") and v.get("is_active") is False
        )
        real_users = [k for k in self._data if not k.startswith("_") and not k.startswith("saved_user_")]
        base = max(self._target_count, len(real_users))
        return max(0, base - inactive_count)

    def set_user_inactive(self, user_id: Any, reason: str = "blocked"):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["is_active"] = False
        self._data[uid]["inactive_reason"] = reason
        self._data[uid]["notifications"] = False
        self._save()

    def set_user_active(self, user_id: Any):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["is_active"] = True
        self._data[uid].pop("inactive_reason", None)
        self._save()

    def set_target_count(self, target: int):
        self._target_count = int(target)
        self._save()


user_prefs = UserPreferences()


# ================= Управление групповыми чатами =================

class ChatPreferences:
    """Управление настройками групповых бесед и каналов (привязка класса к группе, уведомления)"""

    def __init__(self, filepath=CHAT_DATA_FILE):
        self.filepath = filepath
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error(f"Ошибка чтения настроек групповых чатов: {e}")
                self._data = {}

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            try:
                with open(self.filepath.with_suffix(".json.bak"), "w", encoding="utf-8") as bf:
                    json.dump(self._data, bf, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек групповых чатов: {e}")

    def get_chat(self, chat_id: Any) -> Dict[str, Any]:
        return self._data.get(str(chat_id), {})

    def get_class(self, chat_id: Any) -> Optional[str]:
        return self.get_chat(chat_id).get("class_name")

    def set_class(self, chat_id: Any, class_name: str, title: Optional[str] = None):
        cid = str(chat_id)
        if cid not in self._data:
            self._data[cid] = {}
        self._data[cid]["class_name"] = class_name
        if title:
            self._data[cid]["title"] = title
        if "notifications" not in self._data[cid]:
            self._data[cid]["notifications"] = True
        self._save()

    def remove_chat(self, chat_id: Any):
        cid = str(chat_id)
        if cid in self._data:
            self._data.pop(cid, None)
            self._save()

    def is_notifications_enabled(self, chat_id: Any) -> bool:
        return self.get_chat(chat_id).get("notifications", True)

    def set_notifications(self, chat_id: Any, enabled: bool):
        cid = str(chat_id)
        if cid not in self._data:
            self._data[cid] = {}
        self._data[cid]["notifications"] = enabled
        self._save()

    def get_all_chats(self) -> Dict[str, Dict[str, Any]]:
        return {k: v for k, v in self._data.items() if not k.startswith("_")}


chat_prefs = ChatPreferences()


def get_group_chat_keyboard(chat_id: Any) -> InlineKeyboardMarkup:
    """Интерактивная панель управления расписанием для групповых бесед"""
    c_name = chat_prefs.get_class(chat_id) or "Класс не выбран"
    keyboard = [
        [
            InlineKeyboardButton(f"📅 Сегодня ({c_name})", callback_data=f"grp_today_{chat_id}"),
            InlineKeyboardButton(f"📆 Завтра ({c_name})", callback_data=f"grp_tomorrow_{chat_id}")
        ],
        [
            InlineKeyboardButton(f"📚 Расписание {c_name}", callback_data=f"grp_schedule_{chat_id}"),
            InlineKeyboardButton(f"🔄 Замены {c_name}", callback_data=f"grp_subst_{chat_id}")
        ],
        [
            InlineKeyboardButton("🚪 Свободные кабинеты", callback_data=f"grp_free_{chat_id}"),
            InlineKeyboardButton("👩‍🏫 Учителя", callback_data="btn_browse_teachers")
        ],
        [
            InlineKeyboardButton("⚙️ Сменить класс беседы", callback_data=f"grp_setclass_{chat_id}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


# ================= Отслеживание уже отправленных замен =================

class SeenSubstitutionsTracker:
    """Хранит сигнатуры отправленных замен, чтобы не присылать повторные уведомления"""

    def __init__(self, filepath=SEEN_SUBSTITUTIONS_FILE):
        self.filepath = filepath
        self.seen_signatures: set = set()
        self._load()
        self.initialized: bool = len(self.seen_signatures) > 0

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.seen_signatures = set(data.get("seen", []))
            except Exception as e:
                logger.error(f"Ошибка загрузки seen_substitutions: {e}")
                self.seen_signatures = set()

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump({"seen": list(self.seen_signatures)}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения seen_substitutions: {e}")

    def make_signature(self, date_str: str, class_name: str, item: Dict[str, Any]) -> str:
        period = item.get("period", "").strip()
        info = item.get("info", "").strip()
        change_type = item.get("type", "change").strip()
        return f"{date_str}_{class_name}_{period}_{info}_{change_type}"

    def is_seen(self, signature: str) -> bool:
        return signature in self.seen_signatures

    def mark_seen(self, signature: str):
        self.seen_signatures.add(signature)

    def save(self):
        self._save()


seen_tracker = SeenSubstitutionsTracker()


# ================= Клавиатуры (Inline Keyboards) =================

def get_role_selection_keyboard() -> InlineKeyboardMarkup:
    """Выбор роли при первом старте или смене"""
    keyboard = [
        [InlineKeyboardButton("👨‍🎓 Я ученик / родитель", callback_data="role_choose_student")],
        [InlineKeyboardButton("👩‍🏫 Я учитель", callback_data="role_choose_teacher")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Главное меню с кнопками, адаптированное под роль пользователя и статус админа"""
    role = user_prefs.get_role(user_id)
    user_class = user_prefs.get_class(user_id)
    user_teacher = user_prefs.get_teacher(user_id)
    is_admin = admin_manager.is_admin(user_id)

    if role == "teacher" and user_teacher:
        buttons = [
            [
                InlineKeyboardButton("📅 Мои уроки сегодня", callback_data="btn_teacher_today"),
                InlineKeyboardButton("📆 На завтра", callback_data="btn_teacher_tomorrow"),
            ],
            [
                InlineKeyboardButton("🗓 Моё расписание на неделю", callback_data="btn_teacher_week"),
                InlineKeyboardButton("🔄 Мои замены", callback_data="btn_teacher_subst"),
            ],
            [
                InlineKeyboardButton("👩‍🏫 Другие учителя", callback_data="btn_browse_teachers"),
                InlineKeyboardButton("🏫 Замены всей школы", callback_data="btn_all_subst"),
            ],
            [
                InlineKeyboardButton(f"👩‍🏫 Профиль: {user_teacher}", callback_data="btn_profile"),
            ]
        ]
        if is_admin:
            buttons.append([InlineKeyboardButton("👑 Меню администратора", callback_data="admin_panel")])
        return InlineKeyboardMarkup(buttons)

    # Если ученик (или еще не выбрал класс)
    buttons = [
        [
            InlineKeyboardButton("📅 На сегодня", callback_data="btn_today"),
            InlineKeyboardButton("📆 На завтра", callback_data="btn_tomorrow"),
        ],
        [
            InlineKeyboardButton("🗓 Расписание на неделю", callback_data="btn_week"),
            InlineKeyboardButton("🔄 Замены уроков", callback_data="btn_subst"),
        ],
        [
            InlineKeyboardButton("👩‍🏫 Расписание учителей", callback_data="btn_browse_teachers"),
            InlineKeyboardButton("🏫 Замены всей школы", callback_data="btn_all_subst"),
        ]
    ]

    if user_class:
        buttons.append([
            InlineKeyboardButton(f"🎓 Мой класс: {user_class} (Сменить)", callback_data="btn_choose_grade_group")
        ])
    else:
        buttons.append([
            InlineKeyboardButton("🏫 Выбрать мой класс", callback_data="btn_choose_grade_group")
        ])

    buttons.append([
        InlineKeyboardButton("⚙️ Мой профиль и подгруппы", callback_data="btn_profile")
    ])

    if is_admin:
        buttons.append([InlineKeyboardButton("👑 Меню администратора", callback_data="admin_panel")])

    return InlineKeyboardMarkup(buttons)


def get_profile_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура управления профилем пользователя, подгруппами и сменой роли"""
    role = user_prefs.get_role(user_id)
    notif_enabled = user_prefs.is_notifications_enabled(user_id)
    notif_text = "🔔 Уведомления: ВКЛ" if notif_enabled else "🔕 Уведомления: ВЫКЛ"

    buttons = []
    if role == "teacher":
        buttons.append([InlineKeyboardButton("👩‍🏫 Сменить преподавателя", callback_data="btn_choose_teacher_letters")])
        buttons.append([InlineKeyboardButton("👨‍🎓 Сменить роль на «Ученик»", callback_data="role_choose_student")])
    else:
        buttons.append([InlineKeyboardButton("👥 Настройка подгрупп (Физ-ра / Языки)", callback_data="btn_subgroup_settings")])
        buttons.append([InlineKeyboardButton("🏫 Сменить класс", callback_data="btn_choose_grade_group")])
        buttons.append([InlineKeyboardButton("👩‍🏫 Сменить роль на «Учитель»", callback_data="role_choose_teacher")])

    buttons.append([InlineKeyboardButton(notif_text, callback_data="btn_toggle_notifications")])
    buttons.append([InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")])

    return InlineKeyboardMarkup(buttons)


def get_subgroups_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура настройки подгрупп (мальчики/девочки, 1/2 группа)"""
    filt = user_prefs.get_subgroup_filter(user_id)
    g_curr = filt.get("gender")
    a_curr = filt.get("academic")

    btn_boys = "✅ 👦 Мальчики" if g_curr == "boys" else "👦 Мальчики"
    btn_girls = "✅ 👧 Девочки" if g_curr == "girls" else "👧 Девочки"
    btn_g_all = "✅ Физ-ра: Все" if not g_curr else "Физ-ра: Все"

    btn_g1 = "✅ 👥 1 группа" if a_curr == "1" else "👥 1 группа"
    btn_g2 = "✅ 👥 2 группа" if a_curr == "2" else "👥 2 группа"
    btn_a_all = "✅ Языки: Все" if not a_curr else "Языки: Все"

    keyboard = [
        [
            InlineKeyboardButton(btn_boys, callback_data="set_grp_gender_boys"),
            InlineKeyboardButton(btn_girls, callback_data="set_grp_gender_girls"),
        ],
        [
            InlineKeyboardButton(btn_g_all, callback_data="set_grp_gender_all"),
        ],
        [
            InlineKeyboardButton(btn_g1, callback_data="set_grp_acad_1"),
            InlineKeyboardButton(btn_g2, callback_data="set_grp_acad_2"),
        ],
        [
            InlineKeyboardButton(btn_a_all, callback_data="set_grp_acad_all"),
        ],
        [
            InlineKeyboardButton("◀️ Назад в профиль", callback_data="btn_profile"),
            InlineKeyboardButton("🏠 Главное меню", callback_data="btn_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_grade_groups_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора возрастной ступени классов"""
    keyboard = [
        [InlineKeyboardButton("🎒 1 – 4 классы (Начальная школа)", callback_data="grp_1_4")],
        [InlineKeyboardButton("📚 5 – 9 классы (Основная школа)", callback_data="grp_5_9")],
        [InlineKeyboardButton("🎓 10 – 12 классы (Гимназия)", callback_data="grp_10_12")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_classes_keyboard(group: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора конкретного класса в группе"""
    all_classes = sorted(list(parser.get_classes().keys()))

    if group == "1_4":
        classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [1, 2, 3, 4])]
    elif group == "5_9":
        classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [5, 6, 7, 8, 9])]
    elif group == "10_12":
        classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [10, 11, 12])]
    else:
        classes = all_classes

    buttons = []
    row = []
    for c in classes:
        row.append(InlineKeyboardButton(c, callback_data=f"set_class_{c}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("◀️ Назад к группам", callback_data="btn_choose_grade_group")])
    return InlineKeyboardMarkup(buttons)


def get_teacher_letter_groups_keyboard(prefix: str = "tgrp_") -> InlineKeyboardMarkup:
    """Клавиатура алфавитных групп преподавателей"""
    keyboard = [
        [
            InlineKeyboardButton("A – D", callback_data=f"{prefix}A_D"),
            InlineKeyboardButton("E – J", callback_data=f"{prefix}E_J"),
        ],
        [
            InlineKeyboardButton("K", callback_data=f"{prefix}K"),
            InlineKeyboardButton("L – M", callback_data=f"{prefix}L_M"),
        ],
        [
            InlineKeyboardButton("N – R", callback_data=f"{prefix}N_R"),
            InlineKeyboardButton("S – T", callback_data=f"{prefix}S_T"),
        ],
        [
            InlineKeyboardButton("U – Ü, Ш", callback_data=f"{prefix}U_Z"),
        ],
        [
            InlineKeyboardButton("◀️ В главное меню", callback_data="btn_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_teachers_keyboard(group_code: str, callback_prefix: str = "set_teacher_") -> InlineKeyboardMarkup:
    """Клавиатура выбора конкретного учителя в группе"""
    teachers = parser.get_teachers()

    filtered = []
    for full_name, info in teachers.items():
        surname = info.get("surname") or full_name.split()[0]
        first_letter = surname[0].upper() if surname else ""

        if group_code == "A_D" and first_letter in "ABCD":
            filtered.append(full_name)
        elif group_code == "E_J" and first_letter in "EFGHIJ":
            filtered.append(full_name)
        elif group_code == "K" and first_letter == "K":
            filtered.append(full_name)
        elif group_code == "L_M" and first_letter in "LM":
            filtered.append(full_name)
        elif group_code == "N_R" and first_letter in "NOPQR":
            filtered.append(full_name)
        elif group_code == "S_T" and first_letter in "ST":
            filtered.append(full_name)
        elif group_code == "U_Z" and (first_letter in "UVWXYZÕÄÖÜŠŽ" or first_letter in "ШЩ"):
            filtered.append(full_name)

    if not filtered:
        filtered = list(teachers.keys())[:15]

    buttons = []
    row = []
    for t_name in filtered:
        parts = t_name.split()
        short_btn = f"{parts[0]} {parts[1][0]}." if len(parts) >= 2 else t_name
        row.append(InlineKeyboardButton(short_btn, callback_data=f"{callback_prefix}{t_name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    back_callback = "btn_browse_teachers" if "view_t_" in callback_prefix else "btn_choose_teacher_letters"
    buttons.append([InlineKeyboardButton("◀️ Назад к буквам", callback_data=back_callback)])
    return InlineKeyboardMarkup(buttons)


def get_class_action_keyboard(class_name: str) -> InlineKeyboardMarkup:
    """Клавиатура действий для выбранного класса"""
    days_row = [
        InlineKeyboardButton(name, callback_data=f"sched_{class_name}_{i}")
        for i, name in enumerate(WEEKDAYS_SHORT_RU)
    ]

    keyboard = [
        [
            InlineKeyboardButton("📅 Сегодня", callback_data=f"today_{class_name}"),
            InlineKeyboardButton("📆 Завтра", callback_data=f"tomorrow_{class_name}"),
        ],
        days_row,
        [
            InlineKeyboardButton("🗓 Вся неделя", callback_data=f"week_{class_name}"),
            InlineKeyboardButton("🔄 Замены", callback_data=f"subst_{class_name}"),
        ],
        [
            InlineKeyboardButton("👥 Подгруппы", callback_data="btn_subgroup_settings"),
            InlineKeyboardButton("🏫 Сменить класс", callback_data="btn_choose_grade_group"),
        ],
        [
            InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_teacher_action_keyboard(teacher_name: str, is_own_profile: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура действий для расписания преподавателя"""
    if is_own_profile:
        keyboard = [
            [
                InlineKeyboardButton("📅 Мои уроки сегодня", callback_data="btn_teacher_today"),
                InlineKeyboardButton("📆 На завтра", callback_data="btn_teacher_tomorrow"),
            ],
            [
                InlineKeyboardButton("🗓 Вся неделя", callback_data="btn_teacher_week"),
                InlineKeyboardButton("🔄 Мои замены", callback_data="btn_teacher_subst"),
            ],
            [
                InlineKeyboardButton("🚪 Мои кабинеты", callback_data=f"tv_rm_{teacher_name}"),
                InlineKeyboardButton("🔍 Где сейчас?", callback_data=f"tv_wh_{teacher_name}"),
            ],
            [
                InlineKeyboardButton("👩‍🏫 Сменить учителя", callback_data="btn_choose_teacher_letters"),
                InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("📅 Уроки сегодня", callback_data=f"tv_tod_{teacher_name}"),
                InlineKeyboardButton("📆 Завтра", callback_data=f"tv_tom_{teacher_name}"),
            ],
            [
                InlineKeyboardButton("🗓 Вся неделя", callback_data=f"tv_wek_{teacher_name}"),
                InlineKeyboardButton("🔄 Замены", callback_data=f"tv_sub_{teacher_name}"),
            ],
            [
                InlineKeyboardButton("🚪 Кабинеты", callback_data=f"tv_rm_{teacher_name}"),
                InlineKeyboardButton("🔍 Где сейчас?", callback_data=f"tv_wh_{teacher_name}"),
            ],
            [
                InlineKeyboardButton("👩‍🏫 Другой учитель", callback_data="btn_browse_teachers"),
                InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")
            ]
        ]
    return InlineKeyboardMarkup(keyboard)


# ================= Админские клавиатуры =================

def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню панели администратора"""
    keyboard = [
        [InlineKeyboardButton("📢 Рассылка всем пользователям", callback_data="admin_broadcast_prompt")],
        [InlineKeyboardButton("👥 Управление администраторами", callback_data="admin_manage_admins")],
        [
            InlineKeyboardButton("📊 Статистика бота", callback_data="admin_stats"),
            InlineKeyboardButton("🔄 Обновить кэш EduPage", callback_data="admin_refresh_cache"),
        ],
        [InlineKeyboardButton("◀️ Выйти в главное меню", callback_data="btn_main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_admin_manage_keyboard() -> InlineKeyboardMarkup:
    """Меню управления администраторами"""
    keyboard = [
        [InlineKeyboardButton("➕ Добавить администратора", callback_data="admin_add_prompt")],
        [InlineKeyboardButton("➖ Удалить администратора", callback_data="admin_del_list")],
        [InlineKeyboardButton("📋 Список администраторов", callback_data="admin_list_view")],
        [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)


# ================= Вспомогательные функции =================

async def safe_reply(update: Update, text: str, reply_markup=None):
    """
    Безопасная и надёжная отправка ответа:
    1. Авто-разбивка длинных сообщений (>3900 симв.).
    2. Попытка редактирования сообщения при callback_query.
    3. Автоматический fallback на отправку нового сообщения при невозможности редактирования
       (например, сообщение старше 48 часов, фото/медиа или InaccessibleMessage).
    4. Автоматический fallback на обычный текст при ошибках Markdown.
    5. Информативный ответ при "message is not modified".
    """
    chat = update.effective_chat
    target = update.callback_query.message if update.callback_query else update.message
    if not chat and target and hasattr(target, "chat"):
        chat = target.chat

    if not chat and not target:
        logger.error("safe_reply: Не удалось определить целевой чат или сообщение.")
        return

    # В личных чатах вместо inline-кнопок под сообщением показываем клавиатуру возле поля ввода
    is_private = bool(chat and getattr(chat, "type", None) == "private" and update.effective_user)
    if is_private and not isinstance(reply_markup, ReplyKeyboardMarkup):
        reply_markup = reply_kb_for(update.effective_user.id)

    max_len = 3900
    chunks = []
    if len(text) > max_len:
        paragraphs = text.split("\n\n")
        current_chunk = ""
        for p in paragraphs:
            if len(current_chunk) + len(p) + 2 > max_len:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                current_chunk = p
            else:
                current_chunk = f"{current_chunk}\n\n{p}" if current_chunk else p
        if current_chunk:
            chunks.append(current_chunk.strip())
    else:
        chunks = [text]

    for i, chunk in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        mk = reply_markup if is_last else None

        # 1. Попытка редактирования для первого блока callback_query
        if update.callback_query and i == 0 and not is_private:
            edited = False
            try:
                if hasattr(update.callback_query, "edit_message_text"):
                    await update.callback_query.edit_message_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
                    edited = True
                elif target and hasattr(target, "edit_text"):
                    await target.edit_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
                    edited = True
            except Exception as e:
                err_msg = str(e).lower()
                if "message is not modified" in err_msg:
                    try:
                        await update.callback_query.answer("Вы уже просматриваете этот раздел ✅")
                    except Exception:
                        pass
                    return

                # Пробуем редактирование без Markdown
                try:
                    if hasattr(update.callback_query, "edit_message_text"):
                        await update.callback_query.edit_message_text(chunk, reply_markup=mk)
                        edited = True
                    elif target and hasattr(target, "edit_text"):
                        await target.edit_text(chunk, reply_markup=mk)
                        edited = True
                except Exception as e2:
                    if "message is not modified" in str(e2).lower():
                        try:
                            await update.callback_query.answer("Вы уже на этой странице ✅")
                        except Exception:
                            pass
                        return
                    logger.warning(f"Не удалось отредактировать сообщение ({e2}). Отправляем новым сообщением.")

            if edited:
                continue

        # 2. Отправка нового сообщения (или продолжение длинного сообщения)
        try:
            if chat and hasattr(chat, "send_message"):
                await chat.send_message(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
            elif target and hasattr(target, "reply_text"):
                await target.reply_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
        except Exception as e:
            logger.warning(f"Ошибка Markdown при отправке ({e}). Отправка обычным текстом.")
            try:
                if chat and hasattr(chat, "send_message"):
                    await chat.send_message(chunk, reply_markup=mk)
                elif target and hasattr(target, "reply_text"):
                    await target.reply_text(chunk, reply_markup=mk)
            except Exception as e_final:
                logger.error(f"Критическая ошибка отправки сообщения: {e_final}")


def get_target_date_for_tomorrow() -> date:
    """Вычисляет дату следующего учебного дня (если пятница/выходной -> понедельник)"""
    today = date.today()
    weekday = today.weekday()
    if weekday == 4:
        return today + timedelta(days=3)
    elif weekday == 5:
        return today + timedelta(days=2)
    elif weekday == 6:
        return today + timedelta(days=1)
    else:
        return today + timedelta(days=1)


# ================= Обработчики пользовательских команд =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user = update.effective_user
    user_id = user.id
    user_prefs.set_user_active(user_id)

    if not user_prefs.has_role(user_id):
        welcome_text = (
            f"👋 Здравствуйте, *{user.first_name}*!\n\n"
            f"Добро пожаловать в официальный бот расписания и замен уроков "
            f"для *Tallinna Mustjõe Gümnaasium* (www.mjg.ee).\n\n"
            f"Пожалуйста, выберите, кто вы:\n"
            f"_(Вы всегда сможете изменить этот выбор позже в меню)_ 👇"
        )
        await safe_reply(update, welcome_text, reply_markup=get_role_selection_keyboard())
        return

    role = user_prefs.get_role(user_id)
    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        text = (
            f"👋 Здравствуйте, *{teacher_name}*!\n\n"
            f"Вы авторизованы как *Преподаватель* школы MJG.\n"
            f"Здесь вы можете смотреть свои уроки, кабинеты, замены и получать уведомления."
        )
    else:
        user_class = user_prefs.get_class(user_id)
        text = (
            f"👋 Привет, *{user.first_name}*!\n\n"
            f"Ваш текущий класс: *{user_class}* (Ученик / Родитель).\n"
            f"Выберите интересующий пункт ниже 👇"
        )

    await safe_reply(update, text, reply_markup=get_main_menu_keyboard(user_id))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /profile или /role: просмотр и изменение профиля"""
    user_id = update.effective_user.id
    nav_set(user_id, "profile")
    role = user_prefs.get_role(user_id)
    notif = "ВКЛЮЧЕНЫ ✅" if user_prefs.is_notifications_enabled(user_id) else "ВЫКЛЮЧЕНЫ ❌"
    filt = user_prefs.get_subgroup_filter(user_id)
    g_str = "👦 Мальчики" if filt.get("gender") == "boys" else "👧 Девочки" if filt.get("gender") == "girls" else "Все"
    a_str = f"{filt.get('academic')} группа" if filt.get("academic") else "Все"

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        text = (
            f"⚙️ *Настройки вашего профиля:*\n\n"
            f"• Роль: *👩‍🏫 Преподаватель*\n"
            f"• ФИО: *{teacher_name or 'Не выбрано'}*\n"
            f"• Уведомления о заменах: *{notif}*\n\n"
            f"Вы можете в любой момент сменить преподавателя или переключить уведомления:"
        )
    else:
        user_class = user_prefs.get_class(user_id)
        text = (
            f"⚙️ *Настройки вашего профиля:*\n\n"
            f"• Роль: *👨‍🎓 Ученик / Родитель*\n"
            f"• Класс: *{user_class or 'Не выбран'}*\n"
            f"• Подгруппа физкультуры: *{g_str}*\n"
            f"• Языковая группа: *{a_str}*\n"
            f"• Уведомления о заменах: *{notif}*\n\n"
            f"Вы можете настроить свои подгруппы или сменить класс:"
        )

    await safe_reply(update, text, reply_markup=get_profile_keyboard(user_id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    user_id = update.effective_user.id
    is_admin = admin_manager.is_admin(user_id)

    admin_extra = (
        "\n👑 *Команды администратора:*\n"
        "• /admin — Открыть панель администратора\n"
        "• /broadcast `<текст>` — Разослать сообщение всем пользователям\n"
        "• /addadmin `<ID>` — Добавить нового администратора\n"
        "• /deladmin `<ID>` — Удалить администратора\n"
        "• /admins — Список администраторов бота\n"
    ) if is_admin else ""

    text = (
        "📖 *Список доступных команд:*\n\n"
        "• /start — Главное меню с интерактивными кнопками\n"
        "• /profile или /role — Профиль, выбор подгрупп (мальчики/девочки) и уведомления\n"
        "• /today `[класс/учитель]` — Расписание и замены на сегодня\n"
        "• /tomorrow `[класс/учитель]` — Расписание и замены на следующий учебный день\n"
        "• /schedule `[класс]` — Базовое расписание уроков на неделю\n"
        "• /teacher `[фамилия]` — Расписание уроков учителя (кабинеты и классы)\n"
        "• /teachers — Список всех преподавателей школы MJG\n"
        "• /substitutions `[класс]` — Актуальные замены уроков из EduPage\n"
        "• /classes — Список всех классов школы (1A – 12C)\n"
        "• /setclass `<класс>` — Сохранить свой класс (например, `/setclass 10A`)\n"
        "• /setteacher `<фамилия>` — Сохранить профиль учителя (например, `/setteacher Кулакова`)\n"
        "• /notify — Включить / выключить авто-уведомления о новых заменах\n"
        "• /help — Эта справка\n"
        f"{admin_extra}\n"
        "💡 _Совет: Управляйте ботом удобно через кнопки в меню!_"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ В главное меню", callback_data="btn_main_menu")]
    ])
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_classes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /classes"""
    if update.effective_user:
        nav_set(update.effective_user.id, "class_groups")
    classes = (await pcall(parser.get_classes))
    classes_list = sorted(list(classes.keys()))
    text = (
        f"🏫 *Список всех доступных классов MJG* ({len(classes_list)}):\n\n"
        f"{', '.join(classes_list)}\n\n"
        f"Чтобы установить свой класс: `/setclass <название>` или выберите его кнопкой ниже."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎒 Выбрать класс по кнопкам", callback_data="btn_choose_grade_group")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]
    ])
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_teachers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /teachers: список преподавателей с возможностью выбора"""
    if update.effective_user:
        nav_set(update.effective_user.id, "t_letters", mode="view")
    teachers = (await pcall(parser.get_teachers))
    t_names = sorted(list(teachers.keys()))
    text = (
        f"👩‍🏫 *Список преподавателей школы MJG* ({len(t_names)}):\n\n"
        f"{', '.join(t_names[:30])}...\n\n"
        f"Вы можете посмотреть расписание любого учителя (классы и кабинеты), выбрав его ниже 👇"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Посмотреть расписание учителей", callback_data="btn_browse_teachers")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]
    ])
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /teacher <фамилия> для просмотра расписания любого преподавателя"""
    if not context.args:
        nav_set(update.effective_user.id, "t_letters", mode="view")
        text = "👩‍🏫 *Выберите преподавателя для просмотра расписания уроков, классов и кабинетов:*"
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard("view_tgrp_"))
        return

    query = " ".join(context.args)
    found_teacher = (await pcall(parser.normalize_teacher_name, query))

    if not found_teacher:
        await safe_reply(
            update,
            f"❌ Преподаватель по запросу *{query}* не найден.\nИспользуйте /teachers для списка всех учителей."
        )
        return

    msg = (await pcall(parser.format_teacher_day_combined_for_telegram, found_teacher, target_date=date.today()))
    await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(found_teacher, is_own_profile=False))


async def cmd_setclass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setclass <класс>"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    if not context.args:
        await safe_reply(update, "⚠️ Пожалуйста, укажите класс, например: `/setclass 10A`")
        return

    raw_class = context.args[0]
    norm_class = parser.normalize_class_name(raw_class)
    available_classes = (await pcall(parser.get_classes))

    if norm_class not in available_classes:
        await safe_reply(update, f"❌ Класс *{norm_class}* не найден.\nИспользуйте /classes для списка доступных классов.")
        return

    if chat.type in ("group", "supergroup"):
        chat_prefs.set_class(chat.id, norm_class, title=chat.title)
        text = (
            f"✅ Класс *{norm_class}* успешно привязан к этой беседе!\n\n"
            f"Теперь участники могут быстро смотреть расписание через команду `/bot` или кнопки ниже."
        )
        await safe_reply(update, text, reply_markup=get_group_chat_keyboard(chat.id))
        return

    user_prefs.set_student(user_id, norm_class)
    text = (
        f"✅ Ваш класс успешно сохранён: *{norm_class}*!\n"
        f"Роль: *Ученик / Родитель*\n"
        f"🔔 Автоматические уведомления о новых заменах для вашего класса включены."
    )
    await safe_reply(update, text, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_setteacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setteacher <фамилия>"""
    user_id = update.effective_user.id
    if not context.args:
        await safe_reply(update, "⚠️ Укажите фамилию, например: `/setteacher Кулакова` или `/setteacher Kulakova`")
        return

    query = " ".join(context.args)
    found_teacher = (await pcall(parser.normalize_teacher_name, query))

    if not found_teacher:
        await safe_reply(update, f"❌ Преподаватель по запросу *{query}* не найден.\nИспользуйте /teachers для списка.")
        return

    user_prefs.set_teacher(user_id, found_teacher)
    text = (
        f"✅ Вы успешно зарегистрированы как преподаватель:\n*👩‍🏫 {found_teacher}*!\n\n"
        f"🔔 Теперь вы будете получать уведомления в Telegram при появлении новых замен для вас."
    )
    await safe_reply(update, text, reply_markup=get_teacher_action_keyboard(found_teacher, is_own_profile=True))


async def cmd_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /notify: переключение уведомлений"""
    user_id = update.effective_user.id
    new_state = user_prefs.toggle_notifications(user_id)
    if new_state:
        msg = "🔔 *Уведомления о новых заменах ВКЛЮЧЕНЫ.*\nБот пришлет сообщение, как только появятся новые замены."
    else:
        msg = "🔕 *Уведомления о новых заменах ВЫКЛЮЧЕНЫ.*"

    keyboard = get_main_menu_keyboard(user_id)
    await safe_reply(update, msg, reply_markup=keyboard)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /today [класс/учитель]"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    role = user_prefs.get_role(user_id)

    # Если в группе и без аргументов
    if chat.type in ("group", "supergroup") and not context.args:
        c_name = chat_prefs.get_class(chat.id)
        if not c_name:
            await safe_reply(
                update,
                "⚠️ К этой беседе пока не привязан класс.\n"
                "Привяжите его командой: `/setclass <класс>` (например `/setclass 10A`)."
            )
            return
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
        return

    # Если передан аргумент: может быть класс или учитель
    if context.args:
        arg = " ".join(context.args)
        # Проверяем на учителя
        t_match = (await pcall(parser.normalize_teacher_name, arg))
        if t_match:
            msg = (await pcall(parser.format_teacher_day_combined_for_telegram, t_match, target_date=date.today()))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return

        norm_class = parser.normalize_class_name(arg)
        subgroup_filter = user_prefs.get_subgroup_filter(user_id)
        msg = (await pcall(parser.format_day_combined_for_telegram, norm_class, target_date=date.today(), subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    # Без аргументов: смотрим сохраненную роль
    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = (await pcall(parser.format_teacher_day_combined_for_telegram, teacher_name, target_date=date.today()))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите его через меню или введите, например: `/today 10A`",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)
    msg = (await pcall(parser.format_day_combined_for_telegram, norm_class, target_date=date.today(), subgroup_filter=subgroup_filter))
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tomorrow [класс/учитель]"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    role = user_prefs.get_role(user_id)
    tomorrow_dt = get_target_date_for_tomorrow()

    if chat.type in ("group", "supergroup") and not context.args:
        c_name = chat_prefs.get_class(chat.id)
        if not c_name:
            await safe_reply(
                update,
                "⚠️ К этой беседе пока не привязан класс.\n"
                "Привяжите его командой: `/setclass <класс>` (например `/setclass 10A`)."
            )
            return
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=tomorrow_dt))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
        return

    if context.args:
        arg = " ".join(context.args)
        t_match = (await pcall(parser.normalize_teacher_name, arg))
        if t_match:
            msg = (await pcall(parser.format_teacher_day_combined_for_telegram, t_match, target_date=tomorrow_dt))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return

        norm_class = parser.normalize_class_name(arg)
        subgroup_filter = user_prefs.get_subgroup_filter(user_id)
        msg = (await pcall(parser.format_day_combined_for_telegram, norm_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = (await pcall(parser.format_teacher_day_combined_for_telegram, teacher_name, target_date=tomorrow_dt))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите его через меню или введите, например: `/tomorrow 10A`",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)
    msg = (await pcall(parser.format_day_combined_for_telegram, norm_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter))
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /schedule [класс]"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    role = user_prefs.get_role(user_id)

    if chat.type in ("group", "supergroup") and not context.args:
        c_name = chat_prefs.get_class(chat.id)
        if not c_name:
            await safe_reply(
                update,
                "⚠️ К этой беседе пока не привязан класс.\n"
                "Привяжите его командой: `/setclass <класс>`."
            )
            return
        msg = (await pcall(parser.format_schedule_for_telegram, c_name))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
        return

    if context.args:
        arg = " ".join(context.args).strip()
        t_match = (await pcall(parser.normalize_teacher_name, arg))
        if t_match:
            msg = (await pcall(parser.format_teacher_schedule_for_telegram, t_match))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return
        norm_class = parser.normalize_class_name(arg)
        subgroup_filter = user_prefs.get_subgroup_filter(user_id)
        msg = (await pcall(parser.format_schedule_for_telegram, norm_class, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = (await pcall(parser.format_teacher_schedule_for_telegram, teacher_name))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите класс из списка ниже:",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)
    msg = (await pcall(parser.format_schedule_for_telegram, norm_class, subgroup_filter=subgroup_filter))
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_substitutions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /substitutions [класс/учитель]"""
    user_id = update.effective_user.id
    chat = update.effective_chat
    role = user_prefs.get_role(user_id)
    date_str = date.today().strftime("%Y-%m-%d")

    if chat.type in ("group", "supergroup") and not context.args:
        c_name = chat_prefs.get_class(chat.id)
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=c_name))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
        return

    if context.args:
        arg = " ".join(context.args).strip()
        t_match = (await pcall(parser.normalize_teacher_name, arg))
        if t_match:
            msg = (await pcall(parser.format_teacher_substitutions_for_telegram, t_match, date_str=date_str))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return
        norm_class = parser.normalize_class_name(arg)
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=norm_class))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = (await pcall(parser.format_teacher_substitutions_for_telegram, teacher_name, date_str=date_str))
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = user_prefs.get_class(user_id)
    msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=class_name))
    keyboard = get_class_action_keyboard(class_name) if class_name else get_main_menu_keyboard(user_id)
    await safe_reply(update, msg, reply_markup=keyboard)


# ================= Дополнительные пользовательские команды =================

async def cmd_room(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает расписание кабинета и список учителей: /room <номер>"""
    if context.args:
        room_query = " ".join(context.args).strip()
        text = (await pcall(parser.format_classroom_schedule_for_telegram, room_query))
        await safe_reply(update, text)
    else:
        text = (
            "🚪 *Расписание кабинета школы:*\n\n"
            "Укажите номер кабинета после команды, например:\n"
            "• `/room 204`\n"
            "• `/room 106`\n"
            "• `/room 001`\n\n"
            "Или используйте команду `/rooms` для просмотра всех кабинетов."
        )
        await safe_reply(update, text)


async def cmd_rooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Справочник всех учебных кабинетов школы: /rooms"""
    text = (await pcall(parser.format_all_teachers_classrooms_for_telegram))
    await safe_reply(update, text)


async def cmd_freerooms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободные кабинеты прямо сейчас: /freerooms"""
    text = (await pcall(parser.format_free_rooms_for_telegram))
    await safe_reply(update, text)


async def cmd_whereis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Где сейчас находится преподаватель: /whereis <учитель>"""
    if context.args:
        t_query = " ".join(context.args).strip()
        text = (await pcall(parser.format_whereis_teacher_for_telegram, t_query))
        await safe_reply(update, text)
    else:
        text = (
            "🔍 *Где сейчас преподаватель:*\n\n"
            "Укажите фамилию учителя, например:\n"
            "• `/whereis Кулакова`\n"
            "• `/whereis Волосова`\n"
            "• `/whereis Ahv`"
        )
        await safe_reply(update, text)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Универсальный школьный поиск: /search <запрос>"""
    if context.args:
        query = " ".join(context.args).strip()
        text = (await pcall(parser.format_search_for_telegram, query))
        await safe_reply(update, text)
    else:
        text = (
            "🔍 *Универсальный школьный поиск MJG:*\n\n"
            "Введите поисковый запрос (учитель, класс, кабинет, предмет):\n"
            "• `/search Кулакова`\n"
            "• `/search 204`\n"
            "• `/search 8K`\n"
            "• `/search Математика`"
        )
        await safe_reply(update, text)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Статистика пользователей и школьной базы: /stats"""
    active_users = user_prefs.get_active_users_count()
    all_users = len(user_prefs.get_all_users())
    total_chats = len(chat_prefs.get_all_chats())
    total_classes = len((await pcall(parser.get_classes)))
    total_teachers = len((await pcall(parser.get_teachers)))
    total_rooms = len((await pcall(parser.get_all_classrooms)))
    cached_subst_dates = len(parser._substitutions_cache)

    text = (
        "📊 *Статистика Telegram-бота Tallinna Mustjõe Gümnaasium:*\n\n"
        f"👥 *Пользователи:*\n"
        f"• Активных пользователей: *{active_users}*\n"
        f"• Всего зарегистрировано: *{all_users}*\n"
        f"• Подключенных групповых бесед: *{total_chats}*\n\n"
        f"🏫 *Школьная база данных:*\n"
        f"• Классов в школе: *{total_classes}*\n"
        f"• Преподавателей: *{total_teachers}*\n"
        f"• Учебных кабинетов: *{total_rooms}*\n"
        f"• Загружено дат замен: *{cached_subst_dates}*\n\n"
        f"🔄 *Мониторинг замен:* каждые {SUBSTITUTION_CHECK_INTERVAL} сек."
    )
    await safe_reply(update, text)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выход и отписка от бота: /stop или /leave"""
    user_id = update.effective_user.id
    user_prefs.set_user_inactive(user_id, reason="user_stop")
    text = (
        "👋 *Вы успешно отписались от бота.*\n\n"
        "• Все авто-уведомления отключены.\n"
        "• Ваш профиль деактивирован, счётчик активных пользователей обновлён.\n\n"
        "Чтобы снова начать пользоваться ботом и получать расписание, просто отправьте /start в любое время!"
    )
    await safe_reply(update, text)


async def cmd_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вызов интерактивной панели бота в групповых чатах: /bot [запрос]"""
    chat = update.effective_chat
    if chat.type in ("group", "supergroup"):
        bound_class = chat_prefs.get_class(chat.id)
        if context.args:
            q = " ".join(context.args).strip().lower()
            if q in ("сегодня", "today"):
                c_name = bound_class or "8K"
                msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=date.today()))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return
            elif q in ("завтра", "tomorrow"):
                c_name = bound_class or "8K"
                tomorrow_dt = get_target_date_for_tomorrow()
                msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=tomorrow_dt))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return
            elif q in ("замены", "substitutions", "зам"):
                c_name = bound_class or "8K"
                date_str = date.today().strftime("%Y-%m-%d")
                msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=c_name))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return
            elif q in ("свободные", "кабинеты", "rooms", "freerooms"):
                msg = (await pcall(parser.format_free_rooms_for_telegram))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return
            elif q.startswith("где "):
                teacher_query = q[4:].strip()
                msg = (await pcall(parser.format_whereis_teacher_for_telegram, teacher_query))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return
            else:
                msg = (await pcall(parser.format_search_for_telegram, q))
                await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(chat.id))
                return

        if bound_class:
            text = (
                f"🤖 *Школьный бот MJG в беседе: {chat.title or 'Группа'}*\n\n"
                f"🏫 Привязанный класс: *{bound_class}*\n"
                "Выберите нужное действие на кнопках ниже:"
            )
            await safe_reply(update, text, reply_markup=get_group_chat_keyboard(chat.id))
        else:
            text = (
                f"👋 *Приветствую в беседе {chat.title or ''}!*"
                "\n\nК этой группе пока не привязан класс.\n"
                "Чтобы привязать класс к беседе, напишите команду:\n"
                "`/setclass <класс>` (например, `/setclass 10A`)"
            )
            await safe_reply(update, text)
    else:
        await cmd_start(update, context)


# ================= Команды Администратора =================

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admin: открывает отдельное меню администратора"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.* Эта команда доступна только администраторам бота.")
        return

    text = (
        "👑 *Панель администратора Tallinna Mustjõe Gümnaasium*\n\n"
        "Здесь вы можете:\n"
        "• 📢 Отправить рассылку всем пользователям бота\n"
        "• 👥 Добавлять и удалять администраторов прямо через бота\n"
        "• 📊 Просматривать детальную статистику пользователей\n"
        "• 🔄 Принудительно обновить кэш расписания и замен с EduPage\n\n"
        "Выберите нужное действие ниже 👇"
    )
    await safe_reply(update, text, reply_markup=get_admin_main_keyboard())


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /broadcast <текст>: быстрая рассылка всем пользователям"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.*")
        return

    if not context.args:
        context.user_data["admin_state"] = "WAITING_BROADCAST_TEXT"
        text = (
            "📢 *Создание рассылки:*\n\n"
            "Пожалуйста, отправьте сообщение, которое хотите разослать всем пользователям бота.\n"
            "Поддерживается форматирование Telegram (жирный, курсив, ссылки).\n\n"
            "_Для отмены нажмите кнопку ниже:_"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_panel")]])
        await safe_reply(update, text, reply_markup=keyboard)
        return

    broadcast_text = update.message.text.split(None, 1)[1]
    context.user_data["pending_broadcast_text"] = broadcast_text

    all_users = user_prefs.get_all_users()
    preview_msg = (
        "📢 *Предпросмотр сообщения для рассылки:*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{broadcast_text}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Получателей: *{len(all_users)}* пользователей.\n\n"
        "Вы действительно хотите отправить это сообщение всем?"
    )
    confirm_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить и отправить", callback_data="admin_broadcast_confirm")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin_panel")]
    ])
    await safe_reply(update, preview_msg, reply_markup=confirm_kb)


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /addadmin <user_id>"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.*")
        return

    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, "⚠️ Использование: `/addadmin <Telegram ID>` (например: `/addadmin 1637426603`)")
        return

    new_admin_id = int(context.args[0])
    if admin_manager.add_admin(new_admin_id):
        await safe_reply(
            update,
            f"✅ Пользователь с ID *{new_admin_id}* успешно назначен администратором бота!",
            reply_markup=get_admin_manage_keyboard()
        )
    else:
        await safe_reply(
            update,
            f"ℹ️ Пользователь *{new_admin_id}* уже является администратором.",
            reply_markup=get_admin_manage_keyboard()
        )


async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /deladmin <user_id>"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.*")
        return

    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, "⚠️ Использование: `/deladmin <Telegram ID>`")
        return

    target_id = int(context.args[0])
    if admin_manager.remove_admin(target_id):
        await safe_reply(
            update,
            f"✅ Пользователь *{target_id}* удален из администраторов.",
            reply_markup=get_admin_manage_keyboard()
        )
    else:
        await safe_reply(
            update,
            f"❌ Не удалось удалить администратора *{target_id}* (возможно, это последний админ в системе).",
            reply_markup=get_admin_manage_keyboard()
        )


async def cmd_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /admins: список всех администраторов"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.*")
        return

    admins = admin_manager.get_admins()
    lines = [f"👑 *Список администраторов бота ({len(admins)}):*\n"]
    for i, aid in enumerate(admins, 1):
        u_info = user_prefs.get_user(aid)
        extra = ""
        if u_info.get("teacher_name"):
            extra = f" (👩‍🏫 {u_info['teacher_name']})"
        elif u_info.get("class_name"):
            extra = f" (👨‍🎓 Класс {u_info['class_name']})"
        lines.append(f"{i}. ID: `{aid}`{extra}")

    await safe_reply(update, "\n".join(lines), reply_markup=get_admin_manage_keyboard())


async def cmd_setusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setusers <число>: установить целевое количество пользователей бота"""
    user_id = update.effective_user.id
    if not admin_manager.is_admin(user_id):
        await safe_reply(update, "⛔ *Доступ запрещён.*")
        return

    if not context.args or not context.args[0].isdigit():
        await safe_reply(update, "⚠️ Использование: `/setusers <число>`\nНапример: `/setusers 97`")
        return

    new_target = int(context.args[0])
    user_prefs.set_target_count(new_target)
    current_active = user_prefs.get_active_users_count()
    await safe_reply(
        update,
        f"✅ Базовое количество пользователей успешно установлено: *{new_target}*.\n"
        f"Текущий счётчик активных пользователей: *{current_active}*.",
        reply_markup=get_admin_main_keyboard()
    )


# ================= Обработчик нажатий кнопок (Callback Query) =================

async def _notify_callback_problem(update: Update, text: str):
    """Сообщает о проблеме новым сообщением (query.answer() уже был использован, alert показать нельзя)."""
    try:
        chat = update.effective_chat
        if chat:
            await chat.send_message(text)
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(text)
    except Exception as e:
        logger.debug(f"Не удалось отправить сообщение об ошибке: {e}")


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех inline-кнопок с защитой от сбоев и устаревших запросов"""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    user = update.effective_user

    # Личный чат: старые inline-кнопки заменены клавиатурой возле поля ввода
    chat = update.effective_chat
    if chat and chat.type == "private" and user:
        try:
            await query.answer()
        except Exception:
            pass
        nav_set(user.id, "main")
        await safe_reply(
            update,
            "⌨️ Кнопки теперь находятся *под полем ввода сообщения*.\n\n" + main_menu_text(user.id)
        )
        return

    # Админ-кнопки: проверяем доступ ДО ответа на query, чтобы alert реально показался
    if data.startswith("admin_") and not (user and admin_manager.is_admin(user.id)):
        try:
            await query.answer("⛔ Доступ к панели администратора разрешён только администраторам бота.", show_alert=True)
        except Exception:
            pass
        return

    # Сразу снимаем «часики» с кнопки (один раз)
    try:
        await query.answer()
    except Exception as e:
        logger.debug(f"query.answer non-critical: {e}")

    try:
        await _process_callback_query(update, context)
    except Exception as e:
        logger.error(f"Ошибка при обработке callback_query ({data}): {e}", exc_info=True)
        await _notify_callback_problem(
            update,
            "⚠️ Произошла ошибка при обработке кнопки. Попробуйте ещё раз или отправьте /start."
        )


async def _process_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Внутренняя логика обработки inline-кнопок"""
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)
    user_class = user_prefs.get_class(user_id)
    user_teacher = user_prefs.get_teacher(user_id)

    # 1. Главное меню
    if data == "btn_main_menu":
        greeting = (
            "🏠 *Главное меню Tallinna Mustjõe Gümnaasium:*\n\n"
            f"Текущая роль: *{'Преподаватель (' + (user_teacher or '') + ')' if role == 'teacher' else 'Класс: ' + (user_class or 'Не выбран')}*\n"
            "Выберите интересующий пункт ниже 👇"
        )
        await safe_reply(update, greeting, reply_markup=get_main_menu_keyboard(user_id))
        return

    # 2. Роли
    if data == "btn_role_select":
        text = "👤 *Выберите вашу роль в школе:*"
        await safe_reply(update, text, reply_markup=get_role_selection_keyboard())
        return

    if data == "role_choose_student":
        text = "👨‍🎓 *Вы выбрали роль: Ученик / Родитель*\nТеперь выберите возрастную ступень вашего класса:"
        await safe_reply(update, text, reply_markup=get_grade_groups_keyboard())
        return

    if data == "role_choose_teacher":
        text = (
            "👩‍🏫 *Вы выбрали роль: Преподаватель*\n\n"
            "Выберите первую букву вашей фамилии или найдите себя в списке:\n"
            "_(Вы также можете просто отправить фамилию текстом в чат)_"
        )
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
        return

    # 3. Настройки и профиль
    if data == "btn_profile":
        notif = "ВКЛЮЧЕНЫ ✅" if user_prefs.is_notifications_enabled(user_id) else "ВЫКЛЮЧЕНЫ ❌"
        filt = user_prefs.get_subgroup_filter(user_id)
        g_str = "👦 Мальчики" if filt.get("gender") == "boys" else "👧 Девочки" if filt.get("gender") == "girls" else "Все"
        a_str = f"{filt.get('academic')} группа" if filt.get("academic") else "Все"

        if role == "teacher":
            text = (
                f"⚙️ *Ваш профиль:*\n\n"
                f"• Роль: *👩‍🏫 Преподаватель*\n"
                f"• ФИО: *{user_teacher or 'Не выбрано'}*\n"
                f"• Уведомления о заменах: *{notif}*\n\n"
                f"Что вы хотите изменить?"
            )
        else:
            text = (
                f"⚙️ *Ваш профиль:*\n\n"
                f"• Роль: *👨‍🎓 Ученик / Родитель*\n"
                f"• Класс: *{user_class or 'Не выбран'}*\n"
                f"• Подгруппа физкультуры: *{g_str}*\n"
                f"• Языковая группа: *{a_str}*\n"
                f"• Уведомления о заменах: *{notif}*\n\n"
                f"Что вы хотите настроить?"
            )
        await safe_reply(update, text, reply_markup=get_profile_keyboard(user_id))
        return

    # 4. Настройка подгрупп (мальчики / девочки / языковые группы)
    if data == "btn_subgroup_settings":
        filt = user_prefs.get_subgroup_filter(user_id)
        g_str = "👦 Мальчики" if filt.get("gender") == "boys" else "👧 Девочки" if filt.get("gender") == "girls" else "Все (без фильтра)"
        a_str = f"👥 {filt.get('academic')} группа" if filt.get("academic") else "Все (без фильтра)"

        text = (
            "👥 *Настройка подгрупп для уроков:*\n\n"
            f"• Физкультура: *{g_str}*\n"
            f"• Языки и деления на группы: *{a_str}*\n\n"
            "Нажмите на нужные кнопки ниже, чтобы расписание показывало подходящую группу 👇"
        )
        await safe_reply(update, text, reply_markup=get_subgroups_keyboard(user_id))
        return

    if data.startswith("set_grp_"):
        grp_action = data.replace("set_grp_", "")
        if grp_action == "gender_boys":
            user_prefs.set_gender_filter(user_id, "boys")
        elif grp_action == "gender_girls":
            user_prefs.set_gender_filter(user_id, "girls")
        elif grp_action == "gender_all":
            user_prefs.set_gender_filter(user_id, None)
        elif grp_action == "acad_1":
            user_prefs.set_academic_filter(user_id, "1")
        elif grp_action == "acad_2":
            user_prefs.set_academic_filter(user_id, "2")
        elif grp_action == "acad_all":
            user_prefs.set_academic_filter(user_id, None)

        filt = user_prefs.get_subgroup_filter(user_id)
        g_str = "👦 Мальчики" if filt.get("gender") == "boys" else "👧 Девочки" if filt.get("gender") == "girls" else "Все (без фильтра)"
        a_str = f"👥 {filt.get('academic')} группа" if filt.get("academic") else "Все (без фильтра)"

        text = (
            "✅ *Настройки подгрупп обновлены!*\n\n"
            f"• Физкультура: *{g_str}*\n"
            f"• Языки и группы: *{a_str}*\n\n"
            "Теперь расписание уроков будет отображаться с учётом выбранных подгрупп."
        )
        await safe_reply(update, text, reply_markup=get_subgroups_keyboard(user_id))
        return

    # 5. Уведомления
    if data == "btn_toggle_notifications":
        new_state = user_prefs.toggle_notifications(user_id)
        status_txt = "включены 🔔" if new_state else "выключены 🔕"
        await safe_reply(
            update,
            f"✅ Уведомления о новых заменах теперь *{status_txt}*!",
            reply_markup=get_profile_keyboard(user_id)
        )
        return

    # 6. Выбор класса для ученика
    if data == "btn_choose_grade_group":
        text = "🎒 *Выберите группу классов:*"
        await safe_reply(update, text, reply_markup=get_grade_groups_keyboard())
        return

    if data in ("grp_1_4", "grp_5_9", "grp_10_12"):
        group_name = data.replace("grp_", "")
        text = "🏫 *Выберите ваш класс:*"
        await safe_reply(update, text, reply_markup=get_classes_keyboard(group_name))
        return

    # 6.1 Интерактивные кнопки групповых чатов
    if data.startswith("grp_today_"):
        cid = data.replace("grp_today_", "")
        c_name = chat_prefs.get_class(cid)
        if not c_name:
            await safe_reply(update, "⚠️ К этой беседе пока не привязан класс.\nИспользуйте: `/setclass <класс>`")
            return
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_tomorrow_"):
        cid = data.replace("grp_tomorrow_", "")
        c_name = chat_prefs.get_class(cid)
        if not c_name:
            await safe_reply(update, "⚠️ К этой беседе пока не привязан класс.\nИспользуйте: `/setclass <класс>`")
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=tomorrow_dt))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_schedule_"):
        cid = data.replace("grp_schedule_", "")
        c_name = chat_prefs.get_class(cid)
        if not c_name:
            await safe_reply(update, "⚠️ К этой беседе пока не привязан класс.\nИспользуйте: `/setclass <класс>`")
            return
        msg = (await pcall(parser.format_schedule_for_telegram, c_name))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_subst_"):
        cid = data.replace("grp_subst_", "")
        c_name = chat_prefs.get_class(cid)
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=c_name))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_free_"):
        cid = data.replace("grp_free_", "")
        msg = (await pcall(parser.format_free_rooms_for_telegram))
        await safe_reply(update, msg, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_setclass_"):
        cid = data.replace("grp_setclass_", "")
        text = (
            "⚙️ *Смена класса для этой беседы:*\n\n"
            "Отправьте в чат команду:\n"
            "`/setclass <класс>` (например: `/setclass 10A` или `/setclass 8K`).\n\n"
            "Или выберите ступень классов кнопками ниже:"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎒 1 – 4 классы", callback_data=f"gchat_grp_{cid}_1_4")],
            [InlineKeyboardButton("📚 5 – 9 классы", callback_data=f"gchat_grp_{cid}_5_9")],
            [InlineKeyboardButton("🎓 10 – 12 классы", callback_data=f"gchat_grp_{cid}_10_12")],
            [InlineKeyboardButton("◀️ Отмена", callback_data=f"grp_cancel_{cid}")]
        ])
        await safe_reply(update, text, reply_markup=kb)
        return

    if data.startswith("gchat_grp_"):
        rest = data.replace("gchat_grp_", "")
        parts = rest.split("_", 1)
        cid = parts[0]
        group_code = parts[1]
        all_classes = sorted(list((await pcall(parser.get_classes)).keys()))
        if group_code == "1_4":
            classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [1, 2, 3, 4])]
        elif group_code == "5_9":
            classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [5, 6, 7, 8, 9])]
        elif group_code == "10_12":
            classes = [c for c in all_classes if any(c.startswith(str(d)) for d in [10, 11, 12])]
        else:
            classes = all_classes

        buttons = []
        row = []
        for c in classes:
            row.append(InlineKeyboardButton(c, callback_data=f"gchat_set_{cid}_{c}"))
            if len(row) == 3:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data=f"grp_setclass_{cid}")])
        await safe_reply(update, "🏫 *Выберите класс для беседы:*", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("gchat_set_"):
        rest = data.replace("gchat_set_", "")
        parts = rest.split("_", 1)
        cid = parts[0]
        chosen_class = parts[1]
        chat_title = update.effective_chat.title if update.effective_chat else None
        chat_prefs.set_class(cid, chosen_class, title=chat_title)
        resp_text = f"✅ Класс *{chosen_class}* успешно привязан к этой беседе!"
        await safe_reply(update, resp_text, reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("grp_cancel_"):
        cid = data.replace("grp_cancel_", "")
        await safe_reply(update, "Панель управления беседой:", reply_markup=get_group_chat_keyboard(cid))
        return

    if data.startswith("set_class_"):
        chosen_class = data.replace("set_class_", "")
        user_prefs.set_student(user_id, chosen_class)
        text = (
            f"✅ Класс *{chosen_class}* успешно сохранён!\n"
            f"Роль: *Ученик / Родитель*\n"
            f"🔔 Уведомления о заменах включены.\n\n"
            f"Что вы хотите посмотреть?"
        )
        await safe_reply(update, text, reply_markup=get_class_action_keyboard(chosen_class))
        return

    # 7. Выбор себя как учителя
    if data == "btn_choose_teacher_letters":
        text = "👩‍🏫 *Выберите первую букву фамилии преподавателя:*"
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
        return

    if data.startswith("tgrp_"):
        group_code = data.replace("tgrp_", "")
        text = "👩‍🏫 *Выберите себя из списка преподавателей:*"
        await safe_reply(update, text, reply_markup=get_teachers_keyboard(group_code, "set_teacher_"))
        return

    if data.startswith("set_teacher_"):
        chosen_teacher = data.replace("set_teacher_", "")
        user_prefs.set_teacher(user_id, chosen_teacher)
        text = (
            f"✅ Преподаватель *{chosen_teacher}* успешно сохранён!\n"
            f"Роль: *Преподаватель*\n"
            f"🔔 Вы будете получать уведомления при появлении замен для вас.\n\n"
            f"Что вы хотите посмотреть?"
        )
        await safe_reply(update, text, reply_markup=get_teacher_action_keyboard(chosen_teacher, is_own_profile=True))
        return

    # 8. Просмотр расписания любого учителя (для учеников и учителей)
    if data == "btn_browse_teachers":
        text = (
            "👩‍🏫 *Расписание учителей школы MJG*\n\n"
            "Здесь вы можете посмотреть расписание любого преподавателя: "
            "какие у него классы, в каких кабинетах и в какое время проходят уроки.\n\n"
            "Выберите первую букву фамилии преподавателя:\n"
            "_(или отправьте фамилию текстом в чат)_"
        )
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard("view_tgrp_"))
        return

    if data.startswith("view_tgrp_"):
        group_code = data.replace("view_tgrp_", "")
        text = "👩‍🏫 *Выберите преподавателя:*"
        await safe_reply(update, text, reply_markup=get_teachers_keyboard(group_code, "view_t_"))
        return

    if data.startswith("view_t_"):
        t_name = data.replace("view_t_", "")
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, t_name, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_today_") or data.startswith("tv_tod_"):
        t_name = data.replace("tview_today_", "").replace("tv_tod_", "")
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, t_name, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_tomorrow_") or data.startswith("tv_tom_"):
        t_name = data.replace("tview_tomorrow_", "").replace("tv_tom_", "")
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, t_name, target_date=tomorrow_dt))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_week_") or data.startswith("tv_wek_"):
        t_name = data.replace("tview_week_", "").replace("tv_wek_", "")
        msg = (await pcall(parser.format_teacher_schedule_for_telegram, t_name))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_subst_") or data.startswith("tv_sub_"):
        t_name = data.replace("tview_subst_", "").replace("tv_sub_", "")
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_teacher_substitutions_for_telegram, t_name, date_str=date_str))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_rooms_") or data.startswith("tv_rm_"):
        t_name = data.replace("tview_rooms_", "").replace("tv_rm_", "")
        msg = (await pcall(parser.format_teacher_classrooms_for_telegram, t_name))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=(t_name == user_teacher)))
        return

    if data.startswith("tview_where_") or data.startswith("tv_wh_"):
        t_name = data.replace("tview_where_", "").replace("tv_wh_", "")
        msg = (await pcall(parser.format_whereis_teacher_for_telegram, t_name))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=(t_name == user_teacher)))
        return

    # 9. Действия учителя (свой профиль)
    if data == "btn_teacher_today":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, user_teacher, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_tomorrow":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, user_teacher, target_date=tomorrow_dt))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_week":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        msg = (await pcall(parser.format_teacher_schedule_for_telegram, user_teacher))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_subst":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_teacher_substitutions_for_telegram, user_teacher, date_str=date_str))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    # 10. Действия ученика
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)

    if data == "btn_today":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = (await pcall(parser.format_day_combined_for_telegram, user_class, target_date=date.today(), subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_tomorrow":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = (await pcall(parser.format_day_combined_for_telegram, user_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_week":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = (await pcall(parser.format_schedule_for_telegram, user_class, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=user_class))
        kb = get_class_action_keyboard(user_class) if user_class else get_main_menu_keyboard(user_id)
        await safe_reply(update, msg, reply_markup=kb)
        return

    if data == "btn_all_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=None))
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]])
        await safe_reply(update, msg, reply_markup=kb)
        return

    # 11. Действия для конкретного класса
    if data.startswith("today_"):
        c_name = data.replace("today_", "")
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=date.today(), subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("tomorrow_"):
        c_name = data.replace("tomorrow_", "")
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = (await pcall(parser.format_day_combined_for_telegram, c_name, target_date=tomorrow_dt, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("week_"):
        c_name = data.replace("week_", "")
        msg = (await pcall(parser.format_schedule_for_telegram, c_name, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("subst_"):
        c_name = data.replace("subst_", "")
        date_str = date.today().strftime("%Y-%m-%d")
        msg = (await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=c_name))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("sched_"):
        parts = data.split("_")
        c_name = parts[1]
        day_idx = int(parts[2])
        msg = (await pcall(parser.format_schedule_for_telegram, c_name, day_index=day_idx, subgroup_filter=subgroup_filter))
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    # ================= Панель Администратора =================

    if data.startswith("admin_") or data == "admin_panel":
        if not admin_manager.is_admin(user_id):
            await safe_reply(update, "⛔ Доступ к панели администратора разрешён только администраторам бота.")
            return

    if data == "admin_panel":
        context.user_data.pop("admin_state", None)
        text = (
            "👑 *Панель администратора Tallinna Mustjõe Gümnaasium*\n\n"
            "Выберите действие из списка ниже:"
        )
        await safe_reply(update, text, reply_markup=get_admin_main_keyboard())
        return

    if data == "admin_broadcast_prompt":
        context.user_data["admin_state"] = "WAITING_BROADCAST_TEXT"
        text = (
            "📢 *Создание рассылки:*\n\n"
            "Отправьте текст сообщения, которое хотите разослать всем пользователям бота.\n"
            "Поддерживается форматирование Telegram (жирный текст, курсив, ссылки).\n\n"
            "_Для отмены нажмите кнопку ниже:_"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_panel")]])
        await safe_reply(update, text, reply_markup=keyboard)
        return

    if data == "admin_broadcast_confirm":
        broadcast_text = context.user_data.get("pending_broadcast_text")
        if not broadcast_text:
            await safe_reply(update, "⚠️ Текст рассылки не найден. Попробуйте снова.", reply_markup=get_admin_main_keyboard())
            return

        all_users = [k for k in user_prefs.get_all_users().keys() if k.isdigit()]
        total_count = len(all_users)

        status_msg = await query.message.reply_text(f"⏳ Начинаем рассылку для {total_count} пользователей...")

        success_count = 0
        blocked_count = 0
        failed_count = 0

        for uid_str in all_users:
            try:
                target_uid = int(uid_str)
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"📢 *Объявление от администрации MJG:*\n\n{broadcast_text}",
                    parse_mode=ParseMode.MARKDOWN
                )
                success_count += 1
            except Forbidden:
                blocked_count += 1
                user_prefs.set_user_inactive(target_uid, reason="blocked")
            except TelegramError:
                # Попытка отправки без Markdown при ошибке парсинга
                try:
                    await context.bot.send_message(
                        chat_id=int(uid_str),
                        text=f"📢 Объявление от администрации MJG:\n\n{broadcast_text}"
                    )
                    success_count += 1
                except Forbidden:
                    blocked_count += 1
                    user_prefs.set_user_inactive(target_uid, reason="blocked")
                except Exception:
                    failed_count += 1
            except Exception:
                failed_count += 1

            await asyncio.sleep(0.04)  # Защита от лимитов Telegram

        context.user_data.pop("pending_broadcast_text", None)

        report = (
            "📢 *Рассылка успешно завершена!*\n\n"
            f"📊 Всего пользователей: *{total_count}*\n"
            f"✅ Успешно доставлено: *{success_count}*\n"
            f"🚫 Заблокировали бота: *{blocked_count}*\n"
            f"⚠️ Ошибок отправки: *{failed_count}*"
        )
        try:
            await status_msg.delete()
        except Exception:
            pass
        await safe_reply(update, report, reply_markup=get_admin_main_keyboard())
        return

    if data == "admin_manage_admins":
        text = "👥 *Управление администраторами бота:*\n\nВыберите действие:"
        await safe_reply(update, text, reply_markup=get_admin_manage_keyboard())
        return

    if data == "admin_add_prompt":
        context.user_data["admin_state"] = "WAITING_ADD_ADMIN_ID"
        text = (
            "➕ *Добавление администратора:*\n\n"
            "Отправьте Telegram ID пользователя (цифры, например: `1637426603`).\n"
            "_(Узнать свой ID можно через @userinfobot в Telegram)_\n\n"
            "Или отправьте команду `/addadmin <ID>`.\n"
            "_Для отмены нажмите кнопку ниже:_"
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_manage_admins")]])
        await safe_reply(update, text, reply_markup=keyboard)
        return

    if data == "admin_del_list":
        admins = admin_manager.get_admins()
        buttons = []
        for aid in admins:
            # Кнопка удаления для каждого админа
            buttons.append([InlineKeyboardButton(f"❌ Удалить ID {aid}", callback_data=f"admin_do_del_{aid}")])
        buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="admin_manage_admins")])
        text = "➖ *Выберите администратора для удаления:*"
        await safe_reply(update, text, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("admin_do_del_"):
        target_id_str = data.replace("admin_do_del_", "")
        if target_id_str.isdigit():
            target_id = int(target_id_str)
            if admin_manager.remove_admin(target_id):
                text = f"✅ Администратор с ID *{target_id}* успешно удалён!"
            else:
                text = f"⚠️ Не удалось удалить ID *{target_id}* (нельзя удалить последнего администратора)."
        else:
            text = "❌ Неверный ID администратора."
        await safe_reply(update, text, reply_markup=get_admin_manage_keyboard())
        return

    if data == "admin_list_view":
        admins = admin_manager.get_admins()
        lines = [f"📋 *Список администраторов бота ({len(admins)}):*\n"]
        for i, aid in enumerate(admins, 1):
            u_info = user_prefs.get_user(aid)
            extra = ""
            if u_info.get("teacher_name"):
                extra = f" (👩‍🏫 {u_info['teacher_name']})"
            elif u_info.get("class_name"):
                extra = f" (👨‍🎓 Класс {u_info['class_name']})"
            lines.append(f"{i}. ID: `{aid}`{extra}")

        await safe_reply(update, "\n".join(lines), reply_markup=get_admin_manage_keyboard())
        return

    if data == "admin_stats":
        all_u = user_prefs.get_all_users()
        total_u = len(all_u)
        students_c = sum(1 for u in all_u.values() if u.get("role") == "student" or u.get("class_name"))
        teachers_c = sum(1 for u in all_u.values() if u.get("role") == "teacher" or u.get("teacher_name"))
        notif_c = sum(1 for u in all_u.values() if u.get("notifications", True))
        admins_c = len(admin_manager.get_admins())

        classes = (await pcall(parser.get_classes))
        teachers = (await pcall(parser.get_teachers))

        text = (
            "📊 *Статистика Telegram-бота MJG:*\n\n"
            f"👥 Всего пользователей: *{total_u}*\n"
            f"👨‍🎓 Учеников / родителей: *{students_c}*\n"
            f"👩‍🏫 Преподавателей: *{teachers_c}*\n"
            f"🔔 С включенными уведомлениями: *{notif_c}*\n"
            f"👑 Администраторов: *{admins_c}*\n\n"
            f"🏫 Классов в базе: *{len(classes)}*\n"
            f"👩‍🏫 Преподавателей в базе: *{len(teachers)}*\n"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить кэш EduPage", callback_data="admin_refresh_cache")],
            [InlineKeyboardButton("◀️ Назад в админ-панель", callback_data="admin_panel")]
        ])
        await safe_reply(update, text, reply_markup=keyboard)
        return

    if data == "admin_refresh_cache":
        try:
            await asyncio.to_thread(parser.get_regular_timetable, force_refresh=True)
            await asyncio.to_thread(parser.get_substitutions, force_refresh=True)
            text = "✅ *Кэш расписания уроков и замен успешно обновлён онлайн с EduPage!*"
        except Exception as e:
            text = f"⚠️ Ошибка при обновлении кэша: {e}"
        await safe_reply(update, text, reply_markup=get_admin_main_keyboard())
        return

    # Если callback_data не распознан ни одной веткой
    logger.warning(f"Неизвестный callback_data: '{data}' от user_id={user_id}")
    await safe_reply(
        update,
        "⚠️ Эта кнопка устарела. Вот актуальное меню 👇",
        reply_markup=get_main_menu_keyboard(user_id)
    )


# ================= Постоянная клавиатура возле поля ввода (личные чаты) =================
# Кнопки находятся под полем ввода сообщения и отправляют обычный текст, поэтому
# не зависят от callback-запросов и работают всегда. В группах остаются inline-кнопки.

B_MENU = "🏠 Меню"
B_BACK = "◀️ Назад"
B_CANCEL = "❌ Отмена"
B_TODAY = "📅 Сегодня"
B_TOMORROW = "📆 Завтра"
B_WEEK = "🗓 Неделя"
B_SUBST = "🔄 Замены"
B_ALLSUBST = "🏫 Замены школы"
B_TEACHERS = "👩‍🏫 Учителя"
B_FREE = "🚪 Свободные кабинеты"
B_CLASS = "🏫 Мой класс"
B_PROFILE = "⚙️ Профиль"
B_ADMIN = "👑 Админ"
B_T_TODAY = "📅 Мои уроки сегодня"
B_T_TOMORROW = "📆 Мои уроки завтра"
B_T_WEEK = "🗓 Моё расписание"
B_T_SUBST = "🔄 Мои замены"
B_T_ROOMS = "🚪 Мои кабинеты"
B_T_WHERE = "🔍 Где я сейчас"
B_ROLE_STUDENT = "👨‍🎓 Я ученик / родитель"
B_ROLE_TEACHER = "👩‍🏫 Я учитель"
B_G_1_4 = "🎒 1–4 классы"
B_G_5_9 = "📚 5–9 классы"
B_G_10_12 = "🎓 10–12 классы"
GROUP_LABELS = {B_G_1_4: "1_4", B_G_5_9: "5_9", B_G_10_12: "10_12"}
LETTER_LABELS = {
    "🔤 A–D": "A_D", "🔤 E–J": "E_J", "🔤 K": "K", "🔤 L–M": "L_M",
    "🔤 N–R": "N_R", "🔤 S–T": "S_T", "🔤 U–Ü": "U_Z",
}
V_TODAY = "📅 Уроки сегодня"
V_TOMORROW = "📆 Уроки завтра"
V_WEEK = "🗓 Неделя учителя"
V_SUBST = "🔄 Замены учителя"
V_ROOMS = "🚪 Кабинеты учителя"
V_WHERE = "🔍 Где сейчас"
V_OTHER = "👩‍🏫 Другой учитель"
P_SUBGROUPS = "👥 Подгруппы"
P_CHANGE_CLASS = "🏫 Сменить класс"
P_CHANGE_TEACHER = "👩‍🏫 Сменить преподавателя"
P_NOTIF_ON = "🔔 Уведомления: ВКЛ"
P_NOTIF_OFF = "🔕 Уведомления: ВЫКЛ"
S_BOYS, S_GIRLS, S_G_ALL = "👦 Мальчики", "👧 Девочки", "Физ-ра: все"
S_1, S_2, S_A_ALL = "👥 1 группа", "👥 2 группа", "Языки: все"
A_STATS = "📊 Статистика бота"
A_REFRESH = "🔄 Обновить кэш"
A_BROADCAST = "📢 Рассылка"
A_ADMINS = "👥 Администраторы"
A_ADD = "➕ Добавить админа"
A_DEL = "➖ Удалить админа"
A_LIST = "📋 Список админов"
A_SEND = "✅ Отправить рассылку"

ALIASES = {
    "сегодня": B_TODAY, "today": B_TODAY,
    "завтра": B_TOMORROW, "tomorrow": B_TOMORROW,
    "неделя": B_WEEK, "вся неделя": B_WEEK, "расписание": B_WEEK, "schedule": B_WEEK,
    "замены": B_SUBST, "substitutions": B_SUBST, "zameni": B_SUBST,
    "учителя": B_TEACHERS, "teachers": B_TEACHERS,
    "кабинеты": B_FREE, "свободные кабинеты": B_FREE, "rooms": B_FREE, "freerooms": B_FREE,
    "меню": B_MENU, "главное меню": B_MENU, "menu": B_MENU, "старт": B_MENU, "start": B_MENU,
    "профиль": B_PROFILE, "настройки": B_PROFILE, "profile": B_PROFILE, "settings": B_PROFILE,
    "админ": B_ADMIN, "админка": B_ADMIN, "admin": B_ADMIN,
    "отмена": B_CANCEL, "назад": B_BACK,
}

# Состояние меню каждого пользователя (в памяти; после перезапуска сбрасывается в главное меню)
NAV: Dict[int, Dict[str, Any]] = {}


def nav_get(uid: int) -> Dict[str, Any]:
    return NAV.get(uid) or {"state": "main"}


def nav_set(uid: int, state: str = "main", **extra):
    NAV[uid] = {"state": state, **extra}


def _rk(rows: List[List[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _chunk(items: List[str], n: int) -> List[List[str]]:
    return [list(items[i:i + n]) for i in range(0, len(items), n)]


def role_select_reply_kb() -> ReplyKeyboardMarkup:
    return _rk([[B_ROLE_STUDENT], [B_ROLE_TEACHER]])


def main_reply_kb(uid: int) -> ReplyKeyboardMarkup:
    if user_prefs.get_role(uid) == "teacher":
        rows = [
            [B_T_TODAY, B_T_TOMORROW],
            [B_T_WEEK, B_T_SUBST],
            [B_T_ROOMS, B_T_WHERE],
            [B_TEACHERS, B_ALLSUBST],
            [B_FREE, B_PROFILE],
        ]
    else:
        rows = [
            [B_TODAY, B_TOMORROW],
            [B_WEEK, B_SUBST],
            [B_TEACHERS, B_ALLSUBST],
            [B_FREE, B_CLASS],
            [B_PROFILE],
        ]
    if admin_manager.is_admin(uid):
        rows[-1].append(B_ADMIN)
    return _rk(rows)


def profile_reply_kb(uid: int) -> ReplyKeyboardMarkup:
    notif = P_NOTIF_ON if user_prefs.is_notifications_enabled(uid) else P_NOTIF_OFF
    if user_prefs.get_role(uid) == "teacher":
        rows = [[notif], [P_CHANGE_TEACHER], [B_ROLE_STUDENT], [B_MENU]]
    else:
        rows = [[notif], [P_SUBGROUPS, P_CHANGE_CLASS], [B_ROLE_TEACHER], [B_MENU]]
    return _rk(rows)


def subgroups_reply_kb(uid: int) -> ReplyKeyboardMarkup:
    filt = user_prefs.get_subgroup_filter(uid)
    g, a = filt.get("gender"), filt.get("academic")

    def mark(label: str, active: bool) -> str:
        return f"✅ {label}" if active else label

    return _rk([
        [mark(S_BOYS, g == "boys"), mark(S_GIRLS, g == "girls"), mark(S_G_ALL, not g)],
        [mark(S_1, a == "1"), mark(S_2, a == "2"), mark(S_A_ALL, not a)],
        [B_BACK, B_MENU],
    ])


def reply_kb_for(uid: int) -> ReplyKeyboardMarkup:
    """Строит клавиатуру под полем ввода по текущему состоянию меню пользователя"""
    st = nav_get(uid)
    s = st.get("state", "main")

    if s == "class_groups":
        return _rk([[B_G_1_4], [B_G_5_9], [B_G_10_12], [B_MENU]])
    if s == "class_list":
        return _rk(_chunk(st.get("classes", []), 4) + [[B_BACK, B_MENU]])
    if s == "t_letters":
        labels = list(LETTER_LABELS.keys())
        return _rk(_chunk(labels, 3) + [[B_MENU]])
    if s == "t_list":
        return _rk(_chunk(st.get("names", []), 2) + [[B_BACK, B_MENU]])
    if s == "view_teacher":
        return _rk([[V_TODAY, V_TOMORROW], [V_WEEK, V_SUBST], [V_ROOMS, V_WHERE], [V_OTHER, B_MENU]])
    if s == "profile":
        return profile_reply_kb(uid)
    if s == "subgroups":
        return subgroups_reply_kb(uid)
    if s == "admin" and admin_manager.is_admin(uid):
        return _rk([[A_STATS, A_REFRESH], [A_BROADCAST, A_ADMINS], [B_MENU]])
    if s == "admin_admins" and admin_manager.is_admin(uid):
        return _rk([[A_ADD, A_DEL], [A_LIST], [B_BACK, B_MENU]])
    if s == "broadcast_confirm" and admin_manager.is_admin(uid):
        return _rk([[A_SEND], [B_CANCEL]])
    if s in ("admin_wait",) and admin_manager.is_admin(uid):
        return _rk([[B_CANCEL]])

    if not user_prefs.has_role(uid):
        return role_select_reply_kb()
    return main_reply_kb(uid)


def main_menu_text(uid: int) -> str:
    role = user_prefs.get_role(uid)
    if not user_prefs.has_role(uid):
        return "👤 *Выберите, кто вы:*"
    if role == "teacher":
        who = f"Преподаватель ({user_prefs.get_teacher(uid)})"
    else:
        who = f"Класс: {user_prefs.get_class(uid)}"
    return (
        "🏠 *Главное меню Tallinna Mustjõe Gümnaasium*\n\n"
        f"Текущая роль: *{who}*\n"
        "Выберите пункт на кнопках под полем ввода 👇"
    )


def profile_text(uid: int) -> str:
    role = user_prefs.get_role(uid)
    notif = "ВКЛЮЧЕНЫ ✅" if user_prefs.is_notifications_enabled(uid) else "ВЫКЛЮЧЕНЫ ❌"
    if role == "teacher":
        return (
            "⚙️ *Ваш профиль:*\n\n"
            "• Роль: *👩‍🏫 Преподаватель*\n"
            f"• ФИО: *{user_prefs.get_teacher(uid) or 'Не выбрано'}*\n"
            f"• Уведомления о заменах: *{notif}*"
        )
    filt = user_prefs.get_subgroup_filter(uid)
    g_str = "👦 Мальчики" if filt.get("gender") == "boys" else "👧 Девочки" if filt.get("gender") == "girls" else "Все"
    a_str = f"{filt.get('academic')} группа" if filt.get("academic") else "Все"
    return (
        "⚙️ *Ваш профиль:*\n\n"
        "• Роль: *👨‍🎓 Ученик / Родитель*\n"
        f"• Класс: *{user_prefs.get_class(uid) or 'Не выбран'}*\n"
        f"• Подгруппа физкультуры: *{g_str}*\n"
        f"• Языковая группа: *{a_str}*\n"
        f"• Уведомления о заменах: *{notif}*"
    )


def admin_stats_text() -> str:
    all_u = user_prefs.get_all_users()
    students_c = sum(1 for u in all_u.values() if u.get("role") == "student" or u.get("class_name"))
    teachers_c = sum(1 for u in all_u.values() if u.get("role") == "teacher" or u.get("teacher_name"))
    notif_c = sum(1 for u in all_u.values() if u.get("notifications", True))
    return (
        "📊 *Статистика Telegram-бота MJG:*\n\n"
        f"👥 Всего пользователей: *{len(all_u)}*\n"
        f"👨‍🎓 Учеников / родителей: *{students_c}*\n"
        f"👩‍🏫 Преподавателей: *{teachers_c}*\n"
        f"🔔 С включенными уведомлениями: *{notif_c}*\n"
        f"👑 Администраторов: *{len(admin_manager.get_admins())}*\n"
    )


def _teacher_names_for_group(teachers: Dict[str, Any], group_code: str) -> List[str]:
    sets = {
        "A_D": "ABCD", "E_J": "EFGHIJ", "K": "K", "L_M": "LM",
        "N_R": "NOPQR", "S_T": "ST", "U_Z": "UVWXYZÕÄÖÜŠŽ",
    }
    letters = sets.get(group_code, "")
    result = []
    for full_name, info in teachers.items():
        surname = info.get("surname") or full_name.split()[0]
        first = surname[0].upper() if surname else ""
        if first and first in letters:
            result.append(full_name)
    return result or list(teachers.keys())[:20]


def _classes_for_group(all_classes: List[str], group_code: str) -> List[str]:
    def grade(c: str) -> int:
        m = re.match(r"^(\d+)", c)
        return int(m.group(1)) if m else 0
    ranges = {"1_4": range(1, 5), "5_9": range(5, 10), "10_12": range(10, 13)}
    rng = ranges.get(group_code)
    items = [c for c in all_classes if rng and grade(c) in rng]
    return sorted(items, key=lambda c: (grade(c), c))


async def run_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Рассылка сообщения всем пользователям (для кнопки админ-панели)"""
    uid = update.effective_user.id
    broadcast_text = context.user_data.get("pending_broadcast_text")
    if not broadcast_text:
        nav_set(uid, "admin")
        await safe_reply(update, "⚠️ Текст рассылки не найден. Попробуйте снова.")
        return

    all_users = [k for k in user_prefs.get_all_users().keys() if k.isdigit()]
    total_count = len(all_users)
    success = blocked = failed = 0
    await update.effective_chat.send_message(f"⏳ Начинаем рассылку для {total_count} пользователей...")

    for uid_str in all_users:
        target_uid = int(uid_str)
        try:
            await context.bot.send_message(
                chat_id=target_uid,
                text=f"📢 *Объявление от администрации MJG:*\n\n{broadcast_text}",
                parse_mode=ParseMode.MARKDOWN
            )
            success += 1
        except Forbidden:
            blocked += 1
            user_prefs.set_user_inactive(target_uid, reason="blocked")
        except TelegramError:
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"📢 Объявление от администрации MJG:\n\n{broadcast_text}"
                )
                success += 1
            except Forbidden:
                blocked += 1
                user_prefs.set_user_inactive(target_uid, reason="blocked")
            except Exception:
                failed += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.04)

    context.user_data.pop("pending_broadcast_text", None)
    nav_set(uid, "admin")
    await safe_reply(
        update,
        "📢 *Рассылка завершена!*\n\n"
        f"📊 Всего пользователей: *{total_count}*\n"
        f"✅ Доставлено: *{success}*\n"
        f"🚫 Заблокировали бота: *{blocked}*\n"
        f"⚠️ Ошибок отправки: *{failed}*"
    )


async def _prompt_pick(update: Update, uid: int):
    """Просит выбрать класс/учителя, если профиль не заполнен"""
    if user_prefs.get_role(uid) == "teacher":
        nav_set(uid, "t_letters", mode="self")
        await safe_reply(update, "⚠️ Сначала выберите себя в списке преподавателей.\n*Выберите первую букву фамилии:*")
    elif user_prefs.has_role(uid):
        nav_set(uid, "class_groups")
        await safe_reply(update, "⚠️ Сначала выберите ваш класс.\n*Выберите ступень:*")
    else:
        nav_set(uid, "main")
        await safe_reply(update, "👤 *Сначала выберите, кто вы:*")


async def _send_day(update: Update, uid: int, tomorrow: bool):
    role = user_prefs.get_role(uid)
    target = get_target_date_for_tomorrow() if tomorrow else date.today()
    if role == "teacher" and user_prefs.get_teacher(uid):
        msg = await pcall(parser.format_teacher_day_combined_for_telegram, user_prefs.get_teacher(uid), target_date=target)
    elif role != "teacher" and user_prefs.get_class(uid):
        msg = await pcall(
            parser.format_day_combined_for_telegram, user_prefs.get_class(uid),
            target_date=target, subgroup_filter=user_prefs.get_subgroup_filter(uid)
        )
    else:
        await _prompt_pick(update, uid)
        return
    nav_set(uid, "main")
    await safe_reply(update, msg)


async def private_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Обрабатывает нажатия кнопок возле клавиатуры и введённый текст в личном чате.
    Возвращает True, если сообщение обработано.
    """
    uid = update.effective_user.id
    raw = update.message.text.strip()
    t = raw
    if t not in ALL_BUTTON_LABELS:
        alias = ALIASES.get(re.sub(r"^[^\w]+", "", t.lower()).strip())
        if alias:
            t = alias
    t_plain = t.replace("✅ ", "", 1).strip()  # кнопки подгрупп с галочкой

    st = nav_get(uid)
    state = st.get("state", "main")
    role = user_prefs.get_role(uid)
    is_admin = admin_manager.is_admin(uid)
    date_str = date.today().strftime("%Y-%m-%d")

    # ---------- Общие кнопки ----------
    if t == B_MENU:
        nav_set(uid, "main")
        await safe_reply(update, main_menu_text(uid))
        return True

    if t == B_CANCEL:
        context.user_data.pop("admin_state", None)
        context.user_data.pop("pending_broadcast_text", None)
        back_to_admin = is_admin and state in ("admin", "admin_wait", "broadcast_confirm", "admin_admins")
        nav_set(uid, "admin" if back_to_admin else "main")
        await safe_reply(update, "Отменено ✅")
        return True

    if t == B_BACK:
        target = {
            "class_list": ("class_groups", {}),
            "t_list": ("t_letters", {"mode": st.get("mode", "view")}),
            "subgroups": ("profile", {}),
            "admin_admins": ("admin", {}),
            "admin": ("main", {}),
            "profile": ("main", {}),
            "t_letters": ("main", {}),
            "class_groups": ("main", {}),
        }.get(state, ("main", {}))
        nav_set(uid, target[0], **target[1])
        if target[0] == "main":
            await safe_reply(update, main_menu_text(uid))
        elif target[0] == "profile":
            await safe_reply(update, profile_text(uid))
        elif target[0] == "class_groups":
            await safe_reply(update, "🏫 *Выберите ступень:*")
        elif target[0] == "t_letters":
            await safe_reply(update, "👩‍🏫 *Выберите первую букву фамилии:*")
        else:
            await safe_reply(update, "👑 *Панель администратора*")
        return True

    # ---------- Роль, класс ----------
    if t == B_ROLE_STUDENT:
        nav_set(uid, "class_groups")
        await safe_reply(update, "👨‍🎓 *Вы выбрали роль: Ученик / Родитель*\nВыберите ступень класса:")
        return True

    if t == B_ROLE_TEACHER:
        nav_set(uid, "t_letters", mode="self")
        await safe_reply(
            update,
            "👩‍🏫 *Вы выбрали роль: Преподаватель*\nВыберите первую букву фамилии "
            "_(или просто отправьте фамилию текстом)_:"
        )
        return True

    if t in (B_CLASS, P_CHANGE_CLASS):
        nav_set(uid, "class_groups")
        await safe_reply(update, "🏫 *Выберите ступень вашего класса:*")
        return True

    if t in GROUP_LABELS:
        code = GROUP_LABELS[t]
        all_classes = list((await pcall(parser.get_classes)).keys())
        classes = _classes_for_group(all_classes, code)
        nav_set(uid, "class_list", classes=classes, group=code)
        await safe_reply(update, "🏫 *Выберите ваш класс:*")
        return True

    # ---------- Учителя ----------
    if t in (B_TEACHERS, V_OTHER):
        nav_set(uid, "t_letters", mode="view")
        await safe_reply(
            update,
            "👩‍🏫 *Расписание учителей школы MJG*\nВыберите первую букву фамилии "
            "_(или отправьте фамилию текстом)_:"
        )
        return True

    if t == P_CHANGE_TEACHER:
        nav_set(uid, "t_letters", mode="self")
        await safe_reply(update, "👩‍🏫 *Выберите первую букву вашей фамилии:*")
        return True

    if t in LETTER_LABELS:
        teachers = await pcall(parser.get_teachers)
        names = _teacher_names_for_group(teachers, LETTER_LABELS[t])
        mode = st.get("mode", "view") if state in ("t_letters", "t_list") else "view"
        nav_set(uid, "t_list", mode=mode, names=names)
        await safe_reply(update, "👩‍🏫 *Выберите преподавателя:*")
        return True

    # ---------- Главное меню: расписание и замены ----------
    if t in (B_TODAY, B_T_TODAY):
        await _send_day(update, uid, tomorrow=False)
        return True

    if t in (B_TOMORROW, B_T_TOMORROW):
        await _send_day(update, uid, tomorrow=True)
        return True

    if t in (B_WEEK, B_T_WEEK):
        if role == "teacher" and user_prefs.get_teacher(uid):
            msg = await pcall(parser.format_teacher_schedule_for_telegram, user_prefs.get_teacher(uid))
        elif role != "teacher" and user_prefs.get_class(uid):
            msg = await pcall(
                parser.format_schedule_for_telegram, user_prefs.get_class(uid),
                subgroup_filter=user_prefs.get_subgroup_filter(uid)
            )
        else:
            await _prompt_pick(update, uid)
            return True
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    if t in (B_SUBST, B_T_SUBST):
        if role == "teacher" and user_prefs.get_teacher(uid):
            msg = await pcall(parser.format_teacher_substitutions_for_telegram, user_prefs.get_teacher(uid), date_str=date_str)
        elif role != "teacher" and user_prefs.get_class(uid):
            msg = await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=user_prefs.get_class(uid))
        else:
            await _prompt_pick(update, uid)
            return True
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    if t == B_ALLSUBST:
        msg = await pcall(parser.format_substitutions_for_telegram, date_str=date_str, class_name=None)
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    if t == B_FREE:
        msg = await pcall(parser.format_free_rooms_for_telegram)
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    if t == B_T_ROOMS:
        if not user_prefs.get_teacher(uid):
            await _prompt_pick(update, uid)
            return True
        msg = await pcall(parser.format_teacher_classrooms_for_telegram, user_prefs.get_teacher(uid))
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    if t == B_T_WHERE:
        if not user_prefs.get_teacher(uid):
            await _prompt_pick(update, uid)
            return True
        msg = await pcall(parser.format_whereis_teacher_for_telegram, user_prefs.get_teacher(uid))
        nav_set(uid, "main")
        await safe_reply(update, msg)
        return True

    # ---------- Просмотр расписания другого учителя ----------
    if t in (V_TODAY, V_TOMORROW, V_WEEK, V_SUBST, V_ROOMS, V_WHERE):
        name = st.get("name") if state == "view_teacher" else None
        if not name:
            nav_set(uid, "t_letters", mode="view")
            await safe_reply(update, "⚠️ Сначала выберите преподавателя.\n*Выберите первую букву фамилии:*")
            return True
        if t == V_TODAY:
            msg = await pcall(parser.format_teacher_day_combined_for_telegram, name, target_date=date.today())
        elif t == V_TOMORROW:
            msg = await pcall(parser.format_teacher_day_combined_for_telegram, name, target_date=get_target_date_for_tomorrow())
        elif t == V_WEEK:
            msg = await pcall(parser.format_teacher_schedule_for_telegram, name)
        elif t == V_SUBST:
            msg = await pcall(parser.format_teacher_substitutions_for_telegram, name, date_str=date_str)
        elif t == V_ROOMS:
            msg = await pcall(parser.format_teacher_classrooms_for_telegram, name)
        else:
            msg = await pcall(parser.format_whereis_teacher_for_telegram, name)
        await safe_reply(update, msg)
        return True

    # ---------- Профиль и подгруппы ----------
    if t == B_PROFILE:
        nav_set(uid, "profile")
        await safe_reply(update, profile_text(uid))
        return True

    if t in (P_NOTIF_ON, P_NOTIF_OFF):
        new_state = user_prefs.toggle_notifications(uid)
        nav_set(uid, "profile")
        await safe_reply(update, f"✅ Уведомления о новых заменах теперь *{'включены 🔔' if new_state else 'выключены 🔕'}*!")
        return True

    if t == P_SUBGROUPS:
        nav_set(uid, "subgroups")
        await safe_reply(update, "👥 *Настройка подгрупп:*\nВыберите подгруппу физкультуры и языковую группу 👇")
        return True

    if state == "subgroups" and t_plain in (S_BOYS, S_GIRLS, S_G_ALL, S_1, S_2, S_A_ALL):
        if t_plain == S_BOYS:
            user_prefs.set_gender_filter(uid, "boys")
        elif t_plain == S_GIRLS:
            user_prefs.set_gender_filter(uid, "girls")
        elif t_plain == S_G_ALL:
            user_prefs.set_gender_filter(uid, None)
        elif t_plain == S_1:
            user_prefs.set_academic_filter(uid, "1")
        elif t_plain == S_2:
            user_prefs.set_academic_filter(uid, "2")
        else:
            user_prefs.set_academic_filter(uid, None)
        await safe_reply(update, "✅ *Подгруппы обновлены!*\n\n" + profile_text(uid))
        return True

    # ---------- Администратор ----------
    admin_labels = (B_ADMIN, A_STATS, A_REFRESH, A_BROADCAST, A_ADMINS, A_ADD, A_DEL, A_LIST, A_SEND)
    if t in admin_labels:
        if not is_admin:
            await safe_reply(update, "⛔ Доступ только для администраторов бота.")
            return True

        if t == B_ADMIN:
            nav_set(uid, "admin")
            await safe_reply(update, "👑 *Панель администратора Tallinna Mustjõe Gümnaasium*")
        elif t == A_STATS:
            nav_set(uid, "admin")
            await safe_reply(update, admin_stats_text())
        elif t == A_REFRESH:
            nav_set(uid, "admin")
            try:
                await pcall(parser.get_regular_timetable, force_refresh=True)
                await pcall(parser.get_substitutions, force_refresh=True)
                await safe_reply(update, "✅ *Кэш расписания и замен обновлён с EduPage!*")
            except Exception as e:
                await safe_reply(update, f"⚠️ Ошибка при обновлении кэша: {e}")
        elif t == A_BROADCAST:
            context.user_data["admin_state"] = "WAITING_BROADCAST_TEXT"
            nav_set(uid, "admin_wait")
            await safe_reply(update, "📢 *Рассылка:* отправьте текст сообщения для всех пользователей.")
        elif t == A_ADMINS:
            nav_set(uid, "admin_admins")
            await safe_reply(update, "👥 *Управление администраторами:*")
        elif t == A_ADD:
            context.user_data["admin_state"] = "WAITING_ADD_ADMIN_ID"
            nav_set(uid, "admin_wait")
            await safe_reply(update, "➕ Отправьте Telegram ID нового администратора (цифры).")
        elif t == A_DEL:
            context.user_data["admin_state"] = "WAITING_DEL_ADMIN_ID"
            nav_set(uid, "admin_wait")
            ids = ", ".join(f"`{a}`" for a in admin_manager.get_admins())
            await safe_reply(update, f"➖ Отправьте ID администратора для удаления.\nСейчас админы: {ids}")
        elif t == A_LIST:
            lines = [f"📋 *Администраторы бота ({len(admin_manager.get_admins())}):*\n"]
            for i, aid in enumerate(admin_manager.get_admins(), 1):
                lines.append(f"{i}. `{aid}`")
            nav_set(uid, "admin_admins")
            await safe_reply(update, "\n".join(lines))
        elif t == A_SEND:
            if state != "broadcast_confirm":
                nav_set(uid, "admin")
                await safe_reply(update, "⚠️ Нет подготовленной рассылки.")
            else:
                await run_broadcast(update, context)
        return True

    # ---------- Введённый текст: класс или учитель ----------
    norm_class = parser.normalize_class_name(raw)
    classes = await pcall(parser.get_classes)
    in_class_pick = state in ("class_groups", "class_list")
    if norm_class and norm_class in classes:
        if in_class_pick or not user_prefs.has_role(uid) or role != "teacher":
            user_prefs.set_student(uid, norm_class)
            nav_set(uid, "main")
            await safe_reply(
                update,
                f"✅ Класс *{norm_class}* сохранён! (Ученик / Родитель)\n"
                "🔔 Уведомления о заменах включены.\n\nВыберите действие на кнопках под полем ввода 👇"
            )
        else:
            msg = await pcall(parser.format_day_combined_for_telegram, norm_class, target_date=date.today())
            nav_set(uid, "main")
            await safe_reply(update, msg)
        return True

    teachers = await pcall(parser.get_teachers)
    matched = raw if raw in teachers else await pcall(parser.normalize_teacher_name, raw)
    if matched:
        mode = st.get("mode", "view") if state in ("t_letters", "t_list") else "view"
        if mode == "self":
            user_prefs.set_teacher(uid, matched)
            nav_set(uid, "main")
            await safe_reply(
                update,
                f"✅ Преподаватель *{matched}* сохранён!\n"
                "🔔 Вы будете получать уведомления о заменах.\n\nВыберите действие на кнопках под полем ввода 👇"
            )
        else:
            nav_set(uid, "view_teacher", name=matched)
            msg = await pcall(parser.format_teacher_day_combined_for_telegram, matched, target_date=date.today())
            await safe_reply(update, msg)
        return True

    # ---------- Не распознано ----------
    await safe_reply(
        update,
        "🤔 Не понял сообщение.\n\n"
        "Пользуйтесь кнопками под полем ввода или напишите:\n"
        "• класс (например, `10A`)\n"
        "• фамилию учителя (например, `Кулакова` или `Ahv`)"
    )
    return True


ALL_BUTTON_LABELS = {
    B_MENU, B_BACK, B_CANCEL, B_TODAY, B_TOMORROW, B_WEEK, B_SUBST, B_ALLSUBST, B_TEACHERS, B_FREE,
    B_CLASS, B_PROFILE, B_ADMIN, B_T_TODAY, B_T_TOMORROW, B_T_WEEK, B_T_SUBST, B_T_ROOMS, B_T_WHERE,
    B_ROLE_STUDENT, B_ROLE_TEACHER, V_TODAY, V_TOMORROW, V_WEEK, V_SUBST, V_ROOMS, V_WHERE, V_OTHER,
    P_SUBGROUPS, P_CHANGE_CLASS, P_CHANGE_TEACHER, P_NOTIF_ON, P_NOTIF_OFF,
    A_STATS, A_REFRESH, A_BROADCAST, A_ADMINS, A_ADD, A_DEL, A_LIST, A_SEND,
} | set(GROUP_LABELS) | set(LETTER_LABELS)


async def reset_nav_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Любая /команда в личном чате возвращает меню в исходное состояние"""
    chat = update.effective_chat
    if chat and chat.type == "private" and update.effective_user:
        nav_set(update.effective_user.id, "main")
        context.user_data.pop("admin_state", None)


# ================= Обработка текстовых сообщений =================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста: административные состояния, поиск класса или учителя, анти-спам в беседах"""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    chat = update.effective_chat
    # Кнопки отмены/меню прерывают режимы ввода администратора
    if chat.type == "private" and text in (B_MENU, B_CANCEL, B_BACK, B_ADMIN):
        context.user_data.pop("admin_state", None)
        if text != B_ADMIN:
            context.user_data.pop("pending_broadcast_text", None)
    admin_state = context.user_data.get("admin_state")

    # В групповых чатах бот не спамит на обычные разговоры
    if chat.type in ("group", "supergroup"):
        bot_username = (context.bot.username or "").lower()
        text_lower = text.lower()

        is_reply_to_bot = bool(
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.is_bot
            and (update.message.reply_to_message.from_user.username or "").lower() == bot_username
        )
        is_mentioned = bool(bot_username and f"@{bot_username}" in text_lower)
        starts_with_bot = text_lower.startswith("бот ") or text_lower == "бот"

        if not (is_reply_to_bot or is_mentioned or starts_with_bot):
            return

        if bot_username:
            text = re.sub(rf"@{bot_username}\b", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"^бот\b\s*", "", text, flags=re.IGNORECASE).strip()

        if not text:
            bound_class = chat_prefs.get_class(chat.id)
            if bound_class:
                text_resp = (
                    f"🤖 *Школьный бот MJG на связи!*\n\n"
                    f"🏫 Привязанный класс беседы: *{bound_class}*\n"
                    "Выберите нужное действие на кнопках ниже:"
                )
                await safe_reply(update, text_resp, reply_markup=get_group_chat_keyboard(chat.id))
            else:
                text_resp = (
                    "👋 *Приветствую!*\n\n"
                    "Чтобы привязать класс к этой беседе, напишите команду:\n"
                    "`/setclass <класс>` (например, `/setclass 10A`)"
                )
                await safe_reply(update, text_resp)
            return

    # 1. Если админ вводит текст для рассылки
    if admin_state == "WAITING_BROADCAST_TEXT" and admin_manager.is_admin(user_id):
        context.user_data["pending_broadcast_text"] = text
        context.user_data.pop("admin_state", None)
        nav_set(user_id, "broadcast_confirm")

        all_users = user_prefs.get_all_users()
        preview_msg = (
            "📢 *Предпросмотр сообщения для рассылки:*\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{text}\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👥 Получателей: *{len(all_users)}* пользователей.\n\n"
            "Вы действительно хотите отправить это сообщение всем?"
        )
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить и отправить", callback_data="admin_broadcast_confirm")],
            [InlineKeyboardButton("❌ Отмена", callback_data="admin_panel")]
        ])
        await safe_reply(update, preview_msg, reply_markup=confirm_kb)
        return

    # 2. Если админ вводит ID для добавления нового администратора
    if admin_state == "WAITING_ADD_ADMIN_ID" and admin_manager.is_admin(user_id):
        context.user_data.pop("admin_state", None)
        nav_set(user_id, "admin_admins")
        if text.isdigit():
            new_aid = int(text)
            if admin_manager.add_admin(new_aid):
                resp = f"✅ Пользователь с ID *{new_aid}* успешно добавлен в список администраторов!"
            else:
                resp = f"ℹ️ Пользователь с ID *{new_aid}* уже является администратором."
        else:
            resp = "❌ Неверный формат ID. Требуется число (например, `1637426603`)."

        await safe_reply(update, resp, reply_markup=get_admin_manage_keyboard())
        return

    # 2.1 Удаление администратора по ID
    if admin_state == "WAITING_DEL_ADMIN_ID" and admin_manager.is_admin(user_id):
        context.user_data.pop("admin_state", None)
        nav_set(user_id, "admin_admins")
        if text.isdigit():
            if admin_manager.remove_admin(int(text)):
                resp = f"✅ Администратор *{text}* удалён."
            else:
                resp = "⚠️ Не удалось удалить (такого ID нет или это последний администратор)."
        else:
            resp = "❌ Неверный формат ID. Требуется число."
        await safe_reply(update, resp)
        return

    # 2.2 Личный чат: кнопки возле клавиатуры и ввод класса/учителя
    if chat.type == "private":
        await private_router(update, context)
        return

    # 3. Быстрая обработка текстовых кнопок и команд на русском языке (группы)
    clean_lower = text.lower().strip()
    clean_no_emoji = re.sub(r'^[^\w\s]+', '', clean_lower).strip()

    if clean_lower in ("сегодня", "📅 сегодня", "today") or clean_no_emoji == "сегодня":
        await cmd_today(update, context)
        return
    if clean_lower in ("завтра", "📆 завтра", "tomorrow") or clean_no_emoji == "завтра":
        await cmd_tomorrow(update, context)
        return
    if clean_lower in ("расписание", "🗓 расписание", "schedule", "вся неделя", "неделя") or clean_no_emoji in ("расписание", "вся неделя", "неделя"):
        await cmd_schedule(update, context)
        return
    if clean_lower in ("замены", "🔄 замены", "substitutions", "zameni") or clean_no_emoji == "замены":
        await cmd_substitutions(update, context)
        return
    if clean_lower in ("учителя", "👩‍🏫 учителя", "teachers") or clean_no_emoji == "учителя":
        await cmd_teachers(update, context)
        return
    if clean_lower in ("кабинеты", "🚪 кабинеты", "свободные кабинеты", "rooms", "freerooms") or clean_no_emoji in ("кабинеты", "свободные кабинеты"):
        await cmd_freerooms(update, context)
        return
    if clean_lower in ("меню", "🏠 меню", "главное меню", "menu", "старт", "start") or clean_no_emoji in ("меню", "главное меню", "старт"):
        await cmd_start(update, context)
        return
    if clean_lower in ("профиль", "⚙️ профиль", "настройки", "мои настройки", "profile", "settings") or clean_no_emoji in ("профиль", "настройки"):
        await cmd_profile(update, context)
        return
    if clean_lower in ("помощь", "❓ помощь", "help") or clean_no_emoji == "помощь":
        await cmd_help(update, context)
        return
    if (clean_lower in ("админ", "👑 админ", "админка", "admin") or clean_no_emoji in ("админ", "админка")) and admin_manager.is_admin(user_id):
        await cmd_admin(update, context)
        return

    # 4. Проверяем, ввел ли пользователь класс (например "10A", "8k", "1-a")
    norm_class = parser.normalize_class_name(text)
    available_classes = (await pcall(parser.get_classes))

    if norm_class in available_classes:
        user_prefs.set_student(user_id, norm_class)
        resp_text = (
            f"🏫 Выбран класс: *{norm_class}* (Ученик / Родитель)!\n"
            f"🔔 Уведомления о заменах для этого класса включены.\n\n"
            f"Выберите действие ниже:"
        )
        await safe_reply(update, resp_text, reply_markup=get_class_action_keyboard(norm_class))
        return

    # 5. Проверяем, ввел ли пользователь фамилию преподавателя
    matched_teacher = (await pcall(parser.normalize_teacher_name, text))
    if matched_teacher:
        # Просмотр расписания учителя (профиль не меняется)
        msg = (await pcall(parser.format_teacher_day_combined_for_telegram, matched_teacher, target_date=date.today()))
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(matched_teacher, is_own_profile=False))
        return

    # 5. Если текст не распознан -> главное меню
    resp_text = (
        "🤖 Я получил ваше сообщение.\n\n"
        "Чтобы посмотреть расписание или замены:\n"
        "• Напишите класс (например, `10A` или `8K`)\n"
        "• Или фамилию учителя (например, `Кулакова` или `Ahv`)\n"
        "• Или воспользуйтесь кнопками меню ниже 👇"
    )
    await safe_reply(update, resp_text, reply_markup=get_main_menu_keyboard(user_id))


# ================= Фоновая периодическая проверка замен =================

async def check_new_substitutions_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Фоновая задача: опрашивает замены уроков и рассылает уведомления подписанным пользователям.
    """
    try:
        today_str = date.today().strftime("%Y-%m-%d")
        dates_to_check = [today_str]

        # Вечером после 14:00 также проверяем следующий рабочий день
        if datetime.now().hour >= 14:
            next_day_str = get_target_date_for_tomorrow().strftime("%Y-%m-%d")
            if next_day_str not in dates_to_check:
                dates_to_check.append(next_day_str)

        new_items_by_date: Dict[str, List[Dict[str, Any]]] = {}

        for d_str in dates_to_check:
            res = await asyncio.to_thread(parser.get_substitutions, date_str=d_str, force_refresh=True)
            all_classes = res.get("classes", {})

            for c_name, items in all_classes.items():
                for item in items:
                    sig = seen_tracker.make_signature(d_str, c_name, item)
                    if not seen_tracker.is_seen(sig):
                        seen_tracker.mark_seen(sig)
                        item_copy = dict(item)
                        item_copy["class_name"] = c_name
                        new_items_by_date.setdefault(d_str, []).append(item_copy)

        seen_tracker.save()

        # Первый запуск трекера — только инициализация, если не был инициализирован ранее
        if not seen_tracker.initialized:
            seen_tracker.initialized = True
            logger.info("Инициализация списка замен завершена, ожидание новых публикаций.")
            return

        total_new = sum(len(v) for v in new_items_by_date.values())
        if total_new == 0:
            return

        logger.info(f"Обнаружено {total_new} новых замен уроков! Начинаем рассылку подписчикам...")

        # 1. Рассылка пользователям (ученикам и учителям)
        all_users = user_prefs.get_all_users()
        for uid_str, u_pref in all_users.items():
            if not u_pref.get("is_active", True) or not u_pref.get("notifications", True):
                continue

            try:
                user_id = int(uid_str)
            except ValueError:
                continue

            role = u_pref.get("role")
            if not role:
                if u_pref.get("teacher_name"):
                    role = "teacher"
                else:
                    role = "student"

            # 1.1 Уведомление для Ученика
            if role == "student":
                u_class = u_pref.get("class_name")
                if not u_class:
                    continue

                for d_str, items in new_items_by_date.items():
                    class_items = [it for it in items if it["class_name"] == u_class or it["class_name"] == "Все"]
                    if class_items:
                        lines = [
                            "🔔 *Внимание! Новые замены уроков!*",
                            f"🏫 Класс: *{u_class}*",
                            f"📅 Дата: *{d_str}*\n"
                        ]
                        for it in class_items:
                            icon = "❌" if it["type"] == "remove" else "⚠️" if it["type"] == "absent" else "🔄"
                            p_txt = f"{it['period']} урок: " if it.get("period") else ""
                            lines.append(f"{icon} *{p_txt}*{it['info']}")

                        lines.append("\n_Отправьте /today или нажмите кнопки в меню для подробностей._")
                        msg = "\n".join(lines)
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=msg,
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Forbidden:
                            user_prefs.set_user_inactive(user_id, reason="blocked")
                        except TelegramError as e:
                            logger.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

            # 1.2 Уведомление для Учителя
            elif role == "teacher":
                u_teacher = u_pref.get("teacher_name")
                if not u_teacher:
                    continue

                search_terms = [u_teacher.lower()]
                parts = u_teacher.split()
                if len(parts) >= 2:
                    search_terms.append(parts[0].lower())

                for d_str, items in new_items_by_date.items():
                    teacher_items = []
                    for it in items:
                        info_l = it.get("info", "").lower()
                        if any(term in info_l for term in search_terms):
                            teacher_items.append(it)

                    if teacher_items:
                        lines = [
                            "🔔 *Внимание! Новые изменения в ваших уроках!*",
                            f"👩‍🏫 Преподаватель: *{u_teacher}*",
                            f"📅 Дата: *{d_str}*\n"
                        ]
                        for it in teacher_items:
                            icon = "❌" if it["type"] == "remove" else "⚠️" if it["type"] == "absent" else "🔄"
                            p_txt = f"{it['period']} урок" if it.get("period") else "Весь день"
                            lines.append(f"🏫 Класс *{it['class_name']}* ({p_txt}):")
                            lines.append(f"  {icon} {it['info']}\n")

                        lines.append("_Отправьте /today или нажмите кнопки в меню для подробностей._")
                        msg = "\n".join(lines).strip()
                        try:
                            await context.bot.send_message(
                                chat_id=user_id,
                                text=msg,
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Forbidden:
                            user_prefs.set_user_inactive(user_id, reason="blocked")
                        except TelegramError as e:
                            logger.warning(f"Не удалось отправить уведомление учителю {user_id}: {e}")

        # 2. Рассылка по групповым чатам классов
        all_chats = chat_prefs.get_all_chats()
        for cid_str, c_pref in all_chats.items():
            if not c_pref.get("notifications", True):
                continue
            try:
                group_chat_id = int(cid_str)
            except ValueError:
                continue

            bound_class = c_pref.get("class_name")
            if not bound_class:
                continue

            for d_str, items in new_items_by_date.items():
                group_items = [it for it in items if it["class_name"] == bound_class or it["class_name"] == "Все"]
                if group_items:
                    lines = [
                        "🔔 *Внимание! Новые замены уроков!*",
                        f"🏫 Беседа класса: *{bound_class}*",
                        f"📅 Дата: *{d_str}*\n"
                    ]
                    for it in group_items:
                        icon = "❌" if it["type"] == "remove" else "⚠️" if it["type"] == "absent" else "🔄"
                        p_txt = f"{it['period']} урок: " if it.get("period") else ""
                        lines.append(f"{icon} *{p_txt}*{it['info']}")

                    lines.append("\n_Нажмите кнопки ниже для просмотра расписания:_")
                    msg = "\n".join(lines)
                    try:
                        await context.bot.send_message(
                            chat_id=group_chat_id,
                            text=msg,
                            parse_mode=ParseMode.MARKDOWN,
                            reply_markup=get_group_chat_keyboard(group_chat_id)
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить уведомление в группу {group_chat_id}: {e}")

    except Exception as e:
        logger.error(f"Ошибка в фоновой проверке замен: {e}", exc_info=True)


# ================= Глобальный обработчик ошибок =================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Глобальный обработчик исключений: логирует ошибку и предотвращает
    зависание индикатора загрузки на кнопках Telegram.
    """
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "⛔ Conflict: этот токен уже используется другим запущенным экземпляром бота "
            "(локально, на хостинге или в другом контейнере). Остановите лишние копии — "
            "иначе кнопки и команды будут срабатывать случайно."
        )
        return
    if isinstance(err, (NetworkError, TimedOut)):
        logger.warning(f"Сетевая ошибка Telegram (будет повтор): {err}")
        return

    logger.error("Необработанное исключение в обработчике Telegram:", exc_info=err)

    if isinstance(update, Update):
        if update.effective_chat:
            try:
                await update.effective_chat.send_message(
                    "⚠️ Произошла временная ошибка при обработке команды. Отправьте /start для перезапуска меню."
                )
            except Exception:
                pass


# ================= Главная функция запуска бота =================

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ":
        print("\n" + "!" * 60)
        print("ВНИМАНИЕ: Не задан корректный TELEGRAM_BOT_TOKEN в файле .env!")
        print("-" * 60)
        print(get_env_diagnostic_info())
        print("!" * 60 + "\n")
        return

    logger.info("Запуск Telegram бота Tallinna Mustjõe Gümnaasium...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация глобального перехватчика ошибок
    app.add_error_handler(error_handler)

    # Любая команда в личном чате сбрасывает меню в главное состояние (выполняется до самой команды)
    app.add_handler(MessageHandler(filters.COMMAND, reset_nav_on_command), group=-1)

    # Регистрация пользовательских команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["profile", "role", "settings"], cmd_profile))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("classes", cmd_classes))
    app.add_handler(CommandHandler("teachers", cmd_teachers))
    app.add_handler(CommandHandler("teacher", cmd_teacher))
    app.add_handler(CommandHandler("setclass", cmd_setclass))
    app.add_handler(CommandHandler("setteacher", cmd_setteacher))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler(["substitutions", "replacements", "zameni"], cmd_substitutions))
    app.add_handler(CommandHandler(["bot", "call", "mjg", "parcer", "raspisanie"], cmd_bot))
    app.add_handler(CommandHandler("room", cmd_room))
    app.add_handler(CommandHandler("rooms", cmd_rooms))
    app.add_handler(CommandHandler(["freerooms", "free"], cmd_freerooms))
    app.add_handler(CommandHandler(["whereis", "gde"], cmd_whereis))
    app.add_handler(CommandHandler(["search", "find", "poisk"], cmd_search))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler(["stop", "leave"], cmd_stop))

    # Регистрация команд администратора
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CommandHandler("admins", cmd_admins))
    app.add_handler(CommandHandler("setusers", cmd_setusers))

    # Регистрация callback queries
    app.add_handler(CallbackQueryHandler(handle_callback_query))

    # Регистрация текстовых сообщений
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Запуск фонового планировщика для проверки новых замен
    if app.job_queue:
        logger.info(f"Фоновый мониторинг замен активирован (интервал: {SUBSTITUTION_CHECK_INTERVAL} сек).")
        app.job_queue.run_repeating(
            check_new_substitutions_job,
            interval=SUBSTITUTION_CHECK_INTERVAL,
            first=15
        )
    else:
        logger.warning("JobQueue недоступна, фоновый опрос отключен.")

    # Запуск polling
    logger.info("Бот готов к приёму сообщений!")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()