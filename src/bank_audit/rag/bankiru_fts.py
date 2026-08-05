"""Полнотекстовое зеркало корпуса отзывов banki.ru — словесная нога поиска.

Векторный поиск слеп на редких токенах: «нарушения ПДС» возвращал 0 релевантных
из 10, хотя у одного Сбера 127 таких отзывов. Эмбеддинг не кодирует аббревиатуру.
Лечится вторым поиском — по словам, ровно как в базе знаний (`retriever`).

Почему зеркало, а не индекс в источнике: корпус лежит в СОСЕДНЕЙ базе `bankiru`,
её ведёт чужой процесс, прав на CREATE там нет. `to_tsvector` на лету измерен —
8 секунд по одному банку. Поэтому tsvector считается один раз и живёт у нас
(см. migrations/028_review_index.sql).

Тексты отзывов здесь НЕ хранятся: зеркало отдаёт review_id, тела добираются из
`bankiru` по первичному ключу — и только для того десятка строк, что реально
показывается. Это экономит ~450 МБ и держит источник единственным владельцем
текста.
"""
from __future__ import annotations

import logging
import os
import time

from sqlalchemy import text

from .. import db
from .bankiru_reviews import _get_engine as _source_engine

log = logging.getLogger(__name__)

_WATERMARK = "last_review_id"
_BATCH = int(os.getenv("BANKIRU_FTS_BATCH", "4000"))
# Перекрытие при инкременте. Водяной знак по id надёжен, пока источник пишет
# строго по возрастанию, но при параллельных вставках строка с меньшим id может
# закоммититься позже прочитанного максимума. Перекрытие закрывает это окно
# ценой повторного пересчёта нескольких тысяч tsvector (около секунды).
_OVERLAP = int(os.getenv("BANKIRU_FTS_OVERLAP", "5000"))
# Ниже этой длины отзывы не индексируются: тот же порог, что и в поиске, там он
# отсекает тестовый мусор краулера.
_MIN_LEN = 40

_UPSERT = text("""
    INSERT INTO review_index (url, review_id, bank, product, dt, city, tsv, esc)
    VALUES (:url, :review_id, :bank, :product, :dt, :city,
            to_tsvector(CAST('russian' AS regconfig), :body), :esc)
    ON CONFLICT (url) DO UPDATE SET
        review_id = EXCLUDED.review_id,
        bank      = EXCLUDED.bank,
        product   = EXCLUDED.product,
        dt        = EXCLUDED.dt,
        city      = EXCLUDED.city,
        tsv       = EXCLUDED.tsv,
        esc       = EXCLUDED.esc
    WHERE review_index.dt IS NULL
       OR (EXCLUDED.dt IS NOT NULL AND EXCLUDED.dt >= review_index.dt)
""")


_ESC_RE = None


def _is_escalation(body: str) -> bool:
    """Грозит ли клиент уйти в ЦБ, суд, ФАС или прокуратуру.

    Считается ЗДЕСЬ, при индексации, потому что здесь ещё есть текст. При чтении
    его уже нет — индекс хранит tsvector, а выразить эти паттерны через tsquery
    нельзя: ограничителем в них служат предлоги («жалобу В»), а русский словарь
    выбрасывает их из индекса как стоп-слова. Попытка обойтись без них завысила
    метрику вдвое. Регэксп берём тот же, что использовался раньше, чтобы числа
    остались сопоставимы с историей.
    """
    global _ESC_RE
    if _ESC_RE is None:
        import re as _re
        from .reviews_dash import THEMES, _theme_rx
        th = next((t for t in THEMES if t["key"] == "escalation"), None)
        _ESC_RE = _re.compile(_theme_rx(th, r"\b"), _re.I) if th else _re.compile(r"(?!)")
    return bool(_ESC_RE.search(body or ""))


def _city(location: str | None) -> str | None:
    """location в источнике вида «Москва (Московская область)» — витрина везде
    берёт часть до скобки, зеркало обязано резать так же."""
    head = (location or "").split(" (")[0].strip()
    return head or None


