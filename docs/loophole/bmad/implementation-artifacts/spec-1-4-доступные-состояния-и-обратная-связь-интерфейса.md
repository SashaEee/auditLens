---
title: 'Доступные состояния и обратная связь интерфейса'
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
я хочу получать понятную обратную связь от каждого рабочего экрана,
чтобы отличать загрузку, отсутствие данных и ошибку и безопасно управлять действиями с клавиатуры.

**Критерии приёмки:**

**Дано** экран запрашивает данные,
**Когда** запрос выполняется, завершается пустым результатом или ошибкой,
**Тогда** UI показывает три разные поверхности: загрузку, пустой результат с действием «Сбросить» и ошибку с действием «Повторить».
**И** ошибка не маскируется под пустой результат.

**Дано** действие пользователя успешно выполнено или завершилось ошибкой,
**Когда** UI сообщает результат,
**Тогда** он показывает один toast соответствующего типа: информационный, успешный или ошибочный.
**И** деструктивное действие требует модального подтверждения с описанием последствия, а `alert()` и `confirm()` не используются.

**Дано** пользователь управляет модалкой или off-canvas панелью клавиатурой,
**Когда** он открывает и закрывает её,
**Тогда** фокус остаётся внутри активного слоя, Escape закрывает его, а затем фокус возвращается на открывший контрол.
**И** все интерактивные элементы семантические, имеют видимый `:focus-visible`, мишени не меньше 28px и русские подписи.

## Boundaries & Constraints

**Always:** Реализовывать только требования этой истории и сохранять архитектурные инварианты AuditLens: server-side fail-closed авторизацию, изоляцию рабочих контекстов, детерминированные расчёты без делегирования чисел LLM, русские подписи интерфейса и безопасную обработку данных.

**Ask First:** Расширить область на другую историю, изменить схему прав, межэпиковый контракт, внешнюю интеграцию, миграцию или deployment за пределами явно необходимого для этой истории.

**Never:** Не считать этот черновик разрешением реализовать соседние истории; не раскрывать защищённые данные, не обходить существующие domain services, не добавлять неоговорённые зависимости и не создавать git-коммит.

</frozen-after-approval>

## Code Map

Исследовано при реализации (2026-08-29). Весь фронт модуля — без сборки, поэтому
точки расширения текстовые, покрываются тестами в стиле
`tests/loophole/test_iframe_shell_theme.py`.

**Изменяемые файлы:**
- `src/bank_audit/loophole/static/loophole.jsx` — единственный SPA-файл модуля.
  Точки расширения: `loadRecords`/`loadQueue`/`loadParsers` (различимые
  поверхности загрузка/пусто/ошибка, Retry/Reset),
  `recordsRequestRef`/`parsersRequestRef` (generation guard от устаревших
  ответов фильтров, polling и Retry),
  `showToast` (единственный toast, типы info/success/error), `exportCSV`
  (был системный alert-диалог), действия парсеров (по одному outcome-toast),
  удаление парсера (был системный confirm-диалог → модалка `deleteConfirm`),
  модалки `parsersOpen`/`verdictModal`, панель чата (`chatVisible`). Новый
  общий механизм `useFocusLayer` (focus-trap, Escape, возврат фокуса на opener
  либо enabled fallback, стек слоёв) + `sortableThProps` (`aria-sort`) и нативные
  кнопки `.lp-sort-button`/`.lp-row-details` (Enter/Space, `aria-expanded`,
  `aria-controls` только для раскрытой строки) вместо интерактивных строк
  таблицы; backdrop диалогов — нетаббируемые нативные кнопки.
- `src/bank_audit/loophole/static/loophole.css` — варианты toast
  (`.lp-toast-{info,success,error}`), модалка подтверждения (`.lp-confirm-*`,
  `.lp-btn-danger`), кольцо `:focus-visible`, мишени ≥ 28px
  (`.lp-btn`/`.lp-btn-sm`/`.lp-verdict-chip`/`.lp-content-toggle`/`.lp-chip`/
  `.lp-sort-button`/`.lp-row-details`/`.lp-checkbox-hit` и ссылки
  таблиц/парсеров/деталей), слоение `.lp-modal-backdrop` под dialog,
  непрозрачные WCAG-пары `.lp-badge-err`/`.lp-badge-run`/`.lp-badge-attn`,
  чип-чекбокс без `display: none` (доступен с клавиатуры), ширина чата 300px
  при 1100–1399px (`.lp-layout-chat` в `@media (max-width: 1399px)`).
- `tests/loophole/test_accessible_states_feedback.py` — новые текстовые тесты
  всех критериев приёмки (RED → GREEN), включая pointer-target чекбоксов,
  backdrop, контраст badge, outcome-toast и защиту от устаревших ответов.
- `tests/loophole/test_focus_restore_runtime.py` — Playwright runtime-регрессия:
  confirm-delete возвращает фокус с disabled opener на enabled fallback.
- `tests/loophole/test_adaptive_context_routes.py` — регрессия раскрытия деталей
  через нативную кнопку без обработчиков событий на `<tr>`.

**Read-only (не трогать в этой истории):**
- `src/bank_audit/loophole/web.py`, `authorization.py`, `repository.py` —
  серверная fail-closed авторизация и DTO (story 1.1), контракты endpoint'ов
  не меняются.
- `src/bank_audit/loophole/static/loophole.html` — оболочка и синхронизация
  темы (story 1.2).
- `src/bank_audit/web/static/` — основной сайт AuditLens.
- Миграции `migrations/` — история не требует изменений схемы.

## Tasks & Acceptance

**Execution:**
- [x] Исследовать существующий код и обновить Code Map конкретными путями, символами и read-only ограничениями.
- [x] Написать минимальный failing test на основной сценарий и каждый критичный отказ из критериев приёмки.
- [x] Наблюдать ожидаемое RED-падение; только затем внести минимальную production-реализацию.
- [x] Запустить целевые тесты, полный набор тестов модуля и линтер; при необходимости уточнить границы до начала реализации.

**Acceptance Criteria:**
- Критерии приёмки внутри frozen Intent являются обязательным контрактом этой истории и должны быть преобразованы в наблюдаемые тесты до реализации.

## Spec Change Log

- 2026-08-29 — Закрыты audit gaps Story 1.4: семантическая сортировка и раскрытие
  деталей, состояния списка парсеров, безопасная ошибка CSV, русские accessible
  names и связанные подписи полей, мишени ≥ 28px и Reset для пустой очереди.
  Значения `status` в API сохранены без изменений.

- 2026-08-29 — Повторное исправление по аудиту: select-all и записи получили нативный
  28px pointer-target; backdrop стали именованными нетаббируемыми кнопками;
  visible badge `running` локализован без изменения машинского `is_running`;
  `aria-controls` выдаётся только раскрытой строке; `loadRecords` защищён
  generation guard от устаревших успехов и ошибок.

## Verification

**Commands:**
- pytest tests/loophole/test_accessible_states_feedback.py -q — ожидаемое RED до production-кода (18 падений), затем PASS после минимальной реализации.
- pytest tests/loophole -q — отсутствие регрессий соответствующего модуля.
- .venv/Scripts/ruff.exe check <затронутые-файлы> — отсутствие новых lint-ошибок.
