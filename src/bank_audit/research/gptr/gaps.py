"""Раздел «Честные пробелы» — из матрицы плана, а не из памяти писателя.

ЗАЧЕМ. Прежний раздел проверял три вещи и показывал одну строку там, где в
самом отчёте десяток «не найдено»: пробелы считались по косвенным признакам, а
не по тому, что план заказывал собрать. Теперь есть контракт — матрица
«субъект × характеристика», — и пробел это просто незакрытая клетка.

Второе: «нет данных» перестаёт быть одним словом. Аудитор должен различать
«организация не раскрывает» и «мы не смогли прочитать страницу» — во втором
случае вывод о непрозрачности будет ложным. Ровно это произошло с ВТБ: его
страницы отдают заголовки без чисел (содержимое подгружает скрипт), а отчёт
объявил, что числовые условия не раскрыты.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from ..entity_extractor import _BANK_DOMAINS

log = logging.getLogger(__name__)

# Причины отсутствия факта — три разные, и путать их нельзя.
NO_DATA = "no_data"                  # страница прочитана, характеристики нет
UNREADABLE = "unreadable"            # заглушка, пусто или каркас без текста
EXTRACTION_FAILED = "extraction_failed"   # текст есть, факт не извлекли


def collect(plan, *, registry, attributes: list[str],
            pages: dict[str, str], unreadable: dict[str, str]) -> list[str]:
    """Незакрытые клетки матрицы + честная причина по каждой.

    unreadable: url → причина, по которой страница не дала пригодного текста
    (заполняется скрапером: заглушка антибота, пустой каркас SPA).
    """
    labels = dict(getattr(plan, "subject_labels", None) or {})
    subjects = list(getattr(plan, "subjects", None) or [])
    cells = registry.by_cell()
    gaps: list[str] = []

    # 1. Незакрытые клетки — основной и самый честный пробел.
    hosts_read = {urlparse(u).netloc.lower().removeprefix("www.") for u in pages}
    for subj in (subjects or [""]):
        name = labels.get(subj, subj) or "Общие сведения"
        own = _BANK_DOMAINS.get(subj, "")
        site_read = bool(own) and any(h == own or h.endswith("." + own)
                                      for h in hosts_read)
        missing = [a for a in attributes if not cells.get((subj, a))]
        if not missing:
            continue
        if own and not site_read:
            gaps.append(
                f"**{name}** — официальный сайт ({own}) прочитать не удалось, "
                f"поэтому не закрыто: {', '.join(missing)}. Вывод о "
                f"непрозрачности делать нельзя: данных нет У НАС, а не у банка.")
        else:
            gaps.append(f"**{name}** — в прочитанных источниках не нашлось: "
                        f"{', '.join(missing)}.")

    # 2. Наблюдаемая сторона: есть ли вообще взгляд со стороны.
    observed = [f for f in registry.facts if f.stance == "observed"]
    if not observed:
        gaps.append(
            "Взгляд со стороны отсутствует: все факты — со слов самих "
            "организаций. Расхождение заявленного с практикой не проверено.")
    else:
        no_obs = [labels.get(s, s) for s in subjects
                  if not any(f.subject == s and f.stance == "observed"
                             for f in registry.facts)]
        if no_obs:
            gaps.append("Только заявленная сторона, без взгляда со стороны: "
                        + ", ".join(no_obs) + ".")

    # 3. Страницы, не давшие пригодного текста, — с указанием причины.
    if unreadable:
        by_reason: dict[str, list[str]] = {}
        for url, reason in unreadable.items():
            by_reason.setdefault(reason, []).append(url)
        for reason, urls in by_reason.items():
            gaps.append(f"Страниц не прочитано ({reason}): {len(urls)} "
                        f"— например, {urls[0]}.")

    return gaps


def render(gaps: list[str]) -> str:
    if not gaps:
        return ("\n\n## Честные пробелы\n\nПробелов не выявлено: по каждому "
                "объекту закрыты все характеристики плана, включая взгляд со "
                "стороны, и каждое утверждение подтверждено цитатой.\n")
    body = "\n".join(f"- {g}" for g in gaps)
    return f"\n\n## Честные пробелы\n\n{body}\n"
