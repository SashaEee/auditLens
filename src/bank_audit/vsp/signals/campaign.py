"""Сигнал «похоже на кампанию отзывов» и «синхронный всплеск в ГОСБ».

Что считаем всплеском. Берём неподтверждённые пятёрки (не официальный канал «отзыв после
операции») в окне 14 дней со сдвигом 7. Ожидание для точки — её доля в потоке клиентов
группы × неподтверждённые пятёрки остальной группы в том же окне:

    E = U5(группа без точки, окно) × (F(точка) + 1) / (F(группа без точки) + n − 1)

где F — «поток»: отзывы 1–4★ и подтверждённые пятёрки за 24 месяца, n — точек в группе.
Группа — ГОСБ (если неизвестен — субъект, иначе вся сеть). Так рост по всей сети или по
всему ГОСБ ожидание поднимает сам — точка выделяется, только если выше своей группы.
Окно — кандидат, если пятёрок не меньше MIN_N, в MIN_RATIO раз больше ожидания и
вероятность такого числа при ожидании (Пуассон) не больше 10^MAX_P_LOG10. Соседние окна
сливаются в эпизод.

Уровень ГОСБ — то же самое на ступень выше: ГОСБ против сети. Это сигнал №5: если кампанию
спустили сверху, сравнение ВСП со своим ГОСБ её «съест», а здесь она видна.

Признаки эпизода (подкрепления, не порог): авторы без других отзывов, пачки до 10 минут,
последние три дня месяца, шаблонные и пустые тексты, пометка площадки «промотирование» или
«дубли», доля подтверждённых (снижает балл: официальный канал), волна жалоб перед эпизодом.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .data import UTC, Rec

WINDOW_W = 2              # окно — две недели
MIN_N = 10                # пятёрок в окне не меньше
MIN_RATIO = 3.0
MAX_P_LOG10 = -6.0        # ~10 тыс. точек × ~100 окон — поправка на множественность
E_FLOOR = 0.5             # ожидание не ниже (маленькие группы)
FOOTFALL_MONTHS = 24
SERIES_MIN = 10           # «пачка»: следующая пятёрка точки не позже чем через 10 минут
EPOCH = datetime(2000, 1, 3, tzinfo=UTC)   # понедельник


def week_of(d: datetime) -> int:
    return (d - EPOCH).days // 7


def week_start(w: int) -> datetime:
    return EPOCH + timedelta(weeks=w)


def poisson_sf_log10(x: int, lam: float) -> float:
    """log10 P(X ≥ x) для Пуассона(λ); точно для x > λ (хвост суммируется до малости)."""
    if x <= 0:
        return 0.0
    if lam <= 0:
        return -300.0
    if x <= lam:
        return 0.0
    logs = []
    k = x
    while True:
        t = -lam + k * math.log(lam) - math.lgamma(k + 1)
        logs.append(t)
        if k > x + 5 and t < logs[0] - 40:
            break
        k += 1
        if k > x + 5000:
            break
    m = max(logs)
    return (m + math.log(sum(math.exp(v - m) for v in logs))) / math.log(10)


def _groups(recs: list[Rec], key) -> tuple[dict, dict, dict]:
    """По точкам: недельные U5, поток F; по группам — суммы."""
    u5: dict[str, Counter] = defaultdict(Counter)
    foot: Counter = Counter()
    grp: dict[str, str] = {}
    cutoff = max(r.at for r in recs) - timedelta(days=30.4 * FOOTFALL_MONTHS)
    for r in recs:
        grp[r.point] = key(r)
        if r.u5:
            u5[r.point][week_of(r.at)] += 1
        elif r.footfall and r.at >= cutoff:
            foot[r.point] += 1
    return u5, foot, grp


def find_bursts(recs: list[Rec], key, *, min_n: int = MIN_N) -> list[dict]:
    """Окна-всплески по точкам, слитые в эпизоды. key(rec) → группа точки."""
    if not recs:
        return []
    u5, foot, grp = _groups(recs, key)
    g_u5: dict[str, Counter] = defaultdict(Counter)
    g_foot: Counter = Counter()
    g_n: Counter = Counter()
    for p, g in grp.items():
        g_foot[g] += foot[p]
        g_n[g] += 1
        for w, n in u5[p].items():
            g_u5[g][w] += n
    out = []
    for p, weeks in u5.items():
        if sum(weeks.values()) < min_n:
            continue
        g = grp[p]
        share = (foot[p] + 1) / max(1.0, g_foot[g] - foot[p] + g_n[g] - 1)
        e_week = {w: max(0.0, g_u5[g][w] - weeks[w]) * share for w in weeks}
        flagged = []
        for w in sorted(weeks):
            x = weeks[w] + weeks.get(w + 1, 0)
            if x < min_n:
                continue
            e = max(E_FLOOR, e_week.get(w, 0.0) + max(0.0, g_u5[g][w + 1] - weeks.get(w + 1, 0)) * share)
            if x / e >= MIN_RATIO and poisson_sf_log10(x, e) <= MAX_P_LOG10:
                flagged.append(w)
        # слияние: окна [w, w+1] подряд или с перекрытием → один эпизод
        eps: list[list[int]] = []
        for w in flagged:
            if eps and w <= eps[-1][1] + 1:
                eps[-1][1] = max(eps[-1][1], w + 1)
            else:
                eps.append([w, w + 1])
        for a, b in eps:
            x = sum(weeks.get(w, 0) for w in range(a, b + 1))
            e = max(E_FLOOR, sum(max(0.0, g_u5[g][w] - weeks.get(w, 0)) * share for w in range(a, b + 1)))
            out.append({"point": p, "group": g, "w0": a, "w1": b, "n": x, "expected": round(e, 2),
                        "ratio": round(x / e, 1), "p_log10": round(poisson_sf_log10(x, e), 1)})
    return out


# ── признаки эпизода ───────────────────────────────────────────────────────────

def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def template_share(texts: list[str], thr: float = 0.6, min_words: int = 4) -> float:
    """Доля текстов, у которых есть почти-двойник в том же эпизоде (Жаккар по словам)."""
    sets = [set(t.split()) for t in texts]
    idx = [i for i, s in enumerate(sets) if len(s) >= min_words]
    dup = set()
    for k, i in enumerate(idx):
        for j in idx[k + 1:]:
            if _jaccard(sets[i], sets[j]) >= thr:
                dup.add(i)
                dup.add(j)
    return len(dup) / len(texts) if texts else 0.0


def series_flags(recs_sorted: list[Rec], minutes: int = SERIES_MIN) -> dict[str, bool]:
    """review_id → пятёрка пришла не позже чем через N минут после предыдущей пятёрки точки."""
    out = {}
    prev = None
    for r in recs_sorted:
        if r.rating != 5:
            continue
        out[r.rid] = prev is not None and (r.at - prev).total_seconds() <= minutes * 60
        prev = r.at
    return out


def month_end(d: datetime) -> bool:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return (nxt - d.replace(hour=0, minute=0, second=0, microsecond=0)).days <= 3


def episode_features(ep_recs: list[Rec], before_recs: list[Rec], series: dict[str, bool]) -> dict:
    fives = [r for r in ep_recs if r.rating == 5]
    u5 = [r for r in fives if not r.confirmed]
    known = [r for r in u5 if r.author_total is not None and r.provider != "flamp"]
    days = Counter(r.at.date() for r in u5)
    f = {
        "n_all": len(ep_recs), "n5": len(fives), "n_u5": len(u5),
        "confirmed_share": round(sum(r.confirmed for r in fives) / len(fives), 3) if fives else 0.0,
        "oneoff_share": round(sum((r.author_total or 0) <= 1 for r in known) / len(known), 3) if known else None,
        "series_share": round(sum(series.get(r.rid, False) for r in u5) / len(u5), 3) if u5 else 0.0,
        "month_end_share": round(sum(month_end(r.local) for r in u5) / len(u5), 3) if u5 else 0.0,
        "template_share": round(template_share([r.text for r in u5[:400]]), 3) if u5 else 0.0,
        "empty_share": round(sum(r.words <= 3 for r in u5) / len(u5), 3) if u5 else 0.0,
        "hidden": sum(1 for r in ep_recs if r.hidden),
        "hidden_reasons": Counter(_hide_kind(r.hidden) for r in ep_recs if r.hidden).most_common(3),
        "peak_day": max(days.values()) if days else 0,
        "neg": sum(1 for r in ep_recs if r.rating is not None and r.rating <= 2),
        "neg_before_30d": sum(1 for r in before_recs if r.rating is not None and r.rating <= 2),
        "gone": sum(r.gone for r in ep_recs),
        "work_hours_share": round(sum(9 <= r.local.hour < 20 for r in u5) / len(u5), 3) if u5 else 0.0,
    }
    return f


def _hide_kind(reason: str | None) -> str:
    s = (reason or "").lower()
    if "промотир" in s:
        return "промотирование"
    if "дубл" in s or "одинаков" in s:
        return "дубли"
    if "провер" in s:
        return "на проверке"
    return "другое"


def score(f: dict, ratio: float, norms: dict) -> tuple[float, list[dict]]:
    """Балл 0–100 и факты «почему» с нормой. Норма — медиана по сети за тот же год."""
    pts = 0.0
    why = []

    def add(key, label, val, norm, weight, full_at, fmt="{:.0%}"):
        nonlocal pts
        if val is None:
            return
        base = norm or 0.0
        part = max(0.0, min(1.0, (val - base) / max(1e-9, full_at - base))) if full_at > base else 0.0
        pts += weight * part
        if part > 0.15:
            why.append({"key": key, "label": label, "value": val, "norm": norm, "fmt": fmt})
    burst = max(0.0, min(1.0, math.log10(max(ratio, 1.0)) / 2))     # ×100 — полный балл
    pts += 30 * burst
    why.append({"key": "ratio", "label": "пятёрок больше ожидания", "value": ratio, "norm": 1.0, "fmt": "×{:.0f}"})
    add("oneoff_share", "авторы без других отзывов", f.get("oneoff_share"), norms.get("oneoff_share"), 20, 0.85)
    add("series_share", "пятёрки пачками до 10 минут", f["series_share"], norms.get("series_share"), 15, 0.4)
    add("month_end_share", "в последние 3 дня месяца", f["month_end_share"], norms.get("month_end_share"), 10, 0.45)
    add("template_share", "шаблонные тексты", max(f["template_share"], f["empty_share"] * 0.5),
        norms.get("template_share"), 10, 0.4)
    if f["hidden"]:
        pts += min(10.0, 2.5 * f["hidden"])
        why.append({"key": "hidden", "label": "площадка пометила отзывы", "value": f["hidden"], "norm": 0,
                    "fmt": "{}", "reasons": f["hidden_reasons"]})
    if f["neg_before_30d"] >= 3:
        pts += 5
        why.append({"key": "neg_before", "label": "волна жалоб за 30 дней до", "value": f["neg_before_30d"],
                    "norm": None, "fmt": "{}"})
    if f["confirmed_share"] >= 0.3:
        cut = min(20.0, 40 * (f["confirmed_share"] - 0.2))
        pts -= cut
        why.append({"key": "confirmed", "label": "подтверждены операцией (официальный канал)",
                    "value": f["confirmed_share"], "norm": norms.get("confirmed_share"), "fmt": "{:.0%}",
                    "against": True})
    return round(max(0.0, min(100.0, pts)), 1), why


def strength(s: float) -> str:
    return "сильный" if s >= 60 else "средний" if s >= 40 else "слабый"
