"""Тест generator: мок LLM, код сохраняется в общий каталог parsers/catalog/."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from bank_audit.loophole import repository as repo
from bank_audit.loophole.parsers import generator

VALID_SPIDER_CODE = '''import scrapy, json

class LoopholeSpider(scrapy.Spider):
    name = "loophole"
    def parse(self, response):
        yield {"title": "test", "url": response.url}
'''


@pytest.fixture
def workspace_id(session) -> int:
    return repo.create_workspace("test-user", "ws-test", session=session)


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch) -> Path:
    """Перенаправляет CATALOG_DIR во tmp_path."""
    d = tmp_path / "catalog"
    monkeypatch.setattr(generator, "CATALOG_DIR", d)
    return d


@pytest.fixture(autouse=True)
def _mock_venv_and_pip(monkeypatch):
    """venv и pip install в тестах не делаем — медленно."""
    async def fake_create_venv(dir_path: Path) -> Path:
        venv_path = dir_path / "venv"
        venv_path.mkdir(parents=True, exist_ok=True)
        py = venv_path / ("Scripts" if sys.platform == "win32" else "bin") / (
            "python.exe" if sys.platform == "win32" else "python"
        )
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("")
        return venv_path

    monkeypatch.setattr(generator, "create_venv", fake_create_venv)

    async def fake_install_requirements(*a, **kw):
        return None

    monkeypatch.setattr(generator, "install_requirements", fake_install_requirements)


def _llm_mock(code: str = VALID_SPIDER_CODE):
    msg = MagicMock()
    msg.content = code
    llm = MagicMock()
    llm.ainvoke = AsyncMock(return_value=msg)
    return llm


@pytest.mark.asyncio
async def test_generate_saves_to_catalog(session, workspace_id, catalog_dir):
    result = await generator.generate_parser(
        "test-user", workspace_id,
        "скрытые комиссии https://bank-example.ru/deposits",
        llm=_llm_mock(), session=session,
    )
    assert result["parser_id"] > 0
    code_path = Path(result["code_path"])
    parser_dir = code_path.parent
    assert parser_dir.parent == catalog_dir
    assert parser_dir.name.startswith(f"parser_{result['parser_id']}_")
    assert code_path.name == "parser.py"
    assert (parser_dir / "requirements.txt").exists()
    assert "scrapy" in code_path.read_text(encoding="utf-8").lower()

    row = repo.get_parser(result["parser_id"], session=session)
    assert row["code_path"] == str(code_path)
    assert row["created_by"] == "test-user"
    assert json.loads(row["source_keys"]) == ["bank-example.ru/deposits"]
    cfg = json.loads(row["config"])
    assert cfg["targets"] == ["https://bank-example.ru/deposits"]


@pytest.mark.asyncio
async def test_generate_strips_code_fences(session, workspace_id, catalog_dir):
    fenced = "```python\n" + VALID_SPIDER_CODE + "\n```"
    result = await generator.generate_parser(
        "test-user", workspace_id, "тест https://t.me/bank_group",
        llm=_llm_mock(fenced), session=session,
    )
    content = Path(result["code_path"]).read_text(encoding="utf-8")
    assert not content.startswith("```")
    assert "class" in content


@pytest.mark.asyncio
async def test_generate_rejects_query_without_target(session, workspace_id, catalog_dir):
    llm = _llm_mock()
    with pytest.raises(ValueError, match="URL"):
        await generator.generate_parser(
            "test-user", workspace_id, "скрытые комиссии по вкладам",
            llm=llm, session=session,
        )
    llm.ainvoke.assert_not_called()
    assert repo.list_all_parsers(session=session) == []


def test_extract_targets_urls_and_telegram():
    assert generator.extract_targets(
        "смотри https://bank.ru/promo и t.me/bank_loopholes"
    ) == ["t.me/bank_loopholes", "https://bank.ru/promo"]
    assert generator.extract_targets("группа @bank_secrets") == ["@bank_secrets"]
    assert generator.extract_targets("https://t.me/g1 https://t.me/g1") == ["https://t.me/g1"]
    assert generator.extract_targets("просто текст") == []


def test_sanitize_filename_basic():
    assert generator.sanitize_filename("bank-loophole") == "bank-loophole"
    assert generator.sanitize_filename("") == "parser"
    assert generator.sanitize_filename(".../etc/passwd") == "etc_passwd"


def test_build_requirements_includes_base_and_extras():
    req = generator.build_requirements(["fake-pkg==1.0"])
    assert "scrapy" in req
    assert "playwright" in req
    assert "playwright-stealth" in req
    assert "httpx" in req
    assert "fake-pkg==1.0" in req


def test_create_parser_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "CATALOG_DIR", tmp_path)
    path = generator.create_parser_dir(7, "bank-test")
    assert path.exists()
    assert path.name == "parser_7_bank-test"


def test_write_requirements_and_code(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "CATALOG_DIR", tmp_path)
    d = generator.create_parser_dir(8, "x")
    generator.write_requirements(d, "httpx\nfoo\n")
    generator.write_parser_code(d, "print(1)\n")
    assert (d / "requirements.txt").read_text() == "httpx\nfoo\n"
    assert (d / "parser.py").read_text() == "print(1)\n"


@pytest.mark.asyncio
async def test_validation_success_sets_ready_status(
    session, workspace_id, catalog_dir, monkeypatch,
):
    results = [{"title": "t", "url": "https://a.ru/1", "snippet": "s"}]

    class _FakeRunner:
        def __init__(self, parser_id, code_path, *, workspace_id=None,
                     session=None, run_id=None, trigger="validation"):
            self.parser_id = parser_id
            self.run_id = run_id
            self.code_path = code_path
            self.session = session
            self._returncode = 0
        async def start(self):
            pass
        async def wait(self, timeout=None, finalize=True):
            parser_dir = Path(self.code_path).parent
            parser_dir.mkdir(parents=True, exist_ok=True)
            (parser_dir / "results.json").write_text(
                json.dumps(results), encoding="utf-8",
            )
            return 1
        def _finalize(self, status, found, new, dup, err, *, update_parser_status=True):
            if self.run_id is not None:
                repo.finish_run(
                    self.run_id, status,
                    items_found=found, items_new=new, items_dup=dup,
                    error_text=err, session=self.session,
                )
            if update_parser_status:
                repo.update_parser_status(self.parser_id, status, session=self.session)

    monkeypatch.setattr(generator.runner_mod, "ParserRunner", _FakeRunner)
    pid = repo.save_parser(
        workspace_id, "x", "",
        config={"query": "q", "targets": ["https://a.ru/1"]},
        session=session,
    )
    code_path = str(catalog_dir / f"parser_{pid}_x" / "parser.py")
    repo.update_parser_code_path(pid, code_path, session=session)
    validation_run_id = await generator.start_validation(pid, code_path, session=session)
    await asyncio.sleep(0.2)
    assert repo.get_parser(pid, session=session)["status"] == "ready"
    assert validation_run_id > 0
    run = repo.get_run(validation_run_id, session=session)
    assert run["status"] == "success"
