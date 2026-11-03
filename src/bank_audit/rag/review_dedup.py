"""Склейка копий одного отзыва между площадками.

Автор пишет одну историю на banki.ru, sravni и finuslugi — это одна жалоба, а не
три. Индекс отзывов (review_index) ловил копии только по адресу и номеру отзыва
banki.ru (review_annotate._external_ids: наш сборщик и внешний корпус пишут один
отзыв с разным написанием адреса). Копия с другой площадки — другой адрес и другой
номер, и в счётчиках жалоб она шла вторым и третьим случаем.

Старый путь нормализации (normalizer/reviews.py) склеивал копии по хэшу первых 400
знаков нормализованного текста, дню публикации и оценке: любая правка в начале
(«Здравствуйте!» на одной площадке и нет на другой), сдвиг даты на сутки или другая
шкала оценок у площадки ломали ключ. Здесь — сходство текстов целиком:

1. Нормализация (идея старого пути, расширенная): NFKC, регистр, ё→е, без
   пунктуации, любое число — один токен «#» («5 000 ₽» и «5000р» не различаются).
2. Шинглы — по 5 слов подряд, хэш blake2b: детерминирован между процессами, в
   отличие от встроенного hash() со случайной солью.
3. MinHash на 128 перестановок и LSH-бандинг дают кандидатов, не сравнивая все
   пары; решение — по точному Jaccard множеств шинглов кандидатов.
4. Правила копии: тот же банк, даты не дальше окна (14 дней), Jaccard ≥ 0,8.
   Каноническая копия — из корпуса banki.ru (source 'bankiru'), иначе самая
   ранняя. Копия ссылается на неё (review_index.dup_of), строки не удаляются.

Шаблон «не пришёл кэшбэк» у разных людей — разные истории: общие у них отдельные
фразы, а не цепочки по пять слов подряд, Jaccard таких пар — десятые доли.

Встраивание: tick() — из фонового цикла индекса отзывов (digest/scheduler.py) после
sync_local; счётчики жалоб берут строки с пустым dup_of. Схема —
migrations/103_review_dup.sql. Проверка на живых данных без записи:
python -m bank_audit.rag.review_dedup --dry (пограничные пары — в examples).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import numpy as np
from sqlalchemy import bindparam, text
from sqlalchemy.types import DateTime

from .. import db

log = logging.getLogger(__name__)

WINDOW_DAYS = int(os.getenv("REVIEW_DEDUP_WINDOW_DAYS", "14"))
THRESHOLD = float(os.getenv("REVIEW_DEDUP_THRESHOLD", "0.8"))
# Инкремент пересматривает последние N дней: внешний корпус приходит с задержкой
# в сутки-двое, свои коллекторы перечитывают площадки дважды в сутки — 30 дней
# с запасом перекрывают и опоздавшие копии, и окно склейки.
SINCE_DAYS = int(os.getenv("REVIEW_DEDUP_SINCE_DAYS", "30"))
# Полный проход — с начала корпуса вкладки «Аудит отзывов» (с 01.01.2025)
SINCE = os.getenv("REVIEW_DEDUP_SINCE", "2025-01-01")
EVERY_S = int(os.getenv("REVIEW_DEDUP_EVERY_S", str(6 * 3600)))
FULL_EVERY_S = int(os.getenv("REVIEW_DEDUP_FULL_EVERY_S", str(7 * 86400)))   # 0 — без полных
CHUNK = 1000

SHINGLE = 5                 # слов в шингле
NUM_PERM = 128              # перестановок MinHash
# Короче 14 слов не склеиваем: одну фразу («карту заблокировали, поддержка молчит»)
# разные люди пишут слово в слово
MIN_SHINGLES = 10
MAX_TOKENS = 4000           # дальше не читаем: решить «копия или нет» хватает начала
TARGET_RECALL = 0.999       # доля пар с Jaccard на пороге, которую обязан найти бандинг
_EST_MARGIN = 0.2           # отсев кандидатов по оценке MinHash: 0,2 ниже порога — больше 5 сигм
_MAX_NEIGHBORS = 300        # в одной корзине бандинга сравниваем не больше стольких соседей по дате
CANON_SOURCE = "bankiru"    # корпус banki.ru — владелец канонической копии
# Тексты не забрались (соседняя база недоступна) — банк не пересматриваем: без
# канонических копий их копии стали бы «самостоятельными» жалобами.
MAX_MISSING_SHARE = 0.2
_MISSING_MIN = 5
_LOCK_KEY = 710_022_420
_AT_KEY = "dedup_at"
_FULL_KEY = "dedup_full_at"


# ── Текст → отпечаток ────────────────────────────────────────────────────────

# Число вместе с разделителями разрядов, дат и времени: «5 000», «12.03.2025», «10:30».
# NFKC уже превратил неразрывные пробелы в обычные.
_NUM = re.compile(r"\d+(?:[ .,:/-]\d+)*")
_TOKEN = re.compile(r"[^\W\d_]+|#")


def tokens(text_: str | None) -> list[str]:
    """Слова текста для сравнения: регистр, ё→е, без пунктуации, число — «#»."""
    s = unicodedata.normalize("NFKC", text_ or "").lower().replace("ё", "е")
    return _TOKEN.findall(_NUM.sub(" # ", s))[:MAX_TOKENS]


