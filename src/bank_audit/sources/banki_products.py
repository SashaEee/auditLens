"""Каталог продуктов banki.ru — широкое ежедневное покрытие тарифов.

ЗАЧЕМ. Сравни.ру не показывает продукты Сбера: на 11.08.2026 у Сбера было
0 активных ипотек, 1 дебетовая и 1 кредитная карта — витрина «Позиция» просто
не могла сравнить банк с рынком. banki.ru отдаёт и общий каталог, и раздел
КАЖДОГО банка (/products/<категория>/<слаг>/), где ипотека Сбера — 10 программ
со ставками. Проверено с прод-IP: без капчи, ежедневно, все пять категорий.

ЧТО СОБИРАЕМ (разведка 10.08.2026):
  ипотека     /products/hypothec/     3 стр × 15    45 банков в каталоге
  потребкред. /products/credits/      4 стр × 20    79
  кредитки    /products/creditcards/  4 стр × 10    34
  дебет.карты /products/debitcards/   11 стр × 12   122
  вклады      /products/deposits/     каталог БЕЗ пагинации (10 промо) —
                                      объём берётся только по-банковыми страницами
Каталог показывает по одному «головному» офферу на банк, остальное свёрнуто в
JS-блок «Ещё N предложений», которого нет в HTML, — поэтому по-банковые
страницы обязательны, а не факультативны.

ПОЧЕМУ НЕ ХРАНИМ СЫРОЙ HTML. Полный обход — сотни страниц по ~1 МБ, это
гигабайты в сутки; диск VM 11.08.2026 уже был забит под 100 проц. Поэтому
fetch() разбирает страницы на лету и кладёт в снимок компактный JSON с
извлечёнными предложениями, а parse_offers() только переводит его в OfferDraft.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, RawSnapshot

log = logging.getLogger(__name__)

BASE = "https://www.banki.ru/products"
_PAUSE_S = float(__import__("os").getenv("BANKI_PRODUCTS_PAUSE_S", "1.2"))
_MAX_PAGES_HARD = 20

# раздел banki.ru → наша категория + сколько страниц каталога ожидаем
CATEGORIES: dict[str, dict] = {
    "hypothec":    {"category": "mortgage",    "pages": 3,  "engine": "labeled"},
    "credits":     {"category": "credit",      "pages": 4,  "engine": "labeled"},
    "creditcards": {"category": "card_credit", "pages": 4,  "engine": "labeled"},
    "debitcards":  {"category": "card_debit",  "pages": 11, "engine": "labeled"},
    # каталог вкладов — витрина из 10 промо без пагинации: page=40 отдаёт те же
    # карточки байт-в-байт. Объём даёт только обход банков.
    "deposits":    {"category": "deposit",     "pages": 1,  "engine": "deposits"},
}

# Банки, чьи разделы обходим всегда. Сбер — обязателен: ради него всё и затеяно.
BANK_SLUGS: tuple[str, ...] = (
    "sberbank", "vtb", "tcs", "alfabank", "gazprombank", "sovcombank",
    "promsvyazbank", "rshb", "rencredit", "mts-bank", "domrfbank", "uralsib",
    "akbars", "raiffeisen", "russtandard", "ozon", "home", "pochtabank",
    "mkb", "otpbank", "bspb", "ubrr", "yandex", "absolutbank",
)
REQUIRED_SLUG = "sberbank"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ── разбор разметки ──────────────────────────────────────────────────────────
# Подписи и значения — одинаковые <div data-test="text">, подпись отличается
# только инлайновым цветом. Держим ДВА признака подписи (цвет и словарь
# известных слов): цвет ломается при смене темы сайта, словарь — при появлении
# новой подписи.
_RE_CELL = re.compile(r'<div\b([^>]*?)data-test="text"[^>]*>(.*?)</div>', re.S)
_LABEL_COLOR = "color:#4e6174"
_KNOWN_LABELS = {"Ставка", "ПСК", "Сумма", "Срок", "Платёж", "Первоначальный взнос",
                 "Обслуживание", "Льготный период", "Кэшбэк", "Баллы", "Выпуск карты",
                 "Снятие наличных", "Доход", "Пополнение"}
# В подписях встречаются латинские буквы-двойники: «ПСK», «Cумма», «Cложно
# выбрать?» — без нормализации сравнение строк молча промахивается.
_HOMOGLYPH = str.maketrans("CcKkPpHhAaEeOoTtBMXy", "СсКкРрНнАаЕеОоТтВМХу")
_RE_TAGS = re.compile(r"<[^>]+>")


def _clean(s: str) -> str:
    s = _html.unescape(_RE_TAGS.sub("", s or ""))
    for ch in (" ", " ", " "):
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip()


def _canon(s: str) -> str:
    return _clean(s).translate(_HOMOGLYPH)


def _labels(card: str) -> dict[str, str]:
    """Карта «подпись → значение» внутри карточки."""
    cells = [(_LABEL_COLOR in attrs, _clean(val)) for attrs, val in _RE_CELL.findall(card)]
    out: dict[str, str] = {}
    for i, (is_label, txt) in enumerate(cells):
        key = _canon(txt)
        if (is_label or key in _KNOWN_LABELS) and i + 1 < len(cells) and not cells[i + 1][0]:
            out.setdefault(key, cells[i + 1][1])
    return out


_RE_NUM = re.compile(r"(\d[\d\s]*(?:[.,]\d+)?)")


def _nums(text: str) -> list[float]:
    out = []
    for m in _RE_NUM.finditer(text or ""):
        try:
            out.append(float(m.group(1).replace(" ", "").replace(",", ".")))
        except ValueError:
            continue
    return out


def _rate_pair(text: str) -> tuple[float | None, float | None]:
    """«18.5%–21.5%» → (18.5, 21.5); «6%» → (6.0, 6.0)."""
    vals = _nums((text or "").replace("%", " "))
    if not vals:
        return None, None
    return min(vals), max(vals)


def _money_max(text: str) -> float | None:
    """«до 30 000 000 ₽» → 30000000."""
    vals = _nums(text)
    return max(vals) if vals else None


def _term_months(text: str) -> float | None:
    """«до 5 лет» → 60; «91 дн.» → 3; «до 15 мес.» → 15."""
    vals = _nums(text)
    if not vals:
        return None
    v = max(vals)
    t = (text or "").lower()
    if "лет" in t or "год" in t:
        return v * 12
    if "дн" in t:
        return round(v / 30.0, 1)
    return v


def _dec(v: float | None, q: str = "0.0001") -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(round(v, 4)))
    except (InvalidOperation, ValueError):
        return None


_RE_COMPANY = re.compile(r'data-test="offer-company"[^>]*>(.*?)</div>', re.S)
_RE_PRODUCT = re.compile(r'data-test="offer-product-name"[^>]*>(.*?)</div>', re.S)
_RE_LINK = re.compile(r'<a [^>]*href="([^"]+)"[^>]*data-test="offer-link-button"')
_RE_PRODUCT_ID = re.compile(r"product_?[Ii]d=(\d+)")
_CARD_TAIL = 12000          # карточка credits до 8.7 КБ, creditcards ~11.7 КБ
_BOUNDARIES = ("<footer", "window.initModule", 'application/ld+json')


def _split_cards(page: str, marker: str) -> list[str]:
    idx = [m.start() for m in re.finditer(re.escape(marker), page)]
    out = []
    for i, start in enumerate(idx):
        if i + 1 < len(idx):
            end = idx[i + 1]
        else:                       # хвост последней карточки обрезаем по границам
            end = start + _CARD_TAIL
            for b in _BOUNDARIES:
                p = page.find(b, start)
                if 0 < p < end:
                    end = p
        out.append(page[start:end])
    return out


def _is_promo(bank: str, product: str) -> bool:
    """Рекламная карточка-заглушка «Cложно выбрать?» со ставкой 5.99 проц.
    — если её не отсечь, она попадёт в статистику как оффер банка."""
    b = _canon(bank).lower()
    return ("сложно выбрать" in b or "заполните" in _canon(product).lower()
            or not b or len(b) < 2)


def _parse_labeled(page: str, section: str) -> list[dict]:
    """Ипотека, кредиты, кредитки, дебетовые карты — общая разметка."""
    out = []
    for card in _split_cards(page, 'data-test="offer"'):
        mb, mp = _RE_COMPANY.search(card), _RE_PRODUCT.search(card)
        if not mb or not mp:
            continue
        bank, product = _clean(mb.group(1)), _clean(mp.group(1))
        if _is_promo(bank, product):
            continue
        lab = _labels(card)
        link = _RE_LINK.search(card)
        url = _html.unescape(link.group(1)) if link else None
        pid = _RE_PRODUCT_ID.search(url or "")
        rate_min, rate_max = _rate_pair(lab.get("Ставка", ""))
        psk_min, psk_max = _rate_pair(lab.get("ПСК", ""))
        out.append({
            "section": section, "bank": bank, "product": product, "url": url,
            "product_id": pid.group(1) if pid else None,
            "rate_min": rate_min, "rate_max": rate_max,
            "psk_min": psk_min, "psk_max": psk_max,
            "amount": lab.get("Сумма"), "term": lab.get("Срок"),
            "fee": lab.get("Обслуживание"), "grace": lab.get("Льготный период"),
            "cashback": lab.get("Кэшбэк") or lab.get("Баллы"),
            "payment": lab.get("Платёж"), "initial": lab.get("Первоначальный взнос"),
        })
    return out


_RE_DEP_TERM = re.compile(r'data-test="offer-term">([^<]+)<')
_RE_DEP_PROFIT = re.compile(r'data-test="deposit-card-stat-profit">([^<]+)<')
_RE_TEXT_CELL = re.compile(r'data-test="text">([^<]+)<')
_RE_DEP_RATE = re.compile(r"(\d+[.,]\d+|\d+)\s*%")


def _parse_deposits(page: str, section: str) -> list[dict]:
    """Вклады: свой маркер card-offer и позиционные поля (у ставки две
    вёрстки, отдаются случайно — берём максимум процента из карточки)."""
    out = []
    for card in _split_cards(page, 'data-test="card-offer"'):
        cells = [_clean(c) for c in _RE_TEXT_CELL.findall(card)]
        cells = [c for c in cells if c]
        if len(cells) < 2:
            continue
        bank, product = cells[0], cells[1]
        if _is_promo(bank, product):
            continue
        rates = [float(x.replace(",", ".")) for x in _RE_DEP_RATE.findall(card)]
        rates = [r for r in rates if 0 < r <= 40]      # проценты вклада, не «до 100 000 ₽»
        term = _RE_DEP_TERM.search(card)
        profit = _RE_DEP_PROFIT.search(card)
        out.append({
            "section": section, "bank": bank, "product": product, "url": None,
            "product_id": None,
            "rate_min": min(rates) if rates else None,
            "rate_max": max(rates) if rates else None,
            "psk_min": None, "psk_max": None,
            "amount": _clean(profit.group(1)) if profit else None,
            "term": _clean(term.group(1)) if term else None,
            "fee": None, "grace": None, "cashback": None,
            "payment": None, "initial": None,
        })
    return out


def _parse_page(page: str, section: str) -> list[dict]:
    engine = CATEGORIES[section]["engine"]
    page = page.replace("<!-- -->", "")        # React-разделители режут текст
    return (_parse_deposits(page, section) if engine == "deposits"
            else _parse_labeled(page, section))


class BankiProductsAdapter(SourceAdapter):
    """Каталог продуктов banki.ru. Один таргет = одна категория."""
    name = "banki_products"

    def _get(self, url: str) -> str | None:
        try:
            r = self.http.client.get(url, headers={
                "User-Agent": _UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9"})
            if r.status_code != 200:
                return None
            return r.text
        except Exception as e:  # noqa: BLE001 — страница пропускается, обход живёт
            log.info("banki_products: %s — %s", url[:70], type(e).__name__)
            return None

    def _crawl_catalog(self, section: str) -> list[dict]:
        """Страницы каталога. Останавливаемся по ПУСТОЙ странице: за пределами
        данных сервер отдаёт 200 и валидный HTML на мегабайт с нулём карточек."""
        spec = CATEGORIES[section]
        out: list[dict] = []
        prev_keys: set | None = None
        for page in range(1, min(spec["pages"] + 3, _MAX_PAGES_HARD) + 1):
            url = f"{BASE}/{section}/" + (f"?page={page}" if page > 1 else "")
            html_text = self._get(url)
            time.sleep(_PAUSE_S)
            if not html_text:
                break
            items = _parse_page(html_text, section)
            if not items:
                break
            keys = {(i["bank"], i["product"]) for i in items}
            if prev_keys is not None and keys == prev_keys:
                # у вкладов ?page=N отдаёт те же карточки — пагинация фиктивна
                log.info("banki_products: %s — пагинация фиктивна, стоп на стр.%d",
                         section, page)
                break
            prev_keys = keys
            for it in items:
                it["scope"] = "catalog"
                it.setdefault("page_url", url)
            out.extend(items)
        return out

    def _crawl_banks(self, section: str, slugs: Iterable[str]) -> list[dict]:
        """Разделы банков: каталог отдаёт по одному «головному» офферу на банк,
        всё остальное живёт только здесь."""
        out: list[dict] = []
        for slug in slugs:
            html_text = self._get(f"{BASE}/{section}/{slug}/")
            time.sleep(_PAUSE_S)
            if not html_text:
                if slug == REQUIRED_SLUG:
                    log.warning("banki_products: %s/%s недоступен — Сбер не собран",
                                section, slug)
                continue
            items = _parse_page(html_text, section)
            for it in items:
                it["scope"] = f"bank:{slug}"
                # у карточек на странице банка нет кнопки-ссылки: без url
                # наблюдение аудитора невоспроизводимо — некуда кликнуть,
                # поэтому ставим адрес самого раздела (аудит 11.08.2026)
                it.setdefault("page_url", f"{BASE}/{section}/{slug}/")
            out.extend(items)
        return out

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        section = target.get("section") or "hypothec"
        if section not in CATEGORIES:
            raise KeyError(f"неизвестный раздел banki.ru: {section}")
        slugs = target.get("bank_slugs") or BANK_SLUGS
        items = self._crawl_catalog(section)
        items += self._crawl_banks(section, slugs)
        # дедуп: один продукт приходит и из каталога, и со страницы банка
        seen, uniq = set(), []
        for it in items:
            key = (it["bank"], it["product"])
            if key in seen:
                continue
            seen.add(key)
            uniq.append(it)
        n_sber = sum(1 for i in uniq if "сбер" in _canon(i["bank"]).lower())
        log.info("banki_products: %s — предложений %d, банков %d, у Сбера %d",
                 section, len(uniq), len({i["bank"] for i in uniq}), n_sber)
        content = json.dumps({"section": section, "offers": uniq,
                              "collected_at": datetime.now(timezone.utc).isoformat()},
                             ensure_ascii=False).encode("utf-8")
        path, digest, n = self.raw.write(self.name, target["name"], content, "json",
                                         meta={"section": section, "offers": len(uniq)})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"],
            url=f"{BASE}/{section}/", fetched_at=datetime.now(timezone.utc),
            http_status=200, content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=content)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        data = json.loads(html.decode("utf-8"))
        section = data.get("section") or target.get("section") or "hypothec"
        cat = CATEGORIES[section]["category"]
        for it in data.get("offers", []):
            bank, product = it.get("bank"), it.get("product")
            if not bank or not product:
                continue
            # У кредитных продуктов сравнивают нижнюю границу вилки («ставка от»).
            # У ВКЛАДОВ ставку отсюда НЕ берём вовсе: карточка вклада на banki.ru
            # верстается в двух вариантах, и регексп собирает все проценты блока —
            # промо-максимум («Выгодный старт +»: 40 проц. на 91 день для суммы
            # 10-100 тыс. руб.) вставал в один ряд с годовыми ставками. 11.08.2026
            # это дало «Сбер #1 из 141 со ставкой 40 проц.» при честной позиции
            # #55 с базовыми 13.5 проц. — и ровно 40.00 у 18 банков сразу.
            # Ставки вкладов остаются за sravni (1747 офферов), отсюда берём
            # только состав полки: названия, сроки, суммы.
            rate = None if cat == "deposit" else it.get("rate_min")
            if rate is None and cat != "deposit":
                rate = it.get("rate_max")
            ext = it.get("product_id") or f'{_canon(bank)[:24]}|{_canon(product)[:40]}'
            yield OfferDraft(
                bank_name_raw=bank,
                category=cat,
                external_id=f"bp_{section}_{ext}",
                title=product[:200],
                url=it.get("url") or it.get("page_url"),
                rate_pct=_dec(rate),
                rate_min=_dec(it.get("rate_min")), rate_max=_dec(it.get("rate_max")),
                psk_min=_dec(it.get("psk_min")), psk_max=_dec(it.get("psk_max")),
                rate_kind="rate" if rate is not None else None,
                amount_max=_dec(_money_max(it.get("amount") or "")),
                term_months_max=(int(_term_months(it.get("term") or "") or 0) or None),
                fee_service=_dec(_money_max(it.get("fee") or "")) if it.get("fee") else None,
                grace_days=(int(_money_max(it.get("grace") or "") or 0) or None
                            if it.get("grace") else None),
                conditions=" · ".join(x for x in (it.get("payment"), it.get("initial"),
                                                  it.get("cashback")) if x) or None,
                raw={k: it.get(k) for k in
                     ("rate_min", "rate_max", "psk_min", "psk_max", "amount", "term",
                      "fee", "grace", "cashback", "payment", "initial", "scope",
                      "section", "product_id")},
                # ставки меняются, а тарифных колонок это не касается: без
                # digest_extra история замрёт (см. OfferDraft.digest_extra)
                digest_extra={"rmin": it.get("rate_min"), "rmax": it.get("rate_max"),
                              "amount": it.get("amount"), "term": it.get("term"),
                              "fee": it.get("fee"), "grace": it.get("grace")},
            )
