"""Конфиг модуля loophole (env: LOOPHOLE_*)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ROOT


@dataclass
class LoopholeSettings:
    collect_cron: str = "06:00"
    max_results_per_keyword: int = 10
    classify_model: str = ""
    chat_model: str = ""
    nanobot_model: str = ""
    nanobot_max_iterations: int = 20
    trust_min: float = 0.5
    workspace_dir: Path = field(default_factory=lambda: ROOT / "workspace" / "loophole")

    @classmethod
    def load(cls) -> "LoopholeSettings":
        ws_env = os.getenv("LOOPHOLE_WORKSPACE_DIR")
        return cls(
            collect_cron=os.getenv("LOOPHOLE_COLLECT_CRON", "06:00"),
            max_results_per_keyword=int(os.getenv("LOOPHOLE_MAX_RESULTS_PER_KEYWORD", "10")),
            classify_model=os.getenv("LOOPHOLE_CLASSIFY_MODEL", ""),
            chat_model=os.getenv("LOOPHOLE_CHAT_MODEL", ""),
            nanobot_model=os.getenv("LOOPHOLE_NANOBOT_MODEL", ""),
            nanobot_max_iterations=int(os.getenv("LOOPHOLE_NANOBOT_MAX_ITERATIONS", "20")),
            trust_min=float(os.getenv("LOOPHOLE_TRUST_MIN", "0.5")),
            workspace_dir=Path(ws_env).resolve() if ws_env else (ROOT / "workspace" / "loophole"),
        )

    def effective_classify_model(self) -> str:
        return (
            self.classify_model
            or os.getenv("LLM_MODEL_SMART")
            or os.getenv("LLM_MODEL_NAME", "gpt-4o")
        )

    def effective_chat_model(self) -> str:
        return (
            self.chat_model
            or os.getenv("LLM_MODEL_FAST")
            or os.getenv("LLM_MODEL_NAME", "gpt-4o")
        )

    def effective_nanobot_model(self) -> str:
        return (
            self.nanobot_model
            or os.getenv("LLM_MODEL_FAST")
            or os.getenv("LLM_MODEL_NAME", "gpt-4o")
        )


# Стартовые ключевые слова (категория 'seed').
SEED_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("лазейка в кредитном договоре", "cbr"),
    ("скрытые комиссии банк", "cbr"),
    ("условия вклада нарушены", "cbr"),
    ("отказ в выдаче вклада", "forum"),
    ("комиссия за досрочное погашение", "cbr"),
    ("изменение ставки по вкладу", "cbr"),
    ("навязанная страховка кредита", "forum"),
    ("штраф за закрытие счёта", "forum"),
)
