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


def test_chat_panel_only_in_ai_research():
    """Панель агента существует только на экране AI-исследования: на экранах
    общей базы и очереди верификации чат не рендерится."""
    jsx = _norm(_jsx())
    assert _norm('const chatVisible=view==="ai_research"&&chatOpen;') in jsx
    assert _norm("{chatVisible&&(<aside") in jsx


def test_header_titles_follow_context():
    """Заголовок показывает название выбранного контекста."""
    jsx = _jsx()
    assert "Новое AI-исследование" in jsx
    assert "Очередь верификации" in jsx


def test_catalog_actions_are_scoped_and_parser_is_a_separate_route():
    """CSV принадлежит каталогу, а парсер — отдельной вкладке."""
    jsx = _norm(_jsx())
    assert jsx.count(_norm('{view==="catalog"&&(')) >= 2  # действия + таблица
    assert "Заявка на разработку парсера" in _jsx()
    assert _norm('{view==="sources"&&(') in jsx
    assert "⚙ Парсеры" not in _jsx()


def test_chat_toggle_button_in_header():
    """Кнопка «Открыть чат»/«Скрыть чат» доступна на экране AI-исследования."""
    jsx = _jsx()
    assert "Открыть чат" in jsx
    assert "Скрыть чат" in jsx


def test_chat_default_closed_below_1100px():
    """На компактной ширине чат по умолчанию скрыт (off-canvas по кнопке)."""
    assert _norm("window.innerWidth>=1100") in _norm(_jsx())


def test_escape_closes_chat_panel():
    """Escape закрывает off-canvas панель; состояние разговора сохраняется."""
    jsx = _norm(_jsx())
    assert _norm('e.key==="Escape"') in jsx
    assert "setChatOpen(false)" in jsx


def test_research_surface_without_catalog_data():
    """Экран AI-исследования имеет собственную рабочую поверхность
    (без фильтров и таблицы общей базы)."""
    assert "lp-research-surface" in _jsx()
    assert ".lp-research-surface" in _css()


# ── AC2: заголовок переносит действия, страница без горизонтального скролла ──

def test_header_wraps_actions_to_second_line():
    """Действия заголовка переносятся на вторую строку при ширине ~992px."""
    css = _css()
    assert "flex-wrap: wrap" in _block(css, ".lp-main-header")
    assert "flex-wrap: wrap" in _block(css, ".lp-header-actions")


def test_table_container_is_the_only_horizontal_scroller():
    """Горизонтальная прокрутка — только у контейнера таблицы/очереди."""
    assert "overflow: auto" in _block(_css(), ".lp-table-wrap")


def test_horizontal_scroll_is_limited_to_table_queue_container():
    """Ни один иной элемент интерфейса не создаёт горизонтальную прокрутку."""
    css = re.sub(r"/\*.*?\*/", "", _css(), flags=re.DOTALL)
    scroll_containers = {
        selectors.strip()
        for selectors, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
        if re.search(r"\boverflow(?:-x)?\s*:\s*(?:auto|scroll)\b", body)
    }
    assert scroll_containers == {".lp-table-wrap"}


def test_no_100vh_anywhere():
    """Запрет `100vh` из эпика: высота — от геометрии iframe (100%)."""
    assert "100vh" not in _css()


def test_root_has_no_overflow_hidden():
    """Корневой `overflow: hidden` запрещён: ни у #loophole-root, ни у
    .lp-layout; высота задаётся в процентах от iframe."""
    css = _css()
    assert "overflow: hidden" not in _block(css, ".lp-layout")
    assert "overflow: hidden" not in _block(css, "#loophole-root")
    assert "height: 100%" in _block(css, "#loophole-root")


# ── AC3: приоритетное скрытие колонок и URL в деталях строки ────────────────

