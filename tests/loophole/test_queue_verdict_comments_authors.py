"""Очередь верификации и модалка «Вердикт»: все комментарии и их авторы.

Спека: docs/loophole/bmad/implementation-artifacts/
spec-queue-verdict-comments-authors.md

Три слоя приёмки: контрактные JSX/CSS-проверки (фронт без сборки — текстовые
проверки по образцу test_queue_card_comment_and_full_text.py), репозиторные
тесты на session-фикстуре (enrichment очереди, freeze комментария
классификатора) и Greenplum-контракт миграции 068 (по образцу
test_db_schema_048.py).

Покрытие сценариев I/O-матрицы спеки:
- запись с решениями ЦК      → test_queue_enriches_decisions_*,
                               test_card_shows_decisions_*,
                               test_modal_readonly_block_*,
                               test_decision_labels_cover_all_decision_types
- решений нет                → test_empty_decisions_state_is_neutral,
                               test_queue_record_without_decisions_gets_empty_list
- есть комментарий классиф.  → test_modal_classifier_comment_visible_*,
                               test_queue_returns_classifier_verdict_reason
- ручной вердикт (freeze)    → test_update_verdict_freezes_classifier_comment
- legacy-перезапись до 068   → test_update_verdict_freeze_is_idempotent_for_legacy_records,
                               test_classifier_source_falls_back_to_verdict_reason
- модалка из каталога        → test_modal_block_hidden_without_decisions_field
- не-эксперт                 → test_queue_endpoint_keeps_expert_gate
"""
from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import event, text

from bank_audit.config import ROOT
from bank_audit.loophole import repository as repo

SRC = Path(__file__).resolve().parents[2] / "src" / "bank_audit" / "loophole"
LOOPHOLE_JSX = SRC / "static" / "loophole.jsx"
LOOPHOLE_CSS = SRC / "static" / "loophole.css"
LOOPHOLE_WEB = SRC / "web.py"
MIGRATION_068 = ROOT / "migrations" / "068_loophole_record_classifier_comment.sql"

EMPTY_STATE = "Решений ЦК пока нет."


def _jsx() -> str:
    return LOOPHOLE_JSX.read_text(encoding="utf-8")


def _css() -> str:
    return LOOPHOLE_CSS.read_text(encoding="utf-8")


