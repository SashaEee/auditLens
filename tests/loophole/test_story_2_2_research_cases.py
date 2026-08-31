"""TDD-контракт Story 2.2: кандидаты существуют только внутри исследования."""
from __future__ import annotations

import asyncio

from sqlalchemy import text


def _create_research_schema(session) -> None:
    session.execute(text("""
        CREATE TABLE loophole_research (
            research_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            query_text TEXT NOT NULL,
            search_params TEXT NOT NULL
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_research_source (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            extracted_text TEXT,
            published_at TEXT,
            status TEXT NOT NULL,
            limitation_message TEXT,
            access_status TEXT NOT NULL DEFAULT 'active',
            revision INTEGER NOT NULL DEFAULT 1
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_research_candidate (
            candidate_id INTEGER PRIMARY KEY AUTOINCREMENT,
            research_id INTEGER NOT NULL,
            source_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            evidence TEXT NOT NULL,
            category TEXT,
            description TEXT NOT NULL,
            severity TEXT NOT NULL,
            is_loophole INTEGER NOT NULL,
            model_is_loophole INTEGER,
            model_confidence REAL,
            model_reason TEXT,
            model_name TEXT,
            model_classified_at TEXT,
            manual_verdict INTEGER,
            ccks_decision TEXT,
            draft_version INTEGER NOT NULL DEFAULT 1
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_verification_snapshot (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            candidate_id INTEGER NOT NULL,
            research_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            draft_version INTEGER NOT NULL,
            case_snapshot TEXT NOT NULL,
            evidence_snapshot TEXT NOT NULL,
            submitted_by TEXT NOT NULL,
            submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT NOT NULL,
            status TEXT NOT NULL
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_verification_decision (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            decision TEXT NOT NULL,
            comment TEXT NOT NULL,
            decided_by TEXT NOT NULL,
            decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            run_id TEXT NOT NULL
        )
    """))
    session.execute(text("""
        CREATE TABLE loophole_publication_mapping (
            publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            command_key TEXT NOT NULL,
            record_id INTEGER,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT
        )
    """))


def test_research_case_service_keeps_candidate_and_source_outside_catalog(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=1,
        run_id="research-2-2",
        query="проверь скрытую комиссию",
        search_params={"max_results": 12},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="В договоре указана комиссия.",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="В договоре указана комиссия.",
        category="комиссия",
        description="Комиссия не выделена в рекламном предложении.",
        severity="medium",
        is_loophole=True,
    )

    candidate = service.get_candidate(candidate_id)
    assert candidate["research_id"] == research_id
    assert candidate["source_url"] == "https://example.ru/terms"
    assert candidate["evidence"] == "В договоре указана комиссия."
    assert candidate["is_loophole"] is True
    assert candidate["search_params"] == {"max_results": 12}
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 0


def test_managed_run_persists_fetched_source_even_without_candidate(session):
    """Прочитанный источник входит в evidence, даже если лазейка не подтверждена."""
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    persisted = service.persist_managed_run(
        workspace_id=1,
        run_id="managed-without-candidate",
        query="проверь тариф",
        findings=[],
        sources=[{
            "url": "https://example.ru/rules",
            "title": "Правила",
            "extracted_text": "Полный серверный текст",
            "published_at": "2026-08-01T00:00:00+03:00",
        }],
    )

    assert persisted["candidate_ids"] == []
    source = session.execute(
        text("SELECT url, published_at FROM loophole_research_source")
    ).mappings().one()
    assert source == {
        "url": "https://example.ru/rules",
        "published_at": "2026-08-01T00:00:00+03:00",
    }


def test_unavailable_source_keeps_limitation_and_rejects_candidate(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=1,
        run_id="research-source-failure",
        query="проверь комиссию",
        search_params={"max_results": 2},
    )
    source_id = service.record_source_failure(
        research_id,
        url="https://example.ru/unavailable",
        limitation_message="Источник недоступен: HTTP 503",
    )

    assert service.list_limitations(research_id) == ["Источник недоступен: HTTP 503"]
    assert service.add_candidate(
        research_id,
        source_id=source_id,
        title="Не создавать",
        evidence="",
        category=None,
        description="",
        severity="low",
        is_loophole=False,
    ) is None
    assert session.execute(text("SELECT count(*) FROM loophole_research_candidate")).scalar_one() == 0


def test_collect_research_continues_after_unavailable_source(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)

    result = service.collect_research(
        workspace_id=1,
        run_id="research-collect",
        query="проверь комиссию",
        max_results=2,
        search=lambda *_: [
            {"url": "https://example.ru/unavailable", "title": "Недоступно"},
            {"url": "https://example.ru/terms", "title": "Условия"},
        ],
        fetch=lambda url: None if "unavailable" in url else {"excerpt": "Комиссия в договоре"},
        extract=lambda _: [
            {
                "title": "Скрытая комиссия",
                "evidence_quote": "Комиссия в договоре",
                "description": "Комиссия не видна заранее.",
                "category": "комиссия",
                "severity": "high",
                "is_loophole": True,
            }
        ],
    )

    assert result["candidate_count"] == 1
    assert result["limitations"] == ["Источник недоступен: https://example.ru/unavailable"]
    candidate = service.get_candidate(result["candidate_ids"][0])
    assert candidate["source_url"] == "https://example.ru/terms"


