# ТЗ: добавить второй источник отзывов на вкладку «Отзывы»

**Исполнитель:** Дима · **Репозиторий:** `auditLens`, ветка `main`
**Клонировать:** `git clone https://github.com/SashaEee/auditLens.git`
**Все пути ниже — от корня репозитория.** Ссылки вида `файл.py:120` открываются в любом редакторе.

---

## 0. Как здесь всё устроено (10 минут чтения)

### 0.1. Что за проект

AuditLens — внутренний инструмент аудитора банка. Он раз в сутки сам собирает данные из открытых источников (тарифы банков, рейтинги, отзывы, новости), складывает их в PostgreSQL и показывает в веб-интерфейсе: ежедневный дайджест, аналитические вкладки, ИИ-аналитик поверх собранного.

Обзорные документы, если захочешь контекста: `README.md`, `docs/ONBOARDING.md`, `docs/ARCHITECTURE.md`. Онбординг разработчика — самый полезный, читается за 10 минут.

### 0.2. Где что лежит

| Что | Где |
|---|---|
| Весь Python-пакет | `src/bank_audit/` (src-layout, ставится editable как `bank_audit`) |
| **Адаптеры источников** (твоя основная зона) | `src/bank_audit/sources/*.py` |
| Конфиг источников | `config/sources.yaml` |
| Оркестратор сбора | `src/bank_audit/orchestrator/runner.py`, `.../registry.py` |
| Запись отзывов в БД | `src/bank_audit/normalizer/reviews.py` |
| Модели обмена данными | `src/bank_audit/models.py` |
| Планировщик (ежедневный прогон) | `src/bank_audit/digest/scheduler.py` |
| Аналитика вкладки «Отзывы» | `src/bank_audit/rag/reviews_dash.py` |
| Веб-API (FastAPI) | `src/bank_audit/web/app.py` |
| Фронтенд (React, **один файл, без сборки**) | `src/bank_audit/web/static/app.jsx` |
| SQL-миграции | `migrations/001…026*.sql` |
| Тесты | `tests/` (сейчас: `test_smoke.py`, `test_digest.py`, `tests/loophole/`) |

Фронт транспилируется Babel'ом прямо в браузере — правишь `.jsx`, обновляешь страницу, никакого `npm run build` (`docs/ONBOARDING.md:127-129`).

### 0.3. Словарь терминов проекта

- **Источник (source)** — ключ верхнего уровня в `config/sources.yaml`, например `bankiros_reviews` (`config/sources.yaml:244`). Описывает: какой адаптер использовать, каким коллектором ходить, и список **таргетов**.
- **Таргет (target)** — одна конкретная страница для сбора: `{name, url, bank_slug, …}` (`config/sources.yaml:248-250`). Один источник = много таргетов.
- **Адаптер** — Python-класс, наследник `SourceAdapter` (`src/bank_audit/sources/base.py:12`). Обязан реализовать `fetch()` (скачать), и по необходимости `parse_offers()` / `parse_reviews()` (разобрать).
- **Коллектор** — способ ходить в сеть: `http` (обычный httpx) или `browser` (Playwright с профилем). Задаётся полем `collector:` в YAML.
- **RawStore** — файловое хранилище скачанных страниц: `workspace/raw/<источник>/<таргет>/<ГГГГ>/<ММ>/<ДД>/<sha256>.html` (`src/bank_audit/storage/raw_store.py:14-30`). Каждый скачанный байт сохраняется — это доказательная база аудита.
- **ReviewDraft** — pydantic-модель одного отзыва, которую возвращает парсер (`src/bank_audit/models.py:64-76`).
- **Нормализация** — превращение `ReviewDraft` в строки таблиц `review` / `review_sentiment` / `review_topic` (`src/bank_audit/normalizer/reviews.py:130-207`).
- **extraction_run** — журнал прогонов: по строке на каждый таргет, со статусом `ok`/`failed` (`src/bank_audit/orchestrator/runner.py:17-29`).

### 0.4. Как запустить локально

```bash
cd <корень репозитория>

# 1. Окружение (уже есть .venv, пакет установлен editable)
source .venv/bin/activate         # или используй .venv/bin/python напрямую

# 2. Инфраструктура
docker compose up -d postgres searxng      # postgres → localhost:5432

# 3. .env — уже есть в корне (в git НЕ коммитится)
#    ключевая строка: DATABASE_URL (шаблон — .env.example:13)

# 4. Веб-интерфейс
.venv/bin/python -m bank_audit.cli serve --reload    # → http://127.0.0.1:8000
```

**Важно про CLI.** В `pyproject.toml:95` объявлена команда `auditlens = "bank_audit.cli:main"`, но функции `main` в `src/bank_audit/cli.py` нет (файл заканчивается на `cli()` в `cli.py:51-52`), и бинарника `auditlens` в `.venv/bin/` нет. **Работающий способ — `python -m bank_audit.cli`.** Проверено:

```
$ .venv/bin/python -m bank_audit.cli --help
Commands:
  ingest        Запустить ingest источника.
  list-sources  Показать доступные источники.
  quality       Прогнать data-quality чеки и записать отчёт.
  serve         Запустить веб-интерфейс.
```

### 0.5. Где прод

VM `ecs-oarb` в Облаке УВА, инструкция целиком — `docs/ONBOARDING.md:138-202` и `docs/DEPLOY_UVA.md`. Коротко: контейнер `auditlens-app`, порт 8000, секреты в `~/auditlens/.env` на сервере. **Доступ к серверу личный** — у тебя своего пока нет, за провижинингом идти к владельцу/ОАИТ. Твоя работа целиком делается и проверяется локально; деплой — не твой шаг.

Два отличия прода от локалки, которые тебя касаются:
- на проде **нет журнала `schema_migrations`** — миграции накатываются руками через `psql` (вопреки тому, что написано в `docs/ONBOARDING.md:239-240`);
- на проде каталог `WORKSPACE_DIR/raw` должен быть доступен на запись, иначе `RawStore.write` (`src/bank_audit/storage/raw_store.py:19`) падает и ежедневный сбор источника молча даёт `failed`.

