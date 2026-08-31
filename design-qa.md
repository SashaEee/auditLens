# Design QA — адаптация «Лазеек» к финальному макету

Дата проверки: 2026-08-31  
Визуальный итог: пройдено, P0/P1/P2 после последней итерации не осталось.

## Источник истины и нормализация плотности

- Финальный board: `docs/loophole/bmad/planning-artifacts/ux-designs/loophole-variant-3-integrated-auditlens-final.png`.
- Размер board: **1536×1024 px**, плотность **96×96 dpi**.
- Board — монтаж шести разных состояний, а не один CSS viewport. Поэтому проверка выполнена
  state-to-state по нативным панелям: A `544×512`, B `516×512`, C `476×512`,
  D `544×512`, E `516×512`, F `476×512` px; буквальный pixel-overlay всего board
  с одной реализацией неприменим.
- Implementation capture: CSS viewport **1440×900**, `deviceScaleFactor=1`, итоговый PNG
  **1440×900 px**, **96×96 dpi**. Встроенный модуль занимает CSS-область **1206×842**.
- Во всех пяти состояниях корневые `scrollWidth/clientWidth` равны `1440/1440`, у модуля —
  `1206/1206`: корневого горизонтального overflow нет. Внутренний scroll длинной поверхности
  источников допустим и отдельно подтверждён focused capture журнала.

## Среда и состояние capture

- Снят фактический внешний AuditLens shell `/#loophole` с реальным iframe модуля и реальными
  статическими файлами, а не модульный HTML-harness.
- После review UI повторно снят браузером Chromium из того же full-shell harness; встроенный
  Browser в этой сессии не предоставил доступной поверхности (`discovery=[]`), поэтому fallback
  явно зафиксирован и использует тот же Chromium, которым выполняются runtime-тесты.
- Backend: локальный uvicorn и изолированная SQLite QA-БД. Реальные целевые API используются
  для контекстов/RBAC, workspace, банков, каталога, очереди и CSV.
- В минимальной SQLite нет несвязанных host-таблиц, поэтому только host-запросы `/api/me`,
  `/api/banks`, `/api/sources`, `/api/journal` получают детерминированные shell-stub ответы.
- Контролируемые целевые данные применены там, где фактический запуск зависит от LLM,
  внешнего контура или полноты production-БД: lifecycle web-парсера и validation EventSource,
  chat/SSE AI-исследования, репрезентативные admin roles/Telegram/audit aggregates.
  Эти ответы не изменяют product code и перечислены явно, чтобы evidence было воспроизводимым.
- Тема: светлая; locale: `ru-RU`; timezone browser: `Asia/Irkutsk`; роль principal:
  `ccks_expert + module_admin`; onboarding shell помечен завершённым, чтобы не перекрывать UI.

## Full-shell и focused evidence

| Панель | Репрезентативное состояние | Source evidence | Implementation full-shell | Focused implementation | Размер focused |
|---|---|---|---|---|---|
| F — Общая база/CSV | 3 строки выбраны, CSV скачан, toast и повтор доступны | `workspace/qa/loophole-final/source-panel-f-selected-csv.png` | `workspace/qa/loophole-final/implementation-catalog-selected-1440x900.png` | `workspace/qa/loophole-final/implementation-catalog-selected-focus.png` | 1206×733 |
| E — Добавить источник | web-парсер создан, validation завершена, список и журнал видны | `workspace/qa/loophole-final/source-panel-e-sources.png` | `workspace/qa/loophole-final/implementation-sources-1440x900.png` | `workspace/qa/loophole-final/implementation-sources-focus.png` | 1120×135, журнал целиком |
| B — Новое AI-исследование | выполняется фаза, 1/2 подзадач, tool events и partial answer | `workspace/qa/loophole-final/source-panel-b-ai-research.png` | `workspace/qa/loophole-final/implementation-ai-research-1440x900.png` | `workspace/qa/loophole-final/implementation-ai-research-focus.png` | 858×411, research board |
| C — Очередь верификации | 5 записей, первая выбрана, detail и существующее verdict-действие | `workspace/qa/loophole-final/source-panel-c-queue.png` | `workspace/qa/loophole-final/implementation-queue-1440x900.png` | `workspace/qa/loophole-final/implementation-queue-focus.png` | 1206×733 |
| D — Управление доступом | active/revoked роли и агрегированный audit; лишний UI Telegram удалён | `workspace/qa/loophole-final/source-panel-d-admin.png` | `workspace/qa/loophole-admin-telegram-removal/implementation-admin-without-telegram.png` | `workspace/qa/loophole-admin-telegram-removal/implementation-admin-cards-focus.png` | 804×312 |

