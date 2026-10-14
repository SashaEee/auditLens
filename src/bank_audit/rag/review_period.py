"""Период «Аудита отзывов»: что выбрал аудитор и с чем это сравнивать.

Раньше период был числом дней «до сейчас» (3/6/12 мес), а сравнение — окном той
же длины прямо перед ним. Аудиторы думают календарём («за 2026 год», «в июле»,
«с 01.01.2025», «поквартально» — так они спрашивают ИИ-помощника), поэтому
период — это отрезок дат «с — по» по Москве, а готовые варианты разворачиваются
в даты здесь, на сервере: ссылка «последние 30 дней» остаётся живой.

С чем сравнивать (сравнение по умолчанию):
  • последние N дней и свой отрезок — с отрезком той же длины перед ним;
  • месяц, квартал, год — с прошлым месяцем, кварталом, годом;
  • «с начала года» — с тем же отрезком прошлого года;
  • «всё время» — ни с чем.
Можно попросить «год назад» (cmp=yoy) или не сравнивать (cmp=none).

Две поправки, без которых сравнение врёт:
  • Выравнивание по полноте данных. Корпус приходит с опозданием на сутки: в
    окне «до сегодня» последние дни недособраны, и неделя к неделе давала
    «−14%» на пустом месте. Сравнивается только часть периода до конца
    последнего полного дня данных — и такой же по длине кусок прошлого
    периода («1–14 октября против 1–14 сентября», а не против всего сентября).
  • Начало данных. Корпус — с 01.01.2025 (до этого меньше тысячи отзывов за
    все годы). У «12 мес» прошлое окно начиналось в октябре 2024, его первые
    2,5 месяца были пустыми — рост завышался, у рынка выходил даже не тот знак.
    Прошлый период обрезается по началу данных, объём сравнивается в пересчёте
    на день (множитель k), а если данными покрыто меньше половины прошлого
    периода — сравнения нет вовсе («не с чем сравнить»).
"""
from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))          # без перехода на летнее время с 2014 года
DAY = timedelta(days=1)
DATA_START = date.fromisoformat(os.getenv("REVIEWS_DATA_START", "2025-01-01"))
ROLLING = {"7d": 7, "30d": 30, "90d": 90, "180d": 180, "365d": 365}
CMP_MODES = ("prev", "yoy", "none")
MIN_COVER = 0.5                             # меньше — «не с чем сравнить»
_DATE_RX = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ND_RX = re.compile(r"^(\d{1,4})d$")             # «последние N дней» сверх готовых


def _start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=MSK)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, n: int) -> date:
    """Первое число месяца, отстоящего от d на n месяцев (d — первое число)."""
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def _year_back(d: date) -> date:
    try:
        return d.replace(year=d.year - 1)
    except ValueError:                      # 29 февраля
        return d.replace(year=d.year - 1, day=28)


@dataclass(frozen=True)
class Period:
    """Текущий период [t0, t1) и период сравнения [p0, p1) — моменты в МСК.

    c1 — конец сравниваемой части текущего периода (выравнивание по полноте
    данных), k — во сколько раз сравниваемая часть длиннее покрытой данными
    части прошлого периода: ожидаемый объём «как раньше» = prev · k."""
    t0: datetime
    t1: datetime
    c1: datetime
    p0: datetime
    p1: datetime
    k: float
    kind: str                     # rolling | month | quarter | year | ytd | range | all
    cmp: str                      # prev | yoy | none
    n: int | None = None          # длина готового периода «последние N дней»
    cover: float | None = None    # доля прошлого периода, покрытая данными
    cmp_note: str | None = None   # data — сравнивать не с чем; off — выключено
    partial: bool = False         # текущий месяц/квартал/год ещё идёт

    @property
    def has_cmp(self) -> bool:
        return self.cmp != "none" and self.p1 > self.p0

    @property
    def days(self) -> int:
        """Длина периода в днях — для подписей «за N дн» и окон «не короче»."""
        return max(1, math.ceil((self.t1 - self.t0) / DAY))

    @property
    def lo(self) -> datetime:
        """Начало выборки, покрывающей оба периода."""
        return min(self.t0, self.p0) if self.has_cmp else self.t0

    @property
    def key(self) -> str:
        f = "%Y%m%d"
        return (f"{self.t0:{f}}-{self.t1:{f}}~{self.c1:%Y%m%d%H}~"
                f"{self.p0:{f}}-{self.p1:{f}}~{self.cmp}")

    def binds(self) -> dict:
        """Параметры SQL: :t0/:t1 — период, :c1 — конец сравниваемой части,
        :p0/:p1 — прошлый период (пустой, если сравнения нет), :lo — общий низ.
        Без часового пояса: review_index.dt — наивное московское время площадки,
        и сравнение наивного с наивным не зависит от часового пояса сессии БД."""
        nv = lambda t: t.replace(tzinfo=None)  # noqa: E731
        return {"t0": nv(self.t0), "t1": nv(self.t1), "c1": nv(self.c1),
                "p0": nv(self.p0), "p1": nv(self.p1), "lo": nv(self.lo)}

    def expected(self, prev: int | float) -> float:
        """Сколько было бы в сравниваемой части при прежней интенсивности."""
        return prev * self.k

    def delta_pct(self, cur: int, prev: int) -> float | None:
        """Изменение объёма в процентах: cur — за сравниваемую часть, prev — за
        прошлый период; разная длина и неполное покрытие — через k."""
        if not self.has_cmp or not prev:
            return None
        return round(100.0 * (cur / self.expected(prev) - 1), 1)

    def info(self) -> dict:
        """Что на самом деле посчитано — для подписей вкладки («к 1–13 сентября»)."""
        last = lambda t: (t - DAY).date().isoformat()  # noqa: E731 — включительная дата
        out = {"kind": self.kind, "n": self.n, "from": self.t0.date().isoformat(),
               "to": last(self.t1), "days": self.days, "cmp": self.cmp,
               "partial": self.partial}
        if self.has_cmp:
            out.update({"cmp_from": self.p0.date().isoformat(), "cmp_to": last(self.p1),
                        "cover": round(self.cover, 2) if self.cover is not None else None,
                        "per_day": abs(self.k - 1) > 0.005})
            if self.c1 < self.t1:
                out["aligned_to"] = last(self.c1)
        else:
            out["cmp_note"] = self.cmp_note or "off"
        return out


