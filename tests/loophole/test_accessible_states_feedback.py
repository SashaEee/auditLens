"""Story 1.4 — доступные состояния и обратная связь интерфейса.

Спека: docs/loophole/bmad/implementation-artifacts/
spec-1-4-доступные-состояния-и-обратная-связь-интерфейса.md
Дизайн-контракт: docs/loophole/bmad/planning-artifacts/ux-designs/
ux-auditLens-2026-08-25/ADAPTIVE-CHAT-SPEC.md (§4, §6, §8).

Фронт без сборки и без UI-стенда — проверки текстовые (по образцу
test_iframe_shell_theme.py / test_adaptive_context_routes.py).

Покрытые критерии приёмки frozen-интента:
- загрузка, пустой результат с «Сбросить» и ошибка с «Повторить» — три
  разные поверхности; ошибка не маскируется под пустой результат;
- единственный toast с типом info/success/error; деструктивное действие —
  через модальное подтверждение с последствием; alert()/confirm() запрещены;
- модалки и off-canvas: семантика dialog, focus-trap, Escape, возврат фокуса
  на открывший контрол; видимый :focus-visible; мишени >= 28px.
"""

import math
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bank_audit" / "loophole" / "static"
LOOPHOLE_JSX = SRC / "loophole.jsx"
LOOPHOLE_CSS = SRC / "loophole.css"

JSX = LOOPHOLE_JSX.read_text(encoding="utf-8")
CSS = LOOPHOLE_CSS.read_text(encoding="utf-8")


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


# ── AC1: загрузка / пустой результат / ошибка — три разные поверхности ──────

def test_catalog_loading_surface():
    """Пока идёт запрос записей, каталог показывает поверхность загрузки,
    а не пустую таблицу и не «нет записей»."""
    jsx = _norm(JSX)
    assert "Загрузка записей…" in JSX
    # Загрузка — отдельная ветка разметки до проверки на пустой результат.
    assert _norm("{loading?(") in jsx


def test_catalog_empty_state_has_reset_action():
    """Пустой результат содержит действие «Сбросить» (сброс фильтров)."""
    m = re.search(r'Нет записей по выбранным фильтрам\.(.{0,400})', JSX, re.DOTALL)
    assert m, "не найдена поверхность пустого результата каталога"
    assert "Сбросить" in m.group(1)


def test_catalog_error_state_not_masked_as_empty():
    """Ошибка загрузки записей — отдельная поверхность с «Повторить»:
    состояние ошибки хранится отдельно и не проваливается в «нет записей»."""
    jsx = _norm(JSX)
    assert _norm('const[recordsError,setRecordsError]=useState(') in jsx
    # loadRecords перехватывает сбой и выставляет ошибку (раньше был только
    # try/finally — исключение оставляло старые/пустые данные).
    m = re.search(r"const loadRecords = useCallback\(async \(\) => \{(.*?)\}, \[", JSX, re.DOTALL)
    assert m, "не найдено тело loadRecords"
    body = m.group(1)
    assert "catch" in body and "setRecordsError(" in body
    assert "Повторить" in JSX


def test_queue_error_state_has_retry():
    """Ошибка загрузки очереди верификации — поверхность с «Повторить»,
    а не только toast: экран обязан различать ошибку и пустую очередь."""
    jsx = _norm(JSX)
    assert _norm('const[queueError,setQueueError]=useState(') in jsx
    m = re.search(r"queueError\s*\?\s*\((.{0,600})", JSX, re.DOTALL)
    assert m, "нет ветки поверхности ошибки очереди"
    assert "Повторить" in m.group(1)


# ── AC2: единственный типизированный toast; модалки вместо alert/confirm ────

def test_no_alert_or_confirm_calls():
    """alert() и confirm() запрещены — ни прямых, ни window.-вызовов."""
    assert not re.search(r"(?<![\w.])alert\s*\(", JSX), "в jsx остался alert()"
    assert not re.search(r"(?<![\w.])confirm\s*\(", JSX), "в jsx остался confirm()"
    assert "window.confirm" not in JSX
    assert "window.alert" not in JSX


