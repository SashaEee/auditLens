"""Sravni.ru API адаптер — внутренние JSON-эндпоинты + SSR __NEXT_DATA__.

Стратегии по категориям:
  • deposits  → POST /proxy-deposits/deposits        (полная JSON-витрина, пагинация по pageIndex)
  • credit    → GET HTML + parse __NEXT_DATA__ ?page=N (полная пагинация SSR)
  • mortgage  → GET HTML + parse __NEXT_DATA__ ?page=N
  • card_credit / auto_loan → аналогично

Без Playwright и без капч. Чистый HTTP. Поддержка многостраничного забора.
Содержимое сохраняется в RAW-store как JSON-конверт {"strategy", "pages": [...]} —
парсер итерирует страницы и склеивает офферы.
"""
from __future__ import annotations
import json, re, time, logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

import httpx

from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, RawSnapshot, FilterContext
from ..hashing import sha256_bytes, stable_digest

log = logging.getLogger(__name__)

# ── Константы ──────────────────────────────────────────────────────────────────

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

_NEXT_DATA_RE = re.compile(
    r'<script\s[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_CAT_MAP = {
    # ── Розница: вклады/кредиты/ипотека/карты/авто ─────────────────────────
    "deposit": {
        "url_tpl": "https://www.sravni.ru/vklady/?amount={amount}&period={period}&region={region}",
        "strategy": "deposits_api",
        "referer":  "https://www.sravni.ru/vklady/",
    },
    "credit": {
        "url_tpl": "https://www.sravni.ru/kredity/?amount={amount}&period={period}&region={region}",
        "strategy": "ssr_vitrins",
        "ssr_path": "products.list.offers",
    },
    "mortgage": {
        # Подтверждено: products.list.offers (228 items на конец 2025)
        "url_tpl": "https://www.sravni.ru/ipoteka/?cost={amount}&initialPayment=1500000&period={period}&region={region}",
        "strategy": "ssr_vitrins",
        "ssr_path": "products.list.offers",
    },
    "card_credit": {
        "url_tpl": "https://www.sravni.ru/karty/?region={region}",
        "strategy": "ssr_vitrins",
        "ssr_path": "products.list.offers",
    },
    "card_debit": {
        # Та же витрина /karty/ — отфильтруем по item.type в парсере
        "url_tpl": "https://www.sravni.ru/karty/?region={region}",
        "strategy": "ssr_vitrins",
        "ssr_path": "products.list.offers",
    },
    "auto_loan": {
        "url_tpl": "https://www.sravni.ru/avtokredity/?amount={amount}&period={period}&region={region}",
        "strategy": "ssr_vitrins",
        "ssr_path": "products.list.offers",
    },

    # ── Микрозаймы: /zaimy/ → credits.lists.list ──────────────────────────
    "microloan": {
        "url_tpl": "https://www.sravni.ru/zaimy/?region={region}",
        "strategy": "ssr_credits_list",
        "ssr_path": "credits.lists.list",
    },

    # ── Инвестиции: брокеры/НПФ → organizations.organizationsList ────────
    "invest_broker": {
        "url_tpl": "https://www.sravni.ru/brokery/?region={region}",
        "strategy": "ssr_organizations",
        "ssr_path": "organizations.organizationsList",
    },
    "npf": {
        "url_tpl": "https://www.sravni.ru/npf/?region={region}",
        "strategy": "ssr_organizations",
        "ssr_path": "organizations.organizationsList",
    },
}

# ── Утилиты ────────────────────────────────────────────────────────────────────

