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
import re
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

# Ранний HTML-сборщик banki.ru (18.06–25.09) забирал не отзыв, а карточку из ленты:
# «Проблема решена», обрезанный текст «…», дату, счётчик и содержимое <style> —
# в карточке жалобы вместо текста стояли куски CSS (обращение 09.10).
_CSS_RULES = re.compile(r"\s*\.[a-z][a-z0-9]{6,10}\{[a-z-]+:.*\Z", re.S)
_CARD_TAIL = re.compile(r"\s*\n\d{2}\.\d{2}\.\d{2,4}\s+\d{1,2}:\d{2}\s*(?:\n\s*\d{1,4}\s*)?\Z")
_CARD_HEAD = re.compile(r"\A\s*Проблема (?:не )?решена\s*\n")
_BANKI_RESP = "https://www.banki.ru/services/responses/bank/response/"


def clean_body(t: str | None) -> str:
    """Текст отзыва без обвязки карточки ленты: CSS, строка даты со счётчиком,
    метка «Проблема решена». Обычный текст не меняется."""
    t = (t or "").strip()
    if not _CSS_RULES.search(t):
        return t
    t = _CSS_RULES.sub("", t)
    t = _CARD_TAIL.sub("", t)
    return _CARD_HEAD.sub("", t).strip()


def banki_rid(url: str | None) -> str | None:
    m = re.search(r"/response/(\d+)", url or "")
    return m.group(1) if m else None

_UPSERT = text("""
    INSERT INTO review_index (url, review_id, bank, product, dt, city, tsv, esc)
    VALUES (:url, :review_id, :bank, :product, :dt, :city,
            to_tsvector(CAST('russian' AS regconfig), :body), :esc)
    ON CONFLICT (url) DO UPDATE SET
        review_id = EXCLUDED.review_id,
        bank      = EXCLUDED.bank,
        dt        = EXCLUDED.dt,
        city      = coalesce(EXCLUDED.city, review_index.city),
        tsv       = EXCLUDED.tsv
    -- product и esc не переписываем: их ставит LLM-разметка
    -- (review_annotate.apply_to_index), а метка площадки и регулярка неверны
    WHERE review_index.dt IS NULL
       OR (EXCLUDED.dt IS NOT NULL AND EXCLUDED.dt >= review_index.dt)
""")


_ESC_RE = None



# Площадка переименовывает банки, и история одного банка распадается на два
# названия: «Точка» до февраля 2026 и «Точка Банк» после, «Долинск» и «Долинск
# Банк». Старое имя ведём в новое при индексации (исторические строки —
# миграция 072). «Почта Банк» сюда не входит: с мая 2026 его отзывы на площадке
# идут под ВТБ, это слияние, а не переименование, и склеивать его нельзя.
BANK_RENAMES = {"Точка": "Точка Банк", "SBI Bank": "SBI Банк",
                "А7-финансы ПСБ": "А7 Финансы - ПСБ", "Долинск": "Долинск Банк",
                # последний отзыв под старым именем — 19.08.2026 (аудит 03.10, ОТЗ-14)
                "Банк «Санкт-Петербург»": "БСПБ"}