def test_single_typed_toast():
    """Один toast с типом info/success/error: состояние хранит вид,
    разметка рендерит ровно один элемент toast."""
    jsx = _norm(JSX)
    assert _norm("const[toast,setToast]=useState(null);") in jsx
    # Единственная точка рендера toast с классом типа.
    assert JSX.count("lp-toast-") >= 1
    assert re.search(r'className=\{?"?lp-toast lp-toast-"?\s*\+', JSX) or \
        re.search(r'"lp-toast lp-toast-"\s*\+', JSX)
    assert _norm('const showToast=(text,kind="info")') in jsx or \
        _norm("const showToast=(text,kind") in jsx


def test_toast_variants_in_css():
    """У toast есть визуальные варианты info/success/error на токенах."""
    for kind in ("info", "success", "error"):
        block = _block(CSS, f".lp-toast-{kind}::before")
        assert "var(--" in block


def test_export_csv_uses_toast_not_alert():
    """Ошибки и подсказки CSV-экспорта уходят в toast (раньше — alert())."""
    m = re.search(r"const exportCSV = useCallback\(async \(\) => \{(.*?)\}, \[", JSX, re.DOTALL)
    assert m, "не найдено тело exportCSV"
    body = m.group(1)
    assert "alert" not in body
    assert "showToast(" in body


def test_delete_parser_requires_modal_confirmation():
    """Деструктивное удаление парсера — через модальное подтверждение
    с описанием последствия, а не window.confirm()."""
    jsx = _norm(JSX)
    assert _norm('const[deleteConfirm,setDeleteConfirm]=useState(null);') in jsx
    # Кнопка «Удалить» больше не вызывает удаление напрямую.
    assert "deleteParser(p.parser_id)" not in JSX
    # В модалке описано последствие действия.
    m = re.search(r"deleteConfirm&&\((.{0,1200})", _norm(JSX))
    assert m, "не найдена разметка модалки подтверждения удаления"
    assert "будетудалён" in m.group(1) or "удалена" in m.group(1)
    assert "необратимо" in m.group(1)


# ── AC3: клавиатура, фокус, семантика слоёв ─────────────────────────────────

def test_focus_layer_hook_exists():
    """Общий механизм активного слоя: focus-trap по Tab, Escape, возврат
    фокуса на открывший контрол; события обрабатывает только верхний слой."""
    jsx = _norm(JSX)
    assert "FOCUSABLE_SEL" in jsx
    assert "useFocusLayer" in jsx
    assert _norm('e.key==="Escape"') in jsx
    assert _norm('e.key!=="Tab"') in jsx or _norm('e.key==="Tab"') in jsx
    # Возврат фокуса на элемент, открывший слой.
    assert "document.activeElement" in JSX
    assert "enabled(opener)" in JSX
    assert "enabled(fallback)" in JSX
    assert "restoreTarget.focus()" in JSX


def test_focus_layer_used_by_all_layers():
    """Ловушку фокуса используют off-canvas чат и оставшиеся модалки."""
    jsx = _norm(JSX)
    # Чат (off-canvas), вердикт, подтверждения удаления и отзыва роли.
    assert jsx.count("useFocusLayer(") >= 5  # объявление + 4 слоя
    assert _norm("useFocusLayer(chatModalOpen,") in jsx
    assert _norm("useFocusLayer(!!verdictModal,") in jsx
    assert _norm("useFocusLayer(!!deleteConfirm,") in jsx
    assert _norm("useFocusLayer(!!revokeConfirm,") in jsx


def test_chat_panel_modal_semantics_are_limited_to_compact_mode():
    """Панель — модальный dialog только в compact-режиме; desktop — complementary."""
    m = re.search(r"<aside[^>]*className=\"lp-sidebar\"[^>]*>", JSX)
    assert m, "не найден aside панели чата"
    tag = m.group(0)
    assert 'role={chatModalOpen ? "dialog" : "complementary"}' in tag
    assert 'aria-modal={chatModalOpen ? "true" : undefined}' in tag
    assert 'aria-labelledby="lp-chat-title"' in tag
    assert 'id="lp-chat-title"' in JSX


def test_chat_open_focuses_heading_not_textarea():
    """При открытии панели фокус — на заголовок панели (дизайн-контракт §4),
    а не в textarea."""
    jsx = _norm(JSX)
    assert "chatTitleRef" in jsx
    # Старый эффект «фокус в поле ввода при открытии» удалён.
    assert "chatInputRef.current.focus()" not in jsx
    # Заголовок фокусируем программно (tabIndex -1, в Tab-последовательности нет).
    assert re.search(r'id="lp-chat-title"[^>]*tabIndex=\{-1\}', JSX) or \
        re.search(r'tabIndex=\{-1\}[^>]*id="lp-chat-title"', JSX)


