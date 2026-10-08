#!/usr/bin/env bash
# Drop and recreate the local Canarin stand-in schemas, flush Redis state, clear artifacts.
set -euo pipefail
H=${DB_HOST:-127.0.0.1}; U=${DB_USER:-plumage}; P=${DB_PASS:-plumage}
mysql -h "$H" -u "$U" -p"$P" -e "DROP DATABASE IF EXISTS Murmuration; DROP DATABASE IF EXISTS MurmurationInternal;
  DROP DATABASE IF EXISTS Devices; DROP DATABASE IF EXISTS WebFront; DROP DATABASE IF EXISTS \`Data\`;"
redis-cli -h "${REDIS_HOST:-127.0.0.1}" -p "${REDIS_PORT:-6379}" flushdb >/dev/null
rm -rf ./artifacts
murmuration migrate
