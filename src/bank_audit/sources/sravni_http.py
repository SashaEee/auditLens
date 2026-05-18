"""Sravni.ru HTTP-адаптер — использует SSR HTML страницы агрегатора.
   Никакого headless-браузера, никаких капч. Страница отдаётся с сервером
   целиком (Next.js SSR) и содержит карточки предложений + встроенные отзывы.

   Также извлекает встроенные JSON-отзывы из Redux state в HTML."""
from __future__ import annotations
import re, json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from selectolax.parser import HTMLParser
from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, ReviewDraft, RawSnapshot, FilterContext
from ..hashing import sha256_bytes, stable_digest, author_hash

# ── Selectors ──────────────────────────────────────────────────────────────
CARD_SEL  = '[data-qa="Card"]'
LOGO_SEL  = "img[alt]"
NAME_SEL  = "[class*='depositName'], [class*='DepositName'], [class*='_title__']"
LINK_SEL  = "a[href]"

AD_MARKERS = {"реклама", "реклама банка", "партнёрский материал"}
_RATE_RE   = re.compile(r'(\d{1,2}[.,]\d{1,2}|\d{1,2})\s*%')
_TERM_RE   = re.compile(r'(\d+)\s*(мес(?:яц(?:ев|а)?)?|год(?:а|ов)?|лет)', re.IGNORECASE)
_AMOUNT_RE = re.compile(r'от\s*([\d\s]+)\s*(?:₽|руб)', re.IGNORECASE)
# Встроенные отзывы в Redux state
_REVIEW_RE = re.compile(
    r'\{"id":(\d+),"authorName":"([^"]*)",'
    r'"rating":(\d+(?:\.\d+)?),"title":"([^"]*)",'
    r'"text":"((?:[^"\\]|\\.)*)"\}', re.DOTALL
)

def _parse_rate(text: str) -> Decimal | None:
    m = _RATE_RE.search(text)
    if not m: return None
    try: return Decimal(m.group(1).replace(",", "."))
    except Exception: return None

def _parse_term_months(text: str) -> tuple[int | None, int | None]:
    matches = [(int(m.group(1)), m.group(2).lower()) for m in _TERM_RE.finditer(text)]
    if not matches: return None, None
    months = []
    for n, unit in matches:
        if unit.startswith("год") or unit.startswith("лет"):
            months.append(n * 12)
        else:
            months.append(n)
    return min(months), max(months)

def _parse_amount(text: str) -> Decimal | None:
    m = _AMOUNT_RE.search(text)
    if not m: return None
    try: return Decimal(m.group(1).replace(" ", "").replace("\xa0", ""))
    except Exception: return None


