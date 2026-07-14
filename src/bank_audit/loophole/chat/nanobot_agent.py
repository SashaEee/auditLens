"""Nanobot harness для loophole chat.

Создаёт и конфигурирует экземпляр `nanobot.Nanobot`, регистрирует кастомные
tools из `chat.tools_nanobot` и предоставляет helper'ы для формирования
system prompt.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ...config import ROOT
from ..config import LoopholeSettings
from .tools_nanobot import NANOBOT_TOOLS

log = logging.getLogger(__name__)

_SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompt" / "07_nanobot_system.md"


def load_system_prompt() -> str:
    """Загружает системный prompt для nanobot-агента."""
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def _default_provider_config() -> dict:
    """Конфигурация провайдера по переменным окружения проекта."""
    base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", ""))
    return {"apiBase": base_url, "apiKey": api_key}


def build_nanobot_config(
    *,
    model: str | None = None,
    provider: str = "openai",
    temperature: float = 0.3,
    max_iterations: int | None = None,
) -> dict:
    """Строит inline JSON-конфиг для `nanobot.Nanobot.from_config`."""
    settings = LoopholeSettings.load()
    effective_model = model or settings.effective_nanobot_model()
    max_iter = max_iterations or settings.nanobot_max_iterations

    return {
        "providers": {provider: _default_provider_config()},
        "agents": {
            "defaults": {
                "provider": provider,
                "model": effective_model,
                "temperature": temperature,
                "maxToolIterations": max_iter,
            }
        },
        "tools": {
            "web": {"enable": False},
            "exec": {"enable": False},
            "file": {"enable": False},
            "cliApps": {"enable": False},
            "my": {"enable": False},
            "imageGeneration": {"enable": False},
        },
    }


def create_nanobot(
    *,
    model: str | None = None,
    provider: str = "openai",
    temperature: float = 0.3,
    max_iterations: int | None = None,
    workspace: str | Path | None = None,
) -> Any:
    """Создаёт Nanobot, отключает встроенные tools, регистрирует кастомные.

    Возвращает экземпляр `nanobot.Nanobot` и путь к временному config-файлу,
    который вызывающая сторона должна удалить по завершении.
    """
    from nanobot import Nanobot

    cfg = build_nanobot_config(
        model=model, provider=provider, temperature=temperature, max_iterations=max_iterations
    )
    fd, config_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    config_path_obj = Path(config_path)
    config_path_obj.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    ws = workspace or (ROOT / "workspace" / "loophole" / "nanobot")
    ws = Path(ws).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)

    bot = Nanobot.from_config(config_path=config_path, workspace=str(ws))
    for tool_cls in NANOBOT_TOOLS:
        bot._loop.tools.register(tool_cls())

    return bot, config_path


def build_prompt(query: str, history: list[dict[str, str]] | None = None) -> str:
    """Формирует сообщение для nanobot: system prompt + history + query."""
    system = load_system_prompt()
    parts = [system]
    if history:
        for msg in history:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts.append(f"{role}: {content}")
    parts.append(f"user: {query}")
    return "\n\n".join(parts)
