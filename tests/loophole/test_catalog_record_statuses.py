"""Общая база не теряет результаты из-за статуса записи."""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from bank_audit.loophole import repository as repo
from bank_audit.loophole.chat.tools_nanobot import save_loophole
from bank_audit.loophole.models import LoopholeRecord
from bank_audit.loophole.web import list_catalog
from tests.loophole.test_preliminary_research_source_import import _create_import_schema


@pytest.mark.parametrize("status", ["new", "classified", "published", "preliminary"])
def test_catalog_includes_existing_findings_with_every_status(session, status):
    """Старые данные видны через endpoint до применения миграции статусов."""
    _create_import_schema(session)
    record_id = repo.insert_record(
        LoopholeRecord(sha256=status, title="Найденная лазейка", is_loophole=True),
        session=session,
    )
    session.execute(
        text("UPDATE loophole_record SET status = :status WHERE record_id = :id"),
        {"status": status, "id": record_id},
    )
    negative_id = repo.insert_record(
        LoopholeRecord(sha256="negative", is_loophole=False), session=session,
    )
    repo.insert_record(LoopholeRecord(sha256="unknown"), session=session)

    result = list_catalog(session=session)

    # 'all' теперь включает явные «не лазейки»; полностью неразмеченные
    # legacy-строки (is_loophole IS NULL) по-прежнему не попадают в каталог.
    assert result["count"] == 2
    assert [row["record_id"] for row in result["records"]] == [negative_id, record_id]


def test_analyst_saved_finding_is_preliminary_and_visible_in_catalog(session):
    """Реальный путь инструмента сохранения не скрывает найденный результат."""
    _create_import_schema(session)
    saved = save_loophole(
        title="Скрытая комиссия",
        url="https://bank.example/status-regression",
        snippet="Комиссия в примечании",
        raw_text="Комиссия в примечании к условиям договора.",
        is_loophole=True,
        session=session,
    )

    assert saved["record_id"] is not None
    record = repo.get_record(saved["record_id"], session=session)
    assert record["status"] == "preliminary"
    assert [row["record_id"] for row in list_catalog(session=session)["records"]] == [
        saved["record_id"]
    ]


@pytest.mark.parametrize("status", ["published", "preliminary"])
@pytest.mark.parametrize("is_loophole", [True, False])
def test_classification_preserves_publication_status(session, status, is_loophole):
    """Повторный вердикт не переводит запись в устаревший статус."""
    record_id = repo.insert_record(
        LoopholeRecord(sha256="reclassified", status=status), session=session,
    )

    repo.update_verdict(
        record_id, is_loophole=is_loophole, confidence=0.9,
        reason="Повторная оценка", model="test-model", session=session,
    )

    record = repo.get_record(record_id, session=session)
    assert record["status"] == status
    assert record["is_loophole"] is is_loophole
    assert record["classified_at"] is not None


@pytest.mark.parametrize("status", ["new", "classified", "verified", "pending", None])
def test_record_rejects_statuses_outside_publication_lifecycle(status):
    """Новые записи не могут вернуть удалённые статусы в хранилище."""
    with pytest.raises(ValidationError):
        LoopholeRecord(sha256="invalid-status", status=status)
