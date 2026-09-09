---
title: 'Стабилизация агента «Лазейки»: ретраи, таймауты, смягчение fail-closed фильтров'
type: 'bugfix'
created: '2026-09-09'
status: 'done'
baseline_revision: '87955825d7da6ce6c8d938636ec35a30da7b410e'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: [oversized]
deferred:
  - summary: >-
      .env.prod.example не содержит секции LOOPHOLE_* — новые переменные
      (LOOPHOLE_SUBAGENT_READ_TIMEOUT_SECONDS, изменённый дефолт LOOPHOLE_MODEL_TIMEOUT_SECONDS)
      не задокументированы для прод-развёртывания.
    evidence: |-
      Предсуществующий пробел: в .env.prod.example исторически нет ни одной LOOPHOLE_* переменной;
      обнаружено ревью при проверке документационного дрейфа.
    location: >-
      .env.prod.example
    severity: low
---

<intent-contract>

## Intent

**Problem:** В прод-контуре cloud.ru агент «Лазейки» теряет соединение с LLM и возвращает пустые результаты: ретраи урезаны до одной попытки, у основной модели при дефолтных env нет ни одного таймаута, а fail-closed фильтры (дата публикации, `subagent_model_not_configured`, single-shot вызовы) делают пустой результат почти неизбежным при любой деградации канала.

**Approach:** Перенести в loophole устойчивость уровня research v2: вернуть ретраи транзиентных ошибок (по классификатору `research/llm_throttle.py`) всем LLM-вызовам и инструментам, дать основной модели connect-timeout ~10 с и большой конфигурируемый read-таймаут (дефолт ~600 с), ввести HTTP-таймаут ответа внутри порции subagent, смягчить фильтр даты публикации оценочной датой из URL/текста и расширить цепочку резолва subagent-модели до `LLM_MODEL_NAME`.

## Boundaries & Constraints

**Always:**
- Классификация транзиентных ошибок переиспользует `src/bank_audit/research/llm_throttle.py` (`_is_transient_error`, `_is_rate_limit_error`, `_extract_retry_after`) — второго классификатора не создавать.
- Существующие env `LOOPHOLE_*` сохраняют смысл: `LOOPHOLE_MODEL_TIMEOUT_SECONDS=0` по-прежнему отключает read-таймаут основной модели; меняется только дефолт при незаданной переменной (0 → ~600 с) и потолок валидации (300 → ≥600).
- Fail-closed по безопасности не трогаем: RBAC (`workspace_unauthorized`), PII-маскирование (`pii_mask`), READ-ONLY SQL, дословная цитата в excerpt, отклонение источника с подтверждённой датой вне окна (`source_outside_publication_period`).
- Тесты без сети и реальной БД (in-memory SQLite, моки на границе модуля), в стиле `tests/loophole/`.
- Транзиентный сбой не записывается в `budget.source_failures`/`fetch_cache`/`analysis_status` как окончательный — после успешного ретрая источник продолжает участвовать в исследовании.

**Block If:**
- Для устойчивости потребовалось бы ослабить отбор кандидатов (дословная цитата) или добавить поисковые движки/прокси — это non-goals исходной спеки.

