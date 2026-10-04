"""Волна 3 аудита 03.10 «Чистые данные»: РКО без ложных версий, журнал без
откатов, справочник банков, протухание рейтинга, база знаний. БД и сеть не
нужны — сессии и чтения подменяются."""
from __future__ import annotations

import json
import os
import random
import types
from datetime import date, datetime, timedelta, timezone

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


# ── РКО: ключ тарифа и представитель ─────────────────────────────────────────

def _rko(rows):
    from bank_audit.sources.sravni_rko import SravniRkoAdapter
    ad = SravniRkoAdapter.__new__(SravniRkoAdapter)
    return list(ad.parse_offers(json.dumps({"offers": rows}).encode(), {"name": "rko_all"}))


def _t(i, bank, name, price, alias="x", orgs=("ooo",), date_from="2026-01-01"):
    return {"id": str(i), "bank": bank, "bank_alias": bank.lower(), "name": name, "alias": alias,
            "price_month": price, "org_types": list(orgs), "date_from": date_from}


def test_rko_shared_alias_keeps_distinct_tariffs():
    """ПЛТ-02: «Мини/Опти/Макси/Профи» ПСБ под одним алиасом были ОДНИМ оффером."""
    rows = [_t(1, "ПСБ", "Мини", 690, "platite-menshe"), _t(2, "ПСБ", "Опти", 1990, "platite-menshe"),
            _t(3, "ПСБ", "Макси", 3500, "platite-menshe"), _t(4, "ПСБ", "Профи", 9900, "platite-menshe")]
    ds = _rko(rows)
    assert len(ds) == 4 and len({d.external_id for d in ds}) == 4
    assert {d.title: float(d.fee_service) for d in ds}["Профи"] == 9900
    assert all(d.external_id.startswith("rko2_") for d in ds)


def test_rko_regional_copies_one_card_independent_of_order():
    rows = [_t(1, "Совкомбанк", "Оптимальный", 3290, "optimalnyy-4"),
            _t(2, "Совкомбанк", "Оптимальный", 3790, "optimalnyy-5"),
            _t(3, "Банк Х", "Старт", 500, date_from="2026-01-01"),
            _t(4, "Банк Х", "Старт", 900, date_from="2026-03-01")]   # новая редакция
    from bank_audit.normalizer.offers import _digest
    base = None
    for seed in range(20):
        rr = rows[:]
        random.Random(seed).shuffle(rr)
        got = {d.external_id: (_digest(d), float(d.fee_service), d.raw["variants"]) for d in _rko(rr)}
        base = base or got
        assert got == base
    by_price = sorted(v[1] for v in base.values())
    assert by_price == [900.0, 3290.0]          # новая редакция, а среди копий одной — меньшая цена


def test_rko_plus_and_case_and_forms():
    ds = _rko([_t(1, "Б", "Оптимум", 600), _t(2, "Б", "Оптимум+", 1200),
               _t(3, "Б", "Стартовый Лайт", 1), _t(4, "Б", "стартовый лайт", 1),
               _t(5, "Б", "Базовый", 1, orgs=("ip", "ooo")), _t(6, "Б", "Базовый", 1, orgs=("ooo", "ip")),
               _t(7, "Б", "Базовый", 1, orgs=("ooo",))])
    assert len(ds) == 5


def test_normalize_batch_writes_one_version_per_key(monkeypatch):
    """Две копии ключа с разной ценой за прогон — одна версия, а не «туда-обратно»."""
    from bank_audit.models import OfferDraft
    from bank_audit.normalizer import offers as O
    written = []

    class S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(O.db, "session", lambda: S())
    monkeypatch.setattr(O, "upsert_offer", lambda s, d, *a, **k: (written.append(float(d.fee_service)), (1, True))[1])

    def d(price, bank="Солид Банк"):
        return OfferDraft(bank_name_raw=bank, category="rko", external_id="k1", title="Легкий",
                          fee_service=price)
    r1 = O.normalize_batch([d(4992), d(1658, "СОЛИД БАНК")], None, None, "sravni_rko")
    first = written[:]
    written.clear()
    r2 = O.normalize_batch([d(1658, "СОЛИД БАНК"), d(4992)], None, None, "sravni_rko")
    assert len(first) == 1 and first == written
    assert r1["dup_rows"] == 1 and r1["dup_conflicts"] == 1 and r2["seen"] == 2