def test_modals_dialog_semantics():
    """Все оставшиеся модалки имеют role=dialog, aria-modal и подпись."""
    assert JSX.count('role="dialog"') >= 3
    assert JSX.count('aria-modal="true"') >= 3
    for labelledby in ("lp-verdict-title", "lp-confirm-title", "lp-revoke-title"):
        assert f'aria-labelledby="{labelledby}"' in JSX
        assert f'id="{labelledby}"' in JSX
    assert 'aria-labelledby="lp-parsers-title"' not in JSX


def test_focus_visible_ring_for_interactive_controls():
    """Видимое :focus-visible кольцо accent 2px у кнопок и ссылок."""
    css = CSS
    assert re.search(r"\.lp-btn:focus-visible\s*[,{]", css)
    # Проверяем тело именно того правила, что начинается с .lp-btn:focus-visible.
    block = re.search(r"\.lp-btn:focus-visible[^{}]*\{([^}]*)\}", css)
    assert block, "нет правила, начинающегося с .lp-btn:focus-visible"
    body = block.group(1)
    assert "outline: 2px solid var(--accent)" in body


def test_targets_at_least_28px():
    """Интерактивные мишени не меньше 28px."""
    css = CSS
    for sel in (".lp-btn", ".lp-btn-sm", ".lp-verdict-chip", ".lp-content-toggle"):
        assert re.search(r"min-(?:height|width):\s*28px", _block(css, sel)), (
            f"{sel}: нет мишени >= 28px"
        )


def test_chip_checkbox_keyboard_reachable():
    """Чекбокс чипа банков не display:none (иначе недоступен с клавиатуры):
    скрыт визуально, фокус виден через :focus-within на чипе."""
    block = _block(CSS, ".lp-chip input")
    assert "display: none" not in block
    assert "opacity: 0" in block or "clip" in block
    assert re.search(r"\.lp-chip:focus-within\s*\{[^}]*outline", CSS)


def test_chat_width_300px_between_1100_and_1399():
    """При 1100–1399px закреплённая панель чата — 300px (контракт §4/§5)."""
    body = _media_body(CSS, "(max-width: 1399px)")
    m = re.search(r"\.lp-layout-chat\s*\{([^}]*)\}", body)
    assert m, "в @media (max-width: 1399px) нет правила .lp-layout-chat"
    assert "300px" in m.group(1)


def test_escape_still_closes_chat():
    """Escape закрывает панель чата (через общий слой), разговор сохраняется."""
    assert "setChatOpen(false)" in JSX


def test_table_sort_keyboard_accessible():
    """Сортировка таблицы доступна с клавиатуры и передаёт aria-sort:
    интерактивные заголовки колонок — семантические и фокусируемые."""
    jsx = _norm(JSX)
    assert "sortableThProps" in jsx
    assert "aria-sort" in jsx
    assert JSX.count('className="lp-sort-button"') >= 7
    assert re.search(r'<button\s+type="button"\s+className="lp-sort-button"', JSX)
    assert not re.search(r"<th\b[^>]*\bon(?:Click|KeyDown)=", JSX)


# ── Инварианты quality-ревью 1.4 ─────────────────────────────────────────────
def test_record_details_use_native_buttons_not_interactive_rows():
    """Каталог раскрывает детали, а queue master-detail выбирается нативной кнопкой."""
    assert not re.search(r"<tr\b[^>]*\bon(?:Click|KeyDown)=", JSX)
    assert JSX.count('className="lp-row-details"') >= 1
    assert JSX.count("aria-expanded={expanded.has(r.record_id)}") >= 1
    assert JSX.count(
        "aria-controls={expanded.has(r.record_id) ? `lp-record-details-${r.record_id}` : undefined}"
    ) >= 1
    assert JSX.count("onClick={() => toggleContent(r.record_id)}") >= 1
    assert "lp-queue-card-active" in JSX
    assert 'aria-current={active ? "true" : undefined}' in JSX
    assert "onClick={() => setQueueSelectedId(record.record_id)}" in JSX
    # Вложенные checkbox, кнопка вердикта и ссылки не поднимают click выше себя.
    assert JSX.count("onClick={e => e.stopPropagation()}") >= 3


