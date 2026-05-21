"""Deep Research mode: planner → multi-step executor → long-form synthesizer.

Когда срабатывает: классификатор детектит «глубокий» запрос (сравнение,
исследование, audit-style). Иначе используется обычный single-shot путь.

Стримит в SSE события:
  • {type:'mode',          value:'deep'}              — переключение
  • {type:'plan',          steps:[{n,title,tool,query,entity}]}
  • {type:'step_start',    n:N, title, tool}
  • {type:'step_done',     n:N, found:K, sources:[...]}
  • {type:'text',          chunk:'…'}                 — потоковый synthesis
  • {type:'verification',  unverified:[...], confident:K}
  • {type:'chart',         spec:{chartType,labels,datasets,...}}
  • {type:'sources',       sources:[{n,url,bank_name,...}]}
  • {type:'done'}

Все шаги детерминистичны и логируются.
"""
from __future__ import annotations
import asyncio, json, logging, re
from typing import Any, AsyncIterator
from openai import AsyncOpenAI

from .. import db
from .analyst import (
    LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_NAME,
    smart_model, fast_model,
    _run_tool, _extract_sources_from_tool_result,
)

log = logging.getLogger(__name__)


# ── Reasoning-models support ─────────────────────────────────────────────────
# Fireworks-доступные «сильные» модели (kimi-k2p6, deepseek-v4-pro, glm-5p1)
# все reasoning-типа: пишут CoT прямо в content. Договариваемся с моделью
# оборачивать ФИНАЛЬНЫЙ ответ в <answer>...</answer>. Снаружи — её
# рассуждения, мы их игнорируем.

ANSWER_TAG_INSTRUCTION = (
    "\n\nВАЖНО (формат вывода): сначала можешь свободно рассуждать, потом "
    "ОБЯЗАТЕЛЬНО заверни ИТОГОВЫЙ ответ в теги <answer>...</answer>. "
    "Внутри тегов — только финальный markdown/JSON/список БЕЗ метакомментариев. "
    "Снаружи — твои рассуждения (мы их не показываем пользователю)."
)


