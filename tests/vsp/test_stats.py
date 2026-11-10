"""Статистика вкладки: хвост Пуассона, поправка на число сравнений, экспозиция, склейка клиента."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bank_audit.vsp.signals import clients, stats

UTC = timezone.utc


def test_poisson_tail():
    assert stats.poisson_tail(0, 1.0) == 1.0
    assert abs(stats.poisson_tail(1, 1.0) - (1 - 2.718281828 ** -1)) < 1e-9
    assert stats.poisson_tail(5, 0.5) < 0.001
    assert stats.poisson_tail(3, 0.0) == 0.0
    assert 0.4 < stats.poisson_tail(1000, 1000.0) < 0.6          # большое ожидание — без переполнения


def test_bh_учитывает_число_сравнений():
    p = {"a": 0.0001, "b": 0.01, "c": 0.04}
    assert stats.bh(p, 0.05) == {"a", "b", "c"}
    assert stats.bh(p, 0.05, m=1000) == set() or stats.bh(p, 0.05, m=1000) == {"a"}
    assert stats.bh({"a": 0.00001}, 0.05, m=1000) == {"a"}
    assert stats.bh({}, 0.05) == set()


def test_экспозиция_и_ожидание():
    e = stats.Exposure()
    d = datetime(2026, 3, 1, tzinfo=UTC)
    for i in range(100):
        e.add("p1", "yandex", d, 1 if i < 40 else 5, gosb="9038", city="Москва")
    for i in range(300):
        e.add("p2", "yandex", d, 1 if i < 60 else 5, gosb="9038", city="Москва")
    # 10 событий в жалобах по сети: частота 10/100 жалоб
    rates = e.rates([("yandex", 2026, 1)] * 10)
    assert abs(rates[("yandex", 2026, "neg")] - 0.1) < 1e-9
    assert abs(e.expected("p1", rates) - 4.0) < 1e-9             # 40 жалоб × 0,1
    assert abs(e.expected("p2", rates) - 6.0) < 1e-9
    assert e.expected("p1", rates, since="2026-04") == 0.0
    assert e.read("p1") == 100 and e.terr_read("gosb", "9038") == 400 and e.net_read() == 400


def _s(rid, point, at, text, author):
    return {"rid": rid, "point": point, "at": at, "text": text, "author": author}


def test_копия_на_двух_площадках_один_клиент():
    t = datetime(2026, 5, 1, tzinfo=UTC)
    txt = "пришла получить карту сотрудница оформила кредитную карту без согласия узнала только дома"
    ss = [_s("2gis:1", "b1", t, txt, "a1"),
          _s("yandex:1", "b1", t + timedelta(days=1), txt + " ужас", "y9"),         # копия на Яндексе
          _s("yandex:2", "b1", t + timedelta(days=3), "кассир недодала пять тысяч при выдаче пенсии бабушке", "y7"),
          _s("2gis:2", "b2", t, txt, "a1")]                                          # другое отделение — не склеиваем
    assert clients.assign(ss) == 3
    assert ss[0]["client"] == ss[1]["client"] and ss[1]["copy_of"] == "2gis:1" and ss[0]["copy_of"] is None
    assert ss[2]["client"] != ss[0]["client"] and ss[3]["client"] != ss[0]["client"]


def test_тот_же_автор_через_год_один_клиент():
    t = datetime(2025, 10, 7, tzinfo=UTC)
    ss = [_s("2gis:1", "b1", t, "бабушке подключили страховку карты за тридцать тысяч", "a1"),
          _s("2gis:2", "b1", t + timedelta(days=380), "снова запугали пенсионера и продали страховку", "a1"),
          _s("yandex:5", "b1", t + timedelta(days=389), "бабушке подключили страховку карты за тридцать тысяч", "y1")]
    assert clients.assign(ss) == 1
