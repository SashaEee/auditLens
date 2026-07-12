"""Узлы фаз ReAct-графа чата loophole.

Фазы: clarify → plan → execute → aggregate → answer → done.

Каждый узел — async-функция ``(state, *, llm=None, session=None) -> ChatState``.
Промпты лежат в ``chat/prompt/01_clarify.md`` … ``06_keywords.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from . import clarify as clarify_mod
from . import tools as chat_tools
from .state import ChatState
from .tools import load_prompt

from ...ai.llm_utils import _loose_json_loads
from .. import repository as repo
from ..pii_mask import mask as pii_mask

log = logging.getLogger(__name__)

_MAX_REACT_ITERATIONS = 10
_CONCURRENCY = 3


# ── Хелперы LLM ──────────────────────────────────────────────────────────────
def _default_llm() -> Any:
    from langchain_openai import ChatOpenAI
    import os

    from ..config import LoopholeSettings

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_chat_model()
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, temperature=0.3)


def _llm_content(resp: Any) -> str:
    return getattr(resp, "content", None) or str(resp)


async def _llm_invoke(llm: Any, system: str, user: str) -> str:
    from langchain_core.messages import HumanMessage, SystemMessage

    resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    return _llm_content(resp)


# ── clarify ──────────────────────────────────────────────────────────────────
async def clarify_node(state: ChatState, *, llm: Any = None) -> ChatState:
    """Фаза clarify: проверка полноты запроса + уточняющие вопросы.

    complete=true → phase='plan'. Иначе → phase='await_clarify'.
    Создаёт агентную задачу в БД (если есть workspace_id).
    """
    query = state.get("query", "")
    history = state.get("messages") or []
    try:
        result = await clarify_mod.generate_clarifications(query, history=history)
    except Exception as e:
        log.warning("[clarify_node] failed: %s — fail-open", e)
        result = {"complete": True, "questions": [], "reason": "error"}

    task_id = state.get("task_id")
    workspace_id = state.get("workspace_id")
    session = state.get("session")
    if not task_id and workspace_id:
        try:
            task_id = repo.save_task(
                workspace_id,
                query,
                phase="clarify",
                clarify_questions=result.get("questions") or None,
                session=session,
            )
        except Exception as e:
            log.warning("[clarify_node] save_task failed: %s", e)

    if result.get("complete"):
        return {
            **state,
            "phase": "plan",
            "task_id": task_id,
            "clarify_questions": [],
        }
    return {
        **state,
        "phase": "await_clarify",
        "task_id": task_id,
        "clarify_questions": result.get("questions") or [],
    }


# ── plan ─────────────────────────────────────────────────────────────────────
async def plan_node(state: ChatState, *, llm: Any = None) -> ChatState:
    """Фаза plan: декомпозиция запроса на подзадачи через LLM (02_plan.md)."""
    query = state.get("query", "")
    enriched = query
    answers = state.get("clarify_answers") or []
    if answers:
        try:
            enriched = await clarify_mod.build_enriched_question(query, answers)
        except Exception as e:
            log.warning("[plan_node] build_enriched failed: %s", e)

    system = load_prompt("02_plan")
    user = f"Уточнённый запрос:\n{enriched}\n\nВерни JSON по контракту."
    subtasks: list[dict] = []
    try:
        if llm is None:
            llm = _default_llm()
        raw = await _llm_invoke(llm, system, user)
        data = _loose_json_loads(raw)
        if isinstance(data, dict):
            subtasks = data.get("subtasks") or []
        elif isinstance(data, list):
            subtasks = data
    except Exception as e:
        log.warning("[plan_node] LLM failed: %s", e)
        subtasks = []

    task_id = state.get("task_id")
    session = state.get("session")
    if task_id:
        try:
            repo.update_task(
                task_id,
                phase="plan",
                subtasks=subtasks,
                enriched_query=enriched if enriched != query else None,
                session=session,
            )
        except Exception as e:
            log.warning("[plan_node] update_task failed: %s", e)

    return {
        **state,
        "phase": "execute",
        "query": enriched,
        "subtasks": subtasks,
        "subtask_results": [],
        "iterations": 0,
    }


# ── execute (ReAct по подзадачам) ────────────────────────────────────────────
_THOUGHT_RE = re.compile(r"Thought:\s*(.*?)(?=\nAction:|\nFinal Answer:|\Z)", re.DOTALL)
_ACTION_RE = re.compile(r"Action:\s*(\S+)", re.DOTALL)
_ACTION_INPUT_RE = re.compile(r"Action Input:\s*(.*?)(?=\nThought:|\nObservation:|\Z)", re.DOTALL)
_FINAL_RE = re.compile(r"Final Answer:\s*(.*)", re.DOTALL)


def _parse_react_step(text: str) -> dict:
    """Парсит один Thought/Action/Action Input блок из ответа LLM."""
    final = _FINAL_RE.search(text)
    if final:
        return {"final_answer": final.group(1).strip()}
    action = _ACTION_RE.search(text)
    if not action:
        return {}
    action_name = action.group(1).strip()
    inp = _ACTION_INPUT_RE.search(text)
    args: dict = {}
    if inp:
        raw_args = inp.group(1).strip()
        try:
            parsed = _loose_json_loads(raw_args)
            if isinstance(parsed, dict):
                args = parsed
            else:
                args = {"value": parsed}
        except Exception:
            args = {"raw": raw_args}
    thought = _THOUGHT_RE.search(text)
    return {
        "thought": thought.group(1).strip() if thought else "",
        "action": action_name,
        "args": args,
    }


async def _run_subtask_react(
    subtask: dict,
    state: ChatState,
    *,
    llm: Any,
    session: Any = None,
) -> dict:
    """ReAct-цикл для одной подзадачи. До ``_MAX_REACT_ITERATIONS`` итераций."""
    system = load_prompt("03_react")
    title = subtask.get("title") or subtask.get("id") or "подзадача"
    algorithm = subtask.get("algorithm") or ""
    tools_list = subtask.get("tools") or []
    context = (
        f"Подзадача: {title}\nАлгоритм:\n{algorithm}\n"
        f"Доступные инструменты: {', '.join(tools_list)}\n"
        f"Запрос: {state.get('query', '')}\n"
    )
    history_parts: list[str] = [context]
    observations: list[dict] = []
    final_answer = ""

    for it in range(_MAX_REACT_ITERATIONS):
        try:
            raw = await _llm_invoke(llm, system, "\n".join(history_parts))
        except Exception as e:
            log.warning("[execute] LLM iter %s failed: %s", it, e)
            break
        step = _parse_react_step(raw)
        if step.get("final_answer"):
            final_answer = step["final_answer"]
            break
        action = step.get("action")
        args = step.get("args") or {}
        if not action:
            history_parts.append(f"\nThought: не удалось распарсить ответ LLM.\n{raw}")
            continue
        history_parts.append(f"\n{raw}")
        # Исполняем tool.
        try:
            result = chat_tools.dispatch(action, args, session=session)
            if asyncio.iscoroutine(result):
                result = await result
        except Exception as e:
            result = {"error": str(e)}
        observations.append({"action": action, "args": args, "result": result})
        obs_text = json.dumps(result, ensure_ascii=False, default=str)[:2000]
        history_parts.append(f"\nObservation: {obs_text}")
    else:
        # Лимит итераций исчерпан — итог по имеющимся наблюдениям.
        final_answer = (
            f"Достигнут лимит итераций ({_MAX_REACT_ITERATIONS}). "
            f"Накоплено наблюдений: {len(observations)}."
        )

    return {
        "id": subtask.get("id"),
        "title": title,
        "observations": observations,
        "final_answer": final_answer,
        "iterations": len(observations),
    }


async def execute_node(
    state: ChatState,
    *,
    llm: Any = None,
    session: Any = None,
) -> ChatState:
    """Фаза execute: параллельный ReAct-прогон подзадач через asyncio.gather.

    Semaphore(3) ограничивает параллелизм. После прогона → phase='aggregate'.
    Если iterations >= _MAX_REACT_ITERATIONS и есть незавершённые → остаёмся в execute.
    """
    subtasks = state.get("subtasks") or []
    if not subtasks:
        return {**state, "phase": "aggregate", "subtask_results": []}
    if llm is None:
        llm = _default_llm()
    sess = session if session is not None else state.get("session")
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _guarded(st: dict) -> dict:
        async with sem:
            return await _run_subtask_react(st, state, llm=llm, session=sess)

    results = await asyncio.gather(*(_guarded(st) for st in subtasks), return_exceptions=True)
    clean: list[dict] = []
    for r in results:
        if isinstance(r, Exception):
            clean.append({"error": str(r)})
        else:
            clean.append(r)

    iterations = (state.get("iterations") or 0) + 1
    task_id = state.get("task_id")
    if task_id:
        try:
            repo.update_task(
                task_id,
                phase="execute",
                subtask_results=clean,
                iterations=iterations,
                session=sess,
            )
        except Exception as e:
            log.warning("[execute_node] update_task failed: %s", e)

    # Все подзадачи выполнены — переходим к агрегации.
    return {
        **state,
        "phase": "aggregate",
        "subtask_results": clean,
        "iterations": iterations,
    }


# ── aggregate ────────────────────────────────────────────────────────────────
def _collect_loopholes(subtask_results: list[dict]) -> list[dict]:
    """Собирает лазейки из observations (результаты /extract_loopholes)."""
    out: list[dict] = []
    for sr in subtask_results or []:
        if not isinstance(sr, dict):
            continue
        for obs in sr.get("observations") or []:
            result = obs.get("result")
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict) and item.get("is_loophole"):
                        out.append(item)
            elif isinstance(result, dict) and "loopholes" in result:
                for item in result["loopholes"]:
                    if isinstance(item, dict) and item.get("is_loophole"):
                        out.append(item)
    return out


async def aggregate_node(state: ChatState, *, llm: Any = None) -> ChatState:
    """Фаза aggregate: фильтрация is_loophole=true, маскировка ПД, подготовка записей."""
    subtask_results = state.get("subtask_results") or []
    loopholes = _collect_loopholes(subtask_results)

    # Маскируем ПД в текстовых полях каждой лазейки.
    masked_loopholes: list[dict] = []
    for item in loopholes:
        masked_item = dict(item)
        for field in ("title", "description", "evidence_quote"):
            if masked_item.get(field):
                masked, _ = pii_mask(str(masked_item[field]))
                masked_item[field] = masked
        masked_loopholes.append(masked_item)

    # Готовим записи для таблицы фронта.
    table_records: list[dict] = []
    for item in masked_loopholes:
        rec = {
            "title": item.get("title"),
            "url": item.get("url"),
            "domain": item.get("domain"),
            "bank_slug": item.get("bank_slug"),
            "verdict_confidence": item.get("verdict_confidence") or 0.8,
            "verdict_reason": item.get("description") or item.get("category") or "",
            "category": item.get("category"),
            "severity": item.get("severity"),
            "evidence_quote": item.get("evidence_quote"),
        }
        table_records.append(rec)

    # LLM-сводка (опционально).
    summary = ""
    try:
        if llm is None:
            llm = _default_llm()
        system = load_prompt("05_aggregate")
        user = (
            f"Результаты подзадач:\n"
            f"{json.dumps(subtask_results, ensure_ascii=False, default=str)[:4000]}\n\n"
            f"Найденные лазейки:\n"
            f"{json.dumps(masked_loopholes, ensure_ascii=False, default=str)[:4000]}\n\n"
            f"Верни JSON по контракту."
        )
        raw = await _llm_invoke(llm, system, user)
        data = _loose_json_loads(raw)
        if isinstance(data, dict):
            summary = str(data.get("summary") or "")
            if isinstance(data.get("table_records"), list):
                table_records = data["table_records"]
    except Exception as e:
        log.warning("[aggregate_node] LLM failed: %s", e)

    task_id = state.get("task_id")
    sess = state.get("session")
    if task_id:
        try:
            repo.update_task(task_id, phase="aggregate", session=sess)
        except Exception as e:
            log.warning("[aggregate_node] update_task failed: %s", e)

    return {
        **state,
        "phase": "answer",
        "records": masked_loopholes,
        "pending_table_records": table_records,
        "answer": summary,
    }


# ── answer ───────────────────────────────────────────────────────────────────
async def answer_node(state: ChatState) -> ChatState:
    """Фаза answer: финальный ответ (text + records). phase='done'."""
    answer = state.get("answer") or ""
    if not answer:
        records = state.get("records") or []
        if records:
            lines = [f"Найдено лазеек: {len(records)}."]
            for r in records[:10]:
                title = r.get("title") or "—"
                lines.append(f"• {title}")
            answer = "\n".join(lines)
        else:
            answer = "Лазеек не найдено."
    task_id = state.get("task_id")
    sess = state.get("session")
    if task_id:
        try:
            repo.update_task(task_id, phase="done", status="done", session=sess)
        except Exception as e:
            log.warning("[answer_node] update_task failed: %s", e)
    return {**state, "phase": "done", "answer": answer}
