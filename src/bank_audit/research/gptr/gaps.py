"""Раздел «Честные пробелы» — по данным прогона, а не по мнению модели.

Отчёт gpt-researcher выглядит одинаково уверенно и когда источники прочитаны,
и когда по субъекту не нашлось ничего: писатель заполняет структуру тем, что
есть, и умолчание о недостающем ничем не отличается от утверждения. Аудитору
нужно обратное — явный список того, чего добыть не удалось.

Поэтому раздел собирается детерминированно: субъект без единой прочитанной
страницы, источник, вернувший заглушку, число из отчёта без подтверждения в
источниках. Модель тут ничего не решает, поэтому и соврать не может.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..entity_extractor import _BANK_DOMAINS

log = logging.getLogger(__name__)

_TOO_SHORT = 400        # столько же, сколько у скрапера считается пустой


def collect(plan, pages: dict[str, str], verification: dict) -> list[str]:
    """Возвращает строки-пробелы; пустой список — пробелов нет."""
    gaps: list[str] = []
    labels = dict(getattr(plan, "subject_labels", None) or {})
    subjects = list(getattr(plan, "subjects", None) or [])

    hosts = {urlparse(u).netloc.removeprefix("www.") for u in pages}
    for slug in subjects:
        name = labels.get(slug, slug)
        dom = _BANK_DOMAINS.get(slug)
        if dom and not any(h.endswith(dom) for h in hosts):
            gaps.append(
                f"**{name}** — официальный сайт ({dom}) прочитать не удалось; "
                f"сведения по нему взяты из сторонних источников либо отсутствуют.")

    thin = [u for u, t in pages.items() if len(t) < _TOO_SHORT]
    if thin:
        gaps.append(
            f"Страниц, отдавших пустой или защищённый ответ: {len(thin)} "
            f"(например, {', '.join(sorted(thin)[:2])}).")

    unverified = list(verification.get("unverified") or [])
    if unverified:
        shown = ", ".join(_fmt(v) for v in unverified[:8])
        gaps.append(
            f"Чисел в отчёте без подтверждения в прочитанных источниках: "
            f"{len(unverified)} из {verification.get('numeric_checked', 0)} "
            f"({shown}). Их следует перепроверить вручную перед использованием.")
    return gaps


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def render(gaps: list[str]) -> str:
    """Готовый markdown-раздел; при отсутствии пробелов — честная строка об этом."""
    if not gaps:
        return ("\n\n## Честные пробелы\n\nПробелов не выявлено: по каждому "
                "субъекту прочитан официальный источник, все числа отчёта "
                "подтверждены собранными страницами.\n")
    body = "\n".join(f"- {g}" for g in gaps)
    return f"\n\n## Честные пробелы\n\n{body}\n"
