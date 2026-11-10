"""Один клиент — одна история в подсчётах.

Клиент часто оставляет один и тот же отзыв и на 2ГИС, и на Яндексе (авторы там разные),
дописывает его через год или пишет несколько отзывов об одном случае. Для признаков
«разные клиенты» это один человек. Склеиваем внутри одного отделения:
- один автор на одной площадке;
- близкий текст в пределах полутора месяцев (копия на другой площадке);
- тот же или почти тот же текст — в любое время (отзыв, продублированный через год).
"""
from __future__ import annotations

from collections import defaultdict

WINDOW_D = 45
SIMILAR = 0.45        # близкий текст в пределах WINDOW_D дней
SAME = 0.8            # почти тот же текст — в любое время


def _toks(t: str) -> set:
    return {w for w in (t or "").split() if len(w) > 3}


def _jac(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def assign(stories: list[dict]) -> int:
    """Ставит каждой истории поле client (общее у копий) и copy_of (rid первой истории клиента, если это копия).
    Возвращает число разных клиентов."""
    by: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        by[s["point"]].append(s)
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for ss in by.values():
        if len(ss) < 2:
            continue
        tk = [_toks(s.get("text") or "") for s in ss]
        head = [(s.get("text") or "")[:160] for s in ss]
        for i, a in enumerate(ss):
            for j in range(i + 1, len(ss)):
                b = ss[j]
                same_author = bool(a.get("author")) and a.get("author") == b.get("author") and \
                    a["rid"].split(":")[0] == b["rid"].split(":")[0]
                close = abs((a["at"] - b["at"]).days) <= WINDOW_D
                sim = _jac(tk[i], tk[j])
                same_text = (len(head[i]) >= 40 and head[i] == head[j]) or sim >= SAME
                if same_author or same_text or (close and sim >= SIMILAR):
                    parent[find(a["rid"])] = find(b["rid"])
    groups: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        groups[find(s["rid"])].append(s)
    for root, ss in groups.items():
        ss.sort(key=lambda s: s["at"])
        first = ss[0]["rid"]
        for s in ss:
            s["client"] = first
            s["copy_of"] = first if s["rid"] != first else None
    return len(groups)
