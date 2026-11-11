"""«Кого смотреть» на карточках модели: один клиент — один раз, полки, очередь случаев, волны и очаги,
история «про другое отделение», центральный офис."""
from __future__ import annotations

import json
import random
from collections import Counter, defaultdict
from datetime import timedelta, timezone

from sqlalchemy import select

from bank_audit.vsp.models import utcnow
from bank_audit.vsp.registry.enrich import enrich_branches
from bank_audit.vsp.signals import llm_read as lr
from bank_audit.vsp.signals import risk as rk
from bank_audit.vsp.signals.run import latest_run, run
from bank_audit.vsp.storage import Store, branch_stats, meta, network

from test_signals import _background, _net, _rev  # noqa: E402  (соседний модуль тестов)

UTC = timezone.utc
_WORDS = ("январь лестница зонтик соседка паспорт очередь талон окно кассир терминал справка выписка квитанция "
          "договор заявление подпись планшет экран смс уведомление приложение пароль телефон звонок оператор "
          "менеджер руководитель охранник стойка кресло парковка остановка автобус погода пятница понедельник "
          "зарплата пенсия перевод комиссия процент рассрочка ипотека вклад счёт карта лимит баланс остаток "
          "чек банкомат купюра монета конверт папка ручка печать штамп реквизиты отделение филиал район").split()


def _filler(rnd: random.Random, n: int = 14) -> str:
    """Разные слова в каждом отзыве: иначе код справедливо сочтёт похожие тексты копиями одного клиента."""
    return " ".join(rnd.sample(_WORDS, n))


def _card(rid, **kw):
    d = {"relevant": True, "severity": 2, "fault": "сотрудник", "actions": ["оформление_без_согласия"],
         "episode": "подключили платную подписку без согласия", "quote": "подключили подписку без моего согласия",
         "quote_ok": True, "employee_name": None, "vulnerability": [], "products": ["подписка"], "money_rub": None,
         "other_vsp": None, **kw}
    return {"review_id": rid, "prompt_version": lr.PROMPT_VERSION, "text_hash": "x", "model": "fake",
            "relevant": 1, "severity": d["severity"], "staff": d["fault"], "summary": d["episode"], "scheme": "",
            "data_json": json.dumps(d, ensure_ascii=False), "cost_rub": 0.0, "at": utcnow()}


def _detail(rid, **kw):
    d = {"kind": 7, "kind2": None, "label": "подписка без согласия", "benefit": "план продаж", "intent": 2,
         "systemic": [], "outcome": "не сообщается", "escalation": [], "here": True, "elsewhere": None,
         "window": None, "sum_kind": None, "event_date": None, **kw}
    return {"review_id": rid, "prompt_version": lr.DETAIL_VERSION, "text_hash": "x", "model": "fake",
            "relevant": 1, "severity": None, "staff": d["benefit"], "summary": d["label"], "scheme": str(d["kind"]),
            "data_json": json.dumps(d, ensure_ascii=False), "cost_rub": 0.0, "at": utcnow()}


