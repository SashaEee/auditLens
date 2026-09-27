"""Cache-bust статики модуля loophole.

Регрессия 2026-07-26: браузер кэшировал `loophole.css` эвристически
(Starlette StaticFiles не шлёт Cache-Control), а bust-параметр `?v=mtime`
добавлялся только к `loophole.jsx`. После деплоя новый JSX со старым CSS
из кэша давал симптом «не подгрузились css стили для маркирования лазеек».
"""

import os

# conftest ставит sqlite+aiosqlite, но aiosqlite не установлен в .venv;
# bank_audit.web.app при импорте делает db.init() → нужен синхронный драйвер.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from bank_audit.web.app import _loophole_html_with_bust


def test_bust_applied_to_built_js_and_css():
    """С 28.09 страница грузит предсобранный loophole.js (без Babel в браузере)."""
    html = _loophole_html_with_bust()
    assert 'src="/static/loophole/loophole.js?v=' in html
    assert 'href="/static/loophole/loophole.css?v=' in html
    # Голых ссылок без версии не осталось, JSX в браузере больше не собирается.
    assert 'src="/static/loophole/loophole.js"' not in html
    assert 'href="/static/loophole/loophole.css"' not in html
    assert "babel.min.js" not in html and 'type="text/babel"' not in html
