"""Bankiros.ru reviews — парсим из JSON-LD (schema.org/Review).

Bankiros отдаёт structured data на странице /bank/{slug}/otzyvy:
  <script type="application/ld+json">
    {"@type":"BankOrCreditUnion", "name":"СберБанк",
     "review":[{"@type":"Review", "author":{"name":"..."}, "datePublished":"DD.MM.YYYY",
                "reviewBody":"...", "reviewRating":{"ratingValue":5}}, ...]}
  </script>

Преимущества:
  • JSON-LD стандарт schema.org — никаких CSS-классов
  • HTTP без JS, быстро, не банится при разумном rate
  • Дополняет banki.ru/sravni.ru (другая аудитория ⇒ другие настроения)

Ограничения:
  • На странице только ~10 свежих отзывов
  • Пагинация ?page=N не работает (всегда возвращает page=1)
  • Поэтому max_pages=1, фокус на широте охвата (много банков), а не глубине
"""
from __future__ import annotations
import json, logging, re, time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, \
    retry_if_exception_type

from .base import SourceAdapter, FetchResult
from ..models import ReviewDraft, RawSnapshot

log = logging.getLogger(__name__)

_LD_RE = re.compile(
    r'<script[^>]+ld\+json[^>]*>(.*?)</script>', re.DOTALL
)

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}


class BankirosReviewsAdapter(SourceAdapter):
    name = "bankiros_reviews"

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        url = target["url"]

        @retry(stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=2, min=3, max=20),
               retry=retry_if_exception_type((httpx.TransportError,
                                              httpx.TimeoutException)))
        def _get(client, u):
            return client.get(u)

        with httpx.Client(http2=False, headers=_HTTP_HEADERS,
                          follow_redirects=True,
                          timeout=httpx.Timeout(connect=8.0, read=20.0,
                                                write=8.0, pool=8.0)) as client:
            try:
                resp = _get(client, url)
            except Exception as e:
                raise RuntimeError(f"bankiros_reviews HTTP error: "
                                   f"{type(e).__name__}: {str(e)[:120]}")

        if resp.status_code == 404:
            raise RuntimeError(f"bankiros_reviews: 404 для {url} (slug?)")
        if resp.status_code != 200:
            raise RuntimeError(f"bankiros_reviews: HTTP {resp.status_code}")

        html = resp.content
        if b"reviewBody" not in html and b"@type" not in html:
            raise RuntimeError(f"bankiros_reviews: нет JSON-LD на {url}")

        path, digest, n = self.raw.write(
            self.name, target["name"], html, "html",
            meta={"url": url, "target": target["name"]},
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc),
            http_status=resp.status_code,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=html)

    def parse_reviews(self, html: bytes,
                      target: dict[str, Any]) -> Iterable[ReviewDraft]:
        text = html.decode("utf-8", errors="ignore")
        bank_slug = target["bank_slug"]
        seen: set[str] = set()
        n_reviews = 0

        for m in _LD_RE.finditer(text):
            blob = m.group(1).strip()
            try:
                data = json.loads(blob)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            reviews = data.get("review") or []
            if not isinstance(reviews, list):
                continue
            for r in reviews:
                if not isinstance(r, dict):
                    continue
                body = (r.get("reviewBody") or "").strip()
                if len(body) < 20:
                    continue

                author = r.get("author")
                author_name = (author.get("name").strip()
                               if isinstance(author, dict) and author.get("name")
                               else None)

                rating_v = None
                rr = r.get("reviewRating")
                if isinstance(rr, dict):
                    try:
                        rating_v = Decimal(str(rr.get("ratingValue")))
                    except Exception:
                        rating_v = None

                # Дата формата dd.MM.yyyy или yyyy-MM-dd
                posted = None
                dp = r.get("datePublished")
                if isinstance(dp, str):
                    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
                        try:
                            posted = datetime.strptime(dp, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            pass

                # Стабильный source_review_id: hash от (bank, body, date, author).
                # У bankiros JSON-LD нет своего id для отзыва.
                from ..hashing import stable_digest
                rid = stable_digest({
                    "bank": bank_slug, "body": body[:200],
                    "date": dp or "", "author": author_name or "",
                })[:32]
                if rid in seen:
                    continue
                seen.add(rid)
                n_reviews += 1

                yield ReviewDraft(
                    source=self.name,
                    source_review_id=rid,
                    source_url=target.get("url", ""),
                    bank_name_raw=bank_slug,
                    posted_at=posted,
                    rating=rating_v,
                    title=r.get("name") or None,
                    text=body,
                    author_raw=author_name,
                )
        log.info("bankiros_reviews %s: %s reviews from JSON-LD",
                 bank_slug, n_reviews)
