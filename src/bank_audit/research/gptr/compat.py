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

_PARAMS = ("temperature", "top_p", "reasoning_effort",
           "presence_penalty", "frequency_penalty")
# «`temperature` is deprecated for this model», «Unsupported parameter: 'top_p'»
_REJECTED_RE = re.compile(
    r"(deprecated|not supported|unsupported|unknown|invalid)[^.]{0,40}?"
    r"[`'\"]?(?P<a>" + "|".join(_PARAMS) + r")[`'\"]?"
    r"|[`'\"]?(?P<b>" + "|".join(_PARAMS) + r")[`'\"]?[^.]{0,40}?"
    r"(deprecated|not supported|unsupported|unknown|invalid)",
    re.IGNORECASE)


def _rejected_param(err: Exception) -> str | None:
    """Какой параметр модель отвергла, если она вообще про это сказала."""
    m = _REJECTED_RE.search(str(err))
    if not m:
        return None
    return (m.group("a") or m.group("b") or "").lower() or None


def probe_models(models: list[str], *, base_url: str, api_key: str) -> None:
    """Разовая проверка: какие модели отвергают `temperature`.

    Самолечение по исключению не срабатывает: gpt-researcher ретраит вызов
    внутри себя десять раз и наружу отдаёт уже общую ошибку. Поэтому спрашиваем
    провайдера прямо — три коротких запроса на старте прогона, зато список
    строится по факту, а не по зашитым именам моделей.
    """
    import httpx
    import gpt_researcher.llm_provider.generic.base as base
    import gpt_researcher.utils.llm as llm

    for model in dict.fromkeys(m for m in models if m):
        if model in base.NO_SUPPORT_TEMPERATURE_MODELS:
            continue
        try:
            r = httpx.post(base_url.rstrip("/") + "/chat/completions", timeout=30,
                           headers={"Authorization": f"Bearer {api_key}"},
                           json={"model": model, "max_tokens": 1,
                                 "temperature": 0.4,
                                 "messages": [{"role": "user", "content": "."}]})
        except Exception as e:
            log.info("gptr-compat: проба %s не удалась (%s)", model, type(e).__name__)
            continue
        if r.status_code == 200:
            continue
        if _rejected_param(Exception(r.text)) == "temperature":
            base.NO_SUPPORT_TEMPERATURE_MODELS.append(model)
            log.info("gptr-compat: %s не принимает temperature", model)
    llm.NO_SUPPORT_TEMPERATURE_MODELS = base.NO_SUPPORT_TEMPERATURE_MODELS


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
