from __future__ import annotations
import os, time
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

class HttpCollector:
    def __init__(self, user_agent: str | None = None, timeout: float = 30.0,
                 delay_ms: int = 1500):
        self.client = httpx.Client(
            headers={"User-Agent": user_agent or os.getenv("HTTP_USER_AGENT", "BankAuditBot/0.1")},
            timeout=timeout, follow_redirects=True,
        )
        self.delay_s = delay_ms / 1000.0

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=20),
           retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)))
    def fetch(self, url: str) -> tuple[int, bytes]:
        time.sleep(self.delay_s)
        r = self.client.get(url)
        if r.status_code >= 500:
            r.raise_for_status()
        return r.status_code, r.content

    def close(self):
        self.client.close()
