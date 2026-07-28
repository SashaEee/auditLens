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

from bank_audit.web.app import _loophole_html_with_bust  # noqa: E402


def test_bust_applied_to_jsx_and_css():
    html = _loophole_html_with_bust()
    assert 'src="/static/loophole/loophole.jsx?v=' in html
    assert 'href="/static/loophole/loophole.css?v=' in html
    # Голых ссылок без версии не осталось.
    assert 'src="/static/loophole/loophole.jsx"' not in html
    assert 'href="/static/loophole/loophole.css"' not in html
