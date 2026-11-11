"""Conductor — режиссёр-оркестратор: вопрос → ResearchPlan.

Это первый и самый важный reasoning-вызов во всём pipeline. Кондуктор:
  1. Понимает истинный интент вопроса (не по ключевым словам, а семантически)
  2. Извлекает субъектов сравнения (банки/услуги/объекты)
  3. Определяет природу вопроса (тариф/функция/качество/процесс/...)
  4. Решает каких агентов звать, с какими заданиями и в каком порядке
  5. Намечает структуру итогового отчёта

Без Кондуктора автономные агенты не знают ЧТО собирать. Кондуктор — это
«брифинг» для команды.

ВАЖНО: Кондуктор НЕ хардкодит продукты. Он может распознать «автоперевод»,
«качество обслуживания ипотечных клиентов», «эквайринг для ИП», «доверенность» —
что угодно. Интент определяет структуру плана, а не словарь продуктов.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from openai import AsyncOpenAI

from ...ai.llm_utils import (_loose_json_loads, normalize_question,
                             detect_bank_slugs, deep_reasoning_extra)
from ...clock import today_anchor
from .base_agent import AgentMission

log = logging.getLogger(__name__)


@dataclass
class ResearchPlan:
    """План исследования от Кондуктора."""
    intent: str               # короткая метка интента
    intent_summary: str        # что аудитор реально хочет узнать (развёрнуто)
    question_nature: str       # tariff_product | feature | quality | process |
                               # regulatory | company_facts | mixed
    subjects: list[str]        # slug'и банков/объектов
    subject_labels: dict[str, str]  # slug → человекочитаемое имя
    product: str               # нормализованный продукт/услуга/тема
    product_synonyms: list[str] = field(default_factory=list)
    # Задания для агентов
    missions: list[AgentMission] = field(default_factory=list)
    # Зависимости между миссиями (id → [depends_on ids])
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    # Структура отчёта
    output_sections: list[str] = field(default_factory=list)
    # Сегмент клиента: retail (физлица) | business (ИП/юрлица/подрядчики/МСБ).
    # Определяет, какие разделы сайтов и витрины уместны: B2B-вопросу розничный
    # каталог banki.ru не источник, а увод в сторону.
    client_segment: str = "retail"
    # Дочерние компании якорного субъекта, через которые оказывается услуга:
    # для эквайринга Сбера это Эвотор, для страховки при кредите — страховая
    # дочка. Их называет кондуктор по вопросу, списка в коде нет.
    subsidiaries: list[str] = field(default_factory=list)
    # Точка зрения отчёта: слуг субъекта, ОТНОСИТЕЛЬНО которого строится
    # сравнение. Обычно sberbank, но вопрос про Домклик против Циана и Авито
    # смотрит глазами Домклика — и это решает кондуктор, а не код.
    anchor: str = ""
    # Метаданные
    needs_ranking: bool = False
    needs_complaints: bool = False
    needs_regulatory: bool = False
    # ── Задача (11.11) ───────────────────────────────────────────────────────
    # Аудит 11.11: сравнение просят в 19 % вопросов, а план строился так, будто
    # сравнение — это любой вопрос: к «истории кредитной карты» приписывались
    # пять банков. Тип задачи и рамку теперь называет кондуктор, а код следит,
    # чтобы непрошеные конкуренты в план не попадали (enforce_scope).
    task: str = "compare"        # см. TASKS
    scope: str = "named"         # sber | named | market | none
    period: str = ""             # период вопроса словами аудитора
    deliverable: str = "report"  # report | table | chart | deck | file
    checks: str = "short"        # full | short | none — сколько «Что проверять»
    topics: list[str] = field(default_factory=list)   # что собрать (характеристики)
    observed: str = ""           # характеристика взгляда со стороны, если нужна
    regulatory: str = ""         # характеристика нормативной рамки, если нужна
    queries: list[dict] = field(default_factory=list)  # {"q": …, "site": …}

    def to_ui_plan(self) -> list[dict]:
        """Для SSE event 'plan' — список шагов для UI."""
        steps = []
        for m in self.missions:
            steps.append({
                "title": f"{_AGENT_LABELS.get(m.agent_id, m.agent_id)}: {m.goal}",
                "tool": m.agent_id,
                "entity": m.subjects[0] if m.subjects else None,
            })
        if not steps:
            names = [self.subject_labels.get(s, s) for s in self.subjects]
            steps.append({"title": "Данные AuditLens: база знаний, жалобы, рынок, новости",
                          "tool": "own", "entity": None})
            if names:
                steps.append({"title": "Сайты и документы: " + ", ".join(names[:6]),
                              "tool": "researcher", "entity": self.subjects[0]})
            for q in self.queries[:6]:
                steps.append({"title": "Поиск: " + str(q.get("q") or "")[:110]
                              + (f" · {q['site']}" if q.get("site") else ""),
                              "tool": "search", "entity": None})
        for i, st in enumerate(steps, 1):
            st["n"] = i
        return steps


# Типы задач. Подписи — для интерфейса и промптов.
TASKS = {
    "compare": "сравнение с конкурентами",
    "product": "всё о продукте или сервисе",
    "complaints": "жалобы и клиентский опыт",
    "regulation": "нормы и требования",
    "market_data": "рынок и цифры",
    "explain": "справка",
    "news": "разбор новости или события",
    "check": "план проверки",
}
# Сколько места занимает «Что проверять», если аудитор не просил прямо:
# полный план — там, где он и есть ответ; короткий — где отчёт справочный, но
# про продукт или норму банка; в чистой справке и цифрах рынка его нет.
_CHECKS_BY_TASK = {"check": "full", "complaints": "full", "news": "full",
                   "compare": "short", "product": "short", "regulation": "short",
                   "market_data": "none", "explain": "none"}


_AGENT_LABELS = {
    "researcher": "Исследование условий",
    "reviews": "Отзывы и жалобы",
    "regulatory": "Регуляторное поле",
    "ranking": "Рейтинг",
    "market": "Рыночный контекст",
}


SYSTEM_PROMPT = """Ты — планировщик исследований для аудиторов службы внутреннего аудита Сбербанка.
Аудитор задаёт вопрос; ты решаешь, ЧТО это за задача и что нужно собрать. Отчёт потом
пишется по твоему плану, поэтому план должен отвечать на вопрос аудитора — не шире и не уже.

