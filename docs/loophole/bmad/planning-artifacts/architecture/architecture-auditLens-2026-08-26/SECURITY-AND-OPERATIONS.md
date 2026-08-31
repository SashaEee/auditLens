# Security and Operations Contract

## Статус доказательств

Это целевой контракт для реализации AD-21--AD-37. Ни DCL, ни OIDC validation, ни отдельный Telegram worker, ни retention job ещё не реализованы или не проверены на staging. Все runtime-пункты ниже имеют статус **UNVERIFIED** до появления миграций, deployment manifests и evidence.

## Identity и application RBAC

1. `IdentityAdapter` проверяет OIDC JWT по pinned issuer, audience и JWKS. Успешная проверка выдаёт только `Principal(subject_id)`.
2. Отсутствующий, просроченный, неверно подписанный или неверно адресованный JWT даёт deny до router, tool и domain service.
3. Authoritative `workspace_membership` и `role_assignment` хранятся в БД. Они читаются для каждого request, mutating tool и scheduled execution, поэтому revoke действует без ожидания token refresh. Роль `ccks_expert` содержит `subject_id`, actor администратора, время и версию. Активных назначений ровно не больше пяти; изменение требует транзакционной проверки лимита.
4. Роль `module_admin` назначает/отзывает `ccks_expert`, изменяет статус Telegram target, выдаёт/отзывает workspace subscription через `target_subscription_manage` только в workspace своей active membership и читает audit. Это не DB superuser роль.
5. Все authorisation failures, role changes и audit reads создают redacted application audit event.

## DB least-privilege matrix

| Principal | Разрешено | Явно запрещено |
| --- | --- | --- |
| `auditlens_app` | domain repositories, `TargetRegistryService.register_target`, separate `TargetAccessService.grant_workspace_subscription`, controlled mutation functions, audit insert/outbox | DCL, прямой retention delete, Telegram session data |
| `loophole_readonly` | allowlisted views и parsed single `SELECT` DB skill | `INSERT`, `UPDATE`, `DELETE`, DDL, произвольные tables/functions |
| `telegram_worker` | active-target view; fenced functions для attempt, redacted object/checkpoint и `resolve_provisional_target` | direct table writes, catalog, research, verification, publication, role assignments, `agent_audit_log` |
| `ingestion_reaper` | только `terminalize_expired_attempt` и read-only attempt-age view без payload | target registration, batch/checkpoint/object write, catalog, research, audit log, DDL |
| `audit_retention` | bounded `purge_agent_audit_before(cutoff)` и `purge_ingestion_journal_before(cutoff)` functions, только aggregate runs | select audit payload, business data mutation, DDL |
| schema owner | migrations and grants during controlled deployment | application runtime identity |

Миграция `044_loophole_audit_security.sql` обязана содержать idempotent DDL/DCL для этих grants, append-only audit policy, unique keys lifecycle и SECURITY DEFINER functions `resolve_provisional_target`, `terminalize_expired_attempt`, retention purge. `resolve_provisional_target(target_id,generation,resolver_fence_token,attempt_id,remote_peer_id)` проверяет caller/current fence и `resolver_only` attempt, serializably merge-ит alias/subscription, terminalize-ит этот same attempt и вставляет unique `ingestion_audit_outbox(attempt_id)` в одной transaction; отдельного registry outbox нет. Alias index сначала dereference-ится до canonical root, а trigger запрещает `workspace_subscription` на redirect ID. Target registry использует один lock order: `target_alias_index` by normalized address, canonical target rows by UUID, target attempt by UUID, workspace subscriptions by workspace UUID, outbox last; registration reservation в CommandLedger выполняется до этого общего suffix. Deadlock/serialization retry возвращает typed retryable result с тем же command key/fence и не меняет intent. Function не даёт worker-у direct table mutation. Acceptance на PostgreSQL проверяет allow и deny для каждого principal. SQLite unit tests проверяют структурный SQL и domain constraints; они не доказывают реальные grants.

## Worker perimeter

