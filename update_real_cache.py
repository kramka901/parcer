import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def normalize_class_name(class_name: str) -> str:
    if not class_name:
        return ""
    name = class_name.strip().upper()
    ru_to_en = {
        "А": "A", "Б": "B", "В": "B", "С": "C", "Е": "E",
        "К": "K", "Л": "L", "П": "P"
    }
    for r, e in ru_to_en.items():
        name = name.replace(r, e)
    name = re.sub(r'(\d+)\s+([A-ZА-ЯЁ])', r'\1\2', name)
    if re.match(r'^\d+-[A-ZА-ЯЁ]$', name):
        name = name.replace('-', '')
    return name

def build_cache_from_raw():
    raw_path = BASE_DIR / "real_raw_tt.json"
    if not raw_path.exists():
        print("real_raw_tt.json not found!")
        return

    with open(raw_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # Загружаем учителей из teachers.json
    teachers_file = BASE_DIR / "teachers.json"
    t_list = []
    if teachers_file.exists():
        with open(teachers_file, "r", encoding="utf-8") as f:
            t_list = json.load(f)
    t_by_surname = {t["surname"].lower(): t for t in t_list}

    dbi = raw.get("r", {}).get("dbiAccessorRes", {})
    raw_tables = {t["id"]: {row["id"]: row for row in t.get("data_rows", [])} for t in dbi.get("tables", [])}

    classes_map = raw_tables.get("classes", {})
    subjects_map = raw_tables.get("subjects", {})
    teachers_map = raw_tables.get("teachers", {})
    classrooms_map = raw_tables.get("classrooms", {})
    periods_map = raw_tables.get("periods", {})
    lessons_map = raw_tables.get("lessons", {})
    cards_map = raw_tables.get("cards", {})
    groups_map = raw_tables.get("groups", {})

    periods_dict = {}
    for pid, p in periods_map.items():
        periods_dict[str(p.get("period", pid))] = {
            "period": int(p.get("period", pid)),
            "starttime": p.get("starttime", ""),
            "endtime": p.get("endtime", "")
        }

    classes_dict = {}
    for cid, c in classes_map.items():
        name = c.get("name", "").strip()
        norm_name = normalize_class_name(name)
        if norm_name:
            classes_dict[norm_name] = {
                "id": cid,
                "name": name,
                "short": c.get("short", name)
            }

    teachers_dict = {}
    teachers_id_to_name = {}

    for tid, t in teachers_map.items():
        short = t.get("short", "").strip()
        match = t_by_surname.get(short.lower())
        if match:
            full_name = match["full_name"]
            surname = match["surname"]
            firstname = match["firstname"]
        else:
            surname = short or tid
            firstname = ""
            full_name = surname

        teachers_dict[full_name] = {
            "id": tid,
            "name": full_name,
            "surname": surname,
            "firstname": firstname,
            "short": short or surname
        }
        teachers_id_to_name[tid] = full_name

    # Добавляем остальных учителей из teachers.json, которых нет в raw
    for t in t_list:
        fname = t["full_name"]
        if fname not in teachers_dict:
            teachers_dict[fname] = {
                "id": fname,
                "name": fname,
                "surname": t.get("surname", ""),
                "firstname": t.get("firstname", ""),
                "short": t.get("surname", "")
            }

    schedule_by_class = {}
    for cid, c in classes_map.items():
        c_name = normalize_class_name(c.get("name", ""))
        if not c_name:
            continue

        schedule_by_class[c_name] = {d: {} for d in range(5)}
        c_lessons = {lid: l for lid, l in lessons_map.items() if cid in l.get("classids", [])}

        for card in cards_map.values():
            lid = card.get("lessonid")
            if lid not in c_lessons:
                continue

            lesson = c_lessons[lid]
            subject_id = lesson.get("subjectid")
            subject_name = subjects_map.get(subject_id, {}).get("name", "Предмет")

            teacher_names = []
            for tid in lesson.get("teacherids", []):
                t_name = teachers_id_to_name.get(tid, tid)
                teacher_names.append(t_name)

            room_names = []
            for rid in card.get("classroomids", []):
                r = classrooms_map.get(rid, {})
                room_names.append(r.get("short") or r.get("name", rid))

            # Извлечение подгрупп (Poisid, Tüdrukud, Rühm 1, Rühm 2 и т.д.)
            gnames = []
            for gid in lesson.get("groupids", []):
                g_obj = groups_map.get(gid, {})
                gn = g_obj.get("name", "").strip() if isinstance(g_obj, dict) else ""
                if gn and gn != "Terve klass":
                    gnames.append(gn)
            if not gnames and lesson.get("groupnames"):
                for gn in lesson.get("groupnames", []):
                    if gn and gn.strip() and gn.strip() != "Terve klass":
                        gnames.append(gn.strip())
            deduped_gnames = list(dict.fromkeys(gnames))
            group_str = ", ".join(deduped_gnames)

            base_period_num = int(card.get("period", 1))
            duration = int(lesson.get("durationperiods") or 1)
            days_str = card.get("days", "")

            for offset in range(duration):
                period_num = base_period_num + offset
                period_info = periods_dict.get(str(period_num), {})
                starttime = period_info.get("starttime", "")
                endtime = period_info.get("endtime", "")

                card_data = {
                    "period": period_num,
                    "time": f"{starttime}-{endtime}" if starttime else "",
                    "subject": subject_name,
                    "teachers": teacher_names,
                    "classrooms": room_names,
                    "group": group_str
                }

                for d_idx, char in enumerate(days_str):
                    if char == "1" and d_idx < 5:
                        schedule_by_class[c_name][d_idx].setdefault(period_num, []).append(card_data)

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

    out_file = BASE_DIR / "timetable_cache.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(timetable_cache, f, ensure_ascii=False, indent=2)

    print(f"Successfully saved updated timetable into {out_file}!")
    print(f"Total classes: {len(classes_dict)}, Total teachers: {len(teachers_dict)}")

if __name__ == "__main__":
    build_cache_from_raw()
