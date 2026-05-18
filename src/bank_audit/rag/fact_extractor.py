"""Structured fact extraction: после ingest документа отдельный LLM-call
вытаскивает ключевые факты в структурированный JSON и пишет в bank_feature.

Цель: между прогонами одни и те же числа НЕ re-extract'ятся LLM каждый раз,
а живут в БД. RAG-агент может читать их через get_bank_feature вместо
синтезирования заново.

Извлекаются факты:
  • revenue_2025_rub, revenue_2024_rub
  • net_profit_2025_rub, net_profit_2024_rub
  • ebitda_pct, opex_breakdown
  • mau, dau, employees
  • market_share_pct, founded_year
  • business_model_type
  • main_revenue_streams (массив)

Запускается:
  • Inline: после ingest_document_from_url, если trust ≥0.7
  • Batch: scripts/extract_all_facts.py для исторических документов
"""
from __future__ import annotations
import json, logging, os, re
from typing import Any
from openai import OpenAI
from sqlalchemy import text

from .. import db

log = logging.getLogger(__name__)

LLM_BASE_URL   = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY    = os.getenv("LLM_API_KEY", "")
LLM_MODEL_NAME = os.getenv("LLM_MODEL_NAME", "gpt-4o-mini")


EXTRACTOR_SYSTEM = """Ты — финансовый аналитик-экстрактор. Из текста статьи или
отчёта выделяешь СТРУКТУРИРОВАННЫЕ факты компании.

ИЗВЛЕКАЙ ТОЛЬКО ТО, ЧТО ЯВНО НАПИСАНО. НЕ ВЫДУМЫВАЙ.

Верни JSON массив фактов. Каждый факт:
  {"key": "...", "value": ..., "year": 2025, "currency": "RUB" | null,
   "unit": "млрд" | "млн" | "%" | "штук" | null,
   "claim_text": "точная цитата из источника"}

Допустимые `key`:
  • revenue                   — выручка
  • net_profit                — чистая прибыль
  • ebitda                    — EBITDA
  • ebitda_margin             — % маржинальности
  • opex                      — операционные расходы
  • mau                       — monthly active users
  • dau                       — daily active users
  • employees                 — численность персонала
  • market_share              — доля рынка %
  • founded_year              — год основания
  • business_model_type       — текстом ("classifieds" | "bank" | "ecosystem" | ...)
  • main_revenue_stream       — конкретный поток дохода (можно несколько)

Если ничего не нашёл — верни []. Без preamble. Только JSON массив.

Пример:
[
  {"key":"revenue","value":15.2,"year":2025,"currency":"RUB","unit":"млрд",
   "claim_text":"выручка ЦИАН за 2025 год составила 15,2 млрд рублей"},
  {"key":"market_share","value":25,"year":2025,"unit":"%",
   "claim_text":"доля Домклик на рынке сделок без ипотеки выросла до 25%"}
]"""


def _entity_slug_for_url(url: str) -> str | None:
    """По URL пытается определить про какой банк/сервис документ."""
    if not url:
        return None
    low = url.lower()
    map_keywords = {
        "domclick":   ["domclick","домклик"],
        "cian":       ["cian","циан","ciangroup"],
        "avito":      ["avito","авито"],
        "domrf":      ["domrf","дом.рф","домрф"],
        "sberbank":   ["sberbank","сбер","sber.com"],
        "vtb":        ["vtb","втб"],
        "alfabank":   ["alfa","альфа"],
        "tinkoff":    ["tinkoff","tcs","тинькофф","т-банк"],
    }
    for slug, kws in map_keywords.items():
        if any(k in low for k in kws):
            return slug
    return None


def extract_and_store(document_id: int, content_text: str, url: str,
                       limit_chars: int = 8000) -> int:
    """Запускает LLM-extractor и пишет факты в bank_feature.
    Возвращает число добавленных fact'ов."""
    if not LLM_API_KEY or not content_text:
        return 0
    if len(content_text) < 200:
        return 0

    text_chunk = content_text[:limit_chars]
    slug = _entity_slug_for_url(url)

    try:
        client = OpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
        resp = client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": EXTRACTOR_SYSTEM},
                {"role": "user", "content":
                    f"Источник URL: {url}\nКомпания (если ясно): {slug or '?'}\n\n"
                    f"Текст:\n{text_chunk}\n\nВерни JSON массив фактов."},
            ],
            max_tokens=1500,
            temperature=0.0,
        )
        raw = resp.choices[0].message.content or "[]"
    except Exception as e:
        log.info("fact_extractor LLM call failed: %s", e)
        return 0

    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        return 0
    try:
        facts = json.loads(m.group(0))
    except Exception:
        return 0
    if not isinstance(facts, list):
        return 0

    n_added = 0
    with db.session() as s:
        # Резолвим bank_id по slug, если есть
        bank_id = None
        if slug:
            r = s.execute(text("SELECT bank_id FROM bank WHERE slug=:s"),
                          {"s": slug}).first()
            if r: bank_id = r[0]

        for f in facts:
            if not isinstance(f, dict):
                continue
            key = f.get("key")
            value = f.get("value")
            if not key or value is None:
                continue
            # bank_id обязателен — если slug не определён, пропускаем
            if not bank_id:
                continue
            # Композитный feature_key: "revenue_2025_rub", "market_share_2025"
            year = f.get("year")
            unit = f.get("unit", "")
            currency = f.get("currency", "")
            feature_key = key
            if year:
                feature_key += f"_{year}"
            if currency:
                feature_key += f"_{currency.lower()}"
            payload = {
                "value":      value,
                "unit":       unit,
                "currency":   currency,
                "year":       year,
                "claim_text": f.get("claim_text", ""),
            }
            try:
                s.execute(text("""
                    INSERT INTO bank_feature(
                        bank_id, feature_key, feature_value,
                        confidence, source_url, document_id,
                        extracted_by, extracted_at
                    )
                    VALUES (:b, :k, CAST(:v AS jsonb),
                            :c, :u, :d,
                            'fact_extractor_v1', now())
                    ON CONFLICT (bank_id, feature_key, source_id) DO NOTHING
                """), {
                    "b": bank_id, "k": feature_key,
                    "v": json.dumps(payload, ensure_ascii=False, default=str),
                    "c": 0.85, "u": url, "d": document_id,
                })
                n_added += 1
            except Exception as e:
                log.debug("bank_feature insert failed: %s", e)

    if n_added:
        log.info("fact_extractor %s: +%s facts → bank_feature for %s",
                 url[:60], n_added, slug)
    return n_added
