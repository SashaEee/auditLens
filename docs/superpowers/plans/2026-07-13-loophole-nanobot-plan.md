# Loophole Nanobot Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hand-rolled ReAct agent in `src/bank_audit/loophole/chat/` with `nanobot` as the harness, while preserving the SSE API, bank-specific trust scoring, PII masking, and adding READ-ONLY text-to-SQL analytics.

**Architecture:** A new `nanobot_agent.py` harness creates a `Nanobot` instance with inline configuration and registers custom Python tools (`web_search`, `web_fetch`, `db_query`, `extract_loopholes`, `table_load`, `refine_export`). The existing `chat/graph.py` becomes a thin adapter that forwards `run_chat`/`stream_chat` calls to the harness. The old `phases.py`, `nodes.py`, and manual ReAct prompt are removed. Custom tools enforce READ-ONLY SQL and PII masking before returning data to the agent.

**Tech Stack:** Python 3.11+, `nanobot-ai`, FastAPI + SSE, SQLAlchemy Core (Greenplum 6 dialect), existing `search_decorator`/`fetch_decorator`, Pydantic models.

---

## Files Overview

| File | Action | Responsibility |
|------|--------|----------------|
| `pyproject.toml` | Modify | Add `nanobot-ai` dependency. |
| `src/bank_audit/loophole/config.py` | Modify | Add `nanobot_model` and `nanobot_max_iterations` settings. |
| `src/bank_audit/loophole/chat/nanobot_agent.py` | Create | Harness: build Nanobot config, register tools, run/stream, produce `ChatState`. |
| `src/bank_audit/loophole/chat/tools_nanobot.py` | Create | Custom tools for nanobot: web_search, web_fetch, db_query, extract_loopholes, table_load, refine_export. |
| `src/bank_audit/loophole/chat/hooks.py` | Create | `AgentHook` subclass to collect tool results, records, and stream text deltas. |
| `src/bank_audit/loophole/chat/prompt/system_nanobot.md` | Create | System prompt for the loophole agent using nanobot tools. |
| `src/bank_audit/loophole/chat/graph.py` | Modify | Delegate `run_chat`/`stream_chat` to `nanobot_agent`; remove `build_graph`, fallback ReAct, legacy command dispatch. |
| `src/bank_audit/loophole/chat/phases.py` | Delete | Replaced by nanobot agent loop. |
| `src/bank_audit/loophole/chat/nodes.py` | Delete | Manual ReAct parsing no longer needed. |
| `src/bank_audit/loophole/chat/tools.py` | Delete | Replaced by `tools_nanobot.py`. |
| `src/bank_audit/loophole/chat/prompt/02_plan.md` | Delete | Nanobot does its own planning. |
| `src/bank_audit/loophole/chat/prompt/03_react.md` | Delete | ReAct prompt no longer needed. |
| `src/bank_audit/loophole/chat/prompt/05_aggregate.md` | Delete | Aggregation handled by nanobot final answer. |
| `src/bank_audit/loophole/chat/state.py` | Modify | Simplify: remove phase/subtasks/iterations; add `nanobot_run_id`, `tools_used`. |
| `tests/loophole/test_nanobot_agent.py` | Create | Unit tests for harness, tool dispatch, and streaming. |
| `tests/loophole/test_db_query_tool.py` | Create | READ-ONLY SQL guard tests. |
| `tests/loophole/test_graph_adapter.py` | Create | Adapter tests for `run_chat`/`stream_chat`. |
| `tests/loophole/test_chat_phases.py` | Delete | Covers removed phases. |
| `tests/loophole/test_chat_graph.py` | Modify | Remove `build_graph` test; adapt to new adapter behavior. |
| `.cursor/plan_research/MAP.md` | Modify | Update loophole/chat description to nanobot-based architecture. |

---

## Task 1: Add dependency and basic config

**Files:**
- Modify: `pyproject.toml:21-69`
- Modify: `src/bank_audit/loophole/config.py:10-44`

- [ ] **Step 1: Add `nanobot-ai` to dependencies**

Add the line after `langchain-openai>=0.2` in `pyproject.toml`:

```toml
  "nanobot-ai>=0.5",
```

- [ ] **Step 2: Add nanobot settings to `LoopholeSettings`**

In `src/bank_audit/loophole/config.py`, add two fields after `chat_model: str = ""`:

```python
    nanobot_model: str = ""
    nanobot_max_iterations: int = 20
```

And update `load()` to read env vars:

```python
            nanobot_model=os.getenv("LOOPHOLE_NANOBOT_MODEL", ""),
            nanobot_max_iterations=int(os.getenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", "20")),
```

Add method:

```python
    def effective_nanobot_model(self) -> str:
        return (
            self.nanobot_model
            or os.getenv("LLM_MODEL_FAST")
            or os.getenv("LLM_MODEL_NAME", "gpt-4o")
        )
```

- [ ] **Step 3: Install the dependency locally**

