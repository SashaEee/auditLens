"""Миграция статусов на изолированной временной таблице PostgreSQL."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest


def test_record_status_migration_preserves_data_and_rejects_obsolete_statuses():
    """Реальное DDL проверяет перенос, повторный запуск, DEFAULT и ограничения."""
    staging_url = os.getenv("AUDITLENS_POSTGRES_STAGING_URL")
    if not staging_url:
        pytest.skip("Для проверки миграции нужен явный PostgreSQL staging")
    migration = (
        Path(__file__).resolve().parents[2] / "migrations" / "062_loophole_record_status.sql"
    ).read_text(encoding="utf-8")

    connection = psycopg.connect(
        staging_url.replace("postgresql+psycopg://", "postgresql://", 1), connect_timeout=5,
    )
    try:
        # Временная таблица перекрывает рабочую только внутри этой сессии.
        connection.execute("SET LOCAL search_path TO pg_temp")
        connection.execute("""
            CREATE TEMP TABLE loophole_record (
                record_id INTEGER,
                status TEXT DEFAULT 'new',
                evidence TEXT
            )
        """)
        statuses = ["new", "classified", "published", "preliminary", None, "unknown"]
        for record_id, status in enumerate(statuses):
            connection.execute(
                "INSERT INTO loophole_record VALUES (%s, %s, %s)",
                (record_id, status, f"evidence-{record_id}"),
            )

        for _ in range(2):
            connection.execute(migration)
            assert connection.execute(
                "SELECT status FROM loophole_record ORDER BY record_id"
            ).fetchall() == [
                ("preliminary",), ("preliminary",), ("published",),
                ("preliminary",), ("preliminary",), ("preliminary",),
            ]

        assert connection.execute(
            "SELECT evidence FROM loophole_record ORDER BY record_id"
        ).fetchall() == [(f"evidence-{record_id}",) for record_id in range(6)]
        assert connection.execute(
            "INSERT INTO loophole_record (record_id) VALUES (6) RETURNING status"
        ).fetchone() == ("preliminary",)
        for invalid in ["new", "classified", "unknown", None]:
            with pytest.raises(psycopg.IntegrityError), connection.transaction():
                connection.execute(
                    "INSERT INTO loophole_record (status) VALUES (%s)", (invalid,)
                )
    finally:
        # Ни миграция, ни тестовые данные не фиксируются в базе стенда.
        connection.rollback()
        connection.close()
