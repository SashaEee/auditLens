#!/usr/bin/env bash
# Выкатка настроек hermes-al: SOUL, стартовые навыки, конфиг, память.
# Запускать на ВМ из каталога, куда скопирован deploy/hermes-al (~/hermes-al/repo).
# Личный Hermes владельца (~/.hermes хоста) НЕ трогается: всё — внутри контейнера hermes-al.
#
#   bash sync.sh            — выкатка + перезапуск + проверка
#   bash sync.sh --dry-run  — только показать, что будет сделано
set -euo pipefail
cd "$(dirname "$0")"
C=hermes-al
H=/root/.hermes
DRY=0; [ "${1:-}" = "--dry-run" ] && DRY=1
run() { if [ $DRY = 1 ]; then echo "DRY: $*"; else "$@"; fi; }

# Навыки, которые агент написал сам и которые учат вредному или неверному:
# обход защиты сайтов (r.jina.ai, VPN, Tor), выдуманные номера актов,
# несуществующие таблицы, записи одной сессии. Уходят в архив куратора —
# оттуда восстанавливаются (hermes curator / перенос каталога обратно).
ARCHIVE=(audit/audit-deposit-product-info audit/audit-deposit-regulation
         audit/audit-insurance-product-info audit-website-tech-stack
         audit/audit-collector-agency-rating audit/credit-donor-research
         audit/audit-education-credit-limits audit/audit-education-credit-requirements
         audit/audit-regulatory-fines audit/audit-deposit-card-risk
         audit/audit-reviews-general audit/autocredit-reviews
         audit/audit-psk-market audit/audit-psk-recent-changes
         news reviews-api sber-tone)

TS=$(date +%Y%m%d-%H%M%S)
mkdir -p ~/hermes-al/backups
echo "1) резервная копия → ~/hermes-al/backups/hermes-al-$TS.tgz"
run sh -c "docker exec $C tar czf - -C $H config.yaml SOUL.md skills memories > ~/hermes-al/backups/hermes-al-$TS.tgz"

echo "2) конфиг: ключ модели — из действующего конфига контейнера"
KEY=$(docker exec $C python3 -c "import yaml;print(yaml.safe_load(open('$H/config.yaml'))['model']['api_key'])")
[ -n "$KEY" ] || { echo "нет api_key в текущем конфиге"; exit 1; }
TMP=$(mktemp -d)
python3 - "$KEY" "$TMP/config.yaml" <<'PY'
import sys
key, dst = sys.argv[1], sys.argv[2]
open(dst, "w").write(open("config.yaml").read().replace("__LLM_API_KEY__", key))
PY
run docker cp "$TMP/config.yaml" $C:$H/config.yaml

echo "3) ключ MCP в окружении Hermes (из ~/auditlens/.env, значение не печатается)"
MK=$(grep -E '^AGENT_MCP_KEY=' ~/auditlens/.env | head -1 | cut -d= -f2-)
[ -n "$MK" ] || { echo "в ~/auditlens/.env нет AGENT_MCP_KEY"; exit 1; }
if docker exec $C grep -q '^AGENT_MCP_KEY=' $H/.env; then
  echo "   уже задан"
else
  run sh -c "printf 'AGENT_MCP_KEY=%s\n' '$MK' | docker exec -i $C sh -c 'cat >> $H/.env'"
fi

echo "4) SOUL, стартовые навыки, память"
run docker cp SOUL.md $C:$H/SOUL.md
for d in skills/*/; do
  n=$(basename "$d")
  run docker exec $C rm -rf "$H/skills/$n"
  run docker cp "skills/$n" "$C:$H/skills/$n"
done
run docker cp memories/MEMORY.md $C:$H/memories/MEMORY.md

echo "5) архив вредных навыков → skills/.archive/"
run docker exec $C mkdir -p $H/skills/.archive
for s in "${ARCHIVE[@]}"; do
  if docker exec $C test -d "$H/skills/$s"; then
    run docker exec $C sh -c "rm -rf '$H/skills/.archive/$(basename "$s")' && mv '$H/skills/$s' '$H/skills/.archive/'"
    echo "   $s"
  fi
done

echo "6) проверка: размеры файлов в контейнере совпадают с репо"
bad=0
for f in SOUL.md memories/MEMORY.md skills/*/SKILL.md; do
  a=$(wc -c < "$f"); b=$(docker exec $C sh -c "wc -c < $H/$f" 2>/dev/null || echo 0)
  [ "$a" = "$b" ] || { echo "   РАСХОЖДЕНИЕ $f: $a ≠ $b"; bad=1; }
done
[ $DRY = 1 ] || [ $bad = 0 ] || exit 1
rm -rf "$TMP"

echo "7) перезапуск и проверка"
run docker restart $C >/dev/null
[ $DRY = 1 ] && exit 0
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8642/health >/dev/null && break; sleep 2
done
AK=$(docker exec $C sh -c 'echo $API_SERVER_KEY')
curl -s -H "Authorization: Bearer $AK" http://127.0.0.1:8642/v1/toolsets \
  | python3 -c "import sys,json;d=json.load(sys.stdin);d=d.get('data',d) if isinstance(d,dict) else d;print('   наборы:', ', '.join(t['name'] for t in d if t.get('enabled')))"
curl -s -H "Authorization: Bearer $AK" http://127.0.0.1:8642/v1/skills \
  | python3 -c "import sys,json;d=json.load(sys.stdin);d=d.get('data',d) if isinstance(d,dict) else d;print('   навыков:', len(d), '·', ', '.join(s['name'] for s in d if s['name'].startswith('auditlens')))"
echo "готово; откат: docker exec -i $C tar xzf - -C $H < ~/hermes-al/backups/hermes-al-$TS.tgz && docker restart $C"
