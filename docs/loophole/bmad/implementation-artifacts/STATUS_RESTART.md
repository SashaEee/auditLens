# Checkpoint реализации Story «Лазейки»

Дата: 2026-08-31

Этот файл нужен для безопасного продолжения после перезапуска. Базовый checkpoint
зафиксирован коммитом `0e17177`; все изменения текущего продолжения остаются незакоммиченными.

## Текущий checkpoint — 2026-08-31

### Читаемый отчёт AI-исследования и экспорт доказательств

- `spec-research-report-markdown-and-export.md` остаётся `in-progress`: safe Markdown и
  server-side PDF/DOCX endpoints реализованы с ownership-проверкой, но `save_report_result()` пока
  сохраняет пустой `evidence_snapshot`. Поэтому export не может честно содержать реальные
  проверенные URL/источники.
- Карточка «Доказательства и источники» безопасно отображает заголовки, абзацы и списки Markdown;
  server-side `report_id` открывает меню «Скачать исследование» с PDF и Word.
- PDF-failure возвращает typed `503 pdf_unavailable` с предложением выбрать Word или повторить
  PDF; отсутствие evidence сохраняется как честная отметка в документе.
- Проверки: server acceptance — `5 passed`; Browser-runtime safe Markdown/download — `1 passed`;
  scoped Ruff — PASS; полный `tests/loophole` — `722 passed, 1 skipped, 21 warnings`.
  Ручной in-app Browser подтвердил базовую research-поверхность, focus restore и отсутствие console
  errors, но не готовый report/download: в workspace нет завершённого отчёта, а evidence contract
  ещё не реализован.

### Финальный интерфейс варианта 3

- Спецификация `spec-adapt-loophole-ui-to-final-mockup.md` имеет статус `done`.
- Вкладки приведены к порядку «Общая база → Добавить источник → Новое AI-исследование →
  Очередь верификации → Управление доступом» с прежними RBAC-границами.
- В общей базе отсутствует графа «Надёжность», отдельно показаны дата публикации в
  первоисточнике и дата сбора. Выборочный CSV скачивается напрямую для отмеченных строк.
- Web-парсеры вынесены во вкладку «Добавить источник». Сквозной nullable `published_at`
  добавлен migration `058_loophole_publication_date.sql` без подмены неизвестной даты.
- Same-input визуальная проверка и Browser evidence сохранены в `design-qa.md` и
  `workspace/qa/loophole-final/`; финальная итерация не содержит P0/P1/P2.

### Последняя завершённая точечная правка

- По пользовательской Browser-аннотации из «Управления доступом» удалена лишняя карточка
  «Статус Telegram-целей» вместе с клиентским состоянием и запросом
  `/admin/telegram-targets`. Серверный Telegram endpoint и worker-контур не удалялись.
- Две оставшиеся карточки «Роль ЦК КС» и «Сводный аудит» занимают две верхние колонки без
  пустого grid-ряда. Browser-проверка показала ровно две секции и ноль Telegram-заголовков.
- Спецификация: `spec-remove-telegram-target-status-card.md`, статус `done`.
- Проверки после правки: targeted admin/runtime — `52 passed, 1 warning`; полный
  `tests/loophole` — `687 passed, 1 skipped, 15 warnings`; Ruff всего
  `src/bank_audit/loophole` и `tests/loophole` — PASS; `git diff --check` — PASS
  (только предупреждения Git о будущем LF→CRLF).

### Завершённое исправление агента

- `spec-fix-agent-clarification-loop-and-latency.md` переведена в `done` после трёхслойного
  review и patch-цикла; frozen-блок сохранён с SHA256
  `671fe1850ea846e82f34c3278367098f85d6e251a312e704cc1dc31336dac731`.
- Clarification использует один FAST-gate с `timeout=15` и `max_retries=0`; после ответа
  обогащённый запрос собирается детерминированно без второго LLM-вызова. Недействительный
  execution token отклоняется до graph/history и фиксируется в audit.
- Развёрнутый ответ вводится в общем composer и сразу появляется в user bubble. Временный
  503 восстанавливает вопрос и controls, истёкший token создаёт безопасный draft, а сбой
  второго `/chat` не маскируется состоянием done и оставляет подготовленный запрос для retry.
- Light/dark same-input Browser QA: по одному question bubble, два user bubble, typing и
  непрерывный статус «Обдумывает ответ»; console errors — 0, P0/P1/P2 — 0.
