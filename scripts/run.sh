#!/usr/bin/env bash
# Entry point used by launchd/systemd: ./scripts/run.sh api|gateway
# Waits for PostgreSQL, applies migrations (api only), then runs the service in the foreground.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
svc="${1:-}"
case "$svc" in api|gateway) ;; *) fail "usage: run.sh api|gateway" ;; esac
DATABASE_URL="$(env_get "apps/$svc/.env" DATABASE_URL)"
PG_HOST="$(pg_host_from_url "$DATABASE_URL")"; PG_PORT="$(pg_port_from_url "$DATABASE_URL")"
wait_for_pg "$PG_HOST" "$PG_PORT" || fail "PostgreSQL not reachable at $PG_HOST:$PG_PORT"
[ "$svc" = api ] && pnpm --filter @hajiri/api --silent db:migrate
cd "apps/$svc"
exec node --import tsx src/server.ts
