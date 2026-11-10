"""Счёт «больше ожидаемого» для вкладки.

Тяжёлые истории редки: примерно полторы на сто прочитанных отзывов, а у половины отделений
прочитано меньше полусотни. Поэтому «в N раз чаще соседей» почти всегда случайность
(аудит 10.11: отделений с 2+ клиентами за 2 года — 391, чистая случайность даёт 378).
Здесь всё, что нужно, чтобы отличать настоящее превышение от случайного:

- экспозиция — сколько отзывов прочитала модель (а не сколько их всего: короткие пятёрки
  не читаются и только размывают знаменатель), по ячейкам «площадка × жалоба или нет» и месяцам;
- ожидание — экспозиция × частота в тех же ячейках: состав отзывов меняется (в 2026 году на 2ГИС
  в разы выросло число положительных отзывов), и без ячеек свежие волны выглядели бы затухающими;
- хвост Пуассона и биномиальный хвост — шанс увидеть столько или больше случайно;
- поправка на число сравнений (Бенджамини — Хохберг): сравниваем тысячи отделений сразу.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime


def poisson_tail(k: int, lam: float) -> float:
    """P(X ≥ k) при X ~ Пуассон(lam)."""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    term = math.exp(-lam)
    if term == 0.0:                       # большое ожидание: нормальное приближение с поправкой
        z = (k - 0.5 - lam) / math.sqrt(lam)
        return 0.5 * math.erfc(z / math.sqrt(2))
    s = 0.0
    for i in range(k):
        s += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - s)


def bh(pvals: dict, q: float = 0.05, m: int | None = None) -> set:
    """Ключи, значимые при доле ложных находок q. m — сколько сравнений было всего
    (если проверяли больше, чем передали: остальные заведомо незначимы)."""
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = max(m or 0, len(items))
    top = 0
    for i, (_, p) in enumerate(items, 1):
        if p <= q * i / m:
            top = i
    return {k for k, _ in items[:top]}


def binom_tail(k: int, n: int, p: float) -> float:
    """P(X ≥ k) при X ~ Бином(n, p): точное сравнение двух периодов (сколько из n событий пришлось на первый)."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lq, lp = math.log1p(-p), math.log(p)
    s = 0.0
    for i in range(k, n + 1):
        s += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq)
    return min(1.0, s)


def binom_low(k: int, n: int, p: float) -> float:
    """P(X ≤ k) при X ~ Бином(n, p)."""
    return max(0.0, min(1.0, 1.0 - binom_tail(k + 1, n, p)))


def sign(rating) -> str:
    return "neg" if (rating is not None and rating <= 3) else "pos"


def cell(source: str | None, year: int, rating) -> tuple:
    return (source, year, sign(rating))


def ym(d: datetime) -> str:
    return d.strftime("%Y-%m")


def city_key(subject, city) -> str | None:
    """Город как территория: вместе с регионом — одноимённые города в разных регионах не склеиваются."""
    return f"{subject or ''}|{city}" if city else None


def city_name(key: str | None) -> str:
    return (key or "").split("|", 1)[-1]


def story_cell(s: dict) -> tuple:
    """Ячейка истории: площадка отзыва и жалоба это или нет."""
    return (s.get("src"), sign(s.get("rating")))


def cells_of(stories, since: str | None = None, until: str | None = None) -> Counter:
    """Истории по ячейкам за месяцы [since, until]."""
    return Counter(story_cell(s) for s in stories
                   if (since is None or ym(s["at"]) >= since) and (until is None or ym(s["at"]) <= until))


def expected_std(target: Counter, ref_events: Counter, ref_read: Counter) -> float:
    """Ожидание со стандартизацией: прочитанные отзывы цели × частота события в эталоне, по ячейкам."""
    return sum(n * ref_events.get(c, 0) / ref_read[c] for c, n in target.items() if ref_read.get(c))


def share_std(events: Counter, read_a: Counter, read_b: Counter) -> float:
    """Какая доля событий пришлась бы на период (или место) A при равной частоте в A и B — с учётом ячеек."""
    n = sum(events.values())
    if not n:
        return 0.0
    out = 0.0
    for c, k in events.items():
        a, b = read_a.get(c, 0), read_b.get(c, 0)
        out += k * (a / (a + b) if a + b else 0.5)
    return out / n