- Финальные проверки: backend — `135 passed`; browser-runtime — `32 passed`; полный
  `tests/loophole` — `702 passed, 1 skipped, 15 warnings`; Ruff модуля, тестов и QA-скрипта —
  PASS; target `git diff --check` — PASS.

### Текущая незавершённая работа

1. `spec-register-parser-development-request.md` — `draft`, код по этой спецификации ещё не
   изменён; planning завершён, объём — 1577 токенов, ожидается BMad `[A]/[E]`. Текущая форма
   всё ещё вызывает создание/валидацию парсера, показывает кнопку
   «Создать и проверить» и карточку «Telegram-источники». Требуемый следующий контракт —
   только создать `pending`-заявку в `source_proposal(purpose='loophole_parser')`, не вызывая
   LLM, generator, runner, healer или scheduler; каталог этой вкладки станет read-only.
2. `spec-preliminary-research-source-import.md` — `draft`, route/service/migration и UI для
   переноса новых research-source как «Предварительно», фильтра ЦК КС и provenance пока отсутствуют.
3. `spec-research-report-markdown-and-export.md` — нужно сохранить immutable verified evidence
   snapshots и добавить не пустой PDF/DOCX/browsing acceptance, прежде чем менять статус на `done`.
4. `deferred-work.md` содержит отдельные production-риски и verification gaps. Они не
   исправлялись в рамках точечных UI-аннотаций и не должны считаться закрытыми.

### Безопасная точка продолжения

1. После подтверждения draft реализовать `spec-register-parser-development-request.md`:
   route/repository заявки, дедупликацию домена, audit и новый copy; удалить Telegram-note.
2. После каждого изменения запускать целевые тесты, затем полный `pytest tests/loophole -q`,
   `ruff check src/bank_audit/loophole tests/loophole`, Browser-runtime и обязательный
   in-app Browser QA светлой/тёмной тем, сценариев success/error, консоли и comparison.
3. Не создавать коммит без явного отдельного запроса пользователя и не затрагивать прочие
   изменения грязного worktree.

## Принятые Story

- Story 1.1–1.4 и `spec-loophole-refresh-button`: ранее проверены; целевой набор — `109 passed`.
- Story 1.5: завершена и принята после отдельного implementer-а, spec-review и quality-review. Проверки: `test_admin_roles_audit.py` — `32 passed`; полный `tests/loophole` на момент ревью — `564 passed, 1 skipped`; `git diff --check` — PASS.

## Story 2.1 — управляемый агент исследования

- Реализация внесена отдельным subagent и исправлена после quality-review.
- Последние подтверждённые проверки: targeted `89 passed, 398 warnings`; полный `tests/loophole` — `578 passed, 1 skipped, 1010 warnings`; `git diff --check` — PASS.
- Исправлены: обходы SQL `LIMIT -1`/`LIMIT +501`; восстановление mandatory clarification после `clarification_unavailable`; 16 новых Ruff diagnostics; безопасное поведение migration 044 в Greenplum 6.
- Quality-review главного интегратора от 2026-08-30: критических дефектов по критериям Story 2.1 не найдено. Подтверждено: целевые тесты — `89 passed`; полный `tests/loophole` — `578 passed, 1 skipped`; `git diff --check` — PASS. `ruff` показывает 32 уже существующие baseline-диагностики (сравнение с `HEAD`), новых нет. Реальный PostgreSQL/Greenplum staging отсутствует, поэтому migration harness честно остаётся `UNVERIFIED`.

## Story 5.1 — создание валидируемого парсера

- Устранены обязательные gaps TDD-циклом главным интегратором:
  1. `t.me`, `telegram.me` и `@handle` отклоняются до LLM и сохранения обычного parser.
  2. Неуспех validation сохраняет parser со статусом `validation_failed` и итоговый run; включение расписания до статуса `ready` возвращает 409.
  3. Исправлен `I001` в `tests/loophole/test_parsers_web.py`.
- Проверки: целевые parser tests — `29 passed, 1 warning`; полный `tests/loophole` — `583 passed, 1 skipped, 1 warning`; Ruff изменённых файлов и `git diff --check` — PASS.
- После независимого review устранены ещё два обхода: Telegram URL с явным портом отклоняется до LLM/storage, а cron/healer получает только parser со статусом `ready`. Проверки после исправления: `586 passed, 1 skipped, 1 warning`; Ruff изменённых файлов и `git diff --check` — PASS.
- Следующий шаг: повторный независимый spec/quality-review Story 5.1; реальная browser/production validation не запускалась.

