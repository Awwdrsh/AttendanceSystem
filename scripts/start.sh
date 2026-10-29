#!/usr/bin/env bash
# Start Hajiri: PostgreSQL, pending migrations, the web app (waitress), the background worker and the
# device gateway. With WORKER_MODE=split in server/.env the worker is a scheduler plus TASK_WORKERS task workers.
#   ./scripts/start.sh            foreground, Ctrl+C stops everything
#   ./scripts/start.sh --daemon   background; logs in logs/*.log, stop with ./scripts/stop.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path

DAEMON=0
for a in "$@"; do case "$a" in --daemon|-d) DAEMON=1 ;; -h|--help) sed -n '2,6p' "$0"; exit 0 ;; *) fail "unknown option $a" ;; esac; done

[ -f "$WEB_ENV" ] || fail "server/.env missing. Run ./scripts/setup.sh first."
require_venv
DATABASE_URL="$(django_database_url)"
[ -n "$DATABASE_URL" ] || fail "DATABASE_URL missing from server/.env"
WEB_PORT="$(web_port)"; GW_PORT="$(gateway_port)"
PG_HOST="$(pg_host_from_url "$DATABASE_URL")"; PG_PORT="$(pg_port_from_url "$DATABASE_URL")"

step "PostgreSQL ($PG_HOST:$PG_PORT)"
if ! wait_for_django_db; then
  warn "not running; starting"
  pg_service_start
  wait_for_django_db || fail "PostgreSQL did not come up"
fi
ok "ready"

step "Migrations"
manage migrate --noinput >/dev/null
ok "schema up to date"

step "Web app"
check_frontend_build

for port in "$WEB_PORT" "$GW_PORT"; do
  if curl -fs "http://localhost:$port/health" >/dev/null 2>&1; then
    fail "port $port is already serving Hajiri (an earlier start). Stop it first, or set PORT in server/.env / server/gateway.env."
  fi
done

secure_logs
umask 077
if [ "$DAEMON" = 1 ]; then
  for f in "$RUN_DIR"/*.pid; do [ -f "$f" ] && pid_running "$f" && fail "already running (./scripts/stop.sh first)"; done
  step "Starting in background"
  for s in $DJANGO_SERVICES; do
    nohup bash -c 'source "$1/scripts/lib.sh"; service_exec "$2"' _ "$ROOT" "$s" >> "$LOG_DIR/$s.log" 2>&1 &
    echo $! > "$RUN_DIR/$s.pid"
  done
  for _ in $(seq 1 60); do
    curl -fs "http://localhost:$WEB_PORT/health" >/dev/null 2>&1 && curl -fs "http://localhost:$GW_PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
  curl -fs "http://localhost:$WEB_PORT/health" >/dev/null || fail "web app did not start; see logs/web.log"
  curl -fs "http://localhost:$GW_PORT/health" >/dev/null || fail "gateway did not start; see logs/gateway.log"
  logs=""
  for s in $DJANGO_SERVICES; do
    logs="$logs logs/$s.log"
    is_background_service "$s" || continue
    pid_running "$RUN_DIR/$s.pid" || fail "$s did not start; see logs/$s.log"
  done
  ok "Web     http://localhost:$WEB_PORT  (pid $(cat "$RUN_DIR/web.pid"))"
  ok "Gateway http://localhost:$GW_PORT  (pid $(cat "$RUN_DIR/gateway.pid"))"
  for s in $DJANGO_SERVICES; do
    is_background_service "$s" && ok "$(printf '%-9s' "$s")(pid $(cat "$RUN_DIR/$s.pid"))"
  done
  echo "  logs:$logs   stop: ./scripts/stop.sh"
  exit 0
fi

step "Starting (Ctrl+C to stop)"
PIDS=()
cleanup() {
  trap - INT TERM
  local p
  for p in "${PIDS[@]}"; do pkill -TERM -P "$p" 2>/dev/null || true; kill -TERM "$p" 2>/dev/null || true; done
  wait 2>/dev/null
  echo; ok "stopped"
}
trap cleanup INT TERM
for s in $DJANGO_SERVICES; do
  label="$(printf '%-11s' "[$s]")"
  ( service_exec "$s" ) > >(sed -u "s/^/$label /") 2>&1 &
  PIDS+=("$!")
done
echo "  Web     http://localhost:$WEB_PORT/health"
echo "  Gateway http://localhost:$GW_PORT/health"
wait