---

## 1. ГЛАВНЫЙ ПОДВОХ — прочитай до того, как напишешь первую строку кода

**Вкладка «Отзывы» и таблица `review`, куда пишут коллекторы, — это два разных, не связанных между собой набора данных.**

### 1.1. Что читает вкладка

Все панели вкладки идут через `src/bank_audit/rag/reviews_dash.py`, а он с первой строки импортирует движок чужой БД:

```python
# src/bank_audit/rag/reviews_dash.py:20
from .bankiru_reviews import _get_engine, resolve_bank, search_reviews
```

Каждый из ~15 агрегатов бьёт в таблицу `bankiru.reviews`: `banks()` — `reviews_dash.py:210`, `overview()` — `:237-259`, `trend()` — `:289`, `themes()` — `:340`, `vs_market()` — `:382`, `geo()` — `:412`, `products()` — `:446`, `list_reviews()` — `:493-500`, `week_pulse()` — `:566`, `unclassified_week()` — `:653`, `weekly_signals()` — `:739`.

`bankiru` — это **отдельная база данных**, а не схема основной. DSN получается подменой имени БД в `DATABASE_URL` (`src/bank_audit/rag/bankiru_reviews.py:47-60`), под неё открывается **второй, принудительно read-only коннект**:

```python
# src/bank_audit/rag/bankiru_reviews.py:73-77
_engine = create_engine(
    dsn, pool_pre_ping=True, pool_size=2, max_overflow=2, future=True,
    connect_args={"options": "-c default_transaction_read_only=on"},
)
```

Наполняет эту БД ежедневный крон коллеги из другого репозитория (докстринг `bankiru_reviews.py:1-11`). **В нашем репозитории нет ни одного INSERT/UPDATE в `bankiru.*` — только чтение.** Схемы этой таблицы в `migrations/` тоже нет.

### 1.2. Что пишут коллекторы

Три существующих коллектора (`banki_reviews`, `sravni_reviews`, `bankiros_reviews`) пишут в **локальную** таблицу `review` основной БД (`migrations/002_reviews.sql:2-19`) через `upsert_review` (`src/bank_audit/normalizer/reviews.py:130`).

Вкладка «Отзывы» эту таблицу **не читает вообще**. Единственная ручка API, которая её читает, — `/api/reviews/topics` (`src/bank_audit/web/app.py:901-911`), и она используется на вкладке «Банки», а не на «Отзывах».

### 1.3. Следствие

На проде замерено: `banki_reviews` 1475 строк + `sravni_reviews` 388 + `bankiros_reviews` 362 = **2225 отзывов, которых не видно нигде в интерфейсе.**

Проверь свою локальную цифру сам (на машине владельца сейчас 849 — БД разработчика меньше прода):

```bash
.venv/bin/python -c "
from bank_audit import db; from sqlalchemy import text
db.init()
with db.session() as s:
    for r in s.execute(text('SELECT source, count(*) FROM review GROUP BY 1 ORDER BY 2 DESC')).all(): print(r)
"
```

**Если ты просто напишешь ещё один адаптер и добавишь ключ в `config/sources.yaml`, твоя работа станет четвёртой невидимой строкой в этом списке.** Именно поэтому задача поставлена как «источник + вывод на вкладку», а не только «источник».

### 1.4. Почему нельзя просто «долить в bankiru.reviews»

Такой вариант рассматривался и отклонён. Причины, каждая проверяемая по коду:

1. Коннект read-only на уровне кода (`bankiru_reviews.py:76`), пишущего движка в репозитории нет.
2. DDL таблицы `bankiru.reviews` в репозитории отсутствует — неизвестны NOT NULL, дефолты, ключи. Слепая вставка в чужую таблицу.
3. У тебя локально этой БД просто нет: `.env.example:13` задаёт только `DATABASE_URL` на `localhost/bank_audit`, а `docker-compose.yml` базу `bankiru` не создаёт (grep по `docker-compose.yml`, `docker/`, `migrations/`, `scripts/` — ноль совпадений). **Локально вкладка «Отзывы» у тебя будет пустой — это норма, а не поломка.**
4. Даже успешная вставка дала бы половину интеграции: строка поиска в ленте работает только семантикой по чужим эмбеддингам `bankiru.review_embeddings` (`bankiru_reviews.py:265-287`), которые считает сторонний пайплайн. Новые строки без эмбеддинга поиск не найдёт.
5. Корпус `bankiru` по построению — **только негатив 1–2★** (`bankiru_reviews.py:3-4`, подпись в UI `app.jsx:2566`). Рейтинга в витрине нет вообще: `grep -E 'rating|grade' src/bank_audit/rag/reviews_dash.py` даёт ноль. Каждая цифра там читается как «сколько жалоб». Долив обычных отзывов со всеми оценками сломал бы смысл всех подписей.

### 1.5. Принятое решение (вариант, который ты реализуешь)

**Новый источник пишет в локальную таблицу `review` (как три существующих), а на вкладке появляется ОТДЕЛЬНЫЙ блок «Другие площадки» с собственной тонкой витриной.**

Корпуса не смешиваем — они не сравнимы (негатив-онли vs все оценки, 217 банков vs единицы, есть город vs нет города). Существующие 15 запросов, главная страница и дайджест не трогаются вообще. Бонус: этим же блоком становятся видны те самые 2225 уже собранных отзывов.

---

## 2. Какую площадку брать

Владелец сказал: **на твоё усмотрение.** Ниже — критерии и кандидаты. Ни один кандидат живьём из репозитория не проверялся, поэтому **шаг 0 твоей работы — проверить площадку curl'ом до того, как писать код.**

### 2.1. Критерии выбора (в порядке важности)

