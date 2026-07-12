"""Тест базы знаний loophole.kb: примеры лазеек, поиск, сид.

Без сети и реальной БД: embedder.embed_one мокается (возвращает 1024d вектор),
сессия SQLAlchemy — mock-объект с execute/scalar_one/mappings. Проверяем
делегирование в loophole.repository.save_kb_example / search_kb_similar и
корректность SQL list_examples/count_examples/add_doc.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from bank_audit.loophole.kb import repository as kb_repo
from bank_audit.loophole.kb import seed as kb_seed


EMB_DIM = 1024


@pytest.fixture
def mock_embed():
    """Мокает embedder.embed_one — возвращает 1024d вектор из единиц."""
    fake_vec = [1.0] * EMB_DIM
    with patch.object(kb_repo.embedder, "embed_one", return_value=fake_vec) as m:
        yield m, fake_vec


# ── Mock сессии ─────────────────────────────────────────────────────────────
class _Mappings:
    def __init__(self, rows: list[dict] | None = None):
        self._rows = rows or []

    def all(self) -> list[dict]:
        return self._rows

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _Result:
    """Имитирует результат session.execute(): scalar_one / mappings()."""

    def __init__(self, scalar=None, rows: list[dict] | None = None):
        self._scalar = scalar
        self._mappings = _Mappings(rows)

    def scalar_one(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar

    def mappings(self) -> _Mappings:
        return self._mappings


class MockSession:
    """SQLAlchemy-сессия-мок: логирует execute() и возвращает заданные результаты."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        # Очередь результатов, возвращаемых execute(). Каждый элемент — _Result.
        self.results: list[_Result] = []

    def execute(self, stmt, params=None):
        sql_text = str(stmt)
        self.calls.append((sql_text, params or {}))
        if self.results:
            return self.results.pop(0)
        return _Result(scalar=None, rows=[])

    def commit(self) -> None:
        return None


# ── add_example ─────────────────────────────────────────────────────────────
def test_add_example_delegates_to_save_kb_example(mock_embed):
    m, fake_vec = mock_embed
    with patch.object(kb_repo.repo, "save_kb_example", return_value=42) as save_mock:
        result = kb_repo.add_example(
            "Скрытая комиссия", "Банк не раскрывает ПСК", category="hidden_fees"
        )
    assert result == 42
    m.assert_called_once_with("Банк не раскрывает ПСК")
    save_mock.assert_called_once()
    args, kwargs = save_mock.call_args
    assert args == ("Скрытая комиссия", "Банк не раскрывает ПСК")
    assert kwargs["category"] == "hidden_fees"
    assert kwargs["embedding"] == fake_vec
    assert kwargs["session"] is None


def test_add_example_default_category(mock_embed):
    with patch.object(kb_repo.repo, "save_kb_example", return_value=1) as save_mock:
        kb_repo.add_example("title", "desc")
    _, kwargs = save_mock.call_args
    assert kwargs["category"] == "general"


# ── search_similar ──────────────────────────────────────────────────────────
def test_search_similar_delegates_to_search_kb_similar(mock_embed):
    m, fake_vec = mock_embed
    expected = [{"example_id": 1, "title": "x", "distance": 0.1}]
    with patch.object(kb_repo.repo, "search_kb_similar", return_value=expected) as search_mock:
        result = kb_repo.search_similar("скрытая комиссия", k=3)
    assert result == expected
    m.assert_called_once_with("скрытая комиссия")
    search_mock.assert_called_once_with(fake_vec, k=3, session=None)


def test_search_similar_default_k(mock_embed):
    with patch.object(kb_repo.repo, "search_kb_similar", return_value=[]) as search_mock:
        kb_repo.search_similar("запрос")
    _, kwargs = search_mock.call_args
    assert kwargs["k"] == 5


# ── list_examples ───────────────────────────────────────────────────────────
def test_list_examples_no_filter():
    s = MockSession()
    s.results.append(_Result(rows=[{"example_id": 1, "title": "a"}]))
    out = kb_repo.list_examples(session=s)
    assert out == [{"example_id": 1, "title": "a"}]
    assert "SELECT" in s.calls[0][0]
    assert "loophole_kb_example" in s.calls[0][0]
    assert "WHERE" not in s.calls[0][0]
    assert s.calls[0][1]["limit"] == 100


