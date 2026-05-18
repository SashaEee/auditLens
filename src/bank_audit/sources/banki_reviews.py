"""Banki.ru reviews — HTML-парсинг с regex по стабильным якорям.

CSS-классы хешированы (CSS modules), меняются с каждым релизом. Поэтому
не используем их. Стабильны:
  • href вида /services/responses/bank/response/NNNNN/  → review_id, заголовок
  • строки "Оценка:", "Зачтено"/"Не зачтено"/"В обработке"
  • <h3>...<a>title</a></h3>

Тексты отзывов — внутри блока между двумя такими href. Берём regex'ом.
Глубокая пагинация (>20 на страницу) делается через ?page=N — banki.ru
поддерживает классическую пагинацию.
"""
from __future__ import annotations
import logging, re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
import time

from selectolax.parser import HTMLParser

from .base import SourceAdapter, FetchResult
from ..models import ReviewDraft, RawSnapshot

log = logging.getLogger(__name__)

_LINK_RE   = re.compile(
    r'href="(/services/responses/bank/response/(\d+)/)"[^>]*>\s*([^<]{0,200})\s*</a>',
    re.DOTALL,
)
_RATING_RE = re.compile(
    r'Оценка:\s*</span>\s*<div[^>]*>\s*(\d{1,2})\s*</div>',
    re.DOTALL,
)
_STATUS_RE = re.compile(r'\b(Зачтено|Не\s+зачтено|В\s+обработке|Без\s+оценки)\b')
_DATE_RE   = re.compile(r'(\d{1,2}\.\d{2}\.\d{4})')
_AUTHOR_RE = re.compile(r'/services/responses/list/user/[a-zA-Z0-9_.-]+/[^>]*>\s*([^<]{1,80})')


