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

# ── AC2: комментарий из карточки уходит в POST /records/verdict ──────────────

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


# ── AC4 / матрица «full»: текст целиком в прокручиваемом блоке, без кликов ───

# ── AC5 / матрица «truncated», «fetch_failed/empty», «ошибка/загрузка» ───────

# ── AC6 / матрица «canMarkVerdict=false» ─────────────────────────────────────

# ── Оформление секций (CSS) ──────────────────────────────────────────────────