Дополнительный source crop каталога без выбора:
`workspace/qa/loophole-final/source-panel-a-catalog.png` (`544×512`). Все source crops
сохранены при нативной плотности около 96 dpi. Полный board и пять full-shell PNG были
открыты одним comparison input; затем source/implementation пары B–F повторно открыты
одним focused comparison input.

## Findings по поверхностям

### F — Общая база/CSV

- Порядок пяти вкладок, фильтры, таблица, selected-row state и красный CSV primary-state
  соответствуют board.
- Неподдерживаемые backend-контрактом verdict/status select удалены: на их месте с той же
  плотностью показаны честные read-only scope-индикаторы `лазейки` и `опубликованные`.
- Массовая маркировка отсутствует; checkbox использует красный AuditLens accent.
- Статус локализован безопасным allowlist (`подтверждено`), обе даты показывают дату и время;
  `null` остаётся `—`; внутренний Trust не отображается.
- CSV-toast относится только к CSV и даёт повторное скачивание; следующий success-toast
  больше не наследует чужое CSV-действие.

### E — Добавить источник

- Отдельная вкладка, inline web-форма, protected-contour Telegram explanation, список
  подключённых парсеров, статус и validation SSE/log соответствуют назначению панели.
- Full-shell фиксирует форму, список и начало журнала; focused evidence фиксирует все четыре
  строки журнала и итог `успех · новых: 3`, поэтому внутренний вертикальный scroll не скрывает
  обязательное состояние.
- Telegram lifecycle не выдуман: web-форма не принимает токены/приватные приглашения.

### B — Новое AI-исследование

- Основная зона больше не placeholder: параметры, progress, подзадачи и evidence построены
  только из текущих `chat/phase/subtasks/toolEvents`; справа работает существующая AI-панель.
- Active phase, progress bar, текущий запрос и partial answer видны одновременно.

### C — Очередь верификации

- Вместо плоской таблицы реализован master-detail: список с active-state, доверие, даты,
  detail-карточка, source link и существующее действие проверки вердикта.
- Переключение записи обновляет detail, protected route остаётся fail-closed.

### D — Управление доступом

- На пользовательской поверхности остались только две нужные карточки: роли ЦК КС и
  обезличенный аудит. Карточка «Статус Telegram-целей» удалена без пустого grid-track.
- UI больше не запрашивает `/admin/telegram-targets`; серверный RBAC-endpoint и Telegram-контур
  не удалены и остаются доступны своим техническим потребителям.
- Статусы, даты/время, счётчики и действия оставшихся карточек сохранены.

## Допустимые API-driven различия с board

Это не визуальные дефекты и не P0/P1/P2: frozen spec запрещает выдумывать права,
данные и действия.

- Panel C рисует три решения и AI-summary, но backend не предоставляет отдельные операции
  «подтвердить / вернуть на доработку / отклонить» для queue. Реализация показывает только
  существующие source link и verdict modal; чат остаётся только на AI-route по принятому контракту.
- Panel D содержит permission matrix и monitoring chains, которых нет в текущих admin API.
  Реализация не имитирует эти данные и показывает реальные контракты roles и aggregate audit;
  Telegram status исключён по точечной пользовательской аннотации от 2026-08-31.
- Panel B содержит дополнительные desired-verdict controls и report metadata, которых нет
  в существующем request contract. Implementation отображает только state фактического chat/SSE.
- Panel E показывает schedule рядом с черновиком. Текущий API редактирует cron/auto-enabled
  после создания парсера; форма создания не обещает несуществующий Telegram lifecycle.