def canon_bank(name: str | None) -> str | None:
    return BANK_RENAMES.get(name, name) if name else name

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
    # «Москва и область» площадка ведёт отдельным городом, и столица в
    # географии раскалывалась надвое (1 691 жалоба жила отдельно от Москвы)
    m = re.match(r"^(.+?) и (?:область|[А-ЯЁ][а-яё]+ская область)$", head)
    if m:
        head = m.group(1).strip()
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
        # продукт и эскалацию новой строке проставит разметка (в течение часа);
        # до неё строка не входит в счётчики жалоб
        payload = [{"url": r[1], "review_id": r[0], "bank": canon_bank(r[2]), "product": None,
                    "dt": r[4], "city": _city(r[5]), "body": r[6],
                    "esc": False} for r in rows]
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
                   coalesce(r.title, '') || ' ' || r.text AS body,
                   nullif(r.raw->>'city', '') AS city
            FROM review r JOIN bank b USING (bank_id)
            WHERE r.source = ANY(:src)
              AND r.source_url IS NOT NULL AND length(r.text) >= :minlen
              -- sravni без идентификатора банка (ранний сборщик писал отзывы витрины
              -- чужим банкам) — банк не проверить, в индекс не берём
              AND NOT (r.source = 'sravni_reviews' AND r.raw->>'review_object_id' IS NULL)
              -- ранний HTML-сборщик banki.ru без номера отзыва подставлял номер
              -- карточки («…/vtb/response/0/»): ссылка в никуда, текст — обрезок
              AND NOT (r.source = 'banki_reviews' AND r.source_url !~ '/response/[0-9]{6,}')
              -- похвала 4–5★ в анализ жалоб не идёт, а разметка каждой стоит денег
              AND NOT (r.source IN ('banki_reviews', 'sravni_reviews') AND r.rating >= 4)
              -- banki.ru: негатив приходит во внешнем корпусе; свой сбор индексируем,
              -- только если корпус отзыв так и не получил (ждём трое суток)
              AND NOT (r.source = 'banki_reviews' AND (
                    r.posted_at > now() - interval '3 days'
                    OR EXISTS (SELECT 1 FROM review_index c
                                WHERE c.url IN ('https://www.banki.ru/services/responses/bank/response/'
                                                || r.source_review_id,
                                                'https://www.banki.ru/services/responses/bank/response/'
                                                || r.source_review_id || '/')
                                  AND c.source = 'bankiru')))
        """), {"minlen": _MIN_LEN, "src": known}).mappings().all()

    # дата позже завтрашнего дня — ошибка разбора площадки (09.12.2026 в
    # сентябре), а не отзыв из будущего: без даты он не попадёт в динамику
    from datetime import datetime, timedelta, timezone
    horizon = datetime.now(timezone.utc) + timedelta(days=1)

    def _dt(v):
        if v is None:
            return None
        vv = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return None if vv > horizon else v

    written = 0
    for i in range(0, len(rows), batch):
        payload = [{"url": r["source_url"], "review_id": int(r["review_id"]),
                    "bank": canon_bank(canon.get(r["bank"], r["bank"])), "product": None,
                    "dt": _dt(r["posted_at"]), "city": _city(r["city"]) if r["city"] else None,
                    "rating": float(r["rating"]) if r["rating"] is not None else None,
                    "source": r["source"], "body": r["body"], "esc": False}
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
                    city = coalesce(EXCLUDED.city, review_index.city),
                    source = EXCLUDED.source, tsv = EXCLUDED.tsv
            """), payload)
        written += len(payload)

    # Свой отзыв banki.ru попадал в индекс, пока внешний корпус его не получил, и
    # оставался там и после: тот же отзыв считался дважды. Корпус — владелец текста,
    # его строка остаётся, наша уходит.
    # Там же — строки раннего HTML-сборщика: без номера отзыва (номер карточки вместо
    # него) и копии одного адреса, записанные со слешем на конце и без.
    with db.session() as s:
        dropped = s.execute(text("""
            DELETE FROM review_index o
             WHERE o.source = 'banki_reviews'
               AND EXISTS (SELECT 1 FROM review_index c
                            WHERE c.source = 'bankiru'
                              AND c.url IN (:base || substring(o.url from '/response/([0-9]+)'),
                                            :base || substring(o.url from '/response/([0-9]+)') || '/'))
        """), {"base": _BANKI_RESP}).rowcount
        dropped += s.execute(text("""
            DELETE FROM review_index o
             WHERE o.source = 'banki_reviews' AND o.url !~ '/response/[0-9]{6,}'
        """)).rowcount
        dropped += s.execute(text("""
            DELETE FROM review_index o
             WHERE o.source = 'banki_reviews'
               AND EXISTS (SELECT 1 FROM review_index c
                            WHERE c.source = 'banki_reviews' AND c.url <> o.url
                              AND rtrim(c.url, '/') = rtrim(o.url, '/')
                              -- обычно это одна запись review, сменившая адрес: остаётся
                              -- адрес со слешем — так пишет нынешний сборщик
                              AND (c.review_id > o.review_id
                                   OR (c.review_id = o.review_id AND c.url > o.url)))
        """)).rowcount
    if dropped:
        log.info("review_index: убрано дублей и строк без номера отзыва: %d", dropped)

    embedded = embed_missing()
    dt = time.time() - t0
    log.info("review_index: локальных отзывов %d, векторов посчитано %d, %.1f с",
             written, embedded, dt)
    return {"ok": True, "rows": written, "embedded": embedded, "dup_dropped": dropped,
            "banks": len(canon), "seconds": round(dt, 1)}


