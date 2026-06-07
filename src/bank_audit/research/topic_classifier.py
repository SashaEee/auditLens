"""Topic Classifier — LLM-классификатор вопроса аудитора.

Заменяет хардкод-эвристики (kw списки тем) на динамический LLM-анализ.

Определяет TopicProfile:
  • topic_kind — retail/regulatory/social/business/mortgage/deposit/...
  • needs_regulatory      — подгружать ли НПА (ГК РФ, ФЗ, ЦБ)
  • needs_government_programs — для соц. продуктов (маткапитал, военная ипотека)
  • regulatory_domains    — какие официальные домены релевантны
  • regulatory_query_hints — конкретные термины для regulatory search
                              («ст. 185 ГК РФ», «ФЗ-117», «Информационное письмо ЦБ»)
  • applicable_section_kinds — какие секции отчёта стоит вызвать

Этот модуль ВАЖНЕЕ ВСЕХ остальных в Phase 2 — потому что он управляет
ВСЕМИ остальными strategies (какие источники искать, какие секции рендерить).

Если LLM упал — fallback на безопасный «retail» профиль (без regulatory).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field

from openai import AsyncOpenAI

log = logging.getLogger(__name__)


# Каталог известных regulatory доменов с trust scores
REGULATORY_DOMAIN_CATALOG = {
    # Главные регуляторы и законодательство
    "cbr.ru":             1.0,    # Центральный Банк РФ
    "pravo.gov.ru":       1.0,    # Официальный портал правовой информации
    "consultant.ru":      0.95,   # КонсультантПлюс
    "garant.ru":          0.95,   # Гарант
    "duma.gov.ru":        1.0,    # Госдума
    "kremlin.ru":         1.0,    # Президент
    # Минфин / ФНС / ФАС
    "minfin.gov.ru":      0.95,
    "nalog.gov.ru":       0.95,
    "fas.gov.ru":         0.95,
    # Социальный фонд + соц. программы
    "sfr.gov.ru":         1.0,    # Социальный фонд России (бывш. ПФР+ФСС)
    "gosuslugi.ru":       0.95,
    # Военные программы
    "mil.ru":             1.0,    # Минобороны
    "rosvoenipoteka.ru":  1.0,    # Росвоенипотека
    # Нотариат (доверенности, наследование)
    "notariat.ru":        0.95,   # ФНП — Федеральная нотариальная палата
    "mgnp.info":          0.95,   # Московская городская нотариальная палата
    # Ипотека (соц. программы)
    "domrf.ru":           0.95,   # ДОМ.РФ
    # АСВ — для вкладов
    "asv.org.ru":         1.0,    # Агентство по страхованию вкладов
    # Госпрограммы / субсидирование (льготное авто-/кредитование)
    "minpromtorg.gov.ru": 1.0,    # Минпромторг — льготное автокредитование
    "government.ru":      1.0,    # Правительство РФ (постановления о субсидиях)
}


# Ключевые слова госпрограмм/субсидий — детерминированный триггер regulatory.
# LLM иногда не распознаёт «субсидирование» как regulatory-тему; этот override
# гарантирует подгрузку НПА/господдержки.
_SUBSIDY_KW = (
    "субсид", "льготн", "господдержк", "госпрограмм", "государственн програм",
    "минпромторг", "маткапитал", "материнск", "военн ипотек", "семейн ипотек",
)


def _has_subsidy_topic(q: str) -> bool:
    ql = (q or "").lower()
    return any(k in ql for k in _SUBSIDY_KW)


def _apply_subsidy_override(profile: "TopicProfile", question: str) -> "TopicProfile":
    """Детерминированно включает regulatory+госпрограммы для тем субсидий/льгот.
    Применяется на ВСЕХ путях (и при успехе LLM, и в fallback) — иначе при пустом
    ответе LLM субсидирование терялось."""
    if not _has_subsidy_topic(question):
        return profile
    profile.needs_regulatory = True
    profile.needs_government_programs = True
    for d in ("minpromtorg.gov.ru", "government.ru", "consultant.ru", "pravo.gov.ru"):
        if d not in profile.regulatory_domains:
            profile.regulatory_domains.append(d)
    extra_hints = ["льготная программа господдержки условия 2026",
                    "постановление правительства субсидирование ставки кредита"]
    profile.regulatory_query_hints = (
        profile.regulatory_query_hints
        + [h for h in extra_hints if h not in profile.regulatory_query_hints]
    )[:6]
    for s in ("government_programs", "regulatory_box"):
        if s not in profile.applicable_section_kinds:
            profile.applicable_section_kinds.append(s)
    log.warning("[topic_classifier] subsidy-override → reg=True, gov=True")
    return profile


@dataclass
class TopicProfile:
    """Результат классификации темы."""
    topic_kind: str = "retail"
    # Topic_kind: retail / regulatory / social / business / mortgage /
    #            deposit / insurance / investment / loan / mixed

    needs_regulatory: bool = False
    needs_government_programs: bool = False
    needs_pdf_search: bool = True   # практически всегда нужны PDF тарифы

    regulatory_domains: list[str] = field(default_factory=list)
    regulatory_query_hints: list[str] = field(default_factory=list)

    # Какие секции отчёта релевантны (подсказка outline_planner'у)
    applicable_section_kinds: list[str] = field(default_factory=list)

    # Текстовое описание темы для других модулей
    summary: str = ""


SYSTEM_PROMPT = """Ты — классификатор тем для банковского аудита. На основе
вопроса аудитора определяешь:

