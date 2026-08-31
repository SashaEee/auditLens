# Восьмой requirements reviewer gate: финальная коррекция владельцев трассировки

**Дата:** 2026-08-27
**Область:** канонические `SPEC.md`, PRD/addendum и полный TO-BE пакет: `ARCHITECTURE-SPINE.md`, `SOLUTION-DESIGN.md`, `C4.md`, `SECURITY-AND-OPERATIONS.md`, `IMPLEMENTATION-MAP.md`, `TRACEABILITY-MATRIX.md`.
**Метод:** статическая проверка полноты owner/solution/acceptance и отсутствия новых противоречий. Реализация, migrations, PostgreSQL DCL/trigger, worker и staging evidence остаются **UNVERIFIED**.

## Проверенные области

| Область | Результат | Статическое доказательство |
| --- | --- | --- |
| Коррекция owner для selection/access | PASS | CAP-3 имеет владельца `Agent + Research service`, включает AD-37 и требует selection только после `TargetAccess` subscription и successful ingestion. FR-11 также включает AD-37, explicit evidence selection после grant, claim CAS и candidate isolation. |
| Capability и access boundary | PASS | CAP-1/FR-10/NFR-1 назначают `Identity/Authorization` и `Authorization` владельцами capability scope/RBAC. AD-37, Solution Design и Security/Operations ограничивают grant/revoke active `module_admin` с `target_subscription_manage` в своём workspace; redirect/cross-workspace/capability failure fail closed. |
| FR-4 lifecycle и registration | PASS | AD-35 теперь явно фиксирует, что `TargetRegistryService.register_target` изменяет только `TelegramMonitoringTarget v1`, alias index и собственный audit. `workspace_subscription`, membership и lifecycle не являются side effect skill; отдельный TargetAccess service покрыт AD-37 и acceptance инкремента 7. |
| Provisional/alias/revoke semantics | PASS | Grant на active provisional или resolved canonical root допустим и переносится при merge, но access к research требует existing ingested evidence. Revoke increment-ит grant version, блокирует новые selection/submission и сохраняет admitted snapshot path FR-12.4. |
| OQ-13 isolation | PASS | Matrix сохраняет цепочку `worker -> sanitized ingress -> revisioned evidence -> explicit analyst research`; worker не имеет path к candidate, очереди или каталогу. Access grant сам не создаёт evidence и не ослабляет PII/worker boundary. |
| Полнота matrix | PASS | Все CAP-1--6, FR-1--12, NFR-1--5, SM-1--6 и закрытые OQ содержат owner, AD/solution и acceptance/evidence. Новая AD-37 добавлена в CAP-1/CAP-3, FR-4/FR-10/FR-11, NFR-1 и SM-6 без непокрытого требования. |

## Findings

### CRITICAL

Нет.

### HIGH

Нет.

### MEDIUM

Нет.

## Вывод

**PASS -- финальный requirements/traceability gate.** Коррекция владельцев закрыла owner gap для TargetAccess: authorization отвечает за capability и scope, а research -- за selection только существующего ingested evidence. FR-4.2 сохраняет skill-only регистрацию target, а OQ-13 сохраняет изолированный путь без автоматического создания candidate, очереди или каталога.

Этот PASS относится только к согласованности TO-BE документации. Runtime DCL, migrations, worker, transactional behaviour и staging/production evidence остаются **UNVERIFIED**.
