import os
import sys
import json
import logging
import asyncio
from datetime import date, datetime, timedelta
from typing import Optional, Dict, Any, List

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, Forbidden
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
    SEEN_SUBSTITUTIONS_FILE,
    ADMINS_FILE,
    DEFAULT_ADMIN_IDS,
    SUBSTITUTION_CHECK_INTERVAL,
    WEEKDAYS_RU,
    WEEKDAYS_SHORT_RU
)
from parser import MJGParser, format_group_label

# Настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Инициализация парсера
parser = MJGParser()


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

    def __init__(self, filepath=USER_DATA_FILE):
        self.filepath = filepath
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except Exception as e:
                logger.error(f"Ошибка чтения настроек пользователей: {e}")
                self._data = {}

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек пользователей: {e}")

    def get_user(self, user_id: int) -> Dict[str, Any]:
        return self._data.get(str(user_id), {})

    def has_role(self, user_id: int) -> bool:
        """Проверяет, выбрал ли пользователь уже роль и сущность"""
        user = self.get_user(user_id)
        role = user.get("role")
        if role == "teacher" and user.get("teacher_name"):
            return True
        if (role == "student" or not role) and user.get("class_name"):
            return True
        return False

    def get_role(self, user_id: int) -> Optional[str]:
        """Возвращает 'teacher', 'student' или None"""
        user = self.get_user(user_id)
        role = user.get("role")
        if not role:
            if user.get("teacher_name"):
                return "teacher"
            if user.get("class_name"):
                return "student"
        return role

    def get_class(self, user_id: int) -> Optional[str]:
        return self.get_user(user_id).get("class_name")

    def get_teacher(self, user_id: int) -> Optional[str]:
        return self.get_user(user_id).get("teacher_name")

    def set_student(self, user_id: int, class_name: str):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["role"] = "student"
        self._data[uid]["class_name"] = class_name
        self._data[uid].pop("teacher_name", None)
        if "notifications" not in self._data[uid]:
            self._data[uid]["notifications"] = True
        self._save()

    def set_teacher(self, user_id: int, teacher_name: str):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["role"] = "teacher"
        self._data[uid]["teacher_name"] = teacher_name
        self._data[uid].pop("class_name", None)
        if "notifications" not in self._data[uid]:
            self._data[uid]["notifications"] = True
        self._save()

    def reset_role(self, user_id: int):
        uid = str(user_id)
        if uid in self._data:
            self._data[uid].pop("role", None)
            self._data[uid].pop("class_name", None)
            self._data[uid].pop("teacher_name", None)
            self._save()

    def is_notifications_enabled(self, user_id: int) -> bool:
        return self.get_user(user_id).get("notifications", True)

    def set_notifications(self, user_id: int, enabled: bool):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        self._data[uid]["notifications"] = enabled
        self._save()

    def toggle_notifications(self, user_id: int) -> bool:
        current = self.is_notifications_enabled(user_id)
        new_val = not current
        self.set_notifications(user_id, new_val)
        return new_val

    # Настройки подгрупп
    def get_subgroup_filter(self, user_id: int) -> Dict[str, str]:
        user = self.get_user(user_id)
        filt = {}
        if user.get("subgroup_gender"):
            filt["gender"] = user["subgroup_gender"]
        if user.get("subgroup_academic"):
            filt["academic"] = user["subgroup_academic"]
        return filt

    def set_gender_filter(self, user_id: int, gender: Optional[str]):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        if gender in ("boys", "girls"):
            self._data[uid]["subgroup_gender"] = gender
        else:
            self._data[uid].pop("subgroup_gender", None)
        self._save()

    def set_academic_filter(self, user_id: int, academic: Optional[str]):
        uid = str(user_id)
        if uid not in self._data:
            self._data[uid] = {}
        if academic in ("1", "2"):
            self._data[uid]["subgroup_academic"] = academic
        else:
            self._data[uid].pop("subgroup_academic", None)
        self._save()

    def get_all_users(self) -> Dict[str, Dict[str, Any]]:
        return self._data


user_prefs = UserPreferences()


# ================= Отслеживание уже отправленных замен =================

