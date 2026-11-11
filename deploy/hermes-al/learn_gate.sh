#!/usr/bin/env bash
# Еженедельная проверка самообучения hermes-al (cron на ВМ, раз в неделю).
#
# Агент сам пишет и правит навыки без ручного одобрения. Страховка — замер:
#   1. два прогона регрессионного набора (auditlens agent-eval), среднее;
#   2. среднее не ниже планки минус DROP → текущие навыки и память становятся
#      «последним хорошим» снимком; планка при этом НЕ опускается, а подняться
#      может только до худшего из двух прогонов;
#   3. среднее ниже → навыки и память откатываются к последнему хорошему снимку
#      (текущие — в архив, не удаляются), агент перезапускается, прогон
#      повторяется для записи в «Пульс». Планка остаётся прежней.
# Лог: ~/hermes-al/gate/gate.log. Личный Hermes владельца не трогается.
#
# Аудит 11.11: раньше после каждого принятого замера планкой становилось новое
# среднее. Допуск был 15 баллов, и за пять недель планка спустилась лесенкой
# 90,0 → 80,0 → 86,7 → 80,0 → 78,3 → 69,7 без единого отката — пока агент написал
# себе 29 навыков. Планку вниз сдвигает только человек: записью в ref_score.
set -euo pipefail
G=~/hermes-al/gate
C=hermes-al
H=/root/.hermes
DROP=${GATE_DROP:-8}        # разброс среднего двух прогонов; больше — уже не шум
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
LOW=$(python3 -c "print(min($S1, $S2))")
REF=$(cat "$G/ref_score" 2>/dev/null || echo "")
log "прогоны: $S1 и $S2, среднее $S (планка ${REF:-нет})"

if [ -z "$REF" ] || python3 -c "import sys; sys.exit(0 if $S >= $REF - $DROP else 1)"; then
  docker exec $C tar czf - -C $H skills memories > "$G/good.tgz.new" && mv "$G/good.tgz.new" "$G/good.tgz"
  # Планка только растёт, и только по худшему из двух прогонов: удачный
  # одиночный прогон не должен на следующей неделе вызвать ложный откат.
  NEW=$(python3 -c "print(max(${REF:-0}, $LOW) if '${REF}' else $S)")
  echo "$NEW" > "$G/ref_score"
  log "принято: снимок навыков обновлён, планка $NEW"
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
log "откат: $S < $REF − $DROP → навыки из снимка, после отката $S2; упавшие — regressed-$TS.tgz; планка прежняя, $REF"