## История итераций

1. Первый comparison: устранены P2 — отдельный счётчик выбранных строк и неверный красный
   disabled-state CSV до выбора.
2. Comparison panel F: устранены четыре P2 — удалена массовая маркировка, checkbox получил
   AuditLens accent, даты перестали обрезать время, raw `published` заменён русским display label.
3. Full-board comparison: устранены P1 AI placeholder, P1 flat queue и P2 admin hierarchy;
   внедрены state-driven research board, queue master-detail и admin card/grid.
4. Clean recapture: устранены capture-state P2 (shell onboarding и переходящий parser toast),
   а также cross-toast P2, при котором parser success ошибочно показывал CSV repeat-action.
5. Финальный same-input comparison B–F: P0=0, P1=0, P2=0; новых actionable drift нет.
6. Post-review comparison каталога: source panel F, новый full-shell и focused implementation
   открыты одним input после замены select на scope-индикаторы. Плотность filter bar сохранена,
   overflow не появился; P0=0, P1=0, P2=0. Разница 5 source-строк против 3 implementation-строк
   обусловлена контролируемыми API-данными и не меняет геометрию компонента.
7. Аннотация admin: удалены Telegram-card, её client-state и fetch. Source D и новый browser
   capture открыты в `comparison-admin-full.png`, card regions — в `comparison-admin-focus.png`.
   Две карточки выровнены по верхнему краю, оба `gridRowEnd=auto`; P0=0, P1=0, P2=0.

## Интеракции и технические проверки capture

- Проверены: переключение всех вкладок мышью и клавишами Arrow/Home/End с переносом и фокусом,
  связь tab/tabpanel во всех пяти контекстах и deny-state, три checkbox, immediate CSV download,
  повтор, создание web-парсера, validation EventSource с успешным и оборванным соединением,
  безопасные parser-target links, выбор queue item, verdict CTA, active AI SSE.
- Последний capture завершён exit 0: `console_errors=[]`, `page_errors=[]`,
  `target_http_errors=[]`.
- Responsive runtime: 1440/1200/992/735/390 × light/dark, без root overflow и обрезанных
  постоянных контролов.
- `pytest tests/loophole`: **687 passed, 1 skipped**, 15 предупреждений зависимостей,
  **68,44 с**.
- `pytest tests/loophole/test_final_layout_runtime.py`: **19 passed** за **47,42 с**.
- `ruff check src/bank_audit/loophole tests/loophole`: **All checks passed**.
- `git diff --check`: whitespace-ошибок нет; Git вывел только существующие предупреждения
  Windows LF/CRLF.

## Итерация 2026-08-31: удаление Telegram-status

- Source visual truth: пользовательская Browser-аннотация к блоку «Статус Telegram-целей»;
  сохранённый визуальный baseline — `workspace/qa/loophole-final/source-panel-d-admin.png`
  (**544×512 px**, около 96 dpi).
- Implementation: `workspace/qa/loophole-admin-telegram-removal/implementation-admin-without-telegram.png`
  (**843×864 px**), CSS viewport **843×864**, `deviceScaleFactor=1`; iframe **841×806**.
- Состояние: `/#loophole` → «Управление доступом», светлая тема, роль module_admin,
  active/revoked роли и агрегированный audit.
- Full-view same-input evidence:
  `workspace/qa/loophole-admin-telegram-removal/comparison-admin-full.png` (**1068×552 px**);
  implementation нормализован до высоты source 512 px без изменения пропорций.
- Focused same-input evidence:
  `workspace/qa/loophole-admin-telegram-removal/comparison-admin-focus.png` (**1255×434 px**);
  карточки открыты в одном comparison input, исходные crop-пропорции сохранены.
- Fonts/typography: семейство, веса, иерархия H1/H2 и табличный текст не изменялись.
- Spacing/layout: две карточки заняли обе grid-колонки, верхние края совпадают, пустой второй ряд
  удалён. Внутренний горизонтальный scroll таблицы ролей на 843 px существовал до правки и
  остаётся доступным; это P3 вне точечной аннотации.
