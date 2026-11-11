"""Cache-bust основного интерфейса (app.jsx / app.js)."""
import os

# как в tests/loophole/test_static_bust.py: app при импорте делает db.init()
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from bank_audit.web.app import STATIC_DIR, _index_html_with_bust  # noqa: E402


def test_main_bundle_version_follows_jsx_mtime():
    """30.09: в index.html стояла прописанная руками версия ?v=20260926-xlsx1,
    замена её не находила — правки app.jsx 28–30.09 браузеры могли не увидеть.
    С 04.10 версия — отпечаток исходника, из которого собран app.js (по
    времени файлов судить нельзя: при заливке app.jsx пишется позже app.js)."""
    import hashlib
    html = _index_html_with_bust()
    jsx = STATIC_DIR / "app.jsx"
    sha = hashlib.sha256(jsx.read_bytes()).hexdigest()
    built = STATIC_DIR / "app.js"
    if built.exists() and f"sha256 {sha}" in built.open(encoding="utf-8").readline():
        assert f'src="/static/app.js?v={sha[:12]}"' in html
    else:
        assert f'src="/static/app.jsx?v={int(jsx.stat().st_mtime)}"' in html
    assert "20260926-xlsx1" not in html


def test_page_and_bundle_are_compressed_and_cached():
    """Прокси перед приложением ответы не сжимает: страницу (370 КБ) и сборку (2 МБ) сжимает само приложение.
    Страница перепроверяется по ETag (304), сборка с версией в адресе держится в браузере без перепроверки."""
    from fastapi.testclient import TestClient
    from bank_audit.web.app import app
    c = TestClient(app)                                         # без входа в lifespan: фоновые циклы не нужны
    r = c.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and r.headers["etag"]
    assert "<div id=\"root\">" in r.text and int(r.headers["content-length"]) < len(r.content) / 3
    again = c.get("/", headers={"If-None-Match": r.headers["etag"], "Accept-Encoding": "gzip"})
    assert again.status_code == 304 and not again.content
    plain = c.get("/vsp-any-path", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers and plain.text == r.text
    js = c.get("/static/app.js?v=abc", headers={"Accept-Encoding": "gzip"})
    assert js.status_code == 200 and js.headers["content-encoding"] == "gzip" and "immutable" in js.headers["cache-control"]
    assert int(js.headers["content-length"]) < len(js.content) / 2.5 and js.headers["etag"]
    assert c.get("/static/app.js?v=abc", headers={"If-None-Match": js.headers["etag"]}).status_code == 304
    raw = c.get("/static/app.js", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers and raw.content == js.content and "immutable" not in raw.headers.get("cache-control", "")
    font = c.get("/static/vendor/fonts/geist-1.woff2", headers={"Accept-Encoding": "gzip"})
    assert font.status_code == 200 and "content-encoding" not in font.headers     # уже сжатые файлы не трогаем