Run:

```bash
pip install -e ".[dev]"
```

Expected: installs `nanobot-ai` and all dev dependencies without errors.

- [ ] **Step 4: Verify import**

Run:

```bash
python -c "from nanobot import Nanobot; print(Nanobot)"
```

Expected: prints `<class 'nanobot.Nanobot'>` or similar, no `ModuleNotFoundError`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/bank_audit/loophole/config.py
git commit -m "deps(config): add nanobot-ai dependency and loophole settings"
```

---

## Task 2: Discover nanobot custom tool registration API

**Files:**
- Create: `scripts/explore_nanobot.py` (temporary, delete after)

- [ ] **Step 1: Inspect `nanobot` module for tool registration**

Create `scripts/explore_nanobot.py`:

```python
import asyncio
from nanobot import Nanobot

async def main():
    async with Nanobot.from_config() as bot:
        print("runtime model:", bot.runtime.model)
        print("tools:", getattr(bot.runtime, "tools", None))
        print("tool registry type:", type(getattr(bot, "_tool_registry", None)))

if __name__ == "__main__":
    asyncio.run(main())
```

Run:

```bash
python scripts/explore_nanobot.py
```

Expected: prints available attributes or fails with config error. This tells us whether `Nanobot.from_config()` requires an existing config file or accepts inline config.

- [ ] **Step 2: Try inline config**

Modify `scripts/explore_nanobot.py` to pass an inline config dict or temp file:

```python
import json
import tempfile
from pathlib import Path

config = {
    "providers": {
        "openai": {
            "apiBase": "https://api.openai.com/v1",
            "apiKey": "test-key"
        }
    },
    "agents": {
        "defaults": {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "maxToolIterations": 2
        }
    },
    "tools": {
        "python": False,
        "shell": False
    }
}

with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
    json.dump(config, f)
    config_path = f.name

async def main():
    async with Nanobot.from_config(config_path=config_path) as bot:
        print("model:", bot.runtime.model)

if __name__ == "__main__":
    asyncio.run(main())
```

Run and confirm `Nanobot.from_config` accepts a `config_path` pointing at a JSON file.

- [ ] **Step 3: Try custom tool registration**

Add a dummy function and attempt to register it. Try the most likely API shape first (a `tools` dict with module/function references):

```python
def hello_tool(name: str) -> str:
    return f"hello, {name}"

config = {
    ...
    "tools": {
        "python": False,
        "shell": False,
        "custom": {
            "hello": {
                "module": "__main__",
                "function": "hello_tool"
            }
        }
    }
}
```

Run and inspect whether `hello` appears in `bot.runtime.tools`. Record the actual registration API.

- [ ] **Step 4: Try programmatic tool registration**

If JSON config custom tools fail, try:

```python
from nanobot import Nanobot

bot = Nanobot.from_config(config_path=config_path)
bot.register_tool("hello", hello_tool)  # or bot.tools.register
```

Document the exact API that works. This determines the implementation in Task 3.

- [ ] **Step 5: Delete exploration script**

```bash
git rm scripts/explore_nanobot.py
```

- [ ] **Step 6: Commit**

```bash
git commit -m "chore: explore nanobot custom tool registration API"
```

---

## Task 3: Implement READ-ONLY `db_query` tool

**Files:**
- Create: `src/bank_audit/loophole/chat/tools_nanobot.py`
- Create: `tests/loophole/test_db_query_tool.py`

- [ ] **Step 1: Write failing test for READ-ONLY guard**

Create `tests/loophole/test_db_query_tool.py`:

```python
import pytest
from bank_audit.loophole.chat.tools_nanobot import _is_read_only_select, db_query


def test_is_read_only_select_accepts_simple_select():
    assert _is_read_only_select("SELECT * FROM loophole_record LIMIT 5") is True


def test_is_read_only_select_rejects_insert():
    assert _is_read_only_select("INSERT INTO loophole_record (title) VALUES ('x')") is False


def test_is_read_only_select_rejects_semicolon_injection():
    assert _is_read_only_select("SELECT 1; DROP TABLE loophole_record") is False


def test_is_read_only_select_rejects_comment_dash():
    assert _is_read_only_select("SELECT 1 -- DROP TABLE loophole_record") is False


def test_is_read_only_select_rejects_union_injection():
    assert _is_read_only_select("SELECT title FROM loophole_record UNION DROP TABLE x") is False


def test_db_query_rejects_non_select():
    result = db_query(sql="DROP TABLE loophole_record", session=None)
    assert result["error"] == "only SELECT queries are allowed"
```

Run:

```bash
pytest tests/loophole/test_db_query_tool.py -v
```

Expected: FAIL with `ModuleNotFoundError` or `ImportError` for `tools_nanobot`.

- [ ] **Step 2: Implement `tools_nanobot.py` skeleton and `db_query`**

Create `src/bank_audit/loophole/chat/tools_nanobot.py`:

```python
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import text