1. **Отдаётся обычным HTTP.** Нужен `collector: http` — обычный `httpx` с браузерным User-Agent. Не бери площадку, требующую браузера: `collector: browser` тянет Playwright с профилем `OPENCLAW_BROWSER_PROFILE` (`src/bank_audit/config.py:33`) и упирается в капчу. Наглядно: у `sravni_reviews`, единственного browser-источника отзывов (`config/sources.yaml:236`), локально всего 185 строк против 440 у HTTP-шного `banki_reviews`.
2. **У каждого отзыва свой постоянный URL** (deep-link на конкретный отзыв, а не на страницу списка). Это критично — см. грабли №1.
3. **Есть дата публикации в абсолютном виде** (`12.03.2026`, а не «2 дня назад»). Относительные даты ломают дедуп — см. грабли №2.
4. **Есть оценка** (звёзды/баллы), приводимая к шкале 1–5.
5. **Русскоязычные тексты.** Вся таксономия тем — русские regex-подстроки (`src/bank_audit/rag/reviews_dash.py:44-88`). На иноязычном корпусе риск-карта даст 100% «без темы».
6. **Стабильная структура разметки.** По убыванию удобства: JSON-LD `schema.org/Review` в `<script type="application/ld+json">` → встроенный JSON (`__NEXT_DATA__`) → regex по устойчивым якорям (href, подписи). CSS-классы не использовать: на banki.ru и sravni.ru они хешированные и меняются каждый релиз (`banki_reviews.py:2-4`, `sravni_reviews.py:2-4`).
7. **Нет капчи, нет явного запрета в robots.txt/ToS**, нагрузка низкая (пауза между страницами, ограниченный `max_pages`).
8. Полезно, но не обязательно: **город** — под него на вкладке уже есть гео-панель (`reviews_dash.py:400-431`), а в локальной `review` города нет вообще.

### 2.2. Кандидаты

**Первый на проверку — Выберу.ру (vbr.ru), раздел отзывов о банках.** Профильная финансовая площадка, по структуре ожидается тот же паттерн, что уже отлажен на bankiros (JSON-LD). Если JSON-LD есть — адаптер получается почти копипастой `src/bank_audit/sources/bankiros_reviews.py` плюс пагинация по образцу `src/bank_audit/sources/banki_reviews.py:80-106`.

**Запасной — 2ГИС, отзывы об отделениях.** Единственный кандидат, добавляющий новое измерение (город + конкретное отделение). Отдаётся публичным JSON веб-клиента, HTML-парсинга нет. Минусы честные: API неофициальный, нужен предварительный поиск филиалов (двухэтапный сбор), использование клиентского ключа стоит согласовать с владельцем.

**Не рекомендуется — Яндекс.Карты.** Объём лучший, но капча + ToS + в проекте нет поддержки прокси (`grep -iE 'prox(y|ies)' src/bank_audit/**/*.py` — ни одной сетевой настройки). Требование «раз в день» не выполнится.

**Не рекомендуется — otzovik.com / irecommend.ru через browser-коллектор.** Инфраструктура в проекте есть, но капча останавливает ночной прогон. Учти: в локальной БД уже лежит 1 строка с `source='otzovik'` — её положил ИИ-агент через `src/bank_audit/research/v2/passive_indexer.py:192`, к коллекторам она отношения не имеет.

### 2.3. Проверка площадки за 5 минут (шаг 0, обязательный)

```bash
U='https://<площадка>/<банк>/otzyvy'
curl -s -A 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36' "$U" -o /tmp/probe.html
wc -c /tmp/probe.html                     # <20 КБ → скорее всего заглушка/JS-рендер
grep -c 'reviewBody' /tmp/probe.html      # >0 → есть JSON-LD, задача сильно упрощается
grep -c '__NEXT_DATA__' /tmp/probe.html   # >0 → есть SSR-JSON, тоже хорошо
grep -o 'href="[^"]*otzyv[^"]*"' /tmp/probe.html | sort -u | head   # есть ли deep-link на отзыв
curl -s https://<площадка>/robots.txt | head -30
```

Если ни JSON-LD, ни `__NEXT_DATA__`, ни отдельных ссылок на отзывы нет — **бери другую площадку**, не героизируй.

Результат шага 0 (какую площадку выбрал и почему) согласуй с постановщиком одним сообщением до начала кодирования.

---

## 3. Что сделать

### Шаг 1. Прописать источник в `config/sources.yaml`

Дописать новый блок **в конец файла** (последний блок `bankiros_reviews` начинается на `config/sources.yaml:244` и заканчивается на 289):

```yaml
# ── <Площадка> reviews — <способ парсинга> (HTTP) ─────────────────────────────
<site>_reviews:
  adapter: bank_audit.sources.<site>_reviews:<Site>ReviewsAdapter
  collector: http
  targets:
    - name: sber_reviews
      url: "https://<площадка>/bank/sberbank/otzyvy"
      bank_slug: sberbank
      bank_name: "Сбербанк"        # ← русское имя, см. грабли №3
      max_pages: 3
    - name: vtb_reviews
      url: "https://<площадка>/bank/vtb/otzyvy"
      bank_slug: vtb
      bank_name: "ВТБ"
      max_pages: 3
```

Начни с 2–3 таргетов. Расширять — после того, как парсер докажет качество.

`collector: http` читается в `src/bank_audit/orchestrator/runner.py:78` (управляет только паузами между таргетами). Адаптер регистрировать нигде не нужно: строка `adapter:` резолвится через importlib в `src/bank_audit/orchestrator/registry.py:11-12`, а `src/bank_audit/sources/__init__.py` пуст.

**Ежедневный сбор включается сам.** `_run_ingest_all` (`src/bank_audit/digest/scheduler.py:178`) в 05:00 МСК перебирает **все** ключи `load_sources()` (`scheduler.py:190`); час задаётся `INGEST_HOUR_MSK`, дефолт 5 (`scheduler.py:28`). Планировщик править **не надо**.

### Шаг 2. Написать адаптер `src/bank_audit/sources/<site>_reviews.py`

Эталон для копирования — `src/bank_audit/sources/bankiros_reviews.py` (167 строк, самый простой). Пагинация, если нужна, — `src/bank_audit/sources/banki_reviews.py:80-106`.

Каркас:

