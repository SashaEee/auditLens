"""Сотрудники, которых называют в отзывах, — внутри одного отделения.

Что ищем (между отделениями имена не сопоставляются — решение владельца):
- один сотрудник в жалобах нескольких разных клиентов (особенно на навязывание,
  оформление без согласия, деньги) — повторяющееся поведение конкретного человека;
- одного сотрудника хвалят по ФИО новые аккаунты пачкой — собственная «сеть» клиентов
  или заказ отзывов; вместе с жалобами «по знакомству» в том же отделении — повод
  смотреть на неформальные связи.

Человек = набор основ слов ФИО без падежных окончаний: «Дергиева Хеда Аюбовна» и
«Дергиевой Хеды Аюбовны» — один ключ; порядок слов не важен.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta

from .data import Rec

_CAP = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
_PATR = r"[А-ЯЁ][а-яё]+(?:ович|евич|ьич|овн|евн|ичн|инич)[а-яё]*"
# «Фамилия Имя Отчество», «Имя Отчество Фамилия», «Имя Отчество»
FIO = re.compile(rf"(?<![А-ЯЁа-яё])(?:({_CAP})\s+({_CAP})\s+({_PATR})|({_CAP})\s+({_PATR})(?:\s+({_CAP}))?)(?![А-ЯЁа-яё])")
# роль + имя: «менеджер Ольга», «кассир Петрова»
ROLE = re.compile(r"(?i:(?:менеджер|специалист|сотрудни|кассир|консультант|операционист|девушк|управляющ|"
                  r"директор|руководител|администратор|оператор)\w*)\s+(" + _CAP + r")(?:\s+(" + _CAP + r"))?")
_END = re.compile(r"(ого|ому|ыми|ими|ой|ей|ою|ею|ом|ем|ым|им|ую|юю|ая|яя|а|я|ы|и|е|у|ю|ь)$")
STOP = {"сбер", "сбербанк", "банк", "спасибо", "отделени", "офис", "москв", "россия", "огромн", "больш", "очень"}

WINDOW_PRAISE_D = 30
MIN_PRAISE = 3
WINDOW_COMPLAINT_D = 548       # полтора года


def stem(w: str) -> str:
    w = w.lower().replace("ё", "е")
    s = _END.sub("", w) if len(w) > 4 else w
    return s[:8]


def people_in(text: str | None) -> list[tuple[str, str]]:
    """[(ключ человека, как написано)] из текста отзыва."""
    if not text:
        return []
    out = {}
    for m in FIO.finditer(text):
        parts = [g for g in m.groups() if g]
        key = "|".join(sorted(stem(p) for p in parts))
        out.setdefault(key, " ".join(parts))
    for m in ROLE.finditer(text):
        parts = [g for g in m.groups() if g]
        st = [stem(p) for p in parts]
        if any(x in STOP for x in st):
            continue
        key = "|".join(sorted(st))
        # «менеджер Ольга» не плодим, если та же Ольга уже есть в полном ФИО
        if not any(set(st) <= set(k.split("|")) for k in out):
            out.setdefault(key, " ".join(parts))
    return list(out.items())


def analyze(recs: list[Rec], risk_by_rid: dict[str, list[str]]) -> list[dict]:
    """Сотрудники с повторами внутри отделения: в жалобах разных клиентов и в волнах похвалы."""
    by_point: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    shown: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in recs:
        for key, as_written in people_in(r.extra.get("raw_text") or ""):
            by_point[r.point][key].append(r)
            shown[(r.point, key)][as_written] += 1
    out = []
    for p, people in by_point.items():
        for key, rs in people.items():
            rs.sort(key=lambda r: r.at)
            name = shown[(p, key)].most_common(1)[0][0]
            full = "|" in key and len(key.split("|")) >= 2
            # жалобы разных клиентов
            bad = [r for r in rs if (r.rating is not None and r.rating <= 2) or risk_by_rid.get(r.rid)]
            authors = {r.author or r.rid for r in bad}
            if len(authors) >= 2:
                span = (bad[-1].at - bad[0].at).days
                if span <= WINDOW_COMPLAINT_D or len(authors) >= 3:
                    types = Counter(t for r in bad for t in risk_by_rid.get(r.rid, []))
                    sev = sum(types.values())
                    out.append({"point": p, "key": key, "name": name, "full_name": full, "kind": "complaints",
                                "n": len(bad), "authors": len(authors), "first": bad[0].at, "last": bad[-1].at,
                                "types": dict(types), "score": min(100.0, 25 * len(authors) + 15 * sev),
                                "review_ids": [r.rid for r in bad][:60]})
            # волна похвалы от новых аккаунтов
            good = [r for r in rs if r.rating == 5]
            best = []
            j = 0
            for i in range(len(good)):
                while good[i].at - good[j].at > timedelta(days=WINDOW_PRAISE_D):
                    j += 1
                if i - j + 1 > len(best):
                    best = good[j:i + 1]
            if len(best) >= MIN_PRAISE:
                oneoff = sum(1 for r in best if (r.author_total or 0) <= 1 and r.provider != "flamp")
                if oneoff / len(best) >= 0.5:
                    out.append({"point": p, "key": key, "name": name, "full_name": full, "kind": "praise_wave",
                                "n": len(best), "authors": len({r.author or r.rid for r in best}),
                                "oneoff": oneoff, "first": best[0].at, "last": best[-1].at, "types": {},
                                "score": min(100.0, 10 * len(best) + (20 if full else 0)),
                                "review_ids": [r.rid for r in best][:60]})
    return sorted(out, key=lambda x: -x["score"])
