"""Статистика сигналов жалоб: норма как предсказание, сравнение с рынком,
изменение к прошлому окну, поправка на множественность, эпизоды, нулевая модель.

Чистые функции над числами: без БД и без зависимостей, кроме math (numpy —
только в генераторе нулевой модели, лениво). Встраиваются в reviews_dash на
место _nb_tail/_nb_tail_sum, _vs_market и правила bank_specific, _rate_change,
_bh и дневного проигрывания истории.

Что лечится (разбор сигналов, ноябрь 2026):
  • Норма за 7 недель подставлялась как точная: P(X ≥ x) считался при среднем,
    равном выборочному, будто оно известно без ошибки. На малых нормах ошибка
    самой нормы сравнима с ней, и p занижалось в 2–4 раза. Разброс недель
    брался из тех же 7 недель как точный — на волнах жалоб (φ = 1,5–2) ложных
    срабатываний при пороге 0,002 (уровень поправки BH) было в 2–9 раз больше
    номинала, на двух неделях — до 9 раз (A/A на синтетике 04.11).
    predictive_tail — предсказательное распределение: ошибка нормы (Пуассон —
    Гамма, приор Джеффриса) плюс перерассеяние с его собственной
    неопределённостью и общим φ среза (phi_prior) — с ним доля ложных у
    номинала или ниже (до 1,16 номинала, в пределах шума замера) при φ до 3
    и α от 0,002.
  • «Растёт быстрее рынка» — отношение двух отношений без проверки. share_test —
    точный условный тест 2×2 (банк/рынок × окно/база) с интервалом для
    отношения шансов: оно и есть «во сколько раз рост банка сильнее рыночного».
  • «Только у банка» — правило mratio < 1,4 или ratio ≥ 1,8·mratio, а нехватка
    данных по рынку (mratio = None) читалась как «рынок ровный». market_status
    называет рынок ровным, только если интервал его роста узкий, иначе —
    «мало данных» или «неясно»; market_verdict сводит оба теста в один вывод.
  • Сравнение периодов — пуассоновское, без перерассеяния: интервалы уже
    реальных. rate_change принимает φ.
  • BH тратился на каждом дневном якоре при проигрывании истории: проверок в
    год 365, а не 52, и ложных эпизодов 8–41 в год на срез. episode_gate —
    вход раз в неделю, удержание ежедневно, конец — после K спокойных дней;
    null_bench считает ложные эпизоды в год для любого правила на нулевой
    модели (стационарный ряд с перерассеянием). Замер 04.11 (38 проблем,
    4 года на вариант): прежнее _signal_chain — 8,5 / 32 / 48 ложных эпизодов
    в год при φ = 1 / 1,5 / 2; с хвостами predictive_tail (φ по базе самой
    проблемы, без приора среза) — 0,5 / 5,5 / 8; вход _signal_entries с
    новыми хвостами раз в неделю и K = 3 — 0 / 1,3 / 3.

Соглашение о неделях — как в reviews_dash._weeks_at: base_weeks[0] — самая
свежая неделя базы, дальше — всё старше. Дневные ряды — тоже от нового к
старому: daily[0] — последний полный день.
"""
from __future__ import annotations

import functools
import math
from collections.abc import Callable, Iterable, Mapping, Sequence

# ── Неполные бета- и гамма-функции: на них все хвосты модуля ────────────────
# Хвост через 1 − CDF теряет точность уже около 1e-12, а суммирование
# вероятностей стоит O(x) и на объёмах рынка (тысячи жалоб) медленное.
# Через неполную бету и отрицательно-биномиальный, и биномиальный хвосты
# считаются точно в обоих концах, в том числе при дробных «эффективных»
# счётчиках (счётчик, делённый на φ).

_EPS = 1e-15
_TINY = 1e-300


def _betacf(a: float, b: float, x: float) -> float:
    """Цепная дробь неполной бета-функции (модифицированный метод Ленца)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > _TINY else _TINY)
    h = d
    for m in range(1, 200000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = 1.0 + aa / c
        c = c if abs(c) > _TINY else _TINY
        de = d * c
        h *= de
        if abs(de - 1.0) < _EPS:
            break
    return h


def betainc2(a: float, b: float, x: float) -> tuple[float, float]:
    """(I_x(a, b), 1 − I_x(a, b)) — регуляризованная неполная бета-функция и
    её дополнение; меньшее из двух считается напрямую, без вычитания из 1."""
    if x <= 0.0:
        return 0.0, 1.0
    if x >= 1.0:
        return 1.0, 0.0
    lfront = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
              + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        v = min(1.0, max(0.0, math.exp(lfront) * _betacf(a, b, x) / a))
        return v, 1.0 - v
    v = min(1.0, max(0.0, math.exp(lfront) * _betacf(b, a, 1.0 - x) / b))
    return 1.0 - v, v


def _beta_ppf(q: float, a: float, b: float) -> float:
    """t, при котором I_t(a, b) = q. Бисекция: функция монотонна, а точность
    нужна относительная — границы интервалов бывают порядка 1e-6."""
    if q <= 0.0:
        return 0.0
    if q >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(300):
        mid = 0.5 * (lo + hi)
        if betainc2(a, b, mid)[0] < q:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-13 * hi:
            break
    return 0.5 * (lo + hi)


def gammainc_lower(a: float, x: float) -> float:
    """P(a, x) — регуляризованная нижняя неполная гамма-функция (ряд при
    x < a + 1, иначе цепная дробь для дополнения). Нужна для квантилей χ²."""
    if x <= 0.0:
        return 0.0
    lfront = -x + a * math.log(x) - math.lgamma(a)
    if x < a + 1.0:
        ap, s = a, 1.0 / a
        d = s
        for _ in range(100000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * _EPS:
                break
        return min(1.0, s * math.exp(lfront))
    b = x + 1.0 - a
    c = 1.0 / _TINY
    d = 1.0 / b
    h = d
    for i in range(1, 100000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1.0 / (d if abs(d) > _TINY else _TINY)
        c = b + an / c
        c = c if abs(c) > _TINY else _TINY
        de = d * c
        h *= de
        if abs(de - 1.0) < _EPS:
            break
    return max(0.0, 1.0 - math.exp(lfront) * h)


def _chi2_ppf(u: float, nu: float) -> float:
    """Квантиль χ² с nu степенями свободы: P(nu/2, x/2) = u (бисекция)."""
    hi = max(1.0, nu)
    while gammainc_lower(nu / 2.0, hi / 2.0) < u:
        hi *= 2.0
    lo = 0.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if gammainc_lower(nu / 2.0, mid / 2.0) < u:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-12 * hi:
            break
    return 0.5 * (lo + hi)


def _qnorm_sf(z: float) -> float:
    """P(Z ≥ z) для стандартного нормального Z."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _znorm(conf: float) -> float:
    """Двусторонний квантиль нормального: 0,95 → 1,96 (бисекция по erfc)."""
    tail = (1.0 - conf) / 2.0
    lo, hi = 0.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _qnorm_sf(mid) > tail:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ── 1. Перерассеяние и норма как предсказание ───────────────────────────────