def _read_watermark(s) -> int:
    row = s.execute(text("SELECT v FROM review_index_state WHERE k = :k"),
                    {"k": _WATERMARK}).first()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0


def sync(max_batches: int | None = None) -> dict:
    """Догоняет зеркало до текущего состояния источника. Идемпотентна:
    повторный прогон ничего не портит и почти ничего не делает.

    max_batches ограничивает объём работы за вызов (для старта приложения, где
    полный бэкфилл на 400 тыс. строк держать нельзя). None — до конца.
    """
    src = _source_engine()
    if src is None:
        log.warning("bankiru_fts: источник недоступен, синхронизация пропущена")
        return {"ok": False, "reason": "source_unavailable"}

    t0 = time.time()
    with db.session() as s:
        watermark = _read_watermark(s)
    since_id = max(0, watermark - _OVERLAP)
    read = written = 0
    max_id = watermark
    batches = 0

    while max_batches is None or batches < max_batches:
        with src.connect() as c:
            rows = c.execute(text("""
                SELECT id, url, "bankName", "product", "datePublished",
                       location, "reviewBody"
                FROM bankiru.reviews
                WHERE id > :since AND length("reviewBody") >= :minlen
                ORDER BY id
                LIMIT :lim
            """), {"since": since_id, "minlen": _MIN_LEN, "lim": _BATCH}).all()
        if not rows:
            break
        payload = [{"url": r[1], "review_id": r[0], "bank": r[2], "product": r[3],
                    "dt": r[4], "city": _city(r[5]), "body": r[6],
                    "esc": _is_escalation(r[6])} for r in rows]
        # сессия на батч, а не на весь прогон: иначе бэкфилл держит одну
        # транзакцию на сотни тысяч строк и блокирует вакуум
        with db.session() as s:
            s.execute(_UPSERT, payload)
        read += len(rows)
        written += len(payload)
        since_id = max_id = max(max_id, int(rows[-1][0]))
        batches += 1
        with db.session() as s:
            s.execute(text("""
                INSERT INTO review_index_state (k, v, updated_at)
                VALUES (:k, :v, now())
                ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()
            """), {"k": _WATERMARK, "v": str(max_id)})

    dt = time.time() - t0
    done = max_batches is None or batches < max_batches
    log.info("bankiru_fts: синхронизация %s — прочитано %d, записано %d, id до %d, %.1f с",
             "завершена" if done else "прервана по лимиту батчей", read, written, max_id, dt)
    return {"ok": True, "read": read, "written": written, "watermark": max_id,
            "complete": done, "seconds": round(dt, 1)}


def _collector_sources() -> list[str]:
    """Ключи источников отзывов из config/sources.yaml.

    Фильтр обязателен: в таблице review, кроме коллекторов, лежат единичные
    строки от подсистемы автогенерируемых парсеров «Лазеек» (harant.ru,
    brobank_reviews и подобные, по одной записи). Они не отзывы клиентов и во
    вкладке им делать нечего. Список берём из конфига, а не пишем в коде — чтобы
    новый коллектор подхватывался сам, как и в планировщике.
    """
    try:
        from ..config import Settings
        cfg = Settings.load().sources_cfg
    except Exception:
        try:
            import yaml
            from pathlib import Path
            root = Path(__file__).resolve().parents[3]
            cfg = yaml.safe_load((root / "config" / "sources.yaml").read_text("utf-8"))
        except Exception as e:
            log.warning("review_index: конфиг источников не прочитан (%s)", e)
            return []
    return [k for k, v in (cfg or {}).items()
            if "review" in str((v or {}).get("adapter", "")).lower()]


