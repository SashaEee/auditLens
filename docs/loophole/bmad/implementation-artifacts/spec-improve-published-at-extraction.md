---
title: 'Даты публикации из поста попадают в published_at и не теряются при сохранении'
type: 'bugfix'
created: '2026-09-10'
status: 'done'
baseline_revision: c63e0d65d03ae27ac9628ec7508cdd41b2e62847
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
deferred:
  - summary: >-
      save_loophole (chat/tools_nanobot.py) не передаёт вычисленную fetch_and_parse
      дату в LoopholeRecord — записи агентского пути остаются с published_at=NULL.
    evidence: |-
      Серверный дозагруз в save_loophole идёт через content_fetch.fetch_full_content
      (tools_nanobot.py:655), который возвращает только текст и отбрасывает
      page.published_at; LoopholeRecord (tools_nanobot.py:668) создаётся без
      published_at. Поверхность существовала до изменения и диффом не менялась;
      tools_nanobot.py заморожен из-за пересечения с in-progress
      spec-search-strategy-100-pages. У агентского пути есть альтернативный канал
      дат: web_fetch → fetched_sources → persist_managed_run →
      import_preliminary_sources.
    location: >-
      src/bank_audit/loophole/chat/tools_nanobot.py:631-695
    severity: medium
  - summary: >-
      /records/backfill-content (web.py) дозагружает контент записи, но не
      заполняет её published_at, хотя дата при этом вычисляется.
    evidence: |-
      web.py:522 вызывает content_fetch.fetch_full_content, который теряет
      page.published_at; запись обновляется контентом, но не датой. Тот же корень,
      что и save_loophole: контракт fetch_full_content возвращает FullContent без
      даты. Поверхность вне Approach intent-contract и существовала до изменения.
    location: >-
      src/bank_audit/loophole/web.py:522
    severity: medium
---

<intent-contract>

## Intent

**Problem:** `published_at` в `loophole_record` и `loophole_research_source` почти всегда NULL: точной считается только tz-aware метка из meta/JSON-LD/`<time datetime>`. Наивные даты разметки отбрасываются, видимые даты поста («9 сентября 2026», дд.мм.гггг) уходят лишь в `estimated_published_at`, а collector и triaged-источники не передают вычисленную дату при сохранении — фильтры каталога «Дата публикации» показывают «дата не установлена».

**Approach:** расширить извлечение в одной точке — `fetch_decorator` (наивная разметка → якорь UTC; видимая дата текста поста → полночь UTC; приоритет tz-aware сохраняется) — и прокинуть уже вычисленную дату в сохранение: `page.published_at` в collector, текстовую дату сниппета — в triaged-источники research_cases.

## Boundaries & Constraints

**Always:**
- `published_at` — только дата самого поста: разметка или видимый текст. Приоритет: tz-aware разметка → наивная разметка (UTC) → видимая дата текста (полночь UTC).
- Некорректная строка или дата в будущем → `published_at=None`; сохранение не падает из-за даты.
- `estimated_published_at` (URL+текст) и fail-closed период-гейт агента сохраняют семантику.

