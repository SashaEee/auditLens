"""Research Brief — стадия ГЛОБАЛЬНОГО СИНТЕЗА перед генерацией секций.

Главная архитектурная правка против «поверхностности»: до research_brief каждая
секция отчёта генерировалась изолированно и видела лишь срез фактов → получался
набор локальных summary («как будто generic ChatGPT»). Здесь ОДИН «тяжёлый»
reasoning-вызов смотрит на ВСЮ картину (все факты + матрица + конфликты + пробелы
+ сырые выдержки источников) и строит аналитический меморандум:
  • thesis            — главный ответ на вопрос аудитора одним абзацем
  • insights          — нетривиальные выводы с привязкой к источникам [N]
  • bank_archetypes   — чем каждый банк характерен (одна фраза)
  • key_tradeoffs     — ключевые компромиссы/ловушки «витрина↔реальность»
  • critical_gaps     — пробелы, которые МЕНЯЮТ вывод
  • section_directives — что именно должна раскрыть каждая секция

Меморандум прокидывается в NarrativeContext и становится «единым мозгом»,
которому подчиняются все генераторы секций.

Использует reasoning-модель (LLM_MODEL_REASONING) с reasoning_effort=high.
Graceful: при любой ошибке возвращает None — пайплайн деградирует к прежнему
поведению (секции без brief).
"""
from __future__ import annotations
import asyncio
import logging
import os
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from .fact import Fact
from .entity_extractor import Entity
from .matrix_builder import Matrix
from .core_schema import CoreAttr
from .narrative_generators.base import parse_json_object, format_facts_for_prompt
from ..clock import today_anchor

log = logging.getLogger(__name__)


@dataclass
class Insight:
    claim: str
    why_it_matters: str = ""
    evidence_idx: list[int] = field(default_factory=list)   # source [N]
    banks: list[str] = field(default_factory=list)
    confidence: str = "medium"


@dataclass
class ResearchBrief:
    thesis: str = ""
    insights: list[Insight] = field(default_factory=list)
    bank_archetypes: dict[str, str] = field(default_factory=dict)
    key_tradeoffs: list[str] = field(default_factory=list)
    critical_gaps: list[str] = field(default_factory=list)
    section_directives: dict[str, str] = field(default_factory=dict)

    def directive(self, kind: str) -> str:
        return self.section_directives.get(kind, "")

    def brief_context(self, max_insights: int = 8) -> str:
        """Сжатый меморандум для подмешивания в промпт генератора секции."""
        lines = []
        if self.thesis:
            lines.append(f"ГЛАВНЫЙ ТЕЗИС ОТЧЁТА: {self.thesis}")
        if self.insights:
            lines.append("КЛЮЧЕВЫЕ ИНСАЙТЫ (используй их как опору, раскрывай глубже):")
            for i, ins in enumerate(self.insights[:max_insights], 1):
                cites = "".join(f"[{n}]" for n in ins.evidence_idx[:4])
                imp = f" → {ins.why_it_matters}" if ins.why_it_matters else ""
                lines.append(f"  {i}. {ins.claim}{imp} {cites}")
        if self.key_tradeoffs:
            lines.append("ЛОВУШКИ / ВИТРИНА↔РЕАЛЬНОСТЬ: " + "; ".join(self.key_tradeoffs[:6]))
        if self.critical_gaps:
            lines.append("КРИТИЧНЫЕ ПРОБЕЛЫ: " + "; ".join(self.critical_gaps[:6]))
        return "\n".join(lines)


