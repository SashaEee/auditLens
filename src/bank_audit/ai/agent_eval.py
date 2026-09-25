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
    return {w[:5] for w in re.findall(r"[а-яёa-z0-9]{4,}", (s or "").lower().replace("ё", "е"))}


FORBIDDEN = [
    ("внутренний адрес", re.compile(r"127\.0\.0\.1|localhost:\d", re.I)),
    ("служебный ключ темы", re.compile(
        r"\b(" + "|".join(k for k in T.THEME_KEYS if "_" in k or k == "chargeback") + r")\b")),
    ("совет обойти защиту", re.compile(r"r\.jina|обо(йти|йдите|ход)\w*\s+(защит|cloudflare|капч|блокир)",
                                       re.I)),
    ("служебная пометка", re.compile(r"alsql|mcp_auditlens|\[alsql", re.I)),
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
    ev = [{k: c.get(k) for k in ("date", "city", "summary", "quote")} for c in th.get("complaints") or []]
    return {"question": f"Почему на этой неделе выросли жалобы на тему «{sig['label']}»? "
                        "Что за этим стоит и что проверить?",
            "numbers": [("жалоб за неделю", sig["week"], 1)],
            "words": [sig["label"]], "min_words": 1,
            "judge_focus": "Назван ли конкретный общий сюжет жалоб (событие, продавец, сервис, "
                           "площадка), если он виден в жалобах эталона, и его доля; число за "
                           "неделю и норма совпадают с эталоном.",
            "facts": {"signal": sig, "complaints": ev[:25],
                      "groups": th.get("similar_groups")}}


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
            "facts": {"news": n}}


def c_product_complaints() -> dict:
    ov = _j("complaints_overview", product="Вклад", days=90)
    top = [t["label"] for t in (ov.get("themes") or [])[:3]]
    return {"question": "Какие основные жалобы клиентов Сбера на вклады за последние 90 дней?",
            "numbers": [("жалоб на вклады", ov.get("complaints"), 2)],
            "words": top, "min_words": 2,
            "judge_focus": "Ответ именно про вклады (не про все жалобы банка), главные темы "
                           "совпадают с эталоном.",
            "facts": {k: ov.get(k) for k in ("complaints", "prev_period", "change_pct",
                                             "escalation_pct", "themes", "days")}}


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
            "facts": facts}


def c_regulation() -> dict:
    return {"question": "Что изменилось в регулировании вкладов в 2026 году?",
            "words": ["вклад"], "min_words": 1, "need_link": True,
            "judge_focus": "Утверждения опираются на источники со ссылками; номера актов "
                           "не выдуманы (без источника — помечены); закрытый сайт не выдан "
                           "за «изменений нет»; нет советов обойти защиту сайтов.",
            "facts": {}}


def c_fraud() -> dict:
    ov = _j("complaints_overview", product="Вклад", days=180)
    fraud = [t for t in ov.get("themes") or []
             if t["theme"] in ("fraud_loss", "unauthorized_access", "fraud_credit",
                               "block_161", "data_leak")]
    return {"question": "Какие мошеннические схемы вокруг вкладов видны в жалобах клиентов?",
            "words": ["вклад"], "min_words": 1,
            "judge_focus": "Ответ про вклады, а не про все жалобы банка; схемы описаны по "
                           "самим жалобам; числа не взяты из общих тем банка; законы не "
                           "выдуманы.",
            "facts": {"deposit_fraud_themes_180d": fraud,
                      "deposit_complaints_180d": ov.get("complaints")}}


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
            "facts": {"headline": b.get("headline"),
                      "insights": [i.get("title") for i in b.get("insights") or []]}}


def c_market() -> dict:
    mp = _j("market_position", category="deposit")
    c = (mp.get("cells") or [{}])[0]
    return {"question": "Какое место Сбер занимает на рынке вкладов и какой у нас лучший вклад?",
            "numbers": [("место", c.get("rank"), 0), ("банков", c.get("n_banks"), 0),
                        ("ставка", c.get("value"), 0.05)],
            "words": [c.get("title") or ""], "min_words": 1,
            "judge_focus": "Место, число банков, продукт и ставка совпадают с эталоном; "
                           "не пустой ответ.",
            "facts": c}


def c_reviews() -> dict:
    ov = _j("complaints_overview", days=90)
    return {"question": "Сколько жалоб на Сбер за 90 дней и какая доля с эскалацией по "
                        "сравнению с рынком?",
            "numbers": [("жалоб", ov.get("complaints"), 25),
                        ("эскалация", ov.get("escalation_pct"), 0.3),
                        ("эскалация рынка", ov.get("market_escalation_pct"), 0.3)],
            "judge_focus": "Эскалация Сбера и рынка не перепутаны между собой и с долей "
                           "«уже обратились».",
            "facts": {k: ov.get(k) for k in ("complaints", "escalation_pct",
                                             "escalation_filed_pct", "market_escalation_pct",
                                             "share_of_all_bank_complaints_pct")}}


