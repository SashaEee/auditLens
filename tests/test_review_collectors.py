"""Сборщики отзывов площадок и ночной сбор (аудит парсеров 30.09).

БД и сеть не нужны: справочники площадок и распознаватель банков подменены.
"""
from datetime import timedelta

import pytest

from bank_audit.digest import scheduler
from bank_audit.rag import bankiru_reviews as br
from bank_audit.sources import finuslugi_reviews as fin
from bank_audit.sources import review_streams as rs


def test_nightly_sources_exclude_review_streams():
    """review_streams пишет журнал дважды в сутки под именами banki_reviews и
    sravni_reviews — ночной сбор не должен принимать их за свой прогон."""
    nightly = scheduler.nightly_sources()
    assert "banki_reviews" not in nightly and "sravni_reviews" not in nightly
    assert "bankiros_reviews" not in nightly            # выключен 30.09
    assert "finuslugi_reviews" in nightly
    tariffs = scheduler._tariff_sources()
    assert "sravni_api" in tariffs
    assert not [s for s in tariffs if "review" in s]      # свежесть тарифов — не по отзывам


from datetime import datetime  # noqa: E402

MSK = scheduler.MSK
SLOT = scheduler.INGEST_HOUR


def _now(h, m=0):
    return datetime(2026, 10, 12, h, m, tzinfo=MSK)


def _plan(at_slot, now, today=None, busy=False, age_h=5.0, targets=None):
    targets = targets if targets is not None else scheduler.nightly_targets()
    return scheduler._plan(at_slot=at_slot, now=now, targets=targets, today=today or {},
                           busy=busy, age_h=age_h)


def test_nightly_targets_follow_config():
    """«Собрано» считается по целям: у sravni_api их десятки, у РКО — одна."""
    targets = scheduler.nightly_targets()
    srcs = {s for s, _ in targets}
    assert srcs == set(scheduler.nightly_sources())
    assert sum(1 for s, _ in targets if s == "sravni_api") > 10
    assert len(set(targets)) == len(targets)


def test_manual_target_before_slot_does_not_skip_the_rest():
    """04.10: ручной прогон РКО в 02:24 засчитали как весь ночной сбор. Теперь
    собранная заранее цель просто не повторяется — остальные идут в слот."""
    targets = scheduler.nightly_targets()
    rko = next(t for t in targets if t[0] == "sravni_rko")
    reason, todo, _ = _plan(True, _now(SLOT), today={rko: {"done": True, "fails": 0, "last": None}})
    assert reason == "плановый слот" and rko not in todo and len(todo) == len(targets) - 1


def test_partially_failed_source_is_recollected():
    """Проверка 72add4a: у sravni_api прошли 4 цели из 72 (деплой посреди сбора) —
    источник не «собран», досбор берёт оставшиеся цели, а не ни одной."""
    targets = scheduler.nightly_targets()
    api = [t for t in targets if t[0] == "sravni_api"]
    today = {t: {"done": True, "fails": 0, "last": None} for t in targets if t[0] != "sravni_api"}
    today.update({t: {"done": True, "fails": 0, "last": None} for t in api[:4]})
    reason, todo, spent = _plan(False, _now(SLOT + 1), today=today)
    assert reason == "пропущенный слот" and todo == api[4:] and not spent


def test_after_slot_attempts_come_from_journal():
    """Попытки — по упавшим прогонам цели после слота (журнал, а не память
    процесса: рестарт счёт не сбрасывает). Исчерпавшая цель не мешает остальным."""
    targets = scheduler.nightly_targets()
    dead, alive = targets[0], targets[1]
    today = {t: {"done": True, "fails": 0, "last": None} for t in targets[2:]}
    today[dead] = {"done": False, "fails": scheduler.INGEST_MAX_ATTEMPTS, "last": None}
    today[alive] = {"done": False, "fails": scheduler.INGEST_MAX_ATTEMPTS - 1, "last": None}
    reason, todo, spent = _plan(False, _now(SLOT + 3), today=today)
    assert todo == [alive] and spent == [dead] and reason == "пропущенный слот"
    # в слот упавшие ночью цели собираются в любом случае
    reason, todo, _ = _plan(True, _now(SLOT), today={dead: {"done": False, "fails": 9, "last": None}})
    assert dead in todo


