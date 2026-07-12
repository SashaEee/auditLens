# Онбординг разработчика — AuditLens

Короткий практический гайд: где код, как запустить локально, где тестировать, как
деплоить на сервер ОАИТ. Для деталей по конкретным темам — ссылки на другие доки в конце.

> **TL;DR.** AuditLens — Deep-Research платформа для внутреннего аудита банковских
> продуктов: FastAPI-бэкенд + RAG/LLM-агент + React-фронт (без сборки, Babel в браузере)
> + PostgreSQL/pgvector. Локально: подними БД через `docker compose`, поставь пакет
> `pip install -e .`, запусти `auditlens serve --reload`, открой `http://127.0.0.1:8000`.
> Прод крутится в Docker-контейнере на VM `ecs-oarb` в Облаке УВА.

---

## 1. Git

- **Репозиторий:** https://github.com/SashaEee/auditLens
- **Основная ветка:** `main` (деплой идёт с неё).
- Клон:
  ```bash
  git clone https://github.com/SashaEee/auditLens.git
  cd auditLens
  ```
- Работаем фича-ветками → PR в `main`. В коммиты **не** добавляем `Co-Authored-By`-трейлеры.

---

## 2. Структура репозитория

```
auditLens/
├── src/bank_audit/            # весь Python-пакет (устанавливается как `auditlens`)
│   ├── web/                   # ← ВЕБ-СЛОЙ
│   │   ├── app.py             #   FastAPI-приложение, все REST/SSE-эндпоинты
│   │   ├── pdf_export.py      #   серверный HTML→PDF (Chromium/Playwright)
│   │   └── static/            #   ФРОНТЕНД (см. §5)
│   │       ├── index.html     #     разметка + ВСЕ стили (один <style>)
│   │       ├── app.jsx        #     всё SPA-приложение (React, один файл)
│   │       ├── favicon.svg / auditlens-icon.svg
│   ├── ai/                    # analyst.py (быстрый ответ), clarify.py (уточнения), llm_utils.py
│   ├── research/             # пайплайн Deep Research: orchestrator, query_planner,
│   │                          #   source_finder, gap_filler, matrix_builder, narrative_*…
│   ├── rag/                   # индексация/семантический поиск (pgvector)
│   ├── collectors/           # сбор данных с сайтов банков (Playwright)
│   ├── normalizer/ quality/ analytics/ storage/ orchestrator/ sources/ notifier/
│   └── cli.py                 # CLI `auditlens` (serve, ingest, …)
├── migrations/                # SQL-миграции + ensure_vector.sql
├── config/                    # settings.yaml, sources.yaml, CA-сертификаты
├── docker/                    # entrypoint.sh, searxng-конфиг
├── Dockerfile                 # прод-образ
├── docker-compose.yml         # ЛОКАЛЬНАЯ инфра (Postgres+pgvector, SearXNG)
├── docker-compose.prod.yml    # справочный прод-compose
├── pyproject.toml             # зависимости, console-script `auditlens`
├── .env.example               # шаблон локального окружения
├── .env.prod.example          # шаблон прод-окружения
└── docs/                      # документация (см. §9)
```

Бэкенд — обычный Python-пакет (`src`-layout). Фронт — статика, отдаётся FastAPI как есть,
**сборки нет**.

---

## 3. Стек

- **Backend:** Python 3.11+, FastAPI + uvicorn, SSE для стрима ответов ИИ.
- **Frontend:** React 18 через Babel-standalone прямо в браузере (`app.jsx` транспилируется
  на лету). Сборщика/ноды нет — правишь файл, обновляешь страницу.
- **БД:** PostgreSQL + расширение `pgvector` (эмбеддинги/семантический поиск).
- **LLM:** OpenAI-совместимый API (в проде — Foundation Models Облака УВА). Эмбеддинги
  в проде через API (`EMBEDDING_MODE=api`), torch не нужен.
- **Поиск:** SearXNG (self-hosted) + ddgs как fallback.
- **PDF/рендер:** Playwright Chromium (экспорт отчёта в PDF, рендер SPA-сайтов банков).

---

## 4. Локальный запуск

