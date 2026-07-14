# Спецификация: переработка loophole/chat/ на nanobot (harness)

> Дата: 2026-07-13
> Область: `src/bank_audit/loophole/chat/`
> Статус: design (pending implementation plan)

## 1. Контекст и цель

Модуль `loophole` содержит ReAct-агент для поиска банковских лазеек. Текущая реализация (`chat/phases.py`, `chat/graph.py`, `chat/nodes.py`) построена вручную на langgraph + собственном ReAct-парсинге. Цель — заменить ручной цикл на `nanobot` (Python SDK, `nanobot-ai`) как harness, сохранив банковскую специфику (trust scoring, маскировка ПД, дедупликация, диалект Greenplum 6).

### Функции, которые должны получиться

1. **Парсинг сайтов по запросу пользователя** — агент получает запрос, сам решает, что искать, парсит страницы, извлекает лазейки, сохраняет в `loophole_record`.
2. **Аналитика по базе данных лазеек** — text-to-SQL + анализ результата через агента. SQL только READ-ONLY (`SELECT`).

## 2. Границы изменений

### Разрешено изменять

- Всё внутри `src/bank_audit/loophole/chat/`
- Промпты в `src/bank_audit/loophole/chat/prompt/`
- `src/bank_audit/loophole/config.py` (добавить настройки nanobot)

### Запрещено изменять

- `src/bank_audit/loophole/repository.py`
- `src/bank_audit/loophole/web.py` (API-контракт `/api/loophole/chat` SSE)
- `src/bank_audit/loophole/models.py`
- `src/bank_audit/loophole/collector.py`, `classify.py`, `workspace.py`, `pdf_export.py`
- `src/bank_audit/loophole/adapters/`
- `migrations/`
- `pyproject.toml` — без явного согласования с пользователем (добавляется `nanobot-ai`)

## 3. Архитектура (Подход A)

```text
web.py /chat
    │
    ▼
chat/graph.py  ──run_chat() / stream_chat()──► chat/nanobot_agent.py
                                                  │
                                                  ▼
                                          async with Nanobot.from_config(...)
                                                  │
                                                  ▼
                                          custom tools (Python-функции)
                                                  │
            ┌──────────────┬──────────────────┼──────────────────┐
            ▼              ▼                  ▼                  ▼
      web_search       web_fetch         db_query           extract_loopholes
            │              │                  │                  │
            ▼              ▼                  ▼                  ▼
   search_decorator  fetch_decorator   READ-ONLY SQL      LLM (04_extract_*.md)
```

### 3.1 Новые/изменённые файлы

| Файл | Роль |
|------|------|
| `chat/nanobot_agent.py` | Главный harness. Создаёт `Nanobot`, регистрирует кастомные tools, запускает run/stream, обрабатывает `RunResult` / `StreamEvent`. |
| `chat/tools_nanobot.py` | Набор кастомных Python-функций для nanobot: `web_search`, `web_fetch`, `db_query`, `extract_loopholes`, `table_load`, `export`. |
| `chat/tools.py` | Удаляется. Старый `TOOL_REGISTRY` и `/команды` больше не нужны — nanobot сам выбирает инструменты. |
| `chat/graph.py` | Упрощается: `run_chat()` и `stream_chat()` делегируют в `nanobot_agent`. Удаляется `build_graph()` и fallback ReAct. |
| `chat/phases.py` | Удаляется или превращается в no-op-заглушку. Вся фазовая логика переносится в nanobot. |
| `chat/nodes.py` | Удаляется. ReAct-парсинг больше не нужен. |
| `chat/state.py` | Минимальная очистка: удалить `phase`, `subtasks`, `subtask_results`, `iterations`; добавить `nanobot_run_id` (str), `tools_used` (list[str]). |
| `chat/prompt/01_clarify.md` | Оставляется как есть — clarify.py работает отдельно. |
| `chat/prompt/02_plan.md` | Можно удалить (планирование делает nanobot). |
| `chat/prompt/03_react.md` | Удалить — ReAct-промпт больше не нужен. |
| `chat/prompt/04_extract_loopholes.md` | Оставить; используется `extract_loopholes`. |
| `chat/prompt/05_aggregate.md` | Удалить; агрегация делает nanobot/finalize. |
| `chat/prompt/06_keywords.md` | Оставить; возможно, использовать для генерации ключевых слов. |
| `chat/prompt/system_nanobot.md` | Новый системный промпт для nanobot: роль, правила, доступные tools, ограничения (только русский, маскировка ПД, READ-ONLY SQL). |
| `chat/clarify.py` | Оставить без изменений. |

