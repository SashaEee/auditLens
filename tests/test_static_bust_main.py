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