```python
class <Site>ReviewsAdapter(SourceAdapter):        # base: sources/base.py:12
    name = "<site>_reviews"                        # ДОЛЖНО совпадать с ключом в YAML

    def fetch(self, target) -> FetchResult:        # обязателен, base.py:27
        ...
    def parse_reviews(self, html, target) -> Iterable[ReviewDraft]:   # base.py:32
        ...
```

**Требования к `fetch()`:**
- Свой `httpx.Client` с браузерными заголовками — образец `bankiros_reviews.py:39-47` и `banki_reviews.py:45-53`. **Не используй `self.http`**: `HttpCollector` ходит с User-Agent `BankAuditBot/0.1` (`src/bank_audit/collectors/http.py:10`), большинство сайтов такое режет.
- `tenacity` retry **только на сетевые ошибки**: `retry_if_exception_type((httpx.TransportError, httpx.TimeoutException))` (`bankiros_reviews.py:58-59`). 403/404 не ретраить.
- Пауза между страницами при пагинации: `time.sleep(1.2)` (`banki_reviews.py:106`).
- `max_pages` брать из таргета: `int(target.get("max_pages", 3))` (`banki_reviews.py:57`).
- Обязательно сохранить снимок: `self.raw.write(self.name, target["name"], html, "html", meta={...})` (`bankiros_reviews.py:82-85`) и вернуть `RawSnapshot` (`models.py:28-38`).
- **На пустом/невалидном ответе бросать исключение**, а не возвращать пустые байты — образец `banki_reviews.py:132-137` и `bankiros_reviews.py:79-80`. Иначе прогон запишется как `ok` с нулём отзывов и деградацию источника никто не заметит.

**Требования к `parse_reviews()` — возвращает `ReviewDraft` (`src/bank_audit/models.py:64-76`):**

| Поле | Что класть |
|---|---|
| `source` | `self.name` |
| `source_review_id` | **Стабильный** идентификатор отзыва с площадки. Если у площадки есть свой id — брать его (`banki_reviews.py:160` берёт номер из URL). Если нет — `stable_digest` из `src/bank_audit/hashing.py:10` **только по неизменным полям** (см. грабли №2) |
| `source_url` | **Уникальный deep-link на конкретный отзыв.** Эталон — `banki_reviews.py:213`. Антипример — `bankiros_reviews.py:158` (кладёт URL страницы; так делать нельзя) |
| `bank_name_raw` | **Русское каноническое имя банка** из `target["bank_name"]` (см. грабли №3) |
| `product_category` | Оставь `None`, если не уверен (см. грабли №5) |
| `posted_at` | `datetime` с tz. Образцы парсинга: `bankiros_reviews.py:132-141`, `sravni_reviews.py:32-44` |
| `rating` | `Decimal`, приведённый **к шкале 1–5** |
| `title`, `author_raw` | Как есть; автор дальше хешируется (`normalizer/reviews.py:170` → `hashing.py:13`) |
| `text` | Текст отзыва. Короче 20 значимых символов — отбросится спам-фильтром (`normalizer/reviews.py:48-59`) |
| `status` | Если площадка отдаёт («решено»/«не решено») |
| `raw` | Всё остальное, что жалко потерять: `{"city": "Казань", "product_label": "Кредитная карта", "branch": "..."}` — это `jsonb`, миграция не нужна |

### Шаг 3. Фикстура и тест

Тестов для `src/bank_audit/sources/` в репозитории **нет ни одного** — ты пишешь первый. Каталог `tests/fixtures/` уже существует.

1. После первого ручного прогона (шаг 4) взять сохранённый снимок из `workspace/raw/<site>_reviews/<target>/<ГГГГ>/<ММ>/<ДД>/<sha256>.html` и скопировать в `tests/fixtures/<site>_reviews.html`.
2. Создать `tests/test_<site>_reviews.py` по образцу `tests/test_smoke.py:17-25`. Адаптер конструируется без сети и БД — `__init__` объявлен один раз в базовом классе (`sources/base.py:20`), а `parse_reviews` в существующих адаптерах не трогает ни `self.raw`, ни `self.settings`:

```python
items = list(
    <Site>ReviewsAdapter(settings=None, raw_store=None)
    .parse_reviews(FIXTURE_BYTES, {"name": "sber_reviews", "bank_slug": "sberbank",
                                   "bank_name": "Сбербанк", "url": "https://..."})
)
```

3. Проверить в тесте: `len(items) > 0`; у каждого непустые `source`, `source_review_id`, `source_url`, `bank_name_raw`, `text`; **`source_review_id` уникален внутри выдачи**; **`source_url` уникален внутри выдачи**; хотя бы у одного распарсились `posted_at` и `rating`, и `1 <= rating <= 5`; ни одного `posted_at` в будущем.

Что этот тест **не** ловит: смену вёрстки на самом сайте (фикстура заморожена). Он ловит поломку парсера при последующих правках. CI в репозитории нет (`.github/workflows` отсутствует) — тест запускается руками.

### Шаг 4. Ручной прогон и проверка записи

```bash
.venv/bin/python -m bank_audit.cli ingest --source <site>_reviews --target sber_reviews
```

Флаги **именованные** (`src/bank_audit/cli.py:14-15`); позиционный вызов `ingest <source>` упадёт. Команда печатает JSON вида `{"targets":1,"snapshots_new":1,"items_seen":N,"items_written":M}` (`cli.py:20-21`).

Дальше — SQL-проверки из раздела 5.

### Шаг 5. Витрина второго корпуса — новый файл `src/bank_audit/rag/reviews_local.py`

Новый файл, ~120 строк. **Ничего существующего не правим.**

Что переиспользовать из `reviews_dash.py` (скопировать/импортировать, но не менять оригинал):
- декоратор `_safe(default)` (`reviews_dash.py:25-38`) — чтобы падение панели не роняло вкладку;
- кэш `_cached(key, fn, ttl)` (`reviews_dash.py:169-178`);
- `THEMES` и `match_themes(body)` (`reviews_dash.py:44-88` и `:106-110`) — импортировать, темы считать **в Python** по тексту, а не regex'ом в SQL.

