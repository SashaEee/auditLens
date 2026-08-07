"""Banki.ru Rating API — публичный AJAX endpoint, без браузера.

Народный рейтинг всех банков: балл, место, средняя оценка, объём отзывов,
доля решённых обращений. Работает через обычный HTTP.

СЕМАНТИКА ПОЛЕЙ API (разобрана 07.08.2026 на живой выдаче — до этого читалась
неверно, и витрина «Банки» показывала числа в разы больше правды):
  responseCount              — ВСЕ обращения, включая непроверенные и оценки по
                               продуктам; у Альфа-Банка 1 121 396. Это НЕ отзывы:
                               сходится с суммой typeOfPerson (юрлица+физлица).
  checkedResponseCount       — ПРОВЕРЕННЫЕ (опубликованные) отзывы, 232 611 —
                               именно это народный рейтинг показывает как отзывы.
  checkedResponseCountForYear— проверенные за год.
  countableNegativeCount     — засчитанные негативные обращения (знаменатель
  resolutionCountableNegativeCount — из них решённых (числитель) доли решённых.
                               Официальная методика: 16 453 / 39 025 = 42.2 проц.
                               Прежняя формула solvedResponseCount/responseCount
                               давала 1.5 проц. — заниженно в 25 раз.
  rating                     — балл народного рейтинга (0-100), place — место.

Пагинация: страница отдаёт максимум 50 банков независимо от count (проверено
07.08.2026), всего около 292 банков на 6 страницах. Раньше собиралась одна
страница — витрина знала рейтинг лишь для 50 банков из справочника.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, RawSnapshot
from ..hashing import sha256_bytes

log = logging.getLogger(__name__)

API_TMPL = "https://www.banki.ru/services/responses/ajax/?count=300&page={page}"
_MAX_PAGES = 12          # предохранитель: реально страниц около 6

# Маппинг product-id banki.ru -> наша категория (частичный — полного нет публично)
PRODUCT_CATEGORY_MAP = {
    "91": "deposit",       # вклады
    "93": "credit",        # кредиты
    "94": "card_credit",   # кредитные карты
    "68": "card_debit",    # дебетовые карты
    "23": "mortgage",      # ипотека
    "38": "auto_loan",     # автокредит
    "65": "metals",        # металлы
}


class BankiRatingsAdapter(SourceAdapter):
    """HTTP-адаптер для получения рейтингов банков с banki.ru.
       Без браузера, без капчи — чистый JSON."""
    name = "banki_ratings"

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        """Собирает ВСЕ страницы рейтинга и склеивает в один документ.
        Пустая/сбойная страница останавливает обход — уже собранное не теряем."""
        base = target.get("url", API_TMPL)
        if "{page}" not in base:                    # старый таргет с page=1 в URL
            base = API_TMPL
        ratings: list[dict] = []
        pages, status = 0, 200
        for page in range(1, _MAX_PAGES + 1):
            url = base.format(page=page)
            try:
                r = self.http.client.get(url, headers={
                    "Accept": "application/json",
                    "Referer": "https://www.banki.ru/services/responses/"})
                status = r.status_code
                if r.status_code != 200:
                    log.warning("banki_ratings: страница %d — http %d", page, r.status_code)
                    break
                chunk = (json.loads(r.content.decode("utf-8")) or {}).get("ratings") or []
            except Exception as e:  # noqa: BLE001 — частичный сбор лучше пустого
                log.warning("banki_ratings: страница %d не прочитана: %s", page, e)
                break
            if not chunk:
                break
            ratings.extend(chunk)
            pages = page
            if len(chunk) < 10:                     # последняя страница — короткая
                break
        content = json.dumps({"ratings": ratings, "pages": pages},
                             ensure_ascii=False).encode("utf-8")
        log.info("banki_ratings: собрано %d банков со %d страниц", len(ratings), pages)
        path, digest, n = self.raw.write(self.name, target["name"], content, "json",
                                         meta={"url": base, "pages": pages,
                                               "target": target["name"]})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=base.format(page=1),
            fetched_at=datetime.now(timezone.utc), http_status=status,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=content)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        """Рейтинг каждого банка — условный 'offer' типа bank_rating: так
        сохраняется история (SCD2 в product_terms)."""
        data = json.loads(html.decode("utf-8"))
        for r in data.get("ratings", []):
            company = r.get("company")
            bank_name = (company or {}).get("name", "") if isinstance(company, dict) else ""
            if not bank_name:
                continue
            avg_grade = r.get("middleGrade")
            checked = int(r.get("checkedResponseCount") or 0)
            all_resp = int(r.get("responseCount") or 0)
            # доля решённых — по методике народного рейтинга (см. шапку модуля)
            neg = int(r.get("countableNegativeCount") or 0)
            neg_solved = int(r.get("resolutionCountableNegativeCount") or 0)
            solved_pct = round(neg_solved / neg * 100, 2) if neg else None
            ext_id = f"banki_rating_{r.get('bankId', r.get('id'))}"
            yield OfferDraft(
                bank_name_raw=bank_name,
                category="other",
                external_id=ext_id,
                title=f"Рейтинг на banki.ru — {bank_name}",
                url=f"https://www.banki.ru/services/responses/bank/{bank_name.lower().replace(' ','-')}/",
                rate_pct=Decimal(str(round(avg_grade, 4))) if avg_grade else None,
                rate_kind="avg_grade",
                raw={
                    "banki_bank_id": r.get("bankId"),
                    # проверенные отзывы — то, что видит человек на сайте
                    "total_reviews": checked,
                    "total_reviews_year": int(r.get("checkedResponseCountForYear") or 0),
                    # все обращения (включая непроверенные) — для справки
                    "responses_all": all_resp,
                    "responses_all_year": int(r.get("responseCountForYear") or 0),
                    "solved_reviews": neg_solved,
                    "negative_countable": neg,
                    "solved_pct": solved_pct,
                    "bank_answers": r.get("bankAnswerCount"),
                    "place": r.get("place"),
                    "rating_score": r.get("rating"),
                    "problem_count": r.get("problemCount"),
                    "products": r.get("products", {}),
                    "date": r.get("date"),
                },
                # без этого новая версия рождалась только при смене средней
                # оценки, и число отзывов/место/доля решённых замирали месяцами
                digest_extra={"reviews": checked, "solved": solved_pct,
                              "place": r.get("place"), "score": r.get("rating")},
            )
