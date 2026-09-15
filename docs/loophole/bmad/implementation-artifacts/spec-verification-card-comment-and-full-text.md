---
title: 'Карточка проверки: комментарий участника ЦК и полный текст статьи'
type: 'feature'
created: '2026-09-10'
status: 'done'
baseline_revision: '2e36a830fe47ea1307caddc77b982c08a3582bd9'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: ['oversized']
deferred:
  - summary: >-
      Повторная загрузка контента после сбоя не выполняется: loadContent кэширует
      запись с error, guard `if (contentCache[id]) return;` не даёт ретрая —
      единственный путь повторить запрос, перезагрузка страницы.
    evidence: |-
      src/bank_audit/loophole/static/loophole.jsx:1994-2001 — после catch в contentCache
      остаётся truthy-запись {loading:false, data:null, error}, повторный вызов
      loadContent(id) выходит по guard. Поведение унаследовано от прежнего toggleContent
      каталога дословно (код перенесён, а не изменён) и переиспользование contentCache
      как есть предписано intent-contract. Для maybe-false-подобного уточнения: спорно
      только если трактовать строку «Повтор — при повторном выборе записи» I/O-матрицы
      как безусловное обещание; AC5 ретрай не обещает.
    location: >-
      src/bank_audit/loophole/static/loophole.jsx:1994-2001
    severity: low
  - summary: >-
      React-семантика изменения (исполнение useEffect, биндинг textarea, порядок
      хуков) нигде не исполняется: весь фронт-тестовый слой — текстовые контрактные
      проверки по образцу test_adaptive_context_routes.py.
    evidence: |-
      В репозитории нет сборки и JS-раннера (tests/loophole/test_adaptive_context_routes.py:
      «сборки и UI-стенда нет»), текстовые проверки — общепринятый в репозитории
      способ верификации фронтенда; порядок хуков и размещение useEffect до основного
      return (loophole.jsx:2020-2023 vs 2131) проверены вручную при ревью.
    location: >-
      tests/loophole/test_queue_card_comment_and_full_text.py
    severity: low
---

<intent-contract>

## Intent

**Problem:** В карточке проверки на вкладке «Очередь верификации» комментарий участника ЦК можно ввести только внутри модалки вердикта, а полный текст статьи/поста в карточке не виден (только snippet и переход на внешний источник). Эксперт не может в одном месте прочитать материал целиком и зафиксировать свой комментарий.

**Approach:** В самый низ карточки проверки добавить поле комментария участника ЦК (общее состояние с полем модалки вердикта, сохраняется существующим `POST /records/verdict`), а под ним — блок полного текста записи, лениво догружаемый существующим `GET /records/{record_id}/content`. Изменения только во фронтенде и CSS; бэкенд не меняется.

## Boundaries & Constraints

**Always:**
- Порядок в самом низу карточки: сначала поле комментария участника ЦК, ниже — полный текст; существующие секции карточки (включая «Комментарий классификатора» — это причина классификатора) остаются на местах.
- Полный текст переиспользует существующие `contentCache` / `toggleContent` / `renderRecordContent`; повторный выбор записи не повторяет сетевой запрос.
- Комментарий сохраняется только через существующий вердикт-флоу: поле карточки и поле модалки — одно состояние `markComment`.
- Комментарий не переносится между записями: при смене выбранной записи поле очищается.
- Fail-closed очереди и гейт `canMarkVerdict` сохраняются: без права вердикта поле комментария не отображается.

