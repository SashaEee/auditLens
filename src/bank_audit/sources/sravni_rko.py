"""Расчётно-кассовое обслуживание — новая полка витрины «Рынок».

ЗАЧЕМ ОТДЕЛЬНЫЙ ИСТОЧНИК. РКО на banki.ru взять нельзя, и это проверено дважды
(11.08.2026): страница `/products/rko/` заявляет «201 тариф, 23 банка», но и по
HTTP, и ПОСЛЕ РЕНДЕРА браузером отдаёт одну и ту же таблицу из 10 строк — все
десять тарифы ВТБ. По-банковая страница Сбера не содержит НИ ОДНОГО его тарифа,
у Т-Банка — ровно один. Эквайринг там же вырождается в рекламный блок
«⚡ Комиссия: от 0%». Полка из такой выборки была бы ложью.

У sravni.ru тот же рынок отдаётся служебным API: POST /proxy-msb-rko/offers —
523 тарифа 136 банков, и Сбер в нём есть (23 тарифа «Модуль РКО» для ООО и ИП).
Ответ сгруппирован по организации: у каждой группы «головной» тариф и до
groupLimit вложенных в groupItems — разворачиваем и то, и другое.

ЕДИНИЦА ИЗМЕРЕНИЯ. fee_service здесь — рубли В МЕСЯЦ, а не в год, как у карт:
рынок РКО говорит месячной ценой пакета, и переводить её в годовую значило бы
показывать аудитору число, которого нет ни в одном тарифе. Подпись метрики
категории задана соответственно (categories.py → rko).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import httpx

from .base import SourceAdapter, FetchResult
from ..hashing import stable_digest
from ..models import OfferDraft, RawSnapshot

log = logging.getLogger(__name__)

API = "https://www.sravni.ru/proxy-msb-rko/offers"
PAGE = "https://www.sravni.ru/rko/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_LIMIT = 50
# у Сбера 23 тарифа РКО — при groupLimit=10 полка потеряла бы больше половины
_GROUP_LIMIT = 30
_MAX_OFFSET = 600          # 523 тарифа + запас; страховка от бесконечного цикла
_RE_TAGS = re.compile(r"<[^>]+>")


def _plain(s: Any, limit: int = 220) -> str | None:
    """Поля преимуществ приходят кусками HTML («<p>3%&nbsp;— за&nbsp;…»)."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = _RE_TAGS.sub(" ", s).replace("&nbsp;", " ").replace("&mdash;", "—")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit] or None


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _org_name(org: dict) -> str | None:
    name = (org or {}).get("name") or {}
    if isinstance(name, str):
        return name
    return name.get("short") or name.get("full")


_RE_ALIAS_COPY = re.compile(r"-\d+$")


def _base_alias(alias: Any) -> str | None:
    """«modul-rko-aaa-2» → «modul-rko-aaa»: региональная копия того же тарифа."""
    if not isinstance(alias, str) or not alias:
        return None
    return _RE_ALIAS_COPY.sub("", alias)