class Scene:
    """Синтетическая сеть с карточками модели: почерк в отделении, очаг в Калининграде, новая волна,
    история «изнутри», история про другое отделение, центральный офис."""

    def __init__(self, st: Store, seed: int = 5):
        self.st, self.rnd, self.now = st, random.Random(seed), utcnow()
        self.ids = _net(st)
        enrich_branches(st)
        _background(st, self.ids, self.rnd)
        self.reviews, self.cards, self.details = [], [], []

    def story(self, ref, n, days, author, phrase, *, card=None, detail=None, total=7, copy_text=None):
        text = copy_text or f"{phrase} {_filler(self.rnd)}"
        self.reviews.append(_rev(f"g{ref}", n, 1, self.now - timedelta(days=days), author, total=total, text=text))
        rid = f"2gis:g{ref}-{n}"
        self.cards.append(_card(rid, quote=phrase, episode=phrase, **(card or {})))
        if detail is not None:
            self.details.append(_detail(rid, **detail))
        return rid, text

    def build(self):
        s = self
        # 1) почерк: четыре разных клиента одного отделения за четыре месяца — подписка без согласия;
        #    пятый отзыв — тот же клиент повторил свою жалобу (тот же автор): в счёт не идёт
        for i in range(4):
            s.story("6", f"p{i}", 20 + 30 * i, f"client{i}", "подключили подписку без моего согласия", detail={})
        s.story("6", "p0copy", 15, "client0", "подключили подписку без моего согласия и не вернули деньги", detail={})
        # 2) очаг: платная биометрия в трёх отделениях Калининграда за полгода; по остальной сети — редко и давно
        for i, (ref, d) in enumerate((("100", 30), ("100", 55), ("101", 80), ("102", 105), ("101", 130), ("102", 155),
                                      ("100", 180))):
            s.story(ref, f"bio{i}", d, f"kld{i}", "взяли деньги за сдачу биометрии",
                    card={"actions": ["биометрия"], "products": ["биометрия"]},
                    detail={"kind": 15, "label": "платная биометрия"})
        for i in range(5):
            s.story(str(i), f"bio{i}", 500 + 150 * i, f"msk{i}", "взяли деньги за сдачу биометрии",
                    card={"actions": ["биометрия"], "products": ["биометрия"]},
                    detail={"kind": 15, "label": "платная биометрия"})
        # 3) новая волна: перевод пенсии без согласия — шесть клиентов за четыре месяца в разных отделениях Москвы
        for i in range(6):
            s.story(str(i % 5), f"pens{i}", 10 + 18 * i, f"pens{i}", "перевели пенсию в банк без моего согласия",
                    card={"vulnerability": ["пенсионер/пожилой"], "products": ["пенсия"]},
                    detail={"kind": 8, "label": "перевод пенсии через госуслуги"})
        # 4) «изнутри»: сотрудница перевела деньги себе — одна очень тяжёлая история
        self.inside_rid, _ = s.story("7", "heavy", 3, "victim", "взяла телефон мамы и перевела себе 50 тысяч",
                                     card={"severity": 3, "actions": ["доступ_к_телефону_или_аккаунту"], "money_rub": 50000,
                                           "vulnerability": ["пенсионер/пожилой"], "employee_name": None},
                                     detail={"kind": 9, "label": "перевод с телефона клиента", "benefit": "сотрудник лично",
                                             "intent": 3, "escalation": ["полиция"], "sum_kind": "потеря"})
        # 5) отзыв оставлен на карточке отделения 1, но клиент назвал код отделения 2 — история его
        self.moved_rid, _ = s.story("1", "moved", 40, "mover", "в отделении 9038/01002 оформили кредитную карту без меня",
                                    detail={"kind": 4, "label": "кредитная карта без клиента", "here": False,
                                            "elsewhere": "отделение 9038/01002"})
        # 6) история про отделение, которого нет в справочнике: в счёт этого отделения не идёт
        self.else_rid, _ = s.story("3", "else", 60, "elsew", "это было в другом офисе на вокзале, там оформили страховку",
                                   detail={"kind": 7, "label": "страховка без согласия", "here": False,
                                           "elsewhere": "офис на вокзале"})
        # 7) центральный офис: три тяжёлые истории, но в сравнении отделений он не участвует
        for i in range(3):
            s.story("5", f"hq{i}", 25 + 20 * i, f"hq{i}", "банк в целом навязывает подписку домклик",
                    detail={"kind": 7, "label": "подписка домклик"})
        s.st.save_reviews(s.reviews)
        meta.create_all(s.st.engine, tables=[lr.review_llm])
        with s.st.engine.begin() as c:
            c.execute(lr.review_llm.insert(), s.cards + s.details)
        return s


def _load(st):
    rid = latest_run(st)
    with st.engine.connect() as c:
        det = {r.point: json.loads(r.detail_json) for r in c.execute(select(branch_stats).where(branch_stats.c.run_id == rid))}
        net = {r.key: json.loads(r.value_json) for r in c.execute(select(network).where(network.c.run_id == rid))}
    return det, net


