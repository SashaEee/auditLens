"""Обновление модуля «Лазейки» — кнопкой «Обновить» в шапке самого модуля.

История: 26.08.2026 кнопка ⟳ топбара AuditLens перезагружала iframe модуля через
`<LoopholePage key={refreshTick}/>` (спека spec-loophole-refresh-button.md).
25.09.2026 при переходе на единую систему вкладок кнопку ⟳ из топбара убрали
(коммит ab6d4b4): обновление данных есть в каждом контексте модуля, а полная
перезагрузка страницы дублировала его. Тесты фиксируют текущий контракт:
глобального тика нет, модуль обновляет данные сам, прогон ИИ не затрагивается.

Сравнения устойчивы к реформату: исходники нормализуются по whitespace (_norm).
"""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "bank_audit"
APP_JSX = STATIC / "web" / "static" / "app.jsx"
LOOPHOLE_JSX = STATIC / "loophole" / "static" / "loophole.jsx"


def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def test_topbar_has_no_half_removed_refresh_tick():
    """Механизм ⟳ убран целиком: ни стейта, ни ключа ремаунта iframe."""
    app = _norm(APP_JSX.read_text(encoding="utf-8"))
    assert 'page==="loophole"&&setRefreshTick' not in app
    assert "<LoopholePagekey=" not in app


def test_module_header_refreshes_current_context():
    """«Обновить» в шапке модуля перезагружает данные текущего контекста."""
    jsx = _norm(LOOPHOLE_JSX.read_text(encoding="utf-8"))
    assert _norm(
        'onClick={view === "queue" ? loadQueue'
        ' : view === "admin" ? loadAdmin'
        ' : view === "sources" ? loadParsers : loadRecords}'
    ) in jsx
    assert "Обновить" in LOOPHOLE_JSX.read_text(encoding="utf-8")


def test_ai_page_not_affected():
    """Персистентный AIPage не зависит от обновления модуля — прогон не обрывается."""
    s = APP_JSX.read_text(encoding="utf-8")
    ai_block = next((line for line in s.splitlines() if "<AIPage" in line), None)
    assert ai_block, "в app.jsx не найден блок с <AIPage — проверить верстку Shell"
    assert "refresh" not in ai_block.lower()