def test_catalog_checkbox_targets_are_native_labels_at_least_28px():
    """Выбор всех и одной записи доступен указателем по площади минимум 28px,
    не только через скрытую подпись размером 1px."""
    assert '<label className="lp-checkbox-hit" htmlFor="lp-select-all">' in JSX
    assert re.search(
        r'<label className="lp-checkbox-hit"\s+'
        r'htmlFor=\{`lp-select-record-\$\{r\.record_id\}`\}>',
        JSX,
    )
    assert not re.search(
        r'<label className="lp-sr-only" htmlFor=\{?`?lp-select-', JSX
    )
    block = _block(CSS, ".lp-checkbox-hit")
    assert "min-width: 28px" in block
    assert "min-height: 28px" in block


def test_clickable_backdrops_are_named_non_tabstop_native_buttons():
    """Фоны, закрывающие чат и модалки, — кнопки с именем, но не новая
    Tab-остановка за пределами активного dialog."""
    assert not re.search(
        r'<div\b[^>]*className="lp-(?:chat-backdrop|parsers-modal)"[^>]*onClick=',
        JSX,
    )
    chat = re.search(r'<button\b(?=[^>]*className="lp-chat-backdrop")[^>]*>', JSX)
    assert chat, "фон чата должен быть нативной кнопкой"
    assert 'type="button"' in chat.group(0)
    assert 'aria-label="Закрыть чат"' in chat.group(0)
    assert "tabIndex={-1}" in chat.group(0)
    assert "aria-hidden" not in chat.group(0)
    modal_buttons = re.findall(
        r'<button\b(?=[^>]*className="lp-modal-backdrop")'
        r'(?=[^>]*type="button")(?=[^>]*aria-label="Закрыть диалог")'
        r'(?=[^>]*tabIndex=\{-1\})[^>]*>',
        JSX,
    )
    assert len(modal_buttons) >= 3


def test_parser_running_badge_is_russian_without_changing_machine_predicate():
    """Пользователь видит русский статус, а условие по машинному is_running
    остаётся отдельным от локализованного текста."""
    badge = re.search(
        r'\{p\.is_running && <span className="lp-badge lp-badge-run">(.*?)</span>\}',
        JSX,
    )
    assert badge, "не найдена ветка статуса запущенного парсера"
    assert "выполняется" in badge.group(1)
    assert "running" not in badge.group(1)


def test_collapsed_row_button_omits_controls_for_absent_details_region():
    """При свёрнутой строке каталога aria-controls не указывает на отсутствующий region."""
    controls = (
        "aria-controls={expanded.has(r.record_id) "
        "? `lp-record-details-${r.record_id}` : undefined}"
    )
    assert JSX.count(controls) >= 1
    assert JSX.count("aria-expanded={expanded.has(r.record_id)}") >= 1
    assert JSX.count('id={`lp-record-details-${r.record_id}`}') >= 1


def _load_records_body() -> str:
    m = re.search(r"const loadRecords = useCallback\(async \(\) => \{(.*?)\}, \[", JSX, re.DOTALL)
    assert m, "не найдено тело loadRecords"
    return m.group(1)


def test_load_records_ignores_stale_filter_response_generation():
    """Поздний успех или сбой прежнего запроса не перезаписывает записи,
    ошибку и loading нового набора фильтров."""
    body = _load_records_body()
    assert "const recordsRequestRef = useRef(0);" in JSX
    assert "const requestGeneration = ++recordsRequestRef.current;" in body
    assert body.count("requestGeneration !== recordsRequestRef.current") >= 3
    assert re.search(
        r"finally\s*\{\s*if \(requestGeneration === recordsRequestRef\.current\) \{\s*"
        r"setLoading\(false\);",
        body,
    )


def _load_queue_body() -> str:
    m = re.search(r"const loadQueue = useCallback\(async \(\) => \{(.*?)\}, \[\]", JSX, re.DOTALL)
    assert m, "не найдено тело loadQueue"
    return m.group(1)


def test_queue_route_switches_synchronously_on_click():
    """Race-фикс: setView(\"queue\") — синхронно в openContext при клике;
    loadQueue не переключает вид из async-завершения fetch (поздний ответ
    не вырывает пользователя обратно в очередь)."""
    body = _load_queue_body()
    assert "setView" not in body
    m = re.search(r"const openContext = \(id\) => \{(.*?)\n  \};", JSX, re.DOTALL)
    assert m, "не найдено тело openContext"
    assert 'setView("queue")' in m.group(1)