def test_collect_research_keeps_running_when_fetch_raises(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)

    def fetch(url: str):
        if "broken" in url:
            raise OSError("connection refused")
        return {"excerpt": "Текст условия"}

    result = service.collect_research(
        workspace_id=1,
        run_id="research-fetch-error",
        query="проверь условие",
        max_results=2,
        search=lambda *_: [
            {"url": "https://example.ru/broken"},
            {"url": "https://example.ru/available"},
        ],
        fetch=fetch,
        extract=lambda _: [{"title": "Кейс", "evidence_quote": "Текст условия"}],
    )

    assert result["candidate_count"] == 1
    assert result["limitations"] == ["Источник недоступен: https://example.ru/broken"]


def test_collect_research_keeps_limitation_when_extraction_raises(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)

    result = service.collect_research(
        workspace_id=1,
        run_id="research-extract-error",
        query="проверь условие",
        max_results=1,
        search=lambda *_: [{"url": "https://example.ru/terms"}],
        fetch=lambda _: {"excerpt": "Текст условия"},
        extract=lambda _: (_ for _ in ()).throw(ValueError("невалидный ответ модели")),
    )

    assert result["candidate_count"] == 0
    assert result["limitations"] == [
        "Не удалось извлечь текст источника: https://example.ru/terms"
    ]


def test_classification_stores_model_verdict_without_overwriting_manual_case(session):
    from bank_audit.loophole.models import Verdict
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=1,
        run_id="research-classification",
        query="проверь комиссию",
        search_params={"max_results": 2},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="Комиссия скрыта в условии.",
    )
    first_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия скрыта в условии.",
        category="комиссия",
        description="Комиссия не видна заранее.",
        severity="high",
        is_loophole=True,
    )
    manual_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Ручной кейс",
        evidence="Проверено аналитиком.",
        category="комиссия",
        description="Не менять вручную принятый verdict.",
        severity="medium",
        is_loophole=False,
    )
    session.execute(text(
        "UPDATE loophole_research_candidate SET manual_verdict = 1 WHERE candidate_id = :id"
    ), {"id": manual_id})

    async def classifier(candidate_text, *, examples):
        assert "Комиссия" in candidate_text
        assert examples == [{"title": "Подтверждённый пример"}]
        return Verdict(is_loophole=True, confidence=0.91, reason="Прямая цитата")

    result = asyncio.run(
        service.classify_candidates(
            research_id,
            classifier=classifier,
            confirmed_examples=[{"title": "Подтверждённый пример"}],
            model_name="classifier-test",
            batch_size=1,
        )
    )

    assert result == {"classified_ids": [first_id], "failures": []}
    row = session.execute(text(
        "SELECT is_loophole, model_is_loophole, model_confidence, model_reason, model_name "
        "FROM loophole_research_candidate WHERE candidate_id = :id"
    ), {"id": first_id}).mappings().one()
    assert row == {
        "is_loophole": 1,
        "model_is_loophole": 1,
        "model_confidence": 0.91,
        "model_reason": "Прямая цитата",
        "model_name": "classifier-test",
    }
    assert session.execute(text(
        "SELECT model_name FROM loophole_research_candidate WHERE candidate_id = :id"
    ), {"id": manual_id}).scalar_one() is None


def test_classification_reports_partial_failure_and_keeps_success(session):
    from bank_audit.loophole.models import Verdict
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=1,
        run_id="research-classification-failure",
        query="проверь комиссию",
        search_params={"max_results": 2},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="Текст условия.",
    )
    failed_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Ошибка модели",
        evidence="Текст условия.",
        category=None,
        description="Первый кандидат.",
        severity="low",
        is_loophole=False,
    )
    successful_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Успешный кандидат",
        evidence="Текст условия.",
        category=None,
        description="Второй кандидат.",
        severity="low",
        is_loophole=False,
    )

    async def classifier(candidate_text, *, examples):
        if "Ошибка модели" in candidate_text:
            raise RuntimeError("model timeout")
        return Verdict(is_loophole=False, confidence=0.2, reason="Не лазейка")

    result = asyncio.run(
        service.classify_candidates(
            research_id,
            classifier=classifier,
            confirmed_examples=[],
            model_name="classifier-test",
            batch_size=2,
        )
    )

    assert result == {
        "classified_ids": [successful_id],
        "failures": [{"candidate_id": failed_id, "reason": "model timeout"}],
    }
    assert session.execute(text(
        "SELECT model_name FROM loophole_research_candidate WHERE candidate_id = :id"
    ), {"id": failed_id}).scalar_one() is None


