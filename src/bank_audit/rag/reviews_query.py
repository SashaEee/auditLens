"""Понимание поискового запроса аудитора — вместо словаря аббревиатур.

Проблема, с которой пришли аудиторы: в поиске по отзывам пишут «нарушения ПДС»,
а находится пусто. Причин две, и обе не лечатся ранжированием:

1. Вектор не кодирует аббревиатуру. Замер: «нарушения ПДС» — 0 релевантных из 10
   при том, что в корпусе 127 таких отзывов по одному Сберу.
2. Полнотекст соединяет слова через И, поэтому «нарушения ПДС» требует, чтобы в
   отзыве стояли ОБА слова. Замер после наполнения зеркала: 1 попадание против
   10 у запроса «пдс OR "программа долгосрочных сбережений"».

Напрашивался словарь: ПДС → программа долгосрочных сбережений, НСЖ → …, ПСК → …
Это тупик: банковских продуктов и сокращений столько, что список придётся вечно
дописывать руками, и он всё равно будет отставать от того, как пишут клиенты.
Поэтому раскрытием занимается модель — она эти сокращения знает и без словаря.

Устройство слоёное, каждый слой работает сам по себе:
  • базовый — разбор запроса на слова и сборка ИЛИ-запроса. Без сети, без LLM,
    всегда доступен. Одного этого хватает, чтобы «нарушения ПДС» начало находить.
  • поверх — расширение моделью: раскрытие сокращений, синонимы, формулировки,
    какими об этом пишут клиенты. Кэшируется, при недоступности молча падает на
    базовый слой.

Осознанно НЕ делаем: не просим модель ранжировать выдачу и не даём ей решать,
что показать аудитору. Ранжирование остаётся детерминированным и воспроизводимым,
модель отвечает только за то, ЧТО искать, а не за то, что показать.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

# Расширение — не обязательная часть поиска. Выключается без передеплоя.
ENABLED = os.getenv("REVIEWS_QUERY_EXPAND", "1").lower() not in ("0", "false", "no")
_TTL_DAYS = int(os.getenv("REVIEWS_QUERY_TTL_DAYS", "30"))
_TIMEOUT_S = float(os.getenv("REVIEWS_QUERY_TIMEOUT_S", "12"))
_MAX_TERMS = int(os.getenv("REVIEWS_QUERY_MAX_TERMS", "12"))
_NS = "rev_q"

# Служебные слова русского языка — предлоги, союзы, частицы, местоимения.
# Это закрытый список, он не про предметную область и не устаревает.
#
# Предметных слов здесь сознательно НЕТ. Соблазн дописать сюда «банк», «жалоба»,
# «проблема», «отказ» — начало того самого словаря, который придётся вечно вести
# руками. Какое слово бесполезно для поиска, решает сам архив: см.
# drop_too_common(). Он же ловит то, чего ни в одном списке стоп-слов не будет —
# для корпуса жалоб «нарушения» и «проблемы» такие же служебные, как предлоги.
_STOP = {
    "и", "или", "но", "а", "в", "во", "на", "по", "за", "от", "до", "из", "с", "со",
    "к", "у", "о", "об", "для", "при", "про", "что", "как", "где", "когда", "чем",
    "не", "ни", "же", "ли", "бы", "это", "этот", "эта", "все", "весь", "был", "была",
    "есть", "мне", "мой", "моя", "их", "его", "её", "они", "мы", "я", "он", "она",
}

_SYSTEM = (
    "Предметная область: РОЗНИЧНЫЕ БАНКОВСКИЕ ПРОДУКТЫ В РОССИИ и жалобы клиентов "
    "на них. Сокращения раскрывай именно в этом смысле: например ПСК это полная "
    "стоимость кредита, а не что-либо ещё.\n\n"
    "Ты помогаешь аудитору банка искать жалобы клиентов в архиве отзывов. Аудитор "
    "пишет запрос профессиональным жаргоном и сокращениями, а клиенты в отзывах "
    "пишут бытовым языком и часто не знают официальных названий продуктов.\n\n"
    "Преврати запрос аудитора в набор поисковых вариантов, по которым нужная жалоба "
    "действительно найдётся в тексте отзыва.\n\n"
    "Правила:\n"
    "— сокращение раскрывай ПОЛНОСТЬЮ отдельной строкой и оставляй само сокращение;\n"
    "— если у сокращения в банковской рознице есть несколько прочтений, дай их все, "
    "каждое отдельной строкой: лишнее будет отсеяно проверкой по архиву;\n"
    "— добавляй то, КАК об этом пишут обычные люди, а не как это зовётся в документах;\n"
    "— добавляй близкие по смыслу формулировки и однокоренные варианты;\n"
    "— НЕ расширяй тему: спросили про вклад — не добавляй кредит и ипотеку;\n"
    "— не придумывай названий продуктов, которых не существует;\n"
    "— от 4 до 10 вариантов, каждый с новой строки, без нумерации, кавычек и пояснений;\n"
    "— вариант из нескольких слов пиши как есть, он будет искаться точной фразой."
)


def _norm_query(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().lower())


def _cache_key(q: str) -> str:
    return hashlib.sha256(_norm_query(q).encode("utf-8")).hexdigest()[:40]


def _cache_get(q: str) -> list[str] | None:
    try:
        with db.session() as s:
            row = s.execute(text(
                "SELECT value FROM rag_cache WHERE cache_key = :k AND namespace = :n"
                " AND expires_at > now()"), {"k": _cache_key(q), "n": _NS}).first()
        if not row:
            return None
        val = row[0]
        val = json.loads(val) if isinstance(val, str) else val
        return list(val.get("terms") or []) or None
    except Exception:
        return None


def _cache_put(q: str, terms: list[str]) -> None:
    try:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO rag_cache (cache_key, namespace, value, expires_at)
                VALUES (:k, :n, CAST(:v AS jsonb), now() + make_interval(days => :d))
                ON CONFLICT (cache_key) DO UPDATE
                    SET value = EXCLUDED.value, expires_at = EXCLUDED.expires_at
            """), {"k": _cache_key(q), "n": _NS,
                   "v": json.dumps({"q": _norm_query(q), "terms": terms}, ensure_ascii=False),
                   "d": _TTL_DAYS})
    except Exception as e:
        log.debug("reviews_query: кэш не записан (%s)", e)