### 3.2 Конфигурация nanobot

Использовать inline-конфигурацию, а не `~/.nanobot/config.json`. Custom tools регистрируются программно через `bot._loop.tools.register()` после создания `Nanobot` (см. `chat/nanobot_agent.py`).

Пример минимального config JSON:

```json
{
  "providers": {
    "openai": {
      "apiBase": "${LLM_BASE_URL}",
      "apiKey": "${LLM_API_KEY}"
    }
  },
  "agents": {
    "defaults": {
      "provider": "openai",
      "model": "${LOOPHOLE_CHAT_MODEL}",
      "temperature": 0.3,
      "maxToolIterations": 20
    }
  },
  "tools": {
    "web": {"enable": false},
    "exec": {"enable": false},
    "file": {"enable": false},
    "cliApps": {"enable": false},
    "my": {"enable": false},
    "imageGeneration": {"enable": false}
  }
}
```

Custom tools — это подклассы `nanobot.agent.tools.base.Tool` с декоратором `@tool_parameters({...})`, реализующие `name`, `description`, `execute`.

## 4. Кастомные tools

### 4.1 `web_search(query: str, max_results: int = 8) -> list[dict]`

- Делегирует `adapters.search_decorator.search(...)`.
- Возвращает список `{title, url, snippet, domain}`.

### 4.2 `web_fetch(url: str) -> dict`

- Делегирует `adapters.fetch_decorator.fetch_and_parse(...)`.
- Возвращает `{url, final_url, title, excerpt, status, via}`.
- Маскировка ПД перед передачей агенту — обязательна.

### 4.3 `db_query(sql: str) -> dict`

**READ-ONLY.**

- Проверка: `sql` должен начинаться с `SELECT` (case-insensitive), не содержать `;`, `--`, `/*`, `DROP`, `INSERT`, `UPDATE`, `DELETE`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `EXEC`.
- Выполнение через `sqlalchemy.text()` с `session` из `chat.state` (thread-local / FastAPI Depends).
- Лимит строк: `LIMIT 500` или принудительно добавлять `LIMIT 500`.
- Возвращает `{columns: list[str], rows: list[list[Any]], row_count: int}`.
- Всегда `SELECT *` запрещено — рекомендовать агенту указывать столбцы.
- Ошибки SQL возвращаются как `{"error": str(e)}` без stack trace.

### 4.4 `extract_loopholes(text: str) -> list[dict]`

- Перед LLM маскирует текст через `pii_mask.mask()`.
- Использует промпт `chat/prompt/04_extract_loopholes.md` и `tools.py:_default_llm()`.
- Возвращает список лазеек с `is_loophole`, `title`, `description`, `category`, `severity`, `evidence_quote`.

### 4.5 `table_load(...)` и `refine_export(...)`

- Оставить как сейчас (delegates в `repository.py`).
- `table_load` — сохраняет записи в `loophole_record` (INSERT) — это не text-to-SQL, а рабочий tool агента.

## 5. Поток выполнения

### 5.1 `run_chat(state, *, llm=None, session=None)`

1. Сохранить `workspace_id`, `user_id`, `session`, `query`, `messages`.
2. Выполнить внешнюю clarify-воронку (`clarify.py`). Если запрос неполный — вернуть `phase="await_clarify"` и список вопросов (сохранить совместимость с `web.py`).
3. Если clarify вернула `complete=True`, сформировать обогащённый запрос (`build_enriched_question`) и передать его nanobot.
4. Создать `nanobot` через `Nanobot.from_config(..., model=effective_chat_model())`.
5. Подготовить `session_key = f"loophole:{workspace_id}:{task_id}"`.
6. Запустить `await bot.run(system_prompt + enriched_query, session_key=session_key, hooks=[AuditHook()])`.
7. Из `RunResult`:
   - `answer = result.content`
   - `tools_used = result.tools_used`
   - Собрать `records` из `table_load`/`extract_loopholes` через hook.
