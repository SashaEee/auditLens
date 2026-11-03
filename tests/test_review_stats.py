"""Статистика сигналов жалоб (rag/review_stats.py): хвосты, сравнение с рынком,
изменение к прошлому окну, BH, эпизоды и нулевая модель.

Всё — чистые функции над числами: БД и сеть не нужны. Точность хвостов
сверяется с перебором и формулами на малых числах, калибровка — A/A на
синтетике с фиксированным зерном (результат детерминирован).
"""
from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pytest

from bank_audit.rag import review_stats as RS


# ── вспомогательное: перебор ────────────────────────────────────────────────

def _binom_sf(x: int, n: int, p: float) -> float:
    """P(Binom(n, p) ≥ x) суммой."""
    return math.fsum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(x, n + 1))


def _nb_pmf(j: int, r: float, p: float) -> float:
    return math.exp(math.lgamma(j + r) - math.lgamma(r) - math.lgamma(j + 1)
                    + r * math.log(p) + j * math.log1p(-p))


def _nb_sf_sum(x: int, r: float, p: float) -> float:
    """P(X ≥ x) суммой верхнего хвоста (без вычитания из 1)."""
    tot, j = 0.0, x
    while True:
        v = _nb_pmf(j, r, p)
        tot += v
        if v < 1e-22 * max(tot, 1e-300) and j > x + 50:
            return tot
        j += 1


def _pois_sf(x: int, lam: float) -> float:
    return 1.0 - math.fsum(math.exp(i * math.log(lam) - lam - math.lgamma(i + 1))
                           for i in range(x))


def _hyper_pmf(x: int, n1: int, n2: int, m1: int, psi: float = 1.0) -> list[tuple[int, float]]:
    """Нецентральное гипергеометрическое перебором: [(x, P(X = x))]."""
    lo, hi = max(0, m1 - n2), min(n1, m1)
    w = [(j, math.comb(n1, j) * math.comb(n2, m1 - j) * psi ** j) for j in range(lo, hi + 1)]
    tot = math.fsum(v for _, v in w)
    return [(j, v / tot) for j, v in w]


# ── 1. Неполная бета и отрицательно-биномиальный хвост ──────────────────────

@pytest.mark.parametrize("a,b,x", [(1, 1, 0.3), (3, 5, 0.2), (7, 2, 0.9), (12, 30, 0.25),
                                   (40, 3, 0.95), (5, 60, 0.01)])
def test_betainc_matches_binomial_sum(a, b, x):
    """I_x(a, b) при целых a, b = P(Binom(a + b − 1, x) ≥ a)."""
    i, c = RS.betainc2(a, b, x)
    ref = _binom_sf(a, a + b - 1, x)
    assert i == pytest.approx(ref, rel=1e-10, abs=1e-300)
    assert i + c == pytest.approx(1.0, abs=1e-12)


def test_betainc_tiny_tail_keeps_precision():
    """Дальний хвост считается напрямую: 1 − CDF дал бы 0 или шум."""
    i, _ = RS.betainc2(30, 5, 0.01)
    assert i == pytest.approx(_binom_sf(30, 34, 0.01), rel=1e-9)
    assert 0 < i < 1e-40
    # дополнение — тоже точно: 1 − I_0,99(5, 30) = P(Binom(34; 0,99) ≤ 4)
    _, c = RS.betainc2(5, 30, 0.99)
    ref = math.fsum(math.comb(34, j) * 0.99 ** j * 0.01 ** (34 - j) for j in range(5))
    assert c == pytest.approx(ref, rel=1e-9) and c < 1e-40


@pytest.mark.parametrize("r,p", [(0.5, 0.875), (3.5, 0.875), (12.25, 0.6), (1.7, 0.3),
                                 (40.0, 0.95)])
def test_nb_sf_matches_bruteforce(r, p):
    for x in range(0, 30, 3):
        ref = 1.0 if x == 0 else _nb_sf_sum(x, r, p)
        assert RS.nb_sf(x, r, p) == pytest.approx(ref, rel=1e-9, abs=1e-300)


def test_nb_sf_geometric_closed_form():
    """r = 1 — геометрическое: P(X ≥ x) = (1 − p)^x."""
    for p in (0.1, 0.5, 0.9):
        for x in (1, 4, 17):
            assert RS.nb_sf(x, 1.0, p) == pytest.approx((1 - p) ** x, rel=1e-12)


