---
title: 'Очередь верификации и модалка «Вердикт»: все комментарии и их авторы'
type: 'feature'
created: '2026-09-10'
status: 'done'
baseline_revision: '67caf2b0ba4b2c01b163272ce3b72b6a03f7837a'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
deferred: []
---

<intent-contract>

## Intent

**Problem:** В карточке проверки вкладки «Очередь верификации» и в модалке «Вердикт записи» эксперт видит только комментарий классификатора (`verdict_reason`); решения ЦК КС (`loophole_verification_decision`: комментарий, `decided_by`, `decided_at`) на этих поверхностях не показаны вовсе, а комментарий классификатора безвозвратно затирается при ручном вердикте (`POST /records/verdict` перезаписывает `verdict_reason`).

**Approach:** Обогатить ответ `GET /queue` списком решений ЦК по каждой записи (батч-запрос по канонической цепочке record → `loophole_preliminary_import` → `loophole_research_candidate` → `loophole_verification_snapshot` → `loophole_verification_decision`), показывать решения с авторами и датами в карточке и в модалке вердикта; сохранить исходный комментарий классификатора в новой колонке `classifier_verdict_reason` (freeze при перезаписи ручным вердиктом).

## Boundaries & Constraints

**Always:**
- Показывать все решения ЦК записи (запись может быть импортирована из нескольких исследований → несколько snapshot/decision на один `record_id`) с `decided_by` и `decided_at`.
- Секция «Комментарий классификатора» в карточке берёт текст из `classifier_verdict_reason ?? verdict_reason`; исходный текст классификатора переживает ручной вердикт (`COALESCE`-freeze в `update_verdict`).
- Сохранять роль-гейт и fail-closed `GET /queue` (`require_role ccks_expert`) и существующий контракт `POST /records/verdict` (запрос/ответ).
- Пустые состояния нейтральны: нет решений → поясняющий текст; карточка и модалка работоспособны.
- `decided_at` форматируется существующим `fmtDate` (дата и время).

