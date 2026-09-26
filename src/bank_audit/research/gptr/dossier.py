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

from . import brief as al_brief
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
                 "loopholes", "regulatory", "conflicts")
# Порядок НАПИСАНИЯ: сначала материал, потом суждение по нему.
WRITING_ORDER = ("conditions", "market", "voice", "loopholes", "regulatory",
                 "conflicts", "checks", "summary")
LEAD = ("summary", "checks")          # пишутся последними, показываются первыми

TITLES = {
    "summary": "Резюме для руководителя проверки",
    "checks": "Что проверять",
    "conditions": "Карта условий",
    "market": "Сравнение объектов",
    "voice": "Голос клиента",
    "loopholes": "Лазейки и уязвимости",
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
_VOICE_PER_SUBJECT = 25       # прежний общий потолок (запасной путь старого корпуса)
_VOICE_QUOTES = 30            # жалоб из разметки на объект
_VOICE_WEB = 12               # наблюдений из веба на объект
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


def facts_for(section: str, registry, plan, own: dict | None = None) -> list:
    """Факты раздела. Полные, а не «по три на ячейку». own — страницы
    собственных данных AuditLens (url → вид); по умолчанию из состояния прогона."""
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
        own = runstate.current().own_meta if own is None else own
        picked = [f for f in all_facts if f.stance == "observed"]
        # Аналитика жалоб AuditLens (числа среза, сигналы, группы похожих) —
        # целиком и первой: без неё раздел пересказывал цитаты, а на вопрос о
        # всплеске отвечал «не подтверждается». Жалобы из разметки — до своего
        # потолка на объект, наблюдения из веба — отдельным, меньшим.
        stats = [f for f in picked if own.get(f.url, {}).get("kind") == "complaints"]
        quotes = [f for f in picked if own.get(f.url, {}).get("kind") == "review"]
        web = [f for f in picked if f.url not in own]
        # Сбер и дочки — первыми: по ним самый большой корпус.
        order = lambda f: (f.subject not in fam, f.subject, f.date or "")  # noqa: E731
        # Внешнее событие за жалобами (дослежка) объясняет сюжет — его факты
        # идут целиком и раньше прочего веба, иначе их срезал потолок.
        from .followup import EVENT_ATTRIBUTE
        event = [f for f in web if f.attribute == EVENT_ATTRIBUTE]
        web = [f for f in web if f.attribute != EVENT_ATTRIBUTE]
        return (sorted(stats, key=order)
                + _cap_per(sorted(quotes, key=order), lambda f: f.subject, _VOICE_QUOTES)
                + event
                + _cap_per(sorted(web, key=order), lambda f: f.subject, _VOICE_WEB))
    if section == "loopholes":
        return [f for f in all_facts if f.stance == "loophole"]
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


def render_facts(facts, labels: dict[str, str], own: dict | None = None) -> str:
    """Тот же формат строки, что у прежнего писателя, — без ограничения числа."""
    lines = []
    own = runstate.current().own_meta if own is None else own
    for f in facts:
        side = {"declared": "заявлено", "regulatory": "норма регулятора",
                "loophole": "лазейка, раздел «Уязвимости»"}.get(f.stance, "наблюдается")
        if own.get(f.url, {}).get("kind") == "complaints":
            side += ", аналитика жалоб AuditLens"
        elif own.get(f.url, {}).get("kind") == "review":
            side += ", жалоба клиента"
        elif own.get(f.url, {}).get("kind") == "market":
            side += ", витрина «Рынок» AuditLens"
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
        f"{ {'declared': 'заявлено', 'regulatory': 'норма', 'loophole': 'лазейка'}.get(f.stance, 'наблюдается') }"
        for f in facts)


