import os
import sys
import json
import logging
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
    SUBSTITUTION_CHECK_INTERVAL,
    WEEKDAYS_RU,
    WEEKDAYS_SHORT_RU
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


# ================= Управление пользовательскими данными =================

class UserPreferences:
    """Управление сохранёнными настройками пользователей (роль: ученик/учитель, класс, уведомления)"""

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
        """Проверяет, выбрал ли пользователь уже роль и сущность (класс или себя как учителя)"""
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
    """Главное меню с кнопками, адаптированное под роль пользователя"""
    role = user_prefs.get_role(user_id)
    user_class = user_prefs.get_class(user_id)
    user_teacher = user_prefs.get_teacher(user_id)

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
                InlineKeyboardButton("🏫 Замены всей школы", callback_data="btn_all_subst"),
            ],
            [
                InlineKeyboardButton(f"👩‍🏫 Профиль: {user_teacher}", callback_data="btn_profile"),
            ]
        ]
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
        InlineKeyboardButton("⚙️ Мой профиль / Настройки", callback_data="btn_profile")
    ])

    return InlineKeyboardMarkup(buttons)


def get_profile_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Клавиатура управления профилем пользователя и сменой роли"""
    role = user_prefs.get_role(user_id)
    notif_enabled = user_prefs.is_notifications_enabled(user_id)
    notif_text = "🔔 Уведомления: ВКЛ" if notif_enabled else "🔕 Уведомления: ВЫКЛ"

    buttons = []
    if role == "teacher":
        buttons.append([InlineKeyboardButton("👩‍🏫 Сменить преподавателя", callback_data="btn_choose_teacher_letters")])
        buttons.append([InlineKeyboardButton("👨‍🎓 Сменить роль на «Ученик»", callback_data="role_choose_student")])
    else:
        buttons.append([InlineKeyboardButton("🏫 Сменить класс", callback_data="btn_choose_grade_group")])
        buttons.append([InlineKeyboardButton("👩‍🏫 Сменить роль на «Учитель»", callback_data="role_choose_teacher")])

    buttons.append([InlineKeyboardButton(notif_text, callback_data="btn_toggle_notifications")])
    buttons.append([InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")])

    return InlineKeyboardMarkup(buttons)


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


def get_teacher_letter_groups_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура алфавитных групп преподавателей"""
    keyboard = [
        [
            InlineKeyboardButton("A – D", callback_data="tgrp_A_D"),
            InlineKeyboardButton("E – J", callback_data="tgrp_E_J"),
        ],
        [
            InlineKeyboardButton("K", callback_data="tgrp_K"),
            InlineKeyboardButton("L – M", callback_data="tgrp_L_M"),
        ],
        [
            InlineKeyboardButton("N – R", callback_data="tgrp_N_R"),
            InlineKeyboardButton("S – T", callback_data="tgrp_S_T"),
        ],
        [
            InlineKeyboardButton("U – Ü, Ш", callback_data="tgrp_U_Z"),
        ],
        [
            InlineKeyboardButton("◀️ В главное меню", callback_data="btn_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_teachers_keyboard(group_code: str) -> InlineKeyboardMarkup:
    """Клавиатура выбора конкретного учителя в группе"""
    teachers = parser.get_teachers()

    # Фильтрация по первой букве фамилии
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
        # Компактная кнопка с фамилией и инициалом
        parts = t_name.split()
        short_btn = f"{parts[0]} {parts[1][0]}." if len(parts) >= 2 else t_name
        row.append(InlineKeyboardButton(short_btn, callback_data=f"set_teacher_{t_name}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton("◀️ Назад к группам букв", callback_data="btn_choose_teacher_letters")])
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
            InlineKeyboardButton("🏫 Сменить класс", callback_data="btn_choose_grade_group"),
            InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def get_teacher_action_keyboard(teacher_name: str) -> InlineKeyboardMarkup:
    """Клавиатура действий для преподавателя"""
    keyboard = [
        [
            InlineKeyboardButton("📅 Уроки сегодня", callback_data="btn_teacher_today"),
            InlineKeyboardButton("📆 Завтра", callback_data="btn_teacher_tomorrow"),
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
    return InlineKeyboardMarkup(keyboard)


# ================= Вспомогательные функции =================

async def safe_reply(update: Update, text: str, reply_markup=None):
    """
    Безопасная отправка ответа:
    1. Автоматически разбивает длинные сообщения (>4000 символов) на части.
    2. При ошибках Markdown автоматически делает fallback на обычный текст.
    """
    target = update.callback_query.message if update.callback_query else update.message
    if not target:
        return

    # Разбивка на части, если сообщение превышает лимит Telegram (4096 символов)
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
            logger.warning(f"Ошибка отправки Markdown: {e}. Пробуем обычный текст.")
            try:
                if update.callback_query and i == 0:
                    await target.edit_text(chunk, reply_markup=mk)
                else:
                    await target.reply_text(chunk, reply_markup=mk)
            except Exception as e2:
                logger.error(f"Не удалось отправить сообщение: {e2}")


def get_target_date_for_tomorrow() -> date:
    """Вычисляет дату следующего учебного дня (если пятница/выходной -> понедельник)"""
    today = date.today()
    weekday = today.weekday()  # 0=Пн .. 4=Пт, 5=Сб, 6=Вс
    if weekday == 4:  # Пятница -> Понедельник
        return today + timedelta(days=3)
    elif weekday == 5:  # Суббота -> Понедельник
        return today + timedelta(days=2)
    elif weekday == 6:  # Воскресенье -> Понедельник
        return today + timedelta(days=1)
    else:
        return today + timedelta(days=1)


# ================= Обработчики команд =================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start: если пользователь новый — предлагает выбрать роль (ученик/учитель)"""
    user = update.effective_user
    user_id = user.id

    if not user_prefs.has_role(user_id):
        # Первый раз пользуется ботом -> выбор роли
        welcome_text = (
            f"👋 Здравствуйте, *{user.first_name}*!\n\n"
            f"Добро пожаловать в официальный бот расписания и замен уроков "
            f"для *Tallinna Mustjõe Gümnaasium* (www.mjg.ee).\n\n"
            f"Пожалуйста, выберите, кто вы:\n"
            f"_(Вы всегда сможете изменить этот выбор позже в меню)_ 👇"
        )
        await safe_reply(update, welcome_text, reply_markup=get_role_selection_keyboard())
        return

    # Уже выбранная роль
    role = user_prefs.get_role(user_id)
    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        text = (
            f"👋 Здравствуйте, *{teacher_name}*!\n\n"
            f"Вы авторизованы как *Преподаватель* школы MJG.\n"
            f"Здесь вы можете смотреть свои уроки, замены и получать мгновенные уведомления о новых изменениях."
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

    if role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        text = (
            f"⚙️ *Настройки вашего профиля:*\n\n"
            f"• Роль: *👩‍🏫 Преподаватель*\n"
            f"• ФИО: *{teacher_name or 'Не выбрано'}*\n"
            f"• Уведомления о заменах: *{notif}*\n\n"
            f"Вы можете в любой момент сменить роль, выбрать другого преподавателя или настроить уведомления:"
        )
    else:
        user_class = user_prefs.get_class(user_id)
        text = (
            f"⚙️ *Настройки вашего профиля:*\n\n"
            f"• Роль: *👨‍🎓 Ученик / Родитель*\n"
            f"• Класс: *{user_class or 'Не выбран'}*\n"
            f"• Уведомления о заменах: *{notif}*\n\n"
            f"Вы можете в любой момент сменить класс, переключиться на учителя или настроить уведомления:"
        )

    await safe_reply(update, text, reply_markup=get_profile_keyboard(user_id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help: справка по командам"""
    text = (
        "📖 *Список доступных команд:*\n\n"
        "• /start — Главное меню с интерактивными кнопками\n"
        "• /profile или /role — Профиль, сменить роль (учитель/ученик) и уведомления\n"
        "• /today `[класс]` — Расписание и замены на сегодня\n"
        "• /tomorrow `[класс]` — Расписание и замены на следующий учебный день\n"
        "• /schedule `[класс]` — Базовое расписание уроков на неделю\n"
        "• /substitutions `[класс]` — Актуальные замены уроков из EduPage\n"
        "• /classes — Список всех классов школы (1A – 12C)\n"
        "• /teachers — Список всех преподавателей школы\n"
        "• /setclass `<класс>` — Сохранить свой класс (например, `/setclass 10A`)\n"
        "• /setteacher `<фамилия>` — Сохранить свой профиль учителя (например, `/setteacher Кулакова`)\n"
        "• /notify — Включить / выключить авто-уведомления о новых заменах\n"
        "• /help — Эта справка\n\n"
        "💡 _Совет: Вы можете управлять всем через кнопки в меню!_"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("◀️ В главное меню", callback_data="btn_main_menu")]
    ])
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_classes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /classes: список классов"""
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
    """Команда /teachers: список преподавателей"""
    teachers = parser.get_teachers()
    t_names = sorted(list(teachers.keys()))
    text = (
        f"👩‍🏫 *Список преподавателей школы MJG* ({len(t_names)}):\n\n"
        f"{', '.join(t_names[:30])}...\n\n"
        f"Чтобы выбрать себя: отправьте команду `/setteacher <Фамилия>` или выберите в меню."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👩‍🏫 Выбрать преподавателя", callback_data="btn_choose_teacher_letters")],
        [InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]
    ])
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_setclass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setclass <класс>"""
    user_id = update.effective_user.id
    if not context.args:
        await safe_reply(
            update,
            "⚠️ Пожалуйста, укажите класс, например: `/setclass 10A`"
        )
        return

    raw_class = context.args[0]
    norm_class = parser.normalize_class_name(raw_class)
    available_classes = parser.get_classes()

    if norm_class not in available_classes:
        await safe_reply(
            update,
            f"❌ Класс *{norm_class}* не найден.\nИспользуйте /classes для списка доступных классов."
        )
        return

    user_prefs.set_student(user_id, norm_class)
    text = (
        f"✅ Ваш класс успешно сохранён: *{norm_class}*!\n"
        f"Роль: *Ученик / Родитель*\n"
        f"🔔 Автоматические уведомления о новых заменах для вашего класса включены."
    )
    keyboard = get_class_action_keyboard(norm_class)
    await safe_reply(update, text, reply_markup=keyboard)


async def cmd_setteacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /setteacher <фамилия или имя>"""
    user_id = update.effective_user.id
    if not context.args:
        await safe_reply(
            update,
            "⚠️ Пожалуйста, укажите вашу фамилию, например: `/setteacher Кулакова` или `/setteacher Kulakova`"
        )
        return

    query = " ".join(context.args)
    found_teacher = parser.normalize_teacher_name(query)

    if not found_teacher:
        await safe_reply(
            update,
            f"❌ Преподаватель по запросу *{query}* не найден.\nИспользуйте /teachers для просмотра списка."
        )
        return

    user_prefs.set_teacher(user_id, found_teacher)
    text = (
        f"✅ Вы успешно зарегистрированы как преподаватель:\n*👩‍🏫 {found_teacher}*!\n\n"
        f"🔔 Теперь вы будете мгновенно получать уведомления в Telegram, "
        f"когда для вас появятся новые замены или отмены уроков."
    )
    keyboard = get_teacher_action_keyboard(found_teacher)
    await safe_reply(update, text, reply_markup=keyboard)


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
    """Команда /today [класс]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)

    # Если аргумент не указан и пользователь - учитель
    if not context.args and role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_day_combined_for_telegram(teacher_name, target_date=date.today())
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name))
            return

    class_name = context.args[0] if context.args else user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите его через меню или введите, например: `/today 10A`",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    msg = parser.format_day_combined_for_telegram(norm_class, target_date=date.today())
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /tomorrow [класс]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)
    tomorrow_dt = get_target_date_for_tomorrow()

    if not context.args and role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_day_combined_for_telegram(teacher_name, target_date=tomorrow_dt)
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name))
            return

    class_name = context.args[0] if context.args else user_prefs.get_class(user_id)
    if not class_name:
        await safe_reply(
            update,
            "⚠️ Класс не указан. Выберите его через меню или введите, например: `/tomorrow 10A`",
            reply_markup=get_grade_groups_keyboard()
        )
        return

    norm_class = parser.normalize_class_name(class_name)
    msg = parser.format_day_combined_for_telegram(norm_class, target_date=tomorrow_dt)
    await safe_reply(update, msg, reply_markup=get_class_action_keyboard(norm_class))


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /schedule [класс]"""
    user_id = update.effective_user.id
    role = user_prefs.get_role(user_id)

    if not context.args and role == "teacher":
        teacher_name = user_prefs.get_teacher(user_id)
        if teacher_name:
            msg = parser.format_teacher_schedule_for_telegram(teacher_name)
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name))
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
    msg = parser.format_schedule_for_telegram(norm_class)
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
            await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(teacher_name))
            return

    class_name = context.args[0] if context.args else user_prefs.get_class(user_id)
    msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=class_name)
    keyboard = get_main_menu_keyboard(user_id)
    await safe_reply(update, msg, reply_markup=keyboard)


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

    # 1. Возврат в главное меню
    if data == "btn_main_menu":
        greeting = (
            "🏠 *Главное меню Tallinna Mustjõe Gümnaasium:*\n\n"
            f"Текущая роль: *{'Преподаватель (' + (user_teacher or '') + ')' if role == 'teacher' else 'Класс: ' + (user_class or 'Не выбран')}*\n"
            "Выберите интересующий пункт ниже 👇"
        )
        await safe_reply(update, greeting, reply_markup=get_main_menu_keyboard(user_id))
        return

    # 2. Выбор роли (первый старт или смена)
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
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard())
        return

    # 3. Настройки и профиль
    if data == "btn_profile":
        notif = "ВКЛЮЧЕНЫ ✅" if user_prefs.is_notifications_enabled(user_id) else "ВЫКЛЮЧЕНЫ ❌"
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
                f"• Уведомления о заменах: *{notif}*\n\n"
                f"Что вы хотите изменить?"
            )
        await safe_reply(update, text, reply_markup=get_profile_keyboard(user_id))
        return

    # 4. Переключение уведомлений
    if data == "btn_toggle_notifications":
        new_state = user_prefs.toggle_notifications(user_id)
        status_txt = "включены 🔔" if new_state else "выключены 🔕"
        await safe_reply(
            update,
            f"✅ Уведомления о новых заменах теперь *{status_txt}*!",
            reply_markup=get_profile_keyboard(user_id)
        )
        return

    # 5. Группы классов и выбор класса
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

    # 6. Группы учителей и выбор учителя
    if data == "btn_choose_teacher_letters":
        text = "👩‍🏫 *Выберите первую букву фамилии преподавателя:*"
        await safe_reply(update, text, reply_markup=get_teacher_letter_groups_keyboard())
        return

    if data.startswith("tgrp_"):
        group_code = data.replace("tgrp_", "")
        text = "👩‍🏫 *Выберите себя из списка преподавателей:*"
        await safe_reply(update, text, reply_markup=get_teachers_keyboard(group_code))
        return

    if data.startswith("set_teacher_"):
        chosen_teacher = data.replace("set_teacher_", "")
        user_prefs.set_teacher(user_id, chosen_teacher)
        text = (
            f"✅ Преподаватель *{chosen_teacher}* успешно сохранён!\n"
            f"Роль: *Преподаватель*\n"
            f"🔔 Теперь вы будете получать уведомления при появлении замен для вас.\n\n"
            f"Что вы хотите посмотреть?"
        )
        await safe_reply(update, text, reply_markup=get_teacher_action_keyboard(chosen_teacher))
        return

    # 7. Действия учителя: сегодня, завтра, неделя, замены
    if data == "btn_teacher_today":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard())
            return
        msg = parser.format_teacher_day_combined_for_telegram(user_teacher, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher))
        return

    if data == "btn_teacher_tomorrow":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard())
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_teacher_day_combined_for_telegram(user_teacher, target_date=tomorrow_dt)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher))
        return

    if data == "btn_teacher_week":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard())
            return
        msg = parser.format_teacher_schedule_for_telegram(user_teacher)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher))
        return

    if data == "btn_teacher_subst":
        if not user_teacher:
            await safe_reply(update, "⚠️ Сначала выберите себя из списка преподавателей:", reply_markup=get_teacher_letter_groups_keyboard())
            return
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_teacher_substitutions_for_telegram(user_teacher, date_str=date_str)
        await safe_reply(update, msg, reply_markup=get_teacher_action_keyboard(user_teacher))
        return

    # 8. Действия ученика: сегодня, завтра, неделя, замены
    if data == "btn_today":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = parser.format_day_combined_for_telegram(user_class, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_tomorrow":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_day_combined_for_telegram(user_class, target_date=tomorrow_dt)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_week":
        if not user_class:
            await safe_reply(update, "⚠️ Сначала выберите ваш класс:", reply_markup=get_grade_groups_keyboard())
            return
        msg = parser.format_schedule_for_telegram(user_class)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(user_class))
        return

    if data == "btn_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=user_class)
        kb = get_class_action_keyboard(user_class) if user_class else get_main_menu_keyboard(user_id)
        await safe_reply(update, msg, reply_markup=kb)
        return

    # 9. Замены всей школы
    if data == "btn_all_subst":
        date_str = date.today().strftime("%Y-%m-%d")
        msg = parser.format_substitutions_for_telegram(date_str=date_str, class_name=None)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Главное меню", callback_data="btn_main_menu")]])
        await safe_reply(update, msg, reply_markup=kb)
        return

    # 10. Действия для конкретного класса: today_<class>, tomorrow_<class>, week_<class>, subst_<class>
    if data.startswith("today_"):
        c_name = data.replace("today_", "")
        msg = parser.format_day_combined_for_telegram(c_name, target_date=date.today())
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("tomorrow_"):
        c_name = data.replace("tomorrow_", "")
        tomorrow_dt = get_target_date_for_tomorrow()
        msg = parser.format_day_combined_for_telegram(c_name, target_date=tomorrow_dt)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return

    if data.startswith("week_"):
        c_name = data.replace("week_", "")
        msg = parser.format_schedule_for_telegram(c_name)
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
        msg = parser.format_schedule_for_telegram(c_name, day_index=day_idx)
        await safe_reply(update, msg, reply_markup=get_class_action_keyboard(c_name))
        return