**Never:**
- Не менять схему БД (колонки есть: миграции 058, 061); без миграций и backfill.
- Не повышать оценку из URL до `published_at`; не подменять `published_at` датой сбора.
- Не менять логику раундов/бюджета в `agent/__init__.py` и `tools_nanobot.py` (пересечение со in-progress спекой spec-search-strategy-100-pages).

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Наивная дата в разметке | meta/`datePublished`/`<time>` = «2026-08-27T09:25:00» или «2026-08-27» без пояса | `published_at = "2026-08-27T09:25:00+00:00"` | Нет ошибки |
| Видимая дата в тексте | «Опубликовано 9 сентября 2026 года», разметки дат нет | `published_at = "2026-09-09T00:00:00+00:00"`, `estimated = "2026-09-09"` | Нет ошибки |
| Дата только в URL | `/2026/09/09/post`, в разметке/тексте дат нет | `published_at is None`, `estimated = "2026-09-09"` | Нет ошибки |
| Приоритет источников | tz-aware meta + другая дата в тексте | `published_at` = tz-aware из разметки | Нет ошибки |
| Битая строка / дата в будущем | «неизвестно» в разметке или сниппете; «1 января 2099» в тексте | `published_at = None` | Молча (`_plausible` / нормализация) |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/adapters/fetch_decorator.py` -- точка изменения №1. `_PUBLISHED_AT_RES` (54–69) — паттерны разметки; `_exact_published_at` (106–126) — сейчас отбрасывает наивные (`tzinfo is None → continue`, строка 123); `_date_from_text` (217–224) — маркер-якорная дата из текста (русские месяцы, дд.мм.гггг, ISO; `_plausible` режет будущее); `estimate_published_date` (227–237) — контракт не менять; сборка полей в `fetch_and_parse` (298, 308–309).
- `src/bank_audit/loophole/collector.py` -- точка изменения №2: `LoopholeRecord` создаётся без `published_at` (105–118); `page` доступна (81).
- `src/bank_audit/loophole/research_cases.py` -- точка изменения №3: triaged-ветка `persist_managed_run` (214–246) зовёт `record_source` без `published_at`; fetched-ветка (171–177) уже пропагирует `source.published_at` — после задачи 1 покрывается автоматически. `_normalize_published_at` (21–38) толерантна к битым. `import_preliminary_sources` (645–754) уже переносит дату источника в запись каталога — без правок.
- `src/bank_audit/loophole/repository.py` -- read-only: `insert_record` (115–151) уже пишет `published_at`; период-фильтры (306–311, 455–460) начнут видеть заполненные даты без правок.
- `src/bank_audit/loophole/chat/tools_nanobot.py` и `src/bank_audit/loophole/agent/__init__.py` -- без правок: `web_fetch` (326–343), `fetched_sources`/`pending_records` несут обе даты (246–268, 464–486; 94, 179–180); период-гейт `_source_publication_period_error` (213–243) — семантику не менять.
- `src/bank_audit/loophole/static/loophole.jsx` -- поверхность UI: `fmtDate(r.published_at)` (2399, 2783, 2801, 3244) — «дата не установлена» исчезает при заполнении; без правок.
- `migrations/058_loophole_publication_date.sql`, `061_loophole_research_source_publication.sql` -- контракт nullable TIMESTAMPTZ; не менять. 067/068 — контекст, не затрагиваются.
- `tests/loophole/test_fetch_decorator.py` -- пины к намеренному обновлению: 164–178 (наивная → None) и 213–231 (текст → published_at None); URL-пин 195–210 остаётся зелёным (фиксирует «URL не повышается»).
- `tests/loophole/test_collector.py` -- образец нового теста: моки `search_impl`/`fetch_impl`, `sqlite_session`.
- `tests/loophole/test_auto_catalog_import.py` -- triaged-persist тесты (362+), фикстура `_create_import_schema`, тест нормализации дат (199–247).
- `tests/loophole/conftest.py` -- `sqlite_session`; `published_at` в тестовой схеме TEXT — ISO-строки пишутся как есть.

## Tasks & Acceptance

**Execution:**
- [x] `src/bank_audit/loophole/adapters/fetch_decorator.py` -- (а) в `_exact_published_at` заменить отбрасывание наивных значений на якорь UTC: `parsed.replace(tzinfo=timezone.utc)`, вернуть ISO; (б) добавить публичный `published_date_from_text(text: str) -> str | None` — обёртка `_date_from_text`, только текстовая дата «YYYY-MM-DD», URL не участвует; (в) в `fetch_and_parse` fallback: если `_exact_published_at(content)` вернул None — `published_at` = «YYYY-MM-DDT00:00:00+00:00» из текстовой даты (helper `_text_published_at(text)`); `estimated_published_at` считать как раньше. -- Единая точка правды: покрывает collector, `web_fetch` и fetched-источники сразу.
- [x] `src/bank_audit/loophole/collector.py` -- при создании `LoopholeRecord` передать `published_at=page.published_at if page is not None else None`. -- Главный источник NULL-дат: даты вычислены, но терялись при сохранении.
- [x] `src/bank_audit/loophole/research_cases.py` -- в triaged-ветке `persist_managed_run` передать `record_source` `published_at=fetch_decorator.published_date_from_text(snippet)` (`from .adapters import fetch_decorator`; цикла нет — tools_nanobot уже так импортирует). -- Triaged-сниппет — фрагмент поста; дата не должна теряться.
- [x] `tests/loophole/test_fetch_decorator.py` -- обновить пины 164–178 (наивная → «2026-08-27T00:00:00+00:00») и 213–231 (текст → published_at заполнен, estimated сохранён); добавить кейсы I/O-матрицы: URL-only → None; tz-aware приоритетнее текста; битая строка → None; дата в будущем → None. -- Фиксация нового контракта.
- [x] `tests/loophole/test_collector.py` -- новый тест: `collect_once` со страницей с видимой датой → запись в `loophole_record` с `published_at` NOT NULL; страница без дат и `page=None` → NULL.
- [x] `tests/loophole/test_auto_catalog_import.py` -- новый тест: `persist_managed_run` с triaged-сниппетом «Опубликовано 5 августа 2026 …» → `loophole_research_source.published_at` начинается с «2026-08-05»; сниппет без/с битой датой → NULL.

**Acceptance Criteria:**
- Given HTML с наивной датой в разметке («2026-08-27T09:25:00»), when `fetch_and_parse`, then `page.published_at == "2026-08-27T09:25:00+00:00"`.
- Given HTML без разметки дат и текст «Опубликовано 9 сентября 2026 года», when `fetch_and_parse`, then `page.published_at == "2026-09-09T00:00:00+00:00"` и `page.estimated_published_at == "2026-09-09"`.
- Given страница с датой только в URL (`/2026/09/09/`), when `fetch_and_parse`, then `page.published_at is None`, `page.estimated_published_at == "2026-09-09"`.
- Given tz-aware meta «2026-08-27T09:25:00+03:00» и другая дата в тексте, when `fetch_and_parse`, then `page.published_at == "2026-08-27T09:25:00+03:00"`.
- Given `collect_once` со страницей с видимой датой, when сбор завершён, then `repo.get_record(record_id)["published_at"] is not None`; для страницы без дат — NULL.
- Given `persist_managed_run` с triaged-сниппетом «Опубликовано 5 августа 2026 …», when персистенция выполнена, then `loophole_research_source.published_at` начинается с «2026-08-05», и существующий `import_preliminary_sources` переносит её в `loophole_record.published_at`.
- Given некорректная строка даты в разметке или сниппете, when `fetch_and_parse`/`persist_managed_run`, then `published_at = None`, без исключений.
- Given запущенные `tests/loophole/test_tools_nanobot.py` и `tests/loophole/test_agent_stability.py`, when регресс, then все зелёные (период-гейт и estimated-пины без изменения семантики).

## Spec Change Log

- 2026-09-10 (implement): контрактовых отклонений нет. Тестовые уточнения: (1) в `tests/loophole/test_collector.py` ожидание даты — префикс «2026-09-09», а не точная ISO-строка: `LoopholeRecord` — Pydantic-модель и приводит ISO-строку к `datetime`, SQLite-колонка TEXT хранит значение вида «2026-09-09 00:00:00+00:00» (пробел вместо «T»); семантика acceptance (`published_at` NOT NULL, дата поста) соблюдена. (2) Новые collector-тесты вызывают `search_decorator.clear_cache()` перед прогоном: process-level кэш поиска по ключу (query, фильтры, max_results, region) без сброса протягивает результаты соседних тестов того же файла.

## Review Triage Log

### 2026-09-10 — Review pass
- verdicts: 7 findings — high 0, medium 2, low 5, false 0, maybe-false 0
- findings:
  - `[medium]` `[defer]` save_loophole не передаёт вычисленную дату в запись каталога — подтверждено кодом: fetch_full_content (tools_nanobot.py:655) возвращает только текст, LoopholeRecord (tools_nanobot.py:668) создаётся без published_at; поверхность не названа в Approach intent-contract, существовала до изменения, tools_nanobot.py заморожен пересечением со spec-search-strategy-100-pages — записано в deferred.
  - `[medium]` `[defer]` backfill-content не заполняет published_at при дозагрузке — тот же корень (контракт content_fetch.fetch_full_content теряет page.published_at), web.py:522 обновляет контент записи, но не дату; вне Approach intent-contract, pre-existing — записано в deferred.
  - `[low]` `[reject]` tz-aware дата из будущего в разметке не отбрасывается _plausible — pre-existing семантика «tz-aware возвращается как есть» (диффом не менялась); I/O-матрица требует «будущее → None» только для текста, и это покрыто тестом test_fetch_and_parse_future_text_date_yields_none; встречаемость редка, фикс меняет задокументированную семантику период-гейта.
  - `[low]` `[reject]` повторное декодирование/regex-проход по контенту в fetch_and_parse — negligible: один лишний regex-проход на фоне сетевого fetch, users/developers не встречают.
  - `[low]` `[reject]` дублирование published_date_from_text/_text_published_at — две обёртки _date_from_text с разной гранулярностью (date-only для triaged vs полночь UTC для fallback), названный вред (какой caller разойдётся) не указан.
  - `[low]` `[reject]` старые collector-тесты не сбрасывают search-кэш — pre-existing (так жили до диффа), порядок выполнения детерминирован, новые тесты самозащищены clear_cache (Spec Change Log п. 2).
  - `[low]` `[reject]` гипотетические даты с годом <1000 из разметки — вред не показан достижимым; вход нереалистичен для паттернов _PUBLISHED_AT_RES и не ломает сохранение.

## Design Notes

- **UTC-якорь:** детерминированно, без зависимостей (`datetime.timezone.utc`); календарный день сохраняется, дневная гранулярность период-фильтров не искажается.
- **Ревизия контракта:** прежнее «только tz-aware» (STATUS_RESTART, жёсткий publication period; пин 164) сознательно пересматривается по intent. Миграция 058 не нарушается: колонка остаётся nullable TIMESTAMPTZ, подмены датой сбора нет.
- **Запись из publish_decision** (публикация верифицированного кейса) остаётся без published_at: у неё нет первоисточника-URL, подставлять нечего — вне scope.
- **Пересечение со spec-search-strategy-100-pages (in-progress):** там меняются `agent/__init__.py` и tools-контур в части раундов поиска; здесь эти файлы read-only, имена символов не переименовывать.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_fetch_decorator.py tests/loophole/test_collector.py tests/loophole/test_auto_catalog_import.py -q -p no:cacheprovider` -- expected: зелёные, включая новые и два переписанных пина.
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_tools_nanobot.py tests/loophole/test_agent_stability.py -q -p no:cacheprovider` -- expected: без регрессий.

## Auto Run Result

**Summary:** `published_at` перестал быть почти всегда NULL: в единой точке `fetch_and_parse` наивная разметка якорится к UTC, видимая дата текста поста даёт полночь UTC (приоритет tz-aware сохранён), а уже вычисленная дата прокинута в сохранение — `page.published_at` в collector и текстовая дата сниппета в triaged-источники research_cases (fetched-ветка пропагировала дату и раньше). Схема БД, миграции, `agent/__init__.py` и `tools_nanobot.py` не тронуты.

**Files changed:**
- `src/bank_audit/loophole/adapters/fetch_decorator.py` — наивные даты разметки → UTC-якорь в `_exact_published_at`; публичный `published_date_from_text`; fallback `_text_published_at` (полночь UTC) в `fetch_and_parse`.
- `src/bank_audit/loophole/collector.py` — `LoopholeRecord` получает `published_at=page.published_at` (NULL при неуспешном fetch).
- `src/bank_audit/loophole/research_cases.py` — triaged-ветка `persist_managed_run` передаёт `record_source` дату из сниппета.
- `tests/loophole/test_fetch_decorator.py` — переписаны 2 пина (наивная → UTC-якорь, текст → published_at заполнен), добавлены 4 кейса I/O-матрицы (tz-aware datetime, приоритет tz-aware над текстом, битая строка, будущее).
- `tests/loophole/test_collector.py` — 2 новых теста: видимая дата доходит до `loophole_record` (NOT NULL, префикс даты поста); без дат / неуспешный fetch → NULL.
- `tests/loophole/test_auto_catalog_import.py` — новый тест: triaged-сниппет с видимой датой сохраняется в `loophole_research_source` и переносится `import_preliminary_sources` в каталог; без/с битой датой → NULL.

**Review findings breakdown:** патчей не применялось (0 high/medium/low ушли в код); deferred — 2 (обе medium, вне Approach intent-contract и pre-existing: save_loophole и backfill-content не адаптируют вычисляемую дату — см. `deferred` в frontmatter и Review Triage Log); отклонено — 5 low с обоснованиями в Review Triage Log. Follow-up review: не рекомендуется (patch-записей нет; осмысленный unverified-риск не назван).

**Verification performed:**
- Фокус: `.venv/Scripts/python.exe -m pytest tests/loophole/test_fetch_decorator.py tests/loophole/test_collector.py tests/loophole/test_auto_catalog_import.py -q -p no:cacheprovider` — 39 passed.
- Полный: `.venv/Scripts/python.exe -m pytest tests/loophole -q -p no:cacheprovider` — 1100 passed, 3 skipped (включая `test_tools_nanobot.py` и `test_agent_stability.py` — AC-8 зелёный).
- `ruff check` на всех 6 изменённых файлах — All checks passed.
- Ограничения: `git diff c63e0d65 --name-only` не содержит `agent/__init__.py`, `tools_nanobot.py`, миграций; новых py-файлов нет (`from __future__ import annotations` присутствует во всех изменённых), `import *` нет, комментарии/docstrings на русском.

**Residual risks:** записи, создаваемые через `save_loophole` и дозагружаемые через `/records/backfill-content`, продолжают писаться без `published_at` (deferred, medium) — фильтр «Дата публикации» для них по-прежнему пуст до устранения долга; у агентского пути даты попадают в каталог альтернативно через fetched_sources → import_preliminary_sources.
