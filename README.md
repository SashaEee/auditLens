<div align="center">

# 🔍 AuditLens

**Deep-Research платформа для внутреннего аудита банковских продуктов**

Многоагентный pipeline + RAG + structured БД. Задаёшь вопрос на русском — получаешь аналитический отчёт со ссылками на первоисточники, графиками и PDF-экспортом за 1–3 минуты.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Postgres 17 + pgvector](https://img.shields.io/badge/postgres-17+pgvector-336791.svg)](https://github.com/pgvector/pgvector)

[Быстрый старт](#-быстрый-старт-5-минут) · [Онбординг разработчика](docs/ONBOARDING.md) · [Документация](docs/) · [Архитектура](docs/ARCHITECTURE.md) · [Troubleshooting](docs/TROUBLESHOOTING.md)

![AuditLens — Обзор](docs/img/01_overview.png)

</div>

---

## 🎯 Для кого

**Аналитики и аудиторы банковского сектора**, которым нужно регулярно сравнивать продукты разных банков, искать данные по ставкам/тарифам/жалобам клиентов и готовить отчёты для руководства. AuditLens заменяет ручной обзвон 5–10 сайтов банков, копирование цифр в Excel и склеивание в Word — на один запрос на русском языке.

**Принцип, на котором построена система:** числа считает детерминированный код, LLM только формулирует. Всё, что модель могла бы выдумать, сверяется с фактами — а графики и сравнительные таблицы строятся вообще без LLM.

---

## ⚡ Что умеет

<table>
<tr>
<td width="50%" valign="top">

### 📰 Обзор — ежедневный брифинг
Утренний выпуск для аудитора: передовица, «пульс дня» из 6 метрик, риск-карточки с механикой «Разобраться» и «Спросить ИИ».

Все числа — детерминированный SQL. **3 LLM-вызова в сутки на всех** — генерится раз в день и кэшируется.

</td>
<td width="50%" valign="top">

### 🔬 Deep Research
Многоагентный pipeline: **Conductor** (план) → волны агентов (researcher / regulatory / market / reviews / ranking) → **Analyst** → **Critic** → Repair.

Агенты пишут находки не в чат, а в типизированный `KnowledgeBundle`. Типичное время — **1–3 мин**.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 🛡 Anti-hallucination
Эшелонированная защита:
- запрет извлекать числа из SERP-сниппетов — только из прочитанной страницы
- **Critic** регуляркой сверяет каждое число с единицей против фактов (допуск 2%)
- вычистка невалидных `[N]`
- **графики и таблицы считаются без LLM** — числа физически не могут галлюцинировать

</td>
<td width="50%" valign="top">

### 📚 Цитаты и trust scoring
Каждое утверждение в отчёте имеет ссылку `[N]` на источник.

Trust-классы: 🟢 регуляторы (cbr.ru, pravo.gov.ru — 0.98), 🟡 банки (0.95), 🟠 агрегаторы (0.65), 🔴 блоги (исключаются).

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 💬 Отзывы — риск-радар
Корпус ~390 тыс. негативных отзывов banki.ru, **21 тема / 145 regex-паттернов** в классах риска compliance / conduct / ops.

Спайк-детект на **медиане + MAD** (устойчиво к самому пику), гео-аномалии per-100k, Сбер vs рынок.

</td>
<td width="50%" valign="top">

### 🕳 Лазейки
Отдельный агент (nanobot-ai) ищет схемы, которыми клиенты и ИП обходят комиссии и лимиты.

Доменная таксономия ~40 схем, маскировка ПД перед отправкой в LLM, находки сохраняются в таблицу с дедупом.

</td>
</tr>
<tr>
<td width="50%" valign="top">

### 📊 Графики и PDF
Автоматическая визуализация числовых сравнений через Chart.js.

PDF-экспорт через Playwright Chromium — A4, embedded шрифты, графики, источники.

</td>
<td width="50%" valign="top">

### 🧠 Universal product support
Работает с **любым** банковским продуктом без хардкода: Conductor сам определяет тему, синонимы, субъектов и нужны ли govt-источники (НПА, ЦБ).

</td>
</tr>
</table>

---

## 🚀 Быстрый старт (5 минут)

### Что нужно

| | Где взять |
|---|---|
| **Python 3.11+** | `brew install python@3.12` (mac) · `sudo apt install python3.12` (Linux/WSL) |
| **Docker Desktop** | [Mac (Apple Silicon)](https://desktop.docker.com/mac/main/arm64/Docker.dmg) · [Mac (Intel)](https://desktop.docker.com/mac/main/amd64/Docker.dmg) · [Windows](https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe) · [Linux](https://docs.docker.com/engine/install/) |
| **Git** | `brew install git` (mac) · `sudo apt install git` (Linux/WSL) |
| **Ключ к LLM** | любой **OpenAI-совместимый** эндпоинт. Для локального старта проще всего [fireworks.ai](https://fireworks.ai/) (бесплатные $15). В проде — Foundation Models Cloud.ru |

### Шаги (TL;DR)

```bash
# 1. Скачать
git clone https://github.com/SashaEee/auditLens.git
cd auditLens

# 2. Один скрипт = всё установлено (Docker, БД, Python deps, миграции)
bash scripts/setup.sh

# 3. Впиши ключ и эндпоинт в .env
open -e .env       # mac (откроет TextEdit)
# или: nano .env   # любая ОС (Ctrl+X для выхода)
#   LLM_BASE_URL=...   LLM_API_KEY=...   LLM_MODEL_NAME=...

# 4. Запусти сервер
source .venv/bin/activate
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Открой [http://127.0.0.1:8000](http://127.0.0.1:8000) → введи вопрос → готово.

> 📖 **Не уверен в командах?** → [docs/SETUP.md](docs/SETUP.md) — пошаговый гайд с разделением по mac/Windows/Linux
> 🔑 **Где взять API-ключ?** → [docs/API_KEYS.md](docs/API_KEYS.md)
> ☁️ **Развернуть в Облаке УВА (Cloud.ru)?** → [docs/DEPLOY_UVA.md](docs/DEPLOY_UVA.md)

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

**Быстрые запросы (без Deep Research):**

```
Топ-10 жалоб клиентов Тинькофф за последний месяц
```

```
Покажи актуальные ставки по вкладам от 1 млн руб на 12 мес
```

---

## 🖼 Скриншоты

### 01 · Обзор — ежедневный брифинг

![Обзор](docs/img/01_overview.png)
*Утренний выпуск: передовица с акцентом, «пульс дня» из 6 детерминированных метрик, риск-карточки с кнопками «Разобраться» и «Спросить ИИ», лента новостей для аудитора.*

### 05 · ИИ-аналитик

![ИИ-аналитик](docs/img/05_ai.png)
*Чат с Deep Research: живой прогресс по стадиям, ход мысли модели, план отчёта, источники с trust-индикаторами.*

### 02–04 · Аналитические вкладки

<table>
<tr>
<td width="50%">

![Рынок](docs/img/02_market.png)
*Рынок: офферы из `v_offer_current` с фильтрами по категории, «vs лидер».*

</td>
<td width="50%">

![Сбер / Рынок](docs/img/03_sber.png)
*Позиция Сбера против медианы рынка по категориям.*

</td>
</tr>
<tr>
<td width="50%">

![Отзывы](docs/img/04_reviews.png)
*Риск-радар: темы с классами риска, спайк-детект, гео-аномалии.*

</td>
<td width="50%">

![База знаний](docs/img/06_knowledge.png)
*База знаний: покрытие по банкам, live-поиск по pgvector (BGE-M3 1024d).*

</td>
</tr>
</table>

### 07 · Лазейки

![Лазейки](docs/img/07_loophole.png)
*Агент ищет схемы обхода комиссий и лимитов; найденное с вердиктом и trust-score попадает в таблицу.*

### 08–10 · Данные

<table>
<tr>
<td width="33%">

![Банки](docs/img/08_banks.png)
*Банки.*

</td>
<td width="33%">

![Источники](docs/img/09_sources.png)
*Источники с trust-индикаторами.*

</td>
<td width="33%">

![Качество](docs/img/10_quality.png)
*Флаги качества данных.*

</td>
</tr>
</table>

---

## 🏗 Архитектура

```mermaid
flowchart LR
    Q[Вопрос] --> C[Conductor<br/>план]
    C --> W{Волны агентов}
    W --> R[researcher xN]
    W --> RG[regulatory]
    W --> MK[market]
    W --> RV[reviews]
    W --> RK[ranking]
    R --> B[(KnowledgeBundle)]
    RG --> B
    MK --> B
    RV --> B
    RK --> B
    B --> A[Analyst<br/>отчёт]
    A --> CR[Critic<br/>сверка чисел]
    CR --> RP[Repair]
    B --> CH[Графики и таблица<br/>БЕЗ LLM]
    RP --> PDF[PDF export]
```

> Полное описание — [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): тиры моделей, бюджеты времени, анти-галлюцинации, специфика Cloud.ru.

**Стек:**

- **Python 3.11+**, FastAPI, SQLAlchemy 2, asyncio, SSE
- **PostgreSQL 17 + pgvector** — семантический поиск (1024d, BGE-M3, HNSW)
- **React 18 без бандлера** — Babel-standalone компилирует JSX в браузере, ни npm, ни webpack
- **Playwright Chromium** — скрейп за антиботами и PDF-экспорт
- **LLM** — любой OpenAI-совместимый эндпоинт. В проде — **Foundation Models Cloud.ru**; пять тиров моделей (fast / smart / reasoning / analyst / insight) с деградационной цепочкой
- **Web search** — SearXNG (в контуре Cloud.ru живы `bing` + `dogpile`, остальные под капчей)

**Модели (прод):**

| Тир | Модель | Где |
|---|---|---|
| fast / smart | `google/gemini-2.5-flash` | навигация агентов, извлечение фактов |
| reasoning / analyst | `google/gemini-3.1-pro-preview` | план, нарратив, критик |
| insight | `anthropic/claude-sonnet-4.6` | «Обзор», «Отзывы» (3 вызова/сутки) |
| — | `openai/gpt-4.1` | мозг агента «Лазейки» |
| фолбэк | `openai/gpt-oss-120b` | внутренняя модель контура |

---

## 📦 Структура репозитория

```
auditlens/
├── README.md                        ← ты здесь
├── LICENSE                          ← MIT
├── pyproject.toml                   ← зависимости
├── .env.example / .env.prod.example ← шаблоны конфига
├── docker-compose.yml               ← Postgres + SearXNG
├── Dockerfile                       ← прод-образ (+ Playwright)
├── docs/
│   ├── SETUP.md                     ← детальная установка
│   ├── ONBOARDING.md                ← онбординг разработчика
│   ├── API_KEYS.md                  ← где брать ключи
│   ├── USAGE.md                     ← примеры вопросов
│   ├── ARCHITECTURE.md              ← как устроена система
│   ├── DEPLOY_UVA.md                ← развёртывание в Облаке УВА
│   ├── TROUBLESHOOTING.md           ← типовые проблемы
│   └── img/                         ← скриншоты для README
├── migrations/                      ← SQL миграции (001–013 + ensure_vector)
├── config/
│   ├── sources.yaml                 ← реестр источников (7 источников, 45 таргетов)
│   └── ca_bundle_combined.pem       ← certifi + Russian Trusted Root
└── src/bank_audit/
    ├── ai/                          ← LLM-утилиты, тиры моделей, clarify
    ├── research/v2/                 ← Deep Research: conductor, агенты, critic
    ├── rag/                         ← embedder, retriever, отзывы, trust
    ├── digest/                      ← «Обзор» — ежедневный брифинг
    ├── loophole/                    ← «Лазейки» — nanobot-агент
    ├── sources/ collectors/         ← адаптеры источников, Playwright
    ├── normalizer/ quality/         ← нормализация (SCD2), контроль качества
    └── web/                         ← FastAPI + React UI + PDF export
```

---

## 🛣 Roadmap

- [x] Deep Research v2 — многоагентный pipeline с Conductor и Critic
- [x] Claim-level verification (anti-hallucination)
- [x] Universal product support (любой банковский продукт без хардкода)
- [x] PDF export с графиками
- [x] «Обзор» — ежедневный брифинг с детерминированным пульсом
- [x] «Лазейки» — агент поиска схем обхода
- [x] Развёртывание в Облаке УВА (Cloud.ru) + Foundation Models
- [ ] Загрузка собственных PDF из UI
- [ ] Excel-экспорт сравнительных таблиц
- [ ] Snapshot-based diff («что изменилось в условиях по вкладам за месяц»)
- [ ] Telegram/Slack бот-интерфейс

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
- [nanobot-ai](https://pypi.org/project/nanobot-ai/) — harness агента «Лазеек»
- [Chart.js](https://www.chartjs.org/) — графики в UI и PDF

---

<div align="center">
Сделано для аудиторов, которые хотят тратить время на анализ, а не на сбор данных.
</div>