1. ЗАДАЧА (поле "task") — одна из:
  • compare — аудитор просит сравнить, сопоставить, найти позицию на рынке, «у кого лучше»,
    «как у конкурентов». Только когда сравнение ПРОСЯТ.
  • product — рассказать о продукте, сервисе или процессе: условия, тарифы, как устроен,
    как подключить, что изменилось («расскажи всё про…», «условия…», «сервис X в Сбере»).
  • complaints — жалобы, отзывы, проблемы клиентов, клиентский опыт.
  • regulation — нормы: что требует закон и Банк России, что изменилось в регулировании,
    найти документ регулятора, санкции за нарушение.
  • market_data — цифры о рынке: доли, объёмы, динамика, статистика, рейтинги, топ-N.
  • explain — справка: что это такое, история, как устроено, кто участники, обзор темы.
  • news — разбор новости, события, пресс-релиза (часто со ссылкой).
  • check — аудитор прямо просит план проверки, риски, «что проверить», карту рисков.
  Если подходят две — бери ту, что в основе вопроса.

2. РАМКА (поле "scope") и СУБЪЕКТЫ ("subjects"):
  • "sber" — вопрос про Сбер или его дочернюю компанию, сравнения не просят. В subjects —
    только Сбер и дочерние компании, через которые оказывается услуга. Конкурентов НЕ добавляй.
  • "named" — аудитор сам назвал организации. В subjects — ровно они (и Сбер, если он назван
    или сравнение идёт с ним).
  • "market" — аудитор просит сравнить с рынком или конкурентами, не называя их. Выбери 3–4
    самых значимых для ЭТОГО продукта конкурента и добавь Сбер.
  • "none" — вопрос не про конкретные организации (справка, история, документ регулятора,
    цифры рынка в целом). subjects — пустой список.
  Спрашивают аудиторы Сбера. Вопрос о жалобах, продукте, сервисе, процессе или проверке, где
  банк не назван («жалобы самозанятых», «уязвимости при выплате наследства», «карта рисков
  доставки карт»), — это вопрос о Сбере: scope "sber", subjects ["sberbank"], anchor "sberbank".
  Правило: конкурент попадает в subjects, только если аудитор его назвал или прямо попросил
  сравнение. «Жалобы самозанятых», «Сервис Своё Дело в Сбере», «история кредитной карты» —
  это НЕ сравнения: банки-конкуренты там не нужны.
  Слуги: sberbank, vtb, alfabank, tinkoff (Т-Банк), gazprombank, rosselkhozbank, sovcombank,
  psb, domclick, cian, avito, ozon_bank и т.п. — латиницей, без пробелов.

3. ТОЧКА ЗРЕНИЯ ("anchor"): слуг организации, глазами которой смотрит отчёт. "sberbank",
  когда вопрос про продукты, процессы или позицию Сбера; слуг дочки, когда вопрос про неё
  («Домклик против Циана» → "domclick"); "" — когда вопрос вообще не про Сбер.
  ДОЧЕРНИЕ КОМПАНИИ ("subsidiaries"): если услугу Сбер оказывает через дочку или партнёра
  экосистемы (эквайринг и кассы — Эвотор, страхование — СберСтрахование, ипотечные сделки —
  Домклик, пенсионные продукты — СберНПФ, связь — СберМобайл), назови их слуги и добавь в
  subjects и subject_labels.

4. СЕГМЕНТ ("client_segment": retail | business): кредиты и счета юрлиц и ИП, РКО, эквайринг,
  зарплатные проекты, самозанятые, гарантии, факторинг — business; остальное — retail.

5. ЧТО СОБРАТЬ ("topics"): 5–9 коротких характеристик-существительных, по которым нужно найти
  сведения, чтобы ответить (условие, срок, тариф, шаг процесса, показатель, дата, участник).
  Под задачу: для product — «условия подключения», «тарифы и комиссии», «ограничения»,
  «изменения условий»…; для market_data — «доля рынка», «объём», «динамика»…; для explain —
  «история появления», «участники», «как устроено»…
  "observed" — ОДНА характеристика взгляда со стороны (на что жалуются, с чем сталкиваются
  на практике). Обязательна для compare, product, complaints, check; иначе "".
  "regulatory" — ОДНА характеристика нормативной рамки (что требуют закон и ЦБ), если предмет
  регулируется и это нужно для ответа; иначе "".

