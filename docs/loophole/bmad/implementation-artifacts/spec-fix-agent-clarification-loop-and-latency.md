---
title: 'Исправление цикла уточнений и панели AI-аналитика'
type: 'bugfix'
created: '2026-08-31'
status: 'done'
baseline_commit: '0e17177799d123ebc01995bc743cdffbfe8357ed'
review_loop_iteration: 0
context:
  - 'docs/project-context.md'
  - 'docs/loophole/bmad/planning-artifacts/ux-designs/MOCKUPS.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** При сбое clarification-модели LLM-rewrite после ответа выдаёт новый идентичный challenge. Retry, скрытые вопросы и ложный статус «готов» создают задержку. Чат также сохранил всегда тёмное оформление и отдельную форму развёрнутого ответа вместо финального макета AuditLens.

**Approach:** Оставить один быстрый LLM-gate, ответы собирать детерминированно. `text`-вопрос показывать сообщением агента и принимать через composer; карточки оставить для `single`/`multi`. После submit сразу показывать user bubble и «Обдумывает ответ», затем оформить панель по состоянию B финального PNG.

## Boundaries & Constraints

**Always:** Сохранять ownership, execution-token, запрет запуска без ответов, PII-маскирование, text-delta redaction, off-canvas, focus-trap и responsive; эталон — `loophole-variant-3-integrated-auditlens-final.png`.

**Ask First:** Миграция/token-хранилище; изменение методологии nanobot.

**Never:** Второй LLM-вызов; повтор вопроса после непустого ответа; отдельное textarea-окно для `text`; всегда тёмный чат в светлой теме; slash-команды; ослабление PII-защиты; коммит.

## I/O & Edge-Case Matrix

