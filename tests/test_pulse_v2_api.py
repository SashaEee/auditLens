"""«Пульс» v2: новые поля API (CONTRACT §6) — ленты состояния служб за 14 дней,
нерабочие дни, прошлый период в плитках, оценки по дням, сегмент человека,
активность за 14 дней в «Людях», «Итоги» за 14 дней.

Postgres не нужен: правила дня — чистые функции, SQL проверяется фрагментами, а
запросы подменяются фейком _rows, отвечающим по подстроке SQL.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from bank_audit.clock import MSK
from bank_audit.digest import newsflow
from bank_audit.web import telemetry as T

TODAY = date(2026, 11, 5)                                  # четверг, как в макете
NOW = datetime(2026, 11, 5, 10, 3, tzinfo=MSK)
DAYS = [(TODAY - timedelta(days=13 - i)).isoformat() for i in range(14)]
OFF = [1, 2, 8, 9, 12]                                     # 24–25.10, 31.10–01.11, 04.11


def _i(d: str) -> int:
    return DAYS.index(d)


def _iso(d: date) -> str:
    return d.isoformat()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Праздник 4 ноября (DIGEST_HOLIDAYS), фиксированное «сейчас», чистый кэш лент."""
    monkeypatch.setattr(newsflow, "HOLIDAYS", {date(2026, 11, 4)})
    monkeypatch.setattr(T, "_msk_now", lambda: NOW)
    monkeypatch.setattr(T, "_review_disabled", lambda: frozenset())
    monkeypatch.setattr(T, "_canary_runs", list)
    T._HEALTH_CACHE.clear()
    yield
    T._HEALTH_CACHE.clear()


def _calc(raw: dict, now: datetime = NOW, disabled=frozenset()) -> dict:
    return T._health_calc(raw, now.date(), now, disabled)


# ── правила дня ─────────────────────────────────────────────────────────────
def test_worst_state_order():
    assert T._worst([]) == "n"
    assert T._worst(["n", "o"]) == "o"
    assert T._worst(["o", "w", "n"]) == "w"
    assert T._worst(["w", "b", "o"]) == "b"


def test_day_rules_server_and_digest():
    # сервер: пороги пункта очереди — 1–9 ошибок сбои, от 10 не работало
    assert T._day_server(0, 0) == "n"
    assert T._day_server(120, 0) == "o"
    assert T._day_server(120, 1) == "w" and T._day_server(0, 9) == "w"
    assert T._day_server(120, 10) == "b"
    # выпуск: раздел не ok — не работало; утренний сбой, исправленный пересборкой, — сбои
    assert T._day_digest(["ok", "ok"], False, True, True) == "o"
    assert T._day_digest(["ok", "failed"], False, True, True) == "b"
    assert T._day_digest(["ok", "degraded"], False, True, True) == "b"
    assert T._day_digest(["ok"], True, True, True) == "w"
    # выпуска нет: «не работало», только если выпуски уже собирались и он должен был выйти
    assert T._day_digest([], False, True, True) == "b"
    assert T._day_digest([], False, False, True) == "n"
    assert T._day_digest([], False, True, False) == "n"


def test_day_rules_reviews_news_slices():
    # площадка: правило source_health
    assert T._day_review_source(3, 4) == "o"              # малый поток — не поломка
    assert T._day_review_source(0, 70) == "b"             # встал
    assert T._day_review_source(41, 70) == "b"            # просел: ниже 60 % нормы
    assert T._day_review_source(42, 70) == "o"
    assert T._day_review_source(211, 70) == "w"           # всплеск: больше трёх норм
    # новости
    assert T._day_news(0, 0, True, True) == "b"
    assert T._day_news(0, 0, False, True) == "n"          # потока ещё не было
    assert T._day_news(0, 0, True, False) == "n"          # сегодня день не кончился
    assert T._day_news(30, 1, True, True) == "w"
    assert T._day_news(30, 1, True, False) == "o"
    assert T._day_news(30, 0, True, True) == "o"
    # разметка
    assert T._day_slices(None, True, 20) == "n"           # хвост неизвестен
    assert T._day_slices(51, False, None) == "b"
    assert T._day_slices(50, False, None) == "o"
    assert T._day_slices(0, True, 1) == "b"
    assert T._day_slices(0, False, 9.5) == "w"
    assert T._day_slices(0, False, 9.0) == "o"


