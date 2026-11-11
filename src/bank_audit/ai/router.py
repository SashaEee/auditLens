"""Один вход: глубину ответа выбирает помощник, а не переключатель режимов.

Раньше аудитор до вопроса решал сам, «быстрый» ему нужен ответ или «глубокий».
Это решение о внутреннем устройстве помощника, а не о задаче: на справочный
вопрос запускали отчёт на пять минут, на сравнение банков получали три абзаца,
а переключатель ещё и «залипал» (обратная связь трёх терробанков). Теперь поле
ввода одно. По реплике и ходу разговора модель решает, чем отвечать:

    answer — короткая справка: один факт или небольшой список, меньше минуты;
    report — отчёт с источниками: материал нужно собрать и сверить, 3–5 минут;
    reply  — реплика к уже данному ответу, новый сбор не нужен;
    deck   — презентация по отчёту, который уже есть в разговоре.

Решение показывается человеку одной строкой, и он меняет его одним нажатием
(«Нужен отчёт» / «Хватит короткого ответа») — выбирать заранее не приходится.
Слова аудитора главнее догадок модели: «коротко» → справка, «отчёт» → отчёт.

Сбой или долгий ответ модели вопрос не задерживают: остаются прежние правила
по словам (by_rules) — они же страхуют вызовы без маршрута.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

ROUTES = ("answer", "report", "reply", "deck")
_REPORT_LEN = 2500          # ответ длиннее — это отчёт (как и раньше в разборе истории)


@dataclass(frozen=True)
class Route:
    route: str               # answer | report | reply | deck
    by: str                  # model — решила модель; rules — правила по словам; user — человек
    ms: int = 0
    reason: str = ""         # коротко, почему (для журнала и «Пульса»)


def enabled() -> bool:
    return os.getenv("AI_ROUTER", "1").strip().lower() in ("1", "true", "yes", "on")


def model_for() -> str:
    return (os.getenv("AI_ROUTER_MODEL") or os.getenv("LLM_MODEL_SMART")
            or os.getenv("LLM_MODEL_NAME") or "")


def _timeout() -> float:
    try:
        return max(2.0, float(os.getenv("AI_ROUTER_TIMEOUT", "7") or 7))
    except ValueError:
        return 7.0


SYSTEM = """Ты — диспетчер ИИ-помощника AuditLens. Им пользуются внутренние аудиторы банка. По новой реплике аудитора и ходу разговора реши, ЧЕМ на неё отвечать. На сам вопрос не отвечай.

Четыре способа ответа:
• answer — короткая справка, меньше минуты. Нужен один факт или небольшой список: ставка, лимит, комиссия, срок, определение, «что такое», «есть ли», «сколько», «когда», «где посмотреть», свежая новость, один показатель по одному банку, несколько примеров жалоб или отзывов.
• report — отчёт с источниками, несколько минут. Материал нужно собрать из нескольких мест и сверить: сравнение банков или продуктов; обзор продукта или сервиса целиком (условия, тарифы, ограничения, риски); разбор жалоб с причинами и динамикой; требования и нормы по теме; положение на рынке; разбор события с последствиями; план проверки; «исследуй», «разберись», «проанализируй», «подготовь справку, обзор, отчёт».
• reply — реплика к ответу, который уже есть в разговоре, новый сбор не нужен: согласие («да», «давай»); просьба переоформить (короче, таблицей, списком, в файл Excel или Word); вопрос о сказанном («откуда цифра», «что значит», «почему так», «покажи остальные»); мелкое уточнение по уже собранному.
• deck — просьба сделать презентацию или слайды по отчёту, который уже есть в разговоре.

Правила:
1. Прямо сказанное желание аудитора главнее всего остального. «Коротко», «кратко», «в двух словах», «быстро», «без отчёта» → answer. «Отчёт», «исследование», «подробно», «детально», «полный разбор», «справку для дела» → report.
2. Не завышай: вопрос, на который честно отвечает один абзац, — answer, даже если тема серьёзная. Не занижай: если нужно сопоставить несколько банков, продуктов или периодов либо сложить картину из разных источников — report.
3. После отчёта. Вопрос о его содержании или просьба переоформить — reply. Новый предмет, новый банк или новый срез, для которого нужен новый сбор («а по ВТБ?», «добавь Альфа-Банк», «а что для ИП?», «а за прошлый год?»), — report.
4. После короткого ответа. «Подробнее», «разверни», «собери по этому отчёт» — report. Уточнение мелочи — reply.
5. deck — только если в разговоре уже есть отчёт. Презентация по новой теме, когда отчёта ещё нет, — report.
6. Сомневаешься между answer и report: один предмет и один показатель — answer, иначе report.
7. Приветствие, благодарность, вопрос о самом помощнике («что ты умеешь») — answer.

