"""Тест: save_loophole гарантирует полный контент (серверный fetch).

Точка гарантии: если агент не передал raw_text (или он короче сниппета) —
save_loophole сама скачивает страницу. Запись сохраняется всегда.
"""
from __future__ import annotations

from bank_audit.loophole import content_fetch
from bank_audit.loophole.chat import tools_nanobot
from bank_audit.loophole.config import LoopholeSettings


def _full(text, status=content_fetch.STATUS_FULL, truncated=False):
    return content_fetch.FullContent(
        text=text, status=status, length=len(text or ""), truncated=truncated
    )


def test_save_loophole_fetches_full_content(session, monkeypatch):
    calls = []

    def fake_fetch(url, *, settings=None, _fetch_impl=None):
        calls.append(url)
        return _full("ПОЛНЫЙ ТЕКСТ СТРАНИЦЫ " * 10)

    monkeypatch.setattr(tools_nanobot.content_fetch, "fetch_full_content", fake_fetch)
    out = tools_nanobot.save_loophole(
        title="схема", url="https://x.ru/a", snippet="короткая цитата",
        session=session, settings=LoopholeSettings(),
    )
    assert out["record_id"] is not None
    assert calls == ["https://x.ru/a"]
    row = tools_nanobot.repo.get_record(out["record_id"], session=session)
    assert row["raw_text"].startswith("ПОЛНЫЙ ТЕКСТ СТРАНИЦЫ")
    assert row["content_status"] == "full"
    assert row["raw_text_len"] == len(row["raw_text"])


def test_save_loophole_fetch_failed_keeps_snippet(session, monkeypatch):
    """Fetch упал → запись всё равно сохраняется: сниппет + честный статус."""
    monkeypatch.setattr(
        tools_nanobot.content_fetch, "fetch_full_content",
        lambda url, *, settings=None, _fetch_impl=None: _full(
            None, content_fetch.STATUS_FAILED
        ),
    )
    out = tools_nanobot.save_loophole(
        title="схема", url="https://x.ru/dead", snippet="цитата-доказательство",
        session=session, settings=LoopholeSettings(),
    )
    assert out["record_id"] is not None
    row = tools_nanobot.repo.get_record(out["record_id"], session=session)
    assert row["raw_text"] == "цитата-доказательство"
    assert row["content_status"] == "fetch_failed"


def test_save_loophole_agent_raw_text_skips_fetch(session, monkeypatch):
    """Агент передал длинный raw_text → fetch НЕ вызывается, лимит применяется."""

    def forbidden(*a, **kw):
        raise AssertionError("fetch не должен вызываться")

    monkeypatch.setattr(tools_nanobot.content_fetch, "fetch_full_content", forbidden)
    long_text = "длинный текст " * 100  # ~1400 символов > лимита 1000
    out = tools_nanobot.save_loophole(
        title="схема", url="https://x.ru/b", snippet="коротко",
        raw_text=long_text, session=session,
        settings=LoopholeSettings(raw_text_max_chars=1000),
    )
    row = tools_nanobot.repo.get_record(out["record_id"], session=session)
    assert row["content_status"] == "truncated"
    assert row["raw_text_len"] == 1000
    assert row["raw_text_truncated"] in (True, 1)