# ── Промпты ──────────────────────────────────────────────────────────────────
def _common(plan, question: str, labels: dict[str, str]) -> str:
    intent = (getattr(plan, "intent_summary", "") or "").strip()
    a = anchor_of(plan)
    anchor_name = labels.get(a, a) if a else ""
    subs = [labels.get(s, s) for s in subsidiaries_of(plan)]
    subs_line = (f" и его дочерние компании ({', '.join(subs)})" if subs else "")
    if anchor_name:
        stance = (f"СВОЙ БАНК — {anchor_name}{subs_line}. Каждое сравнение "
                  f"строится относительно {anchor_name}: не «пять банков в ряд», "
                  f"а «где мы лучше, где хуже и в чём именно». Конкуренты — "
                  f"бенчмарк, не объект выбора.")
    else:
        # Рейтинг коллекторов, тарифы управляющих компаний, документ ЦБ —
        # своей стороны здесь нет, и притягивать её нельзя.
        stance = ("СВОЕГО БАНКА НЕТ: в вопросе нет своей организации. "
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
        "абзацем «Вывод:» — одна-три фразы по существу; у подразделов своих "
        "«Вывод:» и «Итогов» нет.",
        "",
        "ГИПОТЕЗА — НЕ ПРИГОВОР. Если довод банка может оказаться правомерным "
        "(например, операция была переводом, а не покупкой по карте), не объявляй "
        "его некорректным: назови, какой факт решит вопрос и где его взять. "
        "Прогноз — только из фактов: если событие ещё впереди (дата концерта, "
        "вступление нормы в силу), скажи, что изменится после этой даты.",
        "",
        "ЧЕСТНОСТЬ. Аудиторы пишут это в вопросах дословно, и это требование: "
        "«если информации нет или она неполная — не додумывай, честно укажи это "
        "в отчёте». Не додумывай причин и чисел, которых нет в фактах. Объект не "
        "работает с продуктом — напиши «не кредитует / не предоставляет», а не "
        "пропускай молча.",
        "",
        "ФОРМА. Русский язык, markdown, заголовки по-русски. Заголовок раздела "
        "уже стоит над твоим текстом: начинай сразу с абзаца, а не с заголовка, "
        "и не комментируй это. Внутри раздела — только подзаголовки ###. Не "
        "пиши список источников, методологию и вводные о том, что источники "
        "«предоставлены». Якорь — только в квадратных скобках [f:N]; запись "
        "(f:N) или f:N без скобок ссылкой не считается. Один и тот же якорь "
        "дважды подряд не ставь.",
        "",
        "ЦЕЛЬНОСТЬ. Отчёт — один документ, а не стопка отдельных справок. "
        "Пиши о банке и продукте, а не о своих материалах: никаких «в переданных "
        "фактах», «в материале раздела», «в теле отчёта», «точка отсчёта», «факт "
        "№». Не пиши в разделе «Итог отчёта», «Ограничения отчёта», «Что "
        "проверить» — это делают резюме и план проверки. Банк называй по имени.",
        "",
        "ЗАКАЗ АУДИТОРА. Если в вопросе задана форма ответа — «топ-5 тем», "
        "«в виде таблицы», «полный список», «динамика за год», — раздел обязан "
        "её выполнить буквально, а не пересказать своими словами. Если вопрос "
        "ограничен периодом или датой, факты вне периода помечай как "
        "устаревшие и не смешивай с актуальными: у каждого факта есть дата.",
    ] + ([f"", f"ЧТО ХОЧЕТ УЗНАТЬ АУДИТОР: {intent}"] if intent else []))


