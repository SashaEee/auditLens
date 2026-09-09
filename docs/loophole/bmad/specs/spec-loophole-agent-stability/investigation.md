# Расследование нестабильности агента «Лазейки»

Диагноз по коду и логам `workspace/auditlens-local-8010-*.stderr.log`. Все ссылки — `src/bank_audit/loophole/`, если не указано иное.

## Нестабильное соединение с cloud.ru

1. **Нет ретраев.** Основная модель: `provider._CHAT_RETRY_DELAYS = (1,)` — одна попытка через 1 с (`agent/__init__.py:252`); `max_retries=0` у всех клиентов (`chat/nanobot_agent.py:139`, `chat/clarify.py:319`, `chat/tools_nanobot.py:291`). В research v2 для того же эндпоинта — щедрые ретраи транзиентов: `src/bank_audit/research/llm_throttle.py:116-121` («cloud.ru роняет соединения под нагрузкой, их НУЖНО ретраить»). В loophole фикс не перенесён. Логи: `LLM transient error (attempt 1/1) ... connection error` → `LLM request failed after 2 retries, giving up`.
2. **Вечное зависание основной модели.** При дефолтных env (`LOOPHOLE_MODEL_TIMEOUT_SECONDS=0`, `LOOPHOLE_AGENT_TIMEOUT_SECONDS=0`) у клиента `timeout=None` (`chat/nanobot_agent.py:129-141`), watchdog отключён (`agent/__init__.py:566-569`). Connect-timeout 10 с задан только дочерним subagent'ам (`chat/subagents.py:246`), основной модели — никогда (`agent/__init__.py:726-743`).
3. **Subagent'ы висят до дедлайна порции.** Внутри 180-с порции `chat()` без HTTP-таймаута (`chat/subagents.py:337-340` + `chat/nanobot_agent.py:146-155`). Логи: серии `loophole_subagent_failed ... stage=classifying code=timeout elapsed_seconds=180.0`.
4. **Маскировка сбоев.** Падение модели → `AGENT_UNAVAILABLE_MESSAGE` / `PARTIAL_STOP_MESSAGES["model_unavailable"]` (`agent/__init__.py:26-36`, `chat/graph.py:490-491`). Clarify fail-closed: один сбой при `timeout=15, retries=0` → пользователь застревает на воронке (`chat/clarify.py:417-427`).

## Пустой поиск лазеек

1. **Усечённый поисковый контур:** только bing+dogpile (`docker/searxng/settings.prod.yml:12-16`); форумная фактура, на которую рассчитан промпт, через них почти не находится. Ошибки поиска/fetch без ретрая → `search_unavailable` / `source_unavailable` (`chat/tools_nanobot.py:755-759, 811-820`).
2. **Фильтр даты публикации отсекает всё:** запрос с месяцем/годом включает `_source_publication_period_error` (`chat/tools_nanobot.py:167-190`); источник без tz-aware `published_at` → `source_publication_date_unverified`. Точный timestamp парсится только из JSON-LD/meta (`sources/adapters/fetch_decorator.py:106-126`) — у форумов его почти нет → все прочитанные источники исключаются (`agent/__init__.py:131, 316-318`).
3. **Subagent-ветка отключена без модели:** без `LOOPHOLE_SUBAGENT_MODEL` и `LLM_MODEL_FAST` → `subagent_model_not_configured` (`chat/subagents.py:187-189`); в `.env.example:33` переменная пустая.
4. **Single-shot LLM-вызовы:** обрыв на любом шаге (clarify → classify → extract) = нет кандидатов. `extract_loopholes`: один сбой (45 с, retries=0) → `extraction_failed`, источник помечается навсегда (`chat/tools_nanobot.py:324-331, 888-893`).
5. **Строгий отбор кандидатов:** `is_loophole is True` + дословное вхождение цитаты в первые 4000 символов excerpt (`agent/__init__.py:122-146`, `chat/tools_nanobot.py:260`) — пограничные находки молча отбрасываются.
6. **Добивают:** стоп по `no_progress` после 3 пустых раундов (`agent/__init__.py:343-349`), лимиты `LOOPHOLE_SEARCH_LIMIT=12` / `LOOPHOLE_FETCH_LIMIT=12`; protocol-guard обнуляет ответ при служебном протоколе модели (`chat/hooks.py:134-150`).

## Маппинг на capabilities

- CAP-1 ← п.1 раздела «Нестабильное соединение».
- CAP-2 ← п.2.
- CAP-3 ← п.3.
- CAP-4 ← п.2 раздела «Пустой поиск».
- CAP-5 ← п.3.
- CAP-6 ← п.1 и п.4 раздела «Пустой поиск» (ретраи инструментов).
- Non-goal «поисковый контур» ← п.1 раздела «Пустой поиск» (требует инфраструктурного решения).
- Non-goal «отбор кандидатов» ← п.5 раздела «Пустой поиск».
