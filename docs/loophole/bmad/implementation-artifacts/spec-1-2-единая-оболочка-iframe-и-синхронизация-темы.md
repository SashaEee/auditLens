---
title: 'Единая оболочка iframe и синхронизация темы'
type: 'feature'
created: '2026-08-29'
status: 'draft'
review_loop_iteration: 0
context:
  - 'docs/loophole/bmad/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

Как внутренний пользователь,
я хочу видеть модуль в единой с AuditLens визуальной системе,
чтобы работа в iframe воспринималась как часть основного продукта и оставалась читаемой в обеих темах.

**Критерии приёмки:**

**Дано** модуль открыт внутри same-origin iframe,
**Когда** загружается его интерфейс,
**Тогда** он использует только саморазмещённые React, ReactDOM, Babel и шрифты из `/static/vendor/`.
**И** внешние CDN и локальные hex-палитры не используются.

**Дано** основной сайт переключает `html.dark`,
**Когда** изменяется класс родительского документа,
**Тогда** iframe синхронно применяет или снимает `html.dark` через `MutationObserver`.
**И** при прямом открытии iframe тема выбирается через `prefers-color-scheme`.

**Дано** модуль отображает таблицу, чат, модалку или toast,
**Когда** пользователь переключает тему,
**Тогда** все поверхности используют единые токены AuditLens и сохраняют контраст текста не ниже 4.5:1.

## Boundaries & Constraints

**Always:** Реализовывать только требования этой истории и сохранять архитектурные инварианты AuditLens: server-side fail-closed авторизацию, изоляцию рабочих контекстов, детерминированные расчёты без делегирования чисел LLM, русские подписи интерфейса и безопасную обработку данных.

**Ask First:** Расширить область на другую историю, изменить схему прав, межэпиковый контракт, внешнюю интеграцию, миграцию или deployment за пределами явно необходимого для этой истории.

**Never:** Не считать этот черновик разрешением реализовать соседние истории; не раскрывать защищённые данные, не обходить существующие domain services, не добавлять неоговорённые зависимости и не создавать git-коммит.

</frozen-after-approval>

## Code Map

Исследовано 2026-08-29 перед реализацией:

- `src/bank_audit/loophole/static/loophole.html` — точка входа iframe. До истории:
  внешние CDN (unpkg react/react-dom/babel, Google Fonts). Здесь: переход на
  `/static/vendor/{react,react-dom,babel}.min.js` + `/static/vendor/fonts.css`
  и inline-скрипт синхронизации темы. Строки `src/href` на `loophole.jsx/css`
  не менять — их матчит cache-bust в `web/app.py:_loophole_html_with_bust()`.
- `src/bank_audit/loophole/static/loophole.css` — палитра модуля. До истории:
  локальная hex-палитра в `:root`. Здесь: токены AuditLens verbatim из
  `web/static/index.html` (`:root` + `html.dark`) и алиасы наследия
  (`--bg`→`--paper`, `--panel`→`--surface` и т.д.). Правила компонентов
  переиспользуют алиасы — массового рефакторинга правил не требуется.
- `src/bank_audit/web/static/index.html` (read-only) — эталон токенов
  (`:root`, `html.dark`, oklch) и саморазмещённого vendor-подхода.
- `src/bank_audit/web/static/vendor/` (read-only) — саморазмещённые
  react/react-dom/babel/fonts (Geist, Source Serif 4, JetBrains Mono).
- `src/bank_audit/web/static/app.jsx` (read-only) — `LoopholePage` встраивает
  `/static/loophole/loophole.html` в same-origin iframe; тема родителя —
  `document.documentElement.classList.toggle("dark", ...)` (app.jsx:108).
- `src/bank_audit/web/app.py` (read-only) — `_loophole_html_with_bust()`,
  mount `/static/loophole`; поведение не меняется.
- Тесты: `tests/loophole/test_iframe_shell_theme.py` — текстовые проверки
  (по образцу `test_refresh_button.py`) + вычислительная проверка
  WCAG-контраста токенов. Миграций БД история не требует.

## Tasks & Acceptance

**Execution:**
- [x] Исследовать существующий код и обновить Code Map конкретными путями, символами и read-only ограничениями.
- [x] Написать минимальный failing test на основной сценарий и каждый критичный отказ из критериев приёмки.
- [x] Наблюдать ожидаемое RED-падение; только затем внести минимальную production-реализацию.
- [x] Запустить целевые тесты, полный набор тестов модуля и линтер; при необходимости уточнить границы до начала реализации.

**Acceptance Criteria:**
- Критерии приёмки внутри frozen Intent являются обязательным контрактом этой истории и должны быть преобразованы в наблюдаемые тесты до реализации.

## Spec Change Log

## Verification

**Commands:**
- pytest <целевые-тесты> -q — ожидаемое RED до production-кода, затем PASS после минимальной реализации.
- pytest tests/loophole -q — отсутствие регрессий соответствующего модуля.
- .venv/Scripts/ruff.exe check <затронутые-файлы> — отсутствие новых lint-ошибок.
