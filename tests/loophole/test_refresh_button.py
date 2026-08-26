"""Кнопка ⟳ топбара на странице «Лазейки» (app.jsx).

Регрессия 2026-08-26: обработчик `setPage(p=>p)` отдавал то же значение
стейта, React делал bail-out — кнопка была no-op; а персистентный iframe
«Лазеек» не имел key и не перезагружался бы даже при рабочем ремаунте.

Фронт без сборки и без UI-тестового стенда — проверки текстовые, по образцу
test_static_bust.py. Покрывают строки I/O-матрицы спеки
docs/loophole/bmad/implementation-artifacts/spec-loophole-refresh-button.md:
HAPPY_PATH, OTHER_PAGE, TAB_SWITCH, AI_RUN.

Сравнения устойчивы к косметическому реформату app.jsx: исходник и образцы
нормализуются по whitespace (см. _norm), чтобы переносы строк и пробелы
не ломали тесты при неизменном поведении.
"""

import re
from pathlib import Path

APP_JSX = (
    Path(__file__).resolve().parents[2]
    / "src" / "bank_audit" / "web" / "static" / "app.jsx"
)


def _src() -> str:
    return APP_JSX.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def test_refresh_tick_state_declared():
    """Стейт объявлен: const[refreshTick,setRefreshTick]=useState(0) — единое имя
    для объявления, обработчика ⟳ и key, без рассинхрона."""
    assert _norm("const[refreshTick,setRefreshTick]=useState(0);") in _norm(_src())


def test_handler_increments_tick_only_on_loophole():
    """HAPPY_PATH + OTHER_PAGE: ⟳ инкрементирует refreshTick только на «Лазейках»."""
    s = _norm(_src())
    assert _norm('page==="loophole"&&setRefreshTick(t=>t+1)') in s
    # Мёртвый обработчик-bail-out убран.
    assert "setPage(p=>p)" not in s


def test_loophole_page_remounts_by_tick():
    """HAPPY_PATH: LoopholePage ремонтируется по key={refreshTick} → перезагрузка iframe."""
    assert _norm("<LoopholePage key={refreshTick}/>") in _norm(_src())


def test_tick_never_reset_on_tab_switch():
    """TAB_SWITCH: тик не сбрасывается — возврат на вкладку не перезагружает модуль."""
    assert "setRefreshTick(0)" not in _norm(_src())


def test_ai_page_not_affected():
    """AI_RUN: персистентный AIPage не зависит от тика — прогон не обрывается."""
    s = _src()
    ai_block = next(
        (line for line in s.splitlines() if "<AIPage" in line), None
    )
    assert ai_block, "в app.jsx не найден блок с <AIPage — проверить верстку Shell"
    assert "refreshTick" not in ai_block