6. ГДЕ ИСКАТЬ ("queries"): 5–9 поисковых запросов, какими ты сам искал бы ответ: коротко,
  по-русски, с названием продукта и годом, если важна свежесть. "site" — домен, если знаешь,
  где это лежит: cbr.ru (статистика и акты ЦБ), consultant.ru (тексты законов), nalog.gov.ru,
  frankrg.com, raexpert.ru, сайт банка. Для цифр рынка первым ставь запрос к первоисточнику
  статистики. Запросы к сайтам субъектов из subjects НЕ пиши — их добавит система.

7. "period" — период из вопроса словами аудитора («за 2026 год», «последние 90 дней») или "".
  "deliverable" — что просят на выходе: "report" (по умолчанию), "table" (таблица, выгрузка),
  "chart" (диаграмма, график), "deck" (презентация, слайды), "file" (файл Word/Excel).
  "wants_checks" — true, если аудитор прямо просит риски, план проверки, «что проверить».

ОСОБЫЙ СЛУЧАЙ — ДОКУМЕНТ ИЛИ ДАННЫЕ РЕГУЛЯТОРА («предельные значения ПСК за II квартал»,
«указание Банка России №…»): task="regulation", scope="none", subjects=[], в queries — запрос
к cbr.ru за первоисточником; нужен сам документ и все его значения, а не сравнение банков.

ВЫХОД — строго JSON без преамбулы и markdown:
{"intent": "короткая_метка_латиницей",
 "intent_summary": "что аудитор хочет узнать, 1–2 фразы",
 "task": "...", "scope": "...", "anchor": "...", "subsidiaries": [],
 "subjects": [...], "subject_labels": {"слуг": "Название"},
 "client_segment": "retail|business",
 "product": "нормализованный предмет вопроса", "product_synonyms": [...],
 "period": "", "deliverable": "report", "wants_checks": false,
 "topics": [...], "observed": "", "regulatory": "",
 "queries": [{"q": "...", "site": ""}],
 "question_nature": "tariff_product|feature|quality|process|regulatory|company_facts|mixed",
 "needs_ranking": false, "needs_complaints": false, "needs_regulatory": false}

ПРИМЕР 1. «Сервис Своё Дело для самозанятых в Сбере»
{"intent":"service_profile","intent_summary":"Аудитору нужен полный профиль сервиса «Своё дело»: что входит, как подключается, сколько стоит, какие ограничения и что говорят клиенты.","task":"product","scope":"sber","anchor":"sberbank","subsidiaries":[],"subjects":["sberbank"],"subject_labels":{"sberbank":"Сбербанк"},"client_segment":"business","product":"сервис «Своё дело» для самозанятых","product_synonyms":["Своё дело","самозанятость в СберБанк Онлайн","НПД"],"period":"","deliverable":"report","wants_checks":false,"topics":["состав сервиса","подключение и регистрация","тарифы и комиссии","приём оплаты и чеки","лимиты и ограничения","изменения условий"],"observed":"проблемы клиентов при работе с сервисом","regulatory":"требования к режиму НПД","queries":[{"q":"Своё дело Сбер самозанятые условия комиссия 2026","site":""},{"q":"налог на профессиональный доход условия лимит дохода","site":"nalog.gov.ru"},{"q":"Своё дело Сбербанк отзывы самозанятых проблемы","site":""}],"question_nature":"feature","needs_ranking":false,"needs_complaints":true,"needs_regulatory":true}

ПРИМЕР 2. «Доля рынка по ОМС по данным ЦБ»
{"intent":"market_share_oms","intent_summary":"Аудитору нужны цифры: доли банков на рынке обезличенных металлических счетов по данным Банка России и место Сбера.","task":"market_data","scope":"none","anchor":"sberbank","subsidiaries":[],"subjects":[],"subject_labels":{},"client_segment":"retail","product":"обезличенные металлические счета (ОМС)","product_synonyms":["металлические счета","счета в драгоценных металлах"],"period":"","deliverable":"report","wants_checks":false,"topics":["объём средств на металлических счетах","доля банка на рынке","динамика объёма","число счетов","дата среза"],"observed":"","regulatory":"","queries":[{"q":"статистика металлические счета драгоценные металлы банки объём","site":"cbr.ru"},{"q":"рынок ОМС доли банков 2026 Сбербанк ВТБ объём средств","site":""},{"q":"обезличенные металлические счета рейтинг банков объём 2026","site":""}],"question_nature":"company_facts","needs_ranking":true,"needs_complaints":false,"needs_regulatory":false}