_SECTION_RULES = {
    "conditions": (
        "РАЗДЕЛ ОБ УСЛОВИЯХ И ПРАВИЛАХ БАНКА. То, что аудитор обязан знать о "
        "продукте или процессе до первого запроса в подразделение: ставки, "
        "комиссии, лимиты, сроки, требования, исключения, порядок и мелкий "
        "шрифт — в той мере, в какой это нужно для ответа на вопрос (фокус — в "
        "брифе). Акционные и постоянные условия — отдельно. Где условие "
        "менялось или источники расходятся — оба значения с датами. Таблицы — "
        "где сравниваются значения. Норму регулятора — рядом, если она есть. "
        "Конкуренты сюда не переданы намеренно — о них не пиши."
    ),
    "market": (
        "РАЗДЕЛ «СБЕР ПРОТИВ РЫНКА». По каждой характеристике сопоставь свой банк "
        "с каждым конкурентом и прямо скажи: лучше, хуже, паритет, "
        "несопоставимо — и почему. Сводная таблица обязательна: строки — "
        "характеристики, столбцы — объекты, свой банк первым столбцом. "
        "После таблицы — разбор: где свой банк проигрывает, там "
        "конкретно, чем; где выигрывает — тоже. Ранжирование строй только по "
        "существу, с названным критерием; если сопоставимой базы нет — так и "
        "напиши, почему, и что можно сказать без порядка. Если аудитор просил "
        "«топ-N и позицию» — дай ранжированный список и отдельной фразой "
        "место своего банка в нём: «N-й из M по …». Если своего банка нет — "
        "просто рейтинг с критерием.\n"
        "Факты «витрина «Рынок» AuditLens» — основа сравнения: все банки по "
        "одной методике и на одну дату, с местом своего банка среди всех "
        "банков. Сводную таблицу и место строй по ним; значения из веба — "
        "дополнение: если они расходятся с витриной, это другой продукт, "
        "другая дата или акция — назови расхождение, не подменяй. Ставку "
        "приписывай только тому банку, у которого она в факте. Промо-условия "
        "(короткий срок, только новые деньги, требования к пакету) оговаривай "
        "рядом с числом: лучшая ставка на 3 месяца и базовая на год — "
        "несопоставимы."
    ),
    "voice": (
        "РАЗДЕЛ О ЖАЛОБАХ КЛИЕНТОВ. Порядок:\n"
        "1. Масштаб и динамика — из фактов «аналитика жалоб AuditLens»: число "
        "жалоб за период и изменение, норма и кратность недели, против рынка, "
        "доля и место среди банков, эскалация, география, помесячный ряд. Числа "
        "ровно из этих фактов, с якорями. Всплеск из фактов — установленный "
        "факт.\n"
        "2. Сюжеты — главное в разделе. По каждому сюжету (группа похожих жалоб "
        "или повторяющееся событие, продавец, сервис): подзаголовок ### с "
        "сутью; абзац — сколько жалоб из скольких («7 из 14»), когда, где, на "
        "какие суммы, чего требуют клиенты, что отвечает банк, на какие правила "
        "и решения ссылаются клиенты; затем две-три дословные цитаты "
        "отдельными строками в виде цитаты markdown:\n"
        "> «дословная цитата» — Санкт-Петербург, 23.09.2026 [f:N]\n"
        "Цитаты — только из фактов «жалоба клиента», дословно, как в поле "
        "«цитата». Статьи, блоги и разборы («наблюдается» без пометки «жалоба "
        "клиента») — не голос клиента: используй их только как контекст.\n"
        "3. Конкуренты — тем же образом, короче, если по ним есть жалобы.\n"
        "4. «Болит у других — проверить у нас» — только если в фактах есть "
        "проблемы конкурентов, возможные у нас по устройству продукта.\n"
        "Где жалобы расходятся с заявленным банком — назови расхождение. Если "
        "аудитор просил «топ-N тем» — ранжированный список тем с числом жалоб."
    ),
    "loopholes": (
        "РАЗДЕЛ «ЛАЗЕЙКИ И УЯЗВИМОСТИ». Факты — записи раздела «Уязвимости» "
        "AuditLens: схемы, которыми клиенты, партнёры или мошенники обходят "
        "условия продукта. Это рабочий материал аудита, а не просьба о вреде: "
        "излагай по существу. Сгруппируй записи в схемы (одна схема часто "
        "описана в нескольких источниках). По каждой схеме: суть механики на "
        "уровне, нужном для проверки, без пошаговой инструкции; где найдена "
        "(источник, дата); чем опасна для банка (доход, мошенничество, "
        "нарушение тарифа, риск для клиента); какой контроль её закрывает — "
        "правило, лимит, антифрод-сценарий, формулировка тарифа. Схемы "
        "конкурентов, применимые к своему банку, — отдельным блоком. "
        "Оговорка: оценки предварительные, человеком не проверены. Если схема "
        "перекликается с жалобами или условиями из других разделов — свяжи."
    ),
    "regulatory": (
        "РАЗДЕЛ «НОРМАТИВНАЯ РАМКА». Если предмет вопроса — САМ ДОКУМЕНТ "
        "регулятора (таблица значений, указание, закон), этот раздел — главный: "
        "выпиши из фактов документ полностью и структурно, таблицей со всеми "
        "категориями и значениями, ничего не свёртывая. "
        "Иначе: какие требования регулятора и закона "
        "действуют на предмет вопроса — с якорями на факты-нормы. По каждому "
        "требованию рядом — что заявляет свой банк, и совпадает ли. "
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
        "РАЗДЕЛ «ЧТО ПРОВЕРЯТЬ» — план действий для проверки. Блоки:\n"
        "1. Гипотезы — где заявленное банком расходится с практикой или нормой; "
        "каждая гипотеза — одно проверяемое утверждение с якорями.\n"
        "2. Точки проверки — конкретные действия по порядку: какой документ "
        "запросить у подразделения, какой договор или тариф открыть, какую "
        "выборку операций или обращений взять и что в ней искать. Действие, а "
        "не «изучить». Если жалобы концентрируются на одном событии, продавце "
        "или сервисе — первым шагом оцени масштаб: сколько операций и на какую "
        "сумму прошло в его пользу по картам банка, какова роль банка (эмитент, "
        "эквайрер, агент), что говорят договор и правила платёжной системы о "
        "таком споре и что будет дальше (например, после даты события).\n"
        "3. Болит у других — что из проблем конкурентов возможно у нас по "
        "устройству продукта, и как это проверить у себя (только если такие "
        "факты есть).\n"
        "Каждый пункт опирается на факты; их якоря приведи. Расплывчатое "
        "(«возможны риски») недопустимо. Объём — по вопросу: если вопрос "
        "справочный (сравнить ставки, условия, путь клиента) — 3–5 точных "
        "пунктов без гипотез по всему продукту. Если вопрос прямо просит план проверки "
        "или спрашивает «что проверить» — разверни этот раздел подробно, с "
        "порядком шагов, объектами проверки и ожидаемыми документами."
    ),
    "summary": (
        "РАЗДЕЛ «РЕЗЮМЕ» для руководителя проверки: 4–6 главных выводов — "
        "развёрнутый главный ответ из брифа. Каждый вывод — связный абзац в "
        "две-четыре фразы: с самого важного факта (число, причина, "
        "расхождение), затем что это значит для проверки, затем якоря. Первый "
        "вывод — прямой ответ на вопрос: если вопрос о рынке или ставках — "
        "позиция банка относительно рынка одной фразой; если это вопрос-решение "
        "(«стоит ли реагировать?», «насколько конкурентны условия?») — да или "
        "нет, и почему. Если в фактах есть всплеск жалоб, сюжет или "
        "существенные лазейки — отдельный вывод о них с числами. Последний "
        "вывод — одно главное, что проверить первым. Не повторяй одно и то же "
        "разными словами, не пиши маркетинговым тоном, не начинай с «в целом». "
        "Резюме читается отдельно от отчёта и не теряет смысла."
    ),
}


