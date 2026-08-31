# Telegram worker: защищённый deployment perimeter

Этот каталог описывает отдельный worker ingestion, а не дочерний процесс
AuditLens. В `perimeter.yaml` намеренно нет `Service`, `Ingress`, порта или
HTTP-listener: worker создаёт только исходящие TLS-соединения к `api.telegram.org`
и endpoint `managed-postgres`.

Сервисный аккаунт `auditlens-telegram-worker` не является приложенческой identity.
Секреты поставляются исключительно через `approved-secret-manager`; образ не
содержит session, пароля БД или CA. `DATABASE_SSLMODE=verify-full` и read-only
`auditlens-managed-postgres-ca-bundle` обязательны. Named owners: rotation —
`platform-security`, alerts — `platform-operations`.

## Матрица release evidence

Файл, указанный в `AUDITLENS_TELEGRAM_WORKER_STAGING_EVIDENCE`, формируется
внешним controlled staging-run и имеет формат:

```json
{"checks":{"principal_allow_deny":"VERIFIED","oidc_denials":"VERIFIED","lease_fencing":"VERIFIED","pii_sanitation":"VERIFIED","cleanup":"VERIFIED","secret_rotation":"VERIFIED","firewall":"VERIFIED","alert_ownership":"VERIFIED"}}
```

Пункты означают соответственно: DB allow/deny для всех runtime principals,
OIDC deny, stale lease/fencing, PII до persistence/LLM, cleanup, rotation,
firewall egress и назначенного владельца alert. Отсутствующий или недоступный
артефакт должен оставаться `UNVERIFIED`; локальные тесты не являются заменой
проверки реального principal, network policy или secret store.
