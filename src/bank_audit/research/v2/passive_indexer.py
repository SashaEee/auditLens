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
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Бюджет на возвращаемый текст (не на индексацию — она полная). Согласован с
# read_url (V2_READ_BUDGET_CHARS): отдаём модели больше содержимого страницы.
_RETURN_BUDGET = 16000

# Официальные сайты банков — SPA на JS: по HTTP отдают пустой каркас (без тарифов).
# Для них read_url рендерит страницу через Playwright (браузер на сервере есть).
# Агрегаторы (banki.ru/sravni.ru) НЕ включаем — они читаются по httpx, а браузер
# медленный (агент их читает много). Тюнится V2_BROWSER_RENDER (1=вкл, 0=выкл).
# sberbank.ru ОБЯЗАТЕЛЬНО через browser: по httpx он отдаёт JS-заглушку
# «Please enable JavaScript…» (~91 симв полезного текста, SPA+антибот), реальный
# контент рисуется только JS. (Прошлые ~5.5к по httpx — это сырой HTML-shell+
# скрипты, а извлечённого текста там лишь заглушка.)
_SPA_RENDER_DOMAINS = (
    "sberbank.ru", "vtb.ru", "alfabank.ru", "tbank.ru", "tinkoff.ru",
    "gazprombank.ru", "sovcombank.ru", "rshb.ru", "open.ru", "raiffeisen.ru",
    "pochtabank.ru", "mkb.ru", "psbank.ru", "rosbank.ru", "mtsbank.ru",
    "domrfbank.ru", "uralsib.ru", "akbars.ru",
    # + розничные банки для физлиц
    "homecredit.ru", "otpbank.ru", "unicreditbank.ru", "absolutbank.ru",
    "zenit.ru", "rencredit.ru", "ozon.ru", "yoomoney.ru",
)


def _learned_render_domain(domain: str) -> bool:
    """Домен, где браузерный добор уже побеждал HTTP (самообучение)."""
    try:
        from ...rag import cache as rag_cache
        return bool(rag_cache.get("render_hint", domain))
    except Exception:
        return False


def _learn_render_domain(domain: str) -> None:
    try:
        from ...rag import cache as rag_cache
        rag_cache.put("render_hint", True, 30 * 24 * 3600, domain)
        log.info("[render-hint] домен %s выучен: SPA, рендерим сразу", domain)
    except Exception:
        pass


