#!/usr/bin/env bash
# Еженедельная проверка самообучения hermes-al (cron на ВМ, раз в неделю).
#
# Агент сам пишет и правит навыки без ручного одобрения. Страховка — замер:
#   1. два прогона регрессионного набора (auditlens agent-eval), среднее;
#   2. качество не ниже эталона минус DROP → текущие навыки и память становятся
#      «последним хорошим» снимком, эталон = этот итог;
#   3. качество упало сильнее → навыки и память откатываются к последнему
#      хорошему снимку (текущие — в архив, не удаляются), агент перезапускается,
#      прогон повторяется для записи в «Пульс».
# Лог: ~/hermes-al/gate/gate.log. Личный Hermes владельца не трогается.
set -euo pipefail
G=~/hermes-al/gate
C=hermes-al
H=/root/.hermes
DROP=${GATE_DROP:-15}
mkdir -p "$G"
log() { echo "$(date '+%F %T') $*" >> "$G/gate.log"; }

score() {
  docker exec auditlens-app auditlens agent-eval --trigger "$1" --json 2>/dev/null | tail -1 \
    | python3 -c "import sys,json;print(json.load(sys.stdin).get('score') or 0)"
}

# Два прогона и среднее: у одной модели разброс между прогонами ~10 баллов
# (один-два кейса), одиночный замер откатывал бы навыки из-за шума.
S1=$(score gate); S2=$(score gate)
S=$(python3 -c "print(round(($S1 + $S2) / 2, 1))")
REF=$(cat "$G/ref_score" 2>/dev/null || echo "")
log "прогоны: $S1 и $S2, среднее $S (эталон ${REF:-нет})"

if [ -z "$REF" ] || python3 -c "import sys; sys.exit(0 if $S >= $REF - $DROP else 1)"; then
  docker exec $C tar czf - -C $H skills memories > "$G/good.tgz.new" && mv "$G/good.tgz.new" "$G/good.tgz"
  echo "$S" > "$G/ref_score"
  log "принято: снимок навыков обновлён, эталон $S"
  exit 0
fi

if [ ! -s "$G/good.tgz" ]; then
  log "качество упало ($S < $REF − $DROP), но хорошего снимка нет — только отметка"
  exit 0
fi
TS=$(date +%Y%m%d-%H%M%S)
docker exec $C tar czf - -C $H skills memories > "$G/regressed-$TS.tgz"
docker exec $C sh -c "rm -rf $H/skills $H/memories"
docker exec -i $C tar xzf - -C $H < "$G/good.tgz"
docker restart $C >/dev/null
sleep 20
S2=$(score gate-rollback)
log "откат: $S < $REF − $DROP → навыки из снимка, после отката $S2; упавшие — regressed-$TS.tgz"