SYSTEM_PROMPT = """Ты — главный аудитор-аналитик банковских продуктов. Тебе дают
ВСЕ собранные факты, сравнительную матрицу, расхождения, пробелы и ДОСЛОВНЫЕ
выдержки из источников. Твоя задача — НЕ написать отчёт, а построить
АНАЛИТИЧЕСКИЙ МЕМОРАНДУМ, который будет управлять написанием отчёта.

Думай как настоящий аудитор, а не как пересказчик:
  • Найди НЕОЧЕВИДНОЕ: где «витрина» (реклама/заголовок) расходится с реальными
    условиями; скрытые комиссии/лимиты; ставки «до X», доступные единицам.
  • Сравнивай банки ОТНОСИТЕЛЬНО друг друга (во сколько раз, на сколько ₽/п.п.).
  • Выдели АРХЕТИП каждого банка (в чём его стратегия/подвох одной фразой).
  • Назови РИСКИ и ПРОБЕЛЫ, которые реально меняют вывод.
  • Каждый инсайт — с опорой на источники [N] (используй source_idx из фактов).

ПРАВИЛА:
  • Только то, что следует из данных. Не выдумывай цифр.
  • НЕ складывай разнотипные величины: разовую страховку/комиссию (% от суммы) НЕЛЬЗЯ
    прибавлять к годовой ставке ради «APR». ПСК/APR — только если ЯВНО в источнике.
  • Инсайты — нетривиальные (не «у банка есть карта»), а аналитические выводы.
  • section_directives — короткая инструкция (1-2 предложения) каждой секции:
    что именно она должна раскрыть в свете общего тезиса.

ВЫХОД: строго JSON-объект, без преамбулы и markdown-fences:
{
  "thesis": "ответ на вопрос аудитора одним плотным абзацем",
  "insights": [
    {"claim": "...", "why_it_matters": "...", "evidence_idx": [1,4],
     "banks": ["sber"], "confidence": "high|medium|low"}
  ],
  "bank_archetypes": {"sber": "одна фраза про стратегию/подвох", "...": "..."},
  "key_tradeoffs": ["витрина X vs реальность Y", "..."],
  "critical_gaps": ["что не раскрыто и почему это важно", "..."],
  "section_directives": {
    "key_findings": "...", "per_entity_breakdown": "...",
    "pricing_breakdown": "...", "risks_recommendations": "..."
  }
}"""


def _matrix_summary(matrix: Matrix, core_schema: list[CoreAttr] | None) -> str:
    banks = [e.bank_name for e in matrix.entities]
    core = [a.name for a in (core_schema or [])]
    lines = [f"Банки: {', '.join(banks)}",
             f"Покрытие core-схемы: {round(matrix.coverage * 100)}%"]
    if matrix.conflicts:
        conf = []
        for (bank, attr), group in list(matrix.conflicts.items())[:10]:
            vals = " ↔ ".join(sorted({f"{g.value}{g.unit}" for g in group}))
            conf.append(f"{bank}/{attr}: {vals}")
        lines.append("РАСХОЖДЕНИЯ В ИСТОЧНИКАХ: " + " | ".join(conf))
    # пробелы по core
    if core:
        gaps = []
        for a in core:
            missing = [e.bank_name for e in matrix.entities
                        if matrix.cell(e.bank_slug, a) is None]
            if missing:
                gaps.append(f"{a}: нет у {', '.join(missing)}")
        if gaps:
            lines.append("ПРОБЕЛЫ (не раскрыто): " + " | ".join(gaps[:12]))
    return "\n".join(lines)


def _top_excerpts(sources_index: list[dict], max_n: int = 10,
                    per_chars: int = 600) -> str:
    """Сырые выдержки источников — чтобы синтез видел живой язык, не только факты.

    ПО-БАНКОВЫЙ round-robin (item 43): сначала по одной лучшей выдержке КАЖДОГО
    банка, потом добиваем остаток по trust. Иначе brief видел выдержки только
    лучше-источенных банков, и хуже-покрытые выпадали из синтеза."""
    by_bank: dict[str, list[dict]] = {}
    for s in sources_index:
        if not (s.get("excerpts")):
            continue
        by_bank.setdefault(s.get("bank_slug") or "__shared__", []).append(s)
    for slug in by_bank:
        by_bank[slug].sort(key=lambda s: -(s.get("trust_score") or 0))

    picked: list[dict] = []
    seen_n = set()
    # round-robin
    idx = 0
    while len(picked) < max_n and any(idx < len(v) for v in by_bank.values()):
        for slug, lst in by_bank.items():
            if idx < len(lst) and lst[idx].get("n") not in seen_n:
                picked.append(lst[idx])
                seen_n.add(lst[idx].get("n"))
                if len(picked) >= max_n:
                    break
        idx += 1

    out = []
    for s in picked:
        exc = " ".join(s.get("excerpts") or [])[:per_chars].strip()
        if not exc:
            continue
        out.append(f"[{s.get('n')}] {s.get('domain','')}: {exc}")
    return "\n\n".join(out)