def test_gammainc_and_chi2_quantile():
    # P(1, x) = 1 − e^−x; медиана χ²_2 = 2·ln 2
    assert RS.gammainc_lower(1.0, 0.7) == pytest.approx(1 - math.exp(-0.7), rel=1e-12)
    # при целом a: P(a, x) = P(Пуассон(x) ≥ a); оба плеча — ряд и цепная дробь
    assert RS.gammainc_lower(30.0, 50.0) == pytest.approx(_pois_sf(30, 50.0), rel=1e-9)
    tiny = math.fsum(math.exp(i * math.log(10.0) - 10.0 - math.lgamma(i + 1))
                     for i in range(30, 200))
    assert RS.gammainc_lower(30.0, 10.0) == pytest.approx(tiny, rel=1e-9)
    assert RS._chi2_ppf(0.5, 2.0) == pytest.approx(2 * math.log(2), rel=1e-9)
    assert RS._chi2_ppf(0.95, 6.0) == pytest.approx(12.5916, rel=1e-4)


# ── 2. Предсказательный хвост ───────────────────────────────────────────────

@pytest.mark.parametrize("base,x,k", [([0] * 7, 2, 1), ([1, 0, 2, 1, 0, 1, 1], 5, 1),
                                      ([3, 4, 3, 3, 4, 3, 4], 12, 1), ([5] * 7, 22, 2)])
def test_predictive_is_poisson_gamma_with_jeffreys_prior(base, x, k):
    """При φ = 1 хвост — ровно смесь Пуассона по апостериорной Gamma(S + ½, n):
    сверка численным интегрированием и с NB(S + ½, n/(n + k))."""
    s, n = sum(base), len(base)
    shape, rate = s + 0.5, float(n)
    # интегрирование по λ (Симпсон, плотная сетка до далёкого хвоста гаммы)
    hi = (shape + 12 * math.sqrt(shape) + 20) / rate
    m = 20000
    hstep = hi / m
    tot = 0.0
    for i in range(m + 1):
        lam = max(i * hstep, 1e-12)
        dens = math.exp(shape * math.log(rate) + (shape - 1) * math.log(lam) - rate * lam
                        - math.lgamma(shape))
        coef = 1 if i in (0, m) else (4 if i % 2 else 2)
        tot += coef * dens * _pois_sf(x, k * lam)
    integral = tot * hstep / 3
    got = RS.predictive_tail(x, base, phi=1.0, k=k)
    assert got == pytest.approx(integral, rel=2e-3)
    assert got == pytest.approx(RS.nb_sf(x, s + 0.5, n / (n + k)), rel=1e-12)


def test_long_base_converges_to_poisson():
    """База в тысячи недель — норма известна, хвост пуассоновский."""
    base = [3] * 3000
    for x in (5, 8, 12):
        assert RS.predictive_tail(x, base, phi=1.0) == pytest.approx(_pois_sf(x, 3.0), rel=0.01)


def test_norm_error_is_not_ignored_on_small_norms():
    """Прежняя подстановка нормы как точной занижала p в 2–4 раза: 1 жалоба в
    неделю, пришло 5 — Пуассон 0,0037, предсказательный хвост 0,0092."""
    base = [1] * 7
    plug_in = _pois_sf(5, 1.0)
    assert plug_in == pytest.approx(0.00366, abs=1e-5)
    assert RS.predictive_tail(5, base) == pytest.approx(0.00916, abs=1e-4)
    for b, x in (([2] * 7, 8), ([5] * 7, 13), ([3, 4, 3, 3, 4, 3, 4], 12)):
        ratio = RS.predictive_tail(x, b) / _pois_sf(x, sum(b) / 7)
        assert 2.0 <= ratio <= 4.0


def test_tail_monotone():
    base = [4, 6, 2, 0, 5, 5, 2]
    tails = [RS.predictive_tail(x, base) for x in range(0, 40)]
    assert all(a >= b for a, b in zip(tails, tails[1:]))           # по x — не растёт
    assert tails[0] == 1.0
    # выше норма — менее необычно
    assert RS.predictive_tail(12, [5] * 7) > RS.predictive_tail(12, [3] * 7)
    # больше перерассеяние — тяжелее хвост (x выше среднего)
    by_phi = [RS.predictive_tail(15, [5] * 7, phi=f) for f in (1.0, 1.5, 2.0, 4.0)]
    assert all(a < b for a, b in zip(by_phi, by_phi[1:]))
    by_prior = [RS.predictive_tail(15, [5] * 7, phi_prior=(f, 30)) for f in (1.0, 2.0, 3.0)]
    assert all(a < b for a, b in zip(by_prior, by_prior[1:]))
    # длиннее база — уже предсказание
    assert RS.predictive_tail(10, [3] * 28, phi=1.0) < RS.predictive_tail(10, [3] * 7, phi=1.0)