_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
           "сентябрь", "октябрь", "ноябрь", "декабрь"]
_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def _dm(d: date, year: bool = True) -> str:
    return d.strftime("%d.%m.%Y" if year else "%d.%m")


def label(per: "Period") -> str:
    """Период словами — для выгрузки, имени файла и подсказок модели:
    «сентябрь 2026», «III квартал 2026», «последние 90 дней (18.07–15.10.2026)»."""
    d0, d1 = per.t0.date(), (per.t1 - DAY).date()
    rng = f"{_dm(d0, d0.year != d1.year)}–{_dm(d1)}"
    tail = f" (по {_dm(d1)})" if per.partial else ""
    if per.kind == "rolling":
        return f"последние {per.n} дн. ({rng})"
    if per.kind == "month":
        return f"{_MONTHS[d0.month - 1]} {d0.year}{tail}"
    if per.kind == "quarter":
        return f"{_ROMAN[(d0.month - 1) // 3 + 1]} квартал {d0.year}{tail}"
    if per.kind in ("year", "ytd"):
        return f"{d0.year} год{tail}"
    if per.kind == "all":
        return f"всё время ({rng})"
    return rng


def _shape(d0: date, d1: date, today: date) -> tuple[str, bool]:
    """Чем является отрезок дат [d0, d1] (включительно): месяц, квартал, год или
    свой отрезок. Отрезок, который начинается с 1-го числа и доходит до сегодня,
    — идущий месяц/квартал/год (partial)."""
    if d0.day == 1:
        nm = _add_months(d0, 1) - DAY
        if d1 == nm or (d0 <= today <= nm and d1 == today):
            return "month", d1 != nm
        if d0.month in (1, 4, 7, 10):
            nq = _add_months(d0, 3) - DAY
            if d1 == nq or (d0 <= today <= nq and d1 == today):
                return "quarter", d1 != nq
        if d0.month == 1:
            ny = date(d0.year, 12, 31)
            if d1 == ny or (d0 <= today <= ny and d1 == today):
                return "year", d1 != ny
    return "range", False


