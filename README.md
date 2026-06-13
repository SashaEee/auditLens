# AuditLens

> Deep-research LLM agent + RAG platform for internal audit of Russian retail-banking products — cited reports, charts, and PDF export over a pgvector knowledge base.

**AuditLens** — платформа глубокого исследования (deep research) для внутреннего аудита банковских продуктов. Аудитор задаёт вопрос на естественном языке, а система собирает данные из официальных источников, проверяет факты против цитат и выдаёт структурированный сравнительный отчёт со ссылками `[N]`, графиками и экспортом в PDF.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![pgvector](https://img.shields.io/badge/pgvector-0.3%2B-2C3E50)](https://github.com/pgvector/pgvector)
[![React](https://img.shields.io/badge/React-Babel--standalone-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 📖 Что это

AuditLens помогает аналитику банковского сектора получить ответ на сравнительный вопрос вида «Семейная ипотека: ставки, первый взнос, требования в Сбер, ВТБ, Альфа-банк, ДомРФ» — без ручного обхода десятков сайтов.

Под капотом — единый pipeline с двумя точками входа (веб-интерфейс и CLI) и многопроходным LLM-агентом, который:

- разбирает вопрос, планирует исследование и собирает данные из БД (pgvector) и из веба в реальном времени;
- извлекает факты по каждому банку и **проверяет каждое число против текста цитируемого источника** (анти-галлюцинация);
- синтезирует отчёт в Markdown с цитатами, дорисовывает недостающее в agent-loop и отдаёт результат стримингом;
- строит графики по числовым сравнениям и экспортирует итог в PDF формата A4.

Целевая аудитория — внутренние аудиторы и аналитики, которым нужны отчёты со ссылками на первоисточники, а не «уверенный» текст без подтверждений.

> ⚠️ Проект помечен как **Beta**. Описанное ниже — это то, что реально есть в репозитории; ничего не приукрашено.

---

## ✨ Возможности

- **🔬 Deep Research** — многопроходный pipeline (resolver → planner → executor → fact-extract → claim-verify → synth → critic → agent-loop → merge) для сложных сравнительных вопросов (~1–3 мин).
- **⚡ Quick Mode** — быстрый chat-ответ без deep-research для простых вопросов.
- **🧠 RAG на pgvector** — мультиязычные эмбеддинги BGE-M3 (1024d), markdown-aware чанкинг, семантический поиск по проиндексированным документам.
- **🌐 Web-search с каскадом fallback** — SearXNG (self-hosted) → Brave Search API → DuckDuckGo → Yandex; дедупликация по URL.
- **🛡️ Анти-галлюцинация в 3 слоя** — топикальный фильтр off-topic документов, построчная проверка чисел против цитат, удаление невалидных ссылок `[N]`.
- **⚖️ Trust scoring источников** — взвешивание доменов по классам (регуляторы / госорганы / правовые базы / официальные сайты банков / агрегаторы / блоги); sponsored- и captcha-страницы исключаются из RAG.
- **🏛️ Авто-подтягивание регуляторики** — для социально-регулируемых продуктов (карта ветерана СВО, маткапитал, военная ипотека и т.п.) добавляются шаги по cbr.ru / pravo.gov.ru / mil.ru / gosuslugi.ru.
- **🤖 Сбор данных** — официальные сайты банков через Playwright + httpx (с российским CA-bundle), отзывы и рейтинги с banki.ru / sravni.ru / bankiros.ru, реестр ЦБ.
- **📊 Графики и витрины** — автоматическая визуализация (Chart.js) и SQL-витрины (`v_offer_current`, `v_sber_vs_market`) для структурированных маркет-офферов.
- **📄 PDF-экспорт** — генерация редакционного отчёта A4 через Playwright Chromium.
- **🖥️ Веб-UI + HTTP API** — React-интерфейс (через Babel-standalone) и FastAPI-эндпоинты с SSE-стримингом.
- **🗄️ SCD2-история** — изменения тарифов хранятся версионно (slowly changing dimension).
- **⏰ Оркестрация** — cron-джобы (OpenClaw) для ингеста отзывов и контроля качества данных; опциональные email-алерты.

---

## 🏗️ Архитектура

Один pipeline, две точки входа, несколько слоёв данных. Аудитор задаёт вопрос → FastAPI запускает deep-research → ответ стримится обратно через SSE.

```
👤 Аудитор → React UI → POST /api/ai/analyze (SSE) → FastAPI
                                                         │
        ┌────────────────────────────────────────────────┘
        ▼
  0. Resolver        свободный вопрос → structured JSON (тема, банки, пути, флаги)
  1. Planner         8–16 atomic-шагов; программная инжекция govt/market/review-шагов
  2. Executor        батчи по 4 параллельно; кэш по hash(query+tool); web_search fallback
        │
        ├─ semantic_search  → pgvector (PostgreSQL)
        ├─ web_search       → SearXNG → Brave → DDG → Yandex
        ├─ fetch_official   → Playwright / httpx → Indexer → БД
        ├─ get_market_offers→ SQL-витрины
        └─ topical_reviews  → SQL + JSON-LD
        │
  3. Fact-extract    per-bank параллельно (asyncio.gather), факты с цитатами [N]
  4. Claim-verify    regex-проверка чисел против excerpts источников; не прошедшее отброшено
  5–8. Synth → Critic → Agent-loop (×2) → Final merge   стриминговый markdown
        │
        ├─ Charts gen (Chart.js)
        └─ → UI  ──Download PDF──→  Playwright Chromium → A4 PDF
```

### Слои данных

| Слой | Технология | Что хранит |
|---|---|---|
| **Raw** | файлы `workspace/raw/` | сырые HTML/PDF/JSON-ответы, sha256-индексированы |
| **Document** | таблица `document` | мета: URL, trust_score, fetched_at |
| **Chunks** | `document_chunk` + pgvector | чанки ~512 токенов + эмбеддинги BGE-M3 (1024d) |
| **Структурированный** | `product_offer`, `review`, `bank` | нормализованные офферы/отзывы (SCD2) |
| **Витрины** | `v_offer_current`, `v_sber_vs_market` | SQL-views для быстрых сравнений |

### Trust scoring

Каждому домену присваивается базовый вес с корректировками: регуляторы (cbr.ru, pravo.gov.ru, government.ru, mil.ru) — `0.92–0.98`; госорганы (gosuslugi.ru, fns.gov.ru, rosreestr.ru) — `0.85–0.90`; правовые базы (consultant.ru, garant.ru) — `0.82–0.85`; официальные сайты банков — `~0.95`; агрегаторы (banki.ru, sravni.ru) — `0.65`; неизвестные блоги — `0.30` (cap `0.10` после корректировок). Sponsored / captcha → `0.0` и исключаются из RAG.

Подробнее — в [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## 🧰 Стек

| Слой | Технологии |
|---|---|
| **Язык** | Python 3.11+ |
| **Веб / стриминг** | FastAPI, Uvicorn, SSE (sse-starlette) |
| **БД / RAG** | PostgreSQL 16, pgvector, SQLAlchemy 2.0, psycopg 3, Alembic |
| **Эмбеддинги** | sentence-transformers (BGE-M3, 1024d), PyTorch |
| **LLM-клиенты** | OpenAI-совместимый endpoint (по умолчанию Fireworks AI), Anthropic |
| **Сбор данных** | Playwright (Chromium), httpx, selectolax, tenacity |
| **Web-search** | SearXNG, Brave Search API, ddgs (DuckDuckGo), Yandex |
| **Документы** | парсеры HTML / PDF / XLSX (openpyxl) / PPTX (python-pptx) / JSON-LD |
| **Фронтенд** | React (Babel-standalone), Chart.js, inline-CSS |
| **Утилиты** | Pydantic 2, PyYAML, rapidfuzz, structlog, click, python-dotenv |
| **Инфраструктура** | Docker Compose (PostgreSQL + SearXNG) |

---

## 🚀 Запуск

Нужен **API-ключ LLM** (по умолчанию [Fireworks AI](https://fireworks.ai/), OpenAI-совместимый endpoint) и **Docker** для PostgreSQL + pgvector. Подробный гайд для новичков — [`docs/SETUP.md`](docs/SETUP.md).

### Вариант 1 — автоустановщик (рекомендуется)

```bash
git clone https://github.com/SashaEee/auditLens.git
cd auditLens

# Поднимет Postgres+SearXNG в Docker, создаст .venv, поставит зависимости,
# скачает Playwright Chromium, применит миграции, создаст .env из шаблона.
bash scripts/setup.sh

# Впиши свой LLM_API_KEY в .env (строка LLM_API_KEY=fw_REPLACE_WITH_YOUR_KEY)
nano .env

# Старт сервера
source .venv/bin/activate
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Открой **http://127.0.0.1:8000**, перейди в раздел «ИИ-аналитик», включи **🔬 Deep Research** и задай вопрос.

Полезные подкоманды установщика:

```bash
bash scripts/setup.sh check      # проверить готовность окружения
bash scripts/setup.sh init-db    # только применить миграции
```

### Вариант 2 — вручную (Docker + pip)

```bash
# 1. Инфраструктура: PostgreSQL 16 + pgvector + SearXNG
docker compose up -d
docker compose ps

# 2. Окружение и зависимости
cp .env.example .env              # затем впиши LLM_API_KEY и проверь DATABASE_URL
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
playwright install chromium       # нужен для PDF-экспорта и fetch'а

# 3. Миграции и витрины
bash scripts/setup.sh init-db
# (или вручную: psql "$DSN" -f migrations/*.sql; psql "$DSN" -f src/bank_audit/analytics/views.sql)

# 4. Запуск
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Альтернатива — CLI-команда `serve` (то же самое через click-обёртку):

```bash
python -m bank_audit.cli serve --host 127.0.0.1 --port 8000
# либо после установки пакета:
auditlens serve
```

### CLI

```bash
python -m bank_audit.cli --help          # список команд
python -m bank_audit.cli list-sources    # доступные источники из config/sources.yaml
python -m bank_audit.cli ingest --source <key>   # запустить ингест источника
python -m bank_audit.cli quality         # прогнать data-quality чеки

python scripts/demo_seed.py              # загрузить демо-данные
```

### HTTP API

FastAPI-приложение (`title="Bank Audit Platform"`) отдаёт UI на `/` и набор JSON/SSE-эндпоинтов. Основные:

| Endpoint | Метод | Назначение |
|---|---|---|
| `/api/ai/analyze` | POST (SSE) | стриминг deep-research / quick-ответа |
| `/api/ai/export-pdf` | POST | сгенерировать PDF из markdown + sources |
| `/api/rag/ingest-url` | POST | ингест произвольного URL в БД |
| `/api/rag/semantic-search` | POST | семантический поиск по pgvector |
| `/api/rag/bootstrap-bank/{slug}` | POST | первичный seed данных по банку |
| `/api/banks` | GET | список банков в БД |
| `/api/market` | GET | маркет-офферы (витрины) |
| `/api/reviews/list` | GET | отзывы клиентов |
| `/api/quality` | GET | результаты data-quality чеков |

Пример SSE-запроса:

```bash
curl -N -X POST http://127.0.0.1:8000/api/ai/analyze \
  -H "Content-Type: application/json" \
  -d '{"question":"Сравни вклады Сбера и ВТБ","deep":true}'
```

> Примечание: в коде Swagger UI отключён (`docs_url=None`), поэтому `/docs` недоступен — список роутов смотри в `src/bank_audit/web/app.py`.

---

## 📁 Структура проекта

```
auditLens/
├── src/bank_audit/
│   ├── ai/
│   │   ├── deep_research.py      # главный multi-pass pipeline
│   │   ├── query_resolver.py     # Stage 0: вопрос → structured JSON
│   │   ├── analyst.py            # быстрый chat-режим
│   │   └── outline_planner.py    # структура отчёта
│   ├── rag/
│   │   ├── embedder.py           # BGE-M3 singleton
│   │   ├── chunker.py            # markdown-aware чанкинг
│   │   ├── indexer.py            # URL → document_chunk
│   │   ├── retriever.py          # pgvector search
│   │   ├── fetcher.py            # httpx + российский CA-bundle
│   │   ├── web_search.py         # SearXNG / Brave / DDG / Yandex
│   │   ├── trust.py              # trust scoring доменов
│   │   ├── cache.py              # кэш результатов tool'ов (TTL=1ч)
│   │   └── parsers/              # HTML / PDF / XLSX / PPTX парсеры
│   ├── research/                 # planner, fact-extractor, matrix/narrative-генераторы
│   ├── collectors/               # Playwright browser-коллекторы
│   ├── sources/                  # banki.ru, sravni.ru, bankiros, реестр ЦБ
│   ├── normalizer/               # классификация отзывов → темы, нормализация офферов
│   ├── notifier/                 # email-алерты
│   ├── orchestrator/             # запуск ingest-источников / cron
│   ├── quality/                  # data-quality чеки
│   ├── analytics/views.sql       # SQL-витрины
│   ├── web/
│   │   ├── app.py                # FastAPI-сервер
│   │   ├── pdf_export.py         # Playwright → A4 PDF
│   │   ├── demo_stream.py        # демо-стриминг
│   │   └── static/               # index.html + app.jsx (React)
│   ├── cli.py                    # click-CLI (ingest / quality / serve / list-sources)
│   ├── config.py                 # загрузка settings + auto-expand review-targets
│   ├── db.py · models.py         # доступ к БД и модели
├── migrations/                   # 001–009 SQL-миграции (схема + RAG + whitelist)
├── config/                       # settings.yaml, sources.yaml, CA-bundles
├── docker/                       # postgres init + searxng config
├── openclaw/                     # cron-джобы, агенты, allowlist инструментов
├── scripts/                      # setup.sh, demo_seed.py, тесты компонентов
├── docs/                         # SETUP / USAGE / ARCHITECTURE / API_KEYS / TROUBLESHOOTING
├── demo/responses/               # сохранённые примеры отчётов
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## ⚙️ Конфигурация

Все настройки — через `.env` (шаблон в [`.env.example`](.env.example)). Ключевые переменные:

| Переменная | Назначение |
|---|---|
| `DATABASE_URL` | DSN PostgreSQL с pgvector (обязательно) |
| `LLM_MODE`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL_NAME` | OpenAI-совместимый LLM (обязательно) |
| `LLM_MODEL_FAST`, `LLM_MODEL_SMART` | гибридная схема: быстрая модель для рутины, «умная» для синтеза |
| `SEARXNG_URL`, `BRAVE_SEARCH_API_KEY` | web-search backends (опционально, есть fallback) |
| `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `EMBEDDING_MAX_TOKENS` | модель эмбеддингов (по умолчанию BGE-M3, 1024d) |
| `OPENCLAW_BROWSER_PROFILE` | профиль браузера для обхода Cloudflare/captcha (опционально) |
| `SMTP_*`, `ALERTS_*` | email-алерты (опционально; без настройки — тихий пропуск) |

Параметры HTTP/браузера/нормализатора/качества — в [`config/settings.yaml`](config/settings.yaml); описание источников — в [`config/sources.yaml`](config/sources.yaml).

Подробно о ключах и LLM-провайдерах — [`docs/API_KEYS.md`](docs/API_KEYS.md).

---

## 📚 Документация

- [`docs/SETUP.md`](docs/SETUP.md) — пошаговая установка (в т.ч. для пользователей без опыта разработки и без Docker)
- [`docs/USAGE.md`](docs/USAGE.md) — примеры вопросов, режимы работы, что входит в отчёт
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — устройство pipeline, слои данных, trust scoring
- [`docs/API_KEYS.md`](docs/API_KEYS.md) — получение и настройка ключей LLM
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — типовые проблемы и решения

---

## 📄 Лицензия

Распространяется под лицензией **MIT** — см. [`LICENSE`](LICENSE).
