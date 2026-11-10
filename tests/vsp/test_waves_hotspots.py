"""Волны и очаги на синтетических историях: рост не объясняется составом отзывов, очаг — не случайность."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bank_audit.vsp.signals import hotspots, waves
from bank_audit.vsp.signals.stats import Exposure, bh, binom_low, binom_tail, city_key, share_std

UTC = timezone.utc
NOW = datetime(2026, 11, 10, tzinfo=UTC)


def _expo(points: dict[str, dict], per_month: int = 8, *, flood_from: str | None = None) -> Exposure:
    """Каждая точка читает per_month жалоб и столько же похвалы в месяц с 2021 года;
    flood_from — с этого месяца похвалы на 2ГИС становится вдесятеро больше."""
    e = Exposure()
    d = datetime(2021, 1, 15, tzinfo=UTC)
    while d < NOW:
        for p, v in points.items():
            for _ in range(per_month):
                e.add(p, "2gis", d, 1, gosb=v["gosb"], city=city_key(v["subject"], v["city"]), subject=v["subject"])
            pos = per_month * (10 if flood_from and d.strftime("%Y-%m") >= flood_from else 1)
            for _ in range(pos):
                e.add(p, "2gis", d, 5, gosb=v["gosb"], city=city_key(v["subject"], v["city"]), subject=v["subject"])
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=15)
    return e


def _story(i, point, at, kind, v, **kw):
    return {"rid": f"r{kind}-{i}", "client": f"r{kind}-{i}", "point": point, "at": at, "severity": 2, "kind": kind,
            "motif": None, "rating": 1, "src": "2gis", "gosb": v["gosb"], "subject": v["subject"], "city": v["city"],
            "copy_of": None, "dup_of": None, "elsewhere": False, "hq": False, **kw}


# сеть: 20 городов по три отделения — территория очага занимает несколько процентов сети, как в жизни
CITIES = [("MOW", "9038", "Москва"), ("TA", "8610", "Казань"), ("SPE", "9055", "Санкт-Петербург")] + \
         [(f"R{i:02d}", f"86{i:02d}", f"Город{i}") for i in range(17)]
PTS = {f"{c[0].lower()}{i}": {"gosb": c[1], "subject": c[0], "city": c[2]} for c in CITIES for i in range(3)}
MSK, KZN, SPB = (["mow0", "mow1", "mow2"], ["ta0", "ta1", "ta2"], ["spe0", "spe1", "spe2"])


def _steady(kind, n_per_quarter=2, seed=7):
    """Ровный фон: по сети столько клиентов в квартал, отделение — случайное (без сгущений по построению)."""
    import random
    rnd = random.Random(seed * 100 + kind)
    out, i, d = [], 0, datetime(2021, 2, 10, tzinfo=UTC)
    names = list(PTS)
    while d < NOW:
        for _ in range(n_per_quarter):
            p = rnd.choice(names)
            out.append(_story(i, p, d + timedelta(days=rnd.randint(0, 85)), kind, PTS[p]))
            i += 1
        d += timedelta(days=91)
    return [s for s in out if s["at"] < NOW]


def test_wave_states_do_not_depend_on_review_mix():
    rising = _steady(8, 1) + [_story(900 + i, MSK[i % 3], NOW - timedelta(days=5 + 9 * i), 8, PTS[MSK[i % 3]])
                              for i in range(9)]
    new = [_story(i, SPB[i % 3], NOW - timedelta(days=20 + 40 * i), 16, PTS[SPB[i % 3]]) for i in range(7)]
    gone = [_story(i, KZN[i % 3], datetime(2022, 3 + i % 6, 5, tzinfo=UTC), 18, PTS[KZN[i % 3]]) for i in range(6)]
    steady = _steady(7, 3)
    stories = rising + new + gone + steady
    for flood in (None, "2026-01"):                    # вдесятеро больше похвалы в последний год — вывод тот же
        ws = {w["key"]: w for w in waves.build(stories, _expo(PTS, flood_from=flood), now=NOW)}
        assert ws["k:8"]["status"] == "rising" and ws["k:8"]["horizon"] in ("quarter", "year")
        assert ws["k:8"]["recent"] >= 8 and ws["k:8"]["recent_ratio"] > 3
        assert ws["k:16"]["status"] == "new" and ws["k:18"]["status"] == "gone" and ws["k:7"]["status"] == "steady"
        assert ws["k:18"]["year"] == 0 and ws["k:18"]["peak_year"] == 2022
        assert [w["status"] for w in waves.build(stories, _expo(PTS, flood_from=flood), now=NOW)][:2] == ["new", "rising"]
    # ряд: 48 месяцев, клиенты и прочитанные жалобы
    s = ws["k:7"]["series"]
    assert len(s) == 48 and s[-1]["m"] == "2026-11" and all(x["c"] == 8 * len(PTS) for x in s[:-1])


def test_wave_counts_client_once_and_skips_background():
    a = _story(1, "mow0", NOW - timedelta(days=30), 5, PTS["mow0"])
    copy = {**_story(2, "mow0", NOW - timedelta(days=25), 5, PTS["mow0"]), "client": a["client"], "copy_of": a["rid"]}
    dup = {**_story(3, "mow1", NOW - timedelta(days=24), 5, PTS["mow1"]), "dup_of": a["rid"]}
    light = {**_story(4, "mow2", NOW - timedelta(days=20), 5, PTS["mow2"]), "severity": 1}
    bg = _story(5, "ta0", NOW - timedelta(days=10), 23, PTS["ta0"])          # ошибка без умысла — фон
    other = _story(6, "ta1", NOW - timedelta(days=10), 25, PTS["ta1"])       # «другое» — не способ действия
    old = _story(7, "ta2", datetime(2019, 5, 1, tzinfo=UTC), 5, PTS["ta2"])  # раньше, чем тип ставит модель
    assert waves.series_keys(bg) == [] and waves.series_keys(other) == []
    assert [s["rid"] for s in waves.typed([a, copy, dup, light, bg, other, old])] == [a["rid"], bg["rid"], other["rid"]]
    assert waves.series_keys({**a, "motif": "m0503"}) == ["k:5", "m:m0503"]
    assert waves.series_keys({**a, "motif": "m0500"}) == ["k:5"]           # «прочее» внутри типа — не почерк


def test_hotspot_found_and_uniform_spread_is_not():
    base = _steady(15, 3)                                   # биометрия идёт по всей сети ровно
    burst = [_story(700 + i, KZN[i % 3], NOW - timedelta(days=30 + 22 * i), 15, PTS[KZN[i % 3]]) for i in range(8)]
    expo = _expo(PTS)
    hs = hotspots.build(base + burst, expo, {}, now=NOW)
    top = hs[0]
    assert top["key"] == "k:15" and top["terr"] in ("Казань", "8610") and top["strength"] == "strong"
    assert top["clients"] >= 8 and len(top["points"]) == 3 and top["ratio"] > 3 and top["expected"] < top["clients"] / 3
    assert not top["all_time"] and top["window_months"] in (9, 18) and len(top["rids"]) == top["clients"]
    assert len([h for h in hs if h["key"] == "k:15" and h["subject"] == "TA"]) == 1      # город и его ГОСБ — один очаг
    # копия текста на карточках трёх отделений — один клиент, очага нет
    fake = [_story(800, SPB[0], NOW - timedelta(days=40), 15, PTS[SPB[0]])]
    fake += [{**_story(801 + i, SPB[1 + i % 2], NOW - timedelta(days=39), 15, PTS[SPB[1 + i % 2]]), "dup_of": "r15-800"}
             for i in range(4)]
    assert not [h for h in hotspots.build(base + fake, expo, {}, now=NOW) if h["subject"] == "SPE"]
    # таблица для карты: Татарстан выше ожидаемого, и это значимо
    tt = hotspots.territory_table(base + burst, expo, now=NOW)
    ta = tt["k:15"]["subject"]["TA"]
    assert ta["sig"] and ta["ratio"] > 2 and ta["n"] >= 8 and not tt["k:15"]["subject"]["MOW"]["sig"]
    assert "all" in tt and tt["all"]["clients"] == tt["k:15"]["clients"]


def test_uniform_background_rarely_gives_strong_hotspots():
    """Ровный фон: «сильные» очаги — не чаще, чем обещает поправка (5 % прогонов). Раньше ожидание
    считалось по частоте «остальной сети», и при редком способе действия случайные сгущения проходили."""
    expo = _expo(PTS)
    runs = 120
    for rate in (3, 12):
        strong = sum(any(h["strength"] == "strong" for h in hotspots.build(_steady(15, rate, seed), expo, {}, now=NOW))
                     for seed in range(runs))
        assert strong <= 0.07 * runs, (rate, strong)


def test_statistics_helpers():
    assert binom_tail(0, 10, 0.3) == 1.0 and binom_tail(11, 10, 0.3) == 0.0
    assert abs(binom_tail(1, 2, 0.5) - 0.75) < 1e-9 and abs(binom_low(0, 2, 0.5) - 0.25) < 1e-9
    assert binom_tail(18, 23, 0.5) < 0.01 < binom_tail(13, 23, 0.5)
    # поправка на число сравнений: одна маленькая вероятность среди тысячи проверок — не находка
    assert bh({"a": 0.001}, 0.05, m=1000) == set() and bh({"a": 0.00001}, 0.05, m=1000) == {"a"}
    assert bh({"a": 0.001, "b": 0.002, "c": 0.9}, 0.05) == {"a", "b"}
    from collections import Counter
    ev = Counter({("2gis", "neg"): 10})
    assert share_std(ev, Counter({("2gis", "neg"): 100}), Counter({("2gis", "neg"): 300})) == 0.25
    assert share_std(ev, Counter({("2gis", "neg"): 100, ("2gis", "pos"): 9000}), Counter({("2gis", "neg"): 300})) == 0.25