**Never:**
- Не менять поисковый контур (SearXNG-движки, ddgs, прокси) и UX/SSE-протокол.
- Не создавать новых LLM-клиентов и обёрток вне существующих точек (`nanobot_agent.py`, `clarify.py`, `tools_nanobot.py`, `subagents.py`, `agent/__init__.py`).
- Не хардкодить секреты; сообщения ошибок пользователю остаются безопасными (без деталей провайдера).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| TRANSIENT_LLM_RETRY | Основная модель: connection error при первом вызове | Вызов повторяется с бэкоффом (SDK `_CHAT_RETRY_DELAYS` не урезан до `(1,)`), исследование завершается ответом | После исчерпания попыток — прежний `model_unavailable` |
| FROZEN_TLS_CONNECT | Основная модель: TLS handshake завис | Прерывание по connect-timeout ~10 с, затем ретрай/ошибка — не вечное «Ожидание ответа модели» | Безопасный код, детали провайдера не утекают |
| SUBAGENT_STALLED_BATCH | Subagent: ответ классификатора порции завис | HTTP read-таймаут (< дедлайна порции 180 с) прерывает ожидание, порция ретраится | Исчерпание — прежний код `timeout` порции |
| SOURCE_NO_DATE | Запрос с месяцем/годом; источник без tz-aware `published_at` | Дата оценивается из URL, затем из текста страницы; источник участвует в извлечении | Оценка вне окна → отклонение; без любой даты — допуск с пометкой неподтверждённой даты |
| SUBAGENT_DEFAULT_CONFIG | Задан только `LLM_MODEL_NAME` | Subagent-ветка работает, `subagent_model_not_configured` не возникает | Пустая вся цепочка — прежняя ошибка |
| TRANSIENT_TOOL_FAILURE | `web_fetch`/`extract_loopholes`: единичный обрыв | Повторная попытка до конвертации в `source_unavailable`/`extraction_failed`; источник не помечается непригодным | Нетранзиент — fail-closed без ретрая, как раньше |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/agent/__init__.py:252` — `provider._CHAT_RETRY_DELAYS = (1,)` урезает ретраи SDK (дефолт `(1, 2, 4)`); `:246-277` `_bind_model_deadlines`; `:566-570` watchdog `expire`; `:701-743` `AgentFactory.create` (`disable_model_timeouts=not budget.model_timeout_seconds`, connect-timeout не передаётся); `:131,316-318` period-фильтр в отборе/авто-извлечении.
- `src/bank_audit/loophole/chat/nanobot_agent.py:107-155` — `_configure_direct_provider`/`build_direct_client`: `timeout=None` при `disable_model_timeouts`, `httpx.Timeout(connect=...)` только при переданном connect-timeout, `max_retries=0`; `:176-188` `create_nanobot(disable_model_timeouts, connect_timeout_seconds)`.
- `src/bank_audit/loophole/chat/clarify.py:304-321,417-427` — клиент `timeout=15, max_retries=0`; один сбой → `_clarification_unavailable()`.
- `src/bank_audit/loophole/chat/tools_nanobot.py:30,275-331` — extract: `_EXTRACTION_TIMEOUT_SECONDS=45`, клиент `max_retries=0`, любой Exception → `RuntimeError("extraction_failed")`; `:744-770` `AuditWebSearchTool` (→ `search_unavailable`); `:800-831` `AuditWebFetchTool` (ошибка кэшируется в `source_failures`/`fetch_cache`); `:864-910` `AuditExtractLoopholesTool` (`analysis_status="extraction_failed"` навсегда); `:139-190` `_publication_window`/`_source_publication_period_error`; `:193-211` `_remember_source_publication_date`.
- `src/bank_audit/loophole/chat/subagents.py:176-191` — лимиты и резолв модели (`LOOPHOLE_SUBAGENT_MODEL` → `LLM_MODEL_FAST` → `subagent_model_not_configured`); `:243-259` `_classify_isolated` (`disable_model_timeouts=True, connect_timeout_seconds=10`, `_CHAT_RETRY_DELAYS=(1,)`); `:336-366` порции по 2 источника под `asyncio.timeout(min(180, remaining))`, HTTP read-таймаута нет.
- `src/bank_audit/loophole/adapters/fetch_decorator.py:54-69,106-126,194` — `_PUBLISHED_AT_RES` (только JSON-LD/meta/time), `_exact_published_at` (только tz-aware ISO); дат из URL/текста в проекте нет — новая функция здесь.
- `src/bank_audit/loophole/run_budget.py:36-53,90` — `ResearchBudget`, env-лимиты; `_limit()` валидирует `LOOPHOLE_MODEL_TIMEOUT_SECONDS` (0–300).
- `src/bank_audit/loophole/config.py:92-104` — образец цепочки деградации (`effective_chat_model`/`effective_nanobot_model`: `LOOPHOLE_*` → `LLM_MODEL_FAST` → `LLM_MODEL_NAME`).
- `src/bank_audit/research/llm_throttle.py:91-142,167-224` — образец: `_is_transient_error`, `_is_rate_limit_error`, `_extract_retry_after`, `call_with_throttle` (бэкофф, капы).
- `.env.example:33-42,79` — LOOPHOLE_* дефолты; комментарий про `LOOPHOLE_MODEL_TIMEOUT_SECONDS=0` обновить.
- `tests/loophole/test_research_completion.py:107-153,227-237` — тесты, утверждающие старое поведение (`len(calls)==1`, «максимум один retry») — обновить под ретраи; паттерны моков — оттуда же.
- `src/bank_audit/loophole/web.py:711-832` — `POST /api/loophole/chat` (SSE) — поверхность для e2e-проверки через веб.

## Tasks & Acceptance

**Execution:**
- `src/bank_audit/loophole/agent/__init__.py` — вернуть ретраи SDK основной модели (не урезать `_CHAT_RETRY_DELAYS` ниже `(1, 2, 4)`), передать `connect_timeout_seconds≈10` в `create_nanobot`, read-таймаут из budget с новым дефолтом ~600 с — CAP-1, CAP-2.
- `src/bank_audit/loophole/run_budget.py` + `.env.example` — дефолт `LOOPHOLE_MODEL_TIMEOUT_SECONDS` 600 (0 по-прежнему отключает), потолок ≥600; обновить комментарии — CAP-2.
- `src/bank_audit/loophole/chat/nanobot_agent.py` — connect-timeout применять независимо от read-таймаута (в т.ч. при `disable_model_timeouts`) — CAP-2.
- `src/bank_audit/loophole/chat/subagents.py` — цепочка резолва модели + `LLM_MODEL_NAME` (по образцу `config.py:99-104`); не урезать `_CHAT_RETRY_DELAYS`; конфигурируемый HTTP read-таймаут ответа классификатора < дедлайна порции — CAP-3, CAP-5.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — ретраи транзиентов (классификатор из `research/llm_throttle.py`) в `AuditWebSearchTool`/`AuditWebFetchTool`/`AuditExtractLoopholesTool` до fail-closed кодов; транзиент не кэшируется как окончательный; иерархия даты публикации: exact tz-aware → оценочная (URL → текст) → none (допуск с пометкой) — CAP-4, CAP-6.
- `src/bank_audit/loophole/adapters/fetch_decorator.py` — функция оценочной даты из URL/текста страницы, подключение к `published_at` — CAP-4.
- `src/bank_audit/loophole/chat/clarify.py` — ретраи транзиентов clarify-вызова (классификатор из `llm_throttle`) прежде `_clarification_unavailable()` — CAP-1.
- `tests/loophole/` — новые тесты по I/O-матрице (моки в стиле `test_research_completion.py`) + обновление тестов, утверждавших «одну попытку» — покрытие CAP-1…CAP-6.
- Веб-проверка — запуск `auditlens serve` и прогон запроса через `POST /api/loophole/chat` (SSE): стрим отвечает, при инжекции обрыва виден ретрай в логах — сквозная проверка.

**Acceptance Criteria:**
- Given мок транзиентного connection error у основной модели, when исследование запущено, then ответ получен после ретраев, а не `model_unavailable`, и в логах нет `LLM request failed after 2 retries, giving up`.
- Given мок зависшего TLS handshake, when основная модель вызывается с дефолтными env, then вызов прерван по connect-timeout ~10 с.
- Given мок зависшего ответа классификатора, when subagent классифицирует порцию, then ожидание прервано HTTP-таймаутом раньше 180-с дедлайна и порция ретраинута.
- Given источник без tz-aware `published_at` с датой в URL, when запрос содержит месяц/год, then источник участвует в извлечении (оценочная дата), а не отклонён `source_publication_date_unverified`.
- Given конфигурация только с `LLM_MODEL_NAME`, when запускается subagent-ветка, then `subagent_model_not_configured` не возвращается.
- Given единичный транзиентный обрыв `web_fetch`/`extract_loopholes`, when инструмент вызван, then выполнена повторная попытка, и источник не помечен окончательно непригодным.
- Given источник с подтверждённой датой вне окна, when применяется period-фильтр, then он по-прежнему отклоняется `source_outside_publication_period`.

## Spec Change Log

## Review Triage Log

### 2026-09-09 — Review pass
- intent_gap: 0
- bad_spec: 0
- patch: 19: (high 0, medium 12, low 7)
- defer: 1: (high 0, medium 0, low 1)
- reject: 3: (high 0, medium 1, low 2)
- addressed_findings:
  - `[medium]` `[patch]` Системный промпт 07_nanobot_system.md противоречил мягкому фильтру даты — секция переписана под иерархию exact/оценочная/без даты
  - `[medium]` `[patch]` Оценочная дата была невидима в `_candidate_report` и состоянии модели — `estimated_published_at` проброшен в оба отчётных пути
  - `[medium]` `[patch]` `_date_from_text` брал первую дату без привязки и без защиты от будущего — добавлены маркеры публикации и отсев дат позже сегодня
  - `[medium]` `[patch]` Стебель `ма\w*` матчил «машин»/«магазинов» — ограничен известными окончаниями
  - `[medium]` `[patch]` `retry-after` honoured без капа в трёх retry-циклах — кап 5 с + перепроверка бюджета после сна
  - `[medium]` `[patch]` Нет валидации read < дедлайна порции у subagent — clamp с warning
  - `[medium]` `[patch]` Date-only ISO published_at терялся в слабый путь — принимается как подтверждённая дата
  - `[medium]` `[patch]` Истёкший бюджет маскировался в search_unavailable/source_unavailable — проброс остановки бюджета
  - `[medium]` `[patch]` Документационный дрейф AGENTS.md и .env.example по таймаутам — обновлены
  - `[medium]` `[patch]` Нет теста `fetch_and_parse` → `estimated_published_at` — добавлено два теста (URL, текст с маркером)
  - `[medium]` `[patch]` Нет теста дефолта `LOOPHOLE_MODEL_TIMEOUT_SECONDS=600` при unset env — добавлен delenv-кейс
  - `[medium]` `[patch]` Нет теста ретрая классификатора subagent — добавлен тест с двумя вызовами провайдера
  - `[low]` `[patch]` Докстринг `_call_with_transient_retries` уточнён (кэширование после исчерпания ретраев)
  - `[low]` `[patch]` Дублирование словаря русских месяцев — единый источник в fetch_decorator.py
  - `[low]` `[patch]` Приватные импорты из llm_throttle — добавлены публичные алиасы
  - `[low]` `[patch]` Семантика флага `disable_model_timeouts` задокументирована
  - `[low]` `[patch]` Описание `web_fetch` дополнено `estimated_published_at`
  - `[low]` `[patch]` Удалён no-op `_ensure_tool_active(None)` в цикле extract_loopholes
  - `[low]` `[patch]` None-safe доступ к `provider._CHAT_RETRY_DELAYS`

## Design Notes

Ретраи основной модели и subagent'ов — это восстановление штатного механизма nanobot SDK (`_run_with_retry`, `_is_transient_response`), обрезанного строкой `_CHAT_RETRY_DELAYS = (1,)`; для прямых клиентов (clarify, extract, инструменты) цикл повтора строится на импортированном классификаторе из `research/llm_throttle.py`, а не на новом. Грабля AGENTS.md «0 отключает таймеры» сохранена: явный `LOOPHOLE_MODEL_TIMEOUT_SECONDS=0` по-прежнему даёт `timeout=None`; меняется только дефолт и добавляется connect-timeout, который от read-таймаута не зависит. Оценочная дата: сначала regex по URL (`/2026/09/`, `2026-09-09`), затем по тексту страницы (русские месяцы, ISO-даты); точная дата вне окна по-прежнему отклоняет источник.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole -x -q` — expected: все тесты зелёные, включая новые по I/O-матрице
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole` — expected: нет новых ошибок в затронутых файлах
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_research_completion.py tests/loophole/test_research_subagents.py -q` — expected: обновлённые тесты ретраев зелёные

