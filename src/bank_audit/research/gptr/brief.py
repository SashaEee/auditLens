"""Бриф отчёта: главный ответ, тезисы и состав разделов — до того, как
разделы начнут писаться.

ЗАЧЕМ. Разделы пишутся одновременно, и каждый видит только свой срез фактов.
Аудит отчёта 26.09 («почему выросли жалобы на чарджбэк»):
  • резюме — «всплеск подтверждён, 14 жалоб», а три раздела тела — «данных
    аналитики жалоб нет, всплеск ни подтвердить, ни опровергнуть»;
  • «Карта условий» и «Сбербанк против рынка» сами придумали причину
    («когорта обращений на сроке 25–30 дней», «сбой СМС»);
  • каналы подачи пересказаны четыре раза, итоговых блоков — четыре;
  • на вопрос о всплеске отчёт выдал «Сбербанк против рынка» без единого
    конкурента и «Расхождения» об адресах электронной почты.

Лечение — одна точка решения до письма: модель видит ВСЕ факты сразу и
решает, что отчёт отвечает на вопрос, какими тезисами и какими разделами в
каком порядке. Каждый раздел получает этот бриф: знает главный ответ, свою
задачу и чужие — не спорит с ними и не повторяет их. Числа и цитаты — только
якорями на факты; бриф ничего не считает.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from . import runstate

log = logging.getLogger(__name__)

# Что каждый раздел умеет — для модели, выбирающей состав отчёта.
SECTION_ROLES = {
    "voice": "жалобы и отзывы клиентов: масштаб, динамика, сюжеты с цитатами, "
             "конкуренты",
    "loopholes": "лазейки и схемы обхода условий из раздела «Аудит уязвимостей»",
    "conditions": "условия, правила и процесс ТОЛЬКО своего банка (и дочерних компаний); "
                  "конкурентов в нём нет — не называй его «по каждому банку»",
    "market": "сравнение банков вопроса между собой по условиям и процессу "
              "(по источникам банков) и место на рынке",
    "regulatory": "требования закона и ЦБ и соответствие им; данные и документы самого "
                  "регулятора (таблицы значений, статистика Банка России)",
    "reference": "сведения из сторонних источников: статистика, обзоры рынка, аналитика, "
                 "публикации, новости — цифры рынка, история вопроса, что пишут о предмете",
    "conflicts": "расхождения источников в значениях одной характеристики",
}
BODY_KEYS = tuple(SECTION_ROLES)
# Свой раздел по характеристикам: состав называет бриф. Аудит 11.11: шесть
# готовых видов разделов делят факты по тому, КТО сказал (банк, клиент,
# регулятор), а рассказ о продукте, истории или рынке делится по тому, О ЧЁМ.
TOPIC = "topic"
MAX_TOPICS = 4

# Сколько строк указателя фактов показываем модели. Факты AuditLens идут
# целиком: это главные числа и жалобы, их немного.
_INDEX_LINES = int(os.getenv("GPTR_BRIEF_INDEX_LINES", "420"))


@dataclass
class Brief:
    answer: str = ""
    title: str = ""        # название отчёта: история, обложка PDF, имя файла
    theses: list[str] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)   # {key, title, focus}
    skipped: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return bool(self.answer and self.sections)

    def render(self, key: str | None = None, sections: bool = True) -> str:
        """Бриф для промпта раздела: главный ответ, тезисы, чей какой раздел.

        sections=False — без перечня разделов: короткому плану проверки задания
        чужих разделов не нужны (прогон 11.11: видя «сводная таблица условий»,
        он переписал сравнение целиком перед своими пятью пунктами).
        """
        lines = ["ГЛАВНЫЙ ОТВЕТ ОТЧЁТА (решён по всем фактам; не спорь с ним и не "
                 "пересказывай его — раскрой свою часть):", self.answer, ""]
        if self.theses:
            lines.append("ТЕЗИСЫ ОТЧЁТА:")
            lines += [f"{i}. {t}" for i, t in enumerate(self.theses, 1)]
            lines.append("")
        if not sections:
            return "\n".join(lines).rstrip()
        lines.append("РАЗДЕЛЫ ОТЧЁТА (каждый пишет своё и не повторяет чужое):")
        for s in self.sections:
            mark = "  ← ТВОЙ РАЗДЕЛ" if s["key"] == key else ""
            lines.append(f"- «{s['title']}»: {s['focus']}{mark}")
        return "\n".join(lines)


_ORDER_BY_TASK = {
    "compare": ["market", "voice", "conditions", "loopholes", "regulatory"],
    "product": ["conditions", "voice", "regulatory", "loopholes", "reference"],
    "complaints": ["voice", "conditions", "loopholes", "regulatory"],
    "regulation": ["regulatory", "conditions", "reference"],
    "market_data": ["reference", "regulatory", "market", "conditions"],
    "explain": ["reference", "regulatory", "conditions"],
    "news": ["reference", "voice", "regulatory", "conditions"],
    "check": ["conditions", "voice", "loopholes", "regulatory"],
}


def default_order(plan, scope: dict | None) -> list[str]:
    """Порядок разделов по типу вопроса — запасной путь и подсказка модели.
    Раздел про то, о чём спросили, идёт первым."""
    scope = scope or {}
    task = getattr(plan, "task", None)
    if task in _ORDER_BY_TASK:
        # Запасной состав под задачу: только то, что ей нужно. Сравнения с
        # рынком нет ни в справке, ни в рассказе о продукте.
        return list(_ORDER_BY_TASK[task])
    nature = (getattr(plan, "question_nature", "") or "").lower()
    first: list[str] = []
    if nature == "regulatory":
        first = ["regulatory", "conditions"]
    elif scope.get("loopholes") and not scope.get("complaints") and not scope.get("market"):
        first = ["loopholes", "voice"]
    elif scope.get("complaints") and not scope.get("market"):
        first = ["voice", "loopholes", "conditions"]
    elif scope.get("market") or nature in ("tariff_product", "feature"):
        first = ["market", "conditions"]
    elif nature == "process":
        first = ["conditions", "market"]
    rest = ["conditions", "market", "voice", "loopholes", "regulatory", "reference", "conflicts"]
    return list(dict.fromkeys(first + rest))


_SYSTEM = """Ты — руководитель аналитической группы службы внутреннего аудита Сбербанка. Перед
тобой ВСЕ проверенные факты по вопросу аудитора. Реши, что отчёт отвечает и как он построен.

