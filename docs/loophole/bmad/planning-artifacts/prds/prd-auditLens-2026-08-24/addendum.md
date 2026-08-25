# Addendum: технические детали реализации

Этот документ хранит детали решения, отвергнутые альтернативы и материалы, которые не входят в основное повествование PRD, но нужны downstream-командам (архитектура, разработка).

## Исходные материалы

- Исходный план рефакторинга: `F:/auditLens/_bmad-output/planning-artifacts/loophole-agent-refactor-plan.md`
- Текущий монолит: `src/bank_audit/loophole/chat/tools_nanobot.py`
- Промпт с описанием лазеек: `src/bank_audit/loophole/chat/prompt/09_loopholes_prompt.md`
- Текущий механизм выбора модели: `src/bank_audit/loophole/classify.py` (`LoopholeSettings.load().effective_classify_model()`)

## Решения, вынесенные из PRD

- **Модель classifier'а** — задаётся через `LoopholeSettings`/`config.json`; дефолт — DeepSeek-V4-Flash. Fallback-модель задаётся там же; если fallback пуст, при недоступности возвращается ошибка с пояснением.
- **Схема записи `loophole_record`** — используется существующая таблица; новые колонки в v1 не добавляются.
- **Аудит-лог** — предполагается отдельная таблица `agent_audit_log`; окончательное решение за архитектором.

## Открытые технические вопросы

- Batch-size для classifier: 50–100 записей за вызов (корректируется по лимитам контекста модели).
- Критерий снятия compatibility shim `tools_nanobot.py`: успешное прохождение SM-2 + SM-4.1.