def test_day_rules_rsearch_web_ingest():
    assert T._day_rsearch(5, 0, True) == "o"
    assert T._day_rsearch(5, 1, True) == "w"
    assert T._day_rsearch(0, 0, True) == "n"
    assert T._day_rsearch(5, 0, False) == "n"             # до 04.11.2026 события не было
    # веб-поиск: gw, gw_bad, chain, chain_ok, rd, rd_bad
    assert T._day_web(0, 0, 0, 0, 0, 0) == "n"
    assert T._day_web(10, 1, 5, 5, 0, 0) == "o"
    assert T._day_web(10, 3, 0, 0, 0, 0) == "w"           # размыкатель сработал
    assert T._day_web(10, 5, 0, 0, 0, 0) == "b"           # половина запросов — отказ
    assert T._day_web(0, 0, 5, 0, 0, 0) == "b"            # цепочка ни разу ничего не нашла
    assert T._day_web(0, 0, 4, 0, 0, 0) == "o"            # мало попыток — не вывод
    assert T._day_web(0, 0, 0, 0, 6, 3) == "w"            # копии страниц
    # индексация: ing, orig, hard
    assert T._day_ingest(0, 0, 0) == "n"
    assert T._day_ingest(3, 0, 0) == "o"
    assert T._day_ingest(0, 6, 6) == "b"
    assert T._day_ingest(2, 6, 6) == "w"                  # что-то всё же дошло
    assert T._day_ingest(2, 6, 3) == "w"
    assert T._day_ingest(0, 4, 4) == "o"                  # меньше пяти попыток
    assert T._day_ingest(0, 6, 0) == "o"                  # дубли — штатно


def test_activity_level():
    assert T._act_level(0, 0, 0) == 0
    assert T._act_level(1, 0, 0) == 1 and T._act_level(5, 0, 60) == 1
    assert T._act_level(6, 0, 0) == 2 and T._act_level(0, 1, 0) == 2
    assert T._act_level(30, 0, 0) == 3 and T._act_level(0, 5, 0) == 3
    assert T._act_level(1, 0, 1800) == 3
    assert T._act_level(0, 0, 40) == 1                    # ушёл со страницы, открытой вчера


# ── ленты из сырых строк ────────────────────────────────────────────────────
def test_health_axis_and_empty_is_no_data():
    h = _calc({})
    assert h["days"] == DAYS and h["off"] == OFF
    for k in T._HEALTH_KEYS:
        assert h[k] == ["n"] * 14, k


def test_health_failed_queries_are_no_data_not_ok(monkeypatch):
    """_rows глотает ошибки: упавшие запросы дают «нет данных», ни одной «нормы»."""
    def boom():
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(T.db, "session", boom)
    h = _calc(T._health_raw())
    assert all(h[k] == ["n"] * 14 for k in T._HEALTH_KEYS)


def test_health_server_digest():
    raw = {"server": [{"d": "2026-10-23", "req": 100, "err": 0},
                      {"d": "2026-10-24", "req": 50, "err": 3},
                      {"d": "2026-10-25", "req": 80, "err": 12}],
           "digest": [{"v": "first", "d": "2026-10-25"},
                      {"v": "cur", "d": "2026-10-25", "section": "headline", "status": "ok"},
                      {"v": "cur", "d": "2026-10-26", "section": "headline", "status": "ok"},
                      {"v": "cur", "d": "2026-10-26", "section": "tariff_moves", "status": "failed"},
                      {"v": "cur", "d": "2026-10-27", "section": "headline", "status": "ok"}],
           "digest_arc": [{"d": "2026-10-27", "section": "headline", "status": "stale"}]}
    h = _calc(raw)
    assert h["server"][:4] == ["o", "w", "b", "n"]
    dg = h["digest"]
    assert dg[_i("2026-10-23")] == "n" and dg[_i("2026-10-24")] == "n"    # до первого выпуска
    assert dg[_i("2026-10-25")] == "o"
    assert dg[_i("2026-10-26")] == "b"
    assert dg[_i("2026-10-27")] == "w"                    # утренний сбой из архива
    assert dg[_i("2026-10-28")] == "b"                    # выпуска не было
    assert dg[-1] == "b"                                  # 10:03 — сегодняшний уже должен быть
    early = _calc(raw, now=datetime(2026, 11, 5, 7, 30, tzinfo=MSK))
    assert early["digest"][-1] == "n"                     # до 08:00 его ещё ждём


