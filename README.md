<div align="center">

# 🔍 AuditLens

**Deep-Research платформа для внутреннего аудита банковских продуктов**

LLM-агент + RAG + structured БД. Задаёшь вопрос на русском — получаешь аналитический отчёт со ссылками на первоисточники, графиками и PDF-экспортом за 1-3 минуты.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Postgres 16 + pgvector](https://img.shields.io/badge/postgres-16+pgvector-336791.svg)](https://github.com/pgvector/pgvector)

[Быстрый старт](#-быстрый-старт-5-минут) · [Документация](docs/) · [Архитектура](docs/ARCHITECTURE.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

![AuditLens — главный экран](docs/img/01_dashboard.png)

</div>

---

## 🎯 Для кого

**Аналитики и аудиторы банковского сектора**, которым нужно регулярно сравнивать продукты разных банков, искать данные по ставкам/тарифам/жалобам клиентов, и готовить отчёты для руководства. AuditLens заменяет ручной обзвон 5-10 сайтов банков, копирование цифр в Excel и склеивание в Word — на один запрос на русском языке.

---

## ⚡ Что умеет

<table>
<tr>
<td width="50%" valign="top">

### 🔬 Deep Research
Многоступенчатый pipeline: planner → executor → fact-extract → claim-verify → synth → critic → agent-loop → merge.

12-16 параллельных шагов сбора, 9 LLM-проходов, типичное время **1-3 мин** на отчёт.

</td>
<td width="50%" valign="top">

### 📚 Цитаты и trust scoring
Каждое утверждение в отчёте имеет ссылку `[N]` на источник.

Trust-классы: 🟢 регуляторы (cbr.ru, pravo.gov.ru — 0.98), 🟡 банки (0.95), 🟠 агрегаторы (0.65), 🔴 блоги (исключаются).

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🛡 Anti-hallucination
3 уровня защиты:
- topical filter (off-topic документы исключаются)
- **claim-verify** — regex проверяет что каждое число РЕАЛЬНО есть в источнике
- citation-filter (удаление невалидных `[N]`)

</td>
<td width="50%" valign="top">

### 📊 Графики и PDF
Автоматическая визуализация числовых сравнений через Chart.js (sequential editorial-palette).

PDF-экспорт через Playwright Chromium — A4, embedded шрифты, графики, источники.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🌐 Универсальный поиск
4 backend'а с автоматическим fallback:
**SearXNG** (7 движков, безлимит) → **Brave** API → **DDG** → **Yandex**.

Поддержка Russian Trusted Root CA для Сбера / ЦБ / госсайтов.

</td>
<td width="50%" valign="top">

### 🧠 Universal product support
Работает с **любым** банковским продуктом без хардкода. Resolver-LLM сам определяет:
- тему / синонимы / морф-формы
- URL-paths для landing-страниц
- нужны ли govt-источники (НПА, ЦБ)

</td>
</tr>
</table>

---

## 🚀 Быстрый старт (5 минут)

### Что нужно

| | Версия | Где взять |
|---|---|---|
| **Python** | 3.11+ | [python.org](https://www.python.org/downloads/) |
| **Docker Desktop** | любая | [docker.com](https://www.docker.com/products/docker-desktop/) |
| **Fireworks AI ключ** | — | [fireworks.ai](https://fireworks.ai/) (бесплатные $15) |

### Шаги

```bash
# 1. Клон репо
git clone https://github.com/SashaEee/auditLens.git
cd auditLens

# 2. Один скрипт = всё установлено
bash scripts/setup.sh

# 3. Получи Fireworks ключ (см. docs/API_KEYS.md) и впиши в .env
nano .env   # или открой в любом редакторе
# LLM_API_KEY=fw_твой_ключ

# 4. Запусти сервер
source .venv/bin/activate
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000) → введи вопрос → готово.

> 📖 Подробная установка с альтернативами Docker → [docs/SETUP.md](docs/SETUP.md)
> 🔑 Все API-ключи и где их брать → [docs/API_KEYS.md](docs/API_KEYS.md)

---

## 💬 Примеры вопросов

> Полный гайд с десятками примеров → [docs/USAGE.md](docs/USAGE.md)

**Сравнительные исследования (включи 🔬 Deep Research):**

```
Семейная ипотека: ставки, первый взнос, требования к доходу
в Сбер, ВТБ, Альфа-банк, ДомРФ
```

```
Сравни премиальные дебетовые карты Сбер Прайм, Тинькофф Premium,
Альфа Wealth по комиссиям, кешбэку, привилегиям
```

```
РКО для ИП с оборотом до 10 млн/год: тарифы, эквайринг, бесплатные
операции — Сбер, Тинькофф, Точка, Модульбанк
```

```
Карта ветерана СВО: условия, привилегии, льготы в банках-участниках
проекта (Сбер, ВТБ, ПСБ, Газпромбанк)
```

**Быстрые запросы (без Deep Research):**

```
Топ-10 жалоб клиентов Тинькофф за последний месяц
```

```
Покажи актуальные ставки по вкладам от 1 млн руб на 12 мес
```

---

## 🖼 Скриншоты

### Аналитический dashboard

![Дашборд позиции Сбера vs медианы рынка](docs/img/01_dashboard.png)
*Главный экран — еженедельная сводка по позиции банка в рынке: где сильнее/слабее, динамика, изменения условий, флаги качества.*

### ИИ-аналитик (чат)

![AI welcome](docs/img/02_chat_welcome.png)
*Чат-интерфейс с быстрыми запросами справа и подсказками по подключённым источникам.*

![Вопрос с Deep Research](docs/img/03_question_with_deep.png)
*Длинный вопрос введён, режим Deep Research включён (кнопка слева от поля ввода).*

### Deep Research в работе

![Pipeline в процессе](docs/img/08_deep_research_in_progress.png)
*Стадия Discovery: planner раскладывает вопрос на шаги, сбор источников через semantic_search + fetch_official.*

![План отчёта и счётчики](docs/img/09_report_done.png)
*15/15 шагов выполнено, 27 источников (24 high-trust, 3 mid). План отчёта (4 раздела) сгенерирован outline-planner'ом. 56 фактов прошли claim-verify, 4 отфильтровано (защита от галлюцинаций).*

### Боковые секции

<table>
<tr>
<td width="50%">

![Источники](docs/img/04_sources.png)
*Источники: карточки документов с trust-индикаторами и фильтрами.*

</td>
<td width="50%">

![База знаний](docs/img/05_knowledge_base.png)
*База знаний — семантический поиск по проиндексированным документам.*

</td>
</tr>
<tr>
<td width="50%">

![Отзывы](docs/img/06_reviews.png)
*Отзывы клиентов с группировкой по темам и тональности.*

</td>
<td width="50%">

![Рынок](docs/img/07_market.png)
*Рынок: маркет-офферы из v_offer_current с фильтрами по банку/категории.*

</td>
</tr>
</table>

---

## 🏗 Архитектура

```mermaid
flowchart LR
    Q[Вопрос] --> R[Resolver<br/>понимает тему]
    R --> P[Planner<br/>12-16 шагов]
    P --> E[Executor<br/>parallel batches]
    E --> T{Tools}
    T --> S[semantic_search<br/>pgvector]
    T --> W[web_search<br/>SearXNG→Brave→DDG]
    T --> F[fetch_official<br/>Playwright]
    E --> FE[Fact-extract<br/>per-bank parallel]
    FE --> V[Claim-verify<br/>anti-hallucination]
    V --> SY[Synthesizer<br/>streaming]
    SY --> CR[Critic-pass]
    CR --> AG[Agent-loop ×2]
    AG --> M[Final merge]
    M --> CH[Charts]
    M --> PDF[PDF export]
```

> Полное описание pipeline'а → [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

**Стек:**
- **Python 3.11+**, FastAPI, SQLAlchemy 2, asyncio
- **PostgreSQL 16 + pgvector** для семантического поиска (1024d embeddings, BGE-M3)
- **React** (через Babel-standalone, без bundler'а — простое развёртывание)
- **Playwright Chromium** для real-time fetch и PDF-экспорта
- **LLM** — Fireworks AI (OpenAI-compatible), поддержка Claude/локальных vLLM
- **Web search** — SearXNG / Brave / DuckDuckGo / Yandex с automatic fallback

---

## 📦 Структура репозитория

```
auditlens/
├── README.md                        ← ты здесь
├── LICENSE                          ← MIT
├── pyproject.toml                   ← зависимости
├── .env.example                     ← шаблон конфига
├── docker-compose.yml               ← Postgres + SearXNG
├── docs/
│   ├── SETUP.md                     ← детальная установка
│   ├── API_KEYS.md                  ← где брать ключи (Fireworks $15)
│   ├── USAGE.md                     ← примеры вопросов
│   ├── ARCHITECTURE.md              ← как устроен pipeline
│   ├── TROUBLESHOOTING.md           ← типовые проблемы
│   └── img/                         ← скриншоты для README
├── docker/
│   ├── postgres/init/               ← init pgvector
│   └── searxng/                     ← конфиг SearXNG
├── migrations/                      ← SQL миграции (001-009)
├── scripts/
│   ├── setup.sh                     ← автоустановщик
│   └── demo_seed.py                 ← демо-данные
├── config/
│   ├── ca_bundle_combined.pem       ← certifi + Russian Trusted Root
│   └── russian_trusted_root.pem     ← сертификат Минцифры
└── src/bank_audit/
    ├── ai/                          ← LLM-логика (resolver, planner, deep_research)
    ├── rag/                         ← embedder, retriever, fetcher, web_search, trust
    ├── collectors/                  ← Playwright browser collectors
    ├── normalizer/                  ← rule-based классификация отзывов
    └── web/                         ← FastAPI + React UI + PDF export
```

---

## 🛣 Roadmap

- [x] Deep Research pipeline с agent-loop
- [x] Claim-level verification (anti-hallucination)
- [x] Universal product support (любой банковский продукт без хардкода)
- [x] Govt trust whitelist (cbr.ru / pravo.gov.ru / mil.ru / gosuslugi)
- [x] PDF export с графиками
- [ ] Авторизация (single-user → multi-user)
- [ ] Загрузка собственных PDF из UI
- [ ] Excel-экспорт сравнительных таблиц
- [ ] Snapshot-based diff («что изменилось в условиях по вкладам за месяц»)
- [ ] Telegram/Slack бот-интерфейс
- [ ] Поддержка локальных LLM (vLLM/Ollama) из коробки

---

## 🤝 Контрибьюции

PR'ы welcome. Перед отправкой:
1. `pip install -e ".[dev]"` — поставит pytest + ruff
2. `ruff check src/` — линтер
3. `pytest tests/` — тесты (если есть для твоей области)
4. Опиши **зачем** изменение, не только **что** сделано

---

## 📜 Лицензия

[MIT](LICENSE) — свободное использование с указанием авторства.

## 🙏 Acknowledgments

- [pgvector](https://github.com/pgvector/pgvector) — векторный поиск в Postgres
- [SearXNG](https://github.com/searxng/searxng) — open-source мета-поисковик
- [BGE-M3](https://huggingface.co/BAAI/bge-m3) — multilingual embeddings
- [Fireworks AI](https://fireworks.ai/) — fast inference для open-weights моделей
- [Chart.js](https://www.chartjs.org/) — графики в UI и PDF

---

<div align="center">
Сделано для аудиторов, которые хотят тратить время на анализ, а не на сбор данных.
</div>