def repair_card_bodies(dry: bool = True) -> dict:
    """Разовая починка отзывов раннего HTML-сборщика banki.ru: вместо обрезанной
    карточки с CSS — полный текст того же отзыва из внешнего корпуса (тот же
    отзыв banki.ru, тот же автор); если корпус его не знает — карточка без
    обвязки. Прежний текст — в review_text_bak_card (одна копия на review_id)."""
    pat = r"\.[a-z][a-z0-9]{6,10}\{[a-z-]+:"
    with db.session() as s:
        rows = s.execute(text("""SELECT review_id, source_url, text FROM review
                                  WHERE source = 'banki_reviews' AND text ~ :p"""), {"p": pat}).mappings().all()
    rids = {r["review_id"]: banki_rid(r["source_url"]) for r in rows}
    full: dict[str, str] = {}
    eng = _source_engine()
    if eng is not None and rids:
        urls = [_BANKI_RESP + x for x in rids.values() if x]
        with eng.connect() as c:
            for x in c.execute(text('SELECT rtrim(url, \'/\') AS u, "reviewBody" AS b FROM bankiru.reviews'
                                    ' WHERE rtrim(url, \'/\') = ANY(:u)'), {"u": urls}).mappings():
                b = (x["b"] or "").strip()
                rid = banki_rid(x["u"])
                if rid and len(b) > len(full.get(rid, "")):      # в корпусе бывают копии — берём полную
                    full[rid] = b
    plan = []
    for r in rows:
        new = full.get(rids[r["review_id"]] or "")
        how = "corpus"
        if not new or len(new) < _MIN_LEN or _CSS_RULES.search(new):
            new, how = clean_body(r["text"]), "stripped"
        plan.append({"id": r["review_id"], "text": new, "how": how})
    out = {"rows": len(rows), "from_corpus": sum(p["how"] == "corpus" for p in plan),
           "stripped": sum(p["how"] == "stripped" for p in plan), "dry": dry}
    if dry or not plan:
        return out
    with db.session() as s:
        s.execute(text("""CREATE TABLE IF NOT EXISTS review_text_bak_card (
                              review_id BIGINT PRIMARY KEY, text TEXT NOT NULL,
                              saved_at TIMESTAMPTZ NOT NULL DEFAULT now())"""))
        s.execute(text("""INSERT INTO review_text_bak_card (review_id, text)
                          SELECT review_id, text FROM review WHERE review_id = ANY(:ids)
                          ON CONFLICT (review_id) DO NOTHING"""), {"ids": [p["id"] for p in plan]})
        s.execute(text("UPDATE review SET text = :text WHERE review_id = :id"),
                  [{"id": p["id"], "text": p["text"]} for p in plan])
    return out


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


class Bodies(dict):
    """{url: {text, product, city}} плюс failed — url, чьё хранилище не ответило.
    Отсутствие url в словаре без failed значит «хранилище ответило, текста нет»."""

    def __init__(self):
        super().__init__()
        self.failed: set[str] = set()


def bodies_for(rows: list[dict]) -> Bodies:
    """Тексты по строкам индекса — каждый из своего хранилища.

    Индекс намеренно не хранит тела: у внешнего корпуса владелец текста — база
    banki.ru, у наших коллекторов — таблица review. Копия завела бы вторую
    версию правды, которая начнёт расходиться с первой.

    Если хранилище не ответило, его url попадают в .failed. До 04.11.2026 сбой
    выглядел как «текста нет», и разметка навсегда записывала такие отзывы в мусор
    (на проде 555 жалоб с нулевой стоимостью разметки, у которых текст читается).
    """
    out = Bodies()
    ext = [r for r in rows if (r.get("source") or "bankiru") == "bankiru"]
    loc = [r for r in rows if (r.get("source") or "bankiru") != "bankiru"]
    if ext:
        eng = _source_engine()
        if eng is None:
            out.failed.update(r["url"] for r in ext)
        else:
            try:
                with eng.connect() as c:
                    for x in c.execute(text(
                        'SELECT r.url, r."reviewBody" AS body, r."product" AS product,'
                        ' r.location AS location FROM bankiru.reviews r'
                        ' WHERE r.id = ANY(:ids)'),
                            {"ids": [r["review_id"] for r in ext]}).mappings():
                        out[x["url"]] = {"text": clean_body(x["body"]),
                                         "product": x["product"],
                                         "city": _city(x["location"])}
            except Exception as e:
                out.failed.update(r["url"] for r in ext)
                log.warning("review_index: тексты внешнего корпуса не забрались (%s)", e)
    if loc:
        try:
            with db.session() as s:
                for x in s.execute(text("""
                    SELECT r.source_url AS url, r.text, r.product_category::text AS product
                    FROM review r WHERE r.review_id = ANY(:ids)
                """), {"ids": [r["review_id"] for r in loc]}).mappings():
                    out[x["url"]] = {"text": clean_body(x["text"]),
                                     "product": x["product"], "city": None}
        except Exception as e:
            out.failed.update(r["url"] for r in loc)
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
        clauses.append("al_product_is(i.product, i.slices, :product)"); p["product"] = product
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
        clauses.append("al_product_is(f.product, f.slices, :product)")
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