def _daily(src: str, start: date, end: date, n, skip=()):
    """Строки «площадка × день» с n отзывов (n — число или функция даты)."""
    out, d = [], start
    while d <= end:
        k = n(d) if callable(n) else n
        if k and d not in skip:
            out.append({"source": src, "d": _iso(d), "n": k})
        d += timedelta(days=1)
    return out


def test_health_reviews_drop_surge_and_corpus_freshness():
    start = TODAY - timedelta(days=76)
    # наш сборщик sravni: 10 в день, с 1 ноября — ноль
    sravni = _daily("sravni_reviews", start, TODAY - timedelta(days=1), 10,
                    skip={date(2026, 11, 1) + timedelta(days=j) for j in range(5)})
    h = _calc({"reviews": sravni})["reviews"]
    assert h[_i("2026-11-03")] == "o"                     # неделя 50 при норме 70
    assert h[_i("2026-11-04")] == "b" and h[_i("2026-11-05")] == "b"     # 40 и 30 — просел
    # выключенная в конфиге площадка в оценку не идёт
    assert _calc({"reviews": sravni}, disabled=frozenset({"sravni_reviews"}))["reviews"] == ["n"] * 14
    # корпус banki.ru свеж по 3 ноября: день, неделя которого выходит за свежесть, — нет данных
    corpus = _daily("bankiru", start, date(2026, 11, 3), 20)
    h = _calc({"reviews": corpus})["reviews"]
    assert h[:-1] == ["o"] * 13 and h[-1] == "n"
    # всплеск корпуса: 500 в день 30.10–01.11 — «были сбои», пока он в окне недели
    surge = _daily("bankiru", start, date(2026, 11, 3),
                   lambda d: 500 if date(2026, 10, 30) <= d <= date(2026, 11, 1) else 20)
    h = _calc({"reviews": surge})["reviews"]
    assert h[_i("2026-10-30")] == "o" and h[_i("2026-10-31")] == "w"
    assert h[_i("2026-11-02")] == "w" and h[_i("2026-11-04")] == "w"
    # площадка с потоком меньше 5 в неделю — малый поток, «в норме»
    small = _daily("finuslugi_reviews", start, TODAY, lambda d: 1 if d.day % 3 == 0 else 0)
    assert set(_calc({"reviews": small})["reviews"]) == {"o"}


def test_health_news_silent_feed_and_stopped_stream():
    start = TODAY - timedelta(days=41)
    off = {date.fromisoformat(DAYS[i]) for i in OFF}

    def is_off(d):
        return d.weekday() >= 5 or d in off
    feed_a = _daily("feed_a", start, TODAY, lambda d: 0 if d == date(2026, 10, 27) else 10)
    # лента b пишет только по будням; 29.10 (четверг) замолчала
    feed_b = _daily("feed_b", start, TODAY,
                    lambda d: 0 if is_off(d) or d in (date(2026, 10, 27), date(2026, 10, 29), TODAY) else 5)
    h = _calc({"news": feed_a + feed_b})["news"]
    assert h[_i("2026-10-27")] == "b"                     # ни одной новости за день
    assert h[_i("2026-10-29")] == "w"                     # будничная лента молчит
    assert h[_i("2026-10-24")] == "o"                     # в выходной b и так молчит
    assert h[_i("2026-11-04")] == "o"                     # праздник — как выходной
    assert h[-1] == "o"                                   # сегодня день не кончился
    # поток появился 30.10: раньше — нет данных, а не «не работало»
    late = _daily("feed_a", date(2026, 10, 30), TODAY, 10)
    h = _calc({"news": late})["news"]
    assert h[:_i("2026-10-30")] == ["n"] * _i("2026-10-30")
    assert set(h[_i("2026-10-30"):]) == {"o"}


