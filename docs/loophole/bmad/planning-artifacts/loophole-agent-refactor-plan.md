# План рефакторинга агента лазеек

## 1. Текущее состояние

Агент лазеек — это nanobot-агент в `src/bank_audit/loophole/chat/`. Все его умения сейчас спрятаны в одном файле `chat/tools_nanobot.py` и в промптах `chat/prompt/*.md`:

- **Поиск в интернете** — `audit_web_search`, `audit_web_fetch`, `audit_extract_loopholes`.
- **Сохранение лазеек** — `audit_save_loophole` → таблица `loophole_record`.
- **Работа с БД** — `audit_db_query` (READ-ONLY SELECT) и `audit_table_load`.
- **Создание парсеров** — `POST /api/loophole/parsers`, генерация Scrapy+Playwright-паука в `parsers/generator.py`.
- **Telegram** — распознаётся как target (`t.me/...`, `@handle`) и нормализуется в `parsers/dedup.py`.
- **Экспорт** — JSON/CSV реализованы в `web.py`; PDF реализован в `pdf_export.py`, но отключён (возвращает 501); XLSX отсутствует.
- **Self-heal парсеров** — отдельные heal-tools `fetch_target`, `patch_parser`.

Проблемы:
1. `tools_nanobot.py` разросся: смешаны web, db, export и heal инструменты.
2. Нет явного разделения на «умения» — сложно добавлять новые capability.
3. PDF/XLSX-отчёты не доступны агенту.
4. Нет единого места для конфигурации агента и ограничения итераций.
5. Умения не оформлены в виде переиспользуемых skills по [спецификации Agent Skills](https://agentskills.io/specification).

## 2. Цель

Создать полноценного агента лазеек в директории `src/bank_audit/loophole/agent/`:

1. **Агентский цикл** — любую задачу агент решает через цикл "мышление → вызов инструмента → наблюдение → ... → финальный ответ".
2. **Ограничение итераций** — в цикле не более 20 итераций, значение конфигурируется через `config.json` в директории агента.
3. **Навыки по спецификации** — каждый skill — отдельная директория с `SKILL.md`, опциональными `scripts/`, `references/`, `assets/`.

Агент включает **5 самостоятельных skills** внутри `src/bank_audit/loophole/agent/skills/`:

1. `web-search` — поиск лазеек в интернете, извлечение и сохранение.
2. `parser-creator` — создание и управление парсерами источников.
3. `telegram-parser` — специфика создания парсеров Telegram-каналов.
4. `db` — READ-ONLY SQL-запросы и загрузка таблиц из БД лазеек.
5. `reports` — формирование отчётов XLSX/PDF.

Каждый skill — изолированный пакет по [спецификации Agent Skills](https://agentskills.io/specification): `SKILL.md` с YAML frontmatter (`name`, `description`, `compatibility`, `metadata`) и инструкциями в Markdown, Tool-обёртки в `scripts/tools.py`, сервисная логика в `scripts/service.py`, вспомогательные материалы в `references/`.

Агент в `agent/agent.py` (или адаптированный `chat/graph.py`) регистрирует нужные skills через `SkillRegistry`, читает `config.json` и управляет циклом с заданным лимитом итераций.

## 3. Brief агента лазеек

> Агент лазеек — это автономный помощник для поиска, анализа и сохранения информации о лазейках в банковских продуктах.
>
> **Принцип работы:** любую задачу агент решает через **агентский цикл** (ReAct): на каждом шаге модель либо выдаёт финальный ответ, либо задаёт уточняющий вопрос при недостаточности информации, либо выбирает инструмент, получает результат и продолжает рассуждение.
>
> **Ограничение итераций:** в одном запуске цикла допускается **не более 20 итераций**. Лимит задаётся в `config.json` в директории агента (`src/bank_audit/loophole/agent/config.json`) и может быть переопределён через переменную окружения `LOOPHOLE_AGENT_MAX_ITERATIONS`.
>
> **Уточняющие вопросы:** если запрос пользователя не содержит достаточно контекста (не указан банк, период, формат отчёта, источник или другой обязательный параметр), агент прерывает выполнение и возвращает список уточняющих вопросов. Цикл продолжается только после получения ответов.
>
> **Доступные умения:** поиск в интернете, работа с БД лазеек (READ-ONLY), создание парсеров источников, создание Telegram-парсеров, формирование отчётов XLSX/PDF.
>
> **Выходной контракт:** агент возвращает структурированный ответ пользователю, список использованных инструментов и, при необходимости, записи `loophole_record`.

## 4. Предлагаемая архитектура

```
src/bank_audit/loophole/
├── agent/
│   ├── config.json              # конфигурация агента: max_iterations, model, skills
│   ├── agent.py                 # оркестратор агентского цикла
│   ├── registry.py              # SkillRegistry: загрузка skills, сбор tools/prompts
│   ├── __init__.py
│   └── skills/                  # директория skills по спецификации agentskills.io
│       ├── web-search/
│       │   ├── SKILL.md         # metadata + инструкция skill'у
│       │   ├── scripts/
│       │   │   ├── __init__.py
│       │   │   ├── tools.py     # AuditWebSearchTool, AuditWebFetchTool,
│       │   │   │                # AuditExtractLoopholesTool, AuditSaveLoopholeTool
│       │   │   └── service.py   # web_search, web_fetch, extract_loopholes, save_loophole
│       │   └── references/
│       │       └── prompt.md    # дополнительный prompt-фрагмент
│       ├── parser-creator/
│       │   ├── SKILL.md
│       │   ├── scripts/
│       │   │   ├── __init__.py
│       │   │   ├── tools.py     # AuditCreateParserTool, AuditRunParserTool,
│       │   │   │                # AuditParserStatusTool
│       │   │   └── service.py   # create_parser, run_parser, get_parser_status
│       │   └── references/
│       │       └── prompt.md
│       ├── telegram-parser/
│       │   ├── SKILL.md
│       │   ├── scripts/
│       │   │   ├── __init__.py
│       │   │   ├── tools.py     # AuditCreateTelegramParserTool
│       │   │   └── service.py   # normalize_target, create_tg_parser
│       │   └── references/
│       │       └── prompt.md
│       ├── db/
│       │   ├── SKILL.md
│       │   ├── scripts/
│       │   │   ├── __init__.py
│       │   │   ├── tools.py     # AuditDbQueryTool, AuditTableLoadTool
│       │   │   └── service.py   # db_query, table_load + guard
│       │   └── references/
│       │       └── schema.md    # схема таблиц loophole_record, loophole_parser и др.
│       └── reports/
│           ├── SKILL.md
│           ├── scripts/
│           │   ├── __init__.py
│           │   ├── tools.py     # AuditExportXlsxTool, AuditExportPdfTool
│           │   └── service.py   # export_xlsx, export_pdf
│           └── references/
│               └── prompt.md
├── chat/                        # legacy chat-адаптер
│   ├── nanobot_agent.py         # адаптация create_nanobot под SkillRegistry
│   ├── graph.py                 # run_chat/stream_chat используют agent.Agent
│   └── tools_nanobot.py         # deprecated / compatibility shim
└── ... (parsers, web.py и др.)
```

## 5. Контракт `Skill`

Каждый skill соответствует [спецификации Agent Skills](https://agentskills.io/specification):

```python
@dataclass
class Skill:
    name: str
    description: str
    prompt: str
    tools: tuple[type[Tool], ...]
```

`SKILL.md` обязательно содержит:

```yaml
---
name: web-search
description: Поиск лазеек в интернете, загрузка страниц, извлечение и сохранение записей.
metadata:
  version: "1.0"
  author: auditlens
---
```

`SkillRegistry` умеет:
- `register(skill)` / `register_from_path(path)` — загрузка skill из директории.
- `get_tools()` — список всех Tool-классов.
- `get_prompt()` — объединение prompt'ов всех зарегистрированных skills.
- `load_all(skills_dir)` — автоматическое обнаружение всех skills в `agent/skills/`.

## 6. Конфигурация агента (`config.json`)

Файл `src/bank_audit/loophole/agent/config.json`:

```json
{
  "model": "gpt-4o",
  "provider": "openai",
  "temperature": 0.3,
  "max_iterations": 20,
  "skills": ["web-search", "parser-creator", "telegram-parser", "db", "reports"],
  "workspace": "workspace/loophole/agent"
}
```

Приоритет настроек (от высшего к низшему):
1. Аргументы вызова `Agent.run(..., max_iterations=N)`.
2. Переменная окружения `LOOPHOLE_AGENT_MAX_ITERATIONS`.
3. Значение из `config.json`.
4. Значение по умолчанию — `20`.

## 7. Агентский цикл

```
1. Пользовательский запрос → system prompt + prompt'ы зарегистрированных skills.
2. Проверка корректности вопроса:
   a. Если запрос пользователя не относится к поиску лазеек, созданию парсера источника для лазеек, подготовкой аналитики по базе лазеек или формированию отчета по лазейкам, то ответь пользователю только из своих знаний и завершай цикл. 
3. Проверка достаточности информации:
   a. Если запрос неполный или неоднозначный → агент возвращает уточняющие вопросы и ожидает ответа пользователя.
   b. После получения ответов обогащённый запрос подаётся в цикл.
4. Цикл (i = 1 .. max_iterations):
   a. LLM генерирует либо финальный ответ, либо уточняющий вопрос, либо вызов инструмента.
   b. Если финальный ответ — вернуть результат.
   c. Если уточняющий вопрос — вернуть вопрос пользователю и ожидать ответа (итерация не считается использованной).
   d. Если вызов инструмента — выполнить, добавить observation в контекст.
5. Если достигнут max_iterations и ответ не получен — вернуть ответ на основе накопленных observations с пояснением "достигнут лимит итераций".
```

Цикл реализуется в `agent/agent.py`. Для совместимости с текущим nanobot-адаптером возможна адаптация `create_nanobot` — передача `max_iterations` в конфиг nanobot и регистрация tools из `SkillRegistry`.

## 8. Этапы рефакторинга

### Этап 1. Создание инфраструктуры агента (1 день)

1.1. Создать `src/bank_audit/loophole/agent/`:
- `config.json` с `max_iterations: 20`.
- `agent.py` — оркестратор цикла.
- `registry.py` — `Skill` + `SkillRegistry`.
- `__init__.py`.

1.2. Определить `Skill` и `SkillRegistry` с поддержкой `SKILL.md`.

1.3. Создать пустые директории для 5 skills в `agent/skills/`.

### Этап 2. Перенос существующих capability в skills (2–3 дня)

2.1. **web-search skill**:
- Перенести `web_search`, `web_fetch`, `extract_loopholes`, `save_loophole` из `tools_nanobot.py` в `agent/skills/web-search/scripts/service.py`.
- Создать Tool-обёртки в `agent/skills/web-search/scripts/tools.py`.
- Добавить `SKILL.md` и `references/prompt.md` с инструкцией: когда и как использовать поиск, fetch, extract, save.

2.2. **parser-creator skill**:
- Перенести логику вызова `parsers/generator.py`, `parsers/runner.py`, `parsers/registry.py` в `agent/skills/parser-creator/scripts/service.py`.
- Создать tools: `AuditCreateParserTool`, `AuditRunParserTool`, `AuditParserStatusTool`.
- Добавить `SKILL.md` и `references/prompt.md` с примерами запросов.

2.3. **telegram-parser skill**:
- Перенести `extract_targets`, `normalize_target` для TG в `agent/skills/telegram-parser/scripts/service.py`.
- Создать `AuditCreateTelegramParserTool`.
- Добавить `SKILL.md` и `references/prompt.md` с нюансами TG (аутентификация, `t.me/<name>`, env `TG_API_ID`/`TG_API_HASH`).

2.4. **db skill**:
- Перенести `db_query`, `table_load` и READ-ONLY guard в `agent/skills/db/scripts/service.py`.
- Создать `AuditDbQueryTool`, `AuditTableLoadTool`.
- Добавить `SKILL.md` и `references/schema.md` со схемой таблиц `loophole_record`, `loophole_parser`, `loophole_parser_run`, `loophole_keyword`.

2.5. **reports skill**:
- Реализовать `export_xlsx` через `openpyxl` (уже в `pyproject.toml`).
- Подключить `export_pdf` через существующий `loophole/pdf_export.py`.
- Создать `AuditExportXlsxTool`, `AuditExportPdfTool`.
- Обновить `web.py::export` для поддержки `format=xlsx` и `format=pdf`.
- Добавить `SKILL.md` и `references/prompt.md` с параметрами фильтров.

### Этап 3. Адаптация chat-агента (1–2 дня)

3.1. Обновить `nanobot_agent.py::create_nanobot`:
```python
def create_nanobot(*, skills: tuple[Skill, ...] = (), extra_tools=(), max_iterations: int | None = None, ...):
    bot = Nanobot.from_config(...)
    effective_max_iter = max_iterations or _load_agent_config().max_iterations
    for skill in skills:
        for tool_cls in skill.tools:
            bot._loop.tools.register(tool_cls())
    ...
```

3.2. Обновить `build_prompt` — добавлять prompt'ы зарегистрированных skills к system prompt.

3.3. Обновить `graph.py` — в `run_chat`/`stream_chat` использовать `agent.Agent` со `SkillRegistry(ALL_SKILLS)`.

3.4. Удалить или превратить в shim `chat/tools_nanobot.py`.

### Этап 4. Backend-доработки (1 день)

4.1. **XLSX-экспорт** в `web.py::export`:
- Колонки: `record_id, title, url, domain, bank_slug, keyword, trust_score, is_loophole, verdict_confidence, verdict_reason, verdict_model, status, collected_at, classified_at, content_status, raw_text_len, raw_text`.
- Автоширина, фильтр, закрепление заголовка.

4.2. **PDF-экспорт** в `web.py::export`:
- Вызвать `pdf_export.export_pdf(records)` и вернуть `Response(content=pdf, media_type="application/pdf")`.
- Убрать заглушку 501.

4.3. **Унификация фильтров**:
- Добавить в `ExportRequest` поля фильтров (`bank_slugs`, `period_from`, `period_to`, `query_text`, `only_loophole`, `status`).
- Использовать общий сервис `reports.service.export_records` для `/export` и `/export/csv`.

### Этап 5. Тестирование (1–2 дня)

5.1. Регрессионные тесты чат-агента.
5.2. Новые тесты:
- `tests/loophole/agent/test_agent_loop.py` — проверка лимита итераций.
- `tests/loophole/agent/test_registry.py` — загрузка skills из `SKILL.md`.
- `tests/loophole/agent/skills/test_web_search_skill.py`
- `tests/loophole/agent/skills/test_db_skill.py`
- `tests/loophole/agent/skills/test_reports_skill.py`
- `tests/loophole/test_export_xlsx.py`
- `tests/loophole/test_export_pdf.py`
5.3. `ruff check` по затронутым файлам — критерий «нет новых ошибок».

## 9. Изменяемые и создаваемые файлы

### Создаваемые
- `src/bank_audit/loophole/agent/config.json`
- `src/bank_audit/loophole/agent/__init__.py`
- `src/bank_audit/loophole/agent/agent.py`
- `src/bank_audit/loophole/agent/registry.py`
- `src/bank_audit/loophole/agent/skills/web-search/SKILL.md`
- `src/bank_audit/loophole/agent/skills/web-search/scripts/*.py`
- `src/bank_audit/loophole/agent/skills/web-search/references/prompt.md`
- `src/bank_audit/loophole/agent/skills/parser-creator/SKILL.md`
- `src/bank_audit/loophole/agent/skills/parser-creator/scripts/*.py`
- `src/bank_audit/loophole/agent/skills/parser-creator/references/prompt.md`
- `src/bank_audit/loophole/agent/skills/telegram-parser/SKILL.md`
- `src/bank_audit/loophole/agent/skills/telegram-parser/scripts/*.py`
- `src/bank_audit/loophole/agent/skills/telegram-parser/references/prompt.md`
- `src/bank_audit/loophole/agent/skills/db/SKILL.md`
- `src/bank_audit/loophole/agent/skills/db/scripts/*.py`
- `src/bank_audit/loophole/agent/skills/db/references/schema.md`
- `src/bank_audit/loophole/agent/skills/reports/SKILL.md`
- `src/bank_audit/loophole/agent/skills/reports/scripts/*.py`
- `src/bank_audit/loophole/agent/skills/reports/references/prompt.md`
- `tests/loophole/agent/test_agent_loop.py`
- `tests/loophole/agent/test_registry.py`
- `tests/loophole/agent/skills/*.py`
- `tests/loophole/test_export_xlsx.py`
- `tests/loophole/test_export_pdf.py`

### Изменяемые
- `src/bank_audit/loophole/chat/nanobot_agent.py` — регистрация skills, передача max_iterations.
- `src/bank_audit/loophole/chat/graph.py` — использование `agent.Agent` / `SkillRegistry`.
- `src/bank_audit/loophole/chat/tools_nanobot.py` — удалить/превратить в shim.
- `src/bank_audit/loophole/web.py` — XLSX/PDF в `/export`.
- `src/bank_audit/loophole/models.py` — расширить `ExportRequest` фильтрами.

## 10. Риски

- **Playwright для PDF** — требует браузера в контейнере. Митигация: graceful fallback с понятной ошибкой.
- **XLSX больших выборок** — лимит 10 000 записей. Митигация: оставить текущий лимит, в будущем добавить фоновую генерацию.
- **Telegram-парсеры** — требуют аутентификации. Митигация: skill описывает ограничения и env.
- **Агентский цикл и лимит итераций** — слишком маленький лимит может обрезать сложные задачи, слишком большой — тратить токены. Митигация: значение по умолчанию 20 и возможность переопределения через `config.json`/`LOOPHOLE_AGENT_MAX_ITERATIONS`.
- **Миграция с `src/bank_audit/loophole/skills/`** — в рабочей директории обнаружены stale `__pycache__` и пустые поддиректории skills. Митигация: удалить неиспользуемый `skills/` после завершения рефакторинга.

## 11. Критерии готовности

- [ ] Создана директория `src/bank_audit/loophole/agent/` с `config.json`, `agent.py`, `registry.py`.
- [ ] Агент решает задачи через агентский цикл с лимитом итераций (по умолчанию 20, конфигурируется через `config.json`).
- [ ] Созданы 5 skill-пакетов в `agent/skills/`, каждый с валидным `SKILL.md` по [спецификации Agent Skills](https://agentskills.io/specification).
- [ ] Каждый skill предоставляет tools и prompt.
- [ ] `nanobot_agent.py` и `graph.py` используют `SkillRegistry`.
- [ ] `POST /api/loophole/export` поддерживает `xlsx` и `pdf`.
- [ ] Регрессионные тесты модуля loophole проходят.
- [ ] Новые тесты agent-цикла, registry, skills и экспорта проходят.
- [ ] `ruff check` по затронутым файлам не добавляет новых ошибок.
