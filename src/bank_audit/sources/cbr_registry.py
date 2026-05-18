"""CBR registry adapter — официальный реестр БИК (Банк России).

Источник: https://www.cbr.ru/scripts/XML_bic.asp — открытый XML, ~55KB,
кодировка windows-1251. Содержит: ShortName, Bic, DU (дата установки).

Адаптер не создаёт OfferDraft — он питает таблицу `bank` через специальный
post-processor (см. orchestrator). Возвращает «псевдо-офферы» категории
'bank_rating' с пустыми ставками, чтобы пройти стандартный pipeline и
сохранить snapshot. Реальная upsert в `bank` делается отдельной утилитой.

Альтернатива: банк-метаданные с banki_ratings (place, reviews) — это
другой адаптер. Здесь мы получаем ground-truth список российских банков
с регистрационными данными ЦБ.
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .base import SourceAdapter, FetchResult
from ..models import OfferDraft, RawSnapshot
from ..hashing import stable_digest

log = logging.getLogger(__name__)

CBR_BIC_URL = "https://www.cbr.ru/scripts/XML_bic.asp"

# Простой regex по Record (без полноценного XML-парсера — формат стабилен 25+ лет)
_RECORD_RE = re.compile(
    r'<Record\s+ID="(?P<id>\d+)"\s+DU="(?P<du>[^"]*)"\s*>'
    r'\s*<ShortName>(?P<name>[^<]*)</ShortName>'
    r'\s*<Bic>(?P<bic>\d+)</Bic>'
    r'\s*</Record>',
    re.IGNORECASE,
)


class CBRRegistryAdapter(SourceAdapter):
    """Скачивает официальный XML-реестр БИК Банка России."""

    name = "cbr_registry"

    def fetch(self, target: dict[str, Any]) -> FetchResult:
        url = target.get("url", CBR_BIC_URL)
        with httpx.Client(timeout=30, follow_redirects=True) as c:
            r = c.get(url, headers={
                "User-Agent": "Mozilla/5.0 bank_audit_platform/1.0",
                "Accept": "application/xml,text/xml,*/*",
            })
        if r.status_code != 200:
            raise RuntimeError(f"cbr_registry: {url} → HTTP {r.status_code}")

        content = r.content
        path, digest, n = self.raw.write(
            self.name, target["name"], content, "xml",
            meta={"url": url, "target": target["name"]},
        )
        snap = RawSnapshot(
            source=self.name, target_name=target["name"], url=url,
            fetched_at=datetime.now(timezone.utc), http_status=r.status_code,
            content_sha256=digest, storage_path=path, bytes=n,
            category="bank_rating",
        )
        return FetchResult(snapshot=snap, html=content)

    def parse_offers(self, html: bytes, target: dict[str, Any]) -> Iterable[OfferDraft]:
        """Каждый Record → псевдо-оффер категории 'bank_rating' с metadata в raw.

        Это сохраняет данные в обычный pipeline (offer + terms + history),
        и параллельно нормализатор может апсёртить bank.cbr_reg_no/cbr_status.
        """
        try:
            text = html.decode("cp1251")
        except Exception:
            text = html.decode("utf-8", errors="ignore")

        seen = 0
        for m in _RECORD_RE.finditer(text):
            seen += 1
            name = (m.group("name") or "").strip()
            bic  = m.group("bic")
            du   = m.group("du")
            if not name or not bic:
                continue
            ext_id = stable_digest({"src": "cbr_bic", "bic": bic})[:32]
            yield OfferDraft(
                bank_name_raw=name,
                category="bank_rating",
                external_id=ext_id,
                title=f"CBR registry: {name}",
                url=f"https://www.cbr.ru/banking_sector/credit/coinfo/?BIC={bic}",
                raw={
                    "cbr_bic":     bic,
                    "cbr_reg_no":  bic[-4:],   # эвристика: последние 4 цифры БИК часто = рег. номер
                    "cbr_du":      du,
                    "cbr_record":  m.group("id"),
                    "is_active":   True,        # XML_bic содержит только действующие БИК
                },
            )
        log.info("cbr_registry: parsed %d records", seen)