def _h64(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(), "little")


def shingles(toks: list[str], k: int = SHINGLE) -> np.ndarray:
    """Хэши шинглов — k слов подряд; отсортированы, без повторов (uint64)."""
    if len(toks) < k:
        return np.empty(0, np.uint64)
    return np.unique(np.array([_h64(" ".join(toks[i:i + k])) for i in range(len(toks) - k + 1)],
                              dtype=np.uint64))


_M1 = np.uint64(0xBF58476D1CE4E5B9)
_M2 = np.uint64(0x94D049BB133111EB)
_R30, _R27, _R31 = np.uint64(30), np.uint64(27), np.uint64(31)


def _mix(z: np.ndarray) -> np.ndarray:
    """Финализатор splitmix64 — взаимно однозначен на 64 битах (умножение по модулю
    2^64: numpy на массивах переполняется без предупреждений)."""
    z = (z ^ (z >> _R30)) * _M1
    z = (z ^ (z >> _R27)) * _M2
    return z ^ (z >> _R31)


# Перестановка i — x → mix(x XOR seed_i): XOR с константой и mix биективны, значит
# каждая — честная перестановка 64-битных хэшей. Соли — из blake2b, а не из
# генератора случайных чисел: подпись одного текста одинакова в любом процессе и версии numpy.
_SEEDS = np.array([_h64(f"review_dedup/perm/{i}") for i in range(NUM_PERM)], dtype=np.uint64)


def minhash(sh: np.ndarray) -> np.ndarray:
    """Подпись MinHash: минимум каждой из NUM_PERM перестановок по шинглам (uint64)."""
    if sh.size == 0:
        return np.full(NUM_PERM, np.iinfo(np.uint64).max, np.uint64)
    return _mix(sh[None, :] ^ _SEEDS[:, None]).min(axis=1)


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """Отпечаток текста. Хранится в 32 битах: на полном проходе у Сбера десятки
    тысяч отзывов в памяти разом, а совпадение двух разных шинглов в 32 битах
    (≈ 200²/2³² на пару текстов) Jaccard не сдвигает."""
    sh: np.ndarray      # uint32, отсортированные хэши шинглов — для точного Jaccard
    sig: np.ndarray     # uint32[NUM_PERM] — подпись MinHash — для бандинга и оценки

    @property
    def size(self) -> int:
        return int(self.sh.size)