class SeenSubstitutionsTracker:
    """Хранит сигнатуры отправленных замен, чтобы не присылать повторные уведомления"""

    def __init__(self, filepath=SEEN_SUBSTITUTIONS_FILE):
        self.filepath = filepath
        self.seen_signatures: set = set()
        self._load()

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
                InlineKeyboardButton("👩‍🏫 Сменить учителя", callback_data="btn_choose_teacher_letters"),
                InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("📅 Уроки сегодня", callback_data=f"tview_today_{teacher_name}"),
                InlineKeyboardButton("📆 Завтра", callback_data=f"tview_tomorrow_{teacher_name}"),
            ],
            [
                InlineKeyboardButton("🗓 Вся неделя", callback_data=f"tview_week_{teacher_name}"),
                InlineKeyboardButton("🔄 Замены", callback_data=f"tview_subst_{teacher_name}"),
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
    Безопасная отправка ответа с авто-разбивкой длинных сообщений (>3900 симв.)
    и fallback на обычный текст при ошибках Markdown.
    """
    target = update.callback_query.message if update.callback_query else update.message
    if not target:
        return

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

        try:
            if update.callback_query and i == 0:
                await target.edit_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
            else:
                await target.reply_text(chunk, parse_mode=ParseMode.MARKDOWN, reply_markup=mk)
        except Exception as e:
            if "Message is not modified" in str(e):
                return
            logger.warning(f"Ошибка Markdown: {e}. Отправка в обычном тексте.")
            try:
                if update.callback_query and i == 0:
                    await target.edit_text(chunk, reply_markup=mk)
                else:
                    await target.reply_text(chunk, reply_markup=mk)
            except Exception as e2:
                if "Message is not modified" in str(e2):
                    return
                logger.error(f"Не удалось отправить сообщение: {e2}")


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
    classes = parser.get_classes()
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
    teachers = parser.get_teachers()
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
        text = "👩‍🏫 *Выберите преподавателя для просмотра расписания уроков, классов и кабинетов:*"
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard("view_tgrp_"))
        return

    query = " ".join(context.args)
    found_teacher = parser.normalize_teacher_name(query)

    if not found_teacher:
        await safe_reply(
            update,
            f"❌ Преподаватель по запросу *{query}* не найден.\nИспользуйте /teachers для списка всех учителей."
        )
        return

    msg = parser.format_teacher_day_combined_for_telegram(found_teacher, target_date=date.today())
    await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(found_teacher, is_own_profile=False))


async def cmd_setclass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setclass <класс>"""
    user_id = update.effective_user.id
    if not context.args:
        await safe_reply(update, "⚠️ Пожалуйста, укажите класс, например: `/setclass 10A`")
        return

    raw_class = context.args[0]
    norm_class = parser.normalize_class_name(raw_class)
    available_classes = parser.get_classes()

    if norm_class not in available_classes:
        await safe_reply(update, f"❌ Класс *{norm_class}* не найден.\nИспользуйте /classes для списка доступных классов.")
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
    found_teacher = parser.normalize_teacher_name(query)

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
    role = user_prefs.get_role(user_id)

    # Если передан аргумент: может быть класс или учитель
    if context.args:
        arg = " ".join(context.args)
        # Проверяем на учителя
        t_match = parser.normalize_teacher_name(arg)
        if t_match:
            msg = parser.format_teacher_day_combined_for_telegram(t_match, target_date=date.today())
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return

        norm_class = parser.normalize_class_name(arg)
        subgroup_filter = user_prefs.get_subgroup_filter(user_id)
        msg = parser.format_day_combined_for_telegram(norm_class, target_date=date.today(), subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    # Без аргументов: смотрим сохраненную роль
    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_day_combined_for_telegram(teacher_name, target_date=date.today())
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
    msg = parser.format_day_combined_for_telegram(norm_class, target_date=date.today(), subgroup_filter=subgroup_filter)
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tomorrow [класс/учитель]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)
    tomorrow_dt = get_target_date_for_tomorrow()

    if context.args:
        arg = " ".join(context.args)
        t_match = parser.normalize_teacher_name(arg)
        if t_match:
            msg = parser.format_teacher_day_combined_for_telegram(t_match, target_date=tomorrow_dt)
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_match, is_own_profile=False))
            return

        norm_class = parser.normalize_class_name(arg)
        subgroup_filter = user_prefs.get_subgroup_filter(user_id)
        msg = parser.format_day_combined_for_telegram(norm_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))
        return

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_day_combined_for_telegram(teacher_name, target_date=tomorrow_dt)
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
    msg = parser.format_day_combined_for_telegram(norm_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter)
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /schedule [класс]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)

    if not context.args and role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_schedule_for_telegram(teacher_name)
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = context.args[0] if context.args else user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите класс из списка ниже:",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)
    msg = parser.format_schedule_for_telegram(norm_class, subgroup_filter=subgroup_filter)
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_substitutions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /substitutions [класс]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)
    date_str = date.today().strftime("%Y-%m-%d")

    if not context.args and role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_substitutions_for_telegram(teacher_name, date_str=date_str)
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name, is_own_profile=True))
            return

    class_name = context.args[0] if context.args else user_prefs.get_class(user_id)
    msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=class_name)
    keyboard = get_main_menu_keyboard(user_id)
    await safe_reply(update, msg, reply_markup=keyboard)


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


