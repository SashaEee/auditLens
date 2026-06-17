"""Conductor — режиссёр-оркестратор: вопрос → ResearchPlan.

Это первый и самый важный reasoning-вызов во всём pipeline. Кондуктор:
  1. Понимает истинный интент вопроса (не по ключевым словам, а семантически)
  2. Извлекает субъектов сравнения (банки/услуги/объекты)
  3. Определяет природу вопроса (тариф/функция/качество/процесс/...)
  4. Решает каких агентов звать, с какими заданиями и в каком порядке
  5. Намечает структуру итогового отчёта

Без Кондуктора автономные агенты не знают ЧТО собирать. Кондуктор — это
«брифинг» для команды.

ВАЖНО: Кондуктор НЕ хардкодит продукты. Он может распознать «автоперевод»,
«качество обслуживания ипотечных клиентов», «эквайринг для ИП», «доверенность» —
что угодно. Интент определяет структуру плана, а не словарь продуктов.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from openai import AsyncOpenAI

from ...ai.llm_utils import (_loose_json_loads, normalize_question,
                             detect_bank_slugs, deep_reasoning_extra)
from .base_agent import AgentMission

log = logging.getLogger(__name__)


@dataclass
class ResearchPlan:
    """План исследования от Кондуктора."""
    intent: str               # короткая метка интента
    intent_summary: str        # что аудитор реально хочет узнать (развёрнуто)
    question_nature: str       # tariff_product | feature | quality | process |
                               # regulatory | company_facts | mixed
    subjects: list[str]        # slug'и банков/объектов
    subject_labels: dict[str, str]  # slug → человекочитаемое имя
    product: str               # нормализованный продукт/услуга/тема
    product_synonyms: list[str] = field(default_factory=list)
    # Задания для агентов
    missions: list[AgentMission] = field(default_factory=list)
    # Зависимости между миссиями (id → [depends_on ids])
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # Структура отчёта
    output_sections: list[str] = field(default_factory=list)
    # Метаданные
    needs_ranking: bool = False
    needs_complaints: bool = False
    needs_regulatory: bool = False

    def to_ui_plan(self) -> list[dict]:
        """Для SSE event 'plan' — список шагов для UI (как старый plan)."""
        steps = []
        for i, m in enumerate(self.missions, 1):
            steps.append({
                "n": i,
                "title": f"{_AGENT_LABELS.get(m.agent_id, m.agent_id)}: {m.goal[:60]}",
                "tool": m.agent_id,
                "entity": m.subjects[0] if m.subjects else None,
            })
        return steps


_AGENT_LABELS = {
    "researcher": "Исследование условий",
    "reviews": "Отзывы и жалобы",
    "regulatory": "Регуляторное поле",
    "ranking": "Рейтинг",
    "market": "Рыночный контекст",
}


SYSTEM_PROMPT = """Ты — conductor (режиссёр) аудиторского исследования. Получив
вопрос внутреннего аудитора банка, ты раскладываешь его на:
  1. ИСТИННЫЙ ИНТЕНТ — что аудитор реально хочет узнать (за словами).
  2. СУБЪЕКТОВ — банки/услуги/объекты, которые сравниваются.
  3. ПРИРОДУ ВОПРОСА — это тарифный продукт, функция, качество обслуживания,
     процесс, регуляторика, факты о компании, или смешанный тип?
  4. ПЛАН — какие автономные агенты нужны и с какими заданиями.

ПРИРОДА ВОПРОСА — критична. Примеры:
  • «Сравни условия автоперевода Сбера и Тинькоффа» → FEATURE (функция приложения,
    не тарифный продукт). Параметры: триггеры, комиссии, лимиты, гибкость.
  • «Сравни тарифы дебетовых карт 5 банков» → TARIFF_PRODUCT. Параметры:
    выпуск/обслуживание/кешбэк/лимиты/требования.
  • «На что жалуются клиенты по ипотеке Сбера» → QUALITY. Фокус: отзывы/жалобы.
  • «Какие требования ЦБ к раскрытию комиссий» → REGULATORY.
  • «Сравни качество обслуживания в Сбере vs ВТБ» → QUALITY/PROCESS.