from .. import repository as repo
from ..adapters import fetch_decorator, search_decorator
from ..pii_mask import mask as pii_mask

log = logging.getLogger(__name__)

_FORBIDDEN = re.compile(r"\b(DROP|INSERT|UPDATE|DELETE|ALTER|CREATE|TRUNCATE|GRANT|EXEC|UNION)\b", re.IGNORECASE)


def _is_read_only_select(sql: str) -> bool:
    if not sql or not sql.strip().lower().startswith("select"):
        return False
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        return False
    if _FORBIDDEN.search(sql):
        return False
    return True


def db_query(sql: str, *, session: Any = None) -> dict:
    if not _is_read_only_select(sql):
        return {"error": "only SELECT queries are allowed"}

    # Enforce LIMIT 500
    normalized = " ".join(sql.split())
    if "LIMIT" not in normalized.upper():
        sql = f"{sql} LIMIT 500"

    try:
        with repo._session(session) as s:
            rows = s.execute(text(sql)).mappings().all()
            columns = list(rows[0].keys()) if rows else []
            return {
                "columns": columns,
                "rows": [list(row.values()) for row in rows],
                "row_count": len(rows),
            }
    except Exception as e:
        log.warning("[db_query] failed: %s", e)
        return {"error": str(e)}
```

Run tests:

```bash
pytest tests/loophole/test_db_query_tool.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add src/bank_audit/loophole/chat/tools_nanobot.py tests/loophole/test_db_query_tool.py
git commit -m "feat(loophole): READ-ONLY db_query tool for nanobot"
```

---

## Task 4: Implement remaining custom tools

**Files:**
- Modify: `src/bank_audit/loophole/chat/tools_nanobot.py`
- Create: `tests/loophole/test_tools_nanobot.py`

- [ ] **Step 1: Write failing tests for web_search and web_fetch**

Create `tests/loophole/test_tools_nanobot.py`:

```python
from unittest.mock import MagicMock
from bank_audit.loophole.chat.tools_nanobot import web_fetch, web_search


def test_web_search_delegates_to_decorator(monkeypatch):
    mock = MagicMock(return_value=[{"title": "t", "url": "http://x", "snippet": "s", "domain": "x"}])
    monkeypatch.setattr("bank_audit.loophole.chat.tools_nanobot.search_decorator.search", mock)
    result = web_search("test query")
    assert result[0]["url"] == "http://x"
    mock.assert_called_once_with("test query", max_results=8)


def test_web_fetch_delegates_to_decorator(monkeypatch):
    page = MagicMock()
    page.url = "http://x"
    page.final_url = "http://x"
    page.status = 200
    page.title = "title"
    page.excerpt = "excerpt"
    page.via = "fetch"
    mock = MagicMock(return_value=page)
    monkeypatch.setattr("bank_audit.loophole.chat.tools_nanobot.fetch_decorator.fetch_and_parse", mock)
    result = web_fetch("http://x")
    assert result["title"] == "title"
    mock.assert_called_once_with("http://x")
```

Run:

```bash
pytest tests/loophole/test_tools_nanobot.py -v
```

Expected: FAIL — `web_search` and `web_fetch` not defined or not matching expected signature.

- [ ] **Step 2: Implement web_search, web_fetch, and helpers**

Append to `src/bank_audit/loophole/chat/tools_nanobot.py`:

```python
def web_search(query: str, *, max_results: int = 8) -> list[dict]:
    """Custom tool for nanobot: search web via search_decorator."""
    results = search_decorator.search(query, max_results=max_results)
    out = []
    for r in results:
        out.append({
            "title": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("snippet"),
            "domain": r.get("domain"),
        })
    return out


def web_fetch(url: str) -> dict | None:
    """Custom tool for nanobot: fetch and parse a page."""
    page = fetch_decorator.fetch_and_parse(url)
    if page is None:
        return None
    excerpt, _ = pii_mask(page.excerpt or "")
    return {
        "url": page.url,
        "final_url": page.final_url,
        "status": page.status,
        "title": page.title,
        "excerpt": excerpt,
        "via": page.via,
    }
```

Run tests:

```bash
pytest tests/loophole/test_tools_nanobot.py -v
```

Expected: pass.

- [ ] **Step 3: Implement extract_loopholes, table_load, refine_export**

Append to `src/bank_audit/loophole/chat/tools_nanobot.py`:

```python
from pathlib import Path
from ..ai.llm_utils import _loose_json_loads

_PROMPT_DIR = Path(__file__).parent / "prompt"


