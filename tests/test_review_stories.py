"""«Сюжеты» (rag/review_stories.py): кластеризация, сопоставление прогонов,
название, «новое», пакетный проход, шаг названий с моделью и чтение для
вкладки. Векторы синтетические, база — SQLite по схеме из миграции 104 (и
живой Postgres по STORIES_PG_TEST_URL); модель — подделка, сети нет."""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from bank_audit.rag import review_stories as RS  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


# ── Синтетические векторы ───────────────────────────────────────────────────
# Точка — сумма именованных ортогональных осей с весами: косинусы между
# точками считаются точно, без шума. Сюжет «A» с плотностью c: точка =
# √c·A + √(1−c)·свой_шум, сходство двух точек сюжета — ровно c.

class Space:
    def __init__(self, dim: int = 96):
        self.dim = dim                     # постоянная ширина: точки добавляются и позже
        self.axes: dict[str, int] = {}
        self.points: dict[str, dict[str, float]] = {}

    def add(self, pid: str, **coef: float) -> str:
        for a in coef:
            self.axes.setdefault(a, len(self.axes))
        self.points[pid] = coef
        return pid

    def story(self, name: str, k: int, c: float, start: int = 0) -> list[str]:
        """k точек сюжета name с попарным косинусом c."""
        out = []
        for j in range(start, start + k):
            pid = f"{name}{j:02d}"
            self.add(pid, **{name: c ** 0.5, f"{pid}_own": (1 - c) ** 0.5})
            out.append(pid)
        return out

    def matrix(self, ids, jitter: float = 0.0, seed: int = 0):
        assert len(self.axes) <= self.dim
        rng = np.random.default_rng(seed)
        X = np.zeros((len(ids), self.dim + (8 if jitter else 0)))
        for r, pid in enumerate(ids):
            for a, w in self.points[pid].items():
                X[r, self.axes[a]] = w
        if jitter:
            X[:, self.dim:] = rng.normal(scale=jitter, size=(len(ids), 8))
        return X


def _cos(X, i, j):
    return float(X[i] @ X[j] / np.linalg.norm(X[i]) / np.linalg.norm(X[j]))


# ── Кластеризация ───────────────────────────────────────────────────────────

def _naive_average_link(S, thr):
    """Эталон average linkage «в лоб»: каждый шаг — средние по всем парам заново."""
    groups = [[k] for k in range(len(S))]
    while True:
        best, pair = -np.inf, None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                v = np.mean([S[i, j] for i in groups[a] for j in groups[b]])
                if v > best:
                    best, pair = v, (a, b)
        if pair is None or best < thr:
            break
        a, b = pair
        groups[a] = groups[a] + groups[b]
        del groups[b]
    return sorted(sorted(g) for g in groups if len(g) > 1)


@pytest.mark.parametrize("seed", range(6))
def test_link_matches_naive_average_linkage(seed):
    """Быстрая склейка (Ланс — Уильямс + лучший сосед строки) даёт то же
    разбиение, что пересчёт средних по всем парам на каждом шаге."""
    rng = np.random.default_rng(seed)
    cent = rng.normal(size=(5, 12))
    X = np.vstack([cent[k % 5] + rng.normal(scale=0.6, size=12) for k in range(36)])
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    S = X @ X.T
    thr = float(np.quantile(S[np.triu_indices(36, 1)], 0.8))
    got = sorted(RS._link(S.copy(), thr))
    assert got == _naive_average_link(S, thr)
    assert got, "порог подобран так, что склейки есть"


def test_cluster_finds_stories_and_skips_noise():
    sp = Space()
    a, b, c = sp.story("A", 8, 0.9), sp.story("B", 6, 0.88), sp.story("C", 5, 0.87)
    noise = [sp.add(f"z{k}", **{f"z{k}": 1.0}) for k in range(10)]
    ids = a + b + c + noise
    cl = RS.cluster(sp.matrix(ids), ids)
    assert [set(x.ids) for x in cl] == [set(a), set(b), set(c)]
    assert all(x.medoid in x.ids and len(x.sims) == len(x.ids) for x in cl)
    assert cl[0].cohesion == pytest.approx(0.9, abs=1e-3)
    # две жалобы — не сюжет
    two = sp.story("D", 2, 0.95)
    assert RS.cluster(sp.matrix(two), two) == []


def test_cluster_is_deterministic_regardless_of_input_order():
    sp = Space()
    ids = sp.story("A", 7, 0.9) + sp.story("B", 6, 0.86) + sp.story("C", 4, 0.85)
    # z — 0,25 между собой: не сюжет
    ids += [sp.add(f"z{k}", z=0.5, **{f"z{k}": 0.75 ** 0.5}) for k in range(6)]
    X = sp.matrix(ids, jitter=0.03, seed=1)
    issue = {i: ("fees" if i[0] in "AZ" else "bonus") for i in ids}
    base = RS.cluster(X, ids, issue_of=issue)
    rnd = random.Random(7)
    for _ in range(5):
        perm = list(range(len(ids)))
        rnd.shuffle(perm)
        again = RS.cluster(X[perm], [ids[k] for k in perm], issue_of=issue)
        assert again == base


def test_issue_penalty_is_a_penalty_not_a_ban():
    """Разные главные проблемы поднимают порог пары на 0,04 (0,84 → 0,88), но
    не запрещают склейку: лента ставила для них отдельный строгий порог, и одна
    история, разнесённая разметкой по соседним кодам, рвалась пополам."""
    sp = Space()
    mid = sp.story("M", 6, 0.86)                  # 0,86: между 0,84 и 0,88
    issue = {p: ("fees" if k < 3 else "imposed_addon") for k, p in enumerate(mid)}
    X = sp.matrix(mid)
    assert [set(c.ids) for c in RS.cluster(X, mid)] == [set(mid)]          # без проблем — одна
    split = RS.cluster(X, mid, issue_of=issue)                             # штраф развёл
    assert sorted(set(c.ids) for c in split) == sorted([set(mid[:3]), set(mid[3:])],
                                                        key=sorted)
    assert {c.issue for c in split} == {"fees", "imposed_addon"}
    # очень похожие склеиваются и с разными проблемами: штраф, а не запрет
    hi = sp.story("H", 6, 0.93)
    issue_hi = {p: ("fees" if k < 3 else "imposed_addon") for k, p in enumerate(hi)}
    one = RS.cluster(sp.matrix(hi), hi, issue_of=issue_hi)
    assert [set(c.ids) for c in one] == [set(hi)]
    # без известной проблемы штрафа нет; issue_penalty=0 — тоже
    assert len(RS.cluster(X, mid, issue_of={})) == 1
    assert len(RS.cluster(X, mid, issue_of=issue, issue_penalty=0)) == 1
    # главная проблема — большинства; ничья решается в пользу медоида
    four = sp.story("F", 5, 0.95)
    iss4 = dict(zip(four, ["bonus", "bonus", "bonus", "fees", "fees"]))
    assert RS.cluster(sp.matrix(four), four, issue_of=iss4, issue_penalty=0)[0].issue == "bonus"


def _chain_space():
    """Ядро из 6 жалоб (0,95 между собой), Y1 близка к ядру (0,91), Y2 близка к
    Y1 (0,95), но далека от ядра (0,79). Пара {Y1, Y2} в среднем с ядром — 0,85:
    средняя связь склеивает, хотя Y2 — хвост цепочки, а не та же история."""
    sp = Space()
    ca = 0.95 ** 0.5
    core = sp.story("K", 6, 0.95)
    a1, a2 = 0.91 / ca, 0.79 / ca
    b1, b2 = 0.35, (0.95 - a1 * a2) / 0.35
    sp.add("Y1", K=a1, g=b1, y1=(1 - a1 ** 2 - b1 ** 2) ** 0.5)
    sp.add("Y2", K=a2, g=b2, y2=(1 - a2 ** 2 - b2 ** 2) ** 0.5)
    ids = core + ["Y1", "Y2"]
    X = sp.matrix(ids)
    assert _cos(X, 6, 0) == pytest.approx(0.91, abs=1e-6)
    assert _cos(X, 7, 0) == pytest.approx(0.79, abs=1e-6)
    assert _cos(X, 6, 7) == pytest.approx(0.95, abs=1e-6)
    return ids, X


def test_medoid_check_drops_chain_tail():
    ids, X = _chain_space()
    # без проверки медоидом хвост в группе
    loose = RS.cluster(X, ids, medoid_sim=-1.0)
    assert [set(c.ids) for c in loose] == [set(ids)]
    # с проверкой — хвост выкинут, ближний к ядру Y1 остаётся
    cl = RS.cluster(X, ids)
    assert len(cl) == 1 and "Y2" not in cl[0].ids and "Y1" in cl[0].ids
    assert cl[0].medoid.startswith("K")
    assert min(cl[0].sims) >= RS.SIM


def test_expelled_tails_get_second_chance_together():
    """Выкинутые медоидом склеиваются между собой ещё раз: хвост одной группы
    бывает ядром другой — три Y2-подобные жалобы образуют свою группу."""
    sp = Space()
    ca = 0.95 ** 0.5
    core = sp.story("K", 8, 0.95)
    tails = []
    for k in range(3):
        a = 0.80 / ca
        tails.append(sp.add(f"T{k}", K=a, t=0.55, **{f"T{k}_own": (1 - a * a - 0.3025) ** 0.5}))
    ids = core + tails
    X = sp.matrix(ids)
    cl = RS.cluster(X, ids)
    groups = sorted(set(c.ids) for c in cl)
    assert set(core) in groups
    # хвосты между собой: a² + 0,55² ≈ 0,98 — своя история
    assert set(tails) in groups