def test_night_catchup_only_when_stale_and_throttled():
    """До слота — только при протухших данных и не чаще раза в NIGHT_RETRY_H на
    цель: ночной сбой сети не превращается в ежечасный обход и не съедает слот."""
    targets = scheduler.nightly_targets()[:2]
    assert _plan(False, _now(2), age_h=20.0, targets=targets)[0] is None
    recent = {targets[0]: {"done": False, "fails": 0, "last": _now(1, 30)}}
    reason, todo, _ = _plan(False, _now(2), today=recent, age_h=40.0, targets=targets)
    assert reason.startswith("данные устарели") and todo == [targets[1]]
    old = {targets[0]: {"done": False, "fails": 0, "last": _now(1)}}   # 3,5 ч назад
    assert _plan(False, _now(4, 30), today=old, age_h=40.0, targets=targets)[1] == targets


@pytest.mark.parametrize("busy,all_done", [(True, False), (False, True)])
def test_nothing_when_busy_or_all_done(busy, all_done):
    targets = scheduler.nightly_targets()
    today = {t: {"done": True, "fails": 0, "last": None} for t in targets} if all_done else {}
    assert _plan(True, _now(SLOT), today=today, busy=busy) == (None, [], [])


def test_journal_criteria_in_sql():
    """Критерии живут в SQL (БД в тестах нет) — проверяем текстом, как миграции."""
    import inspect
    sql = inspect.getsource(scheduler._targets_today)
    assert "status IN ('ok', 'partial')" in sql and "finished_at IS NOT NULL" in sql
    assert "started_at >= :slot" in sql and "GROUP BY source, target_name" in sql


def test_run_only_missing_targets_and_tail_once(monkeypatch):
    """Досбор зовёт сборщик только по недостающим целям (источник целиком — одним
    прогоном), а хвост — один раз за сутки и только если что-то собралось."""
    import sys
    import types
    targets = scheduler.nightly_targets()
    api = [t for t in targets if t[0] == "sravni_api"]
    rko = [t for t in targets if t[0] == "sravni_rko"]
    calls, tail = [], []
    fake_runner = types.SimpleNamespace(ingest=lambda src, tn, target_names=None: calls.append((src, target_names)))
    monkeypatch.setitem(sys.modules, "bank_audit.orchestrator.runner", fake_runner)
    monkeypatch.setattr(scheduler, "_captcha_pending", lambda: False)
    for mod, fn in [("bank_audit.normalizer.offers", "expire_stale_offers"),
                    ("bank_audit.normalizer.offers", "validate_offer_urls"),
                    ("bank_audit.quality.checks", "run_quality"),
                    ("bank_audit.normalizer.enrich_llm", "enrich")]:
        monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(
            **{**getattr(sys.modules.get(mod), "__dict__", {}), fn: (lambda *a, _f=fn, **k: tail.append(_f) or {})}))
    state = {"done": set(), "tail_day": None}

    def today(now=None):
        return {t: {"done": True, "fails": 0, "last": None} for t in state["done"]}, False
    monkeypatch.setattr(scheduler, "_targets_today", today)
    monkeypatch.setattr(scheduler, "_tail_due", lambda d: state["tail_day"] != d)
    monkeypatch.setattr(scheduler, "_tail_mark", lambda d: state.update(tail_day=d))

    def run(todo, collect):
        calls.clear(); tail.clear()
        orig = fake_runner.ingest
        fake_runner.ingest = lambda src, tn, target_names=None: (calls.append((src, target_names)),
                                                                 state["done"].update(collect))
        scheduler._run_ingest_all(None, todo)
        fake_runner.ingest = orig
        return list(calls), list(tail)

    c, t = run(api[5:] + rko, api[5:] + rko)              # часть sravni_api + РКО целиком
    assert ("sravni_api", [n for _, n in api[5:]]) in c and ("sravni_rko", None) in c
    assert t.count("run_quality") == 1 and "enrich" in t  # первый сбор дня — хвост есть
    c, t = run(api[:5], api[:5])
    assert c == [("sravni_api", [n for _, n in api[:5]])] and t == []   # хвост уже был сегодня
    state["tail_day"] = None
    c, t = run(api[:5], [])                               # ничего нового — хвост не нужен
    assert t == []


