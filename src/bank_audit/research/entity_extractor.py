"""Entity Extractor — извлекает из вопроса аудитора список (банк, продукт)-пар.

Используется как первая стадия EAV-pipeline. На основе результата:
  • Source Finder пойдёт искать gold sources для каждой entity
  • Matrix Builder проставит entity-строки в финальной матрице

Подход — LLM-only с динамическим списком банков из БД (не хардкод).
"""
from __future__ import annotations
import asyncio, json, logging, re
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import text as _t

from .. import db
from ..ai.deep_research import _loose_json_loads, normalize_question

log = logging.getLogger(__name__)


@dataclass
class Entity:
    """Атомарная единица сравнения: один банк × один продукт."""
    bank_slug: str             # 'sberbank', 'vtb', ...
    bank_name: str             # 'Сбербанк', 'ВТБ', ...
    bank_domain: str | None    # 'sberbank.ru', 'vtb.ru', ...
    product: str               # нормализованное название продукта
    product_synonyms: list[str] = field(default_factory=list)
    audience: str | None = None  # 'пенсионеры', 'ИП', 'участники СВО' — опционально

    def to_dict(self) -> dict:
        return {
            "bank_slug":   self.bank_slug,
            "bank_name":   self.bank_name,
            "bank_domain": self.bank_domain,
            "product":     self.product,
            "product_synonyms": self.product_synonyms,
            "audience":    self.audience,
        }


# Список known-доменов банков (для подсказки LLM и для domain lookup)
_BANK_DOMAINS = {
    "sberbank":     "sberbank.ru",
    "vtb":          "vtb.ru",
    "alfabank":     "alfabank.ru",
    "tinkoff":      "tbank.ru",
    "sovcombank":   "sovcombank.ru",
    "gazprombank":  "gazprombank.ru",
    "rshb":         "rshb.ru",
    "domrf":        "domrfbank.ru",
    "otkritie":     "open.ru",
    "raiffeisen":   "raiffeisen.ru",
    "pochtabank":   "pochtabank.ru",
    "mkb":          "mkb.ru",
    "psb":          "psbank.ru",
    "rosbank":      "rosbank.ru",
    "uralsib":      "uralsib.ru",
    "akbars":       "akbars.ru",
    "mtsbank":      "mtsbank.ru",
    "ozonbank":     "ozon.ru",
    "yandexbank":   "bank.yandex.ru",
    "tochka":       "tochka.com",
    "modulbank":    "modulbank.ru",
}


SYSTEM_PROMPT = """Ты — financial product analyst. Получив вопрос аудитора,
извлекаешь СПИСОК ПАР (банк, продукт), которые нужно сравнить.

ПРАВИЛА:
1) Банки: используй ТОЛЬКО slug'и из переданного списка known_banks
   (нормализуй: "Сбер"→"sberbank", "Т-банк"→"tinkoff", "ГПБ"→"gazprombank").
2) Если в вопросе нет конкретных банков ("сравни ипотеку в крупных банках")
   — возьми топ 4-5 банков из known_banks по релевантности продукту.
3) Продукт: одна НОРМАЛИЗОВАННАЯ короткая фраза.
   ✅ "пенсионная карта", "семейная ипотека", "эквайринг для ИП", "доверенность на распоряжение счётом"
   ❌ "тарифы по картам" (мета-слово), "условия" (мета-слово)
4) Audience: если в вопросе явная категория клиентов — укажи
   ('пенсионеры', 'ИП', 'участники СВО', 'премиум-клиенты'). Иначе null.
5) Product synonyms: 4-8 РАЗНЫХ морф-форм и связанных терминов
   (для русского — окончания падежей; для product — связанные понятия).
   Пример "пенсионная карта": ["пенсионная карта", "карта пенсионера",
   "карта для пенсионеров", "пенсионная", "социальная карта", "pension card"]
   ⚠ НЕ ПОВТОРЯЙ один и тот же синоним — это ломает JSON-парсер.

ВЫХОД: JSON массив entities. Каждый element:
{
  "bank_slug": "...",          // ТОЛЬКО из known_banks
  "product":    "...",          // короткая нормализованная фраза
  "audience":   "..." | null,
  "product_synonyms": [...]    // 4-8 уникальных
}

Если для всех entities продукт ОДИН — повтори в каждом element'е (синонимы
тоже одинаковые). Это структурно проще для downstream.

ВЕРНИ ТОЛЬКО JSON МАССИВ. БЕЗ преамбулы и markdown-fences."""


def _load_banks_from_db(limit: int = 100) -> list[dict]:
    """Топ-N банков по числу отзывов — для подсказки LLM с реальными slug'ами."""
    out: list[dict] = []
    try:
        with db.session() as s:
            rows = s.execute(_t("""
                SELECT b.slug, b.name, COUNT(r.review_id) AS reviews
                  FROM bank b
                  LEFT JOIN review r ON r.bank_id = b.bank_id
                 WHERE b.slug IS NOT NULL AND b.slug NOT LIKE 'unknown_%'
                 GROUP BY b.slug, b.name
                 ORDER BY reviews DESC, b.name
                 LIMIT :lim
            """), {"lim": limit}).all()
        for r in rows:
            out.append({"slug": r.slug, "name": r.name})
    except Exception as e:
        log.warning("entity_extractor _load_banks_from_db failed: %s", e)
    # Гарантируем что hardcoded известные банки в списке (для свежих/маленьких БД)
    known = {b["slug"] for b in out}
    for slug, dom in _BANK_DOMAINS.items():
        if slug not in known:
            out.append({"slug": slug, "name": slug.title()})
    return out


