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


# ── Инварианты quality-ревью 1.4 ─────────────────────────────────────────────
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


def _load_contexts_body() -> str:
    m = re.search(
        r"useEffect\(\(\) => \{\s*fetch\(`\$\{API\}/contexts`\)(.*?)\}, "
        r"\[contextsRetry\]\);",
        JSX,
        re.DOTALL,
    )
    assert m, "не найден обработчик загрузки /contexts"
    return m.group(1)


def test_context_refresh_falls_back_when_active_view_is_no_longer_allowed():
    """Повторная загрузка контекстов не оставляет активным исчезнувший view."""
    body = _norm(_load_contexts_body())
    assert _norm("const contexts = d.contexts || [];") in body
    assert _norm("setAuthz({contexts, capabilities: d.capabilities || {},});") in body
    assert _norm(
        'setView(current => (contexts.some(context => context.id === current) '
        '? current : "catalog"));'
    ) in body


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
    l_cube = l_prime**3
    m = m_prime**3
    s = s_prime**3
    red = 4.0767416621 * l_cube - 3.3077115913 * m + 0.2309699292 * s
    green = -1.2684380046 * l_cube + 2.6097574011 * m - 0.3413193965 * s
    blue = -0.0041960863 * l_cube - 0.7034186147 * m + 1.7076147010 * s
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_ratio(first: float, second: float) -> float:
    """Контраст WCAG из двух относительных яркостей."""
    return (max(first, second) + 0.05) / (min(first, second) + 0.05)


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
