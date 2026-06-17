# План: модуль «asking» — clarification-воронка перед research

> Цель: пользователи плохо формулируют запросы → инструмент обобщает → плохие
> отзывы. Воронка ДИНАМИЧЕСКИ задаёт 2–4 уточняющих вопроса с кликабельными
> вариантами (+ «другое»), собирает ответы, обогащает промпт. Срабатывает
> ТОЛЬКО если запрос реально неполный; полный — молча пропускается.
> Основано на анализе 4 агентов (точки входа, генерация вопросов, UX, сборка промпта).

## 0. Поток (happy path)

```
send(txt)                                   [app.jsx — перехват]
  │  POST /api/ai/clarify {question, history}
  ▼
generate_clarifications (gemini-3.1-pro)    [ai/clarify.py]
  ├─ complete=true  → сразу streamChat(question)         (воронки НЕТ — скип)
  └─ complete=false → {questions:[...]}
        ▼  рендер ClarifyCard (chips + «другое» + free-text)
        пользователь отвечает / «Пропустить»
        ▼  POST /api/ai/clarify {question, answers}
     build_enriched_question (LLM-rewrite + fallback)     [ai/clarify.py]
        ▼  показать «Уточнённый запрос» (редактируемо)
        ▼  streamChat(enriched_question)  → обычный research (без изменений)
```

## 1. Архитектура — ключевые решения

- **Отдельный синхронный JSON-эндпоинт `POST /api/ai/clarify`** (НЕ inline в SSE `/api/ai/analyze`).
  Причина: analyze отдаёт стрим с контрактом «yielded ⇒ no rollback» ([analyst.py:950](bank_audit_platform/src/bank_audit/ai/analyst.py:950)); clarify — короткий request/response. Смешивать = два протокола на фронте + риск сломать контракт.
- **Новый модуль `ai/clarify.py`** (рядом с `analyst.py`/`llm_utils.py`), переиспользует `LLM_BASE_URL/KEY`, `_patch_client_reasoning_effort`, `deep_reasoning_extra`, `_loose_json_loads`, `detect_bank_slugs`, `normalize_question`.
- **Research-pipeline НЕ меняется**: `stream_deep_research_v2` получает обогащённый текст как обычный `question`. Это идеальный шов — conductor сам разберёт NL-запрос, `detect_bank_slugs` подхватит банки.
- **Флаг `ASKING_ENABLED`** (env, дефолт off на первом релизе) — при off эндпоинт всегда `{complete:true}`, текущий flow нетронут.

## 2. Бэкенд — `ai/clarify.py`

### `generate_clarifications(question, history) -> dict`
Один вызов мощной модели (`LLM_MODEL_REASONING` → gemini-3.1-pro, `deep_reasoning_extra()`, temp 0.0, max_tokens ~2500).

**Контракт ответа (строгий JSON через `_loose_json_loads` + salvage):**
```json
{
  "complete": false,
  "reason": "запрос не уточняет тип перевода и список банков",
  "questions": [
    {"id":"transfer_type","question":"Какие переводы анализируем?",
     "type":"single","allow_other":true,
     "options":[{"value":"outgoing_intl","label":"Исходящие заграничные","hint":"SWIFT/СБП за рубеж"},
                {"value":"incoming","label":"Входящие"},
                {"value":"c2c","label":"Между своими счетами"}]},
    {"id":"banks","question":"Какие банки?","type":"multi","allow_other":true,
     "options":[{"value":"top","label":"Топ-4: Сбер/Альфа/Т-Банк/ВТБ","recommended":true},
                {"value":"sberbank","label":"Сбербанк"}, ...]}
  ]
}
```
- `type`: `single` | `multi` | `text` (для открытых, options=[]).
- `allow_other`: показать поле «другое».
- `recommended`: мягкий дефолт (бейдж «рекоменд.»).

**Промпт-контракт** (`SYSTEM_PROMPT_CLARIFY`) — это в первую очередь **КЛАССИФИКАТОР полноты**, не генератор вопросов:
1. Оцени: хватает ли в запросе данных, чтобы research дал НЕ обобщённый ответ.
2. Достаточно → `complete:true, questions:[]` (НЕ выдумывать вопросы ради вопросов — ключевое требование).
3. Иначе → 2–4 ТОЧЕЧНЫХ вопроса под КОНКРЕТНЫЙ запрос. Явный запрет унификации: «для переводов спрашивай тип перевода; для карт — тип карты/валюту; для ипотеки — сегмент заёмщика».
4. **Few-shot калибровка** (критично против over-asking): пример полного запроса → complete; пример «условия переводов в банках» → вопросы про тип+банки+цель.
5. Опции банков строить из `detect_bank_slugs(question)` + дефолтный топ из `conductor._fallback_plan` — предметно.

**Защита:** max 4 вопроса (обрезать); пустой валидный список → complete=true; невалидный JSON → **fail-open `{complete:true}`** (никогда не блокировать пользователя).

### `build_enriched_question(question, answers) -> str`
Собирает обогащённый промпт. **Основной путь — LLM-rewrite** (та же сильная модель, temp ≤0.2, max_tokens ~800): «Собери из исходного запроса и ответов единый чёткий research-запрос на русском, естественным языком; сохрани названия банков ДОСЛОВНО; ничего не добавляй, только переформулируй».
- **Формат — естественный язык**, не JSON: «Проанализируй ИСХОДЯЩИЕ ЗАГРАНИЧНЫЕ переводы физлиц в Сбербанк, Альфа-Банк, Т-Банк, ВТБ: комиссии, лимиты, сроки, валюты».
- **Fallback** (сбой/таймаут LLM): детерминированный шаблон `original + " (уточнения: ...)"` — некрасиво, но рабоче, с явными названиями банков для `detect_bank_slugs`.
- **Анти-галлюцинация:** банки в enriched ⊆ (банки исходника ∪ ответов) через `detect_bank_slugs` на обоих; расхождение → откат на шаблон.

