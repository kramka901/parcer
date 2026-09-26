import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

teachers_path = BASE_DIR / "teachers.json"
if teachers_path.exists():
    with open(teachers_path, "r", encoding="utf-8") as f:
        teachers_list = json.load(f)
else:
    teachers_list = []

classes_list = [
    "1A", "1E", "1K", "2A", "2E", "2K", "3A", "3E", "3K", "4A", "4E", "4K", "4-5E",
    "5A", "5E", "5K", "5L", "6A", "6B", "6E", "6K", "7A", "7B", "7E", "7K", "7-8E",
    "8A", "8B", "8E", "8K", "9A", "9B", "9E", "9K", "10A", "10B", "11A", "11B", "11C",
    "12A", "12B", "12C"
]

periods_dict = {
    "1": {"period": 1, "starttime": "08:30", "endtime": "09:15"},
    "2": {"period": 2, "starttime": "09:25", "endtime": "10:10"},
    "3": {"period": 3, "starttime": "10:35", "endtime": "11:20"},
    "4": {"period": 4, "starttime": "11:45", "endtime": "12:30"},
    "5": {"period": 5, "starttime": "12:55", "endtime": "13:40"},
    "6": {"period": 6, "starttime": "13:50", "endtime": "14:35"},
    "7": {"period": 7, "starttime": "14:45", "endtime": "15:30"},
    "8": {"period": 8, "starttime": "15:40", "endtime": "16:25"}
}

classes_dict = {c: {"id": c, "name": c, "short": c} for c in classes_list}
teachers_dict = {
    t["full_name"]: {
        "id": t["full_name"],
        "name": t["full_name"],
        "surname": t["surname"],
        "firstname": t["firstname"],
        "short": t["surname"]
    }
    for t in teachers_list
}

# Предметы и кабинеты
sample_subjects = [
    ("Matemaatika", ["Romanova Žanna", "Maksimova Margarita", "Savitševa Svetlana"], "205"),
    ("Eesti keel teise keelena", ["Kulakova Dariya", "Lind Valeri", "Leonova Julia"], "301"),
    ("Inglise keel", ["Cojuhari Veronika", "Kovõlina Inna"], "107"),
    ("Ajalugu", ["Ahv Albert"], "210"),
    ("Bioloogia", ["Štšetina Tatjana"], "312"),
    ("Geograafia", ["Kuznetsov Pavel", "Vannik Marina"], "315"),
    ("Kehaline kasvatus", ["Voskoboinikov Vladimir", "Trjapitškina Jekaterina", "Kivimets Raiko"], "Võimla"),
    ("Füüsika", ["Krõlatov Pavel"], "208"),
    ("Keemia", ["Syrtlanova Lilia"], "209"),
    ("Kirjandus", ["Einasto Natalie", "Nikkel Rita"], "304"),
    ("Informaatika", ["Jasjukevitš Oksana"], "Arvutiklass")
]

schedule_by_class = {}
for c in classes_list:
    schedule_by_class[c] = {d: {} for d in range(5)}
    digits = "".join([ch for ch in c.split("-")[0] if ch.isdigit()])
    grade = int(digits) if digits else 1
    num_lessons = 5 if grade <= 4 else 6 if grade <= 9 else 7
    for d in range(5):
        for p in range(1, num_lessons + 1):
            subj, t_pool, room = sample_subjects[(p + d + grade) % len(sample_subjects)]
            t_name = t_pool[p % len(t_pool)]
            period_info = periods_dict[str(p)]
            time_str = f"{period_info['starttime']}-{period_info['endtime']}"
            schedule_by_class[c][d][p] = [{
                "period": p,
                "time": time_str,
                "subject": subj,
                "teachers": [t_name],
                "classrooms": [room],
                "group": ""
            }]

timetable_cache = {
    "periods": periods_dict,
    "classes": classes_dict,
    "teachers": teachers_dict,
    "schedule": schedule_by_class,
    "meta": {
        "default_num": "37",
        "timetables": [],
        "year": 2026
    }
}

output_path = BASE_DIR / "timetable_cache.json"
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(timetable_cache, f, ensure_ascii=False, indent=2)

print(f"Generated {output_path} with {len(classes_dict)} classes and {len(teachers_dict)} teachers!")