def _raw_phi(xs: Sequence[float]) -> float | None:
    """Оценка φ по Пирсону без обрезки снизу; None — не оценить."""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    if m <= 0:
        return None
    return sum((w - m) ** 2 for w in xs) / ((n - 1) * m)


def overdispersion(weeks: Iterable[float]) -> float:
    """φ — во сколько раз разброс недель больше пуассоновского: Σ(x − m)² /
    ((n − 1)·m), не меньше 1. Жалобы идут волнами (новость, сбой, рассылка), и
    у реальных недель разброс больше среднего — пуассоновский порог на них
    даёт ложные всплески. Меньше двух недель или нулевое среднее — 1."""
    r = _raw_phi([float(w) for w in weeks if w is not None])
    return max(1.0, r) if r is not None else 1.0


def _wh_median(nu: float) -> float:
    """Медиана χ²_nu / nu (Уилсон — Хилферти)."""
    return (1.0 - 2.0 / (9.0 * nu)) ** 3


def _pooled(series: Iterable[Iterable[float]], min_mean: float,
            robust: bool) -> tuple[float, int]:
    raws, dfs, num = [], [], 0.0
    for s_ in series:
        xs = [float(w) for w in s_ if w is not None]
        if len(xs) < 2 or sum(xs) / len(xs) < max(min_mean, 1e-12):
            continue
        r = _raw_phi(xs)
        if r is None:
            continue
        raws.append(r)
        dfs.append(len(xs) - 1)
        num += r * (len(xs) - 1)
    df = sum(dfs)
    if not df:
        return 1.0, 0
    if robust and len(raws) >= 5:
        raws.sort()
        mid = len(raws) // 2
        med = raws[mid] if len(raws) % 2 else 0.5 * (raws[mid - 1] + raws[mid])
        dfs.sort()
        return max(1.0, med / _wh_median(dfs[len(dfs) // 2])), df
    return max(1.0, num / df), df


def pooled_overdispersion(series: Iterable[Iterable[float]], *, min_mean: float = 0.5,
                          robust: bool = True) -> float:
    """Общее φ по нескольким рядам — например, по базам всех проблем среза.

    По 7 неделям одной проблемы φ шумное (6 степеней свободы), а у редкой
    проблемы его не оценить вовсе: из нулей и единиц волну не видно. По сорока
    проблемам — устойчиво. Ряды со средним меньше min_mean в неделю
    пропускаются. robust — медиана оценок по рядам с поправкой на медиану χ²:
    недавний инцидент в базе одной проблемы иначе раздувает общий φ для всех
    (на синтетике 04.11, 400 срезов без волн: одна неделя +40 у частой проблемы
    поднимала сумму Пирсона с 1,04 до 1,37, медиану — с 1,02 до 1,03). Меньше
    пяти рядов — сумма Пирсона / Σ df.
    Обе оценки на сильных волнах слегка занижены (при φ = 3 — около 2,7–2,8)."""
    return _pooled(series, min_mean, robust)[0]


def phi_prior(series: Iterable[Iterable[float]], *, min_mean: float = 0.5, max_df: int = 30,
              robust: bool = True) -> tuple[float, int] | None:
    """Приор для predictive_tail: (общий φ среза, его вес в степенях свободы).

    Вес ограничен max_df: проблемы не обязаны быть одинаково «волнистыми», и
    собственные 6 степеней свободы проблемы должны сдвигать оценку. На
    синтетике с φ до 3 приор (φ среза, 30) держит долю ложных не выше
    номинала и при α = 0,05, и при 0,002. Не из чего оценить — None."""
    phi, df = _pooled(series, min_mean, robust)
    if not df:
        return None
    return phi, min(df, max_df)


def _weights(n: int, weight_recent: float) -> list[float]:
    """Веса недель базы: самая свежая — 1, самая старая — 1/weight_recent,
    между ними — геометрически. Сумма весов — эффективное число недель: с
    весами база честно «короче» и предсказание шире."""
    if n <= 1 or weight_recent == 1.0:
        return [1.0] * n
    if weight_recent <= 0:
        raise ValueError("weight_recent должен быть > 0")
    return [weight_recent ** (-j / (n - 1)) for j in range(n)]


# Узлы смеси по неопределённости φ: φ = ν·φ̂/χ²_ν, ячейки — по вероятности χ²,
# узел — середина ячейки. Сетка сгущена к нулю (малые χ², большие φ): от них
# зависит дальний хвост, по которому решает поправка BH. 33 ячейки против
# эталона из 2000: ошибка хвоста ≤ 1,1% при p от 1e-4 (замер 04.11), а
# равномерные 32 ячейки занижали p до 2,6%.
_PHI_CELLS = ([0.0, 1e-6, 1e-5, 5e-5, 2e-4, 6e-4, 1.5e-3, 3e-3, 6e-3, 0.01, 0.016, 0.025,
               0.037, 0.052, 0.07, 0.1] + [round(0.1 + j * 0.05, 10) for j in range(1, 19)])


@functools.lru_cache(maxsize=256)
def _chi2_nodes(nu: float) -> tuple[tuple[float, float], ...]:
    """(вес, квантиль χ²_nu в середине ячейки) по _PHI_CELLS."""
    out = []
    for lo, hi in zip(_PHI_CELLS[:-1], _PHI_CELLS[1:]):
        out.append((hi - lo, _chi2_ppf(0.5 * (lo + hi), nu)))
    return tuple(out)


def _phi_mix(xs: Sequence[float], k: int, phi: float | None,
             prior: tuple[float, float] | None) -> tuple[list[tuple[float, float]], float]:
    """Смесь по φ: [(вес, φ)] и точечная оценка φ.

    phi задан — он считается известным (общий φ среза, посчитанный заранее).
    Иначе φ оценивается по самой базе (для k > 1 — не меньше разброса сумм
    соседних k недель: волны длиннее недели), с приором (φ0, ν0), если он
    есть, и неопределённостью оценки: φ ~ (ν0·φ0 + ν1·φ̂)/χ²_{ν0+ν1}. Так
    предсказательный хвост с оценённым разбросом тяжелее, как t Стьюдента
    тяжелее нормального. Каждый узел обрезается снизу единицей."""
    if phi is not None:
        f = max(1.0, float(phi))
        return [(1.0, f)], f
    n = len(xs)
    raw = _raw_phi(xs)
    if raw is not None and k > 1 and n > k:
        rk = _raw_phi([sum(xs[i:i + k]) for i in range(n - k + 1)])
        raw = max(raw, rk) if rk is not None else raw
    num, nu = 0.0, 0.0
    if raw is not None:
        num, nu = raw * (n - 1), float(n - 1)
    if prior is not None and prior[1] > 0:
        num += float(prior[0]) * float(prior[1])
        nu += float(prior[1])
    if nu <= 0:
        return [(1.0, 1.0)], 1.0
    point = max(1.0, num / nu)
    if num <= 0:
        return [(1.0, 1.0)], 1.0
    acc: dict[float, float] = {}
    for w, q in _chi2_nodes(round(nu, 6)):
        f = max(1.0, num / q) if q > 0 else 1e6
        acc[f] = acc.get(f, 0.0) + w
    return sorted(((w, f) for f, w in acc.items()), key=lambda t: t[1]), point


def _nb_moments(s: float, t: float, k: int, phi: float) -> tuple[float, float, float, float]:
    """(среднее, разброс, r, p) предсказательного NB при данном φ.

    Квазипуассон: данные равносильны s/φ событиям за t/φ недель, приор
    Джеффриса — ½ события, то есть интенсивность ~ Gamma(s/φ + ½, t/φ):
    среднее (s + φ/2)/t. Сумма k недель: среднее k(s + φ/2)/t, разброс
    φ·среднее·(1 + k/t) — неделя разбросана в φ раз сильнее пуассоновской, и
    ошибка нормы тоже. При φ = 1 это ровно NB(s + ½, t/(t + k))."""
    mean = k * (s + 0.5 * phi) / t
    var = phi * mean * (1.0 + k / t)
    return mean, var, mean * mean / (var - mean), mean / var


def predictive(base_weeks: Sequence[float], weight_recent: float = 1.0, *, k: int = 1,
               phi: float | None = None,
               phi_prior: tuple[float, float] | None = None) -> dict | None:
    """Предсказательное распределение суммы следующих k недель по базе.

    Пуассон — Гамма с приором Джеффриса: по n неделям с суммой S интенсивность
    в неделю — Gamma(S + ½, n), и сумма k будущих недель — отрицательное
    биномиальное с r = S + ½, p = n/(n + k): среднее k(S + ½)/n, разброс
    среднее·(1 + k/n). Вторая часть разброса — ошибка самой нормы: чем короче
    база, тем шире. С весами (weight_recent) S и n — взвешенные. При
    перерассеянии — см. _nb_moments и _phi_mix.

    Возвращает {mean, var, r, p, phi, n_eff, sum} при точечной оценке φ
    (хвост predictive_tail при оценённом φ — смесь, а не это распределение);
    пустая база — None."""
    xs = [max(0.0, float(w)) for w in base_weeks if w is not None]
    n = len(xs)
    if not n or k <= 0:
        return None
    w = _weights(n, weight_recent)
    t = sum(w)
    s = sum(wi * xi for wi, xi in zip(w, xs))
    _mix, point = _phi_mix(xs, k, phi, phi_prior)
    mean, var, r, p = _nb_moments(s, t, k, point)
    return {"mean": mean, "var": var, "r": r, "p": p, "phi": point, "n_eff": t, "sum": s}


def nb_sf(x: float, r: float, p: float) -> float:
    """P(X ≥ x) для отрицательного биномиального X: P(X = j) =
    Γ(j + r)/(Γ(r)·j!)·p^r·(1 − p)^j, среднее r(1 − p)/p. Тождество
    P(X ≥ x) = I_{1−p}(x, r) — точно и в дальнем хвосте."""
    x = math.ceil(x)
    if x <= 0:
        return 1.0
    if p >= 1.0:
        return 0.0
    if p <= 0.0 or r <= 0.0:
        return 1.0
    return betainc2(float(x), float(r), 1.0 - p)[0]


def predictive_tail(x: float, base_weeks: Sequence[float], weight_recent: float = 1.0, *,
                    k: int = 1, phi: float | None = None,
                    phi_prior: tuple[float, float] | None = None, mid: bool = False) -> float:
    """P(X ≥ x): насколько необычна сумма x за k недель при норме из base_weeks.

    Замена _nb_tail (k = 1) и _nb_tail_sum (k = 2). Там норма из 7 недель
    подставлялась как точная: 1 жалоба в неделю, а пришло 5 — p ≈ 0,0037, с
    ошибкой нормы — 0,0092; на нормах 1–10 занижение в 2,2–3,6 раза. Разброс
    недель тоже брался как точный, и при волнах ложных срабатываний было
    больше номинала (см. шапку модуля).

    phi — известный φ (без неопределённости); phi_prior — (φ среза, вес) из
    phi_prior(): φ проблемы сдвигается к общему, хвост учитывает остаточную
    неопределённость. Без обоих φ оценивается по самой базе с её
    неопределённостью — честно, но у редких проблем волн по базе не видно,
    поэтому для среза передавайте phi_prior. weight_recent > 1 — свежие
    недели базы весомее (при тренде нормы); по умолчанию все поровну: при
    стационарном ряде веса только расширяют предсказание. mid — mid-p
    (P(X > x) + ½·P(X = x)): мягче на малых числах, но без гарантии «не больше α».
    Пустая база — 1,0 (не с чем сравнить — не необычно)."""
    x = math.ceil(x)
    if x <= 0:
        return 1.0
    xs = tuple(max(0.0, float(w)) for w in base_weeks if w is not None)
    if not xs or k <= 0:
        return 1.0
    pr = (float(phi_prior[0]), float(phi_prior[1])) if phi_prior else None
    return _tail(int(x), xs, float(weight_recent), int(k),
                 None if phi is None else float(phi), pr, bool(mid))


# Проигрывание истории по дням (reviews_dash._signal_chain) считает одни и те
# же пары «неделя — база» десятки раз: база сдвигается на день, удержание идёт
# против замороженной нормы. Кэш делает повтор бесплатным.
@functools.lru_cache(maxsize=1 << 16)
def _tail(x: int, xs: tuple[float, ...], weight_recent: float, k: int, phi: float | None,
          phi_prior: tuple[float, float] | None, mid: bool) -> float:
    n = len(xs)
    w = _weights(n, weight_recent)
    t = sum(w)
    s = sum(wi * xi for wi, xi in zip(w, xs))
    mix, _point = _phi_mix(xs, k, phi, phi_prior)
    sf = sf1 = 0.0
    for wt, f in mix:
        _m, _v, r, p = _nb_moments(s, t, k, f)
        a = nb_sf(x, r, p)
        sf += wt * a
        if mid:
            sf1 += wt * nb_sf(x + 1, r, p)
    tot = sum(wt for wt, _f in mix)
    sf, sf1 = sf / tot, sf1 / tot
    if not mid:
        return min(1.0, max(0.0, sf))
    return min(1.0, max(0.0, sf1 + 0.5 * (sf - sf1)))


# ── 2. Сравнение с рынком ───────────────────────────────────────────────────
# Таблица 2×2: строки — банк и рынок (рынок без самого банка), столбцы —
# текущее окно и база.
#            окно   база
#   банк       a      b
#   рынок      c      d
# «Банк растёт быстрее рынка» ⇔ доля банка в окне выше, чем в базе ⇔
# отношение шансов (a·d)/(b·c) > 1 — ровно то «отношение отношений»
# (a/b)/(c/d), которое раньше показывалось без проверки. При независимых
# пуассоновских счётчиках проверка, не зависящая от неизвестных уровней, —
# условная (все поля таблицы фиксированы): a подчиняется гипергеометрическому
# распределению (точный тест Фишера), а при отношении шансов ψ —
# нецентральному гипергеометрическому; его обращение даёт точный интервал
# (Корнфилд).

_EXACT_SUPPORT = 3000    # шире — интервал по Вулфу (на таких объёмах они совпадают)


def _lchoose(n: int, k: int) -> float:
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _hyper_logw(n1: int, n2: int, m1: int) -> tuple[int, list[float]]:
    """Носитель и логарифмы весов C(n1, x)·C(n2, m1 − x), x — от lo."""
    lo, hi = max(0, m1 - n2), min(n1, m1)
    lw = [_lchoose(n1, lo) + _lchoose(n2, m1 - lo)]
    for x in range(lo, hi):
        lw.append(lw[-1] + math.log((n1 - x) * (m1 - x)) - math.log((x + 1) * (n2 - m1 + x + 1)))
    return lo, lw


def _nchg(lo: int, lw: list[float], a: int, log_psi: float) -> tuple[float, float, float]:
    """(P(X ≥ a), P(X ≤ a), P(X = a)) при отношении шансов e^log_psi."""
    vals = [v + (lo + i) * log_psi for i, v in enumerate(lw)]
    mx = max(vals)
    e = [math.exp(v - mx) for v in vals]
    tot = math.fsum(e)
    i = a - lo
    return math.fsum(e[i:]) / tot, math.fsum(e[:i + 1]) / tot, e[i] / tot


def _solve_log_psi(f: Callable[[float], float], target: float, increasing: bool) -> float:
    """log ψ, при котором монотонная f(log ψ) = target (бисекция на [−50, 50])."""
    lo, hi = -50.0, 50.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if (f(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-9:
            break
    return 0.5 * (lo + hi)


def _fisher(a: int, b: int, c: int, d: int, alternative: str, conf: float,
            want_ci: bool = True) -> tuple[float, float | None, float | None]:
    """Точный условный тест 2×2: p и интервал Корнфилда для отношения шансов
    (None, если не нужен или носитель шире _EXACT_SUPPORT)."""
    lo, lw = _hyper_logw(a + b, c + d, a + c)
    ge, le, _eq = _nchg(lo, lw, a, 0.0)
    if alternative == "greater":
        p = ge
    elif alternative == "less":
        p = le
    else:
        mx = max(lw)
        e = [math.exp(v - mx) for v in lw]
        pa = e[a - lo] * (1 + 1e-7)
        p = min(1.0, math.fsum(v for v in e if v <= pa) / math.fsum(e))
    if not want_ci or len(lw) > _EXACT_SUPPORT:
        return p, None, None
    tail = (1.0 - conf) / 2.0
    hi_x = lo + len(lw) - 1
    ci_lo = 0.0 if a == lo else math.exp(
        _solve_log_psi(lambda t: _nchg(lo, lw, a, t)[0], tail, increasing=True))
    ci_hi = math.inf if a == hi_x else math.exp(
        _solve_log_psi(lambda t: _nchg(lo, lw, a, t)[1], tail, increasing=False))
    return p, ci_lo, ci_hi


def _quasi_score_p(a: int, b: int, c: int, d: int, phi: float, alternative: str) -> float:
    """p квазипуассоновского теста 2×2: знаковый корень χ²-Пирсона на счётчиках,
    делённых на φ, с поправкой Йейтса в тех же единицах (½ эффективного
    счётчика = φ/2 жалобы). Без поправки при φ = 2–3 и 5 жалобах банка в
    неделю ложных «быстрее рынка» было до 6,0% при α = 5%, с ней — 3,5–3,7%
    (при 20 в неделю — 3,2–3,9%); на больших счётчиках (100 в неделю) — 5,4%
    против 6,4% без поправки, у самого α в пределах шума замера (A/A, 2000
    таблиц на вариант, 04.11)."""
    n = a + b + c + d
    den = (a + b) * (c + d) * (a + c) * (b + d)
    if not den:
        return 1.0 if alternative == "two-sided" else 0.5
    diff = a * d - b * c
    adj = max(0.0, abs(diff) - phi * n / 2.0)
    z = math.copysign(adj, diff) * math.sqrt(n) / math.sqrt(den) / math.sqrt(phi)
    if alternative == "greater":
        return _qnorm_sf(z)
    if alternative == "less":
        return _qnorm_sf(-z)
    return min(1.0, math.erfc(abs(z) / math.sqrt(2.0)))


def share_test(a: int, c: int, b: int, d: int, *, k: float = 1.0, phi: float = 1.0,
               alpha: float = 0.05, min_market: int = 20, alternative: str = "greater",
               conf: float = 0.95, ci: bool = True) -> dict:
    """Растёт ли банк быстрее рынка: a, c — жалобы банка и рынка в текущем
    окне, b, d — в базе (рынок — без самого банка). ci=False — без точного
    интервала (он стоит сотню проходов по носителю), интервал — по Вулфу.

    H1 (alternative="greater"): доля банка в окне выросла против базы, то есть
    отношение шансов (a·d)/(b·c) > 1 — рост банка сильнее рыночного. Тест —
    условный точный (Фишера); от длины окон он не зависит, k (длина окна к
    длине базы, как в rate_change) нужен только для показа ростов.

    Перерассеяние (φ > 1): точный тест верен для пуассоновских счётчиков, а на
    волнах жалоб он слишком уверен (при φ = 2–3 — 10–14% ложных вместо 5%). Тогда
    p — большее из точного и квазипуассоновского (_quasi_score_p): точный
    держит дискретность малых чисел, квазипуассоновский — разброс больших;
    интервал — объединение точного и вулфовского с φ.

    «Мало данных по рынку»: c + d < min_market — status "low_market", sig
    всегда False (вывода «быстрее рынка» нет, числа — для журнала); при
    c + d = 0 сравнивать не с чем вовсе — p и отношение шансов None.

    Возвращает {status, p, sig, or, lo, hi, bank_ratio, market_ratio, phi,
    method, a, b, c, d}; or — (a·d)/(b·c), при нуле в клетке — с поправкой ½
    во всех клетках."""
    a, b, c, d = (max(0, int(v)) for v in (a, b, c, d))
    phi = max(1.0, float(phi or 1.0))
    out = {"status": "ok", "p": None, "sig": False, "or": None, "lo": None, "hi": None,
           "bank_ratio": (a / (b * k)) if b and k else None,
           "market_ratio": (c / (d * k)) if d and k else None,
           "phi": phi, "method": "exact" if phi == 1.0 else "exact+quasi",
           "a": a, "b": b, "c": c, "d": d}
    if c + d < min_market:
        out["status"] = "low_market"
    if c + d == 0:
        return out
    if a + b == 0:
        out["p"] = 1.0
        return out
    p, lo, hi = _fisher(a, b, c, d, alternative, conf, ci)
    zc = _znorm(conf)
    cells = ([v + 0.5 for v in (a, b, c, d)] if 0 in (a, b, c, d)
             else [float(v) for v in (a, b, c, d)])
    lor = math.log(cells[0] * cells[3] / (cells[1] * cells[2]))
    se = math.sqrt(phi * sum(1.0 / v for v in cells))
    w_lo, w_hi = math.exp(lor - zc * se), math.exp(lor + zc * se)
    if lo is None or hi is None:         # огромная таблица: точный интервал не считали
        lo, hi = w_lo, w_hi
    if phi > 1.0:
        p = max(p, _quasi_score_p(a, b, c, d, phi, alternative))
        lo, hi = min(lo, w_lo), max(hi, w_hi)
    out.update({"p": p, "or": math.exp(lor), "lo": lo, "hi": hi})
    out["sig"] = out["status"] == "ok" and p < alpha
    return out


# ── 3. Изменение к прошлому окну ────────────────────────────────────────────

def rate_change(n: int, prev: int, k: float = 1.0, phi: float = 1.0, *,
                conf: float = 0.95) -> dict | None:
    """Изменение счётчика к прошлому окну: отношение интенсивностей
    R = n/(prev·k) с интервалом и двусторонним p (H0: R = 1).

    k — во сколько раз текущее окно длиннее прошлого (review_period.Period.k):
    «как раньше» = prev·k. Тест условный: при фиксированной сумме n + prev
    доля текущего окна — биномиальная с π = R·k/(1 + R·k); интервал —
    Клоппер — Пирсон (точный, верен и для нулей и малых чисел), p — удвоенный
    меньший хвост. Перерассеяние: счётчики делятся на φ (эффективный объём —
    у квазипуассона разброс n равен φ·n), дальше та же точная схема в
    дробных счётчиках через неполную бету. Раньше было лог-нормальное
    приближение без φ: на волнах интервал выходил уже реального.

    Совместимо с прежним _rate_change: lo/hi — проценты (целые), p; плюс ratio,
    ratio_lo, ratio_hi (разы), phi. prev = 0 — None (не с чем сравнить);
    n = 0 при prev > 0 — честный интервал от −100%."""
    n, prev = max(0, int(n)), max(0, int(prev))
    if prev <= 0 or k <= 0:
        return None
    phi = max(1.0, float(phi or 1.0))
    x, y = n / phi, prev / phi
    pi0 = k / (1.0 + k)
    up = betainc2(x, y + 1.0, pi0)[0] if x > 0 else 1.0           # P(X ≥ x)
    down = betainc2(y, x + 1.0, 1.0 - pi0)[0]                       # P(X ≤ x)
    p = min(1.0, 2.0 * min(up, down))
    tail = (1.0 - conf) / 2.0
    p_lo = _beta_ppf(tail, x, y + 1.0) if x > 0 else 0.0
    p_hi = _beta_ppf(1.0 - tail, x + 1.0, y)

    def ratio(pi: float) -> float:
        return pi / ((1.0 - pi) * k) if pi < 1.0 else math.inf

    r, r_lo, r_hi = n / (prev * k), ratio(p_lo), ratio(p_hi)
    return {"lo": round(100 * (r_lo - 1)),
            "hi": round(100 * (r_hi - 1)) if math.isfinite(r_hi) else None,
            "p": p, "ratio": r, "ratio_lo": r_lo, "ratio_hi": r_hi, "phi": phi}


# ── Рынок: ровный, растёт или данных мало ──────────────────────────────────

FLAT_MAX = 1.15        # рост рынка меньше ×1,15 — «ровный» (прежний _FLAT)
FLAT_CEILING = 1.5     # …и верхняя граница интервала не выше ×1,5


def market_status(c: int, d: int, *, k: float = 1.0, phi: float = 1.0, min_market: int = 20,
                  flat_max: float = FLAT_MAX, flat_ceiling: float = FLAT_CEILING,
                  conf: float = 0.95) -> dict:
    """Что с темой у рынка: c — жалобы рынка в окне, d — в базе, k — длина
    окна к длине базы (неделя против 7 недель — 1/7).

    Раньше «ровный» значило «рост меньше ×1,15 или посчитать нельзя»: тема с
    тремя жалобами у рынка за 7 недель шла как «по рынку ровная», и заголовок
    писал «только у банка». Теперь «ровный» — утверждение, и на него нужны
    данные: точка ниже flat_max и верхняя граница интервала не выше
    flat_ceiling (рост больше чем в полтора раза исключён).
      • unknown — c + d < min_market или базы нет: «по рынку данных мало»;
      • down    — значимое снижение (верхняя граница < 1);
      • flat    — ровный (см. выше);
      • up      — значимый рост (нижняя граница > 1);
      • unclear — данные есть, но интервал слишком широк для вывода."""
    c, d = max(0, int(c)), max(0, int(d))
    out = {"status": "unknown", "ratio": None, "lo": None, "hi": None, "p": None,
           "c": c, "d": d}
    if d <= 0 or c + d < min_market:
        return out
    rc = rate_change(c, d, k, phi, conf=conf)
    if rc is None:
        return out
    r, lo, hi = rc["ratio"], rc["ratio_lo"], rc["ratio_hi"]
    out.update({"ratio": r, "lo": lo, "hi": hi, "p": rc["p"]})
    if hi < 1.0:
        out["status"] = "down"
    elif r < flat_max and hi <= flat_ceiling:
        out["status"] = "flat"
    elif lo > 1.0:
        out["status"] = "up"
    else:
        out["status"] = "unclear"
    return out


def market_verdict(a: int, c: int, b: int, d: int, *, k: float = 1.0, phi: float = 1.0,
                   phi_market: float | None = None, alpha: float = 0.05,
                   min_market: int = 20, min_bank: int = 0) -> dict:
    """Один вывод «банк и рынок» вместо bank_specific/market_flat.

    claim:
      • only_bank      — банк значимо обгоняет рынок, а рынок ровный или падает;
      • faster         — обгоняет, рынок тоже растёт (или по нему неясно);
      • same           — рынок значимо растёт, банк от него значимо не отличается;
      • market_unknown — по рынку данных мало: о рынке ничего не утверждать;
      • bank_unknown   — у банка в сопоставимом корпусе меньше min_bank жалоб (неделя +
                         база): тест доли на нулях даёт «рынок растёт так же» про
                         всплеск, посчитанный по другим площадкам (ревью 04.11.2026);
      • none           — ничего утверждать нельзя (данные есть, различия нет).
    Плюс совместимые поля: bank_specific (= обгоняет), market_flat (= ровный
    или падает — НЕ при нехватке данных), market_ratio, gap (отношение шансов)
    с интервалом."""
    sh = share_test(a, c, b, d, k=k, phi=phi, alpha=alpha, min_market=min_market)
    mk = market_status(c, d, k=k, phi=phi if phi_market is None else phi_market,
                       min_market=min_market)
    flat = mk["status"] in ("flat", "down")
    if a + b < min_bank:
        claim = "bank_unknown"
    elif sh["status"] == "low_market" or mk["status"] == "unknown":
        claim = "market_unknown"
    elif sh["sig"]:
        claim = "only_bank" if flat else "faster"
    elif mk["status"] == "up":
        claim = "same"
    else:
        claim = "none"
    return {"claim": claim, "bank_specific": claim in ("only_bank", "faster"),
            "market_flat": flat and claim not in ("market_unknown", "bank_unknown"),
            "bank_ratio": sh["bank_ratio"], "market_ratio": mk["ratio"],
            "gap": sh["or"], "gap_lo": sh["lo"], "gap_hi": sh["hi"], "p": sh["p"],
            "share": sh, "market": mk}


def _ru(x: float) -> str:
    """1 знак после запятой по-русски, без «,0»: 2,1 · 4 · 1,9."""
    t = f"{round(float(x), 1):.1f}".replace(".", ",")
    return t[:-2] if t.endswith(",0") else t


def _raz(x: float) -> str:
    """«в 2,1 раза», «в 3 раза», «в 5 раз»."""
    r = round(float(x), 1)
    if r != int(r):
        return "раза"
    n = int(r)
    return "раза" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "раз"


def market_note(v: dict, who: str = "банка") -> str | None:
    """Как честно сказать о рынке по market_verdict: «только у …» — лишь при
    подтверждённо ровном рынке, «сильнее рынка» — лишь при значимом обгоне,
    при нехватке данных — так и сказать. None — о рынке молчать."""
    claim, mr, gap = v.get("claim"), v.get("market_ratio"), v.get("gap")
    if claim == "only_bank":
        return (f"только у {who}: по рынку тема ровная (×{_ru(mr)})" if mr is not None
                else f"только у {who}: по рынку тема ровная")
    if claim == "faster" and gap:
        tail = f" (у рынка ×{_ru(mr)})" if mr is not None else ""
        return f"в {_ru(gap)} {_raz(gap)} сильнее рынка{tail}"
    if claim == "same" and mr is not None:
        return f"рынок растёт так же (×{_ru(mr)})"
    if claim == "market_unknown":
        return "по рынку данных мало для сравнения"
    if claim == "bank_unknown":
        return f"у {who} мало жалоб на banki.ru — с рынком не сравниваем"
    return None


# ── 4. Поправка на множественность и эпизоды ────────────────────────────────

def bh(pvals: Mapping | Sequence, alpha: float = 0.05,
       weights: Mapping | Sequence | None = None) -> tuple[dict | list, set]:
    """Бенджамини — Хохберг: (q-значения, отвергнутые гипотезы).

    pvals — словарь или список; None/NaN пропускаются (в число гипотез не
    идут, q для них None). Отвергнута ⇔ q ≤ alpha — ровно ступенчатое правило
    BH. weights — взвешенный BH (Дженовезе — Рёдер — Вассерман): p делится на
    вес, веса нормируются к среднему 1. Так делится ОБЩИЙ бюджет между частыми
    и редкими проблемами вместо двух отдельных поправок 4% + 1%."""
    is_map = isinstance(pvals, Mapping)
    items = list(pvals.items()) if is_map else list(enumerate(pvals))
    valid = [(key, float(p)) for key, p in items
             if p is not None and not (isinstance(p, float) and math.isnan(p))]
    q: dict = {key: None for key, _ in items}
    if valid:
        if weights is not None:
            wmap = weights if isinstance(weights, Mapping) else dict(enumerate(weights))
            ws = [max(0.0, float(wmap.get(key, 1.0))) for key, _ in valid]
            mw = sum(ws) / len(ws)
            adj = [(key, (p / (w / mw)) if w > 0 and mw > 0 else math.inf)
                   for (key, p), w in zip(valid, ws)]
        else:
            adj = valid
        m = len(adj)
        order = sorted(adj, key=lambda kv: kv[1])
        prev = 1.0
        for rank in range(m, 0, -1):
            key, p = order[rank - 1]
            prev = min(prev, p * m / rank)
            q[key] = min(1.0, prev)
    rejected = {key for key, v in q.items() if v is not None and v <= alpha}
    if is_map:
        return q, rejected
    return [q[i] for i in range(len(items))], rejected


_FLAG_KEYS = ("entry", "hold", "urgent")


def _as_flags(v):
    """Ответ правила по проблеме → флаги дня для episode_gate.

    bool и кортеж — уже флаги; словарь — флаги, только если его entry/hold/
    urgent — булевы. Иначе это данные сигнала (как у _signal_chain: там
    entry — НАЗВАНИЕ проверки входа, а не флаг), и раз проблема в ответе —
    сигнал горит."""
    if isinstance(v, (bool, tuple, list)):
        return v
    if isinstance(v, Mapping):
        vals = [v[k_] for k_ in _FLAG_KEYS if k_ in v]
        if vals and all(isinstance(x, bool) or x is None for x in vals):
            return v
    return True


def _flag(f) -> tuple[bool, bool, bool]:
    """Флаги дня → (вход, удержание, срочный вход)."""
    if isinstance(f, Mapping):
        return bool(f.get("entry")), bool(f.get("hold")), bool(f.get("urgent"))
    if isinstance(f, (tuple, list)):
        e = bool(f[0]) if len(f) > 0 else False
        h = bool(f[1]) if len(f) > 1 else e
        u = bool(f[2]) if len(f) > 2 else False
        return e, h, u
    v = bool(f)
    return v, v, False


def episode_gate(daily_flags: Sequence, *, calm_days: int = 3, entry_every: int = 7,
                 entry_phase: int = 0, start: int = 0) -> dict:
    """Эпизод сигнала по дневным флагам (от старого дня к новому).

    Раньше вход проверялся каждый день, и бюджет BH тратился 365 раз в год на
    сильно перекрытых окнах; плюс сигнал гас в первый же день без удержания и
    тут же мог подняться снова — мигание и лишние «новые» эпизоды. Здесь:
      • вход — только в дни входа: день t (= start + индекс) с
        (t − entry_phase) % entry_every == 0, то есть раз в неделю при
        entry_every = 7. Чтобы дни не зависели от того, с какого дня
        проигрывается история, start — абсолютный номер первого дня
        (date.toordinal()), а entry_phase — номер дня входа по модулю недели;
      • срочный вход (третий флаг) — в любой день: для правила с порогом
        α/entry_every, чтобы сильный всплеск не ждал дня входа;
      • удержание — ежедневно: в эпизоде день активен, если прошёл вход или
        удержание;
      • конец — после calm_days спокойных дней подряд (calm_days = 1 — гаснет
        в первый же спокойный день, как раньше); до этого — «cooling».

    Флаг дня — bool (вход и удержание сразу), кортеж (вход, удержание[,
    срочный]) или словарь {entry, hold, urgent}.

    Возвращает {states, episodes, active}: states — по дням None / "new" /
    "on" / "cooling"; episodes — [{start, end, closed, days}] в номерах дней
    (end — последний активный день, closed — день снятия или None)."""
    if entry_every < 1 or calm_days < 1:
        raise ValueError("entry_every и calm_days должны быть ≥ 1")
    states: list[str | None] = []
    episodes: list[dict] = []
    cur: dict | None = None
    calm = 0
    for i, f in enumerate(daily_flags):
        t = start + i
        entry, hold, urgent = _flag(f)
        if cur is None:
            if (entry and (t - entry_phase) % entry_every == 0) or urgent:
                cur = {"start": t, "end": t, "closed": None, "days": 1}
                episodes.append(cur)
                calm = 0
                states.append("new")
            else:
                states.append(None)
            continue
        if entry or hold or urgent:
            calm = 0
            cur["end"] = t
            cur["days"] = t - cur["start"] + 1
            states.append("on")
            continue
        calm += 1
        if calm >= calm_days:
            cur["closed"] = t
            cur = None
            states.append(None)
        else:
            states.append("cooling")
    return {"states": states, "episodes": episodes, "active": cur is not None}


# ── 5. Нулевая модель: сколько ложных эпизодов даёт правило ─────────────────
# Нормы проблем крупного банка в неделю (38 проблем кодификатора, сентябрь
# 2026): треть — меньше одной жалобы в неделю, десяток — от 5 до 13. Срез с
# таким составом и проверяется по умолчанию.
DEFAULT_NORMS = (0.1, 0.3, 0.3, 0.4, 0.4, 0.4, 0.6, 1.0, 1.1, 1.1, 1.1, 1.3, 1.3, 1.3, 1.6,
                 1.7, 2.1, 2.3, 2.9, 2.9, 2.9, 3.9, 4.0, 4.4, 4.6, 4.9, 5.3, 5.4, 5.6, 5.9,
                 5.9, 6.0, 7.9, 9.1, 10.9, 11.6, 12.6, 12.7)


def _latent_sigma(norm: float, phi: float, rho: float) -> float:
    """σ скрытого множителя интенсивности, при котором у недельных сумм
    разброс = φ·среднее. Дневная интенсивность — norm/7·exp(σ·z − σ²/2), z —
    стационарный AR(1) с корреляцией rho между соседними днями. Тогда
    Var(неделя) = norm + (norm/7)²·Σ_{i,j<7}(exp(σ²·rho^|i−j|) − 1)."""
    if phi <= 1.0 or norm <= 0:
        return 0.0
    target = 49.0 * (phi - 1.0) / norm

    def s(sig2: float) -> float:
        return sum(math.expm1(sig2 * rho ** abs(i - j)) for i in range(7) for j in range(7))

    hi = 1.0
    while s(hi) < target and hi < 200:
        hi *= 2.0
    lo = 0.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if s(mid) < target:
            lo = mid
        else:
            hi = mid
    return math.sqrt(0.5 * (lo + hi))


def _per_topic(v, keys: list[str], i: int, norm: float, default: float) -> float:
    if v is None:
        return default
    if callable(v):
        return float(v(norm))
    if isinstance(v, Mapping):
        return float(v.get(keys[i], default))
    if isinstance(v, (list, tuple)):
        return float(v[i])
    return float(v)


def null_daily(norms: Sequence[float] | Mapping[str, float] = DEFAULT_NORMS, days: int = 455, *,
               phi=1.5, rho: float = 0.9, seed: int = 0) -> dict[str, list[int]]:
    """Нулевая модель среза: дневные жалобы по проблемам без единого настоящего
    всплеска — стационарный ряд с перерассеянием.

    Дневная интенсивность — norm/7 · exp(σ·z − σ²/2), z — AR(1) с корреляцией
    rho между соседними днями (волны длиннее недели: при 0,9 корреляция
    через неделю ≈ 0,48), σ подобрана так, что разброс недельных сумм —
    φ·среднее. phi — число, список/словарь по проблемам или функция от нормы.
    Ряды — от нового дня к старому, как у reviews_dash._topic_day_counts."""
    import numpy as np

    items = (list(norms.items()) if isinstance(norms, Mapping)
             else [(f"t{i:02d}", float(v)) for i, v in enumerate(norms)])
    keys = [k_ for k_, _ in items]
    rng = np.random.default_rng(seed)
    out: dict[str, list[int]] = {}
    s_ar = math.sqrt(max(0.0, 1.0 - rho * rho))
    for i, (key, norm) in enumerate(items):
        sig = _latent_sigma(norm, _per_topic(phi, keys, i, norm, 1.0), rho)
        eps = rng.standard_normal(days)
        z = np.empty(days)
        z[0] = eps[0]
        for t in range(1, days):
            z[t] = rho * z[t - 1] + s_ar * eps[t]
        lam = norm / 7.0 * np.exp(sig * z - 0.5 * sig * sig)
        out[key] = rng.poisson(lam)[::-1].tolist()
    return out


def null_bench(rule: Callable[[dict[str, list[int]]], Iterable[str] | Mapping], weeks: int = 52,
               seeds: int | Iterable[int] = 20, *, norms=DEFAULT_NORMS, phi=1.5,
               rho: float = 0.9, warmup_days: int = 91, gate: Mapping | None = None) -> dict:
    """Сколько ложных эпизодов в год даёт правило на срезе без всплесков.

    rule(daily) зовётся на каждый день weeks недель, от старого к новому;
    daily — {проблема: дневные жалобы от этого дня назад} (daily[k][0] — день
    якоря, история — warmup_days и всё до якоря; будущего правило не видит).
    Вернуть можно перечень проблем, по которым сигнал горит; словарь
    {проблема: флаги дня} для episode_gate (bool, кортеж (вход, удержание[,
    срочный]) или {entry, hold, urgent} с булевыми значениями); или словарь
    {проблема: данные сигнала} (как _signal_chain) — горят его ключи.
    gate — параметры episode_gate; без него эпизод — серия горящих дней
    подряд (calm_days = 1, вход каждый день), то есть «как в ленте».
    Прежнее правило проверяется так:
        null_bench(lambda d: reviews_dash._signal_chain(d, list(d)), …)

    Возвращает {episodes_per_year (на срез, среднее по сидам), by_seed,
    max_per_year, per_topic_year, on_share (доля проблемо-дней с сигналом),
    topics, weeks, seeds}."""
    seed_list = list(range(seeds)) if isinstance(seeds, int) else list(seeds)
    days = int(weeks) * 7
    g = {"calm_days": 1, "entry_every": 1, **(gate or {})}
    years = days / 365.25
    by_seed: list[float] = []
    per_topic: dict[str, int] = {}
    on_days = topic_days = 0
    for sd in seed_list:
        daily = null_daily(norms, warmup_days + days, phi=phi, rho=rho, seed=sd)
        keys = list(daily)
        flags: dict[str, list] = {k_: [] for k_ in keys}
        for i in range(days):
            a = days - 1 - i
            res = rule({k_: v[a:] for k_, v in daily.items()})
            if isinstance(res, Mapping):
                for k_ in keys:
                    flags[k_].append(_as_flags(res[k_]) if k_ in res else False)
            else:
                on = set(res or ())
                for k_ in keys:
                    flags[k_].append(k_ in on)
        n_ep = 0
        for k_ in keys:
            gr = episode_gate(flags[k_], **g)
            n_ep += len(gr["episodes"])
            per_topic[k_] = per_topic.get(k_, 0) + len(gr["episodes"])
            on_days += sum(1 for s_ in gr["states"] if s_)
        topic_days += days * len(keys)
        by_seed.append(n_ep / years)
    ns = max(len(seed_list), 1)
    return {"episodes_per_year": sum(by_seed) / ns, "by_seed": by_seed,
            "max_per_year": max(by_seed) if by_seed else 0.0,
            "per_topic_year": {k_: v / (ns * years) for k_, v in per_topic.items()},
            "on_share": on_days / topic_days if topic_days else 0.0,
            "topics": len(per_topic), "weeks": int(weeks), "seeds": len(seed_list)}


def week_rule(alpha: float = 0.05, *, hold_alpha: float = 0.05,
              urgent_alpha: float | None = None, weight_recent: float = 1.0,
              phi: float | None = None, pool: bool = True,
              min_count: int = 0) -> Callable[[dict], dict]:
    """Образец правила для null_bench и episode_gate: неделя против 7 недель
    базы (недели 2..8, как в reviews_dash) по predictive_tail, вход — BH по
    всем проблемам среза (q ≤ alpha), удержание — p ≤ hold_alpha без поправки
    (держится только уже поднятый сигнал), срочный вход — q ≤ urgent_alpha.
    pool — приор φ по базам всех проблем среза (phi_prior). min_count —
    практический порог недели. Возвращает rule(daily) → {проблема: флаги}."""
    def rule(daily: dict[str, list[int]]) -> dict:
        weeks = {key: [sum(v[7 * j:7 * j + 7]) for j in range(9)] for key, v in daily.items()}
        prior = phi_prior([w[2:9] for w in weeks.values()]) if pool and phi is None else None
        ps: dict[str, float] = {}
        for key, wk in weeks.items():
            ps[key] = (predictive_tail(wk[0], wk[2:9], weight_recent, phi=phi, phi_prior=prior)
                       if wk[0] >= min_count else 1.0)
        q, rej = bh(ps, alpha)
        return {key: (key in rej, p <= hold_alpha,
                      urgent_alpha is not None and q[key] is not None and q[key] <= urgent_alpha)
                for key, p in ps.items()}
    return rule
