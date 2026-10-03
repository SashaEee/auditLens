"""URL discovery: sitemap.xml + robots.txt parsing для каждого банка.

Идея: вместо платного Search API — парсим sitemap.xml банка, индексируем
URL+last-modified, и поиск делаем по локальному индексу.

В bank_profile.key_pages храним preset известных категорий + auto-discovered.
"""
from __future__ import annotations
import logging, re
from urllib.parse import urlparse, urljoin
from typing import Iterable
import httpx

log = logging.getLogger(__name__)

# Категории, которые мы стараемся найти у каждого банка
TOPIC_PATTERNS = {
    "transfers":      [r"/transfer", r"/perevod", r"/perevody", r"/payments?"],
    "transfers_intl": [r"/swift", r"/foreign-?transfer", r"/za-rubezh", r"/zarubezh"],
    "deposits":       [r"/deposit", r"/vklad", r"/savings"],
    "credits":        [r"/credit", r"/kredit", r"/loan", r"/zaim"],
    "mortgage":       [r"/ipoteka", r"/mortgage"],
    "auto":           [r"/auto", r"/car-loan", r"/avtokredit"],
    "cards":          [r"/cards?", r"/karty", r"/kart-"],
    "cards_credit":   [r"/credit-card", r"/kreditnaya-karta"],
    "cards_debit":    [r"/debit-card", r"/debetovaya"],
    "tariffs":        [r"/tariff", r"/tarif", r"/rates?"],
    "fees":           [r"/commission", r"/komissi"],
    "support":        [r"/support", r"/help", r"/contacts?", r"/podderzhka"],
    "mobile_app":     [r"/mobile", r"/app", r"/prilozheni"],
    "business":       [r"/business", r"/sme", r"/biznes"],
    "investments":    [r"/invest", r"/broker"],
    "premium":        [r"/premium", r"/prive", r"/private"],
    "documents":      [r"/document", r"/legal"],
    "about":          [r"/about", r"/o-banke", r"/o-nas"],
    "rko":            [r"/rko", r"/raschetno"],
}

# Расширения, которые нас интересуют как документы
DOC_EXTENSIONS = (".pdf", ".xlsx", ".xls", ".pptx", ".docx")


def _fetch_text(url: str, timeout: float = 10) -> str | None:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent":
                                   "Mozilla/5.0 (compatible; BankAuditBot/0.1; +internal-audit)"}
        ) as c:
            r = c.get(url)
            if r.status_code == 200:
                return r.text
    except Exception as e:
        log.debug("fetch %s failed: %s", url, e)
    return None


def parse_sitemap(sitemap_url: str, max_urls: int = 1000) -> list[dict]:
    """Парсит sitemap.xml (и sitemap index). Возвращает list of {url, lastmod}."""
    text = _fetch_text(sitemap_url)
    if not text:
        return []
    out: list[dict] = []

    # Sitemap index? — содержит <sitemap><loc>...</loc></sitemap>
    sitemap_locs = re.findall(r"<sitemap>\s*<loc>([^<]+)</loc>", text)
    if sitemap_locs:
        for sub in sitemap_locs[:10]:  # max 10 sub-sitemaps
            out.extend(parse_sitemap(sub.strip(), max_urls=max_urls))
            if len(out) >= max_urls:
                break
        return out[:max_urls]

    # Обычный sitemap — <url><loc>...</loc><lastmod>...</lastmod></url>
    for m in re.finditer(
        r"<url>\s*<loc>([^<]+)</loc>(?:[\s\S]*?<lastmod>([^<]+)</lastmod>)?",
        text,
    ):
        out.append({
            "url": m.group(1).strip(),
            "lastmod": (m.group(2) or "").strip() or None,
        })
        if len(out) >= max_urls:
            break
    return out