def test_estimated_phi_widens_tail_vs_known():
    """Оценённый по 7 неделям φ неточен — хвост тяжелее, чем при том же φ,
    известном точно (как t Стьюдента тяжелее нормального)."""
    base = [3, 9, 1, 12, 2, 8, 4]
    phi_hat = RS.overdispersion(base)
    assert phi_hat > 2
    assert RS.predictive_tail(25, base) > RS.predictive_tail(25, base, phi=phi_hat)


def test_two_weeks_tail_accounts_for_waves():
    """Две недели подряд: волны длиннее недели расширяют хвост суммы."""
    flat = [3, 4, 3, 3, 4, 3, 4]
    wavy = [0, 8, 1, 7, 0, 9, 0]
    assert RS.predictive_tail(7, flat, k=2) > 0.3
    assert RS.predictive_tail(22, flat, k=2) < 0.001
    assert RS.predictive_tail(22, wavy, k=2) > RS.predictive_tail(22, flat, k=2)


def test_weight_recent():
    """Свежие недели весомее: норма ближе к ним, а эффективная база короче."""
    rising = [6, 5, 4, 3, 2, 2, 1]                     # [0] — самая свежая
    eq = RS.predictive(rising, phi=1.0)
    wr = RS.predictive(rising, 4.0, phi=1.0)
    assert wr["mean"] > eq["mean"] and wr["n_eff"] < eq["n_eff"] == 7
    # на ровной базе веса только расширяют предсказание
    assert RS.predictive_tail(12, [5] * 7, 4.0, phi=1.0) > RS.predictive_tail(12, [5] * 7,
                                                                              phi=1.0)


def test_degenerate_inputs():
    assert RS.predictive_tail(0, [1, 2]) == 1.0
    assert RS.predictive_tail(5, []) == 1.0
    assert RS.predictive([], phi=1.0) is None
    # нулевая база: необычно уже 2, но не 1
    assert RS.predictive_tail(1, [0] * 7) > 0.05
    assert RS.predictive_tail(2, [0] * 7) < 0.05
    with pytest.raises(ValueError):
        RS.predictive_tail(3, [1, 2, 3], -1.0)


# ── 3. Перерассеяние ────────────────────────────────────────────────────────

def test_overdispersion():
    assert RS.overdispersion([3] * 7) == 1.0               # меньше пуассоновского — 1
    assert RS.overdispersion([]) == 1.0 and RS.overdispersion([5]) == 1.0
    assert RS.overdispersion([0, 0, 0]) == 1.0
    wavy = [0, 8, 1, 7, 0, 9, 0]
    m = sum(wavy) / 7
    assert RS.overdispersion(wavy) == pytest.approx(
        sum((w - m) ** 2 for w in wavy) / (6 * m))


def test_pooled_overdispersion_robust_to_one_incident():
    """Инцидент в базе одной проблемы не раздувает общий φ для всех."""
    rng = np.random.default_rng(3)
    norms = np.array(RS.DEFAULT_NORMS)
    sh = norms / 0.5                                       # φ = 1,5
    series = [row.tolist() for row in rng.poisson(rng.gamma(sh[:, None], 0.5, (len(norms), 7)))]
    clean, clean_sum = RS.pooled_overdispersion(series), RS.pooled_overdispersion(series,
                                                                                robust=False)
    assert clean > 1.2 and clean_sum > 1.2                 # волны видны обоими способами
    series[-1][3] += 40
    assert RS.pooled_overdispersion(series) == pytest.approx(clean, abs=0.1)
    assert RS.pooled_overdispersion(series, robust=False) > clean_sum + 0.25
    pr = RS.phi_prior(series)
    assert pr is not None and pr[1] == 30
    assert RS.phi_prior([[0, 0, 0], [0, 1, 0]]) is None       # не из чего


# ── 4. A/A: на синтетике без всплесков «значимых» не больше α ───────────────

