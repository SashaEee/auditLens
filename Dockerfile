# ════════════════════════════════════════════════════════════════════════════
#  AuditLens — production image для публикации в Магазине приложений Облака УВА.
#
#  Сборка:   docker build -t auditlens:latest .
#  Запуск:   см. docker-compose.prod.yml  и  docs/DEPLOY_UVA.md
#
#  Содержит: FastAPI/uvicorn + Playwright Chromium (рендер SPA-сайтов банков и
#            HTML→PDF экспорт отчётов).
#  НЕ содержит: torch/sentence-transformers (в проде EMBEDDING_MODE=api → bge-m3
#            через Foundation Models, ~2.5 ГБ экономии), Postgres (managed у ОАИТ),
#            секреты (инжектятся из Infisical/env в рантайме, .env в образ не кладём).
# ════════════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000

# Системные пакеты:
#   postgresql-client — накат миграций (entrypoint migrate);
#   ca-certificates   — TLS к Foundation Models / источникам;
#   curl              — HEALTHCHECK;
#   fonts-*           — кириллица в PDF-экспорте (Chromium рендерит отчёт).
RUN apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client \
        ca-certificates \
        curl \
        fonts-dejavu \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) СНАЧАЛА только зависимости — слой не зависит от кода и переживает любые
#    правки src. Раньше `COPY src` стоял ДО установки: каждая правка одной
#    строки Python инвалидировала слой, и заново ставились все пакеты плюс
#    Chromium — сборка на 10-15 минут вместо секунд.
#    Пустой пакет-заглушка нужен, чтобы `pip install .` отработал без исходников.
COPY pyproject.toml README.md ./
RUN mkdir -p src/bank_audit && touch src/bank_audit/__init__.py \
    && pip install . && pip uninstall -y auditlens bank-audit 2>/dev/null || true

# 2) Chromium + системные libs для него (отдельный слой — кэшируется независимо).
#    Браузер кладётся в /ms-playwright (PLAYWRIGHT_BROWSERS_PATH) и делается читаемым
#    для non-root пользователя.
RUN playwright install --with-deps chromium \
    && chmod -R a+rx /ms-playwright

# 3) Только теперь код: правка Python пересобирает ТОЛЬКО этот слой и лёгкую
#    editable-установку — секунды вместо минут. --no-deps: зависимости уже стоят.
COPY src ./src
RUN pip install -e . --no-deps

# 4) Конфиги (settings.yaml, sources.yaml, CA-сертификаты Минцифры), миграции, entrypoint.
COPY config ./config
COPY migrations ./migrations
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 5) Non-root пользователь + папка эфемерных артефактов (для постоянства смонтировать
#    volume на /app/workspace или вынести выгрузки в OBS).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/workspace \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Liveness без БД — /healthz отдаёт 200 пока процесс жив (БД проверяет /readyz).
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${APP_PORT}/healthz" || exit 1

# serve (default) — uvicorn 0.0.0.0; migrate — накатить миграции и выйти.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
