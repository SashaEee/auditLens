# Установка и запуск AuditLens

Два способа: **рекомендуемый (через Docker Compose)** и **полностью локально**. Выбери что удобнее.

---

## Системные требования

| Компонент | Минимум | Рекомендуется |
|---|---|---|
| **ОС** | macOS 12+ / Ubuntu 22+ / Windows 11 WSL2 | macOS 14+ или Ubuntu 24+ |
| **CPU** | 4 ядра | 8 ядер |
| **RAM** | 8 GB (BGE-M3 нужно ~2 GB) | 16 GB |
| **Диск** | 10 GB | 20 GB (модели + индекс + raw) |
| **Python** | 3.11 | 3.12 |
| **Postgres** | 14+ (нужен pgvector) | 16 (рекомендуется) |
| **Docker** | желательно | да |
| **Сеть** | без VPN для Fireworks AI | — |

---

## Путь A — через Docker Compose (рекомендуется)

Один скрипт делает всё: ставит зависимости, поднимает Postgres+pgvector+SearXNG, применяет миграции, проверяет настройку.

### Шаг 1: Клонируй репозиторий

```bash
git clone https://github.com/SashaEee/auditLens.git
cd auditLens
```

### Шаг 2: Запусти автоустановщик

```bash
bash scripts/setup.sh
```

Скрипт:
1. Проверит наличие Python 3.11+ и Docker
2. Создаст `.env` из шаблона
3. Поднимет PostgreSQL 16 + pgvector + SearXNG через `docker compose up -d`
4. Установит Python-зависимости в `.venv/`
5. Скачает Playwright Chromium (для PDF-экспорта)
6. Применит миграции БД
7. Скажет что заполнить в `.env` (LLM_API_KEY)

### Шаг 3: Получи и впиши LLM-ключ

Подробная инструкция → [docs/API_KEYS.md](API_KEYS.md). Кратко:
1. [https://fireworks.ai/](https://fireworks.ai/) → Sign Up (бесплатно, $15 кредитов)
2. [API Keys](https://fireworks.ai/account/api-keys) → Create
3. В `.env`:
   ```bash
   LLM_API_KEY=fw_твой_ключ_здесь
   ```

### Шаг 4: Запусти сервер

```bash
source .venv/bin/activate
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000) → готово.

---

## Путь B — полностью локально (без Docker)

Если по какой-то причине Docker не подходит.

### Шаг 1: Установи PostgreSQL 16 + pgvector

**macOS (Homebrew):**
```bash
brew install postgresql@16
brew services start postgresql@16

# Установка pgvector (требует ggcc/clang)
brew install pgvector
```

**Ubuntu 22+ / Debian:**
```bash
sudo apt update
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl enable --now postgresql
```

**Windows:**
Используй WSL2 + Ubuntu (инструкция выше). Нативный Windows не поддерживается из-за Playwright/Postgres сложностей.

### Шаг 2: Создай БД и пользователя

```bash
sudo -u postgres psql <<EOF
CREATE USER audit WITH PASSWORD 'audit';
CREATE DATABASE bank_audit OWNER audit;
\c bank_audit
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOF
```

### Шаг 3: Python-окружение

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e .
playwright install chromium
```

### Шаг 4: `.env`

```bash
cp .env.example .env
# Открой и заполни LLM_API_KEY (см. docs/API_KEYS.md)
```

### Шаг 5: Миграции

```bash
DSN="postgresql://audit:audit@localhost:5432/bank_audit"
for f in migrations/*.sql; do
    psql "$DSN" -f "$f"
done
psql "$DSN" -f src/bank_audit/analytics/views.sql
```

### Шаг 6: Запуск

```bash
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

---

## Опционально: SearXNG (улучшает web-поиск)

Если идёшь по пути B и хочешь стабильный мета-поиск:

```bash
# Поднимаем только searxng из docker-compose
docker compose up -d searxng

# Тест:
curl 'http://localhost:8888/search?q=сбербанк&format=json' | head -20
```

Без SearXNG AuditLens fallback'нется на DuckDuckGo/Yandex (часто 403 / captcha).

---

## Проверка установки

```bash
bash scripts/setup.sh check
```

Должно показать:
```
✅ python3 найден: Python 3.11.x
✅ docker найден: Docker version 24.x
✅ Docker Compose v2 найден
```

Проверка БД:
```bash
source .venv/bin/activate
python3 -c "from bank_audit import db; \
  print('Tables:', [r[0] for r in db.session().__enter__() \
        .execute(__import__('sqlalchemy').text(\"SELECT tablename FROM pg_tables WHERE schemaname='public'\")).all()])"
```

Должен вывести список из ~15 таблиц (bank, document, document_chunk, review, product_offer, …).

---

## Структура установленного

```
auditlens/
├── .env                    ← твои секреты (gitignored)
├── .venv/                  ← Python virtualenv
├── docker/
│   ├── postgres/init/      ← init-скрипт pgvector
│   └── searxng/            ← конфиг SearXNG
├── migrations/             ← SQL-схема
├── workspace/              ← runtime: raw, logs, PDF-кэш
│   ├── raw/                ← скачанные документы
│   ├── logs/               ← uvicorn-логи
│   └── reports/            ← сгенерированные PDF
├── src/bank_audit/         ← код
└── http://127.0.0.1:8000/  ← UI
```

---

## Следующие шаги

- [Как пользоваться (примеры запросов) →](USAGE.md)
- [Архитектура pipeline →](ARCHITECTURE.md)
- [Что-то не работает → TROUBLESHOOTING.md](TROUBLESHOOTING.md)
