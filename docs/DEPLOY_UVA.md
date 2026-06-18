# Публикация AuditLens в Магазине приложений Облака УВА

Гайд по выкатке AuditLens как **контейнера** в Магазине приложений Облака УВА
(Cloud.ru/SberCloud). Модель платформы: вы поднимаете контейнер приложения на
машине вашей вертикали, а ОАИТ берёт на себя поддомен `<app>.uva-advanced.ru`,
SSL-сертификат и настройку доступа (Authentik / OIDC).

> Для **локальной разработки** используйте `docs/SETUP.md` + корневой
> `docker-compose.yml` (там свой Postgres+SearXNG). Этот документ — про **прод**.

---

## 1. Целевая топология

```
   Пользователь (браузер, банковский контур)
        │  https://auditlens.uva-advanced.ru
        ▼
   Реверс-прокси ОАИТ  ──  SSL-терминация + Authentik (OIDC, доступ)
        │  http (внутри контура)
        ▼
   [ app ]  uvicorn 0.0.0.0:8000   ←─ docker-compose.prod.yml на VM вертикали
        ├── managed PostgreSQL + pgvector   (10.0.3.43:5432, схема auditlens)
        ├── Foundation Models                (https://foundation-models.api.cloud.ru/v1/)
        ├── [ searxng ] sidecar              (внутренняя сеть compose, bing+dogpile)
        └── OBS (S3)                         (опц., для выгрузок — пока не подключён)
```

**Модель — ОДИН общий инстанс на всех.** Один контейнер приложения + одна общая
managed-БД (`oarb_auditlens`, её уже создал ты, pgvector включил суперюзер ОАИТ).
Все пользователи открывают ОДИН сайт за Authentik и пополняют ОБЩУЮ базу — никаких
персональных БД/инстансов. Обычный пользователь просто открывает URL и работает;
миграции, `.env` и ключ API — это одноразовая настройка деплоя (делаешь ты/ОАИТ),
а не действие каждого пользователя.

**Что приложение делает само:** слушает `0.0.0.0:8000`, работает на корне
поддомена, отдаёт SPA + API + SSE, читает/пишет managed-PG, ходит в Foundation
Models и SearXNG.

**Что приложение НЕ делает (берёт на себя платформа ОАИТ):**
- ❌ собственную авторизацию/логин — доступ режет **Authentik** на прокси;
- ❌ терминацию TLS — это делает прокси ОАИТ;
- ❌ свой Postgres/pgvector — он **managed**.

---

## 2. Предусловия от ОАИТ / админов (согласовать ДО публикации)

Это **не зависит от кода** — без этого контейнер не взлетит:

- [ ] **Docker на VM вертикали** + членство пользователя в группе `docker`.
      На `ecs-oarb` у пользователя сейчас **нет** sudo/докер-группы — нужна
      docker-capable машина в подсети Postgres (запросить у ОАИТ).
- [ ] **Расширения в managed-PG ставит суперюзер ОАИТ** (роль приложения — не
      суперюзер). В БД/схеме `auditlens` выполнить:
      ```sql
      CREATE EXTENSION IF NOT EXISTS vector;
      CREATE EXTENSION IF NOT EXISTS pgcrypto;
      ```
      Миграции переживут и отсутствие `vector` («vector-free» фаза, см. §5), но
      для полноценного RAG расширение нужно.
- [ ] **search_path схемы**: либо в `DATABASE_URL` (`?options=-csearch_path%3Dauditlens`),
      либо `ALTER ROLE <app> IN DATABASE <db> SET search_path=auditlens,public;`.
- [ ] **Сеть из контейнера**: до `10.0.3.43:5432`, до `foundation-models.api.cloud.ru`,
      (и до OBS, если будем хранить выгрузки).
- [ ] **Поддомен + SSL + Authentik** на сервис `app` (делает ОАИТ).
- [ ] **Секреты на сервере** — достаточно простого `.env` на VM рядом с compose
      (Infisical — опционально, по желанию ОАИТ). Главное: НЕ коммитить `.env` в git
      и НЕ пекти в образ (там пароль БД и ключ API). `.gitignore`/`.dockerignore` это
      уже обеспечивают.

---

## 3. Сборка и запуск

```bash
# 0. На VM вертикали: получить код (git clone / rsync) и положить .env
cp .env.prod.example .env        # заполнить значениями из Infisical ([SECRET])

# 1. Собрать образ
docker compose -f docker-compose.prod.yml build

# 2. Один раз накатить миграции против managed-PG (идемпотентно)
docker compose -f docker-compose.prod.yml run --rm app migrate

# 3. Поднять сервис (app + searxng)
docker compose -f docker-compose.prod.yml up -d

# 4. Проверка
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8000/healthz   # {"status":"ok"}
curl -fsS http://127.0.0.1:8000/readyz    # {"status":"ready"} (если PG доступна)
```

После этого сообщить ОАИТ имя сервиса/порт — они навесят поддомен, SSL и Authentik.