def section_prompt(section: str, plan, question: str, labels: dict[str, str],
                   *, facts_text: str, prior_text: str = "",
                   gaps_text: str = "", brief=None, order=None) -> str:
    parts = [_common(plan, question, labels), ""]
    if brief is not None:
        parts += [brief.render(None if section in LEAD else section), ""]
    if section in LEAD:
        parts.append(_LEAD_SCOPE)
    else:
        parts.append(_BODY_SCOPE)
    parts += ["", _SECTION_RULES[section]]
    if section == "checks" and "loopholes" in (order or ()):
        parts += ["", _CHECKS_LOOPHOLES]
    if section in LEAD:
        if prior_text:
            parts += ["", "ТЕЛО ОТЧЁТА (уже написано, опирайся на него):", prior_text]
        if gaps_text:
            parts += ["", "ЧТО НЕ УДАЛОСЬ УСТАНОВИТЬ:", gaps_text]
        parts += ["", "ФАКТЫ ОТЧЁТА (все; для якорей):", facts_text]
    else:
        parts += ["", "ФАКТЫ РАЗДЕЛА:", facts_text]
    return "\n".join(parts)


_CHECKS_LOOPHOLES = (
    "4. Закрыть лазейки — в отчёте есть раздел о лазейках: по каждой "
    "существенной схеме — какой контроль проверить и какую выборку операций "
    "взять.")