Верни строго JSON:
{"report_title": "...", "answer": "...", "theses": ["...", ...], "sections": [{"key": "...", "title": "...", "focus": "...", "attributes": ["..."]}, ...]}

report_title — название всего отчёта для истории и обложки: 3–9 слов о предмете (продукт,
банки, тема, период), например «Эквайринг для малого бизнеса: Сбер против Т-Банка и ВТБ»,
«Жалобы на чарджбэк: причины всплеска», «Сервис «Своё дело»: условия и ограничения». Без
глаголов-команд из вопроса («Сделай», «Сравни»), без слов «отчёт», «анализ», «исследование»,
без кавычек вокруг всего названия и точки в конце.

answer — прямой ответ на вопрос в 2–4 фразах: главное число или вывод, причина, что это
значит для аудитора. Отвечай в форме вопроса: на «какие проблемы», «на что жалуются» — сами
проблемы по убыванию веса; на «сколько» — число; «да» или «нет» — только на закрытый вопрос
(«стоит ли…», «конкурентны ли…»). Каждое число и утверждение — с якорем [f:N] из фактов. Если на часть
вопроса данных нет во ВСЕХ фактах — скажи это здесь одной фразой, но начинай с того, что
установлено, а не с того, чего нет.
theses — 3–6 тезисов, из которых складывается ответ; каждый — одна фраза с якорями.
Тезисы не повторяют друг друга.

