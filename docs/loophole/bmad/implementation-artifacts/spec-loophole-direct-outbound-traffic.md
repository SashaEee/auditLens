---
title: 'Прямые исходящие запросы модуля Лазейки'
type: 'bugfix'
created: '2026-08-31'
status: 'in-review'
review_loop_iteration: 0
baseline_commit: 'b640242d5b3a12480bca2d7323c41789b9f71542'
context: ['docs/project-context.md']
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Модуль «Лазейки» наследует `HTTP_PROXY`, `HTTPS_PROXY` и `ALL_PROXY` от процесса AuditLens. В локальном запуске это `http://127.0.0.1:9`, где прокси не слушает, поэтому чат nanobot получает `Connection error` вместо ответа аналитика.

**Approach:** Все исходящие запросы, которыми владеет модуль «Лазейки», должны выполняться напрямую, без proxy-настроек окружения или системы. Изменение изолируется в модуле и не меняет поведение остальных модулей AuditLens.

## Boundaries & Constraints

**Always:** Сохранять явную проверку CA-бандла, маскирование ПД и fail-closed обработку ошибок; покрыть nanobot регрессией с заданными proxy-env; после каждого изменения запускать целевой тест и линтер; перезапустить локальный сервис и проверить реальный сценарий чата.

**Ask First:** Если для прямого соединения понадобится изменять глобальные proxy-переменные процесса, конфигурацию ОС, Docker, инфраструктуру, сетевую политику или поведение другого модуля AuditLens.

**Never:** Не использовать временное глобальное удаление env-переменных во время запроса; не передавать секреты в тесты/логи; не включать произвольный proxy в сгенерированном парсере; не менять RBAC, схему БД либо UI кроме сообщения, реально необходимого для диагностики.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|---------------------------|----------------|
| Чат аналитика | В env задан недоступный `ALL_PROXY` | nanobot создаёт прямой HTTP-транспорт и обращается к LLM без proxy | Существующее безопасное retry-состояние, если прямой LLM недоступен |
| HTTP-поиск и загрузка источника | В env заданы proxy-переменные | SearXNG, DDGS и HTTP-fetch модуля идут напрямую с текущим CA-бандлом | Существующая деградация отдельного источника без утечки transport-detail |
| Прочие LLM-задачи | Классификация, уточнение, refinement, генерация парсера | OpenAI/LangChain-клиенты не наследуют proxy env | Существующий fail-safe конкретной операции |
| Browser/PDF и парсер | Системный proxy либо proxy-env задан | Chromium-операции и дочерний parser запускаются без proxy-наследования | PDF/парсер сохраняют текущие контролируемые ошибки; внешние шрифты не обязательны |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/chat/nanobot_agent.py` — создаёт основной nanobot; сторонний `OpenAICompatProvider` иначе строит SDK с `trust_env=True`.
- `src/bank_audit/loophole/chat/clarify.py` — нативный `AsyncOpenAI` для уточнений, требуется локальный direct HTTP-client.
- `src/bank_audit/loophole/classify.py`, `refine.py`, `chat/tools_nanobot.py`, `parsers/generator.py` — фабрики `ChatOpenAI`; должны переиспользовать безопасный direct transport без утечек сокетов.
- `src/bank_audit/loophole/adapters/fetch_decorator.py` и `adapters/search_decorator.py` — маршрутизируют запросы к shared RAG HTTP/search; нужен изолированный loophole-путь, а не изменение global default.
- `src/bank_audit/rag/fetcher.py`, `rag/web_search.py` — текущие `httpx.Client` по умолчанию наследуют env; допустимы только точечные параметры вызова от Loophole.
- `src/bank_audit/loophole/pdf_export.py` и `src/bank_audit/collectors/browser.py` — Chromium зависит от системного proxy; launch должен получать direct policy только для Loophole-вызовов.
- `src/bank_audit/loophole/parsers/runner.py` — дочерний parser сейчас наследует весь env; запуску нужен очищенный proxy-env.
- `tests/loophole/test_nanobot_agent.py` — место RED→GREEN регрессии: фактически созданный HTTP-транспорт nanobot не содержит `AsyncHTTPProxy` при заданных proxy-env.

## Tasks & Acceptance

**Execution:**
- [x] `src/bank_audit/loophole/` и узкие shared-boundary файлы — введена явная direct-transport политика для Loophole HTTP/LLM/search/fetch/browser/PDF/parser путей без изменения default остальных модулей.
- [x] `src/bank_audit/loophole/chat/nanobot_agent.py` — добавлен instance-local direct transport стороннего nanobot без мутации окружения и с закрытием SDK.
- [x] `tests/loophole/` — добавлены контрактные регрессии nanobot, direct transport, fetch/search, parser и Chromium/PDF; целевой набор прошёл.
- [x] `docs/project-context.md` — зафиксированы диагностика proxy, ограничения тестовой среды и правила повторной проверки.

**Acceptance Criteria:**
- Given `HTTP_PROXY`, `HTTPS_PROXY` и `ALL_PROXY` указывают на недоступный адрес, when создаётся Loophole nanobot, then его фактический HTTP-транспорт не содержит proxy transport.
- Given задан proxy-env, when Loophole выполняет поиск, загрузку источника или LLM-подзадачу, then его клиент не читает proxy-настройки окружения и сохраняет текущую обработку ошибок.
- Given запущен локальный сервис с bad proxy-env, when пользователь отправляет запрос аналитику, then сервер больше не возвращает ошибку соединения, вызванную `127.0.0.1:9`.
- Given выполняется любой другой модуль AuditLens, when он создаёт собственный HTTP-клиент, then эта задача не изменяет его proxy-политику.

## Spec Change Log

## Design Notes

Политика должна быть dependency-local: `httpx` получает `trust_env=False`, а Chromium — явный direct launch option. Для nanobot нельзя временно очищать `os.environ`: это создаст гонку между параллельными запросами и затронет другие модули. Для нерасширяемого стороннего API предпочтителен минимальный адаптер в пределах Loophole с контрактным тестом фактического транспорта.

## Verification

**Commands:**
- `.venv\\Scripts\\python.exe -m pytest tests/loophole/test_nanobot_agent.py -p no:cacheprovider` -- expected: целевой RED→GREEN без внешней сети.
- `.venv\\Scripts\\python.exe -m pytest tests/loophole -p no:cacheprovider` -- expected: нет новых отказов; исторические baseline-отказы фиксируются отдельно.
- `.venv\\Scripts\\ruff.exe check <изменённые Python-файлы>` -- expected: нет новых диагностик.
- `curl.exe --max-time 10 http://127.0.0.1:8000/healthz` и реальный запрос из UI -- expected: сервис готов, nanobot не обращается к `127.0.0.1:9`.