| Scenario | State | Expected behavior | Error handling |
|----------|-------|-------------------|----------------|
| Gate недоступен | Fallback заполнен | Детерминированный execution-token и один запуск | Без rewrite и нового одинакового challenge |
| Открытый вопрос | `type=text` | Вопрос — assistant bubble; ответ — общий composer | Composer отправляет clarification-answer, не новый chat-run |
| Выбор | `single`/`multi` заполнены не все | Видны все; запуск заблокирован | Inline-валидация, ответы сохранены |
| Ответ отправлен | `/clarify/answer` ожидается | Сразу user bubble, карточка скрыта, «Обдумывает ответ» | При ошибке восстановить вопрос и ответ |
| Светлая оболочка | Чат открыт | Светлая интегрированная панель, hairline-границы, красный accent, без чёрного полотна/градиента | Тёмные токены только при `html.dark` |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/chat/clarify.py:21-43,191-325,337-408` — gate сейчас использует SMART, 70 s, retries, deep reasoning и второй LLM-rewrite; `_validate` допускает до пяти вопросов.
- `src/bank_audit/loophole/chat/prompt/01_clarify.md` — prompt разрешает пять вопросов; сузить challenge до одного наиболее полезного вопроса.
- `src/bank_audit/loophole/web.py:538-604,946-1074` — execution-token flow; повторный `/chat` сохраняет enriched-query как ещё одно пользовательское сообщение, а rewrite-error выдаёт новый challenge.
- `src/bank_audit/loophole/chat/graph.py:157-215,285-359` — first-gate/verified-token ветви и PII-mask перед nanobot; не превращать внутреннюю ошибку сборки в повтор уже отвеченного вопроса.
- `src/bank_audit/loophole/static/loophole.jsx:148-207,907-1195,2066-2264` — UI показывает только первый вопрос, блокирует composer, повторно добавляет enriched-query и теряет видимый busy-state.
- `src/bank_audit/loophole/static/loophole.css:80-294,1018-1246` — чат жёстко тёмный, avatar с gradient, отдельная question-card и dark input/bubbles.
- `tests/loophole/{test_chat_clarify.py,test_story_2_1_routes.py,test_story_2_1_agent.py,test_web.py,test_final_layout_runtime.py}` — заменить регрессии, закрепляющие retry, на поведенческий контракт одного gate, недублируемой истории и composer-flow.
- `workspace/qa/loophole-final/source-panel-b-ai-research.png`, `design-qa.md` — визуальный источник и blocking same-input отчёт.

## Tasks & Acceptance

**Execution:**
- [x] `tests/loophole/test_chat_clarify.py`, `test_story_2_1_routes.py`, `test_story_2_1_agent.py` — сначала RED: один FAST-gate, детерминированное обогащение, отсутствие нового challenge после непустого ответа и дубля history.
- [x] `src/bank_audit/loophole/chat/{clarify.py,prompt/01_clarify.md,graph.py}` — ограничить gate одним вопросом, убрать LLM-rewrite и направлять внутренний сбой в ошибку, не в повтор clarification.
- [x] `src/bank_audit/loophole/web.py` — не сохранять enriched-query вторым user-message; отклонять недействительный execution token до SSE и сохранить ownership/audit.
- [x] `src/bank_audit/loophole/static/loophole.jsx` — `text` через composer, `single/multi` через controls, обязательный ответ, один optimistic user bubble и непрерывный статус «Обдумывает ответ».
- [x] `src/bank_audit/loophole/static/loophole.css`, `tests/loophole/test_final_layout_runtime.py` — theme-aware панель без чёрного полотна, gradient и slash-copy; browser-flow с задержанным `/clarify/answer`.
- [x] `design-qa.md`, `workspace/qa/` — same-input сравнение состояния AI-исследования в светлой и тёмной теме.

**Acceptance Criteria:**
- Given расплывчатый запрос, when gate вернул несколько вопросов, then пользователю выдаётся только один наиболее полезный challenge и повторный LLM-вызов после ответа отсутствует.
- Given валидный ответ и execution token, when начинается `/chat`, then в истории остаются исходный запрос и ответ пользователя без третьего enriched-дубля; SSE переходит в `execute`.
- Given недействительный execution token, when запрошен `/chat`, then сервер отвечает до запуска graph и не сохраняет ложное пользовательское сообщение.
- Given открытый чат в light/dark и desktop/off-canvas, when меняется тема или ширина, then токены, focus-trap, composer и scroll-контейнер остаются читаемыми и работоспособными.

## Spec Change Log

### 2026-08-31 — review patch: известный scope и транспортный сбой LLM

- Запрос с уже указанными продуктом и периодом теперь считается исполнимым без LLM-gate;
  отсутствие банка означает поиск по всем банкам, а отсутствие отдельного аспекта — по всем
  аспектам продукта.
- Детерминированный fallback задаёт только отсутствующее измерение (`продукт` или `период`) и
  не повторяет сведения, уже найденные в исходном запросе.
- `stop_reason=error` от managed agent стал terminal provider error: raw provider text не
  попадает ни в bubble, ни в research evidence; UI показывает безопасное retry-сообщение и
  восстанавливает исходный запрос в composer.
- Локальный preview для browser-review запущен без унаследованных
  `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY=http://127.0.0.1:9` и с Windows-путём проектного CA bundle.
- Регрессии: actionable product+period без LLM, fallback только по отсутствующему периоду,
  безопасный SSE provider-error и восстановление initial query в browser runtime.
- Верификация: backend review subset — **142 passed**; browser-runtime — **33 passed**;
  полный `tests/loophole` — **706 passed, 1 skipped**, 15 предупреждений; полный scoped Ruff —
  **All checks passed**. Реальный in-app Browser: clarification cards — **0**, финальная фаза —
  `Готово`, console errors — **0**, `/api/loophole/chat` — **200**, вызовы Foundation Models —
  **HTTP 200**.

### 2026-08-31 — review patch: жёсткая граница по дате публикации

- `web_fetch` извлекает только точную timezone-aware дату публикации первоисточника. Запись без
  подтверждённого `published_at` либо вне пользовательского окна блокируется серверным инструментом
  до LLM-извлечения; дата сниппета и дата сбора не считаются заменой.
- Для запроса `за <месяц> <год>` окно закрыто календарным месяцем. `table_load`, каталог и поиск
  по БД фильтруют `published_at` включительно по последнему дню месяца, а не `collected_at`.
