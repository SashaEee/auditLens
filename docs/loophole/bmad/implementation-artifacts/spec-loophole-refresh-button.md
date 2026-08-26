---
title: 'Фикс кнопки обновления ⟳ на странице «Лазейки»'
type: 'bugfix'
created: '2026-08-26'
status: 'done'
review_loop_iteration: 0
baseline_commit: '787e584898a5885442f3fb13036ce7365c855bbc'
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** На странице «Лазейки» глобальная кнопка ⟳ («Обновить страницу», топбар SPA) ничего не делает: обработчик `setPage(p=>p)` отдаёт то же значение стейта, React делает bail-out; а iframe модуля держится смонтированным постоянно, поэтому даже рабочий ремаунт по `key={page}` его бы не перезагрузил.

**Approach:** Завести в `App` счётчик `refreshTick`; кнопка ⟳ инкрементирует его только на странице «Лазейки», а `LoopholePage` получает `key={refreshTick}` — ремаунт iframe перезагружает модуль. Поведение кнопки на остальных страницах не меняется.

## Boundaries & Constraints

**Always:**
- Правка только в `src/bank_audit/web/static/app.jsx`; фронт без сборки — правишь файл, обновляешь страницу.
- Сохранить персистентность «Лазеек» между вкладками: размонтирование — только по явному клику ⟳ на странице «Лазейки».
- Комментарии в коде — на русском, в стиле соседних строк.

**Ask First:**
- Если захочется распространить фикс на другие страницы или трогать что-то ещё (внутреннюю кнопку «↻ Обновить» в `loophole.jsx`, `AIPage`).

**Never:**
- Не менять поведение кнопки ⟳ на страницах, кроме «Лазейки» (там она остаётся как есть).
- Не трогать `key` обычных страниц (`<Page key={page}>`) и персистентный `AIPage`.
- Не трогать `src/bank_audit/loophole/static/loophole.jsx` — внутренняя кнопка «↻ Обновить» модуля работает (вызывает `loadRecords`), она вне скоупа.
- Никаких `location.reload()` — обновление внутри SPA, без перезагрузки вкладки.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | Открыта страница «Лазейки», клик по ⟳ | iframe `/static/loophole/loophole.html` перезагружается, записи запрашиваются заново | N/A |
| OTHER_PAGE | Открыта любая другая страница, клик по ⟳ | Поведение без изменений (как сейчас) | N/A |
| TAB_SWITCH | Клик по ⟳ на «Лазейках», уход на другую вкладку и возврат | Модуль остаётся смонтированным, повторной перезагрузки нет | N/A |
| AI_RUN | Идёт прогон ИИ-аналитика, клик по ⟳ на «Лазейках» | Скрытый `AIPage` не ремонтируется, прогон не обрывается | N/A |

</frozen-after-approval>

## Code Map

- `src/bank_audit/web/static/app.jsx:8132` — кнопка ⟳, сломанный обработчик `onClick={()=>setPage(p=>p)}` (React bail-out, no-op).
- `src/bank_audit/web/static/app.jsx:7898` — `const[page,setPage]=useState(...)` в `App`; рядом добавить `refreshTick`.
- `src/bank_audit/web/static/app.jsx:8140-8142` — персистентный контейнер «Лазеек»: `{loopholeMounted&&<div style={{display:...}}><LoopholePage/></div>}` — повесить `key={refreshTick}` на `LoopholePage`.
- `src/bank_audit/web/static/app.jsx:6396-6401` — `LoopholePage` = iframe на `/static/loophole/loophole.html`; ремаунт по `key` перезагружает iframe без правок модуля.
- `src/bank_audit/web/static/app.jsx:8143-8146` — `AIPage` и обычные страницы — read-only, НЕ трогать.
- `src/bank_audit/loophole/static/loophole.jsx:752` — внутренняя кнопка «↻ Обновить» модуля — работает, read-only evidence.

## Tasks & Acceptance

**Execution:**
- [x] `src/bank_audit/web/static/app.jsx` — добавить `const[refreshTick,setRefreshTick]=useState(0);` рядом со стейтом `page`; обработчик кнопки ⟳ заменить на инкремент тика только при `page==="loophole"`; `LoopholePage` — `key={refreshTick}` — минимальный фикс no-op кнопки для модуля «Лазеек».

**Acceptance Criteria:**
- Given открыта страница «Лазейки», when пользователь кликает ⟳ в топбаре, then iframe модуля перезагружается и таблица записей обновляется.
- Given открыта любая другая страница, when пользователь кликает ⟳, then поведение не отличается от текущего.
- Given после клика ⟳ на «Лазейках» пользователь ушёл на другую вкладку и вернулся, then модуль остаётся смонтированным без повторной перезагрузки.

## Spec Change Log

## Verification

**Commands:**
- `python -c "import pathlib; s=pathlib.Path('src/bank_audit/web/static/app.jsx').read_text(encoding='utf-8'); assert 'setRefreshTick' in s and 'setPage(p=>p)' not in s"` — expected: отсутствие мёртвого обработчика, наличие тика.
- `pytest tests/loophole -q` — expected: регрессии модуля «Лазеек» зелёные (правка чисто фронтовая, прогон для контроля).

**Manual checks (if no CLI):**
- Открыть SPA → «Лазейки» → DevTools Network → клик ⟳: iframe перезагружается (запрос `loophole.html?...` и `/api/loophole/records`).

## Suggested Review Order

**Фикс кнопки ⟳**

- Мёртвый bail-out `setPage(p=>p)` заменён на инкремент тика только на странице «Лазейки» — входная точка фикса.
  [`app.jsx:8136`](../../../../src/bank_audit/web/static/app.jsx#L8136)

- Счётчик `refreshTick` в `Shell` — ремаунт по key перезагружает iframe без `location.reload()`.
  [`app.jsx:7906`](../../../../src/bank_audit/web/static/app.jsx#L7906)

- `key={refreshTick}` на персистентном `LoopholePage` — размонтирование только по явному клику ⟳.
  [`app.jsx:8145`](../../../../src/bank_audit/web/static/app.jsx#L8145)

**Регрессионные тесты**

- Текстовые проверки контракта (по образцу `test_static_bust.py`), whitespace-нормализованные, покрывают все строки I/O-матрицы.
  [`test_refresh_button.py:35`](../../../../tests/loophole/test_refresh_button.py#L35)
