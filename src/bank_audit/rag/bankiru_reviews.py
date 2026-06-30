"""Доступ к корпусу жалоб banki.ru — соседняя БД `bankiru` на том же Postgres.

Что это: ~390 тыс. отзывов banki.ru за 2025–2026, ТОЛЬКО негатив (1–2★ —
краулер коллеги тянет rate[]=1&rate[]=2), 217 банков, с готовыми эмбеддингами
bge-m3 (1024d) в bankiru.review_embeddings (HNSW по cosine уже построен).
Наполняется ежедневным кроном — данные свежие. Инструмент к ней не писал,
только читает (прямой read-only коннект ко второй БД того же инстанса).

Зачем: своя auditlens.review — мизер (≈800 строк, 22 банка), из-за чего
ИИ-аналитик часто пишет «жалоб нет». Здесь — реальные жалобы по всем крупным
банкам с цитатами и датами.

⚠️ Эмбеддинги асимметричные (bge-m3 query/passage prefix). Векторы-документы
посчитаны с passage-префиксом; ЗАПРОС обязан эмбедиться с QUERY-префиксом
"Represent this sentence for searching relevant passages: " — иначе косинус
деградирует. L2-норма роли не играет (cosine инвариантен к масштабу), поэтому
переиспользуем штатный embedder (он нормирует — не страшно).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from . import embedder

log = logging.getLogger(__name__)

# Префикс запроса bge-m3 (ровно как в репозитории-источнике bankiru-reviews).
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Включение/выключение фичи без передеплоя.
ENABLED = os.getenv("BANKIRU_REVIEWS_ENABLED", "1").lower() not in ("0", "false", "no")

_engine = None
_engine_lock = threading.Lock()
_names_cache: list[str] | None = None
_norm2name: dict[str, str] | None = None


def _bankiru_dsn() -> str | None:
    """DSN ко второй БД `bankiru`: берём основной DATABASE_URL и подменяем имя БД.
    Можно переопределить через BANKIRU_DATABASE_URL."""
    override = os.getenv("BANKIRU_DATABASE_URL")
    if override:
        return override
    base = os.getenv("DATABASE_URL")
    if not base:
        return None
    try:
        return make_url(base).set(database=os.getenv("BANKIRU_DB_NAME", "bankiru")).render_as_string(hide_password=False)
    except Exception as e:
        log.warning("bankiru: не удалось вывести DSN из DATABASE_URL: %s", e)
        return None


def _get_engine():
    global _engine
    if _engine is not None:
        return _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        dsn = _bankiru_dsn()
        if not dsn:
            return None
        _engine = create_engine(
            dsn, pool_pre_ping=True, pool_size=2, max_overflow=2, future=True,
            # read-only намерение: ничего не пишем, но подстрахуемся
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        log.info("bankiru: engine инициализирован (read-only)")
        return _engine


# ── Резолвинг имени банка → каноническое имя в bankiru ──────────────────────
_ALIAS = {
    "сбер": "Сбербанк", "сбербанк": "Сбербанк", "sber": "Сбербанк",
    "тинькофф": "Т-Банк", "тинькофф банк": "Т-Банк", "тбанк": "Т-Банк",
    "т банк": "Т-Банк", "tinkoff": "Т-Банк",
    "втб": "ВТБ", "втб24": "ВТБ", "втб 24": "ВТБ", "vtb": "ВТБ",
    "альфа": "Альфа-Банк", "альфабанк": "Альфа-Банк", "alfa": "Альфа-Банк",
    "газпром": "Газпромбанк", "гпб": "Газпромбанк",
    "озон": "Ozon Банк", "ozon": "Ozon Банк", "озон банк": "Ozon Банк",
    "отп": "ОТП Банк", "райф": "Райффайзен Банк", "райффайзенбанк": "Райффайзен Банк",
    "мкб": "Московский кредитный банк (МКБ)",
    "московский кредитный банк": "Московский кредитный банк (МКБ)",
    "открытие": "Банк «Открытие»", "совком": "Совкомбанк",
    "почтабанк": "Почта Банк", "рсхб": "Россельхозбанк", "россельхоз": "Россельхозбанк",
    "акбарс": "Ак Барс Банк", "промсвязьбанк": "ПСБ", "псб": "ПСБ",
    "яндекс": "Яндекс Банк", "мтс": "МТС Банк", "убрир": "Уральский банк реконструкции и развития (УБРиР)",
    "атб": "Азиатско-Тихоокеанский банк (АТБ)",
}


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[«»\"'`’“”()\[\]]", " ", s)
    s = re.sub(r"[-–—/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_names() -> dict[str, str]:
    """Список 217 имён банков из bankiru (кэш на процесс) + нормализованный индекс."""
    global _names_cache, _norm2name
    if _norm2name is not None:
        return _norm2name
    eng = _get_engine()
    if eng is None:
        _norm2name = {}
        return _norm2name
    try:
        with eng.connect() as c:
            rows = c.execute(text('SELECT DISTINCT "bankName" FROM bankiru.reviews')).all()
        _names_cache = [r[0] for r in rows if r[0]]
        _norm2name = {_norm(n): n for n in _names_cache}
        log.info("bankiru: загружено %d имён банков", len(_names_cache))
    except Exception as e:
        log.warning("bankiru: не удалось загрузить список банков: %s", e)
        _norm2name = {}
    return _norm2name


def resolve_bank(name: str | None) -> str | None:
    """Имя/слаг банка → каноническое имя в bankiru (или None)."""
    if not name:
        return None
    n = _norm(name)
    if n in _ALIAS:
        return _ALIAS[n]
    idx = _load_names()
    if n in idx:
        return idx[n]
    for cand in (n + " банк", "банк " + n):
        if cand in idx:
            return idx[cand]
    try:
        from rapidfuzz import process, fuzz
        m = process.extractOne(n, list(idx.keys()), scorer=fuzz.WRatio)
        if m and m[1] >= 88:
            return idx[m[0]]
    except Exception:
        pass
    return None


# ── Семантический поиск жалоб ────────────────────────────────────────────────
def _dedup_key(body: str) -> str:
    return _norm(body)[:120]


def search_reviews(query: str | None = None, *, bank: str | None = None,
                   product: str | None = None, since_days: int | None = None,
                   k: int = 8) -> list[dict]:
    """Жалобы клиентов из корпуса banki.ru. Два режима:

    • DISCOVERY (query пустой) — свежие жалобы по банку (+продукту), отсортированы
      по дате. ОСНОВНОЙ режим для аудита: когда конкретная проблема заранее НЕ
      известна (напр. отчёт по эквайрингу), агент видит, на что РЕАЛЬНО жалуются,
      и сам кластеризует темы. bank обязателен.
    • SEMANTIC (query задан) — точечный поиск по теме/проблеме (cosine bge-m3).

    bank — имя/слаг банка (резолвится в каноническое имя bankiru).
    product — метка продукта banki.ru (опц.): «Вклад», «Кредитная карта»,
              «Обслуживание юридических лиц» (сюда же эквайринг/РКО), «Ипотека»,
              «Мобильное приложение», «Денежный перевод», …
    since_days — только за последние N дней (опц.; корпус и так с 2025).
    Возвращает list[{bank, product, date, url, text, distance}]; [] если нет данных.
    """
    if not ENABLED:
        return []
    eng = _get_engine()
    if eng is None:
        return []
    bank_canon = resolve_bank(bank) if bank else None
    if bank and bank_canon is None:
        # банка нет в bankiru (мелкий/неизвестный) — пусть вызывающий уйдёт в web
        log.info("bankiru: банк %r не найден в корпусе", bank)
        return []
    discovery = not (query and query.strip())
    if discovery and not bank_canon:
        # без банка discovery бессмысленно (вернули бы случайные свежие по всем)
        return []
    try:
        # дату-отсечку считаем в Python (datePublished — naive timestamp); так
        # избегаем NULL-параметра в make_interval (неоднозначность типа → ошибка).
        since_ts = (datetime.now() - timedelta(days=since_days)) if since_days else None
        # тянем с запасом под дедуп (один отзыв дублируется по продуктам)
        limit = max(k * 6, 30)
        params = {"bank": bank_canon, "product": product,
                  "since_ts": since_ts, "limit": limit}
        if not discovery:
            qvec = embedder.embed_one(QUERY_PREFIX + query.strip())
            params["qvec"] = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        if discovery:
            # Без темы: свежие жалобы по банку/продукту (агент сам кластеризует).
            # Эмбеддинг не нужен → быстро.
            sql = text(
                """
                SELECT r."bankName" AS bank, r."product" AS product,
                       r."datePublished" AS dt, r.url AS url, r."reviewBody" AS body,
                       0.0 AS dist
                FROM bankiru.reviews r
                WHERE r."bankName" = :bank
                  AND (CAST(:product AS text) IS NULL OR r."product" = :product)
                  AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                  AND length(r."reviewBody") >= 40
                ORDER BY r."datePublished" DESC
                LIMIT :limit
                """
            )
        elif bank_canon:
            # Фильтр по конкретному банку → подмножество ≤50k строк. HNSW при
            # селективном фильтре часто возвращает 0 (исследует только ef_search
            # глобальных соседей). Поэтому ТОЧНЫЙ скан по подмножеству через
            # MATERIALIZED-CTE (индекс на CTE не применяется) — надёжно и быстро.
            sql = text(
                """
                WITH cand AS MATERIALIZED (
                  SELECT r."bankName" AS bank, r."product" AS product,
                         r."datePublished" AS dt, r.url AS url,
                         r."reviewBody" AS body, e.embedding AS emb
                  FROM bankiru.reviews r
                  JOIN bankiru.review_embeddings e ON e.review_id = r.id
                  WHERE r."bankName" = :bank
                    AND (CAST(:product AS text) IS NULL OR r."product" = :product)
                    AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                )
                SELECT bank, product, dt, url, body,
                       (emb <=> CAST(:qvec AS vector)) AS dist
                FROM cand ORDER BY emb <=> CAST(:qvec AS vector) LIMIT :limit
                """
            )
        else:
            # Без фильтра по банку — глобальный поиск по всему корпусу через HNSW.
            sql = text(
                """
                SELECT r."bankName" AS bank, r."product" AS product,
                       r."datePublished" AS dt, r.url AS url, r."reviewBody" AS body,
                       (e.embedding <=> CAST(:qvec AS vector)) AS dist
                FROM bankiru.review_embeddings e
                JOIN bankiru.reviews r ON r.id = e.review_id
                WHERE (CAST(:product AS text) IS NULL OR r."product" = :product)
                  AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :limit
                """
            )
        with eng.connect() as c:
            rows = c.execute(sql, params).mappings().all()
    except Exception as e:
        log.warning("bankiru: поиск упал (%s) — отдаю пусто, вызывающий уйдёт в web", type(e).__name__)
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        body = (r["body"] or "").strip()
        key = _dedup_key(body)
        if len(body) < 40 or key in seen:   # отсекаем тест-мусор и дубли по продуктам
            continue
        seen.add(key)
        dt = r["dt"]
        out.append({
            "bank": r["bank"],
            "product": r["product"],
            "date": dt.date().isoformat() if dt else None,
            "url": r["url"],
            "text": body,
            "distance": round(float(r["dist"]), 4),
        })
        if len(out) >= k:
            break
    return out


def is_available() -> bool:
    """Доступна ли БД bankiru (для health/диагностики)."""
    if not ENABLED:
        return False
    eng = _get_engine()
    if eng is None:
        return False
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1 FROM bankiru.reviews LIMIT 1"))
        return True
    except Exception:
        return False
