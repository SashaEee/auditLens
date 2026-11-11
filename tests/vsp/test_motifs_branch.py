"""Почерки (редактор-модель раскладывает описания по каталогу) и вывод по отделению (модель читает все
истории разом). Модель подменена: проверяем, что считает и хранит код."""
from __future__ import annotations

import asyncio
import json

from sqlalchemy import select

from bank_audit.vsp.signals import branch_llm as bl
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import motifs as mo
from bank_audit.vsp.signals import risk as rk
from bank_audit.vsp.signals.run import run
from bank_audit.vsp.storage import Store

from test_risk import Scene, _load  # noqa: E402  (соседний модуль тестов)


class Editor:
    """Редактор почерков: предлагает каталог и относит описания; считает вызовы."""
    rub = 0.0

    def __init__(self, *a, **k):
        self.system, self.calls, self.model = "", 0, "fake"

    async def ask(self, user, system=None):
        self.calls += 1
        if "Перечень почерков" not in user:                         # предложить каталог для типа
            if "биометри" in user:
                return {"motifs": [{"name": "Берут деньги за сдачу биометрии", "description": "Клиенты пишут, что с них "
                                    "взяли деньги за биометрию", "benign": "платная услуга по тарифу",
                                    "ops": "платежи за биометрию", "metric": "доля возвратов"},
                                   {"name": "Мошенничество с биометрией", "description": "x"}]}, 0.05   # запретное слово
            return {"motifs": [{"name": "Подписку подключают без согласия", "description": "Клиенты пишут, что подписку "
                                "подключили без их согласия", "benign": "согласие в анкете", "ops": "подключения подписок",
                                "metric": "доля отключений за 30 дней"}]}, 0.05
        n = user.count("\n[")
        return {"items": [{"n": i + 1, "motif": 1} for i in range(n)], "new": []}, 0.02


def test_motifs_catalog_assign_and_use_in_signals(db_url, monkeypatch):
    monkeypatch.setattr(rk, "HQ_CODES", {"9038/1005"})
    monkeypatch.setattr(mo, "PROPOSE_MIN", 1)
    st = Store(db_url)
    sc = Scene(st).build()
    ed = Editor()
    res = asyncio.run(mo.build(st, reader=ed))
    ms, labels = mo.load(st)
    names = {m["name"] for m in ms.values()}
    assert "Берут деньги за сдачу биометрии" in names and not any("ошенничеств" in n for n in names)
    bio = next(k for k, m in ms.items() if m["name"].startswith("Берут деньги"))
    assert bio.startswith("m15") and not bio.endswith("00") and ms[bio]["ops"] and ms[bio]["metric"]
    assert labels[(15, "платная биометрия")] == bio and res["assigned"] >= 3 and res["motifs"] >= 2
    assert mo.mapper(labels)("x", 15, "Платная биометрия.") == bio and mo.mapper(labels)("x", 15, None) is None
    calls = ed.calls
    again = asyncio.run(mo.build(st, reader=ed))                    # всё разложено — модель больше не зовём
    assert ed.calls == calls and again["labels"] == res["labels"]
    run(st)
    det, net = _load(st)
    waves = {w["key"]: w for w in net["waves"]}
    assert waves[f"m:{bio}"]["name"] == "Берут деньги за сдачу биометрии" and waves[f"m:{bio}"]["level"] == "motif"
    assert waves[f"m:{bio}"]["clients"] == waves["k:15"]["clients"] == 12
    hot = [h for h in net["hotspots"] if h["kind"] == 15]
    # очаг найден по типу (типы и почерки проверяются отдельными семьями), почерк — его главный приём
    assert len(hot) == 1 and hot[0]["level_key"] == "kind" and hot[0]["main_motif"]["id"] == bio
    assert hot[0]["main_motif"]["n"] == hot[0]["clients"] and hot[0]["strength"] == "strong"
    a6 = det[f"b{sc.ids['6']}"]["risk"]
    assert a6["patterns"][0]["source"] == "почерк" and a6["patterns"][0]["name"] == "Подписку подключают без согласия"
    assert len([p for p in a6["patterns"] if p["kind"] == 7]) == 1                         # тип не дублирует почерк
    assert net["motifs"][bio]["name"] and next(x for x in net["stories"] if x["kind"] == 15)["motif"] == bio