def _load_prompt(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _default_llm() -> Any:
    from langchain_openai import ChatOpenAI
    import os
    from ..config import LoopholeSettings

    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    model = LoopholeSettings.load().effective_nanobot_model()
    return ChatOpenAI(model=model, base_url=base_url, api_key=api_key, temperature=0.3)


def _llm_content(resp: Any) -> str:
    return getattr(resp, "content", None) or str(resp)


async def extract_loopholes(text: str, *, llm: Any = None) -> list[dict]:
    masked_text, _ = pii_mask(text or "")
    system = _load_prompt("04_extract_loopholes")
    user = f"Текст для анализа:\n{masked_text}\n\nВерни JSON по контракту."
    try:
        if llm is None:
            llm = _default_llm()
        from langchain_core.messages import HumanMessage, SystemMessage

        resp = await llm.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
        raw = _llm_content(resp)
        data = _loose_json_loads(raw)
    except Exception as e:
        log.warning("[extract_loopholes] failed: %s", e)
        return []

    loopholes = data.get("loopholes") if isinstance(data, dict) else data if isinstance(data, list) else []
    out = []
    for item in loopholes:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": str(item.get("title") or ""),
            "description": str(item.get("description") or ""),
            "category": str(item.get("category") or ""),
            "severity": str(item.get("severity") or "medium"),
            "evidence_quote": str(item.get("evidence_quote") or ""),
            "is_loophole": bool(item.get("is_loophole", False)),
        })
    return out


def table_load(
    *,
    bank_slugs: list[str] | None = None,
    period_from: Any = None,
    period_to: Any = None,
    query_text: str | None = None,
    only_loophole: bool = True,
    status: str | None = None,
    limit: int = 200,
    session: Any = None,
) -> list[dict]:
    return repo.list_records(
        bank_slugs=bank_slugs,
        period_from=period_from,
        period_to=period_to,
        query_text=query_text,
        only_loophole=only_loophole,
        status=status,
        limit=limit,
        session=session,
    )


def refine_export(records: list[dict], *, format: str = "json") -> dict:
    return {"format": format, "count": len(records), "records": records}
```

- [ ] **Step 4: Add minimal tests for extract_loopholes and table_load**

Append to `tests/loophole/test_tools_nanobot.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_extract_loopholes_returns_empty_on_llm_error():
    result = await extract_loopholes("some text", llm=MagicMock(side_effect=RuntimeError("boom")))
    assert result == []


def test_table_load_delegates_to_repo(monkeypatch, session):
    mock = MagicMock(return_value=[{"record_id": 1}])
    monkeypatch.setattr("bank_audit.loophole.chat.tools_nanobot.repo.list_records", mock)
    result = table_load(session=session)
    assert result == [{"record_id": 1}]
    mock.assert_called_once()
```

Run:

```bash
pytest tests/loophole/test_tools_nanobot.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/bank_audit/loophole/chat/tools_nanobot.py tests/loophole/test_tools_nanobot.py
git commit -m "feat(loophole): nanobot custom tools (web, extract, table, export)"
```

---

## Task 5: Create nanobot harness

**Files:**
- Create: `src/bank_audit/loophole/chat/nanobot_agent.py`
- Create: `src/bank_audit/loophole/chat/prompt/system_nanobot.md`
- Create: `tests/loophole/test_nanobot_agent.py`

- [ ] **Step 1: Write system prompt**

Create `src/bank_audit/loophole/chat/prompt/system_nanobot.md`:

```markdown
# Системный промпт агента лазеек (nanobot)

Ты — аудитор-аналитик банковских продуктов РФ в системе AuditLens (модуль loophole). Твоя задача — находить лазейки в банковских продуктах (вклады, кредиты, ипотека, карты, РКО, страхование) и анализировать накопленные находки.

## Доступные инструменты

- `audit_web_search(query, max_results=8)` — веб-поиск.
- `audit_web_fetch(url)` — загрузка страницы.
- `audit_db_query(sql)` — READ-ONLY SQL-запрос к таблице `loophole_record`. SQL должен быть SELECT и содержать LIMIT.
- `audit_extract_loopholes(text)` — извлечение лазеек из текста.
- `audit_table_load(...)` — загрузка записей из БД для таблицы UI.
- `audit_export(records, format="json")` — подготовка экспорта.

## Правила

- Отвечай только на русском языке.
- Маскируй персональные данные: ФИО, счета, карты, паспорта, телефоны.
- Не выдумывай лазейки без цитаты-доказательства из источника.
- Для SQL-запросов используй только `SELECT`; диалект Greenplum 6.
- Если данных недостаточно, скажи об этом прямо.
```

- [ ] **Step 2: Write failing test for harness creation**

Create `tests/loophole/test_nanobot_agent.py`:

```python
import pytest
from bank_audit.loophole.chat import nanobot_agent


@pytest.mark.asyncio
async def test_build_bot_returns_nanobot():
    bot = await nanobot_agent.build_bot()
    assert bot is not None
```

Run:

```bash
pytest tests/loophole/test_nanobot_agent.py -v
```

Expected: FAIL — `build_bot` not defined.

- [ ] **Step 3: Implement `build_bot` and config builder**

Create `src/bank_audit/loophole/chat/nanobot_agent.py`:

```python
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from nanobot import Nanobot