def test_dedup_partitions_rko_by_business_form_and_plus():
    import inspect
    from bank_audit.normalizer import offers as O
    src = inspect.getsource(O.dedup_active_offers)
    assert "sub_segment" in src and "плюс" in src


def test_day_revert_sql_is_the_single_definition():
    """Журнал, итоги выпуска, ИИ и метки жалоб — одно правило отката."""
    import inspect
    from bank_audit.ai import analyst
    from bank_audit.digest import aggregator
    from bank_audit.rag import reviews_work
    from bank_audit.web import app as A
    from bank_audit.normalizer import offers as O
    for mod in (analyst, aggregator, reviews_work, A):
        src = inspect.getsource(mod)
        assert "REVERT_IDS_SQL" in src or "revert_ids_sql(" in src
    # «Обзор» читает колонку change_id из SELECT * FROM (…) — не unnest
    assert O.REVERT_IDS_SQL.strip().startswith("SELECT z.change_id")
    assert 'x["change_id"]' in inspect.getsource(aggregator)
    assert "AND (c.offer_id = :o)" in O.revert_ids_sql("c.offer_id = :o")
    assert "/*SCOPE*/" not in O.REVERT_IDS_SQL


def test_recent_changes_v2_shape_and_folding(monkeypatch):
    from bank_audit.web import app as A
    seen = []

    def fake_q(sql, params=None):
        seen.append((sql, params))
        if "GROUP BY 1 ORDER BY 2 DESC" in sql:
            return [{"category": "rko", "n": 30, "n_banks": 7, "last_at": datetime(2026, 10, 3)}]
        if "ch.change_id = :fid" in sql:
            return [{"change_id": 5, "offer_id": 1, "changed_at": datetime(2026, 10, 3), "diff": {},
                     "bank_slug": "b", "bank_name": "Б", "is_sber": False, "category": "rko",
                     "title": "Т", "url": None, "sig": True, "rev": True}]
        return [{"change_id": 9, "offer_id": 2, "changed_at": datetime(2026, 10, 3),
                 "diff": {"rate_pct": {"from": "10", "to": "11"}}, "bank_slug": "b",
                 "bank_name": "Б", "is_sber": False, "category": "deposit", "title": "В",
                 "url": None, "total_rows": 1}]
    monkeypatch.setattr(A, "q", fake_q)
    monkeypatch.setattr(A, "scalar", lambda *a, **k: 19)
    out = A.recent_changes(fold="rko", focus=5, v=2)
    assert out["total"] == 1 and out["items"][0]["rate_delta"] == 1.0
    assert out["folded"][0]["n"] == 30 and out["hidden_reverts"] == 19
    assert out["focus_status"] == "reverted"
    main_sql = seen[0][0]
    assert "ch.change_id NOT IN" in main_sql and "<> ALL(:fold)" in main_sql
    assert "ORDER BY ch.changed_at DESC, ch.change_id DESC" in main_sql
    # фильтр по банку — без свёртки: человек пришёл за этими строками
    seen.clear()
    out = A.recent_changes(bank_slug="sberbank", fold="rko", v=2)
    assert out["folded"] == [] and "<> ALL(:fold)" not in seen[0][0]
    assert isinstance(A.recent_changes(v=1), list)          # старый формат жив


# ── Справочник банков ─────────────────────────────────────────────────────────

def test_bank_key_pairs():
    from bank_audit.normalizer.rules import bank_key as k
    for a, b in [("Точка Банк", "ТОЧКА"), ("Банк ТКБ", "ТКБ Банк"), ("ВБРР", "Банк ВБРР"),
                 ("РОССИЯ", "Банк «РОССИЯ»"), ("МТС-БАНК", "МТС Банк"),
                 ("Кредит Европа Банк (Россия)", "Кредит Европа Банк"),
                 ("АО «Банк ДОМ.РФ»", "Банк ДОМ.РФ"), ("Контур.Банк", "КОНТУР"), ("ВТБ (ПАО)", "ВТБ")]:
        assert k(a) == k(b), (a, b)
    for a, b in [("ИНГ Банк", "Инго Банк"), ("ВЕК", "НОВЫЙ ВЕК"), ("КАПИТАЛ", "КАПИТАЛБАНК"),
                 ("ТРАСТ", "РИКОМ-ТРАСТ"), ("МИКО-БАНК", "НИКО-БАНК"),
                 ("Банк «Санкт-Петербург»", "Санкт-Петербургский Банк Инвестиций")]:
        assert k(a) != k(b), (a, b)


