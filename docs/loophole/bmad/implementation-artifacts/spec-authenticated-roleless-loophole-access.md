---
title: 'Базовый доступ к «Лазейкам» без прикладной роли'
type: 'bugfix'
created: '2026-08-31'
status: 'in-review'
baseline_commit: '495b540be62d81e282d1f08aa3f3e2107e1ce2dd'
review_loop_iteration: 0
context:
  - 'docs/loophole/bmad/implementation-artifacts/spec-1-1-авторизованный-выбор-рабочего-контекста.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Trusted SSO-пользователь без роли и строки `loophole_workspace_membership` получает 403 на `GET /api/loophole/contexts`. Клиент скрывает весь модуль, включая «Общую базу» и «Новое AI-исследование».

**Approach:** Отсутствие истории membership считать базовым доступом. Явно неактивная membership остаётся запретом. Queue/admin требуют active membership вместе с нужной ролью.

## Boundaries & Constraints

**Always:** Identity брать только из `CurrentUser`; без неё — 401. Если membership-строк нет, отдавать `catalog`, `sources`, `ai_research`. Active membership сохраняет прежний базовый доступ. Если строки есть, но active среди них нет, возвращать 403. `queue`/`admin` доступны только при active membership и роли `ccks_expert`/`module_admin`.

**Ask First:** Удалять «Добавить источник», вводить роли, создавать membership либо менять Authentik/OIDC.

**Never:** Не доверять данным клиента, не добавлять JSX-fallback после 401/403, не ослаблять protected endpoints или revoke, не менять migration 042 и не делать коммит.

## I/O & Edge-Case Matrix

| State | `/contexts` | Protected endpoints |
|---|---|---|
| Trusted principal, membership/roles отсутствуют | 200: `catalog`, `sources`, `ai_research` | 403 |
| Active membership без роли | Те же базовые контексты | 403 |
| Только revoked/non-active membership | 403 | 403 |
| Роль есть, membership отсутствует | Только базовые контексты | 403 |
| Trusted identity отсутствует | 401 | 401 |

</frozen-after-approval>

## Code Map

- `src/bank_audit/loophole/authorization.py` — различение default access и explicit revoke; privileged membership+role gate.
- `src/bank_audit/loophole/web.py` — router guard.
- `src/bank_audit/loophole/static/loophole.jsx` — уже рендерит server-driven contexts; production-правка не ожидается.
- `tests/loophole/test_authorization.py` — основная API/RBAC-матрица.
- `tests/loophole/test_admin_roles_audit.py` — admin/role граница.
- `tests/loophole/test_final_layout_runtime.py` — клики по разрешённым вкладкам.

## Tasks & Acceptance

**Execution:**
- [x] Сначала изменить тест отсутствующей membership на ожидание 200 с базовыми контекстами и получить RED.
- [x] В authorization различить отсутствие строк и explicit revoke; privileged contexts/actions оставить за active membership+role.
- [x] Синхронизировать router guard; JSX менять только при необходимости.
- [x] Закрепить runtime-проверку вкладок и отсутствие queue/admin.

**Acceptance Criteria:**
- Given trusted SSO-пользователь без membership/roles, when он открывает модуль, then доступны и переключаются «Общая база» и «Новое AI-исследование», а privileged surfaces отсутствуют.
- Given explicit revoke либо роль без active membership, when запрошен queue/admin, then сервер возвращает 403 без защищённых данных.

## Spec Change Log

## Design Notes

Missing membership означает default base access, но не active membership. Клиент остаётся server-driven; исправление сосредоточено на backend-границе.

## Verification

- RED→GREEN: `.venv\Scripts\python.exe -m pytest tests\loophole\test_authorization.py::test_contexts_authenticated_without_membership_gets_base_access -q -p no:cacheprovider`.
- RBAC: `.venv\Scripts\python.exe -m pytest tests\loophole\test_authorization.py tests\loophole\test_admin_roles_audit.py -q -p no:cacheprovider`.
- Runtime: `.venv\Scripts\python.exe -m pytest tests\loophole\test_final_layout_runtime.py -q -p no:cacheprovider`.
- Полная проверка: `.venv\Scripts\python.exe -m pytest tests\loophole -q -p no:cacheprovider`, scoped Ruff и `git diff --check`.
- Во встроенном Browser: обе темы, открытие двух вкладок, отсутствие queue/admin, ошибок console/network.
