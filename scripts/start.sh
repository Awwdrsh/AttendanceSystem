#!/usr/bin/env bash
# Start the whole system: PostgreSQL, pending migrations, the API and the device gateway.
#   ./scripts/start.sh            foreground, Ctrl+C stops everything
#   ./scripts/start.sh --daemon   background; logs in logs/, stop with ./scripts/stop.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path

DAEMON=0
for a in "$@"; do case "$a" in --daemon|-d) DAEMON=1 ;; -h|--help) sed -n '2,5p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done

[ -f apps/api/.env ] || fail "apps/api/.env missing. Run ./scripts/setup.sh first."
DATABASE_URL="$(env_get apps/api/.env DATABASE_URL)"
[ -n "$DATABASE_URL" ] || fail "DATABASE_URL missing from apps/api/.env"
API_PORT="$(env_get apps/api/.env PORT 3000)"
GW_PORT="$(env_get apps/gateway/.env PORT 8080)"
PG_HOST="$(pg_host_from_url "$DATABASE_URL")"; PG_PORT="$(pg_port_from_url "$DATABASE_URL")"

step "PostgreSQL ($PG_HOST:$PG_PORT)"
if ! pg_isready -h "$PG_HOST" -p "$PG_PORT" -q 2>/dev/null; then
  warn "not running; starting"
  pg_service_start
  wait_for_pg "$PG_HOST" "$PG_PORT" || fail "PostgreSQL did not come up"
fi
ok "ready"

step "Migrations"
pnpm --filter @hajiri/api --silent db:migrate
ok "schema up to date"

mkdir -p logs .run
# Logs hold employee names and request details: owner-only.
chmod 700 logs
for f in logs/*.log; do [ -f "$f" ] && chmod 600 "$f"; done
umask 077
if [ "$DAEMON" = 1 ]; then
  if [ -f .run/api.pid ] && kill -0 "$(cat .run/api.pid)" 2>/dev/null; then fail "already running (./scripts/stop.sh first)"; fi
  step "Starting in background"
  nohup bash -c 'cd apps/gateway && exec node --import tsx src/server.ts' >> logs/gateway.log 2>&1 & echo $! > .run/gateway.pid
  nohup bash -c 'cd apps/api && exec node --import tsx src/server.ts' >> logs/api.log 2>&1 & echo $! > .run/api.pid
  for _ in $(seq 1 60); do
    curl -fs "http://localhost:$API_PORT/health" >/dev/null 2>&1 && curl -fs "http://localhost:$GW_PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -fs "http://localhost:$API_PORT/health" >/dev/null || fail "API did not start; see logs/api.log"
  curl -fs "http://localhost:$GW_PORT/health" >/dev/null || fail "gateway did not start; see logs/gateway.log"
  ok "API     http://localhost:$API_PORT  (pid $(cat .run/api.pid))"
  ok "Gateway http://localhost:$GW_PORT  (pid $(cat .run/gateway.pid))"
  echo "  logs: logs/api.log, logs/gateway.log   stop: ./scripts/stop.sh"
  exit 0
fi

step "Starting (Ctrl+C to stop)"
cleanup() { trap - INT TERM; pkill -TERM -f "$ROOT/apps/(gateway|api)/src/server.ts" 2>/dev/null || pkill -TERM -f 'node --import tsx src/server.ts' 2>/dev/null || true; wait 2>/dev/null; echo; ok "stopped"; }
trap cleanup INT TERM
(cd apps/gateway && exec node --import tsx "$ROOT/apps/gateway/src/server.ts") 2>&1 | sed -u 's/^/[gateway] /' &
(cd apps/api && exec node --import tsx "$ROOT/apps/api/src/server.ts") 2>&1 | sed -u 's/^/[api]     /' &
echo "  API     http://localhost:$API_PORT/health"
echo "  Gateway http://localhost:$GW_PORT/health"
wait