@pytest.mark.parametrize("n,key,ok", [
    ("рост банк", "т банк", False),          # WRatio 90 — было «Т-Банк»
    ("тинькоф банк", "т банк", False),
    ("пао сбербанк", "сбербанк", True),
    ("сбербанк россии", "сбербанк", True),
    ("втб 24", "втб", True),
    ("альфабанк", "альфа банк", True),        # опечатка/слитно — похоже целиком
])
def test_fuzzy_match_needs_whole_words(n, key, ok):
    assert br._fuzzy_ok(n, key) is ok


def _fake_resolver(monkeypatch, table: dict[str, str]):
    monkeypatch.setattr(br, "resolve_bank", lambda name: table.get(name))


def test_sravni_prefers_exact_name(monkeypatch):
    orgs = [{"id": "rost", "alias": "rost-bank", "name": "РОСТ БАНК"},
            {"id": "tb", "alias": "t-bank", "name": "Т-Банк"}]
    _fake_resolver(monkeypatch, {"РОСТ БАНК": "Т-Банк", "Т-Банк": "Т-Банк"})
    assert rs._sravni_match(orgs, "Т-Банк")["alias"] == "t-bank"


def test_sravni_picks_most_active_among_namesakes(monkeypatch):
    orgs = [{"id": "south", "alias": "uralsib-jug-bank", "name": "Уралсиб-Юг Банк "},
            {"id": "main", "alias": "uralsib", "name": "Банк Уралсиб"}]
    _fake_resolver(monkeypatch, {"Уралсиб-Юг Банк ": "Уралсиб", "Банк Уралсиб": "Уралсиб"})
    totals = {"south": (0, ""), "main": (1410, "2026-09-29")}
    monkeypatch.setattr(rs, "_sravni_total", lambda c, oid: totals[oid])
    monkeypatch.setattr(rs, "_PAUSE_S", 0)
    assert rs._sravni_match(orgs, "Уралсиб", c=object())["alias"] == "uralsib"
    # без клиента — единственный кандидат или первый (как раньше), но не падаем
    assert rs._sravni_match(orgs[:1], "Уралсиб")["alias"] == "uralsib-jug-bank"


def test_finuslugi_matches_renamed_banks(monkeypatch):
    companies = {"45": {"name": "Т-Банк"}, "368": {"name": "БСТ-БАНК"},
                 "30": {"name": "Банк ПСБ"}, "1": {"name": "Сбербанк"}}
    _fake_resolver(monkeypatch, {"Банк ПСБ": "ПСБ", "ПСБ": "ПСБ", "Т-Банк": "Т-Банк",
                                 "Сбербанк": "Сбербанк"})
    assert fin._match_company(companies, "Т-Банк") == "45"      # не «БСТ-БАНК»
    assert fin._match_company(companies, "ПСБ") == "30"
    assert fin._match_company(companies, "Сбербанк") == "1"


def test_finuslugi_time_is_moscow():
    d = fin._parse_date("2026-08-29 21:31:00")
    assert d.utcoffset() == timedelta(hours=3)
    assert d.astimezone(fin.timezone.utc).hour == 18
    assert fin._parse_date("1971-01-01 00:00:00") is None       # заглушка площадки