def test_shelves_clients_queue_waves_hotspots(db_url, monkeypatch):
    monkeypatch.setattr(rk, "HQ_CODES", {"9038/1005"})
    st = Store(db_url)
    sc = Scene(st).build()
    ids = sc.ids
    out = run(st)
    assert out["story_source"] == "модель" and out["detailed"] == out["stories"]
    assert out["moved"] == 1 and out["elsewhere"] == 1
    det, net = _load(st)
    stories = {x["rid"]: x for x in net["stories"]}
    # один клиент — один раз: повтор жалобы тем же автором — копия
    assert stories["2gis:g6-p0copy"]["copy_of"] or stories["2gis:g6-p0"]["copy_of"]
    a6 = det[f"b{ids['6']}"]["risk"]
    assert a6["shelf"] == "pattern" and a6["include"] and a6["heavy_clients"] == 4
    pat = a6["patterns"][0]
    assert pat["source"] == "тип" and pat["kind"] == 7 and pat["window_clients"] == 4 and pat["expected"] < 1
    assert "4 разных клиента" in a6["why"][0] and not any(rk.FORBIDDEN.search(w) for w in a6["why"])
    assert a6["lead"]["rid"] in pat["rids"]                              # главный случай — из историй самой причины
    assert a6["req_from"][:10] == pat["first"][:10] and a6["request"] and a6["request"][0]["kind"] == 7
    assert a6["benign"]["resolve"] and a6["check"]
    # по одному числу историй не выделяется никто. Три клиента с биометрией в одном отделении очага на полку
    # не попадают: при честном счёте сравнений (каждая пара «отделение × способ») такое складывается и случайно;
    # их видно в очаге
    assert out["shelves"] == {"pattern": 1} and not det[f"b{ids['100']}"]["risk"]["include"]
    # «изнутри»: одна история — не полка отделения, а верх очереди случаев
    a7 = det[f"b{ids['7']}"]["risk"]
    assert not a7["include"] and a7["severity"] == 3 and a7["flags"]["inside"] == 1
    top = net["case_queue"][0]
    assert top["rid"] == sc.inside_rid and top["score"] >= 10 and "выгода — сотруднику" in top["why"]
    assert stories[sc.inside_rid]["inside"] and net["risk_react"][0]["point"] == f"b{ids['7']}"
    # история перенесена по коду отделения из текста; история «не здесь» — не в счёт отделения, но в сети остаётся
    assert stories[sc.moved_rid]["point"] == f"b{ids['2']}" and stories[sc.moved_rid]["moved_from"] == f"b{ids['1']}"
    assert stories[sc.else_rid]["elsewhere"] and det[f"b{ids['3']}"]["risk"]["heavy_clients"] == \
        sum(1 for x in net["stories"] if x["point"] == f"b{ids['3']}" and not x["elsewhere"] and not x["copy_of"])
    # центральный офис: истории есть, полки нет
    assert det[f"b{ids['5']}"].get("hq") and f"b{ids['5']}" not in {p for p, d in det.items()
                                                                     if (d.get("risk") or {}).get("include")}
    assert all(x["hq"] for x in net["stories"] if x["point"] == f"b{ids['5']}")
    # волна: перевод пенсии — новое (всё появилось за год); биометрия — не новое
    waves = {w["key"]: w for w in net["waves"]}
    assert waves["k:8"]["status"] == "new" and waves["k:8"]["year"] == 6 and waves["k:8"]["prev_year"] == 0
    assert waves["k:15"]["status"] != "new" and waves["k:15"]["clients"] == 12
    assert len(waves["k:8"]["series"]) == 48 and sum(x["n"] for x in waves["k:8"]["series"]) == 6
    # очаг: Калининград, биометрия — семь клиентов в трёх отделениях за полгода
    hot = [h for h in net["hotspots"] if h["key"] == "k:15"]
    assert hot and hot[0]["strength"] == "strong" and hot[0]["clients"] == 7 and len(hot[0]["points"]) == 3
    assert hot[0]["terr"] in ("Калининград", "8626") and hot[0]["expected"] < 3 and hot[0]["ratio"] > 2
    assert not [h for h in net["hotspots"] if h["key"] == "k:7"]          # один почерк в одном отделении — не очаг
    # таблица для карты: клиенты за 2 года по регионам против ожидания (способ с 8+ клиентами — отдельной строкой)
    kgd = net["terr"]["k:15"]["subject"]["KGD"]
    assert kgd["n"] == 7 and kgd["read"] > 300 and 0 < kgd["expected"] < 7 and not kgd["few"] and kgd["sig"]
    assert net["terr"]["all"]["subject"]["KGD"]["n"] == 7 and "k:8" not in net["terr"]   # меньше восьми клиентов
    assert net["kinds"]["15"]["name"] and net["places"][f"b{ids['6']}"]["code"] == "9038/1006"