def sync_local(batch: int = 400) -> dict:
    """Отзывы, собранные НАШИМИ коллекторами, в тот же индекс и со своими векторами.

    Индекс с самого начала задумывался как единая точка входа для вкладки, и
    теперь в нём живут два вида строк: из внешней базы banki.ru и из локальной
    таблицы review. Аудитору источник виден колонкой, а не отдельным блоком —
    лента, поиск и темы у него одни.

    Имя банка приводим к каноническому виду внешнего корпуса: иначе фильтр по
    банку на вкладке разделил бы «Сбербанк» и «ПАО Сбербанк» на две сущности, и
    половина данных стала бы невидимой.

    Векторы считаем СВОИ, в отдельную таблицу. Смешивать их с векторами внешней
    базы в одном ANN-запросе нельзя: сверка дала косинус 0.887 между вектором
    оттуда и вектором того же текста, посчитанным нашим эмбеддером — модель одна,
    рецепты разные. Поиск ходит по каждому хранилищу своей ногой и сливает
    выдачи по рангам (RRF), которым разница масштабов безразлична.
    """
    from .bankiru_reviews import resolve_bank

    known = _collector_sources()
    if not known:
        log.warning("review_index: не удалось прочитать список коллекторов — пропуск")
        return {"ok": False, "reason": "no_sources"}

    t0 = time.time()
    with db.session() as s:
        names = [r[0] for r in s.execute(text("""
            SELECT DISTINCT b.name FROM review r JOIN bank b USING (bank_id)
            WHERE r.source = ANY(:src) AND b.name IS NOT NULL
        """), {"src": known}).all()]
    canon = {n: (resolve_bank(n) or n) for n in names}
    unresolved = [n for n, c in canon.items() if c == n and resolve_bank(n) is None]
    if unresolved:
        log.info("review_index: банки вне внешнего корпуса, оставлены как есть: %s",
                 ", ".join(unresolved[:6]))

    with db.session() as s:
        rows = s.execute(text("""
            SELECT r.review_id, r.source_url, r.source, r.rating, r.posted_at,
                   b.name AS bank, r.product_category::text AS product,
                   coalesce(r.title, '') || ' ' || r.text AS body
            FROM review r JOIN bank b USING (bank_id)
            WHERE r.source = ANY(:src)
              AND r.source_url IS NOT NULL AND length(r.text) >= :minlen
        """), {"minlen": _MIN_LEN, "src": known}).mappings().all()

    written = 0
    for i in range(0, len(rows), batch):
        payload = [{"url": r["source_url"], "review_id": int(r["review_id"]),
                    "bank": canon.get(r["bank"], r["bank"]), "product": r["product"],
                    "dt": r["posted_at"], "city": None,
                    "rating": float(r["rating"]) if r["rating"] is not None else None,
                    "source": r["source"], "body": r["body"],
                    "esc": _is_escalation(r["body"])}
                   for r in rows[i:i + batch]]
        with db.session() as s:
            s.execute(text("""
                INSERT INTO review_index (url, review_id, bank, product, dt, city,
                                          rating, source, tsv, esc)
                VALUES (:url, :review_id, :bank, :product, :dt, :city, :rating, :source,
                        to_tsvector(CAST('russian' AS regconfig), :body), :esc)
                ON CONFLICT (url) DO UPDATE SET
                    review_id = EXCLUDED.review_id, bank = EXCLUDED.bank,
                    dt = EXCLUDED.dt, rating = EXCLUDED.rating,
                    source = EXCLUDED.source, tsv = EXCLUDED.tsv,
                    esc = EXCLUDED.esc
            """), payload)
        written += len(payload)

    embedded = embed_missing()
    dt = time.time() - t0
    log.info("review_index: локальных отзывов %d, векторов посчитано %d, %.1f с",
             written, embedded, dt)
    return {"ok": True, "rows": written, "embedded": embedded,
            "banks": len(canon), "seconds": round(dt, 1)}


