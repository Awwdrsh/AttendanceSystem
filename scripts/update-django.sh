#!/usr/bin/env bash
# Update the Django stack to the latest version from GitHub: new Python dependencies, migrations and the BS calendar.
# Services installed with install-service-django.sh (or started with start-django.sh --daemon) are
# restarted; otherwise restart start-django.sh yourself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib-django.sh"
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
if [ "$OS" = mac ] && [ -f "$HOME/Library/LaunchAgents/com.hajiri.django-web.plist" ]; then
  for s in gateway worker web; do launchctl kickstart -k "gui/$(id -u)/com.hajiri.django-$s"; done
  ok "launchd services restarted"
elif [ "$OS" = linux ] && systemctl list-unit-files 2>/dev/null | grep -q '^hajiri-django-web.service'; then
  sudo systemctl restart hajiri-django-gateway hajiri-django-worker hajiri-django-web
  ok "systemd services restarted"
elif [ -f "$RUN_DIR/django-web.pid" ]; then
  "$ROOT/scripts/stop-django.sh" && "$ROOT/scripts/start-django.sh" --daemon
else
  warn "no managed services found; start with ./scripts/start-django.sh"
fi
