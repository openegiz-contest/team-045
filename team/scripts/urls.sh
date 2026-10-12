#!/usr/bin/env bash
# Print where the Sarybulak mine can be seen.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
set -a; . "$ROOT/deploy/compose/.env"; set +a
H="${OPENEGIZ_PUBLIC_HOST:-localhost}"
cat <<TXT

Sarybulak mine is running.

  Mine map + what-if scenarios   http://$H:${MINE_PORT:-8095}/
  Grafana dashboards             http://$H:$GRAFANA_PORT/dashboards?query=Sarybulak   admin / $GRAFANA_ADMIN_PASSWORD
  Twins                          Grafana -> OpenEgiz -> Twins (namespace org.openegiz.sarybulak)
  Ditto API                      http://$H:$DITTO_PORT/api/2/search/things?namespaces=org.openegiz.sarybulak   ditto / $DITTO_PASSWORD

Check everything with: make mine-check
TXT
