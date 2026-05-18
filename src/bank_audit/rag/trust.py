"""Trust scoring: выставляет weight источнику и детектит sponsored content.

Принцип:
  • Каждый chunk при ingest получает trust_score (наследуется из source_trust + adj)
  • Adjustments:
      - sponsored URL pattern → 0.0 (исключаем из retrieval)
      - sponsored content markers (lexical) → -0.3
      - контент пуст / robot challenge → 0.0
  • Аудитор всегда видит источник (URL + trust) в Audit Studio

Trust порог по умолчанию 0.5 — RAG пропускает только trust >= 0.5.
Аудитор может временно ослабить (через UI слайдер).
"""
from __future__ import annotations
import re
from urllib.parse import urlparse


# Урл-паттерны заказного/рекламного контента (исключаем полностью)
_SPONSORED_PATH_RE = re.compile(
    r"/(promo|sponsored|spec|advertorial|partners?|advert|ad/|reklam|brand|partn-)/",
    re.IGNORECASE,
)

# Lexical-маркеры заказного контента (понижают score, не обнуляют)
_SPONSORED_LEXICAL = (
    "на правах рекламы",
    "партнёрский материал",
    "партнерский материал",
    "спонсорский материал",
    "реклама. erid",
    "erid:",
)

# Маркеры robot/captcha challenge — невалидный документ
_CAPTCHA_LEXICAL = (
    "вы не робот",
    "smartcaptcha",
    "checking your browser",
    "пройдите проверку",
    # Sberbank WAF block-page — показывается когда detect Playwright/headless,
    # текст misleadingly предлагает «установить сертификат Минцифры», но на
    # самом деле просто bot-detection. Контента нет, индексировать не нужно.
    "не установлены сертификаты национального уц минцифры",
    "please enable javascript to view the page content",
    # Yandex captcha
    "showcaptcha",
)


def domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return ""


def detect_sponsored(url: str, text: str | None = None) -> tuple[bool, str | None]:
    """(is_sponsored, reason). True → исключить из RAG."""
    if _SPONSORED_PATH_RE.search(url or ""):
        return True, "url_pattern"
    if text:
        low = text[:4000].lower()
        for marker in _SPONSORED_LEXICAL:
            if marker in low:
                return True, f"lexical:{marker[:30]}"
    return False, None


def detect_invalid_content(text: str | None) -> tuple[bool, str | None]:
    """Есть ли маркеры что страница невалидна (капча/блок)."""
    if not text or len(text.strip()) < 50:
        return True, "too_short"
    low = text[:4000].lower()
    for marker in _CAPTCHA_LEXICAL:
        if marker in low:
            return True, f"captcha:{marker}"
    return False, None


def compute_trust(base_weight: float, url: str, text: str | None) -> float:
    """Финальный trust_score для документа.
    base_weight — из source_trust.weight
    Adjustments:
      • sponsored → 0.0
      • невалидный контент → 0.0
      • домен НЕ в whitelist → max 0.10 (визуально отображается, но в RAG аудита не попадает)
    """
    invalid, _ = detect_invalid_content(text)
    if invalid:
        return 0.0
    sponsored, _ = detect_sponsored(url, text)
    if sponsored:
        return 0.0
    # Если базовый weight < 0.20 — это unknown_blog (auto-added), ограничиваем сверху
    if base_weight < 0.20:
        return min(0.10, base_weight)
    return min(1.0, max(0.0, base_weight))


# Известные бренд-домены (используем для авто-приклеивания bank_official trust)
# Расширяется через source_trust таблицу.
KNOWN_BANK_DOMAINS = {
    "sberbank.ru":       "sberbank",
    "alfabank.ru":       "alfabank",
    "vtb.ru":            "vtb",
    "tinkoff.ru":        "tinkoff",
    "tbank.ru":          "tinkoff",
    "sovcombank.ru":     "sovcombank",
    "rshb.ru":           "rshb",
    "gazprombank.ru":    "gazprombank",
    "open.ru":           "otkritie",
    "raiffeisen.ru":     "raiffeisen",
    "pochtabank.ru":     "pochtabank",
    "mkb.ru":            "mkb",
    "akbars.ru":         "akbars",
    "mtsbank.ru":        "mtsbank",
    "bank.yandex.ru":    "yandexbank",
    "ozon.ru":           "ozonbank",
    "psbank.ru":         "psb",
    "lockobank.ru":      "lokobank",
    "homecredit.ru":     "homecredit",
    "unicreditbank.ru":  "unicredit",
    "uralsib.ru":        "uralsib",
    "rosbank.ru":        "rosbank",
    "bspb.ru":           "bspb",
    "domrf.ru":          "domrf",
    "dombank.ru":        "domrf",
    "sinarabank.ru":     "sinara",
    "rencredit.ru":      "rencredit",
    "rsb.ru":            "rsb",
    "norvikbank.ru":     "norvikbank",
}