- Colors/tokens: сохранены `--panel`, `--border`, `--accent` и светлая AuditLens-палитра.
- Image/assets: на поверхности нет растровых или декоративных assets; ничего не заменялось.
- Copy/content: «Статус Telegram-целей» и empty-copy удалены; тексты ролей и аудита неизменны.
- Primary interactions: переход на вкладку и «Обновить» выполнены в in-app Browser; после
  обновления видны ровно две секции. Console errors: **0**; остались только штатные предупреждения
  Babel-standalone, обусловленные архитектурой frontend без сборки.
- Regression: `test_admin_roles_audit.py` + `test_final_layout_runtime.py` — **52 passed**;
  targeted Ruff — **All checks passed**.

final result: passed

## Research report export 2026-08-31

- Browser-runtime сценарий создал ответ с заголовком и списком, передал server-side
  `snapshot_id=73` через SSE и подтвердил в карточке «Доказательства и источники» доступное
  меню «Скачать исследование» с URL PDF и Word.
- Safe Markdown использует только React text nodes: HTML из ответа не исполняется. Отдельный
  серверный тест подтверждает HTML escaping PDF-рендера и честную отметку об отсутствии
  проверенных доказательств.
- Выполнено: targeted Browser-runtime + server acceptance — **6 passed**; scoped Ruff —
  **All checks passed**. Ручной in-app Browser QA, реальные файлы PDF/Word и light/dark
  comparison пока не выполнены; это остаётся verification gap.

## Итерация 2026-08-31: clarification composer и theme-aware AI-панель

### Источник, implementation и нормализация

- Source visual truth: `workspace/qa/loophole-final/source-panel-b-ai-research.png`,
  **516×512 px**, состояние B финального board. Source показывает выполняющееся исследование;
  для текущего bugfix он задаёт композицию AI-поверхности, типографику, светлые semantic surfaces,
  hairline-границы, красный accent и solid AI-avatar, но не буквальный текст pending clarification.
- Browser-rendered implementation light:
  `workspace/qa/loophole-agent-chat-fix/implementation-clarification-light-1440x900.png`;
  focused chat: `implementation-clarification-light-focus.png` (**340×900 px**).
- Browser-rendered implementation dark:
  `workspace/qa/loophole-agent-chat-fix/implementation-clarification-dark-1440x900.png`;
  focused chat: `implementation-clarification-dark-focus.png` (**340×900 px**).
- CSS viewport и PNG light/dark: **1440×900**, `devicePixelRatio=1`; root
  `scrollWidth/clientWidth=1440/1440`. Source в comparison-файлах пропорционально
  нормализован до высоты 900 px без crop; implementation не масштабирован.
- Full-view same-input evidence:
  `workspace/qa/loophole-agent-chat-fix/comparison-full-light.png` и
  `comparison-full-dark.png` (оба **2363×936 px**).
- Focused same-input evidence:
  `workspace/qa/loophole-agent-chat-fix/comparison-focused-light-dark.png`
  (**1619×936 px**). Focused pass обязателен: в full-view текст chat bubble и composer слишком
  мал для надёжной проверки типографики, статуса и границ.

### Состояние и browser-flow

- Детерминированный input в обеих темах одинаков:
  `найди лазейки` → assistant bubble `Какой банк исследовать?` → composer-answer `Сбербанк`.
- `/clarify/answer` намеренно задержан; captures сняты до resolve. Видны ровно два user bubble,
  один assistant question bubble, typing bubble и непрерывный статус `Обдумывает ответ`.
- Проверены клики по вкладке, ввод и отправка исходного запроса, смена composer в answer-mode,
  optimistic answer, disabled composer во время submit и сохранение chat state при hide/open.
- Captures получены через in-app Browser API. После финального reload у light/dark
  `console errors=[]`; DOM подтверждает `Тема: найди лазейки`, фазу `Ожидает уточнения` и
  отсутствие текста clarification-вопроса в research evidence.

### Обязательные fidelity surfaces

