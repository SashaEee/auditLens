"""Каталог источников для вкладки «Источники» — витрина для аудитора.

Раньше вкладка показывала прогоны сборщиков и капчи — это работа инженера.
Аудитору нужно понимать, ОТКУДА взялась каждая цифра и насколько источнику
доверяет инструмент, а также иметь канал предложить свой источник.

Контуры (purpose) намеренно разведены: у каждого своя роль в продукте и свои
требования к источнику.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from .. import db

# ── Контуры и требования ─────────────────────────────────────────────────────
# Требования РАЗНЫЕ по контурам: новостной ленте нужен фид и частота, корпусу
# отзывов — массовость и привязка к банку, витрине тарифов — структурированные
# условия. Формулировки — для аудитора, без инженерного жаргона.

PURPOSES: list[dict[str, Any]] = [
    {
        "id": "ai",
        "title": "ИИ-аналитик",
        "lead": "Откуда ИИ берёт факты для отчётов и с каким весом их учитывает.",
        "what_for": "Глубокие отчёты: поиск по вебу, извлечение фактов, сверка чисел "
                    "и ссылки в тексте отчёта.",
        "trust_note": "У каждого домена есть вес доверия 0–1. Всё, что ниже 0.5, "
                      "в отчёт не попадает вовсе. Вес влияет на то, чьё утверждение "
                      "победит при противоречии источников.",
        "requirements": [
            "Публично доступен без регистрации и оплаты",
            "Первоисточник, а не пересказ чужой публикации",
            "Виден автор или организация-издатель",
            "У материалов есть дата публикации",
            "Текст читается без запуска скриптов (не только в приложении)",
            "Не рекламная площадка и не партнёрские подборки",
            "Русский язык или официальный перевод",
        ],
        "examples": "cbr.ru, raexpert.ru, сайт банка, arbitr.ru",
    },
    {
        "id": "digest",
        "title": "Главная страница",
        "lead": "Что попадает в утренний брифинг: новости, регуляторика, инциденты.",
        "what_for": "Лента «Новости для аудитора» и карточки-сигналы на главной.",
        "trust_note": "Источники размечены по роли: регулятор, рынок, инциденты, "
                      "схемы мошенничества. ИИ отбирает из ленты только то, что "
                      "касается розницы, и не выдумывает фактов сверх текста.",
        "requirements": [
            "Есть RSS-лента или публичный веб-просмотр телеграм-канала",
            "Публикует регулярно — хотя бы несколько материалов в неделю",
            "Тематика: банки, регулятор, инциденты, мошенничество, розничные продукты",
            "У каждой публикации есть дата",
            "Открыт без подписки и пейволла",
            "Не пресс-релизная рассылка одного банка",
        ],
        "examples": "cbr.ru/rss, frankmedia.ru/feed, t.me/s/канал",
    },
    {
        "id": "reviews",
        "title": "Отзывы клиентов",
        "lead": "Корпус жалоб, на котором строятся сигналы и темы.",
        "what_for": "Пульс жалоб, темы, эскалации в ЦБ и суд, гео-концентрация.",
        "trust_note": "Каждый отзыв хранится с датой, оценкой и текстом — иначе "
                      "нельзя посчитать норму и сравнить с рынком.",
        "requirements": [
            "Отзывы привязаны к конкретному банку",
            "У отзыва есть дата и оценка (звёзды или аналог)",
            "Объём: хотя бы сотни отзывов по крупным банкам",
            "Открытый доступ без авторизации",
            "Отзывы пишут клиенты, а не редакция площадки",
            "Есть ссылка на каждый отзыв — она нужна как доказательство",
        ],
        "examples": "banki.ru, bankiros.ru, отзовики",
    },
    {
        "id": "tariffs",
        "title": "Тарифы и условия",
        "lead": "Витрины, из которых собираются ставки, сроки и комиссии.",
        "what_for": "Вкладка «Рынок · позиция», журнал изменений условий, "
                    "сравнение Сбера с рынком.",
        "trust_note": "Условия хранятся с историей: каждое изменение фиксируется "
                      "с датой, поэтому нужны стабильные идентификаторы продуктов.",
        "requirements": [
            "Условия структурированы: ставка, сумма, срок, комиссии",
            "Явно указан банк и название продукта",
            "Витрина обновляется — хотя бы раз в неделю",
            "Открытый доступ без личного кабинета",
            "У продукта есть постоянная ссылка",
            "Указано, промо это или базовые условия",
        ],
        "examples": "sravni.ru, banki.ru, витрина банка",
    },
]

PURPOSE_IDS = {p["id"] for p in PURPOSES}

# Человеческие названия видов доверия
KIND_RU = {
    "regulator": "Регулятор", "bank_official": "Официальный сайт банка",
    "aggregator": "Агрегатор", "analyst": "Аналитика и рейтинги",
    "media": "СМИ", "forum": "Форум отзывов", "blog": "Блог",
    "sponsored": "Рекламный", "gov": "Госорган",
}

_TRUST_BANDS = [
    (0.90, "первоисточник", "Считаем эталоном: официальные публикации регулятора, "
                            "госорганов и рейтинговых агентств"),
    (0.70, "проверенный", "Устоявшаяся площадка с редакционным контролем"),
    (0.50, "с оговоркой", "Берём, но при противоречии уступает более надёжным"),
    (0.00, "не используется", "Ниже порога — в отчёты не попадает"),
]


def trust_band(w: float) -> dict[str, str]:
    for edge, label, note in _TRUST_BANDS:
        if w >= edge:
            return {"label": label, "note": note}
    return {"label": "не используется", "note": ""}


def normalize_domain(url: str) -> str:
    """Домен без www и протокола; для телеграм-канала — t.me/канал."""
    u = (url or "").strip()
    if not u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    host = (p.netloc or "").lower().removeprefix("www.")
    if host in ("t.me", "telegram.me"):
        seg = [s for s in (p.path or "").split("/") if s and s != "s"]
        if seg:
            return f"t.me/{seg[0].lower()}"
    return host


# Человеческие названия лент: ключи в коде («tg_banksta») аудитору ничего не говорят
_NEWS_RU = {
    "cbr_press": "Банк России — пресс-релизы",
    "cbr_news": "Банк России — новости",
    "banki_news": "Banki.ru — новости",
    "frankmedia": "Frank Media",
    "tg_cbr": "Банк России — телеграм",
    "tg_banksta": "Банкста",
    "tg_cyberpolice": "Киберполиция",
    "tg_frankmedia": "Frank Media — телеграм",
    "tg_kommersant": "Коммерсантъ",
    "tg_rbc": "РБК",
}


def _news_sources() -> list[dict]:
    from ..digest.news import SOURCES
    tag_ru = {"regulator": "Регулятор", "market": "Рынок и деловая пресса",
              "incident": "Инциденты", "scheme": "Схемы мошенничества"}
    out = []
    for s in SOURCES:
        dom = normalize_domain(s["url"])
        out.append({
            # для телеграма домен == канал, для RSS показываем сайт + имя ленты
            "domain": _NEWS_RU.get(s["key"], s["key"]),
            "url": s["url"],
            "title": dom,
            "role": tag_ru.get(s.get("tag"), s.get("tag") or ""),
            "kind": "RSS-лента" if s.get("kind") == "rss" else "Телеграм-канал",
        })
    return out


def _tariff_sources() -> list[dict]:
    """Витрины тарифов из конфига сборщиков + фактическое покрытие из БД."""
    try:
        from ..config import load_sources
        cfg = load_sources()
    except Exception:  # noqa: BLE001
        cfg = {}
    rows = []
    with db.session() as s:
        raw = s.execute(text("""
            SELECT coalesce(substring(url from '^https?://([^/]+)/'), 'без ссылки') AS dom,
                   count(*), count(DISTINCT bank_id)
              FROM product_offer WHERE is_active GROUP BY 1 ORDER BY 2 DESC
        """)).all()
    # нормализуем домены (в url бывает www.) — иначе дедуп «уже используется»
    # не срабатывает: known_domains вернул бы www.sravni.ru против sravni.ru
    cov: dict[str, tuple[int, int]] = {}
    for dom_raw, n_off, n_banks in raw:
        d = normalize_domain(dom_raw) or dom_raw
        prev = cov.get(d, (0, 0))
        cov[d] = (prev[0] + int(n_off), max(prev[1], int(n_banks)))
    # реестр ЦБ и сборщики отзывов сюда не относятся — они не отдают условия
    NOT_TARIFF = {"cbr_registry", "banki_reviews", "sravni_reviews",
                  "bankiros_reviews", "banki_ratings"}
    seen = set()
    for name, conf in (cfg or {}).items():
        if name in NOT_TARIFF:
            continue
        targets = conf.get("targets") or []
        dom = normalize_domain((targets[0] or {}).get("url", "") if targets else "")
        if not dom:
            base = conf.get("base_url") or ""
            dom = normalize_domain(base)
        if not dom or dom in seen:
            continue
        seen.add(dom)
        n_off, n_banks = cov.get(dom, (0, 0))
        rows.append({"domain": dom, "url": f"https://{dom}/", "title": name,
                     "role": "Витрина условий", "kind": "Сбор по расписанию",
                     "coverage": f"{n_off} офферов · {n_banks} банков" if n_off else None})
    for dom, (n_off, n_banks) in cov.items():
        if dom in seen or dom == "без ссылки" or n_off < 20 or dom.endswith("cbr.ru"):
            continue
        seen.add(dom)
        rows.append({"domain": dom, "url": f"https://{dom}/", "title": dom,
                     "role": "Витрина условий", "kind": "Сбор по расписанию",
                     "coverage": f"{n_off} офферов · {n_banks} банков"})
    return rows


def _review_sources() -> list[dict]:
    out = []
    try:
        from ..rag import reviews_dash as rd
        eng = rd._get_engine()
        if eng is not None:
            with eng.connect() as c:
                n, banks, since = c.execute(text(
                    'SELECT count(*), count(DISTINCT "bankName"), min("datePublished")::date'
                    " FROM bankiru.reviews")).one()
            out.append({
                "domain": "banki.ru", "url": "https://www.banki.ru/services/responses/",
                "title": "banki.ru — народный рейтинг",
                "role": "Корпус отзывов", "kind": "Ежедневный сбор",
                "coverage": f"{n} отзывов · {banks} банков · с {since}",
            })
    except Exception:  # noqa: BLE001
        pass
    with db.session() as s:
        n = s.execute(text("SELECT count(*) FROM review")).scalar() or 0
    if n:
        out.append({"domain": "bankiros.ru", "url": "https://bankiros.ru/",
                    "title": "bankiros.ru", "role": "Дополнительный корпус",
                    "kind": "Сбор по расписанию", "coverage": f"{n} отзывов"})
    return out


def _ai_sources(limit: int = 60) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text("""
            SELECT kind, domain, weight, notes FROM source_trust
             WHERE domain IS NOT NULL
             ORDER BY weight DESC, domain
             LIMIT :l
        """), {"l": limit}).mappings().all()
    out = []
    for r in rows:
        w = float(r["weight"])
        out.append({
            "domain": r["domain"], "url": f"https://{r['domain']}/",
            "title": r["notes"] or r["domain"],
            "role": KIND_RU.get(r["kind"], r["kind"]),
            "kind": "Веб-источник",
            "weight": round(w, 2), "band": trust_band(w)["label"],
        })
    return out


def catalog() -> list[dict]:
    """Полный каталог: по контуру — список источников с ролью и покрытием."""
    builders = {"ai": _ai_sources, "digest": _news_sources,
                "reviews": _review_sources, "tariffs": _tariff_sources}
    out = []
    for p in PURPOSES:
        try:
            items = builders[p["id"]]()
        except Exception:  # noqa: BLE001 — витрина не должна падать целиком
            items = []
        out.append({**p, "sources": items, "n": len(items)})
    return out


def known_domains() -> dict[str, list[str]]:
    """Домены, уже используемые в каждом контуре — для проверки дублей."""
    res = {}
    for p in catalog():
        res[p["id"]] = sorted({s["domain"] for s in p["sources"] if s.get("domain")})
    return res