def _slices(rng, phi: float, reps: int) -> np.ndarray:
    """reps срезов по DEFAULT_NORMS × 9 недель; NB с разбросом φ·среднее."""
    norms = np.array(RS.DEFAULT_NORMS)
    if phi <= 1:
        return rng.poisson(norms[None, :, None], size=(reps, len(norms), 9))
    r = norms / (phi - 1)
    lam = rng.gamma(r[None, :, None], (norms / r)[None, :, None], size=(reps, len(norms), 9))
    return rng.poisson(lam)


@pytest.mark.parametrize("phi,seed", [(1.0, 11), (2.0, 12)])
def test_aa_week_tail_within_alpha(phi, seed):
    """Неделя против 7 недель базы: с приором φ среза доля p ≤ 0,05 не выше 5%
    и на пуассоновских неделях, и на волнах (φ = 2). Неделя 1 — промежуток,
    как в reviews_dash: база — недели 2..8."""
    X = _slices(np.random.default_rng(seed), phi, 120)
    hits = hits2 = n = 0
    for sl in X:
        bases = [row[2:9].tolist() for row in sl]
        prior = RS.phi_prior(bases)
        for row, base in zip(sl, bases):
            hits += RS.predictive_tail(int(row[0]), base, phi_prior=prior) <= 0.05
            hits2 += RS.predictive_tail(int(row[0] + row[1]), base, k=2, phi_prior=prior) <= 0.05
            n += 1
    assert hits / n <= 0.05
    assert hits2 / n <= 0.05


def test_aa_own_phi_on_poisson():
    """Без приора (φ — по самой базе) на пуассоновских неделях — тоже ≤ α."""
    X = _slices(np.random.default_rng(21), 1.0, 80)
    hits = sum(RS.predictive_tail(int(row[0]), row[2:9].tolist()) <= 0.05
               for sl in X for row in sl)
    assert hits / X.shape[0] / X.shape[1] <= 0.05


# ── 5. Сравнение с рынком ───────────────────────────────────────────────────

def test_share_test_same_growth_is_not_faster():
    """Банк и рынок выросли одинаково (×2 к норме) — «быстрее рынка» нет."""
    r = RS.share_test(20, 200, 70, 700, k=1 / 7)
    assert r["status"] == "ok" and not r["sig"] and r["p"] > 0.3
    assert r["or"] == pytest.approx(1.0) and r["lo"] < 1 < r["hi"]
    assert r["bank_ratio"] == pytest.approx(2.0) and r["market_ratio"] == pytest.approx(2.0)


def test_share_test_faster_growth():
    r = RS.share_test(40, 200, 70, 700, k=1 / 7)          # ×4 против ×2
    assert r["sig"] and r["lo"] > 1 and r["or"] == pytest.approx(2.0)


def test_share_test_is_exact_fisher_and_cornfield():
    a, b, c, d = 7, 3, 2, 8
    pm = _hyper_pmf(a, a + b, c + d, a + c)
    ref_ge = math.fsum(p for j, p in pm if j >= a)
    r = RS.share_test(a, c, b, d, min_market=0)
    assert r["p"] == pytest.approx(ref_ge, rel=1e-12)
    pa = dict(pm)[a]
    ref_two = math.fsum(p for _, p in pm if p <= pa * (1 + 1e-7))
    assert RS.share_test(a, c, b, d, min_market=0,
                         alternative="two-sided")["p"] == pytest.approx(ref_two, rel=1e-12)
    # границы интервала — решения хвостовых уравнений Корнфилда (2,5% каждая)
    lo_tail = math.fsum(p for j, p in _hyper_pmf(a, a + b, c + d, a + c, r["lo"]) if j >= a)
    hi_tail = math.fsum(p for j, p in _hyper_pmf(a, a + b, c + d, a + c, r["hi"]) if j <= a)
    assert lo_tail == pytest.approx(0.025, abs=1e-6)
    assert hi_tail == pytest.approx(0.025, abs=1e-6)


def test_share_test_monotone_in_bank_count():
    ps = [RS.share_test(a, 100, 50, 700)["p"] for a in range(5, 30)]
    assert all(x >= y for x, y in zip(ps, ps[1:]))


