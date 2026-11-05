"""Реестр ВСП Сбербанка по данным Банка России — основа справочника отделений.

Источник: публичная страница ЦБ «Внутренние структурные подразделения» кредитной
организации https://www.cbr.ru/finorg/foinfo/branches/?id=<id банка> и JSON, который
она сама подгружает по каждому филиалу:
    GET https://www.cbr.ru/finorg/foinfo/branches/subfilialy/?id=<FILIAL_SBJ_ID>
      → [{FILIAL_SBJ_ID, FILIAL_SBJ_TYPE_CODE: BNK_DOPO|BNK_PPKO|BNK_UTO, FILIAL_NAME,
          FILIAL_NUM, FILIAL_ADDRESS, FILIAL_OPEN_DATE, FILIAL_PARENT_NAME, HasParent}]
Замер 05.11.2026: 9 997 подразделений Сбербанка — 9 540 дополнительных офисов (DOPO),
447 передвижных пунктов кассовых операций (PPKO, адресов несколько — карточки на
картах у них нет) и 10 прочих (UTO). Координат нет: на карту отделение ложится
сопоставлением адреса с карточками 2ГИС и Яндекса (registry/match.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ..http import SourceHttp
from .match import address_key, norm_text

log = logging.getLogger(__name__)

SBER_CBR_ID = "1315037838265"         # Сбербанк на cbr.ru/finorg/foinfo
LIST_URL = "https://www.cbr.ru/finorg/foinfo/branches/?id={id}"
SUB_URL = "https://www.cbr.ru/finorg/foinfo/branches/subfilialy/"
TYPES = {"BNK_DOPO": "office", "BNK_PPKO": "mobile", "BNK_UTO": "remote"}
FEDERAL_CITIES = ("москва", "санкт-петербург", "севастополь")

_REGION = re.compile(r"(область|\bобл\b|край|республика|\bресп\b|автономный округ|автономная область|\bао\b|"
                     r"югра|чувашия)", re.I)
_SETTLEMENT = re.compile(r"^(г|город|пгт|рп|п|пос|поселок|посёлок|с|село|д|дер|деревня|ст-ца|станица|х|хутор|"
                         r"кп|аул|сл|слобода|нп)\.?\s+(.+)$", re.I)


@dataclass
class RegistryRow:
    ref: str
    kind: str                 # office | mobile | remote | cash_center
    name: str
    number: str | None
    address: str
    region: str | None
    city: str | None
    open_date: str | None
    parent: str | None

    @property
    def akey(self) -> str | None:
        return address_key(self.address, self.city)


def split_address(address: str) -> tuple[str | None, str | None]:
    """(регион, населённый пункт) из адреса ЦБ «индекс, РЕГИОН, [РАЙОН,] Г. ГОРОД, УЛ…»."""
    parts = [p.strip() for p in (address or "").split(",") if p.strip()]
    region = city = None
    for p in parts:
        low = norm_text(p)
        if re.fullmatch(r"\d{6}", low):
            continue
        if region is None and _REGION.search(low):
            region = p.title()
            continue
        if re.sub(r"^г\.?\s*", "", low) in FEDERAL_CITIES:
            region = city = re.sub(r"^г\.?\s*", "", low).title()
            continue
        m = _SETTLEMENT.match(low)
        if m and not re.search(r"\d", low) and city is None:
            city = m.group(2).strip().title()
            if region is None:
                region = city
            break
    return region, city


def parse_row(r: dict) -> RegistryRow:
    addr = (r.get("FILIAL_ADDRESS") or "").strip()
    kind = TYPES.get(r.get("FILIAL_SBJ_TYPE_CODE") or "", "office")
    if kind == "office" and "инкассатор" in (r.get("FILIAL_NAME") or "").lower():
        kind = "cash_center"     # кассово-инкассаторский центр: клиентов не обслуживает (~300 из 9 540)
    region, city = split_address(addr.split("#")[0])
    return RegistryRow(ref=str(r["FILIAL_SBJ_ID"]), kind=kind, name=(r.get("FILIAL_NAME") or "").strip(),
                       number=r.get("FILIAL_NUM"), address=addr, region=region, city=city,
                       open_date=(r.get("FILIAL_OPEN_DATE") or "")[:10] or None,
                       parent=r.get("FILIAL_PARENT_NAME"))


async def fetch_registry(http: SourceHttp, bank_id: str = SBER_CBR_ID) -> list[RegistryRow]:
    """Все подразделения банка: страница со списком филиалов → JSON по каждому филиалу
    (и вложенным, если HasParent). Вежливо: частота — из SourceHttp."""
    html = await http.get(LIST_URL.format(id=bank_id), expect_json=False)
    queue = re.findall(r'data-accordion-head="(\d+)"', html)
    if not queue:
        raise RuntimeError("cbr: на странице не нашлось филиалов (изменилась вёрстка?)")
    out: list[RegistryRow] = []
    seen: set[str] = set()
    while queue:
        fid = queue.pop(0)
        if fid in seen:
            continue
        seen.add(fid)
        rows = await http.get(SUB_URL, params={"id": fid}, headers={"X-Requested-With": "XMLHttpRequest"})
        for r in rows or []:
            out.append(parse_row(r))
            if r.get("HasParent"):
                queue.append(str(r["FILIAL_SBJ_ID"]))
    log.info("cbr: подразделений %d (филиалов %d)", len(out), len(seen))
    return out
