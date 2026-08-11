"""Обогащение офферов LLM: смысл тарифа, который регулярным выражением не взять.

ЗАЧЕМ. После наведения порядка в числах (волны 0-1) на витрине остались две
метрики, которые честно посчитаны и всё равно вводят аудитора в заблуждение:

  • дебетовые карты. 124 банка из 163 стоят на «0 руб./год», ранг вырожден и
    вкладка честно его прячет. Но «бесплатно всегда» и «бесплатно при обороте
    от 100 тыс. руб./мес.» — принципиально разные продукты, и разница написана
    прозой: «Обслуживание бесплатно при сумме покупок от …»;
  • кредиты и автокредиты. Минимальная ставка сплошь и рядом достижима только
    зарплатному клиенту или со страховкой. У Сбера на детальной странице прямо
    три ряда: общие условия — от 20,4 проц., для зарплатных — от 18,4 проц.
    Ранг по нижней границе сравнивает рекламу одного банка с реальностью другого.

ОТКУДА ТЕКСТ. В карточке каталога условий нет — только подписи и числа. Но у
каждого оффера banki.ru есть числовой product_id (мы кладём его в raw), а по
нему открывается детальная страница с полным тарифом:
    debitcards/creditcards → /products/<раздел>/card/<id>/
    credits/autocredits    → /products/<раздел>/credit/<id>/
Оттуда и берём текст: он server-rendered, без капчи (проверено 11.08.2026).

ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА. offer_enrichment — НАША интерпретация, а не тариф
банка. В product_terms ей не место: она порождала бы версии SCD2 и попадала в
«банк изменил условия», хотя менялась бы модель, а не банк.

ЭКОНОМИЯ. Полный обход детальных страниц — сотни мегабайт, поэтому берём
инкрементом: оффер обогащается заново, только если записи нет, если её текст
устарел (ENRICH_MAX_AGE_DAYS) или если у оффера появилась новая версия тарифа.
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import os
import re
import time
from typing import Any, Iterable

import requests
from sqlalchemy import text

from .. import db
from ..ai.analyst import LLM_API_KEY, LLM_BASE_URL, fast_model
from ..ai.llm_utils import _loose_json_loads

log = logging.getLogger(__name__)

BASE = "https://www.banki.ru/products"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_PAUSE_S = float(os.getenv("ENRICH_PAUSE_S", "1.2"))
_MAX_AGE_D = int(os.getenv("ENRICH_MAX_AGE_DAYS", "14"))
_BATCH = int(os.getenv("ENRICH_BATCH", "6"))
# текста тарифа берём кусок: у модели рассуждающий режим, и на батче из
# девяти полных страниц бюджет ответа уходил в рассуждения — приходил
# пустой content вместо JSON
_TEXT_CHARS = int(os.getenv("ENRICH_TEXT_CHARS", "3200"))
_LLM_TIMEOUT = float(os.getenv("ENRICH_LLM_TIMEOUT_S", "120"))

# раздел banki.ru → сегмент пути детальной страницы
_DETAIL_PATH = {"debitcards": "card", "creditcards": "card",
                "credits": "credit", "autocredits": "credit"}
# какие категории вообще обогащаем (у ипотеки banki.ru не отдаёт product_id,
# у вкладов детальной страницы с условиями нет)
CATEGORIES = ("card_debit", "card_credit", "credit", "auto_loan")

_RE_TAGS = re.compile(r"<[^>]+>")
_RE_SCRIPT = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def _plain(page: str) -> str:
    s = _RE_SCRIPT.sub(" ", page)
    s = _html.unescape(_RE_TAGS.sub(" ", s))
    for ch in (" ", " ", " "):
        s = s.replace(ch, " ")
    return re.sub(r"\s+", " ", s).strip()


# Страница весит под мегабайт, из которых 90 проц. — меню и SEO-простыня.
# Нужный кусок начинается с блока условий. Якоря перебираем ПО ПРИОРИТЕТУ, а не
# по позиции: «Тарифы» и «Ставка» встречаются ещё в навигации и в чужих
# карточках-рекомендациях, и первое вхождение уводит окно в бонусную программу.
_ANCHORS = ("Общие условия", "Условия карты", "Условия кредита",
            "Обслуживание карты", "Условия обслуживания", "Тарифы")
_STOP = ("Отзывы о", "Читайте также", "Другие карты", "Похожие предложения",
         "Часто задаваемые вопросы")
_WINDOW = 6000


def _conditions_text(page: str) -> str | None:
    """Кусок страницы с тарифом. Без него LLM додумывает, поэтому если якоря
    нет — честно возвращаем None и оффер не обогащаем вовсе."""
    flat = _plain(page)
    start = next((flat.find(a) for a in _ANCHORS if flat.find(a) > 0), -1)
    if start < 0:
        return None
    chunk = flat[start:start + _WINDOW]
    cut = min((chunk.find(s) for s in _STOP if chunk.find(s) > 500), default=-1)
    if cut > 0:
        chunk = chunk[:cut]
    return chunk if len(chunk) > 200 else None


def _fetch(section: str, pid: str, sess: requests.Session) -> str | None:
    path = _DETAIL_PATH.get(section)
    if not path:
        return None
    url = f"{BASE}/{section}/{path}/{pid}/"
    try:
        r = sess.get(url, timeout=40, headers={"User-Agent": _UA,
                                               "Accept-Language": "ru-RU,ru;q=0.9"})
    except requests.RequestException as e:
        log.warning("обогащение: %s не открылась (%s)", url, e)
        return None
    if r.status_code != 200 or len(r.text) < 5000:
        return None
    return _conditions_text(r.text)


# ── извлечение смысла ────────────────────────────────────────────────────────

_SYSTEM = (
    "Ты — методолог службы внутреннего аудита банка. Тебе дают ФРАГМЕНТ "
    "тарифа банковского продукта с сайта-агрегатора. Твоя задача — перевести "
    "прозу в строгие поля для сравнения банков между собой.\n"
    "ГЛАВНОЕ ПРАВИЛО: если сведений в тексте НЕТ — ставь null или \"unknown\". "
    "Никогда не додумывай и не пользуйся своими знаниями о банке: аудитор "
    "принимает решения по этим полям, выдумка дороже пропуска.\n"
    "Отвечай ТОЛЬКО JSON-массивом без пояснений."
)

_FIELDS = """Для каждого продукта верни объект:
{"i": <номер из запроса>,
 "free_kind": "unconditional" | "conditional" | "paid" | "unknown",
 "free_conditions": [{"type":"turnover|balance|payroll|purchases|package|age|other",
                      "threshold_rub": <число или null>, "note":"<до 90 знаков>"}],
 "rate_requires": [ "insurance"|"payroll"|"new_client"|"online"|"promo_period"|
                    "category_spend"|"large_amount"|"collateral"|"subsidy"|"other" ],
 "rate_attainability": "broad" | "narrow" | "promo_only" | "unknown",
 "cashback_max_pct": <число или null>,
 "cashback_note": "<до 90 знаков или null>",
 "client_segment": "mass" | "premium" | "youth" | "pension" | "business" | "unknown",
 "product_kind": "<чем продукт является на самом деле, до 40 знаков, или null>"}

