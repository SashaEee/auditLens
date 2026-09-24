"""LLM-секции дайджеста — 3 вызова/день, числа только из SQL-агрегатов.

Все три вызова идут через insight_model() (env LLM_MODEL_INSIGHT, в проде
anthropic/claude-sonnet-4.6) — умнее для поиска скрытых паттернов; дайджест
кэшируется на день, так что это 3 вызова/сутки на всех.

  reviews_brief — сводка недели по жалобам (~7k in / 0.8k out)
  news          — отбор и сжатие новостей для аудитора розницы (~6k/1.2k)
  headline      — передовица + карточки-инсайты (~2.5k/0.6k), поверх УЖЕ
                  записанных секций; ссылается на сигналы по ref — обогащение
                  (drill/ai_prompt/viz) делает детерминированный python-код,
                  LLM не переписывает числа и URL.

Спец-ключи payload (снимает pipeline): _status, _llm_model, _tokens_in, _tokens_out.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date

from openai import AsyncOpenAI

from ..ai.analyst import (LLM_API_KEY, LLM_BASE_URL, insight_model)
from ..ai.llm_utils import _loose_json_loads, _patch_client_reasoning_effort
from ..clock import today_anchor, today_ru
from sqlalchemy import text

from .. import db
from . import store

log = logging.getLogger(__name__)

_LLM_TIMEOUT = float(os.getenv("DIGEST_LLM_TIMEOUT_S", "90"))


def _client() -> AsyncOpenAI:
    c = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                    max_retries=2, timeout=_LLM_TIMEOUT)
    return _patch_client_reasoning_effort(c)


async def _chat(model: str, system: str, user: str, *,
                max_tokens: int, temperature: float = 0.2) -> tuple[str, int, int]:
    resp = await _client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature, max_tokens=max_tokens)
    content = (resp.choices[0].message.content or "").strip()
    usage = getattr(resp, "usage", None)
    return (content,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0))


# ── reviews_brief ─────────────────────────────────────────────────────────────

_BRIEF_SYSTEM = (
    "Ты — старший аналитик службы внутреннего аудита Сбербанка (розничный бизнес). "
    "Пишешь утреннюю сводку по жалобам клиентов для ежедневного брифинга. НЕ "
    "пересказывай жалобы — дай АНАЛИЗ: что аномально, почему важно, куда смотреть. "
    "Тебе дают точные недельные метрики (НЕ меняй числа) и жалобы каждого сигнала. Сигналы:\n"
    "• рост темы к норме (×N) и УСКОРЕНИЕ — проблема нарастает;\n"
    "• «только у банка» (рынок ровный) → НАША регрессия, высокий приоритет;\n"
    "• гео-концентрация → локальный сбой (отделение/банкомат/регион);\n"
    "• жалобы без точного кода кодификатора → свежий инцидент, которого нет в списке.\n"
    "Без эмодзи, без воды, не алармируй без чисел."
)


async def reviews_brief(day: date) -> dict:
    """Сводка по жалобам для утреннего выпуска.

    Числа — из сигналов недели (LLM-разметка, статистический порог). Причину
    модель ищет ТОЛЬКО в жалобах своего сигнала: раньше ей давали 12 последних
    жалоб банка любых тем, и 22.09 заголовок взял формулировку из чужого
    отзыва о событии 2025 года."""
    from ..rag import reviews_dash as rd
    from ..rag import reviews_llm
    sig = await asyncio.to_thread(rd.weekly_signals, "Сбербанк", None)
    signals = (sig or {}).get("signals") or []
    if not signals:
        return {"markdown": None, "calm": True,
                "overall": (sig or {}).get("overall")}
    lines, ov_line = reviews_llm.signal_lines(sig)
    context = await asyncio.to_thread(reviews_llm.signal_context, sig, "Сбербанк", None)
    user = (
        f"Сводка на {today_ru()}.\n"
        "СИГНАЛЫ НЕДЕЛИ (числа точные, не меняй):\n" + "\n".join(lines) + f"\n{ov_line}\n\n"
        + context + "\n\n"
        "Выдай markdown-список (каждый пункт с «- »):\n"
        "1) 2–4 пункта по приоритету: «**[ВЫСОКИЙ/СРЕДНИЙ]** **<проблема>** — что "
        "изменилось (с цифрой), пометь если *только у банка*/*локально*/*ускоряется*, "
        "вероятная причина — ТОЛЬКО из жалоб этого сигнала, что проверить аудитору».\n"
        "2) Если среди жалоб без точного кода несколько об одном и том же — пункт "
        "«- **Новое:** <суть> (≈N жалоб)».\n"
        "Не переноси формулировки из жалоб одного сигнала в другой, не выдумывай причин. "
        "Коротко, аналитично, без вступления."
    )
    # LLM-сбой → degraded (фронт покажет детерминированные сигнал-чипы),
    # НЕ exception: failed-секция без истории copy_forward держала бы день
    # неполным и провоцировала lazy-перезапуски
    try:
        md, ti, to = await _chat(insight_model(), today_anchor() + "\n\n" + _BRIEF_SYSTEM,
                                 user, max_tokens=1800)
    except Exception as e:  # noqa: BLE001
        log.warning("reviews_brief LLM failed: %s", e)
        md, ti, to = None, None, None
    ov = (sig or {}).get("overall") or {}
    return {"markdown": md or None, "calm": False, "overall": ov,
            **({"_llm_model": insight_model(), "_tokens_in": ti, "_tokens_out": to}
               if md else {"_status": "degraded"})}


# ── news ──────────────────────────────────────────────────────────────────────


# Рубрикатор согласован с аналитиками УВА (фидбек 07.2026): «Сбер» отдельно,
# ставки/экономика слиты в регуляторику, схемы — в инциденты
_NEWS_GROUPS = (("sber", "Сбер: продукты и технологии"),
                ("regulatory", "Регуляторика и экономика"),
                ("incidents", "Инциденты и безопасность"),
                ("market", "Рынок и конкуренты"),
                ("other", "Прочее важное"))

def _news_products(txt: str) -> list[str]:
    """Продуктовые теги новости (детерминированно, 0 LLM) — чипы на карточке."""
    from ..web.userdata import _PRODUCT_KEYWORDS
    return [slug for rx, slug in _PRODUCT_KEYWORDS if rx.search(txt or "")][:2]


# ── этап 2: триаж → жюри → фетч → редакция ────────────────────────────────────
# Одновызовный отбор пропускал в выпуск пиар и рутину (замер 05.08.2026: 50
# процентов мусора), а «why» писались по 160 символам сниппета — вода. Конвейер:
#   1) триаж: вердикт ПО КАЖДОЙ позиции (score/тип события/причина отказа),
#      с явными анти-примерами из реального мусора прошлых выпусков;
#   2) жюри для пограничных (score 4-5): два скептика, задача — ОПРОВЕРГНУТЬ;
#   3) полный текст статей финалистов (HTTP, без Playwright — дайджест не место
#      для браузера) — рубричные заголовки ЦБ без контента бессмысленны;
#   4) редакция: summary/why из фактического текста, без плана-на-12.
# Любая ступень падает → _news_legacy (старый одновызовный путь), не пустой экран.

_FETCH_N = int(os.getenv("DIGEST_NEWS_FETCH_N", "12"))           # статей за прогон
_FETCH_TIMEOUT_S = float(os.getenv("DIGEST_NEWS_FETCH_TIMEOUT_S", "8"))
_BODY_CHARS = int(os.getenv("DIGEST_NEWS_BODY_CHARS", "2000"))


# Анти-примеры — РЕАЛЬНЫЙ мусор из выпусков/пулов (аудит 05.08.2026 + жалоба
# владельца на «задержали в Дубае бизнесмена»). Позитивной рубрики LLM-фильтру
# мало: без негативных примеров пограничное стабильно просачивается.








def _reach_of(url: str | None, bodies: dict[str, str] | None) -> str:
    """Откроется ли ссылка ИЗ КОНТУРА банка — по факту нашей же попытки.

    Аудиторы неоднократно сообщали про кнопку «Источник»: «не удаётся
    получить доступ к сайту». Отдельного зонда для этого не нужно — тексты
    финалистов мы и так тянем тем же клиентом из того же контура, и провал
    там означает, что у аудитора ссылка тоже не откроется.

    Ключевое: молчим там, где НЕ ПРОБОВАЛИ. Пул статей больше лимита закачки,
    и пометить непроверенную ссылку недоступной — оболгать живой источник.
    """
    u = url or ""
    if u.startswith("https://t.me/"):
        return "telegram"          # в контуре обычно закрыт, но источник ценен
    if bodies is None or u not in bodies:
        return "unknown"           # не пробовали — молчим
    return "ok" if bodies[u] else "unreachable"


def _news_bodies(urls: list[str]) -> dict[str, str]:
    """Полные тексты статей финалистов: HTTP-only (без Playwright — дайджест не
    место для браузера), параллельно, каждая ошибка = просто нет текста.
    Рубричные страницы ЦБ («Решения Банка России…») без этого — пустые калории:
    заголовок у них каждый день один и тот же, суть только в содержимом."""
    import concurrent.futures as cf
    import httpx
    from ..rag.fetcher import CA_BUNDLE_PATH, DEFAULT_HEADERS
    from ..rag.parsers.html_parser import parse_html

    def _one(url: str) -> tuple[str, str]:
        try:
            with httpx.Client(http2=False, headers=DEFAULT_HEADERS,
                              follow_redirects=True,
                              verify=CA_BUNDLE_PATH or True,
                              timeout=_FETCH_TIMEOUT_S) as c:
                r = c.get(url)
            if r.status_code != 200 or not r.content:
                return url, ""
            doc = parse_html(r.content, url)
            txt = " ".join((doc.text or "").split())
            return url, txt[:_BODY_CHARS]
        except Exception:  # noqa: BLE001
            return url, ""

    urls = [u for u in urls if u and not u.startswith("https://t.me/")][:_FETCH_N]
    if not urls:
        return {}
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        # Неудачу оставляем пустой строкой, а не выбрасываем: по словарю потом
        # видно, какие ссылки мы пробовали (см. _reach_of). Потребители текста
        # читают через .get(), и пустая строка для них равна отсутствию.
        return dict(ex.map(_one, urls))


# Порог «это продолжение того же сюжета» (косинус заголовков). Сравниваем
# ИСХОДНЫЕ заголовки (src_title), а не переписанные редакцией — в памяти лежат
# исходники пула.
_STORY_T = float(os.getenv("DIGEST_NEWS_STORY_T", "0.70"))


def _news_stories(groups: list[dict]) -> int:
    """Сюжеты: сегодняшняя публикация — продолжение публиковавшегося в прошлые
    дни (эмбеддинг-близость заголовков к digest_news_seen за 14 дн). Пишет в
    item["story"] прошлые эпизоды; headline получает пометку «продолжение
    сюжета». Best-effort: сбой — просто без сюжетов."""
    try:
        from sqlalchemy import text as _t
        from .. import db
        from ..rag import embedder
        with db.session() as s:
            prev = s.execute(_t("""
                SELECT title, url,
                       (picked_at AT TIME ZONE 'Europe/Moscow')::date AS d
                FROM digest_news_seen
                WHERE picked
                  AND picked_at >= now() - interval '14 days'
                  AND (picked_at AT TIME ZONE 'Europe/Moscow')::date
                      < (now() AT TIME ZONE 'Europe/Moscow')::date
                  AND coalesce(title, '') <> ''
            """)).all()
        if not prev:
            return 0
        cur = [it for g in groups for it in (g.get("items") or [])]
        if not cur:
            return 0
        v_prev = embedder.embed_batch([p[0] for p in prev])
        v_cur = embedder.embed_batch(
            [it.get("src_title") or it.get("title") or "" for it in cur])
        n_st = 0
        for k, it in enumerate(cur):
            eps = [{"title": p[0][:140], "url": p[1], "date": str(p[2]),
                    "sim": round(embedder.cosine_similarity(v_cur[k], v_prev[j]), 3)}
                   for j, p in enumerate(prev)
                   if embedder.cosine_similarity(v_cur[k], v_prev[j]) >= _STORY_T]
            if eps:
                eps.sort(key=lambda e: e["date"])
                it["story"] = eps[-3:]      # последние три эпизода
                n_st += 1
        if n_st:
            log.info("news: сюжетов-продолжений %d", n_st)
        return n_st
    except Exception as e:  # noqa: BLE001
        log.info("news: сюжеты пропущены (%s)", e)
        return 0








async def news(day: date) -> dict:
    """Новости выпуска из полного потока (digest/newsflow): весь день идёт сбор
    и оценка, здесь — хвост за последние минуты и выбор.

    Публикуются события с ценностью от 6 по рубрике аудита розницы; ставки,
    тарифы и макроэкономика уходят в «фон рынка» (для абзаца передовицы).
    Резервного одновызовного пути больше нет: при сбое секция остаётся
    прежней (pipeline: kept/stale), а не публикует сырые заголовки — 24.09
    так вышли «#БанковскийСектор» и «🔤 🔤 🔤»."""
    from . import newsflow as nf
    try:
        await nf.tick()
    except Exception as e:  # noqa: BLE001 — хвост, основной сбор шёл весь день
        log.warning("news: хвостовой проход потока не удался (%s)", e)
    evs = await asyncio.to_thread(nf.day_events)
    evs = await nf.merge_events(evs)
    hl = await asyncio.to_thread(nf.health)
    if not evs:
        raise RuntimeError("в потоке нет оценённых событий за окно")
    shown = [e for e in evs if (e["value"] or 0) >= 6][:_NEWS_MAX]
    background = [e for e in evs if (e["value"] or 0) <= 5
                  and (e["s2"] or {}).get("category") in _BG_CATS][:8]
    order = {k: i for i, (k, _t) in enumerate(_NEWS_GROUPS)}
    titles = dict(_NEWS_GROUPS)
    groups: dict[str, dict] = {}
    for e in shown:
        it = _news_item(e)
        g = groups.setdefault(e["group"], {"key": e["group"], "title": titles.get(e["group"], "Прочее важное"),
                                           "items": []})
        g["items"].append(it)
    glist = sorted(groups.values(), key=lambda g: (-max(i["score"] for i in g["items"]),
                                                   order.get(g["key"], 9)))
    await asyncio.to_thread(_news_stories, glist)
    try:
        await asyncio.to_thread(nf.mark_published,
                                [i for e in shown for i in (e.get("merged_ids") or [e["event_id"]])])
        from . import news as news_mod
        await asyncio.to_thread(news_mod.mark_published, [i["url"] for g in glist for i in g["items"]])
    except Exception:  # noqa: BLE001 — память не должна ронять секцию
        log.warning("news: отметка публикации не удалась", exc_info=True)
    sources = [{"name": s["source"], "ok": not s.get("last_error"),
                "items": s.get("items_24h") or 0, "skipped_reason": s.get("last_error")}
               for s in hl["sources"]]
    pool = [{"title": (e["s2"] or {}).get("headline") or e["lead"]["title"],
             "url": e["lead"]["url"], "domain": _domain(e["lead"]["url"]),
             "source": e["lead"]["source"],
             "ts": e["ts"].isoformat() if e["ts"] else None, "tag": e["lead"].get("rtype"),
             "dimension": None, "image": e["lead"].get("image"), "echo": e["n_sources"],
             "tri": e["value"], "event": (e["s2"] or {}).get("category"),
             "snippet": (e["s2"] or {}).get("summary") or ""}
            for e in evs if (e["value"] or 0) >= 4]
    return {"groups": glist,
            "background": [{"title": (e["s2"] or {}).get("headline") or e["lead"]["title"],
                            "url": e["lead"]["url"], "source": e["lead"]["source"],
                            "value": e["value"]} for e in background],
            "sources": sources, "raw_count": hl["items_24h"], "pool": pool,
            "triage": {"stream_24h": hl["items_24h"], "relevant_24h": hl["relevant_24h"],
                       "events": len(evs), "kept": len(shown)},
            "_llm_model": nf.S2_MODEL}




