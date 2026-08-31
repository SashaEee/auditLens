"""Web research tools — обёртки над web_search + fetcher + passive indexing.

Это «руки» автономных агентов. Каждый вызов:
  1. ищет/читает в web
  2. возвращает текст LLM
  3. пассивно индексирует найденное в БД (document) + регистрирует в SourceRegistry

БД = кэш: завтра тот же запрос найдёт этот документ через semantic_search.
"""
from __future__ import annotations

import json
import logging
import os
import re
from urllib.parse import urlparse

from .source_registry_helper import register_source

log = logging.getLogger(__name__)


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


# ── Источники, которые не могут быть основанием для аудиторского вывода ──────
# Поддомены банка, ведущие ЧУЖОЙ бизнес: бронирование отелей, билеты, вакансии.
# Формально это домен банка, по сути — другой сайт. Из-за суффиксного правила
# hotels.tinkoff.ru приписывался Т-Банку и попадал в отчёты про автокредиты
# (пять раз в 125 отчётах).
_OFFTOPIC_SUB = ("hotels", "travel", "avia", "tickets", "turizm", "otel",
                 "job", "jobs", "career", "vacancy", "hr", "try", "promo",
                 "market", "shop", "store")
# Доски объявлений и рекламные платформы: тарифы банка там не публикуются.
_OFFTOPIC_HOSTS = ("avito.ru", "youla.ru", "farpost.ru", "elama.ru",
                   "irecommend.ru", "otzovik.com", "pikabu.ru", "dzen.ru",
                   "livejournal.com")
# Пользовательские разделы: запись частного лица — не источник для аудита.
_UGC_PATH_RE = re.compile(r"/(?:user|users|profile|blogs?/[^/]*user)/", re.I)


def is_offtopic_source(domain: str, url: str = "") -> str | None:
    """Причина, по которой источник не годится для аудита, либо None."""
    d = (domain or "").lower().removeprefix("www.")
    if any(d == h or d.endswith("." + h) for h in _OFFTOPIC_HOSTS):
        return "доска объявлений или рекламная площадка"
    head = d.split(".")[0]
    if head in _OFFTOPIC_SUB and d.count(".") >= 2:
        return f"поддомен «{head}» — другой бизнес, не продукты банка"
    if _UGC_PATH_RE.search(url or ""):
        return "запись частного пользователя, а не редакционный материал"
    return None


def _trust_for(domain: str, url: str) -> float:
    """Эвристика доверия по домену/URL (для SourceRegistry)."""
    # www. отрезаем: ниже банки и агрегаторы сверялись ТОЧНЫМ совпадением, а
    # домен почти всегда приходит как www.sberbank.ru — официальный сайт банка
    # получал вес неизвестного сайта (0.55 вместо 0.92), агрегатор 0.55 вместо
    # 0.7. Регуляторы не пострадали только потому, что у них суффиксная проверка.
    d = domain.lower().removeprefix("www.")
    url_l = url.lower()
    # Офтоп-поддомен банка и площадки объявлений — не первоисточник ни в каком
    # смысле; ставим низко, чтобы forced-read и ранжирование их не поднимали.
    if is_offtopic_source(d, url):
        return 0.2
    # Регуляторные
    reg = ("cbr.ru", "pravo.gov.ru", "consultant.ru", "garant.ru", "fas.gov.ru",
           "nalog.gov.ru", "minfin.gov.ru", "kremlin.ru", "government.ru",
           "notariat.ru", "sfr.gov.ru", "mil.ru", "asv.org.ru", "sbp.nspk.ru",
           "rospotrebnadzor.ru", "fedsfm.ru", "rosstat.gov.ru", "gks.ru",
           "fincult.info")
    if any(r == d or d.endswith("." + r) for r in reg):
        return 0.98
    if d.endswith(".gov.ru") or d.endswith(".mil.ru"):
        return 0.95
    # Офиц. сайты банков (по домену 2 уровня)
    bank_domains = ("sberbank.ru", "vtb.ru", "alfabank.ru", "tbank.ru", "tinkoff.ru",
                    "sovcombank.ru", "gazprombank.ru", "rshb.ru", "domrfbank.ru",
                    "open.ru", "raiffeisen.ru", "pochtabank.ru", "mkb.ru",
                    "psbank.ru", "rosbank.ru", "mtsbank.ru", "ozon.ru",
                    "uralsib.ru", "akbars.ru", "homecredit.ru", "otpbank.ru",
                    "unicreditbank.ru", "absolutbank.ru", "zenit.ru", "rencredit.ru")
    if any(d == bd or d.endswith("." + bd) for bd in bank_domains):
        return 0.92
    # Агрегаторы (высокая, но не первоисточник)
    agg = ("banki.ru", "sravni.ru", "bankiros.ru", "sravni.com")
    if any(d == a or d.endswith("." + a) for a in agg):
        return 0.7
    media = ("forbes.ru", "rbc.ru", "tass.ru", "vedomosti.ru", "kommersant.ru",
             "frankmedia.ru", "interfax.ru")
    if any(d == m or d.endswith("." + m) for m in media):
        return 0.6
    # vc.ru — площадка пользовательских блогов, а не редакция: ниже медиа
    if d == "vc.ru" or d.endswith(".vc.ru"):
        return 0.35
    # Отзовики/пользовательский контент
    if any(d.endswith(x) for x in ("irecommend.ru", "otzovik.com", "vk.com")):
        return 0.5
    # PDF — модификатор ВНУТРИ класса неизвестного домена, а не ранний return:
    # прежний глобальный «*.pdf → 0.9» делал PDF с vc.ru доверием почти как
    # офсайт банка. Официальный документ на неизвестном домене — чуть выше
    # безвестной страницы, но никак не уровень регулятора.
    if url_l.endswith(".pdf"):
        return 0.65
    return 0.55