def test_cluster_input_guards():
    sp = Space()
    ids = sp.story("A", 4, 0.9)
    X = sp.matrix(ids)
    with pytest.raises(ValueError):
        RS.cluster(X, ids[:3])
    with pytest.raises(ValueError):
        RS.cluster(X, [ids[0]] * 4)
    # нулевой вектор (сбой эмбеддера) в группу не попадает
    Z = np.vstack([X, np.zeros(X.shape[1])])
    cl = RS.cluster(Z, ids + ["zero"])
    assert [set(c.ids) for c in cl] == [set(ids)]
    # сверх max_n — лишние с конца
    cl = RS.cluster(X, ids, max_n=3)
    assert [set(c.ids) for c in cl] == [set(ids[:3])]
    assert RS.cluster([], []) == []


def test_cluster_speed_on_thousands():
    """5000 изложений — предел прогона; 3000 должны укладываться в секунды."""
    import time
    rng = np.random.default_rng(3)
    cent = rng.normal(size=(80, 128))
    X = np.vstack([cent[k % 80] / np.linalg.norm(cent[k % 80]) + rng.normal(scale=0.035, size=128)
                   for k in range(3000)])
    t0 = time.time()
    cl = RS.cluster(X, [f"u{k:05d}" for k in range(3000)])
    assert time.time() - t0 < 20
    assert len(cl) == 80


# ── Сопоставление прогонов ──────────────────────────────────────────────────

def _assign(story_of, start=1):
    """Номер группы → id сюжета: продолжения — свои, новым — следующие по счёту."""
    nxt, out = start, []
    for s in story_of:
        if s is None:
            out.append(nxt)
            nxt += 1
        else:
            out.append(s)
    return out


def test_ids_survive_small_changes_between_runs():
    sp = Space()
    a, b, c = sp.story("A", 8, 0.9), sp.story("B", 6, 0.88), sp.story("C", 5, 0.87)
    noise = [sp.add(f"z{k}", **{f"z{k}": 1.0}) for k in range(10)]
    ids1 = a + b + c + noise
    run1 = RS.cluster(sp.matrix(ids1, jitter=0.02, seed=1), ids1)
    m1 = RS.match_stories({}, run1)
    assert [e.kind for e in m1.events] == ["appeared"] * 3 and not m1.closed
    sid1 = _assign(m1.story_of)
    old = {sid: set(g.ids) for sid, g in zip(sid1, run1)}

    # второй прогон: в A две новые жалобы, одна жалоба B ушла из окна, новый шум,
    # все векторы чуть сдвинуты, порядок подачи другой
    a2 = sp.story("A", 2, 0.9, start=8)
    more = [sp.add(f"w{k}", **{f"w{k}": 1.0}) for k in range(4)]
    ids2 = list(reversed(a + a2 + b[1:] + c + noise + more))
    run2 = RS.cluster(sp.matrix(ids2, jitter=0.03, seed=2), ids2)
    seen = set(ids2)
    m2 = RS.match_stories({s: u & seen for s, u in old.items()}, run2)
    assert sorted(m2.story_of) == sorted(sid1)                 # все id живы, новых нет
    sid_a = next(s for s, u in old.items() if a[0] in u)
    k_a = m2.story_of.index(sid_a)
    assert set(a + a2) == set(run2[k_a].ids)
    kinds = [(e.kind, e.story) for e in m2.events]
    assert kinds == [("grew", sid_a)]
    grew = m2.events[0]
    assert grew.detail == {"new": 2, "n": 10}
    assert not m2.closed


def test_growth_beyond_jaccard_keeps_id():
    """Сюжет из 5 жалоб вырос до 12: Jaccard 5/12 < 0,5, но весь прежний состав
    в одной группе — это рост, а не новый сюжет."""
    old = {7: {f"a{k}" for k in range(5)}}
    new = [[f"a{k}" for k in range(5)] + [f"n{k}" for k in range(7)]]
    m = RS.match_stories(old, new)
    assert m.story_of == [7]
    assert [(e.kind, e.detail) for e in m.events] == [("grew", {"new": 7, "n": 12})]
    # а одна общая жалоба — не основание: новая группа, старый сюжет затих
    m = RS.match_stories({7: {"a0", "a1", "a2"}}, [["a0", "x1", "x2", "x3"]])
    assert m.story_of == [None] and m.closed == {7: "quiet"}


def test_merge_of_two_stories():
    old = {1: {f"a{k}" for k in range(5)}, 2: {f"b{k}" for k in range(4)}}
    new = [sorted(old[1] | old[2] | {"n0"})]
    m = RS.match_stories(old, new)
    assert m.story_of == [1]                               # id — у большего (Jaccard 5/10)
    assert m.closed == {2: "merged"}
    ev = {(e.kind, e.story): e.detail for e in m.events}
    assert ev[("merged", 2)] == {"into": 1}
    assert ev[("merged", 1)] == {"from": [2]}
    assert ev[("grew", 1)] == {"new": 1, "n": 10}


def test_split_with_and_without_continuation():
    # от сюжета откололся кусок: сам живёт, кусок — новый сюжет «из» него
    old = {1: {f"x{k}" for k in range(8)}}
    new = [[f"x{k}" for k in range(5)] + ["y0"], ["x5", "x6", "x7"]]
    m = RS.match_stories(old, new)
    assert m.story_of == [1, None] and not m.closed
    ev = {(e.kind, e.story, e.cluster): e.detail for e in m.events}
    assert ev[("split", 1, None)] == {"pieces": [1], "kept": True}
    assert ev[("appeared", None, 1)] == {"n": 3, "from": [1]}
    # распался на три равные части: ни одна не наследует id
    old = {1: {f"x{k}" for k in range(9)}}
    new = [["x0", "x1", "x2", "z0"], ["x3", "x4", "x5", "z1"], ["x6", "x7", "x8"]]
    m = RS.match_stories(old, new)
    assert m.story_of == [None, None, None] and m.closed == {1: "split"}
    split = [e for e in m.events if e.kind == "split"]
    assert split[0].detail == {"pieces": [0, 1, 2], "kept": False}
    assert all(e.detail.get("from") == [1] for e in m.events if e.kind == "appeared")


def test_quiet_and_tie_break_are_deterministic():
    m = RS.match_stories({3: {"q1", "q2", "q3"}, 4: set()}, [["n1", "n2", "n3"]])
    assert m.story_of == [None] and m.closed == {3: "quiet", 4: "quiet"}
    # поровну на две группы: id остаётся у первой, вторая — откол
    old = {5: {"a", "b", "c", "d"}}
    m = RS.match_stories(old, [["c", "d", "e"], ["a", "b", "f"]])
    assert m.story_of == [5, None]
    assert m.events[-1].kind == "split" and m.events[-1].detail == {"pieces": [1], "kept": True}


# ── Название ────────────────────────────────────────────────────────────────

SUMS = [
    "Банк списал плату за подписку СберПрайм после того, как клиент её отключил",
    "После отключения подписки банк продолжает списывать за неё деньги",
    "Клиент отключил подписку, но списания продолжились",
    "Списание за подписку, которую клиент не подключал",
]


def test_name_prompt_contrasts_with_neighbours():
    p = RS.name_prompt(SUMS + [""] * 3, ["Отказ в возврате платы за подписку",
                                         "Отказ в возврате платы за подписку",
                                         "Списание комиссии за уведомления"])
    for s in SUMS:
        assert s in p
    assert p.count("Отказ в возврате платы за подписку") == 1          # соседи без повторов
    assert "отличаться" in p and "без сумм" in p and '"title"' in p and '"criterion"' in p
    assert "Соседние" not in RS.name_prompt(SUMS, [])
    many = RS.name_prompt([f"изложение {k}" for k in range(20)], [], max_summaries=5)
    assert "5. изложение 4" in many and "изложение 5" not in many


def test_validate_name():
    ok = RS.validate_name("Списание платы за подписку после отключения", SUMS)
    assert ok.ok and ok.support >= 0.75, ok
    assert RS.validate_name("«Списание за подписку после отключения.»", SUMS).ok
    bad = {
        "Списание 990 рублей за подписку": "число",
        "Списание тыс. руб. за подписку": "сумма",
        "Списание за подписку в Казани": "город",
        "Списание за подписку в Санкт-Петербурге": "город",
        "Списание за подписку в Кемеровской области": "город",
        "Банк продолжает списывать плату за подписку даже после её отключения клиентом":
            "длиннее",
        "Сбой начисления кэшбэка по картам": "изложений",
        "Подписка": "одно слово",
        "": "пустое",
    }
    for name, why in bad.items():
        r = RS.validate_name(name, SUMS)
        assert not r.ok and why in r.why, (name, r)
    for place in ("Московском регионе", "Краснодарском крае", "Республике Татарстан",
                  "Тульской области", "Омской области"):
        name = f"Списание за подписку в {place}"
        r = RS.validate_name(name, SUMS)
        assert not r.ok and "город" in r.why, (name, r)
    # «город», «область» сами по себе — не география: «в другом городе» законно
    for name in ("Отказ выдать карту в другом городе", "Подписка оформлена в другой области",
                 "Крайний срок отключения подписки"):
        assert "город" not in RS.validate_name(name, SUMS).why, name
    # город из жалоб сюжета, которого нет в общем списке
    r = RS.validate_name("Списание за подписку в Верхней Пышме", SUMS, cities=["Верхняя Пышма"])
    assert not r.ok and "пышме" in r.why
    # «нижний предел» — не город, хотя «Нижний Тагил» среди городов жалоб
    assert "город" not in RS.validate_name("Нижний предел списания за подписку", SUMS,
                                           cities=["Нижний Тагил"]).why
    # повтор соседа
    r = RS.validate_name("Списание платы за подписку после отключения", SUMS,
                         neighbours=["списание платы за подписку после отключения"])
    assert not r.ok and "соседн" in r.why
    # валюта сама по себе не сумма; «банкомат» — значимое слово, не «банк»
    sums = ["Банкомат не вернул доллары при конвертации", "Банкомат удержал доллары",
            "При конвертации долларов банкомат выдал меньше"]
    assert RS.validate_name("Банкомат удерживает доллары при конвертации", sums).ok


