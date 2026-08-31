"""Наблюдаемая сторона из собственного корпуса отзывов.

ЗАЧЕМ. Взгляд со стороны я искал в вебе — и в отчёт приезжали SEO-обзоры
(«Битва дебетовок 2026»), а не люди. При этом у платформы есть корпус
banki.ru: ~390 тысяч негативных отзывов за 2025-2026 по 217 банкам, с датами,
ссылками и готовыми эмбеддингами bge-m3. Отзыв клиента оттуда — это дословная
цитата с датой и ссылкой, то есть ровно то, чего не хватало отчёту.

Веб при этом не отменяется: он нужен для объектов вне корпуса и для сторонних
разборов. Корпус лишь становится первым и главным источником наблюдаемой
стороны — так же, как сайт организации является главным источником заявленной,
а cbr.ru — нормативной.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Сколько жалоб берём на объект: достаточно, чтобы увидеть повторяющиеся темы,
# и не столько, чтобы утопить контекст писателя.
_PER_SUBJECT = 6


def collect(plan, contract, *, per_subject: int = _PER_SUBJECT) -> list[dict]:
    """Жалобы по каждому объекту исследования из корпуса.

    Возвращает сырые записи {bank, product, date, url, text} — превращать их в
    факты будет слой фактов, чтобы правила у всех источников были одни.
    """
    subjects = list(getattr(plan, "subjects", None) or [])
    labels = dict(getattr(plan, "subject_labels", None) or {})
    if not subjects:
        return []
    # Запрос — характеристика наблюдаемой стороны из контракта: она уже
    # сформулирована планом под конкретный вопрос («жалобы на задержки и
    # расхождения со сроками»), никаких слов в коде не нужно.
    # Запрос: характеристика наблюдаемой стороны ПЛЮС предмет вопроса. Без
    # предмета корпус отдаёт жалобы про ипотеку и блокировки по 115-ФЗ — они
    # настоящие, но к вопросу про оформление карты отношения не имеют.
    observed = (getattr(contract, "observed", "") or "").strip()
    product = (getattr(plan, "product", "") or "").strip()
    query = " ".join(x for x in (observed, product) if x) or None
    try:
        from ...rag import bankiru_reviews as br
        if not br.is_available():
            log.info("корпус отзывов недоступен — наблюдаемая сторона только из веба")
            return []
        found = br.search_reviews_multi(
            query, banks=[labels.get(s, s) for s in subjects],
            k_per=per_subject)
    except Exception as e:
        log.info("корпус отзывов: %s", type(e).__name__)
        return []

    out: list[dict] = []
    for bank_name, items in (found or {}).items():
        for it in (items or []):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            out.append({"bank": bank_name, "url": it.get("url") or "",
                        "date": str(it.get("date") or "")[:10],
                        "product": it.get("product") or "",
                        "text": text})
    log.info("корпус отзывов: %d жалоб по %d объектам", len(out), len(subjects))
    return out


# url → {bank, date, product}. Метаданные держим ОТДЕЛЬНО от текста отзыва:
# когда я подставлял их в начало страницы, модель цитировала мою же служебную
# строку («Отзыв клиента о банке Т-Банк от 2025-12-24») как слова клиента.
META: dict[str, dict] = {}


def as_pages(records: list[dict]) -> dict[str, str]:
    """Отзывы в виде «страниц» для общего конвейера извлечения.

    Так у корпуса и веба один путь: одинаковое извлечение, одинаковая проверка
    цитаты, одинаковые якоря. В текст попадает ТОЛЬКО то, что написал клиент.
    """
    pages: dict[str, str] = {}
    META.clear()
    for r in records:
        url = r.get("url") or ""
        if not url:
            continue
        pages[url] = r["text"]
        META[url] = {"bank": r.get("bank", ""), "date": r.get("date", ""),
                     "product": r.get("product", "")}
    return pages


def stamp_dates(registry) -> int:
    """Проставляет фактам из корпуса дату отзыва.

    Дата нужна аудитору, чтобы отличить свежую жалобу от прошлогодней, но
    брать её из текста нельзя — там её нет. Берём из метаданных корпуса.
    """
    n = 0
    for f in registry.facts:
        meta = META.get(f.url)
        if meta and meta.get("date") and not f.date:
            f.date = meta["date"]
            n += 1
    return n
