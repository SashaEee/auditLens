"""Cache-bust основного интерфейса (app.jsx / app.js)."""
import os

# как в tests/loophole/test_static_bust.py: app при импорте делает db.init()
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from bank_audit.web.app import STATIC_DIR, _index_html_with_bust  # noqa: E402


def test_main_bundle_version_follows_jsx_mtime():
    """30.09: в index.html стояла прописанная руками версия ?v=20260926-xlsx1,
    замена по mtime её не находила — правки app.jsx 28–30.09 браузеры могли
    не увидеть. Версия берётся из mtime и поверх прописанной."""
    html = _index_html_with_bust()
    built = STATIC_DIR / "app.js"
    jsx = STATIC_DIR / "app.jsx"
    if built.exists() and built.stat().st_mtime >= jsx.stat().st_mtime:
        assert f'src="/static/app.js?v={int(built.stat().st_mtime)}"' in html
    else:
        assert f'src="/static/app.jsx?v={int(jsx.stat().st_mtime)}"' in html
    assert "20260926-xlsx1" not in html
