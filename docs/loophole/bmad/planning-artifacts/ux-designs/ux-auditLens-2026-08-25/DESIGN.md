---
name: "AuditLens — модуль «Лазейки»"
description: "Визуальная идентичность модуля лазеек: полное наследование дизайн-системы основного сайта auditLens, дельты только там, где у модуля есть уникальные компоненты."
status: draft
created: 2026-08-25
updated: 2026-08-25

colors:
  paper:        'oklch(98.5% 0.004 80)'
  paper-2:      'oklch(96.8% 0.005 80)'
  surface:      'oklch(100% 0 0)'
  ink:          'oklch(17% 0.01 260)'
  ink-2:        'oklch(38% 0.01 260)'
  ink-3:        'oklch(58% 0.012 260)'
  ink-4:        'oklch(76% 0.012 260)'
  hair:         'oklch(91% 0.005 80)'
  hair-2:       'oklch(86% 0.005 80)'
  accent:       'oklch(58% 0.18 25)'
  accent-soft:  'oklch(58% 0.18 25 / 0.08)'
  pos:          'oklch(48% 0.10 150)'
  warn:         'oklch(64% 0.13 75)'
  neg:          'oklch(58% 0.18 25)'
  paper-dark:        'oklch(15% 0.008 260)'
  paper-2-dark:      'oklch(18% 0.008 260)'
  surface-dark:      'oklch(20% 0.008 260)'
  ink-dark:          'oklch(96% 0.005 80)'
  ink-2-dark:        'oklch(76% 0.008 260)'
  ink-3-dark:        'oklch(58% 0.01 260)'
  ink-4-dark:        'oklch(40% 0.012 260)'
  hair-dark:         'oklch(26% 0.008 260)'
  hair-2-dark:       'oklch(32% 0.008 260)'
  accent-dark:       'oklch(68% 0.18 25)'
  accent-soft-dark:  'oklch(68% 0.18 25 / 0.14)'
  pos-dark:          'oklch(72% 0.14 150)'
  warn-dark:         'oklch(80% 0.14 75)'
  neg-dark:          'oklch(70% 0.18 25)'

typography:
  body:      { fontFamily: "'Geist','Inter',system-ui,sans-serif", fontSize: '14px', lineHeight: '1.5' }
  small:     { fontSize: '12.5px', lineHeight: '1.45' }
  mono:      { fontFamily: "'JetBrains Mono',monospace", fontSize: '12.5px' }
  th:        { fontFamily: "'JetBrains Mono',monospace", fontSize: '10.5px', letterSpacing: '0.04em' }
  title:     { fontFamily: "'Source Serif 4',serif", fontSize: '20px', fontWeight: '600' }
  minimum:   { fontSize: '11px' }

rounded:
  sm: 4px
  md: 6px
  lg: 10px
  full: 9999px

spacing:
  '1': 4px
  '2': 8px
  '3': 12px
  '4': 16px
  '5': 24px
  '6': 32px

components:
  button:
    height: 34px
    radius: '{rounded.md}'
    fontSize: '{typography.body.fontSize}'
  badge:
    height: 20px
    radius: 3px
    fontSize: 10.5px
  table-cell:
    fontSize: 13.25px
  input:
    height: 34px
    radius: '{rounded.md}'
  modal:
    radius: '{rounded.lg}'
    shadow: '{colors.shadow-2}'
---

# DESIGN.md — модуль «Лазейки»

## Brand & Style

Модуль «Лазейки» — часть auditLens, а не самостоятельный продукт. Он наследует визуальный язык основного сайта без исключений: тёплая бумажная подложка, сдержанная типографика Geist/Source Serif 4/JetBrains Mono, единственный красный акцент, тонкие hairline-границы. Модуль живёт в iframe внутри `.surface`-карточки сайта, поэтому любое визуальное отличие читается как «чужой» интерфейс — этого быть не должно.

Поскольку iframe не наследует CSS-переменные родителя, токены сайта **реплицируются 1:1** в `loophole.css` (обе темы) под теми же именами (`--paper`, `--ink`, `--hair`, `--accent`, `--pos`, `--warn`, `--neg`, `--r-sm/--r/--r-lg`, `--shadow-1/--shadow-2`). Локальные hex-палитры модуля (`#b03a2e`, `#2f9e44`, `#f08c00`, `#e03131`, зоопарк радиусов 3–14px) упраздняются. Источник истины — `:root` и `html.dark` в `src/bank_audit/web/static/index.html`.

## Colors

