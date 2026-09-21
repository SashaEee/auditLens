"""Карточка проверки очереди: комментарий участника ЦК и полный текст.

Спека: docs/loophole/bmad/implementation-artifacts/
spec-verification-card-comment-and-full-text.md

Фронт без сборки и без UI-стенда — проверки текстовые по образцу
test_adaptive_context_routes.py (_jsx()/_css()/_norm()/_block()).

Покрытие сценариев I/O-матрицы спеки:
- full              → test_fulltext_always_expanded_without_toggle_button
- truncated         → test_content_states_keep_card_usable
- fetch_failed/empty→ test_content_states_keep_card_usable
- ошибка/загрузка   → test_content_states_keep_card_usable
- canMarkVerdict=false → test_comment_field_gated_and_fulltext_available
- смена записи      → test_comment_resets_and_content_reloads_on_record_change
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


def _queue_card() -> str:
    """Разметка карточки проверки (article.lp-queue-detail)."""
    m = re.search(r'<article className="lp-queue-detail".*?</article>', _jsx(), re.DOTALL)
    assert m, "не найдена карточка проверки article.lp-queue-detail"
    return m.group(0)


def _queue_selection_effect() -> str:
    """Тело effect'а автозагрузки контента и сброса комментария."""
    m = re.search(
        r"const queueSelectedRecordId = queueSelected \? queueSelected\.record_id : null;\n"
        r"  useEffect\(\(\) => \{(.*?)\n  \}, \[queueSelectedRecordId\]\);",
        _jsx(),
        re.DOTALL,
    )
    assert m, "не найден effect смены выбранной записи очереди"
    return m.group(1)


def _load_content_body() -> str:
    """Тело loadContent — общей для каталога и очереди загрузки в contentCache."""
    m = re.search(r"const loadContent = \(id\) => \{(.*?)\n  \};", _jsx(), re.DOTALL)
    assert m, "не найдено тело loadContent"
    return m.group(1)


def _record_content_body() -> str:
    """Тело renderRecordContent (включая режим opts.alwaysFull)."""
    m = re.search(r"const renderRecordContent = \(r[^)]*\) => \{(.*?)\n  \};", _jsx(), re.DOTALL)
    assert m, "не найдено тело renderRecordContent"
    return m.group(1)


# ── AC1: в самом низу карточки — комментарий участника ЦК, ниже полный текст ──

def test_comment_and_fulltext_are_last_sections_of_card():
    """Секции внизу карточки идут в порядке: причина классификатора →
    действия → поле комментария участника ЦК → полный текст записи."""
    card = _queue_card()
    positions = [
        card.index("lp-queue-reason"),
        card.index("lp-queue-detail-actions"),
        card.index("lp-queue-comment"),
        card.index("lp-queue-fulltext"),
    ]
    assert positions == sorted(positions), "секции карточки нарушают порядок"
    # Полный текст в карточке рендерится тем же рендерером в режиме always-full.
    assert _norm("renderRecordContent(queueSelected, {alwaysFull: true})") in _norm(card)


# ── AC2: комментарий из карточки уходит в POST /records/verdict ──────────────

def test_card_comment_shares_mark_comment_state_with_modal():
    """Поле карточки и поле модалки — одно состояние markComment."""
    card = _queue_card()
    assert _norm('value={markComment}') in _norm(card)
    assert _norm('onChange={e => setMarkComment(e.target.value)}') in _norm(card)
    jsx = _norm(_jsx())
    # Поле модалки вердикта сохранено и привязано к тому же состоянию.
    assert _norm('id="lp-mark-comment"') in jsx
    assert jsx.count(_norm('onChange={e => setMarkComment(e.target.value)}')) == 2


def test_card_comment_survives_opening_verdict_modal():
    """Открытие «Проверить вердикт» не сбрасывает комментарий; выбор в модалке
    уходит в markVerdict (POST /records/verdict) как comment."""
    card = _queue_card()
    assert _norm('onClick={() => setVerdictModal({record: queueSelected})}') in _norm(card)
    # Прежний объединённый обработчик со сбросом удалён из карточки очереди
    # (в каталоге сброс при открытии модалки сохранён намеренно — там поле
    # карточки очереди нет, и чужой черновик не должен попадать в вердикт).
    assert _norm('{ setMarkComment("");') not in _norm(card)
    assert _norm("markVerdict([rec.record_id], val, markComment.trim())") in _norm(_jsx())


def test_catalog_verdict_button_resets_foreign_draft():
    """Кнопка вердикта в каталоге сбрасывает черновик карточки очереди.

    Черновик в markComment теперь живёт постоянно (поле карточки очереди),
    поэтому сброс в каталожном обработчике стал несущей защитой: без него
    черновик записи A попал бы в вердикт записи B, открытой из каталога.
    """
    jsx = _norm(_jsx())
    assert _norm('() => { setMarkComment(""); setVerdictModal({record: r}); }') in jsx