def base_terms(query: str) -> list[str]:
    """Базовый слой: слова самого запроса, без служебных. Работает всегда."""
    words = re.findall(r"[0-9A-Za-zА-Яа-яЁё]{2,}", query or "")
    out, seen = [], set()
    for w in words:
        low = w.lower()
        if low in _STOP or low in seen:
            continue
        seen.add(low)
        out.append(w)
    # если запрос состоял из одних служебных слов — ищем как есть, не выдумываем
    return out or [w for w in words] or ([query.strip()] if query.strip() else [])


def _llm_terms(query: str) -> list[str]:
    """Расширение моделью. Любая осечка → пустой список, вызывающий не заметит."""
    from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL, fast_model
    if not LLM_API_KEY:
        return []
    try:
        from openai import OpenAI
        c = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                   max_retries=1, timeout=_TIMEOUT_S)
        # _patch_client_reasoning_effort сюда НЕ годится: он оборачивает create в
        # корутину и рассчитан на AsyncOpenAI — на синхронном клиенте вызов молча
        # превращается в неожидаемый coroutine и расширение тихо отключается.
        # Рассуждения здесь выключены намеренно: задача — вспомнить, как продукт
        # называется полностью, размышлять тут не над чем, а thinking-токены
        # считаются в тот же бюджет и обрывают список на полуслове.
        effort = os.getenv("REVIEWS_QUERY_EFFORT", "none").lower()
        extra = {} if effort in ("off", "none", "") else {"reasoning_effort": effort}
        r = c.chat.completions.create(
            model=fast_model(),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": f"Запрос аудитора: {query.strip()}"}],
            temperature=0, max_tokens=2000, extra_body=extra)
        raw = (r.choices[0].message.content or "").strip()
    except Exception as e:
        log.info("reviews_query: расширение недоступно (%s: %s) — работаем базовым слоем",
                 type(e).__name__, e)
        return []
    out = []
    for line in raw.splitlines():
        # модель иногда всё же нумерует и кавычит, несмотря на инструкцию
        t = re.sub(r'^[\s\-—*•\d.)]+', "", line).strip().strip('"«»').strip()
        if 1 < len(t) <= 90:
            out.append(t)
    return out[:_MAX_TERMS]