def discover_sitemap_url(homepage_url: str) -> str | None:
    """Пытается найти sitemap. Сначала robots.txt, потом стандартные пути."""
    base = f"{urlparse(homepage_url).scheme}://{urlparse(homepage_url).netloc}"
    # robots.txt
    robots_text = _fetch_text(f"{base}/robots.txt", timeout=8)
    if robots_text:
        m = re.search(r"^Sitemap:\s*(\S+)", robots_text, re.MULTILINE | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    # Стандартные пути
    for candidate in ("/sitemap.xml", "/sitemap_index.xml", "/sitemaps/sitemap.xml"):
        url = f"{base}{candidate}"
        text = _fetch_text(url, timeout=6)
        if text and ("<urlset" in text or "<sitemapindex" in text):
            return url
    return None


# Темы по адресу — с границами слова по нормализованному пути (нижний регистр,
# «_» и «.» → «-»). Прежние регулярки без границ ставили «Приложение» на
# /application/, «Автокредиты» на /autopayment, «Кредиты» на кредитные карты и
# ипотеку Сбера, а ипотеку и дебетовые карты banki.ru не размечали вовсе: на
# карте покрытия появлялись ложные слепые зоны (аудит 03.10, ДАН-03).
_B, _E = r"(?<![a-z0-9])", r"(?![a-z0-9])"
_CARD, _CRED = r"(?:cards?|kart[a-z]*|bank-cards)", r"(?:credit|kredit)[a-z]*"


def _w(*alts: str) -> str:
    return _B + "(?:" + "|".join(alts) + ")" + _E


TOPIC_RULES = [
    ("mortgage", _w(r"ipotek[a-z]*", r"ipotech[a-z]*", r"mortgage[a-z]*", r"hypothec[a-z]*")
     + r"|/credits/home[a-z]*" + _E),
    ("cards_credit", _w(r"credit-?cards?", r"creditcards", r"kreditn[a-z]*-kart[a-z]*",
                        r"kreditk[a-z]*") + r"|" + _B + _CARD + r"/" + _CRED + _E
     + r"|/(?:credits?|kredit[a-z]*)/(?:cards?|kart[a-z]*)" + _E),
    ("cards_debit", _w(r"debit-?cards?", r"debitcards", r"debetov[a-z]*")
     + r"|" + _B + _CARD + r"/debit" + _E),
    # голые auto/avto — это и автоплатёж, и КАСКО: только кредитные формы и
    # раздел «авто» внутри кредитов
    ("auto", _w(r"avtokredit[a-z]*", r"autocredit[a-z]*", r"auto-credit[a-z]*", r"auto-loans?",
                r"car-loans?", r"avto-?kredit[a-z]*", r"kredit-na-avto[a-z]*")
     + r"|/(?:credits?|kredit[a-z]*|loans?)/(?:auto|avto|cars?)(?:-?(?:loans?|credit[a-z]*|kredit[a-z]*))?"
     + _E),
    ("transfers_intl", _w(r"swift", r"foreign-?transfers?", r"za-?rubezh[a-z]*",
                          r"za-?granic[a-z]*", r"abroad", r"international-transfers?")),
    ("deposits", _w(r"deposits?", r"vklad[a-z]*", r"savings?", r"contributions",
                    r"nakopitel[a-z]*")),
    ("credits", _w(r"credits?", r"kredit[a-z]*", r"loans?", r"cash-loans?", r"zaim[a-z]*",
                   r"zaem", r"refinans[a-z]*", r"refinancing", r"take-credit", r"potrebkredit[a-z]*")),
    ("cards", _w(r"cards?", r"karty", r"karta", r"kart", r"bank-cards")),
    ("tariffs", _w(r"tarif[a-z]*", r"tariffs?")),
    ("fees", _w(r"commissions?", r"komissi[a-z]*", r"fees?")),
    ("transfers", _w(r"transfers?", r"perevod[a-z]*", r"payments?", r"sbp")),
    ("support", _w(r"support", r"help", r"contacts?", r"podderzhk[a-z]*", r"faq")),
    ("mobile_app", _w(r"app", r"apps", r"mobile-app[a-z]*", r"mobile-bank[a-z]*",
                      r"prilozheni[a-z]*", r"internet-bank[a-z]*", r"inner-apps",
                      r"(?:sberbank|sber|vtb|alfa|tinkoff|t-bank|bank)-online")),
    ("business", _w(r"business", r"biznes[a-z]*", r"sme", r"msb", r"corporate")),
    ("rko", _w(r"rko", r"raschetn[a-z]*", r"current-accounts?")),
    ("investments", _w(r"invest[a-z]*", r"broker[a-z]*", r"iis")),
    ("premium", _w(r"premium", r"prive", r"private", r"private-banking")),
    ("documents", _w(r"documents?", r"legal", r"oferta", r"tarify-i-dokumenty")),
    ("about", _w(r"about", r"o-banke", r"o-nas", r"raskrytie[a-z]*", r"disclosure")),
]
# «potreb…» без «kredit» — чаще «защита прав потребителей», а не потребкредит;
# «potrebitelskij-kredit» и так ловится по «kredit»
_RULES = [(t, re.compile(p)) for t, p in TOPIC_RULES]
# специфичная тема гасит общую: ипотека, авто и кредитная карта — не «Кредиты»
_SUPPRESS = {"credits": {"mortgage", "auto", "cards_credit"},
             "cards": {"cards_credit", "cards_debit"}, "transfers": {"transfers_intl"}}


def classify_url(url: str) -> list[str]:
    """Возвращает список topic-меток, к которым подходит URL."""
    raw = (urlparse(url).path or "").lower()
    path = "/" + re.sub(r"[_.]", "-", raw).strip("/") + "/"
    tags = [t for t, rx in _RULES if rx.search(path)]
    tags = [t for t in tags if not (_SUPPRESS.get(t, set()) & set(tags))]
    if raw.endswith(DOC_EXTENSIONS):
        tags.append("document")
    return tags


def discover_key_pages(homepage_url: str,
                        max_per_topic: int = 3) -> dict[str, list[str]]:
    """Возвращает {topic_slug: [url, ...]} — top URLs per topic из sitemap."""
    sitemap_url = discover_sitemap_url(homepage_url)
    if not sitemap_url:
        log.info("discover_key_pages %s: no sitemap found", homepage_url)
        return {}

    log.info("discover_key_pages %s: sitemap=%s", homepage_url, sitemap_url)
    urls = parse_sitemap(sitemap_url, max_urls=2000)

    by_topic: dict[str, list[str]] = {}
    for u in urls:
        for topic in classify_url(u["url"]):
            arr = by_topic.setdefault(topic, [])
            if len(arr) < max_per_topic and u["url"] not in arr:
                arr.append(u["url"])
    return by_topic


# Топ-30 банков с домашними URL — заполняется руками или через
# normalisation от bank table. Часть проверена, часть нужна доразведка.
TOP_BANK_SITES: dict[str, str] = {
    "sberbank":    "https://www.sberbank.ru/",
    "vtb":         "https://www.vtb.ru/",
    "alfabank":    "https://alfabank.ru/",
    "tinkoff":     "https://www.tbank.ru/",
    "sovcombank":  "https://sovcombank.ru/",
    "gazprombank": "https://www.gazprombank.ru/",
    "rshb":        "https://www.rshb.ru/",
    "otkritie":    "https://www.open.ru/",
    "raiffeisen":  "https://www.raiffeisen.ru/",
    "pochtabank":  "https://www.pochtabank.ru/",
    "mkb":         "https://mkb.ru/",
    "akbars":      "https://www.akbars.ru/",
    "mtsbank":     "https://www.mtsbank.ru/",
    "yandexbank":  "https://bank.yandex.ru/",
    "ozonbank":    "https://finance.ozon.ru/",
    "psb":         "https://www.psbank.ru/",
    "lokobank":    "https://www.lockobank.ru/",
    "homecredit":  "https://www.homecredit.ru/",
    "unicredit":   "https://www.unicreditbank.ru/",
    "uralsib":     "https://www.uralsib.ru/",
    "rosbank":     "https://www.rosbank.ru/",
    "bspb":        "https://www.bspb.ru/",
    # сайт банка, а не института развития (дом.рф): у института капча и
    # нет тарифов банка (аудит 03.10, ДАН-03)
    "domrf":       "https://domrfbank.ru/",
    "sinara":      "https://sinarabank.ru/",
    "rencredit":   "https://rencredit.ru/",
    "rsb":         "https://www.rsb.ru/",
    "norvikbank":  "https://www.norvikbank.ru/",
}


def bootstrap_bank_profile(bank_slug: str) -> dict:
    """Bootstrap профиля одного банка: ищем sitemap, key_pages, robots.
    Возвращает dict для записи в bank_profile.
    """
    homepage = TOP_BANK_SITES.get(bank_slug)
    if not homepage:
        return {"error": f"no homepage mapping for {bank_slug}"}

    sitemap_url = discover_sitemap_url(homepage)
    key_pages = discover_key_pages(homepage) if sitemap_url else {}

    return {
        "official_url": homepage,
        "sitemap_url":  sitemap_url,
        "robots_url":   f"{urlparse(homepage).scheme}://{urlparse(homepage).netloc}/robots.txt",
        "key_pages":    key_pages,
        "topics_found": list(key_pages.keys()),
        "n_topics":     len(key_pages),
    }