Пояснения к полям:
• free_kind — плата за обслуживание. "unconditional" = бесплатно всегда;
  "conditional" = бесплатно при выполнении условий (оборот, остаток, зарплата);
  "paid" = взимается всегда. Для КРЕДИТОВ ставь "unknown".
• rate_requires — что нужно, чтобы получить МИНИМАЛЬНУЮ ставку из текста.
  Пустой массив = ставка общая для всех. Для КАРТ обычно пустой.
• rate_attainability — "broad" = минимум доступен обычному клиенту;
  "narrow" = нужен зарплатный проект/страховка/крупная сумма;
  "promo_only" = только акция, первый период или узкий сегмент.
• product_kind — заполняй ТОЛЬКО если название непрозрачно («Лимоны на авто»),
  иначе null.

ЖЁСТКИЕ ПРАВИЛА (нарушение = ошибка):
1. free_kind определяй ТОЛЬКО по строке «Обслуживание карты». Написано
   «бесплатно» без оговорки → "unconditional". Написано «бесплатно при …» или
   «0 ₽ при …» → "conditional" и условие в free_conditions. Указана цена
   («7 000 ₽», «от 199 ₽/мес.») → "paid". Условия из ДРУГИХ разделов (кэшбэк,
   подписка, проценты на остаток) в free_kind не переносить.
2. Если минимальная ставка стоит в строке «Для зарплатных клиентов» — это
   payroll; «со страхованием»/«с полисом» — insurance; «первые N месяцев» или
   «в течение акции» — promo_period; «под залог» — collateral; «семейная»,
   «господдержка», «IT-ипотека» — subsidy. Ставь "other" только если ни одно
   из перечисленных не подходит.