1) topic_kind — к какой категории относится вопрос:
   • retail            — карты, переводы, банкоматы (массовый продукт)
   • regulatory        — доверенности, наследование, опека (юридические продукты)
   • social            — соц. карты, ветеранские, маткапитал, пенсионные программы
   • business          — РКО, эквайринг, кредиты МСП, зарплатные проекты
   • mortgage          — ипотека (любая)
   • deposit           — вклады, накопительные счета
   • insurance         — страхование (ОСАГО/КАСКО/жизни)
   • investment        — брокерские счета, ИИС, ПИФы
   • loan              — потребительские кредиты, автокредиты
   • mixed             — несколько категорий одновременно

2) needs_regulatory (bool) — нужно ли загружать НПА (ГК РФ, ФЗ, инструкции ЦБ)?
   ✓ YES для: доверенности, наследование, страхование вкладов (АСВ),
            военная/семейная ипотека, маткапитал, налоги, эквайринг (115-ФЗ)
   ✗ NO для:  обычные карты, базовые переводы, повседневный кешбэк

3) needs_government_programs (bool) — относится ли тема к гос. программам?
   ✓ YES для: военная ипотека, семейная ипотека, маткапитал, льготные программы,
            социальные карты, пенсионные программы СФР, единые пособия

4) regulatory_domains (list[str]) — какие официальные домены подгружать?
   Выбирай из каталога, релевантные ТЕМЕ:
     • cbr.ru — ЦБ РФ (большинство банковских вопросов)
     • pravo.gov.ru — официальная публикация законов
     • consultant.ru, garant.ru — справочно-правовые системы
     • mil.ru — Минобороны (военная ипотека, военнослужащие)
     • rosvoenipoteka.ru — Росвоенипотека
     • notariat.ru, mgnp.info — нотариат (доверенности, наследование)
     • sfr.gov.ru, gosuslugi.ru — соц. фонд (пенсии, пособия)
     • asv.org.ru — АСВ (страхование вкладов)
     • domrf.ru — ДОМ.РФ (ипотечные программы)
     • nalog.gov.ru — ФНС (налогообложение продуктов)
     • fas.gov.ru — ФАС (тарифные практики)
   Включай ТОЛЬКО релевантные — НЕ все подряд.

5) regulatory_query_hints (list[str]) — конкретные термины для поиска НПА:
   ✅ ["ст. 185 ГК РФ доверенность", "информационное письмо ЦБ доверенность"]
   ✅ ["ФЗ-117 военная ипотека", "Накопительно-ипотечная система НИС"]
   ✅ ["ФЗ-177 страхование вкладов АСВ 1.4 млн"]
   ❌ ["банковский закон"] — слишком общо

6) applicable_section_kinds (list[str]) — какие секции отчёта стоит включить:
   Доступные kinds:
     • key_findings         (обязательно всегда)
     • comparison_table     (обязательно всегда)
     • per_entity_breakdown (обязательно всегда)
     • pricing_breakdown    (для retail/deposit/loan/mortgage)
     • digital_channels     (для retail/business)
     • regulatory_box       (для regulatory/social/mortgage с гос-программой)
     • cant_do_box          (для regulatory/business — где есть ограничения)
     • requirements_box     (для mortgage/loan/regulatory)
     • government_programs  (для social/mortgage с льготами)
     • risks_recommendations (обязательно всегда)
     • conflicts_explained  (если ожидаются расхождения)

7) summary — 1 короткое предложение что это за продукт (для логов).