def c_bank() -> dict:
    bp = _j("bank_profile", bank="Сбербанк")
    r = bp.get("bankiru_people_rating") or {}
    return {"question": "Какой рейтинг у Сбера на banki.ru?",
            "numbers": [("баллы", r.get("score"), 0.05), ("место", r.get("place"), 0),
                        ("оценка", r.get("avg_grade_of_5"), 0.01)],
            "judge_focus": "Баллы, место и средняя оценка совпадают с эталоном, не выдуманы.",
            "facts": r}


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
            "facts": {"fragment": frag}}


def c_loophole() -> dict:
    r = T._q("""SELECT count(*) FILTER (WHERE is_loophole AND collected_at > now() - interval '30 days') AS n30,
                       count(*) FILTER (WHERE is_loophole AND bank_slug = 'sberbank') AS sber
                  FROM loophole_record""")[0]
    return {"question": "Сколько записей в «Уязвимостях» признаны лазейками за последние "
                        "30 дней и сколько всего лазеек отмечено про Сбер?",
            "numbers": [("лазеек за 30 дней", r["n30"], 2), ("про Сбер", r["sber"], 2)],
            "judge_focus": "Числа совпадают с эталоном; сказано, что оценки предварительные.",
            "facts": dict(r)}


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
]


# ── проверка ответа ──────────────────────────────────────────────────────────

def check_answer(answer: str, spec: dict, seconds: float) -> list[dict]:
    from .hermes_quick import is_stub
    res = []
    if is_stub(answer) or re.fullmatch(r"\W*нет данных\W*", answer or "", re.I):
        res.append({"check": "есть ответ", "ok": False, "hard": True, "detail": (answer or "")[:80]})
        return res
    for name, rx in FORBIDDEN:
        m = rx.search(answer)
        res.append({"check": f"нет: {name}", "ok": not m, "hard": True,
                    "detail": m.group(0) if m else None})
    for label, value, tol in spec.get("numbers") or []:
        if value is None:
            continue
        ok = has_number(answer, float(value), float(tol))
        res.append({"check": f"число «{label}» = {value}", "ok": ok, "hard": False})
    words = [w for w in spec.get("words") or [] if w]
    if words:
        need = spec.get("min_words", 1)
        hit = sum(1 for w in words if len(_stems(w) & _stems(answer)) >= min(2, len(_stems(w))))
        res.append({"check": f"по теме ({hit}/{len(words)})", "ok": hit >= need, "hard": False})
    if spec.get("need_link"):
        ok = bool(re.search(r"\]\((https?://|#)", answer))
        res.append({"check": "ссылки на источники", "ok": ok, "hard": False})
    res.append({"check": f"время ≤ {int(SLOW_S)} с", "ok": seconds <= SLOW_S, "hard": False,
                "detail": round(seconds, 1)})
    return res


_JUDGE_SYSTEM = (
    "Ты проверяешь ответы ИИ-аналитика AuditLens для аудиторов розничного бизнеса "
    "Сбербанка. Тебе дают вопрос, ЭТАЛОННЫЕ ДАННЫЕ (ровно то, что показывает интерфейс "
    "AuditLens) и ответ. Оцени ответ строго по эталону: числа должны совпадать, выводы — "
    "следовать из данных. Выдумка — утверждение или число, которого нет в эталоне и "
    "которое нельзя проверить по названному источнику. Ответь ТОЛЬКО JSON: "
    '{"score": 1-5, "correct": true|false, "key_fact": true|false, '
    '"hallucination": true|false, "issues": ["кратко, по-русски"]}. '
    "5 — точно, конкретно, полезно аудитору; 3 — в целом верно, но упущено главное или "
    "расплывчато; 1 — неверно или не по вопросу.")


async def judge(question: str, facts: dict, focus: str, answer: str) -> dict | None:
    if not JUDGE_MODEL:
        return None
    from ..digest.writer import _chat
    from .llm_utils import _loose_json_loads
    user = (f"ВОПРОС: {question}\n\nНА ЧТО СМОТРЕТЬ: {focus}\n\nЭТАЛОННЫЕ ДАННЫЕ:\n"
            f"{json.dumps(facts, ensure_ascii=False, default=str)[:14000]}\n\nОТВЕТ:\n{answer[:9000]}")
    try:
        raw, _, _ = await _chat(JUDGE_MODEL, _JUDGE_SYSTEM, user, max_tokens=700, temperature=0.0)
        d = _loose_json_loads(raw)
        if isinstance(d, dict) and "score" in d:
            return {k: d.get(k) for k in ("score", "correct", "key_fact", "hallucination", "issues")}
    except Exception as e:  # noqa: BLE001 — судья не должен ронять прогон
        log.warning("судья: %s", e)
    return None