def test_stem_and_clean_name():
    assert RS._stem("навязанная") == RS._stem("навязали")[:5] == "навяз"
    assert RS._stem("карта") == RS._stem("карту") == "карт"
    assert RS._stem("блокировка") in "заблокировали"
    assert RS.clean_name('  "списание   за подписку." ') == "Списание за подписку"


# ── «Новое» ─────────────────────────────────────────────────────────────────

def test_is_new():
    sp = Space()
    st = sp.story("A", 5, 0.9)
    other = sp.story("B", 5, 0.9)
    V = sp.matrix(st + other)
    story = {"first_seen": NOW - timedelta(days=3), "vectors": V[:5]}
    far = [(NOW - timedelta(days=60), v) for v in V[5:]]               # история про другое
    assert RS.is_new(story, far, now=NOW)
    assert RS.is_new(story, [], now=NOW)                               # истории нет — новое
    same = [(NOW - timedelta(days=60), V[0]), (NOW - timedelta(days=90), V[1])]
    assert not RS.is_new(story, far + same, now=NOW)                   # уже было
    assert RS.is_new(story, far + same[:1], now=NOW)                   # один сосед — не «было»
    assert RS.is_new(story, far + same, now=NOW, min_hits=3)
    # соседи старше окна истории не считаются; голые векторы — без фильтра дат
    old = [(NOW - timedelta(days=400), V[0]), (NOW - timedelta(days=500), V[1])]
    assert RS.is_new(story, far + old, now=NOW)
    assert not RS.is_new(story, list(V[:2]), now=NOW)
    # моложе 14 дней — обязательно
    assert not RS.is_new({**story, "first_seen": NOW - timedelta(days=20)}, far, now=NOW)
    assert RS.is_new({**story, "first_seen": NOW - timedelta(days=20)}, far, now=NOW,
                     young_days=30)
    # не с чем сравнить — не «новое»
    assert not RS.is_new({"first_seen": NOW, "vectors": []}, far, now=NOW)
    assert not RS.is_new({"vectors": V[:5]}, far, now=NOW)
    # наивные даты (SQLite) — как UTC; готовый центр
    naive = {"first_seen": (NOW - timedelta(days=3)).replace(tzinfo=None),
             "centroid": V[:5].mean(axis=0)}
    assert RS.is_new(naive, far, now=NOW)
    # векторы другой размерности (другая модель) не сравниваются, а не роняют проверку
    assert RS.is_new(story, far + [(NOW - timedelta(days=60), np.ones(7))] * 3, now=NOW)


def test_history_hits_counts_by_window_across_chunks():
    """История считается порциями по 1000: счёт не зависит от границы порций,
    у каждого сюжета — своё окно дат."""
    rng = np.random.default_rng(5)
    C = rng.normal(size=(2, 16)).astype(np.float32)
    C /= np.linalg.norm(C, axis=1, keepdims=True)
    f1, f2 = NOW - timedelta(days=2), NOW - timedelta(days=50)
    hist = []
    for k in range(2500):
        dt = NOW - timedelta(days=1 + k % 300)
        hist.append((dt, C[k % 2] + rng.normal(scale=0.01, size=16)))
    hits = RS._history_hits(C, [f1, f2], iter(hist), 180, 0.9)
    want = [sum(1 for k, (dt, _v) in enumerate(hist)
                if k % 2 == j and f - timedelta(days=180) <= dt < f)
            for j, f in enumerate((f1, f2))]
    assert list(hits) == want and min(want) > 0


def test_vec_parsing():
    assert RS._vec([1.0, 2.0]) == [1.0, 2.0]
    assert RS._vec("[1, 2.5]") == [1.0, 2.5]
    assert RS._vec("{1,2.5,-3}") == [1.0, 2.5, -3.0]                 # текстовый вид real[]
    assert RS._vec("мусор") is None and RS._vec(None) is None


# ── Миграция и пакетный проход на SQLite ────────────────────────────────────

MIG = ROOT / "migrations" / "104_review_story.sql"


def test_migration_shape():
    sql = MIG.read_text(encoding="utf-8")
    assert "SET lock_timeout = '3s'" in sql
    assert "%" not in sql                      # exec_driver_sql: процент — плейсхолдер
    for t in ("review_story", "review_story_member", "review_story_event"):
        assert f"CREATE TABLE IF NOT EXISTS {t} (" in sql
    for col in ("story_id    bigserial", "bank", "title", "criterion", "first_seen",
                "last_seen", "n ", "issue_main", "status", "created_at", "updated_at"):
        assert col in sql
    assert "PRIMARY KEY (story_id, url)" in sql and "detail   jsonb" in sql
    for kind in RS.KINDS:
        assert kind in sql


