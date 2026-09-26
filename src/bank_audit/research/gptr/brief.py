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
    "loopholes": "лазейки и схемы обхода условий из раздела «Уязвимости»",
    "conditions": "условия, правила и процесс самого банка (и дочерних компаний)",
    "market": "сравнение с конкурентами и место на рынке",
    "regulatory": "требования закона и ЦБ и соответствие им",
    "conflicts": "расхождения источников в значениях одной характеристики",
}
BODY_KEYS = tuple(SECTION_ROLES)

# Сколько строк указателя фактов показываем модели. Факты AuditLens идут
# целиком: это главные числа и жалобы, их немного.
_INDEX_LINES = int(os.getenv("GPTR_BRIEF_INDEX_LINES", "420"))


@dataclass
class Brief:
    answer: str = ""
    theses: list[str] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)   # {key, title, focus}
    skipped: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return bool(self.answer and self.sections)

    def render(self, key: str | None = None) -> str:
        """Бриф для промпта раздела: главный ответ, тезисы, чей какой раздел."""
        lines = ["ГЛАВНЫЙ ОТВЕТ ОТЧЁТА (решён по всем фактам; не спорь с ним и не "
                 "пересказывай его — раскрой свою часть):", self.answer, ""]
        if self.theses:
            lines.append("ТЕЗИСЫ ОТЧЁТА:")
            lines += [f"{i}. {t}" for i, t in enumerate(self.theses, 1)]
            lines.append("")
        lines.append("РАЗДЕЛЫ ОТЧЁТА (каждый пишет своё и не повторяет чужое):")
        for s in self.sections:
            mark = "  ← ТВОЙ РАЗДЕЛ" if s["key"] == key else ""
            lines.append(f"- «{s['title']}»: {s['focus']}{mark}")
        return "\n".join(lines)


def default_order(plan, scope: dict | None) -> list[str]:
    """Порядок разделов по типу вопроса — запасной путь и подсказка модели.
    Раздел про то, о чём спросили, идёт первым."""
    scope = scope or {}
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
    rest = ["conditions", "market", "voice", "loopholes", "regulatory", "conflicts"]
    return list(dict.fromkeys(first + rest))


_SYSTEM = """Ты — руководитель аналитической группы службы внутреннего аудита. Перед тобой
ВСЕ проверенные факты по вопросу аудитора. Реши, что отчёт отвечает, и как он построен.

Верни строго JSON:
{"answer": "...", "theses": ["...", ...], "sections": [{"key": "...", "title": "...", "focus": "..."}, ...]}

answer — прямой ответ на вопрос в 2–4 фразах: главное число или вывод, причина, что это
значит для проверки. Каждое число и утверждение — с якорем [f:N] из фактов. Если на часть
вопроса данных нет во ВСЕХ фактах — скажи это здесь одной фразой.
theses — 3–6 тезисов, из которых складывается ответ; каждый — одна фраза с якорями.
Тезисы не повторяют друг друга.
sections — разделы тела в порядке чтения, только из доступных ключей. Первым — раздел,
который прямо отвечает на вопрос. Раздел, который не помогает ответить на ЭТОТ вопрос,
не включай (например, сравнение с рынком без данных о конкурентах, расхождения
источников о второстепенном). Обычно 2–4 раздела.
title — заголовок по сути вопроса, по-русски, до 60 знаков (например «Что стоит за
всплеском», «Сбер против ВТБ и Газпромбанка»), без слов «раздел» и «точка отсчёта».
focus — 1–2 фразы: что именно раскрыть в разделе и чего в нём НЕ писать, потому что
это делает другой раздел.

Правила: опирайся только на факты; факт «аналитика жалоб AuditLens» и «витрина «Рынок»
AuditLens» — это данные системы, им доверяй больше обзоров из веба. Не выдумывай
причин, которых нет в фактах: если причина видна в жалобах (событие, продавец, сервис,
условие) — назови её."""


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
    val = str(f.value or "")[:220]
    unit = f" {f.unit}" if f.unit else ""
    date = f" | {f.date}" if f.date else ""
    return f"[f:{f.id}] {subj} | {f.attribute} | {val}{unit} | {side}{date}"


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


def parse(raw: str, available: dict[str, int]) -> Brief:
    m = re.search(r"\{.*\}", raw or "", re.S)
    data = json.loads(m.group(0)) if m else {}
    secs, seen = [], set()
    for s in data.get("sections") or []:
        key = str(s.get("key") or "").strip()
        if key not in available or key in seen:
            continue
        seen.add(key)
        title = re.sub(r"\s+", " ", str(s.get("title") or "")).strip().strip("«»\"")[:80]
        secs.append({"key": key, "title": title,
                     "focus": re.sub(r"\s+", " ", str(s.get("focus") or "")).strip()[:400]})
    return Brief(answer=str(data.get("answer") or "").strip(),
                 theses=[str(t).strip() for t in data.get("theses") or [] if str(t).strip()][:6],
                 sections=secs,
                 skipped=[k for k in available if k not in seen])


async def make_brief(client, model: str, *, question: str, plan, registry,
                     available: dict[str, int], default: list[str],
                     labels: dict[str, str], state=None) -> Brief | None:
    """Бриф одним вызовом модели. Не вышло — None: отчёт пишется по порядку
    по умолчанию, как раньше, без главного ответа."""
    from .facts import call_model
    from ...ai.llm_utils import deep_reasoning_extra
    state = state or runstate.current()
    own = state.own_meta
    roles = "\n".join(f"- {k}: {SECTION_ROLES[k]} (фактов: {available[k]})"
                      for k in default if k in available)
    user = "\n".join([
        f"# Вопрос аудитора\n{question}",
        f"\n# Что хочет узнать\n{getattr(plan, 'intent_summary', '') or '—'}",
        f"\n# Доступные разделы (ключ: что в нём; порядок по умолчанию)\n{roles}",
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
        brief = parse(resp.choices[0].message.content or "", available)
    except Exception as e:  # noqa: BLE001 — без брифа отчёт всё равно пишется
        log.warning("бриф: %s — пишу по порядку по умолчанию", type(e).__name__)
        return None
    if not brief.ok():
        log.warning("бриф: пустой ответ модели — пишу по порядку по умолчанию")
        return None
    log.info("бриф: разделы %s, пропущены %s", [s["key"] for s in brief.sections],
             brief.skipped)
    return brief
