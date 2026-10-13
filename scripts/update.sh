#!/usr/bin/env bash
# Update to the latest version from GitHub and apply any new migrations.
# If services were installed with install-service.sh they are restarted; otherwise restart start.sh yourself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path

step "Pulling latest code"
git pull --ff-only
ok "$(git log -1 --format='%h %s')"

step "Dependencies"
pnpm install --frozen-lockfile 2>&1 | grep -E "^(Done|Already)" || true

step "Migrations"
DATABASE_URL="$(env_get apps/api/.env DATABASE_URL)"
PG_HOST="$(pg_host_from_url "$DATABASE_URL")"; PG_PORT="$(pg_port_from_url "$DATABASE_URL")"
pg_isready -h "$PG_HOST" -p "$PG_PORT" -q || { pg_service_start; wait_for_pg "$PG_HOST" "$PG_PORT"; }
pnpm --filter @hajiri/api --silent db:migrate
pnpm --filter @hajiri/api --silent db:seed:bs
ok "schema up to date"

step "Web app build"
pnpm --filter @hajiri/web --silent build >/dev/null 2>&1 && ok "apps/web/dist rebuilt" || warn "web build failed (run: pnpm --filter @hajiri/web build)"

step "Restarting services"
if [ "$OS" = mac ] && [ -f "$HOME/Library/LaunchAgents/com.hajiri.api.plist" ]; then
  for s in gateway api; do launchctl kickstart -k "gui/$(id -u)/com.hajiri.$s"; done
  ok "launchd services restarted"
elif [ "$OS" = linux ] && systemctl list-unit-files 2>/dev/null | grep -q '^hajiri-api.service'; then
  sudo systemctl restart hajiri-gateway hajiri-api
  ok "systemd services restarted"
elif [ -f .run/api.pid ]; then
  ./scripts/stop.sh && ./scripts/start.sh --daemon
else
  warn "no managed services found; start with ./scripts/start.sh"
fi
