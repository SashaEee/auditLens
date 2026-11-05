"""Сигналы вкладки на синтетической сети: кампания находится, фон — нет; авторы, кольца, тексты."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from bank_audit.vsp.models import Review, SourceBranch
from bank_audit.vsp.regions import subject_code, tile_grid
from bank_audit.vsp.registry.apply import import_registry
from bank_audit.vsp.registry.cbr import parse_row
from bank_audit.vsp.registry.enrich import enrich_branches
from bank_audit.vsp.signals import campaign as cp
from bank_audit.vsp.signals.run import latest_run, run
from bank_audit.vsp.storage import (Store, author_groups, authors, branch_stats, branches, episodes, network,
                                    source_branches)

UTC = timezone.utc
T0 = datetime(2024, 1, 1, tzinfo=UTC)
T_END = datetime(2026, 10, 1, tzinfo=UTC)


def _cbr(ref, code, addr):
    return parse_row({"FILIAL_SBJ_ID": ref, "FILIAL_SBJ_TYPE_CODE": "BNK_DOPO", "FILIAL_NAME": f"№ {code}",
                      "FILIAL_NUM": f"1481/1/{ref}", "FILIAL_ADDRESS": addr, "FILIAL_OPEN_DATE": "2010-05-01"})


def _net(st: Store, n_moscow=8, n_kld=3):
    rows = [_cbr(str(i), f"9038/0{1000 + i}", f"119048, Г. МОСКВА, УЛ. ТЕСТОВАЯ, Д. {i}") for i in range(n_moscow)]
    rows += [_cbr(str(100 + i), f"8626/0{1000 + i}", f"236000, КАЛИНИНГРАДСКАЯ ОБЛ., Г. КАЛИНИНГРАД, УЛ. МИРА, Д. {i}")
             for i in range(n_kld)]
    import_registry(st, rows)
    with st.engine.begin() as c:
        ids = dict(c.execute(select(branches.c.registry_ref, branches.c.branch_id)).all())
    for ref, bid in ids.items():
        lat = 55.7 + int(ref) * 0.001 if int(ref) < 100 else 54.7 + int(ref) * 0.0001
        st.upsert_source_branch(SourceBranch("2gis", f"g{ref}", address="x", city="x", lat=lat, lon=37.6))
        with st.engine.begin() as c:
            c.execute(update(source_branches).where(source_branches.c.source_branch_id == f"g{ref}")
                      .values(branch_id=bid))
            c.execute(update(branches).where(branches.c.branch_id == bid).values(lat=lat, lon=37.6))
    return ids


def _rev(sid, n, rating, at, author, *, total=5, confirmed=False, text="обычный отзыв про обслуживание в офисе"):
    return Review(source="2gis", source_review_id=f"{sid}-{n}", source_branch_id=sid, rating=rating,
                  review_text=text, review_date=at, author_ref=author,
                  raw={"provider": "2gis", "author_reviews_count": total,
                       "confirmed_by": ["sbol"] if confirmed else None})


def _background(st: Store, ids, rnd: random.Random):
    """Фон: у каждого ВСП ~2 отзыва в неделю, оценки 1–5, часть подтверждена; без кампаний."""
    k = 0
    items = []
    for ref in ids:
        t = T0
        while t < T_END:
            t += timedelta(hours=rnd.randint(40, 130))
            r = rnd.choice([5, 5, 5, 4, 4, 3, 2, 1])
            items.append(_rev(f"g{ref}", k, r, t, f"bg{ref}-{k}", total=rnd.randint(2, 40),
                              confirmed=r == 5 and rnd.random() < 0.4,
                              text=" ".join(rnd.choice(["офис", "очередь", "кассир", "долго", "быстро", "вклад",
                                                        "карта", "спасибо", "плохо", "нормально", "менеджер",
                                                        "консультация", "банкомат"]) for _ in range(8))))
            k += 1
    st.save_reviews(items)


def _campaign(st: Store, ref: str, start: datetime, n=60):
    items = []
    t = start
    for i in range(n):
        t += timedelta(minutes=4 if i % 3 else 300)
        items.append(_rev(f"g{ref}", f"c{i}", 5, t, f"camp-{ref}-{i}", total=1,
                          text="все отлично спасибо менеджеру анне за помощь и быструю консультацию"))
    st.save_reviews(items)


def test_regions_normalize_and_tile_grid():
    assert subject_code("Воронежская Обл.") == subject_code("Воронеж") == "VOR"
    assert subject_code("Республика Татарстан (Татарстан)") == "TA"
    assert subject_code("Томская Область") == "TOM" and subject_code("Омская Обл.") == "OMS"
    assert subject_code("Ставроп. Край") == "STA" and subject_code("Алтайский Край") == "ALT"
    assert subject_code("Республика Алтай") == "AL" and subject_code("Ямало-Ненецкий Автономный Округ") == "YAN"
    assert subject_code("Нижний Новгород") == "NIZ" and subject_code("Новгородская Обл.") == "NGR"
    assert subject_code("Донецкая Народная Республика") == "DN" and subject_code("Неведомо") is None
    g = tile_grid({"MOW": (55.75, 37.6), "MOS": (55.8, 37.9), "SPE": (59.9, 30.3), "KHA": (48.5, 135.1)})
    assert len(set(g.values())) == 4 and g["KHA"][0] > g["MOW"][0] and g["SPE"][1] < g["MOW"][1]


def test_enrich_subject_by_gosb_and_poisson():
    assert cp.poisson_sf_log10(60, 2.0) < -40 and cp.poisson_sf_log10(2, 2.0) == 0.0
    assert -1.5 < cp.poisson_sf_log10(5, 2.0) < -1.0                     # P(X≥5|2) ≈ 0.053


def test_signals_end_to_end(db_url):
    st = Store(db_url)
    ids = _net(st)
    res = enrich_branches(st)
    assert res["не определён"] == 0
    with st.engine.connect() as c:
        subj = dict(c.execute(select(branches.c.registry_ref, branches.c.subject)).all())
        gosb = dict(c.execute(select(branches.c.registry_ref, branches.c.gosb)).all())
    assert subj["0"] == "MOW" and subj["100"] == "KGD" and gosb["0"] == "9038"
    rnd = random.Random(7)
    _background(st, ids, rnd)
    _campaign(st, "3", datetime(2026, 3, 2, 10, tzinfo=UTC))
    # хвалебщик сети: один автор, пятёрки трём ВСП за неделю, одинаковым текстом
    st.save_reviews([_rev(f"g{r}", f"p{r}", 5, datetime(2026, 5, 1 + i, 12, tzinfo=UTC), "praiser", total=3,
                          text="лучший офис города всем советую сотрудники вежливые и внимательные")
                     for i, r in enumerate(("0", "1", "2"))])
    # кольцо: три автора, одни и те же четыре ВСП Калининграда, пятёрки в пределах месяца
    st.save_reviews([_rev(f"g{r}", f"ring{a}{r}", 5, datetime(2025, 6, 2 + i + a, 12, tzinfo=UTC), f"ring{a}",
                          total=20, text=f"хорошо {a} {r}")
                     for a in range(3) for i, r in enumerate(("100", "101", "102", "5"))])
    # возможные нарушения: один сотрудник в жалобах трёх разных клиентов, плюс оформление без согласия
    bad = ["Менеджер Ольга навязала страховку к кредиту, отказаться не дала",
           "Менеджер Ольга навязала подписку, сказала без неё не одобрят",
           "Оформили кредитную карту без моего согласия, менеджер Ольга даже не объяснила условия"]
    st.save_reviews([_rev("g6", f"bad{i}", 1, datetime(2026, 2, 3 + 9 * i, 12, tzinfo=UTC), f"badauth{i}", text=t)
                     for i, t in enumerate(bad)])
    out = run(st)
    rid = latest_run(st)
    assert rid == out["run_id"] and out["episodes_branch"] >= 1
    with st.engine.connect() as c:
        eps = list(c.execute(select(episodes).where(episodes.c.run_id == rid, episodes.c.level == "branch")))
        bs = {r.point: (r.score, json.loads(r.detail_json)) for r in c.execute(
            select(branch_stats).where(branch_stats.c.run_id == rid))}
        au = {r.author_ref: r.kind for r in c.execute(select(authors).where(authors.c.run_id == rid))}
        groups = [(r.kind, r.n_authors, r.n_points) for r in c.execute(
            select(author_groups).where(author_groups.c.run_id == rid))]
        net = {r.key: json.loads(r.value_json) for r in c.execute(select(network).where(network.c.run_id == rid))}
    camp_point = f"b{ids['3']}"
    assert {e.point for e in eps} == {camp_point}, [(e.point, e.n, e.ratio) for e in eps]
    e = eps[0]
    f = json.loads(e.features_json)
    assert e.n >= 50 and e.strength in ("сильный", "средний") and f["oneoff_share"] >= 0.9
    assert f["series_share"] > 0.5 and f["template_share"] > 0.9
    assert {w["key"] for w in f["why"]} >= {"ratio", "oneoff_share", "series_share"}
    score, d = bs[camp_point]
    assert "массовая волна пятёрок от новых аккаунтов" in d["reasons"] and d["campaign_score"] >= 40
    assert d["rating_wo_episodes"] < d["rating_all"]
    bad_point = f"b{ids['6']}"
    score6, d6 = bs[bad_point]
    assert score6 >= 40 and d6["severe_24m"] == 1 and d6["serious_24m"] == 2, (score6, d6["reasons"])
    assert any("«Ольга»" in r and "3 разных клиентов" in r for r in d6["reasons"])
    assert max(s for p, (s, _) in bs.items() if p != bad_point) < 40       # оценки сами по себе — не повод
    assert out["cases_by_type"]["mis_selling"] == 2 and out["persons"]["complaints"] >= 1
    assert au["praiser"] == "хвалебщик сети"
    assert ("ring", 3, 4) in groups and any(k == "same_text" and n_p == 3 for k, _, n_p in groups)
    assert net["calendar"] and net["subjects"]["MOW"]["points"] == 8 and "tile_grid" in net
    assert out["rho_score_vs_volume"] is None or out["rho_score_vs_volume"] < 0.5
    # повторный прогон: хранится не больше трёх
    for _ in range(3):
        run(st)
    with st.engine.connect() as c:
        assert len({r[0] for r in c.execute(select(episodes.c.run_id))}) <= 3