**Never:**
- Не менять таблицы и инварианты верификации epic-3.1: `loophole_verification_snapshot`, append-only `loophole_verification_decision`, `decide_snapshot`, `POST /verification/snapshots/{snapshot_id}/decision`.
- Не создавать ленивый эндпоинт истории комментариев и таблицу истории комментариев записи.
- Не обогащать каталог (`list_catalog_cases`, view `catalog`) решениями — общая модалка вердикта должна безопасно деградировать при отсутствии данных решений.
- Не убирать существующие секции карточки (включая поле «Комментарий участника ЦК» и полный текст) и поле «Комментарий аудитора» в модалке.
- Не отдавать `raw_text` в `GET /queue`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Запись с решениями ЦК | `queueSelected.decisions.length > 0` | Карточка и модалка показывают все решения: тип, комментарий, `decided_by`, дата `decided_at`; порядок по `decided_at`, затем `decision_id` | No error expected |
| Решений нет | `decisions = []` | Секция карточки показывает «Решений ЦК пока нет.»; в модалке список пуст, остальные поля работают | — |
| Есть комментарий классификатора | `verdict_model != 'manual'`, `verdict_reason` заполнен | Показан в карточке («Комментарий классификатора») и в модалке read-only строкой | No error expected |
| Ручной вердикт поставлен (после миграции) | `update_verdict` перезаписал `verdict_reason` | `classifier_verdict_reason` содержит исходный текст классификатора; карточка при последующих показах берёт его | No error expected |
| Legacy-перезапись до миграции | `classifier_verdict_reason IS NULL` | Fallback на текущий `verdict_reason`; утерянный текст восстановить невозможно | Остаточный риск задокументирован (Design Notes) |
| Модалка из каталога | `rec.decisions` отсутствует (каталог не обогащается) | Блок решений не рендерится; остальное поведение модалки прежнее | — |
| Не-эксперт | роль не `ccks_expert` | `GET /queue` → 403, существующий fail-closed экран очереди | Без изменений |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/web.py` -- GET `/queue` (96–107: `require_role ccks_expert` → `repo.list_verification_queue`, ответ `{records, count}`); POST `/records/verdict` (568–628: `reason = body.comment or f"manual:{user_id}"` на 585, `repo.update_verdict(..., model="manual")` на 591–599) — менять не нужно, тело ответа прежнее; POST `/verification/snapshots/{id}/decision` (243–269) — только чтение, не трогать.
- `src/bank_audit/loophole/repository.py` -- `update_verdict` (154–181): сюда freeze-колонка. `list_verification_queue` (839–856): SELECT только из `loophole_record` с `verdict_reason`, без решений — сюда обогащение. Каноническая цепочка джойнов record → import → candidate → snapshot → decision уже есть в `_catalog_where` (416–438, `positive_decision`/`any_decision`) — переиспользовать как образец.
- `src/bank_audit/loophole/research_cases.py` -- `decide_snapshot` (855–921): единственное append-only решение на snapshot — только чтение, не трогать.
- `migrations/049_loophole_verification_decision.sql` -- схема решения: `decision IN ('vulnerability','fraud_scheme','not_confirmed')`, `comment NOT NULL`, `decided_by`, `decided_at TIMESTAMPTZ`, `run_id`; `060_loophole_preliminary_import.sql` -- `record_id` ↔ `research_id`/`source_id`.
- `migrations/066_loophole_record_classification.sql` -- образец стиля миграции `ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS ...` (безопасно для Greenplum 6 и PG); setup-скрипты применяют `migrations/*.sql` glob'ом (`scripts/setup.sh:131`), отдельных реестров дополнять не нужно.
- `src/bank_audit/loophole/static/loophole.jsx` -- карточка `article.lp-queue-detail` (2787): секция «Комментарий классификатора» `lp-queue-reason` (2797–2800, источник `queueSelected.verdict_reason`), поле «Комментарий участника ЦК» (2814–2824), полный текст (2827–2830). Модалка вердикта (3183–3222+): `rec = verdictModal.record` — для очереди это тот же объект `queueSelected` (2807), новых запросов не нужно; `lp-verdict-body` (3205), поле «Комментарий аудитора» (3217–3222). `fmtDate` (1901–1913) парсит ISO со временем. `loadQueue` (989–1029) — без изменений.
- `src/bank_audit/loophole/static/loophole.css` -- образцы: `.lp-queue-reason` (1083–1091), `.lp-queue-comment` (1102), `.lp-verdict-body` (1858), `.lp-verdict-record` (1864).
- `tests/loophole/conftest.py` -- `SCHEMA_SQL` для in-memory SQLite: `loophole_record` (42–72, без `classifier_verdict_reason`) и `loophole_preliminary_import` (265) есть; таблицы `loophole_research_candidate`/`loophole_verification_snapshot`/`loophole_verification_decision` в схеме нет — без дополнения батч-запрос очереди уронит существующие тесты.
- `tests/loophole/test_story_2_5_submission_route.py` (33–70) -- образец SQLite-адаптации candidate/snapshot/decision таблиц для тестов.
- `tests/loophole/test_queue_card_comment_and_full_text.py` -- образец контрактных текстовых проверок `_jsx()/_css()/_norm()/_block()` (сборки и JS-раннера в репозитории нет).
- `tests/loophole/test_db_schema_048.py` -- образец Greenplum-контракта миграции (без PK/UNIQUE, обязательные поля).
- `tests/loophole/test_repository.py` / `test_web.py` -- репозиторные и веб-тесты на `session`-фикстуре из conftest (`test_web.py:22` импортирует `SCHEMA_SQL`) — прогон после изменения схемы обязателен.

## Tasks & Acceptance

**Execution:**
- `migrations/068_loophole_record_classifier_comment.sql` -- создать миграцию: `ALTER TABLE loophole_record ADD COLUMN IF NOT EXISTS classifier_verdict_reason TEXT;` (стиль 066; комментарий-шапка о назначении — заморозка исходного комментария классификатора перед перезаписью ручным вердиктом). -- Сохранение авторства комментария классификатора.
- `src/bank_audit/loophole/repository.py` -- (а) `update_verdict`: в тот же UPDATE добавить `classifier_verdict_reason = COALESCE(classifier_verdict_reason, verdict_reason)` (freeze первого классификаторского текста, идемпотентно); (б) `list_verification_queue`: добавить `classifier_verdict_reason` в SELECT и после основного запроса выполнить один батч-запрос решений для собранных `record_id` (`IN (...)`, пропуск при пустом списке) по цепочке `loophole_preliminary_import → loophole_research_candidate (research_id + source_id) → loophole_verification_snapshot (candidate_id) → loophole_verification_decision (snapshot_id)`, поля `decision_id, snapshot_id, decision, comment, decided_by, decided_at, run_id`, `ORDER BY decided_at, decision_id`; сгруппировать и прикрепить каждой записи `rec["decisions"] = [...]`. -- Данные «все комментарии + авторы» одним дополнительным запросом, без N+1.
- `tests/loophole/conftest.py` -- в `SCHEMA_SQL`: колонка `classifier_verdict_reason TEXT` в `loophole_record`; SQLite-таблицы `loophole_research_candidate`, `loophole_verification_snapshot`, `loophole_verification_decision` (адаптация по образцу `test_story_2_5_submission_route.py:33–70`). -- Иначе батч-запрос очереди ломает существующие тесты.
- `src/bank_audit/loophole/static/loophole.jsx` -- (а) карточка: источник секции «Комментарий классификатора» → `queueSelected.classifier_verdict_reason ?? queueSelected.verdict_reason`; новая секция «Решения ЦК КС» (после сетки/комментария классификатора): список решений — метка типа (`vulnerability`→«Уязвимость», `fraud_scheme`→«Мошенническая схема», `not_confirmed`→«Не подтверждено»), комментарий, `decided_by`, `fmtDate(decided_at)`; пустое состояние «Решений ЦК пока нет.»; (б) модалка «Вердикт записи»: в `lp-verdict-body` перед полем «Комментарий аудитора» read-only блок: список `rec.decisions` (рендер только при `Array.isArray(rec.decisions)`) и строка «Комментарий классификатора» при `rec.verdict_model !== "manual"` и непустом (`classifier_verdict_reason ?? rec.verdict_reason`). -- Интент: все комментарии и авторы на обеих поверхностях.
- `src/bank_audit/loophole/static/loophole.css` -- стили секции решений карточки и read-only блока модалки по образцам `.lp-queue-reason` / `.lp-verdict-record` (метка решения — цветовая точка/бейдж как у `.lp-verdict-option`, опционально). -- Визуальная целостность.
- `tests/loophole/test_queue_verdict_comments_authors.py` -- новый тест: (а) контрактные проверки JSX/CSS по образцу `test_queue_card_comment_and_full_text.py` — все AC и сценарии I/O-матрицы; (б) репозиторные тесты на `session`-фикстуре: вставка import+candidate+snapshot+decision → `list_verification_queue` возвращает `decisions` у записи (все решения, порядок); второй snapshot/decision по второй импорте попадает в тот же список; `update_verdict` замораживает `classifier_verdict_reason` и перезаписывает `verdict_reason`, повторный `update_verdict` не меняет замороженное; (в) Greenplum-контракт миграции 068 по образцу `test_db_schema_048.py` (`ADD COLUMN IF NOT EXISTS`, без PK/UNIQUE). -- Прогоняемая приёмка всех трёх слоёв.

**Acceptance Criteria:**
- Given очередь загружена и выбрана запись, по которой есть решения ЦК КС, when открыта карточка проверки, then секция «Решения ЦК КС» показывает каждое решение с типом, комментарием, автором (`decided_by`) и датой (`decided_at`); при нескольких импортах записи видны все её решения.
- Given эксперт открыл из карточки модалку «Вердикт записи», when модалка отрисована, then в ней виден read-only блок со всеми решениями ЦК (тип, комментарий, автор, дата) и комментарием классификатора, а поле «Комментарий аудитора» и выбор типа работают как прежде.
- Given у записи нет решений ЦК, when открыта карточка, then секция решений показывает «Решений ЦК пока нет.» и карточка работоспособна.
- Given комментарий классификатора присутствует (`verdict_model != 'manual'`), when открыты карточка или модалка, then показан текст классификатора, а не пустая заглушка.
- Given выполняется `POST /records/verdict` по записи с классификаторским `verdict_reason`, when `update_verdict` отрабатывает, then `classifier_verdict_reason` равен прежнему тексту, `verdict_reason` — комментарий аудитора, ответ эндпоинта не изменён.
- Given модалка вердикта открыта для записи из каталога (поле `decisions` отсутствует), when модалка отрисована, then блок решений скрыт и остальное поведение модалки прежнее.

## Spec Change Log

## Review Triage Log

### 2026-09-10 — Review pass
- verdicts: 12 findings — high 0, medium 0, low 4, false 8, maybe-false 0
- findings:
  - `[low]` `[patch]` (blind-hunter, edge-case-hunter; группа) В модалке вердикта список решений `<ul className="lp-verdict-decisions-list">` не имеет CSS-правила — рендерятся дефолтные маркеры списка и отступы браузера внутри read-only блока. Проверено: правила нет в loophole.css, generic-сброса `ul` в области модалки нет; карточечный аналог `.lp-queue-decisions-list` стилизован. — Применён патч: добавлено правило `.lp-verdict-decisions-list` (сброс list-style/margin/padding, grid gap 8px — зеркало карточного) в loophole.css и ассерт `_block(css, ".lp-verdict-decisions-list")` в `test_card_and_modal_decision_blocks_have_styles`; верификация перепрогнана (18 passed; полный tests/loophole 1093 passed, 3 skipped; ruff clean).
  - `[false]` (blind-hunter) Батч-запрос решений без LIMIT. — Опровержение: выборка структурно ограничена — append-only одно решение на snapshot (049), снапшоты на кандидата, один import на source (уникальный индекс `uq_loophole_preliminary_import_source` + service-проверка `already_imported` в research_cases.py:705), очередь LIMIT 200; путь к плохому исходу не показан.
  - `[false]` (blind-hunter) Ручные плейсхолдеры `:r{i}` вместо expanding-биндпараметров. — Опровержение: тот же идиом, что в `_catalog_where` (`:b{i}`) того же файла; консистентный стиль репозитория, плохого исхода нет.
  - `[false]` (blind-hunter) Дублирование тестовых сид-хелперов с test_story_2_5_submission_route.py. — Опровержение: конвенция репозитория — самодостаточные тест-файлы (story_2_5 сеет свои); вреда/расхождения не названо.
  - `[false]` (blind-hunter) Хрупкость regex `.*?</article>` в `_queue_card()`. — Опровержение: вложенных `<article>` в карточке нет (тесты с ассертами содержимого после точки риска проходят); при появлении тест упадёт громко, а не тихо.
  - `[low]` `[reject]` (blind-hunter; группа с V1) Нет HTTP-теста непустого /queue с decisions. — Отклонено: ближайшая наблюдаемая граница изменённого кода — функция репозитория (6 новых репозиторных тестов), веб-слой — pass-through, HTTP покрыт для пустой очереди (test_authorization.py:165–173) и ролей (test_admin_roles_audit.py:448–460); дублирование на HTTP — навязанный пирамидой слой, не способ верификации этого репозитория.
  - `[false]` (blind-hunter) `color-mix` без фолбэка. — Опровержение: паттерн уже использовался до baseline (1 правило в 67caf2b), деградация ограничена оттенком фона бейджа.
  - `[false]` (blind-hunter) Докстринг `update_verdict` «повторные вызовы ничего не меняют» можно прочитать как полную идемпотентность. — Опровержение: фраза в контексте продолжает клаузу COALESCE-freeze; реальная семантика закреплена тестами (`test_update_verdict_freezes_classifier_comment`, `..._is_idempotent_for_legacy_records`).
  - `[false]` (blind-hunter) Заголовок блока решений модалки — `<div>` вместо `<h3>`. — Опровержение: идиом модалки — label/div (lp-verdict-field), карточки — секции с h3; консистентно в пределах каждой поверхности.
  - `[false]` (edge-case-hunter, path trace) Дублирующие import-строки на (record, research_id, source_id) размножали бы решения. — Опровержение: PG-уникальный индекс по `source_id` + service-проверка `already_imported` (research_cases.py:705–710); каждое решение достижимо ровно по одной цепочке import → candidate → snapshot → decision.
  - `[low]` `[reject]` (verification-gap, Other findings) Связка деплоя: до применения миграции 068 `GET /queue` падает на неизвестной колонке. — Отклонено: устоявшаяся модель деплоя репозитория применяет миграции (setup применяет glob migrations/*.sql, `RUN_MIGRATIONS=1` в lifespan); идентичная связка у всех колонко-добавляющих изменений (например 066, читаемая каталогом безусловно); защитный SQL-фолбэк — добавленная сложность для состояния, недостижимого при штатном деплое.
  - `[low]` `[reject]` (verification-gap, Other findings) Enrichment непустой очереди не ассертится через HTTP. — Отклонено: тот же довод, что и HTTP-строкой выше: изменённое поведение покрыто на ближайшей границе (репозиторий), HTTP-слой изменений не имеет.

## Design Notes

- **Развилка 1 (источник «всех комментариев»).** Выбрано обогащение ответа `GET /queue` (батч-второй-запрос по `record_id` из уже выбранных строк; лимит очереди 200). Ленивый эндпоинт истории отклонён: данные малы (append-only, одно решение на snapshot), а ленивая загрузка добавила бы фронту новое async-состояние и кэш ради одного экрана. `GET /queue` остаётся единственной поверхностной загрузкой очереди.
- **Развилка 2 (что показывать в модалке).** Read-only блок (решения ЦК + комментарий классификатора) перед полем ввода; данные уже в `rec` (`verdictModal.record === queueSelected`), новых запросов нет. Видимость блока — по `Array.isArray(rec.decisions)`: у записей очереди массив есть всегда (возможно пустой), у каталожных записей поля нет → блок скрыт, каталог не затронут.
- **Развилка 3 (перезапись `verdict_reason`).** Freeze-колонка `classifier_verdict_reason` с `COALESCE` в том же UPDATE — минимальное изменение без backfill. Таблица истории комментариев отклонена: на поверхности очереди не наблюдаема (записи с `verdict_model='manual'` покидают очередь фильтром `list_verification_queue`), отдельная персистентность создала бы второй источник истины без требования интента. Epic-3.1 не затронута (snapshot/decision/decide_snapshot без изменений).
- Форма элемента `decisions` в ответе очереди:
  ```json
  {"decision_id": 1, "snapshot_id": 7, "decision": "vulnerability",
   "comment": "Подтверждено: …", "decided_by": "ivanov",
   "decided_at": "2026-09-10T12:30:00+03:00", "run_id": "run-1"}
  ```
- Остаточный риск: записи, вручную размеченные до применения миграции 068, теряют исходный комментарий классификатора безвозвратно (`classifier_verdict_reason IS NULL`) — fallback на текущий `verdict_reason`. На очередь такие записи не попадают (фильтр `verdict_model != 'manual'`).

## Verification

**Commands:**
- `python -m pytest tests/loophole/test_queue_verdict_comments_authors.py` -- expected: все тесты нового файла проходят.
- `python -m pytest tests/loophole` -- expected: без регрессий (включая `test_web.py`, `test_repository.py`, `test_queue_card_comment_and_full_text.py`, `test_story_2_5_submission_route.py`).
- `python -m ruff check src/bank_audit/loophole/repository.py tests/loophole/conftest.py tests/loophole/test_queue_verdict_comments_authors.py` -- expected: clean.

**Manual checks (if no CLI):**
- Под экспертом ЦК КС открыть «Очередь верификации»: в карточке видны «Комментарий классификатора» и «Решения ЦК КС» (тип, комментарий, автор, дата); в модалке «Вердикт записи» — read-only блок с теми же данными перед полем «Комментарий аудитора».
- Поставить вердикт с комментарием и проверить в БД: `classifier_verdict_reason` = исходный текст классификатора, `verdict_reason` = комментарий аудитора.

## Auto Run Result

Status: done

**Summary of implemented change.** `GET /queue` обогащает каждую запись списком решений ЦК КС (`rec["decisions"]`: `decision_id, snapshot_id, decision, comment, decided_by, decided_at, run_id`) одним батч-запросом по канонической цепочке `loophole_preliminary_import → loophole_research_candidate → loophole_verification_snapshot → loophole_verification_decision` (порядок `decided_at, decision_id`); `update_verdict` замораживает первый классификаторский комментарий в новой колонке `classifier_verdict_reason` (миграция 068, `COALESCE` в том же UPDATE); карточка очереди показывает секцию «Решения ЦК КС» и берёт комментарий классификатора из freeze-колонки с fallback на `verdict_reason`; модалка «Вердикт записи» получает read-only блок решений (виден только при `Array.isArray(rec.decisions)` — каталог не затронут) и строку комментария классификатора перед полем «Комментарий аудитора». Контракт `/queue` расширен аддитивно, роль-гейт `ccks_expert` и ответ `POST /records/verdict` не изменены; `raw_text` не отдаётся.

**Files changed:**
- `migrations/068_loophole_record_classifier_comment.sql` — новая миграция: `ADD COLUMN IF NOT EXISTS classifier_verdict_reason TEXT` (стиль 066, применена к живой БД, идемпотентна).
- `src/bank_audit/loophole/repository.py` — `update_verdict`: freeze через `COALESCE`; `_verification_decisions_by_record`: батч-джойн решений (без N+1); `list_verification_queue`: `classifier_verdict_reason` в SELECT, прикрепление `decisions`.
- `src/bank_audit/loophole/static/loophole.jsx` — метки решений `decisionLabel`; секция «Решения ЦК КС» в карточке; read-only блок в модалке перед «Комментарий аудитора»; источник комментария классификатора — `classifier_verdict_reason ?? verdict_reason`.
- `src/bank_audit/loophole/static/loophole.css` — стили секции решений карточки, read-only блока модалки, бейджей типов решений; (review-patch) правило `.lp-verdict-decisions-list`.
- `tests/loophole/conftest.py` — SQLite-схема: колонка `classifier_verdict_reason`, таблицы `loophole_research_candidate` / `loophole_verification_snapshot` / `loophole_verification_decision`.
- `tests/loophole/test_queue_verdict_comments_authors.py` — новый файл, 18 тестов (репозиторий, JSX/CSS-контракты, контракт миграции 068); (review-patch) ассерт `.lp-verdict-decisions-list`.
- `tests/loophole/test_story_2_2_research_cases.py` — `CREATE TABLE IF NOT EXISTS` (совместимость с глобальной схемой conftest; прецедент — story_2_5).

**Review findings breakdown:** 12 находок от 4 слоёв: high 0, medium 0, low 4, false 8. Запатчено: 1 группа (low — отсутствовавшее CSS-правило `.lp-verdict-decisions-list` для списка решений модалки; патч применён на review-этапе, верификация перепройдена). Отложено (deferred): 0. Отклонено: 11 — 8 `false` по опровержениям и 3 `low` (HTTP-дублирование покрытия на ближайшей границе, уже покрытой репозиторными тестами; связка деплоя «код после миграции» — устоявшаяся модель репозитория). Полные вердикты с доказательствами — в `## Review Triage Log`.

**Follow-up review recommendation:** false. Запатчеванных записей: 1 с вердиктом `low` (порог — `high` либо две `medium` — не достигнут); конкретного непроверенного риска назвать нельзя: единственная patch-группа закрыта ассертом в контрактном тесте и перепрогоном всей верификации.

**Verification performed:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_queue_verdict_comments_authors.py -q` — 18 passed (после review-patch).
- `.venv/Scripts/python.exe -m pytest tests/loophole -q` — 1093 passed, 3 skipped (после review-patch; первый прогон дал 1 флейк `test_agent_stability.py::test_subagent_classifier_transient_error_retried_and_labels_parsed` — проходит изолированно, в паре с предшествующим файлом и в повторном полном прогоне; к изменению отношения не имеет).
- `.venv/Scripts/python.exe -m ruff check src/bank_audit/loophole/repository.py tests/loophole/conftest.py tests/loophole/test_queue_verdict_comments_authors.py` — clean.
- Ручная инспекция review-слоёв: цепочка джойнов совпадает с `_catalog_where`; дублирование решений исключено (уникальный индекс `uq_loophole_preliminary_import_source` + service-проверка `already_imported`); freeze-semantics `COALESCE` в SET видят старые значения строки — идемпотентно, legacy NULL покрыт fallback; единственный UPDATE `verdict_reason` в бэкенде — `update_verdict` (обходных путей freeze нет); JSX-интерполяции экранируются React (файл отдаётся как `text/babel`), `className`-интерполяция ограничена CHECK-констрейнтом решения; N+1 нет (пин-тест: ровно 2 SELECT на очередь).

**Residual risks:**
- Записи, размеченные вручную до применения миграции 068, теряют исходный комментарий классификатора безвозвратно (freeze NULL, fallback на текущий `verdict_reason`) — задокументировано в Design Notes; на очередь такие записи не попадают.
- До применения миграции 068 `GET /queue` вернёт 500 на неизвестной колонке — стандартная для репозитория связка «код после миграций» (`RUN_MIGRATIONS=1` / setup-glob).
- HTTP-проверка `/queue` с непустой очередью на уровне веб-слоя отсутствует (изменённое поведение покрыто на границе репозитория; веб-слой — pass-through).