_ANSWER_RE = re.compile(r"<answer>([\s\S]*?)(?:</answer>|$)", re.IGNORECASE)
_THINK_END_RE = re.compile(r"</think>|</reasoning>|<\|end_thinking\|>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Извлечь финальный ответ из reasoning-leaked output.

    Стратегии (по приоритету):
      1. <answer>...</answer> → берём последний совпавший
      2. </think> или </reasoning> → берём всё после последнего
      3. Иначе — возвращаем текст как есть (модель не reasoning-типа)
    """
    if not text:
        return text
    m = list(_ANSWER_RE.finditer(text))
    if m:
        return m[-1].group(1).strip()
    th = list(_THINK_END_RE.finditer(text))
    if th:
        return text[th[-1].end():].strip()
    return text


class _StreamReasoningFilter:
    """Stream-фильтр для reasoning-моделей.

    Буферизует chunks пока не встретит `<answer>` — и стримит наружу
    ТОЛЬКО содержимое внутри тегов. CoT снаружи отбрасывается.

    Если модель не использует теги (gpt-oss, обычные LLM) — после первого
    разумного объёма буфера (8 KB или 8s) переходит в pass-through и
    стримит как есть. Это zero-breaking-change для не-reasoning моделей.

    Использование:
        flt = _StreamReasoningFilter()
        async for chunk in stream:
            for piece in flt.feed(chunk_text):
                yield piece
        for piece in flt.flush():
            yield piece
    """
    def __init__(self, soft_buffer_bytes: int = 8000):
        self._buf = ""
        self._inside = False        # True после <answer>
        self._done = False          # True после </answer>
        self._passthrough = False   # True если решили что тегов нет
        self._soft_buffer = soft_buffer_bytes

    def feed(self, chunk: str) -> list[str]:
        if not chunk or self._done:
            return []
        if self._passthrough:
            return [chunk]
        self._buf += chunk
        out: list[str] = []

        if not self._inside:
            # Ищем открывающий <answer>
            low = self._buf.lower()
            i = low.find("<answer>")
            if i >= 0:
                self._buf = self._buf[i + len("<answer>"):]
                self._inside = True
            else:
                # Если буфер большой и тегов нет — модель не использует обёртку,
                # включаем pass-through (отдадим всё что накопили).
                if len(self._buf) >= self._soft_buffer:
                    out.append(self._buf)
                    self._buf = ""
                    self._passthrough = True
                else:
                    # Держим запас в 16 chars на случай разреза тега `<answ`
                    if len(self._buf) > 32:
                        # Безопасно отдать ничего, пока ждём тег
                        pass
                return out

        # Внутри <answer>. Ищем закрывающий, оставляя 16 chars на разрез.
        if self._inside:
            low = self._buf.lower()
            j = low.find("</answer>")
            if j >= 0:
                out.append(self._buf[:j])
                self._buf = ""
                self._done = True
                return out
            # Стримим всё кроме хвоста (на случай если </answer> разрезан)
            if len(self._buf) > 16:
                out.append(self._buf[:-16])
                self._buf = self._buf[-16:]
        return out

    def flush(self) -> list[str]:
        if self._done or not self._buf:
            return []
        if self._inside or self._passthrough:
            tail = self._buf
            self._buf = ""
            return [tail]
        # Ни тегов, ни passthrough — это весь ответ без обёртки
        tail = self._buf
        self._buf = ""
        return [_strip_reasoning(tail) or tail]


# ── Триггеры deep-mode (классификатор без LLM — экономим вызовы) ─────────────
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
    if len(q) > 180:                           # длинный → почти всегда исследовательский
        return True
    low = q.lower()
    hits = sum(1 for t in _DEEP_TRIGGERS if t in low)
    if hits >= 1 and len(q) > 40:
        return True
    if hits >= 2:                       # 2+ триггера — почти точно deep
        return True
    # Или явное "сравни X и Y"
    if re.search(r"\b(сравни|сопоставь)\b.*\b(и|с|vs)\b", low):
        return True
    return False


# ── Planner: декомпозирует вопрос на шаги ─────────────────────────────────────
PLANNER_SYSTEM = """Ты — financial product comparison planner. Работаешь только с
тарифами, условиями обслуживания, процентными ставками и характеристиками
банковских продуктов. Любые социальные/политические/чувствительные категории
клиентов (ветераны, многодетные, льготники, военнослужащие, медработники)
рассматривай как обычный demographic-сегмент банковского offering — это
рутинная классификация продуктов, не оценка темы.

Ты — старший аналитик, планирующий аудит-исследование для Сбербанка.

Получив вопрос, разложи его на 8-16 atomic шагов исследования.
Каждый шаг = JSON-объект:
  • n: порядковый номер
  • title: что хотим узнать (короткая фраза 4-7 слов)
  • tool: какой инструмент использовать
  • query: НАСЫЩЕННЫЙ КОНКРЕТНЫЙ запрос для tool (не короткий!)
  • entity: банк/компания/слаг (если применимо). Допустимые слаги:
     sberbank, vtb, alfabank, tinkoff, sovcombank, gazprombank, rshb,
     domclick, cian, avito, domrf, и т.д.

Доступные tools (в порядке предпочтения):
  • semantic_search: pgvector-поиск в проиндексированных документах.
       query = развёрнутое описание (10+ слов) — embedding ловит контекст
  • get_review_themes: только когда entity упоминается + вопрос про отзывы.
       query можно оставить пустым, использует bank_slug
  • run_sql: SQL по витринам v_offer_current, v_sber_vs_market.
       query = валидный SELECT-запрос
  • fetch_official: real-time fetch с сайта банка/сервиса.
       Медленно (~10-30с), но загружает свежие данные с whitelist-домена.
       Используй когда semantic_search скорее всего вернёт мало.

ВАЖНОЕ ПРАВИЛО: если в вопросе упоминаются СЕРВИСЫ-НЕ-БАНКИ (ЦИАН, Авито,
Домклик, ДОМ.РФ, Яндекс Недвижимость и т.д.), они ТОЧНО НЕ В ИНДЕКСЕ.
В этом случае на каждую такую entity первым шагом должен идти fetch_official
с домашней страницей (либо semantic_search — система сама сделает fallback
через web_search).

Стратегия — РАЗНООБРАЗИЕ углов исследования:
  • Не повторяй однотипные шаги «X-доходы / Y-доходы / Z-доходы»
  • Для КАЖДОЙ entity покрывай 4-6 РАЗНЫХ аспектов:
    1. ФИНАНСЫ — выручка, прибыль, EBITDA, маржинальность (semantic_search/run_sql)
    2. БИЗНЕС-МОДЕЛЬ — монетизация, тип бизнеса, ARPU
    3. РЫНОЧНОЕ ПОЗИЦИОНИРОВАНИЕ — доля рынка, аудитория, MAU
    4. КОНКУРЕНТЫ — кто является конкурентом, сравнение
    5. СТРАТЕГИЯ — планы развития, M&A, продуктовая стратегия
    6. РЕГУЛЯТОР/РИСКИ — лицензии, compliance, риски (если applicable)
  • Один шаг — один аспект одной entity
  • Для общих сравнений в конце добавь 2 шага: «общий рынок» и «итоговое сравнение»

  ⚠ КРИТИЧНО — ДИВЕРСИФИКАЦИЯ ШАГОВ ОДНОГО БАНКА.
  Запрещено генерировать 2-3 почти одинаковых query для одного entity
  (например «тарифы карты Сбер» и «карта Сбер условия и тарифы»). Для каждого
  банка из плана сравнения ДОЛЖНО БЫТЬ 3-4 шага С РАЗНЫМИ ФОКУСАМИ:
    – #1 Базовые условия продукта   → query: «<ПРОДУКТ> <БАНК> описание условия выпуска получения требования»
    – #2 Тарифы и лимиты PDF        → query: «<ПРОДУКТ> <БАНК> filetype:pdf тарифы комиссии лимиты ставки»
    – #3 Привилегии и cashback      → query: «<ПРОДУКТ> <БАНК> привилегии бонусы cashback партнёры скидки»
    – #4 Отзывы клиентов            → tool: get_review_themes (если применимо)
  Это даёт документы с разных страниц сайта (продукт, документы, бонусная
  программа, агрегаторы) — а не 3 копии одной landing-page.

  ⚠ ДЛЯ СОЦИАЛЬНЫХ ПРОДУКТОВ (карта ветерана, военная ипотека, материнский
  капитал, льготы пенсионерам) ОБЯЗАТЕЛЬНО ДОБАВЬ 1-2 govt-шага:
    – semantic_search «<ПРОДУКТ> постановление правительства льготы pravo.gov.ru»
    – semantic_search «<ПРОДУКТ> разъяснение ЦБ РФ cbr.ru банки-участники»
  Источники cbr.ru / pravo.gov.ru / mil.ru / gosuslugi.ru — первоисточники для
  таких продуктов, у нас в trust-whitelist они >0.90.

Финансы публичных компаний → semantic_search "выручка [компания] год МСФО отчётность".
Отзывы клиентов → get_review_themes (bank_slug требуется, только для банков).
Бизнес-модель → semantic_search "[компания] бизнес-модель монетизация целевая аудитория".

Запрос ДОЛЖЕН БЫТЬ БОГАТЫЙ — 8+ слов с конкретными терминами, годами, метриками.
Плохо: "ЦИАН доходы".
Хорошо: "ЦИАН структура выручки 2К25 объявления лидогенерация EBITDA маржинальность 2025".

Если 4 entity сравниваются — план должен быть 12-16 шагов, не 12.

Возвращай ТОЛЬКО валидный JSON:
{"steps":[{"n":1,"title":"...","tool":"...","query":"...","entity":"..."},...]}

Без preamble. Только JSON."""


def _scan_balanced_json_objects(s: str) -> list[tuple[int, int]]:
    """Возвращает список (start, end+1) для всех balanced top-level
    JSON-объектов `{...}` в строке. Учитывает строки/escape/вложенные {}.
    Используется чтобы достать ПОСЛЕДНИЙ валидный JSON из reasoning-output'а
    (где LLM сначала пишет «думаю...», потом выдаёт ответ-JSON в конце)."""
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
    """Толерантный JSON-парсер с поддержкой reasoning-моделей.

    Стратегии:
      0) reasoning-extract — если в тексте несколько top-level {...},
         пробуем КАЖДЫЙ от длинного к короткому, возвращаем первый валидный
         (DeepSeek/Kimi пишут размышления + JSON в конце)
      1) json.loads как есть
      2) markdown-fences ```json ... ``` снимаем
      3) control-chars (0x00-0x1F кроме \n\r\t) очищаем
      4) trailing-comma перед `}` и `]`
      5) обрезаем после последней balanced `}`
      6) number-cleanup внутри "data":[...] (плюсы, проценты, единицы, comma-dec)
      7) array-aware recovery — balanced scanner для outer `[...]`
      8) regex по step-объектам в {"steps":[...]}
    """
    if not raw:
        raise ValueError("empty")

    # Strategy 0: reasoning-extract — берём ПОСЛЕДНИЙ непустой balanced {...}
    # Это спасает от reasoning-моделей которые пишут промпт-эссе перед JSON.
    # Идём от КОНЦА: последний крупный объект — обычно ответ.
    bal_positions = _scan_balanced_json_objects(raw)
    if len(bal_positions) >= 1:
        # От самого крупного к самому маленькому — больше шанс что content-полный
        sorted_pos = sorted(bal_positions, key=lambda x: -(x[1]-x[0]))
        for s_idx, e_idx in sorted_pos[:5]:
            try: return json.loads(raw[s_idx:e_idx])
            except Exception: pass

    # Strategy 1
    try: return json.loads(raw)
    except Exception: pass
    s = raw
    # Strategy 3: fences
    s = re.sub(r"^```(?:json)?\s*", "", s.strip())
    s = re.sub(r"\s*```\s*$", "", s)
    # Strategy 4: control chars
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", s)
    try: return json.loads(s)
    except Exception: pass
    # Strategy 2: trailing-comma cleanup
    s2 = re.sub(r",(\s*[}\]])", r"\1", s)
    try: return json.loads(s2)
    except Exception: pass
    # Strategy 5: truncate to last balanced }
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
        try: return json.loads(s2[:last_balanced+1])
        except Exception: pass

    # Strategy 6: number-cleanup. LLM часто пишет invalid-JSON числа:
    # `+15` (leading plus), `20%`, `13,5` (comma decimal), `1 000` (space sep).
    # Чистим внутри `[...]` data-массивов чтобы получить валидные числа.
    def _fix_numbers(text: str) -> str:
        # data:[+15,-4,20%] → data:[15,-4,20]
        def _clean(m):
            inside = m.group(1)
            # Убираем + перед числом, %/единицы после, пробелы внутри числа
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

    # Strategy 7: array-aware recovery. Если outer — массив `[{...},{...}]`,
    # ищем все цельные top-level объекты внутри массива через scanner с учётом
    # вложенных скобок и строк.
    if s3.lstrip().startswith("["):
        objs = []
        i = s3.find("[") + 1
        n = len(s3)
        while i < n:
            # Скип whitespace/запятых
            while i < n and s3[i] in " \n\r\t,": i += 1
            if i >= n or s3[i] == "]": break
            if s3[i] != "{": i += 1; continue
            # Читаем balanced объект {...} учитывая вложенные {}, [], строки
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
                obj_str = s3[i:j]
                try:
                    objs.append(json.loads(obj_str))
                except Exception:
                    pass
                i = j
            else:
                break  # объект оборван — заканчиваем
        if objs:
            return objs

    # Strategy 8: для planner'а {"steps":[...]} — regex по n:число
    if '"steps"' in s3 and '"n"' in s3:
        try:
            step_objs = re.findall(r'\{[^{}]*?"n"\s*:\s*\d+[^{}]*?\}', s3, re.DOTALL)
            if step_objs:
                return json.loads('{"steps":[' + ",".join(step_objs) + ']}')
        except Exception:
            pass

    raise ValueError("could not parse JSON after all strategies")


async def _condense_long_question(client: AsyncOpenAI, question: str) -> str:
    """Длинные вопросы (>3000 chars) с предысторией съедают context планнера
    и max_tokens, из-за чего JSON-план обрывается. Сжимаем такой вопрос
    одним коротким LLM-вызовом до сути: «что именно хочет аудитор + структура
    + упомянутые сущности», в пределах 800 chars."""
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content":
                  "Извлеки из ДЛИННОГО запроса аудитора суть в 5-10 строк: "
                  "1) что именно хочет узнать (главный вопрос), "
                  "2) какие банки/сервисы упомянуты, "
                  "3) какая структура отчёта запрошена (если есть). "
                  "Сохрани все числа и названия. БЕЗ преамбулы, без markdown."},
                {"role": "user", "content": question[:12000]},
            ],
            max_tokens=900, temperature=0.0,  # reasoning + краткая суть
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if len(out) > 80 else question
    except Exception as e:
        log.info("question-condense failed: %s", e)
        return question


async def _llm_planner(client: AsyncOpenAI, question: str) -> list[dict]:
    """Вызывает LLM-планировщик с hard-timeout.
    Tolerant к битому JSON: 9 стратегий парсинга в _loose_json_loads.
    Для длинных вопросов (>3000 chars) — pre-condense.
    Reasoning-модели (DeepSeek/Kimi) медленнее — таймаут 120s."""
    try:
        planner_input = question
        if len(question) > 3000:
            log.warning("[planner] long question (%s chars) → condensing", len(question))
            try:
                condensed = await asyncio.wait_for(
                    _condense_long_question(client, question), timeout=45)
            except asyncio.TimeoutError:
                log.warning("[planner] condense 45s timeout, using truncated original")
                condensed = question[:2000]
            planner_input = (
                f"СУТЬ ВОПРОСА АУДИТОРА (сжато):\n{condensed}\n\n"
                f"---\nОригинал (для деталей):\n{question[:1500]}…"
            )
        log.warning("[planner] calling LLM (input %s chars, max_tokens=5000)…",
                     len(planner_input))
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=LLM_MODEL_NAME,
                messages=[
                    {"role": "system", "content": PLANNER_SYSTEM},
                    {"role": "user",   "content": planner_input},
                ],
                max_tokens=5000,
                temperature=0.2,
            ),
            timeout=120,    # reasoning-модели на сложных запросах = 60-100s
        )
        text = resp.choices[0].message.content or ""
        # Достаём первый JSON-объект из текста
        m = re.search(r'\{[\s\S]*\}', text)
        if not m:
            log.warning("planner: no JSON in LLM response (first 200 chars: %r)",
                         text[:200])
            return []
        json_str = m.group(0)
        try:
            data = _loose_json_loads(json_str)
        except Exception as e:
            log.warning("planner JSON parse failed (%s); raw first 300: %r",
                         e, json_str[:300])
            return []
        steps = data.get("steps") or []
        # Валидация и нормализация
        clean = []
        for i, s in enumerate(steps):
            if not isinstance(s, dict):
                continue
            tool = s.get("tool", "semantic_search")
            if tool not in ("semantic_search", "get_review_themes",
                            "run_sql", "fetch_official"):
                tool = "semantic_search"
            clean.append({
                "n":      i + 1,
                "title":  str(s.get("title") or f"Step {i+1}")[:120],
                "tool":   tool,
                "query":  str(s.get("query") or s.get("title") or "")[:300],
                "entity": s.get("entity") or s.get("bank_slug") or None,
            })
        return clean[:12]
    except asyncio.TimeoutError:
        log.warning("[planner] HARD TIMEOUT 120s — LLM zависает или очень медленный")
        return []
    except Exception as e:
        log.warning("planner failed: %s", e)
        return []


# ── Bank slug detection (для auto-inject reviews) ────────────────────────────
# Маппинг русских/английских триггеров → slug в БД.
# Этот набор используется для:
#   1. auto-inject get_review_themes когда вопрос содержит триггер «отзывы/плюсы/минусы»
#   2. ограничения semantic_search до банковских документов когда вопрос — про банки
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
}

# Триггеры «вопрос про отзывы / клиентский опыт»
REVIEWS_QUESTION_TRIGGERS = (
    "плюс", "минус", "отзыв", "жалоб", "нрави", "неудобн",
    "претензи", "проблем", "клиентск", "удобств", "сервис",
)


def get_topical_reviews(bank_slug: str, topic: str,
                          *, limit: int = 25,
                          synonyms: list[str] | None = None,
                          max_age_days: int = 365) -> dict:
    """Topic-aware отзывы из ДВУХ источников:

    1) Таблица `review` — наш исторический scrape banki.ru
    2) `document_chunk` от banki.ru/sravni.ru/otzovik — JSON-LD парсер
       извлекает структурированные Review-объекты с rating и body.

    Если первый источник пуст (отзывы в БД общие, не упоминают тему) — fallback
    к чанкам, чтобы получить хоть какие-то тематические отзывы. Это критично:
    раньше для специфических продуктов get_topical_reviews всегда возвращал 0,
    хотя в JSON-LD chunks был валидный контент.

    Аргументы:
      synonyms — если передан, использует его (из query_resolver).
                  Иначе fallback на _TOPIC_SYNONYMS[topic].
    """
    from sqlalchemy import text as _t
    raw_syns = synonyms or _TOPIC_SYNONYMS.get(topic, [topic.lower()])
    # Токенизируем: фразы вроде «мобильное приложение» режем на отдельные слова,
    # иначе ILIKE требует точную фразу и теряет 90% реальных отзывов где
    # клиент пишет «приложение лагает» без слова «мобильное». Stop-words
    # (предлоги, союзы) фильтруем.
    _STOP = {"и","в","на","по","за","для","или","к","с","от","до","из","о","об",
             "the","a","an","of","to","for","in","on","with","and"}
    # Разделяем single-word vs phrase. Single-word через ILIKE поймает
    # любые формы (карта, картой, картам), phrase требует точного match
    # — ненадёжно. Приоритет коротким.
    # Resolver-LLM сам отдаёт морф-формы (вклад/вклада/вкладов/ветеранск/...)
    # — см. RESOLVER_SYSTEM. Никаких эвристик stem-prefix здесь.
    single: set[str] = set()
    phrases: set[str] = set()
    for s in raw_syns or []:
        if not s: continue
        s_low = s.lower().strip()
        if not (3 <= len(s_low) <= 40) or s_low in _STOP:
            continue
        if " " in s_low or "-" in s_low:
            phrases.add(s_low)
        else:
            single.add(s_low)
    syns = sorted(single, key=len)[:12]
    if len(syns) < 16:
        syns += sorted(phrases, key=len, reverse=True)[:16-len(syns)]
    if not syns:
        syns = [topic.lower()]

    # ── Source 1: review-таблица ──────────────────────────────────
    text_clauses_r = " OR ".join(f"r.text ILIKE :kw{i}" for i in range(len(syns)))
    params: dict = {"slug": bank_slug, "lim": limit}
    for i, kw in enumerate(syns):
        params[f"kw{i}"] = f"%{kw}%"
    # P1.6 time-aware: для product/service-вопросов отзывы старше года часто
    # неактуальны (тарифы менялись). Можно расширить max_age_days для
    # стратегических вопросов через параметр.
    params["max_age"] = max_age_days
    sql_reviews = f"""
        SELECT r.text, r.rating, r.posted_at, r.source_url, 'review_table' AS src
          FROM review r
          JOIN bank b ON b.bank_id = r.bank_id
         WHERE b.slug = :slug
           AND r.status = 'active'
           AND (r.posted_at IS NULL
                OR r.posted_at >= now() - make_interval(days => :max_age))
           AND ({text_clauses_r})
         ORDER BY
           CASE WHEN r.rating <= 2 THEN 0 ELSE 1 END,
           r.posted_at DESC NULLS LAST
         LIMIT :lim
    """

    # ── Source 2: document_chunk от агрегаторов отзывов (JSON-LD parsed) ──
    # У JSON-LD reviews текст в chunk выглядит как:
    #   ## Заголовок\nОценка: 1/5 · Автор: ... · Дата: ...\n<тело отзыва>
    # Парсим rating из «Оценка: X/5» когда оно есть.
    text_clauses_c = " OR ".join(f"dc.text ILIKE :kw{i}" for i in range(len(syns)))
    sql_chunks = f"""
        SELECT dc.text, d.url AS source_url, d.fetched_at AS posted_at,
               'json_ld_chunk' AS src
          FROM document_chunk dc
          JOIN document d ON d.document_id = dc.document_id
          JOIN bank b ON b.bank_id = d.bank_id
         WHERE b.slug = :slug
           AND (d.url ILIKE '%banki.ru/services/responses%'
                OR d.url ILIKE '%sravni.ru/bank/%/otzyvy%'
                OR d.url ILIKE '%otzovik.com%'
                OR d.url ILIKE '%irecommend.ru%')
           AND ({text_clauses_c})
           AND dc.text NOT ILIKE 'Оставьте отзыв%'   -- chrome page
         ORDER BY d.fetched_at DESC NULLS LAST
         LIMIT :lim
    """

    rows: list[dict] = []
    try:
        with db.session() as s:
            r1 = s.execute(_t(sql_reviews), params).mappings().all()
            for r in r1:
                rows.append(dict(r))
            # Если из review-таблицы получили мало (<5) — пробуем JSON-LD чанки
            if len(rows) < 5:
                r2 = s.execute(_t(sql_chunks), params).mappings().all()
                for r in r2:
                    d = dict(r)
                    d["rating"] = _parse_rating_from_chunk(d.get("text") or "")
                    rows.append(d)
                    if len(rows) >= limit:
                        break
    except Exception as e:
        log.info("get_topical_reviews failed for %s/%s: %s", bank_slug, topic, e)
        return {"bank_slug": bank_slug, "topic": topic, "found": 0,
                "error": str(e)[:200]}

    # Группируем по rating (если есть). Дедуп по первым 200 chars текста.
    seen_keys: set[str] = set()
    negative: list[dict] = []
    positive: list[dict] = []
    neutral: list[dict] = []
    for r in rows:
        txt = (r.get("text") or "").strip()
        if not txt or len(txt) < 30:
            continue
        key = txt[:200].lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        item = {"text": txt[:700],
                "rating": r.get("rating"),
                "url": r.get("source_url"),
                "src": r.get("src")}
        rt = r.get("rating") or 0
        if rt and rt <= 2:
            negative.append(item)
        elif rt and rt >= 4:
            positive.append(item)
        else:
            neutral.append(item)

    return {"bank_slug": bank_slug, "topic": topic,
            "found": len(negative) + len(positive) + len(neutral),
            "negative_reviews": negative[:8],
            "positive_reviews": positive[:5],
            "neutral_reviews":  neutral[:5]}


_RATING_RE = re.compile(r"оценка:\s*(\d)\s*/\s*5", re.IGNORECASE)


# ── Pre-synth claim verifier (P0.2) ──────────────────────────────────────────
# Регекс достаёт числовые токены из текста: проценты, рубли, годы, числовые
# диапазоны, miscellaneous numbers. Используется чтобы убедиться что КАЖДОЕ
# число из fact-extraction присутствует в excerpts цитированного source'а.
_NUM_TOKEN_RE = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:%|процент|руб|₽|млн|млрд|тыс|долл|\$|€|год|лет|"
    r"мес|дн|дней|days|years|months|pp|п\.п\.|базисн|раз|шт|кв\.?\s*м|кг|т)?",
    re.IGNORECASE,
)
_CITE_RE = re.compile(r"\[(\d{1,3})\]")


def _normalize_number(s: str) -> str:
    """Нормализация числа: '13,14%' → '1314', '50 000 ₽' → '50000', чтобы
    сравнить через простое substring match. Убирает пробелы/comma/единицы.
    """
    if not s:
        return ""
    # Только цифры — самое надёжное
    digits = re.sub(r"[^\d]", "", s)
    return digits


def _verify_fact_line(line: str, sources_by_n: dict[int, dict]) -> tuple[bool, str]:
    """Проверяет одну строку fact-extraction.

    Логика:
      • Достаём все [N] цитаты из строки
      • Достаём все числовые токены из строки (минимум 2 цифры)
      • Для КАЖДОГО числа ищем подстроку (нормализованные digits) в excerpts
        ВСЕХ цитированных source'ов. Если хотя бы один source содержит число —
        ОК. Если ни один — fact галлюцинирован.

    Возвращает (verified: bool, reason: str). Если в строке нет чисел —
    автоматически verified=True (качественные утверждения не проверяются
    числово, проверка тематики уже в matches_topic_generic).
    """
    cites = [int(m) for m in _CITE_RE.findall(line)]
    if not cites:
        return False, "no citation"
    # КРИТИЧНО: убрать [N]-цитаты ДО извлечения чисел, иначе номера ссылок
    # (например [14]) попадут в raw_numbers и regex будет искать «14» в
    # excerpts источников — а там этого числа нет, и ВСЯ строка дропается
    # как «галлюцинированная». Эта баг отбрасывал 60-90% валидных фактов.
    line_no_cites = _CITE_RE.sub(" ", line)
    # Числа в строке. Min-длина зависит от наличия единицы измерения:
    #   • «5%», «3 года», «12 мес» → значимо даже при 1-значном числе
    #   • «5» в свободном тексте → слишком частое, шумит → требуем 2+ цифры
    raw_numbers = []
    for m in _NUM_TOKEN_RE.finditer(line_no_cites):
        token = m.group(0).strip()
        digits = _normalize_number(token)
        if not digits:
            continue
        # Есть ли единица измерения (захвачена тем же regex'ом после цифр)?
        has_unit = bool(re.search(r"[^\d\s.,]", token))
        if has_unit or len(digits) >= 2:
            raw_numbers.append(digits)
    if not raw_numbers:
        return True, "no numbers (qualitative)"
    # Excerpts всех цитированных sources
    pooled = ""
    for n in cites:
        s = sources_by_n.get(n)
        if not s:
            continue
        for ex in (s.get("excerpts") or []):
            pooled += " " + ex.lower()
    pooled_digits = _normalize_number(pooled)
    if not pooled_digits:
        # Нет excerpts вообще (может быть сразу после deep-dive где chunks
        # загружены, но excerpts на source-объекте не накопились). Толерантно.
        return True, "no excerpts to verify against (skip)"
    # Каждое число должно быть подстрокой
    missing = [n for n in raw_numbers if n not in pooled_digits]
    if not missing:
        return True, "ok"
    return False, f"missing numbers: {missing[:3]}"


def filter_verified_facts(bank_facts: dict[str, str],
                            sources: list[dict]) -> tuple[dict[str, str], list[dict]]:
    """Фильтрует bank_facts: оставляет только строки где числа подтверждены
    в excerpts. Возвращает (filtered_facts, dropped_log).

    Это P0.2 фикс — synthesizer получает на вход только проверенные факты,
    физически не может галлюцинировать «13,5%» если в источниках только «13,14%».
    """
    sources_by_n = {s["n"]: s for s in sources if s.get("n")}
    filtered: dict[str, str] = {}
    dropped: list[dict] = []
    for slug, txt in bank_facts.items():
        kept_lines = []
        for line in (txt or "").splitlines():
            line = line.rstrip()
            if not line.strip():
                continue
            ok, reason = _verify_fact_line(line, sources_by_n)
            if ok:
                kept_lines.append(line)
            else:
                dropped.append({"bank": slug, "line": line[:200], "reason": reason})
        if kept_lines:
            filtered[slug] = "\n".join(kept_lines)
    return filtered, dropped


# ── Conflict detection (P0.3) ────────────────────────────────────────────────
_NUMBER_WITH_UNIT_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(%|руб|₽|млн|млрд|тыс|год|мес|дней)",
    re.IGNORECASE,
)


def detect_conflicts(bank_facts: dict[str, str]) -> list[dict]:
    """Простой детектор противоречий: если у ОДНОГО банка в fact-extraction
    есть несколько строк с разными числами для (предположительно) одного
    параметра — флагит. Использует ключевые слова-метрики (ставка/комиссия/
    срок) для группировки.

    Возвращает [{bank, metric, values:[(num, unit, snippet)], severity}, ...].
    Synthesizer обязан показать все варианты явно с пометкой 'разнобой'.
    """
    METRIC_KEYWORDS = {
        "ставка":        ["ставк", "процент", "годовых", "rate"],
        "комиссия":      ["комисси", "fee", "плата за"],
        "минимум":       ["минимальн", "минимум", "от ", "не менее"],
        "максимум":      ["максимальн", "до ", "не более"],
        "срок":          ["срок", "месяц", "лет", "дней"],
    }
    conflicts: list[dict] = []
    for slug, txt in bank_facts.items():
        # Группируем строки fact'ов по метрикам
        by_metric: dict[str, list[tuple[str, str, str]]] = {}
        for line in (txt or "").splitlines():
            low = line.lower()
            for metric, kws in METRIC_KEYWORDS.items():
                if not any(k in low for k in kws):
                    continue
                for m in _NUMBER_WITH_UNIT_RE.finditer(line):
                    num = m.group(1).replace(",", ".")
                    unit = m.group(2).lower()
                    # Дедуп по (num, unit) внутри метрики
                    bucket = by_metric.setdefault(metric, [])
                    if not any(n == num and u == unit for n, u, _ in bucket):
                        bucket.append((num, unit, line[:160]))
                break  # одна метрика на строку — самая ранняя
        for metric, values in by_metric.items():
            if len(values) >= 2:
                # Конфликт ТОЛЬКО если числа реально разные
                unique_nums = {v[0] for v in values}
                if len(unique_nums) >= 2:
                    conflicts.append({
                        "bank": slug, "metric": metric,
                        "values": values[:4],
                        "severity": "high" if len(unique_nums) > 2 else "mid",
                    })
    return conflicts


def _parse_rating_from_chunk(text: str) -> int | None:
    """Достаём rating из JSON-LD chunk текста ('Оценка: 1/5'). Иначе None."""
    if not text:
        return None
    m = _RATING_RE.search(text[:300])
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return None
    return None


def detect_bank_slugs(question: str) -> list[str]:
    """По вопросу извлекает банковские slug'и через словарь триггеров.
    Возвращает уникальный список в порядке появления."""
    if not question:
        return []
    low = question.lower()
    out: list[str] = []
    seen = set()
    for slug, kws in BANK_SLUG_TRIGGERS.items():
        if any(k in low for k in kws):
            if slug not in seen:
                out.append(slug); seen.add(slug)
    return out


def question_wants_reviews(question: str) -> bool:
    """True если вопрос упоминает плюсы/минусы/отзывы/etc."""
    if not question:
        return False
    low = question.lower()
    return any(t in low for t in REVIEWS_QUESTION_TRIGGERS)


_WHITELIST_CACHE: list[str] | None = None
_WHITELIST_CACHE_TS: float = 0


def _get_dynamic_whitelist(min_weight: float = 0.55) -> list[str]:
    """Берёт verified-домены из БД. Кэширует на 60 сек."""
    global _WHITELIST_CACHE, _WHITELIST_CACHE_TS
    import time as _t
    if _WHITELIST_CACHE and (_t.time() - _WHITELIST_CACHE_TS) < 60:
        return _WHITELIST_CACHE
    try:
        from sqlalchemy import text as _txt
        with db.session() as s:
            rows = s.execute(_txt("""
                SELECT domain FROM source_trust
                 WHERE weight >= :w AND domain IS NOT NULL AND domain != ''
                 ORDER BY weight DESC
            """), {"w": min_weight}).all()
        domains = [r[0] for r in rows]
    except Exception as e:
        log.warning("dynamic whitelist load failed: %s", e)
        from ..rag.trust import KNOWN_BANK_DOMAINS
        domains = list(KNOWN_BANK_DOMAINS.keys())
    _WHITELIST_CACHE = domains
    _WHITELIST_CACHE_TS = _t.time()
    return domains


def _enrich_citations_with_corroboration(text: str, sources: list[dict]) -> str:
    """Cross-validation pass: для каждой одиночной цитаты [N] ищем 2-3
    corroborating sources в БД (semantic поиск по контексту вокруг [N]),
    и расширяем до [N][M][K] если они подтверждают то же утверждение.

    Стратегия:
      1. Извлекаем contexts: 80 chars ДО [N] + 30 chars ПОСЛЕ → claim_context
      2. Для каждого claim_context делаем semantic_search top-5
      3. Берём те sources чей trust ≥0.55 и URL ≠ исходного [N]
      4. Добавляем 1-2 corroborating refs

    Без backend-вызова — используем только имеющиеся в sources URL'ы.
    """
    if not text or not sources or len(sources) < 2:
        return text
    src_by_n = {s["n"]: s for s in sources if s.get("n")}
    src_by_url = {s.get("url"): s["n"] for s in sources if s.get("url")}
    if not src_by_n:
        return text

    # Lazy import — heavy embeddings
    try:
        from ..rag import embedder
        from sqlalchemy import text as _t
    except Exception:
        return text

    # Найдём все [N] в тексте с их позициями
    matches = list(re.finditer(r"\[(\d{1,3})\]", text))
    if len(matches) < 2:
        return text

    # Группируем подряд идущие [N][M][K] — их не трогаем (уже corroborated)
    out = []
    last_end = 0
    i = 0
    while i < len(matches):
        m = matches[i]
        # Проверяем — есть ли следующий [N] в пределах 3 chars (значит уже подряд)
        is_chained = False
        if i+1 < len(matches) and matches[i+1].start() - m.end() <= 3:
            is_chained = True
        if is_chained:
            # пропускаем эту группу — уже corroborated
            j = i
            while j+1 < len(matches) and matches[j+1].start() - matches[j].end() <= 3:
                j += 1
            out.append(text[last_end:matches[j].end()])
            last_end = matches[j].end()
            i = j+1
            continue
        # Одиночная цитата → ищем corroboration
        n = int(m.group(1))
        if n not in src_by_n:
            i += 1; continue
        # Контекст вокруг утверждения (80 chars до)
        ctx_start = max(0, m.start()-120)
        ctx_end   = min(len(text), m.end()+30)
        claim_ctx = text[ctx_start:ctx_end]
        # Только содержательные claim'ы (с числом или ключевыми словами)
        if not re.search(r"\d|млрд|млн|%|выручк|прибыль|рост|доля", claim_ctx, re.IGNORECASE):
            i += 1; continue

        # Embedding-based search в БД
        try:
            qvec = embedder.embed_one(claim_ctx)
            with db.session() as s:
                rows = s.execute(_t("""
                    SELECT d.url
                      FROM document_chunk dc
                      JOIN document d USING(document_id)
                     WHERE d.is_sponsored = FALSE AND d.trust_score >= 0.55
                       AND d.url != :u
                     ORDER BY dc.embedding <=> CAST(:qv AS vector)
                     LIMIT 5
                """), {"u": src_by_n[n].get("url",""), "qv": str(qvec)}).all()
        except Exception:
            i += 1; continue

        # Находим в результатах те которые в наших sources
        corroborating = []
        seen_urls = {src_by_n[n].get("url")}
        for r in rows:
            url = r[0]
            if url in seen_urls:
                continue
            seen_urls.add(url)
            ref_n = src_by_url.get(url)
            if ref_n and ref_n != n and ref_n not in corroborating:
                corroborating.append(ref_n)
            if len(corroborating) >= 2:
                break

        # Вставляем результат
        out.append(text[last_end:m.end()])
        if corroborating:
            for c in corroborating:
                out.append(f"[{c}]")
        last_end = m.end()
        i += 1

    out.append(text[last_end:])
    return "".join(out)


def _filter_invalid_citations(text: str, valid_n: set[int]) -> str:
    """Удаляет из текста [N] которых нет в valid_n (выдуманные LLM).
    Поведение:
      • [3] валидно если 3 in valid_n → оставляем
      • [99] невалидно → заменяем на «(без источника)» при первом появлении в абзаце
      • Для уменьшения шума: повторные невалидные на той же строке просто удаляются
    """
    if not text:
        return text
    # Если sources вообще нет — все [N] невалидны
    def _replace(m):
        try:
            n = int(m.group(1))
            if n in valid_n:
                return m.group(0)
        except Exception:
            pass
        return ""
    return re.sub(r"\[(\d{1,3})\]", _replace, text)


def _adaptive_web_fallback(step: dict, max_fetch: int = 2,
                            topic: str | None = None) -> int:
    """Multi-angle web search для шага с пустым результатом.
    Стратегия:
      • 4-5 разных формулировок запроса (RU + EN, годы, terms)
      • Без site:filter в DDG (он часто всё режет), пост-фильтр whitelist
      • Параллельный ingest top результатов
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from ..rag.web_search import search as web_search
    from ..rag.indexer import ingest_document_from_url

    query = step.get("query") or step.get("title") or ""
    entity = step.get("entity") or ""
    title  = step.get("title") or ""
    if not query and not title:
        return 0

    # Извлекаем "термин" (financials/business-model/expenses) из шага
    term_map = [
        (("доход","выручк","revenue"),    ["выручка результаты МСФО","financial results revenue"]),
        (("расход","cost","expense"),     ["структура расходов OPEX","operating expenses"]),
        (("бизнес-модель","business"),    ["бизнес-модель монетизация","business model monetization"]),
        (("стратеги","strategy"),         ["стратегия развития","strategy roadmap"]),
        (("отзыв","review"),              ["отзывы клиентов",""]),
    ]
    angles_kw = []
    low = (query+" "+title).lower()
    for triggers, angles in term_map:
        if any(t in low for t in triggers):
            angles_kw = angles
            break
    if not angles_kw:
        angles_kw = ["обзор аналитика","results analysis"]

    name = entity or query.split()[0]
    # Topic-aware: если topic задан, формируем тематические запросы вместо
    # дефолтных «выручка/financials». Это ключевой фикс для продуктовых вопросов.
    if topic:
        queries = [
            f"site:bank.ru {topic} условия {name}",
            f"{name} {topic} условия тарифы",
            query,
        ]
    else:
        # 2 query вместо 5 — главный latency saving (каждая web_search 5-10s)
        queries = [
            f"{name} {angles_kw[0]} 2025",
            query,
        ]

    whitelist = _get_dynamic_whitelist() + [
        "domclick.ru","cian.ru","avito.ru","дом.рф","ir.ciangroup.ru",
    ]
    whitelist_set = set(whitelist)

    # Собираем кандидатов из всех queries (post-filter)
    candidates: list[dict] = []
    seen = set()
    for q in queries[:2]:
        try:
            res = web_search(q, max_results=8)
        except Exception:
            res = []
        for r in res or []:
            url = r.get("url"); domain = r.get("domain","")
            if not url or url in seen:
                continue
            ok = any(d==domain or domain.endswith("."+d) for d in whitelist_set)
            if ok:
                candidates.append(r); seen.add(url)
        if len(candidates) >= max_fetch:
            break

    if not candidates:
        return 0

    # Topic-aware filter URL'ов: если topic задан — оставляем только URL'ы
    # которые проходят `_matches_topic` (URL+title+snippet содержат topic-слово
    # и не содержат blacklist-маркеры). Без этого фильтра adaptive-fallback
    # ингестил «{name} financial results 2025» страницы для запроса про вклады.
    if topic:
        candidates = [
            r for r in candidates
            if _matches_topic(
                f"{r.get('url','')} {r.get('title','')} {r.get('snippet','')}",
                topic, url=r.get("url"),
            )
        ]

    n = 0
    def _do_ingest(url):
        try:
            ir = ingest_document_from_url(url, bank_slug_hint=entity)
            return 1 if ir.is_new else 0
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_do_ingest, c["url"]) for c in candidates[:max_fetch]]
        for f in as_completed(futures, timeout=45):
            try:
                n += f.result()
            except Exception:
                pass
    return n