def fingerprint(text_: str | None) -> Fingerprint | None:
    """Отпечаток текста или None, если текст слишком короткий для склейки."""
    sh = shingles(tokens(text_))
    if sh.size < MIN_SHINGLES:
        return None
    return Fingerprint(np.unique(sh.astype(np.uint32)), minhash(sh).astype(np.uint32))


def jaccard(a: Fingerprint, b: Fingerprint) -> float:
    """Точный Jaccard множеств шинглов."""
    inter = np.intersect1d(a.sh, b.sh, assume_unique=True).size
    return inter / (a.sh.size + b.sh.size - inter)


def jaccard_estimate(a: Fingerprint, b: Fingerprint) -> float:
    """Оценка Jaccard по MinHash: доля совпавших минимумов (ошибка ≈ 0,035 у 0,8)."""
    return float(np.count_nonzero(a.sig == b.sig)) / NUM_PERM


def lsh_params(threshold: float, num_perm: int = NUM_PERM,
               recall: float = TARGET_RECALL) -> tuple[int, int]:
    """(полос, строк в полосе) для бандинга: самые длинные полосы (меньше ложных
    кандидатов), при которых пара с Jaccard ровно на пороге становится кандидатом с
    вероятностью не ниже recall. Для 0,8 — 25 полос по 5 строк: пара с Jaccard 0,8
    теряется с вероятностью 5·10⁻⁵, а пара разных историй с Jaccard 0,3 становится
    кандидатом в 6% случаев (её отсеет точный Jaccard)."""
    if not 0 < threshold <= 1:
        raise ValueError(f"порог Jaccard вне (0, 1]: {threshold}")
    best = (num_perm, 1)
    for r in range(1, num_perm + 1):
        b = num_perm // r
        if 1 - (1 - threshold ** r) ** b >= recall:
            best = (b, r)
    return best


def _band_keys(sig: np.ndarray, j: int, r: int) -> np.ndarray:
    """Ключ полосы j для всех подписей разом: равные полосы — равные ключи
    (случайное совпадение разных лишь добавит кандидата, его отсеет точный Jaccard)."""
    acc = np.zeros(sig.shape[0], np.uint64)
    for t in range(j * r, (j + 1) * r):
        acc = _mix(acc ^ sig[:, t].astype(np.uint64))
    return acc


# ── Решение: кто чья копия ───────────────────────────────────────────────────

@dataclass(slots=True)
class _Doc:
    url: str
    bank: str
    source: str
    ts: float                    # момент публикации — для «самой ранней»
    day: int                     # календарный день — для окна: площадки пишут время в разных поясах
    fp: Fingerprint | None
    locked: bool = False         # решение прошлых проходов — не пересматриваем
    cur: str | None = None       # нынешняя dup_of


def _rank(d: _Doc) -> tuple:
    """Старшинство на роль канонической: корпус banki.ru, затем самая ранняя, затем адрес."""
    return (0 if d.source == CANON_SOURCE else 1, d.ts, d.url)


