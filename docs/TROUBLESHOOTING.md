# Troubleshooting

Типовые проблемы и решения.

---

## 🔑 LLM / API

### `Error: invalid_api_key` или 401 Unauthorized

**Симптом:** в логах сервера видно `openai.AuthenticationError: invalid_api_key`.

**Причина:** в `.env` лежит placeholder или невалидный ключ.

**Решение:**
1. Проверь `.env`:
   ```bash
   grep LLM_API_KEY .env
   ```
   Должно быть `fw_<длинная_строка>`, не `fw_REPLACE_WITH_YOUR_KEY`.
2. Получи новый ключ → [docs/API_KEYS.md](API_KEYS.md)
3. Перезапусти сервер (env читается при старте).

---

### `Model not found` (404)

**Симптом:** `openai.NotFoundError: Model 'accounts/fireworks/models/...' not found`.

**Причина:** модель снята с Fireworks или опечатка в имени.

**Решение:**
```bash
# Список доступных моделей
curl -H "Authorization: Bearer $LLM_API_KEY" \
     https://api.fireworks.ai/inference/v1/models | jq -r '.data[].id'
```

Подходящие на 2026 год:
- `accounts/fireworks/models/gpt-oss-120b` (по умолчанию)
- `accounts/fireworks/models/glm-5p1` (лучше для русского)
- `accounts/fireworks/models/deepseek-v4-pro` (1M контекст)
- `accounts/fireworks/models/kimi-k2p6`

---

### Reasoning-модель пишет «думаю...» вместо ответа

**Симптом:** в отчёте видны фразы типа «Пользователь спрашивает... Я думаю что...» вместо markdown.

**Причина:** `_StreamReasoningFilter` не распарсил `<answer>`-обёртку. Бывает редко если LLM проигнорировал инструкцию.

**Решение:** переключись на не-reasoning модель временно:
```bash
# .env
LLM_MODEL_SMART=accounts/fireworks/models/gpt-oss-120b
```

---

## 🐘 PostgreSQL

### `connection refused` при старте

**Симптом:** `psycopg.OperationalError: connection refused`.

**Проверка:**
```bash
docker compose ps             # postgres должен быть UP
docker compose logs postgres  # смотрим что в логах
```

**Типовые решения:**
1. Порт 5432 занят локальным postgres'ом:
   ```bash
   brew services stop postgresql@16   # macOS
   sudo systemctl stop postgresql     # Linux
   ```
   Или поменяй порт в `docker-compose.yml` на 5433 и обнови `DATABASE_URL`.
2. Контейнер не запущен:
   ```bash
   docker compose up -d postgres
   ```

---

### `extension "vector" is not available`

**Симптом:** на миграции 005 — `ERROR: extension "vector" is not available`.

**Причина:** используешь обычный `postgres:16` образ вместо `pgvector/pgvector:pg16`.

**Решение:** убедись что в `docker-compose.yml` указан правильный image:
```yaml
image: pgvector/pgvector:pg16
```
Пересоздай контейнер:
```bash
docker compose down -v   # ⚠ удалит данные
docker compose up -d postgres
bash scripts/setup.sh init-db
```

---

### `permission denied for table ...`

**Симптом:** `psycopg.errors.InsufficientPrivilege`.

**Решение:** ребут БД через docker compose:
```bash
docker compose down -v
docker compose up -d postgres
sleep 5
bash scripts/setup.sh init-db
```

---

## 🌐 Web search / fetcher

### Все запросы возвращают 0 результатов

**Проверка SearXNG:**
```bash
curl 'http://localhost:8888/search?q=сбербанк&format=json' | head -50
```
- Если 404/connection refused → запусти: `docker compose up -d searxng`
- Если в ответе есть results — норм, проблема в другом.

**Fallback на Brave:**
```bash
# .env
BRAVE_SEARCH_API_KEY=BSA_твой_ключ
```

---

### Сертификатные ошибки (`CERTIFICATE_VERIFY_FAILED`)

**Симптом:** при fetch'е sberbank.ru / cbr.ru.

**Причина:** не подхватился Russian Trusted Root CA bundle.

**Проверка:**
```bash
ls -la config/ca_bundle_combined.pem
# Должен быть ~250KB файл
```