def _parse_json_array(raw: str) -> list | None:
    """Толерантный парсер JSON-массива. Снимает markdown fences, чинит
    обрезанные строки. Возвращает list или None.
    """
    if not raw:
        return None
    # Снимаем markdown fences
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(),
                flags=re.MULTILINE | re.IGNORECASE)
    # Берём первый balanced массив [...]
    start = t.find("[")
    if start < 0:
        return None
    depth = 0; in_str = False; esc = False; end = -1
    for i in range(start, len(t)):
        ch = t[i]
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "[": depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0: end = i + 1; break
    if end < 0:
        # Обрезано — попробуем patch
        candidate = t[start:].rstrip().rstrip(",") + "]"
    else:
        candidate = t[start:end]
    try:
        return json.loads(candidate)
    except Exception:
        pass
    # Trailing-comma чистка
    cleaned = re.sub(r",\s*([\]}])", r"\1", candidate)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def _normalize_dedup_synonyms(raw: list, max_count: int = 10) -> list[str]:
    """Чистим список синонимов: убираем дубликаты, пустые, слишком короткие."""
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for s in raw:
        if not isinstance(s, str):
            continue
        sn = s.strip().lower()
        if not (3 <= len(sn) <= 60):
            continue
        if sn in seen:
            continue
        seen.add(sn)
        out.append(s.strip())  # сохраняем оригинальный регистр
        if len(out) >= max_count:
            break
    return out


def _resolve_bank(slug_or_name: str, banks: list[dict]) -> dict | None:
    """Сводит slug или name к канонической записи банка из БД."""
    if not slug_or_name:
        return None
    s_low = slug_or_name.strip().lower()
    for b in banks:
        if b["slug"].lower() == s_low or (b.get("name") or "").lower() == s_low:
            return b
    return None


async def extract_entities(client: AsyncOpenAI, question: str,
                            model: str | None = None) -> list[Entity]:
    """Главный API. На вход — вопрос аудитора. На выход — список Entity.

    Безопасный fallback при любых ошибках LLM — возвращает пустой список,
    caller должен это проверить и обработать (например, пометить вопрос
    как «общий, без конкретных entities»).
    """
    import os
    q = normalize_question(question)
    if not q.strip():
        return []
    model = model or os.getenv("LLM_MODEL_FAST") or os.getenv("LLM_MODEL_NAME",
                                                                "gpt-4o-mini")
    banks = _load_banks_from_db()
    known_banks_str = ", ".join(f"{b['slug']}({b['name']})" for b in banks[:60])

    user_msg = (
        f"# Вопрос аудитора\n{q}\n\n"
        f"# known_banks (используй ТОЛЬКО эти slug'и)\n{known_banks_str}\n\n"
        f"Верни JSON массив entities."
    )

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=2500,
                temperature=0.1,
            ),
            timeout=30,
        )
    except Exception as e:
        log.warning("entity_extractor LLM call failed: %s", e)
        return []

    raw = (resp.choices[0].message.content or "").strip()
    data = _parse_json_array(raw)
    if not isinstance(data, list) or not data:
        log.warning("entity_extractor parse failed (raw first 200 = %r)", raw[:200])
        return []

    # Конвертим в Entity объекты с валидацией
    entities: list[Entity] = []
    seen_keys: set[tuple[str, str]] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        bank_id = (item.get("bank_slug") or "").strip()
        if not bank_id:
            continue
        bank = _resolve_bank(bank_id, banks)
        if not bank:
            # LLM выдумал slug — пропускаем
            log.info("entity_extractor: unknown bank slug %r, skipping", bank_id)
            continue
        product = (item.get("product") or "").strip()
        if not product:
            continue
        key = (bank["slug"], product.lower())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        entities.append(Entity(
            bank_slug=bank["slug"],
            bank_name=bank.get("name") or bank["slug"].title(),
            bank_domain=_BANK_DOMAINS.get(bank["slug"]),
            product=product,
            product_synonyms=_normalize_dedup_synonyms(item.get("product_synonyms")),
            audience=(item.get("audience") or "").strip() or None,
        ))

    # Fallback: keyword-based bank detection. Если LLM забыл какой-то банк
    # из вопроса (нестабильность model'и), добавим его с общим product
    # из первой entity (LLM правильно понял product, просто пропустил bank).
    try:
        from ..ai.deep_research import detect_bank_slugs
        keyword_banks = detect_bank_slugs(question)
        already = {e.bank_slug for e in entities}
        missing = [b for b in keyword_banks if b not in already]
        if missing and entities:
            template = entities[0]   # копируем product/synonyms из первой entity
            for slug in missing:
                bank = _resolve_bank(slug, banks)
                if not bank: continue
                entities.append(Entity(
                    bank_slug=bank["slug"],
                    bank_name=bank.get("name") or bank["slug"].title(),
                    bank_domain=_BANK_DOMAINS.get(bank["slug"]),
                    product=template.product,
                    product_synonyms=list(template.product_synonyms),
                    audience=template.audience,
                ))
            log.warning("[entity_extractor] keyword fallback added: %s", missing)
    except Exception as e:
        log.info("entity_extractor keyword fallback failed: %s", e)

    if entities:
        log.warning("[entity_extractor] %s entities: %s",
                    len(entities),
                    [(e.bank_slug, e.product[:30]) for e in entities])
    return entities
