#!/usr/bin/env bash
# Сквозная проверка Решения на работающем стенде:
#   двойники в Ditto -> телеметрия симулятора доходит до Ditto -> Telegraf пишет
#   InfluxDB -> дашборды Grafana подключены -> план горизонта отдаёт живое
#   состояние -> сценарий «что если» проходит через MQTT и виден в двойнике.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
set -a; . "$ROOT/deploy/compose/.env"; set +a
H=127.0.0.1
DITTO="http://$H:$DITTO_PORT"
GRAFANA="http://$H:$GRAFANA_PORT"
MAP="http://$H:${MINE_PORT:-8095}"
NS="org.openegiz.zhezkazgan"
FAILED=0
pass() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }
retry() { local n="$1"; shift; for _ in $(seq 1 "$n"); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 3; done; return 1; }

echo "Двойники"
count=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/search/things/count?namespaces=$NS" || echo 0)
if [ "${count:-0}" -ge 160 ]; then pass "$count двойников в пространстве имён $NS"; else fail "только ${count:-0} двойников в $NS (ожидается >= 160)"; fi
for t in east-mine fan-main panel-1 k1-11 c1-11 aw-main-12 agk-p1 crew-1 pdm-01 shift-plan hoist crusher; do
  if curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:$t" >/dev/null; then :; else fail "нет двойника $NS:$t"; fi
done
pass "ключевые двойники на месте (рудник, ВГП, панели, камеры, целики, выработки, АГК, звенья, ПДМ, наряд, подъём)"

echo "Телеметрия (симулятор -> MQTT -> Ditto)"
t1=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:east-mine/features/sim_time_h/properties/value" || echo 0)
sleep 6
t2=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:east-mine/features/sim_time_h/properties/value" || echo 0)
if awk -v a="$t1" -v b="$t2" 'BEGIN{exit !(b>a)}'; then pass "двойник рудника живёт: модельное время ${t1} ч -> ${t2} ч"; else fail "двойник рудника не обновляется ($t1 -> $t2)"; fi
q=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:fan-main/features/q/properties/value" || echo 0)
if awk -v q="$q" 'BEGIN{exit !(q>100)}'; then pass "ВГП: подача $q м3/с (сеть рассчитана)"; else fail "ВГП: подача $q м3/с, вентиляционная сеть не рассчитана"; fi

echo "История (Ditto -> Telegraf -> InfluxDB)"
flux='from(bucket:"default") |> range(start:-10m) |> filter(fn:(r)=> r._measurement=="mqtt_consumer" and r.thingId=="'"$NS"':fan-main" and r._field=="value_q_properties_value") |> last()'
if retry 10 sh -c "curl -sf -XPOST 'http://$H:$INFLUXDB_PORT/api/v2/query?org=opentwins' -H 'Authorization: Token $INFLUXDB_TOKEN' -H 'Content-Type: application/vnd.flux' -H 'Accept: application/csv' --data-binary '$flux' | grep -q fan-main"; then
  pass "точки подачи ВГП есть в InfluxDB"; else fail "в InfluxDB пока нет точек ВГП"; fi

echo "Grafana"
for uid in vzhr-overview vzhr-air vzhr-ground vzhr-plan vzhr-map; do
  if curl -sf -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA/api/dashboards/uid/$uid" >/dev/null; then pass "дашборд $uid подключён"; else fail "нет дашборда $uid"; fi
done

echo "План горизонта и сценарии"
if curl -sf "$MAP/" | grep -q "Жезказган"; then pass "план горизонта отдаётся на $MAP/"; else fail "план горизонта не отдаётся"; fi
n=$(curl -sf "$MAP/api/state" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["twins"]))' 2>/dev/null || echo 0)
if [ "${n:-0}" -ge 160 ]; then pass "состояние карты содержит $n двойников из Ditto"; else fail "в состоянии карты только ${n:-0} двойников"; fi
curl -sf -XPOST "$MAP/api/command" -H 'Content-Type: application/json' -d '{"action":"door","target":"aw-door-3","on":true}' >/dev/null || fail "POST команды не прошёл"
if retry 10 sh -c "curl -sf -u 'ditto:$DITTO_PASSWORD' '$DITTO/api/2/things/$NS:aw-door-3/features/door_state/properties/value' | grep -q открыта"; then
  pass "сценарий дошёл до симулятора: двойник двери aw-door-3 показывает «открыта»"; else fail "сценарий не дошёл до двойника"; fi
curl -sf -XPOST "$MAP/api/command" -H 'Content-Type: application/json' -d '{"action":"door","target":"aw-door-3","on":false}' >/dev/null || true
if retry 10 sh -c "curl -sf -u 'ditto:$DITTO_PASSWORD' '$DITTO/api/2/things/$NS:aw-door-3/features/door_state/properties/value' | grep -q закрыта"; then
  pass "отмена работает: дверь снова «закрыта»"; else fail "отмена не дошла до двойника"; fi

echo
if [ "$FAILED" = 0 ]; then echo "ВЖР: все проверки пройдены"; else echo "ВЖР: часть проверок НЕ пройдена"; exit 1; fi