# ── headline (+insights) ──────────────────────────────────────────────────────

_HEAD_SYSTEM = (
    "Ты — главный редактор утреннего брифинга службы внутреннего аудита розничного "
    "бизнеса Сбера. Тебе дают УЖЕ УПОРЯДОЧЕННЫЙ по важности список поводов для проверки "
    "(порядок определён по рубрике аудита — НЕ меняй его) и блок «фон рынка».\n"
    "Твоя работа — текст:\n"
    "• headline — заголовок дня про ПЕРВЫЙ повод, до 90 знаков, по существу, без "
    "драматизации и оценок, которых нет в данных;\n"
    "• cards — по каждому поводу в том же порядке: title (суть с цифрой, если она есть в "
    "данных), so_what (1–2 фразы: почему это важно аудиту розницы Сбера), idea (что "
    "проверить — конкретно, опираясь на данные повода; не выдумывай фактов);\n"
    "• market_note — 1–2 предложения «Фон рынка» ТОЛЬКО по блоку фона: ключевая ставка, "
    "движение ставок и тарифов; без оценок «рынок нервничает»; если блока нет — пусто.\n"
    "Строку «Наши данные» у повода показывают отдельно — в so_what её числа не повторяй. "
    "Числа бери только из данных и пиши по-русски: десятичная запятая (3,6; 14%). "
    "Конкуренты — бенчмарк, рекомендации — внутренние действия по Сберу. Без эмодзи."
)