**Never:**
- Не менять `GET /queue` и `repository.list_verification_queue`: `raw_text` в списке не отдаётся намеренно (payload).
- Не создавать новый эндпоинт или персистентность комментария вне вердикта.
- Не убирать модалку вердикта и её поле комментария.
- Не менять бэкенд (`web.py`, `repository.py`) — существующих контрактов достаточно.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Полный текст доступен | `content_status=full` | Блок в карточке показывает `raw_text` целиком в прокручиваемом контейнере, без клика «Развернуть полностью» | No error expected |
| Текст обрезан | `content_status=truncated` | Показывается сохранённый текст с бейджем «обрезан до N КБ» | No error expected |
| Контент не загружен | `content_status=fetch_failed` или `empty` | Блок с пояснением «Полный контент не удалось загрузить…», карточка работоспособна | Не ломает рендер карточки |
| Ошибка/загрузка контента | Сеть/HTTP != 200 или запрос в полёте | Блок ошибки или «Загрузка контента…» со ссылкой на источник | Повтор — при повторном выборе записи (кэш пуст) |
| Нет права вердикта | `canMarkVerdict=false` | Поле комментария не отображается, полный текст доступен | — |
| Смена выбранной записи | Выбран другой `record_id` | Поле комментария пустое, полный текст подгружается для новой записи | — |

</intent-contract>

## Code Map

- `src/bank_audit/loophole/static/loophole.jsx` -- единственный файл UI. Карточка `article.lp-queue-detail` (2766–2792): eyebrow, grid, секция `lp-queue-reason` «Комментарий классификатора» (2777–2780), actions (2781–2790); обработчик «Проверить вердикт» (2786–2789) делает `setMarkComment("")` при открытии модалки — сброс убрать. Состояния: `markComment` (402), `queueSelectedId` (459). `markVerdict` (951–986) — POST `/records/verdict` с `comment`; `loadQueue` (989–1029, авто-выбор первой записи); `queueSelected` (1925–1927). Контент: `toggleContent` (1994–2006, GET `/records/{id}/content` → `contentCache`), `renderRecordContent` (2028–2070 — loading/error/fetch_failed, бейджи, «Развернуть полностью» >2000 знаков), `fullView`. Модалка вердикта: textarea к `markComment` (3177–3182), submit `choose()` → `markVerdict([rec.record_id], val, markComment.trim())` (3146–3149).
- `src/bank_audit/loophole/static/loophole.css` -- `.lp-queue-detail` (1059), `.lp-queue-reason` (1083), `.lp-queue-detail-actions` (1092); контент: `.lp-content-block` (2106), `.lp-content-body` (2129, clamp 2139), `.lp-content-body-full` (2147, прокрутка 70vh); образец поля ввода: `.lp-verdict-field` (1874–1904).
- `src/bank_audit/loophole/web.py` -- только чтение: GET `/queue` (96–107, `require_role` ccks_expert), GET `/records/{record_id}/content` (468–492 → `raw_text`, `content_status`, `raw_text_len`, `raw_text_truncated`, `fetched_at`), POST `/records/verdict` (568–628; `reason = body.comment or f"manual:{user_id}"`, стр. 585).
- `src/bank_audit/loophole/repository.py` -- только чтение: `list_verification_queue` (839–856), `raw_text` намеренно не отдаётся.
- `tests/loophole/test_adaptive_context_routes.py` -- образец контрактных UI-тестов `_jsx()/_css()/_norm()/_block()` по тексту файлов (сборки и UI-стенда нет).

## Tasks & Acceptance

