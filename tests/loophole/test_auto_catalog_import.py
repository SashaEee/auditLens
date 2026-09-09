"""Автоматический перенос подтверждённых находок в общую базу после анализа."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from bank_audit.loophole import repository as repo
from bank_audit.loophole.chat.graph import _persist_confirmed_findings
from bank_audit.loophole.research_cases import ResearchCaseService
from tests.loophole.test_preliminary_research_source_import import _create_import_schema
from tests.loophole.test_story_2_2_research_cases import _create_research_schema


def _payload() -> tuple[list[dict], list[dict]]:
    sources = [{
        "url": "https://bank.example/rules",
        "title": "Условия продукта",
        "extracted_text": "Комиссия указана только в примечании.",
    }]
    findings = [{
        "url": "https://bank.example/rules",
        "title": "Скрытая комиссия",
        "snippet": "Комиссия указана только в примечании.",
        "category": "комиссии",
        "description": "Условие не вынесено в основное предложение.",
        "severity": "medium",
        "is_loophole": True,
    }]
    return findings, sources


def test_confirmed_findings_auto_imported_to_catalog_as_preliminary(session):
    _create_import_schema(session)
    findings, sources = _payload()

    public = _persist_confirmed_findings(
        findings, sources=sources, workspace_id=1, user_id="analyst",
        run_id="run-auto-import", query="проверь комиссии", session=session,
    )

    assert len(public) == 1
    rows = repo.list_catalog_cases(session=session)
    assert len(rows) == 1
    assert rows[0]["status"] == "preliminary"
    assert rows[0]["is_loophole"] is True
    audit = session.execute(
        text("SELECT user_id, action, detail FROM loophole_action_log")
    ).mappings().one()
    assert audit["user_id"] == "analyst"
    assert audit["action"] == "import_research_sources"
    assert "auto_after_analysis" in audit["detail"]


def test_auto_import_is_idempotent_across_repeat_calls(session):
    _create_import_schema(session)
    findings, sources = _payload()

    first = _persist_confirmed_findings(
        findings, sources=sources, workspace_id=1, user_id="analyst",
        run_id="run-auto-import", query="проверь комиссии", session=session,
    )
    second = _persist_confirmed_findings(
        findings, sources=sources, workspace_id=1, user_id="analyst",
        run_id="run-auto-import", query="проверь комиссии", session=session,
    )

    assert len(first) == 1
    # Повторный persist того же run возвращает существующее исследование без
    # candidate_urls, а повторный импорт дедуплицируется как skipped.
    assert second == []
    assert session.execute(
        text("SELECT count(*) FROM loophole_record")
    ).scalar_one() == 1


def test_auto_import_failure_does_not_raise_and_keeps_public_findings(session):
    # Сломанная таблица импорта не роняет чат: ошибка логируется, находки
    # остаются в публичном ответе, общая база не меняется.
    _create_research_schema(session)
    session.execute(text("DROP TABLE loophole_preliminary_import"))
    findings, sources = _payload()

    public = _persist_confirmed_findings(
        findings, sources=sources, workspace_id=1, user_id="analyst",
        run_id="run-auto-import", query="проверь комиссии", session=session,
    )

    # Публичная карточка строится из in-memory результата persist и возвращается
    # даже при откате автоимпорта; в общей базе записей не появляется.
    assert len(public) == 1
    assert session.execute(
        text("SELECT count(*) FROM loophole_record")
    ).scalar_one() == 0


def test_explicit_non_loophole_reaches_catalog_as_preliminary_not_confirmed(session):
    """Явный is_loophole=False проходит очередь → eligible → persist → импорт."""
    from bank_audit.loophole.agent import AgentRunContext, eligible_findings
    from bank_audit.loophole.chat.tools_nanobot import ToolContext, _queue_confirmed_findings

    _create_import_schema(session)
    url = "https://bank.example/fees"
    tool_context = ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=session,
        fetched_sources={
            url: {
                "url": url,
                "title": "Тарифы",
                "extracted_text": "Комиссия не взимается.",
                "published_at": None,
            }
        },
    )
    _queue_confirmed_findings(
        tool_context,
        [{
            "title": "Штатная комиссия",
            "evidence_quote": "Комиссия не взимается.",
            "is_loophole": False,
        }],
        source_url=url,
        bank_slug=None,
        raw_text="",
    )
    assert [item["is_loophole"] for item in tool_context.pending_records] == [False]

    run_context = AgentRunContext(
        "analyst", 1, "проверь комиссии", "run-negative",
        pending_records=tool_context.pending_records,
        fetched_sources=tool_context.fetched_sources,
    )
    findings = eligible_findings(run_context)
    assert len(findings) == 1

    public = _persist_confirmed_findings(
        findings,
        sources=list(tool_context.fetched_sources.values()),
        workspace_id=1, user_id="analyst",
        run_id="run-negative", query="проверь комиссии", session=session,
    )

    # В чат-карточки попадают только лазейки; в общей базе запись есть
    # со статусом preliminary и типом not_confirmed.
    assert public == []
    rows = repo.list_catalog_cases(session=session)
    assert len(rows) == 1
    assert rows[0]["is_loophole"] is False
    assert rows[0]["classification"] == "not_confirmed"
    assert rows[0]["status"] == "preliminary"
    assert repo.list_catalog_cases(classification="confirmed", session=session) == []
    assert len(repo.list_catalog_cases(classification="not_confirmed", session=session)) == 1


def test_findings_without_verdict_are_dropped_from_queue(session):
    """Отсутствие ключа is_loophole по-прежнему отбрасывает находку."""
    from bank_audit.loophole.agent import AgentRunContext, eligible_findings
    from bank_audit.loophole.chat.tools_nanobot import ToolContext, _queue_confirmed_findings

    url = "https://bank.example/fees"
    tool_context = ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=session,
        fetched_sources={
            url: {
                "url": url,
                "title": "Тарифы",
                "extracted_text": "Комиссия указана в примечании.",
                "published_at": None,
            }
        },
    )
    _queue_confirmed_findings(
        tool_context,
        [{"title": "Без вердикта", "evidence_quote": "Комиссия указана в примечании."}],
        source_url=url,
        bank_slug=None,
        raw_text="",
    )
    assert tool_context.pending_records == []

    # И на границе eligible: None-вердикт не проходит проверку качества.
    run_context = AgentRunContext(
        "analyst", 1, "проверь комиссии", "run-no-verdict",
        pending_records=[{
            "title": "Без вердикта", "url": url,
            "evidence_quote": "Комиссия указана в примечании.",
            "is_loophole": None,
        }],
        fetched_sources=tool_context.fetched_sources,
    )
    assert eligible_findings(run_context) == []


def test_persist_managed_run_normalizes_published_at_values(session):
    """Мусорная дата публикации не роняет сохранение источника (timestamptz)."""
    _create_import_schema(session)
    service = ResearchCaseService(session)
    sources = [
        {"url": "https://bank.example/bad-date", "title": "Плохая дата",
         "extracted_text": "Текст первого источника.", "published_at": "неизвестно"},
        {"url": "https://bank.example/iso-date", "title": "ISO со смещением",
         "extracted_text": "Текст второго источника.",
         "published_at": "2026-08-27T09:25:00+03:00"},
        {"url": "https://bank.example/z-date", "title": "ISO с Z",
         "extracted_text": "Текст третьего источника.",
         "published_at": "2026-08-27T06:25:00Z"},
        {"url": "https://bank.example/no-date", "title": "Без даты",
         "extracted_text": "Текст четвёртого источника.", "published_at": None},
        {"url": "https://bank.example/obj-date", "title": "Объекты",
         "extracted_text": "Текст пятого источника.",
         "published_at": date(2026, 8, 27)},
        {"url": "https://bank.example/dt-date", "title": "Datetime",
         "extracted_text": "Текст шестого источника.",
         "published_at": datetime(2026, 8, 27, 9, 25)},
    ]

    persisted = service.persist_managed_run(
        workspace_id=1, run_id="run-dates", query="проверь комиссии",
        findings=[], sources=sources,
    )

    assert persisted["research_id"]
    rows = session.execute(
        text("SELECT url, published_at FROM loophole_research_source ORDER BY source_id")
    ).mappings().all()
    by_url = {row["url"]: row["published_at"] for row in rows}
    assert by_url["https://bank.example/bad-date"] is None
    assert by_url["https://bank.example/no-date"] is None
    assert str(by_url["https://bank.example/iso-date"]).startswith("2026-08-27")
    assert str(by_url["https://bank.example/z-date"]).startswith("2026-08-27")
    assert str(by_url["https://bank.example/obj-date"]).startswith("2026-08-27")
    assert str(by_url["https://bank.example/dt-date"]).startswith("2026-08-27")


def test_persist_managed_run_skips_source_with_insert_error(session, monkeypatch):
    """Savepoint: сбой записи одного источника не откатывает весь прогон."""
    _create_import_schema(session)
    original = ResearchCaseService.record_source

    def flaky(self, research_id, *, url, title, extracted_text, published_at=None):
        if "broken" in url:
            raise OperationalError("INSERT", {}, Exception("boom"))
        return original(
            self, research_id, url=url, title=title,
            extracted_text=extracted_text, published_at=published_at,
        )

    monkeypatch.setattr(ResearchCaseService, "record_source", flaky)
    service = ResearchCaseService(session)
    sources = [
        {"url": "https://bank.example/broken", "title": "Битый",
         "extracted_text": "Текст битого источника."},
        {"url": "https://bank.example/fine", "title": "Целый",
         "extracted_text": "Текст целого источника."},
    ]
    findings = [{
        "url": "https://bank.example/fine", "title": "Скрытая комиссия",
        "snippet": "Текст целого источника.", "is_loophole": True,
    }]

    persisted = service.persist_managed_run(
        workspace_id=1, run_id="run-flaky", query="проверь комиссии",
        findings=findings, sources=sources,
    )

    assert persisted["candidate_ids"]
    rows = session.execute(
        text("SELECT url FROM loophole_research_source ORDER BY source_id")
    ).scalars().all()
    assert rows == ["https://bank.example/fine"]


def test_auto_import_survives_broken_source_date(session):
    """Регрессия: прогон с мусорной датой всё равно автоимпортирует находки."""
    _create_import_schema(session)
    findings, sources = _payload()
    sources[0]["published_at"] = "неизвестно"

    public = _persist_confirmed_findings(
        findings, sources=sources, workspace_id=1, user_id="analyst",
        run_id="run-broken-date", query="проверь комиссии", session=session,
    )

    assert len(public) == 1
    rows = repo.list_catalog_cases(session=session)
    assert len(rows) == 1
    assert rows[0]["status"] == "preliminary"
    assert rows[0]["published_at"] is None
