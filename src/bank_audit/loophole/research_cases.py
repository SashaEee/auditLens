"""Изолированное хранилище источников и кандидатов AI-исследования."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from inspect import isawaitable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from ..hashing import sha256_text
from . import repository as repo
from .models import LoopholeRecord


@dataclass(frozen=True, slots=True)
class CaseContractV1:
    """Структурированный, но ещё не опубликованный кандидат исследования."""

    title: str
    evidence: str
    description: str
    category: str | None
    severity: str
    is_loophole: bool


class ResearchCaseService:
    """Сохраняет CaseContract v1 только в границах одного исследования."""

    def __init__(self, session) -> None:
        self._session = session

    def start_research(
        self,
        *,
        workspace_id: int,
        run_id: str,
        query: str,
        search_params: dict[str, Any],
    ) -> int:
        return self._session.execute(
            text(
                "INSERT INTO loophole_research (workspace_id, run_id, query_text, search_params) "
                "VALUES (:workspace_id, :run_id, :query, :search_params) RETURNING research_id"
            ),
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "query": query,
                "search_params": json.dumps(search_params, ensure_ascii=False),
            },
        ).scalar_one()

    def record_source(
        self, research_id: int, *, url: str, title: str | None, extracted_text: str | None
    ) -> int:
        return self._session.execute(
            text(
                "INSERT INTO loophole_research_source "
                "(research_id, url, title, extracted_text, status, limitation_message) "
                "VALUES (:research_id, :url, :title, :extracted_text, 'fetched', NULL) "
                "RETURNING source_id"
            ),
            {
                "research_id": research_id,
                "url": url,
                "title": title,
                "extracted_text": extracted_text,
            },
        ).scalar_one()

    def record_source_failure(
        self, research_id: int, *, url: str, limitation_message: str
    ) -> int:
        """Фиксирует недоступный источник без создания искусственного кандидата."""
        return self._session.execute(
            text(
                "INSERT INTO loophole_research_source "
                "(research_id, url, title, extracted_text, status, limitation_message) "
                "VALUES (:research_id, :url, NULL, NULL, 'unavailable', :limitation_message) "
                "RETURNING source_id"
            ),
            {
                "research_id": research_id,
                "url": url,
                "limitation_message": limitation_message,
            },
        ).scalar_one()

    def collect_research(
        self,
        *,
        workspace_id: int,
        run_id: str,
        query: str,
        max_results: int,
        search,
        fetch,
        extract,
    ) -> dict[str, Any]:
        """Выполняет поиск и извлечение, изолируя кандидатов одним research_id.

        Внешние вызовы передаются явно: это удерживает сетевую границу в tools,
        делает ход исследования воспроизводимым и позволяет продолжить работу,
        если один из источников недоступен.
        """
        research_id = self.start_research(
            workspace_id=workspace_id,
            run_id=run_id,
            query=query,
            search_params={"max_results": max_results},
        )
        candidate_ids: list[int] = []
        for result in search(query, max_results):
            url = str(result.get("url") or "")
            if not url:
                continue
            try:
                page = fetch(url)
            except Exception:  # noqa: BLE001 — недоступный URL не должен останавливать исследование
                page = None
            excerpt = str((page or {}).get("excerpt") or "")
            if not excerpt:
                self.record_source_failure(
                    research_id,
                    url=url,
                    limitation_message=f"Источник недоступен: {url}",
                )
                continue
            source_id = self.record_source(
                research_id,
                url=url,
                title=str((page or {}).get("title") or result.get("title") or "") or None,
                extracted_text=excerpt,
            )
            try:
                extracted_candidates = extract(excerpt)
            except Exception:  # noqa: BLE001 — ошибка извлечения фиксируется как ограничение
                self._session.execute(
                    text(
                        "UPDATE loophole_research_source "
                        "SET status = 'unavailable', limitation_message = :limitation_message "
                        "WHERE source_id = :source_id"
                    ),
                    {
                        "source_id": source_id,
                        "limitation_message": f"Не удалось извлечь текст источника: {url}",
                    },
                )
                continue
            for item in extracted_candidates:
                if not isinstance(item, dict):
                    continue
                candidate_id = self.add_candidate(
                    research_id,
                    source_id=source_id,
                    title=str(item.get("title") or ""),
                    evidence=str(item.get("evidence_quote") or item.get("description") or ""),
                    category=str(item.get("category") or "") or None,
                    description=str(item.get("description") or ""),
                    severity=str(item.get("severity") or "medium"),
                    is_loophole=bool(item.get("is_loophole", False)),
                )
                if candidate_id is not None:
                    candidate_ids.append(candidate_id)
        return {
            "research_id": research_id,
            "candidate_ids": candidate_ids,
            "candidate_count": len(candidate_ids),
            "limitations": self.list_limitations(research_id),
        }

    def add_candidate(
        self,
        research_id: int,
        *,
        source_id: int,
        title: str,
        evidence: str,
        category: str | None,
        description: str,
        severity: str,
        is_loophole: bool,
    ) -> int | None:
        """Добавляет кандидат лишь из успешно извлечённого источника этого же запуска."""
        contract = CaseContractV1(
            title=title,
            evidence=evidence,
            description=description,
            category=category,
            severity=severity,
            is_loophole=is_loophole,
        )
        return self._session.execute(
            text(
                "INSERT INTO loophole_research_candidate "
                "(research_id, source_id, title, evidence, category, description, severity, is_loophole) "
                "SELECT :research_id, source.source_id, :title, :evidence, :category, "
                ":description, :severity, :is_loophole "
                "FROM loophole_research_source AS source "
                "WHERE source.source_id = :source_id AND source.research_id = :research_id "
                "AND source.status = 'fetched' "
                "RETURNING candidate_id"
            ),
            {
                "research_id": research_id,
                "source_id": source_id,
                **asdict(contract),
            },
        ).scalar_one_or_none()

    def list_limitations(self, research_id: int) -> list[str]:
        """Возвращает понятные аналитiku ограничения источников текущего запуска."""
        rows = self._session.execute(
            text(
                "SELECT limitation_message FROM loophole_research_source "
                "WHERE research_id = :research_id AND status = 'unavailable' "
                "ORDER BY source_id"
            ),
            {"research_id": research_id},
        ).scalars()
        return [message for message in rows if message]

    async def classify_candidates(
        self,
        research_id: int,
        *,
        classifier=None,
        confirmed_examples: list[dict[str, Any]] | None = None,
        model_name: str | None = None,
        batch_size: int | None = None,
    ) -> dict[str, list[Any]]:
        """Сохраняет отдельную модельную оценку без изменения ручного решения.

        Ручной verdict и решение ЦК КС являются terminal-значениями: их
        присутствие исключает кандидат из выборки ещё до вызова модели.
        """
        from .config import LoopholeSettings

        settings = LoopholeSettings.load()
        effective_batch_size = batch_size or settings.research_classify_batch_size
        if effective_batch_size < 1:
            raise ValueError("Размер пакета классификации должен быть положительным")
        if confirmed_examples is None:
            from .kb import repository as kb_repository

            confirmed_examples = kb_repository.list_examples(session=self._session)
        if model_name is None:
            model_name = settings.effective_classify_model()
        if classifier is None:
            from .classify import classify_text
            from .pii_mask import mask as pii_mask

            async def classifier(candidate_text: str, *, examples: list[dict[str, Any]]):
                examples_text = "\n".join(
                    f"- {item.get('title', '')}: {item.get('description', '')}"
                    for item in examples[:10]
                )
                masked_text, _ = pii_mask(
                    "Подтверждённые примеры:\n"
                    f"{examples_text or 'нет'}\n\nКандидат:\n{candidate_text}"
                )
                return await classify_text(masked_text)

        rows = self._session.execute(
            text(
                "SELECT candidate_id, title, evidence, description FROM loophole_research_candidate "
                "WHERE research_id = :research_id AND manual_verdict IS NULL "
                "AND ccks_decision IS NULL ORDER BY candidate_id"
            ),
            {"research_id": research_id},
        ).mappings().all()
        classified_ids: list[int] = []
        failures: list[dict[str, Any]] = []
        for offset in range(0, len(rows), effective_batch_size):
            for row in rows[offset : offset + effective_batch_size]:
                candidate_id = int(row["candidate_id"])
                candidate_text = "\n".join(
                    str(row[field]) for field in ("title", "description", "evidence") if row[field]
                )
                try:
                    verdict = classifier(candidate_text, examples=confirmed_examples)
                    if isawaitable(verdict):
                        verdict = await verdict
                    if getattr(verdict, "reason", "") in {
                        "empty_response",
                        "parse_fail",
                        "not_dict",
                        "llm_error",
                    }:
                        raise ValueError(str(verdict.reason))
                    self._session.execute(
                        text(
                            "UPDATE loophole_research_candidate SET "
                            "model_is_loophole = :is_loophole, model_confidence = :confidence, "
                            "model_reason = :reason, model_name = :model_name, "
                            "model_classified_at = CURRENT_TIMESTAMP WHERE candidate_id = :candidate_id"
                        ),
                        {
                            "candidate_id": candidate_id,
                            "is_loophole": bool(verdict.is_loophole),
                            "confidence": float(verdict.confidence),
                            "reason": str(verdict.reason)[:500],
                            "model_name": model_name,
                        },
                    )
                    classified_ids.append(candidate_id)
                except Exception as exc:  # noqa: BLE001 — одна ошибка не отменяет остальные кейсы
                    failures.append({"candidate_id": candidate_id, "reason": str(exc)[:500]})
        return {"classified_ids": classified_ids, "failures": failures}

    def submit_for_verification(
        self,
        candidate_id: int,
        *,
        evidence_source_ids: list[int],
        submitted_by: str,
        correlation_run_id: str,
    ) -> dict[str, Any] | None:
        """Создаёт неизменяемый submitted snapshot выбранного draft-кейса.

        При отозванном или чужом evidence возвращает только ``None``: вызывающий
        слой может показать безопасное общее сообщение, не раскрывая метаданные.
        """
        if not evidence_source_ids or not submitted_by or not correlation_run_id:
            return None
        candidate = self._session.execute(
            text(
                "SELECT candidate.candidate_id, candidate.research_id, candidate.source_id, "
                "candidate.draft_version, candidate.title, candidate.evidence, "
                "candidate.description, candidate.category, candidate.severity, candidate.is_loophole, "
                "candidate.model_is_loophole, candidate.model_confidence, candidate.model_reason, "
                "candidate.model_name, research.workspace_id "
                "FROM loophole_research_candidate AS candidate "
                "JOIN loophole_research AS research ON research.research_id = candidate.research_id "
                "WHERE candidate.candidate_id = :candidate_id"
            ),
            {"candidate_id": candidate_id},
        ).mappings().one_or_none()
        if candidate is None or int(candidate["source_id"]) not in evidence_source_ids:
            return None
        existing = self._session.execute(
            text(
                "SELECT snapshot_id, candidate_id, research_id, workspace_id, draft_version, "
                "case_snapshot, evidence_snapshot, submitted_by, submitted_at, run_id, status "
                "FROM loophole_verification_snapshot WHERE candidate_id = :candidate_id "
                "AND draft_version = :draft_version AND status = 'submitted' "
                "ORDER BY snapshot_id LIMIT 1"
            ),
            {"candidate_id": candidate_id, "draft_version": candidate["draft_version"]},
        ).mappings().one_or_none()
        if existing is not None:
            return self._snapshot_to_dict(existing)

        placeholders = ", ".join(f":source_{index}" for index in range(len(evidence_source_ids)))
        source_params = {
            "research_id": candidate["research_id"],
            **{f"source_{index}": source_id for index, source_id in enumerate(evidence_source_ids)},
        }
        evidence_rows = self._session.execute(
            text(
                "SELECT source_id, revision, url, title, extracted_text FROM loophole_research_source "
                "WHERE research_id = :research_id AND access_status = 'active' "
                f"AND source_id IN ({placeholders}) ORDER BY source_id"
            ),
            source_params,
        ).mappings().all()
        if len(evidence_rows) != len(set(evidence_source_ids)):
            return None
        case_snapshot = {
            field: candidate[field]
            for field in (
                "candidate_id",
                "research_id",
                "source_id",
                "draft_version",
                "title",
                "evidence",
                "description",
                "category",
                "severity",
                "is_loophole",
                "model_is_loophole",
                "model_confidence",
                "model_reason",
                "model_name",
            )
        }
        evidence_snapshot = [dict(row) for row in evidence_rows]
        snapshot_id = self._session.execute(
            text(
                "INSERT INTO loophole_verification_snapshot "
                "(candidate_id, research_id, workspace_id, draft_version, case_snapshot, "
                "evidence_snapshot, submitted_by, run_id, status) "
                "VALUES (:candidate_id, :research_id, :workspace_id, :draft_version, :case_snapshot, "
                ":evidence_snapshot, :submitted_by, :run_id, 'submitted') RETURNING snapshot_id"
            ),
            {
                "candidate_id": candidate["candidate_id"],
                "research_id": candidate["research_id"],
                "workspace_id": candidate["workspace_id"],
                "draft_version": candidate["draft_version"],
                "case_snapshot": json.dumps(case_snapshot, ensure_ascii=False, default=str),
                "evidence_snapshot": json.dumps(evidence_snapshot, ensure_ascii=False, default=str),
                "submitted_by": submitted_by,
                "run_id": correlation_run_id,
            },
        ).scalar_one()
        inserted = self._session.execute(
            text(
                "SELECT snapshot_id, candidate_id, research_id, workspace_id, draft_version, "
                "case_snapshot, evidence_snapshot, submitted_by, submitted_at, run_id, status "
                "FROM loophole_verification_snapshot WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).mappings().one()
        return self._snapshot_to_dict(inserted)

    def candidate_workspace_id(self, candidate_id: int) -> int | None:
        """Возвращает только workspace_id для server-side проверки ownership."""
        return self._session.execute(
            text(
                "SELECT research.workspace_id FROM loophole_research_candidate AS candidate "
                "JOIN loophole_research AS research ON research.research_id = candidate.research_id "
                "WHERE candidate.candidate_id = :candidate_id"
            ),
            {"candidate_id": candidate_id},
        ).scalar_one_or_none()

    def research_workspace_id(self, research_id: int) -> int | None:
        """Возвращает workspace исследования, не раскрывая его источники."""
        return self._session.execute(
            text("SELECT workspace_id FROM loophole_research WHERE research_id = :research_id"),
            {"research_id": research_id},
        ).scalar_one_or_none()

    def research_id_for_run(self, *, workspace_id: int, run_id: str) -> int | None:
        """Связывает отчёт с research run только внутри того же workspace."""
        return self._session.execute(
            text(
                "SELECT research_id FROM loophole_research "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "ORDER BY research_id DESC LIMIT 1"
            ),
            {"workspace_id": workspace_id, "run_id": run_id},
        ).scalar_one_or_none()

    def import_preliminary_sources(self, research_id: int, *, imported_by: str) -> dict[str, Any]:
        """Явно переносит новые подозрительные источники в общий каталог.

        Исходный research source остаётся неизменяемым provenance. В каталог
        попадает только успешно прочитанная страница с кандидатной оценкой;
        повторный перенос source_id идемпотентно возвращается как skipped.
        """
        workspace_id = self.research_workspace_id(research_id)
        if workspace_id is None:
            return {"imported": 0, "skipped": 0, "record_ids": []}
        rows = self._session.execute(
            text(
                "SELECT source.source_id, source.url, source.title AS source_title, "
                "source.extracted_text, candidate.title AS candidate_title, "
                "MAX(COALESCE(candidate.model_confidence, 0.0)) AS confidence "
                "FROM loophole_research_source AS source "
                "JOIN loophole_research_candidate AS candidate "
                "ON candidate.source_id = source.source_id "
                "WHERE source.research_id = :research_id "
                "AND source.status = 'fetched' AND source.access_status = 'active' "
                "AND source.extracted_text IS NOT NULL AND source.extracted_text != '' "
                "AND (candidate.model_is_loophole = TRUE "
                "OR (candidate.model_is_loophole IS NULL AND candidate.is_loophole = TRUE)) "
                "GROUP BY source.source_id, source.url, source.title, source.extracted_text, candidate.title "
                "ORDER BY source.source_id"
            ),
            {"research_id": research_id},
        ).mappings().all()
        imported_ids: list[int] = []
        skipped = 0
        for row in rows:
            source_id = int(row["source_id"])
            already_imported = self._session.execute(
                text("SELECT 1 FROM loophole_preliminary_import WHERE source_id = :source_id"),
                {"source_id": source_id},
            ).scalar_one_or_none()
            content = str(row["extracted_text"])
            source_url = str(row["url"])
            source_sha = sha256_text(f"research-source:{source_url}\n{content}")
            if already_imported or repo.exists_url(source_url, session=self._session) or repo.exists_sha256(
                source_sha, session=self._session
            ):
                skipped += 1
                continue
            record_id = repo.insert_record(
                LoopholeRecord(
                    sha256=source_sha,
                    title=str(row["source_title"] or row["candidate_title"] or source_url),
                    url=source_url,
                    snippet=content[:1000],
                    raw_text=content,
                    content_status="full",
                    raw_text_len=len(content),
                    status="preliminary",
                    is_loophole=True,
                    verdict_confidence=float(row["confidence"]),
                    verdict_reason="Предварительная оценка из AI-исследования",
                    verdict_model="research_preliminary",
                ),
                session=self._session,
            )
            if record_id is None:
                skipped += 1
                continue
            self._session.execute(
                text(
                    "INSERT INTO loophole_preliminary_import "
                    "(research_id, source_id, workspace_id, record_id, imported_by) "
                    "VALUES (:research_id, :source_id, :workspace_id, :record_id, :imported_by)"
                ),
                {
                    "research_id": research_id,
                    "source_id": source_id,
                    "workspace_id": workspace_id,
                    "record_id": record_id,
                    "imported_by": imported_by,
                },
            )
            imported_ids.append(int(record_id))
        return {"imported": len(imported_ids), "skipped": skipped, "record_ids": imported_ids}

    def get_report_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """Возвращает канонические данные отчёта только из immutable snapshot.

        Mutable draft кандидата и текущие записи источников намеренно не читаются:
        экспорт обязан воспроизводить именно тот набор доказательств, который был
        передан на верификацию.
        """
        row = self._session.execute(
            text(
                "SELECT snapshot.snapshot_id, snapshot.workspace_id, snapshot.submitted_at, "
                "snapshot.case_snapshot, snapshot.evidence_snapshot, research.query_text "
                "FROM loophole_verification_snapshot AS snapshot "
                "JOIN loophole_research AS research ON research.research_id = snapshot.research_id "
                "WHERE snapshot.snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        case = json.loads(row["case_snapshot"])
        evidence = json.loads(row["evidence_snapshot"])
        return {
            "snapshot_id": row["snapshot_id"],
            "workspace_id": row["workspace_id"],
            "submitted_at": row["submitted_at"],
            "query": row["query_text"],
            "result": case.get("description") or case.get("evidence") or "",
            "case": case,
            "evidence": evidence if isinstance(evidence, list) else [],
        }

    def save_report_result(
        self,
        *,
        workspace_id: int,
        run_id: str,
        query: str,
        result: str,
    ) -> int:
        """Сохраняет результат текущего agent run без клиентских доказательств."""
        try:
            evidence_row = self._session.execute(
                text(
                    "SELECT snapshot.evidence_snapshot "
                    "FROM loophole_verification_snapshot AS snapshot "
                    "JOIN loophole_research AS research ON research.research_id = snapshot.research_id "
                    "WHERE research.workspace_id = :workspace_id AND research.run_id = :run_id "
                    "ORDER BY snapshot.snapshot_id DESC LIMIT 1"
                ),
                {"workspace_id": workspace_id, "run_id": run_id},
            ).mappings().one_or_none()
        except OperationalError:
            evidence_row = None
        evidence_snapshot = evidence_row["evidence_snapshot"] if evidence_row else "[]"
        report_id = self._session.execute(
            text(
                "INSERT INTO loophole_research_report "
                "(workspace_id, run_id, query_text, result_text, evidence_snapshot) "
                "VALUES (:workspace_id, :run_id, :query, :result, :evidence) "
                "RETURNING report_id"
            ),
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "query": query,
                "result": result,
                "evidence": evidence_snapshot,
            },
        ).scalar_one()
        return int(report_id)

    def get_report_result(self, report_id: int) -> dict[str, Any] | None:
        """Возвращает экспортируемый result текущего workspace/run, без latest fallback."""
        row = self._session.execute(
            text(
                "SELECT report_id, workspace_id, run_id, query_text, result_text, evidence_snapshot "
                "FROM loophole_research_report WHERE report_id = :report_id"
            ),
            {"report_id": report_id},
        ).mappings().one_or_none()
        if row is None:
            return None
        evidence = json.loads(row["evidence_snapshot"])
        return {
            "report_id": row["report_id"],
            "workspace_id": row["workspace_id"],
            "query": row["query_text"],
            "result": row["result_text"],
            "evidence": evidence if isinstance(evidence, list) else [],
        }

    def decide_snapshot(
        self,
        snapshot_id: int,
        *,
        decision: str,
        comment: str,
        decided_by: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Фиксирует единственное решение ЦК КС для submitted snapshot."""
        allowed = {"vulnerability", "fraud_scheme", "not_confirmed"}
        if decision not in allowed or not comment.strip() or not decided_by or not run_id:
            return None
        dialect_name = self._session.get_bind().dialect.name
        lock_clause = " FOR UPDATE" if dialect_name != "sqlite" else ""
        snapshot = self._session.execute(
            text(
                "SELECT snapshot_id, status FROM loophole_verification_snapshot "
                f"WHERE snapshot_id = :snapshot_id{lock_clause}"
            ),
            {"snapshot_id": snapshot_id},
        ).mappings().one_or_none()
        if snapshot is None:
            return None
        existing = self._session.execute(
            text(
                "SELECT decision_id, snapshot_id, decision, comment, decided_by, decided_at, run_id "
                "FROM loophole_verification_decision WHERE snapshot_id = :snapshot_id "
                "ORDER BY decision_id LIMIT 1"
            ),
            {"snapshot_id": snapshot_id},
        ).mappings().one_or_none()
        if existing is not None:
            return dict(existing)
        if snapshot["status"] != "submitted":
            return None
        decision_id = self._session.execute(
            text(
                "INSERT INTO loophole_verification_decision "
                "(snapshot_id, decision, comment, decided_by, run_id) "
                "VALUES (:snapshot_id, :decision, :comment, :decided_by, :run_id) "
                "RETURNING decision_id"
            ),
            {
                "snapshot_id": snapshot_id,
                "decision": decision,
                "comment": comment.strip(),
                "decided_by": decided_by,
                "run_id": run_id,
            },
        ).scalar_one()
        self._session.execute(
            text(
                "UPDATE loophole_verification_snapshot SET status = 'decided' "
                "WHERE snapshot_id = :snapshot_id"
            ),
            {"snapshot_id": snapshot_id},
        )
        return dict(
            self._session.execute(
                text(
                    "SELECT decision_id, snapshot_id, decision, comment, decided_by, decided_at, run_id "
                    "FROM loophole_verification_decision WHERE decision_id = :decision_id"
                ),
                {"decision_id": decision_id},
            ).mappings().one()
        )

    def publish_decision(self, decision_id: int, *, command_key: str) -> dict[str, Any] | None:
        """Публикует положительное решение в каталог ровно один раз."""
        if not command_key:
            return None
        existing = self._session.execute(
            text(
                "SELECT publication_id, decision_id, command_key, record_id, status, error_message "
                "FROM loophole_publication_mapping WHERE command_key = :command_key "
                "ORDER BY publication_id LIMIT 1"
            ),
            {"command_key": command_key},
        ).mappings().one_or_none()
        if existing is not None:
            return dict(existing)
        decision = self._session.execute(
            text(
                "SELECT decision.decision_id, decision.decision, snapshot.case_snapshot "
                "FROM loophole_verification_decision AS decision "
                "JOIN loophole_verification_snapshot AS snapshot ON snapshot.snapshot_id = decision.snapshot_id "
                "WHERE decision.decision_id = :decision_id"
            ),
            {"decision_id": decision_id},
        ).mappings().one_or_none()
        if decision is None or decision["decision"] not in {"vulnerability", "fraud_scheme"}:
            return None
        mapping_id = self._session.execute(
            text(
                "INSERT INTO loophole_publication_mapping "
                "(decision_id, command_key, status) VALUES (:decision_id, :command_key, 'publishing') "
                "RETURNING publication_id"
            ),
            {"decision_id": decision_id, "command_key": command_key},
        ).scalar_one()
        case = json.loads(decision["case_snapshot"])
        record_id = repo.insert_record(
            LoopholeRecord(
                sha256=sha256_text(f"publication:{decision_id}:{command_key}"),
                title=case["title"],
                url="",
                snippet=case["evidence"],
                raw_text=case["description"],
                status="published",
                is_loophole=True,
            ),
            session=self._session,
        )
        self._session.execute(
            text(
                "UPDATE loophole_publication_mapping SET record_id = :record_id, status = 'published', "
                "updated_at = CURRENT_TIMESTAMP WHERE publication_id = :publication_id"
            ),
            {"record_id": record_id, "publication_id": mapping_id},
        )
        return {
            "publication_id": mapping_id,
            "decision_id": decision_id,
            "command_key": command_key,
            "record_id": record_id,
            "status": "published",
            "error_message": None,
        }

    @staticmethod
    def _snapshot_to_dict(row: Any) -> dict[str, Any]:
        """Преобразует строку snapshot, не читая исходный mutable draft."""
        return {
            "snapshot_id": row["snapshot_id"],
            "candidate_id": row["candidate_id"],
            "research_id": row["research_id"],
            "workspace_id": row["workspace_id"],
            "draft_version": row["draft_version"],
            "case": json.loads(row["case_snapshot"]),
            "evidence": json.loads(row["evidence_snapshot"]),
            "submitted_by": row["submitted_by"],
            "submitted_at": row["submitted_at"],
            "run_id": row["run_id"],
            "status": row["status"],
        }

    def get_candidate(self, candidate_id: int) -> dict:
        row = self._session.execute(
            text(
                "SELECT candidate.candidate_id, candidate.research_id, candidate.title, "
                "candidate.evidence, candidate.description, candidate.category, candidate.severity, "
                "candidate.is_loophole, source.url AS source_url, research.search_params "
                "FROM loophole_research_candidate AS candidate "
                "JOIN loophole_research_source AS source ON source.source_id = candidate.source_id "
                "JOIN loophole_research AS research ON research.research_id = candidate.research_id "
                "WHERE candidate.candidate_id = :candidate_id"
            ),
            {"candidate_id": candidate_id},
        ).mappings().one()
        candidate = dict(row)
        candidate["is_loophole"] = bool(candidate["is_loophole"])
        candidate["search_params"] = json.loads(candidate["search_params"])
        return candidate
