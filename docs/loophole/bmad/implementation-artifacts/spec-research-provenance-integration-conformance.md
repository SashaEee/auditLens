---
title: 'Сквозная целостность исследования, доказательств и общей базы'
type: 'bugfix'
created: '2026-08-31'
status: 'in-progress'
review_loop_iteration: 0
baseline_commit: '40beb1c6cd8522603bfdc36b9edab8439f654812'
context:
  - 'docs/project-context.md'
  - 'docs/loophole/bmad/implementation-artifacts/spec-preliminary-research-source-import.md'
  - 'docs/loophole/bmad/implementation-artifacts/spec-research-report-markdown-and-export.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Проблема:** завершённый managed-запуск сохраняет находки в общий каталог напрямую, но не
создаёт связанное исследование и источники. Поэтому кнопка явного переноса, provenance и
непустые доказательства PDF/Word не работают на настоящем пути; фильтр «Верифицировано ЦК»
ошибочно считает достаточным технический статус `published`.

**Подход:** сервер после успешного запуска фиксирует только проверенные чтением источники,
дату публикации при наличии и кандидаты данного `run_id`. Отчёт получает неизменяемый снимок
этих источников, а аналитик отдельной кнопкой переносит новые подозрения в общую базу как
«Предварительно». Каталог различает положительное решение ЦК КС и ещё не рассмотренные записи.

## Boundaries & Constraints

**Always:** сохранять источники и доказательства только server-side в workspace текущего
пользователя; не считать SERP-сниппет доказательством; не подменять неизвестную дату
публикации датой сбора; оставлять model tools read-only; не публиковать и не переносить запись
без явной кнопки аналитика; определять «Верифицировано ЦК» только положительным append-only
решением ЦК КС; экранировать результат и источники при Markdown/PDF/Word.

**Ask First:** менять права ЦК КС, форматы кроме PDF/Word, схему финальной публикации или
автоматизировать перенос источников.

**Never:** не записывать `loophole_record` из managed agent до явного переноса, не раскрывать
чужой report/research, не использовать клиентский payload как источник истины, не менять
Telegram-контур и не создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Завершённое исследование | Есть успешно прочитанные URL | Один research-run со снимком URL, текста и `published_at` при наличии; отчёт содержит его evidence | Неуспешный fetch не создаёт кандидата и честно фиксируется как ограничение |
| Явный перенос | В отчёте есть новые подозрительные источники | Создаются только `preliminary` записи с вероятностью, provenance и датой публикации при наличии | Повтор идемпотентен; дубль/чужой workspace не раскрывает данные |
| Фильтр ЦК КС | Есть положительное, отрицательное и отсутствующее решения | «Верифицировано ЦК» показывает только положительное; «Ожидает» — нерассмотренные подозрения | Отсутствие решения не маскируется под отказ |
| Скачивание PDF | PDF renderer отвечает 503 | Карточка сохраняет результат и предлагает Word или повтор PDF | Понятное доступное сообщение без ложного download |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/agent/__init__.py`, `chat/tools_nanobot.py`, `chat/graph.py` — доверенный контекст fetched sources и post-run persistence.
- `migrations/061_*`, `research_cases.py`, `web.py` — дата публикации research-source, immutable evidence и ownership/export/import routes.
- `repository.py`, `static/loophole.jsx` — корректный join решений ЦК КС, provenance, честные подписи вероятности и PDF fallback.
- `tests/loophole/test_{tools_nanobot,nanobot_graph,preliminary_research_source_import,research_report_export,final_layout_runtime}.py` — RED/GREEN и browser-runtime контракты.

## Tasks & Acceptance

**Execution:**
- [ ] Добавить RED-сценарий managed agent → research/source/candidate → report → explicit import; затем сохранить его server-side без прямой записи в каталог.
- [ ] Расширить research source реальной `published_at`, создать неизменяемый evidence snapshot текущего run и покрыть PDF/DOCX непустым источником.
- [ ] Исправить каталог: положительное решение ЦК КС, а не только `published`, определяет verified; показать provenance и «Предварительная вероятность» без графы «Доверие».
- [ ] Заменить прямые ссылки экспорта на download-flow с typed PDF fallback; устранить дублированный заголовок заявки и устаревший browser-contract каталога.
- [ ] Обновить traceability/checklist и status только после полного зелёного прогона.

**Acceptance Criteria:**
- Given агент нашёл и прочитал источник, when исследование завершено, then источник и его дата публикации связаны с одним `run_id`, а обычный каталог не меняется до кнопки переноса.
- Given аналитик переносит источники, when открывает «Общую базу», then видит preliminary badge, вероятность и provenance; повтор не добавляет дубликат.
- Given запись не имеет положительного решения ЦК КС, when выбран «Верифицировано ЦК», then она отсутствует независимо от технического статуса публикации.
- Given PDF недоступен, when пользователь выбирает PDF, then остаётся результат, показана причина и доступны Word/повтор.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_tools_nanobot.py tests/loophole/test_nanobot_graph.py tests/loophole/test_preliminary_research_source_import.py tests/loophole/test_research_report_export.py -q -p no:cacheprovider --basetemp <new-owned-dir>` — новый сквозной серверный контракт PASS.
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_final_layout_runtime.py -q -p no:cacheprovider --basetemp <new-owned-dir>` — Browser-runtime PASS.
- `.venv/Scripts/python.exe -m pytest tests/loophole -q -p no:cacheprovider --basetemp <new-owned-dir>` и `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole` — без регрессий.
- Встроенный Browser: light/dark, реальный report с источником, PDF error/Word, import, all/verified/pending, console без ошибок.
