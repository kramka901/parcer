"""
Модуль базы данных SQLite для Telegram-бота школы Tallinna Mustjõe Gümnaasium (MJG).
Обеспечивает:
1. Полноценное хранение аккаунтов пользователей (Telegram ID, username, имя, роль, класс, подгруппы, активность).
2. Журналирование каждого прихода (JOINED, RETURNED) и ухода (LEFT, BLOCKED, STOPPED) пользователя.
3. Фиксацию точного числа активных и общего числа пользователей при каждом событии входа/выхода.
4. Хранение настроек групповых бесед и списка администраторов.
5. Автоматическую миграцию существующих данных из user_preferences.json, admins.json, chats_preferences.json.
6. Двустороннюю синхронизацию с JSON-файлами для обратной совместимости.
"""

import sys
import json
import sqlite3
import logging
import threading
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config import (
    DATABASE_FILE,
    USER_DATA_FILE,
    USER_BACKUP_FILE,
    CHAT_DATA_FILE,
    ADMINS_FILE,
    INITIAL_USER_COUNT,
    DEFAULT_ADMIN_IDS
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Возвращает текущую временную метку в формате ISO 8601"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Database:
    """Потокобезопасный менеджер базы данных SQLite"""

    def __init__(
        self,
        db_path=DATABASE_FILE,
        user_data_file=USER_DATA_FILE,
        user_backup_file=USER_BACKUP_FILE,
        chat_data_file=CHAT_DATA_FILE,
        admins_file=ADMINS_FILE,
        enable_json_sync: bool = True
    ):
        self.db_path = str(db_path)
        self.user_data_file = Path(user_data_file)
        self.user_backup_file = Path(user_backup_file)
        self.chat_data_file = Path(chat_data_file)
        self.admins_file = Path(admins_file)
        self.enable_json_sync = enable_json_sync
        self._lock = threading.RLock()
        self._last_seen_cache: Dict[str, float] = {}  # Кэш для троттлинга обновления last_seen
        self._init_db()
        self._migrate_from_json_if_needed()

    @contextmanager
    def get_connection(self):
        """Создает и настраивает подключение к SQLite с поддержкой WAL и автоматическим закрытием дескрипторов"""
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ================= Инициализация схемы БД =================

    def _init_db(self):
        """Создает таблицы и индексы в SQLite, если они не существуют"""
        with self._lock:
            with self.get_connection() as conn:
                # 1. Таблица аккаунтов пользователей (самостоятельные аккаунты)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        user_id TEXT PRIMARY KEY,
                        telegram_id INTEGER,
                        username TEXT,
                        first_name TEXT,
                        last_name TEXT,
                        full_name TEXT,
                        language_code TEXT,
                        is_bot INTEGER DEFAULT 0,
                        role TEXT DEFAULT 'student',
                        class_name TEXT,
                        teacher_name TEXT,
                        subgroup_gender TEXT,
                        subgroup_academic TEXT,
                        notifications INTEGER DEFAULT 1,
                        is_active INTEGER DEFAULT 1,
                        inactive_reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_seen TEXT NOT NULL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_telegram_id ON accounts(telegram_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_is_active ON accounts(is_active);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_role ON accounts(role);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_class_name ON accounts(class_name);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_last_seen ON accounts(last_seen);")

                # 2. Таблица событий прихода и ухода пользователей (с фиксацией числа пользователей)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id TEXT NOT NULL,
                        telegram_id INTEGER,
                        username TEXT,
                        full_name TEXT,
                        event_type TEXT NOT NULL, -- 'JOINED', 'LEFT', 'BLOCKED', 'RETURNED', 'STOPPED', 'MIGRATION'
                        total_users_count INTEGER NOT NULL,
                        active_users_count INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        details TEXT
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_events_user_id ON user_events(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_events_timestamp ON user_events(timestamp);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_user_events_event_type ON user_events(event_type);")

                # 3. Таблица настроек групповых бесед
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id TEXT PRIMARY KEY,
                        title TEXT,
                        chat_type TEXT,
                        class_name TEXT,
                        notifications INTEGER DEFAULT 1,
                        is_active INTEGER DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                """)

                # 4. Таблица администраторов бота
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS admins (
                        user_id INTEGER PRIMARY KEY,
                        added_at TEXT NOT NULL
                    );
                """)

                # 5. Таблица мета-параметров бота (целевое число пользователей, версия и т.д.)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS bot_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    );
                """)

                # Очистка фиктивных аккаунтов и исправление старого ошибочного target_count=97
                conn.execute("DELETE FROM accounts WHERE user_id LIKE 'saved_user_%';")
                conn.execute("DELETE FROM user_events WHERE user_id LIKE 'saved_user_%';")
                conn.execute("UPDATE bot_meta SET value = '0' WHERE key = 'target_count' AND value = '97';")
                conn.execute("INSERT OR IGNORE INTO bot_meta (key, value) VALUES ('target_count', '0');")
                conn.commit()

    # ================= Мета-данные и целевое число пользователей =================

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT value FROM bot_meta WHERE key = ?", (str(key),))
                row = cur.fetchone()
                return row["value"] if row else default

    def set_meta(self, key: str, value: Any):
        with self._lock:
            with self.get_connection() as conn:
                conn.execute(
                    "INSERT INTO bot_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(key), str(value))
                )
                conn.commit()

    def get_target_count(self) -> int:
        val = self.get_meta("target_count", str(INITIAL_USER_COUNT))
        try:
            target = int(val)
            if target == 97:
                self.set_meta("target_count", "0")
                return 0
            return max(0, target)
        except (ValueError, TypeError):
            return 0

    def set_target_count(self, target: int):
        self.set_meta("target_count", str(int(target)))
        self.sync_to_json()

    # ================= Подсчёт пользователей =================

    def get_total_users_count(self) -> int:
        """Возвращает общее количество зарегистрированных в базе аккаунтов"""
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM accounts WHERE user_id NOT LIKE 'saved_user_%'")
                return cur.fetchone()[0]

    def get_active_users_count(self) -> int:
        """
        Возвращает количество активных пользователей.
        Счёт идёт с нуля по реальным зарегистрированным пользователям (is_active = 1).
        Если администратор явно задал целевой минимум через /setusers, учитывает его.
        """
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 1 AND user_id NOT LIKE 'saved_user_%'")
                real_active = cur.fetchone()[0]

                target = self.get_target_count()
                if target <= 0:
                    return real_active

                cur.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 0 AND user_id NOT LIKE 'saved_user_%'")
                inactive_count = cur.fetchone()[0]
                base = max(target, real_active + inactive_count)
                return max(0, base - inactive_count)

    # ================= Управление аккаунтами (Accounts) =================

    def get_account(self, user_id: Any) -> Optional[Dict[str, Any]]:
        """Возвращает словарь с полными данными аккаунта пользователя"""
        uid = str(user_id)
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,))
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["is_active"] = bool(d.get("is_active", 1))
                d["notifications"] = bool(d.get("notifications", 1))
                d["is_bot"] = bool(d.get("is_bot", 0))
                return d

    def delete_account(self, user_id: Any) -> bool:
        """Удаляет аккаунт пользователя из базы данных"""
        uid = str(user_id)
        with self._lock:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM accounts WHERE user_id = ?", (uid,))
                conn.execute("DELETE FROM user_events WHERE user_id = ?", (uid,))
                conn.commit()
        self.sync_to_json()
        return True

    def get_all_accounts(self, active_only: bool = False) -> List[Dict[str, Any]]:
        """Возвращает список всех аккаунтов"""
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                if active_only:
                    cur.execute("SELECT * FROM accounts WHERE is_active = 1 AND user_id NOT LIKE 'saved_user_%' ORDER BY last_seen DESC")
                else:
                    cur.execute("SELECT * FROM accounts WHERE user_id NOT LIKE 'saved_user_%' ORDER BY last_seen DESC")
                return [dict(r) for r in cur.fetchall()]

    def get_accounts_dict(self) -> Dict[str, Dict[str, Any]]:
        """
        Возвращает словарь всех аккаунтов {user_id: data},
        полностью совместимый с форматом прежнего UserPreferences._data.
        """
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM accounts WHERE user_id NOT LIKE 'saved_user_%'")
                res = {}
                for row in cur.fetchall():
                    d = dict(row)
                    uid = d["user_id"]
                    # Конвертируем целочисленные булевы поля
                    d["is_active"] = bool(d.get("is_active", 1))
                    d["notifications"] = bool(d.get("notifications", 1))
                    d["is_bot"] = bool(d.get("is_bot", 0))
                    # Удаляем пустые значения для компактности и совместимости
                    clean_d = {k: v for k, v in d.items() if v is not None}
                    res[uid] = clean_d
                return res

    def save_account_data(self, user_id: Any, **fields) -> Dict[str, Any]:
        """Создает или обновляет произвольные поля аккаунта"""
        uid = str(user_id)
        now = _now_iso()
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,))
                row = cur.fetchone()

                if row:
                    # Обновление существующего аккаунта
                    update_fields = dict(fields)
                    update_fields["updated_at"] = now
                    set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
                    values = list(update_fields.values()) + [uid]
                    conn.execute(f"UPDATE accounts SET {set_clause} WHERE user_id = ?", values)
                else:
                    # Создание нового аккаунта
                    new_acc = {
                        "user_id": uid,
                        "telegram_id": int(uid) if uid.isdigit() else None,
                        "created_at": now,
                        "updated_at": now,
                        "last_seen": now,
                        "is_active": 1,
                        "notifications": 1,
                        "role": "student"
                    }
                    new_acc.update(fields)
                    cols = ", ".join(new_acc.keys())
                    placeholders = ", ".join("?" for _ in new_acc)
                    conn.execute(f"INSERT INTO accounts ({cols}) VALUES ({placeholders})", list(new_acc.values()))

                conn.commit()

        self.sync_to_json()
        return self.get_account(uid) or {}

    # ================= Отслеживание прихода и ухода пользователей =================

    def record_user_arrival(
        self,
        user: Any,
        source: str = "start",
        details: str = ""
    ) -> Tuple[bool, int, int]:
        """
        Регистрирует приход пользователя (новый пользователь или возвращение после блокировки).
        Автоматически сохраняет все данные аккаунта (Telegram ID, username, имя, язык).
        Логирует событие в user_events с текущим числом пользователей.

        Возвращает: (is_new: bool, active_count: int, total_count: int)
        """
        if not user:
            return (False, self.get_active_users_count(), self.get_total_users_count())

        uid = str(user.id)
        now = _now_iso()
        now_ts = datetime.now().timestamp()
        self._last_seen_cache[uid] = now_ts

        username = getattr(user, "username", None)
        first_name = getattr(user, "first_name", "") or ""
        last_name = getattr(user, "last_name", "") or ""
        full_name = getattr(user, "full_name", f"{first_name} {last_name}".strip()) or ""
        language_code = getattr(user, "language_code", None)
        is_bot = 1 if getattr(user, "is_bot", False) else 0

        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,))
                row = cur.fetchone()

                is_new = False
                is_returned = False

                if not row:
                    # Новый пользователь пришёл!
                    is_new = True
                    conn.execute("""
                        INSERT INTO accounts (
                            user_id, telegram_id, username, first_name, last_name, full_name,
                            language_code, is_bot, role, notifications, is_active,
                            created_at, updated_at, last_seen
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        uid, user.id, username, first_name, last_name, full_name,
                        language_code, is_bot, "student", 1, 1,
                        now, now, now
                    ))
                else:
                    # Пользователь уже был в базе
                    was_active = bool(row["is_active"])
                    if not was_active:
                        is_returned = True

                    conn.execute("""
                        UPDATE accounts SET
                            username = COALESCE(?, username),
                            first_name = COALESCE(?, first_name),
                            last_name = COALESCE(?, last_name),
                            full_name = COALESCE(?, full_name),
                            language_code = COALESCE(?, language_code),
                            is_active = 1,
                            inactive_reason = NULL,
                            last_seen = ?,
                            updated_at = ?
                        WHERE user_id = ?
                    """, (
                        username, first_name, last_name, full_name, language_code,
                        now, now, uid
                    ))

                conn.commit()

            # Подсчитываем актуальное число пользователей
            active_count = self.get_active_users_count()
            total_count = self.get_total_users_count()

            # Если пользователь новый или вернулся — записываем событие в историю
            if is_new or is_returned:
                event_type = "JOINED" if is_new else "RETURNED"
                event_desc = details or (
                    f"Новый пользователь пришёл через {source}" if is_new
                    else f"Пользователь вернулся в бота через {source}"
                )

                with self.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO user_events (
                            user_id, telegram_id, username, full_name, event_type,
                            total_users_count, active_users_count, timestamp, details
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        uid, user.id, username, full_name, event_type,
                        total_count, active_count, now, event_desc
                    ))
                    conn.commit()

                logger.info(
                    f"👤 [СОБЫТИЕ {event_type}] Пользователь {uid} (@{username or 'no_user'}, {full_name}). "
                    f"Активных: {active_count}, Всего: {total_count}."
                )

        self.sync_to_json()
        return (is_new, active_count, total_count)

    def record_user_departure(
        self,
        user_id: Any,
        reason: str = "left",
        username: Optional[str] = None,
        full_name: Optional[str] = None,
        details: str = ""
    ) -> Tuple[bool, int, int]:
        """
        Регистрирует уход пользователя (блокировка бота, отписка через /stop, запрет отправки сообщений).
        Логирует событие в user_events с точным числом пользователей на момент ухода.

        Возвращает: (changed: bool, active_count: int, total_count: int)
        """
        uid = str(user_id)
        now = _now_iso()

        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM accounts WHERE user_id = ?", (uid,))
                row = cur.fetchone()

                if not row:
                    # Создаем запись как неактивную
                    conn.execute("""
                        INSERT INTO accounts (
                            user_id, telegram_id, username, full_name, is_active,
                            inactive_reason, created_at, updated_at, last_seen
                        ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                    """, (
                        uid, int(uid) if uid.isdigit() else None, username, full_name,
                        reason, now, now, now
                    ))
                    conn.commit()
                    changed = True
                else:
                    was_active = bool(row["is_active"])
                    username = username or row["username"]
                    full_name = full_name or row["full_name"]

                    conn.execute("""
                        UPDATE accounts SET
                            is_active = 0,
                            inactive_reason = ?,
                            notifications = 0,
                            updated_at = ?
                        WHERE user_id = ?
                    """, (reason, now, uid))
                    conn.commit()
                    changed = was_active

            active_count = self.get_active_users_count()
            total_count = self.get_total_users_count()

            if changed:
                event_type = "STOPPED" if reason == "user_stop" else "BLOCKED" if "block" in reason.lower() else "LEFT"
                event_desc = details or f"Пользователь ушёл (причина: {reason})"

                with self.get_connection() as conn:
                    conn.execute("""
                        INSERT INTO user_events (
                            user_id, telegram_id, username, full_name, event_type,
                            total_users_count, active_users_count, timestamp, details
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        uid, int(uid) if uid.isdigit() else None, username, full_name, event_type,
                        total_count, active_count, now, event_desc
                    ))
                    conn.commit()

                logger.info(
                    f"🚪 [СОБЫТИЕ {event_type}] Пользователь {uid} (@{username or 'no_user'}, {full_name}). "
                    f"Активных: {active_count}, Всего: {total_count}."
                )

        self.sync_to_json()
        return (changed, active_count, total_count)

    def record_user_interaction(self, user: Any, source: str = "interaction"):
        """
        Вызывается при любом входящем сообщении/кнопке от пользователя.
        Если пользователь новый или был неактивен — фиксирует приход.
        Если уже активен — обновляет last_seen (с троттлингом в 60 секунд) и актуализирует username/имя.
        """
        if not user:
            return

        uid = str(user.id)
        now_ts = datetime.now().timestamp()
        last_ts = self._last_seen_cache.get(uid, 0)

        # Если пользователь уже взаимодействовал менее минуты назад, пропускаем частые SQL-запросы
        if (now_ts - last_ts) < 60:
            return

        account = self.get_account(uid)
        if not account or not account.get("is_active"):
            self.record_user_arrival(user, source=source)
        else:
            self._last_seen_cache[uid] = now_ts
            now = _now_iso()
            username = getattr(user, "username", None)
            first_name = getattr(user, "first_name", "") or ""
            last_name = getattr(user, "last_name", "") or ""
            full_name = getattr(user, "full_name", f"{first_name} {last_name}".strip()) or ""

            with self._lock:
                with self.get_connection() as conn:
                    conn.execute("""
                        UPDATE accounts SET
                            username = COALESCE(?, username),
                            first_name = COALESCE(?, first_name),
                            last_name = COALESCE(?, last_name),
                            full_name = COALESCE(?, full_name),
                            last_seen = ?
                        WHERE user_id = ?
                    """, (username, first_name, last_name, full_name, now, uid))
                    conn.commit()

    # ================= Получение истории и статистики событий =================

    def get_user_events(self, limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
        """Возвращает список последних событий прихода и ухода пользователей"""
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT * FROM user_events
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset))
                return [dict(r) for r in cur.fetchall()]

    def get_events_stats_summary(self) -> Dict[str, Any]:
        """Возвращает сводную статистику по событиям (сегодня, за все время)"""
        today_prefix = date.today().strftime("%Y-%m-%d")
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM user_events WHERE user_id NOT LIKE 'saved_user_%'")
                total_events = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM user_events
                    WHERE timestamp LIKE ? AND event_type IN ('JOINED', 'RETURNED')
                      AND user_id NOT LIKE 'saved_user_%'
                """, (f"{today_prefix}%",))
                joined_today = cur.fetchone()[0]

                cur.execute("""
                    SELECT COUNT(*) FROM user_events
                    WHERE timestamp LIKE ? AND event_type IN ('LEFT', 'BLOCKED', 'STOPPED')
                      AND user_id NOT LIKE 'saved_user_%'
                """, (f"{today_prefix}%",))
                left_today = cur.fetchone()[0]

                active_count = self.get_active_users_count()

                cur.execute("SELECT COUNT(*) FROM accounts WHERE user_id NOT LIKE 'saved_user_%'")
                total_accounts = cur.fetchone()[0]

                cur.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 0 AND user_id NOT LIKE 'saved_user_%'")
                inactive_count = cur.fetchone()[0]

                return {
                    "total_events": total_events,
                    "joined_today": joined_today,
                    "left_today": left_today,
                    "active_users": active_count,
                    "total_accounts": total_accounts,
                    "inactive_users": inactive_count
                }

    # ================= Настройки классов и профилей пользователей =================

    def set_student(self, user_id: Any, class_name: str):
        """Устанавливает роль ученика и привязывает класс"""
        self.save_account_data(
            user_id,
            role="student",
            class_name=class_name,
            teacher_name=None,
            is_active=1,
            inactive_reason=None
        )

    def set_teacher(self, user_id: Any, teacher_name: str):
        """Устанавливает роль преподавателя и сохраняет ФИО"""
        self.save_account_data(
            user_id,
            role="teacher",
            teacher_name=teacher_name,
            class_name=None,
            is_active=1,
            inactive_reason=None
        )

    def reset_role(self, user_id: Any):
        """Сбрасывает выбранную роль и привязанную сущность"""
        uid = str(user_id)
        now = _now_iso()
        with self._lock:
            with self.get_connection() as conn:
                conn.execute("""
                    UPDATE accounts SET
                        role = NULL,
                        class_name = NULL,
                        teacher_name = NULL,
                        updated_at = ?
                    WHERE user_id = ?
                """, (now, uid))
                conn.commit()
        self.sync_to_json()

    def set_notifications(self, user_id: Any, enabled: bool):
        self.save_account_data(user_id, notifications=1 if enabled else 0)

    def toggle_notifications(self, user_id: Any) -> bool:
        acc = self.get_account(user_id)
        curr = bool(acc.get("notifications", 1)) if acc else True
        new_val = not curr
        self.set_notifications(user_id, new_val)
        return new_val

    def set_gender_filter(self, user_id: Any, gender: Optional[str]):
        val = gender if gender in ("boys", "girls") else None
        self.save_account_data(user_id, subgroup_gender=val)

    def set_academic_filter(self, user_id: Any, academic: Optional[str]):
        val = academic if academic in ("1", "2") else None
        self.save_account_data(user_id, subgroup_academic=val)

    # ================= Управление групповыми чатами =================

    def get_chat(self, chat_id: Any) -> Dict[str, Any]:
        cid = str(chat_id)
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM chats WHERE chat_id = ?", (cid,))
                row = cur.fetchone()
                if not row:
                    return {}
                d = dict(row)
                d["notifications"] = bool(d.get("notifications", 1))
                d["is_active"] = bool(d.get("is_active", 1))
                return d

    def get_all_chats(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM chats")
                res = {}
                for r in cur.fetchall():
                    d = dict(r)
                    cid = d["chat_id"]
                    d["notifications"] = bool(d.get("notifications", 1))
                    d["is_active"] = bool(d.get("is_active", 1))
                    res[cid] = d
                return res

    def set_chat_class(self, chat_id: Any, class_name: str, title: Optional[str] = None):
        cid = str(chat_id)
        now = _now_iso()
        with self._lock:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO chats (chat_id, class_name, title, notifications, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, 1, 1, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        class_name = excluded.class_name,
                        title = COALESCE(excluded.title, chats.title),
                        is_active = 1,
                        updated_at = excluded.updated_at
                """, (cid, class_name, title, now, now))
                conn.commit()
        self.sync_chats_to_json()

    def remove_chat(self, chat_id: Any):
        cid = str(chat_id)
        with self._lock:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM chats WHERE chat_id = ?", (cid,))
                conn.commit()
        self.sync_chats_to_json()

    def set_chat_notifications(self, chat_id: Any, enabled: bool):
        cid = str(chat_id)
        now = _now_iso()
        with self._lock:
            with self.get_connection() as conn:
                conn.execute("""
                    INSERT INTO chats (chat_id, notifications, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        notifications = excluded.notifications,
                        updated_at = excluded.updated_at
                """, (cid, 1 if enabled else 0, now, now))
                conn.commit()
        self.sync_chats_to_json()

    # ================= Управление администраторами =================

    def get_admins(self) -> List[int]:
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM admins ORDER BY user_id ASC")
                admins = [int(r["user_id"]) for r in cur.fetchall()]
                # Дополняем администраторами по умолчанию, если их нет
                for aid in DEFAULT_ADMIN_IDS:
                    if aid not in admins:
                        admins.append(aid)
                return sorted(list(set(admins)))

    def is_admin(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False
        return uid in self.get_admins()

    def add_admin(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False

        now = _now_iso()
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM admins WHERE user_id = ?", (uid,))
                if cur.fetchone():
                    return False
                conn.execute("INSERT INTO admins (user_id, added_at) VALUES (?, ?)", (uid, now))
                conn.commit()
        self.sync_admins_to_json()
        return True

    def remove_admin(self, user_id: Any) -> bool:
        try:
            uid = int(user_id)
        except (ValueError, TypeError):
            return False

        admins = self.get_admins()
        if len(admins) <= 1:
            return False  # Нельзя удалить последнего администратора

        with self._lock:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM admins WHERE user_id = ?", (uid,))
                conn.commit()
        self.sync_admins_to_json()
        return True

    # ================= Миграция и синхронизация с JSON =================

    def _migrate_from_json_if_needed(self):
        """
        Проверяет, пуста ли база данных, и автоматически импортирует существующие данные из
        user_preferences.json, admins.json и chats_preferences.json.
        """
        with self._lock:
            with self.get_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM accounts")
                count = cur.fetchone()[0]

            if count > 0:
                return  # База уже содержит данные

            logger.info("База данных SQLite пуста. Начинаем автоматическую миграцию существующих JSON-данных...")
            now = _now_iso()

            # 1. Миграция пользователей
            if self.user_data_file.exists():
                try:
                    with open(self.user_data_file, "r", encoding="utf-8") as f:
                        users_data = json.load(f)

                    meta = users_data.get("_meta", {})
                    raw_target = meta.get("target_count", INITIAL_USER_COUNT)
                    # Сбрасываем ошибочное значение 97 на 0 для чистого отсчёта пользователей с нуля
                    target_count = 0 if str(raw_target).strip() in ("97", "") else int(raw_target)
                    self.set_meta("target_count", str(target_count))

                    with self.get_connection() as conn:
                        for uid_str, u in users_data.items():
                            if uid_str.startswith("_") or uid_str.startswith("saved_user_"):
                                continue

                            telegram_id = int(uid_str) if uid_str.isdigit() else None
                            role = u.get("role") or ("teacher" if u.get("teacher_name") else "student")
                            class_name = u.get("class_name")
                            teacher_name = u.get("teacher_name")
                            subgroup_gender = u.get("subgroup_gender")
                            subgroup_academic = u.get("subgroup_academic")
                            notifications = 1 if u.get("notifications", True) else 0
                            is_active = 1 if u.get("is_active", True) else 0
                            inactive_reason = u.get("inactive_reason")
                            created_at = u.get("created_at") or now
                            last_seen = u.get("last_seen") or now

                            conn.execute("""
                                INSERT OR REPLACE INTO accounts (
                                    user_id, telegram_id, role, class_name, teacher_name,
                                    subgroup_gender, subgroup_academic, notifications,
                                    is_active, inactive_reason, created_at, updated_at, last_seen
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                uid_str, telegram_id, role, class_name, teacher_name,
                                subgroup_gender, subgroup_academic, notifications,
                                is_active, inactive_reason, created_at, now, last_seen
                            ))
                        conn.commit()

                    total = self.get_total_users_count()
                    active = self.get_active_users_count()

                    # Фиксируем начальное событие миграции в истории
                    with self.get_connection() as conn:
                        conn.execute("""
                            INSERT INTO user_events (
                                user_id, event_type, total_users_count, active_users_count, timestamp, details
                            ) VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            "SYSTEM", "MIGRATION", total, active, now,
                            f"Автоматическая миграция {total} аккаунтов из {self.user_data_file.name}"
                        ))
                        conn.commit()

                    logger.info(f"✅ Успешно импортировано {total} аккаунтов (активных: {active}) в SQLite.")
                except Exception as e:
                    logger.error(f"Ошибка миграции пользователей из JSON: {e}")

            # 2. Миграция администраторов
            if self.admins_file.exists():
                try:
                    with open(self.admins_file, "r", encoding="utf-8") as f:
                        admins_data = json.load(f)
                    with self.get_connection() as conn:
                        for aid in admins_data.get("admins", []):
                            try:
                                conn.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (int(aid), now))
                            except Exception:
                                pass
                        conn.commit()
                except Exception as e:
                    logger.error(f"Ошибка миграции администраторов из JSON: {e}")
            else:
                with self.get_connection() as conn:
                    for aid in DEFAULT_ADMIN_IDS:
                        conn.execute("INSERT OR IGNORE INTO admins (user_id, added_at) VALUES (?, ?)", (aid, now))
                    conn.commit()

            # 3. Миграция групповых чатов
            if self.chat_data_file.exists():
                try:
                    with open(self.chat_data_file, "r", encoding="utf-8") as f:
                        chats_data = json.load(f)
                    with self.get_connection() as conn:
                        for cid_str, c in chats_data.items():
                            if cid_str.startswith("_"):
                                continue
                            conn.execute("""
                                INSERT OR REPLACE INTO chats (
                                    chat_id, title, class_name, notifications, is_active, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (
                                cid_str, c.get("title"), c.get("class_name"),
                                1 if c.get("notifications", True) else 0,
                                1, now, now
                            ))
                        conn.commit()
                except Exception as e:
                    logger.error(f"Ошибка миграции чатов из JSON: {e}")

    def sync_to_json(self):
        """
        Сохраняет актуальное состояние пользователей обратно в user_preferences.json
        для гарантированной совместимости со сторонними скриптами и резервного копирования.
        """
        if not self.enable_json_sync:
            return
        try:
            data = self.get_accounts_dict()
            data["_meta"] = {"target_count": self.get_target_count()}
            with open(self.user_data_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            try:
                with open(self.user_backup_file, "w", encoding="utf-8") as bf:
                    json.dump(data, bf, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка синхронизации данных в {self.user_data_file}: {e}")

    def sync_admins_to_json(self):
        """Сохраняет актуальный список администраторов в admins.json"""
        if not self.enable_json_sync:
            return
        try:
            admins = self.get_admins()
            with open(self.admins_file, "w", encoding="utf-8") as f:
                json.dump({"admins": admins}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка синхронизации {self.admins_file}: {e}")

    def sync_chats_to_json(self):
        """Сохраняет актуальные настройки бесед в chats_preferences.json"""
        if not self.enable_json_sync:
            return
        try:
            chats = self.get_all_chats()
            with open(self.chat_data_file, "w", encoding="utf-8") as f:
                json.dump(chats, f, ensure_ascii=False, indent=2)
            try:
                with open(self.chat_data_file.with_suffix(".json.bak"), "w", encoding="utf-8") as bf:
                    json.dump(chats, bf, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Ошибка синхронизации {self.chat_data_file}: {e}")


# Глобальный экземпляр базы данных
db = Database()
