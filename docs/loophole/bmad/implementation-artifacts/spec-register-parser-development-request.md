---
title: 'Заявка на разработку парсера вместо автогенерации'
type: 'feature-change'
created: '2026-08-31'
status: 'done'
review_loop_iteration: 0
context:
  - 'migrations/019_source_proposals.sql'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Вкладка «Добавить источник» обещает немедленно создать и проверить парсер. Сейчас submit вызывает LLM-генератор, пишет код, окружение и запись `loophole_parser`, затем запускает валидацию. Фактический процесс другой: пользователь только регистрирует потребность в разработке нового парсера.

**Approach:** Превратить форму в заявку на разработку. После submit сохранить `pending`-запись в существующей очереди `source_proposal` с отдельным `purpose=loophole_parser`, вернуть номер заявки и показать подтверждение. Каталог уже подключённых парсеров оставить read-only. Карточку «Telegram-источники» удалить полностью.

## Boundaries & Constraints

**Always:** Принимать только `http(s)` URL и непустое описание; нормализовать домен; проверять владельца workspace; сохранять автора, URL, домен, требования и время; писать audit-action; использовать существующий partial unique index для одной активной заявки на домен.

**Ask First:** Статусы сверх `pending/approved/rejected`, уведомления, назначение разработчика, SLA, автоматическое создание или привязка готового парсера.

**Never:** Из формы вызывать LLM, `generate_parser`, runner, healer или scheduler; создавать `loophole_parser`, код, venv, зависимости либо validation-run; выдавать заявку за готовый источник; принимать Telegram-адреса, токены или приватные приглашения; менять старую миграцию `019`; создавать git-коммит.

## I/O & Edge-Case Matrix

| Scenario | State | Expected behavior | Error handling |
|----------|-------|-------------------|----------------|
| Новый веб-источник | URL и описание валидны, workspace свой | `201`, `request_id`, `status=pending`, одна строка `source_proposal`; toast с номером | Поля очищаются только после commit |
| Уже есть парсер | Нормализованная цель найдена в каталоге | Заявка не создаётся | `409`: источник уже подключён |
| Повторная заявка | Для домена уже есть `pending` | Вторая строка не создаётся | `409`: заявка уже зарегистрирована |
| Невалидный/Telegram URL | Не `http(s)` либо `t.me/telegram.me` | Ничего не записано | `422` с понятным текстом |
| Ошибка БД/сети | Commit не подтверждён | Форма и введённые значения остаются | Alert/toast без ложного успеха |

</frozen-after-approval>

## Code Map

- `migrations/019_source_proposals.sql:6-33` — очередь и `pending`-дедуп; без миграции.
- `repository.py:26-33,511-535`, `db.py:16-28` — заявка и audit в одной транзакции.
- `web.py:62-69,1130-1203`, `parsers/{dedup.py,registry.py}` — ownership, строгий URL, дедуп и замена только POST на `/parser-requests`; остальные API сохранить.
- `static/loophole.{jsx:651-743,1467,1726-1887,css:939-1000}` — форма, read-only каталог, одна колонка, без Telegram-card/log.
- `tests/loophole/{conftest.py,test_parsers_web.py,test_web.py,test_final_layout_runtime.py}` — DB/API/browser contracts.

## Tasks & Acceptance

**Execution:**
- [x] RED: `201` пишет только заявку; generator/runner/healer/scheduler не вызваны; старый POST=`405`; дубли/Telegram/invalid=`409/422`.
- [x] Реализовать repository + route: ownership, server-fixed `purpose/status`, target/domain-дедуп, атомарный audit/rollback.
- [x] Переписать форму: отдельные URL/описание, честные success/error, очистка только после `201`; удалить Telegram-card и legacy copy.
- [x] Сделать каталог этой вкладки read-only; не обновлять его заявкой, API управления не удалять.

**Acceptance Criteria:**
- Валидный submit даёт номер одной `pending`-заявки; parser/run/files/processes неизменны.
- В DOM нет Telegram-card и обещаний автосоздания; при ошибке ввод сохранён, ложного success нет.

## Design & Implementation Notes

- `description→reason`, `request_id=proposal_id`; workspace только проверяется и попадает в audit. Полная target-цель дедуплицирует parser, domain+index — заявку.
- Read-only — только UI вкладки. Success: `Заявка №<request_id> зарегистрирована`; без EventSource.

## Verification

**Commands:**
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_parsers_web.py tests/loophole/test_web.py -q`
- `.venv/Scripts/python.exe -m pytest tests/loophole/test_final_layout_runtime.py -q`
- `.venv/Scripts/python.exe -m pytest tests/loophole -q`
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole`
- In-app Browser: light/dark, `201` и `409/503`, console=0, screenshots + comparison.

## Результат проверки

- API-регрессия: `55 passed` (`test_web.py`, `test_parsers_web.py`); финальный набор маршрута и интерфейса — без падений.
- Browser-runtime: заявка, read-only каталог и отсутствие EventSource — `3 passed`.
- Живой локальный Browser: обновлённый заголовок и форма видны; Telegram-card и кнопки управления парсером отсутствуют; console error = 0.
- Ruff по изменённым production и тестовым файлам: `All checks passed!`.