from ..config import LoopholeSettings
from . import tools_nanobot

log = logging.getLogger(__name__)


def _build_config(settings: LoopholeSettings | None = None) -> str:
    settings = settings or LoopholeSettings.load()
    config = {
        "providers": {
            "openai": {
                "apiBase": os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
                "apiKey": os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
            }
        },
        "agents": {
            "defaults": {
                "provider": "openai",
                "model": settings.effective_nanobot_model(),
                "temperature": 0.3,
                "maxToolIterations": settings.nanobot_max_iterations,
            }
        },
        "tools": {
            "python": False,
            "shell": False,
        },
    }
    # Add custom tools in the shape discovered in Task 2.
    # Adjust this block if the API differs.
    config["tools"]["custom"] = {
        "audit_web_search": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "web_search",
        },
        "audit_web_fetch": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "web_fetch",
        },
        "audit_db_query": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "db_query",
        },
        "audit_extract_loopholes": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "extract_loopholes",
        },
        "audit_table_load": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "table_load",
        },
        "audit_export": {
            "module": "bank_audit.loophole.chat.tools_nanobot",
            "function": "refine_export",
        },
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f)
    return path


async def build_bot(settings: LoopholeSettings | None = None) -> Nanobot:
    config_path = _build_config(settings)
    try:
        return Nanobot.from_config(config_path=config_path)
    except Exception as e:
        log.warning("[build_bot] failed to load from inline config: %s", e)
        raise
```

Run test:

```bash
pytest tests/loophole/test_nanobot_agent.py -v
```

Expected: pass if `Nanobot.from_config` accepts the generated config. If it fails, adjust config shape based on Task 2 findings.

- [ ] **Step 4: Commit**

```bash
git add src/bank_audit/loophole/chat/nanobot_agent.py src/bank_audit/loophole/chat/prompt/system_nanobot.md tests/loophole/test_nanobot_agent.py
git commit -m "feat(loophole): nanobot harness builder and system prompt"
```

---

## Task 6: Implement run_chat adapter

**Files:**
- Modify: `src/bank_audit/loophole/chat/nanobot_agent.py`
- Modify: `src/bank_audit/loophole/chat/state.py`
- Modify: `src/bank_audit/loophole/chat/graph.py`
- Create: `tests/loophole/test_graph_adapter.py`

- [ ] **Step 1: Simplify `ChatState`**

Modify `src/bank_audit/loophole/chat/state.py` to:

```python
from typing import Any, TypedDict


class ChatState(TypedDict, total=False):
    messages: list[dict]
    query: str
    bank_slugs: list[str]
    workspace_id: int | None
    user_id: str | None
    session: Any | None
    records: list[dict]
    tool_calls: list[dict]
    tool_results: list[dict]
    answer: str
    error: str | None
    nanobot_run_id: str | None
    tools_used: list[str]
```

- [ ] **Step 2: Write failing test for `run_chat` adapter**

Create `tests/loophole/test_graph_adapter.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bank_audit.loophole.chat.graph import run_chat


@pytest.mark.asyncio
async def test_run_chat_returns_answer(session):
    state = {
        "query": "test",
        "workspace_id": 1,
        "user_id": "u1",
        "messages": [],
        "session": session,
    }
    mock_result = MagicMock()
    mock_result.content = "Ответ."
    mock_result.tools_used = []
    mock_result.error = None

    with patch("bank_audit.loophole.chat.nanobot_agent.build_bot", new_callable=AsyncMock) as mock_build:
        mock_bot = MagicMock()
        mock_bot.run = AsyncMock(return_value=mock_result)
        mock_build.return_value.__aenter__ = AsyncMock(return_value=mock_bot)
        mock_build.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await run_chat(state, session=session)
        assert result["answer"] == "Ответ."
        assert result.get("phase") == "done"
```

Run:

```bash
pytest tests/loophole/test_graph_adapter.py -v
```

Expected: FAIL — `run_chat` does not match new signature/behavior.

- [ ] **Step 3: Implement `run_chat` in `nanobot_agent.py`**

Append to `src/bank_audit/loophole/chat/nanobot_agent.py`:

```python
from . import clarify as clarify_mod
from .state import ChatState
from .. import repository as repo