| Контроль | Требование |
| --- | --- |
| Процесс | отдельный deployment и service account, не дочерний процесс AuditLens, нет HTTP listener |
| Сеть | TLS-verified egress только к Telegram и managed PostgreSQL; firewall запрещает app/catalog/audit route и иной egress |
| Секреты | runtime injection из approved secret manager, rotation owner и отсутствуют в image, logs, DB и tool arguments |
| Образ | signed image/SBOM и ограниченный runtime user без shell/UI доступа к ingress payload |
| БД | отдельный TLS credential только роли `telegram_worker`, CA validation обязательна |
| Наблюдаемость | health, attempt age, lease conflicts, quarantine count, retry count и SLO alert имеют named operations owner |

## Ingestion и data minimization

`TelegramObjectIdentity v1` равен `(logical_account_id, canonical_peer_id, message_id, optional_comment_id, revision)`. Он формирует stable source key. Target registry normalizes `t.me`, `@alias` и invite input и idempotently создаёт active provisional target до membership/access; worker исполняет для него resolver-only attempt и availability audit в пределах 24-hour SLO. Resolver serializably получает remote peer ID и atomically promotes provisional record to canonical target или при unique conflict переносит subscriptions и alias history в existing canonical target, оставляя исходный ID redirect alias. Acquisition/checkpoint/evidence работают только у canonical target. Alias history имеет validity range; rename меняет history, peer migration добавляет immutable alias map к прежнему canonical ID. Недоступная активная цель остаётся зарегистрированной и пишет terminal availability error. Global target делится с workspace через subscription; evidence может выбрать только исследование workspace с active subscription. Worker держит исходный Telegram text только в памяти. Existing regex `pii_mask` является компонентом, но не достаточной security guarantee: versioned sanitizer должен успешно классифицировать text/attachments перед persistence. Он сохраняет только immutable redacted object, identity, redaction policy version/config digest, keyed HMAC и metadata attempt.

- Полный raw Telegram body, replacement map и вложения не сохраняются и не попадают в LLM или audit.
- Detector error, uncertain result или unsupported attachment создаёт только `QuarantineMetadata v1`: opaque IDs, keyed `{algorithm,hmac_key_id,digest}` source-identity fingerprint, UTC time, size, fixed media/reason codes и policy/config identifiers. Plain/hash-only digest, body, caption, filename, URL, peer title, preview, OCR/error text и arbitrary strings запрещены в schema, persistence и logs. До successful sanitizer policy нет evidence/LLM path.
- Immutable `SanitizationResult v1` фиксирует policy version/config digest, outcome, closed reason code, input/media type, parser version и approved-projection fingerprint. Любая смена parser/config создаёт новую policy version; replay создаёт новую object revision с audit, не переписывая прежнюю.
- `SourceEvidenceService` создаёт append-only `source_evidence_revision` на `origin_ingestion_object_id`; submitted snapshot использует revision ID и full `{algorithm, hmac_key_id, digest}`. Rotation не переписывает historical revision: legacy key verify-only, active key создаёт новую revision; cross-key exact match запрещён.
- Unlinked ingress object удаляется через 14 дней. Linked evidence хранится в research/case lifecycle только как redacted projection.
- Worker journal содержит только operational fields. `IngestionAuditProjector` доставляет exactly-one aggregate event в `agent_audit_log` по `(worker_run_id, attempt_id)` через transactional outbox.

## Lease, checkpoint и transitions

