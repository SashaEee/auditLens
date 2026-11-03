"""«Сюжеты» (rag/review_stories.py): кластеризация, сопоставление прогонов,
название, «новое» и пакетный проход. Векторы синтетические, база — SQLite
по схеме из миграции 104; сети нет."""
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


@pytest.fixture
def lite(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from bank_audit import db
    eng = create_engine(f"sqlite:///{tmp_path / 'stories.db'}", future=True)
    raw = eng.raw_connection()
    try:
        raw.driver_connection.executescript(_sqlite_ddl() + """
            CREATE TABLE review_index (url TEXT PRIMARY KEY, bank TEXT, kind TEXT, issue TEXT,
                                       dt TIMESTAMP, city TEXT, dup_of TEXT);
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
