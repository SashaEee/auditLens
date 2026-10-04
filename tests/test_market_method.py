"""Методика ранга вкладки «Рынок» (аудит 03.10, волна 2): ранг по главной
группе, ПСК ниже ставки, окна срока вкладов, акции РКО, «до N%», вырожденная
метрика, господдержка и сторож, знаки разрыва. БД не нужна — строки витрины
подставляются вместо запроса."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.web import app as A  # noqa: E402

_N = [0]


def row(cat, bank, metric_val=None, *, sber=False, title="Продукт", seg=None, sub=None,
        term="any", rate=None, psk=None, rate_min=None, fee=None, grace=None,
        kind="effective", bad=None, free=None):
    _N[0] += 1
    r = {"category": cat, "bank_slug": bank.lower(), "bank_name": bank, "is_sber": sber,
         "offer_id": _N[0], "title": title, "rate_pct": rate, "rate_kind": kind,
         "term_bucket": term, "segment": seg, "sub_segment": sub,
         "rate_min": rate_min, "rate_max": None, "psk_min": psk, "psk_max": None,
         "fee_service": fee, "grace_days": grace, "cashback_pct": None,
         "free_kind": free, "attain": None, "free_conditions": None, "rate_requires": None,
         "implausible_reason": bad}
    if metric_val is not None and cat in ("deposit", "savings_account", "mortgage"):
        r["rate_pct"] = metric_val
    return r


@pytest.fixture
def market(monkeypatch):
    """Подставляет строки витрины; фильтр срока — как в SQL атласа."""
    def setup(rows, key_rate=14.0):
        def fake_q(sql, params=None):
            p = params or {}
            return [dict(r) for r in rows if not p.get("tb") or r["term_bucket"] == p["tb"]]
        monkeypatch.setattr(A, "q", fake_q)
        monkeypatch.setattr(A, "scalar", lambda *a, **k: None)
        from bank_audit.digest import news
        monkeypatch.setattr(news, "fetch_key_rate", lambda *a, **k: {"current": key_rate})
    return setup


def cat_of(atlas, cid):
    return next(c for c in atlas["categories"] if c["category"] == cid)


def test_credit_head_rank_is_the_main_group_not_a_mix_of_kinds(market):
    """РЫН-07: «кредиты #20 из 93, 79-й перцентиль» давал кредит под залог."""
    rows = []
    for i in range(8):                                   # наличными: Сбер в хвосте
        rows.append(row("credit", f"Банк{i}", rate=18 + i, psk=18 + i, sub="cash"))
    rows.append(row("credit", "Сбербанк", rate=27, psk=27, sber=True, sub="cash",
                    title="Наличными"))
    for i in range(5):                                   # под залог: Сбер лучший
        rows.append(row("credit", f"Залог{i}", rate=20 + i, psk=20 + i, sub="pledge"))
    rows.append(row("credit", "Сбербанк", rate=16, psk=16.34, sber=True, sub="pledge",
                    title="Под залог недвижимости"))
    market(rows)
    c = cat_of(A.market_atlas(), "credit")
    assert c["main_group"]["label"] == "наличными"
    assert c["sber"]["title"] == "Наличными" and c["sber"]["rank"] == 9
    assert c["n_banks"] == 9
    assert c["overall"]["rank"] == 1 and c["overall"]["n_banks"] == 14  # слияние — справкой
    assert [g["main"] for g in c["comparable"]] == [True, False]
    assert c["banks_dropped"] == 0            # банки другого вида не «выбыли»


def test_unrecognised_kind_is_labelled_other_when_kinds_exist(market):
    rows = [row("credit", f"Б{i}", rate=20, psk=20 + i) for i in range(6)]
    rows += [row("credit", f"Н{i}", rate=19, psk=19 + i, sub="cash") for i in range(3)]
    rows.append(row("credit", "Сбербанк", rate=21, psk=21, sber=True))
    market(rows)
    c = cat_of(A.market_atlas(), "credit")
    assert c["main_group"]["label"] == "прочие"
    labels = {g["label"] for g in c["groups"]}
    assert labels == {"прочие", "наличными"}


def test_psk_below_own_rate_is_compared_by_rate(market):
    """РЫН-03: ПСК 11,995 при ставке 24 делала банк третьим по рынку."""
    rows = [row("credit", "Нокс", rate=24, rate_min=24, psk=11.995),
            row("credit", "Сбербанк", rate=17.2, rate_min=17.2, psk=16.34, sber=True)]
    rows += [row("credit", f"Б{i}", rate=15 + i, rate_min=15 + i, psk=15.5 + i) for i in range(5)]
    market(rows)
    c = cat_of(A.market_atlas(), "credit")
    nox = next(p for p in c["points"] if p["name"] == "Нокс")
    assert nox["rate"] == 24 and nox["psk_mismatch"] is True
    # порог 0,3 п.п. одинаков для всех: у Сбера ПСК 16,34 при ставке 17,2 —
    # он тоже сравнивается по ставке
    assert c["psk_mismatch"] == 2
    assert c["sber"]["rate"] == 17.2 and c["sber"]["psk_mismatch"] is True
    assert c["leader"]["name"] == "Б0"


