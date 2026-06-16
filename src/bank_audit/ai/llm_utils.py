"""LLM-утилиты, общие для живого EAV-пайплайна.

Вынесены из ai/deep_research.py (legacy-пайплайн удалён): это единственные
функции оттуда, которые реально нужны живому пути (analyst → orchestrator →
research/*). Здесь — только детерминированная сантехника, без зависимостей от
удалённого pipeline-кода.

  • _format_llm_error            — человекочитаемая ошибка LLM-вызова
  • _patch_client_reasoning_effort — глобальный reasoning_effort для клиента
  • is_deep_question            — роутинг quick/deep
  • _loose_json_loads           — толерантный JSON-парсер (reasoning-модели)
  • normalize_question          — чистка юникод-артефактов копипаста
  • detect_bank_slugs           — банк-слаги из вопроса (словарь + БД)
"""
from __future__ import annotations
import json, logging, os, re
from typing import Any

from .. import db

log = logging.getLogger(__name__)


# ── Ошибки LLM ───────────────────────────────────────────────────────────────
def _format_llm_error(e: Exception, stage: str = "LLM-вызов") -> str:
    """User-friendly markdown-сообщение об LLM-ошибке (401/402/403/404/429/5xx/timeout)."""
    msg = str(e)
    low = msg.lower()
    s = stage
    if "401" in msg or "invalid_api_key" in low or "authentication" in low:
        return (f"\n\n⚠ **Ошибка {s}: невалидный API-ключ LLM**\n\n"
                f"Проверь `LLM_API_KEY` в `.env`.\n")
    if "402" in msg or "412" in msg or "suspended" in low or "insufficient" in low or "billing" in low or "credit" in low:
        return (f"\n\n⚠ **Ошибка {s}: закончились кредиты / аккаунт приостановлен**\n\n"
                f"Пополни баланс провайдера или смени `LLM_API_KEY`.\n\n"
                f"Технические детали: `{msg[:200]}`\n")
    if "403" in msg or "content" in low and "policy" in low:
        return (f"\n\n⚠ **Ошибка {s}: запрос отклонён content-policy LLM**\n\n"
                f"Переформулируй вопрос или смени модель (`LLM_MODEL_NAME`).\n\n"
                f"Детали: `{msg[:200]}`\n")
    if "404" in msg or "model" in low and "not found" in low:
        return (f"\n\n⚠ **Ошибка {s}: модель не найдена**\n\n"
                f"Проверь `LLM_MODEL_NAME` в `.env`.\n")
    if "429" in msg or "rate" in low and "limit" in low:
        return (f"\n\n⚠ **Ошибка {s}: rate-limit**\n\nПодожди 1-2 минуты и повтори.\n")
    if "timeout" in low or "timed out" in low:
        return (f"\n\n⚠ **Ошибка {s}: timeout (LLM не ответил вовремя)**\n\n"
                f"Повтори вопрос или смени модель на более быструю.\n")
    if "connection" in low or "network" in low or "5" in msg[:3] and any(c in msg[:4] for c in "012345"):
        return (f"\n\n⚠ **Ошибка {s}: проблема с подключением к LLM**\n\n"
                f"Проверь сеть и повтори через минуту.\n\nДетали: `{msg[:200]}`\n")
    return f"\n\n⚠ Ошибка {s}: `{msg[:300]}`\n"


# ── reasoning_effort patch ───────────────────────────────────────────────────
def _patch_client_reasoning_effort(client):
    """Глобально проставляет reasoning_effort ко всем chat.completions.create
    (тюнится LLM_REASONING_EFFORT: low (default)/medium/high/off) И снимает
    несовместимость температуры для семейства GPT-5.

    GPT-5 (gpt-5*) поддерживает ТОЛЬКО temperature=1 (дефолт) — любое иное
    значение даёт 400. Пайплайн ставит temperature=0.0 для детерминизма; для
    gpt-5* убираем её (используется дефолт). Патч всегда оборачивает клиент,
    даже при reasoning_effort=off, чтобы этот фикс работал."""
    effort = os.getenv("LLM_REASONING_EFFORT", "low").lower()
    add_reasoning = effort not in ("off", "none", "")
    orig = client.chat.completions.create

    async def patched(*args, **kwargs):
        if add_reasoning and "reasoning_effort" not in kwargs:
            extra = kwargs.get("extra_body") or {}
            if "reasoning_effort" not in extra:
                kwargs["extra_body"] = {**extra, "reasoning_effort": effort}
        model = str(kwargs.get("model") or (args[0] if args else ""))
        if "gpt-5" in model and kwargs.get("temperature") not in (None, 1):
            kwargs.pop("temperature", None)   # gpt-5* → только дефолтная temperature
        return await orig(*args, **kwargs)

    client.chat.completions.create = patched
    return client