def excess(k_cells: Counter, t_read: Counter, net_ev: Counter, net_read: Counter) -> tuple[float, float]:
    """Ожидание и вероятность для места, если бы способ действия распределялся по сети ровно.

    Из всех случаев сети за окно на место пришлась бы та же доля, что и его доля прочитанных отзывов —
    отдельно по ячейкам (площадка × жалоба или нет). Сравнение точное (биномиальное): случаев мало,
    и оценивать частоту «остальной сети» по двум-трём историям нельзя — на ровном фоне так рождались
    ложные очаги (проверено симуляцией, tests/vsp/test_waves_hotspots.py). net_ev включает и случаи места."""
    k, n = sum(k_cells.values()), sum(net_ev.values())
    if not n:
        return 0.0, 1.0
    pi = share_std(net_ev, t_read, net_read - t_read)
    return n * pi, binom_tail(k, n, pi)


class Exposure:
    """Прочитанные моделью отзывы: по точке, по сети и по территориям — по месяцам и ячейкам."""

    def __init__(self) -> None:
        self.point: dict[str, Counter] = defaultdict(Counter)      # точка → (месяц, площадка, знак) → n
        self.cells: Counter = Counter()                            # (площадка, год, знак) → n
        self.month: Counter = Counter()                            # месяц → n
        self.net: Counter = Counter()                              # (месяц, площадка, знак) → n
        self.terr: dict[tuple, Counter] = defaultdict(Counter)     # (уровень, ключ) → (месяц, площадка, знак) → n
        self.total = 0

    def add(self, point: str, source: str | None, at: datetime, rating, *, gosb=None, city=None, subject=None) -> None:
        m = ym(at)
        k = (m, source, sign(rating))
        self.point[point][k] += 1
        self.cells[(source, at.year, k[2])] += 1
        self.month[m] += 1
        self.net[k] += 1
        self.total += 1
        if gosb:
            self.terr[("gosb", gosb)][k] += 1
        if city:
            self.terr[("city", city)][k] += 1
        if subject:
            self.terr[("subject", subject)][k] += 1

    def rates(self, events) -> dict:
        """Частота события на прочитанный отзыв по ячейкам. events — (площадка, год, оценка) каждого события."""
        k: Counter = Counter(cell(s, y, r) for s, y, r in events)
        return {c: k[c] / n for c, n in self.cells.items() if n}

    def expected(self, point: str, rates: dict, since: str | None = None) -> float:
        """Ожидание для точки: её прочитанные отзывы × частота по сети в тех же ячейках. since — месяц «ГГГГ-ММ»."""
        return sum(n * rates.get((src, int(m[:4]), sg), 0.0)
                   for (m, src, sg), n in self.point.get(point, {}).items() if since is None or m >= since)

    @staticmethod
    def _cells(src: Counter, since: str | None, until: str | None) -> Counter:
        out: Counter = Counter()
        for (m, source, sg), n in src.items():
            if (since is None or m >= since) and (until is None or m <= until):
                out[(source, sg)] += n
        return out

    def point_cells(self, point: str, since: str | None = None, until: str | None = None) -> Counter:
        return self._cells(self.point.get(point, Counter()), since, until)

    def terr_cells(self, level: str, key: str, since: str | None = None, until: str | None = None) -> Counter:
        return self._cells(self.terr.get((level, key), Counter()), since, until)

    def net_cells(self, since: str | None = None, until: str | None = None) -> Counter:
        return self._cells(self.net, since, until)

    def read(self, point: str, since: str | None = None, until: str | None = None) -> int:
        return sum(self.point_cells(point, since, until).values())

    def terr_read(self, level: str, key: str, since: str | None = None, until: str | None = None) -> int:
        return sum(self.terr_cells(level, key, since, until).values())

    def net_read(self, since: str | None = None, until: str | None = None) -> int:
        return sum(n for m, n in self.month.items() if (since is None or m >= since) and (until is None or m <= until))

    def complaints(self) -> Counter:
        """Прочитанные жалобы (оценка 1–3) по месяцам: знаменатель графика волны."""
        out: Counter = Counter()
        for (m, _, sg), n in self.net.items():
            if sg == "neg":
                out[m] += n
        return out