def embed_missing(limit: int = 2000, batch: int = 64) -> int:
    """Считает векторы для строк индекса, у которых их ещё нет.

    Только для НЕ внешних источников: у строк из banki.ru векторы уже лежат в
    самой внешней базе, пересчитывать их незачем и негде хранить дешевле.
    """
    from . import embedder
    total = 0
    while total < limit:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT i.url, coalesce(r.title, '') || ' ' || r.text AS body
                FROM review_index i JOIN review r ON r.review_id = i.review_id
                                                 AND r.source = i.source
                WHERE i.source <> 'bankiru'
                  AND i.source = ANY(:src)
                  AND NOT EXISTS (SELECT 1 FROM review_embedding e WHERE e.url = i.url)
                LIMIT :n
            """), {"n": batch, "src": _collector_sources()}).mappings().all()
        if not rows:
            break
        try:
            vecs = embedder.embed_batch([r["body"][:4000] for r in rows])
        except Exception as e:
            log.warning("review_index: эмбеддинги не посчитались (%s: %s)",
                        type(e).__name__, e)
            break
        payload = [{"u": r["url"],
                    "v": "[" + ",".join(f"{x:.6f}" for x in v) + "]",
                    "m": getattr(embedder, "EMBEDDING_API_MODEL", "bge-m3")}
                   for r, v in zip(rows, vecs)]
        with db.session() as s:
            s.execute(text("""
                INSERT INTO review_embedding (url, embedding, model)
                VALUES (:u, CAST(:v AS vector), :m)
                ON CONFLICT (url) DO UPDATE
                    SET embedding = EXCLUDED.embedding, model = EXCLUDED.model
            """), payload)
        total += len(payload)
    return total


def status() -> dict:
    """Насколько зеркало отстало от источника — для диагностики и «Пульса»."""
    out: dict = {"rows": 0, "watermark": 0, "source_rows": None, "lag": None}
    try:
        with db.session() as s:
            out["rows"] = int(s.execute(text("SELECT count(*) FROM review_index")).scalar() or 0)
            out["watermark"] = _read_watermark(s)
            out["max_dt"] = s.execute(text("SELECT max(dt) FROM review_index")).scalar()
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    src = _source_engine()
    if src is not None:
        try:
            with src.connect() as c:
                n, mx = c.execute(text(
                    'SELECT count(*), max(id) FROM bankiru.reviews'
                    ' WHERE length("reviewBody") >= :m'), {"m": _MIN_LEN}).one()
            out["source_rows"] = int(n)
            out["lag"] = int(mx) - out["watermark"]
        except Exception as e:
            out["source_error"] = f"{type(e).__name__}: {e}"
    return out


def is_ready() -> bool:
    """Есть ли чем искать. Пока зеркало не наполнено, гибрид должен молча
    работать одной векторной ногой, а не падать."""
    try:
        with db.session() as s:
            return bool(s.execute(text(
                "SELECT 1 FROM review_index LIMIT 1")).first())
    except Exception:
        return False


HL_START, HL_STOP = "⟦", "⟧"      # те же маркеры, что и в базе знаний


def headline(texts: list[str], tsquery: str) -> list[str]:
    """Размечает в текстах слова, по которым отзыв реально нашёлся.

    Считает это Postgres тем же русским словарём, что и сам поиск, поэтому
    подсвечено ровно совпавшее — а не то, что похоже на совпавшее. Для аудитора
    это доказательство: видно, почему отзыв в выдаче. Приблизительная подсветка
    (сопоставление основ на стороне приложения) такого свойства не даёт и
    подсвечивала бы «счётчик» на основу «счет».

    Маркеры ⟦⟧, а не HTML: текст отзыва не должен попадать в разметку страницы.
    """
    if not texts or not (tsquery or "").strip():
        return list(texts)
    # входной текст мог бы содержать сами маркеры — тогда фронт разберёт разметку
    # неверно; чистим до, а не после
    clean = [(t or "").replace(HL_START, "").replace(HL_STOP, "") for t in texts]
    opts = f"StartSel={HL_START}, StopSel={HL_STOP}, HighlightAll=TRUE"
    try:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT ts_headline(CAST('russian' AS regconfig), t.txt,
                                   websearch_to_tsquery(CAST('russian' AS regconfig), :q),
                                   :opts) AS hl
                FROM unnest(CAST(:texts AS text[])) WITH ORDINALITY AS t(txt, i)
                ORDER BY t.i
            """), {"texts": clean, "q": tsquery, "opts": opts}).scalars().all()
        return [r if r else clean[i] for i, r in enumerate(rows)] if rows else clean
    except Exception as e:
        log.warning("bankiru_fts: подсветка не построилась (%s: %s)", type(e).__name__, e)
        return clean


