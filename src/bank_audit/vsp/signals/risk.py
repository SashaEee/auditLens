"""«Где стоит проверить»: истории клиентов → независимые признаки отделения → список и досье.

История клиента — отзыв, в котором модель (llm_read, этап карточки) увидела эпизод для
проверки отделения: что произошло, действия сотрудника, тяжесть, чья вина, дословная цитата.
Пока модель не прочитала отзывы, истории берутся из слов-правил (risks.py) с пометкой
«предварительно».

Отделение попадает в список, если сошлось несколько НЕЗАВИСИМЫХ наблюдений:
  А. тяжёлые истории разных клиентов (тяжесть 2–3);
  Б. повтор одной схемы у разных клиентов;
  В. одно имя сотрудника у разных клиентов (только внутри отделения);
  Г. таких историй заметно больше, чем у похожих отделений ГОСБ (с поправкой на объём);
  Д. люди вокруг: посредник хвалит «своего» менеджера при жалобах; новые аккаунты хвалят
     сотрудника, которого называют и в жалобах;
или есть одна история высшей тяжести с проверяемыми деталями. А и Б по одним и тем же
историям считаются одним признаком. Числа считает код, слова — шаблоны; балла на экране нет.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .people import stem
from .llm_read import quote_ok as llm_quote_ok

# действие → как называем, что может объяснить безобидно, что снимет вопрос, что запросить, что проверить
ACTIONS: dict[str, dict] = {
    "оформление_без_согласия": {
        "label": "оформили или подключили без согласия", "short": "без согласия",
        "benign": "подписку или карту подключают по акции, согласие могло быть в анкете",
        "resolve": "согласие клиента в заявке по каждому случаю",
        "request": [("Подключения услуг и выпуски карт в ВСП за период — по сотрудникам", "ГОСБ"),
                    ("Отключения и возвраты этих услуг в течение 30 дней после подключения", "центр")],
        "check": ["Как при выдаче или замене карты оформляются допуслуги: что видит и подписывает клиент",
                  "Выборочно 10 подключений пожилым клиентам: есть ли подпись, объяснили ли условия"]},
    "подмена_продукта": {
        "label": "выдали один продукт под видом другого", "short": "подмена продукта",
        "benign": "клиент сам выбрал продукт, но не разобрался в условиях",
        "resolve": "подписанные условия и анкета риск-профиля",
        "request": [("Продажи страховых и инвестиционных продуктов и кредитных карт в ВСП — по сотрудникам", "ГОСБ")],
        "check": ["Как объясняют клиенту отличие продукта от вклада или дебетовой карты",
                  "Выборка договоров пожилых клиентов за период"]},
    "ложь_об_обязательности": {
        "label": "говорят, что без допуслуги не дадут", "short": "«без этого не дадим»",
        "benign": "условие действительно есть в программе (например, страхование залога)",
        "resolve": "условия программы и отказ клиента от допуслуги в заявке",
        "request": [("Кредиты и карты с допуслугами за период и отказы от допуслуг в первые 14 дней", "центр")],
        "check": ["Что сотрудники говорят клиенту об обязательности страховки и подписок"]},
    "обусловливание": {
        "label": "услугу ставят условием выдачи", "short": "услуга как условие",
        "benign": "условие действительно есть в программе",
        "resolve": "условия программы и отказ клиента в заявке",
        "request": [("Кредиты, карты и выдачи пенсии с допуслугами за период", "ГОСБ")],
        "check": ["Можно ли получить услугу без допуслуги и что для этого говорят клиенту"]},
    "наказание_за_отказ": {
        "label": "наказывают за отказ от допуслуги", "short": "наказание за отказ",
        "benign": "задержка из-за проверки документов, не из-за отказа",
        "resolve": "сроки и причины задержки по заявке",
        "request": [("Заявки, отклонённые или задержанные после отказа клиента от допуслуги", "ГОСБ")],
        "check": ["Сроки выдачи карт и кредитов клиентам, отказавшимся от допуслуг"]},
    "ложь_об_условиях": {
        "label": "рассказали одно — оформили другое", "short": "другие условия",
        "benign": "условия изменились после оформления по правилам продукта",
        "resolve": "условия на дату оформления и что подписал клиент",
        "request": [("Договоры с жалобами клиентов на условия за период", "ГОСБ")],
        "check": ["Как объясняют условия: ставка, льготный период, платные опции"]},
    "доступ_к_телефону_или_аккаунту": {
        "label": "сотрудник сам работает в телефоне или аккаунтах клиента", "short": "телефон клиента",
        "benign": "клиент сам попросил помочь с приложением",
        "resolve": "журнал действий в приложении банка в момент визита",
        "request": [("Смены номера телефона и входы в приложение клиентов ВСП с устройств отделения", "центр"),
                    ("Операции клиентов, проведённые во время визита в ВСП", "центр")],
        "check": ["Соблюдается ли правило «телефон клиента — только в руках клиента»",
                  "Нет ли служебных учётных записей и устройств, через которые оформляют за клиента"]},
    "биометрия": {
        "label": "платная биометрия или сбор биометрии без согласия", "short": "биометрия",
        "benign": "клиент сам выбрал платный пакет вместе с биометрией",
        "resolve": "чеки и состав пакета при сдаче биометрии",
        "request": [("Продажи пакетов здоровья и сервисов вместе со сдачей биометрии за период", "ГОСБ")],
        "check": ["Что говорят клиенту о стоимости сдачи биометрии"]},
    "касса_недостача_или_чужой_платеж": {
        "label": "наличные: недостача или чужой платёж", "short": "касса",
        "benign": "ошибка пересчёта, исправленная по обращению",
        "resolve": "кассовые документы и видео кассы за дату",
        "request": [("Кассовые расхождения ВСП и акты пересчёта за период", "ГОСБ")],
        "check": ["Пересчитывают ли при клиенте и выдают ли чек", "Как разбирают претензии по наличным"]},
    "оглашение_или_утечка_данных": {
        "label": "утечка или оглашение данных клиента", "short": "утечка данных",
        "benign": "звонок мошенников совпал с визитом случайно",
        "resolve": "кто открывал данные клиента в системе в день визита",
        "request": [("Журнал доступа к данным клиентов ВСП за период", "центр")],
        "check": ["Озвучивают ли суммы и данные клиента вслух в зале", "Видны ли экраны сотрудников клиентам"]},
    "отказ_в_жалобе_или_давление": {
        "label": "не принимают жалобу, давят на клиента", "short": "жалобу не приняли",
        "benign": "обращение приняли в другом канале",
        "resolve": "реестр обращений ВСП за дату",
        "request": [("Обращения клиентов ВСП за период и сроки ответов", "ГОСБ")],
        "check": ["Доступна ли форма или книга жалоб", "Как регистрируют обращения клиентов"]},
    "блат_очередь": {
        "label": "обслуживают по знакомству, в обход очереди", "short": "по знакомству",
        "benign": "приоритетное обслуживание по программе (СберПервый, маломобильные клиенты)",
        "resolve": "журнал электронной очереди",
        "request": [("Журнал электронной очереди: талоны, выданные сотрудниками", "ГОСБ")],
        "check": ["Кто и как выдаёт талоны", "Обслуживание без талона и вне очереди"]},
    "посредник": {
        "label": "посредники и «свой» менеджер", "short": "посредник",
        "benign": "партнёр банка работает по договору",
        "resolve": "реестр партнёров и сделки с их участием",
        "request": [("Сделки ВСП с участием посредников за период — по менеджерам", "ГОСБ")],
        "check": ["Как распределяют клиентов партнёров между менеджерами"]},
    "накрутка_продаж": {
        "label": "продажи «для плана»: оформите, потом откажетесь", "short": "для плана",
        "benign": "клиент сам согласился на продукт и передумал",
        "resolve": "отказы от продуктов в первые 30 дней по сотрудникам",
        "request": [("Отказы и закрытия продуктов в первые 30 дней — по сотрудникам ВСП", "центр")],
        "check": ["Что говорят клиенту о возможности сразу отказаться"]},
    "операционная_ошибка": {
        "label": "ошибка операции с ущербом клиенту", "short": "ошибка операции",
        "benign": "единичная ошибка, исправленная банком",
        "resolve": "исправление по обращению клиента",
        "request": [("Исправительные операции ВСП за период", "ГОСБ")], "check": []},
    "навязывание_без_оформления": {
        "label": "навязывают, но клиент отказался", "short": "навязывание",
        "benign": "обычное предложение продукта", "resolve": "—", "request": [], "check": []},
    "другое": {"label": "другая история", "short": "другое", "benign": "", "resolve": "", "request": [], "check": []},
}

RULE_TO_ACTION = {
    "unauthorized": "оформление_без_согласия", "credit_not_mine": "оформление_без_согласия",
    "mis_selling": "ложь_об_обязательности", "complex_product": "подмена_продукта",
    "phone_access": "доступ_к_телефону_или_аккаунту", "leak": "оглашение_или_утечка_данных",
    "cash": "касса_недостача_или_чужой_платеж", "money_lost": "другое", "deposit_vanished": "касса_недостача_или_чужой_платеж",
    "self_interest": "другое", "rule_bypass": "другое", "favoritism": "блат_очередь",
    "refused_complaint": "отказ_в_жалобе_или_давление", "vulnerable": "другое", "inheritance": "другое",
}
STAFF = {"сотрудник", "отделение", "неясно", ""}
MIN_DATA_2Y = 10          # отзывов за 2 года — данных достаточно для сравнения
FORBIDDEN = re.compile(r"выявлен|мошенник|обманыва|накрутк|доказан|сговор|фейк", re.I)


def pl(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def _sim(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def stories_from(recs, llm: dict[str, dict], rule_cases: list[dict]) -> tuple[list[dict], str]:
    """Истории клиентов. Источник — карточки модели, где они есть; иначе слова-правила."""
    by_rid = {r.rid: r for r in recs}
    out = []
    if llm:
        for rid, d in llm.items():
            r = by_rid.get(rid)
            if r is None or not d.get("relevant") or (d.get("other_vsp") or "").strip():
                continue
            fault = (d.get("fault") or "").strip().lower()
            if fault not in STAFF:                       # политика банка, третьи лица — не про отделение
                continue
            acts = [a for a in (d.get("actions") or []) if a in ACTIONS] or ["другое"]
            emp = (d.get("employee_name") or "").strip()
            low = (r.text or "").lower().replace("ё", "е")
            if emp.lower() in ("null", "none", "нет", "-") or not all(
                    w.lower().replace("ё", "е")[:4] in low for w in emp.split()[:2]):
                emp = ""                                     # имя должно быть в самом отзыве
            out.append(_story(r, severity=int(d.get("severity") or 0), fault=fault, actions=acts,
                              quote=d.get("quote") or "", quote_ok=llm_quote_ok(d.get("quote") or "", (r.extra or {}).get("raw_text") or r.text or ""),
                              episode=d.get("episode") or "", scheme=d.get("scheme_free") or "",
                              vulnerable=bool(d.get("vulnerability")), employee=emp,
                              vuln_types=[str(v) for v in (d.get("vulnerability") or []) if v][:4],
                              money=d.get("money_rub"), products=d.get("products") or [], source="модель",
                              intermediary="посредник" in acts))
        return _mark_dups(out), "модель"
    by: dict[str, list[dict]] = defaultdict(list)
    for c in rule_cases:
        by[c["review_id"]].append(c)
    for rid, cs in by.items():
        r = by_rid.get(rid)
        if r is None:
            continue
        sev = max(c["severity"] for c in cs)
        acts = sorted({RULE_TO_ACTION.get(c["type"], "другое") for c in cs})
        top = max(cs, key=lambda c: c["severity"])
        out.append(_story(r, severity=sev, fault="неясно", actions=acts, quote=top["quote"], quote_ok=True,
                          episode="", scheme="", vulnerable=any(c["type"] == "vulnerable" for c in cs),
                          employee="", money=None, products=[], source="слова", intermediary=False))
    return _mark_dups(out), "слова"


def _mark_dups(stories: list[dict]) -> list[dict]:
    """Один и тот же текст у нескольких отделений (автор разослал жалобу по карточкам или карточка
    привязана не к тому ВСП): dup_points — у скольких ЕЩЁ отделений есть такая же история."""
    import re
    # по самому тексту отзыва (s["text"] — нормализованный: слова, нижний регистр): копии одного отзыва
    # на разных карточках модель цитирует разными кусками, а текст у них один
    key = lambda s: (s.get("text") or "")[:160] if len(s.get("text") or "") >= 60 else \
        re.sub(r"[^а-яёa-z0-9]+", " ", (s.get("quote") or "").lower()).strip()[:160]
    pts: dict[str, set] = defaultdict(set)
    for s in stories:
        k = key(s)
        if len(k) >= 40:
            pts[k].add(s["point"])
    for s in stories:
        s["dup_points"] = max(0, len(pts.get(key(s), ())) - 1)
    return stories


def _story(r, **kw) -> dict:
    return {"rid": r.rid, "point": r.point, "branch_id": r.branch_id, "gosb": r.gosb, "subject": r.subject,
            "at": r.at, "rating": r.rating, "author": r.author or r.rid, "author_total": r.author_total,
            "text": r.text, **kw}


def _date(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


def assess(points: dict[str, dict], stories: list[dict], recs, people_rows: list[dict], *, now: datetime,
           source: str) -> dict[str, dict]:
    """Признаки, «почему», уверенность, безобидные объяснения и чек-листы по каждому отделению."""
    two_y = now - timedelta(days=730)
    vol: Counter = Counter()
    for r in recs:
        if r.at >= two_y:
            vol[r.point] += 1
    sto: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        if s["at"] >= two_y:
            sto[s["point"]].append(s)
    # соседи по ГОСБ: тяжёлые истории (2–3) на отзыв, эмпирический Байес
    heavy = {p: [s for s in ss if s["severity"] >= 2] for p, ss in sto.items()}
    grp = {p: (v.get("gosb") or v.get("subject") or "net") for p, v in points.items()}
    g_h: Counter = Counter()
    g_n: Counter = Counter()
    for p, n in vol.items():
        g = grp.get(p, "net")
        g_h[g] += len({s["author"] for s in heavy.get(p, [])})
        g_n[g] += n
    net_rate = (sum(g_h.values()) + 1) / (sum(g_n.values()) + 100)
    praise_by_point: dict[str, list[dict]] = defaultdict(list)
    for x in people_rows:
        if x["kind"] == "praise_wave":
            praise_by_point[x["point"]].append(x)
    out = {}
    for p, ss in sto.items():
        if p not in points:
            continue
        hv = heavy.get(p, [])
        authors_h = {s["author"] for s in hv}
        signals, context = [], []
        # А. тяжёлые истории разных клиентов
        acts = Counter(a for s in hv for a in set(s["actions"]) if a not in ("другое", "навязывание_без_оформления"))
        top_act, top_n = (acts.most_common(1)[0] if acts else (None, 0))
        top_authors = {s["author"] for s in hv if top_act in s["actions"]} if top_act else set()
        span = (max(s["at"] for s in hv) - min(s["at"] for s in hv)).days if hv else 0
        if len(authors_h) >= 2:
            signals.append({"key": "A", "n": len(authors_h)})
        # Б. повтор одной схемы (тот же признак, если это те же истории)
        if len(top_authors) >= 2 and not (signals and top_authors == authors_h):
            signals.append({"key": "B", "n": len(top_authors), "action": top_act})
        # В. одно имя у разных клиентов
        names: dict[str, list[dict]] = defaultdict(list)
        for s in hv:
            nm = (s.get("employee") or "").strip()
            if nm and len(nm) >= 3:
                names["|".join(sorted(stem(w) for w in nm.split()[:3]))].append(s)
        same_name = max(names.values(), key=lambda v: len({s["author"] for s in v}), default=[])
        if len({s["author"] for s in same_name}) >= 2:
            signals.append({"key": "C", "n": len({s["author"] for s in same_name}), "name": same_name[0]["employee"]})
        # Г. необычно для похожих
        g = grp.get(p, "net")
        rate_g = (g_h[g] - len(authors_h) + net_rate * 50) / max(1.0, g_n[g] - vol[p] + 50)
        expected = rate_g * vol[p]
        ratio = len(authors_h) / expected if expected > 0 else None
        if len(authors_h) >= 2 and ratio and ratio >= 3 and vol[p] >= MIN_DATA_2Y:
            signals.append({"key": "D", "ratio": round(ratio, 1), "expected": round(expected, 2)})
        # Д. люди вокруг
        inter = [s for s in ss if s.get("intermediary")]
        wave = praise_by_point.get(p, [])
        complained = {k for k in names}
        wave_hit = [w for w in wave if any(set(k.split("|")) & set(w.get("key", "").split("|")) for k in complained)]
        if (inter and hv) or wave_hit:
            signals.append({"key": "E", "n": len(inter) + len(wave_hit)})
        vul = sum(1 for s in hv if s.get("vulnerable"))
        if vul:
            context.append(f"пожилые или уязвимые клиенты среди пострадавших — {vul} из {len(hv)}")
        if wave and not wave_hit:
            context.append("после жалоб — волна пятёрок от новых аккаунтов (может быть приглашением банка)")
        severe = [s for s in hv if s["severity"] >= 3 and s.get("quote_ok", True)
                  and (s.get("money") or s.get("products") or s.get("episode"))]
        include = len(signals) >= 2 or bool(severe)
        # уверенность
        hist_authors = {s["author"] for s in hv if (s.get("author_total") or 0) > 1}
        texts = [s["text"] for s in hv]
        similar = any(_sim(a, b) > 0.6 for i, a in enumerate(texts) for b in texts[i + 1:])
        if len(authors_h) >= 3 and len(hist_authors) >= 2 and not similar:
            conf = ("высокая", f"{len(authors_h)} {pl(len(authors_h), 'разный клиент', 'разных клиента', 'разных клиентов')}, "
                               f"у {len(hist_authors)} есть другие отзывы, тексты разные")
        elif len(authors_h) >= 2:
            conf = ("средняя", f"{len(authors_h)} {pl(len(authors_h), 'клиент', 'клиента', 'клиентов')} за "
                               f"{max(1, span // 30)} {pl(max(1, span // 30), 'месяц', 'месяца', 'месяцев')}")
        elif severe:
            conf = ("средняя", "одна очень тяжёлая история с деталями")
        else:
            conf = ("низкая", "одна история")
        if similar:
            conf = ("низкая", conf[1] + "; тексты похожи друг на друга")
        if source == "слова":
            conf = (conf[0], conf[1] + "; найдено по словам — модель ещё читает отзывы")
        # «почему»
        why = []
        n_h = len(authors_h)
        if top_act and len(top_authors) >= 2:
            why.append(f"{len(top_authors)} {pl(len(top_authors), 'клиент', 'разных клиента', 'разных клиентов')} "
                       f"за {max(1, span // 30)} {pl(max(1, span // 30), 'месяц', 'месяца', 'месяцев')} пишут: "
                       f"{ACTIONS[top_act]['label']}")
        elif n_h >= 2:
            why.append(f"{n_h} {pl(n_h, 'клиент', 'разных клиента', 'разных клиентов')} описывают тяжёлые истории в отделении")
        elif severe:
            why.append(f"Клиент пишет: {ACTIONS[severe[0]['actions'][0]]['label']}")
        for sgl in signals:
            if sgl["key"] == "C":
                why.append(f"{sgl['n']} разных клиента называют одного сотрудника ({sgl['name']})")
            if sgl["key"] == "D":
                if sgl["expected"] < 1:
                    why.append(f"в похожих отделениях ГОСБ таких клиентов обычно нет и одного — здесь {n_h}")
                else:
                    exp = f"{sgl['expected']:.1f}".replace(".", ",")
                    why.append(f"в похожих отделениях ГОСБ таких клиентов обычно около {exp} — здесь {n_h}")
            if sgl["key"] == "E":
                why.append("вокруг отделения посредники или одни и те же люди хвалят сотрудника, которого называют в жалобах")
        why = [w for w in why if not FORBIDDEN.search(w)]
        main = hv or ss
        best = max(main, key=lambda s: (s["severity"], bool(s.get("vulnerable")), bool(s.get("money")),
                                        s.get("quote_ok", False), s["at"]))
        recent = now - timedelta(days=180)
        hot = [s for s in hv if s["at"] >= recent and (s["severity"] >= 3 or s.get("vulnerable")
                                                       or (s.get("money") or 0) >= 10000)]
        very = [s for s in hv if s["severity"] >= 3]
        tier = ("urgent" if hot else "converged" if len(signals) >= 3 or (len(signals) >= 2 and very)
                else "watch")
        moneys = [float(s["money"]) for s in hv if isinstance(s.get("money"), (int, float)) and s["money"] > 0]
        act_main = top_act or (best["actions"][0] if best["actions"] else "другое")
        info = ACTIONS.get(act_main, ACTIONS["другое"])
        request, check, seen = [], [], set()
        for a, _ in (acts.most_common(3) or [(act_main, 1)]):
            for t, w in ACTIONS.get(a, ACTIONS["другое"])["request"]:
                if t not in seen:
                    request.append({"text": t, "where": w}); seen.add(t)
            for t in ACTIONS.get(a, ACTIONS["другое"])["check"]:
                if t not in seen:
                    check.append(t); seen.add(t)
        out[p] = {
            "include": include, "signals": signals, "n_signals": len(signals), "context": context,
            "severity": max((s["severity"] for s in ss), default=0), "heavy_clients": n_h,
            "why": why[:3], "confidence": {"level": conf[0], "reason": conf[1]},
            "benign": ({"text": info["benign"], "resolve": info["resolve"]} if info.get("benign") else None),
            "quote": {"text": best["quote"], "date": _date(best["at"]), "rid": best["rid"]},
            "red": any(s["severity"] >= 3 or s.get("vulnerable") or a in (
                "оформление_без_согласия", "касса_недостача_или_чужой_платеж", "оглашение_или_утечка_данных")
                for s in hv for a in s["actions"]),
            "action": act_main, "action_label": info["label"], "request": request, "check": check,
            "people": [{"name": v[0]["employee"], "clients": len({s["author"] for s in v})}
                       for v in sorted(names.values(), key=lambda v: -len(v))][:4],
            "vol_2y": vol[p], "ratio": round(ratio, 1) if ratio else None, "source": source,
            "tier": tier,
            "lead": {"episode": best.get("episode") or "", "severity": best["severity"],
                     "vulnerable": bool(best.get("vulnerable")), "money": best.get("money"),
                     "employee": best.get("employee") or "", "quote_ok": bool(best.get("quote_ok", True)),
                     "dup_points": best.get("dup_points", 0),
                     "labels": [ACTIONS.get(a, ACTIONS["другое"])["short"] for a in best["actions"]][:2]},
            "flags": {"very": sum(1 for s in hv if s["severity"] >= 3), "vulnerable": vul,
                      "named": sum(1 for s in hv if (s.get("employee") or "").strip()),
                      "money_max": max(moneys) if moneys else None, "heavy": len(hv)},
            "acts": [{"short": ACTIONS[a]["short"], "n": n} for a, n in acts.most_common(4)],
        }
    return out


CONF_RANK = {"высокая": 0, "средняя": 1, "низкая": 2}


def order_key(a: dict) -> tuple:
    """Порядок списка: тяжесть сильнейшей истории, уверенность (одинаковые тексты и массовые
    жалобы на продукт — вниз), число независимых признаков, редкость среди похожих."""
    conf = CONF_RANK.get((a.get("confidence") or {}).get("level"), 2)
    return (-a["severity"], conf, -a["n_signals"], -(a.get("ratio") or 0), -a["heavy_clients"])


def map_tiles(points: dict[str, dict], assessed: dict[str, dict], recs, *, now: datetime) -> dict[str, dict]:
    """Доля отделений региона, где за 2 года клиент описал тяжёлую историю (среди отделений с
    достаточными данными); рамка — нижняя граница интервала Уилсона выше доли по сети."""
    two_y = now - timedelta(days=730)
    vol: Counter = Counter(r.point for r in recs if r.at >= two_y)
    by: dict[str, Counter] = defaultdict(Counter)
    for p, v in points.items():
        s = v.get("subject")
        if not s or vol[p] < MIN_DATA_2Y:
            continue
        by[s]["n"] += 1
        a = assessed.get(p)
        by[s]["hit"] += bool(a and a["heavy_clients"] >= 1)
        by[s]["listed"] += bool(a and a["include"])
    tot_n = sum(c["n"] for c in by.values()) or 1
    net = sum(c["hit"] for c in by.values()) / tot_n
    out = {}
    for s, c in by.items():
        n, k = c["n"], c["hit"]
        share = k / n if n else 0.0
        z = 1.96
        lo = ((share + z * z / (2 * n) - z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n))) / (1 + z * z / n)) if n else 0
        out[s] = {"n": n, "hit": k, "listed": c["listed"], "share": round(share, 3), "above": lo > net, "few": n < 15}
    return {"subjects": out, "network_share": round(net, 4)}


def schemes_fallback(stories: list[dict], points: dict[str, dict], *, now: datetime) -> list[dict]:
    """Пока нет кластеров (schemes.py): схемы = действия из карточек, с охватом и новизной."""
    by: dict[str, list[dict]] = defaultdict(list)
    for s in stories:
        if s["severity"] >= 1:
            for a in s["actions"]:
                if a not in ("другое",):
                    by[a].append(s)
    out = []
    recent = now - timedelta(days=90)
    for a, ss in by.items():
        pts = {s["point"] for s in ss}
        gosbs = {s["gosb"] for s in ss if s["gosb"]}
        rec = sum(1 for s in ss if s["at"] >= recent)
        ss_sorted = sorted(ss, key=lambda s: (s["severity"], s["at"]), reverse=True)
        out.append({"id": a, "name": ACTIONS[a]["label"][0].upper() + ACTIONS[a]["label"][1:],
                    "stories": len(ss), "points": len(pts), "gosbs": len(gosbs), "recent": rec,
                    "novel": len(ss) >= 3 and rec / len(ss) >= 0.5, "network_wide": len(gosbs) >= 3,
                    "benign": ACTIONS[a]["benign"], "resolve": ACTIONS[a]["resolve"],
                    "check": ACTIONS[a]["check"] + [t for t, _ in ACTIONS[a]["request"]],
                    "examples": [{"rid": s["rid"], "point": s["point"], "quote": s["quote"], "date": _date(s["at"])}
                                 for s in ss_sorted[:8]],
                    "by_subject": dict(Counter(s["subject"] for s in ss if s["subject"])),
                    "first": min(s["at"] for s in ss).isoformat(), "last": max(s["at"] for s in ss).isoformat()})
    return sorted(out, key=lambda x: (-x["points"], -x["stories"]))


def react_list(stories: list[dict], *, now: datetime, days: int = 14) -> list[dict]:
    """Свежие очень тяжёлые истории — полоса «Требует реакции»."""
    cut = now - timedelta(days=days)
    xs = [s for s in stories if s["severity"] >= 3 and s["at"] >= cut and s.get("quote_ok", True)]
    xs.sort(key=lambda s: s["at"], reverse=True)
    return [{"rid": s["rid"], "point": s["point"], "gosb": s.get("gosb"), "subject": s.get("subject"),
             "quote": s["quote"], "date": _date(s["at"]),
             "action": ACTIONS.get(s["actions"][0], ACTIONS["другое"])["label"]} for s in xs[:300]]


def peer_norms(rows: list[dict], recs, stories: list[dict], *, now: datetime) -> dict[str, dict]:
    """Нормы ГОСБ для сравнения в досье: медианы по отделениям с данными (10+ отзывов за 2 года).
    Показатели: отзывов за год, доля жалоб 1–2★ за год, тяжёлых историй на 100 отзывов за 2 года."""
    import statistics
    year, two_y = now - timedelta(days=365), now - timedelta(days=730)
    n12: Counter = Counter()
    neg12: Counter = Counter()
    n24: Counter = Counter()
    for r in recs:
        if r.at >= two_y:
            n24[r.point] += 1
            if r.at >= year:
                n12[r.point] += 1
                neg12[r.point] += r.rating is not None and r.rating <= 2
    heavy: Counter = Counter()
    for s in stories:
        if s["severity"] >= 2 and s["at"] >= two_y:
            heavy[s["point"]] += 1
    by: dict[str, list] = defaultdict(list)
    for row in rows:
        p, g = row["point"], row["detail"].get("gosb")
        if g and n24[p] >= MIN_DATA_2Y and row["detail"].get("status") in (None, "active"):
            # доля жалоб — только где за год 10+ оценок, иначе доля из 1–2 отзывов шумит
            by[g].append((n12[p], neg12[p] / n12[p] if n12[p] >= MIN_DATA_2Y else None, 100 * heavy[p] / n24[p]))
    out = {}
    for g, xs in by.items():
        negs = [x[1] for x in xs if x[1] is not None]
        out[g] = {"points": len(xs), "n_12m": statistics.median(x[0] for x in xs),
                  "neg_share": round(statistics.median(negs), 3) if negs else None,
                  "heavy_per100": round(statistics.median(x[2] for x in xs), 2)}
    return out


def dumps(x) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)
