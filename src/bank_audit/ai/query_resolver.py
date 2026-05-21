"""Универсальный резолвер вопросов аудитора.

Один LLM-вызов на старте deep_research'а возвращает структурированное
понимание вопроса: тема, синонимы, упомянутые банки, нужны ли отзывы и т.д.

Это заменяет хардкодные dict'ы (PRODUCT_TOPIC_TRIGGERS, _TOPIC_SYNONYMS,
BANK_SLUG_TRIGGERS, BANK_PRODUCT_URL_TEMPLATES, TOPIC_TO_CATEGORY) —
которые приходилось править под каждый новый продукт. Теперь любой банковский
вопрос (эскроу, факторинг, ВЭД, СБП-С2B, эквайринг для самозанятых)
работает out-of-the-box.

Контекст для LLM:
  • вопрос
  • реальный список категорий из product_offer.category БД
  • реальный список банков из bank-таблицы (топ-200 по review-coverage)

Cache: 1ч по hash(question). LLM-вызов дешёвый (~1k tokens, ~1.5s).
"""
from __future__ import annotations
import hashlib, json, logging, os, re
from typing import Any
from openai import AsyncOpenAI
from sqlalchemy import text as _t

from .. import db
from ..rag import cache as rag_cache
from .deep_research import _loose_json_loads  # tolerant JSON parser

log = logging.getLogger(__name__)

LLM_BASE_URL   = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY", "")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