def test_queue_error_clears_denied_state():
    """catch в loadQueue сбрасывает queueDenied: после 403 и последующей
    сетевой ошибки показывается поверхность ошибки с «Повторить», а не
    устаревший fail-closed экран."""
    body = _load_queue_body()
    m = re.search(r"\} catch \(e\) \{(.*?)\} finally", body, re.DOTALL)
    assert m, "в loadQueue нет блока catch"
    assert "setQueueDenied(false)" in m.group(1)


def test_focus_layer_onclose_not_stale():
    """useFocusLayer зовёт onClose через ref, обновляемый каждый рендер:
    эффект с deps [active] не держит устаревшее замыкание."""
    m = re.search(r"function useFocusLayer\(.*?\n\}", JSX, re.DOTALL)
    assert m, "не найдено тело useFocusLayer"
    hook = m.group(0)
    assert "onCloseRef" in hook
    assert "onCloseRef.current = onClose" in hook
    assert "onCloseRef.current()" in hook

def _load_parsers_body() -> str:
    m = re.search(r"const loadParsers = useCallback\(async \(\) => \{(.*?)\}, \[\]\);", JSX, re.DOTALL)
    assert m, "не найдено тело loadParsers"
    return m.group(1)


def test_parser_loading_empty_and_error_states_are_distinguishable():
    """Рабочая поверхность парсеров не маскирует ошибку под пустой список."""
    jsx = _norm(JSX)
    assert _norm("const[parsersLoading,setParsersLoading]=useState(false);") in jsx
    assert _norm("const[parsersError,setParsersError]=useState(null);") in jsx
    body = _load_parsers_body()
    assert "setParsersLoading(true)" in body
    assert "if (!r.ok)" in body
    assert "catch" in body and "setParsersError(" in body
    assert not re.search(r"catch\s*\{\s*\}", body)
    start = JSX.index('<section className="lp-source-list"')
    end = JSX.index("</section>\n          </section>", start)
    markup = JSX[start:end]
    assert "Загрузка парсеров…" in markup
    assert "Не удалось загрузить парсеры" in markup
    assert "Повторить" in markup
    assert "Парсеры не созданы." in markup
    assert "onClick={loadParsers}" in markup


def test_export_csv_reports_network_failures_in_error_toast():
    """Сетевой сбой экспорта CSV виден через существующий error-toast."""
    m = re.search(r"const exportCSV = useCallback\(async \(\) => \{(.*?)\}, \[", JSX, re.DOTALL)
    assert m, "не найдено тело exportCSV"
    catch = re.search(r"catch \(e\) \{(.*?)\}", m.group(1), re.DOTALL)
    assert catch, "exportCSV не перехватывает сетевой сбой"
    assert "showToast(" in catch.group(1)
    assert '"error"' in catch.group(1)


def test_queue_empty_state_has_reset_action():
    """Пустая очередь даёт безопасное действие сброса/повторной загрузки."""
    m = re.search(r"queueRecords\.length === 0 \? \((.*?)\) : \(", JSX, re.DOTALL)
    assert m, "не найдена ветка пустой очереди"
    assert "Сбросить" in m.group(1)
    assert "onClick={loadQueue}" in m.group(1)


def test_icon_controls_and_inputs_have_russian_accessible_names():
    """Иконки и поля имеют русские доступные имена через label или aria-label."""
    chat_send = re.search(r'<button\s+className="lp-chat-send"(.*?)</button>', JSX, re.DOTALL)
    assert chat_send and 'aria-label="Отправить сообщение"' in chat_send.group(1)
    for control_id in (
        "lp-filter-text",
        "lp-filter-from",
        "lp-filter-to",
        "lp-chat-input",
        "lp-parser-url",
        "lp-parser-description",
    ):
        assert f'htmlFor="{control_id}"' in JSX
        assert f'id="{control_id}"' in JSX
    assert 'id="lp-filter-verdict"' not in JSX
    assert 'id="lp-filter-status"' not in JSX
    assert 'aria-label="Каталог показывает только лазейки"' in JSX
    assert 'aria-label="Каталог показывает подтверждённые и предварительные записи"' in JSX
    assert "lp-bulk-comment" not in JSX
    assert 'htmlFor="lp-select-all"' in JSX
    assert 'id="lp-select-all"' in JSX
    assert 'htmlFor={`lp-select-record-${r.record_id}`}' in JSX
    assert 'id={`lp-select-record-${r.record_id}`}' in JSX
    for value in ("new", "classified", "exported"):
        assert f'<option value="{value}">' not in JSX