async def synthesize_brief(client: AsyncOpenAI, question: str,
                             entities: list[Entity], facts: list[Fact],
                             matrix: Matrix, sources_index: list[dict],
                             core_schema: list[CoreAttr] | None = None,
                             model: str | None = None) -> ResearchBrief | None:
    """Главная: один reasoning-вызов → аналитический меморандум."""
    if not facts:
        return None
    model = model or os.getenv("LLM_MODEL_REASONING") or \
              os.getenv("LLM_MODEL_SMART") or os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")
    # Качество ВСЕГО отчёта держится на этом синтезе. Если reasoning-модель не
    # задана и используется дешёвый fallback — громко предупреждаем (item 38).
    if not os.getenv("LLM_MODEL_REASONING"):
        log.warning("[research_brief] LLM_MODEL_REASONING не задан → синтез на "
                     "fallback-модели %s. Для глубины задайте reasoning-модель.", model)

    # Кормим больше фактов, чем прежние 80 (для multi-bank 80 срезало картину),
    # но без перегиба: 110 — баланс глубины и риска таймаута на одном «тяжёлом»
    # reasoning-вызове (синтез — самый ценный шаг, его нельзя терять по таймауту).
    facts_block = format_facts_for_prompt(facts, with_source=True, max_facts=110)
    matrix_block = _matrix_summary(matrix, core_schema)
    excerpts_block = _top_excerpts(sources_index)

    user_msg = (
        f"# ВОПРОС АУДИТОРА\n{question}\n\n"
        f"# СВОДКА МАТРИЦЫ\n{matrix_block}\n\n"
        f"# ФАКТЫ ({len(facts)})\n{facts_block}\n\n"
        f"# ДОСЛОВНЫЕ ВЫДЕРЖКИ ИСТОЧНИКОВ\n{excerpts_block}\n\n"
        f"Построй аналитический меморандум (JSON). Думай как аудитор: ищи "
        f"витрина↔реальность, сравнивай банки относительно, дай архетипы и директивы секциям."
    )
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": today_anchor() + "\n\n" + SYSTEM_PROMPT},
                          {"role": "user", "content": user_msg}],
                max_tokens=4500, temperature=0.0,
                # NB: НЕ форсим reasoning_effort=high — gpt-oss-120b при high льёт
                # CoT в content и ломает JSON. Дефолтный effort даёт чистый JSON.
                # Когда LLM_MODEL_REASONING = настоящая reasoning-модель — поднять.
            ),
            timeout=170,   # запас под «тяжёлый» reasoning-вызов под нагрузкой
        )
    except Exception as e:
        log.warning("[research_brief] LLM failed: %r — fallback без brief", e)
        return None

    data = parse_json_object(resp.choices[0].message.content or "")
    if not isinstance(data, dict) or not data.get("thesis"):
        log.warning("[research_brief] no usable JSON — fallback без brief")
        return None

    insights = []
    for it in (data.get("insights") or []):
        if not isinstance(it, dict) or not it.get("claim"):
            continue
        ev = it.get("evidence_idx") or []
        ev = [int(x) for x in ev if str(x).isdigit()][:6] if isinstance(ev, list) else []
        insights.append(Insight(
            claim=str(it.get("claim")).strip(),
            why_it_matters=str(it.get("why_it_matters") or "").strip(),
            evidence_idx=ev,
            banks=[str(b) for b in (it.get("banks") or []) if b][:6],
            confidence=str(it.get("confidence") or "medium").strip().lower(),
        ))

    arche = data.get("bank_archetypes") or {}
    arche = {str(k): str(v) for k, v in arche.items()} if isinstance(arche, dict) else {}
    directives = data.get("section_directives") or {}
    directives = {str(k): str(v) for k, v in directives.items()} if isinstance(directives, dict) else {}

    brief = ResearchBrief(
        thesis=str(data.get("thesis")).strip(),
        insights=insights,
        bank_archetypes=arche,
        key_tradeoffs=[str(x).strip() for x in (data.get("key_tradeoffs") or []) if x][:8],
        critical_gaps=[str(x).strip() for x in (data.get("critical_gaps") or []) if x][:8],
        section_directives=directives,
    )
    log.warning("[research_brief] OK: thesis=%d chars, %d insights, %d archetypes, %d directives",
                 len(brief.thesis), len(brief.insights), len(brief.bank_archetypes),
                 len(brief.section_directives))
    return brief