- **Fonts/typography:** сохранены Source Serif 4 для заголовков, Geist для интерфейса и
  JetBrains Mono для eyebrow/meta; веса, line-height и иерархия source-панели не дрейфуют.
- **Spacing/layout rhythm:** panel остаётся правой колонкой 340 px на 1440 px, messages имеют
  общий scroll-контейнер, composer закреплён снизу; bubble alignment и вертикальный ритм
  одинаковы в light/dark, root overflow отсутствует.
- **Colors/tokens:** light sidebar использует `--surface/--paper-2/--ink/--hair/--accent`,
  а dark — те же semantic tokens через `html.dark`; постоянного чёрного полотна в light,
  локальной палитры и gradient-avatar нет. Красный accent сохранён как solid avatar,
  active phase и hairline user-bubble border.
- **Image/asset fidelity:** на проверяемой панели нет растровых иллюстраций или product imagery.
  Source использует буквенный `AI` badge; implementation сохраняет тот же solid text badge,
  не заменяя существующий asset CSS-рисунком, SVG или emoji.
- **Copy/content:** slash-команды удалены; text-challenge показан один раз в assistant bubble,
  ответ — один раз в user bubble; internal `await_clarify` локализован; исходный запрос не
  подменяется ответом `Сбербанк` в research summary.
- **Interaction/accessibility:** composer имеет label, question/answer читаются в DOM,
  busy-state не мигает в `Готов`, light/dark контраст проходит существующий WCAG guard,
  desktop/off-canvas flow закреплён runtime-тестами; focus-trap и возврат фокуса не изменены.

### Findings и история сравнения

1. Первый same-input выявил P2 copy/content drift: optimistic answer становился темой
   исследования, raw `await_clarify` попадал в H2/meta, а clarification question повторялся
   в evidence помимо assistant bubble.
2. Исправление: marker-сообщения `_clarificationAnswer` и `_clarificationQuestion` исключены
   из research summary; `await_clarify` получил label `Ожидает уточнения`.
3. Post-fix RED→GREEN: точный browser-test прошёл; revised light/dark captures открыты вместе
   с source в full и focused comparison input. P0=0, P1=0, P2=0.

### Остаточные различия

- Source B показывает running-state 66% и production-подзадачи, implementation — требуемый
  pending-clarification/busy-state 0%. Это намеренная state-разница, поэтому точные числа и
  evidence-copy не оцениваются как visual drift.
- Source crop включает внешний левый shell, standalone browser-fixture — фактическую модульную
  поверхность. Сетка AI-route и chat оцениваются по content region; предыдущий full-shell QA
  выше остаётся evidence неизменённой оболочки.
- Actionable P0/P1/P2 после revised comparison отсутствуют; P3 findings нет.

### Финальная верификация итерации

- Browser-runtime: **27 passed** за **76,90 с**.
- Полный `tests/loophole`: **695 passed, 1 skipped**, 15 предупреждений зависимостей,
  **104,04 с**.
- Scoped Ruff по модулю, тестам и двум QA-скриптам: **All checks passed**.

final result: passed

## Повторный review-pass 2026-08-31: устойчивость после ответа

- Same-input заново воспроизведён через встроенный Browser в light/dark на CSS viewport
  **1440×900**, `devicePixelRatio=1`; root в обеих темах — `1440/1440`, ошибок консоли нет.
- Контрольные кадры:
  `workspace/qa/loophole-agent-chat-fix/implementation-clarification-review-light-1440x900.png`
  и `implementation-clarification-review-dark-1440x900.png`; focused chat —
  `implementation-clarification-review-light-focus.png` и
  `implementation-clarification-review-dark-focus.png` (**340×900 px**).
- Объединённые comparison inputs:
  `workspace/qa/loophole-agent-chat-fix/comparison-review-full-light.png`,
  `comparison-review-full-dark.png` и `comparison-review-focused-light-dark.png`.
- В обеих темах DOM подтверждает ровно два user bubble (`найди лазейки`, `Сбербанк`), один
  assistant question bubble (`Какой банк исследовать?`), видимый typing bubble и непрерывный
  статус `Обдумывает ответ`. Тема исследования остаётся `найди лазейки`, заголовок прогресса —
  `Ожидает уточнения`, текст вопроса в evidence отсутствует.
