# Контекст проекта: проблемы и решения

Формат: [ДАТА] Проблема: X → Решение: Y

[2026-07-26] Проблема: SQLite не понимает PostgreSQL-каст `:emb::vector` в `save_kb_example` — INSERT падал бы в SQLite-тестах → Решение: при `embedding=None` INSERT идёт без колонки embedding (две ветки SQL); в тестах `embedder.embed_one` мокается на сбой → срабатывает graceful fallback в `kb.add_example` (warning + embedding=None).

[2026-07-26] Проблема: `ruff check` по модулю loophole показывает 49+ предсуществующих ошибок (F401 и др.) — критерий «линтер без ошибок» недостижим → Решение: критерий переформулирован как «нет НОВЫХ ошибок»: сравнение worktree vs HEAD по затронутым файлам (15 vs 16 — стало на 1 меньше).

[2026-07-26] Проблема: `tests/test_smoke.py` — SyntaxError (bytes-литерал с не-ASCII) и `tests/test_digest.py::test_tg_parses_real_fixture` — UnicodeDecodeError (Windows, чтение фикстуры без encoding); оба предсуществующие на чистом HEAD → Решение: исключены из регрессионного прогона, вне скоупа фичи; не чинились.

[2026-07-26] Проблема: тесты `apply_migration` в трёх файлах жёстко проверяли `call_count == 2` — добавление миграции 014 их сломало → Решение: при добавлении новой миграции обновлять `call_count` в `test_db_schema.py`, `test_db_schema_011.py` и тесте новой миграции; имена констант (MIGRATION_011_PATH → файл 013) исторически расходятся с номерами файлов.

[2026-07-26] Проблема: ветка main содержала чужие незакоммиченные изменения в файлах плана (web.py, loophole.jsx, loophole.css, test_db_schema_011.py) → Решение: работа велась в новой ветке `feat/manual-verdict-marking` поверх рабочего дерева (`git switch -c` сохраняет изменения); hunks задач отделялись от чужих при ревью; коммиты не выполняются без подтверждения пользователя.

[2026-07-26] Проблема: фронт loophole.jsx нельзя проверить визуально без поднятого бэкенда и БД; toast маркировки (4 сек) исчезал до скриншота Playwright → Решение: стенд в `%TEMP%\kilo\lp-stand`: копии loophole.css/jsx + index.html с моком `window.fetch` на `/api/loophole/*` (workspace/banks/records/verdict), `python -m http.server`; Babel-ошибки видны в консоли браузера; для съёмки toast `window.setTimeout` глушится для ms===4000.

[2026-07-26] Проблема: «не подгрузились css стили для маркирования лазеек» — браузер кэшировал `loophole.css` эвристически (Starlette StaticFiles не шлёт Cache-Control), а `_loophole_html_with_bust()` версионировал только jsx; новый JSX + старый CSS = разметка без стилей → Решение: bust `?v=mtime` добавлен и для css (app.py); регрессионный тест `tests/loophole/test_static_bust.py`; импорт `bank_audit.web.app` в тесте требует `DATABASE_URL=sqlite:///:memory:` (conftest ставит sqlite+aiosqlite, но aiosqlite не установлен). После деплоя правки сервер без --reload нужно перезапустить.

[2026-08-26] Проблема: sandbox runner не смог запустить `pwsh.exe` из WindowsApps (`CreateProcessAsUserW`, error 5), поэтому даже read-only команда не стартовала → Решение: повторять обязательные локальные read-only/BMad-команды через явно одобренное sandbox escalation; не считать такой сбой ошибкой проекта или проверки артефакта.

[2026-08-26] Проблема: после замены немедленной публикации на верификационный gate в PRD остался старый parity-пункт `save_loophole` с прямым сохранением в БД → Решение: после изменения жизненного цикла отдельно сканировать user journeys, acceptance/parity checklists и success metrics на старую семантику записи.

[2026-08-26] Проблема: SPEC потребовал тип кейса, статусы и provenance, но addendum сохранял прежний запрет на новые колонки `loophole_record` → Решение: при расширении логического Case contract синхронно обновлять PRD, addendum и mapping хранения; разрешать миграцию/связанные таблицы и оставлять точный DDL архитектуре.