async def run_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session: Any = None,
) -> ChatState:
    query = state.get("query", "")
    history = state.get("messages") or []
    workspace_id = state.get("workspace_id")
    user_id = state.get("user_id")
    sess = session if session is not None else state.get("session")

    # Clarify step
    try:
        clarify_result = await clarify_mod.generate_clarifications(query, history=history)
    except Exception as e:
        log.warning("[run_chat] clarify failed: %s", e)
        clarify_result = {"complete": True, "questions": [], "reason": "error"}

    if not clarify_result.get("complete"):
        return {
            **state,
            "phase": "await_clarify",
            "clarify_questions": clarify_result.get("questions") or [],
        }

    answers = state.get("clarify_answers") or []
    enriched = query
    if answers:
        try:
            enriched = await clarify_mod.build_enriched_question(query, answers)
        except Exception as e:
            log.warning("[run_chat] build_enriched failed: %s", e)

    # Save task
    task_id = None
    if workspace_id:
        try:
            task_id = repo.save_task(
                workspace_id,
                query,
                enriched_query=enriched if enriched != query else None,
                phase="execute",
                session=sess,
            )
        except Exception as e:
            log.warning("[run_chat] save_task failed: %s", e)

    system_prompt = (Path(__file__).parent / "prompt" / "system_nanobot.md").read_text(encoding="utf-8")
    full_prompt = f"{system_prompt}\n\nЗапрос:\n{enriched}"

    async with build_bot() as bot:
        result = await bot.run(
            full_prompt,
            session_key=f"loophole:{workspace_id}:{task_id}" if workspace_id else "loophole:anonymous",
            hooks=[],
        )

    answer = result.content or ""
    if result.error:
        answer = f"Ошибка агента: {result.error}"

    if workspace_id and answer:
        try:
            repo.add_chat_message(workspace_id, "assistant", answer, session=sess)
        except Exception as e:
            log.warning("[run_chat] add_chat_message failed: %s", e)

    if task_id:
        try:
            repo.update_task(task_id, phase="done", status="done", session=sess)
        except Exception as e:
            log.warning("[run_chat] update_task failed: %s", e)

    return {
        **state,
        "query": enriched,
        "answer": answer,
        "phase": "done",
        "nanobot_run_id": result.metadata.get("run_id") if result.metadata else None,
        "tools_used": result.tools_used or [],
    }
```

- [ ] **Step 4: Update `graph.py` to delegate**

Replace contents of `src/bank_audit/loophole/chat/graph.py` with:

```python
"""Adapter from legacy chat API to nanobot harness."""
from __future__ import annotations

from typing import Any, AsyncIterator

from . import nanobot_agent
from .state import ChatState


async def run_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> ChatState:
    return await nanobot_agent.run_chat(state, llm=llm, session=session)


async def stream_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session=None,
) -> AsyncIterator[dict]:
    async for event in nanobot_agent.stream_chat(state, llm=llm, session=session):
        yield event


def repo_add_chat_message(workspace_id: int, role: str, content: str, *, session=None) -> None:
    from .. import repository as _repo
    _repo.add_chat_message(workspace_id, role, content, session=session)
```

- [ ] **Step 5: Run adapter test**

```bash
pytest tests/loophole/test_graph_adapter.py -v
```

Expected: pass (after fixing any mock issues).

- [ ] **Step 6: Commit**

```bash
git add src/bank_audit/loophole/chat/nanobot_agent.py src/bank_audit/loophole/chat/state.py src/bank_audit/loophole/chat/graph.py tests/loophole/test_graph_adapter.py
git commit -m "feat(loophole): run_chat adapter delegating to nanobot"
```

---

## Task 7: Implement streaming adapter

**Files:**
- Modify: `src/bank_audit/loophole/chat/nanobot_agent.py`
- Modify: `src/bank_audit/loophole/chat/hooks.py` (create)
- Modify: `tests/loophole/test_graph_adapter.py`

- [ ] **Step 1: Create SSE hook**

Create `src/bank_audit/loophole/chat/hooks.py`:

```python
from __future__ import annotations

from typing import Any, Callable

from nanobot.agent import AgentHook, AgentHookContext


class SSEHook(AgentHook):
    """Collects nanobot events and forwards them to an SSE callback."""

    def __init__(self, callback: Callable[[dict], None] | None = None) -> None:
        self.callback = callback
        self.records: list[dict] = []
        self.tool_results: list[dict] = []

    def wants_streaming(self) -> bool:
        return True

    async def on_stream(self, context: AgentHookContext, delta: str) -> None:
        if self.callback:
            self.callback({"event": "token", "data": delta})

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tc in context.tool_calls:
            if self.callback:
                self.callback({"event": "tool_call", "data": {"name": tc.name, "args": tc.arguments}})

    async def after_iteration(self, context: AgentHookContext) -> None:
        for tr in context.tool_results or []:
            self.tool_results.append({"name": tr.name, "result": tr.result})
            if self.callback:
                self.callback({"event": "tool_result", "data": {"name": tr.name, "result": tr.result}})

    async def finalize_content(self, context: AgentHookContext, content: str | None) -> str | None:
        # Collect records from extract/table tools if present
        for tr in self.tool_results:
            result = tr.get("result")
            if isinstance(result, list):
                for item in result:
                    if isinstance(item, dict) and item.get("is_loophole"):
                        self.records.append(item)
            elif isinstance(result, dict) and "records" in result:
                for item in result["records"]:
                    if isinstance(item, dict) and item.get("is_loophole"):
                        self.records.append(item)
        return content
