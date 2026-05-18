"""Sravni.ru browser-адаптер: достаём Redux store после раскрытия списка.

Реальность sravni для категорий credit / mortgage / card / auto:
  • SSR (__NEXT_DATA__) отдаёт только top-10 групп банков
  • Полный список (totalCountGroup ~71 групп / totalCount ~203 предложения)
    живёт ТОЛЬКО в Redux store, расширяется кликами по «Показать ещё»
  • Никаких отдельных XHR endpoints для пагинации не существует —
    items уже в state, кнопка просто меняет видимость
  • После N кликов state.products.list.offers.items содержит все группы

Алгоритм:
  1. browser.fetch_redux_state открывает страницу, кликает «Показать ещё»
     пока она доступна, и достаёт Redux store через walk по React fiber.
  2. Извлекаем state.products.list.offers (items + totalCount) и
     state.organizations (id → name lookup).
  3. Сохраняем как envelope ``{"strategy": "redux_offers_list", ...}``.
  4. Парсер ``_parse_redux_offers_list`` в sravni_api делает OfferDraft.

Структура item:
  { id, name, organization (id), alias, ratePskFrom/To, minRate/maxRate,
    minSumFrom/maxSumTo, minTermFrom/maxTermTo, groupTotalCount }
"""
from __future__ import annotations
import json as _json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable

from .base import SourceAdapter, FetchResult
from .sravni_api import SravniApiAdapter, _CAT_MAP
from ..models import OfferDraft, RawSnapshot, FilterContext

log = logging.getLogger(__name__)


class SravniBrowserAdapter(SourceAdapter):
    name = "sravni_browser"

    def __init__(self, settings, raw_store, http=None, browser=None):
        super().__init__(settings, raw_store, http=http, browser=browser)
        self._parser = SravniApiAdapter(settings, raw_store, http=http, browser=browser)

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        if self.browser is None:
            raise RuntimeError("sravni_browser требует BrowserCollector (collector: browser)")

        category = target.get("category", "credit")
        cat_cfg  = _CAT_MAP.get(category)
        if cat_cfg is None:
            raise RuntimeError(f"sravni_browser: неизвестная категория {category}")

        fc = target.get("filter_context", {})
        url = cat_cfg["url_tpl"].format(
            amount=fc.get("amount", 500000),
            period=fc.get("period_months", 12),
            region=fc.get("region", "msk"),
        )

        max_clicks = int(target.get("max_loadmore_clicks", 30))
        # _resilient: до 3 попыток с jitter + headed fallback на последней
        status, state = self.browser.fetch_redux_state_resilient(
            url,
            max_clicks=max_clicks,
            workspace_dir=str(self.settings.workspace_dir) if self.settings else None,
            source=self.name, target=target.get("name"),
        )

        items: list = []
        organizations: list = []
        total = None
        envelope_strategy = "redux_offers_list"
        if isinstance(state, dict):
            offers = ((state.get("products") or {}).get("list") or {}).get("offers") or {}
            items = offers.get("items") or []
            total = offers.get("totalCount")

            # Fallback для категорий-организаций (брокеры/НПФ): данные не в
            # products.list.offers, а в state.organizations.organizationsList.items
            if not items:
                org_list = ((state.get("organizations") or {})
                            .get("organizationsList") or {}).get("items") or []
                if org_list:
                    items = org_list
                    total = ((state.get("organizations") or {})
                             .get("organizationsList") or {}).get("totalCount") or len(org_list)
                    envelope_strategy = "redux_organizations_list"
            # ОСНОВНОЕ место lookup — products.list.offers.organizations (dict by id)
            orgs_block = offers.get("organizations") or {}
            if isinstance(orgs_block, dict):
                organizations = list(orgs_block.values())
            elif isinstance(orgs_block, list):
                organizations = orgs_block
            # Fallback: state.organizations.data / state.dictionaries.organizations
            if not organizations:
                state_orgs = state.get("organizations") or {}
                cand = state_orgs.get("data") or state_orgs.get("items") or []
                if isinstance(cand, dict):
                    organizations = list(cand.values())
                elif isinstance(cand, list):
                    organizations = cand
            if not organizations:
                dicts = state.get("dictionaries") or {}
                cand = dicts.get("organizations") or dicts.get("banks") or []
                if isinstance(cand, dict):
                    organizations = list(cand.values())
                elif isinstance(cand, list):
                    organizations = cand
            # Inline-organization у item'а (mortgage кладёт object прямо в item.organization)
            for it in items:
                inline = it.get("organization")
                if isinstance(inline, dict) and inline.get("id"):
                    organizations.append(inline)

        log.info("sravni_browser %s/%s: redux items=%s total=%s orgs=%s",
                 category, target.get("name"), len(items), total, len(organizations))

        envelope = _json.dumps(
            {"strategy": envelope_strategy, "category": category,
             "items": items, "totalCount": total, "organizations": organizations},
            ensure_ascii=False, default=str,
        ).encode("utf-8")
        meta = {"url": url, "target": target["name"],
                "strategy": envelope_strategy,
                "items": len(items), "totalCount": total}

        path, digest, n = self.raw.write(
            self.name, target["name"], envelope, "json", meta=meta,
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=status,
            content_sha256=digest, storage_path=path, bytes=n,
            filter_context=FilterContext(**fc),
            category=category,
        )
        return FetchResult(snapshot=snap, html=envelope)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        yield from self._parser.parse_offers(html, target)