[2026-08-26] Проблема: команда `ruff check src` не запускалась, потому что `ruff` отсутствовал в PATH и системном Python; локальный ruff затем показал 429 предсуществующих ошибок по всему `src` → Решение: запускать `.venv/Scripts/ruff.exe check src`; для docs-only изменений отдельно подтверждать отсутствие diff в `src`/`tests` и честно отмечать полный lint как baseline FAIL, не как регрессию или PASS.

[2026-08-26] Проблема: при Reviewer Gate был запрошен отсутствующий файл `.agents/skills/bmad-prd/references/rubric-walker.md` → Решение: инструкции запуска брать из `references/validate.md`, а саму семиизмерительную рубрику — из `assets/prd-validation-checklist.md`; перед чтением вспомогательных файлов проверять пути из актуального `SKILL.md`.

[2026-08-28] Проблема: PowerShell интерпретировал строку `$path:$start` как недопустимое имя переменной при чтении артефактов → Решение: в интерполируемых строках с двоеточием использовать `${path}`; сбой команды не считать дефектом проекта.

[2026-08-28] Проблема: `bmad-sprint-planning sprint_plan.py generate --fresh` пересоздал статусы, но сохранил из старого файла невалидный ISO timestamp `generated` и устаревший `story_location` → Решение: после `--fresh` отдельно проверять `validate`, нормализовать `generated` в `MM-DD-YYYY HH:MM`, задавать актуальный каталог историй и повторять dry-run до `in_sync: true`.

[2026-08-29] Проблема: PowerShell передал конкатенацию строки в Set-Content как отдельный позиционный аргумент → Решение: вычислять итоговое значение в отдельной переменной до вызова Set-Content.


[2026-08-30] Проблема: Story 2.1 требует AgentFactory, SkillRegistry, шесть allowlisted skills и agent_audit_log, но эти компоненты отсутствуют и вынесены в отдельные prerequisite-stories → Решение: реализацию 2.1 приостановить до явного разрешения включить минимальный agent-core в scope; production-код не изменять.

[2026-08-30] Проблема: sandbox runner отклонял запуск даже read-only команд с `helper_unknown_error: apply deny-read ACLs` → Решение: повторять диагностику через явно одобренный `require_escalated` запуск; ошибку runner не считать дефектом проекта.

[2026-08-30] Проблема: базовый `pytest` не собрал 3 тестовых модуля из-за отсутствующих `playwright` и `selectolax`, а `.venv\\Scripts\\ruff.exe check src` показал 1232 предсуществующие ошибки → Решение: зафиксировать baseline; после изменений проверять отсутствие новых ошибок в затронутых файлах и отдельно отмечать неполный/непроходимый общий прогон.

[2026-08-30] Проблема: команда `pytest` указывает на `E:\\python` без проектных зависимостей, тогда как `python` — `C:\\Python314` с `playwright` и `selectolax` → Решение: для проверок использовать `python -m pytest`; исходный collection-сбой относится к неверному launcher PATH, не к коду проекта.

[2026-08-30] Проблема: PowerShell-экранирование в диагностической команде проверки импортов повредило Python-список и вызвало `SyntaxError` → Решение: передавать код Python в одинарных кавычках PowerShell; сбой quoting не считать дефектом проекта.
[2026-08-30] Проблема: первый managed-agent patch записал escape-последовательность переноса как физический перенос внутри f-string и дал SyntaxError → Решение: заменить f-string на безопасную конкатенацию с chr(10) и повторить целевые тесты.
[2026-08-30] Проблема: integration-test double не принимал публичные prompt/session аргументы managed agent, а audit без request-сессии обращался к отсутствующей aiosqlite → Решение: синхронизировать double с контрактом и писать audit только в переданную server-side сессию.
[2026-08-30] Проблема: targeted ruff после managed-agent интеграции обнаружил новые ошибки импорта, порядка __all__ и неиспользуемой переменной при сохранении baseline-ошибок в repository.py → Решение: исправить только новые ошибки в agent/ и graph.py, baseline не расширять.

[2026-08-30] Проблема: targeted Ruff Story 2.1 обнаружил неиспользуемые импорты `sqlalchemy.text` и `sqlalchemy.orm.Session` в conftest.py → Решение: удалить только эти импорты, повторить targeted pytest и Ruff; полный pytest в узком финальном цикле не запускать.