- Full и focused comparison с исходным состоянием B не выявили нового визуального дрейфа:
  типографика, semantic surfaces, hairline-границы, red accent, ширина панели и закреплённый
  composer сохранены. Actionable findings: **P0=0, P1=0, P2=0**.
- Review verification: backend — **135 passed**; browser-runtime — **32 passed**;
  полный `tests/loophole` — **702 passed, 1 skipped**, 15 предупреждений; scoped Ruff модуля,
  тестов и QA-скрипта — **All checks passed**.

final result: passed

## Real-provider review 2026-08-31: запрос с продуктом и периодом

- Во встроенном Browser выполнен исходный пользовательский запрос про кредитную карту за август
  2026 года. До первого ответа DOM фиксировал `questions=0` и фазу `Выполнение`: известные продукт
  и период не вызвали повторного clarification.
- Исследование завершилось фазой `Готово`; в чате остались ровно исходный user bubble и итоговый
  assistant bubble, сырого `Error calling LLM: Connection error.` нет. Console errors: **0**.
- Network/backend evidence: `/api/loophole/chat` — **200 OK**; вызовы
  `https://foundation-models.api.cloud.ru/v1/chat/completions` — **HTTP 200 OK**.
- Light capture: `workspace/qa/loophole-agent-chat-fix/real-browser/final-light-top-843x864.png`;
  dark capture: `workspace/qa/loophole-agent-chat-fix/real-browser/final-dark-top-843x864.png`;
  нижняя часть длинного результата:
  `workspace/qa/loophole-agent-chat-fix/real-browser/final-light-bottom-843x864.png`.
- Один comparison input с исходной композицией и обеими темами:
  `workspace/qa/loophole-agent-chat-fix/comparison-real-final-light-dark.png`. Light/dark сняты
  при одинаковом viewport **843×864** и одном состоянии; исходный reference пропорционально
  нормализован до общей высоты без crop.
- Логическая правка не изменила layout: ширина off-canvas панели, закреплённый composer,
  semantic surfaces, hairline-границы, solid red avatar и phase chips совпадают между темами;
  обрезанных постоянных контролов и горизонтального overflow не обнаружено. Actionable findings:
  **P0=0, P1=0, P2=0**.
- Verification: backend review subset — **142 passed**; browser-runtime — **33 passed**;
  полный `tests/loophole` — **706 passed, 1 skipped**, 15 предупреждений; scoped Ruff модуля,
  тестов и QA-скрипта — **All checks passed**.

final result: passed

## Real-provider review 2026-08-31: строгая дата публикации и нейтральный банк

- Во встроенном Browser выполнен запрос: «Найди одну лазейку по продукту кредитная карта за август
  2026 года. Если дата поста раньше августа 2026 года, не выводи её.» Банк намеренно не указан.
  DOM финального состояния: `messages=2`, `questions=0`, статус `Готово`.
- Агент не повторил запрос о продукте/периоде и не добавил неявный банк. Когда подходящих
  первоисточников с подтверждённой датой публикации не оказалось, ответ честно сообщил об
  отсутствии допустимых фактов; диапазон не был расширен до более ранних материалов.
- Сырой provider-текст `Error calling LLM: Connection error.` не отображается; console errors —
  **0**. Проверены light и dark при одном готовом состоянии. Captures:
  `workspace/qa/loophole-agent-chat-fix/real-browser/period-filter-light-345x815.png` и
  `workspace/qa/loophole-agent-chat-fix/real-browser/period-filter-dark-345x815.png`.
- Сводный comparison input:
  `workspace/qa/loophole-agent-chat-fix/comparison-period-filter-light-dark.png`. Фактический
  размер panel captures — **345×815**; проверены границы, composer, phase chips и отсутствие
  визуального crop/overflow. Actionable findings: **P0=0, P1=0, P2=0**.
- Финальная проверка: `tests/loophole` — **716 passed, 1 skipped**, 21 предупреждение; scoped Ruff
  — **All checks passed**.

final result: passed