# ── Полный набор «со словами запроса»: все значимые слова — рядом ──────────────
# Для ленты, счётчика и выгрузки нужен НАБОР, а не верхушка ранжирования.
# Словесная нога (search_fts) ищет любое из слов — для ранжирования это хорошо,
# как определение «со словами запроса» нет: «переводы самому себе» по любому
# слову — 6 523 жалобы Сбера, все слова где угодно в тексте — 728, точная фраза —
# 33 (замер 03.11.2026). Фраза же теряет «навязали мне страховку» и всё, где
# между словами стоит предлог. Поэтому: все лексемы группы в одном окне
# не длиннее числа слов + NEAR_GAP. Группа — значимые слова запроса или один
# вариант раскрытия (сокращение, синоним) — варианты объединяются.
NEAR_GAP = int(os.getenv("REVIEWS_NEAR_GAP", "2"))
NEAR_CAP = int(os.getenv("REVIEWS_NEAR_CAP", "6000"))
_POS_RX = re.compile(r"'((?:[^']|'')+)':([\d,A-D]+)")


def _positions(tsv_text: str) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for lex, pos in _POS_RX.findall(tsv_text or ""):
        out[lex.replace("''", "'")] = [int(re.sub(r"[A-D]", "", p)) for p in pos.split(",") if p]
    return out


# Основа русского стеммера зависит от формы: «зачисляют» → «зачисля», «зачислили» →
# «зачисл», и «не зачисляют выручку» не находило «не зачислили выручку». Поэтому
# длинные основы сравниваются по началу (PREFIX символов), короткие — целиком.
PREFIX = int(os.getenv("REVIEWS_NEAR_PREFIX", "6"))
# Основа от 5 букв ищется как начало слова: «отказ» (отказ, отказе) должна находить
# «отказали» (основа «отказа») — до 04.11.2026 короткие основы совпадали только
# целиком, и «отказ в реструктуризации ипотеки» находил 2 жалобы за всё время.
# Короче 5 — только целиком: «карт» как начало слова поймала бы «картридж».
_PREFIX_MIN = int(os.getenv("REVIEWS_NEAR_PREFIX_MIN", "5"))


# Семьи слов: snowball даёт совершенному и несовершенному виду разные основы —
# «навязали» → «навяза», «навязывали» → «навязыва», «списали» → «списа», «списывают» →
# «списыва». Общая часть — до суффикса вида: «навяз», «спис». С FAMILY ключ основы —
# эта общая часть, и она ищется как начало слова (от 4 букв). Включено 04.11.2026 по
# замеру на 25 запросах: «навязали страховку» 27 → 50 жалоб за год, «отказывают в
# реструктуризации» 12 → 54; добавленные совпадения, по слепой оценке трёх судей,
# относятся к запросу в 34 из 45 случаев (остальные — соседние ситуации).
FAMILY = os.getenv("REVIEWS_NEAR_FAMILY", "1") == "1"
_ASPECT_RX = re.compile(r"(?:ыва|ива)$")


def _family(lexeme: str) -> str | None:
    """Общая часть видовой пары или None, если основа — не глагол с суффиксом вида."""
    if len(lexeme) >= 7 and _ASPECT_RX.search(lexeme):
        return _ASPECT_RX.sub("", lexeme)
    if len(lexeme) >= 6 and lexeme.endswith("а"):
        return lexeme[:-1]
    return None


def stem_key(lexeme: str) -> str:
    if FAMILY:
        f = _family(lexeme)
        if f and len(f) >= 4:
            return f[:PREFIX]
    return lexeme[:PREFIX] if len(lexeme) > PREFIX else lexeme