class Reader:
    """Вывод по отделению: модель называет истории почерка по номерам."""
    rub, errors = 0.0, 0

    def __init__(self, *a, **k):
        self.calls, self.model, self.prompts = 0, "fake", []

    async def ask(self, user):
        self.calls += 1
        self.prompts.append(user)
        n = user.count("\n   ")
        if "подписк" in user:
            return {"pattern": True, "pattern_name": "Подписку подключают при выдаче карты", "stories": list(range(1, n + 1)),
                    "who": "разные сотрудники", "victims": None, "money": None, "network": None, "other": None,
                    "benign": "согласие могло быть в анкете", "request": ["Подключения подписок по сотрудникам за период"],
                    "confidence": "высокая", "why": "все истории об одном"}, 0.6
        return {"pattern": False, "stories": [], "confidence": "низкая", "why": "жалобы разные", "benign": "",
                "request": []}, 0.6


def test_branch_conclusion_is_read_once_and_checked_by_code(db_url, monkeypatch):
    monkeypatch.setattr(rk, "HQ_CODES", {"9038/1005"})
    st = Store(db_url)
    sc = Scene(st).build()
    run(st)
    rd = Reader()
    res = asyncio.run(bl.run(st, reader=rd))
    b6 = f"b{sc.ids['6']}"
    con = bl.load(st)
    assert res["saved"] == rd.calls >= 1 and b6 in con and f"b{sc.ids['5']}" not in con       # центральный офис не читаем
    c6 = con[b6]
    assert c6["pattern"] and c6["clients"] == 4 and c6["dense"] and c6["confidence"] == "высокая" and len(c6["rids"]) == 4
    assert c6["period"] and c6["kinds"] == [7] and "Сейчас часто встречается по всей сети" in rd.prompts[0]
    calls = rd.calls
    assert asyncio.run(bl.run(st, reader=rd))["todo"] == 0 and rd.calls == calls               # состав историй тот же
    run(st)
    det, _ = _load(st)
    a6 = det[b6]["risk"]
    llm = [p for p in a6["patterns"] if p["source"] == "вывод модели"]
    assert llm and llm[0]["name"] == "Подписку подключают при выдаче карты" and llm[0]["clients"] == 4
    assert not [p for p in a6["patterns"] if p["source"] == "тип"]         # вывод модели вместо типа по тем же клиентам
    assert a6["conclusion"]["pattern"] and a6["conclusion"]["wave_only"] is False and a6["shelf"] == "pattern"
    assert any(s["key"] == "llm" for s in a6["sig"])


def test_branch_conclusion_normalize_counts_by_code():
    from datetime import datetime, timezone
    d = datetime(2026, 1, 10, tzinfo=timezone.utc)
    ss = [{"rid": f"r{i}", "client": "c1" if i < 2 else f"c{i}", "at": d.replace(month=1 + i).isoformat(), "kind": 5}
          for i in range(4)]
    v = bl.normalize({"pattern": True, "pattern_name": "Кредитку выдают как дебетовую", "stories": [1, 2, 3, 9, "x"],
                      "confidence": "очень высокая", "request": ["a", "", "b", "c", "d"]}, ss)
    assert v["rids"] == ["r0", "r1", "r2"] and v["clients"] == 2 and v["confidence"] == "низкая" and len(v["request"]) == 3
    assert v["period"] == "01.2026 — 03.2026" and v["dense"] is True and v["kinds"] == [5]
    assert bl.normalize({"pattern": True, "stories": [1]}, ss)["pattern"] is False             # одна история — не почерк
    assert bl.normalize({"stories": []}, ss) is None and bl.normalize(None, ss) is None
    assert bl.stories_hash(ss) == bl.stories_hash(list(reversed(ss))) != bl.stories_hash(ss[:3])


class Detail(lr.Reader):
    def __init__(self, model="fake", system="", **k):
        import asyncio as aio
        self.model, self.system, self.rub, self.calls, self.errors = model, system, 0.0, 0, 0
        self.drop, self.sem = set(), aio.Semaphore(4)

    async def ask(self, user):
        self.calls += 1
        n = user.count("Отзыв:")
        return {"items": [{"n": k + 1, "kind": 9, "kind2": 9, "label": "Перевод с телефона клиента.", "benefit": "Сотрудник лично",
                           "intent": 7, "systemic": ["так заведено", "выдумка"], "outcome": "банк признал нарушение",
                           "escalation": ["полиция", "газета"], "here": "false", "elsewhere": "9038/01002",
                           "window": "окно 3", "sum_kind": "потеря"} for k in range(n)]}, 0.25


