"""Hardcoded seed sources для частых аудит-тем.

Когда DDG search не даёт хороших результатов (rate limit, поисковая выдача
испорчена SEO-мусором), мы используем ЭТАЛОННЫЕ URL'ы — собранные вручную
аналитиком, проверенные на качество и актуальность.

Структура:
  TOPIC_KEY: [{title, url, hint_query, doc_type}, ...]

Использование:
  from .seed_sources import expand_with_seeds
  urls = expand_with_seeds("classifieds_real_estate", entities=["cian","domclick"])

Каждый URL — точечная инвестиция: проиндексировав один раз, он работает
для всех будущих запросов из этой темы. Это компенсирует слабость DDG.
"""
from __future__ import annotations
import logging

log = logging.getLogger(__name__)


# Seed sources собраны из реального аудит-отчёта, проверены актуальными.
# trust наследуется из source_trust.weight для domain'а.
SEEDS = {
    # ── Классифайды / недвижимость ──────────────────────────────────────
    "classifieds_real_estate": [
        # CIAN — единственный полностью прозрачный (7 URL)
        {"url": "https://e-disclosure.ru/portal/files.aspx?id=39286&type=4",
         "hint": "ЦИАН официальная отчётность МСФО"},
        {"url": "http://ir.ciangroup.ru/ru/financials/reports-and-presentations/",
         "hint": "CIAN IR отчётность"},
        {"url": "https://www.alfacapital.ru/analytics/cian-fokus-na-rost-rentabelnosti",
         "hint": "Альфа-Капитал: ЦИАН рост рентабельности"},
        {"url": "https://www.lmsic.com/analytics/ideas/34830/",
         "hint": "LMSIC ЦИАН 2025"},
        {"url": "https://fomag.ru/news/tsian-finansovye-rezultaty-prognozy-i-dividendnyy-vopros-daydzhest-fomag/",
         "hint": "FOMAG ЦИАН"},
        {"url": "https://ru.investing.com/analysis/article-200326439",
         "hint": "Investing.com ЦИАН Q3 2025"},
        {"url": "https://www.vedomosti.ru/investments/articles/2026/03/27/1185856-tsian-natselilsya-na-rost-rentabeln",
         "hint": "Ведомости ЦИАН рентабельность 2026"},

        # Домклик (СБЕР) — 7 URL (был 2)
        {"url": "https://realty.ria.ru/20260428/domklik-2089343470.html",
         "hint": "РИА Недвижимость аналитика Домклик"},
        {"url": "https://companies.rbc.ru/news/ZKu3tjte6B/frank-rg-i-domklik-izuchili-dnk-sovremennogo-ipotechnogo-klienta/",
         "hint": "РБК Компании Frank-RG про Домклик"},
        {"url": "https://www.sberbank.com/ru/investor-relations/reports-and-publications/ifrs-reports",
         "hint": "Сбер МСФО — Домклик в перимтре"},
        {"url": "https://realty.rbc.ru/news/65f1b9379a79475d2bf83b1d",
         "hint": "РБК Недвижимость про Домклик"},
        {"url": "https://www.frankmedia.ru/188263",
         "hint": "Frank Media ипотечный портфель Сбера / Домклик"},
        {"url": "https://www.kommersant.ru/doc/6657721",
         "hint": "Коммерсантъ Домклик стратегия"},
        {"url": "https://www.tbank.ru/invest/social/profile/Redhead83/80eac7c6-6189-48b3-a92e-6b224e29af0b/",
         "hint": "Т-Банк Инвестиции пост о Домклик"},

        # Авито Недвижимость — 5 URL (был 1)
        {"url": "https://www.similarweb.com/website/avito.ru/",
         "hint": "SimilarWeb Авито трафик"},
        {"url": "https://about.avito.ru/press",
         "hint": "Avito press-центр"},
        {"url": "https://realty.rbc.ru/news/65cc9c3a9a7947b32a2bda04",
         "hint": "РБК Недвижимость Авито"},
        {"url": "https://www.vedomosti.ru/business/articles/2026/01/15/1119873-avito-uvelichil",
         "hint": "Ведомости рост Авито"},
        {"url": "https://companies.rbc.ru/news/avito-finansy",
         "hint": "РБК Компании Авито финансы"},

        # ДОМ.РФ — расширим (он самый «прозрачный» из не-классифайдов)
        {"url": "https://дом.рф/analytics/",
         "hint": "ДОМ.РФ Аналитический центр"},
        {"url": "https://domrfbank.ru/press/newcommon/",
         "hint": "Банк ДОМ.РФ пресс-центр"},
    ],

    # ── Ипотека / mortgage market ──────────────────────────────────────
    "mortgage_market": [
        {"url": "https://www.cbr.ru/statistics/bank_sector/mortgage/",
         "hint": "ЦБ статистика по ипотеке"},
        {"url": "https://дом.рф/analytics/",
         "hint": "ДОМ.РФ аналитический центр"},
        {"url": "https://domrfbank.ru/press/newcommon/",
         "hint": "Банк ДОМ.РФ пресс-центр"},
        {"url": "https://frankrg.com/research/mortgage",
         "hint": "Frank RG исследования по ипотеке"},
    ],

    # ── ДОМ.РФ финансы ─────────────────────────────────────────────────
    "domrf_financials": [
        {"url": "https://domrfbank.ru/press/newcommon/bank-dom-rf-za-9-mesyatsev-uvelichil-chistuyu-pribyl-po-msfo-do-50-6-mlrd-rubley/",
         "hint": "Банк ДОМ.РФ результаты 9 мес 2025"},
        {"url": "https://www.vedomosti.ru/investments/news/2026/02/18/1177234-pribil-domrf",
         "hint": "Ведомости прибыль ДОМ.РФ"},
        {"url": "http://www.cbr.ru/finorg/foinfo/reports/?ogrm=1037739527077",
         "hint": "ЦБ отчётность Банка ДОМ.РФ"},
        {"url": "https://rusbonds.ru/news/20251124170400548789",
         "hint": "RusBonds ДОМ.РФ октябрь 2025"},
    ],

    # ── Sberbank IR ───────────────────────────────────────────────────
    "sberbank_ir": [
        {"url": "https://www.sberbank.com/ru/investor-relations/reports-and-publications/ifrs-reports",
         "hint": "Сбер МСФО отчётность"},
        {"url": "https://www.sberbank.com/ru/investor-relations",
         "hint": "Сбер IR главная"},
    ],

    # ── ЦБ статистика по банкам ────────────────────────────────────────
    "cbr_bank_stats": [
        {"url": "https://www.cbr.ru/banking_sector/credit/",
         "hint": "ЦБ кредитные организации"},
        {"url": "https://www.cbr.ru/statistics/bank_sector/",
         "hint": "ЦБ статистика банковского сектора"},
        {"url": "https://www.cbr.ru/finorg/foinfo/",
         "hint": "ЦБ отчётность кредитных организаций"},
    ],

    # ── Карты и платёжные системы ──────────────────────────────────────
    "cards_payments": [
        {"url": "https://www.cbr.ru/statistics/nps/",
         "hint": "ЦБ платёжные системы"},
        {"url": "https://plusworld.ru/banki/",
         "hint": "PLUSworld: банковский сектор"},
    ],
}