- **paper / surface** — фон страницы и карточек. Никаких чисто-белых/чисто-серых локальных значений.
- **ink / ink-2 / ink-3 / ink-4** — единая шкала текста. `ink-3` — минимум для любого информационного текста (контраст на paper ≈ 4.6:1); `ink-4` — только декоративные элементы и placeholder, никогда носители смысла. Текущий `--muted-2:#9a9a93` (2.9:1) запрещён.
- **accent** — единственный акцент: primary-кнопки, активные чипы фильтров, сортировка, ссылки. Он же `neg` — семантически «лазейка/опасность»; для toast-ошибок и вердикта «лазейка» используется тот же токен, это осознанное совпадение сайта.
- **pos / warn / neg** — статусная семантика: pos — «обычный запрос/готово», warn — «требует внимания/в работе», neg — «лазейка/ошибка». Material-палитра `#2f9e44/#f08c00/#e03131` удаляется.
- **Тёмная тема** обязательна: полный набор `*-dark` токенов, переключаемых классом `html.dark`, синхронизированным с родительским документом сайта (см. EXPERIENCE.md → Foundation). «Всегда тёмный» сайдбар чата в тёмной теме перестаёт быть инверсией — он оформляется как `paper-2-dark` с `hair-dark` границей, т.е. отличается тоном, а не полярностью.

## Typography

- База — {typography.body}: Geist 14px/1.5, как на сайте.
- Моноширинные данные (id, URL, даты, числа в таблице) — JetBrains Mono; числовые колонки с `font-variant-numeric: tabular-nums`.
- Заголовок страницы — Source Serif 4 20px/600, как `.aw-title` сайта.
- **Нижняя граница — 11px** для любого читаемого текста. Текущие 9.5–10.5px (manual-mark, verdict-chip, status-select) поднимаются до 11–12.5px; исключение — заголовки таблиц (mono uppercase 10.5px, как на сайте).
- Англоязычные id (`clarify/execute/answer`) в UI не показываются — только локализованные подписи.

## Layout & Spacing

- Брейкпоинты считаются **от ширины iframe, а не вьюпорта**: при вьюпорте 1280px реальная ширина модуля ≈ 992px. Реальная рабочая сетка: `≥1400px` — просторно (таблица + сайдбар-чат 340–380px), `1100–1399px` — компактно (сайдбар 300px, таблица с приоритетными колонками), `<1100px` — чат уходит в off-canvas панель поверх контента, открывается кнопкой.
- `height:100vh; overflow:hidden` на корне запрещён — корень модуля скроллится сам, высота определяется контейнером iframe сайта.
- Заголовок страницы — flex-wrap с переносом кнопок на вторую строку; обрезка контента без скролла недопустима нигде.
- Отступы — только по шкале {spacing}: 4/8/12/16/24/32.

## Elevation & Depth

Две тени сайта: `--shadow-1` (карточки, таблица) и `--shadow-2` (модалки, плавающая панель маркировки, toast). Ad-hoc rgba-тени удаляются. В тёмной теме — тёмные варианты теней сайта.

## Shapes

Три радиуса: {rounded.sm} (бейджи, чипы), {rounded.md} (кнопки, инпуты, карточки), {rounded.lg} (модалки, сайдбар-панели). Все остальные значения (3/5/8/12/14px) сводятся к шкале.

## Components

- **Кнопки** — высота 34px, radius {rounded.md}: primary (accent), ghost (hair-граница), danger. Нативные `alert()`/`confirm()` не используются нигде — подтверждения модальные, сообщения — toast.
- **Бейджи/чипы** — высота ≥20px (кликабельные — ≥28px), mono 10.5–11px, radius 3–4px. Чип-фильтры банков — с состояниями default/hover/active/disabled.
- **Таблица** — sticky thead, th mono uppercase 10.5px, td 13.25px; сортировка — кнопка с `aria-sort`; строка не является гигантским чекбоксом: выделение — только чекбоксом, раскрытие — отдельной кнопкой-стрелкой ≥28px.
- **Toast** — варианты info/success/error по семантике (pos/ink/neg), а не всегда красный.
- **Модалки** — radius {rounded.lg}, shadow-2, закрытие по Escape, focus-trap, aria-label у кнопки закрытия.
- **Иконки** — без emoji: inline SVG или текстовые подписи (правило сайта «никаких emoji-decorations»).

## Do's and Don'ts

- ✅ Все vendor-ресурсы саморазмещённые (`/static/vendor/`), как у основного сайта. ❌ unpkg, fonts.googleapis.com — полностью белая страница на части сетей.
- ✅ Токены сайта, обе темы. ❌ Локальные hex-палитры и «почти такие же» красные.
- ✅ Layout, живущий в ширине iframe 736–2000px. ❌ Фиксированные `100vh`/`overflow:hidden`, grid `1fr 340px` без fallback.
- ✅ Текст ≥11px, мишени ≥28px, контраст ≥4.5:1 для текста. ❌ 9.5px подписи и 16px стрелки.
- ✅ Русские подписи интерфейса. ❌ Английские технические id в UI.