def test_rules_fallback_without_model(db_url):
    st = Store(db_url)
    ids = _net(st)
    enrich_branches(st)
    now = utcnow()
    st.save_reviews([_rev("g2", f"q{i}", 1, now - timedelta(days=10 + i), f"a{i}", total=5,
                          text="Оформили кредитную карту без моего согласия, не объяснили условия")
                     for i in range(2)])
    out = run(st)
    assert out["story_source"] == "слова" and out["detailed"] == 0
    det, net = _load(st)
    a = det[f"b{ids['2']}"]["risk"]
    assert a["source"] == "слова" and not a["include"] and a["heavy_clients"] >= 1
    assert net["waves"] == [] or all(w["clients"] >= 5 for w in net["waves"])


def test_same_review_on_two_branches_is_marked_even_with_different_quotes():
    from datetime import datetime
    from bank_audit.vsp.signals.risk import _mark_dups
    t = "мою маму пенсионерку обманули на 59 500 продали не ту услугу за которой она пришла деньги не возвращают"
    d = datetime(2026, 1, 1, tzinfo=UTC)
    xs = [{"rid": "a", "point": "b1", "at": d, "text": t, "quote": "Мою маму пенсионерку обманули на 59.500"},
          {"rid": "b", "point": "b2", "at": d + timedelta(days=1), "text": t, "quote": "продали не ту услугу за которой она пришла"},
          {"rid": "c", "point": "b3", "at": d, "text": "совсем другой отзыв про кассу где недодали деньги при размене крупной купюры", "quote": "x"}]
    out = _mark_dups(xs)
    assert [s["dup_points"] for s in out] == [1, 1, 0]
    assert [s["dup_of"] for s in out] == [None, "a", None]              # в волнах и очагах клиент считается один раз


def test_same_client_across_platforms_and_time():
    from datetime import datetime
    from bank_audit.vsp.signals import clients
    d = datetime(2026, 3, 1, tzinfo=UTC)
    base = "оформили кредитную карту вместо дебетовой сказали что это обычная карта для зарплаты потом пришёл долг"
    xs = [{"rid": "2gis:1", "point": "b1", "at": d, "author": "u1", "text": base},
          {"rid": "yandex:1", "point": "b1", "at": d + timedelta(days=3), "author": "u9", "text": base + " ужас"},
          {"rid": "2gis:2", "point": "b1", "at": d + timedelta(days=400), "author": "u1", "text": "дописываю спустя год"},
          {"rid": "2gis:3", "point": "b1", "at": d, "author": "u3", "text": "недодали сдачу в кассе при обмене валюты кассир спорила"},
          {"rid": "2gis:4", "point": "b2", "at": d, "author": "u4", "text": base}]
    assert clients.assign(xs) == 3
    by = {x["rid"]: x for x in xs}
    assert by["yandex:1"]["client"] == by["2gis:1"]["client"] == by["2gis:2"]["client"] == "2gis:1"
    assert by["yandex:1"]["copy_of"] == "2gis:1" and by["2gis:1"]["copy_of"] is None
    assert by["2gis:3"]["client"] != by["2gis:1"]["client"]
    assert by["2gis:4"]["client"] == "2gis:4"                           # другое отделение — другой счёт клиентов