# Раньше здесь жила локальная копия с битыми ключами (autocredit/credit_card
# вместо auto_loan/card_credit из enum) — LLM получал сырые слаги
from ..categories import CAT_RU as _CAT_RU


def _cat_ru(c: str) -> str:
    return _CAT_RU.get(c or "", c or "")


# ── связки «новость ↔ наши данные» (этап 5) ──────────────────────────────────
# Кандидатов ищет детерминированный код (матчинг по типам событий триажа и
# категориям), человеческий текст пишет headline-LLM обычной инсайт-карточкой —
# фронт не меняется. Это главное преимущество платформы: внешняя нейросеть
# новость перескажет, но у неё нет наших тарифных рядов и жалоб.





# Тарифное движение попадает в кандидаты передовицы, только пока оно СВЕЖЕЕ.
# Таблица «Тарифные движения недели» живёт окном в 7 дней — это её смысл, но
# для заголовка дня недельное окно означало, что самое крупное движение
# доминирует всю неделю: скачок ставки автокредита Сбера от 05.08.2026 стоял
# в заголовке 07, 08, 09 и 10 августа. Новость живёт сутки-двое, дальше это
# уже история, и её место — в журнале изменений, а не в передовице.
_TARIFF_FRESH_H = float(os.getenv("DIGEST_TARIFF_FRESH_H", "48"))