def _try_pre_bootstrap_entity(entity: dict, web_search_fn, ingest_fn) -> int:
    """Pre-bootstrap для упомянутой в вопросе сущности (банк/сервис).
    Стратегия: 2 параллельных web_search ('бизнес-модель', 'выручка отчёт') →
    параллельный ingest top результатов с whitelist-доменов.
    Возвращает количество успешно проиндексированных документов."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    domain = entity.get("domain")
    name = entity.get("name") or entity.get("slug") or ""
    if not domain or not name:
        return 0

    # Проверяем кэш в БД
    try:
        with db.session() as s:
            from sqlalchemy import text as _t
            row = s.execute(_t("""
                SELECT count(*) FROM document_chunk dc
                  JOIN document d USING(document_id)
                 WHERE d.url ILIKE :d
            """), {"d": f"%{domain}%"}).first()
        if row and row[0] >= 5:                  # уже >5 chunks — бутстрап не нужен
            return 0
    except Exception:
        pass

    from ..rag.trust import KNOWN_BANK_DOMAINS
    whitelist = list(KNOWN_BANK_DOMAINS.keys()) + [
        "cbr.ru", "vedomosti.ru", "rbc.ru", "companies.rbc.ru",
        "interfax.ru", "kommersant.ru", "e-disclosure.ru",
        "frankrg.com", "alfacapital.ru", "tbank.ru", "fomag.ru",
        "ru.investing.com", "expert.ru", "realty.ria.ru",
    ]

    # 2 query — баланс latency vs coverage
    queries = [
        f"{name} выручка финансовые результаты 2025 МСФО",
        f"{name} бизнес-модель монетизация",
    ]
    urls_to_index: list[str] = []
    seen = set()
    for q in queries:
        try:
            results = web_search_fn(q, site_filter=whitelist, max_results=3)
        except Exception:
            results = []
        # post-filter: только domains из whitelist (DDG site:filter не всегда работает)
        for r in results:
            url = r.get("url")
            domain = r.get("domain", "")
            if not url or url in seen:
                continue
            if any(d == domain or domain.endswith("." + d) for d in whitelist):
                urls_to_index.append(url)
                seen.add(url)
        if len(urls_to_index) >= 6:                # достаточно 6 разных URLs
            break

    if not urls_to_index:
        return 0

    # Параллельный ingest
    n = 0
    def _do(url):
        try:
            ir = ingest_fn(url, bank_slug_hint=entity.get("slug"))
            return 1 if ir.is_new else 0
        except Exception:
            return 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_do, u) for u in urls_to_index[:6]]
        for f in as_completed(futures, timeout=90):
            try:
                n += f.result()
            except Exception:
                pass
    if n:
        log.info("pre-bootstrap %s: indexed %s/%s docs", domain, n, len(urls_to_index))
    return n


def _build_step_args(step: dict, *, question_bank_slugs: list[str] | None = None,
                      is_banking_question: bool = False) -> dict:
    """Конвертирует step в args для _run_tool."""
    tool = step["tool"]
    query = step["query"]
    entity = step.get("entity")
    if tool == "get_market_offers":
        # Auto-injected step передаёт category в _args
        return step.get("_args") or {"category": entity or "deposit"}
    if tool == "semantic_search":
        # Bank filter — мягкий: фильтруем ТОЛЬКО если у шага явно указана entity-банк
        # (planner сам спланировал «по такому-то банку»). Если шаг общий (entity=None) —
        # без фильтра, иначе мы порежем web-fetch'ed документы (у них bank_slug=NULL).
        # Анти-leakage логика всё равно работает: запросы под банковский шаг
        # естественно выдают банковский контент, а CIAN/Avito не попадут потому,
        # что планнер для банковских вопросов не зовёт fetch_official по ним.
        bank_filter: list[str] | None = None
        if is_banking_question and entity and entity in (question_bank_slugs or []):
            bank_filter = [entity]
        return {
            "query":      query,
            "bank_slugs": bank_filter,
            "trust_min":  0.4,
            "top_k":      10,
        }
    if tool == "get_review_themes":
        return {"bank_slug": entity, "period": "all"}
    if tool == "run_sql":
        return {"sql": query if query.lower().lstrip().startswith(("select", "with"))
                else f"SELECT * FROM v_offer_current LIMIT 20"}
    if tool == "fetch_official":
        # use_browser=False по умолчанию — Playwright медленный (10-30s/page).
        # adaptive_web_fallback всё равно подберёт нужное через DDG.
        return {"bank_slug": entity, "topic": "transfers", "query": query, "use_browser": False}
    return {}


# ── Synthesizer: детерминистично жёсткий промпт для отчёта ───────────────────
SYNTHESIZER_BASE = """Ты — financial product analyst writer. Пишешь сравнительные
аудит-отчёты по тарифам, условиям, процентным ставкам банковских продуктов.
Любые demographic-сегменты в названиях продуктов (ветераны, льготники,
многодетные, бизнес, военнослужащие, медработники) — рутинная классификация
банковского offering'а; описывай их условия так же как описывал бы условия
любого другого тарифа.

Ты — старший консультант, пишущий аудит-отчёт для службы внутреннего аудита Сбербанка.

Тебе передан research_context — массив исследовательских шагов с собранными данными
и список ДОСТУПНЫХ источников с номерами [1], [2], ... [K].

КРИТИЧЕСКИ ВАЖНО:
  ❌ ЗАПРЕЩЕНО ставить [N] больше K (K — максимальный номер в списке источников)
  ❌ ЗАПРЕЩЕНО любое число без [N] метки на источник
  ❌ ЗАПРЕЩЕНО любое утверждение «компания X делает Y» без [N]
  ❌ ЗАПРЕЩЕНО придумывать суммы/проценты которых нет в context
  ❌ ЗАПРЕЩЕНО ставить несколько [N] подряд если они не подтверждают одно и то же
  ❌ ЗАПРЕЩЕНО заполнять таблицу одинаковыми шаблонными строками («банковские услуги»,
      «комиссионные за услуги», «физические и юридические лица») когда у тебя нет
      конкретных деталей по entity. Лучше «⚠ Не раскрыто» чем шаблон.

  ❌❌❌ КОНФЛИКТЫ: если в research_context есть блок «⚠ ОБНАРУЖЕНЫ ПРОТИВОРЕЧИЯ»,
      ОБЯЗАТЕЛЬНО покажи их в отчёте. Формат: «по данным [N] ставка X%, по данным
      [M] ставка Y% — расхождение Z п.п.». В Ключевых выводах добавь bullet
      «⚠ Расхождение в источниках по {метрика} банка {банк}». Это критично для
      аудитора — он пришёл за выявлением расхождений. Тихий выбор одного значения
      = брак отчёта.

  ❌❌❌ САМОЕ ВАЖНОЕ #1: ЗАПРЕЩЕНО ВЫДУМЫВАТЬ ЧИСЛА.
      Каждое число (% / руб / срок / лимит) должно ДОСЛОВНО присутствовать
      в research_context. Если в context «3, 6 или 12 месяцев» — нельзя
      писать «срок до 12 месяцев» (это переформулировка). Можно: «срок 3, 6
      или 12 месяцев [N]».
      Если number_value в context = «13,14%», нельзя округлять до «13,5%».
      Если число НЕ найдено в context — пиши «⚠ ставка не указана» и не
      ставь [N]. Лучше пробел чем правдоподобное число.

  ❌❌❌ САМОЕ ВАЖНОЕ #2: ЗАПРЕЩЕНО ЦИТИРОВАТЬ OFF-TOPIC ИСТОЧНИКИ КАК ОТВЕТ НА ВОПРОС.
      В sources index каждый источник помечен ✅ RELEVANT или ⚠ OFF-TOPIC.
      OFF-TOPIC значит, что фрагменты источника НЕ упоминают тему вопроса.
      Например: вопрос «доверенности», источник = отзыв клиента про задержку
      перевода = OFF-TOPIC. Цитировать его как «факт по доверенностям» — ГРУБАЯ
      ОШИБКА, отчёт будет отбракован.
      Если по теме НЕТ relevant источников — пиши «⚠ Не раскрыто» в разделах
      про эту тему. НЕ ВЫДУМЫВАЙ связь между off-topic источником и темой.
      OFF-TOPIC источники можно упомянуть только в отдельной секции «другие
      наблюдения» с явной пометкой «(не по теме вопроса)».

Если список источников ПУСТ или нет данных — пиши БЕЗ [N], в формате:
  «⚠ Не раскрыто»  или  «💭 Логический вывод (без источника):»
НИКОГДА не выдумывай номера [1][2][3][4] чтобы заполнить таблицу.

  ✅ Если данных нет → "⚠ Не раскрыто" в ячейке таблицы
  ✅ Если выводишь умозаключение → начни с "💭 Логический вывод:" + [N] на основания
  ✅ Если факт подтверждается несколькими источниками — разделяй пробелами: "[1] [3] [5]"
     (НЕ слитно "[1][3][5]" — UI ломает рендеринг)

ПРИМЕР СТИЛЯ:
> «Сбербанк требует нотариальную доверенность с обязательным личным присутствием
>  доверителя в отделении [3]. ВТБ принимает доверенности, оформленные через
>  Госуслуги [7]. ⚠ Не раскрыто: тарифы Альфа-Банка на оформление.»

Конкретика → [N] с пробелами. Вывод → префикс 💭. Пробел → ⚠.

ОЦЕНКА КАЧЕСТВА: лучший отчёт — это отчёт, в котором аудитор может ПРОВЕРИТЬ
каждое число по ссылке источника. Если источников мало — лучше короткий
честный отчёт чем длинный с догадками.

СТРУКТУРА: ниже тебе передадут конкретный outline (раздел за разделом).
Пиши ТОЛЬКО эти разделы, в этом порядке. Ничего сверх. Если для секции данных нет —
напиши коротко «⚠ Данных по этому аспекту не получено» и переходи к следующей.

🎯 ОБЯЗАТЕЛЬНО ПО ОБЪЁМУ И ГЛУБИНЕ (отчёт пишется для аудитора, не reader-friendly):
  • В Ключевых выводах — МИНИМУМ 6 пунктов, по 2+ на каждый банк, каждый с [N].
  • В сравнительных таблицах — заполняй ВСЕ ячейки. Если число есть в context —
    пиши его, не пиши «не раскрыто». «Не раскрыто» только если данных РЕАЛЬНО нет.
  • Для каждого банка — детализация: базовая ставка + надбавки, мин-сумма + макс-сумма,
    сроки (диапазон), комиссии, документы, ограничения, целевая аудитория.
    НЕ ограничивайся одной headline-цифрой.
  • Если есть PRE-EXTRACTED FACTS блок — это твой главный источник. Он содержит
    уже извлечённые структурированные факты с [N]. Используй ВСЕ строки оттуда,
    не выкидывай.
  • Минимум 4 уникальных [N] на каждый банк в отчёте. Если в context source [N]=5
    про банк X — обязательно процитируй его хотя бы раз.
  • Объём отчёта: 4000-7000 chars для 2-3 банков. Не короче."""


# Adaptive outline planner — генерирует список секций под конкретный вопрос
OUTLINE_PLANNER_SYSTEM = """Ты — структурный планировщик аудит-отчётов.
Тебе передан вопрос аудитора + список собранных данных. Ты ВЫБИРАЕШЬ структуру
отчёта (4-7 секций) НА ОСНОВЕ типа вопроса, а не шаблона.

Твоя задача — НЕ ПИСАТЬ отчёт, а вернуть JSON с outline:
{"sections": [
  {"title": "...", "kind": "key_findings|comparison_table|per_entity|...",
   "instructions": "что именно должно быть в этой секции, в 1-2 предложения"},
  ...
]}

Допустимые `kind`:
  • key_findings        — нумерованный список 3-7 главных инсайтов с цитатами
  • per_entity          — подзаголовок на каждую entity (банк/компанию) с разбором
  • comparison_table    — markdown-таблица сравнения нескольких entities
  • reviews_summary     — топ жалоб/похвалы клиентов из get_review_themes
  • procedure_steps     — пошаговое описание процедуры (как оформить, что нужно)
  • tariffs_table       — таблица «Банк × Тариф/Комиссия/Условие»
  • pros_cons           — плюсы/минусы по entity
  • leaders_summary     — кто лидер по каким критериям (только если в данных есть числовые отличия)
  • risks_gaps          — что НЕ удалось узнать и что аудитор должен проверить вручную
  • free_text           — текстовый раздел без жёсткой структуры

ПРАВИЛА:
  1. Выбирай ТОЛЬКО те секции, которые отвечают на вопрос. Не плоди мусор.
  2. Если вопрос про продукт/услугу/процедуру (доверенности, переводы, ипотека,
     карты) — фокус на tariffs_table / procedure_steps / pros_cons / reviews_summary.
  3. Если вопрос про конкурентов/бизнес-модели/финансы — comparison_table /
     per_entity / leaders_summary.
  4. Если вопрос упоминает «плюсы/минусы/отзывы/жалобы» — обязательно reviews_summary и pros_cons.
  5. ВСЕГДА последняя секция = risks_gaps (честный список пробелов).
  6. ВСЕГДА первая секция = key_findings.
  7. Минимум 4, максимум 7 секций. Не больше.
  8. leaders_summary ТОЛЬКО если в данных есть РЕАЛЬНЫЕ числовые отличия —
     не суй её в продуктовые сравнения, где «лидер» бессмыслен.

