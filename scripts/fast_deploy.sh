#!/usr/bin/env bash
# Быстрый деплой правок Python/статики на прод — секунды вместо пересборки.
#
# Пакет установлен editable (pip install -e .), поэтому контейнер читает код
# прямо из /app/src: чтобы обновить приложение, достаточно заменить файлы и
# перезапустить процесс. Полная пересборка образа нужна ТОЛЬКО когда меняются
# зависимости (pyproject.toml), Dockerfile или системные пакеты.
#
#   bash scripts/fast_deploy.sh                 # весь src + статика
#   bash scripts/fast_deploy.sh src/bank_audit/ai/llm_utils.py   # точечно
set -euo pipefail

HOST="${AUDITLENS_HOST:-amzenkovskiy-2127124@87.242.123.218}"
KEY="${AUDITLENS_KEY:-$HOME/.ssh/id_ed25519_uva}"
SSH=(ssh -i "$KEY" -o ConnectTimeout=10 -o BatchMode=yes "$HOST")

TARGETS=("$@")
[ ${#TARGETS[@]} -eq 0 ] && TARGETS=(src)

echo "→ пакую: ${TARGETS[*]}"
tar czf /tmp/fast_deploy.tgz "${TARGETS[@]}"
scp -i "$KEY" -o BatchMode=yes /tmp/fast_deploy.tgz "$HOST":~/fast_deploy.tgz >/dev/null

"${SSH[@]}" 'set -e
  cd ~/auditlens && tar xzf ~/fast_deploy.tgz
  # копируем в контейнер и перезапускаем процесс (образ не трогаем)
  docker cp src auditlens-app:/app/ >/dev/null
  docker restart auditlens-app >/dev/null
  for i in $(seq 1 20); do
    sleep 2
    code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/ || true)
    [ "$code" = "200" ] && { echo "✓ приложение отвечает 200 (попытка $i)"; exit 0; }
  done
  echo "✗ приложение не поднялось за 40 с — смотри docker logs auditlens-app"; exit 1'
echo "→ готово"