### Эндпоинт `POST /api/ai/clarify` ([app.py](bank_audit_platform/src/bank_audit/web/app.py) рядом с `/api/ai/analyze`)
- `ClarifyRequest {question:str, history:list=[], answers:list|None=None, deep:bool=False}`.
- `answers is None` → режим генерации: `{complete, questions}`.
- `answers` задан → режим сборки: `{enriched_question, original}`.
- Обычный JSON-ответ (НЕ SSE).

## 3. Фронт — `ClarifyCard` (app.jsx + index.html CSS)

### Поток (перехват `send()` [app.jsx:2187](bank_audit_platform/src/bank_audit/web/static/app.jsx:2187))
Переименовать текущий `send` → `runSend(text)` (рисует bubble + streamChat). Новый `send(txt)`:
1. Рисует user-bubble, ставит `clarifyLoading`.
2. `POST /api/ai/clarify {question, deep}`.
3. `complete:true` или ошибка → `runSend(txt)` (тихий скип, fail-open).
4. `complete:false` → сообщение `role:'clarify'` → рендер `<ClarifyCard>`.
5. Submit → `POST /api/ai/clarify {question, answers}` → enriched → показать «уточнённый запрос» → `runSend(enriched)`.

### Компоненты (один экран, НЕ визард)
- `<ClarifyCard>` — карточка `.surface` с eyebrow «УТОЧНЕНИЕ ЗАПРОСА · N вопросов» + подзаголовок «Ответьте, чтобы отчёт попал точно в цель» + ghost-кнопка «Пропустить».
- `<ClarifyQuestion>` — заголовок + hint + группа чипов.
- `<Chip>` single (radio-семантика) / multi (checkbox) — на базе `.tab`, выбранный `.is-on` (заливка `--accent-soft`, border `--accent`, галочка `Ic.check`).
- `<ChipOther>` «Другое» — пунктирный border, клик раскрывает inline-input.
- `<FreeTextField>` для `type:text`.
- `<ClarifyFooter>` — счётчик «отвечено k/N» + [Пропустить] + [Уточнить и запустить] (акцентная, активна всегда: 0 ответов = запуск исходного).

### CSS-классы (index.html, на существующих oklch-токенах)
`.clarify-card .clarify-head .clarify-q .clarify-q-title .clarify-q-hint .clarify-chips .clarify-chip .clarify-chip.is-on .clarify-chip-box .clarify-other-input .clarify-foot` — editorial-стиль, акцент-терракота, fade-in появление, transition .12s на чипах (без pulse/glow).

## 4. UX-принципы (премиально, не раздражая)
- **Скип обязателен**: «Пропустить» всегда рядом; воронка НЕ блокирующая (анти-паттерн — обязательные поля).
- **Один экран**, все вопросы видны — нет иллюзии бесконечного визарда.
- Eyebrow + микро-подзаголовок объясняют **зачем** («точнее отчёт»), не «заполните форму».
- Мягкий дефолт: `recommended`-вариант с бейджем, не автовыбор.
- Состояния: clarifyLoading (eyebrow + .typing + skel-строки), выбор (.is-on), ошибка → тихий fallback на прямой research.
- Прозрачность: показать «Уточнённый запрос» перед стартом, **с возможностью ручной правки**.
- `prefers-reduced-motion` наследуется глобально.

## 5. Этапы
- **Этап 0** — калибровка промпта: на 10–15 реальных запросах проверить, что полные → complete, расплывчатые → 2–4 точных вопроса (не over-asking).
- **Этап 1** (бэк) — `ai/clarify.py` (generate + build) + эндпоинт `/api/ai/clarify` за флагом `ASKING_ENABLED`. Юнит-проверка JSON-контракта, fail-open.
- **Этап 2** (фронт) — `ClarifyCard` + перехват `send()` + CSS. Скип, сборка, показ enriched.
- **Этап 3** — прозрачность (редактируемый «уточнённый запрос»), `recommended`-дефолты, доступность (rad/checkbox роли, Esc=скип).

## 6. Риски (главные)
1. **Двойная латентность** — clarify добавляет LLM-вызов (3–8с) перед research, +rewrite при сборке. Митигация: короткий промпт/max_tokens, обязательный ClarifyLoading, тумблер; rewrite только при сабмите.
2. **Over-asking** (LLM спрашивает всегда) — убивает UX. Митигация: промпт-классификатор + few-shot + код-лимит max 4, пустой список → complete.
3. **Галлюцинация в rewrite** (добавил банк/параметр) — искажает research. Митигация: «только переформулируй», temp≤0.2, пост-проверка банков ⊆ исходник∪ответы, иначе шаблон.
4. **JSON ненадёжен** (reasoning-leak/fences) — `_loose_json_loads` + salvage + fail-open.
5. **Demo-режим**: `demo_stream` матчит по ключевым словам ([app.py:652](bank_audit_platform/src/bank_audit/web/app.py:652)); переписанный промпт может не сматчиться. Митигация: для demo clarify → complete=true.
6. **history-загрязнение**: `role:'clarify'` сообщения НЕ должны попадать в history как assistant-реплика — фильтровать при сборке history.
7. **Зацикливание**: 1 раунд воронки (на втором заходе всегда complete=true).