def _age_hours(iso_ts: str | None) -> float | None:
    if not iso_ts:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        ts = _dt.fromisoformat(str(iso_ts))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        return (_dt.now(_tz.utc) - ts).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return None




def _ai_prompt(kind: str, d: dict) -> str:
    if kind == "review_spike":
        geo = d.get("geo") or {}
        parts = [f'Разбери всплеск жалоб «{d["label"]}» у Сбербанка: '
                 f'{d["week"]} за 7 дней против ~{d.get("baseline_week")}/нед'
                 + (f' (×{d["ratio"]})' if d.get("ratio") else "")]
        if geo:
            parts.append(f'{geo["share"]}% жалоб из г. {geo["city"]}')
        if d.get("bank_specific"):
            parts.append("у рынка без Сбера роста нет")
        parts.append("Найди вероятную причину, оцени регуляторный риск и предложи шаги аудита.")
        return ". ".join(parts)
    if kind == "tariff_move":
        return (f'Банк {d["bank"]} изменил условия «{d["title"]}» ({_cat_ru(d["category"])}) '
                f'с {d["from"]}% до {d["to"]}%. Оцени последствия для клиентов и риски для аудита розницы.')
    if kind == "news_alert":
        return (f'Проанализируй для аудита розничного бизнеса Сбера: «{d.get("title")}» '
                f'({d.get("url")}). Какой процесс затронут, какие риски, что проверить и что запросить?')
    if kind == "loophole":
        return (f'Разбери лазейку в продуктах Сбера: «{d.get("title")}» ({d.get("url")}). '
                f'Насколько она реальна, каков ущерб для банка, какие контроли проверить?')
    return ""