Возвращай ТОЛЬКО JSON. Без preamble."""


async def _design_outline(client: AsyncOpenAI, question: str,
                           research_summary: str) -> list[dict]:
    """Зовёт outline-planner; возвращает list of {title, kind, instructions}."""
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": OUTLINE_PLANNER_SYSTEM},
                {"role": "user",
                 "content": f"# Вопрос\n{question}\n\n# Краткая сводка собранных данных\n{research_summary[:4000]}"},
            ],
            max_tokens=1500,    # reasoning + outline JSON
            temperature=0.1,
        )
        text = resp.choices[0].message.content or ""
        m = re.search(r'\{[\s\S]*\}', text)
        if not m:
            return []
        data = _loose_json_loads(m.group(0))
        sections = data.get("sections") or []
        VALID_KINDS = {"key_findings","per_entity","comparison_table",
                       "reviews_summary","procedure_steps","tariffs_table",
                       "pros_cons","leaders_summary","risks_gaps","free_text"}
        clean = []
        for s in sections:
            if not isinstance(s, dict): continue
            kind = s.get("kind", "free_text")
            if kind not in VALID_KINDS: kind = "free_text"
            clean.append({
                "title": str(s.get("title") or kind)[:120],
                "kind":  kind,
                "instructions": str(s.get("instructions") or "")[:400],
            })
        # Гарантируем key_findings первым и risks_gaps последним
        kinds = [c["kind"] for c in clean]
        if "key_findings" not in kinds:
            clean.insert(0, {"title": "Ключевые выводы", "kind": "key_findings",
                             "instructions": "3-7 главных инсайтов с цитатами"})
        if "risks_gaps" not in kinds:
            clean.append({"title": "Пробелы данных и риски", "kind": "risks_gaps",
                          "instructions": "Что не удалось получить, что проверить вручную"})
        return clean[:7]
    except Exception as e:
        log.info("outline planner failed: %s — falling back to default", e)
        return []


def _default_outline_for_question(question: str, n_entities: int) -> list[dict]:
    """Fallback outline когда LLM-планировщик упал.
    Эвристически выбирает структуру по ключевым словам вопроса."""
    q = (question or "").lower()
    is_product = any(w in q for w in (
        "тариф","комисси","процедур","оформ","условия","документ",
        "доверенност","перевод","ипотек","карт","вклад","депозит","кредит"))
    has_reviews = any(w in q for w in (
        "плюс","минус","отзыв","жалоб","нрави","неудобн","претензи","проблем"))
    is_compare = n_entities >= 2 or "сравн" in q or "vs" in q

    sections = [{"title": "Ключевые выводы", "kind": "key_findings",
                 "instructions": "3-7 главных инсайтов по вопросу с цитатами"}]
    if is_product:
        sections.append({"title": "Тарифы и условия",
                         "kind": "tariffs_table",
                         "instructions": "Таблица: банк × условия/комиссии/документы. ⚠ если данных нет."})
        sections.append({"title": "Процедура и нюансы",
                         "kind": "procedure_steps",
                         "instructions": "По банкам — что нужно сделать клиенту, какие шаги, ограничения."})
    elif is_compare:
        sections.append({"title": "Сравнение по ключевым критериям",
                         "kind": "comparison_table",
                         "instructions": "Markdown-таблица entity × критерий с числами и [N]."})
        sections.append({"title": "Разбор по компаниям",
                         "kind": "per_entity",
                         "instructions": "Подзаголовок на каждую entity, конкретные факты."})
    else:
        sections.append({"title": "Детальный разбор",
                         "kind": "free_text",
                         "instructions": "Текстовый разбор по теме вопроса с цитатами."})
    if has_reviews:
        sections.append({"title": "Плюсы и минусы по отзывам",
                         "kind": "pros_cons",
                         "instructions": "Из get_review_themes — топ-3 жалобы + топ-3 похвалы по каждому банку."})
    sections.append({"title": "Пробелы данных и риски",
                     "kind": "risks_gaps",
                     "instructions": "Что НЕ удалось получить, что аудитор должен проверить."})
    return sections[:7]


def _outline_to_synth_prompt(outline: list[dict]) -> str:
    """Превращает outline в инструкцию для synthesizer."""
    lines = ["", "# OUTLINE — пиши РОВНО эти разделы, в этом порядке:"]
    for i, s in enumerate(outline, 1):
        lines.append(f"\n## {i}. {s['title']}")
        lines.append(f"_kind: {s['kind']}_")
        if s.get("instructions"):
            lines.append(f"_что должно быть: {s['instructions']}_")
        # Подсказки для распространённых kind'ов
        if s["kind"] == "key_findings":
            lines.append("Нумерованный список 3-7 фактов. Каждый = «X сделал Y [N]» или «⚠ Не раскрыто».")
        elif s["kind"] == "comparison_table":
            lines.append("Markdown-таблица. Колонки выбирай под вопрос. [N] в ячейках.")
        elif s["kind"] == "tariffs_table":
            lines.append("Таблица: | Банк | Условие/Тариф | Комиссия | Документы | Особенности |. ⚠ где нет.")
        elif s["kind"] == "procedure_steps":
            lines.append("По каждому банку — нумерованный список шагов / требований.")
        elif s["kind"] == "per_entity":
            lines.append("Подзаголовок `### Название` на каждую entity, абзац с фактами и [N].")
        elif s["kind"] == "reviews_summary":
            lines.append("ТОЛЬКО отзывы СОДЕРЖАЩИЕ тему вопроса. Если ни один отзыв "
                         "не упоминает тему — пиши «⚠ Тематических отзывов не найдено» "
                         "и НЕ суй сюда отзывы про что-то другое.")
        elif s["kind"] == "pros_cons":
            lines.append(
                "По КАЖДОМУ банку: блок «✅ Плюсы» (3-5 пунктов из тарифов/условий) "
                "и «⚠ Минусы» (3-5 пунктов). КРИТИЧНО: для Минусов ИСПОЛЬЗУЙ "
                "negative_reviews из шагов TR-{slug} (Тематические отзывы) — там реальные "
                "жалобы клиентов с rating=1-2. Каждая жалоба = одна строка в Минусах "
                "с цитатой [N] на источник банки.ру. НЕ пиши «По теме не раскрыто» "
                "если в research_context есть TR-step с found > 0.")
        elif s["kind"] == "leaders_summary":
            lines.append("Таблица | Критерий | Лидер | Почему [N] |. ТОЛЬКО если есть реальные числовые отличия.")
        elif s["kind"] == "risks_gaps":
            lines.append("Конкретный bullet-список: что не удалось получить, что проверить вручную.")
    return "\n".join(lines)


# Backward compat: старый SYNTHESIZER_SYSTEM = base + базовый шаблон.
# Используется только в edge-кейсах (fallback). Новый pipeline собирает
# полный prompt = SYNTHESIZER_BASE + outline.
SYNTHESIZER_SYSTEM = SYNTHESIZER_BASE


def _is_high_priority_doc(url: str, doc_type: str | None) -> bool:
    """Финансовые/IR/регуляторные документы — топ-приоритет в context."""
    if not url:
        return False
    u = url.lower()
    HIGH_PRIORITY_PATTERNS = (
        "e-disclosure.ru", "ir.", "/investor", "investor-relations",
        "ifrs", "msfo", "/reports", "/financials", "cbr.ru/finorg",
        "domrfbank.ru/press", ".pdf",
    )
    if any(p in u for p in HIGH_PRIORITY_PATTERNS):
        return True
    if doc_type in ("pdf", "xlsx"):
        return True
    return False


def _comprehensive_chunks_for_entities(entities: list[dict], sources: list[dict],
                                         max_per_entity: int = 25,
                                         max_total: int = 100,
                                         topic: str | None = None) -> str:
    """Финальный sweep по БД: для каждой упомянутой entity достаём
    топ-N релевантных chunks из ВСЕХ проиндексированных документов.
    Это прокидывает в synthesizer данные которые planner-шаги могли пропустить.
    """
    if not entities:
        return ""
    from sqlalchemy import text as _t
    parts = ["\n\n# COMPREHENSIVE CONTEXT (все релевантные документы из БД)"]
    parts.append(f"# Дополнительные данные сверх 12 шагов плана. Используй активно.\n")

    # Карта source_id → [N] для возможности ссылаться
    src_by_url = {s.get("url"): s for s in sources if s.get("url")}
    used_total = 0

    for ent in entities:
        slug = ent.get("slug","")
        domain = ent.get("domain","")
        d_short = domain.split(".")[0] if domain else slug
        name = ent.get("name") or slug
        if used_total >= max_total:
            break

        try:
            with db.session() as s:
                # Найдём документы где URL содержит entity domain ИЛИ
                # bank_id матчится со slug ИЛИ content содержит name
                rows = s.execute(_t("""
                    SELECT d.document_id, d.url, d.title, d.doc_type::text AS doc_type,
                           d.trust_score, d.fetched_at, d.last_modified,
                           d.is_sponsored, st.kind AS source_kind,
                           b.name AS bank_name,
                           -- priority boost для IR/PDF/регулятора
                           CASE WHEN d.url ILIKE '%e-disclosure%' THEN 100
                                WHEN d.url ILIKE '%ir.%' THEN 80
                                WHEN d.url ILIKE '%investor%' THEN 80
                                WHEN d.url ILIKE '%cbr.ru/finorg%' THEN 90
                                WHEN d.doc_type::text = 'pdf' THEN 60
                                WHEN d.url ILIKE '%/reports%' THEN 50
                                WHEN st.kind = 'regulator' THEN 70
                                WHEN st.kind = 'bank_official' THEN 50
                                WHEN st.kind = 'press' THEN 30
                                ELSE 0 END
                           +
                           -- recency boost: чем свежее документ, тем выше приоритет
                           CASE WHEN d.last_modified >= now() - interval '90 days' THEN 30
                                WHEN d.last_modified >= now() - interval '180 days' THEN 20
                                WHEN d.last_modified >= now() - interval '365 days' THEN 10
                                WHEN d.fetched_at >= now() - interval '7 days' THEN 5
                                ELSE 0 END
                           +
                           -- penalty за упоминание старого года в URL/title
                           CASE WHEN d.url ~* '(2018|2019|2020|2021|2022)'
                                  OR d.title ~* '(2018|2019|2020|2021|2022)'
                                THEN -15 ELSE 0 END
                           AS priority_boost
                      FROM document d
                      LEFT JOIN source_trust st ON st.source_id = d.source_id
                      LEFT JOIN bank b ON b.bank_id = d.bank_id
                     WHERE d.is_sponsored = FALSE
                       AND d.trust_score >= 0.4
                       AND (
                           d.url ILIKE :d
                           OR d.url ILIKE :s
                           OR d.content_text ILIKE :n
                           OR (b.slug = :sl)
                       )
                       /* topic-фильтр: оставляем только doc'и где тема в URL/title.
                          content_text-match УБРАН: для тем-шумных слов вроде
                          «приложение» он притягивал любую бизнес-страницу
                          (там в footer/reviews встречается слово). URL+title
                          гораздо надёжнее: банки в URL пишут продукт явно. */
                       AND (:topic_kw IS NULL
                            OR d.url ILIKE :topic_kw OR d.url ILIKE :topic_kw_lat
                            OR d.title ILIKE :topic_kw
                            OR d.title ILIKE :topic_kw_lat)
                     ORDER BY priority_boost DESC, d.trust_score DESC, d.fetched_at DESC
                     LIMIT 25
                """), {
                    "d": f"%{domain}%" if domain else f"%xxx_no_match%",
                    "s": f"%{d_short}%",
                    "n": f"%{name}%",
                    "sl": slug,
                    # topic_kw: ru-keyword для content_text/title; topic_kw_lat:
                    # latin для URL'ов (sberbank.ru/.../vklad)
                    "topic_kw":      f"%{topic}%" if topic else None,
                    "topic_kw_lat":  (f"%{(_TOPIC_SYNONYMS.get(topic, [topic])[-1])}%"
                                       if topic else None),
                }).mappings().all()

            ent_chunks_added = 0
            ent_parts: list[str] = []
            for r in rows:
                if ent_chunks_added >= max_per_entity or used_total >= max_total:
                    break
                # Возьмём 1-3 chunks из этого документа
                with db.session() as s:
                    chunks = s.execute(_t("""
                        SELECT idx, text, headings_path
                          FROM document_chunk
                         WHERE document_id = :d
                         ORDER BY idx
                         LIMIT 3
                    """), {"d": r["document_id"]}).mappings().all()
                if not chunks:
                    continue
                # Citation reference [N] если url в sources
                src = src_by_url.get(r["url"])
                cite = f"[{src['n']}]" if src else f"(non-cited:{r['url'][:60]})"
                ent_parts.append(f"\n— {r.get('bank_name') or r['title'][:60]} · "
                                  f"{r.get('source_kind') or '?'} · trust={r['trust_score']:.2f} · {cite}")
                if r.get("title"):
                    ent_parts.append(f"  Title: {r['title'][:140]}")
                for c in chunks:
                    if c.get("headings_path"):
                        ent_parts.append(f"  > {c['headings_path']}")
                    txt = (c["text"] or "").strip().replace("\n", " ")
                    ent_parts.append(f"  «{txt[:600]}»")
                    ent_chunks_added += 1
                    used_total += 1
                    if ent_chunks_added >= max_per_entity:
                        break

            if ent_parts:
                parts.append(f"\n## ▸ {name} ({slug}) — {ent_chunks_added} фрагментов из БД")
                parts.extend(ent_parts)
        except Exception as e:
            log.info("comprehensive context for %s failed: %s", slug, e)
            continue

    if used_total == 0:
        return ""
    parts.insert(2, f"# Total: {used_total} chunks из всех документов БД\n")
    return "\n".join(parts)


# Расширение тематических ключевых слов: для каждого «topic» — синонимы,
# которые должны учитываться при определении релевантности фрагмента.
# ВАЖНО: включаем и латинские транслитерации, потому что URL'ы банков
# часто пишут темы латиницей (sberbank.ru/.../vklad, vtb.ru/personal/vklady-i-scheta).
# Без латиницы topical-filter режет правильные URL.
_TOPIC_SYNONYMS = {
    "доверенность": ["доверенност", "довер.", "поверенн", "уполномоч",
                     "doverennost", "doverenost"],
    "тариф":        ["тариф", "комисси", "стоимост", "плата за",
                     "tariff", "tarif"],
    "перевод":      ["перевод", "transfer", "сбп", "p2p", "perevod"],
    "ипотека":      ["ипотек", "жилищн", "mortgage", "ipoteka", "ipotek"],
    "кредит":       ["кредит наличн", "потреб.кредит", "потребкредит",
                     "credit", "kredit"],
    "вклад":        ["вклад", "депозит", "накопит. счёт", "процент по вкладу",
                     "vklad", "deposit", "depozit"],
    "карта":        ["дебетов карт", "кредитн карт", "пластиков карт",
                     "выпуск карт", "карта", "kart", "card", "карты"],
    "счёт":         ["расчётн счёт", "р/с", "р\\\\с", "открытие счёта",
                     "schet", "account", "/account"],
    "сбп":          ["сбп", "система быстрых платежей", "sbp", "fps"],
    "эквайринг":    ["эквайринг", "терминал", "acquiring", "ekvayring"],
    "автокредит":   ["автокредит", "кредит на авто", "auto-loan", "avtokredit"],
    "рефинансирование":["рефинансир", "перекредит", "refinans"],
    "счёт эскроу":  ["эскроу", "escrow", "eskrou"],
    "брокер":       ["брокер", "иис", "инвестсчёт", "broker", "iis"],
}

# Negative URL patterns — для каждой темы URL'ы с этими подстроками = НЕ про тему.
# Защита от случая «страница про автокредит содержит слово 'вклад' в footer»:
# даже если excerpt матчит — source URL должен сначала не содержать negative-маркер.
_TOPIC_URL_BLACKLIST = {
    "вклад":     ["/loan", "/credit", "/cash-", "/mortgage", "/ipoteka", "/auto",
                   "/business", "/insurance", "/strakh", "/kart", "/spasibo",
                   "/loyalty", "credit-history"],
    "доверенность": ["/credit", "/loan", "/cash-", "/mortgage", "/ipoteka", "/auto",
                     "/insurance", "/loyalty"],
    "перевод":   ["/loan", "/credit", "/mortgage", "/ipoteka", "/auto", "/insurance"],
    "ипотека":   ["/cash-", "/auto", "/credit-card", "/insurance"],
    "автокредит":["/cash-", "/mortgage", "/ipoteka", "/credit-card"],
    "тариф":     [],
    "карта":     ["/loan", "/cash-loan", "/mortgage", "/ipoteka"],
    "счёт":      ["/loan", "/credit", "/mortgage", "/ipoteka", "/auto"],
    "сбп":       ["/loan", "/credit", "/mortgage", "/insurance"],
    "брокер":    ["/loan", "/cash-", "/mortgage", "/insurance"],
}


def _is_topical_url(url: str, topic: str | None) -> bool:
    """True если URL не противоречит теме (negative-pattern check).
    Используется как DOPOLNITELЬNAYA проверка поверх excerpt-match."""
    if not url or not topic:
        return True
    low = url.lower()
    blacklist = _TOPIC_URL_BLACKLIST.get(topic, [])
    return not any(p in low for p in blacklist)


def _matches_topic(text: str, topic: str | None,
                    url: str | None = None) -> bool:
    """True если text содержит ключевое слово topic'а ИЛИ синонимы.
    + проверка URL на negative-pattern (если URL передан)."""
    if not topic:
        return True   # без topic — релевантно всё
    # URL negative-check
    if url and not _is_topical_url(url, topic):
        return False
    if not text:
        return False
    low = text.lower()
    keywords = _TOPIC_SYNONYMS.get(topic, [topic.lower()])
    # Считаем количество вхождений: одного упоминания мало (может быть в footer/menu).
    # Минимум 2 хита ИЛИ topic-слово в первой трети текста (где обычно
    # суть страницы/документа).
    hit_count = sum(low.count(kw) for kw in keywords)
    if hit_count >= 2:
        return True
    head = low[:max(800, len(low) // 3)]
    return any(kw in head for kw in keywords)


def _format_research_for_synthesis(steps_results: list[dict],
                                    sources: list[dict],
                                    entities: list[dict] | None = None,
                                    topic: str | None = None) -> str:
    """Формирует context для synthesizer'а:
      1. Результаты 12+ шагов плана
      2. ▸ COMPREHENSIVE CONTEXT — top chunks из БД
      3. Sources index с топик-аннотацией (relevant / off-topic)
    """
    parts = ["# Research context\n"]
    for sr in steps_results:
        parts.append(f"\n## Step {sr['n']}: {sr['title']}")
        parts.append(f"Tool: {sr['tool']} | Query: {sr['query']}")
        if sr.get("entity"):
            parts.append(f"Entity: {sr['entity']}")
        result = sr.get("result_summary") or "(no data)"
        parts.append(result)

    # Comprehensive sweep по БД
    if entities:
        comp = _comprehensive_chunks_for_entities(entities, sources, topic=topic)
        if comp:
            parts.append(comp)

    # Sources index — LLM ссылается на [N] ТОЛЬКО из этого диапазона.
    # КРИТИЧНО: для каждого источника помечаем — релевантен ли он топику вопроса.
    # Если фрагменты источника не упоминают тему → off-topic, использовать
    # ТОЛЬКО для общего бэкграунда, НЕЛЬЗЯ цитировать как ответ на вопрос.
    if sources:
        valid_ns = sorted(s["n"] for s in sources)
        parts.append(f"\n\n# ДОСТУПНЫЕ ИСТОЧНИКИ — можешь ссылаться ТОЛЬКО на эти {len(sources)} [N]")
        parts.append(f"# Допустимые номера: {valid_ns}")
        parts.append(f"# Любой [N] вне этого списка БУДЕТ УДАЛЁН post-фильтром.\n")
        if topic:
            parts.append(f"# ТЕМА ВОПРОСА: «{topic}». Источники помечены RELEVANT/OFF-TOPIC.")
            parts.append(f"# OFF-TOPIC источники НЕЛЬЗЯ цитировать как ответ на вопрос —")
            parts.append(f"# они не содержат информации по теме «{topic}».\n")
        relevant_ns: list[int] = []
        for s in sources:
            line = f"[{s['n']}] "
            if s.get("bank_name"):
                line += f"{s['bank_name']} · "
            if s.get("source_kind"):
                line += f"{s['source_kind']} · "
            line += f"trust={s.get('trust_score') or 0:.2f}"
            # Топик-релевантность по excerpts + URL negative-check
            if topic:
                excerpts = s.get("excerpts") or []
                joined = " ".join(excerpts)
                url = s.get("url")
                is_relevant = _matches_topic(joined, topic, url=url)
                if is_relevant:
                    relevant_ns.append(s["n"])
                else:
                    # Понятная причина — для дебага и для prompt'а LLM
                    if url and not _is_topical_url(url, topic):
                        reason = f"URL содержит off-topic-маркер ({topic})"
                    else:
                        reason = f"не упоминает «{topic}»"
                    line += "\n    ⚠ OFF-TOPIC: " + reason
                if is_relevant:
                    line += "\n    ✅ RELEVANT"
            line += f"\n    URL: {s.get('url')}"
            if s.get("headings_path"):
                line += f"\n    Section: {s['headings_path']}"
            parts.append(line)
        if topic:
            if relevant_ns:
                parts.append(f"\n# Источники РЕЛЕВАНТНЫЕ теме: {relevant_ns}")
                parts.append(f"# Цитируй только из них для ответов по теме.")
            else:
                parts.append(f"\n# ⚠ НЕТ источников релевантных теме «{topic}».")
                parts.append(f"# В разделах по теме пиши «⚠ Не раскрыто», НЕ цитируй off-topic.")
                parts.append(f"# Off-topic источники можно использовать только для")
                parts.append(f"# общей секции «другие наблюдения по банку», явно отметив.")
    else:
        parts.append("\n\n# ВНИМАНИЕ: ИСТОЧНИКОВ НЕТ — НЕ СТАВЬ [N] ВООБЩЕ")
        parts.append("Пиши «⚠ Не раскрыто» или «💭 Логический вывод (без источника)».")
    return "\n".join(parts)


# ── Verifier: пост-чек числовых утверждений ─────────────────────────────────
VERIFIER_SYSTEM = """Ты проверяешь аудит-отчёт на наличие галлюцинаций.

Получаешь:
  1. Готовый отчёт с цитатами [N]
  2. Реальные текстовые фрагменты из источников (excerpts)

Задача:
  • Проверяй ТОЛЬКО утверждения, содержащие конкретное число (%, рубли, штуки, годы).
  • Качественные утверждения («предсказуемая цена», «активно развивается», «рассчитывает компенсировать»)
    НЕ являются числовыми — НЕ включай их в массив, даже если их нет в excerpts.
  • Для каждого числового утверждения ищи подтверждение в excerpts. Учти:
    - русская десятичная запятая равна точке: «15,2» = «15.2»
    - «млрд» / «миллиардов» / «mlrd» — синонимы; «млн» / «миллионов» — синонимы
    - округления допустимы: «50,6» подтверждает «50,6 млрд» и «около 51 млрд»
    - год может быть в смежной фразе: «9 месяцев 2025» подтверждает «2025»
    - формулировка может отличаться: «выросла до 57%» подтверждает «занимает 57%»
  • Верни ТОЛЬКО JSON массив утверждений, которые ТОЧНО НЕ найдены в excerpts:
    [{"claim": "...", "issue": "число X не найдено в excerpts"}]
  • Если число найдено хотя бы в одном excerpt — НЕ добавляй его в массив.
  • Если excerpts пусты или их < 3 — верни [] (нет данных для проверки).

Без объяснений. Только JSON массив. Лучше пропустить сомнительный случай, чем дать false-positive."""


async def _verify_claims(client: AsyncOpenAI, report: str,
                          sources_dump: str) -> list[dict]:
    if not report or len(report) < 200:
        return []
    # Если excerpts'ов пшик — даже не зовём LLM, иначе он галлюционирует false-positives
    if not sources_dump or len(sources_dump) < 400:
        return []
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": VERIFIER_SYSTEM},
                {"role": "user",
                 "content": f"# Отчёт\n{report[:10000]}\n\n# Источники + research context\n{sources_dump[:35000]}"},
            ],
            max_tokens=1500,    # reasoning-buffer
            temperature=0.0,
        )
        text = resp.choices[0].message.content or "[]"
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            return []
        items = _loose_json_loads(m.group(0))
        # Post-filter: оставляем только claim'ы с конкретным числом.
        # Защита от LLM, который игнорирует инструкцию «только числовые».
        _NUMBER_RE = re.compile(r"\d[\d ]*[.,]?\d*\s*(%|млрд|млн|тыс|руб|год)|\b\d{4}\b")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            claim = str(it.get("claim", ""))[:300]
            if not _NUMBER_RE.search(claim):
                continue
            out.append({"claim": claim,
                        "issue": str(it.get("issue", ""))[:200]})
        return out[:10]
    except Exception as e:
        log.info("verifier failed: %s", e)
        return []


# ── Chart generator: извлекает информативные chart specs из отчёта ──────────
CHARTS_SYSTEM = """Ты — data-визуализатор аудит-отчётов. Извлекаешь из отчёта
числовые ряды и собираешь Chart.js specs. ЦЕЛЬ: дать аудитору 1-3 наглядные
картинки с конкретными цифрами.

ПОДХОД: ищи в отчёте ЛЮБЫЕ числовые сравнения:
  • таблицы со ставками/комиссиями/долями/выручкой по банкам
  • перечисления с числами в тексте («Сбер 6-22%, ВТБ 19,9%, Тинькофф 16,9%»)
  • таблицы «Лидер по категории» — превращай в bar где labels=критерии,
    data=абсолютные значения у соответствующего лидера (если все в одной
    единице) ИЛИ доли «N лидерств у каждого банка» если разнородные
  • диапазоны «6-22% годовых» — бери СРЕДНЕЕ или верхнюю границу

ВЫБОР ТИПА:
  • bar / horizontalBar — стандарт для сравнения метрики между 3-7 entity
  • doughnut             — для РАСПРЕДЕЛЕНИЯ ДОЛЕЙ (сумма ~100% или
                          логически замкнутое целое — например, market share)
  • line                 — динамика во времени (нужны временные метки)

ПРАВИЛА КАЧЕСТВА (мягкие):
  • Минимум 3 значения в одном dataset (хотя бы 3 банка/категории)
  • Если одно значение «не раскрыто» — поставь null, остальные оставь
  • dataset.label с единицей измерения: «Ставка, %», «Выручка, млрд руб.»,
    «Кол-во сделок, тыс.», «Активные пользователи, млн»
  • labels — конкретные имена банков/продуктов
  • data — только числа или null

НЕ ОТКАЗЫВАЙСЯ если в отчёте есть хотя бы 3-4 числа сравнимой природы.
Лучше СРЕДНИЙ график чем ноль графиков. Возвращай [] ТОЛЬКО если в отчёте
вообще нет числовых сравнений (только qualitative описание).

ПРИМЕРЫ ИЗВЛЕЧЕНИЯ:

Отчёт говорит «Сбер 6-22%, ВТБ 19,9%, Тинькофф 16,9%» → собирай:
{
  "title": "Ставки по ипотеке (верхняя граница)",
  "chartType": "bar",
  "labels": ["Сбер", "ВТБ", "Тинькофф"],
  "datasets": [{"label": "Ставка, % годовых", "data": [22, 19.9, 16.9]}],
  "sourceCitations": [1, 2, 3]
}

