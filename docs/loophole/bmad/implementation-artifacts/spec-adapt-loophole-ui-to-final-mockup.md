---
title: 'Адаптация интерфейса модуля «Лазейки» к финальному макету'
type: 'feature'
created: '2026-08-30'
status: 'done'
review_loop_iteration: 0
baseline_commit: '0e17177799d123ebc01995bc743cdffbfe8357ed'
context:
  - 'docs/project-context.md'
  - 'docs/loophole/bmad/planning-artifacts/ux-designs/MOCKUPS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Интерфейс «Лазеек» расходится с финальным вариантом 3: парсеры модальные, вкладки и поверхности собраны иначе, видна запрещённая «Надёжность», а дата публикации не отделена от даты сбора.

**Approach:** Адаптировать существующий SPA на токенах AuditLens, добавить сквозной nullable `published_at`, вынести web-парсеры во вкладку и сохранить мгновенный выборочный CSV. Показывать только данные и действия существующих API.

## Boundaries & Constraints

**Always:** Порядок: `Общая база → Добавить источник → Новое AI-исследование → Очередь верификации → Управление доступом`; защита — по RBAC. `published_at` — дата первоисточника, `collected_at` — сбора; неизвестное — `—`. `trust_score` остаётся внутренним, но Trust отсутствует в UI/выбранном CSV; видимая оценка — «Доверие». Сохранить published-only, ownership, clarify-токены, fail-closed, незакоммиченные правки, темы и доступность.

**Ask First:** Новые роли/API, snapshot-публикация, Telegram lifecycle, ownership, зависимости или лишние правки оболочки.

**Never:** Не подменять `published_at`; не выдумывать даты/права/действия; не ослаблять RBAC; не делать CSV модалкой; не обещать Telegram web-парсеру; не удалять внутренний `trust_score`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| CSV | Выбраны ID 1, 3, 8 | `POST /export` получает `[1,3,8]`; файл скачивается сразу, содержит две даты и не содержит Trust | Inline/toast; выбор сохраняется |
| Дата неизвестна | `published_at=null` | UI показывает `—`, CSV — пустую ячейку; `collected_at` не меняется | Без подстановки |
| Web-источник | Валидное описание/URL | Создание, validation SSE, статус и журнал на вкладке | Telegram/ошибка объясняются inline |
| Ограниченная роль | Нет expert/admin | Защищённые вкладки скрыты, API отклоняет доступ | Без утечки данных |

</frozen-after-approval>

## Code Map

- `migrations/058_loophole_publication_date.sql` — новая колонка.
- `src/bank_audit/loophole/{models.py,repository.py,web.py,authorization.py}` — дата, API, CSV и контексты.
- `src/bank_audit/loophole/parsers/{generator.py,runner.py}` — дата web-источника.
- `src/bank_audit/loophole/static/{loophole.jsx,loophole.css}` — весь UI.
- `tests/loophole/` — schema/API/RBAC/UI/runtime-контракты.

## Tasks & Acceptance

**Execution:**
- [x] `tests/loophole/` — сначала RED для вкладок, дат, отсутствия Trust, CSV и responsive.
- [x] DB/model/repository/parser — провести nullable `published_at` без догадок.
- [x] `authorization.py`, `web.py` — контекст, названия, API и CSV при прежней защите.
- [x] `loophole.jsx`, `loophole.css` — вкладка парсеров и адаптация поверхностей.

**Acceptance Criteria:**
- Given доступные контексты, when SPA загружен, then вкладки упорядочены, active-state доступен и RBAC соблюдён.
- Given публикации, when показаны строки/карточки, then две даты разделены, Trust отсутствует.
- Given выбранные строки, when нажат CSV, then скачаны только их ID, показан toast и доступен повтор.
- Given web-парсер, when он создан, then validation/status/log видны на вкладке без parser-модалки.
- Given AI/queue/admin, when они открыты, then макет использует только реальные API-данные.
- Given 1440/1200/992/735/390 px и обе темы, then нет корневого overflow или обрезанных контролов.

## Spec Change Log

## Verification

**Commands:**
- `.\.venv\Scripts\python.exe -m pytest -q tests/loophole` — весь offline-набор зелёный.
- `.\.venv\Scripts\python.exe -m ruff check src/bank_audit/loophole tests/loophole` — lint зелёный.
- `.\.venv\Scripts\python.exe -m pytest -q tests/loophole/test_final_layout_runtime.py -s` — viewport/runtime и console зелёные.

**Manual checks (if no CLI):**
- Сопоставить финальный PNG и runtime одного viewport рядом; проверить геометрию, цвета и основные действия.

## Suggested Review Order

**Каркас и рабочие поверхности**

- Серверный порядок вкладок сразу показывает RBAC-границы утверждённого интерфейса.
  [`authorization.py:49`](../../../../src/bank_audit/loophole/authorization.py#L49)

- Единственный tablist связывает пять маршрутов с доступными клавиатуре панелями.
  [`loophole.jsx:1391`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1391)

- Каталог честно фиксирует published-only срез без неработающих фильтров.
  [`loophole.jsx:1453`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1453)

- Веб-источник живёт отдельной вкладкой со статусом и журналом.
  [`loophole.jsx:1621`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1621)

- AI, очередь и администрирование получили самостоятельные композиции макета.
  [`loophole.jsx:1786`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1786)

- Адаптивные токены сохраняют плотность AuditLens на всех контрольных ширинах.
  [`loophole.css:329`](../../../../src/bank_audit/loophole/static/loophole.css#L329)

**Выборочный CSV**

- Клиент скачивает выбранные строки немедленно и сохраняет повторное действие.
  [`loophole.jsx:374`](../../../../src/bank_audit/loophole/static/loophole.jsx#L374)

- Сервер экспортирует только опубликованные лазейки и нейтрализует формулы.
  [`web.py:612`](../../../../src/bank_audit/loophole/web.py#L612)

**Дата первоисточника**

- Nullable-миграция принципиально не подменяет неизвестную дату временем сбора.
  [`058_loophole_publication_date.sql:1`](../../../../migrations/058_loophole_publication_date.sql#L1)

- Модель и read-model проводят `published_at` отдельно от `collected_at`.
  [`models.py:21`](../../../../src/bank_audit/loophole/models.py#L21)

- Генератор требует точный ISO timestamp либо честный null.
  [`generator.py:51`](../../../../src/bank_audit/loophole/parsers/generator.py#L51)

- Runner отбрасывает дату без ISO-формата или часового пояса.
  [`runner.py:57`](../../../../src/bank_audit/loophole/parsers/runner.py#L57)

**Проверки и визуальные доказательства**

- Контрактные тесты закрепляют даты, published-only CSV и отсутствие Trust.
  [`test_final_layout_contract.py:89`](../../../../tests/loophole/test_final_layout_contract.py#L89)

- Browser-runtime проверяет вкладки, источники, CSV, темы и breakpoints.
  [`test_final_layout_runtime.py:205`](../../../../tests/loophole/test_final_layout_runtime.py#L205)

- Финальный same-input отчёт фиксирует нулевые P0/P1/P2.
  [`design-qa.md:113`](../../../../design-qa.md#L113)