def verdict(checks: list[dict], jd: dict | None) -> str:
    if any(c["hard"] and not c["ok"] for c in checks):
        return "fail"
    soft = [c for c in checks if not c["hard"]]
    bad = sum(1 for c in soft if not c["ok"])
    s = (jd or {}).get("score")
    if (jd or {}).get("hallucination") or (s is not None and s <= 2):
        return "fail"
    real = [c for c in soft if not c["check"].startswith("время")]
    if real and sum(1 for c in real if not c["ok"]) * 2 > len(real):
        return "fail"
    if bad == 0 and (s is None or s >= 4):
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


async def run_case(case: Case, model: str | None, use_judge: bool, tag: str) -> dict:
    try:
        spec = await asyncio.to_thread(case.build)
    except Exception as e:  # noqa: BLE001 — эталон не собрался: кейс пропускаем, не валим прогон
        log.warning("эталон %s: %s", case.id, e)
        spec = None
    base = {"id": case.id, "tab": case.tab, "title": case.title}
    if not spec:
        return base | {"verdict": "skip", "note": "нет данных для эталона"}
    r = await _ask(spec["question"], model, f"eval-{tag}-{case.id}")
    checks = check_answer(r["answer"], spec, r["seconds"])
    jd = None
    if use_judge and r["answer"].strip():
        jd = await judge(spec["question"], spec.get("facts") or {}, spec.get("judge_focus", ""),
                         r["answer"])
    return base | {"question": spec["question"], "verdict": verdict(checks, jd),
                   "seconds": r["seconds"], "tools": r["tools"], "error": r["error"],
                   "checks": checks, "judge": jd, "answer": r["answer"][:6000],
                   "empty_attempts": (r["meta"] or {}).get("empty_attempts")}


def summarize(results: list[dict]) -> dict:
    done = [r for r in results if r["verdict"] != "skip"]
    n = {v: sum(1 for r in done if r["verdict"] == v) for v in ("pass", "partial", "fail")}
    score = round(100 * (n["pass"] + 0.5 * n["partial"]) / len(done), 1) if done else None
    secs = [r["seconds"] for r in done if r.get("seconds") is not None]
    return {"score": score, "n_pass": n["pass"], "n_partial": n["partial"], "n_fail": n["fail"],
            "median_s": round(statistics.median(secs), 1) if secs else None}


async def run_eval(model: str | None = None, only: list[str] | None = None,
                   use_judge: bool = True, trigger: str = "cli", concurrency: int = 2,
                   save: bool = True) -> dict:
    cases = [c for c in CASES if not only or c.id in only]
    tag = uuid.uuid4().hex[:8]
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(c):
        async with sem:
            r = await run_case(c, model, use_judge, tag)
            log.info("[agent-eval] %s %s %s с", c.id, r["verdict"], r.get("seconds"))
            return r

    t0 = time.time()
    results = await asyncio.gather(*(one(c) for c in cases))
    summ = summarize(results)
    out = {"model": model or "default", "trigger": trigger, **summ, "cases": list(results),
           "started": t0, "run_id": None}
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
                VALUES (to_timestamp(:t0), now(), 'hermes', :m, :tr, :sc, :p, :pa, :f, :med,
                        CAST(:cases AS jsonb))
                RETURNING run_id"""), {
                "t0": res["started"], "m": res["model"], "tr": res["trigger"],
                "sc": res["score"], "p": res["n_pass"], "pa": res["n_partial"],
                "f": res["n_fail"], "med": res["median_s"],
                "cases": json.dumps(res["cases"], ensure_ascii=False, default=str)}).scalar()
            s.commit()
            return rid
    except Exception as e:  # noqa: BLE001
        log.warning("agent_eval_run не сохранён: %s", e)
        return None


def history(limit: int = 12) -> dict:
    """Прогоны для «Пульса»: последние с итогом, у последнего — все кейсы."""
    rows = T._q("""SELECT run_id, started_at, finished_at, model, trigger, score, n_pass,
                          n_partial, n_fail, median_s
                     FROM agent_eval_run ORDER BY started_at DESC LIMIT :l""", {"l": limit})
    last = None
    if rows:
        c = T._q("SELECT cases FROM agent_eval_run WHERE run_id = :r", {"r": rows[0]["run_id"]})
        last = c[0]["cases"] if c else None
    return {"runs": rows, "last_cases": last}