def _drill(kind: str, d: dict) -> dict:
    if kind == "review_spike":
        p = {"theme": d.get("key")}
        if d.get("geo"):
            p["city"] = d["geo"]["city"]
        return {"page": "reviews", "params": p}
    if kind == "tariff_move":
        p = {"category": d.get("category"), "view": "changes",
             "bank": d.get("bank_slug"), "offer": d.get("offer_id"),
             "change": d.get("change_id")}
        return {"page": "market", "params": {k: v for k, v in p.items() if v}}
    if kind == "news_alert":
        return {"url": d.get("url")}
    if kind == "loophole":
        return {"page": "loophole", "params": {}} if not d.get("url") else {"url": d.get("url")}
    return {}


def _provenance(kind: str, d: dict) -> str:
    if kind == "review_spike":
        return (f'жалобы всех площадок, разметка ИИ · {d.get("week")} за 7 дн · норма — '
                f'7 прошлых недель, рост статистически значим')
    if kind == "tariff_move":
        return "журнал изменений тарифов · проверено на сбои сбора"
    if kind == "news_alert":
        src = d.get("domain") or d.get("source") or "пресса"
        n = int(d.get("echo") or 1)
        return f'{src}' + (f' · ещё {n - 1} ист.' if n > 1 else "")
    if kind == "loophole":
        return "вкладка «Уязвимости» · предварительная классификация"
    return ""


def _fallback_headline(leads: list[dict]) -> dict:
    """Модель недоступна → детерминированная передовица из поводов."""
    cards = []
    for ld in leads[:5]:
        d = ld["data"]
        if ld["kind"] == "review_spike":
            title = (f'Всплеск жалоб «{d["label"]}»: {d["week"]} за неделю'
                     + (f' (×{d["ratio"]})' if d.get("ratio") else ""))
        elif ld["kind"] == "tariff_move":
            title = f'Сбер: «{d["title"]}» {d["from"]}% → {d["to"]}%'
        else:
            title = d.get("title") or ""
        cards.append({"ref": ld["ref"], "title": title,
                      "so_what": d.get("summary") or "", "idea": d.get("idea") or ""})
    head = cards[0]["title"] if cards else f"Сводка за {today_ru()}"
    return {"headline": head, "hot": "", "cards": cards, "market_note": "", "quiet_note": ""}


