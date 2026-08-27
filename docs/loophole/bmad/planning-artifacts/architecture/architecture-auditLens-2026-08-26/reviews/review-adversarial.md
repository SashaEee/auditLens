# Adversarial reviewer gate

**Область:** восьмой повторный статический gate: устранение ownership-конфликта
`workspace_subscription` между TargetAccess и Research после обновления матрицы
трассировки.

**Вердикт: PASS.** Не найдено CRITICAL, HIGH или MEDIUM расхождений, которые
позволяют независимым командам формально соблюсти архитектуру и создать разные
shared state или mutation path для workspace subscription.

**Статус runtime:** **UNVERIFIED**. Проверены только TO-BE документы; DDL/DCL,
OIDC/RBAC, worker, миграции, PostgreSQL privilege-deny и staging evidence не
реализованы и не запускались.

## CRITICAL

Нет.

## HIGH

Нет.

## MEDIUM

Нет.

## Подтверждённое устранение H-01

- CAP-3 в `TRACEABILITY-MATRIX.md:11` теперь ограничен evidence selection после
  `TargetAccess` subscription и successful ingestion; он больше не назначен
  владельцем grant/revoke.
- FR-11 (`TRACEABILITY-MATRIX.md:30`) фиксирует только explicit evidence selection
  после TargetAccess grant. Тем самым Research/SourceEvidence потребляет active
  subscription и pinned `evidence_access_grant`, но не создаёт и не отзывает её.
- Единственный mutation contract определён AD-37
  (`ARCHITECTURE-SPINE.md:272`) и `SOLUTION-DESIGN.md:104`:
  `TargetAccessService` с namespace `target_subscription`, canonical-root
  dereference, `grant_version`/`intent_sequence`, active `module_admin` и
  workspace capability `target_subscription_manage`; matching revoke использует
  тот же RBAC/lock path.
- `SECURITY-AND-OPERATIONS.md:66` задаёт тот же grant/revoke и selection contract,
  C4 отделяет target registration/access services от worker direct writes, а
  `IMPLEMENTATION-MAP.md:14,50` закрепляет реализацию за одним инкрементом.

## Сквозная проверка shared state

- Provisional grant допустим только для active non-redirect root и до successful
  ingestion не открывает evidence path. Resolver атомарно merge-ит subscriptions
  по latest explicit intent к canonical root; redirect subscription запрещена.
- Selection и submit serializably фиксируют immutable access grant; revoke
  блокирует только новые selection/submission, сохраняя admitted snapshot и
  обязательную FR-12.4 publication path.
- Telegram skill не меняет subscription, worker имеет только fenced resolver path,
  а Research не имеет самостоятельной grant/revoke операции. Следовательно второго
  writer-а одной `workspace_subscription` state machine в пакете не осталось.

## Выполненные статические проверки

- Сверены Traceability Matrix, AD-31/AD-36/AD-37 в `ARCHITECTURE-SPINE.md`,
  `SOLUTION-DESIGN.md`, `SECURITY-AND-OPERATIONS.md`, `C4.md` и
  `IMPLEMENTATION-MAP.md`.
- Runtime-реализация и acceptance tests не выполнялись; их статус остаётся
  **UNVERIFIED**.