**4.1. Зависимости**
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'              # + '.[local-embeddings]' если нужен torch локально
playwright install --with-deps chromium
```

**4.2. Инфраструктура (БД + поиск) — Docker**
```bash
docker compose up -d postgres searxng
# postgres → localhost:5432 (pgvector/pgvector:pg16)
# searxng  → localhost:8888
```

**4.3. Окружение**
```bash
cp .env.example .env
# заполнить как минимум: DATABASE_URL, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL_*, SEARXNG_URL
```
Ключи и значения — см. [docs/API_KEYS.md](API_KEYS.md) и [docs/SETUP.md](SETUP.md).
Боевые секреты в git/образ НЕ коммитим (`.env` в `.gitignore`).

**4.4. Миграции и запуск**
```bash
# применить миграции (см. docs/SETUP.md — там же сидинг демо-данных)
auditlens serve --reload          # → http://127.0.0.1:8000  (хост/порт меняются флагами)
```
Вкладка «ИИ-аналитик» — пятый пункт левого меню (`/#ai`).

---

## 5. Фронтенд: где что (вкладка «ИИ-аналитик»)

Весь фронт — **два файла**, оба в `src/bank_audit/web/static/`:

- **`index.html`** — вся разметка-обёртка и **все CSS** (один большой `<style>`).
  Дизайн-токены (`--paper/--ink/--hair/--accent/--surface`, шрифты Geist / JetBrains Mono /
  Source Serif 4 / Instrument Serif) объявлены сверху, тема `light`/`dark`.
- **`app.jsx`** — всё SPA одним файлом. Ключевые компоненты вкладки ИИ-аналитика:
  - `AIPage` — корневой компонент чата (state ленты `msgs`, отправка, флаги оболочки).
  - `AiWelcome` — стартовый экран (приветствие + карточки-подсказки `QUICK`).
  - `DeepConsole` — **live-консоль Deep Research**: timeline фаз, «размышления модели»
    (через `ThinkingPanel`), карточки агентов, прогресс, таймер. Карта фаз — `DEEP_FLOW`,
    привязка reasoning-стадий — `PHASE_REASON` / `STAGE_PHASE`.
  - `ResearchSummary` — свод над готовым отчётом.
  - `ClarifyCard` — карточка уточняющих вопросов.
  - отчёт: `renderMD` (markdown→HTML с цитатами), `DocTocSlot`, `SourcesRail`,
    `RankingWidget`, `InsightsWidget`, `VerificationBanner`, `ChartCanvas`, `PdfExportButton`.

**Никакой сборки нет:** правишь `.jsx`/`.html` → обновляешь страницу в браузере. Babel
покажет ошибки парсинга в консоли. Большой `app.jsx` транспилируется ~несколько секунд после
загрузки — если элементов «ещё нет», подожди пару секунд.

Соответствующие бэкенд-ручки фронта:
- `POST /api/ai/analyze` — основной ответ (SSE-стрим: фазы, reasoning, агенты, текст, источники).
- `POST /api/ai/clarify` — генерация/применение уточняющих вопросов.
- `POST /api/ai/export-pdf` — экспорт отчёта в PDF.

---

## 6. Прод-деплой (VM `ecs-oarb`, Облако УВА)

> Доступ к серверу — личный (по SSH-ключу владельца). Новому разработчику нужно получить
> **свой** доступ к Облаку УВА у ОАИТ (Евгений) — не переиспользовать чужой ключ.
> Полная инструкция и контакты — [docs/DEPLOY_UVA.md](DEPLOY_UVA.md).

**Координаты (текущий деплой владельца):**
- Хост: `87.242.123.218`, пользователь `amzenkovskiy-2127124`, ключ `~/.ssh/id_ed25519_uva`.
- Контейнер: `auditlens-app` (образ `auditlens:prod`), порт `8000`, `--network host`,
  `--env-file ~/auditlens/.env`, `--restart unless-stopped`.
- **Build-context на сервере:** `~/auditlens-container/` (там `Dockerfile` + копия `src/`).
- **Секреты** в `~/auditlens/.env` (НЕ в образе, НЕ в git).

Подключение:
```bash
ssh -i ~/.ssh/id_ed25519_uva amzenkovskiy-2127124@87.242.123.218
```

### 6.1. Быстрая правка фронта (hot-patch) — для итераций
Статика отдаётся прямо из `/app/src/...` (editable install), поэтому файл можно подменить
в работающем контейнере — изменения видны сразу после обновления страницы. **Слетает при
рестарте контейнера** — для постоянства нужен ребилд (§6.2).
```bash
# из локального каталога static/ (scp/rsync на ecs-oarb сломаны → tar-over-ssh):
tar czf - app.jsx index.html | ssh -i ~/.ssh/id_ed25519_uva amzenkovskiy-2127124@87.242.123.218 \
  'tar xzf - -C /tmp && \
   docker cp /tmp/app.jsx   auditlens-app:/app/src/bank_audit/web/static/app.jsx && \
   docker cp /tmp/index.html auditlens-app:/app/src/bank_audit/web/static/index.html'
```