def test_kind_from_actions_and_request():
    assert rk.kind_from_actions(["биометрия"]) == 15
    assert rk.kind_from_actions(["подмена_продукта"], ["кредитная карта"]) == 5
    assert rk.kind_from_actions(["оформление_без_согласия"], ["потребкредит"]) == 4
    assert rk.kind_from_actions([]) == 25
    r = rk.request_for(15, where="отделение 9038/1", period="01.01.2026 — 01.06.2026")
    assert r["kind"] == 15 and "9038/1" in r["text"] and r["metric"] and r["benign"]
    assert rk.request_for(25, where="x", period="y") is None or rk.request_for(25, where="x", period="y")["text"]
    assert rk.code_key("№ 9038/01919") == "9038/1919" and rk.code_key("СберБанк") is None


def test_uniform_background_rarely_fills_branch_shelves():
    """Ровный фон: истории четырёх типов раскиданы по 60 отделениям случайно. Тройки «один способ за год»
    складываются и так — на полку отделение попадать не должно. Раньше в поправку шли только пары «отделение ×
    способ», где уже набралось три истории, и на перемешанных данных прода на полке оказывалось 1–12 отделений
    при 19 настоящих. «Модель», которая про любое отделение отвечает «почерк есть» и называет все его истории,
    полку тоже не раздувает (на проде 11.11 она ответила так про 424 отделения из 594)."""
    import test_waves_hotspots as tw
    expo = tw._expo(tw.PTS)
    pts = {p: {"gosb": v["gosb"], "subject": v["subject"], "city": v["city"], "code": None, "hq": False}
           for p, v in tw.PTS.items()}

    def rich(s):
        return {**s, "actions": ["другое"], "vulnerable": False, "quote": "x", "summary": "x", "employee": "",
                "money": None, "date": s["at"].strftime("%d.%m.%Y"), "text": "x", "quote_ok": True}

    def background(seed):
        return [rich(s) for kind, rate in ((7, 12), (5, 6), (15, 3), (9, 3)) for s in tw._steady(kind, rate, seed)]

    def yes_model(stories):
        by = defaultdict(list)
        for s in stories:
            by[s["point"]].append(s["rid"])
        return {p: {"pattern": True, "confidence": "высокая", "rids": rids, "pattern_name": "всё подряд"}
                for p, rids in by.items() if len(rids) >= 3}

    runs, hit, hit_llm, triples = 60, 0, 0, 0
    for seed in range(runs):
        st = background(seed)
        a = rk.assess(pts, st, expo, now=tw.NOW, source="модель")
        hit += any(x["shelf"] for x in a.values())
        b = rk.assess(pts, st, expo, now=tw.NOW, source="модель", conclusions=yes_model(st))
        hit_llm += any(x["shelf"] for x in b.values())
        per = Counter((s["point"], s["kind"], s["at"].year) for s in st)
        triples += sum(1 for n in per.values() if n >= 3)
    assert triples >= runs                                    # троек на фоне много — проверка не пустая
    assert hit <= 0.1 * runs and hit_llm <= 0.15 * runs, (hit, hit_llm)
    # а настоящее сгущение — пять клиентов за три месяца в одном отделении — на полку попадает
    st = background(1) + [rich(tw._story(900 + i, "ta0", tw.NOW - timedelta(days=20 + 15 * i), 7, tw.PTS["ta0"]))
                          for i in range(5)]
    a = rk.assess(pts, st, expo, now=tw.NOW, source="модель")
    assert a["ta0"]["shelf"] == "pattern" and a["ta0"]["patterns"][0]["window_clients"] >= 5
    assert sum(1 for x in a.values() if x["shelf"]) == 1