def bodies_for(rows: list[dict]) -> dict[str, dict]:
    """Тексты по строкам индекса — каждый из своего хранилища.

    Индекс намеренно не хранит тела: у внешнего корпуса владелец текста — база
    banki.ru, у наших коллекторов — таблица review. Копия завела бы вторую
    версию правды, которая начнёт расходиться с первой.
    """
    out: dict[str, dict] = {}
    ext = [r for r in rows if (r.get("source") or "bankiru") == "bankiru"]
    loc = [r for r in rows if (r.get("source") or "bankiru") != "bankiru"]
    if ext:
        eng = _source_engine()
        if eng is not None:
            try:
                with eng.connect() as c:
                    for x in c.execute(text(
                        'SELECT r.url, r."reviewBody" AS body, r."product" AS product,'
                        ' r.location AS location FROM bankiru.reviews r'
                        ' WHERE r.id = ANY(:ids)'),
                            {"ids": [r["review_id"] for r in ext]}).mappings():
                        out[x["url"]] = {"text": (x["body"] or "").strip(),
                                         "product": x["product"],
                                         "city": _city(x["location"])}
            except Exception as e:
                log.warning("review_index: тексты внешнего корпуса не забрались (%s)", e)
    if loc:
        try:
            with db.session() as s:
                for x in s.execute(text("""
                    SELECT r.source_url AS url, r.text, r.product_category::text AS product
                    FROM review r WHERE r.review_id = ANY(:ids)
                """), {"ids": [r["review_id"] for r in loc]}).mappings():
                    out[x["url"]] = {"text": (x["text"] or "").strip(),
                                     "product": x["product"], "city": None}
        except Exception as e:
            log.warning("review_index: тексты своих коллекторов не забрались (%s)", e)
    return out


def search_vectors_local(qvec: list[float], *, bank: str | None = None,
                         product: str | None = None, city: str | None = None,
                         month: str | None = None, since_ts=None,
                         k: int = 60) -> list[dict]:
    """Смысловая нога по НАШИМ векторам — для отзывов своих коллекторов.

    Отдельно от векторной ноги по внешней базе, и это не дублирование: рецепты
    эмбеддинга разные (сверка дала косинус 0.887 на одном и том же тексте), и
    расстояния из двух хранилищ несопоставимы. Сливать выдачи можно только по
    рангам — что RRF и делает.

    Скан точный, без ANN: своих векторов тысячи, а не сотни тысяч, и HNSW под
    фильтром по банку здесь только навредил бы (замеряли на внешнем корпусе —
    при селективном фильтре он возвращал 2 строки из 20 запрошенных).
    """
    if not qvec:
        return []
    clauses, p = [], {"qv": "[" + ",".join(f"{x:.6f}" for x in qvec) + "]", "k": int(k)}
    if bank:
        clauses.append("i.bank = :bank"); p["bank"] = bank
    if product:
        clauses.append("i.product = :product"); p["product"] = product
    if city:
        clauses.append("i.city = :city"); p["city"] = city
    if month:
        clauses.append("date_trunc('month', i.dt) = to_date(:month, 'YYYY-MM')")
        p["month"] = month
    if since_ts is not None:
        clauses.append("i.dt >= :since_ts"); p["since_ts"] = since_ts
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    try:
        with db.session() as s:
            rows = s.execute(text(f"""
                SELECT i.url, i.review_id, i.source, i.bank, i.product, i.dt,
                       i.city, i.rating, (e.embedding <=> CAST(:qv AS vector)) AS dist
                FROM review_embedding e JOIN review_index i ON i.url = e.url
                WHERE true{where}
                ORDER BY e.embedding <=> CAST(:qv AS vector)
                LIMIT :k
            """), p).mappings().all()
    except Exception as e:
        log.warning("review_index: смысловая нога по своим векторам упала (%s: %s)",
                    type(e).__name__, e)
        return []
    return [{"url": r["url"], "review_id": int(r["review_id"]), "source": r["source"],
             "bank": r["bank"], "product": r["product"],
             "date": r["dt"].date().isoformat() if r["dt"] else None,
             "city": r["city"],
             "rating": float(r["rating"]) if r["rating"] is not None else None,
             "distance": round(float(r["dist"]), 4)} for r in rows]


