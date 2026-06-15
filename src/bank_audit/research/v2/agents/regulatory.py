"""Regulatory Agent — нормативная база (законы, ЦБ, ФАС, постановления).

Зову когда тема имеет регуляторный контекст: переводы, реклама, вклады,
страховки, потребительское кредитование, ипотека с господдержкой.
"""
from __future__ import annotations

import logging

from ..base_agent import BaseAgent
from ..knowledge_bundle import Regulation
from ..tools.tool_specs import REGULATORY_TOOLS

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — regulatory-агент для аудиторской платформы. Собираешь
нормативную базу по теме: какие законы/постановления/указания ЦБ регулируют
вопрос аудитора.

ИСТОЧНИКИ (приоритет):
  • cbr.ru — указания/положения/реформы ЦБ РФ (особенно пресс-релизы о реформах)
  • pravo.gov.ru — официальные тексты ФЗ/постановлений
  • consultant.ru / garant.ru — консультант/гаранта (с толкованиями)
  • fas.gov.ru — практика ФАС по недобросовестной рекламе
  • sbp.nspk.ru — правила СБП (для переводов)

СТРАТЕГИЯ — СЕЛЕКТИВНО (не гнать в количество поисков):
  1. 1-2 ТОЧЕЧНЫХ web_search: "{тема} закон ЦБ ФЗ постановление" и site:cbr.ru "{тема}".
  2. Из найденного ОТБЕРИ 2-3 САМЫХ релевантных документа (пресс-релиз ЦБ, текст
     ФЗ) и прочитай ТОЛЬКО их (read_url). Нерелевантное — НЕ читай.
  3. Извлекай КОНКРЕТНЫЕ нормы: номер ФЗ, статья, дата вступления, суть.

ВЫХОД (строгий JSON):
{
  "regulations": [
    {"subject":"переводы физлиц",
     "cite":"Реформа ЦБ 01.05.2024 + 01.11.2024",
     "summary":"Бесплатные me2me до 30 млн ₽/мес; C2C по СБП бесплатно до 100 тыс, далее 0,5% макс 1500 ₽",
     "source_n":2,"effective_from":"2024-05-01"}
  ],
  "insights": [
    {"headline":"Цены уравнены регулятором",
     "explanation":"реформа ЦБ убрала ценовую конкуренцию по переводам",
     "evidence_ns":[2,3]}
  ],
  "summary":"Найдено N норм. Главная: ..."
}

Если по теме нет регуляторного контекста (напр. «дизайн карты») — верни
{"regulations":[],"summary":"Регуляторного контекста по теме не выявлено"}.
"""


class RegulatoryAgent(BaseAgent):
    SYSTEM_PROMPT = SYSTEM_PROMPT
    TOOLS = REGULATORY_TOOLS
    # Поиск НПА; номера ФЗ перепроверяет NPA-guard ниже по конвейеру → быстрая.
    MODEL_TIER = "fast"

    async def _integrate(self, artifacts: dict) -> None:
        from ..knowledge_bundle import Insight
        for reg in (artifacts.get("regulations") or []):
            if not isinstance(reg, dict):
                continue
            cite = str(reg.get("cite") or "").strip()
            if not cite:
                continue
            try:
                src_n = int(reg.get("source_n") or 0)
            except (TypeError, ValueError):
                src_n = 0
            self.bundle.regulations.append(Regulation(
                subject=str(reg.get("subject") or ""),
                cite=cite,
                summary=str(reg.get("summary") or ""),
                source_n=src_n,
                effective_from=str(reg.get("effective_from") or ""),
            ))
        # insights из регуляторного контекста — частое место для инсайтов
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