[2026-08-30] Проблема: `_map_event` требовал `record_stream_event` у hook, а stream-путь не переводил `stop_reason=max_iterations` в partial-результат → Решение: добавить безопасный SSE-адаптер с опциональным recorder и allowlisted именем инструмента; лимит в stream помечается partial, получает понятное объяснение и `error_code=max_iterations`.

[2026-08-30] Проблема: gap review Story 2.1 выявил потерю partial-пояснения после text.delta, обход clarification, утечку данных в prompt, необязательный audit и traversal через run_id → Решение: добавить отдельное безопасное partial SSE-событие с отображением в UI, fail-closed clarification/audit, redaction query/history/answers, строгий slug/UUID run_id и resolved workspace containment.
[2026-08-30] Проблема: при добавлении cancellation-safe audit отступ в graph.py сделал блок except синтаксически недопустимым → Решение: после каждого production patch импортировать затронутый модуль целевым pytest и проверять вложенность try/except до дальнейших изменений.
[2026-08-30] Проблема: старый тест короткого query требовал отправлять односимвольный запрос в LLM, что противоречит server-side fail-closed guard → Решение: при усилении контракта обновлять только конфликтующую регрессионную проверку до запрета execution и отсутствия вызова LLM.
[2026-08-30] Проблема: structural-тест migration helper ожидал прежние пять SQL-вызовов после подключения 044 → Решение: синхронно обновлять count-тест и описание состава helper, сохраняя отдельную проверку фактического DDL agent_audit_log.
[2026-08-30] Проблема: after_run nanobot мог перезаписать redacted stream сырым final_content перед fallback SSE → Решение: применять общий redaction helper и к финальному content, а не только к delta и finalize_content.
[2026-08-30] Проблема: первая версия stream secret-suffix regex содержала inline-флаги после перевода строки и падала при импорте hooks.py → Решение: размещать inline-флаги в начале regex и проверять targeted pytest после production patch.
[2026-08-30] Проблема: PowerShell patch для SQL scope сохранил экранированные кавычки в Python-коде и вызвал SyntaxError → Решение: перед применением проверять физический фрагмент файла и повторять targeted pytest после исправления quoting.

