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


def _news_pool(items: list[dict]) -> list[dict]:
    """Полный сырой пул дня — сырьё для персонального ре-ранка («Для вас»).
    LLM-группы дают ≤12 позиций на всех; персональная сетка ранжирует из всего пула."""
    return [{"title": it.get("title"), "url": it.get("url"), "domain": it.get("domain"),
             "source": it.get("source"), "ts": it.get("ts"), "tag": it.get("tag"),
             "dimension": it.get("dimension"), "image": it.get("image"),
             "snippet": (it.get("snippet") or "")[:200]} for it in items]


async def news(day: date) -> dict:
    from . import news as news_mod
    items, statuses = await asyncio.to_thread(news_mod.fetch_all)
    if not items:
        return {"groups": [], "items_raw": [], "sources": statuses,
                "_status": "degraded"}

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
        return f'banki.ru · {d.get("week")} жалоб/7дн · базлайн 6 нед + рынок'
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
