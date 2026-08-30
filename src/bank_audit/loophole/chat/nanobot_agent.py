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
from ..config import LoopholeSettings, validate_nanobot_max_iterations
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
    max_iter = (
        settings.nanobot_max_iterations
        if max_iterations is None
        else validate_nanobot_max_iterations(max_iterations)
    )

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


def _collapse_type_arrays(node: Any) -> Any:
    """Схлопывает ``"type": [X, "null"]`` → ``"type": X`` рекурсивно.

    Gemini (OpenAI-совместимый эндпоинт) отклоняет массив в ``type``:
    ``Proto field is not repeating``. Встроенные tools nanobot
    (complete_goal, long_task) генерируют nullable-схемы с type-массивом.
    """
    if isinstance(node, dict):
        t = node.get("type")
        if isinstance(t, list):
            node["type"] = next((x for x in t if x != "null"), "string")
        for value in node.values():
            _collapse_type_arrays(value)
    elif isinstance(node, list):
        for value in node:
            _collapse_type_arrays(value)
    return node


def _patch_registry_for_gemini(registry: Any) -> None:
    """Оборачивает ``get_definitions`` санитизацией type-массивов."""
    original = registry.get_definitions

    def sanitized() -> list[dict]:
        return [_collapse_type_arrays(d) for d in original()]

    registry.get_definitions = sanitized


def create_nanobot(
    *,
    model: str | None = None,
    provider: str = "openai",
    temperature: float = 0.3,
    max_iterations: int | None = None,
    workspace: str | Path | None = None,
    extra_tools: tuple = (),
    tool_classes: tuple[type, ...] | None = None,
    tool_context: Any = None,
) -> Any:
    """Создаёт Nanobot, отключает встроенные tools, регистрирует кастомные.

    extra_tools — дополнительные классы tools (например, NANOBOT_HEAL_TOOLS
    для healer'а). Возвращает экземпляр `nanobot.Nanobot` и путь к временному
    config-файлу, который вызывающая сторона должна удалить по завершении.
    """
    from nanobot import Nanobot

    cfg = build_nanobot_config(
        model=model, provider=provider, temperature=temperature, max_iterations=max_iterations
    )
    fd, config_path = tempfile.mkstemp(suffix=".json")
    config_path_obj = Path(config_path)
    created = False
    try:
        os.close(fd)
        config_path_obj.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

        ws = workspace or (ROOT / "workspace" / "loophole" / "nanobot")
        ws = Path(ws).expanduser().resolve()
        ws.mkdir(parents=True, exist_ok=True)

        bot = Nanobot.from_config(config_path=config_path, workspace=str(ws))
        selected_tools = NANOBOT_TOOLS if tool_classes is None else tool_classes
        for tool_cls in (*selected_tools, *extra_tools):
            if tool_context is not None and getattr(tool_cls, "requires_context", False):
                bot._loop.tools.register(tool_cls(context=tool_context))
            else:
                bot._loop.tools.register(tool_cls())
        _patch_registry_for_gemini(bot._loop.tools)
        created = True
        return bot, config_path
    finally:
        if not created:
            config_path_obj.unlink(missing_ok=True)


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
