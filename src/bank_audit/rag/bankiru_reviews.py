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


def _slug_to_ru(n: str, idx: dict) -> str:
    """Слаг/латинская форма банка (sberbank/alfabank/vtb) → русский синоним,
    который реально есть в индексе корпуса. Источник синонимов — общий с детектором
    банков (BANK_SLUG_TRIGGERS), чтобы поиск отзывов и веб-поиск брали одни и те же
    банки. Если n не слаг/не триггер — возвращаем как есть."""
    try:
        from ..ai.llm_utils import BANK_SLUG_TRIGGERS
    except Exception:
        return n
    trs = BANK_SLUG_TRIGGERS.get(n)
    if trs is None:
        for slug, t in BANK_SLUG_TRIGGERS.items():
            if n == slug or n in {_norm(x) for x in t}:
                trs = t
                break
    if not trs:
        return n
    norm_trs = [_norm(t) for t in trs]
    for tn in norm_trs:                       # синоним, прямо присутствующий в корпусе
        if tn in idx:
            return tn
    for tn in norm_trs:                       # синоним-префикс имени (россельхоз→россельхозбанк)
        if len(tn) >= 5:
            for k in idx:
                if k.startswith(tn):
                    return k
    cyr = [t for t in norm_trs if re.search(r"[а-я]", t)]   # иначе — для fuzzy
    return max(cyr or norm_trs, key=len)


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
    n = _slug_to_ru(n, idx)        # sberbank/alfabank/… → русский синоним из корпуса
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
                   theme_rx: str | None = None, city: str | None = None,
                   month: str | None = None, k: int = 8,
                   strict: bool = False, _meta: dict | None = None) -> list[dict]:
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
    theme_rx — готовый POSIX-регэксп темы (строит reviews_dash; сюда приходит
               строкой, чтобы этот модуль не знал про таксономию и не возник
               циклический импорт).
    city, month — те же срезы, что и в ленте без поиска ('YYYY-MM' для месяца).
    strict — пробрасывать исключение наружу вместо тихого []. Нужно вкладке:
             аудитор должен видеть «поиск не отработал», а не «ничего не нашлось».
             Агентам strict не нужен — им пустой список означает «уходи в web».
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
        # Кап кандидатов резал bank-scoped скан до 12000 СВЕЖИХ отзывов — у Сбера
        # это 68% корпуса, и всё старше мая 2025 не находилось вообще (жалоба
        # аудиторов «нарушения ПДС не ищутся»: 40 из 127 таких отзывов лежали за
        # окном). Замер на проде: полный скан по банку 248 мс против 206 мс с
        # окном — 42 мс не стоили трети корпуса. Оценка «~13с» в прежнем
        # комментарии относилась к версии ДО дедупа по url (у Сбера 42697 строк
        # против 17642 уникальных). Банков крупнее окна всего 4 из 219.
        # 0 = без окна; env остаётся аварийным рубильником, а не режимом.
        cand_cap = int(os.getenv("BANKIRU_CAND_CAP", "0"))
        params = {"bank": bank_canon, "product": product,
                  "since_ts": since_ts, "limit": limit}
        if cand_cap > 0:
            params["cand_cap"] = cand_cap
        # Срезы вкладки. Раньше list_reviews молча их выбрасывала при непустом
        # запросе: аудитор выбирал тему или город, писал текст — и получал выдачу
        # без фильтра, хотя чип оставался подсвеченным.
        extra = ""
        if theme_rx:
            extra += '\n                    AND r."reviewBody" ~* :theme_rx'
            params["theme_rx"] = theme_rx
        if city:
            extra += "\n                    AND split_part(r.location, ' (', 1) = :city"
            params["city"] = city
        if month:
            extra += ('\n                    AND date_trunc(\'month\', r."datePublished")'
                      " = to_date(:month, 'YYYY-MM')")
            params["month"] = month
        if not discovery:
            qvec = embedder.embed_one(QUERY_PREFIX + query.strip())
            params["qvec"] = "[" + ",".join(f"{x:.6f}" for x in qvec) + "]"
        if discovery:
            # Без темы: свежие жалобы по банку/продукту (агент сам кластеризует).
            # Эмбеддинг не нужен → быстро.
            sql = text(
                f"""
                SELECT r."bankName" AS bank, r."product" AS product,
                       r."datePublished" AS dt, r.url AS url, r."reviewBody" AS body,
                       r.location AS location, 0.0 AS dist
                FROM bankiru.reviews r
                WHERE r."bankName" = :bank
                  AND (CAST(:product AS text) IS NULL OR r."product" = :product)
                  AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                  AND length(r."reviewBody") >= 40{extra}
                ORDER BY r."datePublished" DESC
                LIMIT :limit
                """
            )
        elif bank_canon:
            # Фильтр по конкретному банку → подмножество ≤25k уникальных отзывов.
            # HNSW тут почти не помогает: замер на проде — при ef_search=40 запрос
            # вернул 2 строки из 20 запрошенных (пост-фильтр по банку съедает
            # выдачу), при ef_search=800 вернул все 20, но за 828 мс, то есть
            # медленнее точного скана. Поэтому MATERIALIZED-CTE + точный скан по
            # подмножеству (на CTE индекс всё равно не применяется).
            #
            # DISTINCT ON (url): корпус содержит точные дубли краулера — без дедупа
            # выдачу заполняли бы копии одной жалобы (все с датой заливки), а
            # реальные отзывы вытеснялись.
            dedup = f"""
                    SELECT DISTINCT ON (r.url) r."bankName" AS bank, r."product" AS product,
                           r."datePublished" AS dt, r.url AS url,
                           r."reviewBody" AS body, r.location AS location, e.embedding AS emb
                    FROM bankiru.reviews r
                    JOIN bankiru.review_embeddings e ON e.review_id = r.id
                    WHERE r."bankName" = :bank
                      AND (CAST(:product AS text) IS NULL OR r."product" = :product)
                      AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                      AND length(r."reviewBody") >= 40{extra}
                    ORDER BY r.url, r."datePublished" DESC
            """
            cand = (f"SELECT * FROM ({dedup}) u ORDER BY dt DESC LIMIT :cand_cap"
                    if cand_cap > 0 else dedup)
            sql = text(
                f"""
                WITH cand AS MATERIALIZED ({cand})
                SELECT bank, product, dt, url, body, location,
                       (emb <=> CAST(:qvec AS vector)) AS dist
                FROM cand ORDER BY emb <=> CAST(:qvec AS vector) LIMIT :limit
                """
            )
        else:
            # Без фильтра по банку — глобальный поиск по всему корпусу через HNSW.
            sql = text(
                f"""
                SELECT r."bankName" AS bank, r."product" AS product,
                       r."datePublished" AS dt, r.url AS url, r."reviewBody" AS body,
                       r.location AS location, (e.embedding <=> CAST(:qvec AS vector)) AS dist
                FROM bankiru.review_embeddings e
                JOIN bankiru.reviews r ON r.id = e.review_id
                WHERE (CAST(:product AS text) IS NULL OR r."product" = :product)
                  AND (CAST(:since_ts AS timestamp) IS NULL OR r."datePublished" >= CAST(:since_ts AS timestamp))
                  {extra}
                ORDER BY e.embedding <=> CAST(:qvec AS vector)
                LIMIT :limit
                """
            )
        is_global = not discovery and not bank_canon
        with eng.connect() as c:
            # Страховка от зависания: любой поиск, вышедший за таймаут, падает
            # (ловится ниже) → вызывающий уходит в web/дальше, а не блокирует
            # агента на минуты. Тюнится BANKIRU_STMT_TIMEOUT_MS.
            _stmt_ms = int(os.getenv("BANKIRU_STMT_TIMEOUT_MS", "9000"))
            c.execute(text(f"SET LOCAL statement_timeout = {_stmt_ms}"))
            if is_global:
                # глобальный HNSW по умолчанию (ef_search=40) даёт мелкую и
                # смещённую выдачу (банки-доминанты вытесняют остальных) — поднимаем
                # recall, чтобы рыночный срез был полнее и разнообразнее по банкам.
                c.execute(text("SET LOCAL hnsw.ef_search = 400"))
            rows = c.execute(sql, params).mappings().all()
    except Exception as e:
        # текст исключения обязателен: по одному имени класса причину на проде не
        # восстановить (таймаут, битый вектор и отвалившийся коннект выглядят одинаково)
        log.warning("bankiru: поиск упал (%s: %s) — отдаю пусто, вызывающий уйдёт в web",
                    type(e).__name__, e)
        if strict:
            # вкладке нужно отличить «поиск не отработал» от «ничего не нашлось»:
            # аудитор видел пустой список и считал, что жалоб по теме просто нет
            raise
        return []

    out: list[dict] = []
    seen: dict[str, int] = {}
    for r in rows:
        body = (r["body"] or "").strip()
        if len(body) < 40:                  # отсекаем тест-мусор
            continue
        key = _dedup_key(body)
        if key in seen:
            # Массовость однотипных жалоб — аудит-сигнал, поэтому дубли считаем,
            # а не молча выбрасываем. Лента без поиска так и делает (поле similar),
            # а поиск счётчик терял, хотя вкладка его показывает.
            out[seen[key]]["similar"] += 1
            continue
        seen[key] = len(out)
        dt = r["dt"]
        out.append({
            "bank": r["bank"],
            "product": r["product"],
            "date": dt.date().isoformat() if dt else None,
            "city": (r["location"] or "").split(" (")[0],
            "url": r["url"],
            "text": body,
            "similar": 0,
            "distance": round(float(r["dist"]), 4),
            "source": "bankiru",
            "via": "смысл",
        })
    # режем в конце, а не по ходу: иначе дубли, попавшие в хвост выборки, не
    # успевали бы увеличить счётчик similar у своих оригиналов
    if discovery or not HYBRID:
        return out[:k]
    return _fuse_with_words(out, query.strip(), k=k, bank=bank_canon, product=product,
                            city=city, month=month, since_ts=since_ts, meta=_meta)


