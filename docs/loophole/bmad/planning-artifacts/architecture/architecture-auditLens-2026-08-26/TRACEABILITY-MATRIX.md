# Матрица трассировки требований

**Статус:** архитектурная трассировка. Она доказывает наличие владельца, решения и приёмки в TO-BE дизайне; выполнение кода, DCL, миграций, worker-а и staging evidence остаётся **UNVERIFIED**.

## Capabilities

| ID | Архитектурный владелец | Решение | Проверка реализации |
| --- | --- | --- | --- |
| CAP-1 | Identity/Authorization + UI routes | AD-7, AD-8, AD-12, AD-19, AD-21, AD-27, AD-37 | OIDC deny, role visibility, target-subscription capability scope and separate routes |
| CAP-2 | Catalog/Reports service | AD-5, AD-6, AD-15 | ReportFilter snapshot, XLSX/CSV/PDF tests |
| CAP-3 | Agent + Research service | AD-1, AD-5, AD-18, AD-24, AD-25, AD-27, AD-30, AD-31, AD-37 | evidence selection only after TargetAccess subscription and successful ingestion, isolated research, no catalog write |
| CAP-4 | Verification + Publication service | AD-12, AD-16, AD-18, AD-19, AD-22, AD-25, AD-30, AD-31 | concurrent decision, immutable admitted snapshot, payload mismatch, automatic retry publication |
| CAP-5 | Agent core, Registry, skills | AD-1--AD-3, AD-9--AD-15 | six skill contracts, SSE, parity and audit tests |
| CAP-6 | Classifier service | AD-4, AD-6, AD-16 | batch 50, manual verdict concurrency |

## Functional Requirements

| ID | Архитектурный владелец | Решение | Приёмка |
| --- | --- | --- | --- |
| FR-1 | Agent core | AD-1, AD-10, AD-11, AD-14 | 20-iteration, clarify and partial-result contracts |
| FR-2 | Web-search skill + Research service | AD-2, AD-5, AD-18, AD-25 | source/evidence provenance and isolated candidate tests |
| FR-3 | Parser service + Scheduler | AD-2, AD-9, AD-12 | create/run/status/enable-disable schedule contracts |
| FR-4 | Target registry + external worker | AD-7, AD-9, AD-17, AD-20, AD-23, AD-24, AD-26, AD-28, AD-29, AD-32--AD-37 | skill-only idempotent registration, separate canonical access grant, controlled resolver terminal/promotion, 24-hour SLO, lease/fence/reaper, dedupe, failover, retention and quarantine tests |
| FR-5 | DB skill + typed DB scheduler | AD-9, AD-15, AD-27, AD-34 | parsed SELECT, allow/deny grant, owner/result-owner reauthorization and private result tests |
| FR-6 | Reports service | AD-6 | shared scope/filter, XLSX 10k, CSV snapshot, PDF fallback |
| FR-7 | Classifier service | AD-4, AD-16 | one/batch, configured 50, manual verdict preserved |
| FR-8 | Chat adapter + AgentFactory | AD-1, AD-10, AD-11, AD-14 | HTTP/SSE parity and <=15 second first progress |
| FR-9 | iframe UI | AD-6, AD-8, AD-11 | Russian accessible states, theme and breakpoint tests |
| FR-10 | Authorization + UI routing | AD-7, AD-8, AD-12, AD-19, AD-27, AD-37 | role-hidden UI, target-subscription capability scope and server-side deny |
| FR-11 | Research/SourceEvidence service | AD-5, AD-18, AD-24, AD-25, AD-27, AD-31, AD-33, AD-37 | explicit evidence selection after TargetAccess grant, claim CAS, export, candidate isolation |
| FR-12 | Verification/Publication service | AD-3, AD-12, AD-18, AD-19, AD-22, AD-25, AD-30, AD-31 | one final decision, no negative publication, automatic/idempotent positive publication of admitted snapshot |

## Non-Functional Requirements

| ID | Архитектурный владелец | Решение | Приёмка |
| --- | --- | --- | --- |
| NFR-1.1--1.5 | Authorization, DB skill, Target registry | AD-7, AD-12, AD-15, AD-19, AD-21, AD-27, AD-37 | auth/SQL/grant deny, target lifecycle, target-access RBAC and no secret tests |
| NFR-2.1--2.8 | Agent, Publication, Worker | AD-1, AD-6, AD-18, AD-20, AD-22, AD-24--AD-26, AD-29--AD-33 | retry, checkpoint, terminal recovery, duplicate, crash, sanitizer replay and publication failure tests |
| NFR-3.1--3.3 | SSE adapter, Classifier, Worker | AD-4, AD-11, AD-20, AD-26 | first event <=15 seconds, batch contract, deterministic 24-hour clock |
| NFR-4.1--4.7 | Audit writer, Worker journal, Projector | AD-3, AD-10, AD-13, AD-23, AD-26, AD-29, AD-32, AD-33 | redaction/quarantine allowlist, outbox/inbox horizon/dead-letter exactly-once, target lifecycle audit |
| NFR-5.1--5.4 | Agent migration, SkillRegistry, Worker deployment | AD-2, AD-14, AD-20, AD-21 | six package structure, shim parity, independent worker/failover |

## Success Metrics and build gates

| ID | Владелец измерения | Архитектурная опора | Evidence |
| --- | --- | --- | --- |
| SM-1 | Agent team | AD-1, AD-4, AD-11 | evaluation dataset and run telemetry |
| SM-2 | QA + tech lead | AD-2, AD-14, Implementation Map | pytest, changed-file ruff, skill structure inventory |
| SM-3 | Audit owner | AD-3, AD-10, AD-13, AD-26 | audit/outbox/inbox coverage |
| SM-4 | QA + analysts | AD-1, AD-8, AD-14 | legacy transcript parity and UI acceptance |
| SM-5 | Backend + ЦК КС | AD-18, AD-22, AD-25, AD-30, AD-31 | end-to-end research/grant/decision/automatic-publication trace |
| SM-6 | Ingestion owner | AD-20, AD-23, AD-24, AD-26, AD-28, AD-29, AD-32--AD-37 | staging worker, SLO, failover/reaper, controlled resolver terminal/promotion, separate canonical access grant, retention and PII quarantine evidence |
| BB-1 | Architect + product owner | full package and this matrix | repeat reviewer gate PASS; migrations remain UNVERIFIED until implemented |
| BB-2 | Product manager + tech lead | Implementation Map increments 2--7 | stories add GAP-STORY-01--04 before sprint planning |

## Resolved questions

| ID | Решение | Architecture ADs |
| --- | --- | --- |
| OQ-2 | Parsed, single-statement, allowlisted `SELECT` with separate read-only principal; raw SQL schedule prohibited. | AD-9, AD-15, AD-21, AD-27 |
| OQ-6 | Module admin assigns up to five CCKS experts; no second step in v1. | AD-19, AD-21, AD-27 |
| OQ-7 | Application audit is redacted, admin-only and retained 14 days by bounded retention job. | AD-3, AD-13, AD-21, AD-23, AD-26 |
| OQ-13 | Worker -> sanitized ingress -> revisioned evidence -> explicit analyst research; no automatic candidate/queue/catalog path. | AD-17, AD-20, AD-23--AD-37 |
