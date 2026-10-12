#!/usr/bin/env bash
# End-to-end check of the Submission on a running stack:
#   twins in Ditto -> simulator telemetry reaches Ditto -> Telegraf writes
#   InfluxDB -> Grafana dashboards provisioned -> map page serves live state
#   -> a what-if command round-trips through MQTT and shows up in a twin.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
set -a; . "$ROOT/deploy/compose/.env"; set +a
H=127.0.0.1
DITTO="http://$H:$DITTO_PORT"
GRAFANA="http://$H:$GRAFANA_PORT"
MAP="http://$H:${MINE_PORT:-8095}"
NS="org.openegiz.sarybulak"
FAILED=0
pass() { printf '  \033[32mok\033[0m    %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=1; }
retry() { local n="$1"; shift; for _ in $(seq 1 "$n"); do if "$@" >/dev/null 2>&1; then return 0; fi; sleep 3; done; return 1; }

echo "Twins"
count=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/search/things/count?namespaces=$NS" || echo 0)
if [ "${count:-0}" -ge 60 ]; then pass "$count twins in namespace $NS"; else fail "only ${count:-0} twins in $NS (expected >= 60)"; fi
for t in mine excavator-01 truck-01 crusher mill flotation substation dispatcher road-main block-545-1A; do
  if curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:$t" >/dev/null; then :; else fail "twin $NS:$t missing"; fi
done
pass "key twins present (mine, excavators, trucks, roads, blocks, plant, substation, dispatcher)"

echo "Telemetry (simulator -> MQTT -> Ditto)"
t1=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:mine/features/sim_time_h/properties/value" || echo 0)
sleep 6
t2=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:mine/features/sim_time_h/properties/value" || echo 0)
if awk -v a="$t1" -v b="$t2" 'BEGIN{exit !(b>a)}'; then pass "mine twin advances: sim_time ${t1} h -> ${t2} h"; else fail "mine twin not updating (sim_time $t1 -> $t2)"; fi
st=$(curl -sf -u "ditto:$DITTO_PASSWORD" "$DITTO/api/2/things/$NS:truck-01/features/state/properties/value" || echo '"?"')
pass "truck-01 state: $st"

echo "History (Ditto -> Telegraf -> InfluxDB)"
flux='from(bucket:"default") |> range(start:-10m) |> filter(fn:(r)=> r._measurement=="mqtt_consumer" and r.thingId=="'"$NS"':crusher" and r._field=="value_throughput_properties_value") |> last()'
if retry 10 sh -c "curl -sf -XPOST 'http://$H:$INFLUXDB_PORT/api/v2/query?org=opentwins' -H 'Authorization: Token $INFLUXDB_TOKEN' -H 'Content-Type: application/vnd.flux' -H 'Accept: application/csv' --data-binary '$flux' | grep -q crusher"; then
  pass "crusher throughput points in InfluxDB"; else fail "no crusher points in InfluxDB yet"; fi

echo "Grafana"
for uid in sarybulak-overview sarybulak-fleet sarybulak-plant-energy sarybulak-map; do
  if curl -sf -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA/api/dashboards/uid/$uid" >/dev/null; then pass "dashboard $uid provisioned"; else fail "dashboard $uid missing"; fi
done

echo "Map and scenarios"
if curl -sf "$MAP/" | grep -q "Сарыбұлақ"; then pass "map page served at $MAP/"; else fail "map page not served"; fi
n=$(curl -sf "$MAP/api/state" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["twins"]))' 2>/dev/null || echo 0)
if [ "${n:-0}" -ge 60 ]; then pass "map state has $n twins from Ditto"; else fail "map state has only ${n:-0} twins"; fi
curl -sf -XPOST "$MAP/api/command" -H 'Content-Type: application/json' -d '{"action":"road_closed","target":"road-fuel","on":true}' >/dev/null || fail "command POST failed"
if retry 10 sh -c "curl -sf -u 'ditto:$DITTO_PASSWORD' '$DITTO/api/2/things/$NS:road-fuel/features/status/properties/value' | grep -q closed"; then
  pass "what-if command reached the simulator and the road-fuel twin shows 'closed'"; else fail "what-if command did not reach the twin"; fi
curl -sf -XPOST "$MAP/api/command" -H 'Content-Type: application/json' -d '{"action":"road_closed","target":"road-fuel","on":false}' >/dev/null || true
if retry 10 sh -c "curl -sf -u 'ditto:$DITTO_PASSWORD' '$DITTO/api/2/things/$NS:road-fuel/features/status/properties/value' | grep -q open"; then
  pass "undo works: road-fuel is 'open' again"; else fail "undo did not reach the twin"; fi

echo
if [ "$FAILED" = 0 ]; then echo "Sarybulak mine: all checks passed"; else echo "Sarybulak mine: some checks FAILED"; exit 1; fi