def search_fts(query: str, *, bank: str | None = None, product: str | None = None,
               city: str | None = None, month: str | None = None,
               since_ts=None, k: int = 60) -> list[dict]:
    """Словесная нога: ранжированные попадания по словам запроса.

    query уходит в `websearch_to_tsquery` — он принимает произвольный
    пользовательский текст, не падает на кавычках и спецсимволах и понимает
    OR/кавычки. Расширение запроса (аббревиатуры, синонимы) делается ВЫШЕ и
    приезжает сюда уже строкой вида «пдс OR "программа долгосрочных сбережений"».

    Возвращает [{url, review_id, bank, product, date, city, rank, terms}], где
    terms — лексемы запроса, реально найденные в отзыве. Это и есть честный
    сигнал совпадения: косинусная дистанция, как показали замеры, релевантное от
    мусора не отделяет, а найденные слова проверяемы глазами.
    """
    if not (query or "").strip():
        return []
    clauses, params = [], {"q": query, "k": int(k)}
    if bank:
        clauses.append("f.bank = :bank")
        params["bank"] = bank
    if product:
        clauses.append("f.product = :product")
        params["product"] = product
    if city:
        clauses.append("f.city = :city")
        params["city"] = city
    if month:
        clauses.append("date_trunc('month', f.dt) = to_date(:month, 'YYYY-MM')")
        params["month"] = month
    if since_ts is not None:
        clauses.append("f.dt >= :since_ts")
        params["since_ts"] = since_ts
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    # terms считаются ТОЛЬКО для отобранной верхушки: если положить их в SELECT
    # основного запроса, Postgres развернёт tsvector каждого попадания до
    # сортировки, а попаданий по частому слову бывают тысячи
    sql = text(f"""
        WITH q AS (
            SELECT websearch_to_tsquery(CAST('russian' AS regconfig), :q) AS tq,
                   tsvector_to_array(to_tsvector(CAST('russian' AS regconfig), :q)) AS qterms
        ), hit AS (
            SELECT f.url, f.review_id, f.source, f.bank, f.product, f.dt, f.city,
                   f.tsv, ts_rank_cd(f.tsv, q.tq) AS rank
            FROM review_index f, q
            WHERE f.tsv @@ q.tq{where}
            ORDER BY rank DESC, f.dt DESC
            LIMIT :k
        )
        SELECT h.url, h.review_id, h.source, h.bank, h.product, h.dt, h.city, h.rank,
               ARRAY(SELECT unnest(tsvector_to_array(h.tsv))
                     INTERSECT
                     SELECT unnest(q.qterms)) AS terms
        FROM hit h, q
        ORDER BY h.rank DESC, h.dt DESC
    """)
    try:
        with db.session() as s:
            rows = s.execute(sql, params).mappings().all()
    except Exception as e:
        log.warning("bankiru_fts: словесный поиск упал (%s: %s)", type(e).__name__, e)
        return []
    return [{"url": r["url"], "review_id": int(r["review_id"]), "source": r["source"],
             "bank": r["bank"], "product": r["product"],
             "date": r["dt"].date().isoformat() if r["dt"] else None,
             "city": r["city"], "rank": float(r["rank"]),
             "terms": list(r["terms"] or [])} for r in rows]