_BODY_SCOPE = (
    "ТВОЙ МАТЕРИАЛ. Ты видишь только факты своего раздела; остальные разделы "
    "получили другие. Пиши только о том, что есть в твоих фактах, и держи "
    "линию главного ответа. Чего в твоих фактах нет — не упоминай вовсе: не "
    "пиши «данных нет», «не нашлось», «подтвердить нельзя» — у другого раздела "
    "эти данные могут быть, а пробелы отчёта собирает отдельный блок. Не "
    "объясняй вопрос причинами, которых нет в твоих фактах.")
_LEAD_SCOPE = (
    "ТВОЙ МАТЕРИАЛ. Ты видишь бриф, готовое тело отчёта и все факты (не пиши «в "
    "переданных фактах», «в теле отчёта» — пиши о банке и продукте). Главные "
    "находки тела (событие, причина, довод банка, числа) обязаны попасть в твой "
    "текст — не упрощай их и не спорь с ними. Если на часть вопроса данных нет "
    "во всех фактах — скажи это прямо одной фразой: «по … данных не нашлось».")


# ── Запись ───────────────────────────────────────────────────────────────────
def outline(plan, registry) -> list[str]:
    """Разделы, которые реально будут написаны, в порядке чтения. Раздел без
    фактов пропускается — и обещать его в оглавлении нельзя."""
    ttl = titles(plan)
    return [ttl[k] for k in READING_ORDER
            if k in LEAD or facts_for(k, registry, plan)]


def _brief_model() -> str:
    return (os.getenv("GPTR_BRIEF_MODEL") or os.getenv("LLM_MODEL_REASONING")
            or os.environ["LLM_MODEL_NAME"])


