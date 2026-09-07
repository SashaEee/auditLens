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
import os

from . import runstate

log = logging.getLogger(__name__)

# Сколько жалоб берём на объект. Было 6 — «чтобы не утопить контекст
# писателя»; с досье писатель раздела «Голос клиента» получает только
# наблюдаемые факты, и двадцать цитат на объект ему по силам. Шесть давали
# по Сберу две-три цитаты в отчёте при самом большом корпусе.
_PER_SUBJECT = 20


def _match_slug(bank_name: str, name_to_slug: dict[str, str]) -> str:
    """Корпус мог вернуть каноническое имя, отличное от переданного."""
    low = (bank_name or "").lower()
    for name, slug in name_to_slug.items():
        n = name.lower()
        if n and (n in low or low in n):
            return slug
    return ""


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
        name_to_slug = {labels.get(s, s): s for s in subjects}
        # Без ограничения по свежести корпус отдавал жалобы 2022 года при
        # наличии свежих: аудитор видел в отчёте обращение четырёхлетней
        # давности как актуальную практику. Полтора года — компромисс между
        # свежестью и наполнением по редким продуктам.
        found = br.search_reviews_multi(
            query, banks=list(name_to_slug), k_per=per_subject,
            since_days=int(os.getenv("GPTR_REVIEWS_SINCE_DAYS", "540")))
    except Exception as e:
        log.info("корпус отзывов: %s", type(e).__name__)
        return []

    out: list[dict] = []
    for bank_name, items in (found or {}).items():
        # Чей это отзыв — ЗНАЕТ корпус. Отдавать это на угадывание модели
        # нельзя: текст жалобы часто не называет банк («давний клиент банка»),
        # и жалоба на брокерские комиссии Т-Банка уехала в раздел про Сбер.
        slug = name_to_slug.get(bank_name) or _match_slug(bank_name, name_to_slug)
        for it in (items or []):
            text = (it.get("text") or "").strip()
            if not text:
                continue
            out.append({"bank": bank_name, "subject": slug or "",
                        "url": it.get("url") or "",
                        "date": str(it.get("date") or "")[:10],
                        "product": it.get("product") or "",
                        "text": text})
    # Дочерние компании и сервисы в корпусе не значатся банком: 184 отзыва
    # упоминают Домклик, и все они привязаны к материнскому банку. Объект
    # без единого отзыва по слагу ищем по названию — и оставляем только те
    # тексты, где оно действительно есть, иначе приедут жалобы на банк вообще.
    covered = {r["subject"] for r in out}
    for slug in subjects:
        label = (labels.get(slug) or "").strip()
        if slug in covered or not label:
            continue
        try:
            extra = br.search_reviews(" ".join(x for x in (label, query) if x),
                                      k=per_subject * 3,
                                      since_days=int(os.getenv("GPTR_REVIEWS_SINCE_DAYS", "540")))
        except Exception as e:
            log.info("корпус отзывов, поиск по названию «%s»: %s", label, type(e).__name__)
            continue
        added = 0
        for it in (extra or []):
            text = (it.get("text") or "").strip()
            if not text or label.lower() not in text.lower():
                continue
            out.append({"bank": label, "subject": slug,
                        "url": it.get("url") or "",
                        "date": str(it.get("date") or "")[:10],
                        "product": it.get("product") or "",
                        "text": text})
            added += 1
            if added >= per_subject:
                break
        if added:
            log.info("корпус отзывов: «%s» не банк в корпусе, по названию нашлось %d", label, added)
    log.info("корпус отзывов: %d жалоб по %d объектам", len(out), len(subjects))
    return out


# Метаданные отзыва держим ОТДЕЛЬНО от его текста: когда я подставлял их в
# начало страницы, модель цитировала мою же служебную строку («Отзыв клиента о
# банке Т-Банк от 2025-12-24») как слова клиента. Живут они в состоянии
# прогона, а не в модуле — иначе текут в соседний вопрос.


def as_pages(records: list[dict]) -> dict[str, str]:
    """Отзывы в виде «страниц» для общего конвейера извлечения.

    Так у корпуса и веба один путь: одинаковое извлечение, одинаковая проверка
    цитаты, одинаковые якоря. В текст попадает ТОЛЬКО то, что написал клиент.
    """
    pages: dict[str, str] = {}
    meta = runstate.current().review_meta
    for r in records:
        url = r.get("url") or ""
        if not url:
            continue
        pages[url] = r["text"]
        meta[url] = {"bank": r.get("bank", ""), "date": r.get("date", ""),
                     "product": r.get("product", ""),
                     "subject": r.get("subject", "")}
    return pages


def check_alive(urls: list[str], timeout: float = 6.0,
                workers: int = 8) -> set[str]:
    """Какие из ссылок НЕ открываются. Возвращает множество мёртвых.

    Проверяем только процитированные ссылки — их единицы, и делаем это в
    отдельном потоке, чтобы не морозить поток событий. Ошибка сети не
    объявляет ссылку мёртвой: недоступность нашего контура и отсутствие
    страницы — разные вещи, и обвинять источник без основания нельзя.
    """
    import concurrent.futures as cf

    import httpx

    from ...rag.fetcher import CA_BUNDLE_PATH, DEFAULT_HEADERS

    def one(u: str) -> tuple[str, bool]:
        try:
            with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True,
                              verify=CA_BUNDLE_PATH or True,
                              timeout=timeout) as c:
                r = c.get(u)
            return u, r.status_code in (404, 410)
        except Exception:      # noqa: BLE001 — сеть молчит, источник не виноват
            return u, False

    dead: set[str] = set()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for u, is_dead in ex.map(one, urls):
            if is_dead:
                dead.add(u)
    if dead:
        log.info("отзывы: недоступных ссылок %d из %d", len(dead), len(urls))
    return dead


def subject_hints() -> dict[str, str]:
    """url → слаг объекта, о котором отзыв. Правда корпуса, не догадка модели."""
    return {u: m["subject"]
            for u, m in runstate.current().review_meta.items()
            if m.get("subject")}


def stamp_dates(registry) -> int:
    """Проставляет фактам дату источника.

    Дата нужна аудитору, чтобы отличить свежую жалобу от прошлогодней, но
    брать её из текста нельзя — там её нет. Для отзывов берём из метаданных
    корпуса, для веб-страниц — из метаданных разметки, которые скрапер снял
    при чтении. Без этого модель не может сказать «условие действовало на
    такую-то дату» и все свидетельства выглядят одновременными.
    """
    n = 0
    state = runstate.current()
    meta_all = state.review_meta
    for f in registry.facts:
        if f.date:
            continue
        meta = meta_all.get(f.url)
        when = (meta or {}).get("date") or state.page_dates.get(f.url)
        if when:
            f.date = str(when)[:10]
            n += 1
    return n