def test_health_slices_backlog_lower_bound_and_canary():
    ok = {"k": "ok", "dt_h": None, "ing_h": None, "n": 0}
    pend = {"k": "row", "dt_h": "2026-11-01 12:00", "ing_h": "2026-11-01 13:00", "n": 60}
    h = _calc({"backlog": [pend, ok]})["slices"]
    assert h[:_i("2026-11-01")] == ["o"] * _i("2026-11-01")
    assert h[_i("2026-11-01"):] == ["b"] * 5               # с 18:00 1 ноября ждут дольше 6 часов
    # попали в индекс только 5 ноября — раньше их в хвосте не было
    late = {**pend, "ing_h": "2026-11-05 01:00"}
    h = _calc({"backlog": [late, ok]})["slices"]
    assert h[:-1] == ["o"] * 13 and h[-1] == "b"
    # без строки-признака запрос не прошёл: «нет данных»
    assert _calc({"backlog": [pend]})["slices"] == ["n"] * 14
    # канарейка: тревога прогона 28.10 держится до следующего прогона без тревоги
    runs = [{"run_at": "2026-10-20T09:00:00+00:00", "alert": False},
            {"run_at": "2026-10-28T12:00:00+00:00", "alert": True},
            {"run_at": "2026-11-03T09:00:00+00:00", "alert": False}]
    gold = [{"gold": 120, "since": datetime(2026, 10, 1, tzinfo=UTC)}]
    h = _calc({"backlog": [ok], "gold": gold, "canary": runs})["slices"]
    assert h[_i("2026-10-27")] == "o"
    assert h[_i("2026-10-28"):_i("2026-11-03")] == ["b"] * 6
    assert h[_i("2026-11-03")] == "o"
    # давность: после прогона 20.10 больше 9 дней — «были сбои»
    h = _calc({"backlog": [ok], "gold": gold, "canary": runs[:1]})["slices"]
    assert h[_i("2026-10-28")] == "o" and h[_i("2026-10-29")] == "w" and h[-1] == "w"
    # эталон не загружен — канарейку не ждём
    h = _calc({"backlog": [ok], "gold": [{"gold": 0, "since": None}], "canary": []})["slices"]
    assert h == ["o"] * 14


def test_health_rsearch_web_ingest():
    raw = {"rsearch": [{"d": "2026-10-30", "n": 2, "failed": 0},
                       {"d": "2026-11-04", "n": 5, "failed": 0},
                       {"d": "2026-11-05", "n": 3, "failed": 1}],
           "web": [{"d": "2026-10-26", "gw": 10, "gw_bad": 6},
                   {"d": "2026-10-27", "gw": 10, "gw_bad": 3},
                   {"d": "2026-10-28", "chain": 6, "chain_ok": 0},
                   {"d": "2026-10-29", "gw": 10, "gw_bad": 1, "chain": 5, "chain_ok": 5},
                   {"d": "2026-10-30", "rd": 6, "rd_bad": 3}],
           "ingest": [{"d": "2026-10-26", "ing": 3, "orig": 0, "hard": 0},
                      {"d": "2026-10-27", "ing": 0, "orig": 6, "hard": 6},
                      {"d": "2026-10-28", "ing": 2, "orig": 6, "hard": 3},
                      {"d": "2026-10-29", "ing": 0, "orig": 2, "hard": 2}]}
    h = _calc(raw)
    rs = h["rsearch"]
    assert rs[_i("2026-10-30")] == "n"                    # до 04.11 события не было
    assert rs[_i("2026-11-04")] == "o" and rs[-1] == "w"
    assert rs.count("n") == 12
    w = h["web"]
    assert w[_i("2026-10-26"):_i("2026-10-31")] == ["b", "w", "b", "o", "w"]
    assert w.count("n") == 9
    ing = h["ingest"]
    assert ing[_i("2026-10-26"):_i("2026-10-30")] == ["o", "b", "w", "o"]
    assert ing.count("n") == 10