def test_new_controls_and_links_have_hit_targets_and_visible_focus():
    """Новые кнопки и ссылки не меньше 28px; outline:none не скрывает фокус."""
    for selector in (
        ".lp-row-details",
        ".lp-sort-button",
        ".lp-cell-url a",
        ".lp-content-head a",
    ):
        block = _block(CSS, selector)
        assert "min-width: 28px" in block
        assert "min-height: 28px" in block
    parser_targets = re.search(
        r"\.lp-parser-targets a,\s*\.lp-parser-target-plain\s*\{([^}]*)\}",
        CSS,
    )
    assert parser_targets
    assert "min-width: 28px" in parser_targets.group(1)
    assert "min-height: 28px" in parser_targets.group(1)
    for selector in (
        ".lp-question-other textarea",
        ".lp-parsers-create input",
        ".lp-parser-edit input",
        ".lp-mark-comment",
        ".lp-verdict-field textarea",
    ):
        assert f"{selector}:focus-visible" in CSS


def test_toast_timer_cleared_on_unmount():
    """Таймер toast очищается при размонтировании (setState после unmount)."""
    effects = re.findall(
        r"useEffect\(\(\) => \(\) => \{(.*?)\}, \[\]\);",
        JSX,
        re.DOTALL,
    )
    effect = next((body for body in effects if "clearTimeout(toastTimerRef.current)" in body), None)
    assert effect
    assert "URL.revokeObjectURL(csvUrlRef.current)" in effect


def _theme_token(selector: str, token: str) -> str:
    """Значение CSS-токена в конкретном блоке темы."""
    block = _block(CSS, selector)
    match = re.search(rf"{re.escape(token)}\s*:\s*([^;]+);", block)
    assert match, f"в {selector} не определён токен {token}"
    return match.group(1).strip()


def _oklch_luminance(value: str) -> float:
    """Относительная яркость непрозрачного CSS oklch без gamut mapping."""
    match = re.fullmatch(r"oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)\s*\)", value)
    assert match, f"ожидался непрозрачный oklch-токен, получено {value!r}"
    lightness, chroma, hue = (float(part) for part in match.groups())
    lightness /= 100
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    l_prime = lightness + 0.3963377774 * a + 0.2158037573 * b
    m_prime = lightness - 0.1055613458 * a - 0.0638541728 * b
    s_prime = lightness - 0.0894841775 * a - 1.2914855480 * b
    l = l_prime**3
    m = m_prime**3
    s = s_prime**3
    red = 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s
    green = -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s
    blue = -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first: float, second: float) -> float:
    """Контраст WCAG из двух относительных яркостей."""
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


def test_dark_solid_accent_foreground_meets_wcag():
    """Текст на сплошном accent в тёмной теме имеет контраст не ниже 4.5:1."""
    dark_accent = _theme_token("html.dark", "--accent")
    dark_on_accent = _theme_token("html.dark", "--on-accent-solid")
    ratio = _contrast_ratio(_oklch_luminance(dark_accent), _oklch_luminance(dark_on_accent))
    assert ratio >= 4.5, f"контраст тёмного accent равен {ratio:.2f}:1"
    for selector in (".lp-chat-send", ".lp-btn-primary", ".lp-phase-active", ".lp-mark-btn-bad"):
        block = _block(CSS, selector)
        assert "background: var(--accent)" in block
        assert "color: var(--on-accent-solid)" in block


def test_dark_solid_danger_foreground_meets_wcag():
    """Текст на сплошном danger в тёмной теме имеет контраст не ниже 4.5:1."""
    dark_danger = _theme_token("html.dark", "--neg")
    dark_on_danger = _theme_token("html.dark", "--on-danger-solid")
    ratio = _contrast_ratio(_oklch_luminance(dark_danger), _oklch_luminance(dark_on_danger))
    assert ratio >= 4.5, f"контраст тёмного danger равен {ratio:.2f}:1"
    block = _block(CSS, ".lp-btn-danger")
    assert "background: var(--neg)" in block
    assert "color: var(--on-danger-solid)" in block