async def headline(day: date) -> dict:
    secs = await asyncio.to_thread(store._read_day_rows, day)
    prev = await asyncio.to_thread(store.recent_headlines, day, 3)
    prev_leads = {p["lead_ref"] for p in prev if p.get("lead_ref")}
    leads, bg = await asyncio.to_thread(_build_leads, secs, prev_leads)
    top = await asyncio.to_thread(_distinct_leads, [ld for ld in leads if ld["score"] >= 6])
    top = top[:6]

    result, ti, to, model, degraded = None, 0, 0, None, False
    if top or bg:
        lines = [f"{k + 1}. ({ld['ref']}) {ld['facts']}" for k, ld in enumerate(top)]
        prev_block = ("\n\nЗАГОЛОВКИ ПРОШЛЫХ ВЫПУСКОВ (не повторяй формулировки):\n"
                      + "\n".join(f'- {p["date"]}: {p["headline"]}' for p in prev if p.get("headline"))
                      if prev else "")
        user = (f"Дата выпуска: {today_ru()}.\nПОВОДЫ (по убыванию важности):\n"
                + ("\n".join(lines) or "— поводов с высокой ценностью нет")
                + "\n\nФОН РЫНКА:\n" + ("\n".join(f"- {b}" for b in bg) or "—")
                + prev_block
                + "\n\nВерни СТРОГО JSON без markdown: "
                  '{"headline":"...","hot":"2-4 слова ДОСЛОВНО из headline",'
                  '"cards":[{"ref":"<id из скобок>","title":"...","so_what":"...","idea":"..."}],'
                  '"market_note":"..."} — cards по всем поводам в том же порядке.')
        for mdl in (_HEAD_MODEL, insight_model()):
            try:
                raw, ti, to = await _chat(mdl, today_anchor() + "\n\n" + _HEAD_SYSTEM, user,
                                          max_tokens=3500, temperature=0.2)
                result = _loose_json_loads(raw)
                model = mdl
                break
            except Exception as e:  # noqa: BLE001
                log.warning("headline: %s не ответила (%s)", mdl, e)
    if result is None:
        result = _fallback_headline(top)
        degraded = True

    by_ref = {str(c.get("ref") or "").strip("() "): c for c in (result.get("cards") or [])
              if isinstance(c, dict)}
    insights = []
    for ld in top:                         # порядок — наш, текст — модели
        c = by_ref.get(ld["ref"]) or {}
        d = ld["data"]
        sev, lik, imp = _sev(ld["score"])
        title = str(c.get("title") or d.get("title") or d.get("label") or "")[:180]
        if not title:
            continue
        insights.append({
            "ref": ld["ref"], "kind": ld["kind"], "severity": sev,
            "likelihood": lik, "impact": imp, "score": round(ld["score"], 2),
            "title": title,
            "so_what": str(c.get("so_what") or "")[:280],
            "idea": str(c.get("idea") or d.get("idea") or "")[:260],
            "evidence": _evidence_line(d.get("evidence")) if ld["kind"] == "news_alert" else "",
            "data": d, "drill": _drill(ld["kind"], d), "ai_prompt": _ai_prompt(ld["kind"], d),
            "provenance": _provenance(ld["kind"], d),
        })
    rp = (secs.get("reviews_pulse") or {}).get("payload") or {}
    nw = (secs.get("news") or {}).get("payload") or {}
    n_news = sum(len(g.get("items") or []) for g in (nw.get("groups") or []))
    # «где спокойно» — детерминированно: модель писала странное вроде
    # «по жалобам на кредит мошенников за неделю 0 обращений»
    n_sig = len(rp.get("signals") or [])
    quiet = ("Жалобы клиентов по остальным проблемам кодификатора — в пределах нормы."
             if n_sig else "Значимых всплесков жалоб клиентов за неделю нет.")
    result["quiet_note"] = quiet
    head = str(result.get("headline") or "")[:160]
    if not head or not insights:
        head = head or (insights[0]["title"] if insights else f"Сводка за {today_ru()}")
    return {
        "headline": head,
        "hot": str(result.get("hot") or "")[:60],
        "quiet_note": str(result.get("quiet_note") or "")[:200],
        "market_note": str(result.get("market_note") or "")[:400],
        "insights": insights,
        "lead_ref": insights[0]["ref"] if insights else None,
        "stats": {"risk": sum(1 for i in insights if i["severity"] == "risk"),
                  "good": 0, "news": n_news,
                  "checked_themes": (rp.get("checked") or {}).get("themes") or 0,
                  "stream_24h": (nw.get("triage") or {}).get("stream_24h")},
        **({"_status": "degraded"} if degraded else {}),
        **({"_llm_model": model, "_tokens_in": ti, "_tokens_out": to} if model else {}),
    }


_NEWS_MAX = int(os.getenv("DIGEST_NEWS_MAX", "14"))


_BG_CATS = ("market_background", "macro", "competitor_risk")