def build(d0: date, d1: date, *, kind: str, cmp: str | None, today: date,
          horizon: datetime | None = None, n: int | None = None,
          partial: bool = False, data_start: date | None = None) -> Period:
    """Период по включительным датам [d0, d1] и правилам сравнения.
    data_start — с какого дня есть данные (у срезов — с начала их разметки)."""
    data_start = data_start or DATA_START
    d0 = max(d0, data_start)
    d1 = min(d1, today)
    if d1 < d0:
        d1 = d0
    t0, t1 = _start(d0), _start(d1 + DAY)
    default_cmp = {"all": "none", "ytd": "yoy"}.get(kind, "prev")
    cmp = cmp if cmp in CMP_MODES else default_cmp
    # прошлый период — полностью, до выравнивания и обрезки
    if cmp == "yoy":
        p0, p1 = _start(_year_back(d0)), _start(_year_back(d1) + DAY)
    elif kind == "month":
        m0 = _month_start(d0)
        p0, p1 = _start(_add_months(m0, -1)), _start(m0)
    elif kind == "quarter":
        q0 = _quarter_start(d0)
        p0, p1 = _start(_add_months(q0, -3)), _start(q0)
    elif kind in ("year", "ytd"):
        p0, p1 = _start(date(d0.year - 1, 1, 1)), _start(date(d0.year, 1, 1))
    else:
        p0, p1 = t0 - (t1 - t0), t0
    # выравнивание: сравниваем только дни с полными данными
    h = horizon or _start(today)
    c1 = min(t1, h) if h > t0 else t1
    if c1 < t1:
        p1 = min(p1, p0 + (c1 - t0))
    if cmp == "none":
        return Period(t0, t1, c1, t0, t0, 1.0, kind, "none", n, None, "off", partial)
    # начало данных: прошлый период обрезается, объём — в пересчёте на день
    ds = _start(data_start)
    full = p1 - p0
    p0c = max(p0, ds)
    covered = p1 - p0c
    cover = covered / full if full > timedelta(0) else 0.0
    if covered <= timedelta(0) or cover < MIN_COVER:
        return Period(t0, t1, c1, t0, t0, 1.0, kind, "none", n, max(cover, 0.0), "data", partial)
    k = (c1 - t0) / covered
    return Period(t0, t1, c1, p0c, p1, k, kind, cmp, n, cover, None, partial)


def parse(p: str | None = None, date_from: str | None = None, date_to: str | None = None,
          cmp: str | None = None, days: int | None = None, *,
          now: datetime | None = None, horizon: datetime | None = None,
          data_start: date | None = None) -> Period:
    """Период из параметров запроса: p=7d|30d|90d|180d|365d|mtd|qtd|ytd|all,
    from/to (YYYY-MM-DD, включительно), cmp=prev|yoy|none; days=N — старые
    ссылки и вызовы (последние N дней). Без параметров — последние 90 дней."""
    now = (now or datetime.now(MSK)).astimezone(MSK)
    today = now.date()
    if horizon is None:
        horizon = data_horizon(now)
    ds = data_start or DATA_START
    kw = {"cmp": cmp, "today": today, "horizon": horizon, "data_start": ds}
    if date_from and _DATE_RX.match(date_from):
        d0 = date.fromisoformat(date_from)
        d1 = date.fromisoformat(date_to) if date_to and _DATE_RX.match(date_to) else today
        if d1 < d0:
            d0, d1 = d1, d0
        d0, d1 = max(d0, ds), min(d1, today)
        kind, partial = _shape(d0, d1, today)
        if kind == "year" and partial:
            kind = "ytd"
        return build(d0, d1, kind=kind, partial=partial, **kw)
    if p == "mtd":
        return build(_month_start(today), today, kind="month", partial=True, **kw)
    if p == "qtd":
        return build(_quarter_start(today), today, kind="quarter", partial=True, **kw)
    if p == "ytd":
        return build(date(today.year, 1, 1), today, kind="ytd", partial=True, **kw)
    if p == "all":
        return build(ds, today, kind="all", **kw)
    m = _ND_RX.match(p or "")
    n = ROLLING.get(p or "") or (int(m.group(1)) if m else None) or (int(days) if days else 90)
    n = max(1, min(int(n), 3660))
    return build(today - (n - 1) * DAY, today, kind="rolling", n=n, **kw)


def rolling(days: int, data_start: date | None = None) -> Period:
    """Старые вызовы с числом дней (выпуск, ИИ-инструменты, оповещения)."""
    return parse(days=days, data_start=data_start)


# Конец последнего полного дня данных — тот же, что у недели сигналов
# (reviews_dash._WEEK_END): корпус приходит с опозданием на сутки.
_HORIZON: tuple[float, datetime | None] = (0.0, None)


def data_horizon(now: datetime | None = None) -> datetime | None:
    global _HORIZON
    ts, val = _HORIZON
    if val is not None and time.time() - ts < 600:
        return val
    try:
        from sqlalchemy import text

        from .. import db
        with db.session() as s:
            v = s.execute(text(
                "SELECT date_trunc('day', max(dt)) + interval '1 day'"
                " FROM review_index WHERE source = 'bankiru' AND dt <= now()")).scalar()
        if v is not None:
            now = (now or datetime.now(MSK)).astimezone(MSK)
            v = min(v.replace(tzinfo=MSK), _start(now.date()))
            _HORIZON = (time.time(), v)
            return v
    except Exception:  # noqa: BLE001 — без горизонта сравниваем до начала сегодняшнего дня
        pass
    return None


def from_request(p: str | None, date_from: str | None, date_to: str | None,
                 cmp: str | None, days: int | None,
                 data_start: date | None = None) -> Period:
    """Параметры эндпоинта → период. days — только когда нового нет."""
    if not (p or date_from):
        return parse(cmp=cmp, days=days, data_start=data_start)
    return parse(p, date_from, date_to, cmp, data_start=data_start)