def test_focus_trap_cycles_from_programmatic_title_on_shift_tab():
    """Shift+Tab с title tabindex=-1 остаётся в off-canvas слое."""
    hook_match = re.search(r"function useFocusLayer\(.*?\n\}", JSX, re.DOTALL)
    assert hook_match, "не найдено тело useFocusLayer"
    hook = hook_match.group(0)
    assert re.search(
        r"if\s*\(\s*!items\.includes\(cur\)\s*\)\s*\{\s*"
        r"e\.preventDefault\(\);\s*\(e\.shiftKey\s*\?\s*last\s*:\s*first\)\.focus\(\);\s*return;",
        hook,
        re.DOTALL,
    )


def _css_variables(block: str) -> dict[str, str]:
    """Собирает CSS-переменные из одного блока темы."""
    return {
        name: value.strip()
        for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)
    }


def _badge_theme_palette(is_dark: bool) -> dict[str, str]:
    """Итоговая палитра badge после каскада :root и html.dark."""
    palette: dict[str, str] = {}
    for block in re.findall(r":root\s*\{([^}]*)\}", CSS):
        palette.update(_css_variables(block))
    if is_dark:
        palette.update(_css_variables(_block(CSS, "html.dark")))
    return palette


def _resolve_badge_token(palette: dict[str, str], token: str) -> str:
    """Рекурсивно разворачивает только var(--token) в итоговой палитре."""
    seen: set[str] = set()
    while True:
        assert token not in seen, f"цикл CSS-переменных для {token}"
        seen.add(token)
        value = palette[token]
        ref = re.fullmatch(r"var\((--[\w-]+)\)", value)
        if not ref:
            return value
        token = ref.group(1)


def test_parser_badge_tokens_meet_wcag_in_both_themes():
    """Текст и фон error/running/attention badge имеют контраст >= 4.5:1
    в светлой и тёмной темах; alpha-подложки не допускаются в этой паре."""
    badges = {
        ".lp-badge-err": ("--status-error-fg", "--status-error-bg"),
        ".lp-badge-run": ("--status-running-fg", "--status-running-bg"),
        ".lp-badge-attn": ("--status-warning-fg", "--status-warning-bg"),
    }
    for selector, (foreground, background) in badges.items():
        block = _block(CSS, selector)
        assert f"color: var({foreground})" in block
        assert f"background: var({background})" in block
    for theme, is_dark in (("light", False), ("dark", True)):
        palette = _badge_theme_palette(is_dark)
        for selector, (foreground, background) in badges.items():
            fg = _resolve_badge_token(palette, foreground)
            bg = _resolve_badge_token(palette, background)
            ratio = _contrast_ratio(_oklch_luminance(fg), _oklch_luminance(bg))
            assert ratio >= 4.5, f"{theme}: {selector} = {ratio:.2f}:1"


def test_load_parsers_ignores_stale_poll_or_retry_response_generation():
    """Поздний polling/retry ответ не перезаписывает актуальные parsers,
    error и loading следующего запроса."""
    body = _load_parsers_body()
    assert "const parsersRequestRef = useRef(0);" in JSX
    assert "const requestGeneration = ++parsersRequestRef.current;" in body
    assert body.count("requestGeneration !== parsersRequestRef.current") >= 3
    assert re.search(
        r"finally\s*\{\s*if \(requestGeneration === parsersRequestRef\.current\) \{\s*"
        r"setParsersLoading\(false\);",
        body,
    )


def _parser_action_body(name: str) -> str:
    m = re.search(rf"const {name} = async \([^)]*\) => \{{(.*?)\n  \}};", JSX, re.DOTALL)
    assert m, f"не найдено тело {name}"
    return m.group(1)


def test_parser_request_emits_one_typed_toast_for_each_remote_outcome():
    """Заявка на источник сообщает об успешной регистрации либо ошибке сети/API."""
    body = _parser_action_body("createParserRequest")
    assert body.count("showToast(") == 2
    assert '"success"' in body
    assert '"error"' in body
    assert "if (!r.ok)" in body
    assert "catch (e)" in body


def test_internal_trust_is_absent_and_publication_date_is_sortable():
    """Внутренний trust не виден, а дата публикации остаётся сортируемой."""
    assert "trust_score" not in JSX
    assert "Надёжность" not in JSX
    assert "Trust" not in JSX
    button = re.search(
        r'toggleSort\("published_at"\)\}>(.*?)</button>',
        JSX,
        re.DOTALL,
    )
    assert button, "не найдена кнопка сортировки published_at"
    assert "Дата публикации" in button.group(1)