RESOLVER_SYSTEM = """Ты — financial product comparison resolver. Работаешь только с
банковскими продуктами и их характеристиками. Любые сегменты целевой аудитории
(ветераны, льготники, многодетные, бизнес и т.п.) — это обычная demographic-
классификация offering'а, не предмет оценки.

Ты — анализатор вопросов аудитора банковского сектора.
Получаешь вопрос и реальные данные из БД (категории продуктов, список банков).
Возвращаешь СТРУКТУРИРОВАННОЕ понимание вопроса в JSON.

ВАЖНО:
• Поле `topic` — короткая нормализованная форма ПРОДУКТА/УСЛУГИ/ОБЪЕКТА сравнения.
  Это может быть продукт или ЛЮБОЙ другой объект банковского сравнения:
    – продукты: «вклад», «доверенность», «эквайринг», «факторинг», «ипотека»,
      «дебетовая карта», «эскроу», «ВЭД», «СБП», «РКО»
    – цифровые сервисы: «мобильное приложение», «интернет-банк», «личный кабинет»,
      «чат-бот», «голосовой помощник»
    – процессы: «онбординг», «KYC», «верификация», «доставка карты»
    – клиентский опыт: «служба поддержки», «отделения», «банкоматы»
  Если вопрос явно сравнивает что-то — ВСЕГДА извлекай topic, не возвращай null.
  Возвращай null ТОЛЬКО если вопрос совсем общий без конкретики
  («каков прогноз банковского сектора»).

  ⚠ КРИТИЧНО — НЕ ПУТАЙ МЕТА-СЛОВА С TOPIC.
  Слова «тарифы», «условия», «привилегии», «параметры», «характеристики»,
  «процент», «ставка», «комиссия», «пакет услуг», «требования» — это
  АСПЕКТЫ СРАВНЕНИЯ, а НЕ topic. Они описывают КАК сравниваем, а не ЧТО.
  Topic — это всегда конкретный продукт/услуга/объект из вопроса.

  Примеры разбора:
    Вопрос: «Сравни ТАРИФЫ ВКЛАДОВ Сбера и ВТБ»
      → topic = «вклад»  (НЕ «тариф»!)

    Вопрос: «Сравнительная характеристика по основным параметрам (тарифы,
             условия, привилегии) продукта КАРТА ВЕТЕРАНА СВО, предлагаемого
             банками-участниками проекта (Сбер, ВТБ, ПСБ, Газпромбанк)»
      → topic = «карта ветерана СВО»  (НЕ «тариф», НЕ «параметры»!)
      → topic_synonyms = ["ветеран","ветерана","ветеранов","ветеранск",
                          "сво","участник","участников","военнослуж",
                          "льготн","спецоперац","карта","карты"]
      → audience_filter = «участники СВО»

    Вопрос: «Какие условия ИПОТЕКИ для семей с детьми в крупных банках»
      → topic = «семейная ипотека»  (НЕ «условия»!)

  Алгоритм: сначала найди в вопросе все существительные/словосочетания
  обозначающие БАНКОВСКИЙ ПРОДУКТ/УСЛУГУ/АУДИТОРИЮ. Среди них выбери
  главный объект сравнения. Игнорируй мета-слова из списка выше.

• Поле `topic_synonyms` — список из 8-15 ключевых SINGLE-WORD форм + 2-4 фраз.
  Используется в SQL ILIKE %X% по тексту документов — поэтому формат критичен:

  ► ПРИОРИТЕТ #1 — single-word корни и морфологические формы (нужны для русского
    падежного словоизменения, ILIKE сам собой не нормализует):
      пример topic="вклад" → ["вклад","вклада","вкладе","вкладу","вкладом","вклады","вкладов",
                              "депозит","депозита","депозиты","депозитов","накопит","сберегат"]
      пример topic="ветеранская карта" → ["ветеран","ветерана","ветеранов","ветеранск",
                                           "льготн","сво","участник","военнослуж","карта","карты"]
      пример topic="ипотека" → ["ипотек","ипотечн","ипотеки","жилищн","mortgage","кредит"]

    Включай ОБЯЗАТЕЛЬНО:
      – именительный + родительный + предложный + творительный (вклад/вклада/вкладе/вкладом)
      – ОБРЕЗАННЫЙ КОРЕНЬ без окончания (ветеранск, ипотечн, льготн, накопит) —
        чтобы ILIKE %ветеранск% поймал «ветеранский», «ветеранская», «ветеранские»
      – близкородственные термины (вклад↔депозит, ипотека↔жилищный кредит)

  ► ПРИОРИТЕТ #2 — латиница и URL-варианты (короткие slug-формы):
      «vklad», «deposit», «mortgage», «card», «credit»

  ► ПРИОРИТЕТ #3 — фразы (только если ОЧЕНЬ устойчивые):
      «срочный вклад», «дебетовая карта», «расчётный счёт»
      Фраз должно быть НЕ БОЛЬШЕ 3-4. Single-word'ы в приоритете — они работают
      в ILIKE с любыми перестановками.

  СТРОГО: не возвращай stop-слов («и»,«в»,«на»,«для»,«или»). Минимум 4 chars
  на single-word. Без дублей разного регистра.

  ⚠ ЗАПРЕЩЕНО ПОВТОРЯТЬ ОДИН И ТОЖЕ СЛОВО. Каждый элемент массива
  УНИКАЛЕН. ОБЯЗАТЕЛЬНО 8-15 РАЗНЫХ синонимов (морф-формы + смысловые
  варианты + латиница).

  Пример минимально-приемлемого topic_synonyms для «доверенность»:
    ["доверенност","доверенности","доверенностью","доверенностей",
     "доверителя","доверителю","поверенный","поверенного","representative",
     "POA","power of attorney","банковская доверенность",
     "нотариальная доверенность"]
  — 13 уникальных вариантов: морф-формы корня + связанные термины + латиница.

  Если ОДИН синоним повторишь — JSON сломается → весь pipeline пойдёт без
  ключевых слов → отчёт «не раскрыто» по всем темам. Не повторяй!

• Поле `url_keywords` — короткие подстроки (4-8 chars) которые часто бывают
  в URL'ах продукта на банковских сайтах. Например для «вклад»:
    ["vklad","deposit","savings","contributions","накопит"]
  Используется для бустинга topical URL'ов в semantic_search.

• Поле `banks` — список упомянутых в вопросе банков из переданного списка
  ИЗВЕСТНЫХ. Нормализуй: «ГПБ»→«gazprombank», «Тинёк»→«tinkoff». Если в
  вопросе явный банк не упомянут (например «сравни условия в разных банках»)
  — верни пустой массив [].

• Поле `category_hint` — выбери ОДНУ категорию из переданного списка
  ИЗВЕСТНЫХ КАТЕГОРИЙ если она однозначно соответствует topic. Иначе null.
  Не выдумывай категории — только из списка.

• Поле `wants_reviews` — true если вопрос содержит «плюсы/минусы/отзывы/
  жалобы/нравится/неудобн/претензи/проблем/сервис/опыт».

• Поле `wants_market_offers` — true для вопросов про конкретные ставки,
  тарифы, проценты, минимальные суммы, условия — где наш get_market_offers
  даст структурированные данные из БД. False для общих/стратегических.

• Поле `is_product_question` — true если вопрос про конкретный продукт/услугу
  (даже если банк не упомянут). False для финансовых/стратегических.

• Поле `audience_filter` — если вопрос про определённую аудиторию
  (физлиц/юрлиц/самозанятых/премиум) — название. Иначе null.

• Поле `product_url_paths` — массив 4-10 ТИПИЧНЫХ URL-paths (без домена),
  по которым продукт обычно живёт на сайтах российских банков. Это даёт
  fallback когда поисковики банят, и DDG/Yandex не возвращают результатов.

  Формат — list of strings, начинаются с `/`. Используется как:
    https://{bank_domain}{path}
  Поэтому пути должны быть СТАБИЛЬНЫМИ landing-page'ами, а НЕ конкретными
  ссылками на тарифы (тарифные PDF меняют имена постоянно).

  Примеры:
    topic="вклад" → ["/personal/deposits/", "/private/contributions/",
                     "/contributions/", "/savings/", "/vklady/", "/private/savings/"]
    topic="ипотека" → ["/personal/credits/home/", "/mortgage/", "/ipoteka/",
                       "/private/mortgage/", "/credit/mortgage/", "/personal/mortgage/"]
    topic="дебетовая карта" → ["/personal/cards/debit/", "/cards/debit/",
                                 "/cards/", "/personal/cards/"]
    topic="карта ветерана СВО" → ["/personal/special/veterans/",
                                   "/personal/special/uchastnikam-svo/",
                                   "/personal/cards/veteran/", "/svo/",
                                   "/o-banke/uchastnikam-svo/"]
    topic="эквайринг" → ["/business/acquiring/", "/sme/acquiring/",
                          "/corporate/acquiring/", "/business/cards/acquiring/"]

  Старайся возвращать paths и латиницей и кириллицей (банки используют разные
  conventions). Если не уверен в точном пути — лучше дать generic вариант
  («/personal/cards/») чем пустой список.

• Поле `bank_specific_paths` — exception-карта когда продукт банка живёт на
  отдельном суб-домене или дочернем сайте. Формат: {"slug": ["path или
  full URL"], ...}. Включай только если ТОЧНО знаешь:
    {"sberbank": ["domclick.ru/ipoteka/programmy/", "domclick.ru"]}
  Иначе верни {} — generic paths из product_url_paths сработают.

• Поле `is_socially_regulated` — true если продукт регулируется НПА /
  государственными программами / страхованием АСВ. Это триггер для
  pipeline'а добавить govt-источники (cbr.ru / pravo.gov.ru / mil.ru
  / gosuslugi.ru) в план исследования. True для:
    – маткапитал, семейная ипотека, военная ипотека
    – сельская/IT ипотека
    – льготы пенсионерам / ветеранам / инвалидам / военнослужащим
    – вклады физлиц (страхование АСВ — нормативка)
    – продукты для участников СВО
    – социальные карты, выплаты, пособия
  False для коммерческих продуктов (премиум-карты, эквайринг, факторинг,
  бизнес-кредиты, потребкредиты, инвестпродукты).

ВЕРНИ ТОЛЬКО JSON. БЕЗ преамбулы, без markdown."""


