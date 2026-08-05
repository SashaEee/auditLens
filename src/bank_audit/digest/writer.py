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

from ..ai.analyst import (LLM_API_KEY, LLM_BASE_URL, fast_model, insight_model,
                          smart_model)
from ..ai.llm_utils import _loose_json_loads, _patch_client_reasoning_effort
from ..clock import today_anchor, today_ru
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
    "Тебе дают точные недельные метрики (НЕ меняй числа) и свежие жалобы. Сигналы:\n"
    "• рост темы к норме (×N) и УСКОРЕНИЕ — проблема нарастает;\n"
    "• «только у банка» (рынок ровный) → НАША регрессия, высокий приоритет;\n"
    "• гео-концентрация → локальный сбой (отделение/банкомат/регион);\n"
    "• жалобы ВНЕ известных тем → свежий инцидент, которого нет в таксономии.\n"
    "Без эмодзи, без воды, не алармируй без чисел."
)


async def reviews_brief(day: date) -> dict:
    from ..rag import reviews_dash as rd
    sig = await asyncio.to_thread(rd.weekly_signals, "Сбербанк", None)
    signals = (sig or {}).get("signals") or []
    if not signals:
        return {"markdown": None, "calm": True,
                "overall": (sig or {}).get("overall")}
    recent = await asyncio.to_thread(
        rd.list_reviews, "Сбербанк", None, None, None, 7, None, None, 50)
    unclassified = [r for r in recent if not r.get("themes")]

    lines = []
    for s in signals:
        bits = []
        if s.get("new"):
            bits.append("НОВАЯ тема (раньше почти не было)")
        elif s.get("ratio"):
            bits.append(f"×{s['ratio']} к норме ~{s['baseline_week']}/нед")
        if s.get("accel"):
            bits.append(f"ускоряется (нед: {s.get('prev_week')}→{s['week']})")
        if s.get("bank_specific"):
            bits.append(f"ТОЛЬКО у банка (рынок ×{s.get('market_ratio') or '~1'})")
        elif s.get("market_ratio") is not None and s["market_ratio"] >= 1.4:
            bits.append(f"рынок тоже растёт ×{s['market_ratio']}")
        if s.get("geo"):
            bits.append(f"{s['geo']['share']}% из г. {s['geo']['city']}")
        lines.append(f'- {s["label"]} [{s.get("level", "medium")}]: '
                     f'{s["week"]} за 7 дн; ' + "; ".join(bits))
    ov = (sig or {}).get("overall") or {}
    ov_line = ""
    if ov.get("week") is not None:
        ov_line = (f'Всего за неделю: {ov["week"]} (норма ~{ov.get("baseline_week")}/нед'
                   + (f', рынок ×{ov["market_ratio"]}'
                      if ov.get("market_ratio") is not None else "") + ").")
    samp = "\n".join(f'— {(r.get("text") or "")[:260]}' for r in recent[:12])
    unc = "\n".join(f'— {(r.get("text") or "")[:240]}' for r in unclassified[:12])
    user = (
        f"Сводка на {today_ru()}.\n"
        "СИГНАЛЫ НЕДЕЛИ (числа точные, не меняй):\n" + "\n".join(lines) + f"\n{ov_line}\n\n"
        f"СВЕЖИЕ ЖАЛОБЫ НЕДЕЛИ (для причины):\n{samp}\n\n"
        f"ЖАЛОБЫ ВНЕ ИЗВЕСТНЫХ ТЕМ (ищи НОВЫЙ повторяющийся инцидент):\n{unc or '—'}\n\n"
        "Выдай markdown-список (каждый пункт с «- »):\n"
        "1) 2–4 пункта по приоритету: «**[ВЫСОКИЙ/СРЕДНИЙ]** **<тема>** — что "
        "изменилось (с цифрой), пометь если *только у банка*/*локально*/*ускоряется*, "
        "вероятная причина из жалоб, что проверить аудитору».\n"
        "2) Если вне тем виден НОВЫЙ повторяющийся инцидент — пункт "
        "«- **Новое:** <суть> (≈N жалоб)».\n"
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
    return {"markdown": md or None, "calm": False, "overall": ov,
            **({"_llm_model": insight_model(), "_tokens_in": ti, "_tokens_out": to}
               if md else {"_status": "degraded"})}


# ── news ──────────────────────────────────────────────────────────────────────

_NEWS_SYSTEM = (
    "Ты — аналитик службы внутреннего аудита Сбербанка, розничный бизнес. Тебе дают "
    "сырую ленту новостей за последние 48 часов (RSS ЦБ, банковские СМИ, "
    "телеграм-каналы, поиск). Отбери ТОЛЬКО релевантное аудитору розницы Сбера: "
    "регуляторика ЦБ и законы; инциденты/сбои/утечки/хищения в банках; схемы "
    "мошенничества против клиентов; значимые действия конкурентов (продукты, ставки, "
    "акции); решения по ключевой ставке. Отбрось дубли по смыслу, пиар и нерелевантное. "
    "НЕ выдумывай фактов сверх текста новости."
)

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

_TRIAGE_MIN = int(os.getenv("DIGEST_NEWS_TRIAGE_MIN", "6"))      # порог «в выпуск»
_JURY_LOW = 4                                                    # низ пограничной зоны
_MAX_PICKS = int(os.getenv("DIGEST_NEWS_MAX_PICKS", "14"))       # потолок финалистов
_FETCH_N = int(os.getenv("DIGEST_NEWS_FETCH_N", "12"))           # статей за прогон
_FETCH_TIMEOUT_S = float(os.getenv("DIGEST_NEWS_FETCH_TIMEOUT_S", "8"))
_BODY_CHARS = int(os.getenv("DIGEST_NEWS_BODY_CHARS", "2000"))

_EVENT_TYPES = (
    "rate_decision",      # решение по ключевой ставке
    "reg_enforcement",    # санкция/предписание/штраф/отзыв лицензии
    "reg_rulemaking",     # закон/норматив/проект, затрагивающий розничные операции
    "reg_guidance",       # разъяснения/обзоры/статистика регулятора по рознице
    "bank_incident",      # сбой/авария в банке
    "data_leak",          # утечка данных
    "fraud_scheme",       # схема мошенничества против клиентов банков
    "fraud_stats",        # статистика/отчёты по мошенничеству
    "competitor_product", # продукт/тариф/акция банка-конкурента
    "market_trend",       # динамика рынка розничных продуктов (ставки, просрочка)
    "legal_precedent",    # суд/практика по банковской рознице
    "infosec",            # кибербезопасность банковских каналов
    "sber_news",          # событие самого Сбера
    "payments_infra",     # платёжная инфраструктура: СБП, карты, банкоматы, НСПК
    "other_relevant",
)

# Анти-примеры — РЕАЛЬНЫЙ мусор из выпусков/пулов (аудит 05.08.2026 + жалоба
# владельца на «задержали в Дубае бизнесмена»). Позитивной рубрики LLM-фильтру
# мало: без негативных примеров пограничное стабильно просачивается.
_TRIAGE_SYSTEM = (
    "Ты — фильтр новостной ленты для аудитора РОЗНИЦЫ Сбербанка. По КАЖДОЙ позиции "
    "дай вердикт: score 0-10 (насколько это нужно именно аудитору розницы Сбера), "
    "тип события и, для отвергнутых, короткую причину.\n"
    "РЕЛЕВАНТНО (6-10): санкции/предписания ЦБ банкам; законы и нормативы по "
    "розничным банковским операциям; ключевая ставка; сбои/утечки/хищения В БАНКАХ; "
    "схемы мошенничества против банковских клиентов; продуктовые/тарифные действия "
    "банков-конкурентов; судебная практика по рознице; платёжная инфраструктура "
    "(СБП, карты, банкоматы); события самого Сбера.\n"
    "НЕ РЕЛЕВАНТНО (0-3), реальные примеры пропущенного ранее мусора:\n"
    "• «задержан бизнесмен в Дубае за взятки» — уголовка вне банковского сектора;\n"
    "• «Адмирал расторг контракт с экс-игроком НХЛ» — спорт;\n"
    "• «ПСБ и МЧС окажут поддержку пострадавшим» — пиар банка без продуктовой сути;\n"
    "• «Ставка RUONIA», «Депозиты банков в Банке России» — ежедневная рутина ЦБ;\n"
    "• «мошенничество с пособиями в Польше» — зарубежный сюжет без связи с рынком РФ;\n"
    "• выборы, назначения в министерствах, геополитика, ЧС, шоубиз;\n"
    "• «3 ошибки при выборе депозита» — потребительские советы;\n"
    "• «средняя цена авто», «рост зарплат по отраслям» — общая статистика.\n"
    "ПОГРАНИЧНО (4-5): финансовый сектор без конкретики или связи с розницей.\n"
    "УСТАРЕВШЕЕ = score 0: если в заголовке/сниппете/URL видна дата старше двух "
    "суток от сегодняшней — это не новость (реальный прокол: пресс-релиз 2011 "
    "года о сбое процессинга ушёл в заголовок выпуска). reject: «устаревшее».\n"
    "Сомнительный домен-агрегатор без первоисточника — снижай score на 2. "
    "Пустой рубричный заголовок (одно название рубрики без сути) сам по себе "
    "score не поднимает — оценивай вероятную ценность содержимого.\n"
    f"type — один из: {', '.join(_EVENT_TYPES)}; для score<=3 ставь null.\n"
    'Верни СТРОГО JSON без markdown: {"verdicts":[{"n":1,"score":7,'
    '"type":"reg_enforcement","reject":null}]} — по ВСЕМ позициям, reject — '
    "до 8 слов только для score<=3."
)


async def _news_triage(items: list[dict]) -> dict[int, dict]:
    """Один вызов: вердикты по всем позициям пула. Бросает исключение при сбое —
    news() уходит на _news_legacy."""
    listing = "\n".join(
        f'#{i + 1} [{it.get("tag")}] {it["title"]} — {(it.get("snippet") or "")[:180]} '
        f'({it.get("domain")}{", повторили " + str(it["echo"]) + " ист." if it.get("echo", 1) > 1 else ""})'
        for i, it in enumerate(items))
    raw, ti, to = await _chat(insight_model(), today_anchor() + "\n\n" + _TRIAGE_SYSTEM,
                              f"Лента ({len(items)} позиций):\n{listing}",
                              max_tokens=4000, temperature=0.0)
    try:
        parsed = _loose_json_loads(raw)
    except ValueError:
        raw, ti2, to2 = await _chat(insight_model(),
                                    today_anchor() + "\n\n" + _TRIAGE_SYSTEM,
                                    f"Лента ({len(items)} позиций):\n{listing}",
                                    max_tokens=4000, temperature=0.0)
        ti, to = ti + ti2, to + to2
        parsed = _loose_json_loads(raw)
    out: dict[int, dict] = {}
    for v in (parsed.get("verdicts") or []):
        try:
            n = int(v.get("n"))
            score = max(0, min(10, int(v.get("score"))))
        except (TypeError, ValueError):
            continue
        if not (1 <= n <= len(items)):
            continue
        typ = v.get("type")
        out[n] = {"score": score,
                  "type": typ if typ in _EVENT_TYPES else None,
                  "reject": (str(v.get("reject") or "")[:80] or None)}
    if len(out) < len(items) * 0.7:      # модель размечает не всё → не доверяем
        raise ValueError(f"триаж покрыл {len(out)} из {len(items)} позиций")
    out["_tokens"] = {"in": ti, "out": to}  # type: ignore[assignment]
    return out


_JURY_SYSTEM = (
    "Ты — скептик редакции брифинга аудитора розницы Сбербанка. Тебе дают ОДНУ "
    "пограничную новость. Твоя задача — ОПРОВЕРГНУТЬ её релевантность: найди, "
    "почему она аудитору розницы Сбера НЕ нужна (не банковская розница; пиар; "
    "рутина; нет конкретики; зарубежное без связи с РФ). Если опровергнуть "
    "честно не получается — так и скажи.\n"
    'Верни СТРОГО JSON: {"verdict":"drop"|"keep","reason":"до 12 слов"}'
)


async def _news_jury(item: dict) -> bool:
    """Два скептика по пограничной позиции; выживает при хотя бы одном keep
    (вместе с «за» триажа это 2 из 3). Сбой голосов → drop: порог честнее."""
    body = (f'{item["title"]} — {(item.get("snippet") or "")[:300]} '
            f'({item.get("domain")})')

    async def _vote() -> bool:
        raw, _ti, _to = await _chat(insight_model(),
                                    today_anchor() + "\n\n" + _JURY_SYSTEM,
                                    body, max_tokens=120, temperature=0.3)
        return str(_loose_json_loads(raw).get("verdict")).strip() == "keep"

    votes = await asyncio.gather(_vote(), _vote(), return_exceptions=True)
    return any(v is True for v in votes)


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
        return {u: t for u, t in ex.map(_one, urls) if t}


def _news_pool(items: list[dict]) -> list[dict]:
    """Полный сырой пул дня — сырьё для персонального ре-ранка («Для вас»).
    LLM-группы дают ≤12 позиций на всех; персональная сетка ранжирует из всего пула."""
    return [{"title": it.get("title"), "url": it.get("url"), "domain": it.get("domain"),
             "source": it.get("source"), "ts": it.get("ts"), "tag": it.get("tag"),
             "dimension": it.get("dimension"), "image": it.get("image"),
             # echo — сколько источников продублировали событие (смысловой дедуп);
             # сигнал значимости для персонального ре-ранка и будущего триажа
             "echo": int(it.get("echo") or 1),
             "snippet": (it.get("snippet") or "")[:200]} for it in items]


_EDIT_SYSTEM = (
    "Ты — редактор блока «Новости для аудитора» утреннего брифинга службы "
    "внутреннего аудита Сбербанка (розница). Тебе передают УЖЕ ОТОБРАННЫЕ "
    "фильтром позиции, у большинства есть полный текст статьи. Твоя работа:\n"
    "• сгруппировать по рубрикам;\n"
    "• каждой позиции написать: headline — до 90 знаков (если исходный заголовок "
    "— пустое название рубрики вроде «Решения Банка России в отношении участников "
    "финансового рынка», НАПИШИ заголовок заново по сути содержимого текста); "
    "summary — 1 предложение сути ИЗ ТЕКСТА; why — какой процесс или продукт "
    "розницы Сбера затронут и ЧТО КОНКРЕТНО проверить аудитору (без пустых "
    "«требует мониторинга/наблюдать»); severity.\n"
    "Включи ВСЕ переданные позиции, кроме смысловых дублей друг друга И кроме "
    "устаревших: если дата события в тексте старше двух суток от сегодняшней — "
    "позицию НЕ включай (прокол: мартовская статистика сбоя подавалась как "
    "сегодняшняя). Факты бери ТОЛЬКО из переданного текста, не выдумывай."
)


async def _news_editorial(items: list[dict], picks: list[int],
                          bodies: dict[str, str],
                          verdicts: dict[int, dict]) -> tuple[list[dict], tuple[int, int]]:
    """Редакция по финалистам триажа: группировка + headline/summary/why из
    фактического текста статьи (не 160 символов сниппета)."""
    blocks = []
    for n in picks:
        it = items[n - 1]
        v = verdicts.get(n) or {}
        body = bodies.get(it.get("url") or "")
        echo = f', повторили {it["echo"]} ист.' if it.get("echo", 1) > 1 else ""
        blocks.append(
            f'#{n} [{v.get("type") or it.get("tag")}{echo}] {it["title"]} '
            f'({it.get("domain")}, {it.get("ts") or "без даты"})\n'
            + (f'ТЕКСТ: {body}' if body else f'СНИППЕТ: {(it.get("snippet") or "")[:300]}'))
    group_keys = ", ".join(k for k, _ in _NEWS_GROUPS)
    user = (
        f"Дата: {today_ru()}. Позиции ({len(picks)}):\n\n" + "\n---\n".join(blocks)
        + f"\n\nВерни СТРОГО JSON без markdown. Допустимые key групп: {group_keys}.\n"
          "Смысл групп: sber — всё про Сбер; regulatory — ЦБ, законы, ставка; "
          "incidents — сбои, утечки, хищения, мошенничество; market — конкуренты "
          "и движения рынка; other — важное, не подошедшее выше.\n"
          'Формат: {"groups":[{"key":"regulatory","items":[{"n":3,'
          '"headline":"...","summary":"...","why":"...","severity":"amber"}]}]}\n'
          "severity: red — прямая угроза/инцидент, amber — наблюдать, green — "
          "благоприятное/нейтральное. Группы без позиций не включай."
    )
    raw, ti, to = await _chat(insight_model(), today_anchor() + "\n\n" + _EDIT_SYSTEM,
                              user, max_tokens=3500, temperature=0.1)
    try:
        parsed = _loose_json_loads(raw)
    except ValueError:          # обрезка/флак парсинга → один дешёвый ретрай
        raw, ti2, to2 = await _chat(insight_model(),
                                    today_anchor() + "\n\n" + _EDIT_SYSTEM,
                                    user, max_tokens=3500, temperature=0.0)
        ti, to = ti + ti2, to + to2
        parsed = _loose_json_loads(raw)
    titles = {k: t for k, t in _NEWS_GROUPS}
    picks_set = set(picks)
    groups = []
    for g in (parsed.get("groups") or []):
        key = str(g.get("key") or "").strip()
        if key not in titles:
            key = next((k for k in titles if k in key), "market")
        out_items = []
        for gi in (g.get("items") or [])[:6]:
            try:
                n = int(gi.get("n"))
            except (TypeError, ValueError):
                continue
            if n not in picks_set:      # редакция не добавляет отвергнутое триажом
                continue
            src = items[n - 1]
            v = verdicts.get(n) or {}
            sev = str(gi.get("severity") or "amber")
            head = str(gi.get("headline") or "").strip()[:120]
            out_items.append({
                # headline редакции показываем как title; исходник — рядом
                "title": head or src["title"],
                **({"src_title": src["title"]} if head and head != src["title"] else {}),
                "url": src["url"], "domain": src.get("domain"),
                "source": src["source"], "ts": src.get("ts"), "tag": src.get("tag"),
                "image": src.get("image"),
                "score": v.get("score"), "event": v.get("type"),
                "echo": int(src.get("echo") or 1),
                "products": _news_products(
                    f'{src["title"]} {gi.get("summary") or ""}'),
                "summary": str(gi.get("summary") or "")[:240],
                "why": str(gi.get("why") or "")[:240],
                "severity": sev if sev in ("red", "amber", "green") else "amber",
            })
        if out_items:
            groups.append({"key": key, "title": titles.get(key, key),
                           "items": out_items})
    _ord = {k: i for i, (k, _) in enumerate(_NEWS_GROUPS)}
    groups.sort(key=lambda g: _ord.get(g["key"], 99))
    return groups, (ti, to)


async def news(day: date) -> dict:
    """Секция новостей: конвейер триаж → жюри → фетч → редакция; любой сбой
    конвейера → _news_legacy (одновызовный путь, работал до этапа 2)."""
    from . import news as news_mod
    items, statuses = await asyncio.to_thread(news_mod.fetch_all)
    if not items:
        return {"groups": [], "items_raw": [], "sources": statuses,
                "_status": "degraded"}
    try:
        verdicts = await _news_triage(items)
        tok = verdicts.pop("_tokens", {"in": 0, "out": 0})  # type: ignore[arg-type]
        ti, to = int(tok.get("in") or 0), int(tok.get("out") or 0)
        keep = [n for n, v in verdicts.items() if v["score"] >= _TRIAGE_MIN]
        border = sorted((n for n, v in verdicts.items()
                         if _JURY_LOW <= v["score"] < _TRIAGE_MIN),
                        key=lambda n: -verdicts[n]["score"])[:8]
        if border:      # два скептика на пограничную; выжило — в выпуск
            votes = await asyncio.gather(*(_news_jury(items[n - 1]) for n in border),
                                         return_exceptions=True)
            keep += [n for n, ok in zip(border, votes) if ok is True]
        keep.sort(key=lambda n: -verdicts[n]["score"])
        keep = keep[:_MAX_PICKS]
        if not keep:    # честно тихий день — без добора мусором
            return {"groups": [], "sources": statuses, "raw_count": len(items),
                    "pool": _news_pool(items), "quiet": True,
                    "triage": {"kept": 0, "border": len(border)},
                    "_llm_model": insight_model(), "_tokens_in": ti, "_tokens_out": to}
        bodies = await asyncio.to_thread(
            _news_bodies, [items[n - 1].get("url") or "" for n in keep])
        groups, (ei, eo) = await _news_editorial(items, keep, bodies, verdicts)
        ti, to = ti + ei, to + eo
        if not groups:
            raise ValueError("редакция вернула пустые группы")
        # межднёвная память: опубликованное сегодня завтра в пул не возвращается
        try:
            await asyncio.to_thread(
                news_mod.mark_published,
                [it["url"] for g in groups for it in g["items"] if it.get("url")])
        except Exception:  # noqa: BLE001 — память не должна ронять секцию
            log.warning("news: mark_published failed", exc_info=True)
        return {"groups": groups, "sources": statuses, "raw_count": len(items),
                "pool": _news_pool(items),
                "triage": {"kept": len(keep), "border": len(border),
                           "fetched": len(bodies)},
                "_llm_model": insight_model(), "_tokens_in": ti, "_tokens_out": to}
    except Exception as e:  # noqa: BLE001 — конвейер сломался, не выпуск
        log.warning("news conveyor failed (%s) — одновызовный путь", e)
        return await _news_legacy(items, statuses)


async def _news_legacy(items: list[dict], statuses: list[dict]) -> dict:
    """Одновызовный отбор (до этапа 2) — страховка при сбое конвейера."""
    from . import news as news_mod
    listing = "\n".join(
        f'#{i + 1} [{it["tag"]}] {it["title"]} — {(it.get("snippet") or "")[:160]} '
        f'({it.get("domain")}, {it.get("ts") or "без даты"})'
        for i, it in enumerate(items))
    group_keys = ", ".join(k for k, _ in _NEWS_GROUPS)
    user = (
        f"Дата: {today_ru()}. Лента ({len(items)} позиций):\n{listing}\n\n"
        f"Верни СТРОГО JSON без markdown. Допустимые key групп: {group_keys}.\n"
        "Смысл групп: sber — всё про Сбер (продукты, технологии, сервисы, экосистема); "
        "regulatory — ЦБ, законы, ключевая ставка, макроэкономика; "
        "incidents — сбои, утечки, хищения, схемы мошенничества; "
        "market — конкуренты, их продукты и ставки, движения рынка; "
        "other — важное аудитору, но не подошедшее выше (используй редко).\n"
        "Пример формата (значения — твои):\n"
        '{"groups":[{"key":"regulatory","items":[{"n":3,'
        '"summary":"1 предложение сути","why":"почему важно аудитору розницы Сбера, '
        '1 фраза","severity":"amber"}]}]}\n'
        "Всего не больше 12 позиций, в каждой группе не больше 4. Группы без "
        "позиций не включай. severity: red — прямая угроза/инцидент, amber — "
        "наблюдать, green — благоприятное/нейтральное."
    )
    try:
        raw, ti, to = await _chat(insight_model(),
                                  today_anchor() + "\n\n" + _NEWS_SYSTEM,
                                  user, max_tokens=3000, temperature=0.1)
        try:
            parsed = _loose_json_loads(raw)
        except ValueError:      # обрезка/флак парсинга → один дешёвый ретрай
            raw, ti2, to2 = await _chat(insight_model(),
                                        today_anchor() + "\n\n" + _NEWS_SYSTEM,
                                        user, max_tokens=3000, temperature=0.0)
            ti, to = ti + ti2, to + to2
            parsed = _loose_json_loads(raw)
        titles = {k: t for k, t in _NEWS_GROUPS}
        groups = []
        for g in (parsed.get("groups") or []):
            key = str(g.get("key") or "").strip()
            if key not in titles:       # модель скопировала альтернативу/мусор
                key = next((k for k in titles if k in key), "market")
            out_items = []
            for gi in (g.get("items") or [])[:4]:
                try:
                    n = int(gi.get("n"))
                except (TypeError, ValueError):
                    continue
                if not (1 <= n <= len(items)):
                    continue
                src = items[n - 1]
                sev = str(gi.get("severity") or "amber")
                out_items.append({
                    "title": src["title"], "url": src["url"],
                    "domain": src.get("domain"), "source": src["source"],
                    "ts": src.get("ts"), "tag": src.get("tag"),
                    "image": src.get("image"),
                    "products": _news_products(
                        f'{src["title"]} {gi.get("summary") or ""}'),
                    "summary": str(gi.get("summary") or "")[:220],
                    "why": str(gi.get("why") or "")[:200],
                    "severity": sev if sev in ("red", "amber", "green") else "amber",
                })
            if out_items:
                groups.append({"key": key, "title": titles.get(key, key),
                               "items": out_items})
        if not groups:
            raise ValueError("LLM вернул пустые группы")
        _ord = {k: i for i, (k, _) in enumerate(_NEWS_GROUPS)}
        groups.sort(key=lambda g: _ord.get(g["key"], 99))  # стабильный порядок рубрик
        # межднёвная память: опубликованное сегодня завтра в пул не возвращается
        try:
            await asyncio.to_thread(
                news_mod.mark_published,
                [it["url"] for g in groups for it in g["items"] if it.get("url")])
        except Exception:  # noqa: BLE001 — память не должна ронять секцию
            log.warning("news: mark_published failed", exc_info=True)
        return {"groups": groups, "sources": statuses, "raw_count": len(items),
                "pool": _news_pool(items),
                "_llm_model": insight_model(), "_tokens_in": ti, "_tokens_out": to}
    except Exception as e:  # noqa: BLE001 — деградация: сырые заголовки без LLM
        log.warning("news digest LLM failed: %s", e)
        return {"groups": [], "sources": statuses, "raw_count": len(items),
                "items_raw": [{k: it.get(k) for k in
                               ("title", "url", "domain", "source", "ts", "tag", "image")}
                              for it in items[:15]],
                "pool": _news_pool(items),
                "_status": "degraded"}


# ── headline (+insights) ──────────────────────────────────────────────────────

_HEAD_SYSTEM = (
    "Ты — главный редактор утреннего брифинга службы внутреннего аудита Сбербанка "
    "(розничный бизнес). Тебе дают ГОТОВЫЕ сигналы дня с точными числами (id в "
    "скобках). Твоя работа: выбрать главное, написать заголовок дня и 3–6 "
    "карточек-инсайтов ЧЕЛОВЕЧЕСКИМ языком. Числа бери ТОЛЬКО из сигналов, ничего "
    "не выдумывай. Рекомендации — внутренние действия по Сберу (конкуренты — "
    "бенчмарк и ранний сигнал, НЕ «перейти/закупить у них»). Без эмодзи."
)

# Раньше здесь жила локальная копия с битыми ключами (autocredit/credit_card
# вместо auto_loan/card_credit из enum) — LLM получал сырые слаги
from ..categories import CAT_RU as _CAT_RU


def _cat_ru(c: str) -> str:
    return _CAT_RU.get(c or "", c or "")


def _build_candidates(secs: dict) -> tuple[list[str], dict[str, dict]]:
    """Кандидаты-сигналы для LLM + реестр ref → данные (для обогащения)."""
    lines, reg = [], {}
    rp = (secs.get("reviews_pulse") or {}).get("payload") or {}
    for s in (rp.get("signals") or [])[:6]:
        ref = f"rev:{s['key']}"
        reg[ref] = {"kind": "review_spike", "data": s}
        bits = [f'{s["week"]} за 7 дн']
        if s.get("ratio"):
            bits.append(f'×{s["ratio"]} к норме')
        if s.get("new"):
            bits.append("новая тема")
        if s.get("accel"):
            bits.append("ускоряется")
        if s.get("bank_specific"):
            bits.append("только у Сбера")
        if s.get("geo"):
            bits.append(f'{s["geo"]["share"]}% из {s["geo"]["city"]}')
        lines.append(f'({ref}) жалобы «{s["label"]}» [{s.get("level")}]: ' + ", ".join(bits))
    ov = rp.get("overall") or {}
    if ov.get("week") is not None:
        lines.append(f'(ctx) всего жалоб за нед: {ov["week"]}, норма ~{ov.get("baseline_week")}'
                     + (f', рынок ×{ov["market_ratio"]}' if ov.get("market_ratio") is not None else ""))

    tm = (secs.get("tariff_moves") or {}).get("payload") or {}
    for m in (tm.get("mass_updates") or [])[:3]:
        ref = f"mass:{m['category']}"
        reg[ref] = {"kind": "mass_move", "data": m,
                    "after_pause": tm.get("after_pause")}
        note = " (возможен артефакт: сбор после паузы)" if tm.get("after_pause") else ""
        lines.append(f'({ref}) массовое движение: {m["n_banks"]} банков изменили '
                     f'ставки «{_cat_ru(m["category"])}» за 48 ч'
                     f' ({", ".join(m["banks"][:4])}){note}')
    for i, c in enumerate((tm.get("top_changes") or [])[:5]):
        ref = f"chg:{i}"
        reg[ref] = {"kind": "tariff_move", "data": c}
        lines.append(f'({ref}) {c["bank"]}: «{c["title"]}» ({_cat_ru(c["category"])}) '
                     f'{c["from"]}% → {c["to"]}% (Δ{c["delta"]:+})')
    kr = tm.get("key_rate") or {}
    if kr.get("current") is not None:
        reg["rate"] = {"kind": "rate_move", "data": kr}
        spread = tm.get("dep_spread_pp")
        lines.append(f'(rate) ключевая ставка {kr["current"]}% (на {kr.get("as_of")})'
                     + (f', спред макс.вклад Сбера − КС: {spread:+} пп' if spread is not None else ""))

    nw = (secs.get("news") or {}).get("payload") or {}
    ni = 0
    for g in (nw.get("groups") or []):
        for it in g.get("items") or []:
            ref = f"news:{ni}"
            reg[ref] = {"kind": "news_alert", "data": it, "group": g.get("key")}
            lines.append(f'({ref}) новость [{g.get("key")}/{it.get("severity")}]: '
                         f'{it["title"]} — {it.get("why") or it.get("summary") or ""}')
            ni += 1
            if ni >= 10:
                break
        if ni >= 10:
            break

    qo = (secs.get("quality_ops") or {}).get("payload") or {}
    if qo.get("flags_err"):
        lines.append(f'(ctx) флаги качества данных: {qo["flags_err"]} error, '
                     f'{qo.get("flags_warn", 0)} warn — цифры проверяй с оглядкой')
    return lines, reg


def _ai_prompt(kind: str, d: dict) -> str:
    if kind == "review_spike":
        geo = d.get("geo") or {}
        parts = [f'Разбери всплеск жалоб на тему «{d["label"]}» у Сбербанка: '
                 f'{d["week"]} за 7 дней против ~{d.get("baseline_week")}/нед'
                 + (f' (×{d["ratio"]})' if d.get("ratio") else "")]
        if geo:
            parts.append(f'{geo["share"]}% жалоб из г. {geo["city"]}')
        if d.get("bank_specific"):
            parts.append("рынок по теме ровный — похоже на нашу регрессию")
        parts.append("Найди вероятную причину, оцени регуляторный риск и предложи шаги аудита.")
        return ". ".join(parts)
    if kind == "mass_move":
        return (f'За последние 48 часов {d["n_banks"]} банков '
                f'({", ".join(d["banks"][:5])}) изменили ставки в категории '
                f'«{_cat_ru(d["category"])}». Разбери это движение рынка: вероятные '
                f'причины, сравнение с позицией Сбера, риски и действия для аудита розницы.')
    if kind == "tariff_move":
        return (f'Банк {d["bank"]} изменил ставку по продукту «{d["title"]}» '
                f'({_cat_ru(d["category"])}) с {d["from"]}% до {d["to"]}%. Оцени '
                f'значимость для позиции Сбера и стоит ли реагировать.')
    if kind == "rate_move":
        return (f'Ключевая ставка сейчас {d.get("current")}%. Проанализируй влияние '
                f'на розничные продукты Сбера (вклады, кредиты, ипотека) и позицию '
                f'относительно рынка.')
    if kind == "news_alert":
        return (f'Проанализируй новость для аудита розничного бизнеса Сбера: '
                f'«{d.get("title")}» ({d.get("url")}). Какие риски и какие действия '
                f'стоит предпринять?')
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
    if kind == "mass_move":
        return {"page": "market",
                "params": {"category": d.get("category"), "view": "changes"}}
    if kind == "rate_move":
        return {"page": "market", "params": {"view": "changes"}}
    if kind == "news_alert":
        return {"url": d.get("url")}
    return {}


def _provenance(kind: str, d: dict) -> str:
    if kind == "review_spike":
        return f'banki.ru · {d.get("week")} жалоб/7дн · норма — среднее за 7 нед + сверка с рынком'
    if kind in ("mass_move", "tariff_move"):
        return "журнал изменений тарифов (banki.ru/sravni.ru)"
    if kind == "rate_move":
        return f'ЦБ РФ · официально · на {d.get("as_of")}'
    if kind == "news_alert":
        return f'{d.get("domain") or d.get("source") or "пресса"}'
    return ""


def _fallback_headline(reg: dict[str, dict]) -> dict:
    """LLM недоступен → детерминированная передовица из топ-сигналов."""
    insights = []
    for ref, meta in list(reg.items())[:4]:
        d, kind = meta["data"], meta["kind"]
        if kind == "review_spike":
            title = (f'Всплеск жалоб «{d["label"]}»: {d["week"]} за неделю'
                     + (f' (×{d["ratio"]})' if d.get("ratio") else ""))
            sev = "risk" if d.get("level") == "high" else "watch"
        elif kind == "mass_move":
            title = f'{d["n_banks"]} банков изменили ставки «{_cat_ru(d["category"])}» за 48 ч'
            sev = "watch"
        elif kind == "tariff_move":
            title = f'{d["bank"]}: {d["from"]}% → {d["to"]}% ({_cat_ru(d["category"])})'
            sev = "watch"
        elif kind == "rate_move":
            title = f'Ключевая ставка {d.get("current")}%'
            sev = "neutral"
        else:
            title = d.get("title") or ""
            sev = {"red": "risk", "amber": "watch", "green": "good"}.get(
                d.get("severity"), "neutral")
        insights.append({"ref": ref, "severity": sev, "likelihood": 2, "impact": 2,
                         "title": title, "so_what": ""})
    head = insights[0]["title"] if insights else f"Сводка за {today_ru()}"
    return {"headline": head, "hot": "", "insights": insights}


async def headline(day: date) -> dict:
    secs = await asyncio.to_thread(store._read_day_rows, day)
    lines, reg = _build_candidates(secs)
    brief_md = ((secs.get("reviews_brief") or {}).get("payload") or {}).get("markdown")

    result, ti, to, model, degraded = None, 0, 0, None, False
    if lines:
        user = (
            f"Дата выпуска: {today_ru()}.\nСИГНАЛЫ ДНЯ:\n" + "\n".join(lines)
            + (f"\n\nАНАЛИЗ ЖАЛОБ (для контекста):\n{brief_md[:1200]}" if brief_md else "")
            + "\n\nВерни СТРОГО JSON без markdown:\n"
              '{"headline":"заголовок дня, до 90 знаков, самый сильный сигнал",'
              '"hot":"СКОПИРУЙ ДОСЛОВНО 2-4 слова из headline (теми же буквами и '
              'регистром) — самый важный фрагмент для оранжевого акцента; '
              'предпочти цифру/название банка, если есть",'
              '"insights":[{"ref":"<id сигнала из скобок>","severity":"risk|watch|good|neutral",'
              '"likelihood":1-3,"impact":1-3,"title":"инсайт человеческим языком, с цифрой",'
              '"so_what":"почему важно аудитору розницы Сбера, 1-2 фразы"}],'
              '"quiet_note":"1 фраза про то, где спокойно (или пустая строка)"}\n'
              "3–6 инсайтов, отсортируй по важности для аудита. ref бери ТОЛЬКО из списка."
        )
        try:
            # 3000 токенов: gemini многословен (обёртка ```json + полные инсайты),
            # при 6 сигналах на проде 1400 обрезало JSON посреди строки → парс падал
            raw, ti, to = await _chat(insight_model(),
                                      today_anchor() + "\n\n" + _HEAD_SYSTEM,
                                      user, max_tokens=3000, temperature=0.3)
            try:
                result = _loose_json_loads(raw)
            except ValueError:              # обрезка/мусор → ретрай холоднее
                raw, ti2, to2 = await _chat(insight_model(),
                                            today_anchor() + "\n\n" + _HEAD_SYSTEM,
                                            user, max_tokens=3000, temperature=0.0)
                ti, to = ti + ti2, to + to2
                result = _loose_json_loads(raw)
            model = insight_model()
        except Exception as e:  # noqa: BLE001
            log.warning("headline LLM failed: %s", e)
    if result is None:
        result = _fallback_headline(reg)
        degraded = True

    # обогащение инсайтов детерминированным кодом (drill/ai_prompt/viz/provenance)
    def _enrich(raw_list: list) -> list:
        out, seen_refs = [], set()
        for ins in (raw_list or [])[:10]:
            ref = str(ins.get("ref") or "").strip().strip("()")
            meta = reg.get(ref)
            if not meta or ref in seen_refs:    # дедуп: не 3 карточки про одно
                continue
            seen_refs.add(ref)
            kind, d = meta["kind"], meta["data"]
            sev = str(ins.get("severity") or "watch")
            try:
                lik = max(1, min(3, int(ins.get("likelihood") or 2)))
                imp = max(1, min(3, int(ins.get("impact") or 2)))
            except (TypeError, ValueError):
                lik, imp = 2, 2
            out.append({
                "ref": ref, "kind": kind,
                "severity": sev if sev in ("risk", "watch", "good", "neutral") else "watch",
                "likelihood": lik, "impact": imp,
                "title": str(ins.get("title") or "")[:180],
                "so_what": str(ins.get("so_what") or "")[:280],
                "data": d,
                "drill": _drill(kind, d),
                "ai_prompt": _ai_prompt(kind, d),
                "provenance": _provenance(kind, d),
                **({"after_pause": True} if meta.get("after_pause") else {}),
            })
            if len(out) >= 6:
                break
        return out

    insights = _enrich(result.get("insights") or [])
    if not insights and (result.get("insights") or []):
        log.warning("headline: все ref LLM мимо реестра: %s (reg: %s)",
                    [str(i.get("ref"))[:30] for i in result["insights"][:8]],
                    list(reg)[:12])
    if not insights and reg:
        # LLM вернул пусто или ВСЕ ref мимо реестра → детерминированные карточки
        # (заголовок LLM при этом оставляем)
        insights = _enrich(_fallback_headline(reg)["insights"])

    rp = (secs.get("reviews_pulse") or {}).get("payload") or {}
    nw = (secs.get("news") or {}).get("payload") or {}
    n_news = sum(len(g.get("items") or []) for g in (nw.get("groups") or []))
    stats = {
        "risk": sum(1 for i in insights if i["severity"] == "risk"),
        "good": sum(1 for i in insights if i["severity"] == "good"),
        "news": n_news,
        "checked_themes": (rp.get("checked") or {}).get("themes") or 0,
    }
    return {
        "headline": str(result.get("headline") or "")[:160] or f"Сводка за {today_ru()}",
        "hot": str(result.get("hot") or "")[:60],
        "quiet_note": str(result.get("quiet_note") or "")[:200],
        "insights": insights,
        "stats": stats,
        **({"_status": "degraded"} if degraded else {}),
        **({"_llm_model": model, "_tokens_in": ti, "_tokens_out": to} if model else {}),
    }