def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _dec(v: Any) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _get_nested(d: dict, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(key)
    return d


def _iso_duration_to_days(v: Any) -> int | None:
    """'P5D' / 'P1Y' / 'P3M' / 'P30D' → days. Возвращает None если не распарсилось."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    m = re.fullmatch(r'P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?', s)
    if not m:
        try:
            return int(s)
        except ValueError:
            return None
    y, mo, w, d = (int(x) if x else 0 for x in m.groups())
    return y * 365 + mo * 30 + w * 7 + d


def _parse_rate_display(display: str) -> Decimal | None:
    if not display:
        return None
    m = re.search(r"(\d{1,2}[.,]\d{1,2}|\d{1,2})\s*%", display)
    if not m:
        return None
    return _dec(m.group(1).replace(",", "."))


def _add_query(url: str, key: str, value: Any) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{key}={value}"


class SravniApiAdapter(SourceAdapter):
    """HTTP-адаптер для sravni.ru с полной пагинацией."""

    name = "sravni_api"

    _client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None or self._client.is_closed:
            self._client = httpx.Client(
                headers=_BROWSER_HEADERS,
                follow_redirects=True,
                timeout=30,
            )
        return self._client

    # ── Основные методы ────────────────────────────────────────────────────────

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        category = target.get("category", "deposit")
        cat_cfg  = _CAT_MAP.get(category, _CAT_MAP["deposit"])
        fc       = target.get("filter_context", {})

        base_url = cat_cfg["url_tpl"].format(
            amount=fc.get("amount", 100000),
            period=fc.get("period_months", 12),
            region=fc.get("region", "msk"),
        )

        client   = self._get_client()
        strategy = cat_cfg["strategy"]
        max_pages = int(target.get("max_pages", 30))
        page_size = int(target.get("page_size", 50))

        pages: list[Any] = []
        first_status = 0

        if strategy == "deposits_api":
            # seed для cookie
            client.get(base_url, headers={"Accept": "text/html,*/*", **_BROWSER_HEADERS})
            api_url = "https://www.sravni.ru/proxy-deposits/deposits"

            # API игнорирует offset, но честно отдаёт сколько просили в limit.
            # Один запрос с большим limit → весь рынок (на 2025 ~983 вклада).
            limit = max(int(page_size) or 1000, 2000)
            time.sleep(self.http.delay_s)
            resp = client.post(
                api_url,
                json={
                    "amount":     fc.get("amount", 100000),
                    "regionUrls": [fc.get("region", "msk")],
                    "currency":   "RUB",
                    "sortKey":    "rate",
                    "sortAsc":    False,
                    "limit":      limit,
                    "offset":     0,
                },
                headers={
                    "Content-Type": "application/json",
                    "Accept":       "application/json, */*",
                    "Referer":      cat_cfg["referer"],
                    **_BROWSER_HEADERS,
                },
            )
            first_status = resp.status_code
            if resp.status_code not in (200, 201):
                raise RuntimeError(f"sravni_api deposits: HTTP {resp.status_code}")
            data = resp.json()
            items = data.get("data") or []
            total = data.get("total") or data.get("totalCount") or 0
            pages.append(data)
            log.info("sravni_api deposits: %s items (total=%s)", len(items), total)
        else:
            # SSR HTML. ?page=N на sravni.ru НЕ работает (всегда top-N), поэтому
            # в общем случае хватит одной страницы. max_pages>1 оставлено только
            # как страховка — если sravni когда-нибудь починит SSR-пагинацию,
            # код продолжит идти пока не получит повторяющийся набор.
            prev_ids: list = []
            for page_idx in range(1, max_pages + 1):
                page_url = base_url if page_idx == 1 else _add_query(base_url, "page", page_idx)
                time.sleep(self.http.delay_s)
                resp = client.get(page_url, headers={"Accept": "text/html,*/*", **_BROWSER_HEADERS})
                if page_idx == 1:
                    first_status = resp.status_code
                if resp.status_code not in (200, 201):
                    log.warning("sravni_api SSR page %s → HTTP %s", page_idx, resp.status_code)
                    break
                items, total = self._extract_ssr_page_meta(resp.content, category)
                cur_ids = [it.get("id") or it.get("_id") or it.get("alias") for it in items]
                pages.append(resp.text)
                log.info("sravni_api SSR %s page %s/%s: %s items (total=%s)",
                         category, page_idx, max_pages, len(items), total)
                if not items:
                    break
                # sravni возвращает один и тот же top-N — определяем это и выходим
                if cur_ids == prev_ids:
                    log.info("sravni_api SSR %s: page repeats prev — stop (sravni не пагинирует SSR)", category)
                    break
                prev_ids = cur_ids
                if total:
                    collected = sum(
                        len(self._extract_ssr_page_meta(
                            p.encode() if isinstance(p, str) else p, category)[0])
                        for p in pages
                    )
                    if collected >= int(total):
                        break
                if total is None or total == 0:
                    break

        envelope = json.dumps(
            {"strategy": strategy, "category": category, "pages": pages},
            ensure_ascii=False, default=str,
        ).encode("utf-8")

        path, digest, n = self.raw.write(
            self.name, target["name"], envelope, "json",
            meta={"url": base_url, "target": target["name"], "strategy": strategy,
                  "pages_collected": len(pages)},
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=base_url,
            fetched_at=datetime.now(timezone.utc), http_status=first_status or 200,
            content_sha256=digest, storage_path=path, bytes=n,
            filter_context=FilterContext(**fc),
            category=category,
        )
        return FetchResult(snapshot=snap, html=envelope)

    # ── Парсинг ────────────────────────────────────────────────────────────────

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        category = target.get("category", "deposit")
        try:
            envelope = json.loads(html)
        except Exception:
            log.warning("sravni_api: envelope JSON parse failed")
            return

        if not isinstance(envelope, dict):
            log.warning("sravni_api: bad envelope structure")
            return

        strategy = envelope.get("strategy") or _CAT_MAP.get(category, _CAT_MAP["deposit"])["strategy"]

        # Новый envelope от sravni_browser: items + organizations напрямую
        if strategy == "redux_offers_list":
            seen: set[str] = set()
            for d in self._parse_redux_offers_list(envelope, target):
                if d.external_id in seen:
                    continue
                seen.add(d.external_id)
                yield d
            return

        if strategy == "redux_organizations_list":
            seen2: set[str] = set()
            for d in self._parse_redux_organizations_list(envelope, target):
                if d.external_id in seen2:
                    continue
                seen2.add(d.external_id)
                yield d
            return

        if "pages" not in envelope:
            log.warning("sravni_api: envelope missing 'pages'")
            return

        seen: set[str] = set()
        for page in envelope.get("pages") or []:
            if strategy in ("deposits_api", "captured_xhr"):
                # Перехваченный XHR имеет ту же схему {"data":[{bankDetail,product},...]}
                # что и /proxy-deposits/deposits — переиспользуем парсер.
                drafts = self._parse_deposits_api(page, target)
            else:
                raw = page.encode("utf-8") if isinstance(page, str) else page
                if strategy == "ssr_organizations":
                    drafts = self._parse_ssr_organizations(raw, target)
                elif strategy == "ssr_credits_list":
                    drafts = self._parse_ssr_credits_list(raw, target)
                else:
                    drafts = self._parse_ssr_vitrins(raw, target)
            for d in drafts:
                if d.external_id in seen:
                    continue
                seen.add(d.external_id)
                yield d

    # ── Helpers для метаданных SSR страницы ────────────────────────────────────

    def _extract_ssr_page_meta(self, raw: bytes, category: str) -> tuple[list, int | None]:
        """Возвращает (items, totalCount|None). None — если поля нет (одна страница)."""
        text = raw.decode("utf-8", errors="ignore")
        m = _NEXT_DATA_RE.search(text)
        if not m:
            return [], None
        try:
            nd = json.loads(m.group(1))
        except Exception:
            return [], None
        state    = nd.get("props", {}).get("initialReduxState", {})
        cat_cfg  = _CAT_MAP.get(category, _CAT_MAP["credit"])
        ssr_path = cat_cfg.get("ssr_path", "products.list.offers")
        block    = _get_nested(state, ssr_path) or {}
        # block может быть list (organizations.organizationsList) или dict
        if isinstance(block, list):
            return block, None
        items    = block.get("items") or block.get("list") or []
        total_raw = block.get("totalCount") if "totalCount" in block else block.get("total")
        total = int(total_raw) if total_raw is not None else None
        return items, total

    # ── Парсеры ────────────────────────────────────────────────────────────────

    def _parse_deposits_api(self, data: Any, target: dict) -> Iterable[OfferDraft]:
        if isinstance(data, (bytes, str)):
            try:
                data = json.loads(data)
            except Exception as e:
                log.warning("sravni_api deposits JSON parse error: %s", e)
                return

        category = target.get("category", "deposit")
        fc       = target.get("filter_context", {})

        for item in (data.get("data") if isinstance(data, dict) else None) or []:
            bank = item.get("bankDetail") or {}
            prod = item.get("product") or {}
            details = prod.get("details") or []

            rate_str = next(
                (d.get("displayValue", "") for d in details if d.get("type") == "rate"),
                None,
            )
            rate = _parse_rate_display(rate_str)

            bank_name = bank.get("bankName") or bank.get("bankFullName") or "?"
            bank_slug = bank.get("alias") or ""
            is_sber   = "sberbank" in bank_slug.lower() or "sber" in bank_slug.lower()

            # ВАЖНО: product.id (UUID) уникален per offer; depositType одинаков
            # для всех вкладов, поэтому без id офферы одного банка сворачивались.
            prod_uid = prod.get("id") or prod.get("_id") or prod.get("alias")
            ext_id = stable_digest({
                "bank": bank_name, "alias": bank_slug,
                "category": category, "currency": "RUB",
                "product_id":   prod_uid,
                "product_name": prod.get("name") or prod.get("depositType") or "",
            })[:32]

            card_text = " ".join(d.get("label", "") + " " + d.get("displayValue", "")
                                 for d in details).lower()
            early_w = "досроч" in card_text
            capital = "капитал" in card_text
            replen  = "пополн" in card_text

            # Срок: из filter_context (мы запрашивали именно этот период) +
            # из product.minTerm / maxTerm если есть. Также сумма max — из
            # prod.maxAmount если задана, иначе fc.amount как min.
            period_q  = _to_int(fc.get("period_months"))
            term_min  = _to_int(prod.get("minTerm")) or period_q
            term_max  = _to_int(prod.get("maxTerm")) or period_q
            amount_min_v = _dec(prod.get("minAmount")) or _dec(fc.get("amount"))
            amount_max_v = _dec(prod.get("maxAmount"))

            # Title: depositType — это ENUM ('classic','grow','deal',...) — слабый title.
            # Берём product.name (рыночное имя «Доходный», «СберВклад») с fallback на depositType.
            title = (prod.get("name") or prod.get("depositType") or "Вклад").strip() or "Вклад"

            yield OfferDraft(
                bank_name_raw=bank_name,
                category=category,
                external_id=ext_id,
                title=title,
                url=f"https://www.sravni.ru/bank/{bank_slug}/vklady/",
                rate_pct=rate,
                rate_kind="effective",
                currency="RUB",
                amount_min=amount_min_v,
                amount_max=amount_max_v,
                term_months_min=term_min,
                term_months_max=term_max,
                early_withdraw=early_w or None,
                capitalization=capital or None,
                replenishable=replen or None,
                raw={
                    "bank_alias": bank_slug,
                    "is_sber": is_sber,
                    "details": details,
                    "deposit_type": prod.get("depositType"),
                    "filter_context": fc,
                },
            )

    # Маппинг категория → имена полей в Redux item.
    # Sravni использует разные имена для одного и того же смысла:
    #   credit  →  minSumFrom/maxSumTo, minTermFrom/maxTermTo
    #   mortgage→  minSum/maxSum,       minTerm/maxTerm
    #   auto    →  calcMinSum/calcMaxSum, без срока
    #   card    →  limitFrom/limitTo,   без срока, rate=ratePskPurchaseFrom
    _REDUX_FIELD_MAP = {
        "credit":      {"sum_min": ["minSumFrom"],         "sum_max": ["maxSumTo"],
                        "term_min":["minTermFrom"],        "term_max":["maxTermTo"],
                        "rate":    ["minRate", "ratePskFrom"]},
        "auto_loan":   {"sum_min": ["calcMinSum","minSum"],"sum_max": ["calcMaxSum","maxSum"],
                        "term_min":["calcMinTerm","minTerm"],"term_max":["calcMaxTerm","maxTerm"],
                        "rate":    ["minRate","ratePskFrom"]},
        "mortgage":    {"sum_min": ["minSum","calcMinSum"],"sum_max": ["maxSum","calcMaxSum"],
                        "term_min":["minTerm","calcMinTerm"],"term_max":["maxTerm","calcMaxTerm"],
                        "rate":    ["minRate","minPsk"]},
        "card_credit": {"sum_min": ["limitFrom"],          "sum_max": ["limitTo"],
                        "term_min":[], "term_max":[],
                        "rate":    ["ratePskPurchaseFrom","minRate"]},
        "card_debit":  {"sum_min": ["limitFrom"],          "sum_max": ["limitTo"],
                        "term_min":[], "term_max":[],
                        "rate":    ["ratePskPurchaseFrom","minRate"]},
        # default — credit-style
    }

    def _parse_redux_offers_list(self, envelope: dict, target: dict) -> Iterable[OfferDraft]:
        """Парсит Redux store sravni_browser. Учитывает что schema item'а
        отличается от категории к категории (см. _REDUX_FIELD_MAP).

        Если organizations в envelope пуст и item.organization — inline dict
        (как у mortgage), берём его прямо из item.
        """
        items = envelope.get("items") or []
        orgs  = envelope.get("organizations") or []
        category = target.get("category") or envelope.get("category") or "credit"
        fc       = target.get("filter_context", {})

        fmap = self._REDUX_FIELD_MAP.get(category, self._REDUX_FIELD_MAP["credit"])

        # Lookup organization id → display info (от envelope.organizations
        # — это dict.values() из state.products.list.offers.organizations)
        org_by_id: dict[str, dict] = {}
        for o in orgs:
            if not isinstance(o, dict):
                continue
            oid = o.get("id") or o.get("_id") or o.get("organization")
            if oid:
                org_by_id[str(oid)] = o

        for item in items:
            if not isinstance(item, dict):
                continue

            # ── Organization: id-string или inline-dict (mortgage кладёт целый объект)
            org_field = item.get("organization") or item.get("organizationId")
            if isinstance(org_field, dict):
                org = org_field
                org_id = org.get("id") or org.get("_id") or ""
            else:
                org_id = str(org_field or "")
                org = org_by_id.get(org_id, {})

            # Имя банка: name может быть string ("Т-Банк") или dict {short, ...}
            nm = org.get("name")
            if isinstance(nm, str):
                bank_name = nm
            elif isinstance(nm, dict):
                bank_name = nm.get("short") or nm.get("full") or nm.get("genitive")
            else:
                bank_name = None
            bank_name = (bank_name or org.get("title")
                         or org.get("alias") or item.get("alias") or "?")
            bank_slug = (org.get("alias") or org.get("slug")
                         or item.get("alias") or "").lower()
            is_sber   = "sber" in bank_slug

            # ── Числовые поля по карте полей
            def _first(keys: list[str], cast):
                for k in keys:
                    v = item.get(k)
                    if v is not None and v != "":
                        out = cast(v)
                        if out is not None:
                            return out
                return None

            rate     = _first(fmap.get("rate", []),     _dec)
            sum_min  = _first(fmap.get("sum_min", []),  _dec)
            sum_max  = _first(fmap.get("sum_max", []),  _dec)
            term_min = _first(fmap.get("term_min", []), _to_int)
            term_max = _first(fmap.get("term_max", []), _to_int)

            rate_kind = "min" if rate is not None else None

            ext_id = stable_digest({
                "src":      "sravni_browser",
                "category": category,
                "id":       item.get("id"),
                "alias":    item.get("alias"),
                "org":      org_id,
                "name":     item.get("name"),
            })[:32]

            url = (f"https://www.sravni.ru/bank/{bank_slug}/{item.get('alias')}/"
                   if bank_slug and item.get("alias")
                   else f"https://www.sravni.ru/{category}/")

            yield OfferDraft(
                bank_name_raw=bank_name,
                category=category,
                external_id=ext_id,
                title=item.get("name") or item.get("alias") or category,
                url=url,
                rate_pct=rate,
                rate_kind=rate_kind,
                currency="RUB",
                amount_min=sum_min,
                amount_max=sum_max,
                term_months_min=term_min,
                term_months_max=term_max,
                raw={
                    "bank_alias":        bank_slug,
                    "is_sber":           is_sber,
                    "rate_psk_from":     item.get("ratePskFrom") or item.get("ratePskPurchaseFrom") or item.get("minPsk"),
                    "rate_psk_to":       item.get("ratePskTo")   or item.get("ratePskPurchaseTo")   or item.get("maxPsk"),
                    "rate_min":          item.get("minRate"),
                    "rate_max":          item.get("maxRate"),
                    "group_total_count": item.get("groupTotalCount"),
                    "unit_from":         item.get("unitFrom"),
                    "unit_to":           item.get("unitTo"),
                    "credit_benefits":   item.get("creditBenefits"),
                    "credit_purposes":   item.get("creditPurposes"),
                    "interest_free_days":item.get("interestFreePeriodPurchase"),
                    "maintenance_price": item.get("maintenancePrice"),
                    "filter_context":    fc,
                },
            )

    def _parse_redux_organizations_list(self, envelope: dict, target: dict) -> Iterable[OfferDraft]:
        """Парсит state.organizations.organizationsList.items (брокеры/НПФ).

        Item shape: { id, alias, name (str), fullName, license, ratings,
                      branchesCount, type, status, position }
        """
        items = envelope.get("items") or []
        category = target.get("category") or envelope.get("category") or "invest_broker"

        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, dict):
                name = name.get("short") or name.get("full")
            full = item.get("fullName")
            bank_name = name or full or item.get("alias") or "?"
            slug = (item.get("alias") or "").lower()
            is_sber = "sber" in slug

            # Рейтинг — для брокеров это middle grade или из ratings
            ratings = item.get("ratings") or {}
            rating_v = (ratings.get("middleGrade") if isinstance(ratings, dict)
                        else None) or item.get("middleGrade")
            rating = _dec(rating_v) if rating_v is not None else None

            ext_id = stable_digest({
                "src":      "sravni_browser_org",
                "category": category,
                "id":       item.get("id") or item.get("_id"),
                "alias":    slug,
                "name":     bank_name,
            })[:32]

            yield OfferDraft(
                bank_name_raw=bank_name,
                category=category,
                external_id=ext_id,
                title=name or full or category,
                url=f"https://www.sravni.ru/{('brokery' if category=='invest_broker' else 'npf')}/{slug}/" if slug else "",
                rate_pct=rating,
                rate_kind="rating" if rating is not None else None,
                currency="RUB",
                raw={
                    "alias":            slug,
                    "is_sber":          is_sber,
                    "full_name":        full,
                    "license":          item.get("license"),
                    "branches_count":   item.get("branchesCount"),
                    "branches_count_region": item.get("branchesCountRegion"),
                    "registration_date": item.get("registrationDate"),
                    "position":         item.get("position"),
                    "type":             item.get("type"),
                    "status":           item.get("status"),
                    "filter_context":   target.get("filter_context", {}),
                },
            )

    def _parse_ssr_organizations(self, raw: bytes, target: dict) -> Iterable[OfferDraft]:
        """Брокеры/НПФ: organizations.organizationsList — список организаций."""
        text = raw.decode("utf-8", errors="ignore")
        m = _NEXT_DATA_RE.search(text)
        if not m:
            return
        try:
            nd = json.loads(m.group(1))
        except Exception:
            return

        category = target.get("category", "invest_broker")
        state    = nd.get("props", {}).get("initialReduxState", {})
        cat_cfg  = _CAT_MAP.get(category, _CAT_MAP["invest_broker"])
        ssr_path = cat_cfg.get("ssr_path", "organizations.organizationsList")

        block = _get_nested(state, ssr_path) or []
        items = block if isinstance(block, list) else (block.get("items") or block.get("list") or [])

        for item in items:
            name = item.get("name") if isinstance(item.get("name"), str) else (
                (item.get("name") or {}).get("short") if isinstance(item.get("name"), dict) else None
            )
            name = name or item.get("title") or item.get("alias") or "?"
            slug = item.get("alias") or item.get("slug") or ""
            ext_id = stable_digest({
                "src": "sravni_org", "category": category,
                "alias": slug, "name": name, "id": item.get("id"),
            })[:32]
            rating = item.get("rating") or item.get("ratingScore") or item.get("middleGrade")
            yield OfferDraft(
                bank_name_raw=name,
                category=category,
                external_id=ext_id,
                title=name,
                url=f"https://www.sravni.ru/{ 'brokery' if category=='invest_broker' else 'npf'}/{slug}/" if slug else None,
                rate_pct=_dec(rating),
                rate_kind="org_rating",
                raw={
                    "alias": slug,
                    "id": item.get("id"),
                    "license": item.get("license"),
                    "assets": item.get("assets"),
                    "category": category,
                },
            )

    def _parse_ssr_credits_list(self, raw: bytes, target: dict) -> Iterable[OfferDraft]:
        """Микрозаймы: credits.lists.list — список кредитов МФО."""
        text = raw.decode("utf-8", errors="ignore")
        m = _NEXT_DATA_RE.search(text)
        if not m:
            return
        try:
            nd = json.loads(m.group(1))
        except Exception:
            return

        state = nd.get("props", {}).get("initialReduxState", {})
        cat_cfg = _CAT_MAP.get("microloan")
        block = _get_nested(state, cat_cfg["ssr_path"]) or {}
        items = block if isinstance(block, list) else (block.get("items") or block.get("list") or [])

        category = target.get("category", "microloan")
        fc       = target.get("filter_context", {})

        for item in items:
            org = item.get("organization") or {}
            if not isinstance(org, dict):
                org = {}
            org_name = org.get("name")
            if isinstance(org_name, dict):
                name = org_name.get("short") or org_name.get("full")
            else:
                name = org_name
            name = name or item.get("organizationName") or "?"
            slug = org.get("alias") or ""

            # rateRange: {from, to} в процентах в день/год
            rate_range = item.get("rateRange") or {}
            rate_min = _dec(rate_range.get("from"))
            rate_max = _dec(rate_range.get("to"))

            amt_range = item.get("amountRange") or {}
            amount_min = _dec(amt_range.get("from"))
            amount_max = _dec(amt_range.get("to"))

            term_range = item.get("termRange") or {}
            term_min_d = _iso_duration_to_days(term_range.get("from"))
            term_max_d = _iso_duration_to_days(term_range.get("to"))

            product_name = item.get("name") or "Микрозайм"
            ext_id = stable_digest({
                "bank": name, "alias": slug, "category": category,
                "name": product_name, "id": item.get("_id") or item.get("id"),
            })[:32]

            yield OfferDraft(
                bank_name_raw=name,
                category=category,
                external_id=ext_id,
                title=product_name,
                url=f"https://www.sravni.ru/zaimy/{slug}/" if slug else None,
                rate_pct=rate_min,
                rate_kind="daily_pct",
                currency="RUB",
                amount_min=amount_min,
                amount_max=amount_max,
                term_months_min=max(1, term_min_d // 30) if term_min_d else None,
                term_months_max=max(1, term_max_d // 30) if term_max_d else None,
                raw={
                    "bank_alias": slug,
                    "filter_context": fc,
                    "item_id": item.get("_id") or item.get("id"),
                    "rate_max": str(rate_max) if rate_max else None,
                    "term_min_days": term_min_d,
                    "term_max_days": term_max_d,
                    "type": item.get("type"),
                    "registration_way": item.get("registrationWay"),
                    "consideration_time": item.get("considerationTime"),
                },
            )

    def _parse_ssr_vitrins(self, raw: bytes, target: dict) -> Iterable[OfferDraft]:
        text = raw.decode("utf-8", errors="ignore")
        m = _NEXT_DATA_RE.search(text)
        if not m:
            log.warning("sravni_api: __NEXT_DATA__ not found in SSR HTML")
            return

        try:
            nd = json.loads(m.group(1))
        except Exception as e:
            log.warning("sravni_api SSR JSON parse error: %s", e)
            return

        category = target.get("category", "credit")
        fc       = target.get("filter_context", {})
        state    = nd.get("props", {}).get("initialReduxState", {})

        cat_cfg  = _CAT_MAP.get(category, _CAT_MAP["credit"])
        ssr_path = cat_cfg.get("ssr_path", "products.list.offers")

        offers_block = _get_nested(state, ssr_path) or {}
        items = offers_block.get("items") or []
        orgs  = offers_block.get("organizations") or {}

        if not items:
            return

        # Категории card_debit / card_credit делят одну витрину /karty/.
        # Фильтруем по item.type ("debit" | "credit") чтобы не плодить дубли.
        card_filter = None
        if category == "card_debit":
            card_filter = "debit"
        elif category == "card_credit":
            card_filter = "credit"

        for item in items:
            if card_filter:
                itype = (item.get("type") or item.get("cardType") or "").lower()
                if itype and card_filter not in itype:
                    continue

            org_id = item.get("organization")
            if isinstance(org_id, dict):
                org = org_id
            else:
                org = orgs.get(str(org_id)) or {}

            bank_name = (
                (org.get("name") or {}).get("short")
                or org.get("alias") or str(org_id) or "?"
            )
            bank_slug = org.get("alias") or ""
            is_sber   = "sberbank" in bank_slug.lower() or "sber" in bank_slug.lower()

            rate = _dec(item.get("minRate"))
            rate_max = _dec(item.get("maxRate"))

            if rate is None:
                rates_dict = item.get("rates") or {}
                if rates_dict:
                    first_range = next(iter(rates_dict.values()), {})
                    rate = _dec(first_range.get("from"))
                    rate_max = _dec(first_range.get("to"))

            amount_min = _dec(item.get("minSumFrom"))
            amount_max = _dec(item.get("maxSumTo"))

            tmin = item.get("minTermFrom")
            tmax = item.get("maxTermTo")
            unit = item.get("unitFrom") or item.get("unitTo") or "months"
            if unit == "years":
                tmin = int(tmin) * 12 if tmin else None
                tmax = int(tmax) * 12 if tmax else None

            product_name = item.get("name") or category
            ext_id = stable_digest({
                "bank": bank_name, "alias": bank_slug,
                "name": product_name, "category": category,
            })[:32]

            yield OfferDraft(
                bank_name_raw=bank_name,
                category=category,
                external_id=ext_id,
                title=product_name,
                url=f"https://www.sravni.ru/bank/{bank_slug}/",
                rate_pct=rate,
                rate_kind="psk" if category == "credit" else "effective",
                currency="RUB",
                amount_min=amount_min,
                amount_max=amount_max,
                term_months_min=int(tmin) if tmin else None,
                term_months_max=int(tmax) if tmax else None,
                raw={
                    "bank_alias": bank_slug,
                    "is_sber": is_sber,
                    "rate_max": str(rate_max) if rate_max else None,
                    "filter_context": fc,
                    "item_id": item.get("id"),
                },
            )
