"""Сопоставление одного и того же отделения между источниками (2ГИС, Яндекс, Домклик…).

Правила (порядок важен):
1. Отделения ОДНОГО источника никогда не объединяются: у источника каждая
   карточка — отдельный объект (два офиса в одном торговом центре — два офиса).
2. Кандидаты из другого источника — только рядом: расстояние ≤ GEO_STRONG_M,
   или ≤ GEO_WEAK_M при совпадении ключа адреса (город + улица + дом).
3. Название — только подсказка при равенстве: по похожему названию без адреса
   и координат отделения не объединяются никогда.
4. Однозначность: пара принимается, только если каждый — лучший кандидат другого
   и у лучшего нет соседа-соперника в пределах GEO_STRONG_M с тем же домом
   (иначе — конфликт на ручной разбор, а не склейка).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import cached_property

GEO_STRONG_M = 60.0       # почти наверняка то же здание
GEO_WEAK_M = 250.0        # рядом — только при совпадении адреса

_ABBR = [
    (r"\bпроспект\b|\bпросп\.?\b|\bпр-т\b|\bпр-кт\b|\bпр\.\b", "пр-кт"),
    (r"\bулица\b|\bул\.?\b", "ул"),
    (r"\bпереулок\b|\bпер\.?\b", "пер"),
    (r"\bбульвар\b|\bб-р\b", "б-р"),
    (r"\bшоссе\b|\bш\.\b", "ш"),
    (r"\bплощадь\b|\bпл\.?\b", "пл"),
    (r"\bнабережная\b|\bнаб\.?\b", "наб"),
    (r"\bмикрорайон\b|\bмкр\.?\b|\bмкрн\.?\b", "мкр"),
    (r"\bпроезд\b|\bпр-д\b", "проезд"),
    (r"\bтупик\b|\bтуп\.?\b", "туп"),
    (r"\bаллея\b", "аллея"),
]
_TYPES = r"(ул|пр-кт|пер|б-р|ш|пл|наб|мкр|проезд|туп|аллея)"
_ADMIN = re.compile(r"\b(россия|российская федерация|область|обл|край|республика|респ|район|р-н|"
                    r"автономный округ|ао|городской округ|го|муниципальный округ|с/с|сельсовет|"
                    r"сельское поселение|с/п|поселение)\b")
_CITY_PREFIX = re.compile(r"^(г|город|пос[её]лок|пос|п|пгт|рп|с|село|д|дер|деревня|ст-ца|станица|х|хутор|"
                          r"кп|аул|сл|слобода|нп)\.?\s+")
# дом: «д. 15», «зд. 13» (здание), «вл. 2» / «влд. 2» (владение); корпус: «к1», «корп. 1», «/1»;
# строение: «с1», «ст1», «стр. 1», «строение 1» — у ЦБ и 2ГИС одно здание пишется по-разному
_HOUSE_RX = re.compile(r"(?:\b(?:д|дом|зд|здание|вл|влд|владение)\.?\s*)?(\d+[а-я]?)"
                       r"(?:\s*(?:к(?:орп(?:ус)?)?\.?\s*|/)(\d+[а-я]?))?"
                       r"(?:\s*,?\s*(?:с(?:т(?:р(?:оение)?)?)?\.?)\s*(\d+))?")


def norm_text(s: str | None) -> str:
    s = (s or "").lower().replace("ё", "е").replace("№", " ")
    s = re.sub(r"[«»\"'`]", " ", s)
    s = re.sub(r"\.(?=[а-яa-z])", ". ", s)          # «ул.ленина», «г.барнаул» — пробел после точки
    return re.sub(r"\s+", " ", s).strip()


def norm_city(city: str | None) -> str:
    return _CITY_PREFIX.sub("", norm_text(city)).strip()


def _house(m: re.Match) -> str:
    return m.group(1) + (f"к{m.group(2)}" if m.group(2) else "") + (f"с{m.group(3)}" if m.group(3) else "")


def address_key(address: str | None, city: str | None = None) -> str | None:
    """Ключ адреса «город|улица|дом[к корпус][с строение]» или None, если улицу или дом
    выделить не удалось. Форматы источников разные: «Россия, Москва, улица Ленина,
    15», «Ленина, 15», «г. Москва, ул. Ленина, д. 15», «Тверская ул., 7, Москва»."""
    a = norm_text(address)
    if not a:
        return None
    for pat, rep in _ABBR:
        a = re.sub(pat, rep, a)
    c = norm_city(city)
    parts = []
    for p in (x.strip(" .") for x in a.split(",")):
        if not p or re.fullmatch(r"\d{6}", p) or _ADMIN.search(p):
            continue
        # «г. Химки», «пос. Внуково» — населённый пункт; «д. 15» (дом) — нет: в нём цифры
        if c and norm_city(p) == c or (_CITY_PREFIX.match(p) and not re.search(r"\d", p)
                                       and not re.search(r"\b" + _TYPES + r"\b", p)):
            continue
        parts.append(p)
    for i, p in enumerate(parts):
        words = re.sub(r"\b(д|дом|зд|здание|вл|влд|владение|пом|помещение|"
                       r"стр|строение|корп|корпус|оф|офис|эт|этаж)\b|\d+[а-я]?\b", " ", p)
        if len(re.sub(r"[^а-яa-z]", "", words)) < 3 or not re.search(r"[а-яa-z]{2}", words):
            continue
        street_part, house = p, None
        m = re.search(r"(?:\s|^)" + _HOUSE_RX.pattern + r"\s*$", p)
        if m and m.start() > 0:
            house, street_part = _house(m), p[:m.start()]
        elif i + 1 < len(parts):
            # строение бывает отдельной частью: «д. 165, строение 1, пом. 2н»
            tail = parts[i + 1] + (" " + parts[i + 2] if i + 2 < len(parts)
                                   and re.match(r"с(т(р(оение)?)?)?\.?\s*\d", parts[i + 2]) else "")
            m2 = _HOUSE_RX.match(tail)
            if m2:
                house = _house(m2)
        if not house:
            continue            # «пос. Внуково» без дома — не улица, смотрим дальше
        street = re.sub(r"\b" + _TYPES + r"\b", " ", street_part)
        if not re.search(r"[а-яa-z]{3}", street):
            street = street_part           # улица «Набережная», «Площадь»: тип и есть название
        street = re.sub(r"[^\w\s-]", " ", street)
        street = " ".join(sorted(w for w in street.split() if len(w) > 1 or w.isdigit()))
        return f"{c}|{street}|{house}" if street else None
    return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class Item:
    source: str
    sid: str
    lat: float | None
    lon: float | None
    address: str | None
    city: str | None
    name: str | None = None

    @cached_property
    def akey(self) -> str | None:
        # ключ адреса считается регулярками — один раз на карточку, а не на каждую пару
        return address_key(self.address, self.city)

    @property
    def has_geo(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass
class Match:
    a: Item
    b: Item
    distance_m: float | None
    same_address: bool
    score: float
    status: str           # auto | conflict
    reason: str


def _score(a: Item, b: Item) -> tuple[float | None, bool, float] | None:
    """(расстояние, совпал ли адрес, оценка 0..1) или None, если пара невозможна."""
    same = bool(a.akey and b.akey and a.akey == b.akey)
    d = None
    if None not in (a.lat, a.lon, b.lat, b.lon):
        d = haversine_m(a.lat, a.lon, b.lat, b.lon)   # type: ignore[arg-type]
    if d is not None:
        if d <= GEO_STRONG_M:
            return d, same, 0.9 + (0.1 if same else 0.0) - d / GEO_STRONG_M * 0.05
        if d <= GEO_WEAK_M and same:
            return d, same, 0.75 - d / GEO_WEAK_M * 0.1
        return None
    # без координат — только точное совпадение адреса (и это всегда на проверку)
    return (None, True, 0.6) if same else None


def _cell(lat: float, lon: float) -> tuple[int, int]:
    """Ячейка сетки ≈ 1,1 км по широте и 0,02° по долготе (≥ 380 м даже на 80° с. ш.):
    соседние ячейки покрывают радиус GEO_WEAK_M = 250 м с запасом."""
    return math.floor(lat * 100), math.floor(lon * 50)


def match_sources(left: list[Item], right: list[Item]) -> list[Match]:
    """Пары «то же отделение» между двумя источниками (left и right — разные источники).
    Взаимно-лучшие пары принимаются автоматически; спорные — со статусом conflict.

    Кандидаты ищутся не перебором всех пар (на всей сети это ~10⁸ сравнений), а:
    с координатами — в соседних ячейках сетки; без координат — по точному ключу адреса
    (без координат пара возможна только при совпадении адреса — см. _score)."""
    grid: dict[tuple[int, int], list[int]] = {}
    nogeo_by_key: dict[str, list[int]] = {}
    by_key: dict[str, list[int]] = {}
    for j, b in enumerate(right):
        k = b.akey
        if b.has_geo:
            grid.setdefault(_cell(b.lat, b.lon), []).append(j)   # type: ignore[arg-type]
        elif k:
            nogeo_by_key.setdefault(k, []).append(j)
        if k:
            by_key.setdefault(k, []).append(j)
    cand: dict[tuple[int, int], tuple[float | None, bool, float]] = {}
    for i, a in enumerate(left):
        if a.has_geo:
            ci, cj = _cell(a.lat, a.lon)                          # type: ignore[arg-type]
            js = [j for di in (-1, 0, 1) for dj in (-1, 0, 1) for j in grid.get((ci + di, cj + dj), ())]
            if a.akey:
                js += nogeo_by_key.get(a.akey, [])
        else:
            js = by_key.get(a.akey, []) if a.akey else []
        for j in js:
            s = _score(a, right[j])
            if s:
                cand[(i, j)] = s
    best_l: dict[int, tuple[int, float]] = {}
    best_r: dict[int, tuple[int, float]] = {}
    per_l: dict[int, list[tuple[int, float | None, bool]]] = {}
    per_r: dict[int, list[tuple[int, float | None, bool]]] = {}
    for (i, j), (d, same, sc) in cand.items():
        if sc > best_l.get(i, (-1, -1.0))[1]:
            best_l[i] = (j, sc)
        if sc > best_r.get(j, (-1, -1.0))[1]:
            best_r[j] = (i, sc)
        per_l.setdefault(i, []).append((j, d, same))
        per_r.setdefault(j, []).append((i, d, same))
    out: list[Match] = []
    for i, (j, sc) in best_l.items():
        d, same, _ = cand[(i, j)]
        mutual = best_r.get(j, (-1, 0))[0] == i
        # соперники: другой объект того же источника почти там же и с тем же адресом
        rivals_r = [jj for jj, dd, ss in per_l.get(i, ())
                    if jj != j and (dd is None or dd <= GEO_STRONG_M) and (ss or not same)]
        rivals_l = [ii for ii, dd, ss in per_r.get(j, ())
                    if ii != i and (dd is None or dd <= GEO_STRONG_M) and (ss or not same)]
        if d is None:
            status, reason = "conflict", "совпал только адрес, координат нет"
        elif not mutual:
            status, reason = "conflict", "не взаимно-лучшая пара"
        elif rivals_r or rivals_l:
            status, reason = "conflict", "рядом несколько отделений того же источника"
        else:
            status, reason = "auto", ("рядом и тот же адрес" if same else "в пределах одного здания")
        out.append(Match(left[i], right[j], d, same, round(sc, 3), status, reason))
    return out
