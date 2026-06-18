# 📦 Установка AuditLens — пошаговое руководство

Этот гайд написан для **аналитиков и пользователей без опыта разработки**. Если ты никогда не открывал терминал — ничего страшного, тут всё разжёвано. Установка займёт **15-30 минут** (большую часть времени будут качаться файлы, тебе придётся только нажимать Enter).

---

## 📋 Что мы установим

```
1. Python   — язык программирования, на котором написан AuditLens
2. Docker   — программа, которая запускает базу данных в «коробочке»
3. Git      — программа для скачивания кода с GitHub
4. AuditLens — собственно сам инструмент
5. API-ключ Fireworks AI — чтобы LLM работал
```

Все программы **бесплатные** и **легальные**.

---

## Шаг 0 — Открой терминал

**Терминал** — это окно, куда ты будешь вписывать команды. Не пугайся: всё что нужно — копировать команды отсюда и нажимать Enter.

| Твоя ОС | Как открыть |
|---|---|
| **macOS** | Cmd+Space → введи «Terminal» → Enter |
| **Windows** | Win+R → введи `wt` или `cmd` → Enter (для WSL см. ниже) |
| **Linux** | Ctrl+Alt+T |

> 💡 **Windows-юзеры:** AuditLens лучше запускать в **WSL2 (Linux внутри Windows)**, чем в нативной Windows. Если у тебя Windows 11 — открой PowerShell от администратора и выполни:
> ```powershell
> wsl --install
> ```
> После перезагрузки откроется Ubuntu — все команды ниже выполняй там, а НЕ в обычной PowerShell. Подробнее: [docs.microsoft.com/wsl/install](https://learn.microsoft.com/ru-ru/windows/wsl/install).

---

## Шаг 1 — Установи Python 3.11+

Python — основа AuditLens. Проверь, есть ли он уже:

```bash
python3 --version
```

**Если видишь** `Python 3.11.x` или выше — пропускай этот шаг.

**Если видишь** «command not found» или версию ниже 3.11 — установи:

### macOS
1. Если у тебя **нет Homebrew** — установи (это менеджер пакетов для Mac):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   После установки в терминале появится подсказка добавить brew в PATH — следуй ей (обычно две команды `eval "$(...)"`).

2. Поставь Python:
   ```bash
   brew install python@3.12
   ```

3. Проверь:
   ```bash
   python3 --version
   ```
   Должно показать `Python 3.12.x`.

### Windows (WSL2 / Ubuntu)
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
python3 --version
```

### Linux (Ubuntu/Debian)
То же что для WSL:
```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip
python3 --version
```

> ⚠ **Не используй Python с python.org на macOS** — он ставит в нестандартное место и потом будут проблемы с `pip install`. Всегда через `brew` на маке.

---

## Шаг 2 — Установи Docker Desktop

Docker — это программа, которая запускает базу данных в изолированной «коробочке» (контейнере). Не нужно ничего настраивать вручную — Docker сам поднимет PostgreSQL и SearXNG за тебя.

### macOS

1. Скачай Docker Desktop с официального сайта:
   - **Apple Silicon (M1/M2/M3/M4):** [Docker for Mac (Apple chip)](https://desktop.docker.com/mac/main/arm64/Docker.dmg)
   - **Intel Mac:** [Docker for Mac (Intel chip)](https://desktop.docker.com/mac/main/amd64/Docker.dmg)
   - Не знаешь какой у тебя процессор? → Apple menu → About This Mac → строка «Chip». Если M-что-то — Apple Silicon. Если «Intel» — Intel.

2. Открой `.dmg` → перетащи Docker в Applications.
3. Запусти Docker (Cmd+Space → «Docker» → Enter). При первом запуске попросит права администратора — соглашайся.
4. Дождись пока в строке меню (вверху справа) появится зелёный значок 🐳. Это значит Docker готов.

### Windows (WSL2)

1. Скачай: [Docker Desktop for Windows](https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe)
2. Запусти установщик. На экране «Configuration» **обязательно поставь галочку** «Use WSL 2 instead of Hyper-V» (она обычно стоит по умолчанию).
3. После установки перезагрузи компьютер.
4. Запусти Docker Desktop. Принять условия → дождись зелёного 🐳 в трее.

### Linux (Ubuntu/Debian)

```bash
# Установка Docker Engine + Compose v2
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Чтобы запускать docker без sudo:
sudo usermod -aG docker $USER
# ⚠ Перелогинься (или перезагрузи систему) чтобы изменения применились
```

### Проверка

В терминале:
```bash
docker --version
docker compose version
```

Должно показать что-то типа:
```
Docker version 27.x
Docker Compose version v2.30.x
```

> 💡 **Docker Desktop платный только для крупных компаний** (>250 человек или >$10M выручки). Для личного использования, маленьких команд и open-source — бесплатен.

---

## Шаг 3 — Установи Git

Git — программа для скачивания кода. Проверь:
```bash
git --version
```

Если показывает `git version 2.x` — пропускай шаг.

### macOS
```bash
brew install git
```
Или Git уже придёт с Xcode Command Line Tools (Mac предложит установить при первом вызове `git`).

### Windows (WSL2)
```bash
sudo apt install -y git
```

### Linux
```bash
sudo apt install -y git
```

---

## Шаг 4 — Скачай AuditLens

Выбери куда положить — например, в домашнюю папку:

```bash
cd ~                          # перейти в домашнюю папку
git clone https://github.com/SashaEee/auditLens.git
cd auditLens                  # перейти в скачанную папку
```

После `cd auditLens` ты находишься внутри проекта. Команды ниже выполняй именно отсюда.

> 💡 Альтернатива через визуальный клиент: [GitHub Desktop](https://desktop.github.com/) → Clone Repository → вставь URL `https://github.com/SashaEee/auditLens.git` → выбери папку. Потом всё равно открой терминал в этой папке.

---

## Шаг 5 — Получи бесплатный API-ключ Fireworks AI

AuditLens использует языковую модель (LLM) для понимания вопросов и генерации отчётов. Мы будем использовать **Fireworks AI**, потому что:
- ✅ **$15 кредитов бесплатно** при регистрации (≈ 30-50 deep-research'ей)
- ✅ Работает из РФ **без VPN**
- ✅ Не требует подтверждать карту при регистрации

### Пошагово

1. Открой [https://fireworks.ai/](https://fireworks.ai/) → нажми **Sign Up** в правом верхнем углу.
2. Зарегистрируйся (email + пароль, либо Google/GitHub).
3. На дашборде ты сразу увидишь баланс **$15.00** — это твои бесплатные кредиты.
4. Перейди в раздел [API Keys](https://fireworks.ai/account/api-keys).
5. Нажми **Create API Key** → дай имя (например `auditlens-dev`) → **Create**.
6. **СРАЗУ скопируй ключ** — он показывается только один раз! Формат: `fw_<длинная_строка_букв_и_цифр>`.
7. Сохрани его в надёжное место (Notes / 1Password / просто текстовый файл) на случай если понадобится снова.

> 📖 Если хочешь подробнее про другие LLM-провайдеры (Anthropic Claude, локальные модели) — [docs/API_KEYS.md](API_KEYS.md).

---

## Шаг 6 — Запусти автоустановщик

Из папки `auditLens` (куда ты перешёл на шаге 4) запусти:

```bash
bash scripts/setup.sh
```

Скрипт сделает за тебя:
1. ✅ Проверит что Python 3.11+ и Docker установлены
2. ✅ Создаст файл `.env` из шаблона
3. ✅ Поднимет PostgreSQL 16 + pgvector + SearXNG через Docker
4. ✅ Создаст виртуальное окружение Python в `.venv/`
5. ✅ Скачает и установит все Python-зависимости (~500 МБ)
6. ✅ Скачает Playwright Chromium (~150 МБ — нужен для PDF-экспорта)
7. ✅ Применит миграции базы данных (создаст таблицы)
8. ✅ Проверит что всё работает

**Это займёт 5-15 минут** в зависимости от скорости интернета. Будут долгие моменты молчания пока скачиваются файлы — это нормально, **не закрывай терминал**.

В конце увидишь:
```
═══════════════════════════════════════════════════════════
✅ Установка завершена!
═══════════════════════════════════════════════════════════
```

Если в процессе что-то упало — смотри [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Шаг 7 — Впиши API-ключ в файл `.env`

Скрипт создал файл `.env` с шаблоном. Открой его в любом редакторе и впиши свой ключ Fireworks (из шага 5).

### Как открыть `.env`

| Способ | Команда / действие |
|---|---|
| **Текстовый редактор (mac)** | `open -e .env` — откроет в TextEdit |
| **VS Code (любая ОС)** | `code .env` (если установлен) |
| **nano (терминал)** | `nano .env` (Ctrl+X для выхода, Y для сохранения) |
| **Notepad (Windows)** | `notepad.exe .env` |

### Что заменить

Найди строку:
```
LLM_API_KEY=fw_REPLACE_WITH_YOUR_KEY
```

Замени `fw_REPLACE_WITH_YOUR_KEY` на твой реальный ключ из Fireworks. Должно стать так:
```
LLM_API_KEY=fw_aBcDeFgHiJkLmNoPqRsTuVwXyZ1234567890
```

Сохрани файл и закрой редактор.

> ⚠ Файл `.env` **не попадёт на GitHub** — он в `.gitignore`. Твой ключ останется приватным.

---

## Шаг 8 — Запусти AuditLens

```bash
source .venv/bin/activate
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

Первая команда «активирует» виртуальное окружение (увидишь `(.venv)` в начале строки терминала — это значит активно).

Вторая — стартует веб-сервер. Через 3-5 секунд увидишь:
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000
```

**Не закрывай это окно** — пока оно открыто, сервер работает.

---

## Шаг 9 — Открой AuditLens в браузере

В браузере перейди на: **[http://127.0.0.1:8000](http://127.0.0.1:8000)**

Увидишь главный экран:

![AuditLens — главный экран](img/01_dashboard.png)

Слева в сайдбаре нажми **«ИИ-аналитик»** → откроется чат. Задай вопрос:

```
Сравни вклады для физлиц от 1 млн руб на 6 мес — Сбер, ВТБ, Альфа-банк
```

Включи **🔬 Deep Research** (кнопка слева от поля ввода) → нажми Enter.

Подожди 1-3 минуты — получишь полноценный отчёт со ссылками на источники, графиками и кнопкой «Download PDF».

> 📖 Больше примеров вопросов → [docs/USAGE.md](USAGE.md)

---

## ✅ Финальная проверка — всё ли работает

В **новом** окне терминала (чтобы не закрывать сервер) запусти:
```bash
cd ~/auditLens
bash scripts/setup.sh check
```

Должно показать все ✅:
```
✅ python3 найден: Python 3.12.x
✅ docker найден: Docker version 27.x
✅ Docker Compose v2 найден
```

Проверка БД:
```bash
docker compose ps
```
Должны быть запущены 2 контейнера:
- `auditlens-postgres` (UP, healthy)
- `auditlens-searxng` (UP)

---

## 🔄 Как обновить AuditLens до новой версии

Когда выйдет обновление:
```bash
cd ~/auditLens
git pull                          # скачать новые файлы с GitHub
source .venv/bin/activate
pip install -e '.[local-embeddings]' --upgrade   # обновить зависимости (с локальными эмбеддингами)
bash scripts/setup.sh init-db     # применить новые миграции
# Перезапусти сервер (Ctrl+C в окне где он запущен → команда uvicorn заново)
```

---

## 🛑 Как остановить AuditLens

1. **Сервер:** в окне терминала с uvicorn нажми **Ctrl+C** (на Mac тоже Ctrl, не Cmd!).
2. **База данных и SearXNG:** в папке `auditLens`:
   ```bash
   docker compose down
   ```
   Данные **сохранятся** (база, источники, отзывы). При следующем `docker compose up -d` всё на месте.

3. **Если хочешь УДАЛИТЬ всё** (база, индекс):
   ```bash
   docker compose down -v   # ⚠ удалит данные навсегда
   ```

---

## 🆘 Если что-то не получилось

1. Не паникуй — это нормально для первого раза.
2. Скопируй полностью текст ошибки.
3. Открой [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md) — там разобраны типовые ситуации (порт занят, нет pgvector, БД не подключается, и т.д.).
4. Если в трабблшутинге нет твоей проблемы — создай Issue на GitHub: [github.com/SashaEee/auditLens/issues](https://github.com/SashaEee/auditLens/issues) с:
   - текстом ошибки
   - твоей ОС и версией (`uname -a` на mac/linux, `systeminfo` на Windows)
   - выводом `bash scripts/setup.sh check`

---

## 🔧 Альтернатива: установка без Docker (для продвинутых)

Если по какой-то причине Docker не подходит (нет прав, корпоративная политика и т.п.) — можно поставить PostgreSQL и SearXNG нативно. Это сложнее, требует понимания админки.

<details>
<summary>👉 Развернуть инструкцию по нативной установке</summary>

### Установи PostgreSQL 16 + pgvector

**macOS:**
```bash
brew install postgresql@16
brew services start postgresql@16
brew install pgvector
```

**Ubuntu/Debian:**
```bash
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo systemctl enable --now postgresql
```

### Создай БД и пользователя
```bash
sudo -u postgres psql <<EOF
CREATE USER audit WITH PASSWORD 'audit';
CREATE DATABASE bank_audit OWNER audit;
\c bank_audit
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOF
```

### Python-окружение
```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -e '.[local-embeddings]'   # extra нужен для EMBEDDING_MODE=local (torch+sentence-transformers)
playwright install chromium
```

### Применить миграции
```bash
cp .env.example .env
# Заполни LLM_API_KEY в .env

DSN="postgresql://audit:audit@localhost:5432/bank_audit"
for f in migrations/*.sql; do psql "$DSN" -f "$f"; done
psql "$DSN" -f src/bank_audit/analytics/views.sql
```

### Запустить сервер
```bash
uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000
```

### Опционально: SearXNG
SearXNG лучше всё-таки через Docker (его очень тяжело поставить нативно). Если совсем без Docker — система fallback'нется на DuckDuckGo/Yandex, но качество поиска будет хуже.

</details>

---

## 📚 Что дальше

- **[Как пользоваться (примеры вопросов) →](USAGE.md)** — десятки готовых формулировок для Deep Research
- **[Как устроен pipeline →](ARCHITECTURE.md)** — для тех, кто хочет понять «магию» под капотом
- **[Проблемы и решения →](TROUBLESHOOTING.md)** — типовые ситуации

---

## 📋 Чек-лист новичка

Перед тем как считать установку успешной, у тебя должно быть:

- [ ] `python3 --version` показывает 3.11+
- [ ] `docker --version` работает, в трее зелёный 🐳
- [ ] `git --version` работает
- [ ] Папка `auditLens` склонирована
- [ ] Файл `.env` создан и в нём прописан **твой реальный** `LLM_API_KEY` (не `fw_REPLACE_WITH_YOUR_KEY`)
- [ ] `docker compose ps` показывает `auditlens-postgres` и `auditlens-searxng` в статусе UP
- [ ] Сервер запущен (`uvicorn ...` в одном окне терминала)
- [ ] [http://127.0.0.1:8000](http://127.0.0.1:8000) открывается в браузере
- [ ] В разделе «ИИ-аналитик» можно ввести вопрос и получить ответ

Если все пункты ✅ — поздравляю, AuditLens готов к работе!