---

## 4. Образ — что внутри / чего нет

- База `python:3.12-slim` + **Playwright Chromium** (рендер SPA-сайтов банков и
  HTML→PDF экспорт отчётов). Запуск headless под non-root → флаги `--no-sandbox`
  и `--disable-dev-shm-usage` (уже в коде).
- **Без `torch`/`sentence-transformers`** — эмбеддинги идут через API
  (`EMBEDDING_MODE=api`, bge-m3 1024d). Экономия ~2.5 ГБ.
- Запуск под пользователем `appuser` (uid 10001), не root.
- `config/` (settings.yaml, sources.yaml, CA-сертификаты Минцифры) — внутри образа.
- Секретов в образе нет; `.env`/`.venv`/`workspace`/`.git` исключены `.dockerignore`.
- `HEALTHCHECK` бьёт в `/healthz`.

---

## 5. Миграции и pgvector

- Накат — режим `migrate` энтрипоинта: применяет `migrations/*.sql` через журнал
  `schema_migrations` (каждый файл максимум один раз) + `analytics/views.sql`.
- Все миграции **идемпотентны** (`CREATE … IF NOT EXISTS`, guarded `CREATE TYPE`) —
  безопасно гонять повторно, в т.ч. против уже мигрированной БД.
- `CREATE EXTENSION vector/pgcrypto` обёрнуты так, чтобы **не падать** без прав
  суперюзера (их ставит ОАИТ). Колонка `document_chunk.embedding` и HNSW-индекс
  вынесены в `migrations/ensure_vector.sql` — он применяется на **каждый** `migrate`
  (НЕ журналируется) и создаёт их, только если расширение `vector` доступно (до этого
  — «vector-free» фаза). Поэтому после того как суперюзер выполнит
  `CREATE EXTENSION vector`, достаточно просто повторить `migrate` — вектор-объекты
  до-создадутся автоматически.
- По умолчанию `serve` миграции **не** запускает. Авто-накат на старте —
  `RUN_MIGRATIONS_ON_START=1` (не рекомендуется при нескольких репликах).

---

## 6. Переменные окружения

Полный шаблон — `.env.prod.example`. Кратко:

| Переменная | Секрет | Назначение |
|---|---|---|
| `DATABASE_URL` | ✅ | DSN managed-PG (схема `auditlens`) |
| `LLM_BASE_URL` / `LLM_API_KEY` | ключ ✅ | Foundation Models |
| `LLM_MODEL_*` | — | модели (дефолт — внутренний `gpt-oss-120b`) |
| `EMBEDDING_MODE=api` + `EMBEDDING_*` | ключ ✅ | эмбеддинги bge-m3 через API |
| `SEARXNG_URL=http://searxng:8080` | — | sidecar-поиск |
| `WORKSPACE_DIR=/app/workspace` | — | артефакты (volume) |
| `CORS_ALLOW_ORIGINS` | — | сузить за прокси (или `""` выключить) |
| `SMTP_*` | пароль ✅ | алерты (опц.; без логина — выключены) |

**Не задавать в проде:** `DEMO_MODE`, `OPENCLAW_BROWSER_PROFILE`, `OPENAI_API_KEY`.

---

## 7. Health-эндпоинты

- `GET /healthz` — **liveness**, без БД (200 пока процесс жив).
- `GET /readyz` — **readiness**, делает `SELECT 1` (503 если БД недоступна).

Прокси/оркестратору: liveness → `/healthz`, readiness → `/readyz`.

---

## 8. Локальный smoke-тест образа (до выкатки)

Чтобы проверить образ на машине с Docker (например с локальным Postgres из
корневого `docker-compose.yml`):

```bash
docker compose up -d postgres searxng           # dev-инфра (pgvector + searxng)
docker build -t auditlens:test .
docker run --rm --network host \
  -e DATABASE_URL='postgresql+psycopg://audit:audit@127.0.0.1:5432/bank_audit' \
  auditlens:test migrate
docker run --rm --network host \
  -e DATABASE_URL='postgresql+psycopg://audit:audit@127.0.0.1:5432/bank_audit' \
  -e LLM_API_KEY=test -e EMBEDDING_MODE=api \
  -p 8000:8000 auditlens:test
# затем: curl localhost:8000/healthz  и  localhost:8000/readyz
```

---

## 9. Известные ограничения / TODO

- **OBS (S3) не подключён** — выгрузки/raw пишутся на диск контейнера (volume
  `auditlens_workspace`). Для постоянства/масштаба вынести в OBS (`boto3`,
  pre-signed). Появятся секреты `OBS_*` → Infisical.
- `searxng/searxng:latest` — **запиннить** на протестированный тег/дайджест.
- В UI местами захардкожена подпись модели — не блокер публикации, но поправить.
- Приложение монтируется в **корень** поддомена (root-relative пути). Под-путь
  (`/auditlens/`) не поддержан без `root_path`.
