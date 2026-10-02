"""Название отчёта ИИ-аналитика.

Раньше названием был сам вопрос, обрезанный до 80 знаков: в истории лежали
отчёты «да» и «да, сведи в таблицу», а обложка PDF начиналась с «Сделай
сравнительный анализ по продукту… и…» (аудит PDF 29.09).

Отчёт (deep) получает название из брифа — тем же вызовом модели, что строит
главный ответ. Быстрым ответам и уточнениям название составляет быстрая модель
по цепочке вопросов сессии и началу ответа; модель недоступна — эвристика по
вопросу: без повелительного глагола, с заглавной буквы, по границе слова.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger(__name__)

TITLE_MAX = 90

# Глаголы-команды, с которых аудитор начинает вопрос: в названии они лишние.
_IMPERATIVE = re.compile(
    r"^(?:пожалуйста[,\s]+)?(?:сделай|сделайте|проведи|проведите|подготовь|подготовьте|"
    r"составь|составьте|собери|соберите|сравни|сравните|проанализируй|проанализируйте|"
    r"найди|найдите|выведи|выведите|покажи|покажите|расскажи|расскажите|опиши|опишите|"
    r"оцени|оцените|изучи|изучите|исследуй|исследуйте|разбери|разберите|дай|дайте|"
    r"посмотри|посмотрите|проверь|проверьте|сведи|сведите|объясни|объясните|"
    r"подбери|подберите|выясни|выясните|определи|определите)\b[\s:,]*",
    re.I)
_FILLER = re.compile(r"^(?:мне|нам|пожалуйста|подробн\w*|кратк\w*|полн\w*)\b[\s,]*", re.I)
_BAD_START = re.compile(r"^(?:отч[её]т|анализ|исследование|аналитика)\s*[:—-]\s*", re.I)
# «да», «нет, нужны тарифы…», «ок» — ответ на уточнение, а не тема отчёта.
_SHORT_REPLY = re.compile(r"^(?:да|нет|ок|окей|давай|ага|угу|хорошо|верно)\b", re.I)


def clean_title(raw: str | None) -> str:
    """Название из ответа модели: одна строка, без кавычек, точки в конце и
    служебных слов; обрезка по границе слова."""
    s = re.sub(r"\s+", " ", str(raw or "")).strip().rstrip(".").strip()
    # Кавычки снимаются, только если ими обёрнуто всё название: «Своё дело»
    # в начале — часть названия (29.09 отрезалась открывающая).
    while len(s) >= 2 and s[0] in "«\"“„'" and s[-1] in "»\"”'" \
            and s.count("«") <= 1 and s.count("»") <= 1:
        s = s[1:-1].strip().rstrip(".").strip()
    s = _BAD_START.sub("", s).strip()
    if len(s) > TITLE_MAX:
        cut = s[:TITLE_MAX].rsplit(" ", 1)[0].rstrip(",;:—- ")
        s = cut + "…"
    return s[:1].upper() + s[1:] if s else ""


def heuristic_title(question: str | None) -> str:
    """Название без модели: вопрос без глагола-команды, первое предложение."""
    q = re.sub(r"\s+", " ", str(question or "")).strip()
    if not q:
        return "Без названия"
    q = _IMPERATIVE.sub("", q)
    q = _FILLER.sub("", q)
    q = re.split(r"(?<=[.?!])\s", q)[0].rstrip("?.! ")
    return clean_title(q) or "Без названия"


def is_default_title(title: str | None, question: str | None) -> bool:
    """Название — это просто начало вопроса (так их ставили до 29.09)."""
    q = " ".join(str(question or "").split())
    t = " ".join(str(title or "").split())
    return not t or t in (q[:80].strip(), q or "Без названия")


_SYSTEM = (
    "Ты даёшь название аналитическому отчёту для аудитора банка — как заголовок "
    "в истории отчётов и на обложке. 3–9 слов, по-русски, о предмете: продукт, "
    "банки, тема, период. Без глаголов-команд из вопроса («Сделай», «Сравни»), "
    "без слов «отчёт», «анализ», «исследование», без кавычек и точки в конце. "
    "Чисел, которых нет в вопросе и ответе, не пиши. Если последний вопрос — "
    "уточнение («да», «сведи в таблицу»), назови тему всей беседы. "
    "Примеры: «Эквайринг для малого бизнеса: Сбер против Т-Банка и ВТБ», "
    "«Жалобы на чарджбэк: причины всплеска», «Тарифы НСПК для эквайера и эмитента». "
    "Ответь только названием."
)


def _client():
    from openai import AsyncOpenAI
    from .llm_utils import _patch_client_reasoning_effort
    return _patch_client_reasoning_effort(AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")),
        timeout=float(os.getenv("REPORT_TITLE_TIMEOUT_S", "25")), max_retries=1))


async def llm_title(question: str, answer: str = "", prior: list[str] | None = None,
                    *, client=None) -> str | None:
    """Название быстрой моделью. Сбой или пустой ответ — None (решает вызывающий).

    temperature не передаём: часть моделей её отвергает, и запрос уходил бы в
    ошибку (урок 21.09, см. llm_utils)."""
    from .analyst import fast_model
    chain = [q for q in (prior or []) if q and q.strip()][-4:]
    user = "\n".join([
        *(["Предыдущие вопросы беседы:"] + [f"- {q.strip()[:300]}" for q in chain] if chain else []),
        f"Вопрос: {str(question or '').strip()[:600]}",
        "Начало ответа:",
        re.sub(r"\[\[(?:VIZ|CHART):\d+\]\]", "", str(answer or ""))[:1500],
    ])
    try:
        resp = await (client or _client()).chat.completions.create(
            model=os.getenv("REPORT_TITLE_MODEL") or fast_model(),
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            max_tokens=60)
        title = clean_title((resp.choices[0].message.content or "").splitlines()[0]
                            if resp.choices[0].message.content else "")
    except Exception as e:  # noqa: BLE001 — название не должно ронять отчёт
        log.warning("название отчёта: модель не ответила (%s)", type(e).__name__)
        return None
    if len(title) < 4 or _SHORT_REPLY.match(title):
        return None
    return title


async def title_for(question: str, answer: str = "", prior: list[str] | None = None) -> str:
    """Название с запасным вариантом: модель → эвристика по вопросу (по первому
    вопросу беседы, если последний — короткое уточнение)."""
    t = await llm_title(question, answer, prior)
    if t:
        return t
    base = question
    if _SHORT_REPLY.match(str(question or "").strip()) and prior:
        base = prior[0]
    return heuristic_title(base)
