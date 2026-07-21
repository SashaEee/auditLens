"""Персональный дайджест «Обзора» (Фаза 3): личный слой поверх общего ядра.

Собирает per-user:
  • lead      — редакторский LLM-абзац «что важно именно вам» (insight-модель, 1/сутки);
  • for_you   — детерминированный ре-ранк пунктов ядра (news/tariff/reviews) по весовому
                профилю тем пользователя, с «почему вам» (0 LLM);
  • quiet     — честная тишина, если по темам сигналов нет.
Кэш — personal_digest(username, local_date). Числа только из данных ядра (анти-галлюцинации).
Личный слой НИКОГДА не роняет общий «Обзор»: всё best-effort, ошибки → пустой слой.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from openai import AsyncOpenAI

from ..ai.analyst import insight_model
from ..ai.llm_utils import detect_bank_slugs, _patch_client_reasoning_effort
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
    for m in (tm.get("top") or [])[:12]:
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
        mult = th.get("mult") or th.get("growth") or th.get("x")
        mstr = f' ×{mult}' if mult else ""
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


_LEAD_SYS = (
    "Ты пишешь ЛИЧНУЮ утреннюю сводку для аудитора СБЕРБАНКА (служба внутреннего аудита "
    "розницы Сбера). КРИТИЧНО: все его проверки — про продукты, тарифы, процессы и жалобы "
    "САМОГО СБЕРА. Конкурентов (ВТБ, Альфа, Т-Банк и др.) упоминай ТОЛЬКО как рыночный "
    "бенчмарк/контекст («рынок повышает ставки», «Сбер на фоне рынка»), и НИКОГДА не предлагай "
    "проверять или аудировать чужие банки — это не его зона ответственности. "
    "СТРОГО 2–3 предложения, максимально ёмко, деловой редакторский тон, по-русски. НЕ "
    "здоровайся и НЕ начинай с имени — приветствие показано отдельно; сразу к сути. Свяжи "
    "1–2 главных сигнала дня с его зоной ответственности В СБЕРЕ: на что взглянуть у Сбера, "
    "где вероятны расхождения. Только по предоставленным сигналам, НЕ выдумывай числа/события. "
    "Если сигналов мало — коротко скажи, что по его темам в Сбере спокойно. Без воды, без списков."
)


async def _lead(name: str, self_desc: str, top_topics: list[str], for_you: list[dict]) -> str | None:
    if not for_you and not self_desc:
        return None
    signals = "\n".join(
        f"- {x.get('title')}: {x.get('summary') or ''} (ваш фокус: {x.get('reason') or '—'})"
        for x in for_you[:6]
    ) or "— (значимых сигналов в его темах сегодня нет)"
    user_msg = (
        f"Сегодня: {today_ru()}\n"
        f"Банк (объект аудита): СБЕРБАНК\n"
        f"Аудитор: {name}\n"
        f"Зона ответственности в Сбере (его словами): {self_desc or '— (не описана)'}\n"
        f"Ключевые продукты в фокусе: {', '.join(top_topics) or '—'}\n\n"
        f"Сигналы дня (для контекста; конкуренты — только бенчмарк):\n{signals}"
    )
    try:
        r = await _client().chat.completions.create(
            model=insight_model(),
            messages=[{"role": "system", "content": _LEAD_SYS},
                      {"role": "user", "content": user_msg}],
            temperature=0.4, max_tokens=300,
        )
        note = (r.choices[0].message.content or "").strip()
        # Защита от обрыва на полуслове: если не заканчивается концом предложения —
        # обрезаем до последнего целого предложения.
        if note and note[-1] not in ".!?…»\"":
            cut = max(note.rfind(". "), note.rfind("! "), note.rfind("? "))
            if cut > 40:
                note = note[:cut + 1]
        return note or None
    except Exception:
        log.warning("[personal] lead LLM failed", exc_info=True)
        return None


async def build_personal(username: str, *, force: bool = False) -> dict | None:
    """Собирает (или берёт из кэша) персональный слой. None если выключен."""
    user = userdata.get_user(username) or {}
    prefs = user.get("prefs") or {}
    if isinstance(prefs, str):
        import json
        prefs = json.loads(prefs or "{}")
    if prefs.get("personal_digest") is False:
        return None
    tz = user.get("timezone") or "Europe/Moscow"
    try:
        from zoneinfo import ZoneInfo
        local_date = datetime.now(ZoneInfo(tz)).date()
    except Exception:
        local_date = datetime.now(MSK).date()

    if not force:
        cached = userdata.get_personal_digest(username, local_date)
        if cached and cached.get("payload"):
            return cached["payload"]

    prof = userdata.interest_weight_profile(username)
    has_profile = bool(prof["self_desc"] or prof["weights"] or prof["custom"])
    doc = store.read_latest(datetime.now(MSK).date())
    sections = doc.get("sections") or {}
    cands = _candidates(sections)
    fy = _for_you(cands, prof["weights"], prof["custom"])
    top_topics = [_label(t) for t, _ in
                  sorted(prof["weights"].items(), key=lambda x: -x[1])[:5]]
    name = _first_name(user.get("display_name") or username)
    lead = await _lead(name, prof["self_desc"], top_topics, fy) if has_profile else None

    payload = {
        "name": name,
        "lead": lead,
        "for_you": fy,
        "top_topics": top_topics,
        "quiet": (not fy and not lead),
        "has_profile": has_profile,
        "digest_date": doc.get("date"),
    }
    try:
        userdata.save_personal_digest(username, local_date, payload,
                                      llm_model=insight_model() if lead else None)
    except Exception:
        log.warning("[personal] save failed", exc_info=True)
    return payload
