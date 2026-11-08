"""
Комплексные тесты для базы данных SQLite, аккаунтов пользователей,
отслеживания прихода/ухода и подсчёта числа пользователей.
"""

import os
import sys
import tempfile
import threading
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from database import Database, db
from bot import UserPreferences, ChatPreferences, AdminManager


class DummyUser:
    def __init__(self, uid: int, username: str, first_name: str, last_name: str = "", language_code: str = "ru"):
        self.id = uid
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.full_name = f"{first_name} {last_name}".strip()
        self.language_code = language_code
        self.is_bot = False


def run_tests():
    print("=" * 65)
    print("🧪 ЗАПУСК ТЕСТОВ БАЗЫ ДАННЫХ, АККАУНТОВ И УЧЁТА ПОЛЬЗОВАТЕЛЕЙ")
    print("=" * 65)

    # Используем изолированную временную базу данных для чистоты тестирования
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        temp_db_path = tf.name

    try:
        test_db = Database(db_path=temp_db_path, enable_json_sync=False)
        print("\n[ТЕСТ 1] Инициализация схемы БД и таблиц:")
        with test_db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {r[0] for r in cur.fetchall()}
            for expected in ["accounts", "user_events", "chats", "admins", "bot_meta"]:
                assert expected in tables, f"Таблица {expected} не найдена в БД!"
                print(f"  • Таблица '{expected}': ✅ OK")

        # 2. Тест начального количества пользователей
        print("\n[ТЕСТ 2] Начальное базовое число пользователей:")
        initial_active = test_db.get_active_users_count()
        print(f"  • Начальное число активных пользователей: {initial_active}")
        assert initial_active >= 97, f"Ожидалось не менее 97 пользователей, получено {initial_active}"
        print("  • Базовый счётчик 97 пользователей: ✅ OK")

        # 3. Тест регистрации нового аккаунта (приход пользователя)
        print("\n[ТЕСТ 3] Приход нового пользователя (JOINED):")
        user1 = DummyUser(100000001, "alex_tallinn", "Алексей", "Иванов")
        is_new, active_1, total_1 = test_db.record_user_arrival(user1, source="start", details="Команда /start")

        assert is_new is True, "Новый пользователь должен иметь is_new=True"
        assert active_1 == initial_active, f"Активных пользователей: {active_1}"
        assert total_1 >= 1, f"Всего аккаунтов в базе: {total_1}"

        acc = test_db.get_account(user1.id)
        assert acc is not None, "Аккаунт должен быть сохранён в таблице accounts"
        assert acc["username"] == "alex_tallinn", f"Неверный username: {acc['username']}"
        assert acc["first_name"] == "Алексей", f"Неверное first_name: {acc['first_name']}"
        assert acc["last_name"] == "Иванов", f"Неверное last_name: {acc['last_name']}"
        assert acc["full_name"] == "Алексей Иванов", f"Неверное full_name: {acc['full_name']}"
        assert acc["is_active"] is True, "Аккаунт должен быть активным"
        print(f"  • Аккаунт сохранён: {acc['full_name']} (@{acc['username']}) | Активных: {active_1}, Всего: {total_1} ✅ OK")

        # 4. Проверка записи события в user_events
        print("\n[ТЕСТ 4] Запись события прихода в user_events с фиксацией числа пользователей:")
        events = test_db.get_user_events(limit=5)
        join_event = next((e for e in events if str(e["user_id"]) == str(user1.id) and e["event_type"] == "JOINED"), None)
        assert join_event is not None, "Событие JOINED должно быть зафиксировано в user_events"
        assert join_event["active_users_count"] == active_1, "Событие должно хранить число активных пользователей"
        assert join_event["total_users_count"] == total_1, "Событие должно хранить общее число пользователей"
        assert "alex_tallinn" in join_event["username"], "Событие должно содержать username"
        print(f"  • Событие зафиксировано: {join_event['event_type']} (Активных: {join_event['active_users_count']}, Всего: {join_event['total_users_count']}) ✅ OK")

        # 5. Тест настройки профиля пользователя (ученик, класс, уведомления, подгруппы)
        print("\n[ТЕСТ 5] Настройка профиля (роль, класс, уведомления, подгруппы):")
        test_db.set_student(user1.id, "10A")
        test_db.set_gender_filter(user1.id, "boys")
        test_db.set_academic_filter(user1.id, "1")

        acc = test_db.get_account(user1.id)
        assert acc["role"] == "student", "Роль должна быть student"
        assert acc["class_name"] == "10A", "Класс должен быть 10A"
        assert acc["subgroup_gender"] == "boys", "Подгруппа должна быть boys"
        assert acc["subgroup_academic"] == "1", "Академическая группа должна быть 1"
        print("  • Профиль ученика 10A с подгруппами успешно настроен ✅ OK")

        # Переключение на учителя
        test_db.set_teacher(user1.id, "Кулакова")
        acc = test_db.get_account(user1.id)
        assert acc["role"] == "teacher", "Роль должна измениться на teacher"
        assert acc["teacher_name"] == "Кулакова", "ФИО учителя должно быть сохранено"
        assert acc.get("class_name") is None, "Класс должен быть очищен при смене на учителя"
        print("  • Переключение на роль преподавателя ✅ OK")

        # 6. Тест ухода пользователя (блокировка бота / STOP / LEFT)
        print("\n[ТЕСТ 6] Уход пользователя (BLOCKED / LEFT) и обновление числа пользователей:")
        changed, active_after_leave, total_after_leave = test_db.record_user_departure(
            user1.id,
            reason="blocked",
            details="Пользователь заблокировал бота"
        )
        assert changed is True, "Уход активного пользователя должен вернуть changed=True"
        assert active_after_leave == active_1 - 1, f"Число активных пользователей должно уменьшиться на 1: {active_after_leave}"
        assert total_after_leave == total_1, "Общее число аккаунтов не меняется при блокировке"

        acc = test_db.get_account(user1.id)
        assert acc["is_active"] is False, "Аккаунт должен быть помечен как неактивный"
        assert acc["inactive_reason"] == "blocked", "Причина должна быть 'blocked'"
        print(f"  • Пользователь ушёл: активных стало {active_after_leave} (было {active_1}) ✅ OK")

        # Проверка события ухода в user_events
        events = test_db.get_user_events(limit=5)
        leave_event = next((e for e in events if str(e["user_id"]) == str(user1.id) and e["event_type"] == "BLOCKED"), None)
        assert leave_event is not None, "Событие BLOCKED должно быть зафиксировано"
        assert leave_event["active_users_count"] == active_after_leave, "Событие должно фиксировать новое число пользователей"
        print(f"  • Событие ухода зафиксировано: BLOCKED (Активных: {leave_event['active_users_count']}) ✅ OK")

        # 7. Тест возвращения пользователя (RETURNED / UNBLOCKED)
        print("\n[ТЕСТ 7] Возвращение пользователя (RETURNED) и увеличение счётчика:")
        is_new, active_after_return, total_after_return = test_db.record_user_arrival(
            user1,
            source="unblocked",
            details="Пользователь разблокировал бота"
        )
        assert is_new is False, "При возвращении is_new должен быть False"
        assert active_after_return == active_1, f"Число активных должно восстановиться: {active_after_return}"

        acc = test_db.get_account(user1.id)
        assert acc["is_active"] is True, "Аккаунт снова активен"
        assert acc["inactive_reason"] is None, "inactive_reason должен быть очищен"
        print(f"  • Пользователь вернулся: активных снова {active_after_return} ✅ OK")

        # 8. Тест UserPreferences и ChatPreferences над базой данных
        print("\n[ТЕСТ 8] Совместимость с обёртками UserPreferences и ChatPreferences:")
        up = UserPreferences(database=test_db)
        cp = ChatPreferences(database=test_db)
        am = AdminManager(database=test_db)

        # Проверка UserPreferences
        assert up.get_active_users_count() == active_after_return
        assert up.get_role(user1.id) == "teacher"
        assert up.get_teacher(user1.id) == "Кулакова"
        up.set_student(user1.id, "11C")
        assert up.get_class(user1.id) == "11C"
        print("  • UserPreferences методы: ✅ OK")

        # Проверка ChatPreferences
        cp.set_class("-100999888777", "10A", title="10A Классный чат")
        chat = cp.get_chat("-100999888777")
        assert chat["class_name"] == "10A"
        assert chat["title"] == "10A Классный чат"
        assert cp.is_notifications_enabled("-100999888777") is True
        print("  • ChatPreferences методы: ✅ OK")

        # Проверка AdminManager
        assert am.is_admin(1637426603) is True
        am.add_admin(987654321)
        assert am.is_admin(987654321) is True
        am.remove_admin(987654321)
        assert am.is_admin(987654321) is False
        print("  • AdminManager методы: ✅ OK")

        # 9. Тест сводки статистики событий
        print("\n[ТЕСТ 9] Сводка статистики аудитории (get_events_stats_summary):")
        summary = test_db.get_events_stats_summary()
        assert summary["total_events"] >= 3, "Должно быть минимум 3 события"
        assert summary["joined_today"] >= 1, "Должно быть зафиксировано минимум 1 присоединение за сегодня"
        print(f"  • Сводка: Всего событий={summary['total_events']}, Пришло={summary['joined_today']}, Ушло={summary['left_today']} ✅ OK")

        # 10. Стресс-тест многопоточности (одновременные обращения)
        print("\n[ТЕСТ 10] Проверка потокобезопасности SQLite при параллельных запросах:")
        errors = []

        def worker(thread_id):
            try:
                for i in range(10):
                    u = DummyUser(200000000 + thread_id * 100 + i, f"user_{thread_id}_{i}", f"Имя{thread_id}")
                    test_db.record_user_arrival(u, source="stress_test")
                    test_db.record_user_departure(u.id, reason="stress_test")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Ошибки при параллельной работе с БД: {errors}"
        print("  • 5 параллельных потоков по 10 операций: 0 ошибок ✅ OK")

        # 11. Проверка основной рабочей базы проекта
        print("\n[ТЕСТ 11] Проверка основной рабочей базы проекта (database.db):")
        main_total = db.get_total_users_count()
        main_active = db.get_active_users_count()
        main_events = len(db.get_user_events())
        print(f"  • Всего аккаунтов в основной БД: {main_total}")
        print(f"  • Активных пользователей: {main_active}")
        print(f"  • Событий в журнале: {main_events}")
        assert main_total >= 97, "В основной базе должно быть сохранено не менее 97 аккаунтов"
        assert main_active >= 97, "Активных должно быть не менее 97"
        print("  • Основная база данных: ✅ OK")

    finally:
        # Удаляем временную базу данных
        try:
            os.remove(temp_db_path)
            for extra in [temp_db_path + "-wal", temp_db_path + "-shm"]:
                if os.path.exists(extra):
                    os.remove(extra)
        except Exception:
            pass

    print("\n" + "=" * 65)
    print("🎉 ВСЕ ТЕСТЫ БАЗЫ ДАННЫХ И АККАУНТОВ УСПЕШНО ПРОЙДЕНЫ!")
    print("=" * 65)


if __name__ == "__main__":
    run_tests()