_CACHE_TTL = 3600


def _question_hash(q: str) -> str:
    return hashlib.sha256(q.strip().encode("utf-8")).hexdigest()[:16]


def _load_db_context() -> dict:
    """Тянем реальный список категорий и банков из БД.
    Это передаётся LLM как ground truth — он не выдумывает."""
    cats: list[str] = []
    banks: list[dict] = []
    try:
        with db.session() as s:
            # Категории product_offer (только с актуальными офферами)
            cats_rows = s.execute(_t("""
                SELECT DISTINCT category::text AS c
                  FROM product_offer
                 WHERE category IS NOT NULL
                 ORDER BY category::text
            """)).all()
            cats = [r.c for r in cats_rows if r.c]

            # Топ-200 банков по числу отзывов (приоритет — известным)
            bank_rows = s.execute(_t("""
                SELECT b.slug, b.name,
                       COUNT(r.review_id) AS reviews
                  FROM bank b
                  LEFT JOIN review r ON r.bank_id = b.bank_id
                 WHERE b.slug IS NOT NULL
                   AND b.slug NOT LIKE 'unknown_%'
                 GROUP BY b.slug, b.name
                 ORDER BY reviews DESC, b.name
                 LIMIT 200
            """)).all()
            for r in bank_rows:
                banks.append({"slug": r.slug, "name": r.name})
    except Exception as e:
        log.warning("query_resolver _load_db_context failed: %s", e)
    return {"categories": cats, "banks": banks}