@pytest.mark.parametrize("a,b,ok", [
    ("точка", "точка банк", True), ("пао сбербанк", "сбербанк", True), ("втб 24", "втб", True),
    ("альфабанк", "альфа банк", True), ("риком траст", "траст", False),
    ("инг банк", "инго банк", False), ("камский коммерческий банк", "кетовский коммерческий банк", False),
    ("юнистрим денежные переводы", "юнистрим", False), ("рост банк", "т банк", False)])
def test_corpus_fuzzy_rule(a, b, ok):
    from bank_audit.rag.bankiru_reviews import _fuzzy_ok
    assert _fuzzy_ok(a, b) is ok


def test_bspb_rename_and_corpus_names():
    from bank_audit.rag.bankiru_fts import canon_bank
    from bank_audit.rag.bankiru_reviews import corpus_names
    assert canon_bank("Банк «Санкт-Петербург»") == "БСПБ"
    assert corpus_names("БСПБ") == ["БСПБ", "Банк «Санкт-Петербург»"]


def test_resolver_name_variants_keep_real_matches(monkeypatch):
    """Строгий подбор не теряет настоящие совпадения: расшифровка в скобках,
    имя без скобок и «|» в имени корпуса; ложные склейки по-прежнему режутся."""
    from bank_audit.rag import bankiru_reviews as R
    corpus = ["МТС Деньги (ЭКСИ-Банк)", "Просто|Банк", "Урал ФД", "Инго Банк", "Траст",
              "Банк «Саратов»", "Контур.Банк", "БСПБ",
              "Азиатско-Тихоокеанский банк (АТБ)", "Уральский банк реконструкции и развития (УБРиР)"]
    monkeypatch.setattr(R, "_norm2name", R._index_names(corpus))
    for name, want in [("ЭКСИ-БАНК", "МТС Деньги (ЭКСИ-Банк)"), ("Просто Банк", "Просто|Банк"),
                       ("Урал ФД Банк (Дом.ру Банк)", "Урал ФД"),
                       ("Азиатско-Тихоокеанский Банк", "Азиатско-Тихоокеанский банк (АТБ)"),
                       ("УРАЛЬСКИЙ БАНК РЕКОНСТРУКЦИИ И РАЗВИТИЯ",
                        "Уральский банк реконструкции и развития (УБРиР)"),
                       ("ИНГ Банк", None), ("РИКОМ-ТРАСТ", None),
                       # город в скобках — не банк корпуса
                       ("Экономбанк (Саратов)", None),
                       ("Энергомашбанк (Санкт-Петербург)", None),
                       ("Контур Банк", "Контур.Банк"), ("НБ «ТРАСТ»", "Траст")]:
        assert R.resolve_bank(name) == want, name
    for c in corpus:                       # корпусное имя — само в себя (sync_local)
        assert R.resolve_bank(c) == c


def test_banks_one_row_per_bank_id_with_two_rating_offers():
    """У «Почта Банка» два свежих рейтинговых оффера — строка «Банков» одна."""
    from bank_audit.web import app as A
    rows = [{"bank_id": 9, "slug": "pochtabank", "name": "Почта Банк", "avg_grade": 3.9,
             "place": 26, "total_reviews": 13075, "rating_stale": False, "rating_ext": "banki_rating_1"},
            {"bank_id": 9, "slug": "pochtabank", "name": "Почта Банк", "avg_grade": 4.5,
             "place": 178, "total_reviews": 8, "rating_stale": False, "rating_ext": "banki_rating_2"},
            {"bank_id": 30, "slug": "unknown_x", "name": "Почта-Банк", "avg_grade": 4.0,
             "place": 90, "total_reviews": 50, "rating_stale": False, "rating_ext": "banki_rating_3"}]
    out = A._collapse_banks(rows)
    assert sorted((r["bank_id"], r["place"]) for r in out) == [(9, 26), (30, 90)]