def _web() -> str:
    return LOOPHOLE_WEB.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def _block(css: str, selector: str) -> str:
    """Тело первого правила `selector { ... }` (невложенного)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"в CSS не найдено правило {selector}"
    return m.group(1)


def _queue_card() -> str:
    """Разметка карточки проверки (article.lp-queue-detail)."""
    m = re.search(r'<article className="lp-queue-detail".*?</article>', _jsx(), re.DOTALL)
    assert m, "не найдена карточка проверки article.lp-queue-detail"
    return m.group(0)


def _verdict_modal() -> str:
    """Разметка модалки вердикта (IIFE canMarkVerdict && verdictModal)."""
    source = _jsx()
    start = source.index("canMarkVerdict && verdictModal && (() => {")
    end = source.index("})()}", start)
    return source[start:end]


# ── Репозиторий: сиды цепочки record → import → candidate → snapshot → decision ──


def _insert_record(
    session,
    *,
    sha: str,
    title: str = "Скрытая комиссия",
    verdict_reason: str = "Классификатор: похоже на скрытую комиссию",
    verdict_model: str | None = "model",
) -> int:
    session.execute(
        text(
            "INSERT INTO loophole_record (sha256, title, is_loophole, verdict_reason, "
            "verdict_model, status) VALUES (:sha, :title, 1, :reason, :model, 'preliminary')"
        ),
        {"sha": sha, "title": title, "reason": verdict_reason, "model": verdict_model},
    )
    return session.execute(
        text("SELECT record_id FROM loophole_record WHERE sha256 = :sha"), {"sha": sha},
    ).scalar_one()


def _insert_import(session, *, research_id: int, source_id: int, record_id: int) -> None:
    session.execute(
        text(
            "INSERT INTO loophole_preliminary_import (research_id, source_id, workspace_id, "
            "record_id, imported_by) VALUES (:research, :source, 1, :record, 'analyst')"
        ),
        {"research": research_id, "source": source_id, "record": record_id},
    )


def _insert_candidate(session, *, research_id: int, source_id: int) -> int:
    session.execute(
        text(
            "INSERT INTO loophole_research_candidate (research_id, source_id, title, evidence, "
            "description, severity, is_loophole) VALUES (:research, :source, 'Кейс', "
            "'Комиссия скрыта в условиях', 'Комиссия не видна заранее', 'high', 1)"
        ),
        {"research": research_id, "source": source_id},
    )
    return session.execute(
        text(
            "SELECT candidate_id FROM loophole_research_candidate "
            "WHERE research_id = :research AND source_id = :source"
        ),
        {"research": research_id, "source": source_id},
    ).scalar_one()


def _insert_snapshot(session, *, candidate_id: int, research_id: int) -> int:
    session.execute(
        text(
            "INSERT INTO loophole_verification_snapshot (candidate_id, research_id, workspace_id, "
            "draft_version, case_snapshot, evidence_snapshot, submitted_by, run_id, status) "
            "VALUES (:candidate, :research, 1, 1, '{}', '[]', 'analyst', 'run-1', 'submitted')"
        ),
        {"candidate": candidate_id, "research": research_id},
    )
    return session.execute(
        text(
            "SELECT snapshot_id FROM loophole_verification_snapshot WHERE candidate_id = :candidate"
        ),
        {"candidate": candidate_id},
    ).scalar_one()


def _insert_decision(
    session,
    *,
    snapshot_id: int,
    decision: str = "vulnerability",
    comment: str = "Подтверждено: комиссия не видна заранее.",
    decided_by: str = "ivanov",
    decided_at: str = "2026-09-10T12:30:00",
    run_id: str = "run-1",
) -> int:
    session.execute(
        text(
            "INSERT INTO loophole_verification_decision (snapshot_id, decision, comment, "
            "decided_by, decided_at, run_id) VALUES (:snapshot, :decision, :comment, "
            ":decided_by, :decided_at, :run_id)"
        ),
        {"snapshot": snapshot_id, "decision": decision, "comment": comment,
         "decided_by": decided_by, "decided_at": decided_at, "run_id": run_id},
    )
    return session.execute(
        text(
            "SELECT decision_id FROM loophole_verification_decision WHERE snapshot_id = :snapshot"
        ),
        {"snapshot": snapshot_id},
    ).scalar_one()


def _seed_record_with_two_decisions(session, *, sha: str, base: int = 0) -> tuple[int, list[int]]:
    """Запись с двумя импортами (два snapshot/decision) → все решения в одном списке."""
    record_id = _insert_record(session, sha=sha)
    decision_ids: list[int] = []
    for number, (research_id, source_id, decided_at) in enumerate(
        [(101 + base, 1001 + base, "2026-09-10T15:00:00"),
         (202 + base, 2002 + base, "2026-09-10T09:00:00")], start=1,
    ):
        _insert_import(session, research_id=research_id, source_id=source_id, record_id=record_id)
        candidate_id = _insert_candidate(session, research_id=research_id, source_id=source_id)
        snapshot_id = _insert_snapshot(session, candidate_id=candidate_id, research_id=research_id)
        decision_ids.append(_insert_decision(
            session,
            snapshot_id=snapshot_id,
            decided_by="ivanov" if number == 1 else "petrov",
            decided_at=decided_at,
            run_id=f"run-{number}",
        ))
    return record_id, decision_ids


# ── Репозиторий: обогащение очереди решениями ЦК ─────────────────────────────


def test_queue_enriches_decisions_with_authors_and_order(session):
    """Все решения записи (две импорты) в одном списке, порядок decided_at,
    затем decision_id; форма элемента — как в Design Notes спеки."""
    record_id, decision_ids = _seed_record_with_two_decisions(session, sha="sha-enrich")
    # Вставка идёт в порядке итерации: первое решение (id меньше) имеет более
    # поздний decided_at → проверяем сортировку по decided_at, а не по id.
    first, second = decision_ids

    records = repo.list_verification_queue(session=session)
    by_id = {r["record_id"]: r for r in records}
    decisions = by_id[record_id]["decisions"]

    assert [d["decision_id"] for d in decisions] == [second, first]
    assert decisions[0] == {
        "decision_id": second,
        "snapshot_id": decisions[0]["snapshot_id"],
        "decision": "vulnerability",
        "comment": "Подтверждено: комиссия не видна заранее.",
        "decided_by": "petrov",
        "decided_at": "2026-09-10T09:00:00",
        "run_id": "run-2",
    }
    assert set(decisions[1]) == {
        "decision_id", "snapshot_id", "decision", "comment",
        "decided_by", "decided_at", "run_id",
    }
    assert decisions[1]["decided_by"] == "ivanov"


def test_queue_record_without_decisions_gets_empty_list(session):
    """Запись без импортов/решений получает пустой массив decisions."""
    record_id = _insert_record(session, sha="sha-empty")

    records = repo.list_verification_queue(session=session)
    by_id = {r["record_id"]: r for r in records}

    assert by_id[record_id]["decisions"] == []


def test_queue_response_keeps_backward_compatible_contract(session):
    """Старые поля очереди на месте; новые (classifier_verdict_reason,
    decisions) добавлены; raw_text по-прежнему не отдаётся."""
    _seed_record_with_two_decisions(session, sha="sha-contract")

    record = repo.list_verification_queue(session=session)[0]
    for old_field in ("record_id", "title", "url", "snippet", "domain", "trust_score",
                      "bank_slug", "keyword", "verdict_confidence", "verdict_reason",
                      "status", "published_at", "collected_at", "classified_at"):
        assert old_field in record, f"потеряно старое поле очереди {old_field}"
    assert "classifier_verdict_reason" in record
    assert isinstance(record["decisions"], list)
    assert "raw_text" not in record


def test_queue_enrichment_uses_single_batch_query(session):
    """Батч-обогащение без N+1: на список очереди ровно два SELECT —
    сама очередь и один запрос решений по record_id IN (...)."""
    for number in range(3):
        _seed_record_with_two_decisions(session, sha=f"sha-batch-{number}", base=number * 1000)

    statements: list[str] = []
    engine = session.get_bind()

    def _count(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "after_cursor_execute", _count)
    try:
        records = repo.list_verification_queue(session=session)
    finally:
        event.remove(engine, "after_cursor_execute", _count)

    assert len(records) == 3
    assert all(r["decisions"] for r in records), "решения не прикреплены"
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 2, f"ожидался один батч-запрос, получено SELECT: {len(selects)}"
    assert "IN (" in selects[-1].upper()


def test_queue_returns_classifier_verdict_reason(session):
    """Очередь отдаёт замороженный комментарий классификатора, если он есть."""
    record_id = _insert_record(
        session, sha="sha-frozen-source", verdict_reason="текст классификатора",
    )
    session.execute(
        text(
            "UPDATE loophole_record SET classifier_verdict_reason = 'исходный текст' "
            "WHERE record_id = :id"
        ),
        {"id": record_id},
    )

    record = repo.list_verification_queue(session=session)[0]
    assert record["classifier_verdict_reason"] == "исходный текст"


# ── Репозиторий: freeze комментария классификатора в update_verdict ──────────


def test_update_verdict_freezes_classifier_comment(session):
    """Ручной вердикт: verdict_reason перезаписан, исходный текст классификатора
    заморожен в classifier_verdict_reason; повторный вердикт не меняет freeze."""
    record_id = _insert_record(
        session, sha="sha-freeze", verdict_reason="исходный текст классификатора",
    )

    repo.update_verdict(
        record_id,
        is_loophole=True,
        confidence=1.0,
        reason="комментарий аудитора",
        model="manual",
        classification="vulnerability",
        session=session,
    )
    row = session.execute(
        text(
            "SELECT verdict_reason, classifier_verdict_reason, verdict_model "
            "FROM loophole_record WHERE record_id = :id"
        ),
        {"id": record_id},
    ).one()
    assert row.verdict_reason == "комментарий аудитора"
    assert row.classifier_verdict_reason == "исходный текст классификатора"
    assert row.verdict_model == "manual"

    repo.update_verdict(
        record_id,
        is_loophole=False,
        confidence=1.0,
        reason="не подтвердилось",
        model="manual",
        classification="not_confirmed",
        session=session,
    )
    row = session.execute(
        text(
            "SELECT verdict_reason, classifier_verdict_reason "
            "FROM loophole_record WHERE record_id = :id"
        ),
        {"id": record_id},
    ).one()
    assert row.verdict_reason == "не подтвердилось"
    assert row.classifier_verdict_reason == "исходный текст классификатора"


def test_update_verdict_freeze_is_idempotent_for_legacy_records(session):
    """Если freeze уже заполнен (или перезапись шла до миграции и текста нет),
    COALESCE не подменяет существующее значение."""
    record_id = _insert_record(session, sha="sha-legacy", verdict_reason=None)
    session.execute(
        text("UPDATE loophole_record SET verdict_reason = NULL WHERE record_id = :id"),
        {"id": record_id},
    )

    repo.update_verdict(
        record_id,
        is_loophole=True,
        confidence=1.0,
        reason="manual:test-user",
        model="manual",
        classification="vulnerability",
        session=session,
    )
    row = session.execute(
        text(
            "SELECT verdict_reason, classifier_verdict_reason "
            "FROM loophole_record WHERE record_id = :id"
        ),
        {"id": record_id},
    ).one()
    assert row.verdict_reason == "manual:test-user"
    # Замораживать нечего: legacy-перезапись, текст классификатора утрачен.
    assert row.classifier_verdict_reason is None


# ── Контракт JSX: карточка проверки ──────────────────────────────────────────


def test_card_shows_decisions_with_type_comment_author_and_date():
    """AC1: секция «Решения ЦК КС» показывает тип, комментарий, автора и дату."""
    card = _norm(_queue_card())
    assert _norm("Решения ЦК КС") in card
    assert _norm("{decisionLabel(d.decision)}") in card
    assert _norm("{d.comment}") in card
    assert _norm("{d.decided_by}") in card
    assert _norm("{fmtDate(d.decided_at)}") in card
    # Порядок решений в ответе очереди сохраняется (decided_at, decision_id).
    assert _norm("queueSelected.decisions.map(d =>") in card


def test_card_classifier_comment_uses_frozen_source():
    """AC4/AC5: карточка берёт комментарий классификатора из freeze-колонки
    с fallback на verdict_reason."""
    card = _norm(_queue_card())
    assert _norm("queueSelected.classifier_verdict_reason ?? queueSelected.verdict_reason") in card


def test_classifier_source_falls_back_to_verdict_reason():
    """Матрица «legacy-перезапись до миграции»: пустой freeze → fallback на
    текущий verdict_reason на обеих поверхностях."""
    jsx = _norm(_jsx())
    assert jsx.count(_norm("classifier_verdict_reason ?? queueSelected.verdict_reason")) == 1
    assert _norm("rec.classifier_verdict_reason ?? rec.verdict_reason") in jsx


def test_empty_decisions_state_is_neutral():
    """AC3 / матрица «решений нет»: пустое состояние с поясняющим текстом."""
    empty = _norm(EMPTY_STATE)
    assert empty in _norm(_queue_card())
    assert empty in _norm(_verdict_modal())


# ── Контракт JSX: модалка «Вердикт записи» ───────────────────────────────────


def test_modal_readonly_block_before_auditor_comment_field():
    """AC2: read-only блок решений стоит перед полем «Комментарий аудитора»,
    само поле и выбор типа работают как прежде."""
    modal = _verdict_modal()
    assert _norm('<div className="lp-verdict-decisions">') in _norm(modal)
    positions = [
        modal.index("lp-verdict-decisions"),
        modal.index('id="lp-mark-comment"'),
        modal.index("lp-verdict-options"),
    ]
    assert positions == sorted(positions), "блок решений должен быть до поля комментария"
    # Существующие поля модалки не тронуты.
    assert _norm("<label htmlFor=\"lp-mark-comment\">Комментарий аудитора</label>") in _norm(modal)
    for value in ("vulnerability", "fraud_scheme", "not_confirmed"):
        assert _norm(f'choose("{value}")') in _norm(modal)
    assert _norm("markVerdict([rec.record_id], val, markComment.trim())") in _norm(modal)


def test_modal_block_hidden_without_decisions_field():
    """AC6 / матрица «модалка из каталога»: блок рендерится только при
    Array.isArray(rec.decisions) — каталожные записи без поля блок не получают."""
    modal = _norm(_verdict_modal())
    assert _norm("{Array.isArray(rec.decisions) && (") in modal
    assert _norm("lp-verdict-decisions") in modal


def test_modal_classifier_comment_visible_for_non_manual_verdict():
    """AC4 / матрица «есть комментарий классификатора»: строка видна при
    verdict_model !== "manual" и непустом тексте (freeze ?? текущий)."""
    modal = _norm(_verdict_modal())
    assert _norm('rec.verdict_model !== "manual"') in modal
    assert _norm("Комментарий классификатора:") in modal
    assert _norm("{rec.classifier_verdict_reason ?? rec.verdict_reason}") in modal


def test_decision_labels_cover_all_decision_types():
    """Матрица «запись с решениями ЦК»: все три типа решения имеют метки."""
    jsx = _norm(_jsx())
    mapping = _norm(
        'const decisionLabel = (value) => ({ vulnerability: "Уязвимость", '
        'fraud_scheme: "Мошенническая схема", not_confirmed: "Не подтверждено", '
        "}[value] || value);"
    )
    assert mapping in jsx


# ── Контракт JSX: веб-слой без изменений ─────────────────────────────────────


def test_queue_endpoint_keeps_expert_gate():
    """Матрица «не-эксперт»: GET /queue по-прежнему fail-closed за ролью
    ccks_expert и отвечает {records, count}."""
    endpoint = re.search(r'@router\.get\("/queue"\)(.*?)(?=@router\.|\Z)', _web(), re.DOTALL)
    assert endpoint, "не найден эндпоинт GET /queue"
    body = _norm(endpoint.group(1))
    assert _norm("authorization.require_role(user_id, authorization.ROLE_CCKS_EXPERT") in body
    assert _norm("repo.list_verification_queue(session=session)") in body
    assert _norm('return {"records": records, "count": len(records)}') in body


# ── Контракт CSS ─────────────────────────────────────────────────────────────


def test_card_and_modal_decision_blocks_have_styles():
    """Секция карточки и read-only блок модалки оформлены в стиле существующих
    (.lp-queue-reason / .lp-verdict-record), метка типа — бейдж с цветом."""
    css = _css()
    assert "margin-top: 14px" in _block(css, ".lp-queue-decisions")
    assert "font-size: 0.78rem" in _block(css, ".lp-queue-decisions h3")
    assert _block(css, ".lp-queue-decisions-list")
    assert _block(css, ".lp-queue-decision")
    assert _block(css, ".lp-queue-decision-type")
    for decision in ("vulnerability", "fraud_scheme", "not_confirmed"):
        assert _block(css, f".lp-decision-{decision}")
    assert "padding: 10px 12px" in _block(css, ".lp-verdict-decisions")
    assert _block(css, ".lp-verdict-decisions-title")
    assert _block(css, ".lp-verdict-decisions-list")
    assert _block(css, ".lp-verdict-decisions-empty")
    assert _block(css, ".lp-verdict-classifier-comment")
    # Карточка и модалка используют один и тот же элемент решения.
    jsx = _norm(_jsx())
    assert jsx.count(_norm('className="lp-queue-decision"')) == 2


# ── Greenplum-контракт миграции 068 ──────────────────────────────────────────


def test_migration_068_keeps_classifier_column_greenplum_safe():
    """GP6 (= PG 9.4): только ADD COLUMN IF NOT EXISTS, без PK/UNIQUE и без
    запрещённых конструкций (ON CONFLICT, gen_random_uuid, индексы)."""
    sql = MIGRATION_068.read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in sql.splitlines()).upper()

    assert "ALTER TABLE LOOPHOLE_RECORD" in body
    assert "ADD COLUMN IF NOT EXISTS CLASSIFIER_VERDICT_REASON TEXT" in body
    assert "PRIMARY KEY" not in body
    assert "UNIQUE" not in body
    assert "ON CONFLICT" not in body
    assert "GEN_RANDOM_UUID" not in body
    assert "CREATE INDEX" not in body
