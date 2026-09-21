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


def test_fraud_and_loophole_findings_all_reach_catalog(session):
    """Все находки агента (лазейка, схема, «не лазейка») попадают в loophole_record."""
    from bank_audit.loophole.agent import AgentRunContext, eligible_findings
    from bank_audit.loophole.chat.tools_nanobot import ToolContext
    from bank_audit.loophole.chat.graph import _persist_confirmed_findings

    _create_import_schema(session)
    loophole_url = "https://bank.example/loophole"
    fraud_url = "https://bank.example/fraud"
    tool_context = ToolContext(
        user_id="analyst",
        workspace_id=1,
        session=session,
        fetched_sources={
            loophole_url: {
                "url": loophole_url, "title": "Лазейка",
                "extracted_text": "Механизм обхода комиссии описан.",
                "published_at": None,
            },
            fraud_url: {
                "url": fraud_url, "title": "Схема",
                "extracted_text": "Описание мошеннической схемы вывода средств.",
                "published_at": None,
            },
        },
    )
    findings_input = [
        {
            "title": "Обход комиссии", "url": loophole_url,
            "evidence_quote": "Механизм обхода комиссии описан.",
            "is_loophole": True, "classification": "vulnerability",
        },
        {
            "title": "Схема вывода", "url": fraud_url,
            "evidence_quote": "Описание мошеннической схемы вывода средств.",
            "is_loophole": True, "classification": "fraud_scheme",
        },
    ]
    run_context = AgentRunContext(
        "analyst", 1, "Найди 1 мошенническую схему", "run-all-findings",
        pending_records=findings_input,
        fetched_sources=tool_context.fetched_sources,
    )
    # Persistence-контур сохраняет все типы, независимо от запрошенного типа.
    findings = eligible_findings(run_context, kind=None)
    assert len(findings) == 2

    _persist_confirmed_findings(
        findings,
        sources=list(tool_context.fetched_sources.values()),
        workspace_id=1, user_id="analyst",
        run_id="run-all-findings", query="Найди 1 мошенническую схему", session=session,
    )
    rows = repo.list_catalog_cases(session=session)
    assert len(rows) == 2
    by_classification = {row["classification"]: row for row in rows}
    assert by_classification["vulnerability"]["status"] == "preliminary"
    assert by_classification["fraud_scheme"]["status"] == "preliminary"
    assert by_classification["fraud_scheme"]["is_loophole"] is True
    assert repo.list_catalog_cases(classification="fraud_scheme", session=session)


def test_triaged_subagent_items_reach_catalog_as_preliminary_leads(session):
    """Разметка сниппетов subagents попадает в каталог с пометкой subagent_triage."""
    _create_import_schema(session)
    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="run-triaged",
        query="проверь схемы",
        findings=[],
        sources=[],
        triaged_items=[
            {
                "url": "https://example.ru/fraud-post", "title": "Схема вывода",
                "snippet": "Описание мошеннической схемы вывода средств",
                "category": "fraud", "content_type": "post", "reason": "Признаки обмана",
            },
            {
                "url": "https://example.ru/ad", "title": "Реклама",
                "snippet": "Обычная реклама карты", "category": "irrelevant",
                "content_type": "article", "reason": "Штатная реклама",
            },
        ],
    )
    imported = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert imported["imported"] == 1
    rows = repo.list_catalog_cases(session=session)
    assert len(rows) == 1
    row = rows[0]
    assert row["url"] == "https://example.ru/fraud-post"
    assert row["classification"] == "fraud_scheme"
    assert row["status"] == "preliminary"
    assert row["is_loophole"] is True
    assert row["verdict_model"] == "subagent_triage"
    assert row["content_status"] == "legacy"
    # Повторный импорт идемпотентен.
    again = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert again["imported"] == 0


