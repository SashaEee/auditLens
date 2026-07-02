"""Researcher Agent — универсальный сбор фактов по любому продукту/услуге.

Не хардкодит слоты. Получает от Кондуктора конкретные параметры для темы
(для автоперевода — триггеры/комиссии/лимиты; для ипотеки — ставка/ПВ/срок;
для качества обслуживания — каналы/время ответа/оценки).

Автономен: сам решает сколько источников прочитать, когда достаточно.
Финальный ответ — JSON со списком фактов, которые _integrate кладёт в bundle.
"""
from __future__ import annotations

import logging

from ..base_agent import BaseAgent
from ..knowledge_bundle import Fact
from ..tools.tool_specs import RESEARCHER_TOOLS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — research-агент для аудиторской платформы. Твоя задача —
собрать конкретные факты по заданию, используя инструменты поиска и чтения.

СТРАТЕГИЯ — СНАЧАЛА СТРУКТУРА И КЭШ, ПОТОМ ВЕБ (меньше шума И меньше капчи):
  0. Для тарифов/ставок/лимитов/комиссий — СНАЧАЛА run_sql по структурной БД
     (offers / change_history): это уже собранные данные, БЕЗ похода в веб и БЕЗ
     риска капчи. Возьми оттуда всё, что есть по теме.
  1. Затем semantic_search по кэшу БД — часть данных уже проиндексирована.
  2. Только то, чего НЕ хватает, добирай вебом: 1-2 ТОЧЕЧНЫХ web_search на банк
     (site:{bank.ru} + тема, и один независимый site:banki.ru). НЕ делай 5-6
     поисков подряд — после 1-2 у тебя уже есть кандидаты.
  3. Из найденного прочитай read_url 2-4 САМЫХ релевантных. ПРИОРИТЕТ ИСТОЧНИКА:
     офиц. сайт банка / PDF тарифов / регулятор (cbr.ru) — они и достовернее, и
     реже прячутся за антиботом. Агрегаторы — во вторую очередь. Нерелевантное
     (не про тему/не про этот банк) — НЕ читай.
  4. Извлекай факты: каждое число/условие → отдельный факт со ссылкой [N].
  5. Если источник заблокирован (капча/недоступен) или данных нет — честно
     зафиксируй это в "gaps", НЕ выдумывай. Не ищи бесконечно.

ПРАВИЛА ИЗВЛЕЧЕНИЯ ФАКТОВ:
  • КАЖДЫЙ факт должен иметь source_n (номер источника [N] из read_url/semantic).
  • Числа — только из прочитанного текста, дословно. НЕ выдумывай.
  • Если в источнике диапазон («0,5-1,5%») — value весь диапазон.
  • Если значение условное («0 ₽ при остатке от 30 000») — conditions.
  • Различай ВИТРИНУ и РЕАЛЬНОСТЬ: «от 0%» в рекламе vs реальная базовая ставка.
  • Если на странице описано НЕСКОЛЬКО продуктов — бери только по теме задания.
  • АКТУАЛЬНОСТЬ (для аудита критично): для КАЖДОГО факта ОБЯЗАТЕЛЬНО заполни
    as_of (год/период из источника или дата страницы). Предпочитай первоисточник
    (офиц. сайт банка / PDF тарифов) свежим данным. Если источник старше 2 лет —
    ищи актуальнее; если свежее нет — оставь, но честно укажи год в as_of.

АНАЛИТИЧЕСКАЯ ГЛУБИНА (важно!):
  • Замечай терминологические ловушки (напр. «автоплатёж» = оплата услуг C2B,
    а «автоперевод» = перевод человеку C2C — РАЗНЫЕ тарифы).
  • Замечай регуляторные изменения (реформа ЦБ уравняла цены и т.п.).
  • Если заметил — добавь в поле "insights" в финальном ответе.

ВЫХОД (строгий JSON, БЕЗ markdown):
{
  "facts": [
    {"subject":"Сбербанк","attribute":"комиссия внешнего перевода",
     "value":"1%, мин 30 ₽","source_n":3,
     "verbatim":"дословная цитата","conditions":["при переводе на карту другого банка"],
     "as_of":"2024","confidence":0.9,"tags":["fee"]}
  ],
  "insights": [
    {"headline":"Терминологическая ловушка",
     "explanation":"автоплатёж (C2B) и автоперевод (C2C) — разные тарифы",
     "evidence_ns":[3,5]}
  ],
  "gaps": [
    {"subject":"Газпромбанк","what":"офиц.страница автоперевода не в индексе",
     "recommendation":"запросить тарифный документ напрямую"}
  ],
  "summary": "Собрано N фактов по M объектам. Главный инсайт: ..."
}

Готов вернуть ответ когда:
  • покрыты ВСЕ объекты из задания (или честно указаны пробелы)
  • есть ≥2 факта на объект (где данные реально есть)
  • сделано ≥3 tool-вызова (иначе данных слишком мало)
"""


class ResearcherAgent(BaseAgent):
    """Универсальный research-агент. Адаптируется к любой теме через mission."""
    SYSTEM_PROMPT = SYSTEM_PROMPT
    TOOLS = RESEARCHER_TOOLS
    # Навигация (поиск/чтение) — быстрая модель; финальное извлечение фактов —
    # сильная (точность чисел/цитат критична, плюс Critic перепроверяет).
    MODEL_TIER = "fast"
    FINAL_MODEL_TIER = "smart"

    async def _integrate(self, artifacts: dict) -> None:
        """Превращает JSON-артефакты агента в Fact/Insight/CoverageNote в bundle."""
        # facts
        for f in (artifacts.get("facts") or []):
            if not isinstance(f, dict):
                continue
            try:
                source_n = int(f.get("source_n") or 0)
            except (TypeError, ValueError):
                source_n = 0
            if source_n <= 0:
                continue
            subject = str(f.get("subject") or "").strip()
            attr = str(f.get("attribute") or "").strip()
            value = str(f.get("value") or "").strip()
            if not subject or not attr or not value:
                continue
            self.bundle.add_fact(Fact(
                subject=subject,
                attribute=attr,
                value=value,
                source_n=source_n,
                verbatim=str(f.get("verbatim") or "")[:400],
                conditions=[str(c) for c in (f.get("conditions") or [])][:6],
                as_of=str(f.get("as_of") or ""),
                confidence=float(f.get("confidence") or 0.7),
                tags=[str(t) for t in (f.get("tags") or [])][:5],
            ))

        # insights (аналитические наблюдения — меняют рамку сравнения)
        from ..knowledge_bundle import Insight
        for ins in (artifacts.get("insights") or []):
            if not isinstance(ins, dict):
                continue
            headline = str(ins.get("headline") or "").strip()
            if not headline:
                continue
            self.bundle.insights.append(Insight(
                headline=headline,
                explanation=str(ins.get("explanation") or ""),
                evidence_ns=[int(n) for n in (ins.get("evidence_ns") or [])
                              if str(n).isdigit()][:8],
                impact=str(ins.get("impact") or ""),
            ))

        # gaps → coverage_notes
        from ..knowledge_bundle import CoverageNote
        for g in (artifacts.get("gaps") or []):
            if not isinstance(g, dict):
                continue
            what = str(g.get("what") or "").strip()
            if not what:
                continue
            subj = str(g.get("subject") or "").strip()
            self.bundle.coverage_notes.append(CoverageNote(
                what=what,
                subjects=[subj] if subj else [],
                reason=str(g.get("reason") or "не найдено в открытых источниках"),
                recommendation=str(g.get("recommendation") or ""),
            ))