# ================= Обработка текстовых сообщений =================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текста: распознавание класса или фамилии преподавателя"""
    text = update.message.text.strip()
    user_id = update.effective_user.id

    # 1. Проверяем, ввел ли пользователь класс (например "10A", "8k", "1-a")
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

    # 2. Проверяем, ввел ли пользователь фамилию преподавателя (например "Кулакова", "Kulakova", "Романова")
    matched_teacher = parser.normalize_teacher_name(text)
    if matched_teacher:
        user_prefs.set_teacher(user_id, matched_teacher)
        resp_text = (
            f"👩‍🏫 Преподаватель сохранён: *{matched_teacher}*!\n"
            f"Роль: *Преподаватель*\n"
            f"🔔 Вы будете получать уведомления при появлении замен для вас.\n\n"
            f"Выберите действие ниже:"
        )
        await safe_reply(update, resp_text, reply_markup=get_teacher_action_keyboard(matched_teacher))
        return

    # 3. Если текст не распознан -> главное меню
    resp_text = (
        "🤖 Я получил ваше сообщение.\n"
        "Чтобы узнать расписание или замены, напишите название класса (например, `10A`), "
        "свою фамилию преподавателя (например, `Кулакова`), или воспользуйтесь кнопками ниже 👇"
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

        # Если вечер (после 14:00), также проверяем следующий день
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

        # Сохраняем сигнатуры
        seen_tracker.save()

        # Если это первый запуск трекера, не спамим всем старыми заменами
        if len(seen_tracker.seen_signatures) <= sum(len(v) for v in new_items_by_date.values()):
            logger.info("Инициализация списка замен завершена, ожидание новых публикаций.")
            return

        total_new = sum(len(v) for v in new_items_by_date.values())
        if total_new == 0:
            return

        logger.info(f"Обнаружено {total_new} новых замен уроков! Начинаем рассылку подписчикам...")

        # Рассылка пользователям
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
                                reply_markup=get_teacher_action_keyboard(u_teacher)
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

    # Создание приложения бота
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Регистрация команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler(["profile", "role", "settings"], cmd_profile))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("classes", cmd_classes))
    app.add_handler(CommandHandler("teachers", cmd_teachers))
    app.add_handler(CommandHandler("setclass", cmd_setclass))
    app.add_handler(CommandHandler("setteacher", cmd_setteacher))
    app.add_handler(CommandHandler("notify", cmd_notify))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("tomorrow", cmd_tomorrow))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler(["substitutions", "replacements", "zameni"], cmd_substitutions))

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