class SravniRkoAdapter(SourceAdapter):
    """РКО со sravni.ru. Один таргет = весь рынок (пагинация внутри)."""

    name = "sravni_rko"

    def _body(self, offset: int) -> dict:
        return {
            "limit": _LIMIT, "offset": offset,
            "group": ["organization"], "groupLimit": _GROUP_LIMIT,
            "sortProperty": "advertising.position", "sortDirection": "asc",
            "advertisingOnly": False, "isMix": False,
        }

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        """Обходим страницы API и складываем компактный JSON.

        Сырой ответ — 260 КБ на страницу и до 3 МБ на обход; хранить его целиком
        незачем (диск VM уже забивался под 100 проц.), поэтому в снимок кладём
        только те поля, из которых потом собирается оффер.
        """
        headers = {"User-Agent": _UA, "Accept": "application/json",
                   "Accept-Language": "ru-RU,ru;q=0.9",
                   "Content-Type": "application/json", "Referer": PAGE}
        rows: list[dict] = []
        seen: set[str] = set()
        total = None
        with httpx.Client(timeout=45, follow_redirects=True, headers=headers) as c:
            for offset in range(0, _MAX_OFFSET, _LIMIT):
                r = c.post(API, json=self._body(offset))
                if r.status_code != 200:
                    log.warning("sravni_rko: offset=%s → HTTP %s", offset, r.status_code)
                    break
                data = r.json()
                groups = data.get("items") or []
                if total is None:
                    total = data.get("totalCount")
                if not groups:
                    break
                for g in groups:
                    org = g.get("organization") or {}
                    bank = _org_name(org)
                    if not bank:
                        continue
                    # головной тариф группы + вложенные: у Сбера все 23 лежат
                    # в groupItems, и без них банка на полке просто не будет
                    for t in [g, *(g.get("groupItems") or [])]:
                        tid = str(t.get("id") or "")
                        if not tid or tid in seen:
                            continue
                        seen.add(tid)
                        rows.append({
                            "id": tid,
                            "bank": bank,
                            "bank_alias": org.get("alias"),
                            "name": t.get("name") or t.get("alias") or "Тариф",
                            "alias": t.get("alias"),
                            "price_month": t.get("rkoPriceRub"),
                            "price_open": t.get("priceRkoOpen"),
                            "price_year": t.get("priceYear"),
                            "org_types": t.get("organizationType") or [],
                            "cash_put_pct": t.get("comissMoneyPutPercent"),
                            "cash_take_pct": t.get("comissMoneyTakePercent"),
                            "adv_payment": _plain(t.get("advantagesPaymentTitle")),
                            "adv_transfer": _plain(t.get("advantagesTransferTitle")),
                            "adv_opening": _plain(t.get("advantagesOpeningTitle")),
                            "cashback_pct": t.get("cashbackCardPercent"),
                            "date_from": t.get("tariffDateFrom"),
                        })
                if len(groups) < _LIMIT:
                    break
        payload = json.dumps({"total": total, "count": len(rows), "offers": rows},
                             ensure_ascii=False).encode("utf-8")
        log.info("sravni_rko: тарифов %s (всего в источнике %s)", len(rows), total)
        path, digest, n = self.raw.write(self.name, target["name"], payload, "json",
                                         meta={"url": API, "target": target["name"]})
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=API,
            fetched_at=datetime.now(timezone.utc), http_status=200,
            content_sha256=digest, storage_path=path, bytes=n, category="rko",
        )
        return FetchResult(snapshot=snap, html=payload)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        data = json.loads(html.decode("utf-8"))
        for it in data.get("offers", []):
            price = it.get("price_month")
            conditions = " · ".join(x for x in (it.get("adv_payment"),
                                                it.get("adv_transfer"),
                                                it.get("adv_opening")) if x) or None
            yield OfferDraft(
                bank_name_raw=it["bank"],
                category="rko",
                # НЕ id источника: один и тот же тариф приходит несколькими
                # строками по регионам («Модуль РКО ААА» — трижды), и по id
                # витрина показала бы один банк тремя точками рынка
                # НЕ id и НЕ сырой алиас: один тариф приходит несколькими
                # строками по регионам, отличаясь только суффиксом
                # («modul-rko-aaa», «modul-rko-aaa-1», «modul-rko-aaa-2»), и
                # витрина показала бы восемь тарифов Сбера двадцатью четырьмя
                external_id="rko_" + stable_digest({
                    "b": it.get("bank_alias") or it["bank"],
                    "n": _base_alias(it.get("alias")) or it["name"],
                    "t": sorted(it.get("org_types") or []),
                })[:24],
                title=str(it["name"])[:200],
                url=(f"https://www.sravni.ru/rko/{it['bank_alias']}/"
                     if it.get("bank_alias") else PAGE),
                # ₽/МЕС, а не ₽/год: см. пояснение в шапке модуля
                fee_service=_dec(price),
                fee_open=_dec(it.get("price_open")),
                cashback_pct=_dec(it.get("cashback_pct")),
                conditions=conditions,
                raw={k: it.get(k) for k in
                     ("bank_alias", "alias", "price_month", "price_open", "price_year",
                      "org_types", "cash_put_pct", "cash_take_pct", "date_from")},
                # цена и лимиты живут только в raw — без этого история замрёт
                digest_extra={"p": it.get("price_month"), "o": it.get("price_open"),
                              "t": it.get("org_types"), "d": it.get("date_from")},
            )

    def parse_reviews(self, html: bytes, target: dict[str, Any]):
        return []
