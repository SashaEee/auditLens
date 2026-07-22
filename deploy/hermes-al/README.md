# hermes-al — ОТДЕЛЬНЫЙ Hermes-инстанс для «Быстрого» режима AuditLens

НЕ имеет отношения к личному Hermes владельца (~/.hermes на хосте) — собственный
контейнер, свой дом в volume hermes_al_home, свой порт 127.0.0.1:8642.

Сборка/запуск на VM (~/hermes-al):
  docker build -t hermes-al .
  docker run -d --name hermes-al --network host --restart unless-stopped \
    --env-file env.container -v hermes_al_home:/root/.hermes -v hermes_al_ws:/root/workspace hermes-al
После первого старта: docker cp config.yaml/SOUL.md/skills/alsql внутрь (см. историю деплоя).
Приложение ходит через ai/hermes_quick.py (env: QUICK_ENGINE, HERMES_API_URL, HERMES_API_KEY).
alsql: SELECT/INSERT/UPDATE свободно; DROP/TRUNCATE/DELETE/ALTER — отказ (правило владельца).