# ════════════════════════════════════════════════════════════════════════
# TOOL: web_search
# ════════════════════════════════════════════════════════════════════════


def tool_web_search(args: dict, bundle) -> str:
    """Поиск в web через multi-backend (SearXNG/Brave/ddgs/ddg/yandex).

    Возвращает список {title, url, snippet, domain}. НЕ скачивает содержимое —
    только метаданные SERP. Для чтения страницы вызови read_url.
    """
    from ....rag.web_search import search as _ws
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query пустой"}, ensure_ascii=False)
    max_results = int(args.get("max_results", 8))
    site_filter = args.get("site_filter")  # ["sberbank.ru", "banki.ru", ...]

    try:
        results = _ws(query, max_results=max_results,
                       site_filter=site_filter) or []
    except Exception as e:
        log.warning("web_search %r failed: %s", query[:60], e)
        return json.dumps({"error": f"search failed: {e}"}, ensure_ascii=False)

    out = []
    for r in results:
        url = r.get("url") or ""
        if not url.startswith("http"):
            continue
        dom = r.get("domain") or _domain(url)
        out.append({
            "title": (r.get("title") or "")[:200],
            "url": url,
            "snippet": (r.get("snippet") or "")[:500],
            "domain": dom,
            "trust": round(_trust_for(dom, url), 2),
        })
    return json.dumps({"query": query, "results": out, "count": len(out)},
                      ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
# TOOL: read_url — скачать страницу/PDF, вернуть текст + пассивный индекс
# ════════════════════════════════════════════════════════════════════════


def tool_read_url(args: dict, bundle) -> str:
    """Скачать URL (HTML/PDF), распарсить, вернуть релевантный текст.

    Side effect: документ пассивно индексируется в БД → будущие запросы найдут
    его через semantic_search. Также регистрируется в SourceRegistry bundle.

    Возвращает {title, text, domain, source_n, trust}. text укорочен до
    budget_chars для промпта.
    """
    url = (args.get("url") or "").strip()
    if not url:
        return json.dumps({"error": "url пустой"}, ensure_ascii=False)
    query_hint = (args.get("query") or "").strip()  # для релевантной выборки
    # Раньше 6000 симв/страница: индексатор парсил страницу целиком, а модель
    # видела лишь ~1500 токенов (фрагмент по ключевику) — тариф-PDF/длинная
    # страница отзывов теряли основное содержимое. Поднято (контекст модели
    # огромный, страница уже скачана — отдавать больше почти бесплатно).
    budget = int(args.get("budget_chars",
                          int(os.getenv("V2_READ_BUDGET_CHARS", "12000"))))
    bank_slug_hint = args.get("bank_slug")

    dom = _domain(url)
    trust = _trust_for(dom, url)
    kind = _kind_for(dom, url)

    # Пассивная индексация (best-effort, не блокирует ответ)
    text = ""
    title = ""
    idx: dict = {}
    try:
        from ..passive_indexer import index_and_get_text
        idx = index_and_get_text(url, bank_slug_hint=bank_slug_hint,
                                  query_hint=query_hint, budget=budget)
        text = idx.get("text", "")
        title = idx.get("title", "")
    except Exception as e:
        log.info("passive index failed for %s: %s — raw fetch", url[:80], e)
        # Fallback: прямой fetch без индексации
        try:
            text = _raw_fetch_text(url, budget)
        except Exception as e2:
            return json.dumps({"error": f"fetch failed: {e2}"},
                              ensure_ascii=False)

    # СТАТУС ОТВЕТА — ДО текста. Раньше статус смотрели только когда текст
    # пуст, а страница «404 Страница не найдена» или заглушка антибота пустой
    # не бывает: она уходила в источники как полноценный документ. В отчёте 88
    # так появились три источника вида «Страница не найдена — banki.ru/.../cbr/».
    status = idx.get("status")
    if isinstance(status, int) and status >= 400:
        if status in (401, 403, 429, 503):
            _note_blocked_source(bundle, url, dom, bank_slug_hint)
            return json.dumps({"error": f"источник недоступен (HTTP {status}) — "
                               "помечено в пробелах покрытия",
                               "url": url, "blocked": True}, ensure_ascii=False)
        return json.dumps({"error": f"страницы нет (HTTP {status})", "url": url},
                          ensure_ascii=False)
    if text and _looks_like_stub(title, text):
        # Статус 200, но содержимое — «страница не найдена» или проверка
        # браузера. Такой текст в отчёте выглядит как факт с ссылкой [N].
        _note_blocked_source(bundle, url, dom, bank_slug_hint)
        return json.dumps({"error": "страница-заглушка (404/антибот при HTTP 200) — "
                           "помечено в пробелах покрытия",
                           "url": url, "blocked": True}, ensure_ascii=False)

    if not text:
        # Различаем «просто нет данных» и «заблокировано антиботом/капчей».
        # Блок → фиксируем в coverage_notes, чтобы источник честно попал в
        # «Пробелы покрытия», а не молча урезал отчёт.
        blocked = (bool(idx.get("captcha")) or idx.get("fetch_via") == "failed"
                   or status in (403, 429, 503))
        if blocked:
            _note_blocked_source(bundle, url, dom, bank_slug_hint)
            return json.dumps({"error": "источник заблокирован антиботом/капчей — "
                               "данные недоступны, помечено в пробелах покрытия",
                               "url": url, "blocked": True,
                               # Капча на офсайте ≠ данных нет: банки дублируют
                               # условия в прессе. Агент должен свернуть туда,
                               # а не сдаваться (кейс domrfbank: продуктовая
                               # страница за капчей, все цифры — в пресс-релизах).
                               "next_step": "страница за капчей, но условия "
                               "почти наверняка есть в пресс-релизах банка и "
                               "отраслевых СМИ: сделай web_search "
                               "«<банк> <продукт> пресс-релиз условия» или "
                               "«<банк> запустил <продукт>» и читай их"},
                              ensure_ascii=False)
        # Честная причина вместо дежурной «пустой страницы»: агент, знающий
        # что «формат .doc не поддержан», не будет ретраить URL и запишет
        # осмысленный пробел покрытия.
        reason = (idx.get("skipped_reason") or "").strip()
        return json.dumps({"error": reason or "пустой текст (404/пустая SPA)",
                           "url": url}, ensure_ascii=False)

    # Регистрируем источник в bundle
    # Доказательная база — полный текст страницы, а НЕ то, что увидела модель.
    # Объём ограничиваем отдельной ручкой: это память процесса, не токены.
    _full = idx.get("full") or text
    src_n = register_source(bundle, url=url, title=title, domain=dom,
                              trust=trust, kind=kind, excerpt=text[:600],
                              fulltext=_full[:int(os.getenv(
                                  "V2_SOURCE_FULLTEXT_CHARS", "120000"))])

    resp = {
        "url": url, "title": title, "domain": dom,
        "text": text[:budget], "trust": round(trust, 2),
        "source_n": src_n,
    }
    # НАВИГАЦИЯ НА ШАГ ГЛУБЖЕ. Регуляторы и банки публикуют первоисточники
    # ФАЙЛАМИ, а страница-каталог несёт только ссылки: её текст пуст по сути,
    # и агент объявлял «данных нет», хотя нужный xlsx лежал в разметке. Даём
    # ему ссылки-кандидаты — приоритет файлам и разделам, чьи анкоры пересекаются
    # со словами задания.
    _q = (query_hint or "") + " " + (getattr(bundle, "question", "") or "")
    _words = {w for w in re.findall(r"[а-яёa-z0-9]{4,}", _q.lower())}

    def _score(item: dict) -> int:
        a = (item.get("anchor") or "").lower()
        return sum(1 for w in _words if w in a)

    _files = sorted(idx.get("file_links") or [], key=_score, reverse=True)[:8]
    _secs = [x for x in (idx.get("section_links") or []) if _score(x) > 0]
    _secs = sorted(_secs, key=_score, reverse=True)[:8]
    if _files:
        resp["file_links"] = _files
    if _secs:
        resp["section_links"] = _secs
    if _files or _secs:
        resp["navigation_hint"] = (
            "Если ответа в тексте страницы нет — это может быть оглавление. "
            "Значения и таблицы часто лежат в ФАЙЛАХ по ссылкам выше "
            "(read_url читает xlsx/pdf/docx) или на подстранице раздела. "
            "Открой самый подходящий по названию, а не пиши «данных нет».")
    return json.dumps(resp, ensure_ascii=False)


# Заглушки, которые сайты отдают с кодом 200: «страница не найдена», проверка
# браузера, доступ ограничен. Признак — короткий текст И характерный заголовок.
_STUB_RE = re.compile(
    r"страниц\w*\s+не\s+найден|not\s+found|ошибка\s*40[34]|"
    r"доступ\s+(?:ограничен|запрещ)|access\s+denied|forbidden|"
    r"проверка\s+браузера|checking\s+your\s+browser|just\s+a\s+moment|"
    r"включите\s+javascript|enable\s+javascript|подтвердите,\s+что\s+запросы",
    re.IGNORECASE)
_STUB_MAX_CHARS = 1200


def _looks_like_stub(title: str, text: str) -> bool:
    """Страница-заглушка при HTTP 200: короткая и с характерной фразой."""
    head = f"{title or ''} {(text or '')[:400]}"
    if not _STUB_RE.search(head):
        return False
    # Длинная статья, где фраза встретилась в тексте, заглушкой не считается.
    return len(text or "") <= _STUB_MAX_CHARS


def _kind_for(domain: str, url: str) -> str:
    d = domain.lower()
    reg = ("cbr.ru", "pravo.gov.ru", "consultant.ru", "garant.ru", "fas.gov.ru",
           "nalog.gov.ru", "minfin.gov.ru", "kremlin.ru", "government.ru",
           "notariat.ru", "sfr.gov.ru", "mil.ru", "asv.org.ru", "sbp.nspk.ru",
           "rospotrebnadzor.ru", "fedsfm.ru", "rosstat.gov.ru", "gks.ru",
           "fincult.info")
    if any(r == d or d.endswith("." + r) or d.endswith(".gov.ru") for r in reg):
        return "regulatory"
    bank_domains = ("sberbank.ru", "vtb.ru", "alfabank.ru", "tbank.ru", "tinkoff.ru",
                    "sovcombank.ru", "gazprombank.ru", "rshb.ru", "domrfbank.ru",
                    "open.ru", "raiffeisen.ru", "pochtabank.ru", "mkb.ru",
                    "psbank.ru", "rosbank.ru", "mtsbank.ru", "uralsib.ru",
                    "akbars.ru", "homecredit.ru", "otpbank.ru", "unicreditbank.ru",
                    "absolutbank.ru", "zenit.ru", "rencredit.ru")
    if any(bd == d for bd in bank_domains):
        return "bank_official"
    if any(a == d for a in ("banki.ru", "sravni.ru", "bankiros.ru")):
        return "aggregator"
    if any(d.endswith(x) for x in ("irecommend.ru", "otzovik.com")):
        return "review"
    if any(a == d for a in ("vc.ru", "forbes.ru", "rbc.ru", "tass.ru")):
        return "news"
    return "web"


def _raw_fetch_text(url: str, budget: int) -> str:
    """Простой fallback-fetch без индексации (когда indexer не справился)."""
    from ....rag import fetcher
    from ..passive_indexer import _should_render
    fr = fetcher.fetch(url, prefer_browser=_should_render(url))
    if not fr.content:
        return ""
    from ....rag.parsers import parse_auto
    parsed = parse_auto(fr.content, url=fr.final_url, content_type=fr.content_type)
    text = parsed.text or ""
    return text[:budget]


def _note_blocked_source(bundle, url: str, domain: str, bank_hint: str | None) -> None:
    """Фиксирует источник, заблокированный антиботом/капчей, в coverage_notes bundle
    (дедуп по домену). Так он ЧЕСТНО попадёт в «Пробелы покрытия» и «Честные
    оговорки», а не молча урежет отчёт (капча-деградация)."""
    try:
        from ..knowledge_bundle import CoverageNote
        dom = (domain or _domain(url) or url)[:80]
        if dom and any(dom in (getattr(cn, "what", "") or "")
                       for cn in bundle.coverage_notes):
            return
        bundle.coverage_notes.append(CoverageNote(
            what=f"{dom}: источник недоступен для автосбора (антибот/капча)",
            subjects=[bank_hint] if bank_hint else [],
            reason="сайт заблокировал автоматический доступ (challenge/капча) — "
                   "данные с него не собраны",
            recommendation="проверить вручную по URL источника",
        ))
    except Exception as e:
        log.info("note blocked source failed for %s: %s", url[:80], e)


# ════════════════════════════════════════════════════════════════════════
# TOOL: semantic_search — pgvector по уже проиндексированному (кэш БД)
# ════════════════════════════════════════════════════════════════════════


def tool_semantic_search(args: dict, bundle) -> str:
    """Семантический поиск по уже проиндексированным документам в БД.

    Быстро и бесплатно. Используй ПЕРВЫМ — если данные уже есть, не надо
    лезть в web. Если результатов мало (<3) → web_search/read_url.
    """
    query = (args.get("query") or "").strip()
    if not query:
        return json.dumps({"error": "query пустой"}, ensure_ascii=False)
    bank_slugs = args.get("bank_slugs")
    if isinstance(bank_slugs, str):
        bank_slugs = [bank_slugs]
    doc_types = args.get("doc_types")
    trust_min = float(args.get("trust_min", 0.5))
    top_k = int(args.get("top_k", 6))
    try:
        max_age_days = int(args["max_age_days"]) if args.get("max_age_days") else None
    except (TypeError, ValueError):
        max_age_days = None

    try:
        # Гибрид (вектор + полнотекст, RRF) вместо чистого вектора: точные
        # токены «ПСК 24,7%», «п. 4.2», «указание 6960-У» полнотекст находит
        # дословно, а вектор размазывает. Гибрид уже жил в retriever для
        # вкладки «База знаний» — агентам он был просто не подключён.
        from ....rag.retriever import hybrid_search as _hs
        _h = _hs(query, limit=top_k, bank_slugs=bank_slugs,
                  doc_types=doc_types, trust_min=trust_min,
                  max_age_days=max_age_days)
        # Гибрид группирует по документам ({groups:[{hits:[...]}]}) — плоское
        # представление для агента: фрагмент + атрибуты его документа.
        results = []
        for g in (_h.get("groups") or [])[:top_k]:
            for hit in (g.get("hits") or [])[:2]:
                results.append({
                    "url": g.get("url"), "title": g.get("title") or g.get("text_head"),
                    "trust_score": g.get("trust_score"),
                    "text": hit.get("text") or hit.get("snippet"),
                    "headings_path": hit.get("headings_path"),
                    "bank_slug": g.get("bank_slug"),
                    "doc_type": g.get("doc_type"),
                    "fetched_at": str(g.get("fetched_at") or ""),
                })
        results = results[:max(top_k, 8)]
        if not results:
            raise RuntimeError("hybrid: пусто, пробуем вектор")
    except Exception as e:
        try:
            from ....rag.retriever import semantic_search as _ss
            results = _ss(query, top_k=top_k, bank_slugs=bank_slugs,
                           doc_types=doc_types, trust_min=trust_min,
                           max_age_days=max_age_days,
                           exclude_sponsored=True)
        except Exception:
            return json.dumps({"error": f"semantic_search failed: {e}"},
                              ensure_ascii=False)

    out = []
    for r in results:
        url = r.get("url") or ""
        dom = _domain(url)
        # Регистрируем источник
        # Сверка цитат идёт по fulltext: без него цитата из хвоста фрагмента
        # (модель видит 1500 символов, база сверки была 600) объявлялась
        # неподтверждённой — ложные срабатывания антигаллюцинаций.
        src_n = register_source(bundle, url=url,
                                  title=r.get("title", "") or url[:80],
                                  domain=dom,
                                  fulltext=(r.get("text") or "")[:8000],
                                  trust=float(r.get("trust_score") or 0.6),
                                  kind=_kind_for(dom, url),
                                  excerpt=(r.get("text") or "")[:600])
        out.append({
            "text": (r.get("text") or "")[:1500],
            "headings_path": r.get("headings_path"),
            "bank_slug": r.get("bank_slug"),
            "url": url,
            "source_n": src_n,
            "trust": round(float(r.get("trust_score") or 0.6), 2),
            "relevance": round(r.get("relevance", 0), 3),
        })
    return json.dumps({"query": query, "results": out, "count": len(out)},
                      ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
# TOOL: search_reviews_db — семантический поиск жалоб в корпусе banki.ru
# ════════════════════════════════════════════════════════════════════════


def _sentiment_stats_from_db(banks: list[str]) -> dict:
    """Полнокорпусные агрегаты по банкам (v_review_sentiment_share).

    Вьюха отдаёт total и ДОЛЮ НЕГАТИВА — корпус banki.ru смещён в жалобы,
    поэтому pos/neu из него не выводимы вовсе; это тоже часть правды, которую
    агент обязан знать, а не досочинять.
    """
    from sqlalchemy import text as _t
    from .... import db as _db
    out: dict = {}
    with _db.session() as s:
        rows = s.execute(_t(
            "SELECT bank_name, neg_share, total "
            "FROM v_review_sentiment_share")).mappings().all()
    want = {b.lower() for b in banks}
    for r in rows:
        name = r["bank_name"] or ""
        if want and not any(w in name.lower() or name.lower() in w for w in want):
            continue
        out[name] = {"total": int(r["total"] or 0),
                     "neg_share": round(float(r["neg_share"] or 0), 3),
                     "note": "pos/neu по корпусу не определимы (корпус жалоб)"}
    return out


def tool_search_reviews_db(args: dict, bundle) -> str:
    """Семантический поиск реальных жалоб в корпусе banki.ru (БД bankiru).

    ~390k негативных отзывов (1-2★) за 2025-2026 по 217 банкам, с датами/ссылками
    и готовыми bge-m3 эмбеддингами. Это ОСНОВНОЙ источник жалоб — web нужен лишь
    для банков вне корпуса. Регистрирует найденные отзывы как источники [N].
    """
    query = (args.get("query") or "").strip() or None
    bank = args.get("bank") or args.get("bank_slug") or args.get("bank_name")
    product = args.get("product")
    since_days = args.get("since_days")
    try:
        k = int(args.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    # ── НЕСКОЛЬКО БАНКОВ: точечный поиск по каждому отдельно (надёжнее global) ──
    raw_banks = args.get("banks")
    banks = None
    if isinstance(raw_banks, list) and raw_banks:
        banks = raw_banks
    elif isinstance(raw_banks, str) and raw_banks.strip():
        banks = [x.strip() for x in raw_banks.split(",") if x.strip()]
    elif isinstance(bank, list) and len(bank) > 1:
        banks = bank
    elif isinstance(bank, str) and "," in bank:
        banks = [x.strip() for x in bank.split(",") if x.strip()]
    if isinstance(bank, list):
        bank = bank[0] if bank else None
    # Если LLM не задал банк(и) явно — берём АНАЛИЗИРУЕМЫЕ банки из задания
    # (bundle.subjects, определённые пайплайном из вопроса/плана), а НЕ глобальный
    # поиск по всем. Гарантирует точечный поиск ровно по тем банкам, что в работе,
    # а не по произвольным. (Тот же источник банков, что у web-поиска: # ОБЪЕКТЫ.)
    if not banks and not bank:
        subj = list(getattr(bundle, "subjects", None) or [])
        if subj:
            labels = getattr(bundle, "subject_labels", None) or {}
            banks = [labels.get(s, s) for s in subj]
    from ....rag import bankiru_reviews as br
    if banks:
        kp = max(k, 6)
        try:
            by = br.search_reviews_multi(query, banks=banks, product=product,
                                         since_days=since_days, k_per=kp)
        except Exception as e:
            return json.dumps({"error": f"reviews_db multi failed: {e}"}, ensure_ascii=False)
        out_by, counts, total = {}, {}, 0
        for bnk, revs in by.items():
            arr = []
            for r in revs:
                title = " · ".join(x for x in ["banki.ru", r.get("bank"),
                                               r.get("product"), r.get("date")] if x)
                src_n = register_source(bundle, url=r.get("url") or "", title=title,
                                        domain="banki.ru", trust=0.55, kind="review",
                                        excerpt=(r.get("text") or "")[:600],
                                        fulltext=(r.get("text") or "")[:8000])
                arr.append({"product": r.get("product"), "date": r.get("date"),
                            "url": r.get("url"), "source_n": src_n,
                            "text": (r.get("text") or "")[:900],
                            "relevance": round(1.0 - float(r.get("distance", 0) or 0), 3)})
            out_by[bnk] = arr
            counts[bnk] = len(arr)
            total += len(arr)
        empties = [b for b, v in counts.items() if not v]
        resp = {"mode": "per_bank", "query": query, "by_bank": out_by,
                "counts": counts, "total": total}
        # НАСТОЯЩИЕ агрегаты из БД. Раньше схема финала reviews-агента требовала
        # total и доли pos/neu/neg, и модель экстраполировала их по 12-15
        # отзывам чисто негативного корпуса — отчёт публиковал выдуманную
        # «статистику». Даём посчитанное по всему корпусу, чтобы копировала.
        try:
            db_stats = _sentiment_stats_from_db(banks)
            if db_stats:
                resp["db_sentiment_stats"] = db_stats
                resp["db_stats_note"] = (
                    "Агрегаты (total/доли) в sentiment_profiles бери ТОЛЬКО "
                    "отсюда — это полный корпус БД, а не твоя выборка. Выборка "
                    "выше — для цитат и тем, по ней доли НЕ оценивать.")
        except Exception:
            pass
        if empties:
            resp["empty_banks_note"] = ("Без жалоб в корпусе по этой теме: " + ", ".join(empties) +
                                        " (возможно, банк вне корпуса banki.ru или нет данных по теме).")
        return json.dumps(resp, ensure_ascii=False)
    # ── одиночный банк / общий рыночный срез ──
    if not query and not bank:
        return json.dumps({"error": "нужен bank/banks (для discovery) или query"},
                          ensure_ascii=False)
    # discovery (без темы) — отдаём больше, чтобы из них проступили темы
    if not query:
        k = max(k, 15)
    try:
        results = br.search_reviews(query, bank=bank, product=product,
                                     since_days=since_days, k=k)
    except Exception as e:
        return json.dumps({"error": f"reviews_db failed: {e}"}, ensure_ascii=False)

    if not results:
        if bank:
            note = ("По этому банку жалоб не нашлось. Если задавал query — повтори БЕЗ query "
                    "(discovery по банку, темы проступят сами). Если и так пусто — банк вне "
                    "корпуса banki.ru (217), тогда web_search.")
        else:
            note = "Запрос без bank вернул пусто. Для оценки конкретного банка передай bank=<банк>."
        return json.dumps({"query": query, "bank": bank, "results": [], "count": 0,
                           "note": note}, ensure_ascii=False)

    out = []
    for r in results:
        title = " · ".join(x for x in ["banki.ru", r.get("bank"),
                                        r.get("product"), r.get("date")] if x)
        src_n = register_source(bundle, url=r.get("url") or "",
                                 fulltext=(r.get("text") or "")[:8000],
                                 title=title, domain="banki.ru",
                                 trust=0.55, kind="review",
                                 excerpt=(r.get("text") or "")[:600])
        out.append({
            "bank": r.get("bank"), "product": r.get("product"),
            "date": r.get("date"), "url": r.get("url"), "source_n": src_n,
            "text": (r.get("text") or "")[:900],
            "relevance": round(1.0 - float(r.get("distance", 0) or 0), 3),
        })
    resp = {"query": query, "bank": bank, "results": out, "count": len(out)}
    if not bank:
        # бесбанковый семантический поиск = общий рыночный top-k, НЕ покрывает все
        # банки → запрет выводить отсутствие жалоб у конкретного банка
        resp["note"] = ("ВНИМАНИЕ: запрос без bank — общий рыночный top-k по теме, структурно "
                        "НЕ покрывает все 217 банков (банк может быть в корпусе, но не попасть "
                        "в top-k). НЕ делай вывод, что у банка нет жалоб. Для КАЖДОГО банка "
                        "вызови search_reviews_db отдельно с bank=<банк>.")
    return json.dumps(resp, ensure_ascii=False)


# ════════════════════════════════════════════════════════════════════════
# TOOL: run_sql — read-only SQL по предзаданным view/таблицам
# ════════════════════════════════════════════════════════════════════════


def tool_run_sql(args: dict, bundle) -> str:
    """Read-only SELECT по предзаданным представлениям/таблицам.

    Доступно: v_offer_current, v_sber_vs_market, v_review_topics,
    v_review_sentiment_share, v_bank_coverage, bank, review, review_topic,
    review_sentiment, product_offer, product_terms, quality_flag,
    change_history.

    Запрещено: всё кроме SELECT/WITH. LIMIT обязателен.
    """
    from ....ai.analyst import _run_sql_safe
    sql = (args.get("sql") or "").strip()
    if not sql:
        return json.dumps({"error": "sql пустой"}, ensure_ascii=False)
    raw = _run_sql_safe(sql)
    # БД платформы — полноценный источник. Без [N] researcher._integrate
    # ВЫБРАСЫВАЕТ факт (source_n<=0 → continue): самые доверенные числа —
    # из собственной структурной базы — не доезжали до отчёта, либо агент
    # приписывал им ссылку чужой веб-страницы.
    n = register_source(
        bundle, url=f"internal://auditlens/sql?q={sql[:120]}",
        title="База данных AuditLens (структурные тарифы и отзывы)",
        domain="auditlens.internal", trust=0.95, kind="internal_db",
        excerpt=raw[:600])
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    if isinstance(data, dict):
        data["source_n"] = n
        data["source_note"] = (f"Данные БД AuditLens. Для фактов из этого "
                               f"ответа указывай source_n={n}.")
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({"rows": data, "source_n": n,
                       "source_note": f"Для фактов из этого ответа указывай source_n={n}."},
                      ensure_ascii=False)


# ── ПОЗИЦИЯ НА РЫНКЕ: тот же ответ, что на вкладке «Рынок» ───────────────────
# Пересчитывать ранг самому агенту нельзя: методология витрины — это ПСК вместо
# рекламной ставки, сегменты, отсев господдержки и не-банков, защита от
# вырожденной метрики. Повторить это запросом агент не сможет, а разойтись с
# экраном — сможет, и аудитор увидит два разных числа на один вопрос.

def tool_market_position(args: dict, bundle) -> str:
    """Готовая позиция Сбера в категории — ровно как показывает витрина."""
    category = (args.get("category") or "").strip()
    segment = (args.get("segment") or "").strip() or None
    sub = (args.get("sub_segment") or "").strip() or None
    try:
        from ....web.app import market_atlas
        atlas = market_atlas()
    except Exception as e:  # noqa: BLE001 — витрина не должна ронять исследование
        return json.dumps({"error": f"витрина недоступна: {str(e)[:120]}"},
                          ensure_ascii=False)

    cats = [c for c in (atlas.get("categories") or []) if c.get("status") == "ok"]
    if category:
        cats = [c for c in cats if c.get("category") == category]
        if not cats:
            avail = [c.get("category") for c in (atlas.get("categories") or [])]
            return json.dumps({"error": f"категории «{category}» на витрине нет",
                               "available": avail}, ensure_ascii=False)

    out = []
    for c in cats:
        sb = c.get("sber") or {}
        item = {
            "category": c["category"], "label": c.get("label"),
            "metric": c.get("metric"), "metric_label": c.get("metric_label"),
            "metric_unit": c.get("metric_unit"),
            "lower_is_better": c.get("lower_is_better"),
            "banks_in_comparison": c.get("n_banks"),
            "median": c.get("median"), "p25": c.get("p25"), "p75": c.get("p75"),
            "leader": c.get("leader"),
            # паспорт выборки: без него ранг нельзя цитировать честно
            "sample": {k: c.get(k) for k in
                       ("banks_total", "banks_dropped", "no_metric", "teaser",
                        "subsidized_excluded", "non_bank_excluded", "at_best",
                        "psk_fallback", "small_n", "degenerate")},
        }
        if sb:
            item["sber"] = {k: sb.get(k) for k in
                            ("rank", "rate", "title", "percentile",
                             "tied", "gap_median", "gap_leader")}
            # «value» — понятное имя для агента: это значение метрики категории
            # (ПСК для кредитов, плата для карт, грейс для кредиток)
            item["sber"]["value"] = sb.get("rate")
        if c.get("degenerate"):
            item["warning"] = ("метрика не различает банки: на лучшем значении "
                               f"{c.get('at_best')} из {c.get('n_banks')} — "
                               "ранг цитировать нельзя")
        # разрезы: ответ «где мы среди новостроек» честнее общего по категории
        groups = c.get("groups") or []
        if segment or sub:
            groups = [g for g in groups
                      if (not segment or g.get("segment") == segment)
                      and (not sub or g.get("sub_segment") == sub)]
        if groups:
            item["groups"] = [
                {k: g.get(k) for k in ("segment", "sub_segment", "n_banks",
                                       "median", "leader", "small_n", "sber")}
                for g in groups[:6]]
        if c.get("free_split"):
            item["free_split"] = c["free_split"]
        if c.get("attainability"):
            item["rate_attainability"] = c["attainability"]
        out.append(item)
    # Витрина — источник с максимальным доверием: её числа аудитор видит на
    # экране. Без [N] факты агента из этого ответа выбрасывались в _integrate.
    n = register_source(
        bundle, url="internal://auditlens/market-atlas",
        title="Витрина «Рынок» AuditLens (ПСК-методология, сегменты)",
        domain="auditlens.internal", trust=0.95, kind="internal_view",
        excerpt=json.dumps({"as_of": atlas.get("as_of"),
                            "categories": [c.get("category") for c in cats]},
                           ensure_ascii=False, default=str)[:400])
    return json.dumps({"as_of": atlas.get("as_of"), "categories": out,
                       "source_n": n,
                       "source_note": (f"Числа витрины AuditLens. Для фактов "
                                       f"из этого ответа указывай source_n={n}.")},
                      ensure_ascii=False, default=str)