def _should_render(url: str) -> bool:
    """SPA офиц. сайта банка → рендерим браузером (httpx отдаёт пустой каркас)."""
    if os.getenv("V2_BROWSER_RENDER", "1") == "0":
        return False
    try:
        parsed = urlparse(url)
        # Файл (pdf/xlsx/docx/…) браузером не рендерят: навигация превращается
        # в download, Playwright возвращает пустоту, и живой файл на SPA-домене
        # умирал с ложным диагнозом «пустая страница».
        if parsed.path.lower().endswith(
                (".pdf", ".xlsx", ".xls", ".xlsm", ".docx", ".doc",
                 ".pptx", ".ppt", ".zip", ".csv")):
            return False
        d = parsed.netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        if any(d == x or d.endswith("." + x) for x in _SPA_RENDER_DOMAINS):
            return True
        # Выученные домены: браузерный добор здесь уже побеждал HTTP.
        return _learned_render_domain(d)
    except Exception:
        return False


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
    full_text = ""
    skipped_reason = ""
    file_links: list = []
    section_links: list = []
    _fetch_via, _captcha, _status = "", False, 0
    _render = _should_render(url)
    _content, _ctype, _final = None, None, None
    try:
        from ...rag import fetcher
        from ...rag.parsers import parse_auto
        fr = fetcher.fetch(url, prefer_browser=_render)
        _fetch_via = getattr(fr, "via", "") or ""
        _captcha = bool(getattr(fr, "captcha", False))
        _status = getattr(fr, "status", 0) or 0
        if fr.content:
            _content, _ctype, _final = fr.content, fr.content_type, fr.final_url
            parsed = parse_auto(fr.content, url=fr.final_url,
                                content_type=fr.content_type)
            full = parsed.text or ""
            title = parsed.title or ""
            _meta = getattr(parsed, "meta", None) or {}
            skipped_reason = _meta.get("skipped_reason", "")
            file_links = _meta.get("file_links") or []
            section_links = _meta.get("section_links") or []
            # RENDER-ON-DEMAND (универсально, без ручных списков): большой
            # HTML, из которого извлеклись крохи текста, — сигнатура SPA-
            # каркаса. Ручной список _SPA_RENDER_DOMAINS не масштабируется
            # (domrfbank.ru в нём не было — и «ИЖС-Подряд» превратился в
            # «условия недоступны»). Добираем браузером ОДИН раз и запоминаем
            # домен: дальше он рендерится сразу.
            if (not _render and _status == 200 and len(full) < 600
                    and len(fr.content) > 15000
                    and "html" in (fr.content_type or "html").lower()
                    and os.getenv("V2_BROWSER_RENDER", "1") != "0"):
                try:
                    fr2 = fetcher.fetch(url, prefer_browser=True)
                    if fr2 is not None and fr2.content:
                        parsed2 = parse_auto(fr2.content, url=fr2.final_url,
                                             content_type=fr2.content_type)
                        full2 = parsed2.text or ""
                        if len(full2) > max(len(full) * 2, 800):
                            log.warning("[render-on-demand] %s: HTTP дал %s "
                                        "симв., браузер — %s; домен выучен",
                                        url[:70], len(full), len(full2))
                            full, title = full2, (parsed2.title or title)
                            _content, _ctype = fr2.content, fr2.content_type
                            _final = fr2.final_url
                            _render = True
                            from urllib.parse import urlparse as _up
                            _dom = (_up(url).netloc or "").lower().removeprefix("www.")
                            if _dom:
                                _learn_render_domain(_dom)
                except Exception as _e:  # noqa: BLE001 — добор не роняет чтение
                    log.info("[render-on-demand] %s: %s", url[:60], _e)
            # Два РАЗНЫХ бюджета. `text` уходит в диалог модели и может быть
            # сколь угодно урезан. `full` — доказательная база: по нему идёт
            # сверка дословных цитат (антигаллюцинации). Раньше это была одна
            # и та же строка, поэтому любое сжатие контекста автоматически
            # урезало базу сверки и честные цитаты становились «неподтверждёнными».
            # Выборка работает ВСЕГДА, а не только на страницах больше бюджета:
            # типичная страница банка укладывалась в 12 000 и ехала целиком,
            # вместе с меню и фильтрами. Подсказка — вопрос аудитора, если
            # агент не передал свою.
            _hint = query_hint or _origin_ctx().get("question") or ""
            text = (_relevant_excerpt(full, _hint, budget)
                    if len(full) > budget or _hint else full[:budget])
            full_text = full
    except Exception as e:
        log.info("fetch+parse failed for %s: %s", url[:80], e)

    # 2. Тяжёлая индексация (chunk+embed+insert) — В ФОН: нужна только будущему
    #    semantic_search, не этому запросу. Агент не ждёт.
    #
    # Передаём УЖЕ СКАЧАННОЕ. Раньше фон качал ту же страницу заново и всегда
    # без браузера — а сайты банков это SPA с антиботом: по HTTP они отдают
    # 91 символ «Please enable JavaScript… Your support ID». Именно эта заглушка
    # и оседала в базе вместо тарифов: из 193 страниц sberbank.ru осмысленными
    # были 8. Теперь в базу ложится ровно то, что прочитал агент.
    from ...rag import ingest_queue
    ingest_queue.submit(url, bank_slug_hint=bank_slug_hint,
                        prefer_browser=_render,
                        content=_content, content_type=_ctype, final_url=_final,
                        origin=_origin_ctx())

    return {"title": title, "text": text, "document_id": None, "indexed": False,
            "fetch_via": _fetch_via, "captcha": _captcha, "status": _status,
            "skipped_reason": skipped_reason,
            # full — полный очищенный текст страницы: НЕ для промпта, а для
            # проверки цитат и будущего recall по требованию.
            "full": full_text,
            "file_links": file_links, "section_links": section_links}


# Происхождение текущего чтения: кто и ради какого вопроса читает страницу.
# Через contextvars, чтобы не тащить эти поля через шесть сигнатур подряд —
# от эндпоинта чата до инструмента агента.
def _origin_ctx() -> dict:
    try:
        from ...web.runctx import current_origin
        return current_origin()
    except Exception:
        return {"kind": "report"}


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


# Секции, которые в выдачу попадают ЦЕЛИКОМ, минуя отбор по релевантности:
# таблицы — самые доказательные данные страницы, а дописываются они в конец,
# поэтому любое усечение по объёму било именно по ним.
_ALWAYS_KEEP = ("# Таблицы страницы", "# Таблицы документа")
_UI_TAIL = "# Элементы интерфейса"


def _split_sections(text: str) -> tuple[str, str, str]:
    """Разделяет текст на (основной, обязательные таблицы, служебный хвост)."""
    tail = ""
    if _UI_TAIL in text:
        text, _, tail_part = text.partition(_UI_TAIL)
        tail = _UI_TAIL + tail_part
    keep = ""
    for marker in _ALWAYS_KEEP:
        if marker in text:
            text, _, keep_part = text.partition(marker)
            keep = marker + keep_part
            break
    return text, keep, tail