### 6.2. Полный ребилд (durable) — зашивает изменения в образ
```bash
# 1) синхронизировать build-context с актуальным кодом (статику и/или весь src):
tar czf - app.jsx index.html | ssh -i ~/.ssh/id_ed25519_uva amzenkovskiy-2127124@87.242.123.218 \
  'tar xzf - -C ~/auditlens-container/src/bank_audit/web/static/'

# 2) на сервере: бэкап текущего образа, сборка, пересоздание контейнера
ssh -i ~/.ssh/id_ed25519_uva amzenkovskiy-2127124@87.242.123.218
  docker tag auditlens:prod auditlens:prod-prev            # откат при необходимости
  cd ~/auditlens-container && docker build -t auditlens:prod .
  docker stop auditlens-app && docker rm auditlens-app
  docker run -d --name auditlens-app --init --network host \
    --env-file "$HOME/auditlens/.env" --restart unless-stopped auditlens:prod
  curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/   # ждём 200
```
> ⚠️ Флаг `--init` ОБЯЗАТЕЛЕН: tini как PID 1 реапит зомби-процессы Chromium
> (Playwright). Без него defunct-процессы копятся в process-table.
> ⚠️ Ребилд занимает ~5–8 мин: в текущем `Dockerfile` слой `COPY src` идёт до установки,
> поэтому любая правка `src` заставляет переустановить Playwright Chromium. Режим `serve`
> миграции **не** запускает (только режим `migrate`), так что пересоздание контейнера БД не трогает.

---

## 7. Где смотреть/тестировать прод

SSH-контур не выставляет порт наружу — пробрасываем туннель и открываем в браузере локально:
```bash
ssh -f -N -L 18000:127.0.0.1:8000 -i ~/.ssh/id_ed25519_uva amzenkovskiy-2127124@87.242.123.218
# затем открыть http://127.0.0.1:18000   (вкладка ИИ-аналитик: /#ai)
```
Логи/состояние контейнера:
```bash
docker ps --filter name=auditlens-app
docker logs -f auditlens-app
```

---

## 8. Тесты и качество

```bash
pytest            # тесты в tests/ (+ вспомогательные скрипты в scripts/_test_*.py)
ruff check src    # линт (line-length 100)
mypy src          # типы (опционально)
```

---

## 9. Существующая документация

| Файл | О чём |
|------|-------|
| [README.md](../README.md) | Обзор проекта |
| [docs/ARCHITECTURE.md](ARCHITECTURE.md) | Архитектура: пайплайн данных, RAG, Deep Research |
| [docs/SETUP.md](SETUP.md) | Подробная локальная установка, миграции, сидинг |
| [docs/USAGE.md](USAGE.md) | Как пользоваться инструментом |
| [docs/API_KEYS.md](API_KEYS.md) | Какие ключи/переменные нужны и где взять |
| [docs/DEPLOY_UVA.md](DEPLOY_UVA.md) | Деплой в Облако УВА (ОАИТ): доступы, контейнер, БД |
| [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Частые проблемы и решения |
| `ASKING_PLAN.md`, `STREAMING_PLAN.md` | Заметки по логике уточнений и стриминга |

---

## 10. Памятка / гранатные углы

- **Фронт = 2 файла, без сборки.** Не ищи webpack/node — правь `app.jsx` + `index.html`.
- **`app.jsx` транспилируется в браузере** — синтаксические ошибки видны только в консоли
  DevTools; большой файл «оживает» через пару секунд после загрузки.
- **scp/rsync на `ecs-oarb` не работают** — переноси файлы через `tar | ssh` (см. §6).
- **Секреты только в `~/auditlens/.env` на сервере и локальном `.env`** — никогда в git/образ.
- **Hot-patch ≠ durable:** после `docker cp` сделай ребилд (§6.2), иначе правки слетят при рестарте.
- **БД — managed у ОАИТ** (pgvector). Не пересоздавай схему руками; миграции идемпотентны
  (журнал `schema_migrations`).
- Доступ к серверу/Облаку УВА — у каждого свой; за провижинингом к ОАИТ (см. DEPLOY_UVA.md).