def test_secondary_columns_hidden_by_priority():
    """Ниже 1400px скрываются «Собрано» и URL (lp-col-narrow1), ниже 1100px —
    остальные второстепенные колонки (lp-col-narrow2)."""
    css = _css()
    assert re.search(r"\.lp-col-narrow1\s*\{[^}]*display:\s*none",
                     _media_body(css, "(max-width: 1399px)"))
    assert re.search(r"\.lp-col-narrow2\s*\{[^}]*display:\s*none",
                     _media_body(css, "(max-width: 1099px)"))


def test_catalog_table_columns_tagged_by_priority():
    """В каталоге «Собрано»/URL помечены narrow1, Банк/Доверие/Trust/Статус —
    narrow2 (и th, и td), чтобы CSS скрывал колонку целиком."""
    jsx = _jsx()
    assert jsx.count("lp-col-narrow1") >= 8   # каталог + очередь: th+td × 2 колонки
    assert jsx.count("lp-col-narrow2") >= 8   # каталог: th+td × 4 колонки


def test_url_available_in_row_details():
    """URL доступен в деталях каталога и в master-detail очереди."""
    jsx = _jsx()
    assert "открыть источник ↗" in jsx
    assert jsx.count("renderRecordContent(r)") >= 1
    assert "queueSelected.url" in jsx
    assert "Открыть источник" in jsx


def test_chat_offcanvas_below_1100px():
    """Ниже 1100px чат — off-canvas панель поверх контента: fixed справа,
    основная колонка занимает всю ширину."""
    body = _media_body(_css(), "(max-width: 1099px)")
    sidebar = re.search(r"\.lp-sidebar\s*\{([^}]*)\}", body)
    assert sidebar, "в @media (max-width: 1099px) нет правила .lp-sidebar"
    assert "position: fixed" in sidebar.group(1)
    assert "right: 0" in sidebar.group(1)
    layout = re.search(r"\.lp-layout-chat\s*\{([^}]*)\}", body)
    assert layout and "grid-template-columns: 1fr" in layout.group(1)

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
    """Тело renderRecordContent для проверок деталей раскрытой записи."""
    m = re.search(
        r"const renderRecordContent = \(r\) => \{(.*?)\n  \};",
        _jsx(),
        re.DOTALL,
    )
    assert m, "не найдено тело renderRecordContent"
    return m.group(1)


def test_resize_to_compact_closes_open_chat():
    """Переход с широкой ширины на <1100px закрывает открытую панель чата."""
    source = _jsx()
    normalized = _norm(source)
    assert _norm(
        "const [isCompactViewport, setIsCompactViewport] = "
        "useState(() => window.innerWidth < 1100);"
    ) in normalized
    handler = re.search(
        r"const syncChatViewport = \(\) => \{(.*?)\n\s*\};",
        source,
        re.DOTALL,
    )
    assert handler, "нет обработчика resize для режима чата"
    assert "window.innerWidth < 1100" in handler.group(1)
    assert "setIsCompactViewport(compact)" in handler.group(1)
    assert "setChatOpen(false)" in handler.group(1)
    assert _norm('window.addEventListener("resize", syncChatViewport);') in normalized
    assert _norm('window.removeEventListener("resize", syncChatViewport);') in normalized


def test_compact_chat_survives_resize_inside_compact_viewport():
    """Resize внутри compact-диапазона не закрывает уже открытую панель чата."""
    source = _jsx()
    normalized = _norm(source)
    assert _norm(
        "const previousCompactViewportRef = useRef(isCompactViewport);"
    ) in normalized
    handler = re.search(
        r"const syncChatViewport = \(\) => \{(.*?)\n\s*\};",
        source,
        re.DOTALL,
    )
    assert handler, "не найден обработчик resize для режима чата"
    body = handler.group(1)
    assert "const wasCompact = previousCompactViewportRef.current;" in body
    assert "previousCompactViewportRef.current = compact;" in body
    assert re.search(
        r"if\s*\(\s*!wasCompact\s*&&\s*compact\s*\)\s*setChatOpen\(false\);", body
    )