def _domain(url: str | None) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url or "").netloc.replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def _news_item(e: dict) -> dict:
    s2 = e["s2"] or {}
    v = int(e["value"] or 0)
    lead = e["lead"]
    return {"title": s2.get("headline") or lead["title"], "src_title": lead["title"],
            "summary": s2.get("summary") or "",
            "why": s2.get("idea") or s2.get("sber") or "",
            "sber": s2.get("sber") or "", "idea": s2.get("idea") or "",
            "request": s2.get("request") or "", "deadline": s2.get("deadline") or "",
            "codes": s2.get("codes") or [],
            "url": lead["url"], "domain": _domain(lead["url"]), "source": lead["source"],
            "ts": lead["ts"].isoformat() if lead["ts"] else None,
            "severity": "red" if v >= 9 else ("amber" if v >= 7 else "green"),
            "score": v, "event": s2.get("category"), "echo": e["n_sources"],
            "event_id": e["event_id"], "image": lead.get("image"), "products": [],
            "tag": lead.get("rtype")}


_UNFAV = {"deposit": -1, "savings_account": -1, "credit": 1, "mortgage": 1, "auto_loan": 1,
          "card_credit": 1}


def _codes_evidence(codes: list[str]) -> dict | None:
    """Наши данные к поводу: жалобы Сбера по связанным кодам кодификатора —
    за 90 дней и неделя к норме. Так новость становится связкой «внешнее
    событие ↔ что видят наши клиенты»."""
    codes = [c for c in (codes or []) if c][:3]
    if not codes:
        return None
    try:
        from ..rag import reviews_dash as rd
        from ..rag import review_codebook as cb
        lab = rd._topic_week_counts("Сбербанк", None)
        if not lab:
            return None
        _t, cnt = lab
        week = sum(int(cnt.get(f"{c}_w0", 0)) for c in codes)
        norm = sum(int(cnt.get(f"{c}_b", 0)) for c in codes) / 7.0
        with db.session() as s:
            d90 = int(s.execute(text("""
                SELECT count(*) FROM review_index
                WHERE bank = 'Сбербанк' AND kind IN ('complaint', 'mixed') AND issue = ANY(:c)
                  AND dt >= now() - interval '90 days' AND dt <= now()"""), {"c": codes}).scalar() or 0)
        if not d90:
            return None
        labels = [(cb.issue_obj(c) or {}).get("short") for c in codes]
        return {"codes": codes, "labels": [x for x in labels if x], "week": week,
                "norm": round(norm, 1), "d90": d90,
                "up": bool(week >= 5 and norm > 0 and week >= 1.5 * norm)}
    except Exception:  # noqa: BLE001
        log.info("headline: данные жалоб к поводу не посчитались", exc_info=True)
        return None


def _evidence_line(ev: dict | None) -> str:
    if not ev:
        return ""
    lab = " / ".join(ev["labels"][:2]) or "по теме"
    return (f'жалобы Сбера «{lab}»: {ev["d90"]} за 90 дн, за неделю {ev["week"]}'
            f' при норме ~{ev["norm"]}' + (" — выше нормы" if ev.get("up") else ""))