def test_share_test_low_market():
    r = RS.share_test(30, 0, 10, 0)
    assert r["status"] == "low_market" and r["p"] is None and not r["sig"] and r["or"] is None
    r = RS.share_test(40, 1, 5, 9)                         # рынок 10 жалоб на два окна
    assert r["status"] == "low_market" and not r["sig"] and r["p"] is not None
    assert RS.share_test(40, 1, 5, 9, min_market=5)["status"] == "ok"


def test_share_test_overdispersion_is_conservative():
    a, c, b, d = 60, 300, 70, 700
    r1, r3 = RS.share_test(a, c, b, d), RS.share_test(a, c, b, d, phi=3.0)
    assert r3["p"] > r1["p"] and r3["lo"] < r1["lo"] and r3["hi"] > r1["hi"]
    assert r3["method"] == "exact+quasi"


def test_share_test_aa_same_growth():
    """A/A: банк и рынок растут одинаково (×1…×2,5) — доля «быстрее рынка» ≤ 5%,
    в том числе на волнах при переданном φ."""
    rng = np.random.default_rng(31)
    for phi in (1.0, 2.0):
        hits = n = 0
        for _ in range(2000):
            g = rng.uniform(1.0, 2.5)
            mus = np.array([5 * g, 70 * g, 35, 490])        # a, c — окно; b, d — база
            if phi > 1:
                sh = mus / (phi - 1)
                mus = rng.gamma(sh, mus / sh)
            a, c, b, d = rng.poisson(mus)
            hits += RS.share_test(int(a), int(c), int(b), int(d), phi=phi, ci=False)["sig"]
            n += 1
        assert hits / n <= 0.05, phi


# ── 6. Изменение к прошлому окну ────────────────────────────────────────────