def _as_dt(v) -> datetime | None:
    """Дата отзыва в наивное время: в индексе naive timestamp, в тестах — строка."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    try:
        return _as_dt(datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _doc(r: dict, fp: Fingerprint | None, *, locked: bool = False) -> _Doc | None:
    dt = _as_dt(r.get("dt"))
    if dt is None or not r.get("url") or not r.get("bank"):
        return None
    return _Doc(url=str(r["url"]), bank=str(r["bank"]), source=str(r.get("source") or CANON_SOURCE),
                ts=dt.replace(tzinfo=timezone.utc).timestamp(), day=dt.toordinal(), fp=fp,
                locked=locked, cur=r.get("dup_of"))


def _edges(docs: list[_Doc], *, window_days: int, threshold: float,
           cross_source_only: bool) -> dict[int, dict[int, float]]:
    """Пары-копии {i: {j: jaccard}}: один банк, дни не дальше окна, Jaccard ≥ порога.
    Кандидаты — из совпавших полос MinHash, решение — по точному Jaccard."""
    nb, r = lsh_params(threshold)
    edges: dict[int, dict[int, float]] = defaultdict(dict)
    by_bank: dict[str, list[int]] = defaultdict(list)
    for k, d in enumerate(docs):
        if d.fp is not None:
            by_bank[d.bank].append(k)
    for bank in sorted(by_bank):
        idx = by_bank[bank]
        if len(idx) < 2:
            continue
        sig = np.stack([docs[k].fp.sig for k in idx])
        day = np.array([docs[k].day for k in idx], dtype=np.int64)
        n = len(idx)
        seen: set[int] = set()
        for j in range(nb):
            keys = _band_keys(sig, j, r)
            order = np.argsort(keys, kind="stable")
            ks = keys[order]
            cut = np.flatnonzero(ks[1:] != ks[:-1]) + 1
            starts, ends = np.r_[0, cut], np.r_[cut, n]
            many = (ends - starts) > 1
            for s, e in zip(starts[many].tolist(), ends[many].tolist(), strict=True):
                grp = order[s:e]
                grp = grp[np.argsort(day[grp], kind="stable")].tolist()
                for a in range(len(grp)):
                    for c in range(a + 1, min(len(grp), a + 1 + _MAX_NEIGHBORS)):
                        if day[grp[c]] - day[grp[a]] > window_days:
                            break
                        p, q = (grp[a], grp[c]) if grp[a] < grp[c] else (grp[c], grp[a])
                        key = p * n + q
                        if key in seen:
                            continue
                        seen.add(key)
                        dp, dq = docs[idx[p]], docs[idx[q]]
                        if dp.locked and dq.locked:
                            continue                       # обе решены прошлыми проходами
                        if cross_source_only and dp.source == dq.source:
                            continue
                        lo_, hi_ = sorted((dp.fp.size, dq.fp.size))
                        if lo_ < threshold * hi_:
                            continue                       # Jaccard не больше отношения размеров
                        if jaccard_estimate(dp.fp, dq.fp) < threshold - _EST_MARGIN:
                            continue
                        jv = jaccard(dp.fp, dq.fp)
                        if jv >= threshold:
                            edges[idx[p]][idx[q]] = jv
                            edges[idx[q]][idx[p]] = jv
    return edges


def _decide(docs: list[_Doc], edges: dict[int, dict[int, float]]) -> dict[int, tuple[str, float]]:
    """{номер копии: (адрес канонической, jaccard)} для незакреплённых строк.

    1. Строка, похожая на уже решённую (закреплённую), идёт к ней: к самой строке,
       если та каноническая, или к её канонической, если та — копия. Решённое
       прошлыми проходами не перекраиваем, а опоздавшая копия не становится
       второй жалобой.
    2. Остальные — по старшинству (banki.ru, затем самая ранняя): ещё не занятая
       строка становится канонической и забирает своих соседей. Каждая копия
       сравнена с канонической напрямую — и окно, и порог выполнены для этой пары,
       цепочка «A похожа на B, B на C» не склеивает далёкие A и C.
    """
    order = sorted(range(len(docs)), key=lambda k: _rank(docs[k]))
    out: dict[int, tuple[str, float]] = {}
    taken: set[int] = set()
    for u in order:
        d = docs[u]
        if d.locked:
            continue
        best = None
        for v, jv in edges.get(u, {}).items():
            dv = docs[v]
            if not dv.locked:
                continue
            target = dv.cur or dv.url
            if target == d.url:
                continue                               # закреплённая копия этой самой строки
            if best is None or _rank(dv) < _rank(docs[best[0]]):
                best = (v, target, jv)
        if best:
            out[u] = (best[1], best[2])
            taken.add(u)
    for c in order:
        if c in taken or docs[c].locked:
            continue
        taken.add(c)
        for v in sorted(edges.get(c, {}), key=lambda k: _rank(docs[k])):
            if v in taken or docs[v].locked:
                continue
            out[v] = (docs[c].url, edges[c][v])
            taken.add(v)
    # Каноническая из шага 1 могла сама стать копией на шаге 2 — ведём к корню,
    # чтобы dup_of всегда указывала на строку с пустой dup_of.
    pos = {d.url: k for k, d in enumerate(docs) if not d.locked}
    for u in list(out):
        target, jv = out[u]
        hops = 0
        while target in pos and pos[target] in out and hops < 8:
            target = out[pos[target]][0]
            hops += 1
        if target == docs[u].url:
            del out[u]
        else:
            out[u] = (target, jv)
    return out


def _find(rows: Iterable[dict], *, window_days: int, threshold: float,
          cross_source_only: bool) -> tuple[list[_Doc], dict[int, tuple[str, float]]]:
    docs = []
    for r in rows:
        fp = r["fp"] if "fp" in r else fingerprint(r.get("text"))
        d = _doc(r, fp, locked=bool(r.get("locked")))
        if d is not None:
            docs.append(d)
    edges = _edges(docs, window_days=window_days, threshold=threshold,
                   cross_source_only=cross_source_only)
    return docs, _decide(docs, edges)


def find_dups(rows: Iterable[dict], *, window_days: int = WINDOW_DAYS, threshold: float = THRESHOLD,
              cross_source_only: bool = False) -> dict[str, str]:
    """Копии одной истории: {адрес копии: адрес канонической}.

    rows — словари url, bank, dt, source и text (или готовый fp — отпечаток).
    Правила: тот же банк, |dt| ≤ window_days дней, Jaccard ≥ threshold; каноническая —
    из корпуса banki.ru, иначе самая ранняя. Канонические и одиночки в ответ не
    входят. cross_source_only — склеивать только между разными площадками.
    Необязательно: locked=True — строка уже решена (сама не помечается; если её
    dup_of задана — она копия, и похожие на неё идут к её канонической)."""
    docs, out = _find(rows, window_days=window_days, threshold=threshold,
                      cross_source_only=cross_source_only)
    return {docs[k].url: canon for k, (canon, _jv) in out.items()}


# ── Пакетный проход по индексу ───────────────────────────────────────────────

def _bodies(rows: list[dict]) -> dict[str, dict]:
    from .bankiru_fts import bodies_for
    return bodies_for(rows)


_DT = DateTime()
_BANKS = text("SELECT DISTINCT bank FROM review_index WHERE dt >= :lo AND bank IS NOT NULL"
              ).bindparams(bindparam("lo", type_=_DT))
_ROWS = text("""
    SELECT url, review_id, source, bank, dt, dup_of FROM review_index
     WHERE bank = :bank AND dt >= :lo ORDER BY dt, url""").bindparams(bindparam("lo", type_=_DT))
_SET = text("UPDATE review_index SET dup_of = :d WHERE url = :u")
# Каноническая строка пропала (sync_local убирает свою копию banki.ru, когда отзыв
# пришёл во внешний корпус) или ссылка на саму себя — строка снова считается.
_ORPHAN = text("""
    UPDATE review_index SET dup_of = NULL
     WHERE dup_of IS NOT NULL
       AND (dup_of = url OR NOT EXISTS (
            SELECT 1 FROM review_index p WHERE p.url = review_index.dup_of))""")
# Каноническая сама стала копией (пересмотр окна, опоздавшая копия banki.ru) —
# ссылку ведём к корню: «счётная» строка у истории всегда одна.
_CHAIN = text("""
    UPDATE review_index SET dup_of = (
            SELECT p.dup_of FROM review_index p WHERE p.url = review_index.dup_of)
     WHERE dup_of IS NOT NULL
       AND EXISTS (SELECT 1 FROM review_index p
                    WHERE p.url = review_index.dup_of AND p.dup_of IS NOT NULL)""")


def _retry(fn: Callable[[], int], what: str, attempts: int = 5) -> int | None:
    """Запись с повторами: фоновые задачи пишут индекс параллельно, крупный UPDATE
    ловил взаимную блокировку (как в review_slices.apply_to_index)."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if attempt == attempts - 1:
                log.warning("склейка копий: %s не записалось (%s: %s)", what, type(e).__name__, e)
                return None
            time.sleep(2 + attempt * 3)
    return None


