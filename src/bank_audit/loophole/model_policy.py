"""Локальные параметры короткого ответа для проверенных моделей Loophole."""
from __future__ import annotations


def short_response_extra_body(model: str) -> dict | None:
    """Qwen3.6-35B-A3B поддерживает явный режим без thinking; другие модели не меняем."""
    if model.lower().rsplit("/", 1)[-1] == "qwen3.6-35b-a3b":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return None