# ── SQL лент: по всем учёткам, дни МСК, своя глубина у каждой службы ─────────
def test_health_sql_counts_everyone_and_uses_msk_days(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(T, "_rows", lambda sql, params=None: seen.append((sql, params or {})) or [])
    raw = T._health_raw()
    assert set(raw) == {"server", "digest", "digest_arc", "reviews", "news", "backlog", "gold",
                        "canary", "rsearch", "web", "ingest"}
    for sql, params in seen:
        assert "ALL(:ex)" not in sql and ":ex" not in sql and "ex" not in params, sql
    by = {k: next((s, p) for s, p in seen if frag in s) for k, frag in (
        ("server", "'api_request'"), ("digest", "FROM daily_digest\n"),
        ("arc", "daily_digest_archive"), ("reviews", "review_index\n         WHERE source"),
        ("news", "news_item"), ("backlog", "kind IS NULL"), ("rsearch", "reviews_search"),
        ("web", "web_search_chain"), ("ingest", "rag_ingest"))}
    assert by["reviews"][1]["days"] == 77 and by["news"][1]["days"] == 42
    assert by["server"][1]["days"] == 14 and by["ingest"][1]["hard"] == sorted(T._COLLECT_HARD)
    # строки-признаки успеха: дата первого выпуска и «ok» хвоста разметки
    assert "'first'" in by["digest"][0] and "SELECT 'ok'" in by["backlog"][0]
    # review_index.dt хранится без пояса в МСК — день берём как есть, без AT TIME ZONE
    assert "to_char(dt, 'YYYY-MM-DD')" in by["reviews"][0]
    for k in ("server", "reviews", "news", "rsearch", "web", "ingest"):
        assert "Europe/Moscow" in by[k][0], k


def test_health_is_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_health_raw", lambda: calls.append(1) or {
        "server": [{"d": "2026-11-05", "req": 5, "err": 0}]})
    a = T._health()
    b = T._health()
    assert a is b and len(calls) == 1 and a["server"][-1] == "o"
    key = TODAY.isoformat()
    t, val = T._HEALTH_CACHE[key]
    assert T._HEALTH_TTL >= 120
    T._HEALTH_CACHE[key] = (t - T._HEALTH_TTL - 1, val)   # срок вышел — считаем заново
    T._health()
    assert len(calls) == 2


def test_health_survives_calc_error(monkeypatch):
    monkeypatch.setattr(T, "_health_raw", lambda: {"server": "мусор"})
    h = T._health()
    assert h["days"] == DAYS and h["server"] == ["n"] * 14


# ── «Пульс» целиком: новые поля в ответе ────────────────────────────────────
def _pulse_rows(seen):
    def rows(sql, params=None):
        seen.append(sql)
        if "FROM generate_series" in sql and "page_view" in sql:          # dau
            return [{"d": d, "users": 3, "views": 9} for d in DAYS]
        if "AS reports" in sql and "ts < " in sql:                          # прошлый период
            return [{"ai": 274, "reports": 41}]
        if "AS good" in sql:                                               # оценки по дням
            return [{"d": "2026-11-03", "good": 2, "bad": 1}, {"d": "2026-11-05", "good": 1, "bad": 0}]
        if "::date AS d" in sql and "page_view" in sql and "100 days" in sql:   # удержание
            core = [{"username": "core", "d": TODAY - timedelta(days=2 * j)} for j in range(10)]
            return core + [{"username": "once", "d": TODAY - timedelta(days=4)},
                           {"username": "gone", "d": TODAY - timedelta(days=45)}]
        return []
    return rows


