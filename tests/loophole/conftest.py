"""Общие фикстуры тестов модуля loophole.

Без сети и реальной БД: используем in-memory SQLite для SQL-тестов, где это
безопасно (таблицы без Greenplum-специфики), и моки для LLM/web_search/fetch.
Для тестов, требующих Postgres-специфики (BIGSERIAL/JSONB/TEXT[]), проверяем
структуру миграции без выполнения.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Гарантируем, что src/ в sys.path даже без установленного пакета.
_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Дефолты env, чтобы импорт config не падал в тестах.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("LLM_BASE_URL", "http://localhost:9999/v1")
os.environ.setdefault("LLM_MODEL_NAME", "test-model")


@pytest.fixture
def fake_user_id() -> str:
    return "test-user"
