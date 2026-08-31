"""Контракт защищённого perimeter Telegram worker-а (Story 6.5)."""
from __future__ import annotations

import json
from pathlib import Path

from bank_audit.config import ROOT
from bank_audit.loophole.telegram_perimeter import (
    REQUIRED_STAGING_EVIDENCE,
    verify_telegram_worker_perimeter,
)


def test_migration_057_encodes_least_privilege_and_controlled_functions():
    sql = (ROOT / "migrations" / "057_loophole_telegram_perimeter.sql").read_text(encoding="utf-8")
    normalized = sql.upper()

    for principal in (
        "AUDITLENS_APP",
        "LOOPHOLE_READONLY",
        "TELEGRAM_WORKER",
        "INGESTION_REAPER",
        "AUDIT_RETENTION",
    ):
        assert principal in normalized
    assert "SECURITY DEFINER" in normalized
    assert "LOOPHOLE_TELEGRAM_ACTIVE_TARGET_V1" in normalized
    assert "LOOPHOLE_WORKER_WRITE_SANITIZED_INGRESS" in normalized
    assert "LOOPHOLE_TERMINALIZE_EXPIRED_ATTEMPT" in normalized
    assert "LOOPHOLE_PURGE_AGENT_AUDIT_BEFORE" in normalized
    assert "REVOKE ALL ON TABLE AGENT_AUDIT_LOG FROM TELEGRAM_WORKER" in normalized
    assert "REVOKE ALL ON TABLE LOOPHOLE_RECORD FROM TELEGRAM_WORKER" in normalized
    assert "REVOKE ALL ON ALL TABLES IN SCHEMA PUBLIC FROM TELEGRAM_WORKER" in normalized
    assert "REVOKE ALL ON FUNCTION LOOPHOLE_WORKER_WRITE_SANITIZED_INGRESS" in normalized
    assert "FROM PUBLIC" in normalized


def test_migration_057_bootstraps_all_runtime_principals_before_dcl():
    sql = (ROOT / "migrations" / "057_loophole_telegram_perimeter.sql").read_text(
        encoding="utf-8"
    )

    for principal in (
        "auditlens_app",
        "loophole_readonly",
        "telegram_worker",
        "ingestion_reaper",
        "audit_retention",
    ):
        assert f"CREATE ROLE {principal} NOLOGIN" in sql


def test_deployment_contract_has_no_listener_and_restricts_egress():
    manifest = (ROOT / "deploy" / "telegram-worker" / "perimeter.yaml").read_text(encoding="utf-8")

    assert "kind: Deployment" in manifest
    assert "serviceAccountName: auditlens-telegram-worker" in manifest
    assert "containerPort" not in manifest
    assert "kind: Service\n" not in manifest
    assert "runAsNonRoot: true" in manifest
    assert "readOnlyRootFilesystem: true" in manifest
    assert "allowPrivilegeEscalation: false" in manifest
    assert "api.telegram.org" in manifest
    assert "managed-postgres" in manifest
    assert "secret-manager" in manifest
    assert "ca-bundle" in manifest
    assert "alert_owner" in manifest
    assert "rotation_owner" in manifest


def test_perimeter_verifier_is_honest_without_external_evidence(monkeypatch):
    monkeypatch.delenv("AUDITLENS_TELEGRAM_WORKER_STAGING_EVIDENCE", raising=False)

    result = verify_telegram_worker_perimeter()

    assert result["status"] == "UNVERIFIED"
    assert "evidence" in result["reason"].lower()
    assert set(result["required_checks"]) == set(REQUIRED_STAGING_EVIDENCE)


def test_perimeter_verifier_accepts_only_complete_external_evidence(tmp_path: Path):
    evidence_path = tmp_path / "staging-evidence.json"
    evidence_path.write_text(
        json.dumps({"checks": {name: "VERIFIED" for name in REQUIRED_STAGING_EVIDENCE}}),
        encoding="utf-8",
    )

    result = verify_telegram_worker_perimeter(str(evidence_path))

    assert result["status"] == "VERIFIED"


def test_perimeter_verifier_rejects_incomplete_external_evidence(tmp_path: Path):
    evidence_path = tmp_path / "staging-evidence.json"
    evidence_path.write_text(json.dumps({"checks": {"oidc_denials": "VERIFIED"}}), encoding="utf-8")

    result = verify_telegram_worker_perimeter(str(evidence_path))

    assert result["status"] == "FAILED"
    assert "missing" in result["reason"].lower()


def test_production_worker_uses_only_controlled_db_functions():
    worker = (ROOT / "src" / "bank_audit" / "loophole" / "telegram_worker.py").read_text(
        encoding="utf-8"
    ).upper()

    assert "LOOPHOLE_WORKER_INGEST_BATCH" in worker
    assert "LOOPHOLE_TERMINALIZE_EXPIRED_ATTEMPT" in worker
    assert "INSERT INTO LOOPHOLE_TELEGRAM_WORKER_" not in worker
    assert "UPDATE LOOPHOLE_TELEGRAM_WORKER_" not in worker