def is_prefix(lexeme: str) -> bool:
    """Ищется ли основа запроса как начало слова (а не целиком)."""
    if FAMILY and (f := _family(lexeme)) and len(f) >= 4:
        return True
    return len(lexeme) >= _PREFIX_MIN


def near_window(pos: dict[str, list[int]], lexemes: list[str], gap: int = NEAR_GAP,
                words: int = 0) -> bool:
    """Есть ли окно, где встречается каждая лексема (по началу основы), длиной не
    больше числа слов запроса + gap. words — слов в запросе вместе со служебными:
    в тексте «не», «по», «с» тоже занимают позиции, и «выручку по эквайрингу не
    зачисляют» на запрос «не зачисляют выручку» иначе не попадало в окно (04.11.2026)."""
    lex = [x for x in dict.fromkeys(stem_key(x) for x in lexemes) if x]
    if not lex:
        return False
    pre = {stem_key(x): is_prefix(x) for x in lexemes}
    hits = {x: sorted(p for k, ps in pos.items() if k == x or (pre.get(x) and k.startswith(x))
                      for p in ps) for x in lex}
    if any(not v for v in hits.values()):
        return False
    if len(lex) == 1:
        return True
    events = sorted((p, x) for x in lex for p in hits[x])
    need, have, left = len(lex), {}, 0
    span_max = max(len(lex), words) - 1 + gap
    for right, (p, x) in enumerate(events):
        have[x] = have.get(x, 0) + 1
        while len(have) == need:
            if p - events[left][0] <= span_max:
                return True
            lx = events[left][1]
            have[lx] -= 1
            if not have[lx]:
                del have[lx]
            left += 1
    return False


def _filters(bank, city, month, since_ts) -> tuple[str, dict]:
    clauses, params = [], {}
    if bank:
        clauses.append("f.bank = :bank")
        params["bank"] = bank
    if city:
        clauses.append("f.city = :city")
        params["city"] = city
    if month:
        clauses.append("date_trunc('month', f.dt) = to_date(:month, 'YYYY-MM')")
        params["month"] = month
    if since_ts is not None:
        clauses.append("f.dt >= :since_ts")
        params["since_ts"] = since_ts
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


PHRASE_CAP = int(os.getenv("REVIEWS_PHRASE_CAP", "20000"))


def search_phrases(phrases: list[str], *, bank: str | None = None, city: str | None = None,
                   month: str | None = None, since_ts=None, also: str | None = None,
                   _meta: dict | None = None) -> tuple[list[dict], bool]:
    """Отзывы, где есть КАЖДАЯ фраза дословно (с точностью до словоформы) — phraseto_tsquery
    по индексу целиком в SQL. Раньше фраза искалась окном с разрывом 0 по первым 6000
    кандидатам в Python: часть совпадений терялась, а стоп-слова внутри фразы ломали
    окно. Фраза только из стоп-слов («не без») ничего не ищет — это видно в _meta."""
    phrases = [p.strip() for p in phrases if p and p.strip()]
    if not phrases:
        return [], False
    where, params = _filters(bank, city, month, since_ts)
    try:
        with db.session() as s:
            empty = [p for p in phrases if not s.execute(text(
                "SELECT numnode(phraseto_tsquery(CAST('russian' AS regconfig), :p))"), {"p": p}).scalar()]
            phrases = [p for p in phrases if p not in empty]
            if _meta is not None and empty:
                _meta["stop_phrases"] = empty
            if not phrases and not (also or "").strip():
                return [], False
            tq = " && ".join(f"phraseto_tsquery(CAST('russian' AS regconfig), :p{k})" for k in range(len(phrases)))
            params.update({f"p{k}": p for k, p in enumerate(phrases)})
            if (also or "").strip():
                tq = tq + " && " if tq else ""
                # слова вне кавычек тоже обязательны: «"навязали страховку" ипотека» — фраза
                # и ипотека, а не фраза где угодно (ревью 04.11.2026)
                tq += "plainto_tsquery(CAST('russian' AS regconfig), :also)"
                params["also"] = also
            params["cap"] = PHRASE_CAP
            rows = s.execute(text(f"""
                SELECT f.url, f.review_id, f.source, f.bank, f.product, f.dt, f.city,
                       ts_rank_cd(f.tsv, {tq}) AS rank
                  FROM review_index f
                 WHERE f.tsv @@ ({tq}){where}
                 ORDER BY rank DESC, f.dt DESC
                 LIMIT :cap"""), params).mappings().all()
    except Exception as e:  # noqa: BLE001
        log.warning("bankiru_fts: поиск фразы упал (%s: %s)", type(e).__name__, e)
        if _meta is not None:
            _meta["failed"] = True
        return [], False
    out = [{"url": r["url"], "review_id": int(r["review_id"]), "source": r["source"], "bank": r["bank"],
            "product": r["product"], "date": r["dt"].date().isoformat() if r["dt"] else None,
            "city": r["city"], "rank": float(r["rank"]), "terms": phrases} for r in rows]
    return out, len(rows) >= PHRASE_CAP