def write(changes: list[tuple[str, str | None]], chunk: int = CHUNK) -> tuple[int, int]:
    """dup_of по адресам пакетами по chunk строк с повторами → (записано, не записано)."""
    done = failed = 0
    changes = sorted(changes)                  # один порядок строк — меньше взаимных блокировок
    for k in range(0, len(changes), chunk):
        part = [{"u": u, "d": d} for u, d in changes[k:k + chunk]]

        def go(part=part) -> int:
            with db.session() as s:
                s.execute(_SET, part)
            return len(part)
        n = _retry(go, f"пакет из {len(part)}")
        if n is None:
            failed += len(part)
        else:
            done += n
    return done, failed


def repair() -> int:
    """Ссылки на пропавшие строки — сбросить, цепочки копий — свести к корню."""
    def ex(stmt) -> int:
        with db.session() as s:
            return int(s.execute(stmt).rowcount or 0)
    n = _retry(lambda: ex(_ORPHAN), "сброс ссылок") or 0
    for _ in range(5):
        k = _retry(lambda: ex(_CHAIN), "сведение цепочек") or 0
        n += k
        if not k:
            break
    n += _retry(lambda: ex(_ORPHAN), "сброс ссылок") or 0     # цикл A→B→A свёлся в ссылку на себя
    return n