def test_submit_selected_case_creates_idempotent_immutable_snapshot(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=7,
        run_id="research-submit",
        query="проверь комиссию",
        search_params={"max_results": 1},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия v1",
        extracted_text="Комиссия скрыта в условии.",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия скрыта в условии.",
        category="комиссия",
        description="Комиссия не видна заранее.",
        severity="high",
        is_loophole=True,
    )

    snapshot = service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="agent-run-7",
    )
    repeated = service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="agent-run-7",
    )
    session.execute(text(
        "UPDATE loophole_research_candidate SET title = 'Изменённый черновик' "
        "WHERE candidate_id = :candidate_id"
    ), {"candidate_id": candidate_id})

    assert repeated == snapshot
    assert snapshot["status"] == "submitted"
    assert snapshot["submitted_by"] == "analyst"
    assert snapshot["run_id"] == "agent-run-7"
    assert snapshot["case"]["title"] == "Скрытая комиссия"
    assert snapshot["evidence"] == [{
        "source_id": source_id,
        "revision": 1,
            "url": "https://example.ru/terms",
            "title": "Условия v1",
            "extracted_text": "Комиссия скрыта в условии.",
            "published_at": None,
        }]
    assert session.execute(text(
        "SELECT count(*) FROM loophole_verification_snapshot"
    )).scalar_one() == 1


def test_submit_rejects_revoked_evidence_without_metadata_disclosure(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=7,
        run_id="research-revoked",
        query="проверь комиссию",
        search_params={"max_results": 1},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/private",
        title="Секретный источник",
        extracted_text="Секретный текст",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Кейс",
        evidence="Секретный текст",
        category=None,
        description="Описание",
        severity="low",
        is_loophole=False,
    )
    session.execute(text(
        "UPDATE loophole_research_source SET access_status = 'revoked' WHERE source_id = :id"
    ), {"id": source_id})

    assert service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="agent-run-7",
    ) is None
    assert session.execute(text(
        "SELECT count(*) FROM loophole_verification_snapshot"
    )).scalar_one() == 0


def test_ccks_decision_is_append_only_and_idempotent_for_submitted_snapshot(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=7,
        run_id="research-decision",
        query="проверь комиссию",
        search_params={"max_results": 1},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="Комиссия скрыта в условии.",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия скрыта в условии.",
        category="комиссия",
        description="Комиссия не видна заранее.",
        severity="high",
        is_loophole=True,
    )
    snapshot = service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="agent-run-submit",
    )

    first = service.decide_snapshot(
        snapshot["snapshot_id"],
        decision="vulnerability",
        comment="Подтверждено источником.",
        decided_by="expert",
        run_id="agent-run-decision",
    )
    repeated = service.decide_snapshot(
        snapshot["snapshot_id"],
        decision="not_confirmed",
        comment="Не должно создать второй verdict.",
        decided_by="expert-2",
        run_id="other-run",
    )

    assert repeated == first
    assert first["decision"] == "vulnerability"
    assert first["comment"] == "Подтверждено источником."
    assert first["decided_by"] == "expert"
    assert session.execute(text(
        "SELECT count(*) FROM loophole_verification_decision"
    )).scalar_one() == 1


def test_positive_decision_publishes_catalog_case_once_and_negative_never_publishes(session):
    from bank_audit.loophole.research_cases import ResearchCaseService

    _create_research_schema(session)
    service = ResearchCaseService(session)
    research_id = service.start_research(
        workspace_id=7,
        run_id="research-publish",
        query="проверь комиссию",
        search_params={"max_results": 1},
    )
    source_id = service.record_source(
        research_id,
        url="https://example.ru/terms",
        title="Условия",
        extracted_text="Комиссия скрыта в условии.",
    )
    candidate_id = service.add_candidate(
        research_id,
        source_id=source_id,
        title="Скрытая комиссия",
        evidence="Комиссия скрыта в условии.",
        category="комиссия",
        description="Комиссия не видна заранее.",
        severity="high",
        is_loophole=True,
    )
    snapshot = service.submit_for_verification(
        candidate_id,
        evidence_source_ids=[source_id],
        submitted_by="analyst",
        correlation_run_id="agent-run-submit",
    )
    decision = service.decide_snapshot(
        snapshot["snapshot_id"],
        decision="vulnerability",
        comment="Подтверждено.",
        decided_by="expert",
        run_id="agent-run-decision",
    )

    first = service.publish_decision(decision["decision_id"], command_key="publish-1")
    repeated = service.publish_decision(decision["decision_id"], command_key="publish-1")

    assert first == repeated
    assert first["status"] == "published"
    assert session.execute(text("SELECT count(*) FROM loophole_record")).scalar_one() == 1
    assert session.execute(text(
        "SELECT count(*) FROM loophole_publication_mapping"
    )).scalar_one() == 1