def test_triaged_snippet_publication_date_reaches_source_and_catalog(session):
    """Видимая дата triaged-сниппета сохраняется в источнике и переносится в каталог.

    Битая/отсутствующая дата даёт NULL без исключений (_normalize_published_at
    и published_date_from_text fail-closed по дате).
    """
    _create_import_schema(session)
    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="run-triaged-dated",
        query="проверь схемы",
        findings=[],
        sources=[],
        triaged_items=[
            {
                "url": "https://example.ru/dated-post", "title": "Схема вывода",
                "snippet": "Опубликовано 5 августа 2026: описание схемы вывода средств",
                "category": "fraud", "content_type": "post", "reason": "Признаки обмана",
            },
            {
                "url": "https://example.ru/undated-post", "title": "Без даты",
                "snippet": "Описание мошеннической схемы без даты публикации",
                "category": "fraud", "content_type": "post", "reason": "Признаки обмана",
            },
            {
                "url": "https://example.ru/broken-date-post", "title": "Битая дата",
                "snippet": "Опубликовано неизвестно когда: описание схемы вывода",
                "category": "fraud", "content_type": "post", "reason": "Признаки обмана",
            },
        ],
    )

    rows = session.execute(
        text("SELECT url, published_at FROM loophole_research_source ORDER BY source_id")
    ).mappings().all()
    by_url = {row["url"]: row["published_at"] for row in rows}
    assert str(by_url["https://example.ru/dated-post"]).startswith("2026-08-05")
    assert by_url["https://example.ru/undated-post"] is None
    assert by_url["https://example.ru/broken-date-post"] is None

    # Существующий перенос preliminary-источников в каталог несёт дату с собой.
    imported = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert imported["imported"] == 3
    record_row = session.execute(
        text(
            "SELECT published_at FROM loophole_record "
            "WHERE url = 'https://example.ru/dated-post'"
        )
    ).scalar_one()
    assert str(record_row).startswith("2026-08-05")


def test_import_upgrades_unmarked_record_saved_by_tool(session):
    """URL, сохранённый audit_save_loophole без вердикта, доводится до вердикта исследования.

    Регрессия: немаркированная запись блокировала перенос маркировки
    (exists_url-дедуп) и оставалась невидимой в общей базе навсегда.
    """
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    _create_import_schema(session)
    url = "https://bank.example/rules"
    saved = save_loophole(
        title="Сохранено инструментом",
        url=url,
        snippet="Комиссия",
        raw_text="Полный текст страницы: комиссия указана только в примечании.",
        session=session,
    )
    assert saved["is_new"] is True
    before = repo.get_record(saved["record_id"], session=session)
    assert before["classification"] is None and before["is_loophole"] is None

    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="run-upgrade",
        query="проверь комиссии",
        sources=[{"url": url, "title": "Условия продукта",
                  "extracted_text": "Комиссия указана только в примечании."}],
        findings=[{"url": url, "title": "Скрытая комиссия",
                   "snippet": "Комиссия указана только в примечании.",
                   "is_loophole": True, "classification": "vulnerability"}],
    )

    imported = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert imported["imported"] == 0
    assert imported["upgraded"] == 1
    assert imported["record_ids"] == [saved["record_id"]]

    after = repo.get_record(saved["record_id"], session=session)
    assert after["classification"] == "vulnerability"
    assert after["is_loophole"] is True
    assert after["verdict_model"] == "research_preliminary"
    assert after["status"] == "preliminary"
    # Дубль по URL не создан, provenance переноса записан.
    assert session.execute(
        text("SELECT count(*) FROM loophole_record WHERE url = :u"), {"u": url}
    ).scalar_one() == 1
    source_id = session.execute(
        text("SELECT source_id FROM loophole_research_source WHERE research_id = :rid"),
        {"rid": persisted["research_id"]},
    ).scalar_one()
    provenance = session.execute(
        text(
            "SELECT record_id FROM loophole_preliminary_import "
            "WHERE research_id = :rid AND source_id = :sid"
        ),
        {"rid": persisted["research_id"], "sid": source_id},
    ).mappings().one()
    assert provenance["record_id"] == saved["record_id"]
    # Запись стала видимой в общей базе.
    rows = repo.list_catalog_cases(session=session)
    assert [row["record_id"] for row in rows] == [saved["record_id"]]
    # Повторный импорт идемпотентен: upgraded не накапливается.
    again = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert again["upgraded"] == 0
    assert again["skipped"] >= 1


