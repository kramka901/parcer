import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

from datetime import date
from parser import MJGParser, should_show_card
from bot import AdminManager

def run_tests():
    print("=" * 65)
    print("🚀 НАЧАЛО ТЕСТИРОВАНИЯ ОБНОВЛЁННОГО БОТА И ПАРСЕРА MJG (www.mjg.ee)")
    print("=" * 65)

    parser = MJGParser()

    # 1. Тест нормализации названий классов
    print("\n[ТЕСТ 1] Нормализация названий классов:")
    test_cases = [
        ("10a", "10A"),
        ("10-a", "10A"),
        ("10 A", "10A"),
        ("7-8e", "7-8E"),
        ("1a", "1A"),
        ("8к", "8K"),
        ("11с", "11C"),
        ("6б", "6B"),
        ("5л", "5L"),
        ("5п", "5P")
    ]
    for raw, expected in test_cases:
        res = parser.normalize_class_name(raw)
        status = "✅ OK" if res == expected else f"❌ FAIL ({res} != {expected})"
        print(f"  '{raw}' -> '{res}' {status}")
        assert res == expected, f"Failed normalization for {raw}"

    # 2. Тест получения списка классов
    print("\n[ТЕСТ 2] Получение списка классов школы:")
    classes = parser.get_classes()
    print(f"  Найдено классов: {len(classes)}")
    assert len(classes) >= 30, "В школе MJG должно быть не менее 30 классов!"
    assert "10A" in classes, "Класс 10A должен присутствовать!"
    assert "8K" in classes, "Класс 8K должен присутствовать!"
    print("  ✅ Список классов успешно получен!")

    # 3. Тест учителей и поиска по фамилии
    print("\n[ТЕСТ 3] Список учителей и поиск по имени/транслиту:")
    teachers = parser.get_teachers()
    print(f"  Всего преподавателей в базе: {len(teachers)}")
    assert len(teachers) > 0, "Список преподавателей не должен быть пустым!"

    t1 = parser.normalize_teacher_name("Кулакова")
    print(f"  Поиск 'Кулакова' -> '{t1}'")
    assert t1 == "Kulakova Dariya", f"Ожидалось Kulakova Dariya, получено {t1}"

    t2 = parser.normalize_teacher_name("Ahv")
    print(f"  Поиск 'Ahv' -> '{t2}'")
    assert t2 == "Ahv Albert", f"Ожидалось Ahv Albert, получено {t2}"

    t3 = parser.normalize_teacher_name("Тряпичкина")
    print(f"  Поиск 'Тряпичкина' -> '{t3}'")
    assert t3 == "Trjapitškina Jekaterina", f"Ожидалось Trjapitškina Jekaterina, получено {t3}"

    t_volossova = parser.normalize_teacher_name("Волосова")
    print(f"  Поиск 'Волосова' -> '{t_volossova}'")
    assert t_volossova == "Volossova Olga", f"Ожидалось Volossova Olga, получено {t_volossova}"

    t_laine = parser.normalize_teacher_name("Лайне")
    print(f"  Поиск 'Лайне' -> '{t_laine}'")
    assert t_laine == "Laine Ninel", f"Ожидалось Laine Ninel, получено {t_laine}"

    t_kovylina = parser.normalize_teacher_name("Ковылина")
    print(f"  Поиск 'Ковылина' -> '{t_kovylina}'")
    assert t_kovylina == "Kovõlina Inna", f"Ожидалось Kovõlina Inna, получено {t_kovylina}"

    t_uksiratsa = parser.normalize_teacher_name("Юксиратса")
    print(f"  Поиск 'Юксиратса' -> '{t_uksiratsa}'")
    assert t_uksiratsa == "Üksiratsa Elona", f"Ожидалось Üksiratsa Elona, получено {t_uksiratsa}"

    t_cojuhari = parser.normalize_teacher_name("Кожухарь")
    print(f"  Поиск 'Кожухарь' -> '{t_cojuhari}'")
    assert t_cojuhari == "Cojuhari Veronika", f"Ожидалось Cojuhari Veronika, получено {t_cojuhari}"

    print("  ✅ Поиск преподавателей на кириллице и латинице работает корректно!")

    # 4. Тест расписания для класса и проверки подгрупп (мальчики / девочки)
    print("\n[ТЕСТ 4] Расписание класса 8K и подгруппы (физ-ра мальчики/девочки):")
    sched_8k = parser.get_class_schedule("8K")
    assert sched_8k is not None, "Расписание 8K не должно быть пустым!"
    monday_8k = sched_8k.get(0, {})
    p1_cards = monday_8k.get(1, [])
    assert len(p1_cards) >= 2, "1 урок в понедельник у 8K должен быть разделен на подгруппы (мальчики и девочки)!"

    groups_found = [c.get("group") for c in p1_cards]
    print(f"  Урок 1 понедельник у 8K: найдены группы {groups_found}")
    assert any("Tüdrukud" in g for g in groups_found), "Должна быть группа девочек Tüdrukud!"
    assert any("Poisid" in g for g in groups_found), "Должна быть группа мальчиков Poisid!"

    # Проверка фильтрации подгрупп
    filter_boys = {"gender": "boys"}
    filter_girls = {"gender": "girls"}

    visible_boys = [c for c in p1_cards if should_show_card(c, filter_boys)]
    visible_girls = [c for c in p1_cards if should_show_card(c, filter_girls)]
    print(f"  Фильтр мальчиков оставил карточек: {len(visible_boys)} (ожидается 1)")
    print(f"  Фильтр девочек оставил карточек: {len(visible_girls)} (ожидается 1)")
    assert len(visible_boys) == 1 and "Poisid" in visible_boys[0]["group"]
    assert len(visible_girls) == 1 and "Tüdrukud" in visible_girls[0]["group"]
    print("  ✅ Подгруппы физкультуры (мальчики/девочки) и фильтрация работают идеально!")

    # [ТЕСТ 4.1] Проверка 4-го урока в четверг у 8K (исправление сдвоенных уроков durationperiods=2)
    print("\n[ТЕСТ 4.1] Проверка 4-го урока в четверг у 8K (Käsitöö 3 и 4 уроки):")
    thu_8k = sched_8k.get(3, {})
    assert 4 in thu_8k, "В четверг у 8K ДОЛЖЕН БЫТЬ 4-й урок (сдвоенный Käsitöö)!"
    assert 3 in thu_8k, "В четверг у 8K должен быть 3-й урок!"
    p3_cards = thu_8k[3]
    p4_cards = thu_8k[4]
    assert len(p4_cards) == len(p3_cards), "Количество карточек на 3 и 4 уроках должно совпадать!"
    print(f"  Урок 3 в четверг: {[c['subject'] for c in p3_cards]}")
    print(f"  Урок 4 в четверг: {[c['subject'] for c in p4_cards]}")
    assert all("Käsitöö" in c["subject"] for c in p4_cards), "4 урок должен быть Käsitöö!"

    # Проверка фильтра мальчиков/девочек для 4 урока в четверг
    thu_boys_p4 = [c for c in p4_cards if should_show_card(c, filter_boys)]
    thu_girls_p4 = [c for c in p4_cards if should_show_card(c, filter_girls)]
    assert len(thu_boys_p4) == 1 and "Tesla Aleksander" in thu_boys_p4[0]["teachers"]
    assert len(thu_girls_p4) == 1 and "Jurkina Tatjana" in thu_girls_p4[0]["teachers"]
    print("  ✅ 4-й урок в четверг у 8K успешно восстановлен и корректно фильтруется по группам!")

    # 5. Тест расписания преподавателей: классы и кабинеты
    print("\n[ТЕСТ 5] Расписание преподавателей (классы, кабинеты, группы):")
    sched_k = parser.get_teacher_schedule("Kulakova Dariya")
    total_k = sum(len(lessons) for d in sched_k.values() for lessons in d.values())
    print(f"  Kulakova Dariya: всего уроков на неделе: {total_k}")
    assert total_k > 0, "У Kulakova Dariya должны быть уроки в расписании!"

    sample_lesson = list(sched_k[0].values())[0][0]
    print(f"  Пример урока: Класс: {sample_lesson.get('class_name')}, Кабинет: {sample_lesson.get('classrooms')}, Предмет: {sample_lesson.get('subject')}")
    assert sample_lesson.get("class_name"), "У урока учителя должен быть указан класс!"
    assert sample_lesson.get("classrooms"), "У урока учителя должен быть указан кабинет!"

    sched_trjap = parser.get_teacher_schedule("Trjapitškina Jekaterina")
    total_trjap = sum(len(lessons) for d in sched_trjap.values() for lessons in d.values())
    print(f"  Trjapitškina Jekaterina: всего уроков на неделе: {total_trjap}")
    assert total_trjap > 0, "У Trjapitškina Jekaterina должны быть уроки в расписании!"

    # Учитель Tesla Aleksander должен иметь 8K на 3 и 4 уроках в четверг
    sched_tesla = parser.get_teacher_schedule("Tesla Aleksander")
    thu_tesla = sched_tesla.get(3, {})
    assert 3 in thu_tesla and 4 in thu_tesla, "У Tesla Aleksander должны быть уроки 3 и 4 в четверг!"
    assert any(c.get("class_name") == "8K" for c in thu_tesla[4]), "Урок 4 в четверг должен быть у 8K!"
    print("  ✅ Расписание учителей со сдвоенными уроками сформировано корректно!")

    # 6. Тест панели администратора и управления администраторами
    print("\n[ТЕСТ 6] Управление администраторами (AdminManager):")
    admin_mgr = AdminManager()
    assert admin_mgr.is_admin(1637426603), "Начальный админ 1637426603 должен быть в списке!"

    test_admin_id = 987654321
    added = admin_mgr.add_admin(test_admin_id)
    assert added and admin_mgr.is_admin(test_admin_id), "Новый администратор должен быть добавлен!"
    removed = admin_mgr.remove_admin(test_admin_id)
    assert removed and not admin_mgr.is_admin(test_admin_id), "Администратор должен быть успешно удален!"
    print("  ✅ Добавление, проверка и удаление администраторов работают корректно!")

    # 7. Тест форматирования сообщений
    print("\n[ТЕСТ 7] Проверка форматирования сообщений Telegram:")
    msg_class_sched = parser.format_schedule_for_telegram("8K", day_index=3, subgroup_filter=filter_boys)
    print("--- Сообщение: Расписание 8K на четверг с фильтром мальчиков ---")
    print(msg_class_sched)
    assert "*4.*" in msg_class_sched, "В тексте сообщения на четверг должен присутствовать 4 урок!"

    # 8. Тест диапазона уроков в заменах
    print("\n[ТЕСТ 8] Обработка диапазонов уроков в заменах ('3 - 4', '(7)'):")
    test_subst_data = {
        "classes": {
            "8K": [
                {"period": "3 - 4", "info": "Замена сдвоенного урока", "type": "change"},
                {"period": "(7)", "info": "Отмена 7 урока", "type": "remove"}
            ]
        }
    }
    test_date_key = "2026-10-22"
    parser._substitutions_cache[test_date_key] = {
        "timestamp": 9999999999,
        "data": test_subst_data["classes"],
        "date": test_date_key
    }
    comb = parser.get_day_schedule_with_substitutions("8K", target_date=date(2026, 10, 22))
    p_map = {l["period"]: l for l in comb["lessons"]}
    assert len(p_map[3]["substitutions"]) == 1, "Замена '3 - 4' должна быть прикреплена к 3 уроку!"
    assert len(p_map[4]["substitutions"]) == 1, "Замена '3 - 4' должна быть прикреплена к 4 уроку!"
    assert len(p_map[7]["substitutions"]) == 1, "Замена '(7)' должна быть прикреплена к 7 уроку!"
    print("  ✅ Диапазоны уроков в заменах ('3 - 4') успешно распределяются по соответствующим урокам!")

    # 9. Тест кабинетов преподавателей (в каких кабинетах преподает)
    print("\n[ТЕСТ 9] Кабинеты преподавателей (основной, все кабинеты, по дням и классам):")
    t_rooms = parser.get_teacher_classrooms("Kulakova Dariya")
    print(f"  Kulakova Dariya -> Основной кабинет: {t_rooms.get('primary_room')}, Все кабинеты: {t_rooms.get('all_rooms')}")
    assert t_rooms.get("primary_room") == "106", "Основной кабинет Kulakova Dariya должен быть 106!"
    assert "002" in t_rooms.get("all_rooms", []), "Кабинет 002 должен быть в списке кабинетов Kulakova Dariya!"
    assert len(t_rooms.get("classes_by_room", {})) > 0, "Должна присутствовать привязка классов к кабинетам!"

    msg_teacher_rooms = parser.format_teacher_classrooms_for_telegram("Kulakova Dariya")
    assert "106" in msg_teacher_rooms and "002" in msg_teacher_rooms, "В тексте сообщения должны быть указаны кабинеты!"
    print("  ✅ Кабинеты преподавателей сформированы и отформатированы корректно!")

    # 10. Тест сохранения пользователей (97 пользователей) и уменьшения счётчика при выходе
    print("\n[ТЕСТ 10] Сохранение 97 пользователей и уменьшение счётчика при выходе/блокировке:")
    from bot import user_prefs
    initial_active = user_prefs.get_active_users_count()
    print(f"  Начальное количество активных пользователей: {initial_active}")
    assert initial_active == 97, f"Ожидалось 97 активных пользователей, получено {initial_active}!"

    # Пользователь выходит/блокирует бота -> счетчик уменьшается до 96
    mock_user_id = 777000111
    user_prefs.set_user_inactive(mock_user_id, reason="test_block")
    count_after_leave = user_prefs.get_active_users_count()
    print(f"  После выхода пользователя {mock_user_id}: {count_after_leave}")
    assert count_after_leave == 96, f"Ожидалось 96 пользователей после выхода, получено {count_after_leave}!"

    # Пользователь возвращается -> счетчик возвращается к 97
    user_prefs.set_user_active(mock_user_id)
    count_after_return = user_prefs.get_active_users_count()
    print(f"  После возвращения пользователя {mock_user_id}: {count_after_return}")
    assert count_after_return == 97, f"Ожидалось 97 пользователей после возвращения, получено {count_after_return}!"

    # Очистка тестового пользователя
    user_prefs._data.pop(str(mock_user_id), None)
    user_prefs.set_target_count(97)
    print("  ✅ Сохранение 97 пользователей и точное изменение счётчика протестированы успешно!")

    # 11. Тест поддержки групповых чатов и привязки класса к беседе
    print("\n[ТЕСТ 11] Поддержка групповых чатов (привязка класса к беседе):")
    from bot import chat_prefs, get_group_chat_keyboard
    test_group_id = -100987654321
    chat_prefs.set_class(test_group_id, "10A", title="Тестовая беседа 10A")
    assert chat_prefs.get_class(test_group_id) == "10A", "К чату должен быть привязан класс 10A!"
    kb = get_group_chat_keyboard(test_group_id)
    assert any("10A" in btn.text for row in kb.inline_keyboard for btn in row), "Кнопка клавиатуры группы должна содержать 10A!"
    chat_prefs.remove_chat(test_group_id)
    print("  ✅ Групповые чаты и отдельная панель вызова работают корректно!")

    # 12. Тест универсального школьного поиска
    print("\n[ТЕСТ 12] Универсальный школьный поиск (учителя, кабинеты, классы):")
    s_teacher = parser.search_school("Кулакова")
    assert "Kulakova Dariya" in s_teacher["teachers"], "Поиск должен найти Kulakova Dariya!"
    s_room = parser.search_school("204")
    assert "204" in s_room["classrooms"], "Поиск должен найти кабинет 204!"
    s_class = parser.search_school("8K")
    assert "8K" in s_class["classes"], "Поиск должен найти класс 8K!"
    print("  ✅ Универсальный поиск успешно находит учителей, кабинеты и классы!")

    print("=" * 65)
    print("🎉 ВСЕ ТЕСТЫ БОТА, ПАРСЕРА, ПОДГРУПП И АДМИН-ПАНЕЛИ УСПЕШНО ПРОЙДЕНЫ!")
    print("=" * 65)

if __name__ == "__main__":
    run_tests()