def test_banks_collapse_and_own_one_to_one():
    """ДАН-01: «ТОЧКА» и «Точка Банк» — одна строка; отзывы Инго — не у «ИНГ Банк»."""
    from bank_audit.web import app as A
    rows = [
        {"bank_id": 1, "slug": "unknown_a", "name": "Точка Банк", "avg_grade": 4.1, "place": 18,
         "total_reviews": 900, "rating_stale": False, "rating_ext": "banki_rating_7"},
        {"bank_id": 2, "slug": "unknown_b", "name": "ТОЧКА", "avg_grade": 4.0, "place": 18,
         "total_reviews": 880, "rating_stale": True, "rating_ext": "banki_rating_7"},
        {"bank_id": 3, "slug": "unknown_c", "name": "Инго Банк", "avg_grade": 4.0, "place": 40,
         "total_reviews": 300, "rating_stale": False, "rating_ext": "banki_rating_8"},
        {"bank_id": 4, "slug": "unknown_d", "name": "ИНГ Банк", "avg_grade": None, "place": None,
         "total_reviews": None, "rating_stale": False, "rating_ext": None},
        {"bank_id": 5, "slug": "unknown_e", "name": "ИНГО", "avg_grade": 3.9, "place": 41,
         "total_reviews": 100, "rating_stale": True, "rating_ext": "banki_rating_8"},
        {"bank_id": 7, "slug": "unknown_f", "name": "Таврический", "avg_grade": 3.5, "place": 52,
         "total_reviews": 50, "rating_stale": True, "rating_ext": "banki_rating_10"},
    ]
    out = A._collapse_banks(rows)
    names = {r["name"] for r in out}
    assert "ТОЧКА" not in names and "Точка Банк" in names
    ingo = next(r for r in out if r["name"] == "Инго Банк")
    assert ingo["place"] == 40 and "ИНГО" not in names        # прежнее написание — та же строка
    stale = next(r for r in out if r["name"] == "Таврический")
    assert stale["place"] is None and stale["place_last"] == 52   # место уже занял другой
    own = {"Инго Банк": {"n": 269, "last_dt": date(2026, 10, 2), "avg_rating": 2.1},
           "Почта Банк": {"n": 2536, "last_dt": date(2026, 7, 21), "avg_rating": 1.9}}
    out.append({"bank_id": 6, "slug": "pochtabank", "name": "Почта Банк", "avg_grade": 4.0})
    resolve = {"Инго Банк": "Инго Банк", "ИНГ Банк": "Инго Банк", "ИНГО": "Инго Банк",
               "Почта Банк": "Почта Банк"}.get
    A._assign_own(out, own, resolve)
    by = {r["name"]: r for r in out}
    assert by["Инго Банк"]["own_reviews"] == 269
    assert by["ИНГ Банк"]["own_reviews"] == 0 and by["ИНГ Банк"]["own_shared_with"] == "Инго Банк"
    assert "ВТБ" in by["Почта Банк"]["own_note"]


def test_gone_bank_rule_and_rename_candidates():
    from bank_audit.rag.reviews_dash import classify_gone
    today = date(2026, 10, 3)
    out = classify_gone(
        [("Почта Банк", 0.93, date(2026, 7, 21)), ("Банк «Санкт-Петербург»", 1.3, date(2026, 8, 19)),
         ("Малый", 0.1, date(2026, 9, 3)), ("Тихий", 5.0, date(2026, 9, 29))],
        {"БСПБ": (date(2026, 8, 12), 2.0)}, today)
    by = {g["bank"]: g for g in out}
    assert set(by) == {"Почта Банк", "Банк «Санкт-Петербург»"}
    assert by["Банк «Санкт-Петербург»"]["rename_candidates"] == ["БСПБ"]
    assert by["Почта Банк"]["known"] and by["Банк «Санкт-Петербург»"]["known"] is None


def test_non_bank_service_and_rating_row_binding():
    from bank_audit.categories import NON_BANK_SQL_RE, _NON_BANK_RE, is_non_bank
    assert is_non_bank("Плати по всему миру") and not is_non_bank("Банк «Платина»")
    assert NON_BANK_SQL_RE == _NON_BANK_RE.pattern


# ── Протухание рейтинга ───────────────────────────────────────────────────────