def test_import_does_not_overwrite_marked_record_with_same_url(session):
    """Размеченная запись по тому же URL не перезаписывается вердиктом исследования."""
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    _create_import_schema(session)
    url = "https://bank.example/rules"
    saved = save_loophole(
        title="Помечено аудитором",
        url=url,
        snippet="Комиссия",
        raw_text="Полный текст страницы: комиссия указана только в примечании.",
        is_loophole=False,
        session=session,
    )
    assert repo.get_record(saved["record_id"], session=session)["classification"] == "not_confirmed"

    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="run-no-overwrite",
        query="проверь комиссии",
        sources=[{"url": url, "title": "Условия продукта",
                  "extracted_text": "Комиссия указана только в примечании."}],
        findings=[{"url": url, "title": "Скрытая комиссия",
                   "snippet": "Комиссия указана только в примечании.",
                   "is_loophole": True, "classification": "vulnerability"}],
    )

    imported = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert imported["imported"] == 0
    assert imported["upgraded"] == 0
    assert imported["skipped"] == 1
    after = repo.get_record(saved["record_id"], session=session)
    assert after["classification"] == "not_confirmed"
    assert after["is_loophole"] is False


def test_import_upgrades_unmarked_record_from_subagent_triage(session):
    """Triaged-разметка субагента по URL из немаркированной записи ставит fraud_scheme."""
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    _create_import_schema(session)
    url = "https://example.ru/fraud-post"
    saved = save_loophole(
        title="Сохранено инструментом",
        url=url,
        snippet="Схема",
        raw_text="Полный текст страницы: описание мошеннической схемы вывода средств.",
        session=session,
    )
    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="run-upgrade-triaged",
        query="проверь схемы",
        findings=[],
        sources=[],
        triaged_items=[{
            "url": url, "title": "Схема вывода",
            "snippet": "Описание мошеннической схемы вывода средств",
            "category": "fraud", "content_type": "post", "reason": "Признаки обмана",
        }],
    )

    imported = service.import_preliminary_sources(
        persisted["research_id"], imported_by="analyst",
    )
    assert imported["upgraded"] == 1
    after = repo.get_record(saved["record_id"], session=session)
    assert after["classification"] == "fraud_scheme"
    assert after["is_loophole"] is True
    assert after["verdict_model"] == "subagent_triage"


def test_save_loophole_sets_classification_consistent_with_verdict(session):
    """Инструмент сохраняет согласованную пару (is_loophole, classification)."""
    from bank_audit.loophole.chat.tools_nanobot import save_loophole

    _create_import_schema(session)
    base = {
        "snippet": "Комиссия",
        "raw_text": "Полный текст страницы: комиссия указана только в примечании.",
    }
    positive = save_loophole(
        title="Лазейка", url="https://bank.example/pos", is_loophole=True,
        session=session, **base,
    )
    negative = save_loophole(
        title="Не лазейка", url="https://bank.example/neg", is_loophole=False,
        session=session, **base,
    )
    without = save_loophole(
        title="Без вердикта", url="https://bank.example/none",
        session=session, **base,
    )
    pos = repo.get_record(positive["record_id"], session=session)
    neg = repo.get_record(negative["record_id"], session=session)
    none_row = repo.get_record(without["record_id"], session=session)
    assert (pos["is_loophole"], pos["classification"]) == (True, "vulnerability")
    assert (neg["is_loophole"], neg["classification"]) == (False, "not_confirmed")
    assert none_row["is_loophole"] is None and none_row["classification"] is None
