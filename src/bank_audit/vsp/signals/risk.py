"""«Где стоит проверить»: истории клиентов → три полки и досье отделения.

История клиента — отзыв, в котором модель увидела эпизод для проверки отделения (llm_read:
карточка, для тяжёлых — ещё и уточнение: тип, кому выгода, умысел, произошло ли здесь).
Пока модель не прочитала отзывы, истории берутся из слов-правил (risks.py) с пометкой
«предварительно».

Число тяжёлых историй у отделения само по себе почти ничего не значит: при полутора
историях на сто прочитанных отзывов «2 клиента за 2 года» случайность даёт у тех же
четырёх сотен отделений, что и данные (аудит 10.11). Поэтому полок три:

  1. «Выделяется наверняка» — клиентов с тяжёлыми историями значимо больше, чем ожидалось
     по числу прочитанных отзывов (с поправкой на то, что сравниваем тысячи отделений);
  2. «Виден один почерк» — один и тот же способ действия описывают три и больше разных
     клиентов: плотно во времени и чаще, чем по сети, либо так прочитала модель, когда
     смотрела все истории отделения разом (branch_llm);
  3. отдельные тяжёлые случаи — это очередь случаев, а не отделений (case_queue).

Один клиент считается один раз (clients.py). Карточка центрального офиса в сравнение не идёт.
Числа считает код, слова — шаблоны; вину не утверждаем.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import clients as clients_mod
from .kinds import BACKGROUND, INSIDE, KINDS, kind_name
from .llm_read import quote_ok as llm_quote_ok
from .people import stem
from .stats import Exposure, bh, excess as hot_excess, poisson_tail, story_cell, ym

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
# карточка центрального офиса: на неё пишут жалобы на банк в целом, отделением в смысле вкладки она не является
HQ_CODES = {c.strip() for c in os.getenv("VSP_HQ_CODES", "9038/1919").split(",") if c.strip()}
SURE_Q = 0.05             # доля ложных находок на полке «выделяется наверняка»
PATTERN_MIN = 3           # столько разных клиентов должно описать один способ действия
PATTERN_WINDOW_D = 365
PATTERN_Q = 0.10          # доля ложных находок на полке «виден один почерк»
WINDOWS = (("2 года", 730), ("5 лет", 1826))
CASE_MIN = 5              # балл, с которого случай попадает в очередь случаев
_CODE = re.compile(r"(?<!\d)(\d{2,4})\s*/\s*0*(\d{1,5})(?!\d)")
_W = re.compile(r"[а-яёa-z0-9]+")
_ADDR_STOP = {"ул", "улица", "дом", "д", "г", "город", "обл", "область", "проспект", "пр", "пр-т", "пр-кт", "отделение",
              "офис", "сбербанк", "сбер", "банк", "россия", "корпус", "пом", "помещение", "лит", "стр", "строение",
              "край", "район", "республика", "мкр", "микрорайон"}


def pl(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def code_key(name: str | None) -> str | None:
    """«№ 9038/01919» → «9038/1919»: код отделения без ведущих нулей — для сравнения."""
    m = _CODE.search(name or "")
    return f"{int(m.group(1))}/{int(m.group(2))}" if m else None


def _addr_tokens(s: str | None) -> set:
    return {w for w in _W.findall((s or "").lower().replace("ё", "е")) if w not in _ADDR_STOP and not (w.isdigit() and len(w) == 6)}


def kind_from_actions(actions: list, products: list | None = None) -> int:
    """Тип истории по действиям карточки — для историй, которые этап «уточнение» не читал (старые и лёгкие)."""
    a, pr = set(actions or []), set(products or [])
    if "касса_недостача_или_чужой_платеж" in a:
        return 1
    if "доступ_к_телефону_или_аккаунту" in a:
        return 9
    if "оглашение_или_утечка_данных" in a:
        return 11
    if "посредник" in a:
        return 13
    if "биометрия" in a:
        return 15
    if "подмена_продукта" in a:
        if "кредитная карта" in pr:
            return 5
        if "вклад" in pr and pr & {"страхование", "НПФ/ПДС/НСЖ"}:
            return 6
        return 7
    if "оформление_без_согласия" in a:
        return 4 if pr & {"кредитная карта", "потребкредит"} else 7
    if a & {"ложь_об_условиях", "ложь_об_обязательности", "обусловливание"}:
        return 20 if pr & {"потребкредит", "ипотека/сделка"} else 21
    if a & {"наказание_за_отказ", "накрутка_продаж"}:
        return 21
    if "отказ_в_жалобе_или_давление" in a:
        return 22
    if "блат_очередь" in a:
        return 14
    if "операционная_ошибка" in a:
        return 23
    return 25


def _is_self(other: str, place: dict | None) -> bool:
    """«Другое отделение», названное моделью, — на самом деле это же отделение (его код или адрес)."""
    if not place:
        return False
    k = code_key(other)
    if k and k == place.get("code"):
        return True
    o = _addr_tokens(other)
    words = {w for w in o if not w.isdigit()}
    if not words:
        return False
    return len(words & place.get("addr", set())) >= max(1, math.ceil(len(words) / 2))


def places_of(branch_rows: dict) -> tuple[dict, dict]:
    """Справочник отделений для привязки историй: точка → {code, addr, gosb, subject, city, branch_id, hq};
    код → точка. branch_rows — branch_id → строка vsp_branch."""
    places, by_code = {}, {}
    for bid, b in branch_rows.items():
        p = f"b{bid}"
        code = code_key(b.name)
        places[p] = {"code": code, "addr": _addr_tokens(b.address), "gosb": b.gosb, "subject": b.subject,
                     "city": b.city, "branch_id": bid, "hq": bool(code and code in HQ_CODES)}
        if code and (b.status in (None, "active")):
            by_code.setdefault(code, p)
    return places, by_code


def stories_from(recs, llm: dict[str, dict], rule_cases: list[dict], *, details: dict[str, dict] | None = None,
                 motif_of=None, places: dict | None = None, by_code: dict | None = None) -> tuple[list[dict], str]:
    """Истории клиентов. Источник — карточки модели, где они есть; иначе слова-правила.
    details — ответы этапа «уточнение» (тип, выгода, умысел, произошло ли здесь); motif_of(rid, kind, label) → id почерка."""
    by_rid = {r.rid: r for r in recs}
    details, places, by_code = details or {}, places or {}, by_code or {}
    out = []
    if llm:
        for rid, d in llm.items():
            r = by_rid.get(rid)
            if r is None or not d.get("relevant"):
                continue
            fault = (d.get("fault") or "").strip().lower()
            if fault not in STAFF:                       # политика банка, третьи лица — не про отделение
                continue
            sev = int(d.get("severity") or 0)
            det = details.get(rid) or {}
            acts = [a for a in (d.get("actions") or []) if a in ACTIONS] or ["другое"]
            other = (d.get("other_vsp") or "").strip()
            if other.lower() in ("null", "none", "нет", "-"):
                other = ""
            # где это случилось: уточнение знает лучше карточки; карточка без адреса путала отделение само с собой
            point, moved, elsewhere = r.point, None, False
            here = det.get("here")
            named = (det.get("elsewhere") or "").strip() or other
            if here is False or (here is None and other and not _is_self(other, places.get(r.point))):
                target = by_code.get(code_key(named) or "")
                if target and target != r.point:
                    point, moved = target, r.point       # клиент назвал код другого отделения — история его
                elif not target and not _is_self(named, places.get(r.point)):
                    elsewhere = True
            if elsewhere and sev < 2:                     # лёгкая история не про это отделение — не нужна
                continue
            emp = (d.get("employee_name") or "").strip()
            low = (r.text or "").lower().replace("ё", "е")
            if emp.lower() in ("null", "none", "нет", "-") or not all(
                    w.lower().replace("ё", "е")[:4] in low for w in emp.split()[:2]):
                emp = ""                                     # имя должно быть в самом отзыве
            kind = det.get("kind") or kind_from_actions(acts, d.get("products"))
            s = _story(r, severity=sev, fault=fault, actions=acts,
                       quote=d.get("quote") or "",
                       quote_ok=llm_quote_ok(d.get("quote") or "", (r.extra or {}).get("raw_text") or r.text or ""),
                       episode=d.get("episode") or "", scheme=d.get("scheme_free") or "",
                       vulnerable=bool(d.get("vulnerability")), employee=emp,
                       vuln_types=[str(v) for v in (d.get("vulnerability") or []) if v][:4],
                       money=d.get("money_rub"), products=d.get("products") or [], source="модель",
                       intermediary="посредник" in acts,
                       kind=kind, kind2=det.get("kind2"), label=det.get("label"), benefit=det.get("benefit"),
                       intent=det.get("intent"), systemic=det.get("systemic") or [], outcome=det.get("outcome"),
                       escalation=det.get("escalation") or [], window=det.get("window"),
                       sum_kind=det.get("sum_kind"), event_date=det.get("event_date"), detailed=bool(det),
                       elsewhere=elsewhere, moved_from=moved)
            if moved:
                pl_ = places.get(point) or {}
                s.update(point=point, branch_id=pl_.get("branch_id"), gosb=pl_.get("gosb"), subject=pl_.get("subject"))
            s["hq"] = bool((places.get(s["point"]) or {}).get("hq"))
            s["city"] = (places.get(s["point"]) or {}).get("city")
            s["motif"] = motif_of(rid, kind, det.get("label")) if (motif_of and det) else None
            s["inside"] = is_inside(s)
            out.append(s)
        clients_mod.assign(out)
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
        s = _story(r, severity=sev, fault="неясно", actions=acts, quote=top["quote"], quote_ok=True,
                   episode="", scheme="", vulnerable=any(c["type"] == "vulnerable" for c in cs),
                   employee="", money=None, products=[], source="слова", intermediary=False,
                   kind=kind_from_actions(acts), kind2=None, label=None, benefit=None, intent=None, systemic=[],
                   outcome=None, escalation=[], window=None, sum_kind=None, event_date=None, detailed=False,
                   elsewhere=False, moved_from=None, vuln_types=[])
        s["hq"] = bool((places.get(s["point"]) or {}).get("hq"))
        s["city"] = (places.get(s["point"]) or {}).get("city")
        s["motif"], s["inside"] = None, False
        out.append(s)
    clients_mod.assign(out)
    return _mark_dups(out), "слова"


def is_inside(s: dict) -> bool:
    """История «изнутри»: по словам клиента выгода досталась сотруднику или третьим лицам, либо это
    присвоение, подделка, действия за клиента. Только там, где модель уточняла историю."""
    if s.get("severity", 0) < 2 or not s.get("detailed"):
        return False
    if s.get("benefit") in ("сотрудник лично", "третьи лица") or (s.get("intent") or 0) >= 3:
        return True
    return s.get("kind") in (10, 12, 13, 18, 19) and (s.get("intent") or 0) >= 2


def _mark_dups(stories: list[dict]) -> list[dict]:
    """Один и тот же текст у нескольких отделений (автор разослал жалобу по карточкам или карточка
    привязана не к тому ВСП): dup_points — у скольких ЕЩЁ отделений есть такая же история;
    dup_of — rid самой ранней из них (у неё самой — None): в волнах и очагах такой клиент считается один раз."""
    # по самому тексту отзыва (s["text"] — нормализованный: слова, нижний регистр): копии одного отзыва
    # на разных карточках модель цитирует разными кусками, а текст у них один
    key = lambda s: (s.get("text") or "")[:160] if len(s.get("text") or "") >= 60 else \
        re.sub(r"[^а-яёa-z0-9]+", " ", (s.get("quote") or "").lower()).strip()[:160]
    pts: dict[str, set] = defaultdict(set)
    firsts: dict[str, dict] = {}
    for s in sorted(stories, key=lambda s: s["at"]):
        k = key(s)
        if len(k) >= 40:
            pts[k].add(s["point"])
            firsts.setdefault(k, s)
    for s in stories:
        k = key(s)
        s["dup_points"] = max(0, len(pts.get(k, ())) - 1)
        f = firsts.get(k)
        s["dup_of"] = f["rid"] if (s["dup_points"] and f is not None and f["point"] != s["point"]) else None
    return stories


def _story(r, **kw) -> dict:
    return {"rid": r.rid, "point": r.point, "branch_id": r.branch_id, "gosb": r.gosb, "subject": r.subject,
            "at": r.at, "rating": r.rating, "author": r.author or r.rid, "author_total": r.author_total,
            "text": r.text, "src": r.source, **kw}


def _date(d: datetime) -> str:
    return d.strftime("%d.%m.%Y")


def _months(a: datetime, b: datetime) -> int:
    return max(1, round((b - a).days / 30.4))


def _money_loss(s: dict) -> float:
    """Сумма потери по словам клиента: размер продукта (вклада, лимита) потерей не считаем."""
    m = s.get("money")
    if not isinstance(m, (int, float)) or not (0 < m < 50_000_000):
        return 0.0
    return float(m) if s.get("sum_kind") in (None, "потеря") else 0.0


def request_for(kind: int, *, where: str, period: str) -> dict | None:
    """Точный запрос данных банка по типу истории: что запросить, где, за какой период, что снимет вопрос."""
    k = KINDS.get(kind) or {}
    if not k.get("ops"):
        return None
    text = f"{k['ops'][0].upper()}{k['ops'][1:]} — {where}, {period}, в разрезе сотрудника и окна"
    return {"kind": kind, "text": text, "metric": k.get("metric") or "", "benign": k.get("benign") or ""}


def case_score(s: dict, now: datetime) -> tuple[int, list[str]]:
    """Насколько случай важен аудиту: сначала поступок сотрудника, потом обстоятельства клиента."""
    sc, why = 0, []
    if (s.get("intent") or 0) >= 3:
        sc += 5
        why.append("действие за клиента или присвоение")
    elif s.get("severity", 0) >= 3:
        sc += 3
        why.append("очень тяжёлая")
    if s.get("benefit") == "сотрудник лично":
        sc += 4
        why.append("выгода — сотруднику")
    elif s.get("benefit") == "третьи лица":
        sc += 3
        why.append("выгода — третьим лицам")
    if s.get("kind") in INSIDE:
        sc += 2
        why.append(kind_name(s["kind"]).lower())
    if s.get("systemic"):
        sc += 2
        why.append("клиент пишет, что случай не единичный")
    esc = set(s.get("escalation") or [])
    if esc & {"полиция", "суд", "прокуратура", "ЦБ"}:
        sc += 1
        why.append("клиент обратился: " + ", ".join(sorted(esc & {"полиция", "суд", "прокуратура", "ЦБ"})))
    m = _money_loss(s)
    if m >= 10_000:
        sc += 3 if m >= 100_000 else 2 if m >= 30_000 else 1
        why.append(f"{int(m):,} ₽ по словам клиента".replace(",", " "))
    if s.get("vulnerable"):
        sc += 1
        why.append(((s.get("vuln_types") or ["пожилой или уязвимый клиент"])[0]).split("/")[0])
    if (s.get("employee") or "").strip() or s.get("window"):
        sc += 1
    if s["at"] >= now - timedelta(days=120):
        sc += 1
        why.append("свежая")
    if s.get("dup_points") or s.get("copy_of"):
        sc -= 4
    if not s.get("quote_ok", True):
        sc -= 1
    return sc, why


def case_queue(stories: list[dict], *, now: datetime, days: int = 365, limit: int = 600) -> list[dict]:
    """Третья полка: отдельные тяжёлые случаи за год — очередь случаев, а не отделений."""
    cut = now - timedelta(days=days)
    out = []
    for s in stories:
        if s["severity"] < 2 or s["at"] < cut or s.get("kind") in BACKGROUND or s.get("hq") or s.get("elsewhere"):
            continue
        sc, why = case_score(s, now)
        if sc >= CASE_MIN:
            out.append({"rid": s["rid"], "point": s["point"], "score": sc, "why": why[:5], "at": s["at"].isoformat()})
    out.sort(key=lambda x: (x["score"], x["at"]), reverse=True)
    return out[:limit]


def _best_window(ev: list[dict], days: int) -> tuple[int, int]:
    """Самое плотное окно: индексы первой и последней истории (истории отсортированы по дате)."""
    best, j = (0, 0), 0
    for i in range(len(ev)):
        while ev[i]["at"] - ev[j]["at"] > timedelta(days=days):
            j += 1
        if i - j > best[1] - best[0]:
            best = (j, i)
    return best


def assess(points: dict[str, dict], stories: list[dict], exposure: Exposure, *, now: datetime, source: str,
           conclusions: dict[str, dict] | None = None, motif_names: dict[str, str] | None = None,
           wave_motifs: set | None = None, vol_2y: Counter | None = None) -> dict[str, dict]:
    """Полка, причины простыми словами, главный случай и что запросить — по каждому отделению с историями."""
    conclusions, motif_names, wave_motifs = conclusions or {}, motif_names or {}, wave_motifs or set()
    vol_2y = vol_2y or Counter()
    usable = [s for s in stories if not s.get("elsewhere") and not s.get("hq")]
    heavy = [s for s in usable if s["severity"] >= 2 and s.get("kind") not in BACKGROUND and not s.get("dup_of")]
    by: dict[str, list[dict]] = defaultdict(list)
    for s in heavy:
        by[s["point"]].append(s)
    all_by: dict[str, list[dict]] = defaultdict(list)
    for s in usable:
        all_by[s["point"]].append(s)
    first: dict[str, dict] = {}                           # клиент → первая тяжёлая история
    for s in sorted(heavy, key=lambda s: s["at"]):
        first.setdefault(s["client"], s)
    r_all = exposure.rates([(s.get("src"), s["at"].year, s["rating"]) for s in first.values()])
    r_in = exposure.rates([(s.get("src"), s["at"].year, s["rating"]) for s in first.values() if s.get("kind") in INSIDE])
    # 1. «выделяется наверняка»: клиентов значимо больше ожидаемого по прочитанным отзывам
    stat: dict[str, dict] = {}
    tested = 0
    for p in points:
        if (points[p] or {}).get("hq") or not exposure.read(p):
            continue
        tested += 1
        best = None
        for label, days in WINDOWS:
            since = now - timedelta(days=days)
            sm = ym(since)
            for what, rates, inside_only in (("тяжёлыми историями", r_all, False), ("историями «изнутри»", r_in, True)):
                k = len({s["client"] for s in by.get(p, []) if s["at"] >= since
                         and (not inside_only or s.get("kind") in INSIDE)})
                if k < PATTERN_MIN:
                    continue
                e = exposure.expected(p, rates, sm)
                pv = poisson_tail(k, e) if e > 0 else 1.0
                if best is None or pv < best["p"]:
                    best = {"window": label, "what": what, "clients": k, "expected": round(e, 2), "p": pv,
                            "ratio": round(k / e, 1) if e > 0 else None}
        if best:
            best["p"] = min(1.0, best["p"] * len(WINDOWS) * 2)       # четыре проверки на отделение
            stat[p] = best
    sure = bh({p: v["p"] for p, v in stat.items()}, SURE_Q, m=tested)
    # 2. «виден один почерк»: один способ действия у трёх и больше клиентов за год — чаще, чем по остальной
    #    сети за тот же год (с учётом состава отзывов), с поправкой на выбор окна и на число сравнений
    key_msc: dict = defaultdict(Counter)                  # (уровень, ключ) → (месяц, площадка, знак) → клиентов
    for s in first.values():
        c = (ym(s["at"]), *story_cell(s))
        if s.get("kind"):
            key_msc[("kind", s["kind"])][c] += 1
        if s.get("motif") and not str(s["motif"]).endswith("00"):
            key_msc[("motif", s["motif"])][c] += 1
    net_cache: dict[tuple, Counter] = {}
    cand: dict[tuple, dict] = {}
    groups_tested = 0

    def years_of(p: str, default: str) -> float:
        """Сколько лет отзывов у точки: столько годовых окон можно было выбрать."""
        first_m = min((m for (m, _, _) in exposure.point.get(p, {})), default=default)
        return max(1.0, (now.year - int(first_m[:4])) + (now.month - int(first_m[5:7])) / 12)

    ev_clients: dict[str, list[dict]] = {}
    for p in all_by:
        if p not in points:
            continue
        hv = sorted(by.get(p, []), key=lambda s: s["at"])
        ev_client: dict[str, dict] = {}
        for s in hv:
            ev_client.setdefault(s["client"], s)
        cl_all = list(ev_client.values())
        ev_clients[p] = cl_all
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for s in cl_all:
            if s.get("motif") and not str(s["motif"]).endswith("00"):
                groups[("motif", s["motif"])].append(s)
            if s.get("kind") and s["kind"] != 25:
                groups[("kind", s["kind"])].append(s)
        for (lvl, key), ev in groups.items():
            if len(ev) < PATTERN_MIN:
                continue
            groups_tested += 1
            i, j = _best_window(ev, PATTERN_WINDOW_D)
            if j - i + 1 < PATTERN_MIN:
                continue
            b = ym(ev[j]["at"])
            a = ym(ev[j]["at"] - timedelta(days=PATTERN_WINDOW_D))
            inw = [s for s in ev if a <= ym(s["at"]) <= b]
            if (a, b) not in net_cache:
                net_cache[(a, b)] = exposure.net_cells(a, b)
            e, pv = hot_excess(Counter(story_cell(s) for s in inw), exposure.point_cells(p, a, b),
                               Exposure._cells(key_msc[(lvl, key)], a, b), net_cache[(a, b)])
            cand[(p, lvl, key)] = {"p": min(1.0, pv * years_of(p, a)), "expected": e, "ev": inw, "all": ev}
    # вывод модели по отделению (branch_llm) проходит ту же проверку: названные истории против частоты
    # их типов по остальной сети за тот же срок — иначе «почерком» окажется волна, которая идёт везде
    for p, con in conclusions.items():
        if p not in ev_clients or not con.get("pattern") or con.get("confidence") not in ("средняя", "высокая"):
            continue
        rids = set(con.get("rids") or [])
        ev = [s for s in ev_clients[p] if s["rid"] in rids]
        if len(ev) < PATTERN_MIN:
            continue
        groups_tested += 1
        days = max(PATTERN_WINDOW_D, (ev[-1]["at"] - ev[0]["at"]).days + 1)
        a, b = ym(ev[-1]["at"] - timedelta(days=days)), ym(ev[-1]["at"])
        if (a, b) not in net_cache:
            net_cache[(a, b)] = exposure.net_cells(a, b)
        net_ev: Counter = Counter()
        for k in {s.get("kind") for s in ev}:
            net_ev += Exposure._cells(key_msc[("kind", k)], a, b)
        e, pv = hot_excess(Counter(story_cell(s) for s in ev), exposure.point_cells(p, a, b), net_ev, net_cache[(a, b)])
        cand[(p, "llm", "")] = {"p": min(1.0, pv * max(1.0, years_of(p, a) * 365 / days)), "expected": e,
                                "ev": ev, "all": ev}
    pat_ok = bh({k: v["p"] for k, v in cand.items()}, PATTERN_Q, m=groups_tested)
    cand_by: dict[str, list] = defaultdict(list)
    for (pp, lvl, key), c in cand.items():
        if (pp, lvl, key) in pat_ok:
            cand_by[pp].append((lvl, key, c))
    out = {}
    for p, ss_all in all_by.items():
        if p not in points:
            continue
        hv = sorted(by.get(p, []), key=lambda s: s["at"])
        cl_all = ev_clients[p]
        pats = []
        con = conclusions.get(p) or {}
        for lvl, key, c in cand_by.get(p, ()):
            ev, inw = c["all"], c["ev"]
            base = {"clients": len(ev), "window_clients": len(inw), "first": inw[0]["at"].isoformat(),
                    "last": inw[-1]["at"].isoformat(), "months": _months(inw[0]["at"], inw[-1]["at"]),
                    "expected": round(c["expected"], 2), "p": c["p"], "rids": [s["rid"] for s in inw]}
            if lvl == "llm":
                pats.append({"source": "вывод модели", "motif": None, "kind": None, "name": con.get("pattern_name") or "",
                             "period": con.get("period"), "dense": bool(con.get("dense")), "wave": False, **base})
                continue
            name = motif_names.get(key) if lvl == "motif" else None
            kind = ev[0].get("kind") if lvl == "motif" else key
            pats.append({"source": "почерк" if lvl == "motif" else "тип", "motif": key if lvl == "motif" else None,
                         "kind": kind, "name": name or kind_name(kind),
                         "wave": lvl == "motif" and key in wave_motifs, **base})
        # одна причина — один раз: почерк вместо своего типа; вывод модели вместо типа по тем же клиентам
        motif_kinds = {x["kind"] for x in pats if x["source"] == "почерк"}
        llm_rids = {r for x in pats if x["source"] == "вывод модели" for r in x["rids"]}
        pats = [x for x in pats if not (x["source"] == "тип" and (
            x["kind"] in motif_kinds or len(set(x["rids"]) & llm_rids) >= 0.6 * len(x["rids"])))]
        pats.sort(key=lambda x: (x["p"], -x["window_clients"]))
        if con:
            con = {**con, "wave_only": bool(con.get("pattern")) and not any(x["source"] == "вывод модели" for x in pats)}
        st = stat.get(p)
        shelf = "sure" if p in sure else "pattern" if pats else None
        # главный случай: из историй самой причины (почерка), иначе — из всех тяжёлых;
        # поступок важнее обстоятельств, копии — в конец
        pat_rids = set(pats[0]["rids"]) if pats else set()
        main = [s for s in hv if s["rid"] in pat_rids] or hv or ss_all
        best = max(main, key=lambda s: (case_score(s, now)[0], s["severity"], s["at"]))
        acts = Counter(a for s in cl_all for a in set(s["actions"]) if a not in ("другое", "навязывание_без_оформления"))
        kinds = Counter(s["kind"] for s in cl_all if s.get("kind"))
        act_main = (acts.most_common(1)[0][0] if acts else (best["actions"][0] if best["actions"] else "другое"))
        info = ACTIONS.get(act_main, ACTIONS["другое"])
        names: dict[str, list[dict]] = defaultdict(list)
        for s in hv:
            nm = (s.get("employee") or "").strip()
            if nm and len(nm) >= 3:
                names["|".join(sorted(stem(w) for w in nm.split()[:3]))].append(s)
        code = (points[p] or {}).get("code") or ""
        last = hv[-1]["at"] if hv else None
        firstd = hv[0]["at"] if hv else None
        # период запроса — окно причины, если она есть: запрашивать за десять лет незачем
        req_a = datetime.fromisoformat(pats[0]["first"]) if pats else firstd
        req_b = datetime.fromisoformat(pats[0]["last"]) if pats else last
        period = f"{_date(req_a)} — {_date(req_b)}" if hv else ""
        request, check, seen = [], [], set()
        lead_kinds = [x["kind"] for x in pats if x.get("kind")] + [k for k, _ in kinds.most_common(3)]
        for k in lead_kinds:
            rq = request_for(k, where=f"отделение {code}" if code else "это отделение", period=period)
            if rq and k not in seen:
                request.append(rq)
                seen.add(k)
            if len(request) >= 3:
                break
        for a, _ in (acts.most_common(3) or [(act_main, 1)]):
            for t in ACTIONS.get(a, ACTIONS["другое"])["check"]:
                if t not in check:
                    check.append(t)
        why, sig = [], []
        if shelf == "sure":
            n = st["clients"]
            t = (f"{n} {pl(n, 'клиент', 'клиента', 'клиентов')} с {st['what']} за {st['window']}; по числу прочитанных "
                 f"отзывов ожидалось {str(st['expected']).replace('.', ',')}")
            why.append(t)
            sig.append({"key": "sure", "label": "Выделяется наверняка", "text": t})
        for x in pats[:3]:
            n = x["window_clients"]
            if x["source"] == "вывод модели":
                t = (f"{x['clients']} {pl(x['clients'], 'клиент описывает', 'клиента описывают', 'клиентов описывают')} "
                     f"один приём: {x['name']}" + (f" ({x['period']})" if x.get("period") else ""))
                sig.append({"key": "llm", "label": "Один почерк — по чтению всех историй", "text": t})
            else:
                t = (f"{n} {pl(n, 'клиент', 'разных клиента', 'разных клиентов')} за {x['months']} "
                     f"{pl(x['months'], 'месяц', 'месяца', 'месяцев')} описывают одно и то же: {x['name'][0].lower() + x['name'][1:]}")
                sig.append({"key": "pattern", "label": "Один почерк", "text": t})
            why.append(t)
        if not why and hv:
            n = len(cl_all)
            why.append(f"{n} {pl(n, 'клиент', 'клиента', 'клиентов')} с тяжёлыми историями — "
                       "для отделения с таким числом отзывов это в пределах случайного")
        why = [w for w in why if not FORBIDDEN.search(w)]
        vul = sum(1 for s in cl_all if s.get("vulnerable"))
        moneys = [m for m in (_money_loss(s) for s in cl_all) if m > 0]
        two_y = now - timedelta(days=730)
        five_y = now - timedelta(days=1826)
        out[p] = {
            "shelf": shelf, "include": shelf is not None,
            "stat": ({**st, "sig": p in sure} if st else None),
            "patterns": [{k: v for k, v in x.items() if k != "p"} | ({"p": round(x["p"], 6)} if "p" in x else {})
                         for x in pats[:5]],
            "why": why[:3], "sig": sig,
            "severity": max((s["severity"] for s in ss_all), default=0),
            "heavy_clients": len(cl_all),
            "heavy_2y": len({s["client"] for s in hv if s["at"] >= two_y}),
            "heavy_5y": len({s["client"] for s in hv if s["at"] >= five_y}),
            "read": exposure.read(p), "vol_2y": vol_2y.get(p, 0),
            "first": firstd.isoformat() if firstd else None, "last": last.isoformat() if last else None,
            "req_from": req_a.isoformat() if hv else None, "req_to": req_b.isoformat() if hv else None,
            "benign": ({"text": info["benign"], "resolve": info["resolve"]} if info.get("benign") else None),
            "quote": {"text": best["quote"], "date": _date(best["at"]), "rid": best["rid"]},
            "red": shelf == "sure",
            "action": act_main, "action_label": info["label"], "request": request, "check": check[:6],
            "people": [{"name": v[0]["employee"], "clients": len({s["client"] for s in v})}
                       for v in sorted(names.values(), key=lambda v: -len(v))][:4],
            "source": source,
            "lead": {"rid": best["rid"], "episode": best.get("episode") or "", "severity": best["severity"],
                     "vulnerable": bool(best.get("vulnerable")), "money": best.get("money"),
                     "sum_kind": best.get("sum_kind"), "kind": best.get("kind"),
                     "employee": best.get("employee") or "", "quote_ok": bool(best.get("quote_ok", True)),
                     "dup_points": best.get("dup_points", 0),
                     "labels": [ACTIONS.get(a, ACTIONS["другое"])["short"] for a in best["actions"]][:2]},
            "flags": {"very": sum(1 for s in cl_all if s["severity"] >= 3), "vulnerable": vul,
                      "named": sum(1 for s in cl_all if (s.get("employee") or "").strip()),
                      "money_max": max(moneys) if moneys else None, "heavy": len(cl_all),
                      "inside": sum(1 for s in cl_all if s.get("inside"))},
            "acts": [{"short": ACTIONS[a]["short"], "n": n} for a, n in acts.most_common(4)],
            "kinds": [{"kind": k, "name": kind_name(k), "n": n} for k, n in kinds.most_common(4)],
            "conclusion": con or None,
        }
    return out


SHELF_RANK = {"sure": 0, "pattern": 1}


def order_key(a: dict) -> tuple:
    """Порядок списка отделений: сначала значимые, затем почерки; внутри — сила доказательства и свежесть."""
    st = a.get("stat") or {}
    pat = (a.get("patterns") or [{}])[0]
    return (SHELF_RANK.get(a.get("shelf"), 9), st.get("p", 1.0) if a.get("shelf") == "sure" else pat.get("p", 1.0),
            -(pat.get("window_clients") or 0), -(a.get("heavy_2y") or 0), a.get("last") or "")


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
        by[s]["hit"] += bool(a and a["heavy_2y"] >= 1)
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


def react_list(stories: list[dict], *, now: datetime, days: int = 14) -> list[dict]:
    """Свежие очень тяжёлые истории — полоса «Требует реакции»."""
    cut = now - timedelta(days=days)
    xs = [s for s in stories if s["severity"] >= 3 and s["at"] >= cut and s.get("quote_ok", True)
          and not s.get("copy_of")]
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
    seen = set()
    for s in stories:
        if s["severity"] >= 2 and s["at"] >= two_y and not s.get("elsewhere") and (s["point"], s.get("client")) not in seen:
            seen.add((s["point"], s.get("client")))
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
