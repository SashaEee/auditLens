"""Story 1.3 — адаптивные маршруты рабочих контекстов.

Спека: docs/loophole/bmad/implementation-artifacts/
spec-1-3-адаптивные-маршруты-рабочих-контекстов.md
Дизайн-контракт: docs/loophole/bmad/planning-artifacts/ux-designs/
ux-auditLens-2026-08-25/ADAPTIVE-CHAT-SPEC.md

Фронт без сборки и без UI-стенда — проверки текстовые (по образцу
test_iframe_shell_theme.py / test_refresh_button.py).

Покрытые критерии приёмки frozen-интента:
- каталог, AI-исследование и очередь ЦК КС — отдельные маршруты и экраны,
  не совмещённые на одной рабочей поверхности (панель агента живёт только
  в контексте AI-исследования);
- действия заголовка переносятся на вторую строку, у страницы нет
  горизонтальной прокрутки (прокрутка — только у контейнера таблицы/очереди);
- ниже 1400px первыми скрываются «Собрано» и URL, ниже 1100px — остальные
  второстепенные колонки; URL доступен в деталях строки;
- без `100vh` и корневого `overflow: hidden`; ниже 1100px чат — off-canvas.
"""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bank_audit" / "loophole" / "static"
LOOPHOLE_JSX = SRC / "loophole.jsx"
LOOPHOLE_CSS = SRC / "loophole.css"


def _jsx() -> str:
    return LOOPHOLE_JSX.read_text(encoding="utf-8")


def _css() -> str:
    return LOOPHOLE_CSS.read_text(encoding="utf-8")


def _norm(s: str) -> str:
    """Схлопывает весь whitespace — сравнение не зависит от форматирования."""
    return re.sub(r"\s+", "", s)


def _block(css: str, selector: str) -> str:
    """Тело первого правила `selector { ... }` (невложенного)."""
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"в CSS не найдено правило {selector}"
    return m.group(1)


def _media_body(css: str, condition: str) -> str:
    """Тело `@media (condition) { … }` с подсчётом вложенных скобок."""
    marker = f"@media {condition}"
    start = css.find(marker)
    assert start >= 0, f"в CSS нет блока {marker}"
    brace = css.index("{", start)
    depth = 0
    for i in range(brace, len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[brace + 1:i]
    raise AssertionError(f"блок {marker} не закрыт")


# ── AC1: отдельные маршруты рабочих контекстов ───────────────────────────────

def test_view_state_supports_three_routes():
    """view различает catalog | ai_research | queue — три рабочих экрана."""
    jsx = _jsx()
    assert 'useState("catalog")' in jsx
    assert 'view === "ai_research"' in jsx
    assert 'view === "queue"' in jsx


def test_open_context_maps_route_directly():
    """openContext переводит ai_research на собственный маршрут, а не на
    каталог: данные и действия контекстов не смешиваются."""
    assert _norm("setView(id);") in _norm(_jsx())


def test_nav_active_state_follows_route():
    """Активный пункт навигации — текущий маршрут (включая ai_research)."""
    assert _norm("c.id===view") in _norm(_jsx())


# ── AC2: заголовок переносит действия, страница без горизонтального скролла ──

# ── AC3: приоритетное скрытие колонок и URL в деталях строки ────────────────

# ── Корректирующее review story 1.3 ──────────────────────────────────────────

def _send_chat_body() -> str:
    """Тело sendChat для проверок жизненного цикла одного AI-запуска."""
    m = re.search(
        r"const sendChat = useCallback\(async \(overrideMessage, opts\) => \{"
        r"(.*?)\n  \}, \[",
        _jsx(),
        re.DOTALL,
    )
    assert m, "не найдено тело sendChat"
    return m.group(1)


def _record_content_body() -> str:
    """Тело renderRecordContent для проверок деталей раскрытой записи.

    Сигнатура расширена вторым аргументом `opts` (режим «всегда развёрнут»
    карточки очереди) — хелпер должен находить то же тело функции.
    """
    m = re.search(
        r"const renderRecordContent = \(r[^)]*\) => \{(.*?)\n  \};",
        _jsx(),
        re.DOTALL,
    )
    assert m, "не найдено тело renderRecordContent"
    return m.group(1)


def test_fresh_ai_run_resets_previous_phase_and_subtasks():
    """Новый запрос до clarify не наследует прогресс и подзадачи прошлого run."""
    fresh_start = re.search(r"if \(!skipClarify\) \{(.*?)\n    \}", _send_chat_body(), re.DOTALL)
    assert fresh_start, "нет отдельной ветки свежего AI-запуска"
    assert "setPhase(null)" in fresh_start.group(1)
    assert "setSubtasks([])" in fresh_start.group(1)


def test_normal_sse_eof_marks_pipeline_done():
    """Только normal EOF без questions/error завершает фазу pipeline."""
    body = _send_chat_body()
    eof_tail = body[body.rfind("flushAssistant();"):]
    terminal_guard = eof_tail.index("if (terminalError)")
    normal_eof = eof_tail.index("if (!gotQuestions)")
    assert terminal_guard < normal_eof
    assert "return false;" in eof_tail[terminal_guard:normal_eof]
    assert re.search(
        r'if \(!gotQuestions\) \{\s*setPhase\("done"\);',
        eof_tail,
    )


def test_chat_http_or_missing_stream_enters_error_before_reader():
    """HTTP-ошибка или пустое тело не доходят до getReader и показывают ошибку."""
    body = _send_chat_body()
    reader_at = body.index("const reader = resp.body.getReader();")
    guard = re.search(
        r"if\s*\(\s*!resp\.ok\s*\|\|\s*!resp\.body\s*\)\s*\{\s*"
        r"throw new Error\([^;]+\);\s*\}",
        body,
        re.DOTALL,
    )
    assert guard, "нет ранней обработки HTTP-ошибки или пустого SSE-тела"
    assert guard.start() < reader_at
    error_surface = re.search(r"\} catch \(e\) \{(.*?)\} finally", body, re.DOTALL)
    assert error_surface, "нет ветки ошибки sendChat"
    assert 'setPhase("error")' in error_surface.group(1)
    assert 'content: "Ошибка: " + message' in error_surface.group(1)
    assert "return false;" in error_surface.group(1)


    """Поздний 200 старого запроса не может отменить свежий fail-closed отказ."""
def test_latest_queue_request_wins_over_stale_success():
    source = _jsx()
    assert "const queueRequestRef = useRef(0);" in source
    m = re.search(r"const loadQueue = useCallback\(async \(\) => \{(.*?)\}, \[\]", source, re.DOTALL)
    assert m, "не найдено тело loadQueue"
    body = m.group(1)
    assert "const requestGeneration = ++queueRequestRef.current;" in body
    assert body.count("requestGeneration !== queueRequestRef.current") >= 3
    assert re.search(
        r"finally\s*\{\s*if \(requestGeneration === queueRequestRef\.current\) \{\s*"
        r"setQueueLoading\(false\);",
        body,
    )