def _to_tsquery(terms: list[str]) -> str:
    """Собирает строку для websearch_to_tsquery: варианты через ИЛИ, многословные —
    точной фразой в кавычках. Кавычки внутри вариантов вычищаем, иначе непарная
    кавычка от модели меняет смысл всего запроса."""
    parts, seen = [], set()
    for t in terms:
        clean = re.sub(r'["»«]', " ", t).strip()
        clean = re.sub(r"\s+", " ", clean)
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        parts.append(f'"{clean}"' if " " in clean else clean)
    return " OR ".join(parts)


_MIN_SUPPORT = int(os.getenv("REVIEWS_QUERY_MIN_SUPPORT", "3"))


def corpus_support(terms: list[str], cap: int = 50) -> dict[str, int]:
    """Сколько отзывов в архиве реально содержат каждый вариант.

    Это проверка предложений модели фактом. Раскрытие сокращения бывает
    правдоподобным и неверным: на «ПДС» модель предложила «правила
    дистанционного обслуживания» — звучит складно, но в жалобах клиентов такого
    нет, а «программа долгосрочных сбережений» есть. Словарь такую ошибку не
    поймал бы никогда, архив ловит её сразу и бесплатно.

    Считаем не до конца, а до потолка: нам нужно решение «есть или нет», а не
    точное число, и на частом слове полный подсчёт стоил бы дороже самого поиска.
    """
    terms = [t for t in (terms or []) if t and t.strip()]
    if not terms:
        return {}
    # многословный вариант проверяем ТОЧНОЙ ФРАЗОЙ. Без кавычек
    # websearch_to_tsquery соединяет слова через И где угодно в тексте, и
    # «правила дистанционного обслуживания» находится по трём словам вразброс —
    # проверка перестаёт что-либо проверять и пропускает неверное раскрытие.
    probe = {t: (f'"{t}"' if " " in t.strip() else t) for t in terms}
    try:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT t.term,
                       (SELECT count(*) FROM (
                           SELECT 1 FROM review_index f
                           WHERE f.tsv @@ websearch_to_tsquery(
                                     CAST('russian' AS regconfig), t.probe)
                           LIMIT :cap) z) AS n
                FROM unnest(CAST(:terms AS text[]), CAST(:probes AS text[]))
                     AS t(term, probe)
            """), {"terms": list(probe), "probes": list(probe.values()),
                   "cap": int(cap)}).all()
        return {r[0]: int(r[1]) for r in rows}
    except Exception as e:
        log.info("reviews_query: проверка по корпусу недоступна (%s) — беру как есть", e)
        return {t: _MIN_SUPPORT for t in terms}


# Во сколько раз слово может быть чаще самого редкого слова запроса, оставаясь
# полезным. Порог ОТНОСИТЕЛЬНЫЙ, и это принципиально: абсолютный не работает.
# Замер: «неверная ПСК в договоре» — ПСК встречается в 73 отзывах, «неверная» на
# три порядка чаще; под любым разумным абсолютным потолком «неверная» проходит,
# но в ИЛИ-запросе она топит ПСК, и выдача была 0 релевантных из 10.
_RARITY_RATIO = int(os.getenv("REVIEWS_QUERY_RARITY_RATIO", "20"))
_FREQ_PROBE = int(os.getenv("REVIEWS_QUERY_FREQ_PROBE", "20000"))


def drop_too_common(terms: list[str]) -> tuple[list[str], list[str]]:
    """Оставляет слова, различающие отзывы, и убирает те, что их заглушают.

    Это замена списку стоп-слов в коде, и она точнее любого списка: для корпуса
    жалоб «нарушения», «проблемы» и «отказ» такие же служебные, как предлоги,
    хотя ни в одном словаре стоп-слов их нет. Что здесь часто, знает сам архив.

    Мера — редкость относительно самого редкого слова запроса, а не абсолютное
    число. Аудитор всегда приносит одно-два содержательных слова и обвязку из
    общих («неверная», «проблемы с», «нарушения»); содержательное почти всегда
    самое редкое, и именно оно должно решать, что показать.

    Слова, которых в архиве нет вовсе, из подсчёта редкости исключаются: иначе
    опечатка с нулём совпадений обнулила бы порог и выбросила весь запрос.
    Если различить нечего — не выбрасываем ничего, размытая выдача лучше пустой.
    """
    if len(terms) < 2:
        return list(terms), []
    freq = corpus_support(terms, cap=_FREQ_PROBE)
    present = [freq.get(t, 0) for t in terms if freq.get(t, 0) > 0]
    if not present:
        return list(terms), []
    limit = max(present) if len(set(present)) == 1 else min(present) * _RARITY_RATIO
    kept = [t for t in terms if freq.get(t, 0) <= limit]
    if not kept:
        return list(terms), []
    return kept, [t for t in terms if t not in kept]


def expand(query: str, *, use_llm: bool = False) -> dict:
    """Запрос аудитора → {tsquery, terms, dropped, source}.

    По умолчанию работает базовый слой — он бесплатный и мгновенный, и одного
    его хватает в большинстве случаев: «нарушения ПДС» превращается в
    «нарушения OR ПДС» и находит то, что раньше не находилось.

    use_llm=True вызывающий ставит только тогда, когда базовый слой дал скудную
    выдачу. Так модель не сидит в горячем пути: она стоит 4–5 секунд, и платить
    их на каждый запрос ради случая, который и без неё решается, незачем.

    source: 'base' | 'cache' | 'llm' — уезжает в ответ ручки, чтобы во вкладке
    было видно, по каким словам искали на самом деле. Иначе поиску не доверяют.
    dropped — что модель предложила, а архив не подтвердил.
    """
    q = (query or "").strip()
    if not q:
        return {"tsquery": "", "terms": [], "dropped": [], "source": "base"}
    base = base_terms(q)
    base, common = drop_too_common(base)
    plain = {"tsquery": _to_tsquery(base), "terms": base,
             "dropped": [], "common": common, "source": "base"}
    if not use_llm or not ENABLED:
        return plain

    cached = _cache_get(q)
    source = "cache"
    if cached is None:
        cached = _llm_terms(q)
        source = "llm"
        if cached:
            _cache_put(q, cached)
    if not cached:
        return plain

    # слова самого аудитора проверке не подлежат — он знает, что ищет
    support = corpus_support([t for t in cached if t.lower() not in {b.lower() for b in base}])
    kept = [t for t in cached if support.get(t, _MIN_SUPPORT) >= _MIN_SUPPORT]
    dropped = [t for t in cached if t not in kept]
    if dropped:
        log.info("reviews_query: %r — архив не подтвердил варианты: %s", q, ", ".join(dropped))
    merged = _merge(base, kept)
    return {"tsquery": _to_tsquery(merged), "terms": merged,
            "dropped": dropped, "common": common, "source": source}


def _merge(base: list[str], extra: list[str]) -> list[str]:
    """Слова самого аудитора идут первыми и не выбрасываются: даже если модель
    увела расширение в сторону, исходный запрос обязан остаться в поиске."""
    out, seen = [], set()
    for t in list(base) + list(extra):
        low = t.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(t)
    return out[:_MAX_TERMS + len(base)]
