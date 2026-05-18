"""Banki.ru Rating API — публичный AJAX endpoint, без браузера.
   Возвращает агрегированные рейтинги всех банков: средняя оценка, кол-во отзывов,
   доля решённых, место в рейтинге. Работает через обычный HTTP."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
import httpx
from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, ReviewDraft, RawSnapshot, FilterContext
from ..hashing import sha256_bytes, stable_digest

# API возвращает 290 банков за один запрос
API_URL = "https://www.banki.ru/services/responses/ajax/?count=300&page=1"

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
        url = target.get("url", API_URL)
        r = self.http.client.get(
            url,
            headers={
                "Accept": "application/json",
                "Referer": "https://www.banki.ru/services/responses/",
            },
        )
        content = r.content
        path, digest, n = self.raw.write(self.name, target["name"], content, "json",
                                          meta={"url": url, "target": target["name"]})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=r.status_code,
            content_sha256=digest, storage_path=path, bytes=n,
        )
        return FetchResult(snapshot=snap, html=content)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        """Превращаем рейтинг каждого банка в условный 'offer' типа bank_rating,
           чтобы хранить историю: avg_grade, total_reviews, solved_pct, place."""
        data = json.loads(html.decode("utf-8"))
        for r in data.get("ratings", []):
            bank_name = (r.get("company") or {}).get("name", "") if isinstance(r.get("company"), dict) else ""
            if not bank_name:
                continue
            avg_grade = r.get("middleGrade")
            total = r.get("responseCount", 0)
            solved = r.get("solvedResponseCount", 0)
            solved_pct = round(solved / total * 100, 2) if total else 0
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
                    "total_reviews": total,
                    "total_reviews_year": r.get("responseCountForYear"),
                    "solved_reviews": solved,
                    "solved_pct": solved_pct,
                    "bank_answers": r.get("bankAnswerCount"),
                    "place": r.get("place"),
                    "rating_score": r.get("rating"),
                    "products": r.get("products", {}),
                    "date": r.get("date"),
                },
            )