class _RatingSess:
    def __init__(self, runs, active, stale_ids):
        self.runs, self.active, self.stale_ids, self.updates = runs, active, stale_ids, []

    def execute(self, sql, p=None):
        q_ = str(sql)
        if "FROM extraction_run" in q_:
            return types.SimpleNamespace(all=lambda: self.runs)
        if "count(*)" in q_:
            return types.SimpleNamespace(scalar=lambda: self.active)
        if q_.strip().startswith("UPDATE"):
            self.updates.append(p)
            return types.SimpleNamespace(rowcount=len(p["ids"]))
        return types.SimpleNamespace(all=lambda: [(i,) for i in self.stale_ids])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_rating_rows_expire_only_after_complete_runs(monkeypatch):
    """ДАН-14: 58 строк рейтинга держали места выпавших банков; сломанный
    сборщик при этом не должен гасить рейтинг."""
    from bank_audit.normalizer import offers as O
    t = datetime(2026, 10, 3, 5, tzinfo=timezone.utc)
    ok = [("ok", 290, t), ("ok", 291, t - timedelta(days=1)), ("ok", 289, t - timedelta(days=2))]
    sess = _RatingSess(ok, 300, [11, 12])
    monkeypatch.setattr(O.db, "session", lambda: sess)
    assert O.expire_rating_rows()["expired"] == 2 and sess.updates[0]["ids"] == [11, 12]
    for runs in ([("partial", 120, t)] + ok[1:], ok[:2], [("ok", 0, t)] + ok[1:]):
        sess = _RatingSess(runs, 300, [11, 12])
        monkeypatch.setattr(O.db, "session", lambda: sess)
        assert O.expire_rating_rows()["status"] == "skip_incomplete_runs" and not sess.updates
    sess = _RatingSess(ok, 300, list(range(100)))           # больше четверти — предохранитель
    monkeypatch.setattr(O.db, "session", lambda: sess)
    assert O.expire_rating_rows()["status"] == "blocked" and not sess.updates


def test_rko_expires_by_its_own_runs(monkeypatch):
    """Переименованный тариф РКО заводит новую карточку — старая гаснет по
    прогонам sravni_rko, а не живёт вечно."""
    from bank_audit.normalizer import offers as O
    t = datetime(2026, 10, 3, 5, tzinfo=timezone.utc)
    ok = [("ok", 410, t), ("ok", 405, t - timedelta(days=1)), ("ok", 409, t - timedelta(days=2))]
    sess = _RatingSess(ok, 400, [7])
    seen = []
    orig = sess.execute

    def ex(sql, p=None):
        seen.append(p)
        return orig(sql, p)
    sess.execute = ex
    monkeypatch.setattr(O.db, "session", lambda: sess)
    assert O._expire_by_runs("sravni_rko", "rko", 250)["expired"] == 1
    assert seen[0]["src"] == "sravni_rko" and any((x or {}).get("cat") == "rko" for x in seen)
    assert ("sravni_rko", "rko", 250) in O._RUN_EXPIRY


class _StaleSess:
    def __init__(self, stats):
        self.stats, self.updates = stats, []

    def execute(self, sql, p=None):
        if str(sql).strip().startswith("UPDATE"):
            self.updates.append(p)
            return types.SimpleNamespace(rowcount=1)
        return types.SimpleNamespace(all=lambda: self.stats)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_daily_expiry_breaker_keeps_category_on_collector_outage(monkeypatch):
    """Сломанный сборщик за три ночи не гасит категорию целиком: при массовой
    пропаже гасим только то, чего нет две недели."""
    from bank_audit.normalizer import offers as O
    sess = _StaleSess([("npf", 30, 30), ("deposit", 2000, 40), ("metals", 50, 0)])
    monkeypatch.setattr(O.db, "session", lambda: sess)
    monkeypatch.setattr(O, "_expire_by_runs", lambda *a, **k: {"status": "skip"})
    O.expire_stale_offers()
    by_cat = {u["cat"]: u["d"] for u in sess.updates}
    assert by_cat == {"npf": O._STALE_HARD_D, "deposit": 3}


class _Adapter:
    def __init__(self, *a, **k):
        pass

    def fetch(self, tgt):
        snap = types.SimpleNamespace(url="u", category="other", fetched_at=None, http_status=200,
                                     content_sha256="x", storage_path="p", bytes=1)
        return types.SimpleNamespace(snapshot=snap, html=b"", complete=False)

    def parse_offers(self, html, tgt):
        return []

    def parse_reviews(self, html, tgt):
        return []