3. Все текстовые поля (note) — ПО-РУССКИ.
4. Ровно один объект на каждый номер из запроса, порядок сохраняй."""


def _client():
    from openai import OpenAI
    return OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                  max_retries=2, timeout=_LLM_TIMEOUT)


_ENUM = {
    "free_kind": {"unconditional", "conditional", "paid", "unknown"},
    "rate_attainability": {"broad", "narrow", "promo_only", "unknown"},
    "client_segment": {"mass", "premium", "youth", "pension", "business", "unknown"},
}
_REQ_OK = {"insurance", "payroll", "new_client", "online", "promo_period",
           "category_spend", "large_amount", "collateral", "subsidy", "other"}
_COND_OK = {"turnover", "balance", "payroll", "purchases", "package", "age", "other"}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) < 1e12 else None


def _clean_item(o: dict, category: str, fee: float | None = None) -> dict:
    """Модель отвечает свободнее, чем обещает. Приводим к схеме и выкидываем
    всё, чего в перечислениях нет: мусорное значение в витрине хуже пропуска."""
    out: dict[str, Any] = {}
    for k, allowed in _ENUM.items():
        v = str(o.get(k) or "unknown").strip().lower()
        out[k] = v if v in allowed else "unknown"
    if category not in ("card_debit", "card_credit"):
        out["free_kind"] = "unknown"          # у кредита нет платы за обслуживание
    conds = []
    for c in (o.get("free_conditions") or [])[:4]:
        if not isinstance(c, dict):
            continue
        t = str(c.get("type") or "other").strip().lower()
        conds.append({"type": t if t in _COND_OK else "other",
                      "threshold_rub": _num(c.get("threshold_rub")),
                      "note": (str(c.get("note") or "")[:90] or None)})
    out["free_conditions"] = conds
    reqs = [str(x).strip().lower() for x in (o.get("rate_requires") or [])]
    out["rate_requires"] = sorted({x for x in reqs if x in _REQ_OK})[:5]
    out["cashback_max_pct"] = _num(o.get("cashback_max_pct"))
    out["cashback_note"] = (str(o.get("cashback_note") or "")[:90] or None)
    out["product_kind"] = (str(o.get("product_kind") or "")[:40] or None)
    # «бесплатно при условии» без единого условия — модель не поняла текст
    if out["free_kind"] == "conditional" and not conds:
        out["free_kind"] = "unknown"
    # Сверка с ЧИСЛОМ, которое мы разобрали из каталога сами. Модель читает
    # прозу и ошибается в обе стороны (у пенсионной СберКарты она перенесла
    # условие подписки из блока кэшбэка в плату за обслуживание). Там, где
    # текст и цифра противоречат, доверяем цифре, а не пересказу.
    if fee is not None and out["free_kind"] != "unknown":
        if fee > 0 and out["free_kind"] == "unconditional":
            out["free_kind"] = "paid"
            out["conflict"] = "fee_gt0_vs_unconditional"
        elif fee == 0 and out["free_kind"] == "paid":
            out["free_kind"] = "unknown"
            out["conflict"] = "fee_zero_vs_paid"
    return out


_RE_FENCE = re.compile(r"^```[a-z]*\s*|\s*```$", re.M)


def _parse_array(raw: str) -> list:
    """Ответ модели — массив объектов. _loose_json_loads возвращает ПЕРВЫЙ
    сбалансированный объект, и на батче из восьми продуктов сохранялся ровно
    один: остальные семь молча терялись. Поэтому сначала пробуем массив."""
    s = _RE_FENCE.sub("", raw).strip()
    i, j = s.find("["), s.rfind("]")
    if i >= 0 and j > i:
        try:
            data = json.loads(s[i:j + 1])
            if isinstance(data, list):
                return data
        except ValueError:
            pass
    try:
        data = _loose_json_loads(s)
    except Exception:  # noqa: BLE001 — пустой/битый ответ не должен ронять сбор
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("products") or [data]
    return data if isinstance(data, list) else []


def _ask(batch: list[dict]) -> dict[int, dict]:
    """batch: [{i, category, title, bank, text}] → {i: payload}."""
    parts = []
    for it in batch:
        parts.append(f'### {it["i"]}. {it["bank"]} — {it["title"]} '
                     f'(категория: {it["category"]})\n{it["text"][:_TEXT_CHARS]}')
    user = _FIELDS + "\n\nПРОДУКТЫ:\n\n" + "\n\n".join(parts)
    resp = _client().chat.completions.create(
        model=fast_model(),
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0.0, max_tokens=int(os.getenv("ENRICH_MAX_TOKENS", "6000")))
    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_array(raw)
    if not data:
        log.warning("обогащение: разбор ответа не удался (finish=%s, ответ=%r)",
                    getattr(resp.choices[0], "finish_reason", "?"), raw[:160])
        return {}
    by_i = {it["i"]: it for it in batch}
    out: dict[int, dict] = {}
    for o in data:
        if not isinstance(o, dict):
            continue
        try:
            i = int(o.get("i"))
        except (TypeError, ValueError):
            continue
        src = by_i.get(i)
        if src:
            out[i] = _clean_item(o, src["category"], src.get("fee"))
    return out


# ── выборка и запись ─────────────────────────────────────────────────────────

_SELECT = """
    SELECT o.offer_id, o.category, o.title, b.name AS bank_name,
           t.fee_service,
           t.raw->>'product_id'  AS product_id,
           t.raw->>'section'     AS section
      FROM product_offer o
      JOIN bank b          ON b.bank_id  = o.bank_id
      JOIN product_terms t ON t.offer_id = o.offer_id AND t.valid_to IS NULL
      LEFT JOIN offer_enrichment e ON e.offer_id = o.offer_id
     WHERE o.category::text = ANY(:cats)
       AND o.is_active
       AND t.raw->>'product_id' IS NOT NULL
       AND t.raw->>'section'    = ANY(:sections)
       AND (e.offer_id IS NULL
            OR e.updated_at < now() - make_interval(days => :max_age)
            OR e.updated_at < t.valid_from)
     ORDER BY (e.offer_id IS NULL) DESC, o.offer_id
     LIMIT :lim
