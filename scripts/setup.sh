#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  AuditLens — автоматический установщик
#
#  Использование:
#      bash scripts/setup.sh            # полная установка (с нуля)
#      bash scripts/setup.sh init-db    # только применить миграции
#      bash scripts/setup.sh check      # проверить готовность окружения
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Цвета ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info()    { echo -e "${BLUE}ℹ️  $*${NC}"; }
ok()      { echo -e "${GREEN}✅ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠️  $*${NC}"; }
error()   { echo -e "${RED}❌ $*${NC}" >&2; }

# ── Корень проекта ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

# ── Проверка системных зависимостей ──────────────────────────────────────
check_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        error "$1 не установлен. $2"
        return 1
    fi
    ok "$1 найден: $($1 --version 2>&1 | head -1)"
}

check_prereqs() {
    info "Проверка системных зависимостей…"
    local fail=0
    check_command python3 "Поставь Python 3.11+: https://www.python.org/" || fail=1
    check_command docker   "Поставь Docker Desktop: https://www.docker.com/products/docker-desktop/" || fail=1
    [ $fail -eq 1 ] && { error "Установи недостающие зависимости и повтори запуск."; exit 1; }

    # Версия Python
    PYV=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    if [[ "$(printf '3.11\n%s' "$PYV" | sort -V | head -1)" != "3.11" ]]; then
        error "Python 3.11+ требуется. У тебя: $PYV"
        exit 1
    fi
    ok "Python версия OK: $PYV"

    # Docker Compose v2 (встроен в новые версии Docker)
    if ! docker compose version >/dev/null 2>&1; then
        error "Docker Compose v2 не найден. Обнови Docker Desktop до последней версии."
        exit 1
    fi
    ok "Docker Compose v2 найден"
}

# ── .env ─────────────────────────────────────────────────────────────────
setup_env() {
    if [ -f .env ]; then
        warn ".env уже существует — пропускаю копирование"
    else
        cp .env.example .env
        ok "Создан .env из шаблона"
        warn "‼️  ОТКРОЙ .env И ЗАПОЛНИ LLM_API_KEY (получить → docs/API_KEYS.md)"
    fi
}

# ── Virtualenv + зависимости ─────────────────────────────────────────────
setup_venv() {
    if [ ! -d .venv ]; then
        info "Создаю виртуальное окружение в .venv/"
        python3 -m venv .venv
    fi
    # shellcheck disable=SC1091
    source .venv/bin/activate
    info "Устанавливаю Python-зависимости (займёт 3-7 минут, скачаем ~2GB на ML-модели)…"
    pip install --upgrade pip wheel >/dev/null
    # Локалка по умолчанию EMBEDDING_MODE=local → нужны torch+sentence-transformers
    # (они в extra local-embeddings). В прод-контейнере ставится БЕЗ extra (EMBEDDING_MODE=api).
    pip install -e '.[local-embeddings]' >/dev/null
    ok "Зависимости установлены"

    info "Устанавливаю Playwright Chromium (для PDF-экспорта и сложных страниц)…"
    playwright install chromium >/dev/null 2>&1 || warn "playwright install chromium упал — попробуй вручную"
    ok "Playwright готов"
}

# ── Docker (PostgreSQL + SearXNG) ────────────────────────────────────────
setup_docker() {
    info "Поднимаю PostgreSQL + SearXNG через docker compose…"
    docker compose up -d 2>&1 | tail -10
    info "Жду пока Postgres станет healthy…"
    for i in {1..30}; do
        if docker compose exec -T postgres pg_isready -U audit -d bank_audit >/dev/null 2>&1; then
            ok "Postgres готов"
            return 0
        fi
        sleep 2
    done
    error "Postgres не поднялся за 60s. Логи контейнера (последние 30 строк):"
    docker compose logs --tail=30 postgres
    echo
    warn "Типовые причины:"
    warn "  • Порт 5432 занят локальным postgres: brew services stop postgresql@16 (mac)"
    warn "  • Контейнер с конфликтом локали — обнови репо: git pull"
    warn "  • Сломан volume — пересоздай: docker compose down -v && bash scripts/setup.sh"
    exit 1
}

# ── Миграции ─────────────────────────────────────────────────────────────
init_db() {
    info "Применяю миграции…"
    DSN="${DATABASE_URL:-postgresql://audit:audit@localhost:5432/bank_audit}"
    # Превратим SQLA-DSN (postgresql+psycopg://...) в обычный psql DSN
    PSQL_DSN="${DSN/postgresql+psycopg:/postgresql:}"

    # Используем психопг от venv (если установлен) или docker exec
    apply_sql() {
        local f="$1"
        if [ -f "$f" ]; then
            info "  → $f"
            docker compose exec -T postgres \
                psql -U audit -d bank_audit -v ON_ERROR_STOP=1 -f - < "$f" >/dev/null
        fi
    }

    for migration in migrations/*.sql; do
        apply_sql "$migration"
    done

    if [ -f src/bank_audit/analytics/views.sql ]; then
        apply_sql src/bank_audit/analytics/views.sql
    fi

    ok "Все миграции применены"
}

# ── Финальная проверка ───────────────────────────────────────────────────
final_check() {
    info "Финальная проверка…"
    # shellcheck disable=SC1091
    source .venv/bin/activate
    python3 -c "from bank_audit import db; db.session().__enter__().execute(__import__('sqlalchemy').text('SELECT 1'))" \
        && ok "Подключение к БД работает" \
        || { error "Не удаётся подключиться к БД из Python"; exit 1; }

    if grep -q "fw_REPLACE_WITH_YOUR_KEY" .env 2>/dev/null; then
        warn "В .env остался placeholder LLM_API_KEY=fw_REPLACE_WITH_YOUR_KEY"
        warn "Получи ключ Fireworks: https://fireworks.ai/ (бесплатные \$15)"
        warn "Подробная инструкция: docs/API_KEYS.md"
    else
        ok "LLM_API_KEY заполнен"
    fi
}

# ── Меню ─────────────────────────────────────────────────────────────────
case "${1:-all}" in
    check)
        check_prereqs
        ;;
    init-db)
        init_db
        ;;
    docker)
        setup_docker
        ;;
    venv)
        setup_venv
        ;;
    all|"")
        echo "═══════════════════════════════════════════════════════════"
        echo "  AuditLens — установка с нуля"
        echo "═══════════════════════════════════════════════════════════"
        echo
        check_prereqs
        setup_env
        setup_docker
        setup_venv
        init_db
        final_check
        echo
        echo "═══════════════════════════════════════════════════════════"
        ok "Установка завершена!"
        echo "═══════════════════════════════════════════════════════════"
        echo
        echo "Запусти приложение:"
        echo "    source .venv/bin/activate"
        echo "    uvicorn bank_audit.web.app:app --host 127.0.0.1 --port 8000"
        echo
        echo "Открой в браузере: http://127.0.0.1:8000"
        ;;
    *)
        error "Неизвестная команда: $1"
        echo "Использование: $0 [all|check|init-db|docker|venv]"
        exit 1
        ;;
esac
