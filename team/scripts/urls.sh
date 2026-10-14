#!/usr/bin/env bash
# Где смотреть цифровой двойник горизонта ВЖР.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
set -a; . "$ROOT/deploy/compose/.env"; set +a
H="${OPENEGIZ_PUBLIC_HOST:-localhost}"
cat <<TXT

Восточно-Жезказганский рудник, горизонт 300 м: двойник работает.

  План горизонта, наряд смены, сценарии   http://$H:${MINE_PORT:-8095}/
  Дашборды Grafana (папка «ВЖР»)          http://$H:$GRAFANA_PORT/d/vzhr-overview   admin / $GRAFANA_ADMIN_PASSWORD
  Двойники и иерархия                     Grafana -> OpenEgiz -> Twins, пространство имён org.openegiz.zhezkazgan
  Ditto API                               http://$H:$DITTO_PORT/api/2/search/things?namespaces=org.openegiz.zhezkazgan   ditto / $DITTO_PASSWORD

Сквозная проверка: make mine-check
TXT