"""


def _pending(cats: Iterable[str], limit: int) -> list[dict]:
    with db.session() as s:
        rows = s.execute(text(_SELECT), {
            "cats": list(cats), "sections": list(_DETAIL_PATH),
            "max_age": _MAX_AGE_D, "lim": limit,
        }).mappings().all()
    return [dict(r) for r in rows]


def _save(offer_id: int, digest: str, payload: dict, model: str) -> None:
    with db.session() as s:
        s.execute(text("""
            INSERT INTO offer_enrichment (offer_id, src_digest, payload, model, updated_at)
            VALUES (:o, :d, CAST(:p AS jsonb), :m, now())
            ON CONFLICT (offer_id) DO UPDATE
               SET src_digest = EXCLUDED.src_digest,
                   payload    = EXCLUDED.payload,
                   model      = EXCLUDED.model,
                   updated_at = now()
        """), {"o": offer_id, "d": digest, "p": json.dumps(payload, ensure_ascii=False),
               "m": model})


def enrich(limit: int = 120, categories: Iterable[str] | None = None) -> dict:
    """Обогатить до limit офферов. Возвращает счётчики для лога сбора."""
    cats = list(categories or CATEGORIES)
    todo = _pending(cats, limit)
    stats = {"selected": len(todo), "fetched": 0, "no_text": 0,
             "enriched": 0, "no_answer": 0, "llm_calls": 0, "skipped_same": 0}
    if not todo:
        return stats
    sess = requests.Session()
    batch: list[dict] = []
    model = fast_model()

    def _flush() -> None:
        nonlocal batch
        if not batch:
            return
        stats["llm_calls"] += 1
        try:
            got = _ask(batch)
        except Exception as e:  # noqa: BLE001 — обогащение не должно ронять сбор
            log.warning("обогащение: вызов LLM не удался (%s)", str(e)[:160])
            batch = []
            return
        for it in batch:
            payload = got.get(it["i"])
            if payload is None:
                # Вызов прошёл, но объекта с этим номером в ответе нет. Пишем
                # пустой разбор с тем же хешем: иначе такой оффер качался бы
                # заново каждый прогон и вечно съедал бы квоту обхода.
                _save(it["offer_id"], it["digest"],
                      {"free_kind": "unknown", "rate_attainability": "unknown",
                       "client_segment": "unknown", "free_conditions": [],
                       "rate_requires": [], "no_answer": True}, model)
                stats["no_answer"] += 1
                continue
            _save(it["offer_id"], it["digest"], payload, model)
            stats["enriched"] += 1
        batch = []

    # уже сохранённые хеши: если текст тарифа не изменился, LLM не зовём
    with db.session() as s:
        known = dict(s.execute(text(
            "SELECT offer_id, src_digest FROM offer_enrichment")).all())

    for n, row in enumerate(todo):
        txt = _fetch(row["section"], row["product_id"], sess)
        time.sleep(_PAUSE_S)
        if not txt:
            stats["no_text"] += 1
            continue
        stats["fetched"] += 1
        digest = hashlib.sha256(txt.encode("utf-8")).hexdigest()
        if known.get(row["offer_id"]) == digest:
            # текст тот же — только двигаем отметку свежести
            _touch(row["offer_id"])
            stats["skipped_same"] += 1
            continue
        batch.append({"i": n, "offer_id": row["offer_id"], "digest": digest,
                      "category": row["category"], "title": row["title"],
                      "bank": row["bank_name"], "text": txt,
                      "fee": _num(row.get("fee_service"))})
        if len(batch) >= _BATCH:
            _flush()
    _flush()
    sess.close()
    return stats


def _touch(offer_id: int) -> None:
    with db.session() as s:
        s.execute(text("UPDATE offer_enrichment SET updated_at = now() "
                       "WHERE offer_id = :o"), {"o": offer_id})