Функции: `banks_local()`, `overview_local(bank, days)`, `trend_local(bank, months)`, `themes_local(bank, days)`, `feed_local(bank, theme, limit)`.

Запросы к основной БД через штатную сессию (`src/bank_audit/db.py:16-27`), скелет:

```sql
SELECT r.review_id, r.source, r.source_url, r.posted_at, r.rating, r.title, r.text,
       r.raw->>'city' AS city, s.label AS sentiment
  FROM review r
  JOIN bank b USING (bank_id)
  LEFT JOIN review_sentiment s USING (review_id)
 WHERE b.bank_id = :bank_id
   AND r.posted_at >= now() - make_interval(days => :d)
 ORDER BY r.posted_at DESC
 LIMIT :lim
```

Мостик «имя банка → bank_id» — **только SELECT**, схема `bank` в `migrations/001_init.sql:13-21`:

```sql
SELECT bank_id FROM bank
 WHERE slug = :x OR name ILIKE :x OR :x = ANY(aliases)
 LIMIT 1
```

**Не вызывать `normalizer.offers.resolve_bank`** из витрины — он при промахе **создаёт** новую строку банка (`src/bank_audit/normalizer/offers.py:75-79`).

Учти разницу корпусов при формулировке метрик: локальные отзывы содержат все оценки, поэтому «жалоба» здесь = `rating <= 2 OR sentiment = 'neg'`, и подпись в UI должна это отражать.

### Шаг 6. Эндпоинты в `src/bank_audit/web/app.py`

Блок отзывов занимает `app.py:899-1022`, следующий раздел начинается на `:1025`. Добавь 4 новые ручки внутри этого блока — удобное место сразу после `reviews_theme_defs` (`app.py:947-950`), по образцу `_rd()` (`app.py:915-917`):

```python
def _rl():
    from ..rag import reviews_local
    return reviews_local

@app.get("/api/reviews/local/overview")
def reviews_local_overview(bank: str = "Сбербанк", days: int = 90):
    return _rl().overview_local(bank, days) or {}
```

Аналогично `/api/reviews/local/trend`, `/local/themes`, `/local/feed`. Параметры — те же имена, что у существующих (`bank`, `days`, `theme`, `limit`), чтобы фронт не изобретал новый контракт.

### Шаг 7. Блок на вкладке — `src/bank_audit/web/static/app.jsx`

Компонент `ReviewsPage` начинается на `app.jsx:2445`. Секция «Лента — доказательная база» заканчивается на `app.jsx:2773`. **Новую секцию вставлять сразу после неё (строка 2774)** — перед модалкой (`:2775`) и драуэром (`:2789`).

Что добавить:
- свой `useState` рядом с остальными (`app.jsx:2459-2470`);
- свой `useEffect` с `apiFetch` — образец `app.jsx:2528-2532`; `apiFetch` определён на `app.jsx:99`;
- секцию `<div className="rv-card">` с заголовком **«Другие площадки»**, подписью-источником (по образцу `rv-src` на `app.jsx:2565`) и явной оговоркой, что это **отдельный корпус, не сравнимый по объёму с banki.ru**;
- пустой ответ → `<EmptyState text="…"/>` (`app.jsx:329`), загрузка → `<Skel/>` (`app.jsx:339`), числа через `fmtNum` (`app.jsx:68`);
- `.catch(...)` на каждом запросе — падение твоего блока не должно ломать соседние панели.

**Существующие пять панелей (`app.jsx:2502-2515`) и ленту не трогать.**

### Шаг 8. Две правки-спутника, о которых легко забыть

1. `src/bank_audit/web/sources_catalog.py:199-200` — добавить свой ключ в множество `NOT_TARIFF`, иначе новый источник отзывов попадёт в каталог как «Витрина условий»:
   ```python
   NOT_TARIFF = {"cbr_registry", "banki_reviews", "sravni_reviews",
                 "bankiros_reviews", "banki_ratings", "<site>_reviews"}
   ```
2. `src/bank_audit/web/sources_catalog.py:245-250` — сейчас считает `count(*) FROM review` (всю таблицу целиком) и подписывает результат как **«bankiros.ru»**. Это уже неверно для трёх источников, а с твоим станет неверно для четырёх. Почини: сгруппируй по `source` и покажи по строке на площадку.

### Шаг 9 (опционально, только если площадка даёт город)

Миграция `migrations/027_reviews_city.sql`:
```sql
ALTER TABLE review ADD COLUMN IF NOT EXISTS city TEXT;
CREATE INDEX IF NOT EXISTS review_source_idx ON review(source);
```
Для минимальной версии **не нужна** — город живёт в `review.raw` (jsonb, `migrations/002_reviews.sql:15`). Помни: на проде `schema_migrations` нет, накатывать руками через `psql`.

---

## 4. Чего делать нельзя