**Manual checks (if no CLI):**
- E2E через веб: поднять `auditlens serve` локально, отправить запрос в `POST /api/loophole/chat`, убедиться, что SSE-стрим идёт и в логах видны ретраи при инжектированном обрыве, а не мгновенный `model_unavailable`.

## Auto Run Result

Status: done

### Резюме изменения

Модуль «Лазейки» стабилизирован по спеке `docs/loophole/bmad/specs/spec-loophole-agent-stability/SPEC.md` (CAP-1…CAP-6): восстановлены ретраи транзиентных ошибок LLM (классификатор переиспользован из `research/llm_throttle.py`, второго не создано), основная модель получила connect-timeout 10 с и read-таймаут с дефолтом 600 с (явный `LOOPHOLE_MODEL_TIMEOUT_SECONDS=0` по-прежнему отключает), у subagent-классификатора появился HTTP read-таймаут 60 с (< дедлайна порции 180 с) с валидацией, фильтр даты публикации смягчён иерархией «точная → оценочная из URL/текста → допуск с пометкой», цепочка резолва subagent-модели расширена до `LLM_MODEL_NAME`, инструменты web_search/web_fetch/extract_loopholes ретраят транзиенты до fail-closed кодов и не помечают источник непригодным после единичного обрыва.

### Изменённые файлы

