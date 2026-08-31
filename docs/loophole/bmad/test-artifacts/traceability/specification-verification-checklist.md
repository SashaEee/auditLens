# Итоговый чек-лист верификации спецификаций «Лазейки»

Дата среза: 2026-08-31. Критерий `done` — каждый критерий спецификации имеет трассу на
исполняемые зелёные тесты в полном прогоне. `in-progress` означает, что код/тесты существуют,
но остаётся конкретный незакрытый критерий. `superseded` — исходная формулировка намеренно
заменена более поздним требованием пользователя.

## Общий результат

- [x] Полный прогон: `722 passed, 1 skipped, 21 warnings` за 1 минуту 56 секунд.
- [x] Исправлена SQLite test integration для `loophole_research_report`; Docker-база не
  изменялась, потому что она не была причиной падения.
- [x] Полный `ruff check src/bank_audit/loophole tests/loophole`: PASS.
- [x] `git diff --check`: PASS; только неошибочные предупреждения Git о будущем LF→CRLF.
- [x] In-app Browser: базовый экран исследования, открытие/закрытие панели, focus return и
  отсутствие console errors.
- [x] In-app Browser: подтверждено, что новая заявка на parser ещё не реализована — вкладка
  показывает старые «Создать и проверить» и «Подключение Telegram».
- [ ] Quality gate: **FAIL** — 14/28 спецификаций завершены; P0 coverage 45%, P1 coverage 53%.

## Удаление Telegram-контура

- [x] RED-контракт подтвердил исходное наличие endpoint/runtime/UI, затем
  `tests/loophole/test_telegram_contour_removed.py` прошёл: 3 passed.
- [x] Целевая регрессия обычных функций прошла: 78 passed (RBAC, parser request,
  dedup, generator и отсутствие Telegram-контура).
- [x] Browser-smoke изменённого UI прошёл: 2 passed; безопасны только HTTP(S)-ссылки
  parser targets, админ-поверхность не делает Telegram-запросов.
- [ ] Полный модульный pytest, scoped Ruff и `git diff --check` — ожидают этого
  implementation-cycle. Миграции `046`, `054`–`057` сохранены исторически и не
  применялись повторно.

## Статусы спецификаций

| Статус | Спецификации | Основание |
|---|---|---|
| `done` | 1.1–1.4, 2.1–2.5, 5.2, 6.1–6.4 | Каждый критерий имеет свежую трассу на passing tests |
| `in-progress` | 3.1–3.3, 4.1–4.4, 5.3, 6.5, читаемый отчёт AI-исследования | Есть частичная реализация, но указанные в матрице критерии не доказаны |
| `draft` | Перенос preliminary research-source; заявка на разработку parser | Нет route/service/migration/test реализации |
| `superseded` | 1.5, 5.1 | Заменены пользовательскими решениями: убрать Telegram-card; заявка вместо автогенерации parser |

## Открытые обязательные действия

- [ ] Реализовать перенос новых источников исследования в общую базу как «Предварительно» с
  probability, provenance, фильтрами верификации и fail-closed ownership.
- [ ] Заменить «Создать и проверить» на pending-заявку `source_proposal(purpose='loophole_parser')`,
  убрать Telegram-card и покрыть 201/409/422/error/browser scenarios.
- [ ] Сохранять реальный immutable verified evidence snapshot в research report и экспортировать
  URL/текст из него в PDF/DOCX.
- [ ] Закрыть lifecycle gaps: route-level role denial/concurrency/retry, PostgreSQL role/timeout,
  schedule owner/capability/expiry и parser schedule audit/Telegram-isolation.
- [ ] Предоставить отдельные staging/production-like evidence для PostgreSQL lifecycle и Telegram
  perimeter; до этого эти внешние gate остаются `UNVERIFIED`.

Подробнее: `traceability-matrix.md`, `phase-1-coverage-20260831.json`,
`e2e-trace-summary.json`, `gate-decision.json` в этом же каталоге.