Верни СТРОГО JSON без пояснений и без markdown: {"route": "answer|report|reply|deck", "reason": "<до 12 слов, почему>"}"""


def _kind(h: dict) -> str:
    """Чем был ответ помощника: отчётом или короткой справкой."""
    k = (h.get("kind") or "").strip().lower()
    if k in ("report", "answer"):
        return k
    return "report" if len(h.get("content") or "") > _REPORT_LEN else "answer"


def has_report(history: list | None) -> bool:
    return any(isinstance(h, dict) and h.get("role") == "assistant"
               and (h.get("content") or "").strip() and _kind(h) == "report"
               for h in (history or [])[-10:])


def has_answer(history: list | None) -> bool:
    return any(isinstance(h, dict) and h.get("role") == "assistant"
               and (h.get("content") or "").strip() for h in (history or [])[-8:])


def digest(history: list | None, *, turns: int = 6) -> str:
    """Ход разговора в нескольких строках: кто что сказал и чем отвечал помощник.

    Отчёт целиком модели не нужен — хватает начала: по нему видно предмет."""
    rows: list[str] = []
    for h in (history or [])[-turns:]:
        if not isinstance(h, dict):
            continue
        txt = re.sub(r"\s+", " ", str(h.get("content") or "")).strip()
        if not txt:
            continue
        if h.get("role") == "assistant":
            what = "отчёт" if _kind(h) == "report" else "короткий ответ"
            rows.append(f"Помощник ({what}, {len(txt)} знаков): {txt[:420]}"
                        + ("…" if len(txt) > 420 else ""))
        else:
            rows.append(f"Аудитор: {txt[:320]}" + ("…" if len(txt) > 320 else ""))
    return "\n".join(rows) or "(разговор только начинается, прошлых реплик нет)"


def guard(route: str, history: list | None) -> str:
    """Маршрут, который нечем исполнить, приводим к исполнимому."""
    if route == "deck" and not has_report(history):
        return "report"          # отчёта ещё нет: соберём его, презентация — следом
    if route == "reply" and not has_answer(history):
        return "answer"          # отвечать пока не на что
    return route


def by_rules(question: str, history: list | None) -> Route:
    """Прежние правила по словам. Страховка: модель не ответила или отключена."""
    from .analyst import is_deck_request, is_followup_reply
    from .llm_utils import is_deep_question
    q = question or ""
    if is_deck_request(q, history) and has_report(history):
        return Route("deck", "rules", reason="слова «презентация», есть отчёт")
    if is_followup_reply(q, history):
        return Route("reply", "rules", reason="короткая реплика к ответу")
    prior_report = any(isinstance(h, dict) and h.get("role") == "assistant"
                       and _kind(h) == "report" for h in (history or [])[-4:])
    if is_deep_question(q) or prior_report:
        return Route("report", "rules", reason="слова исследования или идёт разговор об отчёте")
    return Route("answer", "rules", reason="короткий вопрос")


def parse(raw: str) -> tuple[str, str] | None:
    """Ответ модели → (маршрут, причина) или None, если разобрать нельзя."""
    from .llm_utils import _loose_json_loads
    try:
        data = _loose_json_loads(raw or "")
    except Exception:  # noqa: BLE001 — модель ответила не по форме
        m = re.search(r"\b(answer|report|reply|deck)\b", raw or "")
        return (m.group(1), "") if m else None
    if not isinstance(data, dict):
        return None
    route = str(data.get("route") or "").strip().lower()
    if route not in ROUTES:
        return None
    return route, str(data.get("reason") or "")[:160]


async def decide(question: str, history: list | None = None) -> Route:
    """Чем отвечать на реплику. Никогда не бросает и не ждёт дольше лимита."""
    q = (question or "").strip()
    if not q:
        return Route("answer", "rules", reason="пустой вопрос")
    if not enabled() or not model_for() or not os.getenv("LLM_API_KEY"):
        return by_rules(q, history)
    t0 = time.monotonic()
    try:
        from openai import AsyncOpenAI

        from .llm_utils import _patch_client_reasoning_effort
        client = _patch_client_reasoning_effort(AsyncOpenAI(
            base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"],
            timeout=_timeout(), max_retries=0))
        resp = await asyncio.wait_for(client.chat.completions.create(
            model=model_for(),
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": f"Ход разговора:\n{digest(history)}\n\n"
                                                  f"Новая реплика аудитора:\n{q[:1500]}"}],
            temperature=0.0, max_tokens=400), timeout=_timeout())
        got = parse((resp.choices[0].message.content or "").strip())
    except Exception as e:  # noqa: BLE001 — маршрут не должен задерживать ответ
        log.warning("[router] модель не ответила (%s) — решаю по правилам", type(e).__name__)
        got = None
    ms = int((time.monotonic() - t0) * 1000)
    if not got:
        r = by_rules(q, history)
        return Route(r.route, "rules", ms, r.reason)
    route = guard(got[0], history)
    log.info("[router] %s (%d мс): %s", route, ms, got[1])
    return Route(route, "model", ms, got[1])
