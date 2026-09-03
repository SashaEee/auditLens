"""Отчёт как досье к проверке: разделы пишутся по очереди, каждый со своим
полным набором фактов.

ЗАЧЕМ. Отчёт писался одним вызовом, и писатель тонул: на прогоне 31.08 он
получил 1127 фактов и перестал ставить якоря. Лечением стал `per_cell=3` — на
ячейку «объект × характеристика» уходил один заявленный и один наблюдаемый
факт, и ячейка с двадцатью цитатами показывала одну. Отчёт стал тонким по
построению, а не по данным.

Здесь причина устраняется, а не симптом: контекст РАЗДЕЛА в разы меньше
контекста всего отчёта, поэтому раздел получает свои факты целиком. Карта
условий — все факты о Сбере и его дочерних компаниях, голос клиента — все
цитаты, сгруппированные по объектам.

Второе, чего не было: точка отсчёта и право на суждение. Читатель —
руководитель проверки в Сбере, и каждое сравнение строится относительно Сбера,
а каждый раздел обязан закончиться выводом. Прежний промпт состоял из одних
запретов, и модель послушно выдавала перечень фактов без позиции.

Порядок написания отличается от порядка чтения: резюме и «что проверять»
нельзя написать раньше материала, поэтому они пишутся последними и
вставляются наверх.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import AsyncIterator

from . import runstate
from . import viz as al_viz
from .engine import stream_report as _stream

log = logging.getLogger(__name__)

# Якорный субъект: точка зрения отчёта. Кондуктор добавляет его в subjects
# всегда, когда вопрос про рынок; если его нет (вопрос про документ
# регулятора) — точкой отсчёта становится первый субъект или ничего.
ANCHOR = "sberbank"

# Порядок ЧТЕНИЯ. Резюме и «что проверять» стоят впереди, потому что
# руководитель проверки читает сверху вниз и решение принимает по ним.
READING_ORDER = ("summary", "checks", "conditions", "market", "voice",
                 "regulatory", "conflicts")
# Порядок НАПИСАНИЯ: сначала материал, потом суждение по нему.
WRITING_ORDER = ("conditions", "market", "voice", "regulatory", "conflicts",
                 "checks", "summary")
LEAD = ("summary", "checks")          # пишутся последними, показываются первыми

TITLES = {
    "summary": "Резюме для руководителя проверки",
    "checks": "Что проверять",
    "conditions": "Карта условий",
    "market": "Сравнение объектов",
    "voice": "Голос клиента",
    "regulatory": "Нормативная рамка",
    "conflicts": "Расхождения в источниках",
}


def titles(plan) -> dict[str, str]:
    """Заголовки под точку отсчёта. «Сбер против рынка» над рейтингом
    коллекторских агентств — такие вопросы есть, и заголовок там врал бы."""
    out = dict(TITLES)
    labels = dict(getattr(plan, "subject_labels", None) or {})
    a = anchor_of(plan)
    if not a:
        return out
    name = labels.get(a, a)
    subs = [labels.get(s, s) for s in subsidiaries_of(plan)]
    out["conditions"] = (f"Карта условий: {name}" + (" и дочерние компании" if subs else ""))
    out["market"] = f"{name} против рынка"
    return out

# Мягкие потолки на раздел. Не голодный паёк, а защита от вырождения: сотня
# одинаковых «ставка 1%» с разных страниц не добавляет знания.
_MARKET_PER_CELL = 8
_VOICE_PER_SUBJECT = 25
_CONDITIONS_PER_CELL = 12


# ── Субъекты ─────────────────────────────────────────────────────────────────
def anchor_of(plan) -> str:
    """Точку зрения называет кондуктор. Вопрос «Домклик против Циана и
    Авито» смотрит глазами Домклика, а не Сбера; вопрос про документ ЦБ —
    ничьими. Код лишь подстраховывает: Сбер, если он среди субъектов."""
    explicit = (getattr(plan, "anchor", "") or "").strip()
    subjects = list(getattr(plan, "subjects", None) or [])
    if explicit and explicit in subjects:
        return explicit
    if ANCHOR in subjects:
        return ANCHOR
    # «Первый субъект» здесь НЕ подставляется: рейтинг коллекторских агентств
    # или тарифы управляющих компаний своей стороны не имеют, и назначать её
    # значило бы выделять случайный объект. Нет якоря — ровное сравнение.
    return ""


def subsidiaries_of(plan) -> list[str]:
    """Дочерние компании якорного субъекта, если кондуктор их назвал.
    Сам якорь из списка вычитается: когда точка отсчёта — дочка, кондуктор
    порой вписывает её и сюда, и заголовок обещает «дочерние компании»,
    которых нет."""
    a = anchor_of(plan)
    return [s for s in (getattr(plan, "subsidiaries", None) or []) if s and s != a]


def anchor_family(plan) -> set[str]:
    a = anchor_of(plan)
    return ({a} if a else set()) | set(subsidiaries_of(plan))


# ── Отбор фактов под раздел ──────────────────────────────────────────────────
def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", (v or "").strip().lower())


def _dedupe(facts, key):
    seen, out = set(), []
    for f in facts:
        k = key(f)
        if k in seen:
            continue
        seen.add(k)
        out.append(f)
    return out


def _cap_per(facts, key, cap: int):
    """Не больше cap фактов на группу. Разные значения — вперёд: они несут
    больше, чем повторы одного и того же с разных страниц."""
    groups: dict = {}
    for f in facts:
        groups.setdefault(key(f), []).append(f)
    out = []
    for g in groups.values():
        distinct = _dedupe(g, lambda f: _norm(f.value))
        rest = [f for f in g if f not in distinct]
        out.extend((distinct + rest)[:cap])
    return out


_PLACEHOLDER = re.compile(
    r"^\s*(—|-|–|n/?a|null|none|нет( данных)?|не (указан|раскрыт|найден|установлен)\w*"
    r"|данных нет|отсутствует|не приводится)\s*\.?\s*$", re.I)


def substantive(f) -> bool:
    """Факт, в котором есть что цитировать. Извлекатель иногда отдаёт ячейку с
    пустой цитатой и значением-заглушкой «не указано»: это не факт, а пробел,
    и его собирает модуль пробелов. Писателю такой факт только мешает — он
    тратит абзац на «цитата пуста»."""
    if (f.verbatim or "").strip():
        return True
    return not _PLACEHOLDER.match(str(f.value or ""))


def facts_for(section: str, registry, plan) -> list:
    """Факты раздела. Полные, а не «по три на ячейку»."""
    all_facts = [f for f in registry.facts if substantive(f)]
    fam = anchor_family(plan)
    if section == "conditions":
        # Всё, что заявлено самим Сбером и его дочками, плюс общие факты о
        # предмете (без субъекта) и нормы — они и есть «условия на входе».
        picked = [f for f in all_facts
                  if (f.subject in fam or not f.subject)
                  and f.stance in ("declared", "regulatory")]
        return _cap_per(picked, lambda f: (f.subject, f.attribute),
                        _CONDITIONS_PER_CELL)
    if section == "market":
        picked = [f for f in all_facts if f.stance == "declared"]
        return _cap_per(picked, lambda f: (f.subject, f.attribute), _MARKET_PER_CELL)
    if section == "voice":
        picked = [f for f in all_facts if f.stance == "observed"]
        # Сбер и дочки — первыми: по ним самый большой корпус и самый тонкий
        # прежний раздел.
        picked.sort(key=lambda f: (f.subject not in fam, f.subject, f.date or ""),
                    )
        return _cap_per(picked, lambda f: f.subject, _VOICE_PER_SUBJECT)
    if section == "regulatory":
        norms = [f for f in all_facts if f.stance == "regulatory"]
        attrs = {f.attribute for f in norms}
        # К нормам — заявленное Сбером по тем же характеристикам: расхождение
        # видно только рядом.
        own = [f for f in all_facts
               if f.subject in fam and f.stance == "declared" and f.attribute in attrs]
        return norms + own
    if section == "conflicts":
        out = []
        for (subj, attr), cell in registry.by_cell().items():
            declared = [f for f in cell if f.stance == "declared"]
            if len({_norm(f.value) for f in declared}) > 1:
                out.extend(declared)
        return out
    return []


def render_facts(facts, labels: dict[str, str]) -> str:
    """Тот же формат строки, что у прежнего писателя, — без ограничения числа."""
    lines = []
    for f in facts:
        side = {"declared": "заявлено", "regulatory": "норма регулятора"
                }.get(f.stance, "наблюдается")
        subj = labels.get(f.subject, f.subject) or "общее"
        unit = f" {f.unit}" if f.unit else ""
        when = f" | дата: {f.date}" if f.date else ""
        weak = "" if f.support in ("", "дословно") else f" | опора: {f.support}"
        lines.append(
            f"[f:{f.id}] {subj} | {f.attribute} | {f.value}{unit} "
            f"| сторона: {side}{when}{weak} | источник: {f.url}\n"
            f"      цитата: «{f.verbatim}»")
    return "\n".join(lines)


def facts_index(facts, labels: dict[str, str]) -> str:
    """Короткий указатель фактов для разделов суждения: id, объект,
    характеристика, значение. Цитаты им не нужны — они уже в теле."""
    return "\n".join(
        f"[f:{f.id}] {labels.get(f.subject, f.subject) or 'общее'} | "
        f"{f.attribute} | {f.value}{(' ' + f.unit) if f.unit else ''} | "
        f"{ {'declared': 'заявлено', 'regulatory': 'норма'}.get(f.stance, 'наблюдается') }"
        for f in facts)


# ── Промпты ──────────────────────────────────────────────────────────────────
def _common(plan, question: str, labels: dict[str, str]) -> str:
    intent = (getattr(plan, "intent_summary", "") or "").strip()
    a = anchor_of(plan)
    anchor_name = labels.get(a, a) if a else ""
    subs = [labels.get(s, s) for s in subsidiaries_of(plan)]
    subs_line = (f" и его дочерние компании ({', '.join(subs)})" if subs else "")
    if anchor_name:
        stance = (f"ТОЧКА ОТСЧЁТА — {anchor_name}{subs_line}. Каждое сравнение "
                  f"строится относительно {anchor_name}: не «пять банков в ряд», "
                  f"а «где мы лучше, где хуже и в чём именно». Конкуренты — "
                  f"бенчмарк, не объект выбора.")
    else:
        # Рейтинг коллекторов, тарифы управляющих компаний, документ ЦБ —
        # своей стороны здесь нет, и притягивать её нельзя.
        stance = ("ТОЧКИ ОТСЧЁТА НЕТ: в вопросе нет своей организации. "
                  "Сравнивай объекты между собой ровно, без выделенного.")
    return "\n".join([
        f"Ты — аналитик службы внутреннего аудита. Отчёт по вопросу: «{question}».",
        "Читатель — руководитель проверки в Сбере. Он открывает отчёт на входе "
        "в проверку и принимает по нему решения.",
        "",
        stance,
        "",
        "ФАКТЫ. Ниже — проверенные факты, каждый подтверждён дословной цитатой. "
        "Другого материала нет. Каждое утверждение с числом, условием или "
        "цитатой несёт якорь ровно в виде [f:12] сразу после себя; несколько "
        "фактов — [f:12][f:34]. Числа не пересчитывай и не округляй.",
        "",
        "ТРИ СТОРОНЫ. «заявлено» — со слов самой организации; «наблюдается» — "
        "взгляд со стороны (жалобы, отзывы, разборы); «норма регулятора» — "
        "требование закона или ЦБ. Расхождение между сторонами — предмет "
        "аудита, называй его прямо.",
        "",
        "СУЖДЕНИЕ. Ты обязан не только изложить, но и оценить: что это значит "
        "для проверки, где риск, что из этого следует. Вывод без факта "
        "недопустим, но и факт без вывода бесполезен. Раздел заканчивается "
        "абзацем «Вывод:» — одна-три фразы по существу.",
        "",
        "ЧЕСТНОСТЬ. Аудиторы пишут это в вопросах дословно, и это требование: "
        "«если информации нет или она неполная — не додумывай, честно укажи это "
        "в отчёте». Нет факта по объекту — так и напиши: «по … данных не "
        "нашлось». Объект не работает с продуктом — напиши «не кредитует / не "
        "предоставляет», а не пропускай молча.",
        "",
        "ФОРМА. Русский язык, markdown, заголовки по-русски. Заголовок раздела "
        "уже стоит над твоим текстом: начинай сразу с абзаца, а не с заголовка, "
        "и не комментируй это. Внутри раздела — только подзаголовки ###. Не "
        "пиши список источников, методологию и вводные о том, что источники "
        "«предоставлены». Якорь — только в квадратных скобках [f:N]; запись "
        "(f:N) или f:N без скобок ссылкой не считается. Один и тот же якорь "
        "дважды подряд не ставь.",
        "",
        "ЗАКАЗ АУДИТОРА. Если в вопросе задана форма ответа — «топ-5 тем», "
        "«в виде таблицы», «полный список», «динамика за год», — раздел обязан "
        "её выполнить буквально, а не пересказать своими словами. Если вопрос "
        "ограничен периодом или датой, факты вне периода помечай как "
        "устаревшие и не смешивай с актуальными: у каждого факта есть дата.",
    ] + ([f"", f"ЧТО ХОЧЕТ УЗНАТЬ АУДИТОР: {intent}"] if intent else []))


_SECTION_RULES = {
    "conditions": (
        "РАЗДЕЛ «КАРТА УСЛОВИЙ». Это то, что аудитор обязан знать о продукте "
        "до первого запроса в подразделение. Изложи ВСЕ условия по точке "
        "отсчёта и её дочерним компаниям: ставки, комиссии, лимиты, сроки, "
        "требования, исключения, мелкий шрифт, акционные и постоянные условия "
        "отдельно. Где условие менялось или у одного источника одно, а у "
        "другого другое — покажи оба с датами. Структурируй по характеристикам, "
        "используй таблицы. Норму регулятора по каждой характеристике — рядом, "
        "если она есть. Полнота важнее краткости. Конкуренты в этот раздел не "
        "переданы намеренно — им отведён раздел сравнения; оговорок об их "
        "отсутствии не пиши."
    ),
    "market": (
        "РАЗДЕЛ «СБЕР ПРОТИВ РЫНКА». По каждой характеристике сопоставь точку "
        "отсчёта с каждым конкурентом и прямо скажи: лучше, хуже, паритет, "
        "несопоставимо — и почему. Сводная таблица обязательна: строки — "
        "характеристики, столбцы — объекты, точка отсчёта первым столбцом. "
        "После таблицы — разбор: где точка отсчёта проигрывает, там "
        "конкретно, чем; где выигрывает — тоже. Ранжирование строй только по "
        "существу, с названным критерием; если сопоставимой базы нет — так и "
        "напиши, почему, и что можно сказать без порядка. Если аудитор просил "
        "«топ-N и позицию» — дай ранжированный список и отдельной фразой "
        "место точки отсчёта в нём: «N-й из M по …». Если точки отсчёта нет — "
        "просто рейтинг с критерием."
    ),
    "voice": (
        "РАЗДЕЛ «ГОЛОС КЛИЕНТА». Сначала точка отсчёта: сгруппируй жалобы по "
        "темам, каждую тему — с двумя-тремя ДОСЛОВНЫМИ цитатами в кавычках и с "
        "датой. Пересказ бесполезен: аудитору нужна формулировка заявителя. "
        "Затем конкуренты — тем же образом, короче. Отдельным блоком "
        "«Болит у других — проверить у нас»: какие проблемы конкурентов по "
        "устройству продукта возможны и у точки отсчёта, и что именно "
        "проверить. Где жалобы расходятся с заявленным — назови расхождение. "
        "Если аудитор просил «топ-N тем» — дай ровно ранжированный список тем "
        "с числом жалоб по каждой из фактов раздела, самые частые первыми."
    ),
    "regulatory": (
        "РАЗДЕЛ «НОРМАТИВНАЯ РАМКА». Если предмет вопроса — САМ ДОКУМЕНТ "
        "регулятора (таблица значений, указание, закон), этот раздел — главный: "
        "выпиши из фактов документ полностью и структурно, таблицей со всеми "
        "категориями и значениями, ничего не свёртывая. "
        "Иначе: какие требования регулятора и закона "
        "действуют на предмет вопроса — с якорями на факты-нормы. По каждому "
        "требованию рядом — что заявляет точка отсчёта, и совпадает ли. "
        "Расхождение заявленного с нормой — находка аудита, выдели её. Если "
        "текста самой нормы в фактах нет, а есть только упоминание, — скажи "
        "это прямо: ссылка на закон без его текста доказательством не является."
    ),
    "conflicts": (
        "РАЗДЕЛ «РАСХОЖДЕНИЯ В ИСТОЧНИКАХ». Сравнение объектов уже написано в "
        "предыдущих разделах — не повторяй его, сводных таблиц по условиям не "
        "строй. Здесь только расхождения. По одному объекту и одной "
        "характеристике источники дают разные значения. По каждому такому "
        "случаю объясни, ПОЧЕМУ так могло получиться — разные даты, разные "
        "сегменты клиентов, акция против базового тарифа, редакция страницы, "
        "ошибка агрегатора, — и какое значение аудитору принимать за рабочее "
        "и почему. Не выбирай молча: покажи оба значения с якорями."
    ),
    "checks": (
        "РАЗДЕЛ «ЧТО ПРОВЕРЯТЬ». Ты получил готовое тело отчёта. Преврати его в "
        "план действий для проверки. Три блока:\n"
        "1. Гипотезы — где заявленное точкой отсчёта расходится с практикой или "
        "нормой; каждая гипотеза — одно проверяемое утверждение с якорями.\n"
        "2. Точки проверки — конкретные действия: какой документ запросить у "
        "подразделения, какой договор или тариф открыть, какую выборку "
        "операций взять и что в ней искать. Действие, а не «изучить».\n"
        "3. Болит у других — что из проблем конкурентов возможно у точки "
        "отсчёта по устройству продукта, и как это проверить у себя.\n"
        "Каждый пункт опирается на факты тела отчёта; их якоря приведи. "
        "Расплывчатое («возможны риски») недопустимо. Если вопрос прямо просит "
        "план проверки или спрашивает «что мне проверить» — этот раздел "
        "главный: разверни его подробно, с порядком шагов, объектами проверки "
        "и ожидаемыми документами."
    ),
    "summary": (
        "РАЗДЕЛ «РЕЗЮМЕ». Ты получил готовое тело отчёта и план проверки. "
        "Напиши 5–7 главных выводов для руководителя проверки. Каждый вывод — "
        "связный абзац в две-четыре фразы: с самого важного факта (число, "
        "расхождение, ограничение), затем что это значит для проверки, затем "
        "якоря. Первым выводом — позиция точки отсчёта относительно рынка одной "
        "фразой. Если вопрос содержит прямой вопрос-решение — «стоит ли "
        "реагировать?», «насколько конкурентны условия?», «какие действия "
        "предпринять?» — первый вывод отвечает на него прямо: да или нет, и "
        "почему, с якорями. Не повторяй одно и то же разными словами, не пиши "
        "маркетинговым тоном, не начинай с «в целом». Резюме должно читаться "
        "отдельно от отчёта и не терять смысла."
    ),
}


def section_prompt(section: str, plan, question: str, labels: dict[str, str],
                   *, facts_text: str, prior_text: str = "",
                   gaps_text: str = "") -> str:
    parts = [_common(plan, question, labels), "", _SECTION_RULES[section]]
    if section in LEAD:
        parts += ["", "ТЕЛО ОТЧЁТА (уже написано, опирайся на него):", prior_text]
        if gaps_text:
            parts += ["", "ЧТО НЕ УДАЛОСЬ УСТАНОВИТЬ:", gaps_text]
        parts += ["", "УКАЗАТЕЛЬ ФАКТОВ (для якорей):", facts_text]
    else:
        parts += ["", "ФАКТЫ РАЗДЕЛА:", facts_text]
    return "\n".join(parts)


# ── Запись ───────────────────────────────────────────────────────────────────
def outline(plan, registry) -> list[str]:
    """Разделы, которые реально будут написаны, в порядке чтения. Раздел без
    фактов пропускается — и обещать его в оглавлении нельзя."""
    ttl = titles(plan)
    return [ttl[k] for k in READING_ORDER
            if k in LEAD or facts_for(k, registry, plan)]


async def write_dossier(client, model: str, *, question: str, plan, registry,
                        gaps_text: str = "") -> AsyncIterator[tuple[str, str]]:
    """Пишет разделы по очереди.

    Отдаёт события: ("section", key) в начале раздела тела, ("chunk", text)
    по мере генерации, ("lead", markdown) — резюме и план проверки, готовые
    целиком, чтобы вставить наверх. Раздел без фактов пропускается, а не
    пишется из воздуха.
    """
    labels = dict(getattr(plan, "subject_labels", None) or {})
    ttl = titles(plan)
    body: dict[str, str] = {}
    anchor = anchor_of(plan)
    subjects = [s for s in (getattr(plan, "subjects", None) or []) if s]
    by_id = {f.id: f for f in registry.facts}

    # Дизайнер работает в фоне: пока пишется следующий раздел, к
    # предыдущему рисуется визуализация. В текст сразу встаёт маркер
    # [[VIZ:n]], а готовый блок приходит событием («viz», …) — интерфейс
    # подставляет его по номеру, когда бы он ни пришёл.
    slots: list[str] = []
    reported: set[int] = set()
    tasks: set[asyncio.Task] = set()
    state = runstate.current()      # контекст прогона: задача создаётся после yield
    gate = asyncio.Semaphore(al_viz.CONCURRENCY)

    async def _complete(prompt: str) -> str:
        return "".join([p async for p in _stream_section(client, model, prompt)])

    def _spawn(key: str, facts: list, text: str):
        """Дизайнер раздела — фоновая задача. Факты — только те, что раздел
        сам процитировал: блок не должен спорить с текстом."""
        if key not in al_viz.SECTIONS or len(facts) < al_viz.MIN_FACTS or not (text or "").strip():
            return None
        n = len(slots)
        slots.append(key)
        prompt = al_viz.designer_prompt(
            section=key, title=ttl[key], question=question, anchor=anchor,
            labels=labels, facts_text=render_facts(facts, labels),
            section_text=text, subjects=subjects)

        async def run():
            runstate.bind(state)
            t0 = asyncio.get_running_loop().time()
            try:
                async with gate:
                    answer = await asyncio.wait_for(_complete(prompt), al_viz.TIMEOUT)
            except Exception as e:
                log.info("визуализация %s: %s", key, type(e).__name__)
                return {"n": n, "section": key, "html": "", "logos": {},
                        "reason": f"дизайнер не ответил: {type(e).__name__}"}
            if os.getenv("AL_VIZ_DEBUG"):
                # Сырой ответ дизайнера — для разбора отказов на стенде.
                try:
                    with open(f"/tmp/viz_raw_{key}.html", "w", encoding="utf-8") as fh:
                        fh.write(answer)
                except OSError:
                    pass
            try:
                built = al_viz.build(answer, facts=facts, labels=labels,
                                     section=key, subjects=subjects)
                spent = asyncio.get_running_loop().time() - t0
                if not built.html and built.rejected and spent < al_viz.TIMEOUT / 2:
                    # Одна попытка починить, если первый заход был быстрым:
                    # модель видит причины и свой ответ.
                    try:
                        async with gate:
                            answer2 = await asyncio.wait_for(
                                _complete(al_viz.repair_prompt(prompt, answer, built.rejected)),
                                al_viz.TIMEOUT - spent)
                        built2 = al_viz.build(answer2, facts=facts, labels=labels,
                                              section=key, subjects=subjects)
                        if built2.html or not built2.rejected:
                            log.info("визуализация %s: принята со второй попытки", key)
                            built = built2
                        else:
                            built.rejected.extend("повтор: " + r for r in built2.rejected)
                    except Exception as e:
                        log.info("визуализация %s: повтор не удался — %s", key, type(e).__name__)
            except Exception as e:
                log.exception("визуализация %s: сборка", key)
                return {"n": n, "section": key, "html": "", "logos": {},
                        "reason": f"сборка: {type(e).__name__}"}
            log.info("визуализация %s: %s", key,
                     "принята" if built.html else ("пусто" if not built.rejected else "; ".join(built.rejected)))
            return {"n": n, "section": key, "html": built.html, "logos": built.logos,
                    "reason": "; ".join(built.rejected) if not built.html else ""}

        tasks.add(asyncio.create_task(run()))
        return n

    def _ready() -> list:
        out = []
        for t in [t for t in tasks if t.done()]:
            tasks.discard(t)
            try:
                r = t.result()
            except Exception as e:
                log.info("визуализация: %s", type(e).__name__)
                continue
            if r:
                reported.add(r["n"])
                out.append(("viz", r))
        return out

    def _cited(text: str, cap: int = 60) -> list:
        ids = [int(x) for x in re.findall(r"[\[(]f:(\d+)[\])]", text or "")]
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id][:cap]

    try:
        for key in WRITING_ORDER:
            if key in LEAD:
                continue
            facts = facts_for(key, registry, plan)
            if not facts:
                log.info("досье: раздел %s пропущен — фактов нет", key)
                continue
            yield ("section", key)
            yield ("chunk", f"\n\n## {ttl[key]}\n\n")
            prompt = section_prompt(key, plan, question, labels,
                                    facts_text=render_facts(facts, labels))
            buf = []
            async for piece in al_viz.without_markers(
                    _without_heading(_stream_section(client, model, prompt), ttl[key])):
                buf.append(piece)
                yield ("chunk", piece)
                for ev in _ready():
                    yield ev
            body[key] = "".join(buf)
            log.info("досье: раздел %s — %d фактов, %d символов", key, len(facts),
                     len(body[key]))
            n = _spawn(key, _cited(body[key]), body[key])
            if n is not None:
                yield ("marker", n)
            for ev in _ready():
                yield ev

        if not body:
            return

        prior = "\n\n".join(f"## {ttl[k]}\n\n{body[k]}" for k in WRITING_ORDER
                            if k in body)
        index = facts_index(list(registry.facts), labels)
        lead: dict[str, str] = {}
        lead_slot: dict[str, int | None] = {}
        for key in ("checks", "summary"):
            prompt = section_prompt(key, plan, question, labels, facts_text=index,
                                    prior_text=prior + ("\n\n## " + ttl["checks"]
                                                        + "\n\n" + lead["checks"]
                                                        if "checks" in lead else ""),
                                    gaps_text=gaps_text)
            buf = []
            async for piece in al_viz.without_markers(
                    _without_heading(_stream_section(client, model, prompt), ttl[key])):
                buf.append(piece)
                for ev in _ready():
                    yield ev
            lead[key] = "".join(buf).strip()
            log.info("досье: раздел %s — %d символов", key, len(lead[key]))
            # Дизайнер плана стартует сразу, не дожидаясь резюме: факты —
            # только те, на которые раздел сам сослался.
            lead_slot[key] = _spawn(key, _cited(lead[key]), lead[key])
            for ev in _ready():
                yield ev

        head = ""
        for k in LEAD:
            if not lead.get(k):
                continue
            n = lead_slot.get(k)
            marker = al_viz.LEAD_MARKER.format(n=n) + "\n\n" if n is not None else ""
            if k in al_viz.BEFORE_TEXT:
                head += f"## {ttl[k]}\n\n{marker}{lead[k]}\n\n"
            else:
                head += f"## {ttl[k]}\n\n{lead[k]}\n\n{marker}"
        if head:
            yield ("lead", head)

        # Дорисовать, что не успело, — в общий бюджет, отдавая готовое по
        # мере готовности. Дольше не ждём: отчёт без одной картинки лучше
        # отчёта, который не пришёл.
        deadline = asyncio.get_running_loop().time() + al_viz.FINAL_WAIT
        while tasks:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                break
            yield ("status", f"Рисую карточки: осталось {len(tasks)}")
            await asyncio.wait(tasks, timeout=min(left, 10.0),
                               return_when=asyncio.FIRST_COMPLETED)
            for ev in _ready():
                yield ev
        for t in tasks:
            t.cancel()
            log.info("визуализация: не успела за %.0f с", al_viz.FINAL_WAIT)
        if tasks:
            for n_, key_ in enumerate(slots):
                if n_ in reported:
                    continue
                yield ("viz", {"n": n_, "section": key_, "html": "", "logos": {},
                               "reason": f"дизайнер не успел за {int(al_viz.FINAL_WAIT)} с"})
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


async def _without_heading(pieces: AsyncIterator[str], title: str = "") -> AsyncIterator[str]:
    """Модель, вопреки правилу, порой начинает раздел с заголовка — а он уже
    стоит над текстом, и читатель видит его дважды.

    Поток режет текст на куски произвольно: первый кусок бывает «##» без
    пробела, и по нему ещё ничего не решить. Поэтому, пока текст начинается с
    решётки, ждём конца строки. Заголовок, повторяющий название раздела,
    выбрасываем сколько бы их ни было («# Голос клиента» и под ним
    «## Голос клиента»); иной заголовок первого-второго уровня — только один:
    следующий за ним подзаголовок — уже содержание."""
    buf = ""
    dropped_any = False
    async for piece in pieces:
        if buf is None:
            yield piece
            continue
        buf += piece
        while True:
            head = buf.lstrip()
            if not head:
                break
            if head[0] != "#":
                out, buf = head, None
                yield out
                break
            nl = head.find("\n")
            if nl < 0:
                break                         # строка ещё не закончилась — ждём
            line, rest = head[:nl], head[nl + 1:]
            # Заголовок с названием раздела — лишний всегда; чужой — только
            # если он самый первый: после названия он уже содержание.
            if _LEAD_HEADING.match(line) and (_same_title(line, title) or not dropped_any):
                dropped_any = True
                buf = rest
                continue
            out, buf = head, None             # подзаголовок ### или чужой после названия — содержание
            yield out
            break
    if buf:
        yield buf


_LEAD_HEADING = re.compile(r"#{1,2} ")
_WORD = re.compile(r"[а-яёa-z0-9]{4,}")


def _same_title(line: str, title: str) -> bool:
    """«## Голос клиента» против «Голос клиента», «# Сбер против рынка» против
    «Сбербанк против рынка»: совпадение по значимым словам, а не по буквам."""
    a = {w[:5] for w in _WORD.findall(line.lower())}
    b = {w[:5] for w in _WORD.findall(title.lower())}
    if not a or not b:
        return False
    return len(a & b) >= max(1, min(len(a), len(b)) // 2 + (1 if min(len(a), len(b)) > 1 else 0))


async def _stream_section(client, model: str, prompt: str) -> AsyncIterator[str]:
    """Один раздел одним вызовом. Роль и правила уже внутри промпта, поэтому
    системное сообщение короткое — оно лишь фиксирует режим."""
    async for piece in _stream(client, model, question="", plan=None,
                               context="", raw_prompt=prompt):
        yield piece