def test_missing_psk_falls_back_to_rate_and_is_counted(market):
    rows = [row("auto_loan", f"Б{i}", rate=20 + i, psk=21 + i) for i in range(5)]
    rows.append(row("auto_loan", "Сбербанк", rate=19, sber=True))
    market(rows)
    c = cat_of(A.market_atlas(), "auto_loan")
    assert c["psk_fallback"] == 1 and c["sber"]["rate"] == 19 and c["sber"]["rank"] == 1


def test_deposit_rank_by_term_and_guard_border(market):
    """РЫН-04: «Сбер #1 из 133» держался на 3-месячном промо 19%."""
    rows = [row("deposit", "Сбербанк", 19.0, sber=True, term="0-3", title="Выгодный старт +"),
            row("deposit", "Сбербанк", 11.0, sber=True, term="13+", title="СберВклад")]
    rows += [row("deposit", f"Б{i}", 15 - i * 0.5, term="0-3") for i in range(6)]
    rows += [row("deposit", f"Б{i}", 13 - i * 0.3, term="13+") for i in range(6)]
    market(rows)
    atlas = A.market_atlas()
    c = cat_of(atlas, "deposit")
    assert c["sber"]["rank"] == 1 and c["sber"]["near_guard"] is True
    bt = {t["term"]: t for t in c["by_term"]}
    assert bt["0-3"]["rank"] == 1 and bt["13+"]["rank"] == 7 and bt["13+"]["n_banks"] == 7
    v = A.market_verdict()
    assert any("зависит от срока" in d and "от года — 7 из 7" in d for d in v["doubts"])
    assert any("у самой границы проверки правдоподобия" in d for d in v["doubts"])
    # фильтр срока — ранг по своему окну
    c13 = cat_of(A.market_atlas(term="13+"), "deposit")
    assert c13["sber"]["rank"] == 7 and c13["sber"]["title"] == "СберВклад"


def test_credit_has_no_term_windows(market):
    """У кредита окно срока — по минимальному сроку: «до 3 мес» = кредит на 3–60 мес."""
    rows = [row("credit", f"Б{i}", rate=18 + i, psk=18 + i, sub="cash",
                term="0-3" if i % 2 else "13+") for i in range(8)]
    rows.append(row("credit", "Сбербанк", rate=20, psk=20, sber=True, sub="cash", term="0-3"))
    market(rows)
    assert "by_term" not in cat_of(A.market_atlas(), "credit")


def test_upper_bound_rate_is_not_ranked(market):
    rows = [row("deposit", "ПСБ", 30.0, kind="max", title="Народный")]
    rows += [row("deposit", f"Б{i}", 12 + i * 0.1) for i in range(5)]
    rows.append(row("deposit", "Сбербанк", 12.25, sber=True))
    market(rows)
    c = cat_of(A.market_atlas(), "deposit")
    assert c["upper_bound_excluded"] == 1 and c["leader"]["name"] != "ПСБ"


def test_rko_trial_tariffs_are_promo_not_price(market):
    """РЫН-08: «Взлетай! (первые 3 мес.)» стоял в ранге наравне с постоянными нулями."""
    rows = [row("rko", "Сбербанк", fee=0, sber=True, sub="ip", title="Модуль РКО"),
            row("rko", "А", fee=0, sub="ip", title="Взлетай! (первые 3 мес.)"),
            row("rko", "Б", fee=0, sub="ip", title="Свежий старт (на 3 мес.)"),
            row("rko", "В", fee=0, sub="ip", title="Стартовый (для ИП первые 6 мес.)")]
    rows += [row("rko", f"Г{i}", fee=490 + i * 100, sub="ip", title="Базовый") for i in range(6)]
    market(rows)
    c = cat_of(A.market_atlas(), "rko")
    assert c["promo_period_excluded"] == 3
    assert c["at_best"] == 1 and c["sber"]["tied"] == 1 and not c["degenerate"]


def test_degenerate_metric_and_competition_rank(market):
    rows = [row("card_debit", f"Б{i}", fee=0, free="unconditional") for i in range(8)]
    rows += [row("card_debit", f"П{i}", fee=990 + i) for i in range(3)]
    rows.append(row("card_debit", "Сбербанк", fee=0, sber=True, free="unconditional"))
    market(rows)
    c = cat_of(A.market_atlas(), "card_debit")
    assert c["degenerate"] and c["at_best"] == 9
    assert c["sber"]["rank"] == 1 and c["sber"]["tied"] == 9
    v = A.market_verdict()
    assert "card_debit" not in v["weak"]                 # вырожденная — не в выводах
    assert any("ранг не показываем" in d for d in v["doubts"])


