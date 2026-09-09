---
id: SPEC-loophole-agent-stability
companions:
  - investigation.md
  - ../spec-loophole-module-refactor/SPEC.md
sources: []
---

> **Canonical contract.** This SPEC and the files in `companions:` are the complete, preservation-validated contract for what to build, test, and validate. Source documents listed in frontmatter are for traceability — consult them only if you need narrative rationale or prose color this contract intentionally omits.

# Стабилизация агента «Лазейки»

## Why

**Боль оператора.** Агент «Лазейки» в прод-контуре cloud.ru массово теряет соединение с LLM-эндпоинтом и возвращает пустые результаты поиска. Расследование (companion `investigation.md`) показало: фиксы устойчивости из research v2 (`research/llm_throttle.py`) и из журнала грабель AGENTS.md в loophole перенесены лишь частично — ретраи урезаны до одной попытки, при дефолтных env у основной модели нет ни одного таймаута, а каскад fail-closed фильтров (дата публикации, отсутствие subagent-модели, single-shot LLM-вызовы) делает пустой результат почти неизбежным при любой деградации канала. Аналитики не могут пользоваться модулем: вместо отчёта — «Аналитик временно недоступен» или ноль кандидатов.

## Capabilities

- **CAP-1 — Ретраи транзиентных ошибок LLM**
  - **intent:** Все LLM-вызовы loophole-модуля (основная модель, clarify, subagents, extract_loopholes) повторяют транзиентные ошибки (connection error, TLS, 5xx) с бэкоффом вместо одной попытки.
  - **success:** Тест с моком транзиентного connection error завершается успехом после ретраев, а не `model_unavailable`; в логах исчезает `LLM request failed after 2 retries, giving up`.
- **CAP-2 — Конечные таймауты основной модели**
  - **intent:** Основная модель получает connect-timeout (~10 с) и большой конфигурируемый read-таймаут (дефолт порядка 600 с) вместо `timeout=None` при дефолтных env.
  - **success:** Тест с моком зависшего TLS-соединения прерывается по connect-timeout, а не висит бесконечно на «Ожидание ответа модели».
- **CAP-3 — Таймаут ответа внутри порции subagent**
  - **intent:** Зависший ответ классификатора порции прерывается HTTP-таймаутом до истечения 180-секундного дедлайна порции и ретраится.
  - **success:** В логах исчезают серии `loophole_subagent_failed ... code=timeout elapsed_seconds=180.0`; зависший ответ прерывается быстрее дедлайна порции.
- **CAP-4 — Смягчение фильтра даты публикации**
  - **intent:** Источник без точного tz-aware `published_at` не исключается из исследования, а получает оценочную дату из URL/текста страницы.
  - **success:** Запрос с месяцем/годом по форумной фактуре возвращает кандидатов вместо пустого результата; тест показывает участие источника без timestamp в извлечении.
- **CAP-5 — Subagent-ветка из коробки**
  - **intent:** Классификация порций резолвит модель по цепочке деградации и не отключается при типовой конфигурации.
  - **success:** При конфигурации с `LLM_MODEL_FAST` или `LLM_MODEL_NAME` subagents не возвращают `subagent_model_not_configured`.
- **CAP-6 — Ретраи инструментов поиска, загрузки и извлечения**
  - **intent:** Транзиентные сбои `web_search`, `web_fetch`, `extract_loopholes` повторяются до конвертации в fail-closed коды (`search_unavailable`, `source_unavailable`, `extraction_failed`).
  - **success:** Тест с единичным обрывом извлечения показывает повторную попытку, и источник не помечается навсегда непригодным.

## Constraints

- Fail-closed по безопасности не ослабляется: RBAC (`workspace_unauthorized`), PII-маскирование, READ-ONLY SQL остаются. Смягчается только фильтр даты публикации.
- Механизм ретраев повторяет паттерн `src/bank_audit/research/llm_throttle.py`, а не создаёт второй; принцип «числа считает код, LLM формулирует» не нарушается.
- Обратная совместимость env `LOOPHOLE_*`: существующие переменные сохраняют смысл, новые дефолты не ломают прод-конфиг; тесты — без сети и реальной БД по соглашению проекта.

## Non-goals

- Расширение поискового контура (движки SearXNG, прокси, ddgs) — отдельная инфраструктурная задача.
- Ослабление отбора кандидатов (требование дословной цитаты в excerpt) — не меняется.
- Переработка UX/SSE-протокола чата и витрины модуля.

## Success signal

Golden-run исследования на прод-контуре при серии обрывов cloud.ru завершается отчётом с ненулевым числом кандидатов по форумному запросу с месяцем/годом; в логах нет `giving up` после двух попыток и нет серий 180-секундных таймаутов subagent'ов.
