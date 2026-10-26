import re
import sys
import json
import time
import logging
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple, Set
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

from config import (
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
    WEEKDAYS_RU
)

logger = logging.getLogger(__name__)


def format_group_label(group_name: str) -> str:
    """Форматирует название группы из EduPage в читаемый вид с иконками"""
    if not group_name:
        return ""
    parts = [p.strip() for p in group_name.split(",") if p.strip()]
    formatted_parts = []
    for p in parts:
        p_low = p.lower()
        if "poisid" in p_low or "мальчик" in p_low:
            formatted_parts.append("👦 Мальчики")
        elif "tüdrukud" in p_low or "tudrukud" in p_low or "девочк" in p_low:
            formatted_parts.append("👧 Девочки")
        elif "rühm 1" in p_low or "grupp 1" in p_low or "1. rühm" in p_low or "1 grupp" in p_low:
            formatted_parts.append("👥 1 группа")
        elif "rühm 2" in p_low or "grupp 2" in p_low or "2. rühm" in p_low or "2 grupp" in p_low:
            formatted_parts.append("👥 2 группа")
        elif "rühm 3" in p_low or "grupp 3" in p_low:
            formatted_parts.append("👥 3 группа")
        elif "saksa" in p_low:
            formatted_parts.append("🇩🇪 Немецкий яз.")
        elif "prantsuse" in p_low:
            formatted_parts.append("🇫🇷 Французский яз.")
        elif p != "Terve klass":
            formatted_parts.append(f"👥 {p}")

    deduped = list(dict.fromkeys(formatted_parts))
    return " ".join(deduped)


def parse_period_numbers(period_str: str) -> List[int]:
    """
    Разбирает строку с номерами уроков в список чисел:
    '3' -> [3]
    '(7)' -> [7]
    '3 - 4' -> [3, 4]
    '2-3' -> [2, 3]
    '3, 4' -> [3, 4]
    '' -> []
    """
    if not period_str:
        return []
    s = str(period_str).strip().strip("()")
    if not s:
        return []
    range_match = re.match(r"^(\d+)\s*[-–—]\s*(\d+)$", s)
    if range_match:
        start_p = int(range_match.group(1))
        end_p = int(range_match.group(2))
        if start_p <= end_p:
            return list(range(start_p, end_p + 1))
        return [start_p, end_p]
    if "," in s:
        res = []
        for part in s.split(","):
            part_s = part.strip()
            if part_s.isdigit():
                res.append(int(part_s))
        return res
    if s.isdigit():
        return [int(s)]
    return []


def should_show_card(card: Dict[str, Any], subgroup_filter: Optional[Dict[str, str]]) -> bool:
    """Проверяет, подходит ли урок под выбранные пользователем подгруппы"""
    if not subgroup_filter:
        return True

    card_group = card.get("group", "")
    if not card_group:
        return True  # Урок для всего класса показываем всегда

    group_low = card_group.lower()

    # Фильтр по полу (физкультура и т.п.)
    gender_filter = subgroup_filter.get("gender")
    if gender_filter == "boys":
        if any(w in group_low for w in ["tüdrukud", "tudrukud", "девочк", "девочки"]):
            return False
    elif gender_filter == "girls":
        if any(w in group_low for w in ["poisid", "мальчик", "мальчики"]):
            return False

    # Фильтр по академическим/языковым группам (1 группа / 2 группа)
    academic_filter = subgroup_filter.get("academic")
    if academic_filter in ("1", "group1"):
        if any(w in group_low for w in ["rühm 2", "grupp 2", "2. rühm", "2 grupp", "группа 2", "2 группа"]):
            return False
    elif academic_filter in ("2", "group2"):
        if any(w in group_low for w in ["rühm 1", "grupp 1", "1. rühm", "1 grupp", "группа 1", "1 группа"]):
            return False

    return True