sections — разделы тела в порядке чтения. Первым — раздел, который прямо отвечает на вопрос.
Раздел, который не помогает ответить на ЭТОТ вопрос, не включай. Обычно 2–4 раздела.
Два вида разделов:
  • готовый — ключ из списка «Доступные разделы» (поле attributes не нужно);
  • свой раздел по теме — "key": "topic" и "attributes": характеристики из списка
    «Характеристики» дословно. В него попадут все факты с этими характеристиками, кто бы их
    ни сообщил. Свои разделы нужны, когда вопрос — рассказ о продукте, процессе, рынке или
    теме: дели материал по смыслу (что это и как устроено; условия и тарифы; как подключить;
    ограничения; что менялось), а не по тому, кто сказал. Одна характеристика — в один
    раздел. Не больше четырёх своих разделов.
title — заголовок по сути вопроса, по-русски, до 60 знаков (например «Что стоит за
всплеском», «Сбер против ВТБ и Газпромбанка», «Как подключается сервис»), без слов «раздел»
и «точка отсчёта»; не «Что проверить» и не «Резюме» — это отдельные разделы.
focus — 1–2 фразы: что именно раскрыть в разделе и чего в нём НЕ писать, потому что
это делает другой раздел.

СОСТАВ ПО ЗАДАЧЕ (строка «Задача» в запросе):
  • сравнение — раздел market обязателен и первый; voice — вторым, если есть жалобы клиентов.
  • всё о продукте или сервисе — свои разделы по темам (или conditions); market не включай:
    сравнения аудитор не просил. voice — если есть жалобы по предмету вопроса; regulatory —
    если есть нормы.
  • жалобы — voice первым.
  • нормы — regulatory первым.
  • рынок и цифры — первым раздел с цифрами: reference или regulatory (данные регулятора);
    в фокусе — таблица и ряд по периодам.
  • справка — свои разделы по темам и reference.
  • разбор новости, план проверки — по существу вопроса.
Организации, о которых аудитор не спрашивал, в заголовки и фокус не вноси.

ПРОВЕРЬ, О ЧЁМ ЧИСЛО. У факта после стороны стоит начало цитаты. Если цитата говорит о другом
предмете, чем вопрос (другой продукт, другой рынок, другой показатель), этот факт к вопросу не
относится: не клади его в ответ и тезисы, каким бы подходящим ни было название характеристики.
Если после этого по сути вопроса фактов нет — так и скажи в answer первой фразой и назови,
что удалось установить рядом с темой.