@pytest.mark.parametrize("snap_id", [None, 5])
def test_partial_fetch_marks_run_partial(monkeypatch, snap_id):
    """Оборванный обход пишет прогон 'partial' — и при новом, и при неизменном
    снимке; иначе протухание по прогонам засчитало бы его как полный."""
    from bank_audit.orchestrator import runner as R
    from bank_audit.sources.base import FetchResult
    assert FetchResult(snapshot=None, html=b"").complete is True
    fin = []

    class _S:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):          # id уже сохранённого снимка
            return types.SimpleNamespace(scalar=lambda: 5)
    monkeypatch.setattr(R.Settings, "load", classmethod(lambda cls: types.SimpleNamespace(
        raw_dir="/tmp", raw={"http": {"request_delay_ms": 0},
                             "browser": {"headless": True, "nav_timeout_s": 1,
                                         "scroll_pause_ms": 1, "max_scrolls": 1}},
        browser_profile=None)))
    monkeypatch.setattr(R.db, "init", lambda *a: None)
    monkeypatch.setattr(R.db, "session", lambda: _S())
    monkeypatch.setattr(R, "load_adapter", lambda k: (_Adapter, {"targets": [{"name": "t"}]}))
    for n in ("RawStore", "HttpCollector", "BrowserCollector"):
        monkeypatch.setattr(R, n, lambda *a, **k: types.SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(R, "_start_run", lambda *a: 1)
    monkeypatch.setattr(R, "_upsert_source_page", lambda *a: 2)
    monkeypatch.setattr(R, "_store_snapshot", lambda *a: snap_id)
    monkeypatch.setattr(R, "_finish_run", lambda s, rid, st, *a, **k: fin.append(st))
    monkeypatch.setattr(R.offers_norm, "dedup_active_offers", lambda: None)
    monkeypatch.setattr(R.offers_norm, "expire_cross_promo", lambda: None)
    norm = []
    monkeypatch.setattr(_Adapter, "parse_offers", lambda self, h, t: ["draft"])
    monkeypatch.setattr(R.offers_norm, "normalize_batch",
                        lambda offers, sid, pid, source_name=None: norm.append(sid)
                        or {"seen": 1, "written": 0})
    R.ingest("x")
    assert fin == ["partial"]
    # неизменный снимок тоже разбирается — upsert продлевает last_seen
    assert norm == [5]


# ── База знаний ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,want", [
    ("https://www.banki.ru/products/hypothec/gazprombank/", ["mortgage"]),
    ("https://www.banki.ru/products/debitcards/", ["cards_debit"]),
    ("https://www.banki.ru/products/creditcards/", ["cards_credit"]),
    ("https://www.sberbank.ru/ru/person/credits/home/buying_complete_house", ["mortgage"]),
    ("https://www.sberbank.ru/ru/person/autopayment", []),
    ("https://alfabank.ru/everyday/application/", []),
    ("https://www.vtb.ru/personal/karty/kreditnye/", ["cards_credit"]),
    ("https://www.vtb.ru/personal/perevody-za-granicu/", ["transfers_intl"]),
    ("https://www.cbr.ru/hd_base/key-rate/", []),
    # автоплатёж и КАСКО — не автокредит; «защита прав потребителей» — не кредиты
    ("https://www.sberbank.ru/ru/person/dist_services/auto_payment", ["transfers"]),
    ("https://www.tbank.ru/payments/auto-payments/", ["transfers"]),
    ("https://www.vtb.ru/personal/avto-platezh/", []),
    ("https://www.sberbank.ru/ru/person/insurance/auto_kasko", []),
    ("https://www.vtb.ru/personal/zashchita-prav-potrebitelej/", []),
    ("https://www.sberbank.ru/ru/person/credits/auto", ["auto"]),
    ("https://www.vtb.ru/personal/avtokredit/", ["auto"]),
    ("https://www.sberbank.ru/ru/person/credits/cards", ["cards_credit"]),
    ("https://www.vtb.ru/personal/kredit/cards/", ["cards_credit"]),
    ("https://bank.ru/loans/career/", ["credits"]),
    ("https://bank.ru/credits/car-loans/", ["auto"])])
def test_classify_url_word_boundaries(url, want):
    from bank_audit.rag.url_discovery import classify_url
    assert sorted(classify_url(url)) == sorted(want)


def test_topic_from_title_when_url_has_none():
    from bank_audit.rag.topics import classify_document
    assert classify_document("https://www.banki.ru/news/lenta/?id=1",
                             "Сбербанк снизил ставки по семейной ипотеке", "") == (["mortgage"], "text")
    assert classify_document("https://www.cbr.ru/press/event/?id=1",
                             "Банк России повысил ключевую ставку", "")[0] == []
    assert classify_document("https://x.ru/vklady/", "Ипотека", "") == (["deposits"], "url")


