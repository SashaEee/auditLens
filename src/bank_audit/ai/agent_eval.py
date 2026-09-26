"""Регрессионный набор ИИ-аналитика (быстрый режим на Hermes).

Вопросы по всем вкладкам — те же, на которых 25.09 агент провалил 9 из 14.
Эталон считается в момент прогона инструментами AuditLens (ai/agent_tools):
вопросы и числа берутся из живых данных, поэтому набор не устаревает вместе с
неделей — «сигнал дня» сегодня про чарджбэк, завтра про другое.

Проверка ответа — два слоя:
  • детерминированный: числа эталона (с допуском, в русской записи), ключевые
    слова, запреты (внутренние адреса, служебные ключи, советы обойти защиту,
    заглушки вместо ответа), время;
  • судья — модель читает вопрос, эталонные данные и ответ и отвечает, верен
    ли ответ, назван ли главный факт, нет ли выдумок (как судья выпуска).

Итог прогона — таблица agent_eval_run и карточка в «Пульсе». Запуск:
`auditlens agent-eval` (CLI), кнопка в «Пульсе», еженедельно — скрипт
deploy/hermes-al/learn_gate.sh, который после самообучения агента откатывает
навыки, если качество упало.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from . import agent_tools as T

log = logging.getLogger(__name__)

JUDGE_MODEL = os.getenv("AGENT_EVAL_JUDGE_MODEL") or os.getenv("LLM_MODEL_REASONING", "")
SLOW_S = float(os.getenv("AGENT_EVAL_SLOW_S", "180"))


def _j(tool: str, **kw) -> dict:
    return json.loads(T.BY_NAME[tool].fn(**kw))


# ── разбор чисел в русском тексте ────────────────────────────────────────────

_NUM_RX = re.compile(r"(?<![\d.,])\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUM_RX.finditer(text or ""):
        s = re.sub(r"[   ]", "", m.group(0)).replace(",", ".")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def has_number(text: str, value: float, tol: float) -> bool:
    return any(abs(x - value) <= tol for x in numbers_in(text))


def _stems(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]{3,}", (s or "").lower().replace("ё", "е"))}


def mentions(answer: str, phrase: str) -> bool:
    """Ответ упоминает фразу эталона: короткую — любым её словом (3+ букв),
    длинную (заголовок, название новости) — хотя бы третью её слов, но не
    меньше двух."""
    ps = _stems(phrase)
    if not ps:
        return True
    need = 1 if len(ps) <= 2 else max(2, -(-len(ps) // 3))
    return len(ps & _stems(answer)) >= need


FORBIDDEN = [
    ("внутренний адрес", re.compile(r"127\.0\.0\.1|localhost:\d", re.I)),
    ("служебный ключ темы", re.compile(
        r"\b(" + "|".join(k for k in T.THEME_KEYS if "_" in k or k == "chargeback") + r")\b")),
    ("совет обойти защиту", re.compile(
        r"r\.jina|обо(йти|йдите|ход)\w*\s+(защит|cloudflare|капч|блокир)", re.I)),
    ("служебная пометка", re.compile(r"alsql|mcp_+auditlens|\[alsql", re.I)),
    # аудитору отказывать не в чем: «лазейки» — рабочий раздел, а не просьба о вреде
    ("отказ отвечать", re.compile(
        r"не (подскажу|буду (подсказывать|помогать|описывать))|не могу (помочь|подсказать)", re.I)),
]


# ── кейсы ────────────────────────────────────────────────────────────────────

@dataclass
class Case:
    id: str
    tab: str
    title: str
    build: Callable[[], dict | None]     # вопрос + эталон из живых данных; None — пропуск


def _sig() -> dict | None:
    s = _j("complaint_signals")
    sig = (s.get("signals") or s.get("watch_faster_than_market") or [None])[0]
    return sig


def c_signal() -> dict | None:
    sig = _sig()
    if not sig:
        return None
    th = _j("complaint_theme", theme=sig["theme"], days=7)
    return {"question": f"Почему на этой неделе выросли жалобы на тему «{sig['label']}»? "
                        "Что за этим стоит и что проверить?",
            "numbers": [("жалоб за неделю", sig["week"], 1)],
            "words": [sig["label"]], "min_words": 1,
            "judge_focus": "Назван ли конкретный общий сюжет жалоб (событие, продавец, сервис, "
                           "площадка), если он виден в жалобах эталона, и его доля; число за "
                           "неделю и норма совпадают с эталоном.",
            "facts": th}


def c_news_day() -> dict | None:
    b = _j("day_brief")
    news = [x for x in b.get("insights") or [] if x.get("url")]
    if not news:
        return None
    n = news[0]
    return {"question": f"Проанализируй для аудита розничного бизнеса Сбера новость "
                        f"«{n['title']}» ({n['url']}). Что это значит и что проверить?",
            "words": [n["title"]], "min_words": 2,
            "judge_focus": "Ответ про эту новость (не про соседние), суть передана верно, "
                           "есть конкретные проверки в Сбере; нет «нет данных» при наличии "
                           "новости в эталоне.",
            "facts": {"brief_item": n, "news_feed": _j("news_find", url=n["url"])}}


def c_product_complaints() -> dict:
    ov = _j("complaints_overview", product="Вклад", days=90)
    top = [t["label"] for t in (ov.get("themes") or [])[:3]]
    return {"question": "Какие основные жалобы клиентов Сбера на вклады за последние 90 дней?",
            "numbers": [("жалоб на вклады", ov.get("complaints"), 2)],
            "words": top, "min_words": 2,
            "judge_focus": "Ответ именно про вклады (не про все жалобы банка), главные темы "
                           "совпадают с эталоном.",
            "facts": ov}


def c_compare() -> dict:
    mo = _j("market_offers", category="deposit", banks=["ВТБ", "Газпромбанк"])
    nums, facts = [], {}
    for b, d in (mo.get("by_bank") or {}).items():
        best = (d.get("best") or [None])[0]
        if best:
            nums.append((f"лучший вклад {b}", best["metric_value"], 0.05))
            facts[b] = best
    return {"question": "Сравни наши вклады с ВТБ и Газпромбанком.",
            "numbers": nums, "words": ["ВТБ", "Газпромбанк"], "min_words": 2,
            "judge_focus": "Лучшие ставки трёх банков совпадают с эталоном; сравнение "
                           "сопоставимо (срок, условия); есть вывод, а не сырой список; "
                           "короткий промо-срок оговорён, если он есть.",
            "facts": mo}


def c_regulation() -> dict:
    return {"question": "Что изменилось в регулировании вкладов в 2026 году?",
            "words": ["вклад"], "min_words": 1, "need_link": True,
            "judge_focus": "Утверждения опираются на источники со ссылками; номера актов "
                           "не выдуманы (без источника — помечены); закрытый сайт не выдан "
                           "за «изменений нет»; нет советов обойти защиту сайтов.",
            "facts": {}}


_FRAUD = ("fraud_loss", "unauthorized_access", "fraud_credit", "data_leak")


def c_fraud() -> dict:
    ov = _j("complaints_overview", product="Вклад", days=180)
    fraud = [t for t in ov.get("themes") or [] if t["theme"] in _FRAUD]
    cases = {t["theme"]: _j("complaint_search", query="мошенники", product="Вклад",
                            theme=t["theme"], days=180, limit=10).get("complaints")
             for t in fraud}
    return {"question": "Какие мошеннические схемы вокруг вкладов видны в жалобах клиентов "
                        "за последние полгода?",
            "words": ["вклад"], "min_words": 1,
            "judge_focus": "Ответ про вклады, а не про все жалобы банка; схемы описаны по "
                           "самим жалобам (хищения, взлом, кредиты мошенников, утечки). "
                           "Блокировки по 161-ФЗ — антифрод банка, не схема мошенников, но "
                           "упоминание как следствия допустимо. Законы не выдуманы.",
            "facts": {"deposit_fraud_themes_180d": fraud, "fraud_complaints": cases,
                      "deposit_overview_180d": ov}}


def c_news_link() -> dict | None:
    rows = T._q("""SELECT url, title FROM news_item WHERE url LIKE 'https://t.me/%'
                    AND value >= 7 AND ts > now() - interval '3 days'
                    ORDER BY value DESC, ts DESC LIMIT 1""")
    if not rows:
        return None
    r = rows[0]
    return {"question": f"Разбери новость {r['url']}",
            "words": [r["title"]], "min_words": 2,
            "judge_focus": "Разобрана именно эта публикация, суть верна, есть что проверить.",
            "facts": {"news": _j("news_find", url=r["url"]).get("news")}}


def c_overview() -> dict:
    b = _j("day_brief")
    return {"question": "Что главное в сегодняшнем выпуске «Обзора»?",
            "words": [b.get("headline") or ""], "min_words": 2,
            "judge_focus": "Названо главное выпуска (заголовок дня) и поводы «что проверить».",
            "facts": b}


def c_market() -> dict:
    mp = _j("market_position", category="deposit")
    c = (mp.get("cells") or [{}])[0]
    return {"question": "Какое место Сбер занимает на рынке вкладов и какой у нас лучший вклад?",
            "numbers": [("место", c.get("rank"), 0), ("банков", c.get("n_banks"), 0),
                        ("ставка", c.get("value"), 0.05)],
            "words": [c.get("title") or ""], "min_words": 1,
            "judge_focus": "Место, число банков, продукт и ставка совпадают с эталоном; "
                           "не пустой ответ.",
            "facts": mp}


def c_reviews() -> dict:
    ov = _j("complaints_overview", days=90)
    return {"question": "Сколько жалоб на Сбер за 90 дней и какая доля с эскалацией по "
                        "сравнению с рынком?",
            "numbers": [("жалоб", ov.get("complaints"), 25),
                        ("эскалация", ov.get("escalation_pct"), 0.3),
                        ("эскалация рынка", ov.get("market_escalation_pct"), 0.3)],
            "judge_focus": "Эскалация Сбера и рынка не перепутаны между собой и с долей "
                           "«уже обратились».",
            "facts": ov}


def c_bank() -> dict:
    bp = _j("bank_profile", bank="Сбербанк")
    r = bp.get("bankiru_people_rating") or {}
    return {"question": "Какой рейтинг у Сбера на banki.ru?",
            "numbers": [("баллы", r.get("score"), 0.05), ("место", r.get("place"), 0),
                        ("оценка", r.get("avg_grade_of_5"), 0.01)],
            "judge_focus": "Баллы, место и средняя оценка совпадают с эталоном, не выдуманы.",
            "facts": bp}


_FEE_RX = re.compile(r"(\d+[,.]\d+)\s*%[^.]{0,80}?\+\s*(?:фиксированн\w+\s+сумм\w+\s*)?(\d[\d\s]*)\s*(?:руб|₽)",
                     re.I)


def c_knowledge() -> dict:
    ks = _j("knowledge_search", query="комиссия за снятие наличных кредитная СберКарта",
            bank="Сбербанк")
    nums, frag = [], None
    for d in ks.get("documents") or []:
        for fr in d.get("fragments") or []:
            m = _FEE_RX.search(fr or "")
            if m and "налич" in fr.lower():
                pct_ = float(m.group(1).replace(",", "."))
                fix = float(re.sub(r"\s", "", m.group(2)))
                nums = [("процент", pct_, 0.01), ("фикс", fix, 0)]
                frag = fr[:600]
                break
        if nums:
            break
    return {"question": "Какая комиссия за снятие наличных по кредитной СберКарте?",
            "numbers": nums,
            "judge_focus": "Названа комиссия именно за снятие наличных (процент плюс "
                           "фиксированная сумма), не плата за обслуживание; есть документ.",
            "facts": {"fragment": frag, "knowledge_search": ks}}


def c_loophole() -> dict:
    r = T._q("""SELECT count(*) FILTER (WHERE is_loophole AND collected_at > now() - interval '30 days') AS n30,
                       count(*) FILTER (WHERE is_loophole AND bank_slug = 'sberbank') AS sber
                  FROM loophole_record""")[0]
    return {"question": "Сколько записей в «Уязвимостях» признаны лазейками за последние "
                        "30 дней и сколько всего лазеек отмечено про Сбер?",
            # счётчик растёт каждые несколько минут, а «30 дней» агент может считать от
            # полуночи — допуск 5%
            "numbers": [("лазеек за 30 дней", r["n30"], max(2, r["n30"] * 0.05)),
                        ("про Сбер", r["sber"], max(2, r["sber"] * 0.05))],
            "judge_focus": "Числа совпадают с эталоном; сказано, что оценки предварительные.",
            "facts": dict(r) | {"note": "оценки модели предварительные (status=preliminary); "
                                "число за 30 дней растёт каждые несколько минут и зависит от "
                                "границы окна (от текущего момента или от полуночи) — "
                                "расхождение до 5% не ошибка"}}


def c_loopholes() -> dict | None:
    lh = _j("loopholes", query="кредитная карта", bank="Сбербанк")
    recs = [r for r in lh.get("records") or [] if r.get("about_bank")]
    if not recs:
        return None
    titles = [r["title"] for r in recs[:6] if r.get("title")]
    return {"question": "Какие лазейки и уязвимости есть в Сбере по кредитным картам?",
            "words": titles, "min_words": min(2, len(titles)),
            "judge_focus": "Ответ по данным раздела «Уязвимости»: названы конкретные схемы "
                           "из записей (что за схема, источник), есть оговорка про "
                           "предварительный статус и что проверить; нет отказа отвечать. "
                           "Сверка с тарифом, жалобами или рынком допустима как дополнение, "
                           "но не вместо лазеек. Номера записей сверяй со всеми подзапросами.",
            # агент ищет по механикам (грейс, MCC, возврат…) — эталон должен видеть те же
            # записи, иначе судья считает найденные агентом записи несуществующими
            "facts": {"product": lh, "by_mechanics": {
                q: [{k: r.get(k) for k in ("record_id", "title", "why_loophole", "source",
                                           "url", "about_bank")}
                    for r in (_j("loopholes", query=q, bank="Сбербанк", limit=8)
                              .get("records") or [])]
                for q in ("грейс льготный период", "снятие наличных переводы", "MCC квази-кэш",
                          "возврат товара", "электронный кошелёк", "кэшбэк бонусы")}}}


def c_week() -> dict:
    s = _j("complaint_signals")
    sig = s.get("signals") or []
    return {"question": "Что растёт в жалобах на Сбер на этой неделе?",
            "numbers": [(x["label"], x["week"], 1) for x in sig[:2]],
            "words": [x["label"] for x in sig[:2]], "min_words": min(1, len(sig)),
            "judge_focus": "Названы значимые всплески недели с числами и нормой; если "
                           "всплесков нет — так и сказано.",
            "facts": s}


CASES: list[Case] = [
    Case("S1", "Обзор", "сигнал дня: причина всплеска", c_signal),
    Case("S2", "Обзор", "новость дня", c_news_day),
    Case("T1", "Отзывы", "жалобы по продукту", c_product_complaints),
    Case("T2", "Рынок", "сравнение с конкурентами", c_compare),
    Case("T3", "База знаний", "регулирование", c_regulation),
    Case("T4", "Отзывы", "мошенничество вокруг продукта", c_fraud),
    Case("T5", "Обзор", "разбор новости по ссылке", c_news_link),
    Case("P1", "Обзор", "главное в выпуске", c_overview),
    Case("P2", "Рынок", "место Сбера", c_market),
    Case("P3", "Отзывы", "жалобы и эскалация", c_reviews),
    Case("P4", "Банки", "рейтинг banki.ru", c_bank),
    Case("P5", "База знаний", "тариф из документа", c_knowledge),
    Case("P6", "Уязвимости", "лазейки за месяц", c_loophole),
    Case("P7", "Отзывы", "что растёт на неделе", c_week),
    Case("P8", "Уязвимости", "лазейки по продукту", c_loopholes),
]


# ── кейсы отчёта (deep research) ─────────────────────────────────────────────
# Вопросы — из РЕАЛЬНОЙ истории отчётов (337 вопросов, июль–сентябрь 2026), в тех
# же пропорциях: сравнение условий и рынка 43%, жалобы 17%, лазейки и схемы 10%,
# клиентский путь 10%, нормы 9%, разбор новости 4%, всплеск жалоб 0,6%. Первая
# версия набора (5 придуманных вопросов, три из них — про жалобы) мерила не то,
# что спрашивают. Эталон — живые данные вкладок на момент прогона; где эталона
# нет (веб: эквайринг, клиентский путь, нормы), судья сверяет утверждения с
# цитатами источников самого отчёта. Состав разделов выбирает бриф, поэтому
# раздел проверяется по смыслу заголовка, а не по точному названию.

_NOT_CONFIRMED = re.compile(
    r"(рост|всплеск|динамик)[^.\n]{0,120}не подтвержд|не подтвержд[^.\n]{0,120}(рост|всплеск)",
    re.I)
# Служебные фразы о «материалах» вместо ответа — признак отчёта, собранного из
# разрозненных разделов (аудит 26.09).
_META_TALK = re.compile(r"в переданных (фактах|данных|материал)|в материале раздела|"
                        r"в теле отчёта|точк[аи] отсч[её]та", re.I)
DEEP_SLOW_S = float(os.getenv("AGENT_EVAL_DEEP_SLOW_S", "300"))
_BIG4 = ["Альфа", "Т-Банк", "ВТБ"]


def _deep(build: Callable[[], dict | None], question: Callable[[dict], str] | None = None,
          **extra):
    def run() -> dict | None:
        spec = build()
        if not spec:
            return None
        q = question(spec) if question else spec["question"]
        return spec | {"question": q, "slow_s": DEEP_SLOW_S} | extra | {
            "forbid": [("служебные фразы вместо ответа", _META_TALK),
                       *extra.get("forbid", [])]}
    return run


def _web_case(question: str, words: list[str], focus: str, min_words: int = 2) -> Callable:
    def build() -> dict:
        return {"question": question, "words": words, "min_words": min_words,
                "need_link": True, "judge_focus": focus, "facts": {}}
    return build


def c_top_deposits() -> dict:
    mo = _j("market_offers", category="deposit", limit=10)
    mp = _j("market_position", category="deposit")
    top = mo.get("top") or []
    sber = mo.get("sber_best") or {}
    return {"question": "Сравни предложения по вкладам, выдели топ-5 и позицию Сбера.",
            "numbers": [("лучшая ставка Сбера", sber.get("metric_value"), 0.05)]
            if sber.get("metric_value") else [],
            "words": [r["bank_name"] for r in top[:5] if r.get("bank_name")], "min_words": 3,
            "judge_focus": "Топ-5 по ставке совпадает с витриной «Рынок» (эталон top) — банк, "
                           "вклад, ставка, срок; место Сбера — как в market_position; промо и "
                           "короткий срок оговорены рядом с числом; есть вывод.",
            "facts": {"market_offers": mo, "market_position": mp}}


def c_mortgage() -> dict:
    mo = _j("market_offers", category="mortgage", limit=12)
    mp = _j("market_position", category="mortgage")
    return {"question": "Сравни ипотечные ставки между Сбером и рынком, выдели программы "
                        "с господдержкой.",
            "words": ["господдерж", "семейн"], "min_words": 1,
            "judge_focus": "Ставки Сбера и рынка — из витрины «Рынок» (эталон), рыночная "
                           "ипотека и программы с господдержкой разведены; место Сбера "
                           "названо; льготные ставки не выданы за рыночные.",
            "facts": {"market_offers": mo, "market_position": mp}}


def c_offer_reaction() -> dict | None:
    mo = _j("market_offers", category="credit", limit=20)
    rival = next((r for r in mo.get("top") or [] if not r.get("is_sber")
                  and r.get("metric_value") and r.get("title")), None)
    if not rival:
        return None
    rate = str(rival["metric_value"]).replace(".", ",")
    return {"question": f"Проанализируй позицию Сбера относительно оффера «{rival['title']}» "
                        f"банка {rival['bank_name']} (кредиты, ставка {rate}%). Насколько "
                        f"условия Сбера конкурентны и стоит ли реагировать?",
            "numbers": [("ставка оффера", rival["metric_value"], 0.05)],
            "words": [rival["bank_name"]], "min_words": 1,
            "judge_focus": "Условия оффера и лучшее предложение Сбера — как в витрине; "
                           "сопоставимость (срок, сумма, ПСК против рекламной ставки) "
                           "оговорена; прямой ответ «стоит ли реагировать» с обоснованием.",
            "facts": {"offer": rival,
                      "by_bank": _j("market_offers", category="credit",
                                    banks=[rival["bank_name"]]),
                      "market_position": _j("market_position", category="credit")}}


def c_main_complaints() -> dict:
    ov = _j("complaints_overview", days=90)
    top = [t["label"] for t in (ov.get("themes") or [])[:4]]
    return {"question": "Какие основные жалобы у клиентов Сбербанка? Где подводные камни?",
            "numbers": [("жалоб за 90 дней", ov.get("complaints"),
                         max(5, (ov.get("complaints") or 0) * 0.05))],
            "words": top, "min_words": 2,
            "judge_focus": "Главные темы и числа — как в эталоне; «подводные камни» "
                           "раскрыты сюжетами с цитатами клиентов, а не пересказом тарифов; "
                           "есть динамика и что проверить.",
            "facts": ov}


def c_cc_complaints() -> dict:
    ov = _j("complaints_overview", product="Кредитная карта", days=90)
    top = [t["label"] for t in (ov.get("themes") or [])[:3]]
    return {"question": "О каких проблемах с кредитными картами Сбербанка жалуются клиенты?",
            "numbers": [("жалоб на кредитные карты", ov.get("complaints"),
                         max(3, (ov.get("complaints") or 0) * 0.05))],
            "words": top, "min_words": 2,
            "judge_focus": "Ответ именно про кредитные карты; темы и числа — как в эталоне; "
                           "сюжеты с дословными цитатами клиентов; что проверить.",
            "facts": ov}


def c_unique_rival_complaints() -> dict:
    banks = {"ВТБ": "ВТБ", "Альфа-Банк": "Альфа-Банк", "Т-Банк": "Т-Банк",
             "Сбербанк": "Сбербанк"}
    facts = {}
    for name in banks:
        ov = _j("complaints_overview", bank=name, product="Кредитная карта", days=365)
        facts[name] = {"complaints": ov.get("complaints"), "error": ov.get("error"),
                       "themes": [(t["label"], t["n"], t.get("pct"))
                                  for t in ov.get("themes") or []]}
    return {"question": "Покажи жалобы клиентов на кредитные карты ВТБ, Альфы и Т-Банка за "
                        "последние 12 месяцев, отдельно по каждому банку, и только те, "
                        "которые уникальны для них и отсутствуют у клиентов Сбера.",
            "words": ["ВТБ", "Альфа", "Т-Банк"], "min_words": 3,
            "judge_focus": "По каждому банку отдельно — темы, которых нет или заметно "
                           "меньше у Сбера (сравни доли тем в эталоне); числа — из эталона; "
                           "с примерами жалоб; не выданы за уникальные темы, которые у "
                           "Сбера тоже частые.",
            "facts": facts}


def c_news_deep() -> dict | None:
    b = _j("day_brief")
    news = [x for x in b.get("insights") or [] if x.get("url")]
    if not news:
        return None
    n = news[0]
    return {"question": f"Проанализируй новость для аудита розничного бизнеса Сбера: "
                        f"«{n['title']}» ({n['url']}). Какие риски и какие действия стоит "
                        f"предпринять?",
            "words": [n["title"]], "min_words": 2,
            "judge_focus": "Разобрана именно эта новость, суть верна; риски для Сбера "
                           "конкретны (продукт, процесс, норма); действия — проверяемые шаги.",
            "facts": {"brief_item": n, "news_feed": _j("news_find", url=n["url"])}}


def c_spike_cta() -> dict | None:
    """Вопрос в том виде, в каком его задаёт кнопка сигнала на «Обзоре»."""
    spec = c_signal()
    if not spec:
        return None
    sig = spec["facts"].get("weekly_signal") or _sig() or {}
    city = (f" {sig.get('top_city_share_pct')}% жалоб из г. {sig['top_city']}."
            if sig.get("top_city") else "")
    norm = str(sig.get("norm_per_week") or "").replace(".", ",")
    ratio = str(sig.get("ratio") or "").replace(".", ",")
    q = (f"Разбери всплеск жалоб «{sig.get('label')}» у Сбербанка: {sig.get('week')} за 7 "
         f"дней против ~{norm}/нед (×{ratio}).{city} Найди вероятную причину, оцени "
         f"регуляторный риск и предложи шаги аудита.")
    return spec | {"question": q,
                   "judge_focus": spec["judge_focus"] + " Причина всплеска названа по "
                   "жалобам (событие, продавец, сервис) и, если есть, по внешним источникам; "
                   "география объяснена; шаги аудита — к этой причине."}


_VOICE_RX = r"жалоб|клиент|всплеск|сюжет|стоит за|боль|проблем"
_LOOP_RX = r"лазейк|уязвим|схем|обход"

DEEP_CASES: list[Case] = [
    Case("D1", "Рынок", "топ-5 вкладов и позиция Сбера", _deep(c_top_deposits)),
    Case("D2", "Рынок", "ипотека: Сбер и рынок, господдержка", _deep(c_mortgage)),
    Case("D3", "Рынок", "эквайринг четырёх банков (веб)", _deep(_web_case(
        "Сравни торговый эквайринг (POS-терминалы) для розницы в банках Сбер, Альфа-Банк, "
        "Т-Банк, ВТБ при обороте по эквайрингу 500 тыс. — 3 млн ₽ в месяц, с акцентом на "
        "комиссию за эквайринг", [*_BIG4, "комисси"],
        "Комиссии и тарифы четырёх банков для заданного оборота — из источников со "
        "ссылками, сопоставимо; позиция Сбера; неизвестное помечено, а не выдумано.", 3))),
    Case("D4", "Рынок", "оффер конкурента: реагировать?", _deep(c_offer_reaction)),
    Case("D5", "Процесс", "клиентский путь дебетовой карты (веб)", _deep(_web_case(
        "Сравни пользовательский путь оформления и получения дебетовой карты для нового "
        "клиента: Сбер, Альфа-Банк, Т-Банк, ВТБ", _BIG4,
        "Шаги пути (заявка, проверка, доставка/выдача, активация), сроки и каналы по "
        "каждому банку со ссылками; где Сбер проигрывает; без выдуманных шагов.", 3))),
    Case("D6", "Отзывы", "основные жалобы Сбера", _deep(
        c_main_complaints, section_rx=[("раздел о жалобах", _VOICE_RX)])),
    Case("D7", "Отзывы", "жалобы по кредитным картам", _deep(
        c_cc_complaints, section_rx=[("раздел о жалобах", _VOICE_RX)])),
    Case("D8", "Отзывы", "уникальные жалобы конкурентов", _deep(c_unique_rival_complaints)),
    Case("D9", "Уязвимости", "лазейки по кредитным картам", _deep(
        c_loopholes, lambda s: "Какие лазейки и уязвимости есть в Сбере по кредитным картам "
                               "и что с ними делать аудиту?",
        section_rx=[("раздел о лазейках", _LOOP_RX)])),
    Case("D10", "Нормы", "раскрытие ПСК: Сбер и ВТБ (веб)", _deep(_web_case(
        "Какие требования ЦБ к раскрытию полной стоимости кредита обязан соблюдать Сбер и "
        "как это сравнимо с ВТБ", ["полной стоимости", "ВТБ"],
        "Требования названы со ссылками на акты (353-ФЗ, указания ЦБ), номера не выдуманы; "
        "что раскрывает Сбер и ВТБ — по источникам; расхождения с нормой выделены.", 2))),
    Case("D11", "Обзор", "разбор новости дня", _deep(c_news_deep)),
    Case("D12", "Отзывы", "всплеск жалоб (кнопка сигнала)", _deep(
        c_spike_cta, section_rx=[("раздел о жалобах", _VOICE_RX)],
        forbid=[("всплеск «не подтверждается» при данных", _NOT_CONFIRMED)])),
]


# ── проверка ответа ──────────────────────────────────────────────────────────

def check_answer(answer: str, spec: dict, seconds: float) -> list[dict]:
    from .hermes_quick import is_stub
    res = []
    if is_stub(answer) or re.fullmatch(r"\W*нет данных\W*", answer or "", re.I):
        res.append({"check": "есть ответ", "ok": False, "hard": True, "detail": (answer or "")[:80]})
        return res
    # адреса ссылок на страницы AuditLens (#reviews?theme=chargeback) — не текст для
    # пользователя; внешние адреса проверяем (127.0.0.1 в ссылке тоже не откроется)
    visible = re.sub(r"\]\(#[^)\s]*\)", "]", answer)
    for name, rx in FORBIDDEN:
        m = rx.search(visible)
        res.append({"check": f"нет: {name}", "ok": not m, "hard": True,
                    "detail": m.group(0) if m else None})
    for label, value, tol in spec.get("numbers") or []:
        if value is None:
            continue
        ok = has_number(answer, float(value), float(tol))
        res.append({"check": f"число «{label}» = {value}", "ok": ok, "hard": False})
    words = [w for w in spec.get("words") or [] if w]
    if words:
        need = min(spec.get("min_words", 1), len(words))
        hit = sum(1 for w in words if mentions(answer, w))
        res.append({"check": f"по теме ({hit}/{len(words)})", "ok": hit >= need, "hard": False})
    if spec.get("need_link"):
        # быстрый ответ ссылается markdown-ссылками, отчёт — сносками [n] на список источников
        ok = bool(re.search(r"\]\((https?://|#)", answer) or re.search(r"\[\d{1,3}\]", answer))
        res.append({"check": "ссылки на источники", "ok": ok, "hard": False})
    heads = re.findall(r"^#{1,3}\s*(.+)$", answer or "", re.M)
    for name, rx in spec.get("section_rx") or []:
        ok = any(re.search(rx, h, re.I) for h in heads)
        res.append({"check": name, "ok": ok, "hard": False})
    for name, rx in spec.get("forbid") or []:
        m = rx.search(visible)
        res.append({"check": f"нет: {name}", "ok": not m, "hard": True,
                    "detail": m.group(0)[:120] if m else None})
    slow = spec.get("slow_s") or SLOW_S
    res.append({"check": f"время ≤ {int(slow)} с", "ok": seconds <= slow, "hard": False,
                "detail": round(seconds, 1)})
    return res


_JUDGE_SYSTEM = (
    "Ты проверяешь ответы ИИ-аналитика AuditLens для аудиторов розничного бизнеса "
    "Сбербанка. Тебе дают вопрос, ЭТАЛОННЫЕ ДАННЫЕ (то, что показывает интерфейс "
    "AuditLens по этому вопросу) и ответ. Числа, которые есть в эталоне, должны совпадать; "
    "выводы — следовать из данных. hallucination=true ТОЛЬКО если ответ противоречит "
    "эталону (другое число, неверный вывод, перепутаны показатели) или ссылается на "
    "явно несуществующий источник или нормативный акт. Подробности, которых в эталоне "
    "просто нет, но которые ему не противоречат (детали жалоб, другие продукты банка, "
    "контекст, помеченные гипотезы), — не выдумка: учитывай их только в оценке "
    "полезности. Если даны ИСТОЧНИКИ ОТЧЁТА — это страницы, которые отчёт прочитал, с "
    "дословными цитатами: утверждение со сноской [n], которое подтверждает цитата "
    "источника n, — не выдумка и не «неподтверждённое», даже если его нет в эталоне; "
    "акт, названный в цитате источника, существует. Эталон важнее источников: "
    "противоречие эталону — ошибка. Отчёт длинный по устройству (резюме, план "
    "проверки, разделы) — длину не штрафуй, штрафуй разделы не по вопросу. "
    "Ответь ТОЛЬКО JSON: "
    '{"score": 1-5, "correct": true|false, "key_fact": true|false, '
    '"hallucination": true|false, "issues": ["кратко, по-русски"]}. '
    "5 — точно, конкретно, полезно аудитору; 4 — верно, мелкие недочёты; 3 — в целом "
    "верно, но упущено главное или расплывчато; 2 — существенные ошибки; 1 — неверно или "
    "не по вопросу.")


def sources_digest(sources: list[dict], cap: int = 16000) -> str:
    """Источники отчёта для судьи: сноска → страница → дословные цитаты фактов."""
    out, size = [], 0
    for s in sources:
        quotes = " ".join(f"«{f.get('verbatim')}»" for f in (s.get("facts") or [])[:6]
                          if f.get("verbatim"))
        line = f"[{s.get('n')}] {s.get('domain') or ''} — {s.get('title') or s.get('url')}: " + (
            quotes or (s.get("excerpt") or "")[:300])
        size += len(line)
        if size > cap:
            out.append(f"… ещё источников: {len(sources) - len(out)}")
            break
        out.append(line)
    return "\n".join(out)


async def judge(question: str, facts: dict, focus: str, answer: str,
                answer_cap: int = 9000, sources: str = "") -> dict | None:
    if not JUDGE_MODEL:
        return None
    from ..digest.writer import _chat
    from .llm_utils import _loose_json_loads
    user = (f"ВОПРОС: {question}\n\nНА ЧТО СМОТРЕТЬ: {focus}\n\nЭТАЛОННЫЕ ДАННЫЕ:\n"
            f"{json.dumps(facts, ensure_ascii=False, default=str)[:24000]}\n\n"
            + (f"ИСТОЧНИКИ ОТЧЁТА:\n{sources}\n\n" if sources else "")
            + f"ОТВЕТ:\n{answer[:answer_cap]}")
    try:
        # Отчёт длинный — рассуждающей модели 700 токенов не хватало, и у трёх
        # кейсов из двенадцати оценки не было вовсе (26.09).
        raw, _, _ = await _chat(JUDGE_MODEL, _JUDGE_SYSTEM, user,
                                max_tokens=2500 if len(answer) > 9000 else 700,
                                temperature=0.0)
        d = _loose_json_loads(raw)
        if isinstance(d, dict) and "score" in d:
            return {k: d.get(k) for k in ("score", "correct", "key_fact", "hallucination", "issues")}
        log.warning("судья: ответ без оценки (%d знаков): %s", len(raw or ""), (raw or "")[:160])
    except Exception as e:  # noqa: BLE001 — судья не должен ронять прогон
        log.warning("судья: %s", e)
    return None


def verdict(checks: list[dict], jd: dict | None) -> str:
    if any(c["hard"] and not c["ok"] for c in checks):
        return "fail"
    soft = [c for c in checks if not c["hard"]]
    bad = sum(1 for c in soft if not c["ok"])
    s = (jd or {}).get("score")
    if s is not None and s <= 2:
        return "fail"
    real = [c for c in soft if not c["check"].startswith("время")]
    if real and sum(1 for c in real if not c["ok"]) * 2 > len(real):
        return "fail"
    if bad == 0 and (s is None or s >= 4) and not (jd or {}).get("hallucination"):
        return "pass"
    return "partial"


# ── прогон ───────────────────────────────────────────────────────────────────

async def _ask(question: str, model: str | None, hint: str) -> dict:
    from .hermes_quick import HermesNotStreamed, stream_quick_hermes
    t0 = time.monotonic()
    parts, tools, meta, err = [], [], {}, None
    try:
        async for raw in stream_quick_hermes(question, [], session_hint=hint, model=model):
            ev = json.loads(raw)
            if ev.get("type") == "text":
                parts.append(ev.get("chunk") or "")
            elif ev.get("type") == "tool_call":
                tools.append(ev.get("name"))
            elif ev.get("type") == "run_meta":
                meta = {k: v for k, v in ev.items() if k != "type"}
    except HermesNotStreamed as e:
        err = f"нет ответа: {e}"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return {"answer": "".join(parts), "tools": tools, "meta": meta, "error": err,
            "seconds": round(time.monotonic() - t0, 1)}


async def _ask_deep(question: str) -> dict:
    """Отчёт тем же входом, что в чате (stream_analysis с принудительным
    отчётом): через ту же обёртку потока. Прямой вызов конвейера её обходил, и
    набор не видел ошибку, которую видел пользователь, — раздел «Голос клиента»
    терял данные AuditLens только за обёрткой (26.09)."""
    from .analyst import stream_analysis
    t0 = time.monotonic()
    lead, body, stages, sources, err = [], [], [], [], None
    try:
        async for raw in stream_analysis(question, [], force_deep=True):
            ev = json.loads(raw)
            t = ev.get("type")
            if t == "text":
                body.append(ev.get("chunk") or "")
            elif t == "lead":
                lead.append(ev.get("chunk") or "")
            elif t == "stage_status" and ev.get("label"):
                stages.append(f"{round(time.monotonic() - t0)}с {ev['label']}"[:90])
            elif t == "sources":
                sources = ev.get("sources") or []
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    return {"answer": "".join(lead) + "".join(body), "tools": stages[-25:], "meta": {},
            "error": err, "seconds": round(time.monotonic() - t0, 1),
            "sources": sources_digest(sources)}


async def run_case(case: Case, model: str | None, use_judge: bool, tag: str,
                   engine: str = "quick") -> dict:
    try:
        spec = await asyncio.to_thread(case.build)
    except Exception as e:  # noqa: BLE001 — эталон не собрался: кейс пропускаем, не валим прогон
        log.warning("эталон %s: %s", case.id, e)
        spec = None
    base = {"id": case.id, "tab": case.tab, "title": case.title}
    if not spec:
        return base | {"verdict": "skip", "note": "нет данных для эталона"}
    deep = engine == "deep"
    r = (await _ask_deep(spec["question"]) if deep
         else await _ask(spec["question"], model, f"eval-{tag}-{case.id}"))
    checks = check_answer(r["answer"], spec, r["seconds"])
    jd = None
    if use_judge and r["answer"].strip():
        jd = await judge(spec["question"], spec.get("facts") or {}, spec.get("judge_focus", ""),
                         r["answer"], answer_cap=40000 if deep else 9000,
                         sources=r.get("sources") or "")
    return base | {"question": spec["question"], "verdict": verdict(checks, jd),
                   "seconds": r["seconds"], "tools": r["tools"], "error": r["error"],
                   "checks": checks, "judge": jd,
                   "answer": r["answer"][:60000 if deep else 6000],
                   **({"sources": r.get("sources") or ""} if deep else {}),
                   "empty_attempts": (r["meta"] or {}).get("empty_attempts")}


def summarize(results: list[dict]) -> dict:
    done = [r for r in results if r["verdict"] != "skip"]
    n = {v: sum(1 for r in done if r["verdict"] == v) for v in ("pass", "partial", "fail")}
    score = round(100 * (n["pass"] + 0.5 * n["partial"]) / len(done), 1) if done else None
    secs = [r["seconds"] for r in done if r.get("seconds") is not None]
    return {"score": score, "n_pass": n["pass"], "n_partial": n["partial"], "n_fail": n["fail"],
            "median_s": round(statistics.median(secs), 1) if secs else None}


async def run_eval(model: str | None = None, only: list[str] | None = None,
                   use_judge: bool = True, trigger: str = "cli",
                   concurrency: int | None = None,
                   save: bool = True, engine: str = "quick") -> dict:
    """engine: quick — быстрый режим (Hermes), deep — отчёт (deep research)."""
    pool = DEEP_CASES if engine == "deep" else CASES
    cases = [c for c in pool if not only or c.id in only]
    # Отчёт — минуты, 12 вопросов по два шли бы полчаса; провайдер держит три.
    concurrency = concurrency or (3 if engine == "deep" else 2)
    tag = uuid.uuid4().hex[:8]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(c):
        async with sem:
            r = await run_case(c, model, use_judge, tag, engine)
            log.info("[agent-eval] %s %s %s с", c.id, r["verdict"], r.get("seconds"))
            return r

    t0 = time.time()
    results = await asyncio.gather(*(one(c) for c in cases))
    summ = summarize(results)
    out = {"model": model or "default", "trigger": trigger, **summ, "cases": list(results),
           "started": t0, "run_id": None, "engine": "deep" if engine == "deep" else "hermes"}
    if save:
        out["run_id"] = await asyncio.to_thread(_save, out)
    return out


def _save(res: dict) -> int | None:
    from sqlalchemy import text
    from .. import db
    try:
        with db.session() as s:
            rid = s.execute(text("""
                INSERT INTO agent_eval_run (started_at, finished_at, engine, model, trigger,
                                            score, n_pass, n_partial, n_fail, median_s, cases)
                VALUES (to_timestamp(:t0), now(), :eng, :m, :tr, :sc, :p, :pa, :f, :med,
                        CAST(:cases AS jsonb))
                RETURNING run_id"""), {
                "t0": res["started"], "eng": res.get("engine") or "hermes",
                "m": res["model"], "tr": res["trigger"],
                "sc": res["score"], "p": res["n_pass"], "pa": res["n_partial"],
                "f": res["n_fail"], "med": res["median_s"],
                "cases": json.dumps(res["cases"], ensure_ascii=False, default=str)}).scalar()
            s.commit()
            return rid
    except Exception as e:  # noqa: BLE001
        log.warning("agent_eval_run не сохранён: %s", e)
        return None


def history(limit: int = 12, engine: str = "hermes") -> dict:
    """Прогоны для «Пульса»: последние с итогом, у последнего — все кейсы.
    engine: hermes — быстрый режим, deep — отчёт."""
    # прогоны первой версии набора (с ложными срабатываниями) в истории не показываем
    rows = T._q("""SELECT run_id, started_at, finished_at, model, trigger, score, n_pass,
                          n_partial, n_fail, median_s, note
                     FROM agent_eval_run WHERE coalesce(note, '') NOT LIKE 'набор v1%'
                      AND engine = :e
                    ORDER BY started_at DESC LIMIT :l""", {"l": limit, "e": engine})
    last = None
    if rows:
        c = T._q("SELECT cases FROM agent_eval_run WHERE run_id = :r", {"r": rows[0]["run_id"]})
        last = c[0]["cases"] if c else None
    return {"runs": rows, "last_cases": last}