# Маппинг slug → public domain банка. Минимальный — для тех что в БД.
# Используется чтобы передать LLM full info (slug+domain), но если банк
# в БД но домена не знаем — LLM это не критично.
_KNOWN_DOMAINS = {
    "sberbank":   "sberbank.ru",
    "vtb":        "vtb.ru",
    "alfabank":   "alfabank.ru",
    "tinkoff":    "tbank.ru",
    "sovcombank": "sovcombank.ru",
    "gazprombank": "gazprombank.ru",
    "rshb":       "rshb.ru",
    "domrf":      "domrfbank.ru",
    "otkritie":   "open.ru",
    "raiffeisen": "raiffeisen.ru",
    "pochtabank": "pochtabank.ru",
    "mkb":        "mkb.ru",
    "psb":        "psbank.ru",
    "rosbank":    "rosbank.ru",
    "uralsib":    "uralsib.ru",
    "akbars":     "akbars.ru",
    "mtsbank":    "mtsbank.ru",
    "ozonbank":   "ozon.ru",
    "yandexbank": "bank.yandex.ru",
}


async def resolve_question(client: AsyncOpenAI, question: str) -> dict:
    """Главный API. Возвращает structured-понимание вопроса.

    Кэширует на 1ч по hash(question). Идемпотентен.

    Возвращаемая структура:
      {
        "topic":            "вклад" | None,
        "topic_synonyms":   ["вклад","депозит","vklad","deposit",...],
        "url_keywords":     ["vklad","deposit","savings",...],
        "banks":            [{"slug","name","domain"},...],
        "category_hint":    "deposit" | None,   # из реальных категорий БД
        "wants_reviews":    bool,
        "wants_market_offers": bool,
        "is_product_question": bool,
        "audience_filter":  "физлица" | None,
      }

    Пустые/упавшие поля заполнены безопасными defaults — caller может
    вызывать без if-проверок.
    """
    if not question or not question.strip():
        return _empty_result()

    cache_key = _question_hash(question)
    cached = rag_cache.get("query_resolver", cache_key)
    if cached:
        return cached

    db_ctx = _load_db_context()
    cats_str  = ", ".join(db_ctx["categories"]) or "(none)"
    banks_str = ", ".join(f'{b["slug"]}({b["name"]})' for b in db_ctx["banks"][:80])

    user_msg = (
        f"# Вопрос аудитора\n{question}\n\n"
        f"# Известные категории product_offer (выбирай category_hint только из них)\n"
        f"{cats_str}\n\n"
        f"# Известные банки в БД (нормализуй упомянутые в вопросе к этим slug'ам)\n"
        f"{banks_str}\n\n"
        f"Верни JSON со всеми полями."
    )

    import asyncio as _a
    try:
        # Hard timeout 35s — reasoning-модели обычно укладываются.
        # Без него висящий LLM-вызов блокирует ВЕСЬ pipeline (вызов первый).
        resp = await _a.wait_for(
            client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[
                    {"role": "system", "content": RESOLVER_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=3500,    # +CoT-buffer + banks + product_url_paths
                # temperature 0.0 + reasoning_effort=low часто зацикливают
                # генерацию (один синоним × 50 повторов → JSON не закрывается).
                # 0.15 даёт минимальную вариативность чтобы разорвать loop.
                temperature=0.15,
            ),
            timeout=35,
        )
        raw = resp.choices[0].message.content or "{}"
    except _a.TimeoutError:
        log.warning("query_resolver: 35s timeout — fallback на пустой resolver")
        return _empty_result()
    except Exception as e:
        log.warning("query_resolver LLM call failed: %s", e)
        return _empty_result()

    # Достаём JSON. Поддерживаем 2 случая: обычный {...} и обрезанный
    # на середине (finish_reason=length от reasoning-loop'ов).
    m = re.search(r"\{[\s\S]*\}", raw)
    truncated = False
    if not m:
        # Возможно closing } обрезалось — пробуем починить
        idx = raw.find("{")
        if idx >= 0:
            # Добавляем закрывающие скобки — _loose_json_loads разберёт
            patched = raw[idx:] + '"]}'  # на случай обрыва внутри string в array
            m = re.match(r".+", patched, re.DOTALL)
            truncated = True
        if not m:
            log.warning("query_resolver: no JSON in LLM response (len=%s, first 200=%r)",
                         len(raw), raw[:200])
            return _empty_result()
    try:
        data = _loose_json_loads(m.group(0))
        if truncated:
            log.warning("query_resolver: JSON was truncated, partial recovery succeeded "
                         "(topic=%s, %s syn, %s banks)",
                         data.get("topic"),
                         len(data.get("topic_synonyms") or []),
                         len(data.get("banks") or []))
    except Exception as e:
        log.warning("query_resolver JSON parse failed: %s (raw first 200=%r)",
                     e, raw[:200])
        return _empty_result()

    result = _normalize_result(data, db_ctx)
    rag_cache.put("query_resolver", result, _CACHE_TTL, cache_key)
    log.info("[query_resolver] topic=%s, banks=%s, market=%s, reviews=%s",
             result.get("topic"),
             [b["slug"] for b in result.get("banks", [])],
             result.get("wants_market_offers"),
             result.get("wants_reviews"))
    return result


def _empty_result() -> dict:
    return {
        "topic":                None,
        "topic_synonyms":       [],
        "url_keywords":         [],
        "banks":                [],
        "category_hint":        None,
        "wants_reviews":        False,
        "wants_market_offers":  False,
        "is_product_question":  False,
        "audience_filter":      None,
        "product_url_paths":    [],
        "bank_specific_paths":  {},
        "is_socially_regulated": False,
    }


def _normalize_result(data: dict, db_ctx: dict) -> dict:
    """Валидация + нормализация полей. Защита от LLM-творчества."""
    cats_set  = set(db_ctx["categories"])
    banks_map = {b["slug"]: b["name"] for b in db_ctx["banks"]}

    def _str_or_none(v) -> str | None:
        if v is None: return None
        s = str(v).strip()
        return s if s and s.lower() not in ("null","none","") else None

    def _str_list(v) -> list[str]:
        if not isinstance(v, list): return []
        out = []
        for x in v:
            if x is None: continue
            sx = str(x).strip()
            if sx and sx.lower() not in ("null","none"):
                out.append(sx)
        # Дедуп case-insensitive
        seen, dedup = set(), []
        for s in out:
            k = s.lower()
            if k not in seen:
                seen.add(k); dedup.append(s)
        return dedup[:25]   # хватит на 15 single + 4 фразы + латиница

    topic = _str_or_none(data.get("topic"))
    # Sanity: LLM иногда возвращает мета-слово вместо продукта
    # (промпт это явно запрещает, но reasoning-модели могут срываться).
    # Если topic — пустое мета-слово, обнуляем — лучше null чем мусор.
    _META_WORDS = {
        "тариф","тарифы","условия","условие","привилегии","привилегия",
        "параметры","параметр","характеристики","характеристика",
        "процент","проценты","ставка","ставки","комиссия","комиссии",
        "пакет","пакеты","требования","требование","сравнение","сравнительная",
    }
    if topic and topic.lower().strip() in _META_WORDS:
        log.warning("query_resolver: topic='%s' — мета-слово, отбросили", topic)
        topic = None

    # Banks: оставляем только те что реально в БД (защита от выдуманных slug'ов).
    # LLM может вернуть либо ["slug1","slug2"], либо [{"slug":"slug1",...}].
    banks_raw = data.get("banks") or []
    banks_clean: list[dict] = []
    seen_slugs: set[str] = set()
    if isinstance(banks_raw, list):
        for b in banks_raw:
            if isinstance(b, str):
                slug = _str_or_none(b)
                domain_raw = None
            elif isinstance(b, dict):
                slug = _str_or_none(b.get("slug"))
                domain_raw = _str_or_none(b.get("domain"))
            else:
                continue
            if not slug or slug in seen_slugs: continue
            if slug not in banks_map:
                # LLM придумал slug которого нет в БД — игнорируем
                continue
            seen_slugs.add(slug)
            banks_clean.append({
                "slug": slug,
                "name": banks_map[slug],
                "domain": _KNOWN_DOMAINS.get(slug) or domain_raw,
            })

    cat = _str_or_none(data.get("category_hint"))
    if cat and cat not in cats_set:
        cat = None  # LLM выдумал категорию

    # product_url_paths — list[str], начинаются с `/` или http(s)://
    raw_paths = data.get("product_url_paths") or []
    paths_clean: list[str] = []
    if isinstance(raw_paths, list):
        seen_p: set[str] = set()
        for p in raw_paths:
            ps = _str_or_none(p)
            if not ps: continue
            if not (ps.startswith("/") or ps.startswith("http")):
                ps = "/" + ps.lstrip()
            if ps not in seen_p and 2 < len(ps) < 200:
                seen_p.add(ps)
                paths_clean.append(ps)
        paths_clean = paths_clean[:12]

    # bank_specific_paths — {slug: [path,...]}, фильтруем slug'и через banks_map
    raw_bsp = data.get("bank_specific_paths") or {}
    bsp_clean: dict[str, list[str]] = {}
    if isinstance(raw_bsp, dict):
        for slug, plist in raw_bsp.items():
            slug_s = _str_or_none(slug)
            if not slug_s or slug_s not in banks_map: continue
            if not isinstance(plist, list): continue
            collected: list[str] = []
            for p in plist:
                ps = _str_or_none(p)
                if ps and 2 < len(ps) < 300:
                    collected.append(ps if (ps.startswith("/") or ps.startswith("http")) else "/" + ps)
            if collected:
                bsp_clean[slug_s] = collected[:6]

    return {
        "topic":                 topic,
        "topic_synonyms":        _str_list(data.get("topic_synonyms")),
        "url_keywords":          _str_list(data.get("url_keywords")),
        "banks":                 banks_clean,
        "category_hint":         cat,
        "wants_reviews":         bool(data.get("wants_reviews")),
        "wants_market_offers":   bool(data.get("wants_market_offers")),
        "is_product_question":   bool(data.get("is_product_question")),
        "audience_filter":       _str_or_none(data.get("audience_filter")),
        "product_url_paths":     paths_clean,
        "bank_specific_paths":   bsp_clean,
        "is_socially_regulated": bool(data.get("is_socially_regulated")),
    }


# ── Helper-функции, использующие resolved result ─────────────────────────────
def matches_topic_generic(text: str, synonyms: list[str],
                            url: str | None = None,
                            url_keywords: list[str] | None = None) -> bool:
    """Generic topical-match: text/url содержит хотя бы один synonym/url_keyword.
    Работает для ЛЮБОГО topic'а — не зависит от хардкодного словаря."""
    if not synonyms and not url_keywords:
        return True   # нет topic — всё проходит
    low_text = (text or "").lower()
    low_url  = (url  or "").lower()
    syns = [s.lower() for s in synonyms or []]
    kws  = [k.lower() for k in url_keywords or []]
    # Hit в URL keyword'ах достаточно
    if low_url and any(k in low_url for k in kws):
        return True
    # 1+ синоним в тексте — достаточно (мягкий threshold)
    if any(s in low_text for s in syns):
        return True
    return False
