"""Passive indexer — пассивное индексирование web-находок в БД.

Принцип: БД — кэш. Каждый раз когда агент скачивает URL, документ попадает
в `document` + `document_chunk` (с embeddings). Завтра тот же запрос найдёт
его мгновенно через semantic_search, без повторного fetch.

Также: отзывы, найденные на отзовиках (irecommend/otzovik), пассивно
ложатся в `review` таблицу (через upsert_review).

Все операции best-effort: если индексация упала — агент всё равно получает
текст (fallback на raw fetch). Это не должно блокировать исследование.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# Бюджет на возвращаемый текст (не на индексацию — она полная)
_RETURN_BUDGET = 8000


def index_and_get_text(url: str, *,
                        bank_slug_hint: str | None = None,
                        query_hint: str = "",
                        budget: int = _RETURN_BUDGET) -> dict:
    """Возвращает релевантный текст URL для промпта; тяжёлую индексацию
    (chunk+embed+insert) выполняет В ФОНЕ.

    КЛЮЧЕВОЕ (перф): раньше функция ЖДАЛА полную индексацию (fetch→parse→chunk→
    embed_batch→bulk insert) ПЕРЕД возвратом текста — а embed_batch это десятки
    эмбеддингов на страницу (CPU/или вызовы к тому же деградирующему эндпоинту),
    которые текущему запросу НЕ нужны (текст агенту берётся из content_text,
    который пишется ДО эмбеддинга; эмбеддинги нужны только будущему
    semantic_search). Это сериализовало агента на горячем пути.

    Теперь: fetch+parse → текст СРАЗУ; индексация — daemon-thread fire-and-forget
    (как extract-факты в indexer.py). Возврат {title, text, document_id, indexed}
    (document_id=None — на горячем пути его никто не использует)."""
    # 1. Быстрый путь: fetch + parse → текст немедленно (без ожидания эмбеддинга).
    text, title = "", ""
    try:
        from ...rag import fetcher
        from ...rag.parsers import parse_auto
        fr = fetcher.fetch(url, prefer_browser=False)
        if fr.content:
            parsed = parse_auto(fr.content, url=fr.final_url,
                                content_type=fr.content_type)
            full = parsed.text or ""
            title = parsed.title or ""
            text = (_relevant_excerpt(full, query_hint, budget)
                    if query_hint and len(full) > budget else full[:budget])
    except Exception as e:
        log.info("fetch+parse failed for %s: %s", url[:80], e)

    # 2. Тяжёлая индексация (chunk+embed+insert) — В ФОН: нужна только будущему
    #    semantic_search, не этому запросу. Агент не ждёт.
    def _bg_index():
        try:
            from ...rag.indexer import ingest_document_from_url
            ingest_document_from_url(url, bank_slug_hint=bank_slug_hint,
                                     prefer_browser=False)
        except Exception as e:
            log.info("background index failed for %s: %s", url[:80], e)
    try:
        import threading
        threading.Thread(target=_bg_index, daemon=True).start()
    except Exception:
        pass

    return {"title": title, "text": text, "document_id": None, "indexed": False}


def _load_from_db(document_id: int, query_hint: str, budget: int) -> tuple[str, str]:
    """Загружает документ из БД. Если есть query_hint — выбираем релевантные
    окна (как _relevant_excerpt в старом fact_extractor)."""
    from sqlalchemy import text as _t
    from ... import db
    with db.session() as s:
        row = s.execute(_t("""
            SELECT title, content_text FROM document WHERE document_id = :d
        """), {"d": document_id}).first()
    if not row:
        return "", ""
    title = row[0] or ""
    full = row[1] or ""
    if not query_hint or len(full) <= budget:
        return full[:budget], title
    return _relevant_excerpt(full, query_hint, budget), title


def _relevant_excerpt(text: str, query_hint: str, budget: int) -> str:
    """Выбирает окна, наиболее релевантные query_hint (плотность ключевых слов)."""
    terms = [w.lower() for w in re.split(r"\W+", query_hint) if len(w) >= 4]
    if not terms:
        return text[:budget]
    win = 1200
    step = int(win * 0.75)
    windows = []
    for start in range(0, len(text), step):
        chunk = text[start:start + win]
        if len(chunk) < 200:
            continue
        low = chunk.lower()
        score = sum(low.count(t) for t in terms)
        # бонус за числа (часто тарифы)
        score += len(re.findall(r"\d[\d .,]*\s*(?:₽|руб|%|мес|год|дн)", chunk)) * 2
        windows.append((start, chunk, score))
    if not windows:
        return text[:budget]
    windows.sort(key=lambda x: -x[2])
    picked, total = [], 0
    for start, chunk, _ in windows:
        if total >= budget:
            break
        picked.append((start, chunk))
        total += len(chunk)
    picked.sort(key=lambda x: x[0])
    return "\n…\n".join(c for _, c in picked)[:budget]


def _raw_fetch_full(url: str, budget: int) -> tuple[str, str]:
    """Прямой fetch без индексации — последний рубеж."""
    from ...rag import fetcher
    from ...rag.parsers import parse_auto
    fr = fetcher.fetch(url, prefer_browser=False)
    if not fr.content:
        return "", ""
    parsed = parse_auto(fr.content, url=fr.final_url, content_type=fr.content_type)
    return (parsed.text or "")[:budget], parsed.title or ""


# ════════════════════════════════════════════════════════════════════════
# PASSIVE REVIEW INDEXING — отзывы с отзовиков → таблица review
# ════════════════════════════════════════════════════════════════════════


def index_review_passive(*, source: str, source_review_id: str,
                          source_url: str, bank_name_raw: str,
                          text: str, rating: float | None = None,
                          title: str | None = None,
                          posted_at: datetime | None = None,
                          product_category: str | None = None) -> bool:
    """Пассивно сохраняет отзыв в БД (через upsert_review).

    Используется Reviews Agent когда находит отзывы на irecommend/otzovik и
    хочет их сохранить для будущих запросов. Дедуп через content_key.
    Возвращает True если отзыв записан (новый).
    """
    if not text or len(text) < 40:
        return False
    try:
        from ...models import ReviewDraft
        from ...normalizer.reviews import upsert_review
        from ... import db
        draft = ReviewDraft(
            source=source,
            source_review_id=source_review_id or source_url[-60:],
            source_url=source_url,
            bank_name_raw=bank_name_raw,
            product_category=product_category,  # type: ignore[arg-type]
            posted_at=posted_at,
            rating=rating,
            title=title,
            text=text[:8000],
            raw={"passive_index": True, "indexed_at": datetime.now(timezone.utc).isoformat()},
        )
        with db.session() as s:
            _, written = upsert_review(s, draft, snapshot_id=None)
            if written:
                s.commit()
                return True
            return False
    except Exception as e:
        log.info("passive review index failed: %s", e)
        return False