# ── Роутинг quick/deep ───────────────────────────────────────────────────────
_DEEP_TRIGGERS = (
    "сравни", "сравнение", "сопоставь", "vs ", " vs.", " против ",
    "исследование", "проведи анализ", "проведи исследование", "разберись",
    "полный анализ", "полный отчёт", "полный отчет",
    "детальный", "глубокий",
    "конкуренты", "конкурентный анализ", "бизнес-модели", "бизнес-модель",
    "доходы и расходы", "финансовая модель", "audit", "аудит-отчёт", "аудит отчет",
)


def is_deep_question(q: str) -> bool:
    if not q:
        return False
    if len(q) > 180:
        return True
    low = q.lower()
    hits = sum(1 for t in _DEEP_TRIGGERS if t in low)
    if hits >= 1 and len(q) > 40:
        return True
    if hits >= 2:
        return True
    if re.search(r"\b(сравни|сопоставь)\b.*\b(и|с|vs)\b", low):
        return True
    return False


# ── Толерантный JSON-парсер ──────────────────────────────────────────────────
def _scan_balanced_json_objects(s: str) -> list[tuple[int, int]]:
    """Список (start, end+1) для всех balanced top-level `{...}` в строке."""
    positions = []
    depth = 0; start = -1
    in_str = False; esc = False
    for i, ch in enumerate(s):
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "{":
            if depth == 0: start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                positions.append((start, i + 1))
                start = -1
    return positions


def _loose_json_loads(raw: str) -> Any:
    """Толерантный JSON-парсер (reasoning-leak, fences, trailing comma, числа)."""
    if not raw:
        raise ValueError("empty")
    bal_positions = _scan_balanced_json_objects(raw)
    if len(bal_positions) >= 1:
        sorted_pos = sorted(bal_positions, key=lambda x: -(x[1] - x[0]))
        for s_idx, e_idx in sorted_pos[:5]:
            try: return json.loads(raw[s_idx:e_idx])
            except Exception: pass
    try: return json.loads(raw)
    except Exception: pass
    s = raw
    s = re.sub(r"^```(?:json)?\s*", "", s.strip())
    s = re.sub(r"\s*```\s*$", "", s)
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    try: return json.loads(s)
    except Exception: pass
    s2 = re.sub(r",(\s*[}\]])", r"\1", s)
    try: return json.loads(s2)
    except Exception: pass
    depth = 0; last_balanced = -1
    in_str = False; esc = False
    for i, ch in enumerate(s2):
        if esc: esc = False; continue
        if ch == "\\" and in_str: esc = True; continue
        if ch == '"': in_str = not in_str; continue
        if in_str: continue
        if ch == "{": depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                last_balanced = i
    if last_balanced > 0:
        try: return json.loads(s2[:last_balanced + 1])
        except Exception: pass

    def _fix_numbers(text: str) -> str:
        def _clean(m):
            inside = m.group(1)
            cleaned = re.sub(r"\+(\d)", r"\1", inside)
            cleaned = re.sub(r"(\d)\s+(\d)", r"\1\2", cleaned)
            cleaned = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|руб|млн|млрд|тыс|год|мес)",
                              r"\1", cleaned)
            cleaned = re.sub(r"(\d),(\d)", r"\1.\2", cleaned)
            return f'"data":[{cleaned}]'
        return re.sub(r'"data"\s*:\s*\[([^\[\]]*)\]', _clean, text)

    s3 = _fix_numbers(s2)
    if s3 != s2:
        try: return json.loads(s3)
        except Exception: pass

    if s3.lstrip().startswith("["):
        objs = []
        i = s3.find("[") + 1
        n = len(s3)
        while i < n:
            while i < n and s3[i] in " \n\r\t,": i += 1
            if i >= n or s3[i] == "]": break
            if s3[i] != "{": i += 1; continue
            depth, j = 1, i + 1
            in_str_loc, esc = False, False
            while j < n and depth > 0:
                ch = s3[j]
                if esc: esc = False; j += 1; continue
                if ch == "\\" and in_str_loc: esc = True; j += 1; continue
                if ch == '"': in_str_loc = not in_str_loc; j += 1; continue
                if not in_str_loc:
                    if ch == "{": depth += 1
                    elif ch == "}": depth -= 1
                j += 1
            if depth == 0:
                try: objs.append(json.loads(s3[i:j]))
                except Exception: pass
                i = j
            else:
                break
        if objs:
            return objs

    if '"steps"' in s3 and '"n"' in s3:
        try:
            step_objs = re.findall(r'\{[^{}]*?"n"\s*:\s*\d+[^{}]*?\}', s3, re.DOTALL)
            if step_objs:
                return json.loads('{"steps":[' + ",".join(step_objs) + ']}')
        except Exception:
            pass

    raise ValueError("could not parse JSON after all strategies")