def _join_sections(*parts: str) -> str:
    """Склейка секций через пустую строку: без неё последняя строка окна
    слипалась с заголовком таблицы и переставала быть дословной подстрокой
    исходника — сверка цитат такую строку уже не находила."""
    return "\n\n".join(p.strip("\n") for p in parts if p and p.strip())


def _relevant_excerpt(text: str, query_hint: str, budget: int) -> str:
    """Окна текста, наиболее релевантные вопросу.

    Отбор ЭКСТРАКТИВНЫЙ: куски копируются дословно, поэтому сверка цитат по
    полному тексту источника (этап 2) продолжает работать, а число не может
    «переехать» к чужой подписи. Границы окон — по переводам строк: раньше рез
    шёл по символам и разрывал строку таблицы посередине.
    """
    body, tables, ui_tail = _split_sections(text or "")
    reserve = len(tables) + len(ui_tail)
    body_budget = max(600, budget - reserve)

    terms = [w.lower() for w in re.split(r"\W+", query_hint or "") if len(w) >= 4]
    lines = body.split("\n")
    if not terms or len(body) <= body_budget:
        return _join_sections(body[:body_budget], tables, ui_tail)[:budget]

    win = int(os.getenv("V2_EXCERPT_WINDOW", "1200"))
    # Окна по строкам, а не по символам: строка таблицы или «Ставка — 16,5%»
    # не должна рваться пополам.
    windows, cur, cur_len, start_idx = [], [], 0, 0
    for i, line in enumerate(lines):
        cur.append(line)
        cur_len += len(line) + 1
        if cur_len >= win:
            windows.append((start_idx, "\n".join(cur)))
            # шаг с перекрытием: условие получения ставки часто стоит абзацем
            # ниже самого числа, окна не должны разлучать их
            back = max(1, len(cur) // 4)
            cur, cur_len, start_idx = cur[-back:], sum(len(x) + 1 for x in cur[-back:]), i - back + 1
    if cur:
        windows.append((start_idx, "\n".join(cur)))

    # Вес числа зависит от ТИПА ВОПРОСА, а не задан раз и навсегда. Вопрос
    # «сравни ставки» и вопрос «как оформить карту, где проще» требуют разного:
    # во втором числа почти не нужны, важны шаги, требования и оговорки. Ставка
    # чисел выше ключевых слов ВСЕГДА выбрасывала бы описание процесса в пользу
    # таблицы тарифов.
    _proc_markers = ("как ", "процесс", "оформ", "получ", "открыт", "закрыт",
                     "требован", "документ", "услови", "порядок", "шаг",
                     "проще", "сложн", "срок рассмотр", "отказ", "жалоб",
                     "поддержк", "приложени", "онлайн", "офис", "заявк")
    _q = (query_hint or "").lower()
    _process_q = any(m in _q for m in _proc_markers)
    _num_w = int(os.getenv("V2_EXCERPT_NUM_WEIGHT",
                           "2" if _process_q else "6"))
    # Слова-маркеры процедурного текста: по ним узнаём абзац с описанием шагов,
    # даже если в нём нет ни одной цифры.
    _proc_words = ("нужно", "необходимо", "потребуется", "заявк", "документ",
                   "паспорт", "анкет", "подпис", "рассмотр", "решение",
                   "одобрен", "отказ", "шаг", "этап", "онлайн", "в офисе",
                   "в приложении", "курьер", "доставк", "активац")
    scored = []
    for start, chunk in windows:
        low = chunk.lower()
        n_terms = sum(low.count(t) for t in terms)
        n_nums = len(re.findall(r"\d[\d .,]*\s*(?:₽|руб|%|мес|год|дн)", chunk))
        n_proc = sum(1 for w in _proc_words if w in low)
        score = n_nums * _num_w + n_terms * 2 + n_proc * (4 if _process_q else 1)
        if (n_nums or n_proc) and n_terms:
            score += 5              # и по теме, и с содержанием — лучший случай
        scored.append((score, start, chunk))
    if not scored:
        return _join_sections(body[:body_budget], tables, ui_tail)[:budget]

    scored.sort(key=lambda x: -x[0])
    picked, total = [], 0
    for score, start, chunk in scored:
        # Проверяем ДО добавления: иначе окно перевешивало бюджет и срезалось
        # финальной обрезкой по символам — причём срезалось самое релевантное,
        # если оно стояло в середине страницы (после сортировки по позиции).
        if total + len(chunk) > body_budget:
            if picked:
                continue
            chunk = chunk[:body_budget]        # одно окно и то не влезло
        picked.append((start, chunk))
        total += len(chunk)
        if total >= body_budget:
            break
    picked.sort(key=lambda x: x[0])
    return _join_sections("\n…\n".join(c for _, c in picked), tables, ui_tail)


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
