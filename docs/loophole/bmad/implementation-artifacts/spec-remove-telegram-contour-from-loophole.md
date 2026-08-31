---
title: 'Удаление неиспользуемого Telegram-контура из модуля Лазеек'
type: 'refactor'
created: '2026-08-31'
status: 'in-progress'
review_loop_iteration: 0
baseline_commit: '40beb1c6cd8522603bfdc36b9edab8439f654812'
context:
  - 'docs/project-context.md'
  - 'docs/loophole/bmad/implementation-artifacts/spec-6-1-идемпотентная-регистрация-telegram-цели.md'
  - 'docs/loophole/bmad/implementation-artifacts/spec-6-5-защищённый-perimeter-и-доказательства-готовности-worker.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Проблема:** Telegram target/ingestion/worker был добавлен в «Лазейки», но сейчас не нужен.
Его мёртвые маршруты, UI, runtime-код и тесты создают ложную поверхность продукта и ломают
интеграционный прогон из-за отсутствующего deployment-артефакта.

**Подход:** удалить активный Telegram-контур только из модуля «Лазейки»: Python runtime,
маршруты, repository/UI-ссылки, module-specific fixtures и тесты. Уже применённые SQL-миграции
и таблицы остаются исторически совместимыми; код и документы остальных подсистем не меняются.

## Boundaries & Constraints

**Always:** ограничить diff `src/bank_audit/loophole`, `tests/loophole` и Loophole-specific
документацией; удалить все доступные пользователю Telegram actions/endpoint'ы; сохранить RBAC
остальных контекстов, обычные web parser requests и общий агент; удалить module-only тесты
вместе с функцией; зафиксировать removal в status/checklist.

**Ask First:** удалять исторические миграции/таблицы, внешние deployment-артефакты других
подсистем, shared Telegram-adapters или данные работающей БД.

**Never:** не менять код за пределами модуля «Лазейки»; не выполнять DDL/drop в БД; не
восстанавливать Telegram worker/perimeter; не заменять удалённую функцию скрытым feature flag;
не создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Открытие Лазеек | Обычный пользователь | Нет Telegram-вкладок, карточек, API-вызовов или фонового worker-а | Остальные вкладки работают как прежде |
| Старый Telegram URL | Клиент обращается к старому Loophole endpoint | Маршрут отсутствует | 404 без раскрытия состояния |
| Историческая БД | В ней есть таблицы миграций 046/054–057 | Приложение стартует без обращения к ним | Таблицы не удаляются и не мигрируются повторно |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/{telegram_targets,target_access,telegram_ingestion,telegram_worker,telegram_worker_sqlite,telegram_perimeter}.py` — удаляемые runtime boundaries.
- `src/bank_audit/loophole/{web.py,repository.py,authorization.py,parsers/dedup.py,parsers/generator.py,static/loophole.jsx}` — imports, routes, model-specific branches и UI-ссылки для очистки.
- `tests/loophole/test_story_6_{1,2,3,4,5}_*.py`, fixtures и связанные assertion'ы — удаляемые contracts; оставшиеся tests проверяют отсутствие маршрута и регрессию обычных функций.
- `docs/loophole/bmad/implementation-artifacts/{STATUS_RESTART.md,specification-verification-checklist.md}` — честный status/deprecation след.

## Tasks & Acceptance

**Execution:**
- [ ] Сначала создать RED-контракт отсутствия старого endpoint/import/UI-вызова, сохранив обычные contexts и parser request.
- [ ] Удалить Telegram-specific runtime, routes, repository hooks и module UI; устранить неиспользуемые imports/schema fixture и module tests.
- [ ] Оставить migrations 046/054–057 и существующие БД нетронутыми, обозначив их историческими в Loophole status.
- [ ] Запустить точечный absence/regression набор, затем полный модульный тест, ruff и Browser smoke.

**Acceptance Criteria:**
- Given запущенный модуль «Лазейки», when пользователь открывает все доступные вкладки, then Telegram UI, network requests и worker entry points отсутствуют, а catalog/source request/research/queue/admin продолжают работать.
- Given существующая БД с историческими Telegram-таблицами, when приложение запускается, then оно не читает и не изменяет их.
- Given любой файл вне Loophole scope, when проверяется diff, then он не изменён этим removal.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole -q -p no:cacheprovider --basetemp <new-owned-dir>` — PASS после обновления набора.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole` и `git diff --check` — PASS.
- Встроенный Browser: все вкладки, source request, research и admin без Telegram UI/requests, console без ошибок.