# ── Гибрид: смысл + слова ────────────────────────────────────────────────────
HYBRID = os.getenv("BANKIRU_HYBRID", "1").lower() not in ("0", "false", "no")
# Ниже этого числа словесных попаданий считаем, что запрос аудитора не лёг на
# язык отзывов, и зовём модель раскрыть сокращения и подобрать формулировки.
# Порог, а не «всегда»: расширение стоит секунды, а в большинстве запросов слова
# аудитора в отзывах и так есть.
_THIN = int(os.getenv("BANKIRU_WORDS_THIN", "5"))
_RRF_K = int(os.getenv("BANKIRU_RRF_K", "60"))


def _fuse_with_words(vec: list[dict], query: str, *, k: int, bank: str | None,
                     product: str | None, city: str | None, month: str | None,
                     since_ts, meta: dict | None = None) -> list[dict]:
    """Сливает векторную выдачу со словесной ранговой суммой (RRF).

    Зачем вообще вторая нога: вектор не кодирует аббревиатуры. Замер на проде —
    «нарушения ПДС» давал 0 релевантных из 10, хотя в архиве 127 таких отзывов
    только по Сберу. Словесный поиск находит их за 6 мс.

    Почему именно ранговая сумма, а не порог по косинусу: дистанции релевантного
    и мусора перекрываются полностью (замер: мусор на 0.496 «лучше» верного
    попадания на 0.535), поэтому отсечь по числу нельзя в принципе. RRF работает
    с МЕСТАМИ в двух списках, а не с абсолютными оценками, и от их несравнимости
    не страдает.

    Импорты внутри функции: bankiru_fts берёт отсюда движок источника, импорт на
    уровне модуля замкнул бы кольцо.
    """
    from . import bankiru_fts, reviews_query
    if not bankiru_fts.is_ready():          # зеркало ещё не наполнено — одна нога
        return vec[:k]

    fetch = max(k * 6, 30)
    qx = reviews_query.expand(query)
    words = bankiru_fts.search_fts(qx["tsquery"], bank=bank, product=product,
                                   city=city, month=month, since_ts=since_ts, k=fetch)
    if len(words) < _THIN:
        # слова аудитора не легли на язык отзывов — просим модель раскрыть
        qx2 = reviews_query.expand(query, use_llm=True)
        if qx2["tsquery"] != qx["tsquery"]:
            more = bankiru_fts.search_fts(qx2["tsquery"], bank=bank, product=product,
                                          city=city, month=month, since_ts=since_ts, k=fetch)
            if len(more) > len(words):
                qx, words = qx2, more

    # Третья нога: смысл по НАШИМ векторам. Отзывы своих коллекторов лежат в
    # другом хранилище и посчитаны другим рецептом эмбеддинга, поэтому в один
    # ANN-запрос с внешним корпусом их класть нельзя. RRF складывает МЕСТА в
    # списках, а не расстояния, и разница масштабов между ногами ему безразлична
    # — ровно поэтому здесь и подходит ранговое слияние.
    qvec = embedder.embed_one(QUERY_PREFIX + query)
    local = bankiru_fts.search_vectors_local(
        qvec, bank=bank, product=product, city=city, month=month,
        since_ts=since_ts, k=fetch)

    rank_v = {r["url"]: i + 1 for i, r in enumerate(vec)}
    rank_w = {r["url"]: i + 1 for i, r in enumerate(words)}
    rank_l = {r["url"]: i + 1 for i, r in enumerate(local)}
    by_url = {r["url"]: r for r in vec}
    terms_by_url = {r["url"]: r.get("terms") or [] for r in words}

    score: dict[str, float] = {}
    for ranks in (rank_v, rank_w, rank_l):
        for url, i in ranks.items():
            score[url] = score.get(url, 0.0) + 1.0 / (_RRF_K + i)
    order = sorted(score, key=lambda u: (-score[u], rank_v.get(u, 10**6)))

    # Тела отзывов, найденных не векторной ногой по внешнему корпусу, лежат в
    # разных хранилищах: у banki.ru — в его базе, у наших коллекторов — в
    # таблице review. Диспетчер разбирается по признаку источника.
    need = {r["url"]: r for r in (words + local) if r["url"] not in by_url}
    if need:
        bodies = bankiru_fts.bodies_for(list(need.values()))
        # имя НЕ meta: одноимённый выходной параметр функции затенился бы, и
        # сводка поиска молча уезжала бы в строку выдачи вместо ответа ручки
        for url, hit in need.items():
            b = bodies.get(url) or {}
            body = (b.get("text") or "").strip()
            if len(body) < 40:
                continue
            by_url.setdefault(url, {
                "bank": hit.get("bank"), "product": hit.get("product") or b.get("product"),
                "date": hit.get("date"), "city": hit.get("city") or b.get("city"),
                "url": url, "text": body, "similar": 0,
                "rating": hit.get("rating"),
                "source": hit.get("source") or "bankiru",
                "distance": hit.get("distance"), "via": "слова"})

    out: list[dict] = []
    for url in order:
        r = by_url.get(url)
        if not r:
            continue
        hit_sense = url in rank_v or url in rank_l   # смысл из любого хранилища
        hit_w = url in rank_w
        r["via"] = ("слова и смысл" if hit_sense and hit_w
                    else ("слова" if hit_w else "смысл"))
        r["terms"] = terms_by_url.get(url, [])
        out.append(r)
        if len(out) >= k:
            break

    # Подсветка совпавшего — только по тем отзывам, что реально показываем.
    marked = bankiru_fts.headline([r["text"] for r in out], qx["tsquery"])
    for r, m in zip(out, marked):
        r["marked"] = m

    if meta is not None:
        base = set(w.lower() for w in reviews_query.base_terms(query))
        meta.update({
            "terms": qx["terms"],
            "added": [t for t in qx["terms"] if t.lower() not in base],
            "common": qx.get("common") or [],
            "dropped": qx.get("dropped") or [],
            "source": qx["source"],
            "n_words": sum(1 for r in out if r["via"] != "смысл"),
            "n_sense": sum(1 for r in out if r["via"] == "смысл"),
        })
    return out