1. Worker получает global lease на deployment `telegram_session_id` и target lease с generation-bound fencing token. Logical account остаётся ключом target registry, а не session lease.
2. Каждый batch записи и checkpoint выполняются в транзакции при совпадении target generation и fence token. Порядок: object insert/dedupe -> append-only batch event -> checkpoint.
3. `active(g) -> inactive(g+1)` сериализуется с fenced writes. Registry transaction terminalize-ит все non-terminal attempts поколения `g` кодом `target_deactivated` и unique outbox; worker после fence loss только прекращает работу. Если deactivate коммитится первым, batch stale worker-а отвергается; если batch коммитится первым, deactivate закрывает attempt до следующего write.
4. `inactive -> active` не очищает checkpoint. Незавершённый initial sync продолжается, завершённый target идёт incremental path.
5. Attempt состоит из immutable header, append-only batch events и ровно одного terminal event. Header transaction всегда создаёт durable `initial_checkpoint(sequence=0,cursor=null)` до Telegram read. Normal terminal требует current fence; system actor `ingestion_reaper` conditional terminalize-ит любой non-terminal attempt с expired lease как `lease_expired|abandoned`, включая zero-batch, создавая тот же unique outbox. Новый worker начинает attempt только после этого. Projector создаёт unique inbox and audit summary. `SourceEvidenceService` claim-ит ingress object в состояниях `new|processing|linked|retryable_failed|quarantined` с token/expiry; completion/failure делает CAS `claim_token + unexpired claim_expires_at + processing`, а stale projector получает `claim_lost` без mutation. Reaper reclaim-ит только истёкший claim до retention cutoff. Два projector-а не могут создать два evidence revision благодаря unique `origin_ingestion_object_id`.

## Audit retention

`agent_audit_log` append-only для application roles. Daily job от `audit_retention` вызывает только bounded `purge_agent_audit_before(cutoff)` с cutoff 14 days и `purge_ingestion_journal_before(cutoff)` с проверкой 31/90-day условий, подтверждённого terminal и delivery horizon. Каждая function ограничена batch size, возвращает только aggregate count/status в `audit_retention_run|ingestion_retention_run` и поднимает alert при failure; job не читает и не логирует payload удалённых событий.

Attempt header/batch/terminal хранятся 90 дней. `ingestion_audit_outbox` и `audit_inbox` хранятся не менее 31 дня после terminal, delivery horizon -- 30 дней. Недоставленный outbox после 7 дней переходит в redacted dead-letter, вызывает alert и может replay-иться только по immutable `outbox_id` до конца horizon. После него late delivery получает `expired_outbox` без audit mutation; purge inbox/outbox разрешён только после terminal и horizon. Это удерживает exactly-once dedupe дольше 14-day retention audit log.

## Subscription, publication и scheduled results

`TargetAccessService` разрешает grant/revoke только active `module_admin` с `target_subscription_manage` в том же workspace. Grant допустим для non-redirect provisional либо resolved target и создаёт active `workspace_subscription`; он не создаёт evidence и не заменяет Telegram membership. Selection всё равно требует active subscription и successfully ingested evidence. Subscription row имеет monotonic `grant_version`. Selection и submit берут её `FOR UPDATE`, требуют active current version и атомарно фиксируют immutable `evidence_access_grant` в submitted snapshot. Revoke берёт тот же lock и increment version: после него нельзя выбрать новое evidence или submit draft; draft требует re-subscribe и new explicit selection. Уже admitted snapshot сохраняет свой grant, поэтому pending decision, mandatory automatic publication positive decision и idempotent retry продолжаются детерминированно и не зависят от будущего revoke. Каждый grant/revoke/submit/publish порядок аудируется без PII.

Scheduled run допускается только при active membership owner и result owner в одном contract workspace, capability `db_schedule_execute` owner-а для pinned query version, capability `scheduled_result_read` result owner-а и неистекшем expiry. Иначе создаётся `schedule_skipped_<reason>` без query. `ScheduledResult v1` остаётся в private internal destination этого workspace, имеет ACL только owner/result owner, run correlation и TTL не более 24 часов. Foreign workspace, external destination и automatic export запрещены.

## Required staging evidence

- OIDC: valid token allowed; bad issuer/audience/signature and absent claim denied.
- DB: matrix allow/deny для пяти runtime principals.
- Lifecycle: two concurrent experts, repeated publish, publication failure and retry.
- Worker: lease expiry, stale fencing token, crash before/after checkpoint, deactivate/re-activate, duplicate/revision source identity and poison object.
- Data: PII mask before persistence/LLM; 14-day cleanup of unlinked ingress and audit events.
- Deployment: image provenance, secret rotation, CA/TLS, egress firewall and owner alerts.
