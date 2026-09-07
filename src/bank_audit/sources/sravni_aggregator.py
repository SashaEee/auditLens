"""Sravni-агрегатор: страницы вида /vklady/?amount=...&period=...&region=...
   На странице — список карточек предложений по разным банкам с уже применёнными
   фильтрами. Селекторы вынесены в SELECTORS — единая точка адаптации к редизайну."""
from __future__ import annotations
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from selectolax.parser import HTMLParser
from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, RawSnapshot, FilterContext
from ..hashing import sha256_bytes, stable_digest

SELECTORS = {
    "card": "[data-qa='product-card'], .product-card, article",
    "bank_name": "[data-qa='bank-name'], .product-card__bank, h3",
    "title": "[data-qa='product-name'], .product-card__title",
    "rate": "[data-qa='rate'], .product-card__rate",
    "amount_range": "[data-qa='amount'], .product-card__amount",
    "term_range": "[data-qa='term'], .product-card__term",
    "url": "a[href]",
}

_RATE_RE = re.compile(r"(\d{1,2}[.,]\d{1,2}|\d{1,2})\s*%")
_AMOUNT_RE = re.compile(r"([\d\s]+)")
_PERIOD_RE = re.compile(r"(\d+)\s*(мес|год|лет)", re.IGNORECASE)
_PERIOD_RANGE_RE = re.compile(r"от\s*(\d+)\s*до\s*(\d+)\s*(мес|год|лет)", re.IGNORECASE)

def _to_decimal(s: str | None) -> Decimal | None:
    if not s: return None
    s = s.replace(",", ".").replace("\xa0", "").replace(" ", "")
    try: return Decimal(s)
    except Exception: return None

# Карточка агрегатора пишет либо ставку, либо ВЕРХНЮЮ ГРАНИЦУ: «до 30%».
# Прежний разбор брал первый процент и объявлял его ставкой — так ПСБ
# «Народный вклад» встал первой строкой рынка с 30% при реальных 10,5%
# (обратная связь ТБ, три банка). Это разбор текста, а не догадка о теме:
# «до» — грамматический маркер верхней границы, ровно как «%» — маркер доли.
_UPPER_BOUND_RE = re.compile(r"\bдо\s*$", re.IGNORECASE)


def _extract_rate(s: str | None) -> tuple[Decimal | None, str]:
    """Ставка и её ВИД: 'effective' — как есть, 'max' — верхняя граница.

    Вид важнее самой цифры: 30% «до» и 30% «от» — разные утверждения, и
    ранжировать их вместе нельзя.
    """
    if not s:
        return None, "effective"
    m = _RATE_RE.search(s)
    if not m:
        return None, "effective"
    kind = "max" if _UPPER_BOUND_RE.search(s[:m.start()]) else "effective"
    return _to_decimal(m.group(1)), kind

def _extract_amount(s: str | None) -> tuple[Decimal | None, Decimal | None]:
    if not s: return (None, None)
    nums = [_to_decimal(x.replace(" ", "")) for x in re.findall(r"[\d\s]{3,}", s)]
    nums = [n for n in nums if n is not None]
    if not nums: return (None, None)
    if len(nums) == 1: return (nums[0], None)
    return (min(nums), max(nums))

def _extract_term_months(s: str | None) -> tuple[int | None, int | None]:
    if not s: return (None, None)
    rng = _PERIOD_RANGE_RE.search(s)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        if rng.group(3).lower().startswith(("год", "лет")):
            lo, hi = lo * 12, hi * 12
        return (min(lo, hi), max(lo, hi))
    months = []
    for num, unit in _PERIOD_RE.findall(s):
        v = int(num)
        if unit.lower().startswith("год") or unit.lower().startswith("лет"):
            v *= 12
        months.append(v)
    if not months: return (None, None)
    return (min(months), max(months))

class SravniAggregatorAdapter(SourceAdapter):
    name = "sravni_aggregator"

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        url = target["url"]
        status, html = self.browser.fetch_html(
            url, wait_selector=SELECTORS["card"].split(",")[0],
        )
        path, digest, n = self.raw.write(self.name, target["name"], html, "html",
                                          meta={"url": url, "target": target["name"]})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=status,
            content_sha256=digest, storage_path=path, bytes=n,
            filter_context=FilterContext(**target.get("filter_context", {})),
            category=target.get("category", "deposit"),
        )
        return FetchResult(snapshot=snap, html=html)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        tree = HTMLParser(html.decode("utf-8", errors="ignore"))
        category = target.get("category", "deposit")
        seen_cards: set[int] = set()
        for card in tree.css(SELECTORS["card"]):
            if card.mem_id in seen_cards:
                continue
            seen_cards.add(card.mem_id)
            bank = (card.css_first(SELECTORS["bank_name"]) or None)
            title = (card.css_first(SELECTORS["title"]) or None)
            rate_node = card.css_first(SELECTORS["rate"])
            amount_node = card.css_first(SELECTORS["amount_range"])
            term_node = card.css_first(SELECTORS["term_range"])
            url_node = card.css_first(SELECTORS["url"])
            if not bank or not title:
                continue
            bank_name = bank.text(strip=True)
            title_text = title.text(strip=True)
            rate, rate_kind = _extract_rate(rate_node.text() if rate_node else None)
            amin, amax = _extract_amount(amount_node.text() if amount_node else None)
            tmin, tmax = _extract_term_months(term_node.text() if term_node else None)
            href = url_node.attributes.get("href") if url_node else None
            ext_id_payload = {
                "bank": bank_name, "title": title_text,
                "category": category, "currency": "RUB",
            }
            yield OfferDraft(
                bank_name_raw=bank_name,
                category=category,
                external_id=stable_digest(ext_id_payload)[:32],
                title=title_text,
                url=href if href and href.startswith("http") else (
                    f"https://www.sravni.ru{href}" if href else None
                ),
                rate_pct=rate, rate_kind=rate_kind,
                amount_min=amin, amount_max=amax,
                term_months_min=tmin, term_months_max=tmax,
                raw={
                    "filter_context": target.get("filter_context", {}),
                    "card_text": card.text(separator=" ", strip=True)[:2000],
                },
            )