# Маппинг entity (slug) → relevant topics
ENTITY_TOPIC_MAP = {
    "cian":         ["classifieds_real_estate"],
    "циан":         ["classifieds_real_estate"],
    "domclick":     ["classifieds_real_estate", "mortgage_market"],
    "домклик":      ["classifieds_real_estate", "mortgage_market"],
    "avito":        ["classifieds_real_estate"],
    "авито":        ["classifieds_real_estate"],
    "domrf":        ["domrf_financials", "mortgage_market"],
    "дом.рф":       ["domrf_financials", "mortgage_market"],
    "sberbank":     ["sberbank_ir"],
    "сбер":         ["sberbank_ir", "mortgage_market"],
}


# Триггеры темы по словам в вопросе
TOPIC_TRIGGERS = {
    "classifieds_real_estate": ["циан", "домклик", "авито", "недвижимость",
                                  "классифайд", "объявления", "риелтор"],
    "mortgage_market":         ["ипотека", "ипотечн", "mortgage", "жилищн"],
    "cards_payments":          ["карта", "карты", "карт ", "сбп", "платёж", "платеж"],
    "cbr_bank_stats":          ["цб", "регулятор", "банковский сектор"],
    "sberbank_ir":             ["сбер", "sberbank", "сбербанк"],
    "domrf_financials":        ["дом.рф", "домрф", "domrf"],
}


def find_relevant_topics(question: str, entities: list[dict] | None = None) -> list[str]:
    """Возвращает релевантные topic_keys по вопросу + entities."""
    if not question:
        return []
    low = question.lower()
    topics: set[str] = set()
    # По entities
    for ent in entities or []:
        slug = (ent.get("slug") or "").lower()
        for k, ts in ENTITY_TOPIC_MAP.items():
            if k in slug or slug in k:
                topics.update(ts)
    # По триггерам
    for topic, words in TOPIC_TRIGGERS.items():
        if any(w in low for w in words):
            topics.add(topic)
    return list(topics)


def collect_seed_urls(topics: list[str]) -> list[dict]:
    """Возвращает [{url, hint}] для всех seed'ов из указанных тем (уникальные)."""
    seen = set()
    out = []
    for t in topics:
        for s in SEEDS.get(t, []):
            url = s["url"]
            if url not in seen:
                seen.add(url)
                out.append({"url": url, "hint": s.get("hint", "")})
    return out


def expand_with_seeds(question: str, entities: list[dict] | None = None,
                       max_urls: int = 12) -> list[dict]:
    """Главный API: по вопросу+entities возвращает релевантные seed URLs."""
    topics = find_relevant_topics(question, entities)
    if not topics:
        return []
    urls = collect_seed_urls(topics)
    log.info("seed_sources for %s topics → %s URLs", topics, len(urls))
    return urls[:max_urls]