- `src/bank_audit/loophole/agent/__init__.py` — ретраи SDK не урезаются (восстановление до (1,2,4), None-safe), фабрика передаёт connect/read-таймауты, оценочная дата в отчёте кандидатов и состоянии модели
- `src/bank_audit/loophole/run_budget.py` — дефолт `LOOPHOLE_MODEL_TIMEOUT_SECONDS` 0→600, потолок 300→3600
- `src/bank_audit/loophole/chat/nanobot_agent.py` — параметр `read_timeout_seconds`, connect-timeout независимо от read, документация семантики `disable_model_timeouts`
- `src/bank_audit/loophole/chat/subagents.py` — цепочка резолва модели +`LLM_MODEL_NAME`, SDK-ретраи сохранены, `LOOPHOLE_SUBAGENT_READ_TIMEOUT_SECONDS` (дефолт 60) с clamp-валидацией
- `src/bank_audit/loophole/chat/clarify.py` — ретраи транзиентов с капом retry-after перед fail-closed
- `src/bank_audit/loophole/chat/tools_nanobot.py` — `_call_with_transient_retries` для search/fetch, ретраи в extract_loopholes, иерархия дат (date-only ISO = подтверждённая), проброс остановки бюджета, `estimated_published_at` в отчётах и описании инструмента
- `src/bank_audit/loophole/adapters/fetch_decorator.py` — `estimate_published_date(url, text)`: URL → текст с маркерами публикации, отсев будущих дат, единый словарь русских месяцев
- `src/bank_audit/loophole/chat/prompt/07_nanobot_system.md` — секция фильтра даты переписана под новую иерархию
- `src/bank_audit/research/llm_throttle.py` — публичные алиасы классификатора транзиентов
- `.env.example`, `AGENTS.md` — документация новых дефолтов и диапазонов
- `tests/loophole/test_agent_stability.py` (новый, 21 тест) + обновления test_fetch_decorator, test_research_completion, test_research_subagents, test_tools_nanobot, test_tools_network_io, test_agent_latency_budget, test_subagent_failure_causes