АГЕНТЫ (выбирай строго по необходимости, не плоди лишних):
  • researcher — собирает конкретные факты/условия/параметры по теме.
    Ему скажи какие ПАРАМЕТРЫ искать (список 5-10), адаптированный к ПРИРОДЕ
    вопроса. НЕ используй универсальные слоты — подстрой под тему.
  • reviews — собирает отзывы/жалобы/похвалы с цитатами + sentiment.
    Зови ВСЕГДА когда вопрос упоминает «жалобы/отзывы/проблемы/недовольство»
    или когда auditor хочет понять клиентский опыт.
  • regulatory — ищет нормативную базу (законы, ЦБ, ФАС).
    Зови когда есть регуляторный контекст (переводы/реклама/вклады/страховки).
  • ranking — строит рейтинг субъектов. Зови когда аудитор прямо просит
    «рейтинг/лучший/худший/ранжируй» или сравнивает ≥3 субъекта с целью выбора.
  • market — рыночный контекст (доли, тренды, реформы). Опционально.

ПРАВИЛА:
  • Минимум 2 агента (researcher + хотя бы один из reviews/regulatory/ranking).
  • Максимум 5 (иначе перегруз).
  • ranking зависит от researcher (+reviews если есть). Укажи depends_on.
  • Если в вопросе «сравни и покажи жалобы» — researcher + reviews ОБЯЗАТЕЛЬНО.
  • В задании researcher-у укажи КОНКРЕТНЫЕ параметры для этой темы.
  • Если продуктов/услуг несколько («ипотека + автокредит») — это mixed,
    researcher может собрать по обоим, но раздели в задании.

