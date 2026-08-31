# Карта реализации и ответственность

**Статус:** TO-BE. Ни один инкремент ниже не выполнен этим архитектурным пакетом. В частности, DDL/DCL, OIDC, worker, migrations, staging и production evidence имеют статус **UNVERIFIED**.

| Порядок | Инкремент | Владелец | Результат | Приёмка |
| --- | --- | --- | --- | --- |
| 0 | AS-IS baseline и cutover guard | tech lead + QA | inventory direct `save_loophole -> loophole_record`, legacy transcripts, API/SSE contracts, shim imports | golden SM-2/SM-4.1; test доказывает, что target route не имеет reachable catalog write вне PublicationService |
| 1 | Lifecycle, ingestion и DCL schema | backend + DBA + security | 042/043/044 migrations: evidence revisions, snapshots, payload-bound ledger, decisions, mappings, subscriptions, leases, immutable worker events, inbox/outbox/dead-letter, audit/retention and grants | idempotent migration; PostgreSQL principal allow/deny; SQLite structural contract tests |
| 2 | Identity и RBAC | backend + security | OIDC IdentityAdapter, authoritative membership/role store, role assignment <=5 CCKS experts, admin audit reader | invalid/missing JWT deny; revoke on next request/run; concurrent role limit; server-side endpoint/tool denial |
| 3 | Research and evidence | backend | explicit research, workspace subscription, selected evidence revision, candidate revision, `ReportFilter(scope)` | worker/projector creates no research/case; two projectors cannot duplicate evidence revision |
| 4 | Verification and publication | backend + QA | immutable evidence snapshot with evidence-access grant, conditional final decision, payload-bound command ledger, canonical versioned `CaseFingerprint v1` | concurrent opposite decision, repeat submit/publish, HMAC rotation, revoke versus submit serial order, automatic publication of admitted snapshot and no direct catalog write |
| 5 | Agent core and six skills | agent team | AgentFactory, registry, audit writer, SSE versioning and six Agent Skills packages | six packages each have `SKILL.md`, `scripts/`, `references/`, valid frontmatter and allowlisted factory; 20-iteration/clarification contracts |
| 6 | Scheduled DB task and reports | backend | `ScheduledQueryContract v1`: named query ID/version, workspace, owner/result owner capabilities/expiry, `ScheduledResult v1`, read-only path, shared ReportFilter | raw SQL/foreign destination denied; revoke each subject skip; enable/disable/expiry/audit; XLSX 10k, CSV snapshot, PDF fallback |
| 7 | Telegram target and worker | ingestion owner + backend + security | skill-only `TargetRegistryService.register_target`, separate `TargetAccessService.grant_workspace_subscription`, worker-only resolver-terminal function, separate deployment, active provisional registration and atomic resolver promotion, fail-closed sanitizer result, session/target leases, fences, registry/reaper terminalization, immutable attempt/batch/terminal journal, projector and inbox/outbox/dead-letter relay | alias/rename/migration, repeat/inactive registration without subscription write, old-alias dereference, explicit subscription dedupe/merge, resolver attempt one terminal/outbox, deferred access resolver attempt, initial history, incremental revision/dedupe, poison replay, stale fence, crash after header/before first batch and checkpoint, deactivate/reactivate, stale claim, <=24h SLO |
| 8 | UI and UX | frontend + UX | three routes: catalog, research, CCKS queue; admin surfaces and Russian accessible states | role visibility, keyboard, both themes, iframe breakpoints; UX artefacts updated first |
| 9 | Cutover | tech lead + QA + operations | graph through Agent, target behaviour enabled, legacy shim removal and rollback evidence | full regression, changed-file ruff, no direct shim imports, no dual-write, deployment proof |
| 10 | Production gate | product owner + security + operations | dashboard, retention job, access review, worker/runbook, sign-off | staging evidence in Security and Operations Contract, named owner for every alert |

## Границы изменения файлов

