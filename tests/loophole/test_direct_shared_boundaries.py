from __future__ import annotations

from bank_audit.rag import fetcher, web_search


def test_direct_fetch_rejects_non_direct_supplied_browser():
    result = fetcher._fetch_browser("https://example.test", browser=object(), direct=True)
    assert result is None


def test_direct_search_passes_direct_to_all_backends(monkeypatch):
    monkeypatch.setattr(web_search, "_searxng_url", lambda: "https://search.test")
    received = []

    def backend(*args, **kwargs):
        received.append(kwargs["direct"])
        return []

    monkeypatch.setattr(web_search, "_search_searxng", backend)
    monkeypatch.setattr(web_search, "_search_ddgs", backend)
    monkeypatch.setattr(web_search, "_search_ddg", backend)
    monkeypatch.setattr(web_search, "_search_yandex", backend)
    monkeypatch.setattr(web_search.rag_cache, "get", lambda *args: None)
    monkeypatch.setattr(web_search.rag_cache, "put", lambda *args: None)
    web_search.search("проверка", direct=True)
    assert received and all(received)


def test_ddgs_direct_path_rejects_ddgs_proxy(monkeypatch):
    monkeypatch.setenv("DDGS_PROXY", "http://127.0.0.1:9")
    assert web_search._search_ddgs("проверка", direct=True) == []