Если файла нет — собери заново:
```bash
cat config/russian_trusted_root.pem > config/ca_bundle_combined.pem
python -c "import certifi; print(open(certifi.where()).read())" >> config/ca_bundle_combined.pem
```

---

### Playwright не запускается

**Симптом:** `playwright._impl._api_types.Error: Executable doesn't exist`.

**Решение:**
```bash
source .venv/bin/activate
playwright install chromium
playwright install-deps chromium    # только Linux: ставит system libs
```

---

## 🧠 Embeddings / BGE-M3

### Первый запрос висит 5+ минут

**Это норма** при первом запуске — скачивается модель BAAI/bge-m3 (~2GB). В логах:
```
Downloading: 100%|██████████| 2.27G/2.27G [04:21<00:00, 8.69MB/s]
```

Кешируется в `~/.cache/huggingface/`. Следующие запросы — 50ms.

---

### Out of memory при embed

**Симптом:** `torch.cuda.OutOfMemoryError` (на GPU) или Python убит OS-killer'ом.

**Решение:** перейди на лёгкую модель:
```bash
# .env
EMBEDDING_MODEL=intfloat/multilingual-e5-small
EMBEDDING_DIM=384
```
Затем пересоздай таблицу `document_chunk` со схемой `vector(384)`:
```sql
ALTER TABLE document_chunk ALTER COLUMN embedding TYPE vector(384);
```

---

## 📄 PDF export

### `Failed to launch browser`

**Решение:**
```bash
playwright install chromium
playwright install-deps chromium   # только Linux
```

---

### Графики не появляются в PDF (но в UI есть)

**Симптом:** в UI видишь Chart.js графики, в PDF — пустые рамки.

**Причина:** Playwright не дождался `window.__chartsRendered`.

**Решение:** проверить `src/bank_audit/web/pdf_export.py` — там есть `wait_for_function("window.__chartsRendered === true", timeout=12000)`. Если в логах `[pdf] charts timeout` — увеличь до 30000.

---

## 🖥 Server / UI

### Браузер показывает «Load failed» во время Deep Research

**Симптом:** SSE-соединение оборвалось.

**Причина:** длинный pipeline (>2 мин) + браузер закрыл idle.

**Проверка:** в logs `[merge-pass]` идёт работа.

**Решение:** уже встроено:
- `EventSourceResponse(ping=10)` — каждые 10s ping
- `X-Accel-Buffering: no` — отключает буферизацию

Если всё равно падает — посмотри `tail -f workspace/logs/uvicorn.log` и сообщи issue.

---

### Сервер не запускается: `Address already in use`

**Решение:**
```bash
lsof -ti :8000 | xargs kill -9
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Или используй другой порт:
```bash
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8001
```

---

## 📊 Качество отчётов

### Отчёт получился пустой / «не раскрыто» в каждом разделе

**Причины:**
1. В БД нет документов для этих банков. Залей seed:
   ```bash
   python scripts/demo_seed.py
   ```
2. Resolver неправильно понял тему. Проверь логи:
   ```bash
   grep "query_resolver" workspace/logs/uvicorn.log | tail -5
   ```
   Должно быть `topic=<твой продукт>`, не `topic=тариф` / `topic=условия`.
3. Все источники отфильтровались claim-verify. Это значит модель галлюцинировала — попробуй другую (`glm-5p1` обычно стабильнее `gpt-oss`).

---

### 19 из 30 фактов дропнуто claim-verify — это нормально?

**Да.** Claim-verify работает агрессивно: каждое число должно ПРЯМО встречаться в excerpts источника. Если LLM написал «ставка 13.5%», а в источнике только «13,14%» — дропнется. Это защита от галлюцинаций.

Drop-rate 50-70% — норма для коротких отчётов. 80-90% — модель плохая, смени.

---

## 🆘 Если ничего не помогло

1. Проверь логи: `tail -100 workspace/logs/uvicorn.log`
2. Запусти диагностику:
   ```bash
   bash scripts/setup.sh check
   ```
3. Создай issue: [GitHub Issues](https://github.com/SashaEee/auditLens/issues) с:
   - версия Python (`python3 --version`)
   - ОС
   - последние 50 строк uvicorn.log
   - содержимое `.env` (без значения LLM_API_KEY!)
