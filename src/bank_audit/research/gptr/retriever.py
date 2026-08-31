"""Ретривер gpt-researcher поверх шлюза fleet-searxng.

Штатный searx-ретривер gpt-researcher делает GET к открытому инстансу. Наш
поиск — версионированный контракт POST /v1/search с Bearer-токеном, серверной
фильтрацией доменов и набором движков (google cse / yandex / duckduckgo);
локальный инстанс на дешёвых движках отдаёт по банковским запросам мусор.

Контракт возврата тот же, что у их ретриверов: [{"href": ..., "body": ...}].
"""
from __future__ import annotations

import logging
import os
import re

import httpx

log = logging.getLogger(__name__)


class FleetSearch:
    """Совместимый с gpt-researcher поиск через наш шлюз."""

    def __init__(self, query: str, query_domains=None):
        # `site:` вырезаем из текста и переводим в серверный фильтр доменов:
        # гейтвей фильтрует у себя, а движки поиска этот оператор понимают
        # по-разному. Фильтр действует только на ТОТ запрос, где site: стоял —
        # иначе общий сравнительный запрос тоже запирается на сайты банков и
        # отчёт остаётся без обзоров и жалоб (замер 31.08.2026: контекст упал
        # с 24 800 до 4 600 символов).
        sites = re.findall(r"site:(\S+)", query, flags=re.IGNORECASE)
        clean = re.sub(r"site:\S+", " ", query, flags=re.IGNORECASE)
        self.query = re.sub(r"\s+", " ", clean).strip() or query
        self.query_domains = [d.split("/")[0].lower().removeprefix("www.")
                              for d in (list(sites) + list(query_domains or []))]
        self.base = (os.getenv("FLEET_SEARXNG_URL") or "").rstrip("/")
        self.token = os.getenv("FLEET_SEARXNG_TOKEN")
        self.engines = [e.strip() for e in os.getenv(
            "FLEET_SEARXNG_ENGINES", "google cse,yandex,duckduckgo").split(",")
            if e.strip()]

    def search(self, max_results: int = 10) -> list[dict]:
        if not self.base:
            log.error("FLEET_SEARXNG_URL не задан — поиск недоступен")
            return []
        body: dict = {"query": self.query[:512], "language": "ru",
                      "max_results": max(1, min(int(max_results or 8), 20))}
        if self.engines:
            body["engines"] = self.engines
        if self.query_domains:
            body["include_domains"] = self.query_domains[:10]
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with httpx.Client(timeout=httpx.Timeout(connect=5, read=60,
                                                    write=5, pool=5)) as c:
                r = c.post(f"{self.base}/v1/search", json=body, headers=headers)
        except Exception as e:
            log.info("fleet %s: %s", self.query[:50], type(e).__name__)
            return []
        if r.status_code == 403:
            log.error("fleet-searxng: КВОТА ТРАФИКА ИСЧЕРПАНА (403)")
            return []
        if r.status_code == 401:
            log.error("fleet-searxng: токен не принят (401)")
            return []
        if r.status_code != 200:
            log.warning("fleet-searxng %s: HTTP %s", self.query[:50], r.status_code)
            return []
        try:
            data = r.json()
        except Exception:
            return []
        out: list[dict] = []
        for it in (data.get("results") or []):
            href = it.get("url") or it.get("href") or ""
            if not href:
                continue
            out.append({"href": href,
                        "body": it.get("content") or it.get("snippet") or ""})
            if len(out) >= max_results:
                break
        return out