1. **Не править** `src/bank_audit/rag/reviews_dash.py`, `src/bank_audit/rag/bankiru_reviews.py`, `src/bank_audit/digest/aggregator.py`. Первые два — существующая вкладка, третий питает главную страницу и дайджест (`digest/aggregator.py:43-58` вызывает `rd.weekly_signals` и `rd.week_pulse`). Все сигналы там построены на базлайнах «медиана + MAD» по окну 14–63 дня; подмешивание второго корпуса задним числом = ложные всплески владельцу на главной.
2. **Не писать в БД `bankiru`** ни в каком виде. Коннект read-only (`bankiru_reviews.py:76`), таблица чужая, схема неизвестна, параллельный писатель может снести твои строки.
3. **Не переименовывать и не удалять** существующие ключи в `config/sources.yaml` (`cbr_registry`, `sravni_api`, `sravni_browser`, `banki_ratings`, `banki_reviews`, `sravni_reviews`, `bankiros_reviews`). PyYAML не считает дубль ключа ошибкой — побеждает последний, скопированный и непереименованный блок **тихо затрёт** существующий источник.
4. **Не менять** `src/bank_audit/config.py` и `src/bank_audit/digest/scheduler.py` — ежедневность включается сама.
5. **Не брать `collector: browser`** для нового источника.
6. Не поднимать агрессию сбора: `max_pages` в разумных пределах, пауза между страницами обязательна.
7. Не коммитить `.env`, реальные снимки страниц с персональными данными, любые секреты. `.env` в `.gitignore` — так и оставить.
8. Не писать в БД из тестов и не ходить из тестов в сеть.
9. Не делать миграций с `DROP`/`ALTER` существующих колонок — только `ADD COLUMN IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.
10. В git-коммитах **не добавлять трейлер `Co-Authored-By`** — явная просьба владельца.

---

## 5. Критерии приёмки

Все команды — **из корня репозитория**, все — **до коммита**.

### 5.1. YAML валиден
```bash
.venv/bin/python -c "import yaml;yaml.safe_load(open('config/sources.yaml'))"
```
Молчание = успех. Это единственная правка, которая роняет **весь** ночной сбор целиком, а не один источник: `load_sources()` вызывается в `scheduler.py:188-190` **до** цикла по источникам и вне per-source `try/except` (`scheduler.py:192-196`); исключение улетает в общий guard цикла (`scheduler.py:254-256` → лог + `sleep 300` + повтор). Ту же функцию без обёртки дёргают `app.py:1206` (`GET /api/sources`) и `app.py:1269-1270` (`POST /api/ingest/run-all`) — оба отдадут 500.

### 5.2. Источник виден реестру, старые ключи на месте
```bash
.venv/bin/python -m bank_audit.cli list-sources
```
Эталон «до» (снято сейчас):
```
auto-review-targets: +25 banki, +25 sravni
cbr_registry → …:CBRRegistryAdapter (1 targets)
sravni_api → …:SravniApiAdapter (68 targets)
sravni_browser → …:SravniBrowserAdapter (7 targets)
banki_ratings → …:BankiRatingsAdapter (1 targets)
banki_reviews → …:BankiReviewsAdapter (27 targets)
sravni_reviews → …:SravniReviewsAdapter (27 targets)
bankiros_reviews → …:BankirosReviewsAdapter (14 targets)
```
Проверяй **не только появление своего ключа**, но и что все 7 прежних на месте с теми же числами.

Две вещи, которые собьют с толку и это **не ошибка**:
- у `banki_reviews`/`sravni_reviews` таргетов больше, чем строк в YAML (27 против 2) и печатается `auto-review-targets: +25 banki, +25 sravni` — это `_expand_review_targets` подмешивает топ-30 банков из БД (`config.py:91-164`);
- подмешивание **захардкожено ровно для этих двух ключей** (`config.py:122-123`). У твоего источника число таргетов обязано совпасть с YAML **один-в-один**. Несовпадение = опечатка в YAML.

### 5.3. Адаптер резолвится
```bash
.venv/bin/python -c "
from bank_audit.orchestrator.registry import load_adapter
print(load_adapter('<site>_reviews'))"
```
Печатает `(<class '...<Site>ReviewsAdapter'>, {...})`. Опечатка в строке `adapter:` даёт `ImportError`/`AttributeError` здесь же.

### 5.4. Тест парсера зелёный
```bash
.venv/bin/python -m pytest tests/test_smoke.py tests/test_<site>_reviews.py -q
```
Базовая часть (`test_smoke.py`) уже проходит — если она красная, дело не в тебе.

### 5.5. Ручной прогон: все таргеты `ok`
```bash
.venv/bin/python -m bank_audit.cli ingest --source <site>_reviews
```
Строка `extraction_run` создаётся на **каждый** таргет внутри цикла (`runner.py:83-89`), падение одного не валит остальные (`runner.py:124-129`):
```sql
SELECT target_name, status, items_seen, items_written, left(error, 200)
  FROM extraction_run
 WHERE source = '<site>_reviews'
 ORDER BY run_id DESC LIMIT 20;
```
Приёмка: у всех таргетов `status = 'ok'`, `items_written > 0` хотя бы у половины.

### 5.6. Строки появились и распределены по банкам
```sql
SELECT source, count(*) FROM review GROUP BY 1 ORDER BY 2 DESC;

SELECT b.name, count(*) FROM review r JOIN bank b USING(bank_id)
 WHERE r.source = '<site>_reviews' GROUP BY 1 ORDER BY 2 DESC;
```

### 5.7. Ноль «unknown»-банков у нового источника
```sql
SELECT count(*) FROM review r JOIN bank b USING(bank_id)
 WHERE r.source = '<site>_reviews' AND b.slug LIKE 'unknown\_%';
```
**Должно быть 0.** Для сравнения, замер по существующим источникам локально: `bankiros_reviews` — 101 из 223, `sravni_reviews` — 79 из 185. Это ровно та ошибка, которую ты не должен повторить (грабли №3).

### 5.8. Каждый отзыв — свой URL
```sql
SELECT source, count(*) AS rows, count(DISTINCT source_url) AS urls
  FROM review GROUP BY 1;
```
У твоего источника `rows` и `urls` должны совпадать. Замер сейчас: `bankiros_reviews` — 223 строки на **14** уникальных URL (209 дублей), `banki_reviews` и `sravni_reviews` — 0 дублей.

### 5.9. Даты и оценки вменяемые
```sql
SELECT count(*) FILTER (WHERE posted_at IS NULL)      AS no_date,
       count(*) FILTER (WHERE posted_at > now())      AS future,
       min(rating), max(rating)
  FROM review WHERE source = '<site>_reviews';