def test_list_examples_with_category_filter():
    s = MockSession()
    s.results.append(_Result(rows=[]))
    kb_repo.list_examples(category="hidden_fees", limit=10, session=s)
    sql, params = s.calls[0]
    assert "WHERE" in sql
    assert "category = :cat" in sql
    assert params == {"cat": "hidden_fees", "limit": 10}


# ── count_examples ──────────────────────────────────────────────────────────
def test_count_examples():
    s = MockSession()
    s.results.append(_Result(scalar=7))
    n = kb_repo.count_examples(session=s)
    assert n == 7
    assert "count(*)" in s.calls[0][0]
    assert "loophole_kb_example" in s.calls[0][0]


# ── add_doc ─────────────────────────────────────────────────────────────────
def test_add_doc_embeds_and_inserts(mock_embed):
    m, fake_vec = mock_embed
    s = MockSession()
    s.results.append(_Result(scalar=99))
    doc_id = kb_repo.add_doc("cbr://doc/1", "текст документа", session=s)
    assert doc_id == 99
    m.assert_called_once_with("текст документа")
    sql, params = s.calls[0]
    assert "INSERT INTO loophole_kb_doc" in sql
    assert "RETURNING doc_id" in sql
    assert params["source"] == "cbr://doc/1"
    assert params["content"] == "текст документа"
    # emb — строковое pgvector-представление
    assert isinstance(params["emb"], str)
    assert params["emb"].startswith("[")
    assert params["emb"].endswith("]")


# ── seed_examples ───────────────────────────────────────────────────────────
def test_seed_examples_empty_db_adds_all(mock_embed, monkeypatch):
    # Принуждаем seed использовать встроенный список (YAML может отсутствовать
    # или быть некорректным в окружении теста).
    monkeypatch.setattr(kb_seed, "SEED_YAML_PATH", __import__("pathlib").Path("__nonexistent__.yaml"))

    added_ids = iter(range(1, 1000))
    with patch.object(kb_repo, "count_examples", return_value=0), \
         patch.object(kb_repo, "add_example", side_effect=lambda *a, **k: next(added_ids)) as add_mock:
        n = kb_seed.seed_examples()
    assert n == len(kb_seed._BUILTIN_EXAMPLES)
    assert add_mock.call_count == len(kb_seed._BUILTIN_EXAMPLES)


def test_seed_examples_non_empty_db_skips(mock_embed):
    with patch.object(kb_repo, "count_examples", return_value=5), \
         patch.object(kb_repo, "add_example") as add_mock:
        n = kb_seed.seed_examples()
    assert n == 0
    add_mock.assert_not_called()


def test_seed_examples_returns_builtin_count(mock_embed, monkeypatch):
    monkeypatch.setattr(kb_seed, "SEED_YAML_PATH", __import__("pathlib").Path("__nonexistent__.yaml"))
    with patch.object(kb_repo, "count_examples", return_value=0), \
         patch.object(kb_repo, "add_example", return_value=1):
        n = kb_seed.seed_examples()
    # 15 встроенных примеров
    assert n == 15


# ── seed YAML loading ───────────────────────────────────────────────────────
def test_load_seed_examples_fallback_when_no_file(monkeypatch):
    monkeypatch.setattr(kb_seed, "SEED_YAML_PATH", __import__("pathlib").Path("__nonexistent__.yaml"))
    examples = kb_seed._load_seed_examples()
    assert len(examples) == 15
    assert all(isinstance(t, tuple) and len(t) == 3 for t in examples)
    titles = {t[0] for t in examples}
    assert "Скрытые комиссии по кредиту" in titles
    assert "Отказ в реструктуризации" in titles


def test_load_seed_examples_from_yaml(tmp_path, monkeypatch):
    yaml_path = tmp_path / "seed.yaml"
    yaml_path.write_text(
        "examples:\n"
        "  - title: Тест1\n"
        "    description: Описание1\n"
        "    category: cat1\n"
        "  - title: Тест2\n"
        "    description: Описание2\n"
        "    category: cat2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kb_seed, "SEED_YAML_PATH", yaml_path)
    examples = kb_seed._load_seed_examples()
    assert len(examples) == 2
    assert examples[0] == ("Тест1", "Описание1", "cat1")
    assert examples[1] == ("Тест2", "Описание2", "cat2")
