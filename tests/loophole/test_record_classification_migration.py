"""Миграция типов на временных таблицах PostgreSQL без изменения рабочих данных."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest


def test_classification_migration_preserves_history_and_is_idempotent():
    staging_url = os.getenv("AUDITLENS_POSTGRES_STAGING_URL")
    if not staging_url:
        pytest.skip("Для проверки миграции нужен явный PostgreSQL staging")
    migration = (
        Path(__file__).resolve().parents[2] / "migrations" / "066_loophole_record_classification.sql"
    ).read_text(encoding="utf-8")
    connection = psycopg.connect(
        staging_url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=5,
    )
    try:
        connection.execute("SET LOCAL search_path TO pg_temp")
        connection.execute("""
            CREATE TEMP TABLE loophole_record (
                record_id INTEGER PRIMARY KEY, is_loophole BOOLEAN,
                verdict_model TEXT, status TEXT DEFAULT 'preliminary', title TEXT
            );
            CREATE TEMP TABLE loophole_publication_mapping (record_id INTEGER, decision_id INTEGER);
            CREATE TEMP TABLE loophole_verification_decision (decision_id INTEGER, decision TEXT);
            INSERT INTO loophole_record (record_id, is_loophole, verdict_model, title) VALUES
                (1, TRUE, NULL, 'Уязвимость'), (2, FALSE, NULL, 'Не подтверждено'),
                (3, NULL, NULL, 'Не классифицировано'), (4, TRUE, NULL, 'Схема'),
                (5, TRUE, 'manual', 'Ручное решение');
            INSERT INTO loophole_publication_mapping VALUES (4, 1), (5, 1);
            INSERT INTO loophole_verification_decision VALUES (1, 'fraud_scheme');
        """)
        before = connection.execute(
            "SELECT record_id, is_loophole, verdict_model, status, title "
            "FROM loophole_record ORDER BY record_id"
        ).fetchall()
        for _ in range(2):
            connection.execute(migration)
            assert connection.execute(
                "SELECT classification FROM loophole_record ORDER BY record_id"
            ).fetchall() == [
                ("vulnerability",), ("not_confirmed",), (None,),
                ("fraud_scheme",), ("vulnerability",),
            ]
        assert connection.execute(
            "SELECT record_id, is_loophole, verdict_model, status, title "
            "FROM loophole_record ORDER BY record_id"
        ).fetchall() == before
        connection.execute(
            "UPDATE loophole_record SET classification = 'not_confirmed' WHERE record_id = 4"
        )
        connection.execute(migration)
        assert connection.execute(
            "SELECT classification FROM loophole_record WHERE record_id = 4"
        ).fetchone() == ("not_confirmed",)
        with pytest.raises(psycopg.IntegrityError), connection.transaction():
            connection.execute(
                "UPDATE loophole_record SET classification = 'unknown' WHERE record_id = 1"
            )
    finally:
        connection.rollback()
        connection.close()
