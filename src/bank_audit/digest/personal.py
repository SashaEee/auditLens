"""Персональный разворот «Для вас» (v2): полноценная страница поверх общего ядра.

Собирает per-user (1 LLM-вызов/сутки):
  • headline+lead+checks — редакторская «шапка» и 2-3 аудиторские зацепки (insight-модель);
  • focus     — стат-карты по фокус-продуктам аудитора (жалобы Сбера: reviews_dash);
  • news      — персональная новостная сетка из полного пула дня (ре-ранк, 0 LLM);
  • for_you   — топ-3 для «личной полосы» на главной (детерминированный ре-ранк);
  • tariffs   — тарифные движения Сбера/рынка в фокус-категориях;
  • quiet     — честная тишина, если по темам сигналов нет.
Кэш — personal_digest(username, local_date), один писатель: «полоса» на главной читает
своё подмножество из этого же payload. Числа только из данных ядра (анти-галлюцинации).
Личный слой НИКОГДА не роняет общий «Обзор»: всё best-effort, ошибки → пустой слой.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

from openai import AsyncOpenAI

from ..ai.analyst import insight_model
from ..ai.llm_utils import (_loose_json_loads, _patch_client_reasoning_effort,
                            detect_bank_slugs)
from ..clock import MSK, today_ru
from . import store
from ..web import userdata

log = logging.getLogger(__name__)

# slug → человекочитаемая метка (для «почему вам» и топ-тем).
_LABEL = {
    "sberbank": "Сбербанк", "vtb": "ВТБ", "alfabank": "Альфа-Банк", "tinkoff": "Т-Банк",
    "gazprombank": "Газпромбанк", "rshb": "Россельхозбанк", "domrf": "ДОМ.РФ", "psb": "ПСБ",
    "sovcombank": "Совкомбанк", "mtsbank": "МТС-Банк", "raiffeisen": "Райффайзен",
    "ipoteka": "ипотека", "deposit": "вклады", "credit_card": "кредитные карты",
    "debit_card": "дебетовые карты", "consumer_loan": "потребкредиты", "auto": "автокредиты",
    "rko": "РКО", "savings": "накопительные счета", "acquiring": "эквайринг",
    "premium": "премиальные пакеты", "transfers": "переводы и комиссии",
}


def _label(t: str) -> str:
    return _LABEL.get(t, t)


def _first_name(name: str) -> str:
    return (name or "").strip().split()[0] if (name or "").strip() else ""


def _tag(text: str) -> set[str]:
    """Детерминированные теги (банки+продукты) из текста пункта."""
    text = text or ""
    topics = set(detect_bank_slugs(text))
    for rx, slug in userdata._PRODUCT_KEYWORDS:
        if rx.search(text):
            topics.add(slug)
    return topics


def _candidates(sections: dict) -> list[dict]:
    """Пункты-кандидаты из секций ядра, тегированные банками/продуктами."""
    out: list[dict] = []
    news = (sections.get("news") or {}).get("payload") or {}
    for g in (news.get("groups") or []):
        for it in (g.get("items") or []):
            txt = " ".join(str(it.get(k) or "") for k in ("title", "summary", "why"))
            out.append({
                "kind": "news", "title": it.get("title"),
                "summary": it.get("summary") or it.get("why") or "",
                "url": it.get("url"), "severity": it.get("severity") or "amber",
                "group": g.get("title"), "topics": _tag(txt),
            })
    tm = (sections.get("tariff_moves") or {}).get("payload") or {}
    for m in (tm.get("top_changes") or [])[:12]:
        txt = " ".join(str(m.get(k) or "") for k in ("bank", "category", "title"))
        topics = _tag(txt)
        if m.get("category"):
            topics.add(str(m["category"]))
        delta = m.get("delta")
        dstr = (f" {float(delta):+.2f} п.п." if isinstance(delta, (int, float)) else "")
        out.append({
            "kind": "tariff",
            "title": f'{m.get("bank")}: {m.get("title") or m.get("category") or "изменение тарифа"}',
            "summary": f'ставка {m.get("from")}→{m.get("to")}{dstr}',
            "severity": "amber", "topics": topics,
        })
    rp = (sections.get("reviews_pulse") or {}).get("payload") or {}
    for th in (rp.get("themes_up") or [])[:8]:
        lbl = th.get("label") or th.get("theme") or th.get("name") or ""
        if not lbl:
            continue
        topics = _tag(lbl) | {"sberbank"}   # reviews_pulse — Сбер
        dp = th.get("delta_pct")
        mstr = f' +{int(dp)}%' if isinstance(dp, (int, float)) and dp > 0 else ""
        out.append({
            "kind": "review", "title": f'Жалобы: {lbl}',
            "summary": f'рост темы жалоб{mstr} по Сберу',
            "severity": "red", "topics": topics,
        })
    return out


def _score(cand: dict, weights: dict, custom: list[str]) -> tuple[float, list[str], list[str]]:
    s, labels, slugs = 0.0, [], []
    for t in cand.get("topics", ()):
        w = weights.get(t)
        if w:
            s += w
            labels.append(_label(t)); slugs.append(t)
    text = (str(cand.get("title") or "") + " " + str(cand.get("summary") or "")).lower()
    for c in custom:
        if c and c.lower() in text:
            s += 2.5
            labels.append(c)
    return s, labels, slugs


_COMPETITORS = {"vtb", "alfabank", "tinkoff", "gazprombank", "rshb", "domrf",
                "psb", "sovcombank", "mtsbank", "raiffeisen", "otkritie"}
_PRODUCTS = {"ipoteka", "deposit", "credit_card", "debit_card", "consumer_loan",
             "auto", "rko", "savings", "acquiring", "premium", "transfers"}


def _for_you(cands: list[dict], weights: dict, custom: list[str], k: int = 3) -> list[dict]:
    """Ре-ранк под Сбер-аудитора: Сбер — якорь, конкуренты — только рыночный бенчмарк."""
    scored = []
    for c in cands:
        s, labels, slugs = _score(c, weights, custom)
        if s <= 0:
            continue
        topics = c.get("topics", set()) or set()
        is_sber = "sberbank" in topics
        comp_only = bool(topics & _COMPETITORS) and not is_sber
        if is_sber:
            s += 1.5                       # якорь: Сбер важнее
        if comp_only:
            s *= 0.3                        # чисто конкурент — только как рыночный контекст
        scored.append((s, labels, slugs, is_sber, c))
    scored.sort(key=lambda x: -x[0])
    seen, out = set(), []
    for _s, labels, slugs, is_sber, c in scored:
        key = (c.get("title") or "")[:40]
        if key in seen:
            continue
        seen.add(key)
        # Причина в Сбер-формулировке: «Сбер · <продукты>»; банки-конкуренты в причину не кладём.
        prod_labels = [_label(sl) for sl in slugs if sl in _PRODUCTS]
        for c2 in custom:
            if c2 and c2.lower() in ((c.get("title") or "") + " " + (c.get("summary") or "")).lower():
                prod_labels.append(c2)
        prod_labels = list(dict.fromkeys(prod_labels))[:2]
        reason_parts = (["Сбер"] if is_sber else ["рынок"]) + prod_labels
        out.append({
            "title": c.get("title"), "summary": c.get("summary"), "url": c.get("url"),
            "severity": c.get("severity"), "kind": c.get("kind"),
            "reason": " · ".join(reason_parts),
            "reason_slugs": [sl for sl in slugs if sl in _PRODUCTS][:3],
        })
        if len(out) >= k:
            break
    return out


def _client() -> AsyncOpenAI:
    base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    return _patch_client_reasoning_effort(
        AsyncOpenAI(base_url=base, api_key=key, timeout=70, max_retries=1))


# ── страница «Для вас» (v2) ───────────────────────────────────────────────────

_PAGE_V = 2

# интерес-слаг → кандидаты меток r."product" БД bankiru (сравнение в SQL строгое,
# фактический список меток подтягиваем в рантайме через reviews_dash.products)
_SLUG_PRODUCT_CANDIDATES = {
    "deposit":       ("вклад", "вклады"),
    "credit_card":   ("кредитная карта", "кредитные карты"),
    "ipoteka":       ("ипотека",),
    "debit_card":    ("дебетовая карта", "дебетовые карты"),
    "transfers":     ("денежный перевод", "денежные переводы"),
    "consumer_loan": ("потребительский кредит", "кредит наличными",
                      "потребительские кредиты"),
    "auto":          ("автокредит", "автокредиты"),
    "rko":           ("обслуживание юридических лиц",),
    "acquiring":     ("обслуживание юридических лиц",),
    "savings":       ("накопительный счёт", "накопительные счета"),
    "premium":       ("премиальное обслуживание",),
}
# ключ темы reviews_dash.THEMES → интерес-слаг (для «горячей темы» карточки)
_THEME_SLUG = {"deposit": "deposit", "mortgage": "ipoteka", "transfer": "transfers"}
# категория тарифного трекера → интерес-слаг
_TARIFF_CAT_SLUG = {"deposit": "deposit", "mortgage": "ipoteka",
                    "card_credit": "credit_card", "card_debit": "debit_card",
                    "auto_loan": "auto", "credit": "consumer_loan"}


def _focus_slugs(weights: dict, k: int = 4) -> tuple[list[str], bool]:
    """Фокус-продукты по весам профиля; без профиля — популярный стартовый набор."""
    prods = sorted(((t, w) for t, w in weights.items() if t in _PRODUCTS),
                   key=lambda x: -x[1])
    if prods:
        return [t for t, _ in prods[:k]], False
    return list(userdata._POPULAR[:3]), True


def _focus_cards(slugs: list[str]) -> list[dict]:
    """Стат-карты фокус-продуктов (жалобы Сбера): 90 дней + тренд + горячая тема.
    Синхронный SQL (кэш reviews_dash 1ч) — звать через asyncio.to_thread."""
    from ..rag import reviews_dash as rd
    # top=100 — нужен фактический СЛОВАРЬ меток БД, а не топ-10 UI-виджета:
    # нишевые продукты (РКО, премиум) в топ-10 Сбера не попадают
    plist = [(p.get("product") or "", int(p.get("n") or 0))
             for p in ((rd.products("Сбербанк", top=100) or {}).get("items") or [])]
    theme_by_key = {t.get("key"): t
                    for t in ((rd.themes("Сбербанк") or {}).get("themes") or [])}

    def db_label(slug: str) -> str | None:
        for cand in _SLUG_PRODUCT_CANDIDATES.get(slug, ()):
            for label, _n in plist:
                if label.lower() == cand:
                    return label
        return None

    cards: list[dict] = []
    for slug in slugs:
        label = db_label(slug)
        ov = rd.overview("Сбербанк", label) if label else None
        tr = rd.trend("Сбербанк", label) if label else None
        tkey = next((k for k, s in _THEME_SLUG.items() if s == slug), None)
        theme = theme_by_key.get(tkey) if tkey else None
        if not ov and not theme:
            continue                     # ни product-метки, ни темы — карточку не рисуем
        cards.append({
            "slug": slug, "label": _label(slug), "product": label,
            "stats": ({k: ov.get(k) for k in
                       ("total", "prev", "delta_pct", "delta_low_n", "market_share_pct",
                        "market_rank", "market_banks", "as_of")} if ov else None),
            "trend": [{"ym": p.get("ym"), "n": p.get("n"), "spike": bool(p.get("spike"))}
                      for p in ((tr or {}).get("series") or [])][-14:],
            "theme": ({k: theme.get(k) for k in
                       ("key", "label", "risk", "n", "pct", "delta_pct")} if theme else None),
        })
        if len(cards) >= 4:
            break
    return cards


def _news_tiles(sections: dict, weights: dict, custom: list[str], k: int = 8) -> list[dict]:
    """Персональная новостная сетка: ре-ранк полного пула дня под профиль (0 LLM).
    Обогащение (summary/severity) подмешиваем из LLM-групп ядра по url."""
    news = (sections.get("news") or {}).get("payload") or {}
    enrich: dict[str, dict] = {}
    for g in (news.get("groups") or []):
        for it in (g.get("items") or []):
            if it.get("url"):
                enrich[it["url"]] = {"summary": it.get("summary") or it.get("why") or "",
                                     "severity": it.get("severity"),
                                     "group": g.get("title"), "image": it.get("image")}
    pool = news.get("pool") or news.get("items_raw") or []
    if not pool:                        # payload до v-pool — падаем на элементы групп
        pool = [dict(it) for g in (news.get("groups") or [])
                for it in (g.get("items") or [])]
    seen, scored = set(), []
    for it in pool:
        url, title = it.get("url") or "", it.get("title") or ""
        key = url or title[:60]
        if not title or key in seen:
            continue
        seen.add(key)
        e = enrich.get(url) or {}
        txt = " ".join([title, str(it.get("snippet") or ""), str(e.get("summary") or "")])
        topics = _tag(txt)
        s, _labels, slugs = _score({"topics": topics, "title": title,
                                    "summary": it.get("snippet")}, weights, custom)
        is_sber = "sberbank" in topics
        comp_only = bool(topics & _COMPETITORS) and not is_sber
        if is_sber:
            s += 1.5
        if comp_only:
            s *= 0.3
        if e.get("severity") == "red":
            s += 0.6                    # редакция ядра пометила как прямую угрозу
        elif e:
            s += 0.25                   # прошла отбор редакции
        if s <= 0:
            continue
        prod_labels = list(dict.fromkeys(
            _label(sl) for sl in slugs if sl in _PRODUCTS))[:2]
        scored.append((s, {
            "title": title, "url": url or None, "domain": it.get("domain"),
            "source": it.get("source"), "ts": it.get("ts"),
            "image": it.get("image") or e.get("image"),
            "summary": (e.get("summary") or it.get("snippet") or "")[:220],
            "severity": e.get("severity"), "group": e.get("group"),
            "reason": " · ".join((["Сбер"] if is_sber else ["рынок"]) + prod_labels),
            "reason_slugs": [sl for sl in slugs if sl in _PRODUCTS][:3],
        }))
    scored.sort(key=lambda x: -x[0])
    return [t for _s, t in scored[:k]]


def _tariff_block(sections: dict, focus: list[str]) -> dict:
    """Тарифный срез: движения Сбера + фокус-категорий, Сбер vs рынок по фокусу."""
    tm = (sections.get("tariff_moves") or {}).get("payload") or {}
    fset = set(focus)
    moves = []
    for m in (tm.get("top_changes") or []):
        slug = _TARIFF_CAT_SLUG.get(str(m.get("category") or ""))
        if m.get("is_sber") or (slug and slug in fset):
            moves.append({**{k: m.get(k) for k in
                             ("bank", "is_sber", "category", "title",
                              "from", "to", "delta", "changed_at")}, "slug": slug})
        if len(moves) >= 6:
            break
    gap = [{"category": r.get("category"), "slug": _TARIFF_CAT_SLUG.get(str(r.get("category"))),
            "sber_max": r.get("sber_max"), "market_max": r.get("market_max"),
            "market_median": r.get("market_median"),
            "sber_vs_median_pp": r.get("sber_vs_median_pp")}
           for r in (tm.get("sber_gap") or [])
           if _TARIFF_CAT_SLUG.get(str(r.get("category"))) in fset
           and r.get("sber_max") is not None]
    return {"moves": moves, "gap": gap[:4],
            "key_rate": (tm.get("key_rate") or {}).get("current")}


_PAGE_SYS = (
    "Ты — редактор персональной страницы «Для вас» аудитора СБЕРБАНКА (служба "
    "внутреннего аудита розницы). КРИТИЧНО: все его проверки — про продукты, тарифы, "
    "процессы и жалобы САМОГО СБЕРА; конкурентов (ВТБ, Альфа, Т-Банк и др.) упоминай "
    "ТОЛЬКО как рыночный бенчмарк и НИКОГДА не предлагай проверять чужие банки. "
    "По-русски, деловой редакторский тон, без приветствий и обращений по имени. "
    "Только данные из контекста, числа НЕ выдумывай.\n"
    "Верни СТРОГО JSON без markdown:\n"
    '{"headline":"заголовок 4-8 слов, газетный, про главное ЕГО дня",'
    '"hot":"точная подстрока headline (2-4 слова) — самое горячее",'
    '"lead":"2-3 предложения: что важно именно ему сегодня и почему",'
    '"checks":[{"title":"что проверить в Сбере, 3-7 слов",'
    '"why":"почему именно сейчас, 1 фраза со ссылкой на сигнал"}]}\n'
    "checks — 2-3 пункта, каждый привязан к конкретному сигналу из контекста. "
    "Если сигналов мало — честно скажи в lead, что по его темам в Сбере спокойно."
)


async def _page_ai(self_desc: str, cards: list[dict], tiles: list[dict],
                   signals: list[dict], tariffs: dict) -> dict:
    """Один LLM-вызов на весь разворот: headline + hot + lead + checks."""
    empty = {"headline": None, "hot": None, "lead": None, "checks": []}
    ctx = [f"Сегодня: {today_ru()}", "Банк (объект аудита): СБЕРБАНК",
           f"Зона ответственности аудитора (его словами): {self_desc or '— (не описана)'}"]
    if cards:
        ctx.append("\nЕго направления — жалобы клиентов Сбера за 90 дней:")
        for c in cards:
            st = c.get("stats") or {}
            line = f"- {c['label']}: {st.get('total', '—')} жалоб"
            if isinstance(st.get("delta_pct"), (int, float)):
                line += f", {st['delta_pct']:+.0f}% к пред. периоду"
            th = c.get("theme")
            if th:
                line += f"; горячая тема: {th.get('label')} ({th.get('n')} шт.)"
            ctx.append(line)
    if signals:
        ctx.append("\nАномалии недели по Сберу: " + "; ".join(
            f"{s.get('label')} (×{s.get('ratio')})" for s in signals if s.get("label")))
    if tiles:
        ctx.append("\nНовости под его профиль (конкуренты — только бенчмарк):")
        ctx += [f"- {t['title']} [{t.get('reason') or '—'}]" for t in tiles[:6]]
    if tariffs.get("moves"):
        ctx.append("\nТарифные движения: " + "; ".join(
            f"{m.get('bank')} · {m.get('title') or m.get('category')}: "
            f"{m.get('from')}→{m.get('to')}" for m in tariffs["moves"][:4]))
    msgs = [{"role": "system", "content": _PAGE_SYS},
            {"role": "user", "content": "\n".join(ctx)}]
    try:
        client = _client()
        r = await client.chat.completions.create(
            model=insight_model(), messages=msgs, temperature=0.4, max_tokens=700)
        raw = (r.choices[0].message.content or "").strip()
        try:
            parsed = _loose_json_loads(raw)
        except ValueError:              # обрезка/флак парсинга → один дешёвый ретрай
            r = await client.chat.completions.create(
                model=insight_model(), messages=msgs, temperature=0.0, max_tokens=700)
            parsed = _loose_json_loads((r.choices[0].message.content or "").strip())
        headline = str(parsed.get("headline") or "").strip()[:90] or None
        hot = str(parsed.get("hot") or "").strip()[:60] or None
        lead = str(parsed.get("lead") or "").strip() or None
        if lead and lead[-1] not in ".!?…»\"":
            cut = max(lead.rfind(". "), lead.rfind("! "), lead.rfind("? "))
            if cut > 40:
                lead = lead[:cut + 1]
        checks = []
        for c in (parsed.get("checks") or [])[:3]:
            t = str((c or {}).get("title") or "").strip()
            if t:
                checks.append({"title": t[:120],
                               "why": str((c or {}).get("why") or "").strip()[:160]})
        return {"headline": headline, "hot": hot, "lead": lead, "checks": checks}
    except Exception:
        log.warning("[personal] page LLM failed", exc_info=True)
        return empty


# защита от параллельной сборки (полоса + страница в двух вкладках → 1 LLM, не 2);
# лок процессный — при нескольких воркерах редкий дубль допустим (upsert идемпотентен)
_BUILD_LOCKS: dict[str, asyncio.Lock] = {}


async def build_foryou(username: str, *, force: bool = False) -> dict | None:
    """Собирает (или берёт из кэша) персональный разворот. None если выключен."""
    lock = _BUILD_LOCKS.setdefault(username, asyncio.Lock())
    async with lock:
        return await _build_foryou_locked(username, force=force)


async def _build_foryou_locked(username: str, *, force: bool = False) -> dict | None:
    user = userdata.get_user(username) or {}
    prefs = user.get("prefs") or {}
    if isinstance(prefs, str):
        prefs = json.loads(prefs or "{}")
    if prefs.get("personal_digest") is False:
        return None
    tz = user.get("timezone") or "Europe/Moscow"
    try:
        from zoneinfo import ZoneInfo
        local_date = datetime.now(ZoneInfo(tz)).date()
    except Exception:
        local_date = datetime.now(MSK).date()

    doc = store.read_latest(datetime.now(MSK).date())

    if not force:
        cached = userdata.get_personal_digest(username, local_date)
        payload = (cached or {}).get("payload") or {}
        # кэш валиден, только пока ядро не сменилось: разворот, собранный в 06:50
        # из вчерашнего выпуска, инвалидируется появлением свежего ядра в 07:0x
        if (payload.get("v") == _PAGE_V and payload.get("digest_date")
                and payload.get("digest_date") == doc.get("date")):
            return payload              # старый payload без v — пересоберём разворотом

    prof = userdata.interest_weight_profile(username)
    # «профиль есть» = что-то кроме постоянного Сбер-якоря (он у всех)
    has_profile = bool(prof["self_desc"] or prof["custom"] or prof.get("pinned")
                       or [t for t in prof["weights"] if t != "sberbank"])
    meta = doc.get("meta") or {}
    sections = doc.get("sections") or {}

    cands = _candidates(sections)
    fy = _for_you(cands, prof["weights"], prof["custom"])
    focus, default_focus = _focus_slugs(prof["weights"])
    top_topics = [_label(t) for t, _ in
                  sorted(prof["weights"].items(), key=lambda x: -x[1])[:5]]
    name = _first_name(user.get("display_name") or username)

    tiles = _news_tiles(sections, prof["weights"], prof["custom"])
    tariffs = _tariff_block(sections, focus)
    rp = (sections.get("reviews_pulse") or {}).get("payload") or {}
    signals = [{k: s.get(k) for k in
                ("key", "label", "short", "risk", "week", "ratio", "new", "accel", "level")}
               for s in (rp.get("signals") or [])[:3]]
    try:
        cards = await asyncio.to_thread(_focus_cards, focus)
    except Exception:
        log.warning("[personal] focus cards failed", exc_info=True)
        cards = []

    ai = (await _page_ai(prof["self_desc"], cards, tiles, signals, tariffs)
          if (has_profile or cards or tiles)
          else {"headline": None, "hot": None, "lead": None, "checks": []})

    payload = {
        "v": _PAGE_V,
        # подмножество «личной полосы» (контракт /api/overview/personal не меняется)
        "name": name, "lead": ai["lead"], "for_you": fy, "top_topics": top_topics,
        "quiet": (not fy and not ai["lead"]), "has_profile": has_profile,
        "digest_date": doc.get("date"),
        # разворот
        "headline": ai["headline"], "hot": ai["hot"], "checks": ai["checks"],
        "focus": cards, "default_focus": default_focus,
        "news": tiles, "tariffs": tariffs, "signals": signals,
        "generated_at": datetime.now(MSK).isoformat(),
    }
    # Кэшируем только «хороший» результат: ядро — СЕГОДНЯШНЕЕ и не в процессе
    # генерации (визит в 06:50 не должен запекать вчерашний выпуск на весь день),
    # а LLM либо отработал, либо был не нужен (разовый сбой → ретрай следующим GET).
    core_today = bool(meta.get("today")) and bool(sections)
    refreshing = bool(meta.get("refreshing"))
    llm_ok = bool(ai["headline"] or ai["lead"] or ai["checks"])
    llm_wanted = bool(has_profile or cards or tiles)
    if force or (core_today and not refreshing and (llm_ok or not llm_wanted)):
        try:
            userdata.save_personal_digest(
                username, local_date, payload,
                llm_model=insight_model() if (ai["lead"] or ai["headline"]) else None)
        except Exception:
            log.warning("[personal] save failed", exc_info=True)
    return payload


async def build_personal(username: str, *, force: bool = False) -> dict | None:
    """Личная полоса на главной — подмножество разворота (один писатель кэша)."""
    p = await build_foryou(username, force=force)
    if p is None:
        return None
    return {k: p.get(k) for k in ("name", "lead", "for_you", "top_topics",
                                  "quiet", "has_profile", "digest_date")}