def _cp_bounds(x: int, n: int, conf: float = 0.95) -> tuple[float, float]:
    """Клоппер — Пирсон перебором по π (бисекция по точной сумме)."""
    tail = (1 - conf) / 2

    def solve(f, target):
        lo, hi = 0.0, 1.0
        for _ in range(200):
            mid = (lo + hi) / 2
            if f(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    lo = 0.0 if x == 0 else solve(lambda p: _binom_sf(x, n, p), tail)
    hi = 1.0 if x == n else solve(lambda p: 1 - (1 - _binom_sf(x + 1, n, p)), 1 - tail)
    return lo, hi


def test_rate_change_exact_interval():
    n, prev, k = 12, 24, 1 / 3.5
    r = RS.rate_change(n, prev, k)
    lo, hi = _cp_bounds(n, n + prev)
    assert r["ratio_lo"] == pytest.approx(lo / (1 - lo) / k, rel=1e-6)
    assert r["ratio_hi"] == pytest.approx(hi / (1 - hi) / k, rel=1e-6)
    pi0 = k / (1 + k)
    up, down = _binom_sf(n, n + prev, pi0), 1 - _binom_sf(n + 1, n + prev, pi0)
    assert r["p"] == pytest.approx(min(1.0, 2 * min(up, down)), rel=1e-9)
    # совместимость с прежним _rate_change: проценты целыми
    assert isinstance(r["lo"], int) and isinstance(r["hi"], int) and r["lo"] < 75 < r["hi"]


def test_rate_change_edges_and_k():
    assert RS.rate_change(10, 0) is None
    z = RS.rate_change(0, 10)
    assert z["lo"] == -100 and z["ratio"] == 0 and z["p"] < 0.01
    assert RS.rate_change(40, 10, 2.0)["ratio"] == pytest.approx(2.0)
    big = RS.rate_change(1200, 1000)
    assert 9 <= big["lo"] <= 11 and 30 <= big["hi"] <= 32 and big["p"] < 1e-4


def test_rate_change_overdispersion():
    a, b = RS.rate_change(140, 100), RS.rate_change(140, 100, phi=3.0)
    assert b["ratio_lo"] < a["ratio_lo"] and b["ratio_hi"] > a["ratio_hi"] and b["p"] > a["p"]
    assert a["p"] < 0.05 < b["p"]
    # A/A на волнах: с φ ложных ≤ 5%, без него — заметно больше
    rng = np.random.default_rng(41)
    sh = 100 / 2.0                                         # φ = 3 при среднем 100
    lam = rng.gamma(sh, 100 / sh, size=(1000, 2))
    xs = rng.poisson(lam)
    with_phi = np.mean([RS.rate_change(int(n), int(p), phi=3.0)["p"] < 0.05 for n, p in xs])
    no_phi = np.mean([RS.rate_change(int(n), int(p))["p"] < 0.05 for n, p in xs])
    assert with_phi <= 0.05 < no_phi


# ── 7. Рынок: ровный, растёт, мало данных ───────────────────────────────────

def test_market_status():
    wk = 1 / 7
    assert RS.market_status(1, 3, k=wk)["status"] == "unknown"     # раньше — «ровный»
    assert RS.market_status(5, 0, k=wk)["status"] == "unknown"
    assert RS.market_status(70, 490, k=wk)["status"] == "flat"
    assert RS.market_status(10, 70, k=wk)["status"] == "unclear"   # мало для «ровного»
    assert RS.market_status(140, 490, k=wk)["status"] == "up"
    assert RS.market_status(30, 490, k=wk)["status"] == "down"


def test_market_verdict_and_note():
    wk = 1 / 7
    v = RS.market_verdict(30, 70, 70, 490, k=wk)          # банк ×3, рынок ровный
    assert v["claim"] == "only_bank" and v["bank_specific"] and v["market_flat"]
    assert RS.market_note(v, "Сбера").startswith("только у Сбера: по рынку тема ровная")
    v = RS.market_verdict(30, 126, 70, 490, k=wk)         # банк ×3, рынок ×1,8
    assert v["claim"] == "faster" and not v["market_flat"]
    assert "сильнее рынка (у рынка ×1,8)" in RS.market_note(v)
    v = RS.market_verdict(18, 126, 70, 490, k=wk)         # оба ×1,8
    assert v["claim"] == "same" and not v["bank_specific"]
    assert RS.market_note(v) == "рынок растёт так же (×1,8)"
    v = RS.market_verdict(30, 1, 70, 3, k=wk)             # у рынка 4 жалобы
    assert v["claim"] == "market_unknown" and not v["market_flat"] and not v["bank_specific"]
    assert RS.market_note(v) == "по рынку данных мало для сравнения"
    v = RS.market_verdict(11, 70, 70, 490, k=wk)          # обычная неделя
    assert v["claim"] == "none" and RS.market_note(v) is None


# ── 8. Бенджамини — Хохберг ─────────────────────────────────────────────────

def test_bh_known_values_and_formats():
    q, rej = RS.bh([0.01, 0.04, 0.03, 0.005], 0.05)
    assert q == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert rej == {0, 1, 2, 3}
    q, rej = RS.bh({"a": 0.01, "b": None, "c": 0.5, "d": float("nan")}, 0.05)
    assert q["b"] is None and q["d"] is None and q["a"] == pytest.approx(0.02)
    assert rej == {"a"}


def test_bh_matches_step_up_rule():
    rng = np.random.default_rng(5)
    for _ in range(200):
        m = int(rng.integers(1, 40))
        ps = np.where(rng.random(m) < 0.3, rng.random(m) * 0.01, rng.random(m)).tolist()
        _q, rej = RS.bh(ps, 0.05)
        order = sorted(range(m), key=lambda i: ps[i])
        kmax = max([r + 1 for r, i in enumerate(order) if ps[i] <= (r + 1) * 0.05 / m],
                   default=0)
        assert rej == set(order[:kmax])


def test_bh_weights_share_one_budget():
    ps = {"freq": 0.02, "rare1": 0.6, "rare2": 0.7, "rare3": 0.8}
    assert RS.bh(ps, 0.05)[1] == set()
    w = {"freq": 4.0, "rare1": 0.0, "rare2": 0.0, "rare3": 0.0}
    assert RS.bh(ps, 0.05, w)[1] == {"freq"}
    # одинаковые веса — обычный BH
    assert RS.bh(ps, 0.05, {k: 2.0 for k in ps})[0] == RS.bh(ps, 0.05)[0]


# ── 9. Эпизоды ──────────────────────────────────────────────────────────────

def test_entry_only_on_entry_days_and_daily_hold():
    flags = [(True, True)] * 10
    g = RS.episode_gate(flags, entry_every=7, entry_phase=3, calm_days=3)
    assert g["states"][:3] == [None, None, None] and g["states"][3] == "new"
    assert g["states"][4:] == ["on"] * 6 and g["active"]
    assert g["episodes"] == [{"start": 3, "end": 9, "closed": None, "days": 7}]


def test_calm_days_end_episode_and_no_flicker():
    seq = ([(True, True)] + [(False, True)] * 3 + [(False, False)] * 2 + [(False, True)]
           + [(False, False)] * 3 + [(False, False)] * 4)
    g = RS.episode_gate(seq, entry_every=1, calm_days=3)
    assert g["states"] == (["new", "on", "on", "on", "cooling", "cooling", "on",
                            "cooling", "cooling", None] + [None] * 4)
    assert g["episodes"] == [{"start": 0, "end": 6, "closed": 9, "days": 7}]
    # calm_days = 1 — как раньше: два эпизода вместо одного
    g1 = RS.episode_gate(seq, entry_every=1, calm_days=1)
    assert len(g1["episodes"]) == 1 and g1["episodes"][0]["closed"] == 4
    g1 = RS.episode_gate([1, 1, 0, 1, 1, 0, 0, 1], entry_every=1, calm_days=1)
    assert [e["start"] for e in g1["episodes"]] == [0, 3, 7]


def test_new_entry_waits_for_entry_day_unless_urgent():
    seq = [False] * 8 + [True] * 8                       # всплеск с 8-го дня, дни входа 0, 7, 14
    g = RS.episode_gate(seq, entry_every=7, calm_days=2)
    assert [e["start"] for e in g["episodes"]] == [14]
    urgent = [(False, False)] * 8 + [(True, True, True)] + [(True, True)] * 4
    g = RS.episode_gate(urgent, entry_every=7, calm_days=2)
    assert g["episodes"][0]["start"] == 8
    d = RS.episode_gate([{"entry": True, "hold": True}], entry_every=1)
    assert d["states"] == ["new"]


def test_entry_day_from_calendar():
    """Дни входа не зависят от начала проигрывания: номер дня — ordinal даты."""
    monday = dt.date(2026, 11, 2)
    phase = monday.toordinal() % 7
    for shift in range(7):
        first = monday - dt.timedelta(days=shift)
        g = RS.episode_gate([True] * 10, start=first.toordinal(), entry_phase=phase)
        assert g["episodes"][0]["start"] == monday.toordinal()


# ── 10. Нулевая модель ──────────────────────────────────────────────────────

def test_null_daily_has_requested_dispersion():
    d = RS.null_daily([10.0], 7 * 3000, phi=2.0, rho=0.9, seed=1)["t00"]
    weeks = [sum(d[i:i + 7]) for i in range(0, len(d), 7)]
    m = sum(weeks) / len(weeks)
    v = sum((w - m) ** 2 for w in weeks) / (len(weeks) - 1)
    assert m == pytest.approx(10.0, rel=0.05)
    assert v / m == pytest.approx(2.0, rel=0.15)
    p = RS.null_daily([10.0], 7 * 3000, phi=1.0, seed=2)["t00"]
    pw = [sum(p[i:i + 7]) for i in range(0, len(p), 7)]
    pm = sum(pw) / len(pw)
    assert sum((w - pm) ** 2 for w in pw) / (len(pw) - 1) / pm == pytest.approx(1.0, rel=0.1)


def test_null_bench_counts_episodes():
    years = 8 * 7 / 365.25

    def always_t00(daily):
        assert len(daily["t00"]) >= 91                   # история до якоря видна
        return {"t00"}

    r = RS.null_bench(always_t00, weeks=8, seeds=2, norms=[1.0, 2.0])
    assert r["episodes_per_year"] == pytest.approx(1 / years)
    assert r["per_topic_year"]["t01"] == 0 and r["topics"] == 2
    assert r["on_share"] == pytest.approx(0.5)
    # словарь «данных сигнала» (как у _signal_chain): горит, даже если entry — строка
    r = RS.null_bench(lambda d: {"t00": {"entry": "week", "p": 0.01}}, weeks=8, seeds=1,
                      norms=[1.0])
    assert r["episodes_per_year"] == pytest.approx(1 / years)


def test_weekly_entry_cuts_false_episodes():
    """Вход раз в неделю и конец после 3 спокойных дней — ложных эпизодов в
    разы меньше, чем при входе каждый день с тем же правилом."""
    rule = RS.week_rule(0.05)
    daily = RS.null_bench(rule, weeks=26, seeds=[0, 1])
    weekly = RS.null_bench(rule, weeks=26, seeds=[0, 1], gate={"entry_every": 7, "calm_days": 3})
    assert daily["episodes_per_year"] > 0
    assert weekly["episodes_per_year"] < 0.6 * daily["episodes_per_year"]