- `058_loophole_publication_date.sql` применена к локальной проверочной БД. В интерфейсе период
  каталога подписан как «Дата публикации», без двусмысленного «Периода сбора».
- Роль агента и prompt нейтральны к банку: отсутствие банка означает все банки, а не неявный
  Сбербанк. Реальный запрос без банка не добавил такой scope и при отсутствии допустимых фактов
  завершился честным сообщением вместо расширения периода.
- Верификация: полный `tests/loophole` — **716 passed, 1 skipped**, 21 предупреждение; scoped Ruff —
  **All checks passed**. Во встроенном Browser: `questions=0`, фаза `Готово`, console errors `0`;
  light/dark screenshot comparison не выявил P0/P1/P2.

## Design Notes

- Один challenge содержит один вопрос: `text` показывается ровно один раз как bubble аналитика; `single/multi` остаётся компактной карточкой выбора.
- Composer меняет режим, а не создаёт новый run: при `text` он отвечает текущему challenge; после submit optimistic bubble остаётся единственным пользовательским ответом.
- Статус панели вычисляется из `clarifySubmitting || chatLoading`: сначала «Обдумывает ответ», затем фаза выполнения; состояние «готов» не показывается между этими запросами.
- Светлая панель использует существующие `--surface/--paper-2/--ink/--hair/--accent`; `html.dark` переключает те же семантические токены без отдельной постоянно тёмной палитры.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_chat_clarify.py tests/loophole/test_story_2_1_routes.py tests/loophole/test_story_2_1_agent.py tests/loophole/test_web.py -q` — RED, затем PASS для gate/token/history.
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_final_layout_runtime.py -q` — composer-flow, optimistic busy-state, темы и отсутствие дубля.
- `.venv/Scripts/python.exe -m pytest tests/loophole -q` — regression.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole` — scoped lint.

**Manual checks:**
- Открыть `/#loophole` на «Новом AI-исследовании», пройти vague-query → answer → execute и сравнить Browser-capture с `workspace/qa/loophole-final/source-panel-b-ai-research.png` в одном input.

## Suggested Review Order

**Серверный контракт уточнения**

- Входная граница проверяет ownership, собирает запрос и только затем поглощает challenge.
  [`web.py:1034`](../../../../src/bank_audit/loophole/web.py#L1034)

- FAST-клиент ограничивает ожидание 15 секундами и запрещает транспортные повторы.
  [`clarify.py:249`](../../../../src/bank_audit/loophole/chat/clarify.py#L249)

- Один LLM-gate выдаёт максимум один вопрос; ответ объединяется без второго вызова.
  [`clarify.py:315`](../../../../src/bank_audit/loophole/chat/clarify.py#L315)

- Execution token пропускает повторный gate, сохраняя основной graph-пайплайн.
  [`graph.py:155`](../../../../src/bank_audit/loophole/chat/graph.py#L155)

**Устойчивый composer-flow**

- SSE-клиент различает terminal error и success, не маскируя сбой состоянием done.
  [`loophole.jsx:911`](../../../../src/bank_audit/loophole/static/loophole.jsx#L911)

- Ответ показывается оптимистично; 400 и временный 503 восстанавливаются разными путями.
  [`loophole.jsx:1169`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1169)

- Busy-state объединяет отправку уточнения и запуск исследования без мигания «Готов».
  [`loophole.jsx:1433`](../../../../src/bank_audit/loophole/static/loophole.jsx#L1433)

- Semantic tokens дают читаемую боковую панель в light и dark.
  [`loophole.css:155`](../../../../src/bank_audit/loophole/static/loophole.css#L155)

**Регрессии и визуальные доказательства**

- Route-тест использует реальную детерминированную сборку и проверяет историю.
  [`test_story_2_1_routes.py:249`](../../../../tests/loophole/test_story_2_1_routes.py#L249)

- Browser-runtime закрепляет composer, ошибки, повторный id и восстановление выбора.
  [`test_final_layout_runtime.py:325`](../../../../tests/loophole/test_final_layout_runtime.py#L325)

- Same-input review фиксирует одинаковое состояние, геометрию и нулевой visual drift.
  [`design-qa.md:266`](../../../../design-qa.md#L266)