ВЫХОД: JSON-объект. БЕЗ преамбулы, БЕЗ markdown-fences.
{
  "topic_kind": "regulatory",
  "needs_regulatory": true,
  "needs_government_programs": false,
  "regulatory_domains": ["cbr.ru", "consultant.ru", "notariat.ru", "mgnp.info"],
  "regulatory_query_hints": ["ст. 185-189 ГК РФ доверенность",
                              "информационное письмо ЦБ доверенность банковский счёт"],
  "applicable_section_kinds": ["key_findings", "comparison_table",
                                 "per_entity_breakdown", "regulatory_box",
                                 "cant_do_box", "requirements_box",
                                 "risks_recommendations"],
  "summary": "Юридический продукт — доверенность на банковский счёт"
}"""


async def classify_topic(client: AsyncOpenAI, question: str,
                           model: str | None = None) -> TopicProfile:
    """Анализирует вопрос и возвращает TopicProfile."""
    model = model or os.getenv("LLM_MODEL_FAST") or \
              os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")

    user_msg = (
        f"# Вопрос аудитора\n{question}\n\n"
        f"Определи topic_kind и стратегию загрузки источников. JSON."
    )

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=800, temperature=0.0,
            ),
            timeout=30,
        )
    except Exception as e:
        log.warning("[topic_classifier] LLM failed (%s): %s — using retail fallback",
                     type(e).__name__, e or "(empty)")
        return _default_profile(question)

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_object(raw)
    if not isinstance(data, dict):
        log.warning("[topic_classifier] no JSON object (raw 200=%r)", raw[:200])
        return _default_profile(question)

    # Sanitize
    topic_kind = str(data.get("topic_kind") or "retail").strip().lower()
    valid_kinds = {"retail", "regulatory", "social", "business", "mortgage",
                   "deposit", "insurance", "investment", "loan", "mixed"}
    if topic_kind not in valid_kinds:
        topic_kind = "retail"

    needs_reg = bool(data.get("needs_regulatory", False))
    needs_gov = bool(data.get("needs_government_programs", False))

    # Регуляторные домены — только из каталога
    reg_domains_raw = data.get("regulatory_domains") or []
    if not isinstance(reg_domains_raw, list):
        reg_domains_raw = []
    reg_domains = [d for d in reg_domains_raw
                    if isinstance(d, str) and d.lower() in REGULATORY_DOMAIN_CATALOG]
    # Если needs_regulatory=True но domains пуст — дефолтные
    if needs_reg and not reg_domains:
        reg_domains = ["cbr.ru", "consultant.ru", "pravo.gov.ru"]

    # Регуляторные query hints
    reg_hints = data.get("regulatory_query_hints") or []
    if not isinstance(reg_hints, list):
        reg_hints = []
    reg_hints = [str(h).strip() for h in reg_hints if h and len(str(h)) < 200][:6]

    # Section kinds
    section_kinds = data.get("applicable_section_kinds") or []
    if not isinstance(section_kinds, list):
        section_kinds = []
    valid_sections = {"key_findings", "comparison_table", "per_entity_breakdown",
                       "pricing_breakdown", "digital_channels", "regulatory_box",
                       "cant_do_box", "requirements_box", "government_programs",
                       "risks_recommendations", "conflicts_explained"}
    section_kinds = [s for s in section_kinds
                      if isinstance(s, str) and s.lower() in valid_sections]

    summary = str(data.get("summary") or "").strip()[:200]

    profile = TopicProfile(
        topic_kind=topic_kind,
        needs_regulatory=needs_reg,
        needs_government_programs=needs_gov,
        regulatory_domains=reg_domains,
        regulatory_query_hints=reg_hints,
        applicable_section_kinds=section_kinds,
        summary=summary or f"Тема: {topic_kind}",
    )

    # Детерминированный override субсидий/льгот — на всех путях (см. helper).
    profile = _apply_subsidy_override(profile, question)

    log.warning("[topic_classifier] kind=%s reg=%s gov=%s domains=%s hints=%d sections=%d",
                 profile.topic_kind, profile.needs_regulatory,
                 profile.needs_government_programs,
                 profile.regulatory_domains, len(profile.regulatory_query_hints),
                 len(profile.applicable_section_kinds))
    return profile


def _default_profile(question: str) -> TopicProfile:
    """Безопасный дефолт для случая когда LLM упал."""
    profile = TopicProfile(
        topic_kind="retail",
        needs_regulatory=False,
        needs_government_programs=False,
        needs_pdf_search=True,
        regulatory_domains=[],
        regulatory_query_hints=[],
        applicable_section_kinds=["key_findings", "comparison_table",
                                    "per_entity_breakdown", "pricing_breakdown",
                                    "risks_recommendations"],
        summary=f"Fallback: {question[:100]}",
    )
    # Даже в fallback: тема субсидий/льгот → грузим НПА и госпрограммы.
    return _apply_subsidy_override(profile, question)


def _parse_json_object(raw: str) -> dict | None:
    """Извлекает JSON-объект из ответа LLM (с обработкой fences)."""
    if not raw:
        return None
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    end = -1
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
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    cand = t[start:end] if end > 0 else t[start:].rstrip().rstrip(",") + "}"
    try:
        return json.loads(cand)
    except Exception:
        pass
    try:
        return json.loads(re.sub(r",\s*([\]}])", r"\1", cand))
    except Exception:
        return None