## Story 6.1 — идемпотентная регистрация Telegram-цели

- Отдельный subagent добавил `src/bank_audit/loophole/telegram_targets.py` и `tests/loophole/test_story_6_1_target_registry.py`.
- Главный интегратор с явного разрешения пользователя восстановил одну синтаксически повреждённую строку после ACL-сбоя.
- Последняя проверка: `python -m py_compile src/bank_audit/loophole/telegram_targets.py` и целевой тест — `10 passed`.
- Исправлено: `https://t.me:99999/bank_news` fail-closed преобразует ошибку `parts.port` в `InvalidTelegramTarget`; целевой test — `11 passed`, Ruff — PASS.
- Создана migration `046_loophole_telegram_target.sql`: таблица реестра и уникальный индекс
  `normalized_address` обеспечивают race-safe idempotency через существующий IntegrityError-path.
- Проверки после миграции: registry + DDL контракт — `12 passed`; Ruff нового теста — PASS.

## Story 2.2 — поиск источников и извлечение кейсов

- Реализованы изолированные таблицы `loophole_research`, `loophole_research_source` и
  `loophole_research_candidate` в migration `045_loophole_research_cases.sql`.
- `ResearchCaseService` хранит параметры поиска, источники и извлечённый текст; кандидат
  формируется по `CaseContractV1` только для successfully fetched источника того же
  `research_id` и всегда возвращается со ссылкой на источник. В `loophole_record` ничего
  не публикуется.
- Недоступный URL или ошибка извлечения сохраняются как понятное ограничение, не создают
  фиктивный кандидат и не прерывают обработку остальных результатов.
- Проверки: целевые тесты — `6 passed`; полный `tests/loophole` — `592 passed, 1 skipped,
  1 warning`; Ruff затронутых файлов — PASS.

## Story 2.3 — модельная классификация результатов исследования

- Migration `047_loophole_research_classification.sql` вводит отдельные traceable поля
  model verdict для исследовательского кандидата, не затрагивая общий каталог.
- Классификатор использует профильный prompt, подтверждённые примеры KB, выбранную модель и
  размер batch из `LOOPHOLE_RESEARCH_CLASSIFY_BATCH_SIZE`. Ручной verdict и решение ЦК КС
  исключают кандидат из модельной выборки до вызова LLM.
- Частичная ошибка classifier возвращается пользователю с `candidate_id` и причиной; успешно
  обработанные кандидаты сохраняют model verdict.
- Проверки: целевые тесты — `12 passed`; полный `tests/loophole` — `599 passed, 1 skipped,
  1 warning`; Ruff затронутых файлов — PASS.

## Story 2.4 — наблюдаемый и адаптивный ход исследования

- Проверено и закреплено отдельным тестом ранее реализованное поведение managed SSE: первый
  локализованный статус `clarify` выдаётся до первого внешнего вызова (в тесте менее секунды,
  что существенно меньше SLA 15 секунд).
- Панель имеет русские фазы «Уточнение», «Выполнение», «Ответ», на ширине от 1100px остаётся
  desktop sidebar, ниже 1100px — off-canvas с backdrop. Состояния `chat`, `chatInput` и
  progress находятся вне условного рендера панели, поэтому закрытие не отменяет запуск.
- Проверки: целевые тесты — `2 passed`; полный `tests/loophole` — `601 passed, 1 skipped,
  1 warning`; Ruff затронутого теста — PASS.

## Story 2.5 — передача выбранного кейса на верификацию

- Migration `048_loophole_verification_snapshot.sql` добавляет Greenplum-совместимый
  immutable submitted snapshot с case/evidence JSONB, автором, временем и correlation `run_id`.
- `ResearchCaseService.submit_for_verification` принимает только active evidence текущего
  исследования, фиксирует revision и полный текст evidence в snapshot; повторная отправка
  той же draft_version возвращает существующий snapshot, а последующие изменения draft его
  не меняют.
- `POST /research/candidates/{candidate_id}/submit` проверяет owner workspace server-side и
  при отказанном evidence возвращает нейтральный 409 без метаданных. Ответ несёт статус
  «Ожидает решения ЦК КС». UI выбора/рассмотрения объединён с реализацией Story 3.1.
