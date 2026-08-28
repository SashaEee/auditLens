# AGENTS.md — AuditLens

Файл для AI-агентов и разработчиков, работающих с этим репозиторием.
Писать и комментировать код/документацию — на русском (так ведётся весь проект).

## 1. Что это за проект

**AuditLens** — Deep-Research платформа для внутреннего аудита банковских продуктов
(закрытый корпоративный контур, боевых данных нет). Аудитор задаёт вопрос на русском
языке — получает аналитический отчёт со ссылками `[N]` на первоисточники, графиками
и PDF-экспортом за 1–3 минуты.

**Главный архитектурный принцип:** числа считает детерминированный код, LLM только
формулирует. Графики и сравнительные таблицы строятся из `bundle.facts` без LLM;
Critic регуляркой сверяет каждое число в тексте с фактами (допуск 2%); запрещено
извлекать числа из SERP-сниппетов — только из прочитанной страницы. Любые изменения
не должны нарушать этот принцип.

Детальная документация: `docs/ARCHITECTURE.md`, `docs/ONBOARDING.md`,
`docs/SETUP.md`, `docs/USAGE.md`, `docs/DEPLOY_UVA.md`, `docs/TROUBLESHOOTING.md`.

## 2. Стек

- **Backend:** Python 3.11+, FastAPI + uvicorn, SQLAlchemy 2 + psycopg3, SSE
  (`sse-starlette`), structlog, Pydantic 2.
- **БД:** PostgreSQL + pgvector (локально pg16 в Docker, на проде managed PG 17.5),
  эмбеддинги BGE-M3 1024d (HNSW). Alembic в зависимостях есть, но миграции —
  это плоские SQL-файлы в `migrations/` (см. §6).
- **Frontend:** React 18 **без сборки и без node** — `index.html` + `app.jsx`,
  Babel-standalone транспилирует JSX прямо в браузере. Графики — Chart.js.
- **LLM:** любой OpenAI-совместимый эндпоинт (прод — Foundation Models Cloud.ru).
  Пять тиров моделей: FAST / SMART / REASONING / ANALYST / INSIGHT + деградационная
  цепочка `явный аргумент → спец-env → SMART/FAST → LLM_MODEL_NAME → хардкод`
  (реестр — `src/bank_audit/ai/analyst.py`, `_tier_models()`). Смена модели =
  env + рестарт, без правки кода.
- **Поиск:** SearXNG (self-hosted, в контуре Cloud.ru живы только `bing`+`dogpile`),
  fallback — ddgs.
- **Скрейпинг / PDF:** Playwright Chromium + playwright-stealth; httpx + selectolax
  для простых страниц; pdfplumber/pdfminer для PDF-документов.
- **Агент «Лазейки»:** nanobot-ai (отдельный harness, модель `openai/gpt-4.1` —
  Gemini валит tool-схемы, см. `docs/ARCHITECTURE.md` §4.4).

## 3. Структура кода

Python-пакет в `src`-layout, устанавливается как `auditlens` (модуль `bank_audit`):

```
src/bank_audit/
├── cli.py                  # CLI `auditlens` (serve / ingest / enrich / quality / list_sources)
├── config.py               # Settings (БД, workspace; LLM-полей НЕТ — модели через os.getenv)
├── db.py                   # SQLAlchemy-сессии
├── web/                    # FastAPI-приложение
│   ├── app.py              #   все REST/SSE-эндпоинты (+/healthz, /readyz — строго ДО catch-all SPA)
│   ├── pdf_export.py       #   серверный HTML→PDF через Playwright
│   └── static/             #   ВЕСЬ фронт: index.html (разметка+все CSS), app.jsx (всё SPA)
├── research/v2/            # Deep Research v2: conductor.py, base_agent.py, analyst.py,
│   │                       #   critic.py, knowledge_bundle.py, numbers.py, chart_designer.py
│   ├── agents/             #   researcher, regulatory, market, reviews, ranking
│   └── tools/              #   tool_specs, web_tools, source_registry_helper
├── research/               # legacy-пайплайн v1 (orchestrator, query_planner, gap_filler…)
├── ai/                     # analyst.py (быстрый ответ), clarify.py, llm_utils.py (тиры, reasoning_effort)
├── rag/                    # embedder, indexer, retriever, crawler, trust.py, отзывы (review_topics и пр.)
├── digest/                 # «Обзор» — ежедневный брифинг (детерминированный SQL + 3 LLM-вызова/сутки)
├── loophole/               # «Лазейки» — nanobot-агент: chat/, parsers/, kb/, pii_mask.py, repository.py
├── sources/ collectors/    # адаптеры источников (banki/sravni/cbr/finuslugi…), browser.py/http.py
├── normalizer/ quality/    # нормализация офферов (SCD2), data-quality чеки
├── analytics/              # views.sql (аналитические вью, CREATE OR REPLACE)
├── storage/ orchestrator/ notifier/  # хранилище, раннер ingest'а, email-алерты
└── categories.py models.py clock.py hashing.py logging_setup.py
```