def test_only_compact_chat_is_modal_and_focus_trapped():
    """Desktop sidebar не модален и не ловит Tab; слой включается только compact."""
    source = _jsx()
    normalized = _norm(source)
    assert _norm("const chatModalOpen = chatVisible && isCompactViewport;") in normalized
    assert _norm(
        "useFocusLayer(chatModalOpen, chatPanelRef, () => setChatOpen(false), chatTitleRef);"
    ) in normalized
    aside = re.search(r"<aside[^>]*className=\"lp-sidebar\"[^>]*>", source)
    assert aside, "не найден aside панели чата"
    assert 'role={chatModalOpen ? "dialog" : "complementary"}' in aside.group(0)
    assert 'aria-modal={chatModalOpen ? "true" : undefined}' in aside.group(0)


def test_compact_chat_backdrop_blocks_page_and_title_has_focus_ref():
    """Off-canvas получает нативную нетаббируемую кнопку-backdrop, а title — ref."""
    source = _jsx()
    normalized = _norm(source)
    assert _norm(
        '{chatModalOpen && (<button type="button" className="lp-chat-backdrop" '
        'aria-label="Закрыть чат" tabIndex={-1} onClick={() => setChatOpen(false)} />)}'
    ) in normalized
    title = re.search(r"<div[^>]*className=\"lp-agent-name\"[^>]*>", source)
    assert title and "ref={chatTitleRef}" in title.group(0)
    backdrop = re.search(
        r"\.lp-chat-backdrop\s*\{([^}]*)\}",
        _media_body(_css(), "(max-width: 1099px)"),
    )
    assert backdrop, "в compact media нет backdrop off-canvas чата"
    assert "position: fixed" in backdrop.group(1)
    assert "inset: 0" in backdrop.group(1)
    assert "z-index: 899" in backdrop.group(1)


def test_expanded_detail_keeps_source_link_while_loading_or_failing():
    """Скрытый URL остаётся в деталях даже до загрузки контента и при ошибке."""
    body = _record_content_body()
    assert "const sourceLink = r.url ?" in body
    loading = re.search(r"if \(!entry \|\| entry.loading\) \{(.*?)\n    \}", body, re.DOTALL)
    error = re.search(r"if \(entry.error\) \{(.*?)\n    \}", body, re.DOTALL)
    assert loading and "{sourceLink}" in loading.group(1)
    assert error and "{sourceLink}" in error.group(1)


def test_pipeline_phase_labels_are_russian():
    """Названия clarify/execute/answer/done выводятся на русском языке."""
    source = _jsx()
    expected = {
        "clarify": "Уточнение",
        "execute": "Выполнение",
        "answer": "Ответ",
        "done": "Готово",
        "error": "Ошибка",
    }
    for phase_name, label in expected.items():
        assert re.search(rf'{phase_name}:\s*"{label}"', source)
    assert source.count("PHASE_LABELS[p]") >= 2


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


def test_table_details_button_opens_details_without_stealing_controls():
    """Каталог раскрывает детали, queue выбирает card; вложенные controls не всплывают."""
    source = _jsx()
    assert not re.search(r"<tr\b[^>]*\bon(?:Click|KeyDown)=", source)
    assert source.count('className="lp-row-details"') >= 1
    assert source.count("onClick={() => toggleContent(r.record_id)}") >= 1
    assert source.count("aria-expanded={expanded.has(r.record_id)}") >= 1
    assert source.count(
        "aria-controls={expanded.has(r.record_id) ? `lp-record-details-${r.record_id}` : undefined}"
    ) >= 1
    assert "onClick={() => setQueueSelectedId(record.record_id)}" in source
    assert 'aria-current={active ? "true" : undefined}' in source
    assert source.count("onClick={e => e.stopPropagation()}") >= 3
    assert source.count('onClick={e => e.stopPropagation()}>открыть ↗</a>') >= 1
    assert 'onChange={() => toggleRow(r.record_id)}' in source

