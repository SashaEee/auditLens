---
title: 'Авторизованный выбор рабочего контекста'
type: 'feature'
created: '2026-08-29'
status: 'done'
review_loop_iteration: 0
context:
  - 'docs/loophole/bmad/implementation-artifacts/epic-1-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Модуль «Лазейки» доверяет произвольному `X-User-Id`, создаёт workspace до авторизации и не различает каталог, новое AI-исследование и очередь ЦК КС. Пользователь без роли может обращаться к legacy API по прямому URL, а отзыв роли не действует на уже открытую сессию.

**Approach:** Ввести server-side границу авторизации: trusted nginx-заголовки дают только identity, а membership и роли читаются из нового авторитетного хранилища при каждом защищённом запросе. UI получает список доступных контекстов от сервера и fail-closed очищает очередь при отказе; полноценная JWT-проверка OIDC остаётся отдельной инфраструктурной задачей.

## Boundaries & Constraints

**Always:** Считать `X-Authentik-*` identity только за trusted nginx; запрос без аутентифицированного principal отклонять до чтения данных. Хранить membership и назначение `ccks_expert` в БД, перепроверять их на каждом защищённом endpoint. Всегда возвращать каталог и создание AI-исследования активному члену workspace, очередь — только активному `ccks_expert`. При deny не возвращать данные очереди, кейсов или источников. Миграция `042_loophole_authorization.sql` идемпотентна; последующие зарезервированные миграции должны быть перенумерованы в своих историях.

**Ask First:** Реализовать pinned OIDC JWT/JWKS, менять контракт nginx или распространять новый RBAC на API и домены за пределами контекстов, workspace, очереди и тестов этой истории.

**Never:** Не доверять `X-User-Id`, role/capability из клиента или скрытию пункта UI как контролю доступа. Не создавать default workspace до успешной авторизации. Не реализовывать решения ЦК КС, публикацию, AI-исследование, адаптивную компоновку либо административное назначение роли: это следующие истории. Не делать коммит.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Доступный пользователь | Trusted principal с active membership | `GET /contexts` возвращает «Общую базу» и «Новое AI-исследование» | N/A |
| Эксперт ЦК КС | Active membership и active `ccks_expert` | `GET /contexts` дополнительно возвращает «Очередь верификации» | N/A |
| Прямой доступ без роли | Principal без `ccks_expert` запрашивает очередь | Server-side 403; UI показывает «Нет доступа к очереди верификации» и действие возврата | Никакие данные очереди не попадают в JSON или DOM |
| Отзыв роли | Роль отозвана после успешной загрузки очереди | Следующий запрос возвращает 403; клиент очищает ранее загруженные защищённые данные | Отображается тот же fail-closed экран |
| Недостоверная identity | Нет trusted principal либо нет active membership | Server-side 401/403 до workspace, очереди и данных | Не создавать workspace и не раскрывать контексты |

</frozen-after-approval>

## Code Map

- `src/bank_audit/web/auth.py` — существующий `CurrentUser` и trust boundary Authentik/nginx; использовать как единственный transport identity выбранного варианта.
- `src/bank_audit/loophole/web.py` — router `/api/loophole`, небезопасный `get_user_id` и endpoints workspace/history/chat; точка подключения dependency авторизации и contract endpoint контекстов.
- `src/bank_audit/loophole/repository.py` и `workspace.py` — текущие per-user workspaces; переиспользовать после server-side проверки membership, не принимать ID workspace как полномочие.
- `src/bank_audit/loophole/static/loophole.jsx` — старт приложения сейчас выполняет создание default workspace и загрузку до проверки; заменить на загрузку доступных контекстов и fail-closed экран.
- `src/bank_audit/loophole/static/loophole.html`, `src/bank_audit/web/app.py` — same-origin iframe и cache bust; сохранить механизм отдачи статики.
- `migrations/041_*` — последняя существующая миграция; новая `042_loophole_authorization.sql` должна следовать ей.
- `tests/loophole/conftest.py`, `tests/loophole/test_web.py` — SQLite fixture и FastAPI dependency overrides; расширить red/green тестами RBAC без сети и реальной БД.

## Tasks & Acceptance

**Execution:**
- [ ] `tests/loophole/test_authorization.py` и связанные fixtures — сначала зафиксировать RED для видимости роли, server-side deny, отзыва роли и отсутствия данных в ответе; затем довести реализацию до GREEN.
- [ ] `migrations/042_loophole_authorization.sql` — добавить идемпотентную схему principal/workspace membership/role assignment и redacted auth audit, ограничивающую активную роль ЦК КС; пройти structural contract-тест миграции.
- [ ] `src/bank_audit/loophole/authorization.py` и `repository.py` — минимально реализовать typed server-side principal, membership и role checks, не зависящие от входных role/workspace заголовков.
- [ ] `src/bank_audit/loophole/web.py` — заменить `X-User-Id` boundary на dependency trusted `CurrentUser` плюс authorization service; добавить endpoint доступных контекстов и защищённый endpoint очереди, а legacy workspace/history/chat оградить проверкой ownership/membership.
- [ ] `src/bank_audit/loophole/static/loophole.jsx` — не выполнять запросы данных до получения контекстов; показать разрешённые пункты, а при 403 очереди очистить состояние и отрисовать русскую fail-closed поверхность без защищённых данных.

**Acceptance Criteria:**
- Given active internal member, when он открывает модуль, then серверный контракт возвращает только доступные ему рабочие контексты.
- Given пользователь без роли ЦК КС, when он запрашивает очередь напрямую, then сервер отвечает отказом и клиент не хранит и не отображает защищённые данные.
- Given активный эксперт теряет роль, when происходит следующий запрос очереди, then сервер повторно авторизует запрос и UI очищает прежнее состояние.

## Spec Change Log

## Design Notes

Граница identity намеренно отделена от RBAC: nginx передаёт только уже проверенный subject, но никакой workspace или роль не берётся из заголовков. Это реализует выбранный временный контракт без подмены требуемой в будущем OIDC JWT-валидации.

## Verification

**Commands:**
- `pytest tests/loophole/test_authorization.py -q` — сначала ожидаемое RED-падение до production-кода, затем зелёный целевой набор.
- `pytest tests/loophole -q` — регрессии модуля проходят без сети и PostgreSQL.
- `.venv/Scripts/ruff.exe check src/bank_audit/loophole tests/loophole/test_authorization.py` — нет новых lint-ошибок в затронутом коде.

**Manual checks (if no CLI):**
- Открыть iframe как аналитик и эксперт ЦК КС; убедиться, что очередь видна только эксперту, а после отзыва роли прямой переход показывает отказ без карточек и источников.