# ── Нормализация вопроса ─────────────────────────────────────────────────────
_DASH_TRANSLATE = str.maketrans({
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    " ": " ",   # non-breaking space
    " ": " ",   # narrow no-break space
})


def normalize_question(q: str) -> str:
    """Чистка юникодных артефактов копипаста."""
    if not q:
        return q
    return q.translate(_DASH_TRANSLATE)


# ── Распознавание банков ─────────────────────────────────────────────────────
BANK_SLUG_TRIGGERS = {
    "sberbank":   ["сбер", "sberbank", "сбербанк"],
    "vtb":        ["втб", "vtb"],
    "alfabank":   ["альфа", "alfa", "альфабанк"],
    "tinkoff":    ["тинькофф", "тинков", "т-банк", "тбанк", "tinkoff", "tbank"],
    "sovcombank": ["совком", "sovcom"],
    "gazprombank":["газпромбанк", "gazprombank", "гпб"],
    "rshb":       ["россельхоз", "рсхб", "рсбх", "rshb", "ршб"],
    "domrf":      ["дом.рф", "домрф", "domrf", "дом рф", "банк дом"],
    "otkritie":   ["открытие", "otkritie"],
    "raiffeisen": ["райффайзен", "raiffeisen"],
    "pochtabank": ["почта банк", "почтабанк"],
    "mkb":        ["мкб", "московский кредитный"],
    "psb":        ["псб", "промсвязьбанк"],
    "rosbank":    ["росбанк", "rosbank"],
    "uralsib":    ["уралсиб", "uralsib"],
    "akbars":     ["ак барс", "akbars"],
    "mtsbank":    ["мтс банк", "мтсбанк"],
    "ozonbank":   ["озон банк", "ozonbank"],
    "yandexbank": ["яндекс банк"],
    "tochka":     ["точка банк", "точка", "tochka"],
    "modulbank":  ["модульбанк", "модуль банк", "modulbank", "modul bank"],
    "blanc":      ["бланк банк", "blanc"],
    "qiwi":       ["киви", "qiwi"],
    "rencredit":  ["ренессанс", "ren-credit", "rencredit"],
}

_DYN_BANK_TRIGGERS_CACHE: dict | None = None
_DYN_BANK_TRIGGERS_TS: float = 0.0


def _get_dynamic_bank_triggers() -> dict[str, list[str]]:
    """Дополняет статический BANK_SLUG_TRIGGERS банками из БД (кеш 5 мин)."""
    import time as _t
    global _DYN_BANK_TRIGGERS_CACHE, _DYN_BANK_TRIGGERS_TS
    if _DYN_BANK_TRIGGERS_CACHE is not None and (_t.time() - _DYN_BANK_TRIGGERS_TS) < 300:
        return _DYN_BANK_TRIGGERS_CACHE
    out: dict[str, list[str]] = {k: list(v) for k, v in BANK_SLUG_TRIGGERS.items()}
    try:
        from sqlalchemy import text as _t_sql
        with db.session() as s:
            rows = s.execute(_t_sql(
                "SELECT slug, name FROM bank WHERE slug IS NOT NULL "
                "AND slug NOT LIKE 'unknown_%' LIMIT 500"
            )).all()
        for slug, name in rows:
            if not slug: continue
            existing = out.setdefault(slug, [])
            if slug.lower() not in existing and len(slug) >= 4:
                existing.append(slug.lower())
            if name:
                nlow = name.lower().strip()
                if nlow and nlow not in existing and len(nlow) >= 5:
                    existing.append(nlow)
    except Exception as e:
        log.warning("dynamic bank triggers load failed: %s", e)
    _DYN_BANK_TRIGGERS_CACHE = out
    _DYN_BANK_TRIGGERS_TS = _t.time()
    log.info("dynamic bank triggers: %s банков", len(out))
    return out


def detect_bank_slugs(question: str) -> list[str]:
    """По вопросу извлекает банковские slug'и (словарь + динамика из БД)."""
    if not question:
        return []
    low = normalize_question(question).lower()
    out: list[str] = []
    seen = set()
    triggers = _get_dynamic_bank_triggers()
    for slug, kws in triggers.items():
        if any(k in low for k in kws):
            if slug not in seen:
                out.append(slug); seen.add(slug)
    return out