def search_near(groups: list[list[str]], *, bank: str | None = None, city: str | None = None,
                month: str | None = None, since_ts=None, gap: int = NEAR_GAP,
                spans: list[int] | None = None, cap: int | None = None,
                _meta: dict | None = None) -> tuple[list[dict], bool]:
    """Отзывы, где для какой-нибудь группы все её слова стоят рядом. (строки, обрезано ли).
    Сбой базы — пустой список и _meta["failed"]: «слов нет» и «поиск не отработал» —
    разные вещи, и аудитор должен их различать."""
    # spans — сколько слов было в исходной группе вместе со служебными (запрос
    # «не зачисляют выручку» → 3, хотя значимых слов два): окно растёт на них
    spans = list(spans or [])
    pairs = [([w for w in g if w.strip()], spans[k] if k < len(spans) else 0)
             for k, g in enumerate(groups) if g]
    pairs = [(g, n) for g, n in pairs if g]
    groups = [g for g, _n in pairs]
    if not groups:
        return [], False
    where, params = _filters(bank, city, month, since_ts)
    params["cap"] = int(cap or NEAR_CAP)
    found: dict[str, dict] = {}
    capped = False
    try:
        with db.session() as s:
            for gi, g in enumerate(groups):
                lexs = s.execute(text(
                    "SELECT tsvector_to_array(to_tsvector(CAST('russian' AS regconfig), :t))"),
                    {"t": " ".join(g)}).scalar() or []
                if not lexs:
                    continue
                # основы уже готовые (как в индексе) — 'simple', а не 'russian': русский
                # словарь обработал бы основу заново («навяза» → другая основа) и выкинул
                # бы «сам» как стоп-слово — «навязали страховку» находила 0 (замер 03.11)
                params["tq"] = " & ".join(
                    "'" + stem_key(x).replace("'", "''") + "'" + (":*" if is_prefix(x) else "")
                    for x in dict.fromkeys(lexs))
                rows = s.execute(text(f"""
                    SELECT f.url, f.review_id, f.source, f.bank, f.product, f.dt, f.city, f.tsv::text AS tsv,
                           ts_rank_cd(f.tsv, to_tsquery(CAST('simple' AS regconfig), :tq)) AS rank
                      FROM review_index f
                     WHERE f.tsv @@ to_tsquery(CAST('simple' AS regconfig), :tq){where}
                     ORDER BY rank DESC, f.dt DESC
                     LIMIT :cap"""), params).mappings().all()
                capped = capped or len(rows) >= params["cap"]
                for r in rows:
                    if r["url"] in found:
                        continue
                    pos = _positions(r["tsv"])
                    # лексемы запроса, которых нет в словаре отзыва (to_tsvector их
                    # не узнал), — не повод отбросить: окно считаем по найденным
                    if not near_window(pos, lexs, gap, words=max(len(g), pairs[gi][1])):
                        continue
                    found[r["url"]] = {"url": r["url"], "review_id": int(r["review_id"]),
                                       "source": r["source"], "bank": r["bank"], "product": r["product"],
                                       "date": r["dt"].date().isoformat() if r["dt"] else None,
                                       "city": r["city"], "rank": float(r["rank"]), "terms": lexs}
    except Exception as e:  # noqa: BLE001
        log.warning("bankiru_fts: поиск «рядом» упал (%s: %s)", type(e).__name__, e)
        if _meta is not None:
            _meta["failed"] = True
        return [], False
    rows = sorted(found.values(), key=lambda r: (-r["rank"], r["date"] or ""))
    return rows, capped