def _new_sber_loopholes() -> list[dict]:
    try:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT record_id, coalesce(title, left(snippet, 120)) AS title, url, verdict_reason,
                       verdict_confidence, collected_at
                FROM auditlens.loophole_record
                WHERE is_loophole AND bank_slug = 'sberbank' AND verdict_confidence >= 0.85
                  AND collected_at > now() - interval '72 hours'
                ORDER BY verdict_confidence DESC, collected_at DESC LIMIT 3""")).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _build_leads(secs: dict, prev_leads: set[str]) -> tuple[list[dict], list[str]]:
    """Поводы из всех вкладок с детерминированной оценкой. Возвращает
    (упорядоченные поводы, строки блока «фон рынка»)."""
    leads: list[dict] = []
    nw = (secs.get("news") or {}).get("payload") or {}
    for g in nw.get("groups") or []:
        for it in g.get("items") or []:
            v = float(it.get("score") or 0)
            if v < 7:
                continue
            ev = _codes_evidence(it.get("codes") or [])
            score = v
            if it.get("event") == "sber" or "сбер" in (it.get("title") or "").lower():
                score += 0.5
            if it.get("deadline"):
                try:
                    left = (date.fromisoformat(it["deadline"]) - date.today()).days
                    if 0 <= left <= 14:
                        score += 0.5
                except ValueError:
                    pass
            if int(it.get("echo") or 1) >= 3:
                score += 0.3
            if ev and ev.get("up"):
                score += 0.5
            leads.append({"ref": f"news:{it.get('event_id') or it.get('url')}", "kind": "news_alert",
                          "score": score, "data": {**it, "evidence": ev},
                          "facts": (f'новость ({it.get("domain")}, ценность {int(v)}/10): {it.get("title")}. '
                                    f'{it.get("summary") or ""} Затронуто: {it.get("sber") or "—"}. '
                                    f'Идея проверки от отбора: {it.get("idea") or "—"}'
                                    + (f'. Срок: {it["deadline"]}' if it.get("deadline") else "")
                                    + (f'. Наши данные: {_evidence_line(ev)}' if ev else ""))})
    rp = (secs.get("reviews_pulse") or {}).get("payload") or {}
    for s_ in (rp.get("signals") or [])[:4]:
        score = 8.5 if s_.get("level") == "high" else 7.5
        if s_.get("bank_specific"):
            score += 0.5
        leads.append({"ref": f"rev:{s_['key']}", "kind": "review_spike", "score": score, "data": s_,
                      "facts": (f'всплеск жалоб клиентов Сбера «{s_["label"]}»: {s_["week"]} за 7 дн '
                                f'при норме ~{s_.get("baseline_week")}/нед'
                                + (f', ×{s_["ratio"]}' if s_.get("ratio") else "")
                                + (", только у Сбера" if s_.get("bank_specific") else "")
                                + (f', {s_["geo"]["share"]}% из г. {s_["geo"]["city"]}' if s_.get("geo") else "")
                                + ". Причину см. в анализе жалоб недели.")})
    tm = (secs.get("tariff_moves") or {}).get("payload") or {}
    for c in (tm.get("top_changes") or []):
        if not c.get("is_sber"):
            continue
        age = _age_hours(c.get("changed_at"))
        if age is not None and age > _TARIFF_FRESH_H:
            continue
        direction = _UNFAV.get(c.get("category"), 0) * (1 if c["delta"] > 0 else -1)
        score = 6.5 + (0.5 if direction > 0 else 0)
        leads.append({"ref": f"chg:{c.get('change_id')}", "kind": "tariff_move", "score": score,
                      "data": c,
                      "facts": (f'Сбер изменил условия «{c["title"]}» ({_cat_ru(c["category"])}): '
                                f'{c["from"]}% → {c["to"]}%'
                                + (" — хуже для клиента" if direction > 0 else ""))})
        break
    for lp in _new_sber_loopholes()[:1]:
        leads.append({"ref": f"loop:{lp['record_id']}", "kind": "loophole", "score": 6.0,
                      "data": {**lp, "collected_at": str(lp.get("collected_at"))},
                      "facts": f'новая лазейка в продуктах Сбера (предварительно): {lp.get("title")}. '
                               f'{lp.get("verdict_reason") or ""}'})
    for ld in leads:                       # не вести выпуск тем же поводом второй день
        if ld["ref"] in prev_leads:
            ld["score"] -= 1.0
    leads.sort(key=lambda x: -x["score"])

    bg: list[str] = []
    kr = tm.get("key_rate") or {}
    if kr.get("current") is not None:
        bg.append(f'ключевая ставка {kr["current"]}% (на {kr.get("as_of")})'
                  + (f', спред макс. вклада Сбера к ключевой {tm["dep_spread_pp"]:+} пп'
                     if tm.get("dep_spread_pp") is not None else ""))
    for m in (tm.get("mass_updates") or [])[:2]:
        bg.append(f'{m["n_banks"]} банков изменили ставки «{_cat_ru(m["category"])}» за 48 ч')
    n = 0
    for c in (tm.get("top_changes") or []):
        if c.get("is_sber"):
            continue
        age = _age_hours(c.get("changed_at"))
        if age is not None and age > _TARIFF_FRESH_H:
            continue
        bg.append(f'{c["bank"]}: «{c["title"]}» {c["from"]}% → {c["to"]}%')
        n += 1
        if n >= 3:
            break
    for b in (nw.get("background") or [])[:5]:
        bg.append(f'новость: {b.get("title")}')
    return leads, bg


def _distinct_leads(leads: list[dict]) -> list[dict]:
    """Два повода об одном сюжете — одна карточка (оставляем более весомый).
    Пример 24.09: «ЦБ и НСПК дорабатывают «Добрую волю»» и «НСПК тестирует
    автоисключение реквизитов» — разные источники одного события."""
    if len(leads) < 2:
        return leads
    try:
        from ..rag import embedder
        texts = [str(ld["data"].get("title") or ld["data"].get("label") or ld["facts"])[:200]
                 for ld in leads]
        vecs = embedder.embed_batch(texts)
    except Exception:  # noqa: BLE001 — без векторов просто не склеиваем
        return leads
    keep: list[int] = []
    for i in range(len(leads)):
        if any(embedder.cosine_similarity(vecs[i], vecs[j]) >= 0.72 for j in keep):
            continue
        keep.append(i)
    return [leads[i] for i in keep]


def _sev(score: float) -> tuple[str, int, int]:
    if score >= 8.5:
        return "risk", 3, 3
    if score >= 7:
        return "watch", 2, 3
    return "neutral", 2, 2


_HEAD_MODEL = os.getenv("DIGEST_HEAD_MODEL", "anthropic/claude-opus-4.8")