class _Lock:
    """Сессионный advisory lock Postgres: второй проход не стартует. Транзакцию не
    держим — замок живёт на соединении. На SQLite (тесты) замка нет."""

    def __init__(self):
        self.conn = None

    def acquire(self) -> bool:
        with db.session() as s:
            eng = s.get_bind()
        if eng.dialect.name != "postgresql":
            return True
        conn = eng.connect()
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
        conn.commit()
        if not got:
            conn.close()
            return False
        self.conn = conn
        return True

    def release(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
            self.conn.commit()
        except Exception:  # noqa: BLE001
            self.conn.invalidate()
        finally:
            self.conn.close()
            self.conn = None


def _bank_pass(bank: str, *, since: datetime, lo: datetime, window_days: int, threshold: float,
               cross_source_only: bool, chunk: int) -> dict:
    """Один банк: тексты, отпечатки, решение, разница с базой (без записи)."""
    with db.session() as s:
        rows = [dict(r) for r in s.execute(_ROWS, {"bank": bank, "lo": lo}).mappings().all()]
    res = {"rows": len(rows), "revisable": 0, "short": 0, "missing": 0, "frozen": 0,
           "changes": [], "marks": [], "dups": 0, "pairs": Counter(), "skipped": None}
    if not rows:
        return res
    # Отпечатки — по ходу чтения пакетами: тексты всего Сбербанка за полный проход
    # разом в памяти — сотни мегабайт, отпечаток в несколько раз меньше текста.
    fps: dict[str, Fingerprint | None] = {}
    for k in range(0, len(rows), chunk):
        part = rows[k:k + chunk]
        bodies = _bodies(part) or {}
        for r in part:
            if r["url"] in bodies:
                fps[r["url"]] = fingerprint((bodies[r["url"]] or {}).get("text"))
    missing = {r["url"] for r in rows if r["url"] not in fps}
    res["missing"] = len(missing)
    if len(missing) > max(_MISSING_MIN, MAX_MISSING_SHARE * len(rows)):
        res["skipped"] = f"нет текстов у {len(missing)} из {len(rows)}"
        return res
    # Строки без текста и копии таких строк остаются как есть: без текста
    # канонической её копия «потеряла» бы её и стала отдельной жалобой.
    frozen = missing | {r["url"] for r in rows if r.get("dup_of") in missing}
    res["frozen"] = len(frozen)
    docs: list[_Doc] = []
    for r in rows:
        if r["url"] in missing:
            continue
        dt = _as_dt(r["dt"])
        if dt is None:
            continue
        fp = fps[r["url"]]
        d = _doc(r, fp, locked=dt < since or r["url"] in frozen)
        if d is None:
            continue
        if not d.locked:
            res["revisable"] += 1
            res["short"] += fp is None
        docs.append(d)
    edges = _edges(docs, window_days=window_days, threshold=threshold,
                   cross_source_only=cross_source_only)
    out = _decide(docs, edges)
    src = {d.url: d.source for d in docs}
    for k, d in enumerate(docs):
        if d.locked:
            continue
        want, jv = out.get(k, (None, None))
        if want is not None:
            res["dups"] += 1
            res["pairs"][f"{d.source}→{src.get(want, '?')}"] += 1
        if want != d.cur:
            res["changes"].append((d.url, want))
            if want is not None:
                res["marks"].append({"url": d.url, "canon": want, "jaccard": round(jv, 3),
                                     "bank": bank, "source": d.source,
                                     "canon_source": src.get(want, "?")})
    return res


def run(since_days: int | None = SINCE_DAYS, *, window_days: int = WINDOW_DAYS,
        threshold: float = THRESHOLD, cross_source_only: bool = False, dry: bool = False,
        chunk: int = CHUNK, now: datetime | None = None, examples: int = 20) -> dict:
    """Пакетный проход по review_index: помечает копии (dup_of = адрес канонической).

    Пересматриваются строки за последние since_days дней (None — с REVIEW_DEDUP_SINCE,
    полный проход). Строки на окно раньше — контекст: их решения не меняются, но
    новые копии к ним приклеиваются. Ничего не удаляется. Повторный проход по тем
    же данным ничего не пишет. dry — посчитать без записи; examples — сколько новых
    пометок с самым низким Jaccard вернуть для проверки глазами."""
    t0 = time.time()
    now = _as_dt(now) or datetime.now(timezone.utc).replace(tzinfo=None)
    since = _as_dt(SINCE) if since_days is None else now - timedelta(days=since_days)
    lo = since - timedelta(days=window_days + 1)       # +1: окно — по календарным дням
    lock = _Lock()
    if not lock.acquire():
        return {"skipped": "locked"}
    stats = {"mode": "full" if since_days is None else f"{since_days}d", "dry": dry,
             "since": since.isoformat(sep=" "), "banks": 0, "rows": 0, "revisable": 0,
             "short": 0, "missing": 0, "frozen": 0, "dups": 0, "changed": 0, "written": 0,
             "failed": 0, "repaired": 0, "skipped_banks": {}, "pairs": {}}
    pairs: Counter = Counter()
    marks: list[dict] = []
    try:
        with db.session() as s:
            banks = sorted(b for (b,) in s.execute(_BANKS, {"lo": lo}).all() if b)
        for bank in banks:
            try:
                res = _bank_pass(bank, since=since, lo=lo, window_days=window_days,
                                 threshold=threshold, cross_source_only=cross_source_only,
                                 chunk=chunk)
            except Exception as e:  # noqa: BLE001
                log.warning("склейка копий: банк %s пропущен (%s: %s)", bank, type(e).__name__, e)
                stats["skipped_banks"][bank] = f"{type(e).__name__}: {e}"[:200]
                continue
            stats["banks"] += 1
            for k in ("rows", "revisable", "short", "missing", "frozen", "dups"):
                stats[k] += res[k]
            if res["skipped"]:
                stats["skipped_banks"][bank] = res["skipped"]
                continue
            pairs.update(res["pairs"])
            marks.extend(res["marks"])
            stats["changed"] += len(res["changes"])
            if res["changes"] and not dry:
                done, failed = write(res["changes"], chunk)
                stats["written"] += done
                stats["failed"] += failed
        if not dry:
            stats["repaired"] = repair()
    finally:
        lock.release()
    if stats["skipped_banks"]:
        # соседняя база легла — пропущены десятки банков; одна строка в лог, а не сотня
        log.warning("склейка копий: пропущено банков %d (%s)", len(stats["skipped_banks"]),
                    "; ".join(f"{b}: {why}" for b, why in list(stats["skipped_banks"].items())[:3]))
    stats["pairs"] = dict(pairs.most_common())
    if examples:
        stats["examples"] = sorted(marks, key=lambda m: (m["jaccard"], m["url"]))[:examples]
    stats["seconds"] = round(time.time() - t0, 1)
    log.info("склейка копий (%s%s): строк %d, копий %d, изменено %d, записано %d, "
             "починено %d, %.1f с", stats["mode"], ", без записи" if dry else "", stats["rows"],
             stats["dups"], stats["changed"], stats["written"], stats["repaired"], stats["seconds"])
    return stats


def _state_get() -> dict[str, float]:
    with db.session() as s:
        rows = s.execute(text("SELECT k, v FROM review_index_state WHERE k IN (:a, :b)"),
                         {"a": _AT_KEY, "b": _FULL_KEY}).all()
    out = {}
    for k, v in rows:
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            pass
    return out


_STATE_SET = text("""
    INSERT INTO review_index_state (k, v, updated_at) VALUES (:k, :v, CURRENT_TIMESTAMP)
    ON CONFLICT (k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at""")


def _state_set(keys: list[str], at: float) -> None:
    """Когда был проход (секунды эпохи) — в review_index_state рядом с водяными
    знаками индекса: срок переживает перезапуск контейнера."""
    with db.session() as s:
        for k in keys:
            s.execute(_STATE_SET, {"k": k, "v": f"{at:.0f}"})


def tick(now: float | None = None) -> dict:
    """Для фонового цикла индекса отзывов: проход не чаще раза в REVIEW_DEDUP_EVERY_S,
    полный — раз в REVIEW_DEDUP_FULL_EVERY_S (и первым запуском — это и есть
    начальная разметка копий), остальные — инкремент. Полный проход сводит редкие
    расхождения инкремента на краю окна (опоздавшая копия banki.ru к уже решённой
    истории). Полный проход, пропустивший банки (соседняя база не отдала тексты) или
    не записавший пакет, повторяется — но тоже не чаще раза в EVERY_S."""
    at = time.time() if now is None else now
    st = _state_get()
    if at - st.get(_AT_KEY, 0.0) < EVERY_S:
        return {"skipped": "not_due"}
    full = bool(FULL_EVERY_S) and at - st.get(_FULL_KEY, 0.0) >= FULL_EVERY_S
    res = run(None if full else SINCE_DAYS)
    if res.get("skipped"):
        return res                                  # замок занят — повторит следующий тик
    done = not res.get("failed") and not res.get("skipped_banks")
    _state_set([_AT_KEY, _FULL_KEY] if full and done else [_AT_KEY], at)
    return res


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Склейка копий одного отзыва между площадками")
    p.add_argument("--full", action="store_true", help=f"полный проход с {SINCE}")
    p.add_argument("--since-days", type=int, default=SINCE_DAYS)
    p.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    p.add_argument("--threshold", type=float, default=THRESHOLD)
    p.add_argument("--cross-source-only", action="store_true")
    p.add_argument("--dry", action="store_true", help="посчитать без записи")
    p.add_argument("--examples", type=int, default=20)
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = run(None if a.full else a.since_days, window_days=a.window_days, threshold=a.threshold,
              cross_source_only=a.cross_source_only, dry=a.dry, examples=a.examples)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