Дебетовые/специальные карты (кешбэк, лимиты, годовые комиссии):
Отчёт говорит «Сбер кешбэк до 5%, ВТБ до 7%, ПСБ 3%, ГПБ 4%» → собирай:
{
  "title": "Максимальный кешбэк по картам",
  "chartType": "bar",
  "labels": ["Сбер", "ВТБ", "ПСБ", "ГПБ"],
  "datasets": [{"label": "Кешбэк, %", "data": [5, 7, 3, 4]}],
  "sourceCitations": [1, 2, 3, 4]
}

Лимит снятия наличных: «Сбер 500к, ВТБ 600к, ПСБ 300к» → bar по «Лимит, тыс. ₽».
Сравнение комиссий за обслуживание: «Сбер 0₽, ВТБ 0₽, ПСБ 99₽» → bar по «Комиссия, ₽».

Отчёт говорит «50%, 96.5%, 134.6 тыс. сделок, 34.3 млн, 68.5 тыс.» по разным
банкам/метрикам → НЕ собирай в один график (несравнимо). Лучше выбери ОДНУ
метрику где есть >=3 банка с сопоставимыми числами.

⚠ ИСТОЧНИК ЧИСЕЛ: тебе передаётся отчёт + опционально BANK_FACTS (сырая
фактура из источников ДО синтеза). Числа бери в первую очередь из BANK_FACTS —
там фактура максимально сохранена. Отчёт после synthesizer'а может быть
загущен прозой и потерять часть конкретики.

ФОРМАТ ОТВЕТА — массив 1-3 spec'ов в JSON:
[{"title":"...","chartType":"bar","labels":[...],"datasets":[{"label":"...","data":[...]}],"sourceCitations":[...]}]