Правила: опирайся только на факты. «Аналитика жалоб AuditLens» — главный источник о
клиентах. Условия банков бери из их собственных источников (тарифы, страницы продуктов,
документы) — в них подробности; «витрина «Рынок» AuditLens» — только сверочные цифры и место
на рынке, без подробностей. Раздел voice включай, только если жалобы относятся к предмету
вопроса. Раздел loopholes — если записи раздела «Аудит уязвимостей» относятся к продукту или
процессу вопроса (после voice); если записи о другом продукте — не включай его.
Не выдумывай причин, которых нет в фактах: если причина видна в жалобах (событие, продавец,
сервис, условие) — назови её."""


def attributes_digest(facts: list) -> tuple[str, dict[str, int]]:
    """Характеристики с числом фактов по сторонам — чтобы бриф мог собрать свои
    разделы по темам. Возвращает текст и словарь «характеристика → число»."""
    count: dict[str, dict[str, int]] = {}
    for f in facts:
        c = count.setdefault(f.attribute, {})
        c[f.stance] = c.get(f.stance, 0) + 1
    names = {"declared": "заявлено", "observed": "со стороны", "regulatory": "норм",
             "loophole": "лазеек"}
    lines, total = [], {}
    for attr, c in sorted(count.items(), key=lambda kv: -sum(kv[1].values())):
        total[attr] = sum(c.values())
        lines.append(f"- {attr}: {total[attr]} ("
                     + ", ".join(f"{names.get(k, k)} {v}" for k, v in c.items()) + ")")
    return "\n".join(lines[:60]), total


def _index_line(f, labels: dict[str, str], own: dict) -> str:
    kind = own.get(f.url, {}).get("kind")
    side = {"declared": "заявлено", "regulatory": "норма", "loophole": "лазейка"}.get(
        f.stance, "наблюдается")
    if kind == "complaints":
        side += ", аналитика жалоб AuditLens"
    elif kind == "review":
        side += ", жалоба клиента"
    elif kind == "market":
        side += ", витрина «Рынок» AuditLens"
    subj = labels.get(f.subject, f.subject) or "общее"
    # Для данных AuditLens смысл — в строке страницы («Группа похожих жалоб …:
    # 7 жалоб …»), а значение — голое число: по одному «7» бриф решил, что
    # групп семь (26.09).
    val = str((f.verbatim if kind in ("complaints", "market") else f.value) or "")[:320]
    unit = f" {f.unit}" if f.unit else ""
    date = f" | {f.date}" if f.date else ""
    # Начало цитаты — чтобы было видно, О ЧЁМ значение. По одному «2,4 трлн руб.»
    # под характеристикой «объём средств на металлических счетах» замысел и
    # резюме назвали главным числом рынка резервы по страхованию жизни (11.11).
    quote = ""
    if kind not in ("complaints", "market") and f.verbatim and f.verbatim.strip() != val.strip():
        quote = " | «" + re.sub(r"\s+", " ", f.verbatim).strip()[:110] + "»"
    return f"[f:{f.id}] {subj} | {f.attribute} | {val}{unit} | {side}{date}{quote}"


def facts_digest(facts: list, labels: dict[str, str], own: dict,
                 limit: int = _INDEX_LINES) -> str:
    """Указатель фактов для брифа: собственные данные AuditLens — целиком и
    первыми, остальное — по одному-два на клетку «объект × характеристика»,
    пока не кончится лимит."""
    mine = [f for f in facts if f.url in own]
    web = [f for f in facts if f.url not in own]
    seen: dict = {}
    picked = []
    for f in web:
        k = (f.subject, f.attribute)
        if seen.get(k, 0) >= 2:
            continue
        seen[k] = seen.get(k, 0) + 1
        picked.append(f)
    lines = [_index_line(f, labels, own) for f in mine]
    lines += [_index_line(f, labels, own) for f in picked[:max(0, limit - len(lines))]]
    dropped = len(picked) - max(0, limit - len(mine))
    if dropped > 0:
        lines.append(f"… и ещё {dropped} фактов из источников того же рода")
    return "\n".join(lines)


def _norm_attr(a: str) -> str:
    return re.sub(r"\s+", " ", str(a or "")).strip().lower().replace("ё", "е")


def parse(raw: str, available: dict[str, int],
          attributes: dict[str, int] | None = None) -> Brief:
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    known = {_norm_attr(a): a for a in (attributes or {})}
    secs, seen, used_attrs, topics = [], set(), set(), 0
    for s in data.get("sections") or []:
        if not isinstance(s, dict):
            continue
        key = str(s.get("key") or "").strip()
        attrs: list[str] = []
        if key == TOPIC:
            # Свой раздел: характеристики — только существующие и ещё не занятые.
            for a in s.get("attributes") or []:
                real = known.get(_norm_attr(a))
                if real and real not in used_attrs:
                    attrs.append(real)
            if not attrs or topics >= MAX_TOPICS:
                continue
            used_attrs.update(attrs)
            topics += 1
            key = f"{TOPIC}{topics}"
        elif key not in available or key in seen:
            continue
        seen.add(key)
        title = re.sub(r"\s+", " ", str(s.get("title") or "")).strip().strip("«»\"")[:80]
        # «Что проверить в процессе Сбера» рядом с «Что проверять» — два плана
        # проверки в оглавлении; такой заголовок заменяем штатным.
        if re.match(r"(что|как)\s+провер|резюме", title, re.I):
            title = ""
        secs.append({"key": key, "title": title, "attributes": attrs,
                     "focus": re.sub(r"\s+", " ", str(s.get("focus") or "")).strip()[:400]})
    from ...ai.report_title import clean_title
    return Brief(answer=str(data.get("answer") or "").strip(),
                 title=clean_title(data.get("report_title")),
                 theses=[str(t).strip() for t in data.get("theses") or [] if str(t).strip()][:6],
                 sections=secs,
                 skipped=[k for k in available if k not in seen])


_TASK_RU = {"compare": "сравнение", "product": "всё о продукте или сервисе", "complaints": "жалобы",
            "regulation": "нормы", "market_data": "рынок и цифры", "explain": "справка",
            "news": "разбор новости", "check": "план проверки"}
_SCOPE_RU = {"sber": "только Сбер и его дочерние компании, конкурентов аудитор не просил",
             "named": "организации, названные аудитором", "market": "Сбер и рынок",
             "none": "вопрос не про конкретные организации"}


async def make_brief(client, model: str, *, question: str, plan, registry,
                     available: dict[str, int], default: list[str],
                     labels: dict[str, str], state=None) -> Brief | None:
    """Бриф одним вызовом модели. Не вышло — None: отчёт пишется по порядку
    по умолчанию, как раньше, без главного ответа."""
    from .facts import call_model
    from ...ai.llm_utils import deep_reasoning_extra
    state = state or runstate.current()
    own = state.own_meta
    order = list(dict.fromkeys(list(default) + [k for k in available if k not in default]))
    roles = "\n".join(f"- {k}: {SECTION_ROLES[k]} (фактов: {available[k]})"
                      for k in order if k in available)
    attrs_text, attrs = attributes_digest(list(registry.facts))
    task = getattr(plan, "task", None)
    from ...clock import today_msk
    user = "\n".join([
        f"# Сегодня\n{today_msk().strftime('%d.%m.%Y')} — правила с датой вступления позже "
        f"сегодняшней будущие, не действующие",
        f"# Вопрос аудитора\n{question}",
        f"\n# Что хочет узнать\n{getattr(plan, 'intent_summary', '') or '—'}",
        (f"\n# Задача\n{_TASK_RU.get(task, task)}; рамка — "
         f"{_SCOPE_RU.get(getattr(plan, 'scope', ''), 'по вопросу')}" if task else ""),
        f"\n# Доступные разделы (ключ: что в нём; порядок по умолчанию)\n{roles}",
        f"\n# Характеристики (для своих разделов «topic»; число фактов)\n{attrs_text}",
        "\n# Факты",
        facts_digest(list(registry.facts), labels, own),
    ])
    kw = {"model": model, "temperature": 0.0, "max_tokens": 6000,
          "response_format": {"type": "json_object"},
          "messages": [{"role": "system", "content": _SYSTEM},
                       {"role": "user", "content": user}],
          "extra_body": deep_reasoning_extra({"reasoning_effort": os.getenv(
              "GPTR_BRIEF_REASONING_EFFORT", "low")})}
    try:
        resp = await call_model(client, model, kw)
        brief = parse(resp.choices[0].message.content or "", available, attrs)
    except Exception as e:  # noqa: BLE001 — без брифа отчёт всё равно пишется
        log.warning("бриф: %s — пишу по порядку по умолчанию", type(e).__name__)
        return None
    if not brief.ok():
        log.warning("бриф: пустой ответ модели — пишу по порядку по умолчанию")
        return None
    log.info("бриф: разделы %s, пропущены %s", [s["key"] for s in brief.sections],
             brief.skipped)
    return brief