```
`future = 0`; `rating` в диапазоне 1–5. (Для справки: у `banki_reviews` сейчас одна строка с датой в будущем — регулярка `_DATE_RE` в `banki_reviews.py:36` подхватывает первую попавшуюся дату в блоке.)

### 5.10. Идемпотентность — главная проверка
```bash
# запомнить count(*), прогнать второй раз, сравнить
.venv/bin/python -m bank_audit.cli ingest --source <site>_reviews
```
Прирост строк по своему источнику должен быть **0** (или равен числу реально новых отзывов на площадке, если между прогонами прошли часы). Ненулевой прирост на неизменившейся странице = нестабильный `source_review_id` → база будет пухнуть дублями каждую ночь.

Нюанс: если HTML страницы **не изменился**, снимок конфликтует по `(source_page_id, content_sha256)` (`runner.py:44-52`), парсинг вообще **не вызывается** и прогон завершается как `ok` с нулями (`runner.py:102-106`). **Это норма, а не баг.** Чтобы честно проверить дедуп, прогоняй парсер на фикстуре дважды в тесте или удали снимок из `workspace/raw/`.

### 5.11. Вкладка «Отзывы» не сломалась
Открыть `http://127.0.0.1:8000/#reviews`. Локально верхние пять панелей будут пустыми/«нет данных» — **это ожидаемо**, БД `bankiru` локально нет (раздел 1.4). Проверить, что:
- страница не падает в белый экран, в консоли DevTools нет ошибок Babel;
- твой блок «Другие площадки» отрисован и показывает числа;
- при остановленном бэкенде блок показывает пустое состояние, а не ломает страницу.

### 5.12. В тексте нет персональных данных
```sql
SELECT count(*) FROM review
 WHERE source = '<site>_reviews'
   AND (text ~ '\+7[\s\-(]?\d{3}' OR text ~ '[\w.]+@[\w.]+\.\w+' OR text ~ '\d{16}');
```
Ожидание — 0 или единицы; если много, нужен фильтр в парсере. Имя автора в БД не хранится — только хеш (`normalizer/reviews.py:170`, `hashing.py:13-16`).

### 5.13. Линт
```bash
.venv/bin/python -m ruff check src/bank_audit/sources/<site>_reviews.py src/bank_audit/rag/reviews_local.py
```
Длина строки в проекте — 100 (`pyproject.toml`, секция `[tool.ruff]`).

---

## 6. Грабли (все замерены на этом коде)

**1. `source_url` = ссылка на страницу вместо ссылки на отзыв.**
`bankiros_reviews.py:158` кладёт `target.get("url", "")` — URL списка. Результат: 223 строки на 14 уникальных URL. В интерфейсе такие отзывы ведут «в никуда», а любая аналитика по `count(DISTINCT url)` даёт мусор. Эталон — `banki_reviews.py:213`: `f"https://www.banki.ru/services/responses/bank/response/{rid}/"`.

**2. Нестабильный `source_review_id` + относительные даты = база пухнет дублями каждую ночь.**
`bankiros_reviews.py:143-149` считает id как хеш от `(bank, body[:200], date, author)`. Если площадка пишет «2 дня назад» вместо «12.03.2026», строка `date` меняется ежедневно → каждую ночь тот же отзыв получает новый id. Ни `UNIQUE (source, source_review_id)` (`migrations/002_reviews.sql:18`), ни cross-source дедуп по `content_key` (`normalizer/reviews.py:115-127`, проверка `:147-154`) это не поймают — оба завязаны на дату. **Лечение:** брать собственный id площадки; если его нет — хешировать только по полям, которые физически не меняются (текст + автор), дату в хеш не класть.

**3. Резолвер банка: передавай русское имя, а не латинский слаг.**
`resolve_bank` (`src/bank_audit/normalizer/offers.py:37-79`) работает так: нормализация имени → словарь `BANK_ALIASES` → fuzzy (порог 88) → запасная ветка «а вдруг это уже существующий slug» (`offers.py:59-67`) → иначе **молча создаёт** банк со slug `unknown_<хеш>` (`offers.py:68-79`). Существующие ревью-адаптеры передают `bank_slug` (`bankiros_reviews.py:159`), и результат виден в замере: 101 из 223 строк `bankiros_reviews` и 79 из 185 строк `sravni_reviews` висят на `unknown_*`-банках. В базе уже 664 строки в `bank` — значительная часть от этого.
**Лечение:** добавь в таргет поле `bank_name` с русским каноническим именем и передавай его в `bank_name_raw`. Проверка — критерий 5.7.
Отдельно осторожно: fuzzy склеивает похожие имена («Совкомбанк Страхование» → банк `sovcombank`), поэтому имена в YAML пиши точные.

**4. Пустой ответ, отданный как «успех», — тихая деградация.**
Если `fetch()` вернёт пустые байты вместо исключения, снимок запишется, прогон получит `ok`, отзывов будет 0, и никто ничего не заметит месяцами. Делай как `banki_reviews.py:132-137`.

**5. `product_category` — это enum Postgres, не свободный текст.**
Допустимые значения перечислены в `src/bank_audit/models.py:10-19` (и в БД: `deposit, credit, card_debit, card_credit, mortgage, auto_loan, metals, investment, insurance, other, savings_account, refinance, mortgage_refinance, microloan, invest_broker, invest_pif, npf, osago, kasko, insurance_mortgage, insurance_travel, insurance_life, rko, business_loan, leasing, factoring, acquiring, currency_exchange, bank_rating`). Придуманное значение даст либо `ValidationError` в pydantic, либо ошибку enum в Postgres — а `normalize_reviews` держит **весь батч отзывов таргета в одной транзакции** (`normalizer/reviews.py:196-207`), то есть один битый отзыв откатит все остальные. **Оставляй `None`**, а метку продукта площадки клади в `raw["product_label"]`.

**6. Тексты короче 20 значимых символов отбрасываются молча.**
Спам-фильтр `_is_spam` (`normalizer/reviews.py:48-59`) режет короткие тексты, тексты с <15 буквами и с маркерами контактов (`+7-9`, `wa.me/`, `пиши в личку` — `reviews.py:29-32`). Если `items_seen` большой, а `items_written` нулевой — смотри сюда: скорее всего парсер вытаскивает анонсы вместо полных текстов.

**7. `self.http` ходит с User-Agent `BankAuditBot/0.1`** (`collectors/http.py:10`) — почти гарантированный 403. Все ревью-адаптеры поэтому делают свой `httpx.Client`.

**8. `http2=False`** — banki.ru не принимает HTTP/2 от не-браузеров (`banki_reviews.py:75`). Если получаешь странные обрывы, попробуй то же.

