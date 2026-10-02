"""LLM-разметка отзывов по кодификатору — каскад двух моделей с арбитром.

Векторная разметка (review_topics) ставит тему по близости к описанию и верна
в 57% случаев, продукт — в 42%. Здесь отзыв ЧИТАЕТ модель и ставит коды из
закрытого списка кодификатора (config/review_codebook/), а код проверяет.

Каскад выбран пилотом на 100 отзывах против двух эталонных моделей:
  • A — Gemini 3 Flash пакетами по 10: 94% главной проблемы, 0,17 ₽ за отзыв;
  • B — DeepSeek V4 Flash пакетами: другое семейство моделей, чтобы ошибки
    не совпадали. Где A и B согласны (~64%), разметка принимается;
  • C — арбитр по одному отзыву на расхождениях: DeepSeek V4 Pro, запасной —
    Claude Sonnet. Итог пилота — 96%.

Что проверяет код, а не модель:
  • коды из списка кодификатора, иначе повтор;
  • цитата дословно есть в тексте — модели «причёсывают» цитаты (дословных от
    74 до 100% по моделям); искажённую заменяем близким фрагментом текста или
    очищаем;
  • из изложения вычищаются телефоны, номера карт, почта.

Мусор (CSS вместо текста у сборщика banki.ru) и копии отзывов внешнего
корпуса размечаются кодом, модели их не видят.

Схема: migrations/069_review_annotation.sql.
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

SCHEMA = os.getenv("REVIEW_ANN_SCHEMA", "v2.1-draft")
_ROOT = Path(__file__).resolve().parents[3]
CODEBOOK = Path(os.getenv("REVIEW_ANN_CODEBOOK",
                          str(_ROOT / "config" / "review_codebook" / "v2.1-draft.txt")))

MODEL_A = os.getenv("REVIEW_ANN_A", "google/gemini-3-flash-preview")
MODEL_B = os.getenv("REVIEW_ANN_B", "deepseek-ai/DeepSeek-V4-Flash")
MODEL_C = os.getenv("REVIEW_ANN_C", "deepseek-ai/DeepSeek-V4-Pro")
MODEL_C2 = os.getenv("REVIEW_ANN_C2", "anthropic/claude-sonnet-4.6")
BATCH = int(os.getenv("REVIEW_ANN_BATCH", "10"))
CAP_RUB = float(os.getenv("REVIEW_ANN_CAP_RUB", "150000"))
INFLIGHT = int(os.getenv("REVIEW_ANN_INFLIGHT", "64"))
SINCE = os.getenv("REVIEW_ANN_SINCE", "2025-01-01")
STOP_FILE = Path(os.getenv("REVIEW_ANN_STOP", "/tmp/ra/STOP"))
# Потолок можно поднять на ходу: число в этом файле перекрывает REVIEW_ANN_CAP_RUB,
# и для этого не нужно останавливать прогон с сотнями пакетов в работе.
CAP_FILE = Path(os.getenv("REVIEW_ANN_CAP_FILE", "/tmp/ra/CAP"))


def _cap() -> float:
    try:
        return float(CAP_FILE.read_text().strip())
    except (OSError, ValueError):
        return CAP_RUB
PROGRESS = Path(os.getenv("REVIEW_ANN_PROGRESS", "/tmp/ra/progress.json"))
# Окно тишины по Москве: в 07:00 выпускается обзор, и массовая разметка не
# должна отнимать у него лимиты облака. Новые пакеты в это окно не запускаются.
QUIET = os.getenv("REVIEW_ANN_QUIET", "06:40-07:40")


def _quiet_now() -> bool:
    if not QUIET:
        return False
    from datetime import datetime
    from zoneinfo import ZoneInfo
    lo, hi = QUIET.split("-")
    now = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
    return lo <= now < hi

# ₽ за 1 млн токенов, вход/выход — тарифы облака из /v1/models (24.09.2026)
PRICES = {
    "google/gemini-3-flash-preview": (85.4, 512.4),
    "deepseek-ai/DeepSeek-V4-Flash": (18.5318, 37.0758),
    "deepseek-ai/DeepSeek-V4-Pro": (183.0, 732.0),
    "anthropic/claude-sonnet-4.6": (589.26, 2946.3),
    "google/gemini-3.1-flash-lite": (42.7, 256.2),
}
_SEM = {MODEL_A: int(os.getenv("REVIEW_ANN_SEM_A", "40")), MODEL_B: int(os.getenv("REVIEW_ANN_SEM_B", "80")),
        MODEL_C: int(os.getenv("REVIEW_ANN_SEM_C", "32")), MODEL_C2: 8}
_MAX_TOKENS = {MODEL_A: 8000, MODEL_B: 14000, MODEL_C: 6000, MODEL_C2: 2000}

# ── Кодификатор ──────────────────────────────────────────────────────────────
from . import review_codebook as cb  # noqa: E402

KINDS = set(cb.KINDS)
SEGMENTS = set(cb.SEGMENTS)
PRODUCTS = set(cb.PRODUCTS)
CHANNELS = set(cb.CHANNELS)
GROUP_OF = dict(cb.GROUP_OF)
ISSUES = set(cb.ISSUES)
ESC = set(cb.ESC)
ESC_TO = set(cb.ESC_TO)
VULNERABLE = set(cb.VULNERABLE)

FIELDS = ('{"kind":"...","segment":"...","product":"...","channel":[],"issue":"...",'
          '"issues2":[],"esc":"...","esc_to":[],"no_consent":false,"misled":false,'
          '"vulnerable":[],"amount":null,"event_date":"","city":"","code_fit":"exact",'
          '"summary":"...","quote":"...","new_topic":""}')


def _system() -> str:
    return CODEBOOK.read_text(encoding="utf-8")


# ── Подготовка текста ────────────────────────────────────────────────────────
_CSS_RULE = re.compile(r"[^{}\n]{0,300}\{[^{}]{0,4000}\}")
_TAG = re.compile(r"<[^>]{0,500}>")
# обрывки HTML-атрибутов без угловых скобок: href="…", class="…"
_ATTR = re.compile(r"\b[a-z][\w-]*=(?:\"[^\"]{0,500}\"|'[^']{0,500}')")
_JUNK_MARK = ("{background", '"@type"', "{margin", "data-gtm-click", "{display", "{font-")


def clean_text(raw: str) -> tuple[str, str | None]:
    """(текст для модели, причина отбраковки или None).

    Сборщик banki.ru приклеивал к отзыву стили страницы, а иногда сохранял их
    вместо отзыва. Стили вырезаем; если после этого текста не осталось — это
    не отзыв, и тратить на него модель незачем."""
    t = raw or ""
    if len(t) > 50000:
        return "", "too_long"
    if any(m in t for m in _JUNK_MARK):
        for _ in range(3):
            t2 = _CSS_RULE.sub(" ", t)
            if t2 == t:
                break
            t = t2
    t = _TAG.sub(" ", t)
    t = _ATTR.sub(" ", t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    # отзывы русские: латиница и цифры остаются от разметки, считаем кириллицу
    letters = sum(("а" <= ch.lower() <= "я") or ch in "ёЁ" for ch in t)
    if letters < 25:
        return "", "no_text"
    if any(m in t for m in _JUNK_MARK):
        return "", "markup"
    if len(t) > 8000:
        t = t[:6500] + " … " + t[-1200:]
    return t, None


def text_hash(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:16]


# ── Проверки ответа ──────────────────────────────────────────────────────────
def _s(v) -> str:
    return (v or "").strip().lower() if isinstance(v, str) else ""


def _list(v, allowed: set, cap: int = 3) -> list[str]:
    if isinstance(v, str):
        v = [v]
    out = []
    for x in v or []:
        x = _s(x) if isinstance(x, str) else ""
        if x in allowed and x not in out:
            out.append(x)
    return out[:cap]


def normalize(d: dict | None) -> dict | None:
    """Приводит ответ модели к кодификатору. None — ответ негоден (неизвестный
    код в главных полях): такой отзыв уходит на повтор или к арбитру."""
    if not isinstance(d, dict):
        return None
    kind, product, issue = _s(d.get("kind")), _s(d.get("product")), _s(d.get("issue"))
    if issue == "praise":
        issue = "no_issue"
    if kind not in KINDS or product not in PRODUCTS or issue not in ISSUES:
        return None
    esc = _s(d.get("esc")) or "none"
    if esc not in ESC:
        esc = "none"
    esc_to = _list(d.get("esc_to"), ESC_TO, 7) if esc != "none" else []
    issues2 = [x for x in _list(d.get("issues2"), ISSUES - {"no_issue", "other"}, 3) if x != issue][:2]
    if kind in ("praise", "junk"):
        issue, issues2 = "no_issue", []
    amount = d.get("amount")
    try:
        amount = float(str(amount).replace(" ", "").replace(",", ".")) if amount not in (None, "", "null") else None
    except ValueError:
        amount = None
    ev = _event_date(_s(d.get("event_date")))
    fit = _s(d.get("code_fit"))
    fit = fit if fit in ("exact", "approx") else "exact"
    new_topic = (d.get("new_topic") or "").strip()[:80] or None
    if issue != "other" and fit == "exact":
        new_topic = None
    return {
        "kind": kind,
        "segment": _s(d.get("segment")) if _s(d.get("segment")) in SEGMENTS else "unclear",
        "product": product,
        "channels": _list(d.get("channel") or d.get("channels"), CHANNELS, 3),
        "issue": issue,
        "issues2": issues2,
        "esc": esc,
        "esc_to": esc_to,
        "no_consent": bool(d.get("no_consent")) and d.get("no_consent") not in ("false", "no"),
        "misled": bool(d.get("misled")) and d.get("misled") not in ("false", "no"),
        "vulnerable": _list(d.get("vulnerable"), VULNERABLE, 6),
        "amount": amount,
        "event_date": ev,
        "city": ((d.get("city") or "").strip()[:60] or None),
        "code_fit": fit,
        "new_topic": new_topic,
        "summary": (d.get("summary") or "").strip()[:400],
        "quote": (d.get("quote") or "").strip()[:600],
    }


def _event_date(v: str) -> str | None:
    """«2026-03-15» или «2026-03» → та же строка, если это настоящая дата;
    иначе None. Модель пишет и «2026-02-30», и «2026-13»."""
    from datetime import date
    m = re.search(r"(20\d\d)-(\d\d)(?:-(\d\d))?", v or "")
    if not m:
        return None
    try:
        date(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    except ValueError:
        return None
    return m.group(0)


def agree(a: dict, b: dict) -> bool:
    """Совпали ли модели в том, на чём стоят счётчики и сигналы."""
    cls = lambda d: d["kind"] in ("complaint", "mixed")
    return (cls(a) == cls(b) and a["product"] == b["product"] and a["issue"] == b["issue"]
            and (a["esc"] != "none") == (b["esc"] != "none")
            and a["no_consent"] == b["no_consent"])


def diff_fields(a: dict, b: dict) -> list[str]:
    out = []
    for k in ("kind", "product", "issue", "esc", "no_consent"):
        if a.get(k) != b.get(k):
            out.append(k)
    return out


# ── Цитата: дословно из текста ───────────────────────────────────────────────
_QCH = set("«»\"“”„'`")


def _norm_map(t: str) -> tuple[str, list[int]]:
    """Нормализованный текст и позиция каждого его символа в исходном."""
    out, pos, prev_space = [], [], False
    for i, ch in enumerate(t):
        if ch in _QCH:
            continue
        c = ch.lower().replace("ё", "е")
        if c.isspace():
            if prev_space:
                continue
            c, prev_space = " ", True
        else:
            prev_space = False
        out.append(c)
        pos.append(i)
    return "".join(out), pos


def _find(q: str, t: str) -> str | None:
    nt, pos = _norm_map(t)
    nq, _ = _norm_map(q.strip())
    nq = nq.strip()
    if len(nq) < 8:
        return None
    k = nt.find(nq)
    if k < 0:
        return None
    return t[pos[k]:pos[k + len(nq) - 1] + 1]


def fix_quote(q: str, t: str) -> tuple[str, bool]:
    """Возвращает дословный фрагмент текста и признак, что он найден.

    Модель может сократить цитату многоточием — тогда ищем каждый кусок.
    Может пересказать — тогда берём самое похожее окно из 1–3 предложений
    текста, если сходство не ниже 0,72; иначе цитату очищаем: лучше без
    цитаты, чем с выдуманной."""
    q = (q or "").strip()
    if not q or not t:
        return "", False
    hit = _find(q, t)
    if hit:
        return hit, True
    parts = [p for p in re.split(r"\.\.\.|…", q) if len(p.strip()) >= 8]
    if len(parts) > 1:
        found = [_find(p, t) for p in parts]
        if all(found):
            return " … ".join(found), True
    sents = [s for s in re.split(r"(?<=[.!?…])\s+|\n", t) if s.strip()]
    nq = _norm_map(q)[0]
    best, best_r = None, 0.0
    for i in range(len(sents)):
        for w in (1, 2, 3):
            win = " ".join(sents[i:i + w]).strip()
            if not win or len(win) > 3 * len(q) + 40:
                continue
            r = difflib.SequenceMatcher(None, nq, _norm_map(win)[0]).ratio()
            if r > best_r:
                best, best_r = win, r
    if best and best_r >= 0.72:
        return best[:600], True
    return "", False


_PII = [
    (re.compile(r"(?:\+7|\b8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}\b"), "[телефон]"),
    (re.compile(r"\b(?:\d[ \-]?){15,18}\d\b"), "[номер]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[почта]"),
]


def scrub(s: str) -> str:
    for rx, rep in _PII:
        s = rx.sub(rep, s or "")
    return s


# ── Вызовы моделей ───────────────────────────────────────────────────────────
class Ledger:
    """Учёт токенов и рублей по всему прогону — для потолка трат."""

    def __init__(self, spent: float = 0.0):
        self.rub, self.calls, self.errors = spent, 0, 0
        self.by_model: dict[str, list[float]] = {}

    def add(self, model: str, tin: int, tout: int) -> float:
        pin, pout = PRICES.get(model, (0.0, 0.0))
        rub = tin / 1e6 * pin + tout / 1e6 * pout
        self.rub += rub
        self.calls += 1
        m = self.by_model.setdefault(model, [0, 0, 0.0])
        m[0] += tin
        m[1] += tout
        m[2] += rub
        return rub


def _loose(raw: str):
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else None


class Caller:
    def __init__(self, ledger: Ledger):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(base_url=os.environ["LLM_BASE_URL"],
                                  api_key=os.environ["LLM_API_KEY"],
                                  timeout=420, max_retries=0)
        self.sys = _system()
        self.ledger = ledger
        self.sem = {m: asyncio.Semaphore(n) for m, n in _SEM.items()}
        self.no_temp: set[str] = set()

    async def ask(self, model: str, user: str) -> tuple[object, int, int]:
        """(разобранный JSON или None, токены входа, выхода). Повторы на сетевые
        ошибки и перегрузку с растущей паузой; отказ от temperature
        запоминается, как в llm_utils."""
        kw = dict(model=model, max_tokens=_MAX_TOKENS.get(model, 4000),
                  messages=[{"role": "system", "content": self.sys},
                            {"role": "user", "content": user}])
        last = None
        for attempt in range(6):
            if model not in self.no_temp:
                kw["temperature"] = 0
            else:
                kw.pop("temperature", None)
            try:
                async with self.sem.setdefault(model, asyncio.Semaphore(8)):
                    r = await self.client.chat.completions.create(**kw)
                u = r.usage
                tin, tout = int(u.prompt_tokens or 0), int(u.completion_tokens or 0)
                self.ledger.add(model, tin, tout)
                try:
                    return _loose(r.choices[0].message.content), tin, tout
                except Exception:
                    return None, tin, tout
            except Exception as e:  # noqa: BLE001
                last = e
                self.ledger.errors += 1
                if "temperature" in str(e).lower() and model not in self.no_temp:
                    self.no_temp.add(model)
                    continue
                # запрос негоден сам по себе — повтор ничего не даст
                if getattr(e, "status_code", None) in (400, 401, 403, 404, 422):
                    break
                await asyncio.sleep(min(90, 5 * 2 ** attempt))
        log.warning("review_annotate: %s не ответила (%s)", model, str(last)[:200])
        return None, 0, 0


def card(x: dict, n: int | None = None) -> str:
    head = f"[{n}] " if n else ""
    r = f", оценка {x['rating']:g}★" if x.get("rating") is not None else ""
    return f"{head}Банк: {x['bank']}. Дата отзыва: {x['dt']}{r}.\nТекст:\n{x['text']}"


async def label_batch(c: Caller, model: str, items: list[dict]) -> dict[int, dict]:
    """Пакет отзывов одной модели → {номер в пакете: разметка}. Кого модель
    пропустила или разметила негодными кодами — повтор по одному."""
    user = ("Разметь каждый отзыв отдельно.\n\n"
            + "\n\n".join(card(x, k + 1) for k, x in enumerate(items))
            + '\n\nВерни JSON: {"items":[{"n":1, ...поля...}, ...]} — по объекту на каждый '
              "отзыв, в том же порядке, поле n обязательно. Поля: " + FIELDS)
    d, tin, tout = await c.ask(model, user)
    share_in, share_out = tin / len(items), tout / len(items)
    got: dict[int, dict] = {}
    arr = [it for it in ((d or {}).get("items") or []) if isinstance(it, dict)] if isinstance(d, dict) else []
    by_n = {int(it["n"]): it for it in arr if str(it.get("n", "")).isdigit()}
    if not by_n and len(arr) == len(items):
        by_n = {k + 1: it for k, it in enumerate(arr)}
    for k in range(len(items)):
        v = normalize(by_n.get(k + 1))
        if v:
            v["_tin"], v["_tout"] = share_in, share_out
            got[k] = v
    missing = [k for k in range(len(items)) if k not in got]
    for k in missing:
        d1, tin1, tout1 = await c.ask(model, card(items[k]) + "\n\nВерни JSON ровно с полями: " + FIELDS)
        v = normalize(d1)
        if v:
            v["_tin"], v["_tout"] = share_in + tin1, share_out + tout1
            got[k] = v
    return got


def _arb_prompt(a: str, b: str, diff: str) -> str:
    # не str.format: в FIELDS фигурные скобки JSON
    return ("Два аналитика независимо разметили этот отзыв по кодификатору и разошлись.\n"
            f"Разметка A: {a}\nРазметка B: {b}\n"
            f"Расходятся в полях: {diff}.\n"
            "Прочитай отзыв сам и выдай ИТОГОВУЮ разметку строго по кодификатору: можешь "
            "согласиться с A, с B или выбрать третий вариант. Цитата — дословно из текста.\n"
            "Верни JSON ровно с полями: " + FIELDS)


def _brief(d: dict | None) -> str:
    if not d:
        return "нет (аналитик не справился)"
    keep = ("kind", "segment", "product", "issue", "issues2", "esc", "esc_to",
            "no_consent", "misled", "code_fit", "new_topic")
    return json.dumps({k: d.get(k) for k in keep}, ensure_ascii=False)


async def arbitrate(c: Caller, x: dict, a: dict | None, b: dict | None) -> tuple[dict | None, str]:
    diff = ", ".join(diff_fields(a, b)) if a and b else "одна из разметок отсутствует"
    user = card(x) + "\n\n" + _arb_prompt(_brief(a), _brief(b), diff)
    # второй заход того же арбитра дешевле запасного в пять раз: Sonnet — только
    # если V4 Pro дважды не дал годной разметки
    for model in (MODEL_C, MODEL_C, MODEL_C2):
        d, tin, tout = await c.ask(model, user)
        v = normalize(d)
        if v:
            v["_tin"], v["_tout"] = tin, tout
            return v, model
    return None, ""


# ── Отбор и запись ───────────────────────────────────────────────────────────
_RESP_ID = re.compile(r"/response/(\d+)")


def _todo(limit: int, urls: list[str] | None = None) -> list[dict]:
    with db.session() as s:
        if urls:
            rows = s.execute(text("""
                SELECT i.url, i.review_id, i.source, i.bank, i.dt, i.rating FROM review_index i
                WHERE i.url = ANY(:u) AND NOT EXISTS (SELECT 1 FROM review_annotation a
                    WHERE a.url = i.url AND a.schema_version = :v)"""),
                {"u": urls, "v": SCHEMA}).mappings().all()
        else:
            rows = s.execute(text("""
                SELECT i.url, i.review_id, i.source, i.bank, i.dt, i.rating FROM review_index i
                WHERE i.dt >= CAST(:since AS timestamp) AND i.dt <= now()
                  AND NOT EXISTS (SELECT 1 FROM review_annotation a
                                  WHERE a.url = i.url AND a.schema_version = :v)
                ORDER BY i.dt DESC LIMIT :n"""),
                {"since": SINCE, "v": SCHEMA, "n": limit}).mappings().all()
    return [dict(r) for r in rows]


def _external_ids() -> dict[str, str]:
    """id отзыва banki.ru → url во внешнем корпусе: наш сборщик дублирует его
    с другим написанием адреса (слеш на конце)."""
    with db.session() as s:
        urls = s.execute(text("SELECT url FROM review_index WHERE source = 'bankiru'")).scalars().all()
    out = {}
    for u in urls:
        m = _RESP_ID.search(u or "")
        if m:
            out[m.group(1)] = u
    return out


_INSERT = text("""
    INSERT INTO review_annotation (url, schema_version, status, kind, segment, product,
        channels, issue, issues2, esc, esc_to, no_consent, misled, vulnerable, amount,
        event_date, city, code_fit, new_topic, summary, quote, quote_ok, dup_of, a, b, c,
        models, tokens_in, tokens_out, cost_rub, text_hash)
    VALUES (:url, :sv, :status, :kind, :segment, :product, :channels, :issue, :issues2,
        :esc, :esc_to, :no_consent, :misled, :vulnerable, :amount, :event_date, :city,
        :code_fit, :new_topic, :summary, :quote, :quote_ok, :dup_of,
        CAST(:a AS jsonb), CAST(:b AS jsonb), CAST(:c AS jsonb), :models, :tin, :tout,
        :cost, :th)
    ON CONFLICT (url, schema_version) DO UPDATE SET
        status = EXCLUDED.status, kind = EXCLUDED.kind, segment = EXCLUDED.segment,
        product = EXCLUDED.product, channels = EXCLUDED.channels, issue = EXCLUDED.issue,
        issues2 = EXCLUDED.issues2, esc = EXCLUDED.esc, esc_to = EXCLUDED.esc_to,
        no_consent = EXCLUDED.no_consent, misled = EXCLUDED.misled,
        vulnerable = EXCLUDED.vulnerable, amount = EXCLUDED.amount,
        event_date = EXCLUDED.event_date, city = EXCLUDED.city, code_fit = EXCLUDED.code_fit,
        new_topic = EXCLUDED.new_topic, summary = EXCLUDED.summary, quote = EXCLUDED.quote,
        quote_ok = EXCLUDED.quote_ok, dup_of = EXCLUDED.dup_of, a = EXCLUDED.a,
        b = EXCLUDED.b, c = EXCLUDED.c, models = EXCLUDED.models,
        tokens_in = EXCLUDED.tokens_in, tokens_out = EXCLUDED.tokens_out,
        cost_rub = EXCLUDED.cost_rub, text_hash = EXCLUDED.text_hash, created_at = now()
