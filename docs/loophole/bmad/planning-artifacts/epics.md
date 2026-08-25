# BMAD Epics — auditLens

Generated from `docs/superpowers/plans` for sprint tracking.

---

## Epic 1: Loophole Nanobot Migration

**Goal:** Replace the hand-rolled ReAct agent in `src/bank_audit/loophole/chat/` with `nanobot` as the harness, preserving the SSE API, bank-specific trust scoring, PII masking, and adding READ-ONLY text-to-SQL analytics.

### Story 1.1: Add dependency and basic config

Add `nanobot-ai` dependency and `LoopholeSettings` fields for nanobot model and max iterations.

### Story 1.2: Discover nanobot custom tool registration API

Inspect `nanobot` module, inline config, and custom tool registration API.

### Story 1.3: Implement READ-ONLY `db_query` tool

Implement a READ-ONLY SQL guard tool for nanobot.

### Story 1.4: Implement remaining custom tools

Implement `web_search`, `web_fetch`, `extract_loopholes`, `table_load`, `refine_export` tools.

### Story 1.5: Create nanobot harness

Build `nanobot_agent.py` harness with inline config and tool registration.

### Story 1.6: Implement run_chat adapter

Delegate `run_chat` from `graph.py` to the nanobot harness.

### Story 1.7: Implement streaming adapter

Delegate `stream_chat` and emit SSE deltas from the nanobot harness.

### Story 1.8: Cleanup old ReAct files

Delete `phases.py`, `nodes.py`, `tools.py`, and legacy prompts.

### Story 1.9: Fix imports and run full test suite

Update imports and run `pytest tests/loophole` until green.

### Story 1.10: Update MAP.md

Update `.cursor/plan_research/MAP.md` with nanobot-based architecture.

### Story 1.11: Manual integration checks

Run manual chat integration checks for loophole module.

---

## Epic 2: Manual Verdict Marking

**Goal:** Allow manual marking of `loophole_record` entries as loophole / normal request, with KB synchronization.

### Story 2.1: Migration 014 + db_schema registration

Create migration `014_loophole_manual_mark.sql` and register it in `db_schema.py`.

### Story 2.2: Repository record_id helpers

Add `record_id` support to `save_kb_example`, `get_kb_example_by_record`, `delete_kb_example_by_record`.

### Story 2.3: KB repository record_id and graceful fallback

Update `kb/repository.py` `add_example` with `record_id` and embedding graceful fallback.

### Story 2.4: Verdict endpoint

Implement `POST /api/loophole/records/verdict` endpoint.

### Story 2.5: UI verdict marking

Add clickable verdict badge, modal, bulk panel, and toast in `loophole.jsx`.

### Story 2.6: Final verification

Run full verification and update project context.

---

## Epic 3: Parsers Shared Catalog

**Goal:** Build a shared catalog of parsers with scheduler, runner, healer, and UI.

### Story 3.1: Dependency and migration 015

Add `croniter` dependency and migration `015_loophole_parser_shared.sql`.

### Story 3.2: conftest schema and parser repository extension

Update SQLite schema and parser repository CRUD helpers.

### Story 3.3: parser_run CRUD and reaper

Implement `parser_run` CRUD and reaper logic.

### Story 3.4: Models and deduplication

Add dedup models and URL/full-text deduplication.

### Story 3.5: parsers/dedup.py key normalization

Create `parsers/dedup.py` with key normalization.

### Story 3.6: registry list_catalog and find_conflicts

Implement catalog listing and conflict detection in `registry.py`.

### Story 3.7: generator shared catalog code

Build generator that emits parsers into a shared catalog structure.

### Story 3.8: runner with records and dedup

Implement runner with three-key dedup, SSE log bus, and JSON output.

### Story 3.9: scheduler cron validation and ticker

Add cron validation and scheduler ticker.

### Story 3.10: web endpoints for catalog

Implement parser catalog endpoints in `web.py`.

### Story 3.11: app lifespan scheduler and reaper

Wire scheduler and reaper into FastAPI lifespan.

### Story 3.12: nanobot tools fetch_target and patch_parser

Add nanobot tools for fetching targets and patching parsers.

### Story 3.13: healer self-recovery

Implement healer self-recovery for parser runs.

### Story 3.14: UI catalog and editor

Build parser catalog UI with cards, editing form, and live log.

### Story 3.15: Final verification

Run final verification for parser catalog.

---

## Epic 4: Loophole Full Content

**Goal:** Ensure nanobot and collector always save full page content, and UI displays it with expand/collapse.

### Story 4.1: Migration 016 and db_schema

Create migration `016_loophole_content.sql` and register it in `db_schema.py`.

### Story 4.2: Model and repository updates

Update `LoopholeRecord` model, repository, and test schema.

### Story 4.3: Config and content_fetch.py

Add content fetch helper and settings.

### Story 4.4: Guarantee point in save_loophole

Ensure `save_loophole` downloads full content when agent does not provide it.

### Story 4.5: Auto-collector

Update `collector.py` to fetch and store full content.

### Story 4.6: API GET /records/{id}/content

Add endpoint to retrieve full content for a record.

### Story 4.7: API POST /records/backfill-content

Add backfill endpoint for missing content.

### Story 4.8: CSV export content columns

Add full content columns to CSV export.

### Story 4.9: UI expandable rows

Implement expandable rows in UI with full content.

### Story 4.10: Final verification and context update

Run full verification and update project context.

---

## Epic 5: Parser Isolated Venv

**Goal:** Move parser creation and execution to isolated directories with their own venv, JSON logs from stdout, and results read from `results.json`.

### Story 5.1: Helpers env.py and generator files

Create `env.py` and file helpers in `generator.py`.

### Story 5.2: Generator isolated directory and requirements

Generate `requirements.txt` and save parser to isolated directory.

### Story 5.3: Runner venv JSON logs and results.json

Run parser from its own venv, parse JSON logs, and read `results.json`.

### Story 5.4: Validation loop and web endpoint

Add validation loop on parser creation and web endpoint.

### Story 5.5: Healer requirements reinstall

Implement healer reinstall of requirements after parser patch.

### Story 5.6: Registry delete directory

Add parser directory deletion to registry.

### Story 5.7: Frontend validation_run_id

Use `validation_run_id` in frontend parser UI.

### Story 5.8: Integration and final checks

Run integration and final checks.