```

- [ ] **Step 2: Implement `stream_chat` in `nanobot_agent.py`**

Append:

```python
from nanobot import STREAM_EVENT_TEXT_DELTA, STREAM_EVENT_TOOL_STARTED, STREAM_EVENT_TOOL_COMPLETED, STREAM_EVENT_RUN_COMPLETED


async def stream_chat(
    state: ChatState,
    *,
    llm: Any = None,
    session: Any = None,
) -> AsyncIterator[dict]:
    from .hooks import SSEHook

    query = state.get("query", "")
    workspace_id = state.get("workspace_id")
    sess = session if session is not None else state.get("session")

    system_prompt = (Path(__file__).parent / "prompt" / "system_nanobot.md").read_text(encoding="utf-8")
    full_prompt = f"{system_prompt}\n\nЗапрос:\n{query}"

    sse_hook = SSEHook()

    async with build_bot() as bot:
        final_result = None
        async for event in bot.stream(full_prompt, session_key=f"loophole:{workspace_id}", hooks=[sse_hook]):
            if event.type == STREAM_EVENT_TEXT_DELTA:
                yield {"event": "token", "data": event.delta}
            elif event.type == STREAM_EVENT_TOOL_STARTED:
                yield {"event": "tool_call", "data": {"name": event.name, "args": event.arguments}}
            elif event.type == STREAM_EVENT_TOOL_COMPLETED:
                yield {"event": "tool_result", "data": {"name": event.name, "result": event.result}}
            elif event.type == STREAM_EVENT_RUN_COMPLETED:
                final_result = event.result

    if final_result and final_result.content:
        yield {"event": "answer", "data": final_result.content}
        if workspace_id:
            try:
                repo.add_chat_message(workspace_id, "assistant", final_result.content, session=sess)
            except Exception as e:
                log.warning("[stream_chat] add_chat_message failed: %s", e)

    if sse_hook.records:
        yield {"event": "records", "data": sse_hook.records}
```

- [ ] **Step 3: Add streaming test**

Append to `tests/loophole/test_graph_adapter.py`:

```python
@pytest.mark.asyncio
async def test_stream_chat_emits_token_events():
    state = {"query": "test", "workspace_id": 1}
    mock_event = MagicMock()
    mock_event.type = "text.delta"
    mock_event.delta = "hello"

    with patch("bank_audit.loophole.chat.nanobot_agent.build_bot", new_callable=AsyncMock) as mock_build:
        mock_bot = MagicMock()
        mock_bot.stream = AsyncMock(return_value=iter([mock_event]))
        mock_build.return_value.__aenter__ = AsyncMock(return_value=mock_bot)
        mock_build.return_value.__aexit__ = AsyncMock(return_value=False)
        events = [ev async for ev in run_chat(state)]  # wait, this is wrong: should be stream_chat
        # Use stream_chat directly
        events = [ev async for ev in nanobot_agent.stream_chat(state)]
        assert events[0] == {"event": "token", "data": "hello"}
```

(Adjust import and test to call `stream_chat` directly.)

- [ ] **Step 4: Run streaming tests**

```bash
pytest tests/loophole/test_graph_adapter.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/bank_audit/loophole/chat/hooks.py src/bank_audit/loophole/chat/nanobot_agent.py tests/loophole/test_graph_adapter.py
git commit -m "feat(loophole): SSE streaming adapter for nanobot"
```

---

## Task 8: Cleanup old ReAct files

**Files:**
- Delete: `src/bank_audit/loophole/chat/phases.py`
- Delete: `src/bank_audit/loophole/chat/nodes.py`
- Delete: `src/bank_audit/loophole/chat/tools.py`
- Delete: `src/bank_audit/loophole/chat/prompt/02_plan.md`
- Delete: `src/bank_audit/loophole/chat/prompt/03_react.md`
- Delete: `src/bank_audit/loophole/chat/prompt/05_aggregate.md`
- Delete: `tests/loophole/test_chat_phases.py`
- Modify: `tests/loophole/test_chat_graph.py`

- [ ] **Step 1: Delete old files**

```bash
git rm src/bank_audit/loophole/chat/phases.py
 git rm src/bank_audit/loophole/chat/nodes.py
 git rm src/bank_audit/loophole/chat/tools.py
 git rm src/bank_audit/loophole/chat/prompt/02_plan.md
 git rm src/bank_audit/loophole/chat/prompt/03_react.md
 git rm src/bank_audit/loophole/chat/prompt/05_aggregate.md
 git rm tests/loophole/test_chat_phases.py