# ================= Обработчик нажатий кнопок (Callback Query) =================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка всех inline-кнопок"""
    query = update.callback_query
    await query.answer()

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

    # 6. Выбор класса
    if data == "btn_choose_grade_group":
        text = "🎒 *Выберите группу классов:*"
        await safe_reply(update, text, reply_markup=get_grade_groups_keyboard())
        return

    if data.startswith("grp_"):
        group_name = data.replace("grp_", "")
        text = "🏫 *Выберите ваш класс:*"
        await safe_reply(update, text, reply_markup=get_classes_keyboard(group_name))
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
        msg = parser.format_teacher_day_combined_for_telegram(t_name, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_today_"):
        t_name = data.replace("tview_today_", "")
        msg = parser.format_teacher_day_combined_for_telegram(t_name, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_tomorrow_"):
        t_name = data.replace("tview_tomorrow_", "")
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_teacher_day_combined_for_telegram(t_name, target_date=tomorrow_dt)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_week_"):
        t_name = data.replace("tview_week_", "")
        msg = parser.format_teacher_schedule_for_telegram(t_name)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    if data.startswith("tview_subst_"):
        t_name = data.replace("tview_subst_", "")
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_teacher_substitutions_for_telegram(t_name, date_str=date_str)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(t_name, is_own_profile=False))
        return

    # 9. Действия учителя (свой профиль)
    if data == "btn_teacher_today":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        msg = parser.format_teacher_day_combined_for_telegram(user_teacher, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_tomorrow":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_teacher_day_combined_for_telegram(user_teacher, target_date=tomorrow_dt)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_week":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        msg = parser.format_teacher_schedule_for_telegram(user_teacher)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    if data == "btn_teacher_subst":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard("tgrp_"))
            return
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_teacher_substitutions_for_telegram(user_teacher, date_str=date_str)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher, is_own_profile=True))
        return

    # 10. Действия ученика
    subgroup_filter = user_prefs.get_subgroup_filter(user_id)

    if data == "btn_today":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = parser.format_day_combined_for_telegram(user_class, target_date=date.today(), subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_tomorrow":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_day_combined_for_telegram(user_class, target_date=tomorrow_dt, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_week":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = parser.format_schedule_for_telegram(user_class, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=user_class)
        kb = get_class_action_keyboard(user_class) if user_class else get_main_menu_keyboard(user_id)
        await safe_reply(update, msg, reply_markup=kb)
        return

    if data == "btn_all_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]])
        await safe_reply(update, msg, reply_markup=kb)
        return

    # 11. Действия для конкретного класса
    if data.startswith("today_"):
        c_name = data.replace("today_", "")
        msg = parser.format_day_combined_for_telegram(c_name, target_date=date.today(), subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("tomorrow_"):
        c_name = data.replace("tomorrow_", "")
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_day_combined_for_telegram(c_name, target_date=tomorrow_dt, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("week_"):
        c_name = data.replace("week_", "")
        msg = parser.format_schedule_for_telegram(c_name, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("subst_"):
        c_name = data.replace("subst_", "")
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=c_name)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("sched_"):
        parts = data.split("_")
        c_name = parts[1]
        day_idx = int(parts[2])
        msg = parser.format_schedule_for_telegram(c_name, day_index=day_idx, subgroup_filter=subgroup_filter)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    # ================= Панель Администратора =================

    if not admin_manager.is_admin(user_id):
        return  # Не админ

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

        all_users = list(user_prefs.get_all_users().keys())
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
            except TelegramError as te:
                # Попытка отправки без Markdown при ошибке парсинга
                try:
                    await context.bot.send_message(
                        chat_id=int(uid_str),
                        text=f"📢 Объявление от администрации MJG:\n\n{broadcast_text}"
                    )
                    success_count += 1
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

        classes = parser.get_classes()
        teachers = parser.get_teachers()

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
            parser.get_regular_timetable(force_refresh=True)
            parser.get_substitutions(force_refresh=True)
            text = "✅ *Кэш расписания уроков и замен успешно обновлён онлайн с EduPage!*"
        except Exception as e:
            text = f"⚠️ Ошибка при обновлении кэша: {e}"
        await safe_reply(update, text, reply_markup=get_admin_main_keyboard())
        return


# ================= Обработка текстовых сообщений =================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста: административные состояния, поиск класса или учителя"""
    text = update.message.text.strip()
    user_id = update.effective_user.id
    admin_state = context.user_data.get("admin_state")

    # 1. Если админ вводит текст для рассылки
    if admin_state == "WAITING_BROADCAST_TEXT" and admin_manager.is_admin(user_id):
        context.user_data["pending_broadcast_text"] = text
        context.user_data.pop("admin_state", None)

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

    # 3. Проверяем, ввел ли пользователь класс (например "10A", "8k", "1-a")
    norm_class = parser.normalize_class_name(text)
    available_classes = parser.get_classes()

    if norm_class in available_classes:
        user_prefs.set_student(user_id, norm_class)
        resp_text = (
            f"🏫 Выбран класс: *{norm_class}* (Ученик / Родитель)!\n"
            f"🔔 Уведомления о заменах для этого класса включены.\n\n"
            f"Выберите действие ниже:"
        )
        await safe_reply(update, resp_text, reply_markup=get_class_action_keyboard(norm_class))
        return

    # 4. Проверяем, ввел ли пользователь фамилию преподавателя
    matched_teacher = parser.normalize_teacher_name(text)
    if matched_teacher:
        role = user_prefs.get_role(user_id)
        if role == "teacher":
            user_prefs.set_teacher(user_id, matched_teacher)
            resp_text = (
                f"👩‍🏫 Преподаватель сохранён: *{matched_teacher}*!\n"
                f"Роль: *Преподаватель*\n"
                f"🔔 Вы будете получать уведомления при появлении замен для вас.\n\n"
                f"Выберите действие ниже:"
            )
            await safe_reply(update, resp_text, reply_markup=get_teacher_action_keyboard(matched_teacher, is_own_profile=True))
        else:
            # Ученик или гость ищет расписание учителя
            msg = parser.format_teacher_day_combined_for_telegram(matched_teacher, target_date=date.today())
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
            res = parser.get_substitutions(date_str=d_str, force_refresh=True)
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

        # Первый запуск трекера — только инициализация
        if len(seen_tracker.seen_signatures) <= sum(len(v) for v in new_items_by_date.values()):
            logger.info("Инициализация списка замен завершена, ожидание новых публикаций.")
            return

        total_new = sum(len(v) for v in new_items_by_date.values())
        if total_new == 0:
            return

        logger.info(f"Обнаружено {total_new} новых замен уроков! Начинаем рассылку подписчикам...")

        all_users = user_prefs.get_all_users()
        for uid_str, u_pref in all_users.items():
            if not u_pref.get("notifications", True):
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

            # 1. Уведомление для Ученика
            if role == "student":
                u_class = u_pref.get("class_name")
                if not u_class:
                    continue

                for d_str, items in new_items_by_date.items():
                    class_items = [it for it in items if it["class_name"] == u_class or it["class_name"] == "Все"]
                    if class_items:
                        lines = [
                            f"🔔 *Внимание! Новые замены уроков!*",
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
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=get_class_action_keyboard(u_class)
                            )
                        except (Forbidden, TelegramError) as e:
                            logger.warning(f"Не удалось отправить уведомление пользователю {user_id}: {e}")

            # 2. Уведомление для Учителя
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
                            f"🔔 *Внимание! Новые изменения в ваших уроках!*",
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
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=get_teacher_action_keyboard(u_teacher, is_own_profile=True)
                            )
                        except (Forbidden, TelegramError) as e:
                            logger.warning(f"Не удалось отправить уведомление учителю {user_id}: {e}")

    except Exception as e:
        logger.error(f"Ошибка в фоновой проверке замен: {e}", exc_info=True)


# ================= Главная функция запуска бота =================

def main():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "ВАШ_ТОКЕН_БОТА_ЗДЕСЬ":
        print("\n" + "!" * 60)
        print("ВНИМАНИЕ: Не задан TELEGRAM_BOT_TOKEN в файле .env!")
        print("!" * 60 + "\n")
        return

    logger.info("Запуск Telegram бота Tallinna Mustjõe Gümnaasium...")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

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

    # Регистрация команд администратора
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CommandHandler("admins", cmd_admins))

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
    app.run_polling()


if __name__ == "__main__":
    main()
