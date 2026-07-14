"""Тест generator: мок LLM возвращает код Scrapy-паука, проверяем сохранение файла и БД."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import text

from bank_audit.loophole.parsers import generator
from bank_audit.loophole import repository as repo

from tests.loophole.test_repository import session as sqlite_session  # noqa: E402


VALID_SPIDER_CODE = '''import scrapy, json

class LoopholeSpider(scrapy.Spider):
    name = "loophole"
    def parse(self, response):
        yield {"title": "test", "url": response.url}
'''


@pytest.fixture
def session(sqlite_session):
    # Добавляем таблицу loophole_parser (миграция 011) в SQLite-схему.
    sqlite_session.execute(text(
        "CREATE TABLE IF NOT EXISTS loophole_parser ("
        "parser_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "workspace_id INTEGER, name TEXT, code_path TEXT, "
        "status TEXT DEFAULT 'created', config TEXT, "
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP, last_run_at TEXT)"
    ))
    sqlite_session.execute(text(
        "CREATE TABLE IF NOT EXISTS loophole_record ("
        "record_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "sha256 TEXT NOT NULL, title TEXT, url TEXT, snippet TEXT, "
        "domain TEXT, trust_score REAL, fetched_at TEXT, "
        "collected_at TEXT DEFAULT CURRENT_TIMESTAMP, bank_slug TEXT, "
        "keyword TEXT, raw_text TEXT, is_loophole INTEGER, "
        "verdict_confidence REAL, verdict_reason TEXT, verdict_model TEXT, "
        "classified_at TEXT, status TEXT DEFAULT 'new')"
    ))
    sqlite_session.commit()
    return sqlite_session


@pytest.fixture
def workspace_id(session) -> int:
    wid = repo.create_workspace("test-user", "ws-test", session=session)
    return wid


def _llm_mock(code: str = VALID_SPIDER_CODE):
    msg = MagicMock()
    msg.content = code
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


@pytest.mark.asyncio
async def test_generate_parser_saves_file_and_registers(
    tmp_path, monkeypatch, session, workspace_id,
):
    # Перенаправим workspace-директорию во tmp_path.
    from bank_audit.loophole.config import LoopholeSettings
    settings = LoopholeSettings(workspace_dir=tmp_path)
    monkeypatch.setattr(LoopholeSettings, "load", classmethod(lambda cls: settings))

    llm = _llm_mock()
    result = await generator.generate_parser(
        "test-user", workspace_id, "скрытые комиссии по вкладам",
        llm=llm, session=session,
    )

    assert "parser_id" in result and result["parser_id"] > 0
    assert result["code_path"].endswith(".py")
    from pathlib import Path
    p = Path(result["code_path"])
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "class" in content and "scrapy" in content.lower()

    # Запись в БД.
    row = repo.get_parser(result["parser_id"], session=session)
    assert row is not None
    assert row["name"] == result["name"]
    assert row["code_path"] == result["code_path"]
    assert row["status"] == "created"


@pytest.mark.asyncio
async def test_generate_parser_strips_code_fences(
    tmp_path, monkeypatch, session, workspace_id,
):
    from bank_audit.loophole.config import LoopholeSettings
    settings = LoopholeSettings(workspace_dir=tmp_path)
    monkeypatch.setattr(LoopholeSettings, "load", classmethod(lambda cls: settings))

    fenced = "```python\n" + VALID_SPIDER_CODE + "\n```"
    llm = _llm_mock(fenced)
    result = await generator.generate_parser(
        "test-user", workspace_id, "тест", llm=llm, session=session,
    )
    from pathlib import Path
    content = Path(result["code_path"]).read_text(encoding="utf-8")
    assert not content.startswith("```")
    assert "class" in content


def test_sanitize_filename_basic():
    assert generator.sanitize_filename("bank-loophole") == "bank-loophole"
    assert generator.sanitize_filename("") == "parser"
    assert generator.sanitize_filename(".../etc/passwd") == "etc_passwd"
    # Кириллица заменяется на _, но не должна давать пустоту.
    out = generator.sanitize_filename("скрытые комиссии")
    assert out and all(c.isalnum() or c in "-_" for c in out)


def test_sanitize_filename_alnum_only():
    out = generator.sanitize_filename("query 123!")
    assert all(c.isalnum() or c in "-_" for c in out)
    assert out