**9. Дубль ключа в YAML — не ошибка для парсера.** Побеждает последний. Скопировал блок и забыл переименовать — тихо потерял существующий источник. Ловится критерием 5.2.

**10. Битый `config/sources.yaml` роняет весь ночной сбор**, а не только твой источник — механика в критерии 5.1. Для контраста: битый файл адаптера локален, `load_adapter` (`registry.py:6`) вызывается изнутри `runner.ingest` (`runner.py:58`) уже под per-source `try`.

**11. `WORKSPACE_DIR/raw` должен быть доступен на запись** (`raw_store.py:12,19`) — иначе `fetch()` падает на сохранении снимка. Локально путь берётся из `.env` (`WORKSPACE_DIR=./workspace`, `.env.example:54`).

**12. Кэш витрины на процесс, TTL 1 час** (`reviews_dash.py:164-178`). Если делаешь свой `_cached` — на время отладки ставь TTL 0, иначе будешь час смотреть на старые числа и думать, что код не применяется.

**13. `app.jsx` транспилируется в браузере.** Синтаксическая ошибка не даст ошибки в терминале — только в консоли DevTools, и страница останется белой. Файл на 7065 строк «оживает» через пару секунд после загрузки (`docs/ONBOARDING.md:234-235`).

**14. В таблицу `review` пишет не только ingest.** Четвёртый писатель — `src/bank_audit/research/v2/passive_indexer.py:192` (`index_review_passive`), его вызывает ИИ-агент отзывов, когда находит что-то на отзовиках. Отсюда строка `source='otzovik'` в локальной базе. Не пугайся и не удаляй.

---

## 7. Оценка объёма

| Шаг | Что | Оценка |
|---|---|---|
| 0 | Выбор и curl-проверка площадки, согласование | 0.5–1 ч |
| 1 | `config/sources.yaml` | 0.5 ч |
| 2 | Адаптер `sources/<site>_reviews.py` | 4–8 ч (JSON-LD ~4 ч, regex по HTML ~8 ч) |
| 3 | Фикстура + `tests/test_<site>_reviews.py` | 1–2 ч |
| 4 | Ручной прогон, SQL-проверки, доводка парсера | 1–2 ч |
| 5 | `rag/reviews_local.py` — витрина второго корпуса | 3–5 ч |
| 6 | 4 эндпоинта в `web/app.py` | 1 ч |
| 7 | Блок «Другие площадки» в `app.jsx` | 3–5 ч |
| 8 | `sources_catalog.py`: `NOT_TARIFF` + починка подписи | 0.5 ч |
| 9 | Миграция 027 (опционально) | 0.5 ч |
| — | Прогон критериев приёмки, оформление PR | 1–2 ч |
| | **Итого** | **~2–4 рабочих дня** |

Разумная точка «показать промежуточный результат» — после шага 4: источник собирается, строки в базе, дедуп работает. Дальше идёт визуализация, её проще обсуждать на живых данных.

---

## 8. С чем прийти, если застрял

Приложи **всё** из списка ниже — это ровно тот минимум, по которому можно ответить, не переспрашивая.

**Всегда:**
1. Точная команда, которую запускал, и её полный вывод (не пересказ).
2. Ветка и `git status --short` — что изменено.
3. Какую площадку выбрал и результат шага 0 (вывод curl-проверки из раздела 2.3).

**Если не собирается / падает прогон:**
4. Вывод `.venv/bin/python -m bank_audit.cli ingest --source <ключ> --target <таргет>` целиком.
5. SQL:
   ```sql
   SELECT run_id, target_name, status, items_seen, items_written, error
     FROM extraction_run WHERE source = '<ключ>' ORDER BY run_id DESC LIMIT 10;
   ```
   Поле `error` обрезается до 500 символов (`runner.py:126`) — этого обычно хватает.
6. Путь к сохранённому снимку в `workspace/raw/<ключ>/<таргет>/…` и его размер (`ls -la`). Если файла нет — значит `fetch()` не дошёл до `raw.write`.

**Если парсер даёт 0 отзывов:**
7. Сам файл фикстуры (`tests/fixtures/<site>_reviews.html`) или ссылка на страницу.
8. `items_seen` vs `items_written` из вывода `ingest`: `seen>0, written=0` — режет спам-фильтр или дедуп; `seen=0` — не работает парсер.
9. Вывод:
   ```bash
   .venv/bin/python -c "
   from bank_audit.sources.<site>_reviews import <Site>ReviewsAdapter
   h=open('tests/fixtures/<site>_reviews.html','rb').read()
   items=list(<Site>ReviewsAdapter(settings=None, raw_store=None).parse_reviews(h, {...}))
   print(len(items)); print(items[0] if items else 'пусто')"
   ```

**Если данные записались, но выглядят неправильно:**
10. Результаты SQL-проверок 5.6–5.9 (банки, дубли URL, даты, оценки) — прямо таблицей.
11. Пример 2–3 строк:
    ```sql
    SELECT source, source_review_id, source_url, posted_at, rating, left(text, 200), raw
      FROM review WHERE source = '<ключ>' ORDER BY review_id DESC LIMIT 3;
    ```

**Если не работает вкладка:**
12. Скриншот вкладки + **скриншот консоли DevTools** (без консоли по фронту диагностировать нечего).
13. Ответ ручки напрямую: `curl -s 'http://127.0.0.1:8000/api/reviews/local/overview?bank=Сбербанк&days=90' | head -c 1000`.
14. Лог uvicorn за время запроса.

**Отдельно стоит прийти, не тратя время на самостоятельные раскопки, если:**
- площадка отдаёт капчу или требует JS-рендера — это смена площадки, решение постановщика;
- нужен доступ к проду или к БД `bankiru` — доступы личные, выдаёт владелец;
- хочется «а давайте всё-таки объединим два корпуса в одну витрину» — это отдельный эпик (собственная таблица-двойник контракта + переключатель корпуса на вкладке), обсуждается после того, как новый источник докажет качество данных.