- Проверки: целевые тесты — `10 passed`; полный `tests/loophole` — `605 passed, 1 skipped,
  1 warning`; Ruff новых файлов — PASS.

## Story 3.1–3.3 — решение ЦК КС, публикация и lifecycle

- Migration `049_loophole_verification_decision.sql` и `ResearchCaseService.decide_snapshot`
  хранят одно append-only решение с обязательным комментарием, экспертом и run_id; повторный
  запрос возвращает уже сохранённый итог. Route решения проверяет роль `ccks_expert` на сервере.
- Положительные решения публикуются через idempotent `loophole_publication_mapping` из
  migration `050_loophole_publication_mapping.sql`; повтор command key не создаёт вторую
  запись каталога. Решение `not_confirmed` публикацию не запускает.
- Migration `051_loophole_lifecycle_constraints.sql` добавляет DB-side unique keys на
  submitted draft, decision snapshot и publication decision. `verify_lifecycle_postgres()`
  выполняет lifecycle DDL на явном staging или честно возвращает `UNVERIFIED` без него.
- Проверки: целевые lifecycle tests — `12 passed`; полный `tests/loophole` —
  `612 passed, 1 skipped, 1 warning`; `git diff --check` — PASS.

## Story 4.1 — поиск и фильтрация опубликованного каталога

- `GET /catalog` возвращает только `status='published'` и подтверждённые (`is_loophole`) кейсы;
  черновики AI-исследований и pending-кейсы не проходят server-side фильтр.
- Главный экран каталога использует published API. Единые bank/period/text/verdict/status
  фильтры сохраняют существующее состояние таблицы, текстовый поиск debounce 350 мс, а reset
  возвращает исходный ReportFilter UI-state. Сортировка, aria-sort, чекбоксы и детали покрыты
  существующими accessibility-проверками.
- Проверки: UI/accessibility subset — `74 passed`; полный `tests/loophole` —
  `614 passed, 1 skipped, 1 warning`; Ruff нового теста — PASS.

## Story 4.2 — экспорт отфильтрованных данных

- `ReportFilterV1` используется CSV, XLSX и PDF endpoints и принудительно выбирает только
  опубликованные подтверждённые кейсы. XLSX загружает до 10 001 строки, отклоняет 10 001+
  с фактическим числом и не выдаёт неполный файл.
- При недоступном Playwright PDF endpoint возвращает typed `503` с кодом `pdf_unavailable`
  и русским сообщением для UI fallback на CSV/XLSX.
- Проверки: форматы и limit — `3 passed`; полный `tests/loophole` —
  `617 passed, 1 skipped, 1 warning`; `git diff --check` — PASS.

## Story 4.3 — безопасная аналитика опубликованного каталога

- Migration `052_loophole_published_analytics_view.sql` вводит allowlisted
  `loophole_published_catalog_v1` и grant роли `loophole_readonly`.
- `POST /analytics/query` принимает параметризованный single SELECT только к этой view,
  блокирует DML/DDL, multi-statement, функции и другие таблицы до обращения к БД, применяет
  limit 500 и PostgreSQL `statement_timeout` 3 секунды, возвращает JSON-таблицу.
- Проверки: security query tests — `5 passed`; полный `tests/loophole` —
  `622 passed, 1 skipped, 1 warning`; `git diff --check` — PASS.

## Story 4.4 — управляемое расписание аналитических запросов

- Migration `053_loophole_scheduled_analytics.sql` хранит только идентификатор и
  версию именованной задачи, workspace, владельца, получателя, cron и срок действия;
  raw SQL и внешние адресаты отсутствуют.
- `ScheduledAnalyticsService` повторно проверяет membership владельца/получателя,
  ownership workspace и актуальность серверного query capability. При отказе создаёт
  один `schedule_skipped_<причина>` audit event и не выполняет запрос.
- Результат остаётся во внутреннем workspace с явным owner/recipient ACL; TTL —
  минимум из срока контракта и 24 часов. Фоновый запуск включается только через
  `SCHEDULED_ANALYTICS_ENABLED=1`.
- Проверки: контракт/ACL/migration — `5 passed`; полный `tests/loophole` —
  `627 passed, 1 skipped, 1 warning`; Ruff новых файлов — PASS.

## Story 5.2 — запуск и наблюдение обычного парсера

- `ParserRunner` создаёт изолированный run, транслирует безопасный log-tail через
  SSE и финализирует `success`/`empty`/`error` без изменения конфигурации парсера.
