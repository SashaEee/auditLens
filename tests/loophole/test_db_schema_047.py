"""Контракт хранения model verdict в изолированном research-контексте."""
from __future__ import annotations

from bank_audit.config import ROOT


def test_migration_047_adds_separate_traceable_model_verdict():
    sql = (ROOT / "migrations" / "047_loophole_research_classification.sql").read_text(encoding="utf-8")
    body = "\n".join(line.split("--")[0] for line in sql.splitlines()).upper()

    for column in (
        "MODEL_IS_LOOPHOLE BOOLEAN",
        "MODEL_CONFIDENCE DOUBLE PRECISION",
        "MODEL_REASON TEXT",
        "MODEL_NAME TEXT",
        "MODEL_CLASSIFIED_AT TIMESTAMPTZ",
    ):
        assert column in body
    assert "LOOPHOLE_RECORD" not in body
    assert "PRIMARY KEY" not in body
    assert "UNIQUE (" not in body and "UNIQUE(" not in body