| Область | Целевые модули | Не изменяет |
| --- | --- | --- |
| Agent | `loophole/agent/`, `chat/graph.py`, temporary `tools_nanobot.py` | repository schema, UI state |
| Research/verification | application services, repository, migrations, `web.py` routes | Telegram session and worker secrets |
| Evidence/Telegram | worker deployment, ingestion repository, target registry, projectors | `loophole_record` direct write, decision and publication ownership |
| UI | `loophole/static/` and approved DTO | skill factories and database SQL |
| Security | identity adapter, DCL migration, tests and runbook | LLM prompt behaviour |
| Operations | deployment manifests, retention job, dashboards and alerts | business state transitions |

## Mandatory acceptance matrix

| Risk | Проверка |
| --- | --- |
| Unverified identity | valid token allowed; absent/bad issuer/audience/signature denied before router/tool |
| DB privilege escalation | PostgreSQL allow/deny matrix for app, readonly, worker, ingestion reaper and retention principals |
| Публикация без решения | service/endpoint deny; catalog query excludes candidate |
| Конкурентные решения ЦК КС | two experts race; exactly one decision and winner result is returned to loser |
| Повтор команды | idempotency test для target registration, submit, decision и publication |
| Повтор с другим payload | тот же namespace/key с иным canonical SHA-256 возвращает `idempotency_payload_mismatch` без side effect |
| Case snapshot drift | change draft after submit; decision and publication use immutable submitted evidence ids |
| Cross-case duplicate | exact fingerprint with canonical IDs/taxonomy/HMAC key-id links provenance once; fuzzy match and cross-key comparison create no automatic merge |
| Ручной verdict | concurrent classifier/manual-update returns `manual_verdict_preserved` |
| Subscription revoke | revoke versus submit has one lock order; revoke blocks only new selection/draft submission, while admitted snapshot still decides and automatically publishes once |
| Scheduled DB escape | raw SQL, foreign workspace/destination and automatic export denied; revoke owner or result owner skips audited run |
| Утечка Telegram secrets/PII | PII mask before persistence/LLM; `QuarantineMetadata v1` allowlist and sanitizer config/replay tests; config scan and worker DB grant review |
| Двойной Telegram sync | session and target lease contention; stale fencing token cannot write |
| Потеря инкремента | crash after batch before terminal is closed once by reaper; retry dedupes versioned object identity |
| Target lifecycle | deactivate races batch: registry terminalizes old generation/outbox, stale worker cannot write; reactivate continues checkpoint |
| Target reconciliation | `t.me == @alias`, active provisional target before membership/access with resolver-only SLO attempt, rename, peer migration and concurrent registration atomically promote/attach one canonical target/checkpoint |
| Target registration grant | `target_registration` creates/returns only canonical target; active `module_admin` with `target_subscription_manage` uses separate `target_subscription` to grant/revoke one workspace subscription even before resolution; selection still requires ingested evidence; differing payload/cross-workspace/redirect/RBAC failure is denied; old alias dereferences before each operation; merge preserves one canonical workspace grant by latest explicit intent; lock-order deadlock retry keeps key/fence/intent |
| Resolver audit origin | `resolver_only` promotion/merge terminalizes one normal attempt and one `ingestion_audit_outbox(attempt_id)`; no competing registry outbox |
| Claim race | stale completer versus reclaimer returns `claim_lost` and creates one evidence revision |
| Poison object | single quarantine, controlled replay and no infinite processing |
| Нарушение 24-hour SLO | deterministic clock test and staging monitor evidence |
| Audit/retention | audit relay exactly once through 30-day delivery horizon; dead-letter/late-delivery behavior; admin-only read; bounded no-payload purge functions with 14-day audit-log and 31/90-day journal boundaries and failure alert |
| Регрессия чата | golden transcript and versioned SSE contract tests |

## Зависимости и блокеры

- UX для трёх отдельных контекстов ещё не описан. Инкремент 8 не начинается до UX update.
- Инкремент 7 требует отдельного deployment owner, service account, secret rotation owner и DCL. Это не задача FastAPI process.
- Cutover невозможен, пока не пройдены SM-2/SM-4.1 и автоматическая проверка не докажет отсутствие reachable direct catalog write.
- Production gate не может быть PASS без staging evidence из `SECURITY-AND-OPERATIONS.md`; пока эти проверки остаются UNVERIFIED.