def test_sponsored_only_off_official_sites():
    from bank_audit.rag.trust import detect_sponsored
    assert detect_sponsored("https://www.sberbank.ru/ru/person/promo/vklady")[0] is False
    assert detect_sponsored("https://www.vtb.ru/personal/partners/", "Реклама. erid: 1")[0] is False
    assert detect_sponsored("https://rbc.ru/promo/x/")[0] is True
    assert detect_sponsored("https://www.vtb.ru/x/", "на правах рекламы")[0] is True


def _row(doc, score, url, kind="aggregator", fetched=None, snippet="текст", title="Семейная ипотека в банках"):
    return {"document_id": doc, "score": score, "url": url, "title": title, "text_head": "head",
            "doc_type": "html", "trust_score": 0.7, "fetched_at": fetched or datetime(2026, 9, 1),
            "bank_slug": None, "bank_name": None, "source_kind": kind,
            "source_domain": url.split("/")[2], "snippet": snippet, "text": snippet,
            "idx": 0, "headings_path": "", "vec_rel": 0.5, "via_vec": True, "via_txt": True}


def test_rank_one_page_one_place_and_sources_weighted():
    """ДАН-04: три версии одной страницы banki.ru занимали три места, а сумма
    очков меню обгоняла тариф банка."""
    from bank_audit.rag.retriever import _group_rows, norm_url
    rows = [_row(1, .016, "https://www.banki.ru/x/semeynaya/?utm_source=a", fetched=datetime(2026, 8, 1)),
            _row(2, .015, "https://banki.ru/x/semeynaya/", fetched=datetime(2026, 9, 1)),
            _row(3, .010, "https://www.sravni.ru/menu/", snippet="м1"),
            _row(3, .010, "https://www.sravni.ru/menu/", snippet="м2"),
            _row(3, .010, "https://www.sravni.ru/menu/", snippet="м2"),
            _row(4, .016, "https://www.sberbank.ru/doc.pdf", kind="bank_official", title="Тарифы")]
    out, total = _group_rows(rows)
    assert total == 3
    first = out[0]
    assert first["document_id"] == 4                       # сайт банка выше агрегатора
    sem = next(g for g in out if "semeynaya" in g["url"])
    assert sem["versions"] == 1 and sem["document_id"] == 2  # свежая версия
    menu = next(g for g in out if g["document_id"] == 3)
    assert len(menu["hits"]) == 2                           # одинаковые сниппеты схлопнуты
    assert menu["score"] < first["score"]
    assert norm_url("https://www.a.ru/p/?utm_source=x&id=2#t") == "a.ru/p?id=2"


def test_coverage_failures_by_domain_and_untagged_parts():
    from bank_audit.web.app import _kb_failed_cells, _kb_untagged_parts
    cells, banks = _kb_failed_cells([
        {"url": "https://domrfbank.ru/mortgage/", "skipped_reason": "captcha", "n": 84,
         "last_at": datetime(2026, 9, 1)},
        {"url": "https://www.banki.ru/bank/psb/", "skipped_reason": "fetch_failed", "n": 3, "last_at": None},
        {"url": "https://www.vtb.ru/personal/", "skipped_reason": "duplicate", "n": 9, "last_at": None}])
    assert cells == [{"slug": "domrf", "topic": "mortgage", "n": 84, "reason": "captcha",
                      "last_at": datetime(2026, 9, 1)}]
    assert [b["slug"] for b in banks] == ["domrf"]
    assert _kb_untagged_parts([{"kind": "regulator", "n": 213}, {"kind": "legal_db", "n": 220},
                               {"kind": "press", "n": 182}, {"kind": "aggregator", "n": 900}]) == {
        "total": 1515, "legal": 433, "press": 182, "rest": 900}


def test_aggregator_menu_by_repetition_within_site():
    from bank_audit.rag import kb_backfill as K
    menu = "\n".join(f"- Ипотека пункт {i}" for i in range(8))
    chunks = [(i, f"banki.ru/p/{i}", menu) for i in range(25)]
    chunks.append((100, "banki.ru/p/100", "Необходимые документы:\n- Паспорт\n- СНИЛС\n- Справка о доходах\n- Заявление"))
    assert set(K.nav_chunks(chunks)) == set(range(25))
    # 20 версий ОДНОЙ страницы — не шаблон сайта
    offer = "\n".join(f"- Семейная ипотека условие {i}" for i in range(8))
    assert K.nav_chunks([(i, "banki.ru/products/hypothec/x", offer) for i in range(30)]) == []