- Ручной endpoint теперь server-side допускает запуск только parser со
  `status='ready'`; невалидированная конфигурация возвращает 409 до создания run.
- UI отображает запуск, итоговые статусы и журнал; повторный запуск доступен той же
  кнопкой после terminal state, а ошибочный run не меняет другие runs.
- Проверки: parser web/runner/scheduler — `45 passed`; регрессия реализованных
  историй — `628 passed, 1 skipped, 1 warning` (ожидаемый RED 6.2 временно исключён).

## Story 5.3 — расписание и управление жизненным циклом обычного парсера

- `PATCH /parsers/{id}` сохраняет cron/auto-enabled/next-run только отдельно для
  указанного валидного parser и аудирует изменение; пустой cron отключает расписание.
- `parsers.scheduler` выбирает только `ready` + `auto_enabled` parsers, запускает их
  тем же `ParserRunner` с trigger `cron` и не создаёт Telegram-клиент, сессию или цель.
- Проверки: `test_parsers_scheduler.py` и `test_parsers_web.py` входят в зелёный
  набор `45 passed`; регрессия — `628 passed, 1 skipped, 1 warning`.

## Story 6.2 — управление доступом и жизненным циклом Telegram-цели

- `TargetAccessService` управляет subscription только canonical root Telegram-цели;
  redirect ID, другой workspace и отсутствие capability отклоняются fail-closed.
- Выдача/отзыв subscription версионируются и аудируются. Деактивация останавливает
  новые lease, увеличивает generation/fence и сохраняет durable fenced terminal event,
  не удаляя историю или checkpoint; реактивация продолжает incremental путь.
- Migration `054_loophole_target_access.sql`; проверки: `8 passed`, полный
  `tests/loophole` — `636 passed, 1 skipped, 1 warning`; Ruff и diff — PASS.

## Story 6.3 — независимый безопасный Telegram ingestion

- `telegram_ingestion.py` — worker-only контур: initial обход сохраняет доступную
  историю ingress-объектов, incremental путь продолжает с устойчивого checkpoint и
  принимает поздние комментарии; research/candidate/verification/catalog не создаются.
- Migration `055_loophole_telegram_ingestion.sql` фиксирует identity+version
  дедупликацию, run history и checkpoint. Неодобренный текст/вложение/metadata
  идёт только в metadata-only quarantine; raw body, replacement map и attachment
  не персистируются, не попадают в LLM или audit.
- Проверки: целевые ingress/quarantine — `5 passed`; полный `tests/loophole` —
  `641 passed, 1 skipped, 1 warning`; Ruff новых файлов и diff — PASS.

## Story 6.4 — устойчивый Telegram worker и наблюдаемый SLO

- `telegram_worker.py` использует global и target lease с fencing token: stale worker
  не может записать batch или checkpoint. `ingestion_reaper` terminalize-ит незавершённый
  обход одним outbox/audit summary, а новый владелец продолжает checkpoint без дублей.
- Migration `056_loophole_telegram_worker.sql`; структурированный safe journal содержит
  режим, checkpoint до/после, счётчики, длительность и безопасную ошибку, а SLO проверяет
  скользящее окно 24 часа либо единственный активный обход.
- Проверки: worker/reaper/SLO — `4 passed`; полный `tests/loophole` —
  `645 passed, 1 skipped, 12 warnings`; Ruff и diff — PASS.

## Story 6.5 — защищённый perimeter и доказательства готовности worker

- Migration `057_loophole_telegram_perimeter.sql` задаёт least-privilege DCL для
  `auditlens_app`, `loophole_readonly`, `telegram_worker`, `ingestion_reaper` и
  `audit_retention`, плюс fenced SECURITY DEFINER functions для lease, attempt,
  batch, reaper и retention. Worker не имеет прямого DML/DCL или доступа к
  catalog/research/verification/agent audit tables.
- Production facade `telegram_worker.py` вызывает только controlled functions;
  legacy SQLite-механика изолирована в `telegram_worker_sqlite.py` исключительно
  для unit-тестов. Batch контролирует fencing, run, sanitized ingress/quarantine,
  checkpoint, счётчики и journal.
- `deploy/telegram-worker/` описывает отдельный service account без inbound HTTP,
  TLS/CA, approved secret manager и ограниченный egress. Perimeter verifier
  возвращает честный `UNVERIFIED` без внешнего staging-evidence.