def is_bank_official(url: str) -> tuple[bool, str | None]:
    """Возвращает (True, slug) если URL — официальный сайт банка."""
    d = domain_of(url)
    if d in KNOWN_BANK_DOMAINS:
        return True, KNOWN_BANK_DOMAINS[d]
    # Поддоменные совпадения (online.sberbank.ru, lk.alfabank.ru, ...)
    for known, slug in KNOWN_BANK_DOMAINS.items():
        if d.endswith("." + known):
            return True, slug
    return False, None


# ── Govt / regulatory whitelist ──────────────────────────────────────────
# Эти домены — самый достоверный класс источников для аудита банковской темы.
# Особенно важны для социальных продуктов (карта ветерана, военная ипотека,
# материнский капитал) — там законодательная и нормативная база — первоисточник.
# Trust выше bank_official потому что регулятор > банк сам по себе.
GOVT_TRUST_DOMAINS: dict[str, tuple[str, float, str]] = {
    # (kind, weight, notes)
    "cbr.ru":               ("regulator", 0.98, "Банк России"),
    "pravo.gov.ru":         ("regulator", 0.97, "Официальное опубликование НПА"),
    "publication.pravo.gov.ru": ("regulator", 0.97, "Публикация НПА"),
    "government.ru":        ("regulator", 0.95, "Правительство РФ"),
    "kremlin.ru":           ("regulator", 0.95, "Президент РФ"),
    "duma.gov.ru":          ("regulator", 0.93, "Государственная Дума"),
    "council.gov.ru":       ("regulator", 0.93, "Совет Федерации"),
    "minfin.gov.ru":        ("regulator", 0.93, "Минфин РФ"),
    "minfin.ru":            ("regulator", 0.93, "Минфин РФ"),
    "mil.ru":               ("regulator", 0.92, "Минобороны РФ"),
    "gosuslugi.ru":         ("government", 0.90, "Госуслуги"),
    "rosreestr.gov.ru":     ("government", 0.90, "Росреестр"),
    "rosreestr.ru":         ("government", 0.90, "Росреестр"),
    "fns.gov.ru":           ("government", 0.90, "ФНС"),
    "nalog.gov.ru":         ("government", 0.90, "ФНС"),
    "nalog.ru":             ("government", 0.90, "ФНС"),
    "rospotrebnadzor.ru":   ("government", 0.88, "Роспотребнадзор"),
    "asv.org.ru":           ("regulator", 0.92, "АСВ"),
    "moex.com":             ("regulator", 0.90, "Московская биржа"),
    # Юридические БД — third-party, но de-facto authoritative
    "consultant.ru":        ("legal_db", 0.85, "КонсультантПлюс"),
    "garant.ru":            ("legal_db", 0.85, "Гарант"),
    "kodeks.ru":            ("legal_db", 0.82, "Кодекс"),
    # Региональные госструктуры (примеры — расширяем по необходимости)
    "mos.ru":               ("government", 0.85, "Правительство Москвы"),
    "spb.ru":               ("government", 0.83, "Правительство СПб"),
}


def is_govt_official(url: str) -> tuple[bool, str, float, str]:
    """Возвращает (True, kind, weight, notes) если URL — gov/regulator/legal_db.

    Поддерживает поддомены (statistics.cbr.ru → cbr.ru).
    """
    d = domain_of(url)
    if not d:
        return False, "", 0.0, ""
    if d in GOVT_TRUST_DOMAINS:
        kind, w, n = GOVT_TRUST_DOMAINS[d]
        return True, kind, w, n
    for known, (kind, w, n) in GOVT_TRUST_DOMAINS.items():
        if d.endswith("." + known):
            return True, kind, w, n
    return False, "", 0.0, ""