class BankiReviewsAdapter(SourceAdapter):
    name = "banki_reviews"

    # Browser-эмулирующий User-Agent: banki.ru ОТЛИЧНО отдаёт HTML по HTTP.
    # Playwright тут лишний (медленный, periodic timeout, конфликт за profile-lock).
    _HTTP_HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    }

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        url = target["url"]
        max_pages = int(target.get("max_pages", 5))
        chunks: list[bytes] = []
        first_status = 0

        # HTTP/1.1 + браузерные заголовки + retry с backoff. Banki.ru блокирует
        # bot-like UA, не принимает HTTP/2 от не-браузеров. Работают только
        # реалистичные браузерные параметры.
        import httpx
        from tenacity import retry, stop_after_attempt, wait_exponential, \
            retry_if_exception_type

        @retry(stop=stop_after_attempt(3),
               wait=wait_exponential(multiplier=2, min=4, max=30),
               retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)))
        def _fetch_with_retry(client, page_url):
            return client.get(page_url)

        with httpx.Client(
            http2=False,                              # banki.ru недолюбливает h2 от не-браузеров
            headers=self._HTTP_HEADERS,
            follow_redirects=True,
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
        ) as client:
            for p in range(1, max_pages + 1):
                page_url = url if p == 1 else (url + ("&" if "?" in url else "?") + f"page={p}")
                try:
                    resp = _fetch_with_retry(client, page_url)
                except Exception as e:
                    log.warning("banki_reviews %s page %s: HTTP error %s",
                                target.get("name"), p, type(e).__name__ + ":" + str(e)[:120])
                    break
                if p == 1:
                    first_status = resp.status_code
                if resp.status_code == 404:
                    log.warning("banki_reviews %s: HTTP 404 — slug неверен (url=%s)",
                                target.get("name"), page_url)
                    break
                if resp.status_code != 200:
                    log.warning("banki_reviews %s page %s: HTTP %s",
                                target.get("name"), p, resp.status_code)
                    break
                html = resp.content
                if b"/services/responses/bank/response/" not in html:
                    log.info("banki_reviews %s page %s: no review links — stop",
                             target.get("name"), p)
                    break
                chunks.append(html)
                log.info("banki_reviews %s page %s: %s bytes",
                         target.get("name"), p, len(html))
                time.sleep(1.2)  # больше delay'я между страницами одного банка

        body = b"\n<!--PAGE-->\n".join(chunks) if chunks else b""

        # Fallback на Playwright если HTTP не сработал (banki блокирует наш IP).
        # Прогретый OPENCLAW-профиль с cookies может пройти когда чистый HTTP — нет.
        if not body and self.browser is not None:
            log.warning("banki_reviews %s: HTTP пусто, fallback на Playwright",
                        target.get("name"))
            try:
                p1_url = url
                status, html = self.browser.fetch_html(
                    p1_url,
                    workspace_dir=str(self.settings.workspace_dir) if self.settings else None,
                    source=self.name, target=target.get("name"),
                )
                if html and b"/services/responses/bank/response/" in html:
                    body = html
                    first_status = status
                    log.info("banki_reviews %s: Playwright fallback OK (%s bytes)",
                             target.get("name"), len(html))
            except Exception as e:
                log.warning("banki_reviews %s: Playwright fallback тоже упал: %s",
                            target.get("name"), str(e)[:200])

        # Если получили пустой body — это failure, не "снимок без изменений"
        if not body:
            raise RuntimeError(
                f"banki_reviews: пустой ответ для {target.get('name')} "
                f"(url={url}, status={first_status}). Возможно IP заблокирован "
                "banki.ru — попробуй позже (30-60 мин) или с другой сети."
            )
        path, digest, n = self.raw.write(self.name, target["name"], body, "html",
                                          meta={"url": url, "target": target["name"],
                                                "pages": len(chunks)})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=first_status or 200,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=body)

    def parse_reviews(self, html: bytes, target: dict[str, Any]) -> Iterable[ReviewDraft]:
        text = html.decode("utf-8", errors="ignore")
        bank_slug = target["bank_slug"]

        # Разрезаем на отдельные блоки по позициям ссылок-заголовков отзывов
        links = list(_LINK_RE.finditer(text))
        # Берём только заголовочные ссылки (содержат текст заголовка), а не упоминания/комменты
        title_links = [m for m in links if m.group(3).strip() and "#comments" not in m.group(1)]
        log.info("banki_reviews %s: found %d review-link matches", bank_slug, len(title_links))

        seen: set[str] = set()
        for i, lm in enumerate(title_links):
            rid = lm.group(2)
            if rid in seen:
                continue
            seen.add(rid)
            title = re.sub(r"\s+", " ", lm.group(3)).strip()
            # Блок отзыва — от текущего href до следующего такого же
            start = lm.start()
            end = title_links[i + 1].start() if i + 1 < len(title_links) else min(len(text), start + 8000)
            block = text[start:end]

            # rating
            rating = None
            rm = _RATING_RE.search(block)
            if rm:
                try:
                    rating = Decimal(rm.group(1))
                except Exception:
                    rating = None
            # status
            status = None
            sm = _STATUS_RE.search(block)
            if sm:
                status = re.sub(r"\s+", " ", sm.group(1)).strip()
            # date
            posted = None
            dm = _DATE_RE.search(block)
            if dm:
                try:
                    posted = datetime.strptime(dm.group(1), "%d.%m.%Y").replace(tzinfo=timezone.utc)
                except Exception:
                    posted = None
            # author
            author = None
            am = _AUTHOR_RE.search(block)
            if am:
                author = re.sub(r"\s+", " ", am.group(1)).strip()
            # body — берём всё что между rating-блоком и concrete url-следующим, как plain text
            tree = HTMLParser(block)
            plain = tree.text(separator="\n", strip=True)
            # отрежем мусор: всё до заголовка
            after_title = plain.split(title, 1)
            body_text = after_title[1] if len(after_title) > 1 else plain
            # обрежем хвостовые служебные строки
            body_text = re.sub(r"\b(Зачтено|Не\s+зачтено|В\s+обработке|Без\s+оценки)\b.*", "",
                               body_text, flags=re.DOTALL).strip()
            body_text = re.sub(r"^Оценка:\s*\d+\s*", "", body_text)
            body_text = re.sub(r"\n{2,}", "\n", body_text).strip()
            if len(body_text) < 20:
                continue

            yield ReviewDraft(
                source=self.name,
                source_review_id=rid,
                source_url=f"https://www.banki.ru/services/responses/bank/response/{rid}/",
                bank_name_raw=bank_slug,
                posted_at=posted,
                rating=rating,
                title=title,
                text=body_text,
                author_raw=author,
                status=status,
            )