def _sqlite_ddl() -> str:
    """Миграция 104 → диалект SQLite: код проверяется на той же схеме."""
    sql = MIG.read_text(encoding="utf-8")
    sql = re.sub(r"(?m)^SET .*$", "", sql)
    sql = sql.replace("bigserial   PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = re.sub(r"\btimestamptz\b", "TIMESTAMP", sql)
    sql = re.sub(r"\bjsonb\b", "TEXT", sql)
    return sql.replace("DEFAULT now()", "DEFAULT CURRENT_TIMESTAMP")


def _ts(dt: datetime) -> str:
    """Дата так, как её пишет SQLAlchemy в SQLite: сравнения строк = сравнения дат."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def _al_product_is(product, slices, want):
    """al_product_is (migrations/102) для SQLite: продукт или срез; срезы — JSON-список."""
    tags = json.loads(slices) if slices else []
    by_slice = {"Зарплатные проекты": "payroll", "УС — банкоматы и терминалы": "us"}
    return int(product == want or (by_slice.get(want) in tags))


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    eng = create_engine(f"sqlite:///{tmp_path / 'stories.db'}", future=True)
    event.listen(eng, "connect",
                 lambda c, _r: c.create_function("al_product_is", 3, _al_product_is))
    raw = eng.raw_connection()
    try:
        raw.driver_connection.executescript(_sqlite_ddl() + """
            CREATE TABLE review_index (url TEXT PRIMARY KEY, bank TEXT, kind TEXT, issue TEXT,
                                       dt TIMESTAMP, city TEXT, dup_of TEXT, product TEXT,
                                       slices TEXT);
            CREATE TABLE review_summary_vec (url TEXT PRIMARY KEY, vec TEXT NOT NULL);
            CREATE TABLE review_annotation (url TEXT, schema_version TEXT, summary TEXT,
                                            PRIMARY KEY (url, schema_version));
        """)
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))

    def put(rows):
        """rows: (url, bank, kind, issue, dt, vec, summary, city)."""
        from bank_audit.rag import reviews_dash as rd
        with eng.begin() as c:
            for url, bank, kind, issue, dt, vec, summary, city in rows:
                c.execute(text("INSERT OR REPLACE INTO review_index (url, bank, kind, issue, dt, city) "
                               "VALUES (:u, :b, :k, :i, :d, :c)"),
                          {"u": url, "b": bank, "k": kind, "i": issue, "d": _ts(dt), "c": city})
                if vec is not None:
                    c.execute(text("INSERT OR REPLACE INTO review_summary_vec VALUES (:u, :v)"),
                              {"u": url, "v": json.dumps([float(x) for x in vec])})
                c.execute(text("INSERT OR REPLACE INTO review_annotation VALUES (:u, :s, :t)"),
                          {"u": url, "s": rd._ann_schema(), "t": summary})

    def q(sql, **p):
        with eng.connect() as c:
            return c.execute(text(sql), p).all()

    yield put, q, eng
    eng.dispose()


BANK = "Банк Тест"
SUM_A = "Банк списал плату за подписку после того, как клиент её отключил"
SUM_B = "Счёт заблокирован по 115-ФЗ, банк не объясняет причину блокировки"


def _world():
    sp = Space()
    a = sp.story("A", 8, 0.9)
    b = sp.story("B", 5, 0.88)
    noise = [sp.add(f"z{k}", **{f"z{k}": 1.0}) for k in range(6)]
    return sp, a, b, noise


def _rows(sp, ids, issue, day0, summary, bank=BANK, kind="complaint", city=None):
    X = sp.matrix(ids)
    return [(f"https://r/{i}", bank, kind, issue, NOW - timedelta(days=day0 + k), X[k],
             summary, city) for k, i in enumerate(ids)]


def test_run_writes_and_keeps_story_ids(lite):
    put, q, _eng = lite
    sp, a, b, noise = _world()
    put(_rows(sp, a[:6], "fees", 1, SUM_A, city="Казань")
        + _rows(sp, b, "block_115", 2, SUM_B)
        + _rows(sp, noise, "staff", 3, "Грубость сотрудника в отделении"))
    # чужое не попадает: другой банк, похвала, старше окна
    sp.add("XB", A=1.0)
    sp.add("XP", A=1.0)
    sp.add("XO", A=1.0)
    put(_rows(sp, ["XB"], "fees", 1, SUM_A, bank="Другой банк")
        + _rows(sp, ["XP"], "fees", 1, SUM_A, kind="praise")
        + _rows(sp, ["XO"], "fees", 200, SUM_A))

    st = RS.run(BANK, 90, now=NOW)
    assert st["items"] == 6 + 5 + 6 and st["clusters"] == 2
    assert st["appeared"] == 2 and st["grew"] == 0
    rows = q("SELECT story_id, n, issue_main, status, title, medoid_url, first_seen, last_seen "
             "FROM review_story ORDER BY n DESC")
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [(6, "fees", "active", None),
                                                        (5, "block_115", "active", None)]
    sid_a, sid_b = rows[0][0], rows[1][0]
    assert rows[0][5].startswith("https://r/A")
    assert rows[0][6] == _ts(NOW - timedelta(days=6)) and rows[0][7] == _ts(NOW - timedelta(days=1))
    mem = {r[0] for r in q("SELECT url FROM review_story_member WHERE story_id = :s", s=sid_a)}
    assert mem == {f"https://r/{i}" for i in a[:6]}
    ev = q("SELECT story_id, kind, detail FROM review_story_event ORDER BY event_id")
    assert [(r[0], r[1]) for r in ev] == [(sid_a, "appeared"), (sid_b, "appeared")]
    assert json.loads(ev[0][2]) == {"n": 6}

    # повтор того же прогона — ничего нового
    st = RS.run(BANK, 90, now=NOW)
    assert st["appeared"] == st["grew"] == st["quiet"] == 0
    assert len(q("SELECT * FROM review_story")) == 2
    assert len(q("SELECT * FROM review_story_event")) == 2

    # день спустя: две новые жалобы в A — тот же id, событие «вырос»
    later = NOW + timedelta(days=1)
    X = sp.matrix(a)
    put([(f"https://r/{a[k]}", BANK, "complaint", "fees", later - timedelta(hours=k - 6), X[k],
          SUM_A, None) for k in (6, 7)])
    st = RS.run(BANK, 90, now=later)
    assert st["grew"] == 1 and st["appeared"] == 0 and st["stories"] == sorted([sid_a, sid_b])
    n, last = q("SELECT n, last_seen FROM review_story WHERE story_id = :s", s=sid_a)[0]
    assert n == 8 and last == _ts(later)
    grew = q("SELECT detail FROM review_story_event WHERE kind = 'grew'")
    assert json.loads(grew[0][0]) == {"new": 2, "n": 8}

    # векторы B пропали — B затих; вернулись — тот же id, «вернулся»
    _eng.dispose()
    with _eng.begin() as c:
        from sqlalchemy import text
        saved = c.execute(text("SELECT url, vec FROM review_summary_vec "
                               "WHERE url LIKE '%/B%'")).all()
        c.execute(text("DELETE FROM review_summary_vec WHERE url LIKE '%/B%'"))
    st = RS.run(BANK, 90, now=later)
    assert st["quiet"] == 1
    assert q("SELECT status FROM review_story WHERE story_id = :s", s=sid_b)[0][0] == "quiet"
    st = RS.run(BANK, 90, now=later)
    assert st["quiet"] == 0                                    # «затих» — один раз
    with _eng.begin() as c:
        c.execute(text("INSERT INTO review_summary_vec VALUES (:u, :v)"),
                  [{"u": u, "v": v} for u, v in saved])
    st = RS.run(BANK, 90, now=later)
    assert st["returned"] == 1 and st["appeared"] == 0
    assert q("SELECT status FROM review_story WHERE story_id = :s", s=sid_b)[0][0] == "active"
    kinds = [r[0] for r in q("SELECT kind FROM review_story_event WHERE story_id = :s "
                             "ORDER BY event_id", s=sid_b)]
    assert kinds == ["appeared", "quiet", "returned"]

    # окно ушло целиком — выборка пуста, сюжеты не трогаем
    st = RS.run(BANK, 90, now=NOW + timedelta(days=400))
    assert st.get("skipped") and st["items"] == 0
    assert {r[0] for r in q("SELECT status FROM review_story")} == {"active"}


def test_run_moves_member_between_stories(lite):
    """Жалоба внутри окна — ровно в одном действующем сюжете: перешла в другую
    группу — уходит из прежнего состава."""
    put, q, _eng = lite
    sp, a, b, _noise = _world()
    put(_rows(sp, a[:6], "fees", 1, SUM_A) + _rows(sp, b, "block_115", 2, SUM_B))
    RS.run(BANK, 90, now=NOW)
    sid_a = q("SELECT story_id FROM review_story WHERE issue_main = 'fees'")[0][0]
    moved = a[5]
    # теперь жалоба — копия B
    Xb = sp.matrix(b)
    put([(f"https://r/{moved}", BANK, "complaint", "block_115", NOW - timedelta(days=1), Xb[0],
          SUM_B, None)])
    RS.run(BANK, 90, now=NOW)
    homes = q("SELECT m.story_id FROM review_story_member m JOIN review_story r "
              "ON r.story_id = m.story_id WHERE m.url = :u AND r.status = 'active'",
              u=f"https://r/{moved}")
    assert len(homes) == 1 and homes[0][0] != sid_a
    assert q("SELECT n FROM review_story WHERE story_id = :s", s=sid_a)[0][0] == 5


def test_run_all_and_naming(lite):
    put, q, _eng = lite
    sp, a, b, noise = _world()
    put(_rows(sp, a[:6], "fees", 1, SUM_A, city="Казань") + _rows(sp, b, "block_115", 2, SUM_B))
    sp.add("solo", solo=1.0)
    put(_rows(sp, ["solo"], "fees", 1, SUM_A, bank="Маленький банк"))
    out = RS.run_all(90, min_items=3, now=NOW)
    assert [r["bank"] for r in out] == [BANK]                 # у маленького меньше min_items

    todo = RS.to_name(BANK)
    assert [t["n"] for t in todo] == [6, 5]
    first = todo[0]
    assert first["summaries"] and all(s == SUM_A for s in first["summaries"])
    assert first["cities"] == ["Казань"] and SUM_A in first["prompt"]
    assert first["neighbours"] == []                          # назван пока никто

    bad = RS.save_name(first["story_id"], "Списание за подписку в Казани", now=NOW)
    assert not bad.ok
    assert q("SELECT title FROM review_story WHERE story_id = :s",
             s=first["story_id"])[0][0] is None
    good = RS.save_name(first["story_id"], "«Списание платы за подписку после отключения.»",
                        "Деньги за подписку списаны после её отключения клиентом", now=NOW)
    assert good.ok
    title, crit = q("SELECT title, criterion FROM review_story WHERE story_id = :s",
                    s=first["story_id"])[0]
    assert title == "Списание платы за подписку после отключения" and crit.startswith("Деньги")
    ev = q("SELECT detail FROM review_story_event WHERE kind = 'named'")
    assert json.loads(ev[0][0])["title"] == title

    # у второго сюжета первый уже сосед: задание с контрастом
    todo = RS.to_name(BANK)
    assert len(todo) == 1 and todo[0]["neighbours"] == [title]
    assert title in todo[0]["prompt"]
    assert not RS.save_name(10 ** 6, "Что угодно про подписку").ok


def test_new_stories(lite):
    put, q, _eng = lite
    sp, a, b, _noise = _world()
    put(_rows(sp, a[:6], "fees", 1, SUM_A) + _rows(sp, b, "block_115", 2, SUM_B))
    # у A есть история: похожие жалобы 100–101 день назад (вне окна прогона, в окне истории)
    hist = sp.story("A", 2, 0.9, start=20)
    put(_rows(sp, hist, "fees", 100, SUM_A))
    RS.run(BANK, 90, now=NOW)
    ids = dict(q("SELECT issue_main, story_id FROM review_story"))
    assert RS.new_stories(BANK, now=NOW) == [ids["block_115"]]
    assert RS.new_stories(BANK, now=NOW, min_hits=3) == sorted(ids.values())
    assert RS.new_stories(BANK, now=NOW + timedelta(days=30)) == []    # уже не молоды


def test_tick_runs_once_per_period(monkeypatch):
    calls = []
    state = {}

    class R:
        def __init__(self, v): self.v = v
        def first(self): return self.v

    class Sess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, p=None):
            if "SELECT v FROM review_index_state" in str(sql):
                return R((state["v"],) if "v" in state else None)
            state["v"] = p["v"]
    monkeypatch.setattr(RS.db, "session", lambda: Sess())
    named = []

    def run_all():
        calls.append(1)
        assert len(named) == len(calls) - 1                         # названия — после прохода
        return [{"bank": "Б"}]
    monkeypatch.setattr(RS, "run_all", run_all)
    monkeypatch.setattr(RS, "name_stories",
                        lambda limit: named.append(limit) or {"todo": 3, "named": 2})
    monkeypatch.delenv("REVIEW_STORIES_NAME_LIMIT", raising=False)
    assert RS.tick(now=1_000_000.0) == {"banks": 1, "errors": 0, "named": 2}
    assert named == [60] and state["v"] == "1000000"               # отметка уже записана
    assert RS.tick(now=1_000_000.0 + 3600)["skipped"] == "not_due" and len(calls) == 1
    assert named == [60]                                            # не по сроку — и не называем
    monkeypatch.setenv("REVIEW_STORIES_NAME_LIMIT", "7")
    assert RS.tick(now=1_000_000.0 + RS.TICK_EVERY_S + 1)["banks"] == 1 and len(calls) == 2
    assert named == [60, 7]
    # опечатка в env не роняет цикл — берётся значение по умолчанию
    monkeypatch.setenv("REVIEW_STORIES_NAME_LIMIT", "шестьдесят")
    RS.tick(now=1_000_000.0 + 2 * RS.TICK_EVERY_S + 2)
    assert named == [60, 7, 60]


# ── Шаг названий с моделью ──────────────────────────────────────────────────

SUM_C = "Кэшбэк за покупки в супермаркетах не начислен по карте"
SUM_D = "Перевод по СБП завис, деньги не пришли получателю"
_BY_SUM = {"A": SUM_A, "B": SUM_B, "C": SUM_C, "D": SUM_D}
TITLE_A = "Списание платы за подписку после отключения"
TITLE_B = "Блокировка счёта без объяснения причины"
TITLE_C = "Кэшбэк за покупки не начислен по карте"
TITLE_D = "Перевод по СБП завис и не пришёл получателю"


def _four(put):
    """Четыре сюжета банка: A — 6 жалоб, B — 5, C — 4, D — 3; у каждого свои изложения."""
    sp = Space()
    ids = {"A": sp.story("A", 6, 0.9), "B": sp.story("B", 5, 0.88),
           "C": sp.story("C", 4, 0.9), "D": sp.story("D", 3, 0.9)}
    issue = {"A": "fees", "B": "block_115", "C": "cashback", "D": "transfer"}
    for day, (key, pts) in enumerate(ids.items(), 1):
        put(_rows(sp, pts, issue[key], day, _BY_SUM[key], city="Казань" if key == "A" else None))
    RS.run(BANK, 90, now=NOW)
    return sp, ids


class FakeModel:
    """Модель по сценарию: ответы для каждого сюжета по очереди (узнаёт сюжет по
    изложению в задании); исключение в сценарии — бросается, как сбой связи."""

    def __init__(self, script: dict):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list]] = []

    def __call__(self, messages):
        key = next(k for k, s in _BY_SUM.items() if s in messages[0]["content"])
        self.calls.append((key, [dict(m) for m in messages]))
        r = self.script[key].pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)


def _ans(title, crit="Жалоба в сюжете, если это та же история"):
    return {"title": title, "criterion": crit}


def test_name_stories_with_fake_model(lite):
    put, q, _eng = lite
    _four(put)
    sid = {r[0]: r[1] for r in q("SELECT issue_main, story_id FROM review_story")}
    fake = FakeModel({
        "A": [_ans(f"«{TITLE_A}.»", "  жалоба в сюжете, если плату за подписку списали  "
                                    "после отключения ")],
        "B": [_ans(TITLE_A), _ans(TITLE_B)],                # повтор соседа → переспрос
        "C": ["не JSON вовсе", '{"name": "Кэшбэк"}'],        # дважды мимо — отложить
        "D": [ConnectionError("нет связи")],                # сбой связи — не вина сюжета
    })
    st = RS.name_stories(10, caller=fake, now=NOW)
    assert {k: st[k] for k in ("todo", "named", "retried", "invalid", "duplicate", "rejected",
                               "failed", "skipped")} == {
        "todo": 4, "named": 2, "retried": 1, "invalid": 1, "duplicate": 0, "rejected": 0,
        "failed": 1, "skipped": 0}
    assert "error" not in st and "stop" not in st
    titles = dict(q("SELECT story_id, title FROM review_story"))
    assert titles[sid["fees"]] == TITLE_A and titles[sid["block_115"]] == TITLE_B
    assert titles[sid["cashback"]] is None and titles[sid["transfer"]] is None
    crit = q("SELECT criterion FROM review_story WHERE story_id = :s", s=sid["fees"])[0][0]
    assert crit == "Жалоба в сюжете, если плату за подписку списали после отключения"
    # крупные первыми; у B в задании уже есть название A, названное минуту назад
    assert [k for k, _m in fake.calls] == ["A", "B", "B", "C", "C", "D"]
    b1, b2 = [m for k, m in fake.calls if k == "B"]
    assert TITLE_A in b1[0]["content"] and "Соседние" in b1[0]["content"]
    assert len(b2) == 3 and b2[1]["role"] == "assistant" and TITLE_A in b2[1]["content"]
    assert "совпадает с соседним" in b2[2]["content"]
    assert "не JSON" in [m for k, m in fake.calls if k == "C"][1][2]["content"]
    # служебная отметка — только у C: сбой связи сюжет не откладывает
    failed = q("SELECT story_id, detail FROM review_story_event WHERE kind = 'name_failed'")
    assert [r[0] for r in failed] == [sid["cashback"]]
    assert json.loads(failed[0][1])["kind"] == "invalid"
    assert len(q("SELECT * FROM review_story_event WHERE kind = 'named'")) == 2

    # назавтра: C отложен на неделю, D — в очереди
    fake2 = FakeModel({"D": [_ans(TITLE_D)]})
    st = RS.name_stories(10, caller=fake2, now=NOW + timedelta(days=1))
    assert st["todo"] == 1 and st["named"] == 1 and [k for k, _m in fake2.calls] == ["D"]
    # через неделю C спрашивают снова — состав и соседи могли измениться
    fake3 = FakeModel({"C": [_ans(TITLE_C)]})
    st = RS.name_stories(10, caller=fake3, now=NOW + timedelta(days=8))
    assert st["named"] == 1
    assert dict(q("SELECT story_id, title FROM review_story"))[sid["cashback"]] == TITLE_C
    # безымянных не осталось — модель не зовётся вовсе
    assert RS.name_stories(10, caller=FakeModel({}), now=NOW + timedelta(days=9))["todo"] == 0


def test_name_stories_rejects_and_counts(lite):
    put, q, _eng = lite
    _four(put)
    sid = {r[0]: r[1] for r in q("SELECT issue_main, story_id FROM review_story")}
    fake = FakeModel({
        "A": [_ans(TITLE_A)],
        # повтор названия A дважды — не записан; город — не годится и со второго раза
        "B": [_ans(TITLE_A), _ans("Списание платы за подписку после её отключения")],
        "C": [_ans("Кэшбэк по карте не начислен в Казани"), _ans("Кэшбэк не начислен в Самаре")],
        "D": [_ans("Перевод")] * 2,
    })
    st = RS.name_stories(10, caller=fake, now=NOW)
    assert (st["named"], st["duplicate"], st["rejected"]) == (1, 1, 2)
    why = dict((r[0], json.loads(r[1])) for r in q(
        "SELECT story_id, detail FROM review_story_event WHERE kind = 'name_failed'"))
    assert why[sid["block_115"]]["kind"] == "duplicate" and "соседн" in why[sid["block_115"]]["why"]
    assert "город" in why[sid["cashback"]]["why"] and "одно слово" in why[sid["transfer"]]["why"]
    assert why[sid["cashback"]]["title"] == "Кэшбэк не начислен в Самаре"
    # не годится — не пишется
    assert q("SELECT count(*) FROM review_story WHERE title IS NOT NULL")[0][0] == 1


def test_name_stories_stops_when_model_is_down(lite):
    put, q, _eng = lite
    _four(put)
    down = FakeModel({k: [TimeoutError("таймаут")] for k in "ABCD"})
    st = RS.name_stories(10, caller=down, now=NOW)
    assert st["failed"] == RS.NAME_MAX_FAILS == 3 and st["stop"] == "модель не отвечает"
    assert len(down.calls) == 3 and st["named"] == 0
    assert not q("SELECT * FROM review_story_event WHERE kind = 'name_failed'")
    # бюджет времени вышел — тоже остановка, а не исключение
    st = RS.name_stories(10, caller=FakeModel({}), now=NOW, budget_s=-1)
    assert st["stop"] == "время вышло" and st["named"] == 0


def test_name_stories_defers_rejected_requests(lite):
    # 4xx от провайдера повторится и завтра: сюжет откладывается, шаг идёт дальше,
    # а не стоит каждый день на трёх «неназываемых» (ревью 04.11.2026)
    put, q, _eng = lite
    _four(put)

    class Rejected(Exception):
        status_code = 400

    fake = FakeModel({k: [Rejected("фильтр содержания")] for k in "ABC"}
                     | {"D": [_ans("Перевод по СБП завис"), _ans("Перевод по СБП не дошёл получателю")]})
    st = RS.name_stories(10, caller=fake, now=NOW)
    assert st["failed"] == 3 and st["named"] == 1 and "stop" not in st, repr(st)
    assert q("SELECT count(*) FROM review_story_event WHERE kind = 'name_failed'")[0][0] == 3
    assert not RS._rejected_request(TimeoutError()) and not RS._rejected_request(
        type("E", (Exception,), {"status_code": 429})())


def test_name_stories_without_key_or_summaries(lite, monkeypatch):
    put, q, eng = lite
    _four(put)
    from bank_audit.ai import analyst
    monkeypatch.setattr(analyst, "LLM_API_KEY", "")
    st = RS.name_stories(10, now=NOW)                      # модель по умолчанию, ключа нет
    assert st["todo"] == 4 and st["stop"] == "нет ключа модели" and st["named"] == 0
    # у сюжета нет изложений — называть не по чему: пропуск с отсрочкой
    from sqlalchemy import text
    with eng.begin() as c:
        c.execute(text("DELETE FROM review_annotation WHERE url LIKE '%/D%'"))
    st = RS.name_stories(10, caller=FakeModel({k: [_ans(t)] for k, t in (
        ("A", TITLE_A), ("B", TITLE_B), ("C", TITLE_C))}), now=NOW)
    assert st["named"] == 3 and st["skipped"] == 1
    assert len(q("SELECT * FROM review_story_event WHERE kind = 'name_failed'")) == 1


def test_name_stories_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("база недоступна")
    monkeypatch.setattr(RS.db, "session", broken)
    st = RS.name_stories(5, caller=FakeModel({}), now=NOW)
    assert st["error"].startswith("RuntimeError") and st["named"] == 0


def test_parse_name():
    assert RS._parse_name('{"title": " «Списание за подписку.» ", "criterion": "жалоба в '
                          'сюжете, если списали"}') == ("Списание за подписку",
                                                        "Жалоба в сюжете, если списали")
    # JSON внутри текста; без признака — None; признак-простыня — не фраза
    assert RS._parse_name('Вот ответ: {"title": "Списание за подписку"} — готово') == (
        "Списание за подписку", None)
    assert RS._parse_name(json.dumps({"title": "Списание за подписку", "criterion": "x" * 301})
                          ) == ("Списание за подписку", None)
    for bad in ("", "   ", "не JSON", "[1, 2]", '{"title": ""}', '{"title": 5}',
                '{"name": "x"}', None):
        assert RS._parse_name(bad) is None, bad


def test_default_caller_settings(monkeypatch):
    import types

    import openai

    from bank_audit.ai import analyst
    seen: dict = {}

    class Client:
        def __init__(self, **kw):
            seen["client"] = kw
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

        def create(self, **kw):
            seen["create"] = kw
            msg = types.SimpleNamespace(content='{"title": "Списание за подписку"}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    monkeypatch.setattr(openai, "OpenAI", Client)
    monkeypatch.setattr(analyst, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(analyst, "LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.delenv("REVIEW_STORIES_NAME_MODEL", raising=False)
    call = RS._default_caller()
    msgs = [{"role": "user", "content": "задание"}]
    assert call(msgs) == '{"title": "Списание за подписку"}'
    assert seen["client"] == {"base_url": "http://llm.test/v1", "api_key": "test-key",
                              "timeout": 20.0, "max_retries": 0}
    c = seen["create"]
    assert (c["model"], c["temperature"], c["response_format"], c["messages"]) == (
        "google/gemini-3.1-flash-lite", 0, {"type": "json_object"}, msgs)
    monkeypatch.setenv("REVIEW_STORIES_NAME_MODEL", "other/light-model")
    RS._default_caller()(msgs)
    assert seen["create"]["model"] == "other/light-model"
    monkeypatch.setattr(analyst, "LLM_API_KEY", "")
    assert RS._default_caller() is None


# ── Чтение для вкладки ──────────────────────────────────────────────────────

def _set(eng, url, **cols):
    from sqlalchemy import text
    with eng.begin() as c:
        c.execute(text(f"UPDATE review_index SET {', '.join(f'{k} = :{k}' for k in cols)} "
                       "WHERE url = :u"), {**cols, "u": url})


def _event(eng, sid, kind, at, detail):
    from sqlalchemy import text
    with eng.begin() as c:
        c.execute(text("INSERT INTO review_story_event (story_id, kind, at, detail) "
                       "VALUES (:s, :k, :a, :d)"),
                  {"s": sid, "k": kind, "a": _ts(at), "d": json.dumps(detail, ensure_ascii=False)})


def _timeline(put, q, eng):
    """Сюжеты A и B появились 20 дней назад; сегодня A вырос на две жалобы и
    появился C. Потом A[0] переразметили в похвалу, B[0] оказался копией жалобы
    с другой площадки, у C разные продукты."""
    sp = Space()
    a, b, c = sp.story("A", 8, 0.9), sp.story("B", 5, 0.88), sp.story("C", 4, 0.9)
    t0 = NOW - timedelta(days=20)
    Xa, Xb, Xc = sp.matrix(a), sp.matrix(b), sp.matrix(c)
    put([(f"https://r/{a[k]}", BANK, "complaint", "fees", t0 - timedelta(days=1 + k), Xa[k],
          SUM_A, None) for k in range(6)]
        + [(f"https://r/{b[k]}", BANK, "complaint", "block_115", t0 - timedelta(days=2 + k),
            Xb[k], SUM_B, None) for k in range(5)])
    RS.run(BANK, 90, now=t0)
    put([(f"https://r/{a[6]}", BANK, "complaint", "fees", NOW - timedelta(days=1), Xa[6],
          SUM_A, None),
         (f"https://r/{a[7]}", BANK, "complaint", "fees", NOW - timedelta(days=2), Xa[7],
          SUM_A, None)]
        + [(f"https://r/{c[k]}", BANK, "complaint", "cashback", NOW - timedelta(days=3 + k),
            Xc[k], SUM_C, None) for k in range(4)])
    st = RS.run(BANK, 90, now=NOW)
    assert (st["grew"], st["appeared"]) == (1, 1)
    sid = {r[0]: r[1] for r in q("SELECT issue_main, story_id FROM review_story")}
    _set(eng, f"https://r/{a[0]}", kind="praise")
    put([("https://r/canon", "Другой банк", "complaint", "block_115", NOW - timedelta(days=30),
          None, SUM_B, None)])
    _set(eng, f"https://r/{b[0]}", dup_of="https://r/canon")
    for k, (prod, sl) in enumerate([("Дебетовые карты", None), ("Дебетовые карты", None),
                                    ("Прочее", '["payroll"]'), (None, None)]):
        _set(eng, f"https://r/{c[k]}", product=prod, slices=sl)
    return sid, a, b, c


def test_list_stories(lite):
    put, q, eng = lite
    sid, a, b, c = _timeline(put, q, eng)
    assert RS.save_name(sid["fees"], TITLE_A, "Жалоба в сюжете, если списали за подписку",
                        now=NOW).ok
    got = RS.list_stories(BANK, now=NOW)
    assert [x["story_id"] for x in got] == [sid["fees"], sid["cashback"], sid["block_115"]]
    xa, xc, xb = got
    assert set(xa) == {"story_id", "title", "named", "criterion", "summary", "n", "n_window",
                       "first_seen", "last_seen", "issue_main", "issue_label", "risk", "status",
                       "badges", "top_urls", "urls"}
    # A: похвала не в счёт; вырос за две недели — с 5 до 7 по составу
    assert (xa["n"], xa["n_window"], xa["title"], xa["named"]) == (7, 7, TITLE_A, True)
    assert xa["criterion"].startswith("Жалоба в сюжете")
    assert xa["badges"] == [{"kind": "grew", "label": "вырос с 5 до 7", "from": 5, "to": 7}]
    assert f"https://r/{a[0]}" not in xa["urls"] + xa["top_urls"]
    assert xa["urls"][:2] == [f"https://r/{a[6]}", f"https://r/{a[7]}"]      # свежие первыми
    assert xa["last_seen"] == (NOW - timedelta(days=1)).isoformat()
    assert xa["first_seen"] == (NOW - timedelta(days=26)).isoformat()
    assert xa["status"] == "active" and xa["issue_main"] == "fees"
    # C: новый, без названия — заглушка из изложения медоида; медоид ближе всех к себе
    assert (xc["n"], xc["named"], xc["title"], xc["summary"]) == (4, False, SUM_C, SUM_C)
    assert xc["badges"] == [{"kind": "appeared", "label": "новый сюжет"}]
    medoid = q("SELECT medoid_url FROM review_story WHERE story_id = :s", s=sid["cashback"])[0][0]
    assert xc["top_urls"][0] == medoid and len(xc["top_urls"]) == 3
    # B: копия с другой площадки не в счёт; событий за две недели нет
    assert (xb["n"], xb["badges"]) == (4, [])
    assert f"https://r/{b[0]}" not in xb["urls"]

    # окно 10 дней: у A в окне две жалобы, B выпадает целиком
    got = RS.list_stories(BANK, days=10, now=NOW)
    assert [(x["story_id"], x["n"], x["n_window"]) for x in got] == [
        (sid["cashback"], 4, 4), (sid["fees"], 7, 2)]
    assert got[1]["urls"] == [f"https://r/{a[6]}", f"https://r/{a[7]}"]
    assert [x["story_id"] for x in RS.list_stories(BANK, now=NOW, limit=1)] == [sid["fees"]]
    assert RS.list_stories("Другой банк", now=NOW) == []

    # продукт: считаются только жалобы продукта или среза
    deb = RS.list_stories(BANK, product="Дебетовые карты", now=NOW)
    assert [(x["story_id"], x["n"], x["n_window"]) for x in deb] == [(sid["cashback"], 2, 2)]
    assert set(deb[0]["urls"]) == {f"https://r/{c[0]}", f"https://r/{c[1]}"}
    pay = RS.list_stories(BANK, product="Зарплатные проекты", now=NOW)
    assert [(x["story_id"], x["n"]) for x in pay] == [(sid["cashback"], 1)]
    assert pay[0]["urls"] == [f"https://r/{c[2]}"] and pay[0]["badges"][0]["kind"] == "appeared"
    assert RS.list_stories(BANK, product="Ипотека", now=NOW) == []

    # подпись проблемы — из кодификатора; нет изложений — заглушка названия из неё
    assert (xb["issue_label"], xb["risk"]) == ("Блокировки по 115-ФЗ", "compliance")
    from sqlalchemy import text
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM review_annotation WHERE url LIKE '%/B%'"))
    xb = next(x for x in RS.list_stories(BANK, now=NOW) if x["story_id"] == sid["block_115"])
    assert (xb["title"], xb["summary"], xb["named"]) == ("Блокировки по 115-ФЗ", None, False)


def test_list_stories_badges_and_quiet(lite):
    put, q, eng = lite
    sid, a, b, c = _timeline(put, q, eng)
    # слияние (у принявшего) и возвращение — из журнала за две недели; старше — нет
    _event(eng, sid["fees"], "merged", NOW - timedelta(days=3), {"from": [999]})
    _event(eng, sid["cashback"], "returned", NOW - timedelta(days=2), {"n": 4})
    _event(eng, sid["block_115"], "returned", NOW - timedelta(days=15), {"n": 5})
    _event(eng, sid["block_115"], "merged", NOW - timedelta(days=1), {"into": sid["fees"]})
    by = {x["story_id"]: x for x in RS.list_stories(BANK, now=NOW)}
    assert [x["label"] for x in by[sid["fees"]]["badges"]] == ["вырос с 5 до 7", "слился"]
    assert [x["label"] for x in by[sid["cashback"]]["badges"]] == ["новый сюжет", "вернулся"]
    assert by[sid["block_115"]]["badges"] == []
    # события позже «сейчас» не видны: list_stories на дату — как было тогда
    early = {x["story_id"]: x for x in RS.list_stories(BANK, now=NOW - timedelta(days=10))}
    assert sid["cashback"] not in early and early[sid["fees"]]["badges"] == [
        {"kind": "appeared", "label": "новый сюжет"}]
    # с продуктом рост не своего продукта значка не даёт
    assert RS.list_stories(BANK, product="Дебетовые карты", now=NOW)[0]["badges"][0]["kind"] \
        == "appeared"

    # векторы B пропали — B затих; в списке остаётся, пока последняя жалоба в окне
    from sqlalchemy import text
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM review_summary_vec WHERE url LIKE '%/B%'"))
    assert RS.run(BANK, 90, now=NOW)["quiet"] == 1
    xb = next(x for x in RS.list_stories(BANK, now=NOW) if x["story_id"] == sid["block_115"])
    assert xb["status"] == "quiet" and xb["n"] == 4
    assert sid["block_115"] not in {x["story_id"] for x in RS.list_stories(BANK, days=21, now=NOW)}


def test_story_events_labels(lite):
    put, q, eng = lite
    from sqlalchemy import text
    with eng.begin() as c:
        for title in ("Главный сюжет", "Списание за подписку", None, "Кусок"):
            c.execute(text("INSERT INTO review_story (bank, title, status) "
                           "VALUES (:b, :t, 'active')"), {"b": BANK, "t": title})
    s, x, y, z = [r[0] for r in q("SELECT story_id FROM review_story ORDER BY story_id")]
    evs = [("appeared", {"n": 3, "from": [x]}), ("grew", {"new": 2, "n": 5}),
           ("merged", {"from": [x, y]}), ("split", {"into": [z], "kept": True}),
           ("quiet", {"visible": 0}), ("returned", {"n": 21}),
           ("named", {"title": "Главный сюжет", "prev": None}),
           ("named", {"title": "Главный сюжет", "prev": "Старое"}),
           ("name_failed", {"why": "не JSON"}), ("merged", {"into": y}),
           ("split", {"into": [y, z], "kept": False}), ("appeared", {"n": 1})]
    for k, (kind, d) in enumerate(evs):
        _event(eng, s, kind, NOW - timedelta(days=len(evs) - k), d)
    got = RS.story_events(s)
    assert [e["kind"] for e in got] == [k for k, _d in reversed(evs) if k != "name_failed"]
    assert [e["label"] for e in reversed(got)] == [
        "Появился: 3 жалобы — откололся от сюжета «Списание за подписку»",
        "Вырос на 2 жалобы: в группе 5",
        f"К нему присоединились сюжеты «Списание за подписку», №{y}",
        "От него откололся сюжет «Кусок»",
        "Затих: похожие жалобы больше не складываются в группу",
        "Вернулся: в группе 21 жалоба",
        "Назван «Главный сюжет»",
        "Переименован: «Старое» → «Главный сюжет»",
        f"Влился в сюжет №{y}",
        f"Распался на сюжеты №{y}, «Кусок»",
        "Появился: 1 жалоба"]
    assert got[0]["at"] == (NOW - timedelta(days=1)).isoformat()
    assert got[0]["kind_label"] == "появился" and got[-1]["detail"] == {"n": 3, "from": [x]}
    assert RS.story_events(10 ** 6) == []
    assert len(RS.story_events(s, limit=2)) == 2


# ── Живой Postgres ──────────────────────────────────────────────────────────
# Запуск: пустая база и STORIES_PG_TEST_URL=postgresql+psycopg://user@127.0.0.1:5432/db —
# таблицы тест создаёт сам (миграция 104 и al_product_is из последней миграции). Без
# переменной пропускается: в общем прогоне базы нет. Проверяет то, чего SQLite не
# видит: jsonb приходит словарём, timestamptz, срезы — text[], сама al_product_is.

PG = os.getenv("STORIES_PG_TEST_URL")

_PG_BASE = """
DROP TABLE IF EXISTS review_story_event, review_story_member, review_story, review_index,
                     review_summary_vec, review_annotation CASCADE;
CREATE TABLE review_index (url text PRIMARY KEY, bank text, kind text, issue text,
                           dt timestamptz, city text, dup_of text, product text, slices text[]);
CREATE TABLE review_summary_vec (url text PRIMARY KEY, vec real[] NOT NULL);
CREATE TABLE review_annotation (url text, schema_version text, summary text,
                                PRIMARY KEY (url, schema_version));
"""


def _product_fn() -> str:
    """al_product_is(product, slices, want) из последней миграции, где она определена."""
    head = "CREATE OR REPLACE FUNCTION al_product_is(product text, slices text[]"
    src = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "migrations").glob("*.sql"))
           if head in p.read_text(encoding="utf-8")][-1]
    return re.search(re.escape(head) + r".*?\$\$;", src, re.S).group(0)


@pytest.fixture
def pgs(monkeypatch):
    if not PG:
        pytest.skip("нужна живая база: STORIES_PG_TEST_URL")
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    from bank_audit.rag import reviews_dash as rd
    eng = create_engine(PG, future=True)
    raw = eng.raw_connection()
    try:
        cur = raw.cursor()
        for sql in (_PG_BASE, MIG.read_text(encoding="utf-8"), _product_fn()):
            cur.execute(sql)
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(db, "_Session", sessionmaker(bind=eng, expire_on_commit=False, future=True))

    def put(rows):
        """rows: (url, issue, dt, vec, summary, product, slices) — все жалобы банка BANK."""
        with eng.begin() as c:
            for url, issue, dt, vec, summary, product, slices in rows:
                c.execute(text("""INSERT INTO review_index
                                      (url, bank, kind, issue, dt, product, slices)
                                  VALUES (:u, :b, 'complaint', :i, :d, :p, :s)"""),
                          {"u": url, "b": BANK, "i": issue, "d": dt, "p": product, "s": slices})
                c.execute(text("INSERT INTO review_summary_vec VALUES (:u, :v)"),
                          {"u": url, "v": [float(x) for x in vec]})
                c.execute(text("INSERT INTO review_annotation VALUES (:u, :s, :t)"),
                          {"u": url, "s": rd._ann_schema(), "t": summary})

    def q(sql, **p):
        with eng.connect() as c:
            return c.execute(text(sql), p).all()

    yield put, q
    eng.dispose()


def test_pg_naming_list_and_events(pgs):
    put, q = pgs
    sp = Space()
    a, c = sp.story("A", 8, 0.9), sp.story("C", 4, 0.9)
    Xa, Xc = sp.matrix(a), sp.matrix(c)
    prod = ["Дебетовые карты"] * 3 + [None] * 5
    put([(f"https://r/{a[k]}", "fees", NOW - timedelta(days=1 + k), Xa[k], SUM_A, prod[k],
          ["payroll"] if k == 3 else None) for k in range(6)]
        + [(f"https://r/{c[k]}", "cashback", NOW - timedelta(days=2 + k), Xc[k], SUM_C, None,
            None) for k in range(4)])
    assert RS.run(BANK, 90, now=NOW)["appeared"] == 2
    sid = {r[0]: r[1] for r in q("SELECT issue_main, story_id FROM review_story")}
    st = RS.name_stories(10, caller=FakeModel({"A": [_ans(TITLE_A)],
                                               "C": ['{"x": 1}', "не JSON"]}), now=NOW)
    assert (st["named"], st["invalid"]) == (1, 1) and "error" not in st
    # отложенный сюжет на завтра не берётся: сравнение timestamptz
    assert RS.name_stories(10, caller=FakeModel({}), now=NOW + timedelta(days=1))["todo"] == 0

    # день спустя A вырос на две жалобы
    later = NOW + timedelta(days=1)
    put([(f"https://r/{a[k]}", "fees", later - timedelta(hours=k), Xa[k], SUM_A, None, None)
         for k in (6, 7)])
    assert RS.run(BANK, 90, now=later)["grew"] == 1
    got = {x["story_id"]: x for x in RS.list_stories(BANK, now=later)}
    xa, xc = got[sid["fees"]], got[sid["cashback"]]
    assert (xa["title"], xa["n"], xa["n_window"]) == (TITLE_A, 8, 8)
    assert [b["label"] for b in xa["badges"]] == ["новый сюжет", "вырос с 6 до 8"]
    assert (xc["title"], xc["named"], xc["n"]) == (SUM_C, False, 4)
    deb = RS.list_stories(BANK, product="Дебетовые карты", now=later)
    assert [(x["story_id"], x["n"]) for x in deb] == [(sid["fees"], 3)]
    assert [b["kind"] for b in deb[0]["badges"]] == ["appeared"]          # рост — не их
    pay = RS.list_stories(BANK, product="Зарплатные проекты", now=later)
    assert [(x["story_id"], x["n"], x["urls"]) for x in pay] == [
        (sid["fees"], 1, [f"https://r/{a[3]}"])]

    ev = RS.story_events(sid["fees"])
    assert [e["kind"] for e in ev] == ["grew", "named", "appeared"]
    assert ev[0]["detail"] == {"new": 2, "n": 8}                          # jsonb — словарём
    assert ev[0]["label"] == "Вырос на 2 жалобы: в группе 8"
    assert ev[1]["label"] == f"Назван «{TITLE_A}»"
    assert RS.story_events(sid["cashback"])[0]["kind"] == "appeared"     # name_failed не виден


def test_short_and_plural():
    assert RS._short("коротко") == "коротко" and RS._short("") is None
    long = "слово " * 60
    s = RS._short(long)
    assert s.endswith("…") and len(s) <= RS.TITLE_MAX + 1 and "  " not in s
    assert [RS._complaints(n) for n in (1, 2, 5, 11, 12, 21, 22, 25, 101, 111)] == [
        "1 жалоба", "2 жалобы", "5 жалоб", "11 жалоб", "12 жалоб", "21 жалоба", "22 жалобы",
        "25 жалоб", "101 жалоба", "111 жалоб"]


def test_pg_story_signals_week_against_own_norm_and_market(pgs):
    # 5.6 (04.11.2026): сюжет против своей нормы — V6 и BH по сюжетам; молодой сюжет —
    # не всплеск; рынок — та же история у других банков banki.ru в ту же неделю
    put, q = pgs
    from bank_audit import db as _db
    from sqlalchemy import text as _t
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    with _db.session() as s:
        s.execute(_t("ALTER TABLE review_index ADD COLUMN IF NOT EXISTS source text DEFAULT 'bankiru'"))
        s.execute(_t("ALTER TABLE review_index ADD COLUMN IF NOT EXISTS ev_date date"))
    sp = Space(dim=240)
    A, B, Y = sp.story("A", 60, 0.95), sp.story("B", 60, 0.95), sp.story("Y", 12, 0.95)
    D = sp.story("D", 40, 0.95)
    XA, XB, XY, XD = sp.matrix(A), sp.matrix(B), sp.matrix(Y), sp.matrix(D)
    rows, members = [], {"A": [], "B": [], "Y": [], "D": []}

    def add(tag, ids, X, k, day):
        url = f"https://r/{tag}{k}"
        rows.append((url, "fees", now - timedelta(days=day, hours=3), X[k], f"изложение {tag}",
                     None, None))
        members[tag].append(url)
    # A: по 2 жалобы в неделю восемь недель, на неделе сигнала — 14
    k = 0
    for w in range(1, 9):
        for j in range(2):
            add("A", A, XA, k, 7 * w + 1 + j); k += 1
    for j in range(14):
        add("A", A, XA, k, 1 + j % 6); k += 1
    # B: ровно по 3 в неделю все девять недель — не всплеск
    k = 0
    for w in range(9):
        for j in range(3):
            add("B", B, XB, k, 7 * w + 1 + j); k += 1
    # Y: 10 жалоб за последнюю неделю, но сюжету 6 дней — «новый», не всплеск
    for k in range(10):
        add("Y", Y, XY, k, 1 + k % 5)
    # D: без названия, вторая неделя подъёма (10 и 10 при норме 2): прошлая неделя в
    # норму не входит, и сигнал не гаснет; жалоба о событии годичной давности не в счёт
    k = 0
    for w in range(2, 9):
        for j in range(2):
            add("D", D, XD, k, 7 * w + 1 + j); k += 1
    for w in (0, 1):
        for j in range(10):
            add("D", D, XD, k, 7 * w + 1 + j % 6); k += 1
    old_ev = members["D"][-1]
    put(rows)
    with _db.session() as s:
        s.execute(_t("UPDATE review_index SET ev_date = (dt - interval '400 days')::date WHERE url = :u"),
                  {"u": old_ev})
    for tag, first in (("A", 70), ("B", 70), ("Y", 6), ("D", 70)):
        with _db.session() as s:
            sid = s.execute(_t("""INSERT INTO review_story (bank, title, first_seen, issue_main, status,
                                                            medoid_url)
                                  VALUES (:b, :t, :f, 'fees', 'active', :m) RETURNING story_id"""),
                            {"b": BANK, "t": None if tag == "D" else f"Сюжет {tag}",
                             "f": now - timedelta(days=first), "m": members[tag][0]}).scalar()
            for u in members[tag]:
                s.execute(_t("INSERT INTO review_story_member (story_id, url) VALUES (:s, :u)"),
                          {"s": sid, "u": u})
    # у рынка: две жалобы другого банка той же истории, одна — с другой площадки
    with _db.session() as s:
        for url, src, vec in (("https://m/1", "bankiru", XA[0]), ("https://m/2", "bankiru", XA[1]),
                              ("https://m/3", "sravni", XA[2]), ("https://m/4", "bankiru", XB[0])):
            s.execute(_t("""INSERT INTO review_index (url, bank, kind, issue, dt, source)
                            VALUES (:u, 'Другой банк', 'complaint', 'fees', :d, :s)"""),
                      {"u": url, "d": now - timedelta(days=2), "s": src})
            s.execute(_t("INSERT INTO review_summary_vec VALUES (:u, :v)"),
                      {"u": url, "v": [float(x) for x in vec]})
    got = {x["title"]: x for x in RS.story_signals(BANK, now=now)}
    assert set(got) == {"Сюжет A", "изложение D"}, list(got)
    d = got["изложение D"]
    assert (d["week"], d["prev_week"], d["baseline_week"], d["named"]) == (10, 9, 2.0, False)
    a = got["Сюжет A"]
    assert a["week"] == 14 and a["baseline_week"] == 2.0 and a["ratio"] == 7.0
    assert len(a["urls"]) == 14 and a["q"] <= RS.STORY_SIG_ALPHA
    assert a["market"] == {"n": 2, "banks": 1}
    assert all(x["market"] is None for x in RS.story_signals(BANK, now=now, market=False))


def test_pg_list_stories_window_from_period(pgs):
    # окно вкладки (Period.binds — наивное МСК) считает база: широкое окно берёт всех,
    # окно в прошлом — никого из свежих; total — до обрезки по limit (04.11.2026)
    put, q = pgs
    sp = Space()
    a = sp.story("A", 6, 0.9)
    Xa = sp.matrix(a)
    now = datetime.now(timezone.utc)
    put([(f"https://r/{a[k]}", "fees", now - timedelta(days=1 + k), Xa[k], SUM_A, None, None)
         for k in range(6)])
    assert RS.run(BANK, 90, now=now)["appeared"] == 1
    naive = datetime.now()
    meta = {}
    got = RS.list_stories(BANK, t0=naive - timedelta(days=400), t1=naive + timedelta(days=1),
                          _meta=meta)
    assert [(x["n"], x["n_window"]) for x in got] == [(6, 6)] and meta["total"] == 1
    assert RS.list_stories(BANK, t0=naive - timedelta(days=400), t1=naive - timedelta(days=30)) == []


def test_new_badge_only_for_young_stories():
    # первый запуск пишет appeared всем сюжетам — «новый» лишь тот, чья первая жалоба
    # моложе двух недель
    now = NOW
    ev = [("appeared", now - timedelta(days=1), {})]
    old = {"first": now - timedelta(days=60), "n": 5, "m": []}
    young = {"first": now - timedelta(days=5), "n": 5, "m": []}
    assert RS._badges(old, ev, now) == []
    assert [b["kind"] for b in RS._badges(young, ev, now)] == ["appeared"]