class MJGParser:
    """
    Парсер расписания и замен для Таллиннской Мустйыэской гимназии
    (www.mjg.ee / mustjoe.edupage.org)
    Поддерживает работу с классами, учителями (с кабинетами и классами),
    подгруппами (мальчики/девочки, группы 1/2), устойчивое дисковое кэширование и автономный режим.
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

        # 2. Учителя (из отдельного файла teachers.json, если кэш пуст или для дополнения)
        if TEACHERS_DATA_FILE.exists():
            try:
                with open(TEACHERS_DATA_FILE, "r", encoding="utf-8") as f:
                    t_list = json.load(f)
                    if not self._teachers_cache:
                        self._teachers_cache = {}
                    for t in t_list:
                        fname = t.get("full_name")
                        if fname and fname not in self._teachers_cache:
                            self._teachers_cache[fname] = {
                                "id": fname,
                                "name": fname,
                                "surname": t.get("surname", ""),
                                "firstname": t.get("firstname", ""),
                                "short": t.get("surname", "")
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
        """Приводит название класса к каноническому виду (например, '10 a' -> '10A', '7-8e' -> '7-8E', '6б' -> '6B')"""
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

        def clean_key(s: str) -> str:
            s = s.lower().strip()
            s = s.replace('õ', 'y').replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')
            s = re.sub(r'^ju', 'u', s)            # juksiratsa -> uksiratsa
            s = re.sub(r'([aeouy])j(?=[^aeiouy]|$)', r'\1i', s)  # lajne -> laine
            s = s.replace('ž', 'z').replace('š', 's')
            s = s.replace('c', 'k')
            s = s.replace('j', 'z')
            s = re.sub(r'(.)\1+', r'\1', s)  # collapse double letters (e.g. ss -> s, ll -> l)
            return s

        q_clean = clean_key(q)
        q_translit_clean = clean_key(q_translit)

        # 1. Точное совпадение с полным именем
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

        # 4. Упрощенное фонетическое совпадение с фамилией
        for t_name, info in teachers.items():
            surname = info.get("surname", "")
            sur_clean = clean_key(surname)
            if sur_clean and (sur_clean == q_clean or sur_clean == q_translit_clean or
                              sur_clean.startswith(q_clean) or sur_clean.startswith(q_translit_clean) or
                              q_clean in sur_clean or q_translit_clean in sur_clean):
                return t_name

        # 5. Совпадение по полному имени
        for t_name in teachers.keys():
            t_clean = clean_key(t_name)
            if q_clean in t_clean or q_translit_clean in t_clean:
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
            and self._timetable_cache.get("teachers")
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
                    groups_map = raw_tables.get("groups", {})

                    # Загружаем учителей из teachers.json для точного сопоставления ФИО
                    t_list = []
                    if TEACHERS_DATA_FILE.exists():
                        try:
                            with open(TEACHERS_DATA_FILE, "r", encoding="utf-8") as f:
                                t_list = json.load(f)
                        except Exception:
                            pass
                    t_by_surname = {t["surname"].lower(): t for t in t_list}

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

                    # Добавляем остальных учителей из teachers.json
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

        if self._timetable_cache:
            return self._timetable_cache

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
        schedule[day_idx][period_num] = list of lessons (с указанием класса, кабинета и подгруппы)
        """
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        tt = self.get_regular_timetable()
        schedule_by_class = tt.get("schedule", {})

        teacher_schedule: Dict[int, Dict[int, List[Dict[str, Any]]]] = {d: {} for d in range(5)}

        search_terms = [norm_t.lower()]
        parts = norm_t.split()
        if len(parts) >= 2:
            search_terms.append(parts[0].lower())  # Фамилия отдельно

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
                            card_teachers = [t.lower() for t in card.get("teachers", [])]
                            matched = any(term in ct for ct in card_teachers for term in search_terms)
                            if matched:
                                lesson_copy = dict(card)
                                lesson_copy["class_name"] = c_name
                                teacher_schedule[d_int].setdefault(p_int, []).append(lesson_copy)

        if day_index is not None:
            return {day_index: teacher_schedule.get(day_index, {})}
        return teacher_schedule

    # ================= Кабинеты преподавателей и школы =================

    def get_teacher_classrooms(self, teacher_name: str) -> Dict[str, Any]:
        """
        Возвращает информацию о кабинетах преподавателя:
        - primary_room: основной (наиболее частый) кабинет
        - all_rooms: список всех кабинетов, где преподает учитель
        - room_counts: статистика по кабинетам {кабинет: число уроков}
        - by_day: кабинеты по дням недели
        - classes_by_room: какие классы учит в каком кабинете
        - subjects_by_room: какие предметы ведёт в каком кабинете
        """
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        sched = self.get_teacher_schedule(norm_t)

        room_counts: Dict[str, int] = {}
        by_day: Dict[int, List[str]] = {d: [] for d in range(5)}
        classes_by_room: Dict[str, Set[str]] = {}
        subjects_by_room: Dict[str, Set[str]] = {}

        for d_int, periods in sched.items():
            day_rooms = set()
            for p_int, cards in periods.items():
                for card in cards:
                    c_name = card.get("class_name", "")
                    subj = card.get("subject", "")
                    for r in card.get("classrooms", []):
                        r_str = str(r).strip()
                        if r_str:
                            room_counts[r_str] = room_counts.get(r_str, 0) + 1
                            day_rooms.add(r_str)
                            if c_name:
                                classes_by_room.setdefault(r_str, set()).add(c_name)
                            if subj:
                                subjects_by_room.setdefault(r_str, set()).add(subj)
            by_day[d_int] = sorted(list(day_rooms))

        all_rooms = sorted(list(room_counts.keys()))
        primary_room = None
        if room_counts:
            primary_room = max(room_counts.items(), key=lambda x: x[1])[0]

        return {
            "teacher_name": norm_t,
            "primary_room": primary_room,
            "all_rooms": all_rooms,
            "room_counts": room_counts,
            "by_day": by_day,
            "classes_by_room": {k: sorted(list(v)) for k, v in classes_by_room.items()},
            "subjects_by_room": {k: sorted(list(v)) for k, v in subjects_by_room.items()}
        }

    def get_all_teachers_classrooms(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает информацию о кабинетах всех преподавателей школы"""
        teachers = self.get_teachers()
        res = {}
        for t_name in teachers.keys():
            t_rooms = self.get_teacher_classrooms(t_name)
            if t_rooms.get("all_rooms"):
                res[t_name] = t_rooms
        return res

    def get_all_classrooms(self) -> List[str]:
        """Возвращает отсортированный список всех кабинетов школы из расписания"""
        tt = self.get_regular_timetable()
        schedule_by_class = tt.get("schedule", {})
        rooms = set()
        for c_days in schedule_by_class.values():
            for day_lessons in c_days.values():
                for cards in day_lessons.values():
                    for card in cards:
                        for r in card.get("classrooms", []):
                            r_str = str(r).strip()
                            if r_str:
                                rooms.add(r_str)
        return sorted(list(rooms))

    def get_classroom_schedule(self, room_query: str, day_index: Optional[int] = None) -> Dict[str, Any]:
        """
        Возвращает расписание уроков в конкретном кабинете по дням недели.
        room_schedule[day_idx][period_num] = list of lessons (class_name, subject, teachers, time, group)
        """
        norm_room = str(room_query).strip()
        all_rooms = self.get_all_classrooms()
        # Поиск точного или частичного совпадения кабинета
        matched_room = None
        for r in all_rooms:
            if r.lower() == norm_room.lower():
                matched_room = r
                break
        if not matched_room:
            for r in all_rooms:
                if norm_room.lower() in r.lower():
                    matched_room = r
                    break
        if not matched_room:
            matched_room = norm_room

        tt = self.get_regular_timetable()
        schedule_by_class = tt.get("schedule", {})

        room_schedule: Dict[int, Dict[int, List[Dict[str, Any]]]] = {d: {} for d in range(5)}
        teacher_counts: Dict[str, int] = {}

        for c_name, class_days in schedule_by_class.items():
            for d_key, day_lessons in class_days.items():
                try:
                    d_int = int(d_key)
                except ValueError:
                    continue
                if d_int not in room_schedule:
                    continue
                if isinstance(day_lessons, dict):
                    for p_key, cards in day_lessons.items():
                        try:
                            p_int = int(p_key)
                        except ValueError:
                            continue
                        for card in cards:
                            card_rooms = [str(r).lower() for r in card.get("classrooms", [])]
                            if matched_room.lower() in card_rooms:
                                lesson_copy = dict(card)
                                lesson_copy["class_name"] = c_name
                                room_schedule[d_int].setdefault(p_int, []).append(lesson_copy)
                                for t in card.get("teachers", []):
                                    teacher_counts[t] = teacher_counts.get(t, 0) + 1

        top_teachers = sorted(teacher_counts.items(), key=lambda x: x[1], reverse=True)

        if day_index is not None:
            day_res = {day_index: room_schedule.get(day_index, {})}
        else:
            day_res = room_schedule

        return {
            "room_name": matched_room,
            "schedule": day_res,
            "top_teachers": [t[0] for t in top_teachers[:3]],
            "total_lessons": sum(len(cards) for d in room_schedule.values() for cards in d.values())
        }

    def get_current_period(self, target_dt: Optional[datetime] = None) -> Tuple[Optional[int], str, Optional[int]]:
        """
        Определяет текущий или следующий урок по времени звонков MJG.
        Возвращает: (current_period, status, next_period)
        status: 'in_lesson', 'in_break', 'before_school', 'after_school'
        """
        if target_dt is None:
            target_dt = datetime.now()

        cur_min = target_dt.hour * 60 + target_dt.minute

        tt = self.get_regular_timetable()
        periods = tt.get("periods", {})

        parsed_periods = []
        for p_str, p_info in sorted(periods.items(), key=lambda x: int(x[0])):
            st = p_info.get("starttime", "")
            et = p_info.get("endtime", "")
            if st and et:
                s_parts = [int(x) for x in st.split(":")]
                e_parts = [int(x) for x in et.split(":")]
                s_min = s_parts[0] * 60 + s_parts[1]
                e_min = e_parts[0] * 60 + e_parts[1]
                parsed_periods.append((int(p_str), s_min, e_min, st, et))

        if not parsed_periods:
            return None, "unknown", None

        # Проверяем, идет ли сейчас урок
        for i, (p_num, s_min, e_min, st, et) in enumerate(parsed_periods):
            if s_min <= cur_min <= e_min:
                next_p = parsed_periods[i+1][0] if i+1 < len(parsed_periods) else None
                return p_num, "in_lesson", next_p

        # До уроков
        if cur_min < parsed_periods[0][1]:
            return None, "before_school", parsed_periods[0][0]

        # После уроков
        if cur_min > parsed_periods[-1][2]:
            return None, "after_school", None

        # Перемена между уроками
        for i in range(len(parsed_periods) - 1):
            cur_end = parsed_periods[i][2]
            next_start = parsed_periods[i+1][1]
            if cur_end < cur_min < next_start:
                return parsed_periods[i][0], "in_break", parsed_periods[i+1][0]

        return None, "unknown", None

    def get_free_classrooms(
        self,
        day_index: Optional[int] = None,
        period_num: Optional[int] = None,
        target_time: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Возвращает список свободных кабинетов на указанный урок и день.
        Если день или урок не указаны, определяются по текущему времени.
        """
        now = target_time or datetime.now()
        if day_index is None:
            day_index = now.weekday()

        cur_p, status, next_p = self.get_current_period(now)

        if period_num is None:
            if status == "in_lesson" and cur_p:
                period_num = cur_p
            elif status == "in_break" and next_p:
                period_num = next_p
            elif status == "before_school" and next_p:
                period_num = next_p
            else:
                period_num = 1

        all_rooms = self.get_all_classrooms()
        tt = self.get_regular_timetable()
        schedule_by_class = tt.get("schedule", {})
        periods_info = tt.get("periods", {})

        p_info = periods_info.get(str(period_num), {})
        time_str = f"{p_info.get('starttime', '')}-{p_info.get('endtime', '')}" if p_info.get("starttime") else ""

        occupied_map: Dict[str, List[Dict[str, Any]]] = {}

        for c_name, class_days in schedule_by_class.items():
            day_lessons = class_days.get(day_index, {}) or class_days.get(str(day_index), {})
            if isinstance(day_lessons, dict):
                cards = day_lessons.get(period_num, []) or day_lessons.get(str(period_num), [])
                for card in cards:
                    for r in card.get("classrooms", []):
                        r_str = str(r).strip()
                        if r_str:
                            occupied_map.setdefault(r_str, []).append({
                                "class_name": c_name,
                                "subject": card.get("subject", ""),
                                "teachers": card.get("teachers", [])
                            })

        occupied_rooms = sorted(list(occupied_map.keys()))
        free_rooms = [r for r in all_rooms if r not in occupied_map]

        return {
            "day_index": day_index,
            "day_name": WEEKDAYS_RU[day_index] if 0 <= day_index < 5 else "Выходной",
            "period_num": period_num,
            "time_str": time_str,
            "status": status,
            "free_rooms": free_rooms,
            "occupied_rooms": occupied_rooms,
            "occupied_details": occupied_map,
            "total_rooms": len(all_rooms)
        }

    def get_teacher_current_lesson(self, teacher_name: str, target_dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Определяет, где сейчас находится учитель (кабинет, класс, урок) или когда следующий урок.
        """
        now = target_dt or datetime.now()
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        weekday = now.weekday()

        if weekday >= 5:
            return {
                "teacher_name": norm_t,
                "status": "weekend",
                "message": "Сейчас выходной день. Уроков в школе нет."
            }

        cur_p, status, next_p = self.get_current_period(now)
        sched = self.get_teacher_schedule(norm_t, day_index=weekday)
        day_sched = sched.get(weekday, {}) if sched else {}

        # Проверяем замены на сегодня
        today_str = now.strftime("%Y-%m-%d")
        t_subst = self.get_teacher_substitutions(norm_t, date_str=today_str).get("substitutions", [])
        subst_by_p = {}
        for s in t_subst:
            p_nums = parse_period_numbers(s.get("period", ""))
            for p in p_nums:
                subst_by_p[p] = s

        current_lesson = None
        if cur_p and cur_p in day_sched:
            current_lesson = day_sched[cur_p][0] if day_sched[cur_p] else None

        current_subst = subst_by_p.get(cur_p) if cur_p else None

        next_lesson = None
        next_lesson_period = None
        for p in sorted(day_sched.keys()):
            if (cur_p and p > cur_p) or (not cur_p and status == "before_school"):
                next_lesson = day_sched[p][0] if day_sched[p] else None
                next_lesson_period = p
                break

        return {
            "teacher_name": norm_t,
            "weekday": weekday,
            "time_now": now.strftime("%H:%M"),
            "status": status,
            "current_period": cur_p,
            "current_lesson": current_lesson,
            "current_subst": current_subst,
            "next_period": next_lesson_period,
            "next_lesson": next_lesson
        }

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
                    elif "tühistatud" in info_lower or "отмен" in info_lower:
                        role = "cancelled"
                    elif "õpetaja:" in info_lower or "учитель:" in info_lower:
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

        subst_by_period: Dict[int, List[Dict[str, Any]]] = {}
        whole_day_subst = []
        for item in subst_items:
            p_nums = parse_period_numbers(item.get("period", ""))
            if p_nums:
                for p in p_nums:
                    subst_by_period.setdefault(p, []).append(item)
            else:
                whole_day_subst.append(item)

        periods_info = self.get_regular_timetable().get("periods", {})
        combined_lessons = []

        all_periods = set(day_sched.keys()) | set(subst_by_period.keys())

        for p_num in sorted(all_periods):
            p_str = str(p_num)
            p_info = periods_info.get(p_str, {})
            time_str = f"{p_info.get('starttime', '')}-{p_info.get('endtime', '')}" if p_info.get("starttime") else ""

            reg_cards = day_sched.get(p_num, [])
            subst_for_p = subst_by_period.get(p_num, [])

            combined_lessons.append({
                "period": p_num,
                "time": time_str,
                "regular": reg_cards,
                "substitutions": subst_for_p
            })

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

        subst_by_p: Dict[int, List[Dict[str, Any]]] = {}
        whole_day_t_subst = []
        for s in subst_items:
            p_nums = parse_period_numbers(s.get("period", ""))
            if p_nums:
                for p in p_nums:
                    subst_by_p.setdefault(p, []).append(s)
            else:
                whole_day_t_subst.append(s)

        periods_info = self.get_regular_timetable().get("periods", {})
        combined = []

        all_p = set(day_lessons.keys()) | set(subst_by_p.keys())

        for p_num in sorted(all_p):
            p_str = str(p_num)
            p_info = periods_info.get(p_str, {})
            time_str = f"{p_info.get('starttime', '')}-{p_info.get('endtime', '')}" if p_info.get("starttime") else ""

            combined.append({
                "period": p_num,
                "time": time_str,
                "regular": day_lessons.get(p_num, []),
                "substitutions": subst_by_p.get(p_num, [])
            })

        return {
            "is_weekend": False,
            "date": date_str,
            "teacher_name": norm_t,
            "weekday_index": weekday,
            "weekday_name": WEEKDAYS_RU[weekday],
            "lessons": combined,
            "whole_day_substitutions": whole_day_t_subst,
            "has_substitutions": len(subst_items) > 0
        }

    # ================= Форматирование для Telegram (Классы) =================

    def format_schedule_for_telegram(
        self,
        class_name: str,
        day_index: Optional[int] = None,
        subgroup_filter: Optional[Dict[str, str]] = None
    ) -> str:
        """Форматирует расписание класса для Telegram с поддержкой подгрупп и кабинетов"""
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
                    visible_cards = [c for c in cards if should_show_card(c, subgroup_filter)]
                    if not visible_cards:
                        continue

                    for card in visible_cards:
                        time_part = f"_{card['time']}_ " if card.get("time") else ""
                        teachers = f" ({', '.join(card['teachers'])})" if card.get("teachers") else ""
                        rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                        grp_label = f" {format_group_label(card['group'])}" if card.get("group") else ""

                        lines.append(f"  *{p_num}.* {time_part}*{card['subject']}*{grp_label}{teachers}{rooms}")
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
        target_date: Optional[date] = None,
        subgroup_filter: Optional[Dict[str, str]] = None
    ) -> str:
        """Форматирует расписание + замены на день для класса с подгруппами"""
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
                    visible_cards = [c for c in reg_cards if should_show_card(c, subgroup_filter)]
                    if visible_cards:
                        for card in visible_cards:
                            teachers = f" ({', '.join(card['teachers'])})" if card.get("teachers") else ""
                            rooms = f" [каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else ""
                            grp_label = f" {format_group_label(card['group'])}" if card.get("group") else ""
                            lines.append(f"*{p_num}.* {time_str}*{card['subject']}*{grp_label}{teachers}{rooms}")
                    elif reg_cards:
                        # Урок есть, но в другой подгруппе пользователя
                        continue
                    else:
                        lines.append(f"*{p_num}.* {time_str}_Окно (нет урока)_")

        if data.get("has_substitutions"):
            lines.append("\n_🔄 Внимание: на сегодня действуют замены уроков!_")
        else:
            lines.append("\n_✅ Замен на этот день нет, уроки по обычному расписанию._")

        return "\n".join(lines).strip()

    # ================= Форматирование для Telegram (Учителя и Кабинеты) =================

    def format_teacher_schedule_for_telegram(self, teacher_name: str, day_index: Optional[int] = None) -> str:
        """Форматирует расписание уроков преподавателя на неделю с указанием классов и кабинетов"""
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        sched = self.get_teacher_schedule(norm_t)
        rooms_info = self.get_teacher_classrooms(norm_t)

        lines = [f"👩‍🏫 *Расписание преподавателя: {norm_t}*"]
        prim_r = rooms_info.get("primary_room")
        all_r = rooms_info.get("all_rooms", [])
        other_r = [r for r in all_r if r != prim_r]

        if prim_r and other_r:
            lines.append(f"🚪 *Кабинеты преподавания:* *{prim_r}* (основной), {', '.join(other_r)}\n")
        elif prim_r:
            lines.append(f"🚪 *Основной кабинет:* *{prim_r}*\n")
        else:
            lines.append("")

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
                        c_name = f"Класс *{card.get('class_name', '')}*"
                        rooms = f" [🚪 каб. {', '.join(card['classrooms'])}]" if card.get("classrooms") else (f" [🚪 каб. {prim_r}]" if prim_r else " [каб. не указан]")
                        grp_label = f" ({format_group_label(card['group'])})" if card.get("group") else ""
                        lines.append(f"  *{p_num}.* {time_part}{c_name}{grp_label} — *{card['subject']}*{rooms}")
            lines.append("")

        return "\n".join(lines).strip()

    def format_teacher_substitutions_for_telegram(self, teacher_name: str, date_str: Optional[str] = None) -> str:
        """Форматирует список замен для конкретного преподавателя с указанием классов и кабинетов"""
        if not date_str:
            date_str = date.today().strftime("%Y-%m-%d")

        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        data = self.get_teacher_substitutions(norm_t, date_str=date_str)
        items = data.get("substitutions", [])
        rooms_info = self.get_teacher_classrooms(norm_t)
        prim_r = rooms_info.get("primary_room")

        lines = [
            "🔄 *Замены уроков для преподавателя:*",
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
                s_info = it.get("info", "")

                # Проверяем, есть ли указание кабинета в замене
                room_badge = ""
                r_match = re.search(r'Vaheta ruumi:\s*(?:\([^)]+\)\s*➔\s*)?([^\s,]+)', s_info, re.IGNORECASE)
                if r_match:
                    room_badge = f" [🚪 каб. {r_match.group(1)}]"
                elif prim_r and role == "substituting":
                    room_badge = f" [🚪 каб. {prim_r}]"

                lines.append(f"🏫 Класс *{it['class_name']}* ({p_str}){room_badge}")
                lines.append(f"  {role_badge} {s_info}\n")

        return "\n".join(lines).strip()

    def format_teacher_day_combined_for_telegram(
        self,
        teacher_name: str,
        target_date: Optional[date] = None
    ) -> str:
        """Форматирует уроки и замены преподавателя на день с указанием классов и кабинетов"""
        if target_date is None:
            target_date = date.today()

        data = self.get_teacher_day_schedule_with_substitutions(teacher_name, target_date)
        norm_t = data["teacher_name"]
        rooms_info = self.get_teacher_classrooms(norm_t)
        prim_r = rooms_info.get("primary_room")
        all_r = rooms_info.get("all_rooms", [])
        other_r = [r for r in all_r if r != prim_r]

        if data.get("is_weekend"):
            return (
                f"🏖 *{data['weekday_name']} ({data['date']})*\n"
                f"Преподаватель *{norm_t}*: в выходные уроков нет!"
            )

        lines = [
            f"📅 *{data['weekday_name']}, {data['date']}*",
            f"👩‍🏫 Преподаватель: *{norm_t}*",
        ]

        if prim_r and other_r:
            lines.append(f"🚪 Кабинет(ы): *{prim_r}* (также: {', '.join(other_r)})\n")
        elif prim_r:
            lines.append(f"🚪 Кабинет: *{prim_r}*\n")
        else:
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
                        role = s.get("teacher_role")
                        prefix = "🟢 Заменяете" if role == "substituting" else "🔴 Вас заменяют" if role == "absent" else "❌ Отмена"
                        s_info = s.get("info", "")
                        r_match = re.search(r'Vaheta ruumi:\s*(?:\([^)]+\)\s*➔\s*)?([^\s,]+)', s_info, re.IGNORECASE)
                        room_badge = ""
                        if r_match:
                            room_badge = f" [🚪 каб. {r_match.group(1)}]"
                        elif prim_r and role == "substituting":
                            room_badge = f" [🚪 каб. {prim_r}]"
                        lines.append(f"*{p_num}.* {time_str}*[{prefix}]* Класс *{s['class_name']}*: {s_info}{room_badge}")
                elif reg_cards:
                    for card in reg_cards:
                        c_name = f"Класс *{card.get('class_name', '')}*"
                        card_rooms = card.get("classrooms", [])
                        rooms = f" [🚪 каб. {', '.join(card_rooms)}]" if card_rooms else (f" [🚪 каб. {prim_r}]" if prim_r else " [каб. не указан]")
                        grp_label = f" ({format_group_label(card['group'])})" if card.get("group") else ""
                        lines.append(f"*{p_num}.* {time_str}{c_name}{grp_label} — *{card['subject']}*{rooms}")
                else:
                    lines.append(f"*{p_num}.* {time_str}_Окно_")

        if data.get("has_substitutions"):
            lines.append("\n_🔄 Внимание: на сегодня у вас есть изменения в уроках!_")
        else:
            lines.append("\n_✅ Уроки проходят в штатном режиме._")

        return "\n".join(lines).strip()

    def format_classroom_schedule_for_telegram(self, room_query: str, day_index: Optional[int] = None) -> str:
        """Форматирует расписание уроков в кабинете для Telegram"""
        data = self.get_classroom_schedule(room_query, day_index=day_index)
        r_name = data["room_name"]
        sched = data["schedule"]
        top_teachers = data["top_teachers"]

        days_to_show = [day_index] if day_index is not None else range(5)

        lines = [f"🚪 *Кабинет {r_name}*"]
        if top_teachers:
            lines.append(f"👩‍🏫 Преподаватель(и): *{', '.join(top_teachers)}*")
        lines.append("")

        has_any = False
        for d in days_to_show:
            d_name = WEEKDAYS_RU[d]
            lines.append(f"📅 *{d_name}*")
            day_lessons = sched.get(d, {})
            if not day_lessons:
                lines.append("  _Свободен весь день (нет уроков)_")
            else:
                has_any = True
                for p_num in sorted(day_lessons.keys()):
                    cards = day_lessons[p_num]
                    for card in cards:
                        time_part = f"_{card['time']}_ " if card.get("time") else ""
                        c_name = f"Класс *{card.get('class_name', '')}*"
                        teachers = f" ({', '.join(card['teachers'])})" if card.get("teachers") else ""
                        grp_label = f" [{format_group_label(card['group'])}]" if card.get("group") else ""
                        lines.append(f"  *{p_num}.* {time_part}{c_name} — *{card['subject']}*{grp_label}{teachers}")
            lines.append("")

        if not has_any and day_index is None:
            return f"🚪 В кабинете *{r_name}* нет постоянных уроков по базовому расписанию (или кабинет свободен)."

        return "\n".join(lines).strip()

    def format_free_rooms_for_telegram(
        self,
        day_index: Optional[int] = None,
        period_num: Optional[int] = None
    ) -> str:
        """Форматирует список свободных кабинетов для Telegram"""
        res = self.get_free_classrooms(day_index=day_index, period_num=period_num)
        p_num = res["period_num"]
        time_str = f" ({res['time_str']})" if res.get("time_str") else ""
        d_name = res["day_name"]
        free_rooms = res["free_rooms"]

        lines = [
            "🚪 *Свободные кабинеты школы MJG*",
            f"📅 {d_name}, *{p_num}-й урок*{time_str}\n"
        ]

        if not free_rooms:
            lines.append("❌ Все кабинеты заняты на этот урок!")
        else:
            lines.append(f"🟢 *Свободно кабинетов:* *{len(free_rooms)}* из {res['total_rooms']}\n")
            floor0 = [r for r in free_rooms if r.startswith("0")]
            floor1 = [r for r in free_rooms if r.startswith("1")]
            floor2 = [r for r in free_rooms if r.startswith("2")]
            floor3 = [r for r in free_rooms if r.startswith("3")]
            others = [r for r in free_rooms if r not in floor0 + floor1 + floor2 + floor3]

            if floor0:
                lines.append(f"• *Цокольный / 0 этаж:* `{', '.join(floor0)}`")
            if floor1:
                lines.append(f"• *1-й этаж:* `{', '.join(floor1)}`")
            if floor2:
                lines.append(f"• *2-й этаж:* `{', '.join(floor2)}`")
            if floor3:
                lines.append(f"• *3-й этаж:* `{', '.join(floor3)}`")
            if others:
                lines.append(f"• *Спец. помещения:* `{', '.join(others)}`")

        lines.append("\n💡 _Чтобы посмотреть расписание кабинета, напишите /room <номер>_")
        return "\n".join(lines).strip()

    def format_whereis_teacher_for_telegram(self, teacher_name: str) -> str:
        """Форматирует ответ на вопрос 'Где сейчас преподаватель?'"""
        info = self.get_teacher_current_lesson(teacher_name)
        norm_t = info["teacher_name"]

        if info.get("status") == "weekend":
            return f"🏖 Преподаватель *{norm_t}*:\nСейчас выходной день, уроков в школе нет!"

        lines = [f"🔍 *Где сейчас преподаватель: {norm_t}*\n"]
        status = info.get("status")
        cur_p = info.get("current_period")
        cur_l = info.get("current_lesson")
        cur_s = info.get("current_subst")
        next_p = info.get("next_period")
        next_l = info.get("next_lesson")

        if cur_s:
            role = cur_s.get("teacher_role")
            action = "ведет замену" if role == "substituting" else "заменяется" if role == "absent" else "изменение"
            lines.append(f"⚡️ *Сейчас идет {cur_p}-й урок:*")
            lines.append(f"🔄 Преподаватель *{action}* в классе *{cur_s['class_name']}*")
            lines.append(f"📝 {cur_s['info']}\n")
        elif cur_l:
            c_name = cur_l.get("class_name", "")
            rooms = ", ".join(cur_l.get("classrooms", [])) or "не указан"
            subj = cur_l.get("subject", "")
            t_str = f" ({cur_l['time']})" if cur_l.get("time") else ""
            lines.append(f"🟢 *ИДЕТ УРОК:* *{cur_p}-й урок*{t_str}")
            lines.append(f"🏫 Класс: *{c_name}*")
            lines.append(f"🚪 Кабинет: *{rooms}*")
            lines.append(f"📖 Предмет: *{subj}*\n")
        elif status == "in_lesson":
            lines.append(f"☕️ *Сейчас {cur_p}-й урок:* у учителя окно (нет уроков).\n")
        elif status == "in_break":
            lines.append("☕️ *Сейчас перемена.*\n")
        elif status == "before_school":
            lines.append("🌅 *Занятия еще не начались.*\n")
        elif status == "after_school":
            lines.append("🌙 *Уроки на сегодня окончены.*\n")

        if next_l:
            c_next = next_l.get("class_name", "")
            r_next = ", ".join(next_l.get("classrooms", [])) or "не указан"
            subj_next = next_l.get("subject", "")
            t_next = f" ({next_l['time']})" if next_l.get("time") else ""
            lines.append(f"🔜 *Следующий урок:* *{next_p}-й урок*{t_next}")
            lines.append(f"🏫 Класс: *{c_next}* | 🚪 Кабинет: *{r_next}* ({subj_next})")
        elif not cur_l and not cur_s and status in ("after_school", "in_lesson", "in_break"):
            lines.append("✅ Больше уроков на сегодня у преподавателя нет.")

        return "\n".join(lines).strip()

    def format_teacher_classrooms_for_telegram(self, teacher_name: str) -> str:
        """Подробно форматирует информацию о всех кабинетах преподавателя"""
        norm_t = self.normalize_teacher_name(teacher_name) or teacher_name
        info = self.get_teacher_classrooms(norm_t)
        primary_room = info.get("primary_room")
        all_rooms = info.get("all_rooms", [])
        room_counts = info.get("room_counts", {})
        by_day = info.get("by_day", {})
        classes_by_room = info.get("classes_by_room", {})
        subjects_by_room = info.get("subjects_by_room", {})

        if not all_rooms:
            return (
                f"🚪 *Кабинеты преподавателя: {norm_t}*\n\n"
                f"ℹ️ Для данного преподавателя в расписании не указаны закреплённые кабинеты."
            )

        lines = [
            f"🚪 *Кабинеты преподавания: {norm_t}*\n"
        ]
        if primary_room:
            p_count = room_counts.get(primary_room, 0)
            p_subjs = ", ".join(subjects_by_room.get(primary_room, []))
            subjs_text = f" ({p_subjs})" if p_subjs else ""
            lines.append(f"🏛 *Основной кабинет:* *{primary_room}*{subjs_text} — *{p_count}* ур./нед.")

        other_rooms = [r for r in all_rooms if r != primary_room]
        if other_rooms:
            other_parts = []
            for r in other_rooms:
                r_c = room_counts.get(r, 0)
                r_s = ", ".join(subjects_by_room.get(r, []))
                s_note = f" [{r_s}]" if r_s else ""
                other_parts.append(f"*{r}*{s_note} ({r_c} ур.)")
            lines.append(f"📍 *Другие кабинеты:* {', '.join(other_parts)}")

        lines.append("\n📅 *Кабинеты по дням недели:*")
        for d in range(5):
            d_name = WEEKDAYS_RU[d]
            day_rooms = by_day.get(d, [])
            if not day_rooms:
                lines.append(f"  • *{d_name}:* уроков нет")
            else:
                day_str = ", ".join([f"каб. *{r}*" for r in day_rooms])
                lines.append(f"  • *{d_name}:* {day_str}")

        lines.append("\n🏫 *Классы по кабинетам:*")
        for r in all_rooms:
            cls_list = classes_by_room.get(r, [])
            cls_str = ", ".join(cls_list) if cls_list else "—"
            lines.append(f"  • Каб. *{r}*: классы {cls_str}")

        lines.append("\n_💡 Нажмите «Где сейчас?», чтобы узнать, где учитель находится прямо в данный момент._")
        return "\n".join(lines).strip()

    def format_all_teachers_classrooms_for_telegram(self, letter: Optional[str] = None) -> str:
        """Справочник всех преподавателей школы с их кабинетами"""
        all_tc = self.get_all_teachers_classrooms()
        teachers = sorted(list(all_tc.keys()))
        if letter:
            teachers = [t for t in teachers if t.upper().startswith(letter.upper())]

        lines = [f"📋 *Преподаватели и кабинеты школы MJG* ({len(teachers)}):\n"]
        for t in teachers:
            tc = all_tc[t]
            prim = tc.get("primary_room")
            all_r = tc.get("all_rooms", [])
            other = [r for r in all_r if r != prim]
            if prim and other:
                lines.append(f"• *{t}* ➔ каб. *{prim}* _(также: {', '.join(other)})_")
            elif prim:
                lines.append(f"• *{t}* ➔ каб. *{prim}*")
            elif all_r:
                lines.append(f"• *{t}* ➔ каб. *{', '.join(all_r)}*")

        lines.append("\n💡 _Чтобы посмотреть расписание кабинета: `/room <номер>` (напр. `/room 106`)_")
        lines.append("💡 _Чтобы посмотреть расписание учителя: `/teacher <фамилия>`_")
        return "\n".join(lines).strip()

    def search_school(self, query: str) -> Dict[str, Any]:
        """
        Универсальный поиск по школе:
        - классы
        - учителя
        - кабинеты
        - предметы
        """
        q = query.strip()
        q_lower = q.lower()
        res = {
            "query": q,
            "classes": [],
            "teachers": [],
            "classrooms": [],
            "subjects": []
        }

        # 1. Поиск по классам
        norm_c = self.normalize_class_name(q)
        classes = self.get_classes()
        if norm_c in classes:
            res["classes"].append(norm_c)
        else:
            for c in classes:
                if q_lower in c.lower():
                    res["classes"].append(c)

        # 2. Поиск по учителям
        norm_t = self.normalize_teacher_name(q)
        if norm_t:
            res["teachers"].append(norm_t)
        else:
            teachers = self.get_teachers()
            for t in teachers:
                if q_lower in t.lower() or q_lower in self.transliterate_ru_to_et(t).lower():
                    if t not in res["teachers"]:
                        res["teachers"].append(t)

        # 3. Поиск по кабинетам
        clean_room = re.sub(r'^(каб\.?|кабинет|room)\s*', '', q_lower, flags=re.IGNORECASE).strip()
        all_rooms = self.get_all_classrooms()
        for r in all_rooms:
            if clean_room == r.lower() or (len(clean_room) >= 2 and clean_room in r.lower()):
                res["classrooms"].append(r)

        # 4. Поиск по предметам
        tt = self.get_regular_timetable()
        all_subjects = set()
        for c_days in tt.get("schedule", {}).values():
            for day_lessons in c_days.values():
                for cards in day_lessons.values():
                    for card in cards:
                        subj = card.get("subject", "")
                        if subj and q_lower in subj.lower():
                            all_subjects.add(subj)
        res["subjects"] = sorted(list(all_subjects))

        return res

    def format_search_for_telegram(self, query: str) -> str:
        """Форматирует результат универсального поиска"""
        res = self.search_school(query)
        q = res["query"]

        has_results = any(res[k] for k in ["classes", "teachers", "classrooms", "subjects"])
        if not has_results:
            return (
                f"🔍 *Поиск по запросу «{q}»*\n\n"
                f"❌ Ничего не найдено.\n"
                f"Попробуйте указать класс (напр. `8K`), фамилию учителя (напр. `Кулакова`), "
                f"кабинет (напр. `204`) или предмет (напр. `Keemia`)."
            )

        lines = [f"🔍 *Результаты поиска «{q}»:*\n"]

        if res["classes"]:
            cls_str = ", ".join([f"*{c}*" for c in res["classes"][:10]])
            lines.append(f"🏫 *Классы:* {cls_str}")
            lines.append("  _Команда:_ `/today <класс>` или `/schedule <класс>`\n")

        if res["teachers"]:
            lines.append("👩‍🏫 *Преподаватели:*")
            for t in res["teachers"][:5]:
                t_info = self.get_teacher_classrooms(t)
                prim = t_info.get("primary_room")
                prim_str = f" [🚪 каб. *{prim}*]" if prim else ""
                lines.append(f"  • *{t}*{prim_str} — `/teacher {t}`")
            lines.append("")

        if res["classrooms"]:
            lines.append(f"🚪 *Кабинеты:* {', '.join([f'*{r}*' for r in res['classrooms'][:10]])}")
            lines.append("  _Команда:_ `/room <номер>`\n")

        if res["subjects"]:
            lines.append(f"📖 *Предметы:* {', '.join(res['subjects'][:8])}\n")

        return "\n".join(lines).strip()