```

- [ ] **Step 2: Rewrite `tests/loophole/test_chat_graph.py`**

Replace with minimal tests that verify the new adapter contract:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bank_audit.loophole.chat import graph


@pytest.mark.asyncio
async def test_run_chat_delegates_to_nanobot_agent(session):
    state = {"query": "q", "workspace_id": 1, "messages": [], "session": session}
    expected = {**state, "answer": "answer", "phase": "done"}
    with patch("bank_audit.loophole.chat.graph.nanobot_agent.run_chat", new_callable=AsyncMock) as mock:
        mock.return_value = expected
        result = await graph.run_chat(state, session=session)
        assert result == expected


@pytest.mark.asyncio
async def test_stream_chat_delegates_to_nanobot_agent():
    state = {"query": "q", "workspace_id": 1}
    with patch("bank_audit.loophole.chat.graph.nanobot_agent.stream_chat") as mock:
        mock.return_value = iter([{"event": "token", "data": "x"}])
        events = [ev async for ev in graph.stream_chat(state)]
        assert events == [{"event": "token", "data": "x"}]
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/loophole/test_chat_graph.py -v
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add tests/loophole/test_chat_graph.py
git commit -m "refactor(loophole): remove old ReAct nodes, phases, tools, and legacy tests"
```

---

## Task 9: Fix imports and run full test suite

**Files:**
- Modify: any remaining broken imports in `src/bank_audit/loophole/chat/`

- [ ] **Step 1: Search for references to deleted modules**

```bash
rg "from \. import (phases|nodes|tools)" src/bank_audit/loophole/
rg "from \. import tools" src/bank_audit/loophole/
rg "from \.phases import" src/bank_audit/loophole/
rg "from \.nodes import" src/bank_audit/loophole/
rg "from \.tools import" src/bank_audit/loophole/
```

Expected: no matches. If there are, fix them.

- [ ] **Step 2: Run all loophole tests**

```bash
pytest tests/loophole/ -v
```

Expected: all pass. If failures, fix and re-run.

- [ ] **Step 3: Run linter**

```bash
ruff check src/bank_audit/loophole/chat tests/loophole
```

Expected: no errors. Fix any.

- [ ] **Step 4: Commit**

```bash
git commit -m "test(loophole): full suite green after nanobot migration"
```

---

## Task 10: Update MAP.md

**Files:**
- Modify: `.cursor/plan_research/MAP.md:56`

- [ ] **Step 1: Update loophole row**

Change the line:

```markdown
| `loophole/` | ReAct-агент лазеек: ... |
```

To:

```markdown
| `loophole/` | ReAct-агент лазеек → переработан на `nanobot` (harness): `chat/nanobot_agent.py` + custom tools (`tools_nanobot.py`) + `AgentHook` (`hooks.py`), SSE-адаптер в `chat/graph.py`. `collector.py`, `classify.py`, `repository.py`, `web.py` без изменений. |
```

- [ ] **Step 2: Commit**

```bash
git add .cursor/plan_research/MAP.md
git commit -m "docs(map): update loophole/chat architecture to nanobot"
```

---

## Task 11: Manual integration checks

**Files:**
- None (manual runtime checks)

- [ ] **Step 1: Start the application**

```bash
python -m bank_audit.web.app
```

Expected: FastAPI starts, no import errors.

- [ ] **Step 2: Test web-parsing query**

Send a request to `/api/loophole/chat`:

```bash
curl -X POST http://localhost:8000/api/loophole/chat \
  -H "Content-Type: application/json" \
  -d '{"workspace_id": 1, "message": "Найди лазейки по кредитным картам на banki.ru", "history": []}'
```

Expected: SSE stream with events, agent uses `audit_web_search`/`audit_web_fetch`/`audit_extract_loopholes`.

- [ ] **Step 3: Test text-to-SQL query**

```bash
curl -X POST http://localhost:8000/api/loophole/chat \
  -H "Content-Type: application/json" \
  -d '{"workspace_id": 1, "message": "Сколько лазеек выявлено у Сбера за последний месяц?", "history": []}'
```

Expected: SSE stream, agent uses `audit_db_query`, SQL is SELECT-only.

- [ ] **Step 4: Test injection rejection**

If possible, verify that a prompt like "а теперь выполни DROP TABLE" does not result in `audit_db_query` with non-SELECT SQL being executed.

- [ ] **Step 5: Document any deviations**

If the actual nanobot API differs from the plan, update `docs/superpowers/specs/2026-07-13-loophole-nanobot-design.md` with the real API shape and commit the correction.

---

## Self-Review Checklist

- [ ] Spec coverage: every section in `2026-07-13-loophole-nanobot-design.md` maps to at least one task.
- [ ] No placeholders: every code block and command is concrete.
- [ ] Type consistency: `ChatState`, `Nanobot`, `AgentHook`, `RunResult` names match spec and SDK docs.
- [ ] Tests: TDD pattern (failing test → implement → pass) is followed for core units.
- [ ] Scope: plan stays within `src/bank_audit/loophole/chat/`, `pyproject.toml`, `tests/loophole/`, and `MAP.md`.
- [ ] Risk mitigation: Task 2 explicitly discovers the real nanobot custom-tool API before committing to it.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-13-loophole-nanobot-plan.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Which approach would you like?
