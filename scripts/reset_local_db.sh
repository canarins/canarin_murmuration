#!/usr/bin/env bash
# Drop and recreate the local Plumage stand-in, flush Redis state, clear artifacts.
set -euo pipefail
PGHOST=${PGHOST:-localhost}; PGUSER=${PGUSER:-plumage}
psql -h "$PGHOST" -U "$PGUSER" -d postgres -qc "DROP DATABASE IF EXISTS plumage WITH (FORCE)"
psql -h "$PGHOST" -U "$PGUSER" -d postgres -qc "CREATE DATABASE plumage"
redis-cli -u "${MURM_REDIS_URL:-redis://localhost:6379/0}" flushdb >/dev/null
rm -rf ./artifacts
murmuration migrate