**Execution:**
- `src/bank_audit/loophole/static/loophole.jsx` -- В конец `article.lp-queue-detail` после `lp-queue-detail-actions` добавить: (а) секцию поля «Комментарий участника ЦК» — textarea с `value=markComment` / `onChange=setMarkComment`, рендер только при `canMarkVerdict`; (б) секцию полного текста — вызов `renderRecordContent(queueSelected)`; автозагрузку контента при смене выбранной записи (useEffect по `queueSelected?.record_id` → `toggleContent(id)`); сброс `setMarkComment("")` при смене записи; убрать сброс `setMarkComment("")` из обработчика «Проверить вердикт» (2787), чтобы комментарий из карточки попадал в модалку; расширить `renderRecordContent` опцией «всегда развёрнут» (класс `lp-content-body-full`, без кнопки «Развернуть полностью»), карточка использует её. -- Интент: поле комментария и полный текст в самом низу карточки.
- `src/bank_audit/loophole/static/loophole.css` -- Стили секции комментария карточки по образцу `.lp-verdict-field` и отступы секции полного текста; проверить, что развёрнутый `.lp-content-body-full` внутри карточки не ломает сетку (прокрутка внутри блока). -- Визуальная целостность карточки.
- `tests/loophole/test_queue_card_comment_and_full_text.py` -- Новый контрактный тест в стиле `_jsx()/_norm()` из `test_adaptive_context_routes.py`, покрывающий все AC и сценарии I/O-матрицы: порядок секций внизу карточки, привязка `markComment`, отсутствие сброса при открытии модалки, сброс при смене записи, always-full блок контента, гейт `canMarkVerdict`, переиспользование `contentCache`. -- Прогоняемая проверка приёмки.

**Acceptance Criteria:**
- Given открыта вкладка «Очередь верификации» и выбрана запись, when пользователь просматривает карточку проверки, then последними секциями карточки идут поле «Комментарий участника ЦК» и ниже — блок полного текста записи.
- Given в поле карточки введён комментарий, when эксперт открывает «Проверить вердикт» и подтверждает выбор, then введённый комментарий уходит в POST `/records/verdict` как `comment` (не сбрасывается при открытии модалки).
- Given в поле введён текст для записи A, when пользователь выбирает запись B, then поле комментария пустое, а блок текста соответствует записи B.
- Given у записи `content_status=full`, when карточка открыта, then полный текст виден в карточке целиком в прокручиваемом блоке без дополнительных кликов.
- Given `content_status=fetch_failed`/`empty` либо ошибка загрузки контента, when карточка открыта, then блок показывает состояние сбоя с пояснением и ссылкой на источник, карточка остаётся работоспособной.
- Given `canMarkVerdict=false`, when карточка открыта, then поле комментария не отображается, полный текст доступен.

## Spec Change Log

## Review Triage Log

### 2026-09-10 — Review pass

Слои: blind-hunter (7), edge-case-hunter (2), verification-gap (2), intent-alignment (0 — расхождений интента и diff не найдено). Верификация: claim каждой находки проверена по исходникам (loophole.jsx 397–460, 945–1030, 1925, 1990–2104, 2340–2410, 2758–2832, 3183–3222; loophole.css 1059–1110, 1874–1904, 2147).