def _bodies_by_id(need: dict[int, dict]) -> dict[str, dict]:
    """Тексты отзывов из источника по id — для попаданий, которые дала только
    словесная нога. Одним запросом, максимум на пару десятков строк."""
    eng = _get_engine()
    if eng is None or not need:
        return {}
    try:
        with eng.connect() as c:
            rows = c.execute(text(
                'SELECT r.id, r."bankName" AS bank, r."product" AS product,'
                ' r."datePublished" AS dt, r.url AS url, r."reviewBody" AS body,'
                ' r.location AS location'
                ' FROM bankiru.reviews r WHERE r.id = ANY(:ids)'),
                {"ids": list(need)}).mappings().all()
    except Exception as e:
        log.warning("bankiru: тела по id не забрались (%s: %s)", type(e).__name__, e)
        return {}
    out = {}
    for r in rows:
        body = (r["body"] or "").strip()
        if len(body) < 40:
            continue
        dt = r["dt"]
        out[r["url"]] = {"bank": r["bank"], "product": r["product"],
                         "date": dt.date().isoformat() if dt else None,
                         "city": (r["location"] or "").split(" (")[0],
                         "url": r["url"], "text": body, "similar": 0,
                         "distance": None, "via": "слова"}
    return out


def search_reviews_multi(query: str | None = None, *, banks: list[str],
                         product: str | None = None, since_days: int | None = None,
                         k_per: int = 8) -> dict:
    """Точечный поиск по КАЖДОМУ банку ОТДЕЛЬНО (bank-scoped) — для сравнения/«топа».
    Надёжнее глобального семантического: у каждого банка свой top-k (точный скан по
    подмножеству), банки не вытесняют друг друга. Возвращает {canonBank: [reviews]}.
    Эмбеддинг запроса кэшируется → повторные банки не пересчитывают вектор."""
    out: dict[str, list] = {}
    seen: set[str] = set()
    for b in (banks or [])[:10]:          # верхний предел на число банков за вызов
        if not b:
            continue
        canon = resolve_bank(b)
        if not canon or canon in seen:    # нерезолвящиеся/дубли пропускаем
            if b and not canon:
                out.setdefault(b, [])     # пометим как «нет в корпусе» пустым списком
            continue
        seen.add(canon)
        out[canon] = search_reviews(query, bank=canon, product=product,
                                    since_days=since_days, k=k_per)
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