def test_comment_resets_and_content_reloads_on_record_change():
    """Смена выбранной записи: комментарий пуст, полный текст — новой записи."""
    effect = _queue_selection_effect()
    assert "setMarkComment(\"\");" in effect
    assert "loadContent(queueSelectedRecordId);" in effect
    # Зависимость — именно идентификатор выбранной записи.
    assert "[queueSelectedRecordId]" in _jsx()


def test_content_cache_prevents_duplicate_requests():
    """Повторный выбор записи не повторяет сетевой запрос (guard по кэшу),
    каталог и очередь используют одну и ту же загрузку contentCache."""
    body = _load_content_body()
    assert "if (contentCache[id]) return;" in body
    assert _norm("fetch(`${API}/records/${id}/content`)") in _norm(body)
    # toggleContent каталога переиспользует loadContent, а не дублирует его.
    toggle = re.search(r"const toggleContent = \(id\) => \{(.*?)\n  \};", _jsx(), re.DOTALL)
    assert toggle and "loadContent(id);" in toggle.group(1)


# ── AC4 / матрица «full»: текст целиком в прокручиваемом блоке, без кликов ───

def test_fulltext_always_expanded_without_toggle_button():
    """alwaysFull: блок сразу с классом lp-content-body-full, кнопка
    «Развернуть полностью» в карточке не показывается."""
    body = _record_content_body()
    assert "const showFull = alwaysFull || fullView.has(r.record_id);" in body
    # Кнопка разворота — только вне режима always-full.
    assert _norm("{!alwaysFull && !failed && (d.raw_text_len || 0) > 2000 && (") in _norm(body)
    # Прокручиваемый контейнер: overflow-y внутри блока (селектор на границе
    # строки — чтобы не цеплять карточную переопределённую высоту).
    base = re.search(r"(?m)^\.lp-content-body-full\s*\{([^}]*)\}", _css())
    assert base, "в CSS не найдено базовое правило .lp-content-body-full"
    assert "overflow-y: auto" in base.group(1)
    override = re.search(
        r"\.lp-queue-fulltext \.lp-content-body-full\s*\{([^}]*)\}", _css()
    )
    assert override, "нет карточного ограничения высоты .lp-content-body-full"
    assert "max-height" in override.group(1)


# ── AC5 / матрица «truncated», «fetch_failed/empty», «ошибка/загрузка» ───────

def test_content_states_keep_card_usable():
    """Состояния контента читаемы в карточке: загрузка и ошибка — со ссылкой
    на источник, обрезан — с бейджем «до N КБ», сбой — с пояснением."""
    body = _record_content_body()
    loading = re.search(r"if \(!entry \|\| entry\.loading\) \{(.*?)\n    \}", body, re.DOTALL)
    error = re.search(r"if \(entry\.error\) \{(.*?)\n    \}", body, re.DOTALL)
    assert loading and "Загрузка контента…" in loading.group(1)
    assert loading and "{sourceLink}" in loading.group(1)
    assert error and "Ошибка загрузки:" in error.group(1)
    assert error and "{sourceLink}" in error.group(1)
    assert "до ${sizeKb} КБ" in body  # бейдж обрезанного контента
    assert "Полный контент не удалось загрузить" in body  # пояснение сбоя


# ── AC6 / матрица «canMarkVerdict=false» ─────────────────────────────────────

def test_comment_field_gated_and_fulltext_available():
    """Без права вердикта поле комментария не рендерится, полный текст
    остаётся доступным (не завёрнут в гейт)."""
    jsx = _norm(_jsx())
    assert _norm('{canMarkVerdict && (<section className="lp-verdict-field lp-queue-comment"') in jsx
    assert _norm('{canMarkVerdict && (<section className="lp-queue-fulltext"') not in jsx
    card = _queue_card()
    assert _norm('<section className="lp-queue-fulltext"') in _norm(card)


# ── Оформление секций (CSS) ──────────────────────────────────────────────────

def test_card_sections_have_styles():
    """Секции карточки оформлены: отступы комментария и полного текста,
    поле ввода наследует стиль .lp-verdict-field."""
    css = _css()
    assert "margin-top: 14px" in _block(css, ".lp-queue-comment")
    assert "margin-top: 14px" in _block(css, ".lp-queue-fulltext")
    assert "font-size: 0.78rem" in _block(css, ".lp-queue-fulltext h3")
    # Разметка поля карточки переиспользует класс поля модалки.
    assert _norm('className="lp-verdict-field lp-queue-comment"') in _norm(_jsx())
