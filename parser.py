import re
import sys
import json
import time
import logging
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

from config import (
    EDUPAGE_BASE_URL,
    SUBSTITUTION_URL,
    TIMETABLE_URL,
    TT_VIEWER_RPC_URL,
    REGULAR_TT_RPC_URL,
    TIMETABLE_CACHE_TTL,
    SUBSTITUTION_CACHE_TTL,
    TIMETABLE_CACHE_FILE,
    SUBSTITUTIONS_CACHE_FILE,
    TEACHERS_DATA_FILE,
    EDUPAGE_PROXY,
    REQUEST_TIMEOUT,
    WEEKDAYS_RU,
    WEEKDAYS_SHORT_RU
)

logger = logging.getLogger(__name__)


class MJGParser:
    """
    Парсер расписания и замен для Таллиннской Мустйыэской гимназии
    (www.mjg.ee / mustjoe.edupage.org)
    Поддерживает работу с классами и учителями, устойчивое дисковое кэширование,
    прокси и автономный режим.
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json"
        })

        if EDUPAGE_PROXY:
            logger.info(f"Используется прокси для EduPage: {EDUPAGE_PROXY}")
            self.session.proxies.update({
                "http": EDUPAGE_PROXY,
                "https": EDUPAGE_PROXY
            })

        # Кэш данных
        self._classes_cache: Optional[Dict[str, Any]] = None
        self._teachers_cache: Optional[Dict[str, Any]] = None
        self._timetable_cache: Optional[Dict[str, Any]] = None
        self._timetable_cache_time: float = 0
        self._substitutions_cache: Dict[str, Dict[str, Any]] = {}

        # Загрузка предварительного дискового кэша
        self._load_disk_cache()

    # ================= Работа с дисковым кэшем =================

    def _load_disk_cache(self):
        """Загрузка сохраненного расписания и замен с диска при старте"""
        # 1. Расписание
        if TIMETABLE_CACHE_FILE.exists():
            try:
                with open(TIMETABLE_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._timetable_cache = data
                    self._classes_cache = data.get("classes", {})
                    self._teachers_cache = data.get("teachers", {})
                    self._timetable_cache_time = time.time()
                    logger.info("Загружено расписание из дискового кэша.")
            except Exception as e:
                logger.warning(f"Ошибка загрузки timetable_cache.json: {e}")

        # 2. Учителя (если есть отдельный файл)
        if not self._teachers_cache and TEACHERS_DATA_FILE.exists():
            try:
                with open(TEACHERS_DATA_FILE, "r", encoding="utf-8") as f:
                    t_list = json.load(f)
                    self._teachers_cache = {
                        t["full_name"]: {
                            "id": t["full_name"],
                            "name": t["full_name"],
                            "surname": t["surname"],
                            "firstname": t["firstname"],
                            "short": t["surname"]
                        }
                        for t in t_list
                    }
            except Exception as e:
                logger.warning(f"Ошибка загрузки teachers.json: {e}")

        # 3. Замены
        if SUBSTITUTIONS_CACHE_FILE.exists():
            try:
                with open(SUBSTITUTIONS_CACHE_FILE, "r", encoding="utf-8") as f:
                    self._substitutions_cache = json.load(f)
                    logger.info(f"Загружено замен из дискового кэша: {len(self._substitutions_cache)} дат.")
            except Exception as e:
                logger.warning(f"Ошибка загрузки substitutions_cache.json: {e}")

    def _save_timetable_cache(self):
        """Сохранение базового расписания на диск"""
        if not self._timetable_cache:
            return
        try:
            with open(TIMETABLE_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._timetable_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Не удалось сохранить timetable_cache.json: {e}")

    def _save_substitutions_cache(self):
        """Сохранение замен на диск"""
        try:
            with open(SUBSTITUTIONS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._substitutions_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Не удалось сохранить substitutions_cache.json: {e}")

    # ================= Нормализация =================

    @staticmethod
    def normalize_class_name(class_name: str) -> str:
        """Приводит название класса к каноническому виду (например, '10 a' -> '10A', '7-8e' -> '7-8E')"""
        if not class_name:
            return ""
        name = class_name.strip().upper()
        # Замена русских букв на латиницу для единообразия в названиях классов
        ru_to_en = {"А": "A", "В": "B", "С": "C", "Е": "E", "К": "K"}
        for r, e in ru_to_en.items():
            name = name.replace(r, e)
        # Убираем пробелы между цифрами и буквами: "10 A" -> "10A"
        name = re.sub(r'(\d+)\s+([A-ZА-ЯЁ])', r'\1\2', name)
        # Убираем дефис, если это не диапазон классов: "10-A" -> "10A"
        if re.match(r'^\d+-[A-ZА-ЯЁ]$', name):
            name = name.replace('-', '')
        return name

    @staticmethod
    def transliterate_ru_to_et(text: str) -> str:
        """Транслитерация с русского на эстонскую/латинскую латиницу для поиска преподавателей"""
        mapping = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e',
            'ж': 'ž', 'з': 'z', 'и': 'i', 'й': 'j', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'tš', 'ш': 'š', 'щ': 'štš',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'ju', 'я': 'ja'
        }
        res = []
        for ch in text.lower():
            res.append(mapping.get(ch, ch))
        return "".join(res)

    def normalize_teacher_name(self, query: str) -> Optional[str]:
        """
        Ищет преподавателя по частичному совпадению фамилии или имени (на латинице или кириллице).
        Возвращает полное имя преподавателя (например, 'Kulakova Dariya') или None.
        """
        if not query:
            return None
        q = query.strip().lower()
        q_translit = self.transliterate_ru_to_et(q)
        teachers = self.get_teachers()

        # 1. Точное совпадение
        for t_name in teachers.keys():
            t_low = t_name.lower()
            if t_low in (q, q_translit):
                return t_name

        # 2. Совпадение по фамилии
        for t_name, info in teachers.items():
            surname = info.get("surname", "").lower()
            if surname and surname in (q, q_translit):
                return t_name

        # 3. Фамилия начинается с запроса или запрос входит в фамилию/имя
        for t_name, info in teachers.items():
            surname = info.get("surname", "").lower()
            if surname and (surname.startswith(q) or surname.startswith(q_translit)):
                return t_name

        for t_name in teachers.keys():
            t_low = t_name.lower()
            # Проверяем и с мягкими знаками / без (например zanna vs žanna)
            t_simplified = t_low.replace('ž', 'z').replace('š', 's').replace('õ', 'o').replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')
            q_simplified = q_translit.replace('ž', 'z').replace('š', 's').replace('õ', 'o').replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')
            if q in t_low or q_translit in t_low or q_simplified in t_simplified:
                return t_name

        return None

    # ================= Работа с сетью EduPage =================

    def _get_gsec_hash(self) -> str:
        """Получает gsechash со страницы расписания"""
        try:
            resp = self.session.get(TIMETABLE_URL, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                match = re.search(r'ASC\.gsechash\s*=\s*"([^"]+)"', resp.text)
                if match:
                    return match.group(1)
        except Exception as e:
            logger.warning(f"Не удалось получить gsechash: {e}")
        return "00000000"

    def get_timetable_meta(self) -> Dict[str, Any]:
        """Получает метаданные расписания (номер актуального расписания tt_num и учебный год)"""
        current_year = date.today().year
        gsechash = self._get_gsec_hash()

        payload = {
            "__args": [None, current_year],
            "__gsh": gsechash
        }

        try:
            resp = self.session.post(TT_VIEWER_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json().get("r", {})
                regular = data.get("regular", {})
                default_num = str(regular.get("default_num", "37"))
                timetables = regular.get("timetables", [])
                return {
                    "default_num": default_num,
                    "timetables": timetables,
                    "year": current_year
                }
        except Exception as e:
            logger.warning(f"Не удалось получить метаданные расписания онлайн: {e}")

        # Fallback
        return {
            "default_num": "37",
            "timetables": [],
            "year": current_year
        }

    def get_regular_timetable(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        Загружает и парсит базовое расписание уроков школы на учебный период.
        При ошибках сети безопасно возвращает локальный кэш.
        """
        now = time.time()
        if (
            not force_refresh
            and self._timetable_cache is not None
            and (now - self._timetable_cache_time) < TIMETABLE_CACHE_TTL
        ):
            return self._timetable_cache

        try:
            meta = self.get_timetable_meta()
            tt_num = meta["default_num"]
            gsechash = self._get_gsec_hash()

            needed_part = {
                "students": ["classid"],
                "classes": ["classroomid"],
                "lessons": ["studentids", "groupnames"],
                "groups": ["classid", "divisionid"],
                "divisions": ["groupids"],
                "days": ["__name"],
                "weeks": ["__name"],
                "terms": ["__name"],
                "classroomsupervisions": ["weeks", "terms"],
                "periods": ["period"]
            }

            payload = {
                "__args": [needed_part, tt_num],
                "__gsh": gsechash
            }

            resp = self.session.post(REGULAR_TT_RPC_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                res_json = resp.json()
                if "r" in res_json and "dbiAccessorRes" in res_json["r"]:
                    dbi = res_json.get("r", {}).get("dbiAccessorRes", {})
                    raw_tables = {t["id"]: {row["id"]: row for row in t.get("data_rows", [])} for t in dbi.get("tables", [])}

                    classes_map = raw_tables.get("classes", {})
                    subjects_map = raw_tables.get("subjects", {})
                    teachers_map = raw_tables.get("teachers", {})
                    classrooms_map = raw_tables.get("classrooms", {})
                    periods_map = raw_tables.get("periods", {})
                    lessons_map = raw_tables.get("lessons", {})
                    cards_map = raw_tables.get("cards", {})

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
                        norm_name = self.normalize_class_name(name)
                        if norm_name:
                            classes_dict[norm_name] = {
                                "id": cid,
                                "name": name,
                                "short": c.get("short", name)
                            }

                    teachers_dict = {}
                    for tid, t in teachers_map.items():
                        t_name = t.get("name", "").strip()
                        surname = t.get("lastname", "").strip() or (t_name.split()[0] if t_name else "")
                        firstname = t.get("firstname", "").strip() or (t_name.split()[1] if len(t_name.split()) > 1 else "")
                        full_name = f"{surname} {firstname}".strip() or t_name
                        if full_name:
                            teachers_dict[full_name] = {
                                "id": tid,
                                "name": full_name,
                                "surname": surname,
                                "firstname": firstname,
                                "short": t.get("short", surname)
                            }

                    schedule_by_class: Dict[str, Dict[int, Dict[int, List[Dict[str, Any]]]]] = {}

                    for cid, c in classes_map.items():
                        c_name = self.normalize_class_name(c.get("name", ""))
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
                                t = teachers_map.get(tid, {})
                                full_t = f"{t.get('lastname', '')} {t.get('firstname', '')}".strip() or t.get("name", tid)
                                teacher_names.append(full_t)

                            room_names = []
                            for rid in card.get("classroomids", []):
                                r = classrooms_map.get(rid, {})
                                room_names.append(r.get("short") or r.get("name", rid))

                            period_num = int(card.get("period", 1))
                            period_info = periods_dict.get(str(period_num), {})
                            starttime = period_info.get("starttime", "")
                            endtime = period_info.get("endtime", "")

                            card_data = {
                                "period": period_num,
                                "time": f"{starttime}-{endtime}" if starttime else "",
                                "subject": subject_name,
                                "teachers": teacher_names,
                                "classrooms": room_names,
                                "group": lesson.get("groupnames", [""])[0] if lesson.get("groupnames") else ""
                            }

                            days_str = card.get("days", "")
                            for d_idx, char in enumerate(days_str):
                                if char == "1" and d_idx < 5:
                                    schedule_by_class[c_name][d_idx].setdefault(period_num, []).append(card_data)

                    self._classes_cache = classes_dict
                    self._teachers_cache = teachers_dict
                    self._timetable_cache = {
                        "periods": periods_dict,
                        "classes": classes_dict,
                        "teachers": teachers_dict,
                        "schedule": schedule_by_class,
                        "meta": meta
                    }
                    self._timetable_cache_time = now
                    self._save_timetable_cache()
                    return self._timetable_cache

        except Exception as e:
            logger.warning(f"Ошибка загрузки расписания онлайн ({e}), используется локальный кэш.")

        # Если онлайн не удался, используем сохраненный кэш
        if self._timetable_cache:
            return self._timetable_cache

        # Экстренный пустой fallback
        return {
            "periods": {},
            "classes": {},
            "teachers": {},
            "schedule": {},
            "meta": {}
        }

    # ================= Получение классов и учителей =================

    def get_classes(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Возвращает словарь всех доступных классов школы"""
        if self._classes_cache is None or force_refresh:
            self.get_regular_timetable(force_refresh=force_refresh)
        return self._classes_cache or {}

    def get_teachers(self, force_refresh: bool = False) -> Dict[str, Dict[str, Any]]:
        """Возвращает словарь всех преподавателей школы, отсортированных по фамилии"""
        if self._teachers_cache is None or force_refresh:
            self.get_regular_timetable(force_refresh=force_refresh)
        if self._teachers_cache:
            sorted_teachers = dict(sorted(self._teachers_cache.items(), key=lambda x: x[1].get("surname", x[0])))
            return sorted_teachers
        return {}

    # ================= Расписание для классов =================

    def get_class_schedule(self, class_name: str, day_index: Optional[int] = None) -> Optional[Dict[int, Dict[int, List[Dict[str, Any]]]]]:
        """
        Возвращает расписание для конкретного класса.
        day_index: 0=Пн, 1=Вт, 2=Ср, 3=Чт, 4=Пт, или None для всей недели.
        """
        tt = self.get_regular_timetable()
        norm_name = self.normalize_class_name(class_name)

        schedule_by_class = tt.get("schedule", {})
        if norm_name not in schedule_by_class:
            return None

        class_tt = schedule_by_class[norm_name]
        # Приводим строковые ключи из JSON к целым числам (дни 0..4, уроки 1..8)
        normalized_tt: Dict[int, Dict[int, List[Dict[str, Any]]]] = {}
        for d_key, d_val in class_tt.items():
            try:
                d_int = int(d_key)
            except ValueError:
                continue
            normalized_tt[d_int] = {}
            if isinstance(d_val, dict):
                for p_key, cards in d_val.items():
                    try:
                        p_int = int(p_key)
                    except ValueError:
                        continue
                    normalized_tt[d_int][p_int] = cards

        if day_index is not None:
            return {day_index: normalized_tt.get(day_index, {})}
        return normalized_tt

    # ================= Расписание для учителей =================

    def get_teacher_schedule(self, teacher_name: str, day_index: Optional[int] = None) -> Dict[int, Dict[int, List[Dict[str, Any]]]]:
        """
        Формирует расписание уроков конкретного преподавателя на неделю по всем классам.
        schedule[day_idx][period_num] = list of lessons (с указанием класса и кабинета)
        """
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        tt = self.get_regular_timetable()
        schedule_by_class = tt.get("schedule", {})

        teacher_schedule: Dict[int, Dict[int, List[Dict[str, Any]]]] = {d: {} for d in range(5)}

        for c_name, class_days in schedule_by_class.items():
            for d_key, day_lessons in class_days.items():
                try:
                    d_int = int(d_key)
                except ValueError:
                    continue
                if d_int not in teacher_schedule:
                    continue

                if isinstance(day_lessons, dict):
                    for p_key, cards in day_lessons.items():
                        try:
                            p_int = int(p_key)
                        except ValueError:
                            continue
                        for card in cards:
                            card_teachers = card.get("teachers", [])
                            if any(norm_t.lower() in t.lower() or t.lower() in norm_t.lower() for t in card_teachers):
                                lesson_copy = dict(card)
                                lesson_copy["class_name"] = c_name
                                teacher_schedule[d_int].setdefault(p_int, []).append(lesson_copy)

        if day_index is not None:
            return {day_index: teacher_schedule.get(day_index, {})}
        return teacher_schedule

    # ================= Замены (Substitutions) =================

    def get_substitutions(
        self,
        date_str: Optional[str] = None,
        class_name: Optional[str] = None,
        force_refresh: bool = False
    ) -> Dict[str, Any]:
        """
        Получает замены и отмены уроков с EduPage на указанную дату (по умолчанию сегодня).
        При отсутствии сети обращается к локальному кэшу.
        """
        if not date_str:
            date_str = date.today().strftime("%Y-%m-%d")

        now = time.time()
        cached = self._substitutions_cache.get(date_str)

        all_classes_subst: Dict[str, List[Dict[str, Any]]] = {}

        if not force_refresh and cached and (now - cached.get("timestamp", 0)) < SUBSTITUTION_CACHE_TTL:
            all_classes_subst = cached.get("data", {})
        else:
            fetched_online = False
            try:
                url = f"{SUBSTITUTION_URL}?date={date_str}"
                resp = self.session.get(url, timeout=REQUEST_TIMEOUT)
                if resp.status_code == 200:
                    match = re.search(r'React\.createElement\(type,\s*(\{.*?\})\);', resp.text, re.DOTALL)
                    if match:
                        data_json_str = match.group(1)
                        data_json_str = re.sub(r':\s*undefined', ': null', data_json_str)
                        raw_data = json.loads(data_json_str)
                        report_html = raw_data.get("report_html", "")

                        soup = BeautifulSoup(report_html, "html.parser")
                        sections = soup.find_all("div", class_="section")

                        online_subst: Dict[str, List[Dict[str, Any]]] = {}

                        for sec in sections:
                            header = sec.find("div", class_="header")
                            raw_cname = header.get_text(strip=True) if header else "Все"
                            norm_cname = self.normalize_class_name(raw_cname)

                            items = []
                            rows = sec.find_all("div", class_="row")
                            for row in rows:
                                classes = row.get("class", [])
                                change_type = "change"
                                for c in ["remove", "absent", "change"]:
                                    if c in classes:
                                        change_type = c
                                        break

                                period_el = row.find("div", class_="period")
                                info_el = row.find("div", class_="info")

                                period = period_el.get_text(strip=True) if period_el else ""
                                info = info_el.get_text(" ", strip=True) if info_el else ""

                                items.append({
                                    "period": period,
                                    "info": info,
                                    "type": change_type
                                })

                            if items:
                                online_subst[norm_cname] = items

                        all_classes_subst = online_subst
                        self._substitutions_cache[date_str] = {
                            "timestamp": now,
                            "data": all_classes_subst,
                            "date": raw_data.get("date", date_str)
                        }
                        self._save_substitutions_cache()
                        fetched_online = True

            except Exception as e:
                logger.warning(f"Не удалось загрузить замены онлайн ({e}). Пробуем локальный кэш.")

            if not fetched_online:
                if cached:
                    all_classes_subst = cached.get("data", {})
                else:
                    for k in sorted(self._substitutions_cache.keys(), reverse=True):
                        if self._substitutions_cache[k].get("data"):
                            all_classes_subst = self._substitutions_cache[k]["data"]
                            break
                self._substitutions_cache[date_str] = {
                    "timestamp": now,
                    "data": all_classes_subst,
                    "date": date_str
                }

        if class_name:
            norm_c = self.normalize_class_name(class_name)
            class_items = all_classes_subst.get(norm_c, [])
            return {
                "date": date_str,
                "class_name": norm_c,
                "substitutions": class_items,
                "total_classes_with_subst": len(all_classes_subst)
            }

        return {
            "date": date_str,
            "classes": all_classes_subst,
            "total_classes_with_subst": len(all_classes_subst)
        }

    # ================= Замены для конкретного учителя =================

    def get_teacher_substitutions(
        self,
        teacher_name: str,
        date_str: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Фильтрует замены дня, относящиеся к данному преподавателю.
        Определяет, заменяет ли учитель кого-то или заменяют его.
        """
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        subst_res = self.get_substitutions(date_str=date_str)
        all_classes_subst = subst_res.get("classes", {})

        teacher_items = []
        search_terms = [norm_t.lower()]
        parts = norm_t.split()
        if len(parts) >= 2:
            search_terms.append(parts[0].lower())  # Фамилия

        for c_name, items in all_classes_subst.items():
            for it in items:
                info = it.get("info", "")
                info_lower = info.lower()

                if any(term in info_lower for term in search_terms):
                    role = "other"
                    if "➔" in info:
                        left, right = info.split("➔", 1)
                        if any(term in left.lower() for term in search_terms):
                            role = "absent"  # Преподаватель отсутствует / заменяется
                        elif any(term in right.lower() for term in search_terms):
                            role = "substituting"  # Преподаватель ведет замену
                    elif "tühistatud" in info_lower:
                        role = "cancelled"
                    elif "õpetaja:" in info_lower:
                        role = "assigned"

                    teacher_items.append({
                        "class_name": c_name,
                        "period": it.get("period", ""),
                        "info": info,
                        "type": it.get("type", "change"),
                        "teacher_role": role
                    })

        return {
            "date": subst_res.get("date", date_str),
            "teacher_name": norm_t,
            "substitutions": teacher_items
        }

    # ================= Объединенный просмотр дня для класса =================

    def get_day_schedule_with_substitutions(
        self,
        class_name: str,
        target_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """Объединяет базовое расписание и актуальные замены для класса на день"""
        if target_date is None:
            target_date = date.today()

        norm_class = self.normalize_class_name(class_name)
        weekday = target_date.weekday()

        if weekday >= 5:
            return {
                "is_weekend": True,
                "date": target_date.strftime("%Y-%m-%d"),
                "class_name": norm_class,
                "weekday_name": "Выходной (" + ("Суббота" if weekday == 5 else "Воскресенье") + ")",
                "lessons": []
            }

        class_sched = self.get_class_schedule(norm_class, day_index=weekday)
        day_sched = class_sched.get(weekday, {}) if class_sched else {}

        date_str = target_date.strftime("%Y-%m-%d")
        subst_res = self.get_substitutions(date_str=date_str, class_name=norm_class)
        subst_items = subst_res.get("substitutions", [])

        subst_by_period: Dict[str, List[Dict[str, Any]]] = {}
        for item in subst_items:
            p_str = item["period"].strip("()")
            subst_by_period.setdefault(p_str, []).append(item)

        periods_info = self.get_regular_timetable().get("periods", {})
        combined_lessons = []

        all_periods = set(day_sched.keys())
        for p_str in subst_by_period.keys():
            if p_str.isdigit():
                all_periods.add(int(p_str))

        for p_num in sorted(all_periods):
            p_str = str(p_num)
            p_info = periods_info.get(p_str, {})
            time_str = f"{p_info.get('starttime', '')}-{p_info.get('endtime', '')}" if p_info.get("starttime") else ""

            reg_cards = day_sched.get(p_num, [])
            subst_for_p = subst_by_period.get(p_str, [])

            combined_lessons.append({
                "period": p_num,
                "time": time_str,
                "regular": reg_cards,
                "substitutions": subst_for_p
            })

        whole_day_subst = [item for item in subst_items if not item["period"].strip("()").isdigit()]

        return {
            "is_weekend": False,
            "date": date_str,
            "class_name": norm_class,
            "weekday_index": weekday,
            "weekday_name": WEEKDAYS_RU[weekday],
            "lessons": combined_lessons,
            "whole_day_substitutions": whole_day_subst,
            "has_substitutions": len(subst_items) > 0
        }

    # ================= Объединенный просмотр дня для учителя =================

    def get_teacher_day_schedule_with_substitutions(
        self,
        teacher_name: str,
        target_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """Объединяет расписание уроков учителя и замены на указанный день"""
        if target_date is None:
            target_date = date.today()

        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        weekday = target_date.weekday()

        if weekday >= 5:
            return {
                "is_weekend": True,
                "date": target_date.strftime("%Y-%m-%d"),
                "teacher_name": norm_t,
                "weekday_name": "Выходной (" + ("Суббота" if weekday == 5 else "Воскресенье") + ")",
                "lessons": []
            }

        sched = self.get_teacher_schedule(norm_t, day_index=weekday)
        day_lessons = sched.get(weekday, {}) if sched else {}

        date_str = target_date.strftime("%Y-%m-%d")
        t_subst = self.get_teacher_substitutions(norm_t, date_str=date_str)
        subst_items = t_subst.get("substitutions", [])

        subst_by_p: Dict[str, List[Dict[str, Any]]] = {}
        for s in subst_items:
            p_str = s["period"].strip("()")
            subst_by_p.setdefault(p_str, []).append(s)

        periods_info = self.get_regular_timetable().get("periods", {})
        combined = []

        all_p = set(day_lessons.keys())
        for p_str in subst_by_p.keys():
            if p_str.isdigit():
                all_p.add(int(p_str))

        for p_num in sorted(all_p):
            p_str = str(p_num)
            p_info = periods_info.get(p_str, {})
            time_str = f"{p_info.get('starttime', '')}-{p_info.get('endtime', '')}" if p_info.get("starttime") else ""

            combined.append({
                "period": p_num,
                "time": time_str,
                "regular": day_lessons.get(p_num, []),
                "substitutions": subst_by_p.get(p_str, [])
            })

        return {
            "is_weekend": False,
            "date": date_str,
            "teacher_name": norm_t,
            "weekday_index": weekday,
            "weekday_name": WEEKDAYS_RU[weekday],
            "lessons": combined,
            "has_substitutions": len(subst_items) > 0
        }

    # ================= Форматирование для Telegram (Классы) =================

    def format_schedule_for_telegram(self, class_name: str, day_index: Optional[int] = None) -> str:
        """Форматирует расписание класса для Telegram"""
        norm_class = self.normalize_class_name(class_name)
        classes = self.get_classes()
        if norm_class not in classes:
            return f"❌ Класс *{norm_class}* не найден в школе MJG.\nИспользуйте команду /classes для просмотра списка."

        sched = self.get_class_schedule(norm_class)
        if not sched:
            return f"Расписание для класса *{norm_class}* пока не заполнено."

        lines = [f"📚 *Расписание уроков: {norm_class}*\n"]
        days_to_show = [day_index] if day_index is not None else range(5)

        for d in days_to_show:
            d_name = WEEKDAYS_RU[d]
            lines.append(f"📅 *{d_name}*")
            day_lessons = sched.get(d, {})
            if not day_lessons:
                lines.append("  _Нет уроков_")
            else:
                for p_num in sorted(day_lessons.keys()):
                    cards = day_lessons[p_num]
                    for card in cards:
                        time_part = f"_{card['time']}_ " if card.get("time") else ""
                        teachers = f" ({', '.join(card['teachers'])})" if card.get("teachers") else ""
                        rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                        lines.append(f"  *{p_num}.* {time_part}*{card['subject']}*{teachers}{rooms}")
            lines.append("")

        return "\n".join(lines).strip()

    def format_substitutions_for_telegram(
        self,
        date_str: Optional[str] = None,
        class_name: Optional[str] = None
    ) -> str:
        """Форматирует замены уроков для Telegram (для класса или всей школы)"""
        if not date_str:
            date_str = date.today().strftime("%Y-%m-%d")

        res = self.get_substitutions(date_str=date_str, class_name=class_name)
        lines = [f"🔄 *Замены уроков на {date_str}*\n"]

        if class_name:
            norm_c = self.normalize_class_name(class_name)
            substs = res.get("substitutions", [])
            lines.append(f"Класс: *{norm_c}*")
            if not substs:
                lines.append("✅ *На этот день замен и отмен уроков нет.*")
            else:
                for s in substs:
                    icon = "❌" if s["type"] == "remove" else "⚠️" if s["type"] == "absent" else "🔄"
                    p_txt = f"{s['period']} урок: " if s['period'] else ""
                    lines.append(f"  {icon} *{p_txt}*{s['info']}")
        else:
            all_classes = res.get("classes", {})
            if not all_classes:
                lines.append("✅ *На этот день замен и отмен уроков в школе нет.*")
            else:
                lines.append(f"Всего классов с изменениями: *{len(all_classes)}*\n")
                for c_name, items in sorted(all_classes.items()):
                    lines.append(f"🏫 *Класс {c_name}*")
                    for s in items:
                        icon = "❌" if s["type"] == "remove" else "⚠️" if s["type"] == "absent" else "🔄"
                        p_txt = f"{s['period']} урок: " if s['period'] else ""
                        lines.append(f"  {icon} {p_txt}{s['info']}")
                    lines.append("")

        return "\n".join(lines).strip()

    def format_day_combined_for_telegram(
        self,
        class_name: str,
        target_date: Optional[date] = None
    ) -> str:
        """Форматирует расписание + замены на день для класса"""
        if target_date is None:
            target_date = date.today()

        data = self.get_day_schedule_with_substitutions(class_name, target_date)
        norm_class = data["class_name"]

        if data.get("is_weekend"):
            return (
                f"🏖 *{data['weekday_name']} ({data['date']})*\n"
                f"Для класса *{norm_class}* в выходные уроков нет!"
            )

        lines = [
            f"📅 *{data['weekday_name']}, {data['date']}*",
            f"🏫 Класс: *{norm_class}*\n"
        ]

        if data.get("whole_day_substitutions"):
            lines.append("⚠️ *Общее объявление на день:*")
            for w in data["whole_day_substitutions"]:
                lines.append(f"  • {w['info']}")
            lines.append("")

        lessons = data.get("lessons", [])
        if not lessons:
            lines.append("_На этот день уроков в расписании нет._")
        else:
            for l in lessons:
                p_num = l["period"]
                time_str = f"_{l['time']}_ " if l.get("time") else ""
                substs = l.get("substitutions", [])
                reg_cards = l.get("regular", [])

                if substs:
                    for s in substs:
                        icon = "❌" if s["type"] == "remove" else "⚠️" if s["type"] == "absent" else "🔄"
                        lines.append(f"*{p_num}.* {time_str}{icon} *ЗАМЕНА:* {s['info']}")
                else:
                    if reg_cards:
                        for card in reg_cards:
                            teachers = f" ({', '.join(card['teachers'])})" if card.get("teachers") else ""
                            rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                            lines.append(f"*{p_num}.* {time_str}*{card['subject']}*{teachers}{rooms}")
                    else:
                        lines.append(f"*{p_num}.* {time_str}_Окно (нет урока)_")

        if data.get("has_substitutions"):
            lines.append("\n_🔄 Внимание: на сегодня действуют замены уроков!_")
        else:
            lines.append("\n_✅ Замен на этот день нет, уроки по обычному расписанию._")

        return "\n".join(lines).strip()

    # ================= Форматирование для Telegram (Учителя) =================

    def format_teacher_schedule_for_telegram(self, teacher_name: str, day_index: Optional[int] = None) -> str:
        """Форматирует расписание уроков преподавателя на неделю"""
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        sched = self.get_teacher_schedule(norm_t)

        lines = [f"👩‍🏫 *Расписание преподавателя: {norm_t}*\n"]
        days_to_show = [day_index] if day_index is not None else range(5)

        for d in days_to_show:
            d_name = WEEKDAYS_RU[d]
            lines.append(f"📅 *{d_name}*")
            day_lessons = sched.get(d, {})
            if not day_lessons:
                lines.append("  _Нет уроков в этот день_")
            else:
                for p_num in sorted(day_lessons.keys()):
                    cards = day_lessons[p_num]
                    for card in cards:
                        time_part = f"_{card['time']}_ " if card.get("time") else ""
                        c_name = f"*{card.get('class_name', '')}*"
                        rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                        lines.append(f"  *{p_num}.* {time_part}{c_name} — {card['subject']}{rooms}")
            lines.append("")

        return "\n".join(lines).strip()

    def format_teacher_substitutions_for_telegram(self, teacher_name: str, date_str: Optional[str] = None) -> str:
        """Форматирует список замен для конкретного преподавателя"""
        if not date_str:
            date_str = date.today().strftime("%Y-%m-%d")

        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        data = self.get_teacher_substitutions(norm_t, date_str=date_str)
        items = data.get("substitutions", [])

        lines = [
            f"🔄 *Замены уроков для преподавателя:*",
            f"👩‍🏫 *{norm_t}*",
            f"📅 Дата: *{date_str}*\n"
        ]

        if not items:
            lines.append("✅ *На этот день у вас нет замен или отмен уроков!*")
        else:
            lines.append(f"Найдено изменений: *{len(items)}*\n")
            for it in items:
                role = it.get("teacher_role", "")
                if role == "substituting":
                    role_badge = "🟢 *ВЫ ВЕДЕТЕ ЗАМЕНУ:*"
                elif role == "absent":
                    role_badge = "🔴 *ВАС ЗАМЕНЯЮТ:*"
                elif role == "cancelled":
                    role_badge = "❌ *УРОК ОТМЕНЕН:*"
                else:
                    role_badge = "🔄 *ИЗМЕНЕНИЕ:*"

                p_str = f"{it['period']} урок" if it.get("period") else "Весь день"
                lines.append(f"🏫 Класс *{it['class_name']}* ({p_str})")
                lines.append(f"  {role_badge} {it['info']}\n")

        return "\n".join(lines).strip()

    def format_teacher_day_combined_for_telegram(
        self,
        teacher_name: str,
        target_date: Optional[date] = None
    ) -> str:
        """Форматирует уроки и замены преподавателя на конкретный день"""
        if target_date is None:
            target_date = date.today()

        data = self.get_teacher_day_schedule_with_substitutions(teacher_name, target_date)
        norm_t = data["teacher_name"]

        if data.get("is_weekend"):
            return (
                f"🏖 *{data['weekday_name']} ({data['date']})*\n"
                f"Преподаватель *{norm_t}*: в выходные уроков нет!"
            )

        lines = [
            f"📅 *{data['weekday_name']}, {data['date']}*",
            f"👩‍🏫 Преподаватель: *{norm_t}*\n"
        ]

        lessons = data.get("lessons", [])
        if not lessons:
            lines.append("_На этот день уроков в расписании нет._")
        else:
            for l in lessons:
                p_num = l["period"]
                time_str = f"_{l['time']}_ " if l.get("time") else ""
                substs = l.get("substitutions", [])
                reg_cards = l.get("regular", [])

                if substs:
                    for s in substs:
                        role = s.get("teacher_role")
                        prefix = "🟢 Заменяете" if role == "substituting" else "🔴 Вас заменяют" if role == "absent" else "❌ Отмена"
                        lines.append(f"*{p_num}.* {time_str}*[{prefix}]* {s['class_name']}: {s['info']}")
                elif reg_cards:
                    for card in reg_cards:
                        c_name = f"*{card.get('class_name', '')}*"
                        rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                        lines.append(f"*{p_num}.* {time_str}{c_name} — {card['subject']}{rooms}")
                else:
                    lines.append(f"*{p_num}.* {time_str}_Окно_")

        if data.get("has_substitutions"):
            lines.append("\n_🔄 Внимание: на сегодня у вас есть изменения в уроках!_")
        else:
            lines.append("\n_✅ Уроки проходят в штатном режиме._")

        return "\n".join(lines).strip()