ПРИМЕР 3. «Сравни условия кредитных карт Сбера и Т-Банка, покажи жалобы»
{"intent":"compare_credit_cards","intent_summary":"Аудитор сравнивает условия кредитных карт Сбера и Т-Банка и хочет увидеть, на что жалуются клиенты каждого.","task":"compare","scope":"named","anchor":"sberbank","subsidiaries":[],"subjects":["sberbank","tinkoff"],"subject_labels":{"sberbank":"Сбербанк","tinkoff":"Т-Банк"},"client_segment":"retail","product":"кредитная карта","product_synonyms":["кредитка","карта с льготным периодом"],"period":"","deliverable":"report","wants_checks":false,"topics":["льготный период","ставка","стоимость обслуживания","кредитный лимит","комиссия за снятие наличных","требования к заёмщику"],"observed":"жалобы клиентов на кредитные карты","regulatory":"","queries":[{"q":"кредитные карты сравнение условий Сбер Т-Банк 2026","site":""},{"q":"кредитная карта жалобы льготный период комиссия отзывы","site":""}],"question_nature":"tariff_product","needs_ranking":false,"needs_complaints":true,"needs_regulatory":false}"""


_BUSINESS_MARKERS = (
    "подрядчик", "юрлиц", "юридическ", "ип ", " ип", "b2b", "бизнес",
    "мсб", "малому бизнесу", "малого бизнеса", "корпоратив", "застройщик",
    "девелопер", "эквайринг", "рко", "расчетн", "расчётн", "зарплатн",
    "проектное финансирование", "кредитная линия", "оборотн", "факторинг",
    "лизинг", "гарантия", "тендер", "госзакуп", "самозанят",
)


def detect_client_segment(text: str) -> str:
    """Сегмент клиента по тексту вопроса: business | retail.

    Детерминированный страхующий слой поверх LLM-классификации: провал
    отчёта 199 начался с того, что B2B-вопрос («подрядчики ИЖС») получил
    розничную обвязку — каталоги ипотеки banki.ru «обязательным первым
    источником» и розничные поисковые запросы.
    """
    t = (text or "").lower()
    return "business" if any(m in t for m in _BUSINESS_MARKERS) else "retail"


# Рассуждение кондуктора. Замер 26.09 на пяти вопросах набора: high — медиана
# 57 с, medium — 27 с при тех же объектах, продукте, признаке жалоб и агентах
# сбора. Замер 11.11 на 16 вопросах с новой подсказкой: low — 7,7 с у gpt-5.4 и
# 7,5 с у Claude Opus 5.5, задача определена верно в 15 из 16; рамку после
# модели всё равно правит код (enforce_scope). План ждёт каждый прогон — low.
_PLAN_EFFORT = ({"reasoning_effort": os.getenv("CONDUCTOR_REASONING_EFFORT", "low")}
                if os.getenv("CONDUCTOR_REASONING_EFFORT", "low") not in ("", "default")
                else None)


async def plan_research(client: AsyncOpenAI, model: str,
                          question: str, history: list[dict] | None = None,
                          on_reasoning=None,
                          ) -> ResearchPlan:
    """Главный API Кондуктора: вопрос → ResearchPlan."""
    q = normalize_question(question)

    # Сначала быстрая локальная подсказка: какие банки явно в вопросе
    hinted_banks = detect_bank_slugs(q)

    user_msg = (
        f"# Вопрос аудитора\n{q}\n\n"
        f"# Организации, названные в вопросе\n"
        f"{', '.join(hinted_banks) or '(не названы — не добавляй конкурентов, если сравнение не просят)'}\n\n"
        f"Верни JSON-план исследования."
    )
    messages = [{"role": "system", "content": today_anchor() + "\n\n" + SYSTEM_PROMPT}]
    if history:
        # Последние реплики — для контекста продолжения («а по ВТБ?»). Прошлый
        # отчёт на 30 тыс. знаков целиком не нужен: хватит начала, где резюме.
        for h in history[-4:]:
            if h.get("role") in ("user", "assistant") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:2500]})
    messages.append({"role": "user", "content": user_msg})

    async def ask(use: str) -> str:
        if on_reasoning is not None:
            from ._streaming import stream_completion
            raw, _r, _t = await stream_completion(
                client, on_reasoning=on_reasoning,
                model=use, messages=messages, temperature=0.0,
                max_tokens=8000, extra_body=deep_reasoning_extra(_PLAN_EFFORT))
            return (raw or "").strip()
        from ...ai.llm_utils import drop_known_rejected, remember_rejected
        kwargs = {
            "model": use, "messages": messages,
            "temperature": 0.0,
            "max_tokens": 8000,   # 3000 рвало план на 5 банках → fallback
            "extra_body": deep_reasoning_extra(_PLAN_EFFORT),
        }
        drop_known_rejected(use, kwargs)
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as e:
            # Провайдер отвергает параметр — снимаем, запоминаем за моделью
            # и повторяем. Иначе план молча подменяется заглушкой:
            # claude-opus отвергает temperature, и вместо разбора вопроса
            # конвейер получал рамку по умолчанию за 0,9 с, чего в логе
            # никто не читал.
            param = remember_rejected(use, e, kwargs)
            if not param:
                raise
            log.warning("[conductor] %s не принимает %s — запомнили, "
                        "повторяем без него", use, param)
            resp = await client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()

    # Основная модель не ответила или вернула не план — пробуем запасную, и
    # только потом строим план по словам вопроса: он заметно беднее.
    models = [model]
    for key in ("GPTR_PLAN_FALLBACK", "LLM_MODEL_REASONING", "LLM_MODEL_SMART"):
        m = (os.getenv(key) or "").strip()
        if m and m not in models:
            models.append(m)
            break
    data = None
    for use in models:
        try:
            raw = await ask(use)
        except Exception as e:
            log.warning("[conductor] %s не ответила: %s", use, str(e)[:160])
            continue
        data = _parse_plan_json(raw)
        if data:
            break
        log.warning("[conductor] %s: ответ не разобран, начало=%r", use, raw[:200])
    if not data:
        log.warning("[conductor] план по словам вопроса — модели не ответили")
        return _fallback_plan(q, hinted_banks)

    plan = _build_plan_from_dict(data, q)
    # Страхующий слой: LLM могла проспать сегмент, маркеры в вопросе — нет.
    if detect_client_segment(q) == "business":
        plan.client_segment = "business"
    enforce_scope(plan, q, hinted_banks)
    log.warning("[conductor] intent=%s, task=%s, scope=%s, nature=%s, %s subjects, %s queries",
                plan.intent, plan.task, plan.scope, plan.question_nature,
                len(plan.subjects), len(plan.queries))
    return plan


def _parse_plan_json(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        data = _loose_json_loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Спасение ОБРЕЗАННОГО JSON (truncation): закрываем незакрытые строки/скобки и
    # отбрасываем хвостовую неполную запись. Лучше план с N миссий, чем fallback.
    salv = _salvage_truncated_json(raw)
    if salv:
        try:
            return json.loads(salv)
        except Exception:
            try:
                d = _loose_json_loads(salv)
                return d if isinstance(d, dict) else None
            except Exception:
                return None
    return None


def _salvage_truncated_json(raw: str) -> str | None:
    """Из обрезанного JSON-объекта собирает синтаксически валидный, балансируя
    скобки. Отрезает по последней закрытой записи (запятая/закрывающая скобка на
    верхнем уровне), затем добавляет недостающие ] и }."""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t, flags=re.IGNORECASE).rstrip("`").strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    cut = -1            # позиция последнего «безопасного» среза (запятая/}/] на глубине ≥1)
    for i in range(start, len(t)):
        ch = t[i]
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            cut = i + 1
        elif ch == "," and depth >= 1:
            cut = i        # срез по запятой — отбросит неполную следующую запись
    if cut < 0:
        return None
    body = t[start:cut].rstrip().rstrip(",")
    # _rebalance пересчитает незакрытые скобки именно для позиции среза.
    return _rebalance(body)


def _rebalance(body: str) -> str | None:
    """Балансирует скобки в усечённом фрагменте, добавляя закрывающие в конце."""
    depth_stack: list[str] = []
    in_str = False
    esc = False
    for ch in body:
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth_stack.append("}")
        elif ch == "[":
            depth_stack.append("]")
        elif ch in "}]" and depth_stack:
            depth_stack.pop()
    tail = '"' if in_str else ""
    return body + tail + "".join(reversed(depth_stack))


def _build_plan_from_dict(data: dict, question: str) -> ResearchPlan:
    subjects = [str(s) for s in (data.get("subjects") or []) if s]
    subject_labels = {str(k): str(v) for k, v in
                       (data.get("subject_labels") or {}).items()}
    # Если меток нет — используем slug как есть
    if not subject_labels:
        subject_labels = {s: s for s in subjects}

    missions: list[AgentMission] = []
    for m in (data.get("missions") or []):
        if not isinstance(m, dict):
            continue
        agent_id = str(m.get("agent_id") or "").strip()
        if not agent_id:
            continue
        # Per-mission subjects: «сравни 5 банков и покажи жалобы по Сберу» —
        # reviews обязан крыть один банк, а не размазывать бюджет на пять.
        # Свои субъекты миссии валидируем против глобальных (незнакомые слаги
        # выбрасываем), пустой список = глобальные, как раньше.
        m_subj = [str(x).strip() for x in (m.get("subjects") or []) if str(x).strip()]
        m_subj = [x for x in m_subj if x in subjects] or list(subjects)
        missions.append(AgentMission(
            agent_id=agent_id,
            goal=str(m.get("goal") or "").strip(),
            subjects=m_subj,
            focus=str(m.get("focus") or "").strip(),
            constraints=[str(c) for c in (m.get("constraints") or []) if c],
            context="",
        ))

    # Миссий новый план не заказывает: что искать, говорят topics и queries.
    # Старый формат (с миссиями) по-прежнему разбирается.
    queries: list[dict] = []
    for it in (data.get("queries") or [])[:12]:
        if isinstance(it, str):
            it = {"q": it}
        if not isinstance(it, dict):
            continue
        qtext = re.sub(r"\s+", " ", str(it.get("q") or "")).strip()[:200]
        # модель порой пишет оператор site: прямо в запросе — переносим в поле
        inline = re.search(r"\bsite:(\S+)", qtext)
        if inline:
            qtext = re.sub(r"\s*\bsite:\S+", "", qtext).strip()
        site = re.sub(r"^https?://", "", str(it.get("site") or (inline.group(1) if inline else "")
                                              ).strip().lower())
        site = re.sub(r"/.*$", "", re.sub(r"^www\.", "", site))
        if not re.fullmatch(r"[a-z0-9а-яё.-]+\.[a-zа-яё]{2,}", site or ""):
            site = ""
        if len(qtext) >= 6:
            queries.append({"q": qtext, "site": site})
    topics = [re.sub(r"\s+", " ", str(t)).strip()[:120]
              for t in (data.get("topics") or []) if str(t).strip()][:9]

    deps_raw = data.get("dependencies") or {}
    dependencies = {str(k): [str(x) for x in v]
                      for k, v in deps_raw.items() if isinstance(v, list)}

    return ResearchPlan(
        intent=str(data.get("intent") or "general").strip(),
        intent_summary=str(data.get("intent_summary") or "").strip(),
        question_nature=str(data.get("question_nature") or "mixed").strip(),
        client_segment=(str(data.get("client_segment") or "").strip().lower()
                        or detect_client_segment(question)),
        subjects=subjects,
        subject_labels=subject_labels,
        product=str(data.get("product") or "").strip(),
        product_synonyms=[str(s) for s in (data.get("product_synonyms") or []) if s],
        missions=missions,
        dependencies=dependencies,
        output_sections=[str(s) for s in (data.get("output_sections") or []) if s],
        subsidiaries=[str(s) for s in (data.get("subsidiaries") or []) if s],
        anchor=str(data.get("anchor") or "").strip(),
        needs_ranking=bool(data.get("needs_ranking")),
        needs_complaints=bool(data.get("needs_complaints")),
        needs_regulatory=bool(data.get("needs_regulatory")),
        task=str(data.get("task") or "").strip().lower(),
        scope=str(data.get("scope") or "").strip().lower(),
        period=re.sub(r"\s+", " ", str(data.get("period") or "")).strip()[:80],
        deliverable=str(data.get("deliverable") or "report").strip().lower(),
        checks="full" if data.get("wants_checks") is True else "",
        topics=topics,
        observed=re.sub(r"\s+", " ", str(data.get("observed") or "")).strip()[:120],
        regulatory=re.sub(r"\s+", " ", str(data.get("regulatory") or "")).strip()[:120],
        queries=queries,
    )


# ── Рамка: тип задачи и состав субъектов проверяет код ───────────────────────
_COMPARE_RX = re.compile(
    r"сравн|сопостав|конкурент|\bпротив\b|\bvs\b|на фоне|у других банк|в других банк|"
    r"с другими банк|относительно рынк|позици\w* .{0,25}рынк|место .{0,25}рынк|бенчмарк|"
    r"benchmark|лучше.{0,20}чем|выгодн\w* услови|топ[- ]?\d|рейтинг банк|в разрезе банк|"
    r"по банкам|среди банк", re.I)
_CHECK_RX = re.compile(
    r"что проверит|что проверять|план проверк|программ\w+ проверк|карт\w+ риск|"
    r"какие риски|риски для|для аудита|как аудитор|точки контрол|что запросить", re.I)
_TASK_RX = (
    ("news", re.compile(r"https?://|проанализируй (новость|для аудита)|пресс-?релиз", re.I)),
    ("check", _CHECK_RX),
    ("complaints", re.compile(r"жалоб|отзыв|претензи|обращени[яй] клиент|недовольств|"
                              r"клиентск\w+ (опыт|бол)|на что жалу", re.I)),
    ("compare", _COMPARE_RX),
    ("regulation", re.compile(r"закон|регулир|указани\w* (цб|банка)|положени\w* (цб|банка)|"
                              r"-фз\b|\bфз[- ]|норматив|требовани\w* (цб|банка россии|регулятор)|"
                              r"санкци|штраф|предельн\w+ значени", re.I)),
    ("market_data", re.compile(r"дол[яюи] рынк|объ[её]м рынк|статистик|динамик|сколько |"
                               r"численност|количеств|эмисси|рейтинг", re.I)),
    ("explain", re.compile(r"что такое|кто так|истори[яию]|как устроен|как работает|"
                           r"объясни|расскажи об? ", re.I)),
)
_DELIVERABLE_RX = (
    ("deck", re.compile(r"презентац|слайд|pptx", re.I)),
    ("chart", re.compile(r"диаграмм|график|инфографик", re.I)),
    ("table", re.compile(r"таблиц|выгруз|excel|эксель|xlsx", re.I)),
    ("file", re.compile(r"\bфайл|word|docx|\bpdf\b", re.I)),
)


def guess_task(question: str, default: str = "product") -> str:
    """Тип задачи по словам вопроса — запасной путь, когда кондуктор не ответил
    или назвал тип, которого нет."""
    for task, rx in _TASK_RX:
        if rx.search(question or ""):
            return task
    return default


def _named_in(question: str, slug: str, label: str) -> bool:
    """Организация названа в вопросе: по слугу или по значимому слову названия."""
    low = (question or "").lower().replace("ё", "е")
    words = [w for w in re.findall(r"[a-zа-я0-9]{3,}", (label or "").lower().replace("ё", "е"))
             if w not in ("банк", "bank", "страхование", "россии")]
    stems = {w[:5] for w in words} | {slug.lower().replace("_", "")[:5]}
    return any(st and st in low for st in stems)


def enforce_scope(plan: ResearchPlan, question: str, named: list[str] | None = None) -> ResearchPlan:
    """Доводит план до вопроса: тип задачи из перечня, конкуренты — только
    названные или запрошенные, объём «Что проверять» — по задаче.

    Замер 11.11 на шести вопросах без сравнения: текущая модель добавила
    конкурентов в трёх, более сильная — в пяти. Инструкция это запрещает, но
    полагаться на неё одну нельзя: непрошеное сравнение стоило каждому третьему
    такому отчёту раздела «Сбер против рынка».
    """
    q = question or ""
    asked_compare = bool(_COMPARE_RX.search(q))
    labels = plan.subject_labels or {}
    family = {x for x in [plan.anchor, *plan.subsidiaries] if x}
    named_set = set(named or [])
    outsiders = [s for s in plan.subjects
                 if s not in family and (s in named_set or _named_in(q, s, labels.get(s, "")))]

    task = plan.task if plan.task in TASKS else guess_task(q)
    if task == "compare" and not asked_compare and not outsiders:
        task = guess_task(q)
        if task == "compare":
            task = "product"
    plan.task = task
    # Свой банк подразумевается в вопросах о продукте, жалобах и проверке
    # («жалобы самозанятых» аудитор Сбера задаёт про Сбер). В справке, цифрах
    # рынка и нормах он остаётся, только если назван.
    if task not in ("product", "complaints", "check", "compare"):
        family = {s for s in family
                  if s in named_set or _named_in(q, s, labels.get(s, "") or
                                                 ("Сбербанк" if s == "sberbank" else s))}

    if task != "compare" and not asked_compare:
        keep = family | set(outsiders) | {s for s in plan.subjects if s in named_set}
        dropped = [s for s in plan.subjects if s not in keep]
        if dropped:
            log.warning("[conductor] непрошеные субъекты убраны: %s", dropped)
            plan.subjects = [s for s in plan.subjects if s in keep]
            plan.subject_labels = {s: name for s, name in labels.items() if s in keep}
            for m in plan.missions:
                m.subjects = [s for s in m.subjects if s in keep]
    # Спрашивают аудиторы Сбера: вопрос о жалобах, продукте или проверке без
    # названного банка — про свой банк. Без субъекта отчёт не взял бы ни жалоб,
    # ни документов базы знаний.
    if task in ("product", "complaints", "check") and not plan.subjects and not plan.anchor:
        plan.anchor = "sberbank"
    if plan.anchor and plan.anchor not in plan.subjects and task in ("product", "complaints", "check"):
        plan.subjects.insert(0, plan.anchor)
        plan.subject_labels.setdefault(plan.anchor, "Сбербанк" if plan.anchor == "sberbank"
                                       else plan.anchor)

    family = {x for x in [plan.anchor, *plan.subsidiaries] if x}
    if not plan.subjects:
        plan.scope = "none"
    elif set(plan.subjects) <= family:
        plan.scope = "sber"
    elif plan.scope not in ("named", "market"):
        plan.scope = "named"

    if plan.checks != "full":
        plan.checks = "full" if _CHECK_RX.search(q) else _CHECKS_BY_TASK.get(task, "short")
    if plan.deliverable not in ("report", "table", "chart", "deck", "file"):
        plan.deliverable = "report"
    if plan.deliverable == "report":
        plan.deliverable = next((d for d, rx in _DELIVERABLE_RX if rx.search(q)), "report")
    if task in ("complaints",) or re.search(r"жалоб|отзыв", q, re.I):
        plan.needs_complaints = True
    return plan


def open_subjects(plan) -> bool:
    """Список объектов открыт: факты могут относиться к организациям, которых
    в плане нет (доли рынка, рейтинг, история). Тогда название организации у
    факта сохраняется как есть, а не обнуляется."""
    return (getattr(plan, "task", "") in ("market_data", "explain", "news", "regulation")
            or getattr(plan, "scope", "") in ("market", "none"))


def fan_out_researcher(plan: ResearchPlan) -> ResearchPlan:
    """Раскладывает ЕДИНУЮ researcher-миссию (все банки сразу) на N миссий —
    по одному субъекту на агента.

    Корень «мелких» отчётов: один researcher держал ВСЕ банки × ВСЕ измерения и,
    упираясь в потолок чтений, читал ~1 страницу на банк. Разнесённые по банкам
    агенты работают параллельно (волна 1) и тратят весь свой бюджет чтений на
    ОДИН банк → ~5 страниц/банк вместо ~1. agent_id остаётся "researcher", поэтому
    зависимости (ranking←researcher) и реестр агентов не ломаются: ranking ждёт
    завершения волны 1 как и раньше. Тюнится V2_FANOUT_RESEARCHER (1=вкл, 0=выкл),
    порог V2_FANOUT_MIN_SUBJECTS (по умолчанию ≥2 субъекта)."""
    if os.getenv("V2_FANOUT_RESEARCHER", "1") == "0":
        return plan
    min_subj = int(os.getenv("V2_FANOUT_MIN_SUBJECTS", "2"))
    new_missions: list[AgentMission] = []
    fanned = 0
    for m in plan.missions:
        subs = [s for s in (m.subjects or []) if s]
        if m.agent_id != "researcher" or len(subs) < min_subj:
            new_missions.append(m)
            continue
        for s in subs:
            label = plan.subject_labels.get(s, s)
            goal = (f"[ТОЛЬКО {label}] {m.goal}\n\n"
                    f"ВАЖНО: исследуй ИСКЛЮЧИТЕЛЬНО «{label}» — глубоко, по всем "
                    f"параметрам из задания. Не отвлекайся на другие банки (их "
                    f"берут параллельные агенты). Цель — максимум конкретики именно "
                    f"по этому субъекту.")
            new_missions.append(AgentMission(
                agent_id="researcher",
                goal=goal,
                subjects=[s],
                focus=m.focus,
                constraints=list(m.constraints),
                context=m.context,
            ))
            fanned += 1
    if fanned > 1:
        plan.missions = new_missions
        log.warning("[conductor] researcher fan-out: %s миссий по 1 субъекту",
                     fanned)
    return plan


# ── banki.ru product-каталог как детерминированный источник тарифов ──────────
# Стабильный URL /products/{path}/{slug}/ отдаёт тарифы ЧИСТЫМ HTML по HTTP (без
# браузера/капчи, ~1с). Без этого агент по «{банк} ипотека» находит через поиск
# только сайт банка (sberbank.ru — SPA за F5-антиботом), тарифы не читаются, и в
# отчёте по главному банку пусто. Подсовываем URL researcher-агенту явно.
_BANKI_SLUG_OVERRIDE = {           # наш slug → banki.ru slug (где отличается)
    "tinkoff": "tbank", "tbank": "tbank", "rosselkhozbank": "rshb",
}


def _banki_category_paths(text: str) -> list[str]:
    """Категории banki.ru-каталога, упомянутые в тексте задания (мультивыбор —
    отчёт может охватывать несколько продуктов). 404 по URL обрабатывается мягко."""
    t = (text or "").lower()
    out: list[str] = []
    # «жилищн» убран: «жилищное строительство» в B2B-вопросе матчило
    # розничную ипотеку. Ипотека — это когда спрашивают про ипотеку.
    if any(k in t for k in ("ипотек", "ipotek", "hypothec")):
        out.append("hypothec")
    if "автокредит" in t or ("auto" in t and "кредит" in t) or "автокред" in t:
        out.append("autocredits")
    if ("кредитн" in t and "карт" in t) or "creditcard" in t or "кредитк" in t:
        out.append("creditcards")
    if ("дебетов" in t and "карт" in t) or "debitcard" in t or "дебетовк" in t:
        out.append("debitcards")
    if any(k in t for k in ("вклад", "депозит", "deposit", "накопит", "сбереж")):
        out.append("deposits")
    # потребкредит: «кредит» есть, но НЕ только как «кредитная карта»/«автокредит»
    if any(k in t for k in ("потреб", "кредит наличн", "заём", "заем", "ссуд",
                            "рассрочк")) or ("кредит" in t
                                             and "кредитн карт" not in t
                                             and "кредитк" not in t
                                             and t.replace("автокредит", "").find("кредит") >= 0):
        out.append("credits")
    return list(dict.fromkeys(out))[:3]   # дедуп, максимум 3 категории


def attach_banki_sources(plan: ResearchPlan) -> ResearchPlan:
    """Детерминированно даёт researcher-миссиям приоритетный источник тарифов —
    banki.ru/products/{cat}/{bank}/. Тюнится V2_BANKI_PRODUCT_HINT (1=вкл, 0=выкл)."""
    if os.getenv("V2_BANKI_PRODUCT_HINT", "1") == "0":
        return plan
    # Витрины уместны для вопросов о ТАРИФАХ/ПРОДУКТАХ банков. Вопросу о
    # документе регулятора они меняли предмет: агенту навязывалось «прочитай
    # banki.ru ПЕРВЫМ», и вместо таблицы ЦБ отчёт сравнивал витринные ставки.
    if (plan.question_nature or "").strip().lower() in ("regulatory", "process", "event"):
        return plan
    if not plan.subjects:
        return plan
    # B2B-вопросу розничный каталог не источник: у banki.ru вообще НЕТ
    # каталога кредитов юрлицам (/products/businesscredits/ → 404), и инжект
    # физически может подсовывать только ипотеку/вклады/потребкредиты. Ровно
    # так отчёт 199 получил 8 розничных страниц «обязательным первым чтением».
    if (plan.client_segment or "").strip().lower() == "business":
        return plan
    hay = (f"{plan.product} {plan.intent_summary} "
           + " ".join(m.goal for m in plan.missions if m.agent_id == "researcher"))
    cats = _banki_category_paths(hay)
    if not cats:
        return plan
    for m in plan.missions:
        if m.agent_id != "researcher":
            continue
        subs = [s for s in (m.subjects or []) if s]
        urls: list[str] = []
        for s in subs[:6]:
            bslug = _BANKI_SLUG_OVERRIDE.get(s, s)
            label = plan.subject_labels.get(s, s)
            for cat in cats:
                urls.append(f"{label}: https://www.banki.ru/products/{cat}/{bslug}/")
        if not urls:
            continue
        m.goal = (m.goal or "") + (
            "\n\nПРИОРИТЕТНЫЙ ИСТОЧНИК ТАРИФОВ (banki.ru — чистый HTML по HTTP, без "
            "капчи, актуальные ставки/ПСК/условия/требования). ОБЯЗАТЕЛЬНО прочитай "
            "эти URL через read_url ПЕРВЫМ, ДО сайта банка (сайты банков — SPA за "
            "антиботом, часто не отдают тарифы):\n" + "\n".join(urls[:8]))
    return plan


def _fallback_plan(question: str, hinted_banks: list[str]) -> ResearchPlan:
    """Минимальный план, если кондуктор не ответил. Субъекты — только названные
    в вопросе (раньше подставлялись четыре банка, и любой вопрос превращался в
    сравнение); тип задачи — по словам вопроса."""
    subjects = list(hinted_banks)
    if not subjects and re.search(r"сбер|sber", question or "", re.I):
        subjects = ["sberbank"]
    labels = {s: ("Сбербанк" if s == "sberbank" else s.title()) for s in subjects}
    plan = ResearchPlan(
        intent="general",
        intent_summary="",
        question_nature="mixed",
        client_segment=detect_client_segment(question),
        subjects=subjects,
        subject_labels=labels,
        product="",
        product_synonyms=[],
        missions=[],
        dependencies={},
        output_sections=[],
        anchor="sberbank" if "sberbank" in subjects else "",
        task=guess_task(question),
        queries=[{"q": normalize_question(question)[:200], "site": ""}],
    )
    return enforce_scope(plan, question, hinted_banks)
