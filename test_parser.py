import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import os
from datetime import date, timedelta
from parser import MJGParser, format_group_label, should_show_card
from bot import AdminManager, UserPreferences

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
        ("11с", "11C")
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
    print("  ✅ Расписание учителей с классами и кабинетами сформировано корректно!")

    # 6. Тест панели администратора и управления администраторами
    print("\n[ТЕСТ 6] Управление администраторами (AdminManager):")
    admin_mgr = AdminManager()
    assert admin_mgr.is_admin(1637426603), "Начальный админ 1637426603 должен быть в списке!"

    test_admin_id = 987654321
    added = admin_mgr.add_admin(test_admin_id)
    assert admin_mgr.is_admin(test_admin_id), "Новый администратор должен быть добавлен!"
    removed = admin_mgr.remove_admin(test_admin_id)
    assert not admin_mgr.is_admin(test_admin_id), "Администратор должен быть успешно удален!"
    print("  ✅ Добавление, проверка и удаление администраторов работают корректно!")

    # 7. Тест форматирования сообщений
    print("\n[ТЕСТ 7] Проверка форматирования сообщений Telegram:")
    msg_class_sched = parser.format_schedule_for_telegram("8K", day_index=0, subgroup_filter=filter_boys)
    print("--- Сообщение: Расписание 8K с фильтром мальчиков ---")
    print(msg_class_sched[:180] + "...\n")

    msg_teacher_sched = parser.format_teacher_schedule_for_telegram("Kulakova Dariya", day_index=0)
    print("--- Сообщение: Расписание учителя с классами и кабинетами ---")
    print(msg_teacher_sched[:220] + "...\n")

    print("=" * 65)
    print("🎉 ВСЕ ТЕСТЫ БОТА, ПАРСЕРА, ПОДГРУПП И АДМИН-ПАНЕЛИ УСПЕШНО ПРОЙДЕНЫ!")
    print("=" * 65)

if __name__ == "__main__":
    run_tests()
