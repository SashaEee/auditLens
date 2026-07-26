# Контекст проекта: проблемы и решения

Формат: [ДАТА] Проблема: X → Решение: Y

[2026-07-26] Проблема: SQLite не понимает PostgreSQL-каст `:emb::vector` в `save_kb_example` — INSERT падал бы в SQLite-тестах → Решение: при `embedding=None` INSERT идёт без колонки embedding (две ветки SQL); в тестах `embedder.embed_one` мокается на сбой → срабатывает graceful fallback в `kb.add_example` (warning + embedding=None).

[2026-07-26] Проблема: `ruff check` по модулю loophole показывает 49+ предсуществующих ошибок (F401 и др.) — критерий «линтер без ошибок» недостижим → Решение: критерий переформулирован как «нет НОВЫХ ошибок»: сравнение worktree vs HEAD по затронутым файлам (15 vs 16 — стало на 1 меньше).

[2026-07-26] Проблема: `tests/test_smoke.py` — SyntaxError (bytes-литерал с не-ASCII) и `tests/test_digest.py::test_tg_parses_real_fixture` — UnicodeDecodeError (Windows, чтение фикстуры без encoding); оба предсуществующие на чистом HEAD → Решение: исключены из регрессионного прогона, вне скоупа фичи; не чинились.

[2026-07-26] Проблема: тесты `apply_migration` в трёх файлах жёстко проверяли `call_count == 2` — добавление миграции 014 их сломало → Решение: при добавлении новой миграции обновлять `call_count` в `test_db_schema.py`, `test_db_schema_011.py` и тесте новой миграции; имена констант (MIGRATION_011_PATH → файл 013) исторически расходятся с номерами файлов.

[2026-07-26] Проблема: ветка main содержала чужие незакоммиченные изменения в файлах плана (web.py, loophole.jsx, loophole.css, test_db_schema_011.py) → Решение: работа велась в новой ветке `feat/manual-verdict-marking` поверх рабочего дерева (`git switch -c` сохраняет изменения); hunks задач отделялись от чужих при ревью; коммиты не выполняются без подтверждения пользователя.

[2026-07-26] Проблема: фронт loophole.jsx нельзя проверить визуально без поднятого бэкенда и БД; toast маркировки (4 сек) исчезал до скриншота Playwright → Решение: стенд в `%TEMP%\kilo\lp-stand`: копии loophole.css/jsx + index.html с моком `window.fetch` на `/api/loophole/*` (workspace/banks/records/verdict), `python -m http.server`; Babel-ошибки видны в консоли браузера; для съёмки toast `window.setTimeout` глушится для ms===4000.

[2026-07-26] Проблема: «не подгрузились css стили для маркирования лазеек» — браузер кэшировал `loophole.css` эвристически (Starlette StaticFiles не шлёт Cache-Control), а `_loophole_html_with_bust()` версионировал только jsx; новый JSX + старый CSS = разметка без стилей → Решение: bust `?v=mtime` добавлен и для css (app.py); регрессионный тест `tests/loophole/test_static_bust.py`; импорт `bank_audit.web.app` в тесте требует `DATABASE_URL=sqlite:///:memory:` (conftest ставит sqlite+aiosqlite, но aiosqlite не установлен). После деплоя правки сервер без --reload нужно перезапустить.