async def write_dossier(client, model: str, *, question: str, plan, registry,
                        gaps_text: str = "", state=None,
                        brief_model: str | None = None) -> AsyncIterator[tuple[str, str]]:
    """Пишет отчёт: бриф → все разделы, резюме и план проверки одновременно.

    Отдаёт события: ("titles", {ключ: заголовок}) и ("outline", [заголовки])
    после брифа; ("section", key) в начале раздела тела, ("chunk", text) по
    мере генерации; ("lead", markdown) — резюме и план проверки целиком,
    чтобы вставить наверх. Раздел без фактов пропускается, а не пишется из
    воздуха; раздел, который бриф счёл не относящимся к вопросу, — тоже.
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
    # Состояние прогона — ЯВНО от вызывающего. Интерфейс получает поток через
    # обёртку, которая исполняет каждый шаг отдельной задачей с копией контекста
    # (analyst._keepalive): взятое из контекста состояние здесь пустое. Так
    # раздел «Голос клиента» не узнавал факты AuditLens и отбрасывал их по
    # лимиту веба — «данных аналитики жалоб нет» при 14 жалобах в реестре.
    state = state or runstate.current()
    runstate.bind(state)
    gate = asyncio.Semaphore(al_viz.CONCURRENCY)

    async def _complete(prompt: str) -> str:
        return "".join([p async for p in _stream_section(client, model, prompt)])

    def _spawn(key: str, facts: list, text: str):
        """Дизайнер раздела — фоновая задача. Факты — только те, что раздел
        сам процитировал: блок не должен спорить с текстом."""
        runstate.bind(state)        # вызывается после yield — контекст чужой
        if key not in al_viz.SECTIONS or len(facts) < al_viz.MIN_FACTS or not (text or "").strip():
            return None
        n = len(slots)
        slots.append(key)
        prompt = al_viz.designer_prompt(
            section=key, title=ttl[key], question=question, anchor=anchor,
            labels=labels, facts_text=render_facts(facts, labels, state.own_meta),
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

    # ── Бриф: главный ответ, тезисы, состав и порядок разделов ──────────
    # Аудит 26.09: разделы, написанные вслепую, противоречили друг другу
    # («данных нет» против «всплеск подтверждён») и повторялись. Бриф видит
    # все факты сразу; каждый раздел пишется вокруг него.
    runstate.bind(state)
    own = state.own_meta
    material = {k: facts_for(k, registry, plan, own) for k in al_brief.BODY_KEYS}
    for k in [k for k, v in material.items() if not v]:
        log.info("досье: раздел %s пропущен — фактов нет", k)
        material.pop(k)
    default = [k for k in al_brief.default_order(plan, state.own_scope) if k in material]
    brief = None
    if material:
        yield ("status", "Строю каркас отчёта: главный ответ и разделы")
        brief = await al_brief.make_brief(
            client, brief_model or _brief_model(), question=question, plan=plan,
            registry=registry, available={k: len(material[k]) for k in default},
            default=default, labels=labels, state=state)
        runstate.bind(state)
    order = [s_["key"] for s_ in brief.sections] if brief else default
    if brief:
        ttl.update({s_["key"]: s_["title"] for s_ in brief.sections if s_["title"]})
    yield ("titles", dict(ttl))
    yield ("outline", [ttl[k] for k in LEAD] + [ttl[k] for k in order])
    if not order:
        return

    # Всё пишется одновременно: разделы тела, план проверки и резюме. Резюме
    # и план больше не ждут тела — главный ответ у них тот же, из брифа, а
    # факты они видят все. Замер 26.09: по очереди тело и суждение занимали
    # ~190 + ~120 с. В поток тело идёт в порядке чтения: первый раздел —
    # живьём, остальные к этому моменту дописаны или дописываются в фоне.
    items = [(key, material[key],
              section_prompt(key, plan, question, labels,
                             facts_text=render_facts(material[key], labels, own),
                             brief=brief, order=order))
             for key in order]
    digest = al_brief.facts_digest(list(registry.facts), labels, state.own_meta)
    queues: dict[str, asyncio.Queue] = {k: asyncio.Queue() for k, _, _ in items}
    gate_w = asyncio.Semaphore(int(os.getenv("GPTR_SECTION_CONCURRENCY", "8")))

    async def _produce(key: str, prompt: str) -> None:
        runstate.bind(state)
        q = queues[key]
        try:
            async with gate_w:
                async for piece in al_viz.without_markers(_demote_headings(
                        _without_heading(_stream_section(client, model, prompt), ttl[key]))):
                    await q.put(piece)
        except Exception as e:  # noqa: BLE001 — отдаём потребителю, он решит
            await q.put(e)
        finally:
            await q.put(None)

    async def _lead(key: str, prior: str) -> str:
        runstate.bind(state)
        prompt = section_prompt(key, plan, question, labels, facts_text=digest,
                                prior_text=prior, gaps_text=gaps_text, brief=brief,
                                order=order)
        async with gate_w:
            text = "".join([p async for p in al_viz.without_markers(
                _without_heading(_stream_section(client, model, prompt), ttl[key]))])
        return _demote_text(text).strip()

    writers = [asyncio.create_task(_produce(k, p)) for k, _, p in items]
    lead_tasks: dict[asyncio.Task, str] = {}
    lead: dict[str, str] = {}
    lead_slot: dict[str, int | None] = {}

    def _take_lead() -> None:
        """Готовые резюме и план — сразу дизайнеру: факты — только те, на
        которые раздел сам сослался."""
        for t, key in lead_tasks.items():
            if t.done() and key not in lead:
                lead[key] = t.result()
                log.info("досье: раздел %s — %d символов", key, len(lead[key]))
                lead_slot[key] = _spawn(key, _cited(lead[key]), lead[key])

    try:
        yield ("status", f"Пишу разделы одновременно: {len(items)}")
        for key, facts, _prompt in items:
            yield ("section", key)
            yield ("chunk", f"\n\n## {ttl[key]}\n\n")
            buf = []
            while True:
                piece = await queues[key].get()
                if piece is None:
                    break
                if isinstance(piece, Exception):
                    raise piece
                buf.append(piece)
                yield ("chunk", piece)
                _take_lead()
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

        # Резюме и план проверки — по готовому телу. Замер 26.09: написанные
        # одновременно с телом, они не видели его находок (событие за
        # жалобами, довод банка про перевод по СБП) и путали «одну группу из 7»
        # с «7 группами». Между собой — одновременно.
        prior = "\n\n".join(f"## {ttl[k]}\n\n{body[k]}" for k in order if k in body)
        for k in LEAD:
            lead_tasks[asyncio.create_task(_lead(k, prior))] = k
        writers.extend(lead_tasks)
        pending = set(lead_tasks)
        yield ("status", "Пишу резюме и план проверки по готовому отчёту")
        while pending:
            _done, pending = await asyncio.wait(pending, timeout=5.0,
                                                return_when=asyncio.FIRST_COMPLETED)
            _take_lead()
            for ev in _ready():
                yield ev
        _take_lead()

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
        for t in list(tasks) + writers:
            if not t.done():
                t.cancel()


_H12 = re.compile(r"^#{1,2}(?=\s)", re.M)
# Якорь с приписками («[f:158 — в теле]») перенумеровщик не узнаёт, и он уходит
# в текст как есть (26.09) — приписку отрезаем.
_ANCHOR_TAIL = re.compile(r"\[f:(\d+)[^\d\]\n][^\]\n]{0,59}\]")


_REF_POINT = re.compile(r"\s*[(·,—–-]\s*точка отсч[её]та\s*\)?|точк[аи] отсч[её]та\s*[·:—–-]\s*",
                        re.I)


def _demote_text(text: str) -> str:
    """Внутри раздела — только подзаголовки. «# Что проверить» внутри «Карты
    условий» становился заголовком всего PDF и ломал оглавление (27 пунктов).
    Служебное «(точка отсчёта)» у имени банка — вон: это термин промпта."""
    text = _ANCHOR_TAIL.sub(r"[f:\1]", _H12.sub("###", text or ""))
    return _REF_POINT.sub("", text)


async def _demote_headings(pieces: AsyncIterator[str]) -> AsyncIterator[str]:
    """_demote_text для потока: отдаём целыми строками, чтобы «##» в начале
    строки не разрезался между кусками."""
    buf = ""
    async for piece in pieces:
        buf += piece
        if "\n" not in buf:
            continue
        head, _, buf = buf.rpartition("\n")
        yield _demote_text(head + "\n")
    if buf:
        yield _demote_text(buf)


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


# Временные отказы провайдера: перегрузка, лимит запросов, шлюз. Постоянные
# ошибки (неверный запрос, нет модели) сюда не попадают — их повтор не лечит.
_RETRY_ERR = re.compile(
    r"overload|rate.?limit|too many requests|\b(429|502|503|504)\b|"
    r"timeout|timed out|temporar", re.IGNORECASE)


async def _stream_section(client, model: str, prompt: str,
                          attempts: int = 3) -> AsyncIterator[str]:
    """Один раздел одним вызовом. Роль и правила уже внутри промпта, поэтому
    системное сообщение короткое — оно лишь фиксирует режим.

    Перегрузка провайдера на старте раздела больше не роняет прогон: пока
    наружу не ушло ни куска, повторяем. После первого же выданного куска
    повтор запрещён — иначе читатель получил бы раздел дважды.
    """
    for attempt in range(attempts):
        sent = False
        try:
            async for piece in _stream(client, model, question="", plan=None,
                                       context="", raw_prompt=prompt):
                sent = True
                yield piece
            return
        except Exception as e:
            if sent or attempt == attempts - 1 or not _RETRY_ERR.search(str(e)):
                raise
            log.warning("раздел: %s — повтор %d из %d",
                        str(e)[:80], attempt + 2, attempts)
            await asyncio.sleep(2 * (attempt + 1))