def test_detail_stage_reads_only_heavy_recent_and_normalizes(db_url, monkeypatch):
    from datetime import datetime, timezone
    from bank_audit.vsp.models import Review
    st = Store(db_url)
    new, old = datetime(2026, 5, 1, tzinfo=timezone.utc), datetime(2019, 5, 1, tzinfo=timezone.utc)
    t = "Сотрудница взяла телефон мамы и перевела себе пятьдесят тысяч рублей, маме восемьдесят лет, написали заявление"
    st.save_reviews([Review("2gis", "h", "g1", 1, t, new, author_ref="a"), Review("2gis", "l", "g1", 1, t + " ещё", new, author_ref="b"),
                     Review("2gis", "o", "g1", 1, t + " давно", old, author_ref="c")])
    from test_risk import _card
    from bank_audit.vsp.storage import meta
    meta.create_all(st.engine, tables=[lr.review_llm])
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert(), [_card("2gis:h", severity=3), _card("2gis:l", severity=1), _card("2gis:o", severity=3)])
    monkeypatch.setattr(lr, "Reader", Detail)
    assert lr.unread(st)["detail"] == 1                              # лёгкая и старая история — не уточняются
    res = asyncio.run(lr.run(st, stage="detail", limit=50))
    assert res["detail"]["saved"] == 1 and res["detail"]["waiting"] == 1 and lr.unread(st)["detail"] == 0
    with st.engine.connect() as c:
        row = c.execute(select(lr.review_llm).where(lr.review_llm.c.prompt_version == lr.DETAIL_VERSION)).mappings().one()
    d = json.loads(row["data_json"])
    assert row["review_id"] == "2gis:h" and row["scheme"] == "9" and row["staff"] == "сотрудник лично"
    assert d["kind"] == 9 and d["kind2"] is None and d["label"] == "перевод с телефона клиента" and d["intent"] == 3
    assert d["systemic"] == ["так заведено"] and d["escalation"] == ["полиция"] and d["here"] is False
    assert d["elsewhere"] == "9038/01002" and d["window"] == "окно 3" and d["sum_kind"] == "потеря"
    assert asyncio.run(lr.run(st, stage="detail", limit=50))["detail"]["read_unique"] == 0


class Editor2:
    """Редактор: в первом каталоге — лишние и повторные почерки; редкие описания сначала не подходят,
    потом из остатка появляется один новый почерк."""
    rub = 0.0

    def __init__(self):
        self.system, self.model, self.kinds_of_call = "", "fake", []

    async def ask(self, user, system=None):
        if "не подошли" in user:                                   # шаг 3: остаток
            self.kinds_of_call.append("more")
            return {"motifs": [{"name": "Сим-карту выдают вместе с картой", "description": "Клиенты пишут, что вместе с "
                                "картой им выдали сим-карту", "ops": "выдачи сим-карт", "metric": "доля отказов"},
                               {"name": "Ещё один лишний почерк", "description": "x"}]}, 0.05
        if "Перечень почерков" not in user:                         # шаг 1: первый каталог
            self.kinds_of_call.append("propose")
            ms = [{"name": f"Приём номер {i} повторяется", "description": "Клиенты пишут, что так бывает"} for i in range(12)]
            return {"motifs": ms + [{"name": "Приём номер 1 повторяется", "description": "повтор названия"}]}, 0.05
        self.kinds_of_call.append("assign")
        has_new = "Сим-карту выдают вместе с картой" in user
        n_cat = user.split("\n\nИстории:")[0].count("\n")
        items = []
        for i, line in enumerate(user.split("\n\nИстории:\n")[1].split("\n"), 1):
            if "сим" in line:
                items.append({"n": i, "motif": n_cat if has_new else 0})   # новый почерк — последний в перечне
            elif "редк" in line:
                items.append({"n": i, "motif": 0})
            else:
                items.append({"n": i, "motif": 1})
        return {"items": items, "new": [{"id": "new1", "name": "Не должен появиться"}]}, 0.02


def test_motifs_new_ones_come_only_from_leftover_and_catalog_is_capped(db_url, monkeypatch):
    from bank_audit.vsp.storage import meta
    from test_risk import _card, _detail
    monkeypatch.setattr(mo, "PROPOSE_MIN", 5)
    monkeypatch.setattr(mo, "MAX_MORE", 1)
    st = Store(db_url)
    meta.create_all(st.engine, tables=[lr.review_llm])
    labels = [f"обычное описание {i}" for i in range(30)] + [f"сим-карта к карте {i}" for i in range(8)] + \
        [f"редкое описание {i}" for i in range(4)]
    rows = []
    for i, lb in enumerate(labels):
        rows += [_card(f"2gis:x-{i}"), _detail(f"2gis:x-{i}", kind=5, label=lb)]
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert(), rows)
    ed = Editor2()
    res = asyncio.run(mo.build(st, reader=ed))
    ms, lab = mo.load(st)
    names = [m["name"] for k, m in sorted(ms.items()) if not k.endswith("00")]
    assert len(names) == mo.MAX_FIRST + 1 and len(set(names)) == len(names)      # 10 из первого каталога + 1 из остатка
    assert names[-1] == "Сим-карту выдают вместе с картой" and "Не должен появиться" not in names
    assert ed.kinds_of_call[0] == "propose" and ed.kinds_of_call.count("more") == 1
    assert ed.kinds_of_call.index("more") > ed.kinds_of_call.index("assign")     # новые почерки — после раскладки
    sim = next(k for k, m in ms.items() if m["name"].startswith("Сим-карту"))
    assert sum(1 for v in lab.values() if v == sim) == 8                          # остаток отнесён к новому почерку
    assert sum(1 for v in lab.values() if v == "m0500") == 4 and res["other"] == 4 and res["new"] == 1
    assert len(lab) == len(labels) and res["proposed"] == mo.MAX_FIRST
    n = len(ed.kinds_of_call)
    asyncio.run(mo.build(st, reader=ed))                                          # всё разложено — вызовов нет
    assert len(ed.kinds_of_call) == n