- Проверки: target perimeter/runtime — `10 passed`; полный `pytest` —
  `684 passed, 1 skipped, 12 warnings`; Ruff новых файлов и `git diff --check` — PASS.

## Review patch — известный scope и безопасный LLM transport failure

- Корневая причина browser-сбоя подтверждена: локальный preview унаследовал
  `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://127.0.0.1:9`. С `trust_env=True` Cloud.ru
  воспроизводимо завершался `ConnectError [WinError 10061]`, а прямой HTTPX/OpenAI-клиент с
  проектным CA bundle возвращал HTTP 200.
- Clarification gate теперь не вызывается, когда в исходном запросе уже есть банковский продукт
  и период. Банк по умолчанию означает «все банки»; fallback спрашивает только действительно
  отсутствующий продукт или период.
- Ошибка managed agent больше не проходит как готовый ответ: graph выдаёт typed
  `agent_unavailable`, UI скрывает сырой provider text, показывает безопасное retry-сообщение и
  возвращает исходный запрос в composer.
- Реальная проверка во встроенном Browser тем же пользовательским запросом: карточек уточнения
  `0`, запрос сразу перешёл в выполнение, финальная фаза `Готово`; `/api/loophole/chat` и все
  вызовы Foundation Models завершились HTTP 200, console errors `0`. Светлая и тёмная темы
  проверены на состоянии готового ответа; captures лежат в
  `workspace/qa/loophole-agent-chat-fix/real-browser/`.
- Регрессия: review backend subset — `142 passed`; browser-runtime — `33 passed`; полный
  `tests/loophole` — `706 passed, 1 skipped, 15 warnings`; scoped Ruff — PASS.

## Review patch — жёсткий publication period и нейтральный bank scope

- Извлечение web-источника сохраняет только точный timezone-aware `published_at`. Для запроса
  `за <месяц> <год>` серверный `web_fetch` и `extract_loopholes` не допускают источник без
  подтверждённой даты либо за пределами закрытого календарного месяца до обращения к LLM.
- `table_load`, каталог и поиск по записи БД используют `published_at` (верхняя граница включает
  последний день месяца), а не дату сбора. Миграция `058_loophole_publication_date.sql` применена
  в локальной проверочной БД; UI переименовал фильтры в «Дата публикации».
- Prompt больше не содержит неявную установку на Сбербанк: если пользователь не назвал банк,
  область — все банки. Живой запрос про кредитную карту за август 2026 без банка завершился без
  уточнений и честно сообщил об отсутствии допустимых источников вместо использования старых данных.
- Финальная регрессия: `tests/loophole` — `716 passed, 1 skipped, 21 warnings`; scoped Ruff — PASS.
  Реальный in-app Browser: `questions=0`, фаза `Готово`, console errors `0`, light/dark comparison
  — `P0=0, P1=0, P2=0`; артефакты в `workspace/qa/loophole-agent-chat-fix/real-browser/`.

## Открытая работа

- Story 1.1–6.5 реализованы в текущем незакоммиченном worktree, но часть production/staging
  доказательств остаётся `UNVERIFIED`; подробности перечислены в `deferred-work.md`.
- Change-spec исправления clarification loop/latency завершена со статусом `done`;
  `spec-register-parser-development-request.md` остаётся единственной пользовательской
  change-spec 2026-08-31 в статусе `draft`.

## Порядок и параллелизация

- Единственный владелец общих контрактов и миграций: главный интегратор. Не создавать параллельно миграции после `044_loophole_agent_audit.sql`.
- Критические общие файлы: `repository.py`, `db_schema.py`, `authorization.py`, `web.py`, `static/loophole.jsx`, `config.py`, `tests/loophole/conftest.py`.
- Зависимости: `2.2 → 2.3 → 3.3 → 2.5 → 3.1 → 3.2 → 4.1 → {4.2, 4.3} → 4.4`; `5.1 → 5.2 → 5.3`; `6.1 → 6.2 → 6.3 → 6.4 → 6.5`; также `6.2 → 2.5`, `6.5 → 4.3`.

## Важное ограничение среды

У subagent систематически падает штатный `apply_patch` с `windows sandbox failed: helper_unknown_error: apply deny-read ACLs`. Не использовать неявные обходы. Перед любым следующим точечным изменением главным интегратором требуется явное разрешение пользователя.
