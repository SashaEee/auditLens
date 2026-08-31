---
title: 'Защищённый perimeter и доказательства готовности worker'
type: 'feature'
created: '2026-08-29'
status: 'in-progress'
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

Как владелец платформы,
я хочу развернуть Telegram worker с отдельными правами и проверить его perimeter,
чтобы внешний ingestion не мог получить доступ к данным или действиям AuditLens вне своей роли.

**Критерии приёмки:**

**Дано** развёрнуты runtime principals,
**Когда** применяются DCL и controlled functions,
**Тогда** `auditlens_app`, `loophole_readonly`, `telegram_worker`, `ingestion_reaper` и `audit_retention` имеют только минимально необходимые права.
**И** worker не имеет прямой записи таблиц, доступа к каталогу/research/verification/`agent_audit_log` или DCL.

**Дано** worker запущен в production-like окружении,
**Когда** проверяется его perimeter,
**Тогда** он является отдельным deployment без inbound HTTP listener, использует отдельный service account, TLS/CA validation и secrets только из approved secret manager.
**И** его egress ограничен Telegram и managed PostgreSQL.

**Дано** готовится release candidate,
**Когда** выполняются staging-проверки,
**Тогда** есть доказательства allow/deny для principals, OIDC отказов, lease/fencing, PII sanitation, cleanup, secret rotation, firewall и alert ownership.
**И** отсутствие внешней проверки фиксируется как UNVERIFIED.

## Boundaries & Constraints

**Always:** Реализовывать только требования этой истории и сохранять архитектурные инварианты AuditLens: server-side fail-closed авторизацию, изоляцию рабочих контекстов, детерминированные расчёты без делегирования чисел LLM, русские подписи интерфейса и безопасную обработку данных.

**Ask First:** Расширить область на другую историю, изменить схему прав, межэпиковый контракт, внешнюю интеграцию, миграцию или deployment за пределами явно необходимого для этой истории.

**Never:** Не считать этот черновик разрешением реализовать соседние истории; не раскрывать защищённые данные, не обходить существующие domain services, не добавлять неоговорённые зависимости и не создавать git-коммит.

</frozen-after-approval>

## Code Map

Этот документ намеренно остаётся draft: перед переводом в eady-for-dev нужно исследовать текущие файлы, точки расширения, миграции и тестовые fixtures именно для этой истории. Нельзя подменять эту проверку предположениями из эпика.

## Tasks & Acceptance

**Execution:**
- [ ] Исследовать существующий код и обновить Code Map конкретными путями, символами и read-only ограничениями.
- [ ] Написать минимальный failing test на основной сценарий и каждый критичный отказ из критериев приёмки.
- [ ] Наблюдать ожидаемое RED-падение; только затем внести минимальную production-реализацию.
- [ ] Запустить целевые тесты, полный набор тестов модуля и линтер; при необходимости уточнить границы до начала реализации.

**Acceptance Criteria:**
- Критерии приёмки внутри frozen Intent являются обязательным контрактом этой истории и должны быть преобразованы в наблюдаемые тесты до реализации.

## Spec Change Log

## Verification

**Commands:**
- pytest <целевые-тесты> -q — ожидаемое RED до production-кода, затем PASS после минимальной реализации.
- pytest tests/loophole -q — отсутствие регрессий соответствующего модуля.
- .venv/Scripts/ruff.exe check <затронутые-файлы> — отсутствие новых lint-ошибок.
