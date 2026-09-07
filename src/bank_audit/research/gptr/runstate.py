"""Состояние одного прогона — вместо словарей, общих на весь процесс.

ЗАЧЕМ. Прочитанные страницы, причины нечитаемости, метаданные отзывов и
подзапросы планировщика жили модульными словарями. На проде это тихая порча
данных: пока аудитор A собирает источники, аудитор B начинает свой прогон,
делает `clear()` — и A извлекает факты из ЧУЖИХ страниц, сверяет числа по
чужому корпусу и получает приложение с источниками другого вопроса. Ни одной
ошибки в логе. Отдельно: словарь причин нечитаемости не очищался вовсе, и
«Честные пробелы» копили URL из прошлых вопросов за всё время жизни процесса.

Состояние передаётся через contextvars. Одна тонкость: gpt-researcher зовёт
`scraper.scrape` через `run_in_executor`, а туда контекст НЕ переносится.
Поэтому состояние связывается со скрапером в момент выбора класса
(`get_scraper`), который вызывается ещё в асинхронном контексте прогона.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class RunState:
    """Всё, что принадлежит одному вопросу и не должно течь в соседний."""
    pages: dict[str, str] = field(default_factory=dict)          # url → текст
    unreadable: dict[str, str] = field(default_factory=dict)     # url → причина
    review_meta: dict[str, dict] = field(default_factory=dict)   # url → банк/дата
    subqueries: list[str] = field(default_factory=list)          # план поиска
    page_dates: dict[str, str] = field(default_factory=dict)     # url → ISO-дата

    def note_page(self, url: str, text: str) -> None:
        self.pages[url] = text
        # Страница, прочитанная со второй попытки, больше не «непрочитанная».
        self.unreadable.pop(url, None)

    def note_unreadable(self, url: str, reason: str) -> None:
        self.unreadable[url] = reason


_current: ContextVar[RunState | None] = ContextVar("auditlens_run", default=None)


def new_run() -> RunState:
    """Начинает прогон и делает его состояние текущим."""
    state = RunState()
    _current.set(state)
    return state


def bind(state: RunState) -> None:
    """Делает состояние текущим ПРЯМО СЕЙЧАС.

    Нужно потому, что поток событий — асинхронный генератор, а установка
    contextvar внутри такого генератора не переживает `yield`: каждый шаг
    исполняется в контексте вызывающего. Из-за этого планировщик получал
    пустой список подзапросов, а подсказки о принадлежности отзывов терялись.
    Поэтому состояние переустанавливается в том же шаге, где им пользуются, —
    между bind() и вызовом не должно быть ни одного yield.
    """
    _current.set(state)


def current() -> RunState:
    """Состояние текущего прогона.

    Вне прогона (тесты, разовые вызовы скрапера) заводим одноразовое: так
    вызывающему не нужно знать про контекст, а утечь между вопросами нечему.
    """
    state = _current.get()
    if state is None:
        state = RunState()
        _current.set(state)
    return state