ВЫХОД: строгий JSON без преамбулы и markdown-fences:
{
  "intent": "compare_feature_with_sentiment_and_ranking",
  "intent_summary": "Аудитор хочет понять, у какого банка автоперевод выгоднее/удобнее, и какие есть клиентские риски через жалобы. Решение для продуктовой команды.",
  "question_nature": "feature",
  "subjects": ["sberbank", "tinkoff", "alfabank", "vtb", "gazprombank"],
  "subject_labels": {"sberbank":"Сбербанк", "tinkoff":"Т-Банк", ...},
  "product": "автоперевод",
  "product_synonyms": ["автоплатёж", "регулярный перевод", "планируемый перевод"],
  "missions": [
    {"agent_id":"researcher",
     "goal":"Собери условия автоперевода: триггеры запуска, направление (C2B/C2C/me2me), комиссия внутри банка и на внешнюю карту, лимиты операции/сутки/мес, поддержка шаблонов, отмена/пауза, каналы управления. Различай автоплатёж (C2B) и автоперевод (C2C/me2me) — это разные тарифы!",
     "focus":"только механика функции автоперевода, не тарифы карт/вкладов",
     "constraints":["учи терминологическую разницу автоплатёж vs автоперевод"]},
    {"agent_id":"reviews",
     "goal":"Собери топ-5 жалоб клиентов по автопереводу/автоплатежу для каждого банка с цитатами. Фокус: сбои/несрабатывания, скрытые комиссии, сложность отмены, спам-СМС. Отметь свежие (2024-2026) vs устаревшие.",
     "focus":"только жалобы на автоперевод/автоплатёж"},
    {"agent_id":"ranking",
     "goal":"Построй рейтинг 5 банков по совокупности: цена + гибкость + надёжность (по жалобам).",
     "focus":"с учётом уравниловки цен регулятором — ранжируй по гибкости"}
  ],
  "dependencies": {"ranking": ["researcher", "reviews"]},
  "output_sections": ["summary", "key_insights", "conditions_table",
                       "ranking", "complaints", "risks", "methodology", "sources"],
  "needs_ranking": true, "needs_complaints": true, "needs_regulatory": false
}"""


async def plan_research(client: AsyncOpenAI, model: str,
                          question: str, history: list[dict] | None = None,
                          on_reasoning=None,
                          ) -> ResearchPlan:
    """Главный API Кондуктора: вопрос → ResearchPlan."""
    q = normalize_question(question)

    # Сначала быстрая локальная подсказка: какие банки явно в вопросе
    hinted_banks = detect_bank_slugs(q)

    user_msg = (
        f"# Вопрос аудитора\n{q}\n\n"
        f"# Подсказка: банки в вопросе (можешь дополнить/убрать)\n"
        f"{', '.join(hinted_banks) or '(явных банков нет — определи топ-5 релевантных)'}\n\n"
        f"Верни JSON-план исследования."
    )
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        # Берём последние 2 реплики истории для контекста
        messages.extend(history[-2:])
    messages.append({"role": "user", "content": user_msg})

    try:
        if on_reasoning is not None:
            from ._streaming import stream_completion
            raw, _r, _t = await stream_completion(
                client, on_reasoning=on_reasoning,
                model=model, messages=messages, temperature=0.0,
                max_tokens=8000, extra_body=deep_reasoning_extra())
            raw = (raw or "").strip()
        else:
            resp = await client.chat.completions.create(
                model=model, messages=messages,
                temperature=0.0, max_tokens=8000,   # 3000 рвало план на 5 банках → fallback
                extra_body=deep_reasoning_extra(),  # план — главный reasoning-шаг: effort=high
            )
            raw = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.warning("[conductor] LLM failed: %s — fallback plan", e)
        return _fallback_plan(q, hinted_banks)

    data = _parse_plan_json(raw)
    if not data:
        log.warning("[conductor] no JSON parse, raw 200=%r", raw[:200])
        return _fallback_plan(q, hinted_banks)

    plan = _build_plan_from_dict(data, q)
    log.warning("[conductor] intent=%s, nature=%s, %s subjects, %s missions",
                 plan.intent, plan.question_nature,
                 len(plan.subjects), len(plan.missions))
    return plan


def _parse_plan_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        data = _loose_json_loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Спасение ОБРЕЗАННОГО JSON (truncation): закрываем незакрытые строки/скобки и
    # отбрасываем хвостовую неполную запись. Лучше план с N миссий, чем fallback.
    salv = _salvage_truncated_json(raw)
    if salv:
        try:
            return json.loads(salv)
        except Exception:
            try:
                d = _loose_json_loads(salv)
                return d if isinstance(d, dict) else None
            except Exception:
                return None
    return None


def _salvage_truncated_json(raw: str) -> str | None:
    """Из обрезанного JSON-объекта собирает синтаксически валидный, балансируя
    скобки. Отрезает по последней закрытой записи (запятая/закрывающая скобка на
    верхнем уровне), затем добавляет недостающие ] и }."""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE).rstrip("`").strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    cut = -1            # позиция последнего «безопасного» среза (запятая/}/] на глубине ≥1)
    for i in range(start, len(t)):
        ch = t[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            cut = i + 1
        elif ch == "," and depth >= 1:
            cut = i        # срез по запятой — отбросит неполную следующую запись
    if cut < 0:
        return None
    body = t[start:cut].rstrip().rstrip(",")
    # _rebalance пересчитает незакрытые скобки именно для позиции среза.
    return _rebalance(body)


def _rebalance(body: str) -> str | None:
    """Балансирует скобки в усечённом фрагменте, добавляя закрывающие в конце."""
    depth_stack: list[str] = []
    in_str = False
    esc = False
    for ch in body:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_stack.append("}")
        elif ch == "[":
            depth_stack.append("]")
        elif ch in "}]" and depth_stack:
            depth_stack.pop()
    tail = '"' if in_str else ""
    return body + tail + "".join(reversed(depth_stack))


def _build_plan_from_dict(data: dict, question: str) -> ResearchPlan:
    subjects = [str(s) for s in (data.get("subjects") or []) if s]
    subject_labels = {str(k): str(v) for k, v in
                       (data.get("subject_labels") or {}).items()}
    # Если меток нет — используем slug как есть
    if not subject_labels:
        subject_labels = {s: s for s in subjects}

    missions: list[AgentMission] = []
    for m in (data.get("missions") or []):
        if not isinstance(m, dict):
            continue
        agent_id = str(m.get("agent_id") or "").strip()
        if not agent_id:
            continue
        missions.append(AgentMission(
            agent_id=agent_id,
            goal=str(m.get("goal") or "").strip(),
            subjects=list(subjects),
            focus=str(m.get("focus") or "").strip(),
            constraints=[str(c) for c in (m.get("constraints") or []) if c],
            context="",
        ))

    if not missions:
        # аварийно: хотя бы researcher
        missions.append(AgentMission(
            agent_id="researcher",
            goal=f"Собери факты по вопросу: {question}",
            subjects=list(subjects),
        ))

    deps_raw = data.get("dependencies") or {}
    dependencies = {str(k): [str(x) for x in v]
                      for k, v in deps_raw.items() if isinstance(v, list)}

    return ResearchPlan(
        intent=str(data.get("intent") or "general").strip(),
        intent_summary=str(data.get("intent_summary") or "").strip(),
        question_nature=str(data.get("question_nature") or "mixed").strip(),
        subjects=subjects,
        subject_labels=subject_labels,
        product=str(data.get("product") or "").strip(),
        product_synonyms=[str(s) for s in (data.get("product_synonyms") or []) if s],
        missions=missions,
        dependencies=dependencies,
        output_sections=[str(s) for s in (data.get("output_sections") or []) if s],
        needs_ranking=bool(data.get("needs_ranking")),
        needs_complaints=bool(data.get("needs_complaints")),
        needs_regulatory=bool(data.get("needs_regulatory")),
    )


def fan_out_researcher(plan: ResearchPlan) -> ResearchPlan:
    """Раскладывает ЕДИНУЮ researcher-миссию (все банки сразу) на N миссий —
    по одному субъекту на агента.

    Корень «мелких» отчётов: один researcher держал ВСЕ банки × ВСЕ измерения и,
    упираясь в потолок чтений, читал ~1 страницу на банк. Разнесённые по банкам
    агенты работают параллельно (волна 1) и тратят весь свой бюджет чтений на
    ОДИН банк → ~5 страниц/банк вместо ~1. agent_id остаётся "researcher", поэтому
    зависимости (ranking←researcher) и реестр агентов не ломаются: ranking ждёт
    завершения волны 1 как и раньше. Тюнится V2_FANOUT_RESEARCHER (1=вкл, 0=выкл),
    порог V2_FANOUT_MIN_SUBJECTS (по умолчанию ≥2 субъекта)."""
    if os.getenv("V2_FANOUT_RESEARCHER", "1") == "0":
        return plan
    min_subj = int(os.getenv("V2_FANOUT_MIN_SUBJECTS", "2"))
    new_missions: list[AgentMission] = []
    fanned = 0
    for m in plan.missions:
        subs = [s for s in (m.subjects or []) if s]
        if m.agent_id != "researcher" or len(subs) < min_subj:
            new_missions.append(m)
            continue
        for s in subs:
            label = plan.subject_labels.get(s, s)
            goal = (f"[ТОЛЬКО {label}] {m.goal}\n\n"
                    f"ВАЖНО: исследуй ИСКЛЮЧИТЕЛЬНО «{label}» — глубоко, по всем "
                    f"параметрам из задания. Не отвлекайся на другие банки (их "
                    f"берут параллельные агенты). Цель — максимум конкретики именно "
                    f"по этому субъекту.")
            new_missions.append(AgentMission(
                agent_id="researcher",
                goal=goal,
                subjects=[s],
                focus=m.focus,
                constraints=list(m.constraints),
                context=m.context,
            ))
            fanned += 1
    if fanned > 1:
        plan.missions = new_missions
        log.warning("[conductor] researcher fan-out: %s миссий по 1 субъекту",
                     fanned)
    return plan


def _fallback_plan(question: str, hinted_banks: list[str]) -> ResearchPlan:
    """Минимальный план если Кондуктор упал. Всегда researcher + reviews."""
    subjects = hinted_banks or ["sberbank", "tinkoff", "alfabank", "vtb"]
    labels = {s: s.title() for s in subjects}
    return ResearchPlan(
        intent="general_comparison",
        intent_summary="Сравнительный анализ по вопросу аудитора",
        question_nature="mixed",
        subjects=subjects,
        subject_labels=labels,
        product="",
        product_synonyms=[],
        missions=[
            AgentMission(
                agent_id="researcher",
                goal=f"Собери конкретные факты и условия по вопросу: {question}. "
                     f"Используй semantic_search + web_search + read_url. "
                     f"Верни факты со ссылками [N].",
                subjects=list(subjects),
            ),
            AgentMission(
                agent_id="reviews",
                goal=f"Собери отзывы и жалобы клиентов по теме вопроса: {question}. "
                     f"С фокусом на проблемы и на что жалуются.",
                subjects=list(subjects),
            ),
        ],
        dependencies={},
        output_sections=["summary", "key_findings", "conditions_table",
                          "complaints", "risks", "methodology", "sources"],
        needs_ranking=False,
        needs_complaints=True,
        needs_regulatory=False,
    )
