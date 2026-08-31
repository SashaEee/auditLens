"""Ежедневный авто-сборщик лазеек: keywords × web_search → fetch → classify → persist.

Поток:
  1. active_keywords() → список ключевых слов.
  2. Для каждого keyword → adapters.search_decorator.search() → список URL'ов.
  3. Для каждого результата → adapters.fetch_decorator.fetch_and_parse() → raw_text.
  4. sha256-дедуп → insert_record.
  5. classify_record → вердикт.

Без сети/БД в тестах — все внешние вызовы инъектируются.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ..ai.llm_utils import detect_bank_slugs
from ..hashing import sha256_text
from ..rag.trust import KNOWN_BANK_DOMAINS, compute_trust
from . import content_fetch
from . import keywords as kw_mod
from . import repository as repo
from .adapters import fetch_decorator, search_decorator
from .classify import classify_record
from .config import LoopholeSettings
from .models import LoopholeRecord

log = logging.getLogger(__name__)


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().replace("www.", "")
    except Exception:
        return ""


async def collect_once(
    *,
    settings: LoopholeSettings | None = None,
    llm: Any = None,
    session=None,
    search_impl: Any = None,
    fetch_impl: Any = None,
    max_per_keyword: int | None = None,
) -> int:
    """Один цикл сбора. Возвращает количество новых записей.

    Все внешние зависимости инъектируются для тестов.
    """
    settings = settings or LoopholeSettings.load()
    max_per = max_per_keyword or settings.max_results_per_keyword
    keywords = kw_mod.active_keywords(session=session)
    if not keywords:
        kw_mod.seed_keywords(session=session)
        keywords = kw_mod.active_keywords(session=session)
    new_count = 0
    for keyword in keywords:
        try:
            results = search_decorator.search(
                keyword, max_results=max_per, _impl=search_impl
            )
        except Exception as e:
            log.warning("[collector] search %r failed: %s", keyword, e)
            continue
        for r in results:
            url = r.get("url") or ""
            if not url:
                continue
            domain = r.get("domain") or _domain_of(url)
            # Пропускаем низко-доверенные источники.
            trust = compute_trust(0.5, url, r.get("snippet"))
            if trust < settings.trust_min and domain not in KNOWN_BANK_DOMAINS:
                continue
            sha = sha256_text(url + "|" + (r.get("snippet") or ""))
            if repo.exists_sha256(sha, session=session):
                continue
            # Fetch + parse. Полный текст страницы (не excerpt) — с лимитом
            # из конфига и честным статусом контента.
            page = fetch_decorator.fetch_and_parse(url, _fetch_impl=fetch_impl)
            if page is not None:
                content = content_fetch.limit_content(
                    page.text, max_chars=settings.raw_text_max_chars
                )
                if content.text is None:
                    # пустая страница — фиксируем empty, текст со сниппета
                    content = content_fetch.FullContent(
                        text=r.get("snippet") or "",
                        status=content_fetch.STATUS_EMPTY,
                        length=len(r.get("snippet") or ""),
                        truncated=False,
                    )
                title = page.title
            else:
                content = content_fetch.FullContent(
                    text=r.get("snippet") or "",
                    status=content_fetch.STATUS_FAILED,
                    length=len(r.get("snippet") or ""),
                    truncated=False,
                )
                title = r.get("title")
            raw_text = content.text
            bank_slugs = detect_bank_slugs((r.get("title") or "") + " " + (r.get("snippet") or ""))
            rec = LoopholeRecord(
                sha256=sha,
                title=title,
                url=url,
                snippet=r.get("snippet"),
                domain=domain,
                trust_score=trust,
                bank_slug=bank_slugs[0] if bank_slugs else None,
                keyword=keyword,
                raw_text=raw_text,
                content_status=content.status,
                raw_text_len=content.length,
                raw_text_truncated=content.truncated,
            )
            rid = repo.insert_record(rec, session=session)
            if rid is None:
                continue
            # Проверяем, была ли это новая запись (дедуп мог вернуть существующий id).
            new_count += 1
            try:
                await classify_record(rid, llm=llm, session=session)
            except Exception as e:
                log.warning("[collector] classify %s failed: %s", rid, e)
    return new_count
