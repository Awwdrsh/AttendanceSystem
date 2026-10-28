#!/usr/bin/env bash
# Update Hajiri to the latest version from GitHub: new Python dependencies, migrations and the BS calendar.
# Services installed with install-service.sh (or started with start.sh --daemon) are
# restarted; otherwise restart start.sh yourself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
require_venv

step "Pulling latest code"
git pull --ff-only
ok "$(git log -1 --format='%h %s')"

step "Python dependencies"
if ! "$VENV_PY" -m pip install -q -r "$SERVER/requirements.txt" >/tmp/hajiri-pip.log 2>&1; then
  tail -20 /tmp/hajiri-pip.log
  fail "pip install failed (full log: /tmp/hajiri-pip.log)"
fi
"$VENV_PY" -c 'import waitress' 2>/dev/null || "$VENV_PY" -m pip install -q 'waitress==3.0.2' >/dev/null
ok "server/.venv ready"

step "Migrations"
wait_for_django_db || { pg_service_start; wait_for_django_db || fail "PostgreSQL not reachable"; }
manage migrate --noinput
manage seed_bs_calendar >/dev/null
ok "schema up to date"

step "Restarting services"
any_hajiri_unit() {
  local f
  for f in "$HOME/Library/LaunchAgents"/com.hajiri.*.plist /etc/systemd/system/hajiri-*.service; do [ -f "$f" ] && return 0; done
  return 1
}
if [ "$OS" = mac ] && [ -f "$HOME/Library/LaunchAgents/com.hajiri.web.plist" ]; then
  for s in $DJANGO_SERVICES; do launchctl kickstart -k "gui/$(id -u)/com.hajiri.$s"; done
  ok "launchd services restarted"
elif [ "$OS" = linux ] && systemctl list-unit-files 2>/dev/null | grep -q '^hajiri-web.service'; then
  units=(); for s in $DJANGO_SERVICES; do units+=("hajiri-$s"); done
  sudo systemctl restart "${units[@]}"
  ok "systemd services restarted"
elif any_hajiri_unit; then
  # Units under an older name (com.hajiri.django-*, hajiri-django-*, or the TypeScript api/gateway units).
  "$ROOT/scripts/install-service.sh"
  ok "services reinstalled under their current names"
elif [ -f "$RUN_DIR/web.pid" ] || [ -f "$RUN_DIR/django-web.pid" ]; then
  "$ROOT/scripts/stop.sh" && "$ROOT/scripts/start.sh" --daemon
else
  warn "no managed services found; start with ./scripts/start.sh"
fi