class SravniHttpAdapter(SourceAdapter):
    """HTTP-адаптер для sravni.ru (без браузера).
       Работает для агрегаторных страниц /vklady/, /kredity/, /kreditnye-karty/ и др."""
    name = "sravni_http"

    # Заголовки, имитирующие настоящий браузерный запрос.
    # User-Agent ОБЯЗАТЕЛЕН здесь — он должен перебить заголовок клиента.
    _BROWSER_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        import time, httpx
        url = target["url"]
        # Создаём свой клиент без follow_redirects и без bot-UA.
        # sravni.ru при follow_redirects=True видит технические заголовки httpx
        # и отправляет 302→captcha. При follow_redirects=False возвращает 200 с данными.
        time.sleep(self.http.delay_s)
        with httpx.Client(timeout=30, follow_redirects=False) as c:
            r = c.get(url, headers=self._BROWSER_HEADERS)
        content = r.content
        status = r.status_code

        # Капча-детекция: или маркеры в тексте, или подозрительно маленький ответ
        # (реальная страница ≥ 400KB, captcha-страница ≈ 10-15KB)
        body_preview = content[:8192].decode("utf-8", errors="ignore")
        is_captcha = (
            any(m in body_preview for m in ["Вы не робот", "SmartCaptcha", "showcaptcha",
                                             "tmgrdfrend", "checkcaptcha"])
            or len(content) < 50_000
        )
        if is_captcha:
            from ..config import Settings
            ws = Settings.load().workspace_dir
            import json as _json
            path_cp = ws / "captcha_pending.json"
            items: list = []
            if path_cp.exists():
                try: items = _json.loads(path_cp.read_text())
                except Exception: pass
            if not any(i["url"] == target["url"] for i in items):
                items.append({"url": target["url"], "source": self.name})
                path_cp.write_text(_json.dumps(items, ensure_ascii=False))
            raise RuntimeError(
                f"sravni.ru вернул captcha-страницу ({len(content)} байт). "
                f"Откройте {target['url']} в браузере — IP временно заблокирован."
            )

        path, digest, n = self.raw.write(
            self.name, target["name"], content, "html",
            meta={"url": url, "target": target["name"]}
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=status,
            content_sha256=digest, storage_path=path, bytes=n,
            filter_context=FilterContext(**target.get("filter_context", {})),
            category=target.get("category", "deposit"),
        )
        return FetchResult(snapshot=snap, html=content)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        text_html = html.decode("utf-8", errors="ignore")
        tree = HTMLParser(text_html)
        category = target.get("category", "deposit")

        seen_keys: set[str] = set()
        for card in tree.css(CARD_SEL):
            # Банк из alt логотипа
            logo = card.css_first(LOGO_SEL)
            bank = (logo.attributes.get("alt", "") if logo else "").strip()
            if not bank or bank.lower() in AD_MARKERS:
                continue

            # Название продукта
            name_node = card.css_first(NAME_SEL)
            full_text = card.text(separator="|", strip=True)
            name = name_node.text(strip=True) if name_node else ""
            if not name:
                # Fallback: берём вторую часть текста
                parts = [p.strip() for p in full_text.split("|") if p.strip()]
                name = parts[1] if len(parts) > 1 else parts[0] if parts else bank

            rate = _parse_rate(full_text)
            tmin, tmax = _parse_term_months(full_text)
            amin = _parse_amount(full_text)

            # Условия
            tl = full_text.lower()
            early_w = "досроч" in tl or "отзыв" in tl
            capital = "капитализация" in tl or "с капит" in tl
            replen  = "пополн" in tl

            # URL
            link = card.css_first(LINK_SEL)
            href = link.attributes.get("href", "") if link else ""
            url_full = (f"https://www.sravni.ru{href}" if href.startswith("/") else href) or None

            dedup_key = f"{bank}|{name}|{rate}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            ext_id = stable_digest({"bank": bank, "name": name, "category": category})[:32]
            yield OfferDraft(
                bank_name_raw=bank,
                category=category,
                external_id=ext_id,
                title=name or bank,
                url=url_full,
                rate_pct=rate,
                rate_kind="effective",
                currency="RUB",
                amount_min=amin,
                term_months_min=tmin,
                term_months_max=tmax,
                early_withdraw=early_w or None,
                capitalization=capital or None,
                replenishable=replen or None,
                raw={
                    "filter_context": target.get("filter_context", {}),
                    "card_text": full_text[:500],
                },
            )

    def parse_reviews(self, html: bytes, target: dict[str, Any]) -> Iterable[ReviewDraft]:
        """Извлекаем встроенные JSON-отзывы из Redux state."""
        text_html = html.decode("utf-8", errors="ignore")
        bank_slug = target.get("bank_slug", "")
        source_url = target["url"]

        for m in _REVIEW_RE.finditer(text_html):
            rid, author, rating, title, text_raw = m.groups()
            # Раскодируем JSON-escape в тексте
            try:
                text_clean = json.loads(f'"{text_raw}"')
            except Exception:
                text_clean = text_raw.replace("\\n", "\n").replace('\\"', '"')

            if len(text_clean) < 15:
                continue

            yield ReviewDraft(
                source=self.name,
                source_review_id=f"sravni_html_{rid}",
                source_url=source_url,
                bank_name_raw=bank_slug or "sravni_embedded",
                rating=Decimal(rating),
                title=title or None,
                text=text_clean,
                author_raw=author or None,
                raw={"embedded": True},
            )