""")


def _row(x: dict, status: str, final: dict | None = None, a=None, b=None, cc=None,
         models: str = "", dup_of: str | None = None) -> dict:
    f = final or {}
    quote, qok = (fix_quote(f.get("quote", ""), x.get("text", "")) if final else ("", None))
    tin = sum(int((d or {}).get("_tin") or 0) for d in (a, b, cc))
    tout = sum(int((d or {}).get("_tout") or 0) for d in (a, b, cc))
    cost = 0.0
    for d, m in ((a, MODEL_A), (b, MODEL_B), (cc, models.split("+")[-1] if cc else "")):
        if d:
            pin, pout = PRICES.get(m, (0.0, 0.0))
            cost += (d.get("_tin") or 0) / 1e6 * pin + (d.get("_tout") or 0) / 1e6 * pout
    clean = lambda d: json.dumps({k: v for k, v in d.items() if not k.startswith("_")},
                                 ensure_ascii=False) if d else None
    return {
        "url": x["url"], "sv": SCHEMA, "status": status,
        "kind": f.get("kind") or ("junk" if status == "junk" else None),
        "segment": f.get("segment"), "product": f.get("product"),
        "channels": f.get("channels"), "issue": f.get("issue"), "issues2": f.get("issues2"),
        "esc": f.get("esc"), "esc_to": f.get("esc_to"), "no_consent": f.get("no_consent"),
        "misled": f.get("misled"), "vulnerable": f.get("vulnerable"),
        "amount": f.get("amount"), "event_date": f.get("event_date"), "city": f.get("city"),
        "code_fit": f.get("code_fit"), "new_topic": f.get("new_topic"),
        "summary": scrub(f.get("summary", "")) or None, "quote": quote or None,
        "quote_ok": qok, "dup_of": dup_of, "a": clean(a), "b": clean(b), "c": clean(cc),
        "models": models, "tin": tin, "tout": tout, "cost": round(cost, 4),
        "th": text_hash(x.get("raw", "")),
    }


def _write(rows: list[dict]) -> None:
    if rows:
        with db.session() as s:
            s.execute(_INSERT, rows)


# ── Прогон ───────────────────────────────────────────────────────────────────
class Stats:
    def __init__(self):
        self.t0 = time.time()
        self.c = {"agree": 0, "arbitrated": 0, "junk": 0, "dup": 0, "failed": 0}
        self.arb_by: dict[str, int] = {}

    def snapshot(self, ledger: Ledger, backlog: int | None = None) -> dict:
        done = sum(self.c.values())
        el = time.time() - self.t0
        llm = self.c["agree"] + self.c["arbitrated"]
        return {"schema": SCHEMA, "done": done, **self.c,
                "agree_pct": round(100 * self.c["agree"] / llm, 1) if llm else None,
                "rub": round(ledger.rub, 1), "calls": ledger.calls, "errors": ledger.errors,
                "per_review_rub": round(ledger.rub / llm, 3) if llm else None,
                "rate_per_s": round(done / el, 2) if el else None, "elapsed_s": int(el),
                "backlog": backlog, "arb_by": self.arb_by,
                "by_model": {m: {"in": int(v[0]), "out": int(v[1]), "rub": round(v[2], 1)}
                             for m, v in ledger.by_model.items()},
                "at": time.strftime("%Y-%m-%d %H:%M:%S")}


async def _process(c: Caller, items: list[dict], stats: Stats) -> None:
    try:
        await _process_inner(c, items, stats)
    except Exception as e:  # noqa: BLE001
        # неожиданная ошибка не должна терять пакет: пишем «failed», следующий
        # запуск его переразметит
        log.warning("review_annotate: пакет упал (%s: %s)", type(e).__name__, e)
        await asyncio.to_thread(_write, [_row(x, "failed", None) for x in items])
        stats.c["failed"] += len(items)


async def _process_inner(c: Caller, items: list[dict], stats: Stats) -> None:
    ra, rb = await asyncio.gather(label_batch(c, MODEL_A, items), label_batch(c, MODEL_B, items))
    rows, disputed = [], []
    for k, x in enumerate(items):
        a, b = ra.get(k), rb.get(k)
        if a and b and agree(a, b):
            rows.append(_row(x, "agree", a, a=a, b=b, models=f"{MODEL_A}+{MODEL_B}"))
            stats.c["agree"] += 1
        else:
            disputed.append((x, a, b))
    # спорные разбираются параллельно: по очереди они растягивали пакет на минуты
    verdicts = await asyncio.gather(*(arbitrate(c, x, a, b) for x, a, b in disputed))
    for (x, a, b), (v, m) in zip(disputed, verdicts):
        if v:
            rows.append(_row(x, "arbitrated", v, a=a, b=b, cc=v,
                             models=f"{MODEL_A}+{MODEL_B}+{m}"))
            stats.c["arbitrated"] += 1
            stats.arb_by[m] = stats.arb_by.get(m, 0) + 1
        else:
            rows.append(_row(x, "failed", None, a=a, b=b, models=f"{MODEL_A}+{MODEL_B}"))
            stats.c["failed"] += 1
    await asyncio.to_thread(_write, rows)


def _spent(today: bool = False) -> float:
    """Сколько рублей уже ушло на эту версию кодификатора (или за сегодня по
    Москве — для ежедневной доразметки)."""
    with db.session() as s:
        return float(s.execute(text(
            "SELECT coalesce(sum(cost_rub), 0) FROM review_annotation WHERE schema_version = :v"
            + (" AND created_at >= date_trunc('day', now() AT TIME ZONE 'Europe/Moscow')"
               " AT TIME ZONE 'Europe/Moscow'" if today else "")),
            {"v": SCHEMA}).scalar() or 0)


# Один разметчик на базу. Массовый прогон идёт отдельным процессом, фоновый тик
# приложения — своим; без замка они брали бы одни и те же свежие отзывы и
# платили за них дважды.
_LOCK_KEY = 710_022_401
DAILY_CAP_RUB = float(os.getenv("REVIEW_ANN_DAILY_CAP_RUB", "3000"))


def _try_lock():
    conn = db._engine.connect() if db._engine is not None else None
    if conn is None:
        db.init()
        conn = db._engine.connect()
    got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar())
    # замок сессионный и переживает транзакцию; держать открытую транзакцию
    # часами нельзя — она мешает вакууму
    conn.commit()
    if not got:
        conn.close()
        return None
    return conn


def _unlock(conn) -> None:
    try:
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
        conn.commit()
    except Exception:  # noqa: BLE001
        # соединение с неснятым замком нельзя возвращать в пул
        conn.invalidate()
    finally:
        conn.close()


async def run(limit: int | None = None, urls: list[str] | None = None,
              retry_failed: bool = True, daily: bool = False) -> dict:
    """Размечает всё неразмеченное в текущей версии кодификатора, от свежих к
    старым. Перезапускаемо: размеченное пропускается.

    Потолок трат: массовый прогон — общий по версии (REVIEW_ANN_CAP_RUB или
    файл CAP), ежедневная доразметка (daily=True) — за сутки по Москве
    (REVIEW_ANN_DAILY_CAP_RUB). Останавливается и по файлу-стопу. Если
    разметчик уже работает в другом процессе — ничего не делает."""
    lock = await asyncio.to_thread(_try_lock)
    if lock is None:
        return {"skipped": "locked"}
    try:
        return await _run(limit, urls, retry_failed, daily)
    finally:
        await asyncio.to_thread(_unlock, lock)


async def _run(limit, urls, retry_failed, daily) -> dict:
    from .bankiru_fts import bodies_for

    PROGRESS.parent.mkdir(parents=True, exist_ok=True)
    if retry_failed:
        with db.session() as s:
            s.execute(text("DELETE FROM review_annotation WHERE schema_version = :v AND status = 'failed'"),
                      {"v": SCHEMA})
    ledger = Ledger(_spent(today=daily))
    c = Caller(ledger)
    stats = Stats()
    ext = _external_ids()
    inflight: dict[asyncio.Task, list[dict]] = {}
    total_left = limit
    stop_reason = "done"

    def dump(backlog=None):
        try:
            PROGRESS.write_text(json.dumps(stats.snapshot(ledger, backlog), ensure_ascii=False, indent=1))
        except OSError:
            pass

    last_dump = 0.0
    while True:
        if STOP_FILE.exists():
            stop_reason = "stop_file"
            break
        cap = DAILY_CAP_RUB if daily else _cap()
        if ledger.rub >= cap:
            stop_reason = f"cap {cap:.0f} ₽"
            break
        if _quiet_now():
            dump()
            await asyncio.sleep(60)
            continue
        want = 500 if total_left is None else min(500, total_left)
        if want <= 0:
            break
        # уже отправленные в работу, но ещё не записанные, не берём повторно;
        # отбираем с запасом на них, иначе конвейер упрётся в собственный хвост
        busy = {x["url"] for items in inflight.values() for x in items}
        todo = await asyncio.to_thread(_todo, want + len(busy), urls)
        todo = [x for x in todo if x["url"] not in busy][:want]
        if not todo:
            if not inflight:
                break
            await asyncio.wait(list(inflight), return_when=asyncio.FIRST_COMPLETED)
            inflight = {t: v for t, v in inflight.items() if not t.done()}
            continue
        bodies = await asyncio.to_thread(bodies_for, todo)
        pre_rows, ready = [], []
        for x in todo:
            raw = ((bodies.get(x["url"]) or {}).get("text") or "")
            x["raw"] = raw
            x["dt"] = str(x["dt"])[:10]
            m = _RESP_ID.search(x["url"])
            if x["source"] != "bankiru" and m and m.group(1) in ext and ext[m.group(1)] != x["url"]:
                pre_rows.append(_row(x, "dup", None, dup_of=ext[m.group(1)]))
                stats.c["dup"] += 1
                continue
            t, why = clean_text(raw)
            if why:
                pre_rows.append(_row(x, "junk"))
                stats.c["junk"] += 1
                continue
            x["text"] = t
            ready.append(x)
        await asyncio.to_thread(_write, pre_rows)
        if total_left is not None:
            total_left -= len(todo)
        for j in range(0, len(ready), BATCH):
            chunk = ready[j:j + BATCH]
            while len(inflight) >= INFLIGHT:
                await asyncio.wait(list(inflight), return_when=asyncio.FIRST_COMPLETED)
                inflight = {t: v for t, v in inflight.items() if not t.done()}
            inflight[asyncio.create_task(_process(c, chunk, stats))] = chunk
            if time.time() - last_dump > 30:
                dump()
                last_dump = time.time()
        if urls:
            # явный список: второй раз его не отбираем, иначе упавшие
            # закрутятся в цикле
            break
    if inflight:
        await asyncio.gather(*list(inflight), return_exceptions=True)
    snap = stats.snapshot(ledger)
    snap["stop"] = stop_reason
    try:
        PROGRESS.write_text(json.dumps(snap, ensure_ascii=False, indent=1))
    except OSError:
        pass
    if stop_reason.startswith("cap"):
        log.warning("review_annotate: остановлено по потолку трат (%s)", stop_reason)
    return snap


# ── Перенос в индекс ─────────────────────────────────────────────────────────
_APPLIED = "ann_applied_at"
_CITY_ALIAS = {"москва и область": "Москва", "москва и московская область": "Москва",
               "г. москва": "Москва", "санкт-петербург и область": "Санкт-Петербург",
               "спб": "Санкт-Петербург", "питер": "Санкт-Петербург"}


def _city(v: str | None) -> str | None:
    v = (v or "").strip().strip(".").strip()
    if not v or len(v) > 40:
        return None
    v = _CITY_ALIAS.get(v.lower(), v)
    return v[:1].upper() + v[1:]


def _ev(v: str | None):
    from datetime import date
    v = _event_date(v or "")
    if not v:
        return None
    y, m, *d = v.split("-")
    return date(int(y), int(m), int(d[0]) if d else 1)


def apply_to_index(full: bool = False, chunk: int = 2000) -> dict:
    """Переносит разметку в review_index: тип текста, проблемы, продукт,
    эскалацию, дату событий и город (если у отзыва его не было).

    Инкрементально — по записям разметки новее водяного знака; full=True
    переносит всё (после смены версии кодификатора или ручной правки).
    Отзыв без разметки остаётся с kind = NULL и в счётчики жалоб не входит:
    неразмеченное честнее не считать, чем считать по старой разметке."""
    from datetime import datetime, timezone
    with db.session() as s:
        row = s.execute(text("SELECT v FROM review_index_state WHERE k = :k"),
                        {"k": _APPLIED}).first()
    since = None if full or not row else row[0]
    t0 = time.time()
    done, last = 0, since
    while True:
        with db.session() as s:
            rows = s.execute(text("""
                SELECT url, status, kind, product, issue, issues2, esc, event_date, city,
                       created_at
                FROM review_annotation
                WHERE schema_version = :v
                  AND (CAST(:since AS timestamptz) IS NULL OR created_at > CAST(:since AS timestamptz))
                ORDER BY created_at, url LIMIT :n"""),
                {"v": SCHEMA, "since": last, "n": chunk}).mappings().all()
        if not rows:
            break
        payload = []
        for r in rows:
            ok = r["status"] in ("agree", "arbitrated")
            kind = r["kind"] if ok else (r["status"] if r["status"] in ("junk", "dup") else None)
            complaint = ok and r["kind"] in cb.COMPLAINT_KINDS
            payload.append({
                "u": r["url"], "kind": kind,
                "issue": r["issue"] if ok else None,
                "issues2": list(r["issues2"] or []) if ok else None,
                "product": cb.product_label(r["product"]) if ok else None,
                "esc": bool(complaint and r["esc"] in ("threat", "filed")),
                "ev": _ev(r["event_date"]) if ok else None,
                "city": _city(r["city"]) if ok else None,
                "at": r["created_at"],
            })
        with db.session() as s:
            s.execute(text("""
                UPDATE review_index SET kind = :kind, issue = :issue, issues2 = :issues2,
                       product = :product, esc = :esc, ev_date = :ev,
                       city = coalesce(city, :city), ann_at = :at
                 WHERE url = :u"""), payload)
        done += len(payload)
        last = rows[-1]["created_at"].isoformat()
        if len(rows) < chunk:
            break
    if last and last != since:
        with db.session() as s:
            s.execute(text("""
                INSERT INTO review_index_state (k, v, updated_at) VALUES (:k, :v, now())
                ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = now()"""),
                {"k": _APPLIED, "v": last})
    dt = time.time() - t0
    if done:
        log.info("review_annotate: в индекс перенесено %d записей разметки за %.1f с", done, dt)
    return {"applied": done, "seconds": round(dt, 1), "at": datetime.now(timezone.utc).isoformat()}


def coverage(days: int = 90) -> dict:
    """Доля отзывов окна, у которых уже есть разметка, — для «Пульса» и для
    честной пометки на вкладке, пока идёт массовый прогон."""
    with db.session() as s:
        n, lab = s.execute(text("""
            SELECT count(*), count(*) FILTER (WHERE kind IS NOT NULL)
            FROM review_index WHERE dt >= now() - make_interval(days => :d) AND dt <= now()"""),
            {"d": days}).one()
    return {"days": days, "total": int(n), "labeled": int(lab),
            "pct": round(100.0 * lab / n, 1) if n else None}