def test_report_reads_go_to_archive_once(monkeypatch):
    """ДАН-02: прочитанное движком отчётов терялось — архив рос только из быстрых ответов."""
    from bank_audit.rag import fetcher, ingest_queue
    from bank_audit.research.gptr import runstate, scraper as SC
    html = ("<html><head><title>Тарифы</title></head><body>"
            + "<p>" + "Условия вклада и ставки по нему. " * 120 + "</p></body></html>").encode()
    monkeypatch.setattr(fetcher, "fetch", lambda *a, **k: types.SimpleNamespace(
        content=html, content_type="text/html", final_url="https://bank.ru/v"))
    sent = []
    monkeypatch.setattr(ingest_queue, "submit", lambda url, **kw: sent.append((url, kw)) or True)
    monkeypatch.setattr(ingest_queue, "is_running", lambda: True)
    # архивируем только то, что поиск найдёт: здесь «bank.ru» — известный источник
    monkeypatch.setattr(SC, "_archivable", lambda u: "//bank.ru/" in u)
    SC._archived_at.clear()
    st = runstate.RunState(origin={"kind": "report", "run_id": "r1"})
    text_, _l, _t = SC.AuditLensScraper("https://bank.ru/v", state=st).scrape()
    assert text_ and len(sent) == 1
    url, kw = sent[0]
    assert kw["origin"]["run_id"] == "r1" and kw["origin"]["fetch_mode"] == "http"
    assert kw["content"] == html
    SC.AuditLensScraper("https://bank.ru/v", state=st).scrape()
    assert len(sent) == 1                                    # повтор той же страницы
    monkeypatch.setattr(fetcher, "fetch", lambda *a, **k: types.SimpleNamespace(
        content=html, content_type="text/html", final_url="https://blog.example/v"))
    SC.AuditLensScraper("https://blog.example/v", state=st).scrape()
    assert len(sent) == 1                                    # неизвестный блог — не в архив
    monkeypatch.setattr(ingest_queue, "is_running", lambda: False)
    SC._archived_at.clear()
    SC.AuditLensScraper("https://bank.ru/w", state=runstate.RunState()).scrape()
    assert len(sent) == 1                                    # очередь не поднята — не пишем


def test_crawl_due_banks_and_stop():
    from bank_audit.rag.crawler import due_banks
    now = datetime(2026, 10, 4, 3, tzinfo=timezone.utc)
    last = {"sberbank": now - timedelta(days=2), "vtb": now - timedelta(days=3),
            "gazprombank": now - timedelta(days=7) + timedelta(hours=1)}
    assert due_banks(now, last, ["sberbank", "vtb", "alfabank", "gazprombank"], 7, 2) == [
        "sberbank", "alfabank", "gazprombank"]


def test_crawl_one_bank_rotates_records_origin_and_stops(monkeypatch):
    from bank_audit.rag import crawler as C
    from bank_audit.rag import ingest_queue

    class S:
        def execute(self, *a, **k):
            return types.SimpleNamespace(first=lambda: ({"deposits": ["u1", "u2", "u3"],
                                                         "credits": ["c1"]}, 7))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(C.db, "session", lambda: S())
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    read, origins = [], []
    monkeypatch.setattr(C.indexer, "ingest_document_from_url", lambda url, **k: (
        read.append(url), types.SimpleNamespace(document_id=1, chunks_added=2, doc_type="html",
                                                trust_score=.9, is_new=True, skipped_reason=None))[1])
    monkeypatch.setattr(ingest_queue, "record_origin", lambda *a: origins.append(a))
    r = C.crawl_one_bank("sberbank", rotation=2, record_kind="crawl")
    assert read == ["u3", "c1"] and r["urls_attempted"] == 2 and r["new"] == 2
    assert origins[0][3]["kind"] == "crawl" and origins[0][3]["run_id"].startswith("crawl:sberbank:")
    read.clear()
    assert C.crawl_one_bank("sberbank", should_stop=lambda: True)["urls_attempted"] == 0
    assert read == []