Прочее:

- `migrations/` — SQL-миграции `NNN_*.sql` (001–041) + `ensure_vector.sql`.
- `config/` — `settings.yaml`, `sources.yaml` (реестр источников), CA-бандлы.
- `openclaw/` — yaml-описания агентов/cron-джобов парсеров.
- `scripts/` — `setup.sh`/`setup.ps1`, демо-сидинг, вспомогательные `_test_*.py`/`_*.py`.
- `deploy/hermes-al/` — конфигурация развёртывания агента Hermes.
- `workspace/` — рантайм-артефакты (raw-выгрузки, отчёты), не код.
- `.worktrees/`, `_bmad/`, `docs/loophole/bmad/` — рабочие ветки и BMAD-артефакты
  планирования; в код не ходят.
- `demo/responses/`, `scripts/golden_*` — демо и golden-run для проверки качества.

## 4. Команды

Установка с нуля — `bash scripts/setup.sh` (venv, deps с `[local-embeddings]`,
docker compose, миграции). Ручной вариант:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'                # + '.[local-embeddings]' если нужен torch локально
playwright install chromium
docker compose up -d                   # Postgres (порт 5434) + SearXNG (порт 8888)
bash scripts/setup.sh init-db          # миграции
cp .env.example .env                   # заполнить DATABASE_URL, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_*
```

Запуск и проверки:

```bash
auditlens serve --reload               # или: uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
pytest                                 # тесты
ruff check src                         # линт (line-length 100, target py311)
mypy src                               # типы (опционально)
auditlens ingest --source <ключ>       # прогон парсера из config/sources.yaml
auditlens quality                      # data-quality чеки
```

Замечания:

- **Windows:** Bash — Git Bash; в venv активировать `source .venv/Scripts/activate`.
- Postgres из `docker-compose.yml` слушает хостовый порт **5434** (не 5432) —
  в `.env` это `localhost:5434`, в `.env.example` стоит 5432 (не сработает с compose).
- Пакет ставится editable (`pip install -e .`): правки `src/` и статики видны сразу.
- `uv.lock` в репо есть, но основной путь установки — pip (Dockerfile использует pip).

## 5. Тесты

- `pytest` из корня; тесты лежат в `tests/` (`test_smoke.py`, `test_digest.py`,
  `test_review_themes.py`) и `tests/loophole/` (~35 файлов, покрывают весь модуль «Лазеек»).
- **Тесты работают без сети и без реальной БД:** `tests/loophole/conftest.py`
  выставляет env-дефолты и поднимает in-memory SQLite; LLM/web_search/fetch мокаются.
  Тесты Postgres-специфики (JSONB/TEXT[]) проверяют структуру миграции текстом, не выполняя её.
- Парсеры тестируются на HTML-фикстурах (`tests/fixtures/`, встроенные строки).
- При добавлении функциональности — добавлять тесты в том же стиле (без сети/БД).
- `scripts/_test_*.py` и `scripts/_*.py` — ad-hoc проверочные скрипты, под pytest не попадают.

## 6. Миграции БД

- Плоские SQL-файлы `migrations/NNN_*.sql`, накатываются **по одному разу** через журнал
  `schema_migrations` (см. `docker/entrypoint.sh`, режим `migrate`); миграции идемпотентны.
- Новая миграция = новый файл со следующим номером; `ON_ERROR_STOP=1` при накате.
- `ensure_vector.sql` НЕ журналируется и применяется **каждый** migrate: до-создаёт
  `document_chunk.embedding vector(1024)` + HNSW, когда суперюзер поставит pgvector.
  `CREATE EXTENSION vector` в `005_rag_foundation.sql` обёрнут в
  `DO $$ … EXCEPTION WHEN insufficient_privilege` — роль приложения в managed-PG не суперюзер.
- После миграций применяется `src/bank_audit/analytics/views.sql` (CREATE OR REPLACE).
- Режим `serve` миграции не запускает (только при `RUN_MIGRATIONS_ON_START=1`).
- На проде миграции накатываются руками (`docker cp` + `psql`), журнала нет —
  см. честный разрыв в `docs/ARCHITECTURE.md` §3.4.

## 7. Соглашения по коду

- Язык комментариев, докстрингов, коммитов и документации — **русский**.
- Ruff: line-length 100, Python 3.11+; `.editorconfig`: UTF-8, LF, 4 пробела,
  final newline, trim trailing whitespace.
- `from __future__ import annotations` в начале модулей — распространённый паттерн.
- Логирование — через `logging_setup.setup()` / structlog, не `print`.
- LLM-вызовы — только через существующие утилиты (`ai/llm_utils.py`, тиринг моделей),
  не создавать новых клиентов. Централизованного конфига моделей нет — модели
  резолвятся через `os.getenv` в точках вызова (де-факто реестр — `ai/analyst.py`).
- Зависимости: не добавлять без необходимости. Известные грабли — `nanobot-ai`
  конфликтует с langgraph по websockets (<16 vs >=16), поэтому стоит
  `langchain-openai` без мета-пакета `langchain`; torch/sentence-transformers —
  только в extra `local-embeddings`, в прод-образ их не тянуть (~2.5 ГБ).
- Коммиты: короткое описание по-русски (префиксы `docs:`/`chore:` встречаются),
  **без `Co-Authored-By`-трейлеров**; фича-ветки → PR в `main`.
- Фронт — только `index.html` + `app.jsx`, **никакой сборки/node**: правишь файл,
  обновляешь страницу; ошибки парсинга JSX смотреть в консоли DevTools.

## 8. Деплой

- Прод: один контейнер `auditlens-app` на ВМ в Cloud.ru (`ecs-oarb`,
  `--network host`, порт 8000, `--env-file ~/auditlens/.env`). TLS и аутентификация
  терминируются на внешнем nginx + SSO (OIDC) — приложение за периметром.
- **Флаг `--init` ОБЯЗАТЕЛЕН** (tini реапит зомби-процессы Chromium от Playwright).
- Dockerfile: editable install, Playwright в отдельном слое, non-root user `appuser`,
  entrypoint `serve` (default) / `migrate`. Healthcheck — `/healthz` (без БД), БД — `/readyz`.
- Итеративный hot-patch статики: `tar czf - app.jsx index.html | ssh … 'docker cp …'`
  (scp/rsync на сервере сломаны). **Hot-patch ≠ durable** — слетает при рестарте,
  для постоянства нужен ребилд образа (полная процедура — `docs/ONBOARDING.md` §6,
  `docs/DEPLOY_UVA.md`).
- Режим `serve` БД не трогает; миграции на проде — отдельно и руками.

## 9. Безопасность

- Секреты — только в `.env` (в `.gitignore`) и `~/auditlens/.env` на сервере;
  **никогда** не коммитить и не класть в образ. Шаблоны: `.env.example`, `.env.prod.example`.
- В прод-контуре дефолт — внутренняя модель `openai/gpt-oss-120b`; внешние модели
  (gemini/claude/gpt) включаются по согласованию с командой инфраструктуры сменой
  5 строк env (см. «рубильник комплаенса», `docs/ARCHITECTURE.md` §4.3).
- Модуль «Лазейки» маскирует ПД перед отправкой в LLM (`loophole/pii_mask.py`) —
  не обходить это.
- Источники с trust-классами: регуляторы 0.98, банки 0.95, агрегаторы 0.65, блоги исключаются.
- HTTP-клиенту (httpx) CA-бандл передаётся явно (`verify=CA_BUNDLE_PATH`,
  `config/ca_bundle_combined.pem` — certifi + Russian Trusted Root).

## 10. Грабли, о которые уже споткнулись

- OpenAI-совместимый эндпоинт ≠ OpenAI-совместимое поведение: Gemini валит
  tool-схемы nanobot-ai → у «Лазеек» отдельная модель `LOOPHOLE_NANOBOT_MODEL=openai/gpt-4.1`.
- `pgvector/pgvector:pg16` не имеет локали `ru_RU.UTF-8` — в compose стоит `C.UTF-8`,
  не менять.
- `/healthz` и `/readyz` должны регистрироваться строго до catch-all `/{full_path:path}`
  в `web/app.py` — иначе SPA-фоллбэк отдаст 200+HTML на health-пробу.
- `reasoning_effort` подмешивается monkey-patch'ем клиента (`_patch_client_reasoning_effort`
  в `ai/llm_utils.py`): глобально `low`, точечно `medium` через `deep_reasoning_extra()`.
- Директория `.worktrees/` содержит копии кода рабочих веток — не путать с основным `src/`.