- verdicts: 11 findings — high 0, medium 1, low 8, false 2, maybe-false 0
- findings:
  - `[low]` `[defer]` Текстовые проверки не исполняют React-семантику (эффект, биндинг, порядок хуков) — нет сборки и JS-раннера в репозитории; текстовые контрактные тесты — установленный способ верификации фронтенда; размещение useEffect до основного return проверено вручную. Группа с находкой verification-gap ниже; см. `deferred`.
  - `[low]` `[defer]` Нет ретрая контента после сбоя: `loadContent` кэширует error-запись, guard блокирует повтор — поведение унаследовано от прежнего `toggleContent` каталога дословно (код перенесён, не изменён), переиспользование contentCache как есть предписано intent-contract. Группа с находкой edge-case ниже; см. `deferred`.
  - `[false]` Отсутствие `loadContent` в deps useEffect — двойной fetch невозможен: эффект не перезапускается при неизменном `queueSelectedRecordId`, размещение хука до условного return (компонентный return на 2131) проверено.
  - `[low]` `[reject]` Хрупкие exact-substring-проверки (`count == 2` на onChange) — намеренный pin: легитимное расширение даёт громкий понятный падёж теста, тихой поломки нет; ослабление — не прямая правка.
  - `[false]` I/O-матрица «Повтор — при повторном выборе записи» якобы противоречит коду — скобка «(кэш пуст)» в той же строке обусловливает обещание, AC5 ретрая не требует; поведение совпадает с каталогом, чей reuse предписан контрактом.
  - `[low]` `[reject]` Дублирование хелперов-regex между двумя тест-файлами — дрейф сигнатуры роняет оба файла громко (`assert m`), извлечение общего модуля — рефакторинг сверх прямого исправления.
  - `[low]` `[reject]` `.lp-queue-fulltext h3` повторяет стили `.lp-queue-reason h3` — косметика, пользователи не встречают; общий селектор — рефакторинг.
  - `[low]` `[defer]` Edge-case: ретрай после закэшированной ошибки недостижим — та же находка, что строкой выше (тот же корень и локация), группа defer, см. `deferred`.
  - `[low]` `[reject]` Claim: в Execution спеки effect вызывает `toggleContent(id)`, код — `loadContent(id)` — отклонение заявлено implement-этапом и корректно: буквальный toggleContent раскрывал бы строки каталога при просмотре карточки (нарушение Always контракта); фикс — правка спеки.
  - `[low]` `[defer]` Verification-gap: React-семантика не исполняется ни одним тестом — тот же корень, что у первой находки blind-hunter; группа defer, см. `deferred`.
  - `[medium]` `[patch]` Каталожный сброс `setMarkComment("")` (loophole.jsx:2377) стал несущим барьером — поле карточки делает markComment постоянным черновиком, и без сброса черновик записи A ушёл бы в вердикт записи B из каталога; ни один тест это не пинил (удаление сброса не роняет ничего). Применён patch: добавлен `test_catalog_verdict_button_resets_foreign_draft` в tests/loophole/test_queue_card_comment_and_full_text.py. Прогон после патча: файл 10 passed; полный tests/loophole 1075 passed, 3 skipped; ruff clean; diff переписан.

## Auto Run Result

**Summary.** В самом низу карточки проверки вкладки «Очередь верификации» добавлены две секции: поле «Комментарий участника ЦК» (общее состояние `markComment` с полем модалки вердикта, рендер только при `canMarkVerdict`, сохраняется существующим `POST /records/verdict`) и ниже — блок полного текста записи (`renderRecordContent(queueSelected, {alwaysFull: true})`, ленивая догрузка существующим content-эндпоинтом, прокрутка внутри блока). Комментарий сбрасывается при смене выбранной записи (useEffect по `queueSelected.record_id`) и больше не сбрасывается при открытии «Проверить вердикт». Бэкенд не менялся.

**Files changed:**
- `src/bank_audit/loophole/static/loophole.jsx` — `toggleContent` разделён на `loadContent` (загрузка в contentCache) и `toggleContent` (раскрытие строки каталога + загрузка); useEffect сброса комментария и автозагрузки контента по смене выбранной записи очереди; `renderRecordContent` получил `opts.alwaysFull` (без кнопки «Развернуть полностью»); в карточку добавлены секции комментария и полного текста; убран сброс комментария из обработчика «Проверить вердикт» карточки (в каталоге сохранён).
- `src/bank_audit/loophole/static/loophole.css` — `.lp-queue-comment`, `.lp-queue-fulltext` (+h3, переопределение `max-height: 48vh` для `.lp-content-body-full` внутри карточки).
- `tests/loophole/test_queue_card_comment_and_full_text.py` — новый контрактный тест (10 тестов после ревью): порядок секций, общее состояние `markComment`, выживание комментария при открытии модалки, pin каталожного сброса чужого черновика, сброс/перезагрузка при смене записи, contentCache, always-full без кнопки, состояния контента, гейт `canMarkVerdict`, CSS-оформление.
- `tests/loophole/test_adaptive_context_routes.py` — regex хелпера `_record_content_body` расширен до `\(r[^)]*\)` под новую сигнатуру `renderRecordContent`.
- `docs/loophole/bmad/implementation-artifacts/spec-verification-card-comment-and-full-text.md` — спека (этот файл).

