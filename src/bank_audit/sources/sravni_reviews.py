"""Sravni.ru reviews — парсим из __NEXT_DATA__ (SSR JSON), не из CSS-классов.

CSS-селекторы устарели: sravni переехал на CSS-modules с хешированными именами.
Но Redux-состояние полное и стабильное:
  state.reviews.list.items[] — массив отзывов
  state.reviews.list.total   — totalCount

Каждый item: { id, userId, date, title, text, ratingBase,
               reviewObjectId, reviewObjectType, specificProductId, ... }

Для глубокой пагинации (>10) sravni использует client-side AJAX,
здесь ловим только первую страницу — достаточно как baseline.
Для глубокого сбора → отдельный browser-таргет с прокруткой
(добавим если понадобится).
"""
from __future__ import annotations
import json, logging, re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from .base import SourceAdapter, FetchResult
from ..models import ReviewDraft, RawSnapshot

log = logging.getLogger(__name__)

_NEXT_DATA_RE = re.compile(
    r'<script\s[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(int(v), tz=timezone.utc)
        except Exception:
            return None
    s = str(v)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


class SravniReviewsAdapter(SourceAdapter):
    name = "sravni_reviews"

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        url = target["url"]
        # Не дожидаемся капризного селектора — нам нужно SSR, оно в HTML с первого ответа.
        # Скролл всё равно делаем — страница может подгрузить больше items в Redux.
        status, html = self.browser.fetch_html(
            url, scroll_to_bottom=True,
            workspace_dir=str(self.settings.workspace_dir) if self.settings else None,
            source=self.name, target=target.get("name"),
        )
        path, digest, n = self.raw.write(self.name, target["name"], html, "html",
                                          meta={"url": url, "target": target["name"]})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=status,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=html)

    def parse_reviews(self, html: bytes, target: dict[str, Any]) -> Iterable[ReviewDraft]:
        text = html.decode("utf-8", errors="ignore")
        m = _NEXT_DATA_RE.search(text)
        if not m:
            log.warning("sravni_reviews: __NEXT_DATA__ not found")
            return

        try:
            nd = json.loads(m.group(1))
        except Exception as e:
            log.warning("sravni_reviews: JSON parse error: %s", e)
            return

        state = (nd.get("props") or {}).get("initialReduxState") or {}
        rev   = (state.get("reviews") or {}).get("list") or {}
        items = rev.get("items") or []
        total = rev.get("total")
        bank_slug = target["bank_slug"]
        log.info("sravni_reviews %s: SSR %s items (total=%s)", bank_slug, len(items), total)

        for item in items:
            rid = item.get("id") or item.get("_id")
            if not rid:
                continue
            txt = (item.get("text") or "").strip()
            if len(txt) < 10:
                continue
            rating_v = item.get("ratingBase") or item.get("rating") or item.get("rate")
            try:
                rating = Decimal(str(rating_v)) if rating_v is not None else None
            except Exception:
                rating = None
            yield ReviewDraft(
                source=self.name,
                source_review_id=str(rid),
                source_url=f"https://www.sravni.ru/bank/{bank_slug}/otzyvy/{rid}/",
                bank_name_raw=bank_slug,
                posted_at=_parse_dt(item.get("date")),
                rating=rating,
                title=item.get("title") or None,
                text=txt,
                author_raw=str(item.get("userId")) if item.get("userId") else None,
                status=item.get("status") or item.get("reviewTag"),
                raw={
                    "review_object_id":   item.get("reviewObjectId"),
                    "review_object_type": item.get("reviewObjectType"),
                    "specific_product":   item.get("specificProductName"),
                    "specific_product_id":item.get("specificProductId"),
                    "comments_count":     item.get("commentsCount"),
                    "is_legal":           item.get("isLegal"),
                    "tag":                item.get("reviewTag"),
                },
            )