def test_subsidized_and_implausible_are_out_of_the_rank(market):
    rows = [row("mortgage", "Сбербанк", 17.7, sber=True, sub="new"),
            row("mortgage", "Госбанк", 6.0, sub="new", title="Семейная ипотека"),
            row("mortgage", "Ошибка", 0.1, sub="new", bad="implausible_value")]
    rows += [row("mortgage", f"Б{i}", 16 + i, sub="new") for i in range(5)]
    market(rows)
    c = cat_of(A.market_atlas(), "mortgage")
    assert c["subsidized_excluded"] == 2 and c["leader"]["rate"] == 16
    # 0,1% ниже ключевой − 3 п.п. — числовой страж господдержки срабатывает раньше сторожа
    assert c["implausible_excluded"] == 0


def test_verdict_gap_sign_and_units(market):
    rows = [row("credit", f"Б{i}", rate=15 + i, psk=15 + i, sub="cash") for i in range(6)]
    rows.append(row("credit", "Сбербанк", rate=25, psk=25, sber=True, sub="cash"))
    rows += [row("rko", f"Р{i}", fee=500 + 100 * i, sub="ip") for i in range(5)]
    rows.append(row("rko", "Сбербанк", fee=1490, sber=True, sub="ip"))
    market(rows)
    v = A.market_verdict()
    cr = next(c for c in v["cells"] if c["category"] == "credit")
    assert cr["gap_median"] > 0 and cr["gap_unit"] == " п.п." and cr["group_label"] == "наличными"
    assert "кредиты (наличными): 25%" in v["lead"] and "хуже на" in v["lead"]
    rko = next(c for c in v["cells"] if c["category"] == "rko")
    assert rko["gap_unit"].strip() == "₽/мес"


def test_digest_market_rows_use_atlas_and_term_spread(market):
    """РЫН-02/ОБЗ-08: выпуск считал «Сбер против рынка» по старой вьюхе и
    спред вклада — по 3-месячному промо."""
    from bank_audit.digest import aggregator as AG
    rows = [row("deposit", "Сбербанк", 19.0, sber=True, term="0-3"),
            row("deposit", "Сбербанк", 12.0, sber=True, term="7-12")]
    rows += [row("deposit", f"Б{i}", 13 - i * 0.2, term="7-12") for i in range(6)]
    rows += [row("deposit", f"Б{i}", 16 - i * 0.2, term="0-3") for i in range(6)]
    rows.append(row("deposit", "ПСБ", 30.0, bad="implausible_value"))
    market(rows)
    rs = AG.market_position_rows()
    dep = next(r for r in rs if r["category"] == "deposit")
    assert dep["market_max"] < 30                       # выброс шкалу не тянет
    assert dep["sber_max"] == 19.0 and dep["rank"] == 1
    sp = AG.deposit_spread(rs, 14.0)
    assert sp["dep_spread_term"] == "7–12 мес" and sp["dep_spread_pp"] == -2.0
    assert sp["dep_spread_market_pp"] == pytest.approx(12.4 - 14, abs=0.01)   # медиана 7 банков


# ── фронт: единицы и знаки форматтеров «Рынка» ───────────────────────────────

_JSX = Path(__file__).resolve().parents[1] / "src/bank_audit/web/static/app.jsx"


def _js_consts(names):
    src = _JSX.read_text(encoding="utf-8")
    out = []
    for n in names:
        m = re.search(rf"^const {n}\s*=.*?;\n(?=const |// |function |\n)", src, re.S | re.M)
        assert m, n
        out.append(m.group(0))
    return "\n".join(out)


@pytest.mark.skipif(not shutil.which("node"), reason="node не установлен")
def test_front_market_formatters():
    code = _js_consts(["pct", "fmtNum", "mkFeeU", "mkGap", "mkPskBad", "mkVal", "mkMetric"])
    probe = code + """
console.log(JSON.stringify([
  mkMetric(1490,"fee_service"," ₽/мес"), mkMetric(0,"fee_service"," ₽/год"),
  mkMetric(120,"grace_days"," дн"), mkMetric(16.34,"psk_min","%"),
  mkGap(2.5,"psk_min","%"), mkGap(-1,"rate_pct","%"), mkGap(0,"fee_service"," ₽/мес"),
  mkGap(-300,"fee_service"," ₽/мес"),
  mkVal({psk_min:11.995,rate_min:24,rate_pct:24},"psk_min"),
  mkVal({psk_min:16.5,rate_min:16.4},"psk_min"), mkVal({rate_pct:19},"psk_min")
]));"""
    res = subprocess.run(["node", "-e", probe], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    got = json.loads(res.stdout.strip().splitlines()[-1])
    assert got[0].endswith("₽/мес") and got[1] == "бесплатно"
    assert got[2] == "120 дн" and got[3] == "16,34%"
    assert got[4] == "+2,50 п.п." and got[5] == "−1,00 п.п." and got[6] == "наравне"
    assert got[7].startswith("−300") and got[7].endswith("₽/мес")
    assert got[8:] == [24, 16.5, 19]