**Review findings breakdown.** 11 находок от 4 слоёв: high 0, medium 1, low 8, false 2, maybe-false 0. Patch применён: 1 (medium — pin каталожного сброса `setMarkComment("")`, добавлен тест `test_catalog_verdict_button_resets_foreign_draft`). Deferred: 2 группы (ретрай контента после закэшированной ошибки; отсутствие JS-раннера — React-семантика не исполняется тестами). Отклонено 6: две `false` (deps useEffect — двойного fetch нет; формулировка I/O-матрицы о ретрае обусловлена скобкой «(кэш пуст)» и AC5 ретрая не требует) и четыре `low`-reject (хрупкие substring-пин-тесты — намеренный pin с громким падежом; дублирование тест-хелперов — дрейф роняет оба файла громко; дублирование h3-стилей — косметика; claim «toggleContent vs loadContent» — заявленное и корректное отклонение, фикс был бы правкой спеки). Отклонений интента не найдено (intent-alignment: diff реализует узкое чтение интента дословно).

**Follow-up review recommendation:** false. Patched-записей этого прохода: 1 (medium) — ни одной high и менее двух medium; конкретный непроверенный риск, который мог бы обосновать `true`, не назван. Patched counts by verdict: high 0, medium 1, low 0.

**Verification performed:**
- `python -m pytest tests/loophole/test_queue_card_comment_and_full_text.py` — 10 passed (после ревью-патча).
- `python -m pytest tests/loophole/test_queue_card_comment_and_full_text.py tests/loophole/test_adaptive_context_routes.py` — 39 passed (до патча, 9+30).
- `python -m pytest tests/loophole` — до патча 1074 passed, 3 skipped; после патча 1075 passed, 3 skipped.
- `python -m ruff check` на изменённых py-файлах — clean.
- Ручная инспекция при ревью: порядок хуков (useEffect до компонентного return, условных return нет), соответствие вызовов сигнатурам, CSS-специфичность переопределения `max-height`, поведение каталога не изменено.

**Residual risks:** React-семантика (исполнение эффекта, биндинг textarea) проверяется только текстовыми контрактами — стандартное для репозитория ограничение (нет сборки/JS-раннера); зафиксировано в `deferred`. Повторная загрузка контента после сбоя требует перезагрузки страницы — унаследованное поведение каталога, зафиксировано в `deferred`.

## Design Notes

- У комментария нет собственной персистентности: единственный путь сохранения — `comment` в `POST /records/verdict` (становится `verdict_reason`). Поле карточки и поле модалки — одно состояние `markComment`; отдельное хранение создало бы второй «комментарий» без требования из интента.
- Полный текст лениво догружается content-эндпоинтом: `GET /queue` намеренно лёгкий, `raw_text` в нём не отдаётся. `contentCache` исключает повторные запросы при возврате к записи. В карточке блок всегда развёрнут (`lp-content-body-full`, внутренняя прокрутка) — кнопка «Развернуть полностью» из спискового вида не нужна.
- «Комментарий классификатора» (`verdict_reason`) — причина классификатора; после ручного вердикта туда попадает введённый комментарий. Секции не путать: новая — поле ввода участника ЦК.

## Verification

**Commands:**
- `python -m pytest tests/loophole/test_queue_card_comment_and_full_text.py` -- expected: все тесты нового файла проходят.
- `python -m pytest tests/loophole` -- expected: без регрессий (включая `test_adaptive_context_routes.py`, `test_manual_mark.py`, `test_accessible_states_feedback.py`).

**Manual checks (if no CLI):**
- Запустить приложение под экспертом ЦК КС, открыть вкладку «Очередь верификации»: внизу карточки видны поле комментария и полный текст; введённый в карточке комментарий сохраняется вместе с вердиктом; при `fetch_failed` блок показывает пояснение, карточка работает.
