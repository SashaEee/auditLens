"""Общая база тестов сборщика ВСП: SQLite по умолчанию; с VSP_TEST_PG_URL — PostgreSQL
(своя схема на каждый тест), чтобы те же тесты проверяли серверную СУБД."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def db_url(tmp_path):
    pg = os.getenv("VSP_TEST_PG_URL")
    if not pg:
        yield f"sqlite:///{tmp_path / 'v.sqlite'}"
        return
    schema = "t_" + uuid.uuid4().hex[:12]
    eng = create_engine(pg)
    with eng.begin() as c:
        c.execute(text(f"CREATE SCHEMA {schema}"))
    yield f"{pg}{'&' if '?' in pg else '?'}options=-csearch_path%3D{schema}"
    with eng.begin() as c:
        c.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    eng.dispose()