### Итоги ревью

- patch: 19 (high 0, medium 12, low 7) — все исправлены и перепроверены
- defer: 1 (low) — отсутствие секции LOOPHOLE_* в .env.prod.example (предсуществующее)
- reject: 3 — тривиальные retry-циклы поверх общего классификатора допустимы (Design Notes); предложение вернуть `_CHAT_RETRY_DELAYS=(1,)` противоречит спеке; константы задержек
- Follow-up review: patched medium 12, low 7 → score 3×12+7=43 ≥ 5 → `followup_review_recommended: true`

### Проверка

- `.venv/Scripts/python.exe -m pytest tests/loophole -q` — 1028 passed, 3 skipped (два полных прогона: после реализации и после patch-фиксов)
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole` — All checks passed
- E2E через веб: uvicorn на :8017, создан workspace, `POST /api/loophole/chat` — SSE-стрим прошёл полный цикл (clarify → execute → subagents searching/classifying/completed по 8 источникам) на живом LLM и поиске; стенд удалён после проверки

### Остаточные риски

- Инжекция обрыва в живой сервер не выполнялась (нельзя внедрить сбой в запущенный процесс); пути ретраев покрыты юнит-тестами, включая логи SDK-ретраев
- При деградированном эндпоинте один вызов модели может занимать до `min(LOOPHOLE_MODEL_TIMEOUT_SECONDS, бюджет)` — осознанное поведение спеки
- Golden-run на прод-контуре (success signal спеки) из рабочей копии не проверялся — требует развёртывания
