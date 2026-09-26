import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import os
from datetime import date, timedelta
from parser import MJGParser

def run_tests():
    print("=" * 65)
    print("🚀 НАЧАЛО ТЕСТИРОВАНИЯ ПАРСЕРА И МОДУЛЕЙ MJG (www.mjg.ee)")
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
    sample_classes = list(classes.keys())[:10]
    print(f"  Примеры классов: {', '.join(sample_classes)}")
    assert len(classes) >= 30, "В школе MJG должно быть не менее 30 классов!"
    assert "10A" in classes, "Класс 10A должен присутствовать!"
    print("  ✅ Список классов успешно получен!")

    # 3. Тест получения преподавателей и поиска по фамилии
    print("\n[ТЕСТ 3] Список учителей и поиск по имени/транслиту:")
    teachers = parser.get_teachers()
    print(f"  Всего преподавателей в базе: {len(teachers)}")
    assert len(teachers) > 0, "Список преподавателей не должен быть пустым!"

    t1 = parser.normalize_teacher_name("Кулакова")
    print(f"  Поиск 'Кулакова' -> '{t1}'")
    assert t1 == "Kulakova Dariya", f"Ожидалось Kulakova Dariya, получено {t1}"

    t2 = parser.normalize_teacher_name("Романова")
    print(f"  Поиск 'Романова' -> '{t2}'")
    assert t2 == "Romanova Žanna", f"Ожидалось Romanova Žanna, получено {t2}"

    t3 = parser.normalize_teacher_name("Ahv")
    print(f"  Поиск 'Ahv' -> '{t3}'")
    assert t3 == "Ahv Albert", f"Ожидалось Ahv Albert, получено {t3}"
    print("  ✅ Поиск преподавателей на кириллице и латинице работает корректно!")

    # 4. Тест расписания для класса 10A
    print("\n[ТЕСТ 4] Расписание для класса 10A на неделю:")
    sched_10a = parser.get_class_schedule("10A")
    assert sched_10a is not None, "Расписание 10A не должно быть пустым!"
    print(f"  Дней с уроками: {len(sched_10a)}")
    day0 = sched_10a.get(0, {})
    print(f"  Понедельник (уроков: {len(day0)})")
    for p, cards in list(day0.items())[:2]:
        for c in cards:
            print(f"    Урок {p} ({c['time']}): {c['subject']} {c['teachers']} [каб. {', '.join(c['classrooms'])}]")
    print("  ✅ Базовое расписание класса 10A загружено!")

    # 5. Тест расписания для преподавателя
    print("\n[ТЕСТ 5] Расписание преподавателя Kulakova Dariya:")
    sched_kulakova = parser.get_teacher_schedule("Kulakova Dariya")
    total_teacher_lessons = sum(len(lessons) for d in sched_kulakova.values() for lessons in d.values())
    print(f"  Всего уроков у преподавателя на неделе: {total_teacher_lessons}")
    assert total_teacher_lessons > 0, "У преподавателя должны быть уроки в расписании!"
    print("  ✅ Расписание преподавателя успешно сформировано!")

    # 6. Тест замен уроков
    print("\n[ТЕСТ 6] Получение замен уроков:")
    subst_today = parser.get_substitutions()
    classes_with_subst = subst_today.get("classes", {})
    subst_date = subst_today.get("date")
    print(f"  Дата замен: {subst_date}")
    print(f"  Классов с заменами: {len(classes_with_subst)}")

    # Замены для конкретного класса
    subst_10a = parser.get_substitutions(class_name="10A")
    print(f"  Замен для класса 10A: {len(subst_10a.get('substitutions', []))}")
    for it in subst_10a.get('substitutions', [])[:2]:
        print(f"    [{it['period']} урок]: {it['info']}")

    # Замены для учителя
    t_subst = parser.get_teacher_substitutions("Kulakova Dariya")
    print(f"  Замен для учителя Kulakova Dariya: {len(t_subst.get('substitutions', []))}")
    for it in t_subst.get('substitutions', [])[:2]:
        print(f"    [{it['class_name']}, {it['period']} урок, роль: {it['teacher_role']}]: {it['info']}")
    print("  ✅ Замены для классов и учителей успешно обработаны!")

    # 7. Тест форматирования сообщений для Telegram
    print("\n[ТЕСТ 7] Проверка форматирования сообщений Telegram:")
    msg_class_sched = parser.format_schedule_for_telegram("10A", day_index=0)
    print("--- Сообщение: Расписание 10A на понедельник ---")
    print(msg_class_sched[:180] + "...\n")

    msg_teacher_subst = parser.format_teacher_substitutions_for_telegram("Kulakova Dariya")
    print("--- Сообщение: Замены преподавателя ---")
    print(msg_teacher_subst[:220] + "...\n")

    msg_combined = parser.format_day_combined_for_telegram("10A", target_date=date.today())
    print("--- Сообщение: Объединенный день для класса ---")
    print(msg_combined[:200] + "...\n")

    print("=" * 65)
    print("🎉 ВСЕ ТЕСТЫ ПАРСЕРА И МОДУЛЕЙ УСПЕШНО ПРОЙДЕНЫ!")
    print("=" * 65)

if __name__ == "__main__":
    run_tests()