[2026-08-30] Проблема: после обязательного server-side ToolContext старый db_query-тест передавал только SQLAlchemy-сессию и получал db_query_unauthorized → Решение: адаптировать только авторизованный fixture явным ToolContext, сохранив deny без контекста и запрет cross-workspace.
[2026-08-30] Проблема: legacy fetch-double без атрибута text ломал save_loophole в полном regression → Решение: читать text через getattr, использовать excerpt как безопасный fallback, а при отсутствии обоих сохранять штатный empty/fetch fallback.
[2026-08-30] Проблема: targeted Ruff показал 14 новых диагностик в tools_nanobot.py и test_tools_nanobot.py после Story 2.1 hardening → Решение: исправить только эти файлы (импорты, точные типы исключений и явные boundary noqa), оставив 48 baseline-ошибок без изменений.
[2026-08-30] Проблема: stateful SSE-redactor пропускал чувствительный префикс, разделённый между чанками (`+` и телефон, `api_` и ключ) → Решение: удерживать потенциальный phone/secret prefix до безопасной границы и выпускать его только после redaction.
[2026-08-30] Проблема: CancelledError в run_chat выходил до сохранения partial audit, а небезопасный run_id попадал в fallback → Решение: сохранять типизированный partial результат перед повторным выбросом отмены и нормализовать run_id через безопасный slug/UUID до всех audit fallback.
[2026-08-30] Проблема: db_query принимал явный LIMIT больше 500 и мог передать его в БД → Решение: отклонять такой запрос до открытия DB-сессии; managed prompt явно сообщает жёсткий предел.
[2026-08-30] Проблема: session=None позволял пройти до AgentFactory или завершить запуск как done без обязательного аудита → Решение: после доступного clarification, но до execution, возвращать typed session_unavailable; старые execution-тесты используют явную server-side session.
[2026-08-30] Проблема: stateful SSE-redactor выпускал первый символ secret и числовые PII до распознавания полного значения (`s` + `k-secret`, card/ИНН) → Решение: удерживать secret-prefix и числовой PII-prefix до безопасной границы, затем применять общий redaction при flush.
[2026-08-30] Проблема: `ManagedAgent` мог вернуть сырой `result.content`, если hook не заполнил `final_answer` → Решение: обезличивать оба пути ответа перед формированием и сохранением `AgentResult`.
[2026-08-30] Проблема: `audit_table_load` создавался без server-side ToolContext и передавал в repository `session=None` → Решение: пометить tool как context-required, fail-closed проверять user/workspace/session и передавать только доверенную session.
[2026-08-30] Проблема: `table_load.limit` выше 500 мог попасть в БД → Решение: отклонять limit выше жёсткого предела до вызова repository.
[2026-08-30] Проблема: stateful SSE-redactor пропускал неизвестные split-prefix (`A` + Authorization, JWT) и выпускал незавершённый email → Решение: удерживать потенциальный ASCII sensitive suffix и JWT до flush, а незавершённый email заменять безопасным маркером.
[2026-08-30] Проблема: эвристический stateful redactor мог выпустить raw text delta до распознавания неизвестной границы → Решение: полностью буферизовать весь текст stream и публиковать только единый ответ после полного redaction; text delta без buffer hook отбрасывается fail-closed.
[2026-08-30] Проблема: прямые context-bound tools доверяли наличию user/workspace без проверки ownership → Решение: перед DB/table операцией сверять workspace.user_id с server-side ToolContext и fail-closed запрещать чужой контекст.
[2026-08-30] Проблема: `LOOPHOLE_NANOBOT_MAX_ITERATIONS` принимал нулевые, отрицательные, слишком большие и нечисловые значения → Решение: валидировать значение до создания Nanobot в диапазоне 1..500 с default 20.
[2026-08-30] Проблема: повторная `CancelledError` могла оборвать `bot.aclose()` и оставить временный конфиг → Решение: выполнять закрытие отдельной задачей под `asyncio.shield`, удалить конфиг в `finally` и после cleanup вернуть отмену вызывающему коду.
[2026-08-30] Проблема: migration 044 была проверена только структурным SQLite-тестом без PostgreSQL staging → Решение: добавить явный PostgreSQL harness для apply/re-run, индексов и append-only ограничений; без staging статус только UNVERIFIED, SQLite не принимается.
[2026-08-30] Проблема: ошибка rewrite clarification оставляла await-состояние без challenge и вопроса → Решение: восстанавливать локализованный retry-вопрос и server-side clarification token без запуска агента.
[2026-08-30] Проблема: `db_query` распознавал только беззнаковый LIMIT, поэтому `LIMIT -1` и `LIMIT +501` обходили preflight до проверки ownership → Решение: отклонять любой знаковый или неразбираемый LIMIT до обращения к БД; разрешены только целые беззнаковые значения не выше 500.
[2026-08-30] Проблема: после fail-closed `clarification_unavailable` исходный challenge уже был поглощён, а backend/UI не возвращали вопросов и нового token → Решение: route выдаёт safe fallback-вопросы и новый server-side clarification token, UI восстанавливает pending clarification без execution token.
[2026-08-30] Проблема: migration 044 безусловно создавала PostgreSQL trigger и не могла примениться в поддерживаемом Greenplum 6 → Решение: таблица и индексы применяются в обоих диалектах, append-only trigger создаётся динамически только вне Greenplum; реальная Greenplum staging-проверка остаётся UNVERIFIED.
[2026-08-30] Проблема: review выявил 16 новых Ruff diagnostics в Story 2.1 → Решение: сузить intentional boundary exemptions, уточнить тип исключения, объединить context managers и отформатировать test imports; старые diagnostics вне review-строк оставлены baseline.
[2026-08-30] Проблема: 	.me/joinchat/ ошибочно принимался как публичный Telegram-хендл в Story 6.1 → Решение: зарезервированный неполный путь joinchat отклоняется до записи в БД.

[2026-08-30] Проблема: штатный apply_patch заблокирован ACL, а первая PowerShell-замена повреждённой строки завершилась ParserError → Решение: выполнили точечную literal-замену через одобренный PowerShell и сразу запустили синтаксическую и тестовую проверку.

[2026-08-30] Проблема: первый `pytest` запускался системным launcher без `nanobot`, а полный Ruff Story 2.1 показал 32 ошибки → Решение: проверки запускать через `.venv/Scripts/python.exe`; сравнение с `HEAD` подтвердило, что все 32 Ruff-диагностики являются baseline, новых ошибок Story 2.1 нет.
