---
title: 'Читаемый отчёт AI-исследования и экспорт доказательств'
type: 'feature'
created: '2026-08-31'
status: 'in-progress'
review_loop_iteration: 0
baseline_commit: '0e17177799d123ebc01995bc743cdffbfe8357ed'
context:
  - 'docs/project-context.md'
  - 'docs/loophole/bmad/implementation-artifacts/STATUS_RESTART.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Проблема:** итог AI-исследования в блоке «Доказательства и источники» показывается одним
текстовым полотном. Пользователь не может прочитать структуру Markdown и не может забрать
результат исследования вместе с подтверждёнными доказательствами в удобном документе.

**Подход:** отобразить итог безопасным ограниченным Markdown с отдельными абзацами и блоками,
а в той же карточке дать выбор скачивания отчёта в PDF или Word. Экспорт строится сервером из
разрешённого исследования, его исходного запроса, результата и проверенных snapshots источников.

## Boundaries & Constraints

**Always:** сохранять server-side ownership и workspace-изоляцию; не доверять HTML/Markdown из
LLM, URL или браузера; экранировать пользовательский и извлечённый текст; не выдавать
неподтверждённые материалы за доказательства; оставлять расчёты и факты детерминированными;
использовать русские подписи, доступные кнопки и существующие токены интерфейса.

**Ask First:** расширять права доступа, менять жизненный цикл case/evidence, добавлять внешнюю
интеграцию или deployment; добавлять формат помимо PDF и Word.

**Never:** не рендерить произвольный HTML, не экспортировать клиентские tool events как источник
истины, не использовать записи другого workspace, не подменять отсутствие доказательств
выдуманными источниками, не менять экспорт опубликованного каталога и не создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Читаемый итог | AI-ответ с заголовками, списком и абзацами | Карточка выводит семантические блоки и сохраняет переносы строк | Неизвестная разметка остаётся безопасным текстом |
| Скачивание | Авторизованный завершённый отчёт, формат PDF или Word | Скачивается документ с запросом, итогом и проверенными источниками | Кнопка сообщает понятную ошибку без потери результата |
| Нет evidence snapshot | Отчёт есть, подтверждённых snapshots нет | Документ содержит честную отметку об отсутствии проверенных доказательств | Не добавляет ссылки из UI или предположения |
| Чужой/неизвестный отчёт | Другой workspace или отсутствующий объект | Экспорт не раскрывает содержимое | 403/404 без деталей чужого исследования |
| PDF renderer недоступен | Playwright не сформировал файл | Пользователь видит предложение выбрать Word или повторить PDF | Типизированный 503, audit события сохраняются |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/static/loophole.jsx` — `LoopholeApp`, блок
  `.lp-research-evidence` около строки 1964 и sidebar messages: заменить plain text на
  безопасный Markdown-компонент; добавить доступное меню «Скачать исследование» и download-flow.
- `src/bank_audit/loophole/web.py` — chat history/ownership и router export около строк 688–847:
  добавить отдельный read-only endpoint отчёта исследования, не смешивая его с catalog `/export/*`.
- `src/bank_audit/loophole/research_cases.py` — `ResearchCaseService.submit_for_verification()`
  создаёт immutable `case_snapshot`/`evidence_snapshot`; использовать его как единственный
  источник доказательств для экспорта.
- `src/bank_audit/loophole/pdf_export.py` — существующий PDF экспортирует каталог и вставляет
  поля без HTML escaping; не переиспользовать `_record_html()` для LLM/evidence. Создать
  отдельный безопасный renderer отчёта исследования или выделенный exporter.
- `pyproject.toml` — `python-docx` есть в lock, но не declared runtime dependency; добавить
  прямую зависимость только для требуемого Word-экспорта.
- `tests/loophole/test_final_layout_contract.py`,
  `tests/loophole/test_final_layout_runtime.py` — contracts/Browser-runtime для Markdown,
  форматов и download UI; `tests/loophole/test_story_4_2_filtered_export.py` — образец
  типизированного PDF-failure и audit-проверок.

## Tasks & Acceptance

**Execution:**

- [ ] `src/bank_audit/loophole/...` — дополнить серверный service и авторизованные endpoints
  PDF/DOCX отчёта каноническим immutable snapshot с реальными проверенными evidence URL/текстом.
- [x] `src/bank_audit/loophole/static/loophole.jsx` и CSS — отрисовать safe Markdown, показать
  отдельные абзацы/списки и добавить выбор формата непосредственно в «Доказательства и источники».
- [x] `pyproject.toml` — объявить требуемую runtime-зависимость Word без новых фронтенд-сборок.
- [ ] `tests/loophole/...` — зафиксировать RED/GREEN для непустого immutable evidence snapshot,
  XSS, ownership, PDF/DOCX и PDF fallback.
- [ ] `docs/loophole/bmad/implementation-artifacts/STATUS_RESTART.md` и `design-qa.md` —
  зафиксировать реальную in-app Browser-проверку готового результата и download.

**Acceptance Criteria:**

- Given завершённое исследование, when пользователь открывает вкладку, then Markdown читается
  как заголовки, абзацы и списки, а не как непрерывная строка.
- Given доступный отчёт, when пользователь выбирает PDF или Word, then скачанный документ содержит
  тему, итог и только проверенные evidence snapshots с URL.
- Given злоумышленная разметка или внешний текст, when он отображается или попадает в документ,
  then он не исполняется и не меняет разметку страницы.
- Given PDF недоступен, when выбран PDF, then UI предлагает Word или повтор, а исходный отчёт
  остаётся доступен.

## Spec Change Log

- 2026-08-31: добавлены immutable PDF/DOCX-экспорт, safe Markdown и Browser-runtime контракт
  для передачи server-side `snapshot_id` в карточку исследования.
- 2026-08-31: полный модульный pytest зелёный после синхронизации SQLite fixture с migration 059,
  но трассировка обнаружила незавершённый контракт: `save_report_result()` сохраняет пустой
  `evidence_snapshot`, поэтому реальный проверенный источник ещё не может попасть в PDF/DOCX.

## Design Notes

Карточка сохраняет текущую спокойную AuditLens-композицию: действия располагаются в её шапке,
а не внутри текста. Основной CTA — «Скачать исследование»; после нажатия доступны два равных
формата: «PDF» для печати и «Word» для дальнейшей работы. Отчёт не превращается в редактор.

## Verification

**Commands:**

- `.venv/Scripts/python.exe -m pytest tests/loophole/test_final_layout_contract.py tests/loophole/test_final_layout_runtime.py tests/loophole/test_story_4_2_filtered_export.py -q -p no:cacheprovider` — RED, затем PASS.
- `.venv/Scripts/python.exe -m pytest tests/loophole -q -p no:cacheprovider` — регрессия модуля.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole` — отсутствие новых ошибок.

**Manual checks:**

- Во встроенном Browser выполнить исследование с заголовками, списком и источниками; проверить
  light/dark, PDF и Word download, console errors и comparison screenshot.
