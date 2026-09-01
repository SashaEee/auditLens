"""Совместимость gpt-researcher с нашим провайдером моделей.

gpt-researcher рассчитан на прямой OpenAI и держит СПИСОК моделей, которым
нельзя передавать `temperature`, — списком имён моделей OpenAI. У нас за одним
эндпоинтом десятки моделей разных вендоров, и состав их причуд меняется без
предупреждения: на замере 31.08.2026 `temperature` отвергал claude-opus-4.8, а
DeepSeek-V4 принимал — то есть любой зашитый список устареет.

Поэтому здесь не список, а самолечение: первый отказ по параметру запоминается
для этой модели, вызов повторяется без него, и дальше параметр не посылается.
Тот же приём, что в ai/llm_utils.py для reasoning_content.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Распознавание отвергнутого параметра переехало в ai/llm_utils: им теперь
# пользуется и Кондуктор, а он лежит слоем ниже и на gptr ссылаться не может.
from ...ai.llm_utils import _rejected_param  # noqa: E402,F401


def install() -> None:
    """Оборачивает вызов модели самолечением по отвергнутым параметрам."""
    import gpt_researcher.llm_provider.generic.base as base
    import gpt_researcher.utils.llm as llm

    if getattr(llm, "_auditlens_patched", False):
        return

    original = llm.create_chat_completion

    async def create_chat_completion(*args, **kwargs):
        model = kwargs.get("model") or (args[1] if len(args) > 1 else None)
        try:
            return await original(*args, **kwargs)
        except Exception as e:
            param = _rejected_param(e)
            if not param or not model:
                raise
            if param == "temperature":
                if model not in base.NO_SUPPORT_TEMPERATURE_MODELS:
                    base.NO_SUPPORT_TEMPERATURE_MODELS.append(model)
                    llm.NO_SUPPORT_TEMPERATURE_MODELS = \
                        base.NO_SUPPORT_TEMPERATURE_MODELS
            elif param == "reasoning_effort":
                if model in base.SUPPORT_REASONING_EFFORT_MODELS:
                    base.SUPPORT_REASONING_EFFORT_MODELS.remove(model)
                    llm.SUPPORT_REASONING_EFFORT_MODELS = \
                        base.SUPPORT_REASONING_EFFORT_MODELS
            else:
                raise
            log.info("gptr-compat: %s не принимает %s — запомнили, повторяем",
                     model, param)
            return await original(*args, **kwargs)

    llm.create_chat_completion = create_chat_completion
    llm._auditlens_patched = True

    # Модули, которые импортировали функцию по имени до нашей правки.
    for mod_name in ("gpt_researcher.actions.query_processing",
                     "gpt_researcher.actions.report_generation",
                     "gpt_researcher.skills.context_manager",
                     "gpt_researcher.skills.curator"):
        try:
            mod = __import__(mod_name, fromlist=["x"])
        except Exception:
            continue
        if hasattr(mod, "create_chat_completion"):
            mod.create_chat_completion = create_chat_completion