def _descr(st, labels, kind=5, prefix="x"):
    from bank_audit.vsp.storage import meta
    from test_risk import _card, _detail
    meta.create_all(st.engine, tables=[lr.review_llm])
    rows = []
    for i, lb in enumerate(labels):
        rows += [_card(f"2gis:{prefix}-{i}"), _detail(f"2gis:{prefix}-{i}", kind=kind, label=lb)]
    with st.engine.begin() as c:
        c.execute(lr.review_llm.insert(), rows)


def test_motifs_other_bucket_grows_into_a_new_motif(db_url, monkeypatch):
    """Новый приём приходит по паре историй за ночь: сначала лежит в «прочем», а когда «прочее» подросло —
    редактор смотрит его целиком, заводит почерк и переносит туда и старые описания."""
    monkeypatch.setattr(mo, "PROPOSE_MIN", 5)
    monkeypatch.setattr(mo, "MAX_MORE", 1)
    st = Store(db_url)
    _descr(st, [f"обычное описание {i}" for i in range(30)] + [f"сим-карта к карте {i}" for i in range(3)])
    ed = Editor2()
    r1 = asyncio.run(mo.build(st, reader=ed))
    _, lab = mo.load(st)
    assert "more" not in ed.kinds_of_call and r1["other"] == 3                 # трёх описаний мало для нового почерка
    assert sum(1 for v in lab.values() if v == "m0500") == 3
    assert st.kv_get("motifs:more_seen") == {"5": 3}                           # столько было в «прочем», когда смотрели
    _descr(st, [f"сим-карта к карте {i}" for i in range(3, 5)], prefix="y")
    asyncio.run(mo.build(st, reader=ed))                                        # +2 описания — ещё рано
    assert "more" not in ed.kinds_of_call
    _descr(st, [f"сим-карта к карте {i}" for i in range(5, 8)], prefix="z")
    r2 = asyncio.run(mo.build(st, reader=ed))
    ms, lab = mo.load(st)
    sim = next(k for k, m in ms.items() if m["name"].startswith("Сим-карту"))
    assert ed.kinds_of_call.count("more") == 1 and r2["new"] == 1 and r2["moved"] == 5
    assert sum(1 for v in lab.values() if v == sim) == 8 and "m0500" not in lab.values()
    n = len(ed.kinds_of_call)
    asyncio.run(mo.build(st, reader=ed))                                        # «прочее» не выросло — не смотрим
    assert len(ed.kinds_of_call) == n


def test_motifs_cap_leaves_other_bucket_for_next_run(db_url, monkeypatch):
    """Потолок трат кончился до шага «новые почерки»: остаток уходит в «прочее», но следующий запуск к нему вернётся."""
    monkeypatch.setattr(mo, "PROPOSE_MIN", 5)
    monkeypatch.setattr(mo, "MAX_MORE", 1)
    st = Store(db_url)
    _descr(st, [f"обычное описание {i}" for i in range(30)] + [f"сим-карта к карте {i}" for i in range(8)])
    ed = Editor2()
    r1 = asyncio.run(mo.build(st, reader=ed, cap_rub=0.06))                     # хватает на каталог и одну раскладку
    _, lab = mo.load(st)
    assert "more" not in ed.kinds_of_call and r1["other"] == 8 and len(lab) == 38
    assert not (st.kv_get("motifs:more_seen") or {})
    r2 = asyncio.run(mo.build(st, reader=ed))
    ms, lab = mo.load(st)
    sim = next(k for k, m in ms.items() if m["name"].startswith("Сим-карту"))
    assert r2["new"] == 1 and r2["moved"] == 8 and sum(1 for v in lab.values() if v == sim) == 8
    assert st.kv_get("motifs:more_seen") == {"5": 0}