def test_metrics_new_fields(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(T, "_rows", _pulse_rows(seen))
    monkeypatch.setattr(T, "_scalar", lambda sql, params=None: 0)
    m = T.metrics(14, ["svc"])
    # нерабочие дни на графике аудитории — по newsflow.is_workday
    assert [i for i, r in enumerate(m["dau"]) if r["off"]] == OFF
    # прошлый такой же период: тот же фильтр людей, окно [_SINCE − days, _SINCE)
    assert m["features"]["ai_total_prev"] == 274 and m["features"]["reports_prev"] == 41
    sql = next(s for s in seen if "AS reports" in s and "ts < " in s)
    assert sql.count("ALL(:ex)") == 2 and "- :days * interval '1 day'" in sql
    assert "<> 'quick'" in sql
    # оценки ответов ИИ по дням периода, с нулями; суммы — как likes / dislikes
    fb = m["ai_feedback"]
    assert [x["d"] for x in fb["by_day"]] == DAYS
    assert fb["by_day"][_i("2026-11-03")] == {"d": "2026-11-03", "good": 2, "bad": 1}
    assert fb["by_day"][0] == {"d": DAYS[0], "good": 0, "bad": 0}
    assert (fb["likes"], fb["dislikes"]) == (3, 1)
    # сегмент каждого — из того же расчёта, что segments; «ушедшие 45 дн назад» — churned
    r = m["retention"]
    assert r["seg_of"] == {"core": "core", "once": "once", "gone": "churned"}
    assert r["segments"]["core"] == 1 and r["segments"]["once"] == 1 and r["segments"]["churned"] == 1
    # ленты — в том же ответе; при пустых запросах — «нет данных»
    assert m["health"]["days"] == DAYS
    assert all(m["health"][k] == ["n"] * 14 for k in T._HEALTH_KEYS)


def test_metrics_prev_unknown_when_query_failed(monkeypatch):
    monkeypatch.setattr(T, "_rows", lambda sql, params=None: [])
    monkeypatch.setattr(T, "_scalar", lambda sql, params=None: None)
    m = T.metrics(7, [])
    assert m["features"]["ai_total_prev"] is None and m["features"]["reports_prev"] is None
    assert len(m["ai_feedback"]["by_day"]) == 7 and m["ai_feedback"]["by_day"][-1]["d"] == DAYS[-1]


def test_retention_seg_of_matches_segments():
    t = date(2026, 10, 4)
    daily = {"a": {t - timedelta(days=j) for j in range(0, 20, 2)}, "b": {t - timedelta(days=5)},
             "c": {t - timedelta(days=1), t - timedelta(days=9), t - timedelta(days=15)},
             "d": {t - timedelta(days=40)}, "e": {t - timedelta(days=95)}, "f": set()}
    r = T._retention_calc(daily, {}, t)
    assert r["seg_of"] == {"a": "core", "b": "once", "c": "regular", "d": "churned"}
    seg = r["segments"]
    assert len(r["seg_of"]) == seg["core"] + seg["regular"] + seg["once"] + seg["churned"]


# ── «Люди»: активность за 14 дней ───────────────────────────────────────────
def test_users_directory_act14(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def rows(sql, params=None):
        calls.append((sql, params or {}))
        if "FROM app_user au" in sql:
            return [{"username": "a", "days_active": 3, "last_seen_ago_s": 60, "today_views": 31,
                     "top_pages": "reviews,ai"},
                    {"username": "svc", "days_active": 0, "last_seen_ago_s": None, "today_views": 0,
                     "top_pages": None}]
        if "UNION ALL" in sql and "ai_query" in sql:
            return [{"username": "a", "d": "2026-11-05", "views": 31, "time_s": 0, "ai": 0},
                    {"username": "a", "d": "2026-11-04", "views": 3, "time_s": 0, "ai": 0},
                    {"username": "a", "d": "2026-11-03", "views": 0, "time_s": 0, "ai": 1},
                    {"username": "a", "d": "2026-10-23", "views": 1, "time_s": 1800, "ai": 0},
                    {"username": None, "d": "2026-11-05", "views": 9, "time_s": 0, "ai": 0}]
        return []
    monkeypatch.setattr(T, "_rows", rows)
    d = T.users_directory(30, ["svc"])
    assert d["act_days"] == DAYS and d["act_off"] == OFF
    by = {r["username"]: r for r in d["users"]}
    assert by["a"]["act14"] == "3" + "0" * 10 + "213"
    assert by["svc"]["act14"] == "0" * 14 and by["svc"]["excluded"]
    # полоса — всегда 14 дней, какой бы период ни был у страницы
    act_sql, act_p = next(c for c in calls if "UNION ALL" in c[0])
    assert act_p == {"days": 14} and "(:days - 1)" in act_sql
    assert next(p for s, p in calls if "FROM app_user au" in s) == {"days": 30}
    assert "'page_leave'" in act_sql and "kind = 'ai_query'" in act_sql


# ── HTTP: ответ «Пульса», «Итоги» за 14 дней, ссылка уведомления об отметке ──
def _h(u):
    return {"X-Authentik-Username": u, "X-Authentik-Name": u}


def test_pulse_endpoint_carries_new_fields(monkeypatch):
    from fastapi.testclient import TestClient

    from bank_audit.web import app as A
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setenv("PULSE_USERS", "viewer-1")
    monkeypatch.setattr(T, "_rows", _pulse_rows([]))
    monkeypatch.setattr(T, "_scalar", lambda sql, params=None: 0)
    c = TestClient(A.app)
    assert c.get("/api/admin/pulse", headers=_h("user-1")).status_code == 403
    m = c.get("/api/admin/pulse?days=14", headers=_h("viewer-1")).json()
    assert m["health"]["days"] == DAYS and m["health"]["off"] == OFF
    assert set(T._HEALTH_KEYS) <= set(m["health"])
    assert m["features"]["ai_total_prev"] == 274 and all("off" in r for r in m["dau"])
    assert len(m["ai_feedback"]["by_day"]) == 14 and "seg_of" in m["retention"]


def test_outcomes_accept_14_days(monkeypatch):
    from bank_audit.web import outcomes as O
    funnel = {"questions": 5, "askers": 2, "vuln_queries": 1, "cases": 1, "case_reports": 0,
              "case_reviews": 0, "case_documents": 0}
    seen = []
    monkeypatch.setattr(T, "hidden_users", list)
    monkeypatch.setattr(O, "_collect", lambda ex, since, until: seen.append((since, until)) or {})
    monkeypatch.setattr(O, "_build", lambda raw, since: {"funnel": dict(funnel),
                                                         "quality": {"ai": {"good_share": 0.5}}})
    monkeypatch.setattr(O, "last_run", lambda: None)
    monkeypatch.setattr(O, "pending_count", lambda: 0)
    O._CACHE.clear()
    rep = O.report("viewer-1", None, 14)
    assert rep["period"]["days"] == 14 and rep["prev"]["questions"] == 5
    since, _until, prev = O.period(14)
    assert seen == [(since, None), (prev, since)]         # прошлые 14 дней — до начала текущих
    assert (since - prev).days == 14
    assert O.period(14, TODAY)[0] == datetime(2026, 10, 23, tzinfo=MSK)
    O._CACHE.clear()
    assert O.report("viewer-1", None, 15)["period"]["days"] == 0          # чужой период — всё время
    O._CACHE.clear()
    assert 14 in O.PERIODS


def test_outcomes_xlsx_name_for_14_days(monkeypatch):
    from urllib.parse import quote

    from fastapi.testclient import TestClient

    from bank_audit.web import app as A
    from bank_audit.web import outcomes as O
    monkeypatch.setenv("ADMIN_USERS", "owner-1")
    monkeypatch.setenv("PULSE_USERS", "")
    monkeypatch.setattr(T, "hidden_users", list)
    monkeypatch.setattr(O, "report", lambda v, wm, days: {"period": {"days": days}})
    monkeypatch.setattr(O, "to_xlsx", lambda rep: b"xlsx")
    r = TestClient(A.app).get("/api/admin/pulse/outcomes.xlsx?days=14", headers=_h("owner-1"))
    assert r.status_code == 200 and quote("AuditLens_итоги_14_дн.xlsx") in r.headers["content-disposition"]


def test_slice_flag_notice_opens_flags_view(monkeypatch):
    from fastapi.testclient import TestClient

    from bank_audit.rag import review_slices as rs
    from bank_audit.web import app as A
    from bank_audit.web import inbox
    sent = {}
    monkeypatch.setattr(rs, "flag", lambda url, key, username, note=None: True)
    monkeypatch.setattr(inbox, "notify_team", lambda kind, **kw: sent.update(kind=kind, **kw))
    r = TestClient(A.app).post("/api/reviews/slice-flag", headers=_h("auditor-1"),
                               json={"url": "https://x/1", "key": "youth"})
    assert r.status_code == 200
    assert sent["kind"] == "team_slice" and sent["link"] == "#pulse?tab=inbox&view=flags"
