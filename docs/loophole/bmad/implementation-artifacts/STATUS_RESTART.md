# Checkpoint реализации Story «Лазейки»

Дата: 2026-08-30

Этот файл нужен для безопасного продолжения после перезапуска. Коммиты не создавались.

## Принятые Story

- Story 1.1–1.4 и `spec-loophole-refresh-button`: ранее проверены; целевой набор — `109 passed`.
- Story 1.5: завершена и принята после отдельного implementer-а, spec-review и quality-review. Проверки: `test_admin_roles_audit.py` — `32 passed`; полный `tests/loophole` на момент ревью — `564 passed, 1 skipped`; `git diff --check` — PASS.

## Story 2.1 — управляемый агент исследования

- Реализация внесена отдельным subagent и исправлена после quality-review.
- Последние подтверждённые проверки: targeted `89 passed, 398 warnings`; полный `tests/loophole` — `578 passed, 1 skipped, 1010 warnings`; `git diff --check` — PASS.
- Исправлены: обходы SQL `LIMIT -1`/`LIMIT +501`; восстановление mandatory clarification после `clarification_unavailable`; 16 новых Ruff diagnostics; безопасное поведение migration 044 в Greenplum 6.
- Quality-review главного интегратора от 2026-08-30: критических дефектов по критериям Story 2.1 не найдено. Подтверждено: целевые тесты — `89 passed`; полный `tests/loophole` — `578 passed, 1 skipped`; `git diff --check` — PASS. `ruff` показывает 32 уже существующие baseline-диагностики (сравнение с `HEAD`), новых нет. Реальный PostgreSQL/Greenplum staging отсутствует, поэтому migration harness честно остаётся `UNVERIFIED`.

## Story 5.1 — создание валидируемого парсера

- Устранены обязательные gaps TDD-циклом главным интегратором:
  1. `t.me`, `telegram.me` и `@handle` отклоняются до LLM и сохранения обычного parser.
  2. Неуспех validation сохраняет parser со статусом `validation_failed` и итоговый run; включение расписания до статуса `ready` возвращает 409.
  3. Исправлен `I001` в `tests/loophole/test_parsers_web.py`.
- Проверки: целевые parser tests — `29 passed, 1 warning`; полный `tests/loophole` — `583 passed, 1 skipped, 1 warning`; Ruff изменённых файлов и `git diff --check` — PASS.
- Следующий шаг: независимый spec/quality-review Story 5.1; реальная browser/production validation не запускалась.

## Story 6.1 — идемпотентная регистрация Telegram-цели

- Отдельный subagent добавил `src/bank_audit/loophole/telegram_targets.py` и `tests/loophole/test_story_6_1_target_registry.py`.
- Главный интегратор с явного разрешения пользователя восстановил одну синтаксически повреждённую строку после ACL-сбоя.
- Последняя проверка: `python -m py_compile src/bank_audit/loophole/telegram_targets.py` и целевой тест — `10 passed`.
- Открытый gap: `https://t.me:99999/bank_news` вызывает обычный `ValueError` при `parts.port`, а должен fail-closed через `InvalidTelegramTarget`; нужны RED-тест и минимальное исправление.
- Миграция не создана по правилу единого интегратора: для production нужна таблица `loophole_telegram_target` с уникальным `normalized_address`.
- Для продолжения требуется то же разрешение на точечные правки интегратором или восстановление прав `apply_patch` у subagent.

## Не начатые Story

- 2.2–2.5, 3.1–3.3, 4.1–4.4, 5.2–5.3, 6.2–6.5.

## Порядок и параллелизация

- Единственный владелец общих контрактов и миграций: главный интегратор. Не создавать параллельно миграции после `044_loophole_agent_audit.sql`.
- Критические общие файлы: `repository.py`, `db_schema.py`, `authorization.py`, `web.py`, `static/loophole.jsx`, `config.py`, `tests/loophole/conftest.py`.
- Зависимости: `2.2 → 2.3 → 3.3 → 2.5 → 3.1 → 3.2 → 4.1 → {4.2, 4.3} → 4.4`; `5.1 → 5.2 → 5.3`; `6.1 → 6.2 → 6.3 → 6.4 → 6.5`; также `6.2 → 2.5`, `6.5 → 4.3`.

## Важное ограничение среды

У subagent систематически падает штатный `apply_patch` с `windows sandbox failed: helper_unknown_error: apply deny-read ACLs`. Не использовать неявные обходы. Перед любым следующим точечным изменением главным интегратором требуется явное разрешение пользователя.