8. Сохранить ответ в `repository.add_chat_message`.
9. Вернуть `ChatState` с `answer`, `records`, `phase="done"` (для совместимости с `web.py`).

> `llm`-параметр в `run_chat` сохраняется для совместимости сигнатуры, но в nanobot-версии используется только в тестах (мок) или как fallback, если nanobot не сконфигурирован.

### 5.2 `stream_chat(state, ...)`

- Использовать `bot.stream(...)`.
- Через `AgentHook` транслировать события nanobot в SSE-события:
  - `text.delta` → `{"event": "token", "data": ...}`
  - `tool.started` / `tool.completed` → `{"event": "tool_call"/"tool_result", ...}`
  - `run.completed` → `{"event": "answer", ...}`
- Сохранить совместимость с frontend (`loophole.jsx`).

### 5.3 `AgentHook` (`chat/hooks.py`)

- Собирает `tool_calls`, `tool_results`, `records`.
- Передаёт text delta в callback для SSE.
- Маскирует ПД в `finalize_content`.

## 6. Интеграция с web.py

`/api/loophole/chat` должен продолжать возвращать `EventSourceResponse` с тем же набором событий. Внутри `chat/graph.py` — только адаптер. Никаких изменений в `web.py` не требуется.

## 7. Риски и неопределённости

1. **Формат custom tools в nanobot.** Необходимо проверить в runtime, как именно регистрировать Python-функции как tools. Возможно, потребуется враппер `@tool` или JSON-описание функций.
2. **Session management.** `nanobot` использует `session_key` для истории. Нужно гарантировать, что SQLAlchemy-сессия FastAPI не конфликтует с `session` nanobot.
3. **Streaming mapping.** Не все события nanobot 1:1 мапятся на текущие SSE. Нужно протестировать `loophole.jsx`.
4. **READ-ONLY SQL.** Парсинг SQL — эвристический. Нужно убедиться, что невозможно обойти проверку (`UNION SELECT ... ; DROP ...` и т.п.).
5. **Зависимость `nanobot-ai`.** Добавление в `pyproject.toml` требует согласования с пользователем. Альтернатива — установить вручную, но тогда CI/докер не будут работать.

## 8. Критерии успеха

- `pytest tests/loophole/` проходит (тесты адаптированы или новые написаны).
- `/api/loophole/chat` возвращает SSE-события и финальный ответ.
- Агент может выполнить: «Найди лазейки по кредитным картам на banki.ru» → использует `web_search` + `web_fetch` + `extract_loopholes`.
- Агент может выполнить: «Сколько лазеек выявлено у Сбера за последний месяц?» → генерирует SELECT, выполняет `db_query`, анализирует результат.
- SQL-запросы отклоняются, если не READ-ONLY.
- Линтер (`ruff`) без ошибок на изменённых файлах.

## 9. Тестовый план

1. **Модульные тесты:**
   - `tests/loophole/test_nanobot_agent.py` — мок `Nanobot` и `AgentHook`.
   - `tests/loophole/test_db_query_tool.py` — проверка READ-ONLY, инъекции, лимит строк.
   - `tests/loophole/test_graph_adapter.py` — `run_chat`/`stream_chat` возвращают ожидаемую структуру.
2. **Интеграционные тесты (ручные):**
   - Запуск `/api/loophole/chat` с запросом web-парсинга.
   - Запуск `/api/loophole/chat` с запросом аналитики БД.
3. **Регрессионные тесты / удаляемые:**
   - `tests/loophole/test_chat_phases.py` — удалить; логика фаз перенесена в nanobot.
   - `tests/loophole/test_chat_graph.py` — переписать под `nanobot_agent` / `graph.py` адаптер; удалить `test_build_graph_returns_compiled`.
   - `tests/loophole/test_chat_clarify.py` — оставить, если не зависит от `phases.py`.

## 10. Следующий шаг

После утверждения спецификации — invoke `writing-plans` skill для создания пошагового implementation plan.