ВЕРНИ ТОЛЬКО МАССИВ. БЕЗ преамбулы, БЕЗ markdown-fences."""


async def _generate_charts(client: AsyncOpenAI, report: str,
                            bank_facts: dict[str, str] | None = None) -> list[dict]:
    """Генерирует chart-specs.

    bank_facts (опциональный) — словарь {slug: extracted_facts_md} из
    fact-extract pipeline'а. Передаётся как BANK_FACTS секция — даёт chart-LLM
    сырую фактуру с числами ДО synthesizer'а (где она часто разбавляется прозой).
    """
    if not report or len(report) < 200:   # 400 → 200: короткие отчёты тоже могут содержать таблицу
        log.warning("[charts] report too short (%s chars), skipping", len(report))
        return []
    # Собираем bank_facts блок если есть
    facts_block = ""
    if bank_facts:
        facts_lines = []
        for slug, facts in bank_facts.items():
            if facts and len(facts) > 30:
                facts_lines.append(f"## {slug.upper()}\n{facts[:2500]}")
        if facts_lines:
            facts_block = "\n\n# BANK_FACTS (сырая фактура из источников)\n\n" + "\n\n".join(facts_lines)
    user_content = f"# REPORT\n{report[:14000]}{facts_block[:14000]}"
    try:
        resp = await client.chat.completions.create(
            model=LLM_MODEL_NAME,
            messages=[
                {"role": "system", "content": CHARTS_SYSTEM},
                {"role": "user",   "content": user_content},
            ],
            max_tokens=3500,    # 1200→2000→3500: reasoning-модели + 3 chart-spec'а
            temperature=0.2,
        )
        text = resp.choices[0].message.content or "[]"
        m = re.search(r"\[[\s\S]*\]", text)
        if not m:
            log.warning("[charts] LLM no JSON array; raw first 200: %r", text[:200])
            return []
        try:
            items = _loose_json_loads(m.group(0))
        except Exception as e:
            log.warning("[charts] JSON parse failed: %s; raw first 200: %r",
                         e, m.group(0)[:200])
            return []
        out = []
        rejected: list[str] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            labels = it.get("labels") or []
            datasets = it.get("datasets") or []
            title = str(it.get("title", ""))[:80]
            if len(labels) < 2:
                rejected.append(f"<2 labels '{title}'"); continue
            if not datasets:
                rejected.append(f"no datasets '{title}'"); continue
            # Считаем валидные числа (преобразуем строки '13.5' → 13.5)
            valid_datasets = []
            total_numbers = 0
            for d in datasets:
                if not isinstance(d, dict): continue
                data = d.get("data") or []
                # Convert: «13.5», 13.5, "+15", «20%» → числа; '⚠'/'не раскрыто' → null
                cleaned = []
                for x in data:
                    if isinstance(x, (int, float)):
                        cleaned.append(float(x)); continue
                    if isinstance(x, str):
                        s = re.sub(r"[+%\s ]", "", x)
                        s = s.replace(",", ".")
                        try:
                            cleaned.append(float(s)); continue
                        except Exception: pass
                    cleaned.append(None)
                d2 = {**d, "data": cleaned}
                nn = sum(1 for v in cleaned if v is not None)
                # Смягчено: минимум 1 число в ряду (даже 1 точка лучше отказа)
                if nn >= 1:
                    valid_datasets.append(d2)
                    total_numbers += nn
            # Глобальный минимум: 2 числа всего (для bar — 2 entity = базовое сравнение)
            if not valid_datasets or total_numbers < 2:
                rejected.append(f"only {total_numbers} numbers '{title}'"); continue
            ctype = it.get("chartType") or "bar"
            if ctype not in ("bar","horizontalBar","doughnut","line"):
                ctype = "bar"
            # Авто-переключение на horizontalBar при длинных labels
            if ctype == "bar":
                avg_len = sum(len(str(l)) for l in labels) / max(len(labels),1)
                if avg_len > 22:
                    ctype = "horizontalBar"
            out.append({
                "title":            str(it.get("title", ""))[:200],
                "chartType":        ctype,
                "labels":           [str(l)[:60] for l in labels],
                "datasets":         valid_datasets,
                "sourceCitations":  it.get("sourceCitations") or [],
            })
        if out:
            log.warning("[charts] generated %s spec(s): %s",
                         len(out), [c["title"][:50] for c in out[:3]])
        elif rejected:
            log.warning("[charts] LLM returned %s items, ALL rejected: %s",
                         len(items), rejected[:5])
        else:
            log.warning("[charts] LLM returned 0 items — нет числовых сравнений в отчёте")
        return out[:3]
    except Exception as e:
        log.info("chart-gen failed: %s", e)
        return []


# ── Главный async-генератор: stream_deep_analysis ───────────────────────────
async def stream_deep_analysis(question: str,
                                history: list[dict]) -> AsyncIterator[str]:
    """Многошаговый research+synthesis.
    Стримит SSE-события (см. модульный docstring)."""
    # max_retries=4 — Fireworks бывает шлёт 5xx или ConnectionTimeout,
    # SDK сам делает exp-backoff и повторяет. Без этого первая транзиентная
    # ошибка ломает весь deep-research (≥9 LLM-вызовов в цепочке).
    client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY,
                          max_retries=4, timeout=180.0)

    # 1. mode переключение
    yield json.dumps({"type": "mode", "value": "deep"})

    # 1.5 Universal Query Resolver — ОДИН LLM-вызов даёт structured-понимание
    # вопроса (тема, синонимы, банки, нужны ли market_offers/отзывы) для
    # ЛЮБОГО банковского продукта/услуги без хардкода. Заменяет PRODUCT_TOPIC_TRIGGERS,
    # _TOPIC_SYNONYMS, BANK_SLUG_TRIGGERS, TOPIC_TO_CATEGORY и т.д.
    from .query_resolver import resolve_question, matches_topic_generic
    try:
        resolved = await asyncio.wait_for(
            resolve_question(client, question), timeout=12)
    except Exception as e:
        log.info("query_resolver failed (%s) — fallback to hardcoded triggers", e)
        resolved = {"topic": None, "topic_synonyms": [], "url_keywords": [],
                    "banks": [], "category_hint": None,
                    "wants_reviews": False, "wants_market_offers": False,
                    "is_product_question": False, "audience_filter": None}

    # 2. Plan + Pre-bootstrap ПАРАЛЛЕЛЬНО.
    # Pre-bootstrap не зависит от плана (только от question/entities), поэтому
    # запускаем его в фоне сразу — экономим ~10-15s.
    yield json.dumps({"type": "phase", "value": "planning"})

    from ..rag.web_search import (detect_entities, search as web_search,
                                    search_pdfs_on_domain, search_topical_reviews,
                                    detect_product_topic,
                                    get_direct_product_urls, get_direct_review_urls)
    from ..rag.indexer import ingest_document_from_url
    from ..rag.seed_sources import expand_with_seeds
    from ..rag.trust import KNOWN_BANK_DOMAINS

    entities = detect_entities(question)

    # ── Product-aware deep dive ──────────────────────────────────────────
    # Если вопрос про конкретный банковский продукт (доверенности, тарифы,
    # переводы…) И в нём упомянуты конкретные банки → запускаем углублённый
    # PDF/тариф-поиск на каждом банковском домене + тематический поиск
    # отзывов по продукту на банки.ру/sravni.ru.
    # ВАЖНО: приоритет resolved (LLM) поверх хардкода. Хардкод остаётся
    # safety net на случай LLM-fail (см. resolved == empty выше).
    _q_topic = resolved.get("topic") or detect_product_topic(question)
    # Generic fallback: если ни resolver ни хардкод не нашли topic, но в вопросе
    # есть явные banking-объекты сравнения (приложение, поддержка, отделения,
    # карта, etc.) — используем их как topic-keyword. Это покрывает темы вне
    # PRODUCT_TOPIC_TRIGGERS.
    if not _q_topic and question:
        _q_low = question.lower()
        for kw, normalized in [
            ("мобильн", "приложение"), ("приложен", "приложение"),
            ("интернет-банк", "интернет-банк"), ("личный кабинет", "личный кабинет"),
            ("чат-бот", "чат-бот"), ("банкомат", "банкомат"),
            ("отделен", "отделения"), ("поддерж", "поддержка"),
            ("колл-центр", "поддержка"),
            ("дистанц", "дистанционное обслуживание"),
            ("онбординг", "онбординг"), ("kyc", "верификация"),
        ]:
            if kw in _q_low:
                _q_topic = normalized
                log.warning("[topic-fallback] inferred q_topic=%s from keyword %r",
                             normalized, kw)
                break
    _resolved_bank_slugs = [b["slug"] for b in (resolved.get("banks") or [])]
    _q_bank_slugs = _resolved_bank_slugs or detect_bank_slugs(question)
    # Generic synonyms+URL kws из резолвера — эти списки прокидываются в
    # все topic-aware проверки (matches_topic_generic) вместо
    # _TOPIC_SYNONYMS[topic] хардкода.
    _topic_synonyms = (resolved.get("topic_synonyms")
                        or _TOPIC_SYNONYMS.get(_q_topic) or [])
    _topic_url_kws  = resolved.get("url_keywords") or []
    if _q_topic and not _topic_url_kws:
        # Fallback URL keywords — производные от synonyms (короткие подстроки)
        _topic_url_kws = list({s[:8].lower() for s in _topic_synonyms if len(s) >= 4})
    # Маппинг slug → (domain, human_name) для product-search
    _slug_to_domain = {v: k for k, v in KNOWN_BANK_DOMAINS.items()}
    _slug_to_name = {
        "sberbank":    "Сбербанк",
        "vtb":         "ВТБ",
        "alfabank":    "Альфа-Банк",
        "tinkoff":     "Тинькофф",
        "sovcombank":  "Совкомбанк",
        "gazprombank": "Газпромбанк",
        "rshb":        "Россельхозбанк",
        "domrf":       "Банк ДОМ.РФ",
        "otkritie":    "Открытие",
        "raiffeisen":  "Райффайзен",
        "pochtabank":  "Почта Банк",
        "mkb":         "МКБ",
        "psb":         "ПСБ",
        "rosbank":     "Росбанк",
        "uralsib":     "Уралсиб",
        "akbars":      "Ак Барс",
        "mtsbank":     "МТС Банк",
        "ozonbank":    "Озон Банк",
        "yandexbank":  "Яндекс Банк",
    }

    def _do_pre_bootstrap():
        """Sync блок pre-bootstrap для запуска в executor."""
        try:
            from concurrent.futures import ThreadPoolExecutor as _TPE

            seed_urls = expand_with_seeds(question, entities, max_urls=14)
            if seed_urls:
                def _ingest_one(s):
                    try: return ingest_document_from_url(s["url"])
                    except Exception: return None
                with _TPE(max_workers=6) as pool:
                    list(pool.map(_ingest_one, seed_urls, timeout=60))
            # Entity DDG search (если есть упомянутые компании)
            if entities:
                with _TPE(max_workers=4) as pool:
                    list(pool.map(
                        lambda e: _try_pre_bootstrap_entity(e, web_search, ingest_document_from_url),
                        entities[:4]))

            # ── Product-aware deep dive: PDF + тематические отзывы ──
            if _q_topic and _q_bank_slugs:
                log.info("product deep-dive: topic=%s, banks=%s",
                         _q_topic, _q_bank_slugs)
                # Собираем список «ингест-задач» — все URL'ы (PDF и отзывы)
                # параллелим в пуле; ингест дешёвый, главное — fan-out поиска.
                deep_urls: list[tuple[str, str | None]] = []   # (url, slug_hint)
                seen: set[str] = set()
                # ВАЖНО: DDG/Yandex часто банят (HTTP 403/202/timeout) → они
                # съедают 30-60s впустую. Здесь полагаемся ТОЛЬКО на прямые URL'ы.
                # slug_hint нужен чтобы ingest присвоил bank_id, иначе фильтр
                # bank_slugs на semantic_search их исключает.
                for slug in _q_bank_slugs[:8]:
                    domain = _slug_to_domain.get(slug)
                    if domain:
                        # Direct URL templates (стабильные landing-pages)
                        for r in get_direct_product_urls(
                                    domain, _q_topic,
                                    synonyms=_topic_synonyms,
                                    audience_filter=resolved.get("audience_filter"),
                                    product_url_paths=resolved.get("product_url_paths"),
                                    bank_slug=slug,
                                    bank_specific_paths=resolved.get("bank_specific_paths"),
                                 )[:10]:   # 8 → 10 (LLM-paths добавляют 4-6 URL'ов)
                            u = r.get("url") or ""
                            if u and u not in seen:
                                deep_urls.append((u, slug)); seen.add(u)
                        # SearXNG/Brave таргетный поиск на сайте банка
                        for sq in (
                            f'site:{domain} "{_q_topic}" filetype:pdf',
                            f'site:{domain} {_q_topic} условия',
                            f'site:{domain} {_q_topic} тарифы',
                        ):
                            try:
                                rs = web_search(sq, max_results=4,
                                                cache_ttl_seconds=1800)
                            except Exception:
                                rs = []
                            for r in rs[:3]:
                                u = r.get("url") or ""
                                if not u or u in seen: continue
                                blob = f"{u} {r.get('title','')} {r.get('snippet','')}"
                                # Generic match по resolver-synonyms — работает
                                # для ЛЮБОГО topic'а, не только хардкодных.
                                if not matches_topic_generic(blob, _topic_synonyms,
                                                              url=u, url_keywords=_topic_url_kws):
                                    continue
                                deep_urls.append((u, slug)); seen.add(u)
                    for r in get_direct_review_urls(slug, _q_topic):
                        u = r.get("url") or ""
                        if u and u not in seen:
                            deep_urls.append((u, slug)); seen.add(u)
                if deep_urls:
                    # Round-robin между банками: чтобы Sber/ВТБ/Тинькофф получили
                    # равно URL'ов, а не тонули в порядке списка (раньше [:18] cap
                    # дропал последний банк). Per-bank cap = 12, всего до 96.
                    PER_BANK = 12
                    by_bank: dict[str | None, list] = {}
                    for u, hint in deep_urls:
                        by_bank.setdefault(hint, []).append((u, hint))
                    interleaved: list[tuple[str, str | None]] = []
                    while any(by_bank.values()):
                        for slug in list(by_bank.keys()):
                            if by_bank[slug]:
                                interleaved.append(by_bank[slug].pop(0))
                    # cap по PER_BANK на каждый slug
                    counts: dict = {}
                    final_urls: list = []
                    for u, hint in interleaved:
                        counts[hint] = counts.get(hint, 0) + 1
                        if counts[hint] <= PER_BANK:
                            final_urls.append((u, hint))
                    log.warning("[deep-dive] pre-ingesting %s/%s URLs (per-bank cap %s)",
                                len(final_urls), len(deep_urls), PER_BANK)
                    def _ingest_url(item):
                        u, hint = item
                        ul = u.lower()
                        use_br = not (ul.endswith(".pdf") or "/press/" in ul)
                        try: return ingest_document_from_url(
                            u, prefer_browser=use_br, bank_slug_hint=hint)
                        except Exception: return None
                    with _TPE(max_workers=8) as pool:
                        list(pool.map(_ingest_url, final_urls, timeout=120))
        except Exception as e:
            log.info("pre-bootstrap failed: %s", e)

    # Запускаем pre-bootstrap в фоне И planner LLM-call параллельно
    bootstrap_task = asyncio.get_event_loop().run_in_executor(None, _do_pre_bootstrap)
    plan = await _llm_planner(client, question)
    if not plan:
        plan = [{
            "n": 1, "title": "Общий поиск по вопросу",
            "tool": "semantic_search", "query": question, "entity": None,
        }]

    # Auto-inject get_review_themes для всех упомянутых банков (либо в
    # самом вопросе, либо в plan.step.entity если вопрос был общий).
    # Триггер: «плюсы/минусы/отзыв/жалоб/нрави/неудобн».
    plan_bank_slugs = []
    seen = set()
    for sl in detect_bank_slugs(question):
        if sl not in seen: plan_bank_slugs.append(sl); seen.add(sl)
    for st in plan:
        # 1. Прямой entity-slug
        e = (st.get("entity") or "").lower()
        if e in BANK_SLUG_TRIGGERS and e not in seen:
            plan_bank_slugs.append(e); seen.add(e)
        # 2. Распознаём slugs из title шага (planner часто пишет
        # "Сбербанк доверенности условия" с entity=null)
        title_slugs = detect_bank_slugs(st.get("title") or "")
        for sl in title_slugs:
            if sl not in seen:
                plan_bank_slugs.append(sl); seen.add(sl)
    log.warning("[deep-dive] plan_bank_slugs=%s, q_topic=%s, q_bank_slugs=%s",
                plan_bank_slugs, _q_topic, _q_bank_slugs)

    if question_wants_reviews(question):
        already_covered = {(s.get("entity") or "").lower()
                            for s in plan if s.get("tool") == "get_review_themes"}
        next_n = max((s["n"] for s in plan), default=0) + 1
        injected_n = 0
        for slug in plan_bank_slugs:
            if slug in already_covered:
                continue
            plan.append({
                "n":      next_n,
                "title":  f"Отзывы клиентов: {slug}",
                "tool":   "get_review_themes",
                "query":  "",
                "entity": slug,
            })
            next_n += 1
            injected_n += 1
        if injected_n:
            log.info("auto-injected %s review steps for: %s",
                     injected_n, plan_bank_slugs)

    # ── Auto-inject get_market_offers для product-вопросов про конкретные
    # ставки/тарифы. Это даёт synthesizer'у структурированную таблицу
    # «банк × ставка × мин-сумма × срок» прямо из БД-витрин (v_offer_current),
    # независимо от того что найдёт SearXNG / scrape.
    _market_cat = resolved.get("category_hint")
    _wants_market = resolved.get("wants_market_offers")
    if _market_cat and _wants_market:
        next_n = max((s["n"] for s in plan if isinstance(s.get("n"), int)), default=0) + 1
        plan.append({
            "n":      next_n,
            "title":  f"Маркет-офферы: {_market_cat}",
            "tool":   "get_market_offers",
            "query":  "",
            "entity": None,
            "_args":  {"category": _market_cat, "limit": 30},
        })
        log.info("auto-injected get_market_offers(category=%s)", _market_cat)

    # ── Auto-inject GOVT-шагов для социально-регулируемых продуктов ──
    # Карта ветерана СВО, военная/семейная/IT ипотека, льготы пенсионерам,
    # маткапитал, страхование вкладов АСВ — для них первоисточник это
    # нормативка, не маркетинг банка. Triggered флагом resolver.is_socially_regulated
    # (LLM решает по семантике вопроса, а не по словарю). Fallback — keyword
    # триггеры для случая когда resolver-LLM ленится.
    _is_social = bool(resolved.get("is_socially_regulated"))
    if not _is_social:
        # Fallback на keyword-триггеры (на случай если resolver не выставил флаг)
        _audience = (resolved.get("audience_filter") or "").lower()
        _q_topic_low = (_q_topic or "").lower()
        _FALLBACK_TRIGGERS = (
            "ветеран", "сво", "военнослуж", "участник спецоперац",
            "пенсионер", "льготн", "многодет", "инвалид", "медработник",
            "материнск", "семья с детьми", "ипотек", "страхов",
        )
        _is_social = any(t in _audience or t in _q_topic_low
                           for t in _FALLBACK_TRIGGERS) or any(
            t in " ".join(s.lower() for s in _topic_synonyms or [])
            for t in _FALLBACK_TRIGGERS
        )
    if _is_social and _q_topic:
        next_n = max((s["n"] for s in plan if isinstance(s.get("n"), int)), default=0) + 1
        # 2 шага — одного не хватит чтобы перекрыть и НПА, и разъяснения
        govt_steps = [
            {
                "n": next_n,
                "title": f"Нормативка: {_q_topic}",
                "tool": "semantic_search",
                "query": (f"{_q_topic} постановление правительства приказ "
                          f"закон льготы pravo.gov.ru government.ru НПА"),
                "entity": None,
            },
            {
                "n": next_n + 1,
                "title": f"Регулятор/госуслуги: {_q_topic}",
                "tool": "semantic_search",
                "query": (f"{_q_topic} разъяснения ЦБ РФ banki-участники "
                          f"программа реестр cbr.ru gosuslugi.ru mil.ru"),
                "entity": None,
            },
        ]
        plan.extend(govt_steps)
        log.info("auto-injected %s govt-шага для social product '%s'",
                 len(govt_steps), _q_topic)

    # ── Post-planner product deep-dive (если plan дал нам банки которых не
    # было в исходной фразе вопроса). Запускаем в фоне параллельно с
    # executor — он не блокирует main pipeline.
    _deep_task: asyncio.Task | None = None
    if _q_topic and plan_bank_slugs and not _q_bank_slugs:
        # _q_bank_slugs было пусто — но planner раскрыл «разные банки» в
        # конкретные. Запускаем deep-dive здесь.
        # Захватим synonyms/audience/paths для universal URL discovery
        _aud = (resolved or {}).get("audience_filter")
        _syns = list(_topic_synonyms or [])
        _paths = list((resolved or {}).get("product_url_paths") or [])
        _bsp = dict((resolved or {}).get("bank_specific_paths") or {})
        def _do_post_deep_dive(slugs, topic, syns=_syns, aud=_aud,
                                paths=_paths, bsp=_bsp):
            try:
                from concurrent.futures import ThreadPoolExecutor as _TPE
                # (url, prefer_browser, bank_slug_hint).
                # bank_slug_hint критичен: без него banki.ru/sravni.ru документы
                # ingest'ятся с bank_id=NULL, и фильтр bank_slugs=[slug] на
                # semantic_search их исключает — мы их не находим.
                deep_urls: list[tuple[str, bool, str | None]] = []
                seen_u: set[str] = set()
                for slug in slugs[:8]:
                    domain = _slug_to_domain.get(slug)
                    if domain:
                        # 1. Direct URL templates (стабильные landing-pages)
                        urls = get_direct_product_urls(domain, topic,
                                                         synonyms=syns,
                                                         audience_filter=aud,
                                                         product_url_paths=paths,
                                                         bank_slug=slug,
                                                         bank_specific_paths=bsp)
                        bank_specific = [r for r in urls
                                          if "(direct generic)" not in r.get("title","")]
                        generic = [r for r in urls
                                    if "(direct generic)" in r.get("title","")]
                        top_urls = bank_specific[:1] + generic[:1]
                        for r in top_urls:
                            u = r.get("url") or ""
                            if u and u not in seen_u:
                                ul = u.lower()
                                use_br = not (ul.endswith(".pdf") or "/press/" in ul)
                                deep_urls.append((u, use_br, slug)); seen_u.add(u)
                        # 2. SearXNG/Brave search — таргетный поиск на сайте банка.
                        # Берём топ-5 результатов которые содержат topic-слово в URL/title.
                        # SearXNG не банится → можем спамить (но всё равно лимитируемся).
                        for sq in (
                            f'site:{domain} "{topic}" filetype:pdf',
                            f'site:{domain} {topic} условия',
                            f'site:{domain} {topic} тарифы',
                        ):
                            try:
                                rs = web_search(sq, max_results=4,
                                                cache_ttl_seconds=1800)
                            except Exception:
                                rs = []
                            for r in rs[:3]:
                                u = r.get("url") or ""
                                if not u or u in seen_u: continue
                                blob = f"{u} {r.get('title','')} {r.get('snippet','')}"
                                if not matches_topic_generic(blob, _topic_synonyms,
                                                              url=u, url_keywords=_topic_url_kws):
                                    continue
                                ul = u.lower()
                                use_br = not (ul.endswith(".pdf") or "/press/" in ul)
                                deep_urls.append((u, use_br, slug)); seen_u.add(u)
                    # 3. banki.ru reviews — HTTP+JSON-LD парсер. bank_slug_hint=slug
                    # привязывает документ к банку для filter-recall.
                    for r in get_direct_review_urls(slug, topic)[:2]:
                        u = r.get("url") or ""
                        if u and u not in seen_u:
                            deep_urls.append((u, False, slug)); seen_u.add(u)
                if deep_urls:
                    log.warning("[deep-dive] post-ingesting %s URLs (banks=%s)",
                                len(deep_urls), slugs)
                    def _ingest(item):
                        u, use_br, hint = item
                        try:
                            return ingest_document_from_url(
                                u, prefer_browser=use_br, bank_slug_hint=hint)
                        except Exception as e:
                            log.info("[deep-dive] ingest %s failed: %s",
                                     u[:80], type(e).__name__)
                            return None
                    with _TPE(max_workers=6) as pool:
                        list(pool.map(_ingest, deep_urls[:24], timeout=90))
            except Exception as e:
                log.info("post-deep-dive failed: %s", e)
        _deep_task = asyncio.get_event_loop().run_in_executor(
            None, _do_post_deep_dive, plan_bank_slugs, _q_topic)

    yield json.dumps({"type": "plan", "steps": plan}, ensure_ascii=False)

    # Дожидаемся pre-bootstrap (с timeout 90s) — он наверняка ещё работает
    yield json.dumps({"type": "phase", "value": "discovery"})
    try:
        await asyncio.wait_for(bootstrap_task, timeout=90)
    except asyncio.TimeoutError:
        log.info("pre-bootstrap exceeded 90s timeout — proceeding")
    except Exception as e:
        log.info("pre-bootstrap error: %s", e)
    # Дожидаемся post-planner deep-dive если он был запущен
    if _deep_task is not None:
        try:
            await asyncio.wait_for(_deep_task, timeout=70)
        except asyncio.TimeoutError:
            log.info("post-deep-dive exceeded timeout — proceeding")
        except Exception as e:
            log.info("post-deep-dive error: %s", e)

    # 4. Execute steps
    sources: list[dict] = []
    steps_results: list[dict] = []
    yield json.dumps({"type": "phase", "value": "research"})

    # Анализируем вопрос: банки + упомянутые слуги для bank-only фильтра.
    # Этот контекст пробрасывается в semantic_search чтобы CIAN/Avito-документы
    # не вылезали в банковский ответ.
    _question_bank_slugs = detect_bank_slugs(question)
    _is_banking_q = bool(_question_bank_slugs) or any(
        w in (question or "").lower()
        for w in ("банк", "банки", "банков", "банка", "кредит", "вклад",
                  "ипотек", "карт", "перевод", "тариф", "комисси")
    )
    # Если упомянуты ТАКЖЕ классифайды (циан/авито/домклик) — не суживаем,
    # чтобы не порезать законный case «Сбер vs Домклик по ипотеке».
    _has_classifieds = any(
        w in (question or "").lower()
        for w in ("циан", "cian", "авито", "avito", "домклик", "domclick",
                  "классифайд", "недвижимост"))
    if _has_classifieds:
        _is_banking_q = False
    log.info("banking-q=%s, slugs=%s, classifieds=%s",
             _is_banking_q, _question_bank_slugs, _has_classifieds)

    # ── Параллельное выполнение шагов в batch'ах по 4 ──
    # Это главный latency-win: 12 шагов sequential = 60-180s, batch×4 = 30-50s.
    # Sources аккумулируются threadsafe (GIL + dict). Events стримим после batch.

    async def _exec_one_step(step):
        """Выполнить один шаг — вернуть (events, summary_dict).
        Все internal yields собираются в events list, потом стримятся в порядке."""
        events = [{"type":"step_start","n":step["n"],
                   "title":step["title"],"tool":step["tool"]}]
        try:
            args = _build_step_args(step,
                                     question_bank_slugs=_question_bank_slugs,
                                     is_banking_question=_is_banking_q)
            result_json = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _run_tool, step["tool"], args),
                timeout=45,
            )
            # Adaptive fallback
            try: parsed_check = json.loads(result_json)
            except Exception: parsed_check = None
            is_empty = False
            if step["tool"] == "semantic_search":
                is_empty = isinstance(parsed_check, dict) and not parsed_check.get("results")
            elif step["tool"] == "fetch_official":
                is_empty = (isinstance(parsed_check, dict)
                            and (parsed_check.get("chunks_added", 0) == 0
                                 or parsed_check.get("skipped_reason") in
                                     ("empty_after_parse","captcha","fetch_failed",
                                      "duplicate","sponsored_or_low_trust","no_chunks")))
            if is_empty:
                try:
                    fallback_count = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(
                            None, _adaptive_web_fallback, step, 2, _q_topic),
                        timeout=45,
                    )
                except asyncio.TimeoutError:
                    fallback_count = 0
                if fallback_count > 0:
                    # retry: ВКЛЮЧАЕМ bank_slugs filter если step имел entity
                    # И топик добавляем в query чтобы semantic_search ранжировал
                    # тематические доки выше.
                    _retry_q = step.get("query") or step.get("title") or ""
                    if _q_topic and _q_topic not in _retry_q.lower():
                        _retry_q = f"{_retry_q} {_q_topic}"
                    retry_args = {"query": _retry_q,
                                  "trust_min": 0.4, "top_k": 10}
                    if _is_banking_q and step.get("entity") in (_question_bank_slugs or []):
                        retry_args["bank_slugs"] = [step["entity"]]
                    try:
                        result_json = await asyncio.wait_for(
                            asyncio.get_event_loop().run_in_executor(
                                None, _run_tool, "semantic_search", retry_args),
                            timeout=15,
                        )
                    except asyncio.TimeoutError:
                        pass
                    log.info("step %s adaptive: +%s docs", step["n"], fallback_count)

            result_json = _extract_sources_from_tool_result(step["tool"], result_json, sources)

            # Summary
            try:
                parsed = json.loads(result_json)
                if isinstance(parsed, dict):
                    if step["tool"] == "semantic_search":
                        rs = parsed.get("results") or []
                        summary_parts = [f"Найдено {len(rs)} фрагментов:"]
                        for r in rs[:5]:
                            cite = r.get("citation", "")
                            summary_parts.append(
                                f"  {cite} {r.get('bank_name','?')} "
                                f"[{r.get('source_kind','')}]: "
                                f"{(r.get('text') or '')[:280]}")
                        result_summary = "\n".join(summary_parts)
                    elif step["tool"] == "get_review_themes":
                        result_summary = (
                            f"Bank {parsed.get('bank_slug')}: "
                            f"total={parsed.get('total_reviews')}, "
                            f"avg_rating={parsed.get('avg_rating')}\n"
                            f"Top complaints: {json.dumps(parsed.get('top_complaints') or [], ensure_ascii=False)[:1500]}\n"
                            f"Top praise: {json.dumps(parsed.get('top_praise') or [], ensure_ascii=False)[:600]}")
                    else:
                        result_summary = json.dumps(parsed, ensure_ascii=False)[:6000]
                else:
                    result_summary = result_json[:6000]
            except Exception:
                result_summary = result_json[:6000]

            new_n_count = sum(1 for s in sources if s.get("step_n") is None)
            for s in sources:
                if s.get("step_n") is None:
                    s["step_n"] = step["n"]
            # total_used — сколько источников шаг ИСПОЛЬЗОВАЛ (включая уже виденные).
            # new_n_count — сколько добавил впервые. UI должен показывать оба.
            total_used = 0
            try:
                _p = json.loads(result_json)
                if isinstance(_p, dict):
                    if step["tool"] == "semantic_search":
                        total_used = len(_p.get("results") or [])
                    elif step["tool"] == "fetch_official":
                        total_used = 1 if _p.get("url") and _p.get("trust_score") else 0
            except Exception:
                pass
            events.append({"type":"step_done","n":step["n"],
                           "found":new_n_count, "used": total_used,
                           "tool":step["tool"]})
            return events, {**step, "result_summary": result_summary}
        except asyncio.TimeoutError:
            events.append({"type":"step_done","n":step["n"],"found":0,"error":"timeout"})
            return events, {**step, "result_summary": "(timeout)"}
        except Exception as e:
            log.warning("step %s failed: %s", step["n"], e)
            events.append({"type":"step_done","n":step["n"],"found":0,"error":str(e)[:200]})
            return events, {**step, "result_summary": f"(ошибка: {str(e)[:200]})"}

    BATCH_SIZE = 4
    for batch_start in range(0, len(plan), BATCH_SIZE):
        batch = plan[batch_start:batch_start+BATCH_SIZE]
        # Стримим step_start событий ДО запуска (UI видит «начался»)
        for st in batch:
            yield json.dumps({"type":"step_start","n":st["n"],
                              "title":st["title"],"tool":st["tool"]},
                             ensure_ascii=False)
        # Параллельно выполняем
        results = await asyncio.gather(*[_exec_one_step(s) for s in batch],
                                         return_exceptions=True)
        # Стримим step_done и сохраняем summaries
        for r in results:
            if isinstance(r, Exception):
                continue
            ev_list, summary = r
            steps_results.append(summary)
            for ev in ev_list:
                # Пропускаем step_start (он уже стримился выше)
                if ev.get("type") == "step_start":
                    continue
                yield json.dumps(ev, ensure_ascii=False)

    # ── 3.4 Gap-detection: проходим по упомянутым в вопросе entity'ям и
    # считаем сколько источников собрано на каждую. Если <2 — запускаем
    # 3 dop-step'а с разными формулировками (auto-spawn sub-tasks).
    try:
        from ..rag.web_search import detect_entities as _de2
        ents_for_gap = _de2(question)
        # Считаем sources per entity — по domain matching в URL
        for ent in ents_for_gap[:5]:
            slug = ent.get("slug","").lower()
            domain = ent.get("domain","")
            d_short = domain.split(".")[0] if domain else slug
            ent_sources = sum(1 for s in sources
                              if d_short in (s.get("url","")+s.get("bank_name","")).lower())
            if ent_sources >= 2:
                continue
            # Gap: 1 combined sub-step (вместо 3) с богатым query
            log.info("gap detected for %s (%s sources) — auto-spawning 1 sub-task",
                     slug, ent_sources)
            # Topic-aware: если у вопроса есть тема (вклад/доверенность/тариф),
            # gap-fill ищет ИМЕННО по теме, а не дефолтные «выручка/бизнес-модель».
            if _q_topic:
                gap_templates = [
                    f"{ent.get('name')} {_q_topic} условия тарифы 2025",
                ]
            else:
                gap_templates = [
                    f"{ent.get('name')} выручка прибыль бизнес-модель 2025 финансовые результаты",
                ]
            for i, q_template in enumerate(gap_templates):
                sub_step = {
                    "n": f"G-{slug}-{i+1}",
                    "title": f"Gap-fill: {ent.get('name')} ({['доходы','модель','стратегия'][i]})",
                    "tool": "fetch_official",   # pass to adaptive fallback
                    "query": q_template,
                    "entity": slug,
                }
                yield json.dumps({"type": "step_start",
                                  "n": sub_step["n"], "title": sub_step["title"],
                                  "tool": sub_step["tool"]}, ensure_ascii=False)
                try:
                    fc = await asyncio.get_event_loop().run_in_executor(
                        None, _adaptive_web_fallback, sub_step, 2, _q_topic
                    )
                    # После ingest — semantic_search чтобы поднять chunks в sources
                    if fc > 0:
                        ss_args = {"query": q_template, "trust_min": 0.4, "top_k": 8}
                        rj = await asyncio.get_event_loop().run_in_executor(
                            None, _run_tool, "semantic_search", ss_args)
                        rj = _extract_sources_from_tool_result("semantic_search", rj, sources)
                        try:
                            parsed_sub = json.loads(rj)
                            new_n = sum(1 for s in sources if s.get("step_n") is None)
                            for s in sources:
                                if s.get("step_n") is None:
                                    s["step_n"] = sub_step["n"]
                            steps_results.append({**sub_step, "result_summary":
                                f"Gap-fill found {fc} new docs; "
                                f"top: {[r.get('url') for r in (parsed_sub.get('results') or [])[:3]]}"})
                        except Exception:
                            pass
                    yield json.dumps({"type": "step_done",
                                      "n": sub_step["n"], "found": fc,
                                      "tool": sub_step["tool"]}, ensure_ascii=False)
                except Exception as e:
                    yield json.dumps({"type": "step_done",
                                      "n": sub_step["n"], "found": 0, "error": str(e)[:200]},
                                     ensure_ascii=False)
    except Exception as e:
        log.info("gap-detection failed: %s", e)

    # 3.5 Post-research entity enrichment: для каждой обнаруженной entity
    # делаем дополнительный широкий semantic_search чтобы поднять chunks из
    # seed-индексированных документов которые planner мог пропустить.
    try:
        from ..rag.web_search import detect_entities as _de
        ents = _de(question)
        for ent in ents[:5]:
            slug = ent.get("slug")
            name = ent.get("name") or slug
            # Topic-aware enrichment: для продуктовых вопросов берём шаблоны
            # по теме, а не «выручка/бизнес-модель» — иначе тянем нерелевантный
            # IR-мусор который потом синтезатор путает с ответом по теме.
            if _q_topic:
                templates = [
                    f"{name} {_q_topic} условия процент тарифы",
                    f"{name} {_q_topic} оформление документы требования",
                ]
            else:
                templates = [
                    f"{name} выручка доходы расходы прибыль 2025",
                    f"{name} бизнес-модель монетизация целевая аудитория",
                    f"{name} рынок доля конкуренты анализ",
                ]
            for q_template in templates:
                args = {"query": q_template, "top_k": 6, "trust_min": 0.4}
                try:
                    result_json = await asyncio.get_event_loop().run_in_executor(
                        None, _run_tool, "semantic_search", args)
                    result_json = _extract_sources_from_tool_result(
                        "semantic_search", result_json, sources)
                    # Добавляем как "Entity enrichment" step (виртуальный, не в plan)
                    parsed_e = json.loads(result_json)
                    rs = parsed_e.get("results") or []
                    if rs:
                        summary_e = f"Entity enrichment for {name}:\n"
                        for r in rs[:5]:
                            cite = r.get("citation", "")
                            summary_e += (
                                f"  {cite} {r.get('bank_name','?')} "
                                f"[{r.get('source_kind','')}] trust={r.get('trust_score'):.2f}: "
                                f"{(r.get('text') or '')[:300]}\n"
                            )
                        steps_results.append({
                            "n": f"E-{slug}", "title": f"Entity context: {name}",
                            "tool": "semantic_search", "query": q_template,
                            "entity": slug,
                            "result_summary": summary_e,
                        })
                        break    # достаточно одного успешного query per entity
                except Exception:
                    pass
    except Exception as e:
        log.info("entity enrichment failed: %s", e)

    # 3.6 Topical reviews — для banking-вопросов с конкретной темой берём
    # отзывы из таблицы review которые УПОМИНАЮТ тему. Это заменяет тематически
    # бессмысленный get_review_themes.
    log.warning("[topical_reviews] entering block: q_topic=%s, plan_bank_slugs=%s",
                 _q_topic, plan_bank_slugs)
    if _q_topic and plan_bank_slugs:
        try:
            for slug in plan_bank_slugs[:8]:
                # Передаём synonyms из resolver — для arbitrary topic'ов работает
                # лучше чем hardcoded _TOPIC_SYNONYMS[topic].
                tr = get_topical_reviews(slug, _q_topic, synonyms=_topic_synonyms)
                if not tr.get("found"):
                    continue
                neg = tr.get("negative_reviews") or []
                pos = tr.get("positive_reviews") or []
                lines = [f"Тематические отзывы по теме «{_q_topic}» для {slug}: {tr['found']} шт."]
                if neg:
                    lines.append(f"\n⚠ Жалобы ({len(neg)}):")
                    for r in neg[:6]:
                        lines.append(f"  · [rating={r['rating']}] «{r['text'][:300]}»")
                if pos:
                    lines.append(f"\n✅ Похвалы ({len(pos)}):")
                    for r in pos[:3]:
                        lines.append(f"  · [rating={r['rating']}] «{r['text'][:250]}»")
                steps_results.append({
                    "n":      f"TR-{slug}",
                    "title":  f"Тематические отзывы: {slug} × {_q_topic}",
                    "tool":   "get_topical_reviews",
                    "query":  _q_topic,
                    "entity": slug,
                    "result_summary": "\n".join(lines),
                })
                log.warning("[topical_reviews] %s/%s: %s reviews (neg=%s, pos=%s)",
                             slug, _q_topic, tr["found"], len(neg), len(pos))
        except Exception as e:
            log.info("topical reviews failed: %s", e)

    # 4. Coverage report — UI покажет насколько богатая база собрана
    high_trust_n  = sum(1 for s in sources if (s.get("trust_score") or 0) >= 0.85)
    mid_trust_n   = sum(1 for s in sources if 0.55 <= (s.get("trust_score") or 0) < 0.85)
    low_trust_n   = sum(1 for s in sources if (s.get("trust_score") or 0) < 0.55)
    coverage_warning = None
    if len(sources) < 3:
        coverage_warning = (f"Найдено всего {len(sources)} источников — отчёт будет "
                            "ограниченным. Попробуйте уточнить вопрос или дать конкретные "
                            "URLs через POST /api/rag/ingest-url.")
    elif high_trust_n == 0:
        coverage_warning = ("Нет источников с trust ≥ 0.85 (регуляторы / IR компаний). "
                            "Числа в отчёте могут быть из вторичных источников.")
    yield json.dumps({"type": "coverage",
                      "total_sources": len(sources),
                      "high_trust": high_trust_n,
                      "mid_trust":  mid_trust_n,
                      "low_trust":  low_trust_n,
                      "warning":    coverage_warning},
                     ensure_ascii=False)

    # 4.9 Per-bank deep fact extraction. Перед synthesizer'ом отдельный
    # LLM-вызов на КАЖДЫЙ банк извлекает максимум структурированных фактов
    # из его чанков. Решает проблему: synthesizer писал «не раскрыто» хотя
    # данные были — он просто не успевал прочитать всё. Теперь факты ему
    # подаются уже извлечёнными списком.
    bank_facts: dict[str, str] = {}
    if _q_topic and plan_bank_slugs and len(sources) >= 3:
        try:
            from sqlalchemy import text as _t
            _STOP_FE = {"и","в","на","по","за","для","или","к","с","от","до","из","о","об"}
            # Резолвер уже отдал морф-формы (ветеран/ветерана/ветеранск/...).
            _kws_short: set[str] = set()
            _kws_phrase: set[str] = set()
            for s in (_topic_synonyms or [_q_topic]):
                sl = (s or "").lower().strip()
                if not (3 <= len(sl) <= 40) or sl in _STOP_FE:
                    continue
                if " " in sl or "-" in sl:
                    _kws_phrase.add(sl)
                else:
                    _kws_short.add(sl)
            kws_list = sorted(_kws_short, key=len)[:12]
            if len(kws_list) < 16:
                kws_list += sorted(_kws_phrase, key=len, reverse=True)[:16-len(kws_list)]
            kws = kws_list or [_q_topic]
            url_to_n = {s.get("url"): s["n"] for s in sources if s.get("url")}

            async def _extract_for_bank(slug: str) -> tuple[str, str | None]:
                """Один банк: SQL → LLM extract → return (slug, facts|None)."""
                log.info("[fact-extract] %s kws=%s", slug, kws[:8])
                where_kws = " OR ".join(f"dc.text ILIKE :k{i}" for i in range(len(kws)))
                params = {"slug": slug}
                for i, k in enumerate(kws): params[f"k{i}"] = f"%{k}%"
                try:
                    with db.session() as s:
                        rows = s.execute(_t(f"""
                            SELECT dc.text, d.url, d.doc_type::text doc_type
                              FROM document_chunk dc
                              JOIN document d ON d.document_id = dc.document_id
                              JOIN bank b ON b.bank_id = d.bank_id
                             WHERE b.slug = :slug
                               AND d.trust_score >= 0.4
                               AND ({where_kws})
                             ORDER BY d.trust_score DESC, d.fetched_at DESC
                             LIMIT 50
                        """), params).mappings().all()
                except Exception as e:
                    log.info("fact-extract SQL %s: %s", slug, e)
                    return slug, None
                if not rows:
                    return slug, None
                src_block = "\n\n".join(
                    f"[доступная цитата: {url_to_n.get(r['url'], '?')}] {r['url'][:80]}\n"
                    f"«{(r['text'] or '')[:1200]}»"
                    for r in rows[:30]
                )
                EXTRACT_SYS = (
                    f"Извлеки из источников банка {slug.upper()} ВСЕ конкретные "
                    f"факты по теме «{_q_topic}»: процентные ставки (включая базовую и надбавки), "
                    f"минимальные/максимальные суммы, сроки, комиссии, требования к документам, "
                    f"особенности продуктов, ограничения, целевая аудитория. "
                    f"Формат: маркированный список. Каждый факт = одна строка с [N] "
                    f"в конце (используй ТОЛЬКО номера из «доступная цитата»). "
                    f"Минимум 6-12 фактов если данные позволяют. БЕЗ преамбулы."
                    + ANSWER_TAG_INSTRUCTION
                )
                try:
                    extract_resp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=smart_model(),
                            messages=[
                                {"role": "system", "content": EXTRACT_SYS},
                                {"role": "user",   "content": src_block[:30000]},
                            ],
                            max_tokens=4500, temperature=0.0,
                        ), timeout=70)
                    facts = _strip_reasoning(
                        (extract_resp.choices[0].message.content or "").strip())
                    if facts and len(facts) > 50:
                        log.warning("[fact-extract] %s: %s chars, %s lines",
                                     slug, len(facts), facts.count("\n"))
                        return slug, facts
                except Exception as e:
                    log.info("fact-extract LLM %s: %s", slug, e)
                return slug, None

            # PARALLEL: все 4-6 банков одновременно. Раньше sequential ~30s × 6 = 3min,
            # теперь max(per-bank time) ≈ 30-50s. Семантически идентично.
            _t0 = __import__("time").time()
            results = await asyncio.gather(
                *[_extract_for_bank(slug) for slug in plan_bank_slugs[:6]],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, tuple) and r[1]:
                    bank_facts[r[0]] = r[1]
            log.warning("[fact-extract] parallel: %s banks → %s facts in %.1fs",
                         len(plan_bank_slugs[:6]), len(bank_facts),
                         __import__("time").time() - _t0)
        except Exception as e:
            log.info("fact-extraction overall failed: %s", e)

    # 5. Synthesizer
    yield json.dumps({"type": "phase", "value": "synthesizing"})
    # Передаём упомянутые entity для comprehensive sweep по БД
    try:
        from ..rag.web_search import detect_entities as _de_for_synth
        ents_for_synth = _de_for_synth(question)
    except Exception:
        ents_for_synth = []
    research_context = _format_research_for_synthesis(
        steps_results, sources, ents_for_synth, topic=_q_topic)

    # P0.2: Claim-level verification ДО synthesizer'а. Каждая extracted-строка
    # с числом проверяется regex'ом на наличие числа в excerpts цитированного
    # source'а. Невозможна галлюцинация «13,5%» если в источниках только «13,14%».
    if bank_facts:
        before_lines = sum(len((v or "").splitlines()) for v in bank_facts.values())
        bank_facts, dropped = filter_verified_facts(bank_facts, sources)
        after_lines = sum(len((v or "").splitlines()) for v in bank_facts.values())
        if dropped:
            log.warning("[claim-verify] dropped %s/%s unverified facts",
                         len(dropped), before_lines)
            for d in dropped[:5]:
                log.warning("  ✗ %s: %s — %s", d["bank"], d["line"][:120], d["reason"])
        else:
            log.warning("[claim-verify] all %s facts verified", before_lines)
        # Стримим UI событие — счётчик «верифицировано / отфильтровано»
        # это trust-сигнал: pipeline защитил от N галлюцинаций.
        yield json.dumps({
            "type": "claim_check",
            "verified":  after_lines,
            "dropped":   len(dropped),
            "samples":   [{"bank": d["bank"], "line": d["line"][:160],
                            "reason": d["reason"]} for d in dropped[:3]],
        }, ensure_ascii=False)

    # P0.3: Conflict detection — если у банка в fact'ах противоречивые числа
    # для одного параметра (ставка/комиссия/срок), synthesizer обязан показать
    # все варианты вместо тихого выбора одного.
    fact_conflicts: list[dict] = detect_conflicts(bank_facts) if bank_facts else []
    if fact_conflicts:
        log.warning("[conflicts] detected %s within-bank conflicts: %s",
                     len(fact_conflicts),
                     [(c["bank"], c["metric"], len(c["values"])) for c in fact_conflicts])

    # Подмешиваем pre-extracted bank facts ВВЕРХ research_context — synthesizer
    # их видит первыми, не теряет при чтении. Даёт +30-50% recall.
    if bank_facts:
        facts_block = ["\n\n# 🎯 PRE-EXTRACTED FACTS (используй эти готовые "
                       "факты в первую очередь, они уже привязаны к [N]):"]
        for slug, fx in bank_facts.items():
            facts_block.append(f"\n## {slug.upper()}\n{fx}\n")
        # Conflict block: synthesizer обязан показать оба варианта явно
        if fact_conflicts:
            facts_block.append(
                "\n## ⚠ ВНИМАНИЕ: ОБНАРУЖЕНЫ ПРОТИВОРЕЧИЯ В ИСТОЧНИКАХ"
                "\nВ отчёте ОБЯЗАТЕЛЬНО покажи оба варианта явно (пример: "
                "«ставка 14% [3] или 12% [7] — расхождение 2 п.п.»). НЕ выбирай "
                "один вариант молча.\n"
            )
            for c in fact_conflicts[:6]:
                vs = " · ".join(f"{v[0]}{v[1]}" for v in c["values"])
                facts_block.append(f"  • {c['bank'].upper()} / {c['metric']}: {vs}")
        research_context = "\n".join(facts_block) + "\n\n" + research_context

    # 5.0 Adaptive outline — структура отчёта подбирается под ВОПРОС, а не
    # по фиксированному шаблону. Это устраняет нелепые «Бизнес-модели:
    # банковские услуги» когда вопрос про доверенности.
    research_summary = "\n".join(
        f"- step {sr['n']} {sr['title']}: {(sr.get('result_summary') or '')[:200]}"
        for sr in steps_results[:18]
    )
    try:
        outline = await asyncio.wait_for(
            _design_outline(client, question, research_summary), timeout=20)
    except Exception:
        outline = []
    if not outline:
        outline = _default_outline_for_question(question, len(ents_for_synth))
    yield json.dumps({"type": "outline",
                      "sections": [{"title": s["title"], "kind": s["kind"]}
                                   for s in outline]}, ensure_ascii=False)
    outline_block = _outline_to_synth_prompt(outline)

    synth_messages = [
        {"role": "system", "content": SYNTHESIZER_BASE + ANSWER_TAG_INSTRUCTION},
        {"role": "user",
         "content": f"# Исходный вопрос\n{question}\n\n{research_context}\n"
                    f"{outline_block}\n\n"
                    f"Напиши отчёт строго по этому outline. Только markdown."},
    ]
    full_report = ""
    # valid_n: только источники, отмеченные как RELEVANT для текущей темы.
    # Если темы нет — считаем все источники валидными.
    if _q_topic:
        relevant_set = set()
        for s in sources:
            ex_joined = " ".join(s.get("excerpts") or [])
            if _matches_topic(ex_joined, _q_topic, url=s.get("url")):
                relevant_set.add(s["n"])
        # Если по теме нет ни одного релевантного — оставляем общий список
        # (synthesizer всё равно увидит OFF-TOPIC-метки и должен писать «не раскрыто»)
        valid_n = relevant_set if relevant_set else {s["n"] for s in sources}
        log.info("topical filter: %s/%s sources are RELEVANT to '%s'",
                 len(relevant_set), len(sources), _q_topic)
    else:
        valid_n = {s["n"] for s in sources}
    try:
        stream = await client.chat.completions.create(
            model=smart_model(), messages=synth_messages,  # synth = глубокое reasoning
            max_tokens=14000, stream=True, temperature=0.15,  # +CoT-buffer для reasoning-моделей
        )
        # Двойная буферизация:
        # 1) _StreamReasoningFilter — отрезает CoT снаружи <answer>...</answer>
        # 2) ch_buf — собирает [N]-ссылки чтобы не разрезать [10] на [1 и 0]
        rfilter = _StreamReasoningFilter()
        ch_buf = ""

        def _emit(piece: str) -> str | None:
            """Накапливаем в ch_buf, по готовности отдаём очищенный chunk."""
            nonlocal ch_buf
            ch_buf += piece
            if "[" in ch_buf and "]" not in ch_buf[ch_buf.rindex("["):] and len(ch_buf) < 200:
                return None
            cleaned = _filter_invalid_citations(ch_buf, valid_n)
            ch_buf = ""
            return cleaned

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not (choice and choice.delta and choice.delta.content):
                continue
            for piece in rfilter.feed(choice.delta.content):
                out = _emit(piece)
                if out:
                    full_report += out
                    yield json.dumps({"type": "text", "chunk": out})
        # Финал: flush filter, потом ch_buf
        for piece in rfilter.flush():
            out = _emit(piece)
            if out:
                full_report += out
                yield json.dumps({"type": "text", "chunk": out})
        if ch_buf:
            cleaned = _filter_invalid_citations(ch_buf, valid_n)
            full_report += cleaned
            yield json.dumps({"type": "text", "chunk": cleaned})
    except Exception as e:
        log.warning("synthesizer failed: %s", e)
        err = f"\n\n⚠ Ошибка синтеза: {str(e)[:200]}\n"
        full_report += err
        yield json.dumps({"type": "text", "chunk": err})

    # 4.4 Critic-pass: если отчёт получился коротким (<3500 chars) ИЛИ
    # использовано <50% источников — критик-LLM находит конкретные пропуски,
    # и второй pass synthesizer'а добавляет недостающее (не переписывая старое).
    try:
        used_cites = set(re.findall(r"\[(\d+)\]", full_report))
        recall = len(used_cites) / max(len(sources), 1)
        if (len(full_report) < 3500 or recall < 0.5) and len(sources) >= 4:
            log.warning("[critic-pass] triggered: len=%s recall=%.0f%% sources=%s",
                         len(full_report), recall*100, len(sources))
            CRITIC_SYS = (
                "Ты — критик аудит-отчёта. Сравни ОТЧЁТ и CONTEXT. Найди КОНКРЕТНЫЕ "
                "факты которые есть в context но пропущены в отчёте. Только то что "
                "реально есть в context — не выдумывай. Верни JSON: "
                '{"missing":[{"fact":"<конкретный факт>","cite":"<N>","section":"<куда добавить>"},...]}'
                ". Минимум 3, максимум 8 пропусков. БЕЗ преамбулы."
                + ANSWER_TAG_INSTRUCTION
            )
            try:
                crit_resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=smart_model(),  # критик ищет пропуски — нужен deep
                        messages=[
                            {"role":"system","content":CRITIC_SYS},
                            {"role":"user","content":
                             f"# ОТЧЁТ\n{full_report[:6000]}\n\n# CONTEXT\n{research_context[:18000]}"},
                        ],
                        max_tokens=1500, temperature=0.0,  # reasoning + missing-list
                    ), timeout=20)
                crit_raw = _strip_reasoning(crit_resp.choices[0].message.content or "{}")
                m = re.search(r"\{[\s\S]*\}", crit_raw)
                missing = []
                if m:
                    try: missing = (_loose_json_loads(m.group(0)).get("missing") or [])[:8]
                    except Exception: missing = []
                if missing:
                    # Второй pass — добавляем missing facts как addendum
                    addendum_block = "\n".join(
                        f"  • {it.get('fact','')} [{it.get('cite','?')}] "
                        f"(в раздел: {it.get('section','')})"
                        for it in missing if isinstance(it, dict)
                    )
                    SUPP_SYS = (
                        "Ты ДОПОЛНЯЕШЬ существующий аудит-отчёт. На основе списка "
                        "missing-фактов ниже — выдай ТОЛЬКО ADDENDUM-блок markdown'а:\n"
                        "## 🔍 Дополнительные детали\n"
                        "Сгруппируй missing-факты по разделам как в оригинале. Каждый "
                        "факт = строка с [N]. БЕЗ преамбулы и заключения."
                        + ANSWER_TAG_INSTRUCTION
                    )
                    supp = await asyncio.wait_for(
                        client.chat.completions.create(
                            model=smart_model(),  # addendum пишет факты
                            messages=[
                                {"role":"system","content":SUPP_SYS},
                                {"role":"user","content":
                                 f"# Missing facts\n{addendum_block}\n\n"
                                 f"# Исходный отчёт (для понимания структуры)\n{full_report[:3000]}"},
                            ],
                            max_tokens=1500, temperature=0.1,
                        ), timeout=30)
                    supp_text = _strip_reasoning(
                        (supp.choices[0].message.content or "").strip())
                    supp_text = _filter_invalid_citations(supp_text, valid_n)
                    if supp_text and len(supp_text) > 100:
                        full_report += "\n\n" + supp_text
                        yield json.dumps({"type": "text", "chunk": "\n\n" + supp_text})
                        log.warning("[critic-pass] addendum +%s chars (%s missing facts)",
                                     len(supp_text), len(missing))
            except Exception as e:
                log.info("critic-pass failed: %s", e)
    except Exception as e:
        log.info("critic-pass overall: %s", e)

    # 4.45 AGENT LOOP (P0.1) — главное отличие от one-shot pipeline'а.
    # После первого drафта критик ищет конкретные ПРОБЕЛЫ КОНТЕНТА (что
    # реально не покрыто в имеющихся источниках), формирует targeted web-search
    # запросы, мы их выполняем, ингестим новые документы, дописываем addendum.
    # До 2 итераций (каждая ~30-50s).
    AGENT_GAP_SYS = (
        "Ты — research agent аудитора. Получаешь черновик аудит-отчёта и его "
        "research_context. Найди КОНКРЕТНЫЕ информационные ПРОБЕЛЫ — что-то, "
        "чего в context'е нет, но для отчёта аудитору необходимо.\n"
        "Для каждого пробела сформулируй ОДИН таргетный поисковый запрос "
        "(на русском или английском, как удобнее для гугления русских банков).\n"
        "ТОЛЬКО реальные пробелы — если в context уже всё есть, верни []. "
        "Хороший пример: «комиссия Сбербанка за досрочное расторжение вклада».\n"
        "Плохой: «общая информация о банке». \n"
        "Возвращай JSON: {\"gaps\":[{\"section\":\"<куда добавить>\","
        "\"what\":\"<что неизвестно>\",\"query\":\"<поисковый запрос>\","
        "\"bank_slug\":\"<slug или null>\"},...]}.\n"
        "Максимум 4 пробела на итерацию (важнее качество чем количество). "
        "БЕЗ преамбулы."
    )

    async def _identify_content_gaps(report: str, ctx: str) -> list[dict]:
        try:
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=LLM_MODEL_NAME,
                    messages=[
                        {"role": "system", "content": AGENT_GAP_SYS},
                        {"role": "user",
                         "content": f"# ВОПРОС АУДИТОРА\n{question}\n\n"
                                    f"# DRAFT-ОТЧЁТ\n{report[:7000]}\n\n"
                                    f"# RESEARCH_CONTEXT (что уже найдено)\n{ctx[:14000]}"},
                    ],
                    max_tokens=1500, temperature=0.0,  # reasoning + 4 gaps
                ), timeout=20)
            raw = _strip_reasoning(resp.choices[0].message.content or "{}")
            m = re.search(r"\{[\s\S]*\}", raw)
            if not m:
                return []
            data = _loose_json_loads(m.group(0))
            gaps = (data.get("gaps") or [])[:4]
            clean: list[dict] = []
            for g in gaps:
                if not isinstance(g, dict): continue
                q = (g.get("query") or "").strip()
                if not q or len(q) < 8: continue
                clean.append({
                    "section":   str(g.get("section") or "")[:120],
                    "what":      str(g.get("what") or "")[:200],
                    "query":     q[:200],
                    "bank_slug": (g.get("bank_slug") or None) if isinstance(g.get("bank_slug"), str) else None,
                })
            return clean
        except Exception as e:
            log.info("identify_gaps failed: %s", e)
            return []

    def _agent_research_gap(gap: dict) -> int:
        """Выполняет таргетный поиск + ingest для одного пробела.
        Жёсткие timeout'ы: search 12s, каждый ingest 25s, всего 45s
        (иначе один залипший Playwright тормозит весь iter)."""
        from concurrent.futures import ThreadPoolExecutor as _TPE
        from ..rag.web_search import search as _ws
        from ..rag.indexer import ingest_document_from_url as _ing
        try:
            results = _ws(gap["query"], max_results=5,
                          cache_ttl_seconds=900) or []
        except Exception as e:
            log.info("agent-loop search failed for %s: %s", gap["query"][:50], e)
            return 0
        seen_urls = {s.get("url") for s in sources}
        new_urls = [r["url"] for r in results
                    if r.get("url") and r["url"] not in seen_urls][:3]   # 4→3
        if not new_urls:
            return 0
        n_added = 0
        def _do_ingest(u):
            try:
                ul = u.lower()
                use_br = not (ul.endswith(".pdf") or "/press/" in ul)
                ir = _ing(u, prefer_browser=use_br,
                          bank_slug_hint=gap.get("bank_slug"))
                return 1 if ir and ir.is_new else 0
            except Exception:
                return 0
        try:
            with _TPE(max_workers=3) as pool:
                # Жёсткий 35s — после этого pool.map бросит TimeoutError,
                # незавершённые тасы будут отменены при выходе из with.
                for r in pool.map(_do_ingest, new_urls, timeout=35):
                    n_added += int(r)
        except Exception as e:
            log.warning("[agent-loop] ingest pool timed out for %s: %s",
                         gap["query"][:50], type(e).__name__)
        return n_added

    AGENT_LOOP_MAX = 2
    for _iter in range(1, AGENT_LOOP_MAX + 1):
        try:
            yield json.dumps({"type": "phase",
                              "value": f"agent_iter_{_iter}"})
            log.warning("[agent-loop] iter %s: identifying gaps...", _iter)
            gaps = await _identify_content_gaps(full_report, research_context)
            if not gaps:
                log.warning("[agent-loop] iter %s: no gaps — stopping", _iter)
                break
            log.warning("[agent-loop] iter %s: %s gaps detected: %s",
                         _iter, len(gaps),
                         [g["query"][:50] for g in gaps])
            yield json.dumps({"type": "agent_gaps",
                              "iteration": _iter,
                              "gaps": [{"what": g["what"], "query": g["query"][:80]}
                                       for g in gaps]}, ensure_ascii=False)
            # Параллельно ресёрчим все gaps. Раньше было `sum(... for g in gaps)`
            # — последовательно, что съедало 4×40 = 160s. Один залипший
            # Playwright блокировал весь iter. Теперь 4 gaps идут одновременно
            # через ThreadPoolExecutor, плюс жёсткий outer timeout 70s.
            from concurrent.futures import ThreadPoolExecutor as _AGTPE
            def _research_all_gaps():
                with _AGTPE(max_workers=4) as pool:
                    try:
                        return sum(pool.map(_agent_research_gap, gaps, timeout=65))
                    except Exception as e:
                        log.warning("[agent-loop] iter %s pool timeout: %s",
                                     _iter, type(e).__name__)
                        return 0
            try:
                n_total_added = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None, _research_all_gaps),
                    timeout=75,
                )
            except asyncio.TimeoutError:
                log.warning("[agent-loop] iter %s outer timeout 75s — proceeding",
                             _iter)
                n_total_added = 0
            if not n_total_added:
                log.warning("[agent-loop] iter %s: no new docs ingested — stopping", _iter)
                break
            log.warning("[agent-loop] iter %s: ingested %s new docs",
                         _iter, n_total_added)
            # Поднимаем новые chunks через semantic_search в sources
            queries_done: set[str] = set()
            for g in gaps:
                if g["query"] in queries_done: continue
                queries_done.add(g["query"])
                try:
                    rj = await asyncio.get_event_loop().run_in_executor(
                        None, _run_tool, "semantic_search",
                        {"query": g["query"], "trust_min": 0.4, "top_k": 8})
                    rj = _extract_sources_from_tool_result(
                        "semantic_search", rj, sources)
                    parsed = json.loads(rj)
                    for s in sources:
                        if s.get("step_n") is None:
                            s["step_n"] = f"AG{_iter}"
                    rs = parsed.get("results") or []
                    if rs:
                        steps_results.append({
                            "n":      f"AG{_iter}-{abs(hash(g['query']))%1000:03d}",
                            "title":  f"Agent gap-fill: {g['what'][:60]}",
                            "tool":   "semantic_search",
                            "query":  g["query"],
                            "entity": g.get("bank_slug"),
                            "result_summary": (
                                f"Found {len(rs)} chunks for gap '{g['what']}':\n" +
                                "\n".join(
                                    f"  {r.get('citation','')} {r.get('bank_name','?')}: "
                                    f"{(r.get('text') or '')[:300]}"
                                    for r in rs[:5])
                            ),
                        })
                except Exception as e:
                    log.info("agent semantic_search failed: %s", e)
            # Перекомпилируем research_context
            try:
                research_context = _format_research_for_synthesis(
                    steps_results, sources, ents_for_synth, topic=_q_topic)
            except Exception as e:
                log.info("recompile context failed: %s", e)

            # Дописываем addendum от обновлённого контекста
            ADDENDUM_SYS = (
                "Ты дополняешь аудит-отчёт новыми фактами, найденными после "
                "первого drафта. Получаешь оригинальный отчёт + новые данные "
                "из research_context. Выдай ТОЛЬКО ADDENDUM-блок markdown'а:\n"
                "## 🔄 Дополнение по итогам уточнения (итерация {})\n"
                "Сгруппируй новые факты по разделам оригинала. Каждый факт = "
                "одна строка с [N]. ТОЛЬКО факты которых не было в исходном "
                "отчёте. БЕЗ преамбулы."
            ).format(_iter) + ANSWER_TAG_INSTRUCTION
            try:
                add_resp = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=smart_model(),  # agent-addendum = новые факты
                        messages=[
                            {"role": "system", "content": ADDENDUM_SYS},
                            {"role": "user",
                             "content": f"# ИСХОДНЫЙ ОТЧЁТ\n{full_report[:5000]}\n\n"
                                        f"# НОВЫЕ ДАННЫЕ\n{research_context[:14000]}"},
                        ],
                        max_tokens=1500, temperature=0.1,
                    ), timeout=30)
                add_text = _strip_reasoning(
                    (add_resp.choices[0].message.content or "").strip())
                add_text = _filter_invalid_citations(add_text, valid_n)
                if add_text and len(add_text) > 100:
                    full_report += "\n\n" + add_text
                    yield json.dumps({"type": "text", "chunk": "\n\n" + add_text})
                    log.warning("[agent-loop] iter %s: addendum +%s chars",
                                 _iter, len(add_text))
            except Exception as e:
                log.info("agent addendum failed: %s", e)
        except Exception as e:
            log.info("[agent-loop] iter %s overall failed: %s", _iter, e)
            break

    # 4.49 FINAL MERGE-PASS — консолидация всех addendum'ов в ОДИН чистый
    # отчёт. После critic-pass + 2 agent-iter'аций мы имеем структуру:
    #   draft + 🔍 critic-addendum + 🔄 iter1 + 🔄 iter2
    # — каждая секция повторяется N раз. Это плохо для аудитора. Делаем
    # один LLM-вызов: «слей дубликаты, сохрани все [N], выдай ONE clean
    # report». Заменяем full_report и стримим как replace-событие.
    has_addendum = ("🔍 Дополнительные детали" in full_report
                    or "🔄 Дополнение по итогам уточнения" in full_report)
    if has_addendum and len(full_report) > 1500:
        try:
            MERGE_SYS = (
                "Ты — финальный редактор аудит-отчёта. Получил DRAFT с оригиналом + "
                "1-3 ADDENDUM-блоками ('🔍 Дополнительные детали', '🔄 Дополнение "
                "по итогам уточнения'). Каждый ADDENDUM повторяет структуру оригинала.\n\n"
                "ЗАДАЧА: выпустить ПОЛНЫЙ, ПОДРОБНЫЙ итоговый отчёт для аудитора.\n\n"
                "ПРАВИЛА:\n"
                "  • СОХРАНИ ВСЕ цитаты [N] и [TR-slug] — это критично\n"
                "  • Удали addendum-заголовки '## 🔍 ...' и '## 🔄 ...'\n"
                "  • В каждой секции ВПЛЕТИ ВСЕ факты из ADDENDUM'ов в оригинал —\n"
                "    не выбрасывай числа/цитаты, перенеси в естественное место\n"
                "  • Дубликаты: если факт повторяется — ОДИН раз с самой полной формулировкой\n"
                "  • Не сокращай ради сокращения. Аудитор хочет всю фактуру.\n"
                "  • Минимум 5000-8000 chars итогового текста (для 2-4 entity)\n"
                "  • Структура секций — как в оригинальном outline\n"
                "  • КАЖДОЕ числовое утверждение ОБЯЗАНО иметь [N]\n"
                "  • НЕ выдумывай новые числа — только то что было в draft\n"
                "  • НЕ убирай '⚠ Не раскрыто' и '⚠ Расхождение/Противоречие'\n"
                "  • РАСШИРЯЙ описательные части: если в draft было «X имеет высокую "
                "    маржинальность [3]» — добавь контекст вокруг (что это значит,\n"
                "    как сравнивается с конкурентами) ИЗ ТОГО ЖЕ источника\n\n"
                "ВЫХОД: полный markdown-отчёт, БЕЗ преамбулы, БЕЗ заключений вне секций."
                + ANSWER_TAG_INSTRUCTION
            )
            yield json.dumps({"type": "phase", "value": "merging"})
            # stage_status — для UI: показать «Финальная сборка отчёта…»
            # видимо, с estimate. Иначе пользователь думает, что зависло.
            yield json.dumps({
                "type": "stage_status", "stage": "merging",
                "label": "Финальная сборка отчёта",
                "detail": f"Сливаем черновик и дополнения в единый отчёт",
                "estimate_s": 60,
            }, ensure_ascii=False)
            log.warning("[merge-pass] consolidating draft (%s chars)", len(full_report))
            merge_stream = await client.chat.completions.create(
                model=smart_model(),  # финальная редактура — самый важный pass
                messages=[
                    {"role": "system", "content": MERGE_SYS},
                    {"role": "user",
                     "content": f"# DRAFT с addendum'ами\n{full_report[:20000]}"},
                ],
                # 18000 = 12k финальный отчёт + 6k CoT-буфер для reasoning-моделей
                max_tokens=18000, temperature=0.0, stream=True,
            )
            # Стримим chunks ВНУТРИ внешнего генератора чтобы периодически
            # отдавать merge_progress-события клиенту — UI видит «идёт сборка,
            # уже накопили X символов» и не думает что зависло.
            merged_buf: list[str] = []
            import time as _time
            merge_started = _time.time()
            last_progress_at = merge_started
            # Reasoning-фильтр: модель может писать CoT перед <answer>.
            # Буфер 32KB — достаточно для длинных reasoning-преамбул, иначе
            # passthrough (для не-reasoning моделей).
            merge_rfilter = _StreamReasoningFilter(soft_buffer_bytes=32000)
            try:
                async for chunk in merge_stream:
                    # 180s — kimi/reasoning стримит CoT перед <answer>, надо запас
                    if _time.time() - merge_started > 180:
                        log.warning("[merge-pass] hit 130s soft-cap, partial=%s",
                                     sum(len(b) for b in merged_buf))
                        break
                    ch = chunk.choices[0] if chunk.choices else None
                    if not (ch and ch.delta and ch.delta.content):
                        continue
                    for piece in merge_rfilter.feed(ch.delta.content):
                        merged_buf.append(piece)
                        now = _time.time()
                        if now - last_progress_at > 6:
                            yield json.dumps({
                                "type": "merge_progress",
                                "chars": sum(len(b) for b in merged_buf),
                                "elapsed_s": int(now - merge_started),
                            })
                            last_progress_at = now
                for piece in merge_rfilter.flush():
                    merged_buf.append(piece)
            except Exception as e:
                log.warning("[merge-pass] stream error: %s", e)
            merged = "".join(merged_buf).strip()
            log.warning("[merge-pass] stream produced %s chars", len(merged))
            merged = _filter_invalid_citations(merged, valid_n)
            # Threshold 0.3: после слива дубликатов merged может быть 35-50%
            # от draft'а — это нормально (addendum'ы часто повторяют контент).
            # Главное чтоб не пустой и содержательный.
            if merged and len(merged) > max(1500, len(full_report) * 0.3):
                # Стримим replace — UI должен заменить весь body
                yield json.dumps({"type": "report_replace", "text": merged},
                                 ensure_ascii=False)
                log.warning("[merge-pass] replaced %s → %s chars",
                             len(full_report), len(merged))
                full_report = merged
            else:
                log.info("[merge-pass] result too short, keeping draft+addendums")
        except Exception as e:
            log.warning("[merge-pass] failed: %s", e)

    # 4.5 Multi-pass review: считаем «не раскрыто» в драфте, и для каждого
    # упомянутого entity где >2 «не раскрыто» — делаем 2-й pass с новыми
    # web search и enrichment, потом дописываем addendum.
    try:
        not_disclosed_count = full_report.count("Не раскрыто") + full_report.count("не раскрыто")
        unique_cites = len(set(re.findall(r"\[(\d+)\]", full_report)))
        # 2-й pass только когда РЕАЛЬНО мало источников использовано (<4 unique).
        # Иначе comprehensive context уже покрыл — расходуем токены зря.
        should_second_pass = (unique_cites < 4 and not_disclosed_count >= 5
                              and bool(ents_for_synth))
        if should_second_pass:
            yield json.dumps({"type": "phase", "value": "second_pass"})
            log.info("multi-pass: %s 'не раскрыто' detected → 2nd research pass",
                     not_disclosed_count)
            # Параллельно делаем web search для всех entity'ей с разными query
            additional_indexed = 0
            from concurrent.futures import ThreadPoolExecutor, as_completed
            sub_steps = []
            for ent in ents_for_synth[:4]:
                name = ent.get("name") or ent.get("slug")
                for q in [
                    f"{name} годовой отчёт 2025 финансовые показатели выручка",
                    f"{name} операционные расходы IT маркетинг персонал",
                    f"{name} стратегия конкуренты доля рынка",
                ]:
                    sub_steps.append({
                        "n": f"R2-{ent.get('slug')}-{len(sub_steps)}",
                        "title": f"2nd pass: {name}",
                        "tool": "fetch_official",
                        "query": q,
                        "entity": ent.get("slug"),
                    })
            # Параллельно по subset
            with ThreadPoolExecutor(max_workers=4) as pool:
                futs = [pool.submit(_adaptive_web_fallback, ss) for ss in sub_steps[:8]]
                for f in as_completed(futs, timeout=120):
                    try: additional_indexed += f.result()
                    except: pass
            log.info("2nd pass: indexed %s additional docs", additional_indexed)

            # Поднимем все новые chunks через широкий semantic_search
            for ent in ents_for_synth[:4]:
                name = ent.get("name") or ent.get("slug")
                for q in [
                    f"{name} выручка прибыль расходы EBITDA",
                    f"{name} бизнес-модель сегменты монетизация",
                ]:
                    try:
                        rj = await asyncio.get_event_loop().run_in_executor(
                            None, _run_tool, "semantic_search",
                            {"query": q, "trust_min": 0.4, "top_k": 6})
                        rj = _extract_sources_from_tool_result("semantic_search", rj, sources)
                    except Exception: pass

            # ВСЕГДА запускаем addendum (comprehensive context может содержать
            # данные которых не было в первом pass'е, даже без новых docs)
            if True:
                # Перегенерируем context с обновлёнными sources, делаем addendum
                new_context = _format_research_for_synthesis(
                    steps_results, sources, ents_for_synth, topic=_q_topic)
                addendum_messages = [
                    {"role": "system", "content": SYNTHESIZER_SYSTEM + ANSWER_TAG_INSTRUCTION},
                    {"role": "user", "content":
                        f"# Исходный вопрос\n{question}\n\n"
                        f"# Первый драфт отчёта (с пробелами):\n{full_report[:8000]}\n\n"
                        f"{new_context}\n\n"
                        f"Дополни/перепиши отчёт используя НОВЫЕ источники, появившиеся "
                        f"после второго pass'а. Заполни все блоки '⚠ Не раскрыто' "
                        f"если в новых данных есть информация. Формат тот же. "
                        f"Только markdown."},
                ]
                yield json.dumps({"type": "text", "chunk":
                    "\n\n---\n\n## Дополнительные данные (2-й pass)\n\n"})
                full_report += "\n\n## Дополнительные данные (2-й pass)\n\n"
                try:
                    stream2 = await client.chat.completions.create(
                        model=smart_model(), messages=addendum_messages,
                        max_tokens=6000, stream=True, temperature=0.15,
                    )
                    ch_buf2 = ""
                    rfilter2 = _StreamReasoningFilter()
                    valid2 = valid_n | {s["n"] for s in sources}

                    def _emit2(piece: str) -> str | None:
                        nonlocal ch_buf2
                        ch_buf2 += piece
                        if "[" in ch_buf2 and "]" not in ch_buf2[ch_buf2.rindex("["):] and len(ch_buf2) < 200:
                            return None
                        cleaned = _filter_invalid_citations(ch_buf2, valid2)
                        ch_buf2 = ""
                        return cleaned

                    async for chunk in stream2:
                        choice = chunk.choices[0] if chunk.choices else None
                        if not (choice and choice.delta and choice.delta.content):
                            continue
                        for piece in rfilter2.feed(choice.delta.content):
                            out = _emit2(piece)
                            if out:
                                full_report += out
                                yield json.dumps({"type": "text", "chunk": out})
                    for piece in rfilter2.flush():
                        out = _emit2(piece)
                        if out:
                            full_report += out
                            yield json.dumps({"type": "text", "chunk": out})
                    if ch_buf2:
                        cleaned2 = _filter_invalid_citations(ch_buf2, valid2)
                        full_report += cleaned2
                        yield json.dumps({"type": "text", "chunk": cleaned2})
                    valid_n = valid2
                except Exception as e:
                    log.warning("2nd pass synthesizer failed: %s", e)
    except Exception as e:
        log.info("multi-pass review failed: %s", e)

    # 4.7 + 5 + 6 ПАРАЛЛЕЛЬНО: Cross-validation, Verifier, Charts.
    # Они независимы → asyncio.gather. Экономия ~15-20s.
    yield json.dumps({"type": "phase", "value": "post_processing"})

    # sources_dump — РАСШИРЕННЫЙ контекст для verifier'а. Раньше передавали
    # только excerpts (до 4×600 chars), и verifier флагал ВСЕ числа из больших
    # PDF как «не найдены» — потому что 95% документа было вне excerpts.
    # Теперь:
    #   1) до 6 excerpts × 800 chars на каждый source
    #   2) ПЛЮС полный research_context (steps_results) — там snippet'ы и
    #      tool-результаты с числами которые synthesizer реально видел
    _dump_lines = []
    for s in sources:
        head = f"[{s['n']}] {s.get('bank_name','?')} · {s.get('source_kind','?')} :: {s.get('url','')}"
        _dump_lines.append(head)
        for ex in (s.get("excerpts") or [])[:6]:
            ex_norm = ex.replace("\n", " ").strip()
            if ex_norm:
                _dump_lines.append(f"    «{ex_norm[:750]}»")
    # Дополняем research_context (полный — что видел synthesizer)
    _research_excerpt = (research_context or "")[:25000]
    sources_dump = "\n".join(_dump_lines) + (
        "\n\n# RESEARCH CONTEXT (числа и факты которые видел synthesizer):\n"
        + _research_excerpt if _research_excerpt else ""
    )

    async def _do_verify():
        try:
            return await _verify_claims(client, full_report, sources_dump)
        except Exception as e:
            log.info("verifier failed: %s", e)
            return []

    async def _do_charts():
        try:
            return await _generate_charts(client, full_report,
                                            bank_facts=bank_facts)
        except Exception as e:
            log.info("charts failed: %s", e)
            return []

    async def _do_cross_validation():
        try:
            before_cites = re.findall(r"\[(\d{1,3})\]", full_report)
            enriched = await asyncio.get_event_loop().run_in_executor(
                None, _enrich_citations_with_corroboration, full_report, sources)
            after_cites = re.findall(r"\[(\d{1,3})\]", enriched)
            added = len(after_cites) - len(before_cites)
            return enriched, added
        except Exception as e:
            log.info("cross-validation failed: %s", e)
            return full_report, 0

    # Запускаем все 3 параллельно
    verify_t = asyncio.create_task(_do_verify())
    charts_t = asyncio.create_task(_do_charts())
    cv_t     = asyncio.create_task(_do_cross_validation())

    unverified, charts, (cv_report, cv_added) = await asyncio.gather(
        verify_t, charts_t, cv_t, return_exceptions=False
    )

    # Применяем cross-validation результат и стримим event
    if cv_added > 0:
        full_report = cv_report
        yield json.dumps({"type": "cross_validation",
                          "added_refs": cv_added}, ensure_ascii=False)

    # Verifier: дополнительный invalid-N check
    valid_ns = {s["n"] for s in sources}
    found_ns = set(int(m) for m in re.findall(r"\[(\d{1,3})\]", full_report))
    invalid_ns = sorted(n for n in found_ns if n not in valid_ns)
    if invalid_ns:
        unverified.append({
            "claim":  f"Цитаты [{','.join(str(n) for n in invalid_ns)}] вне диапазона",
            "issue":  f"Доступные источники: {sorted(valid_ns) or 'нет'}",
        })
    yield json.dumps({"type": "verification",
                      "unverified": unverified,
                      "valid_citations": sorted(valid_ns),
                      "checked": True}, ensure_ascii=False)

    # Charts
    for ch in charts:
        yield json.dumps({"type": "chart", "spec": ch}, ensure_ascii=False)

    # 7. Final sources dump
    if sources:
        # Чистим internal field перед отправкой
        # Убираем step_n (internal). Excerpts оставляем — UI использует их
        # для tooltip'ов при наведении на [N] (reproducibility для аудитора:
        # видеть точный фрагмент из источника без открытия URL).
        _internal = {"step_n"}
        clean_sources = []
        for s in sources:
            cs = {k: v for k, v in s.items() if k not in _internal}
            # Capping excerpts на 3×400 chars в payload — больше не нужно UI
            ex = cs.get("excerpts") or []
            cs["excerpts"] = [e[:400] for e in ex[:3]]
            clean_sources.append(cs)
        yield json.dumps({"type": "sources", "sources": clean_sources},
                         ensure_ascii=False)

    yield json.dumps({"type": "done"})
