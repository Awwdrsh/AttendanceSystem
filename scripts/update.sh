#!/usr/bin/env bash
# Update Hajiri to the latest version from GitHub: new Python dependencies, migrations and the BS calendar. The web
# app's build comes with the code (server/frontend_build/); it is rebuilt here only when Node.js is installed and the
# pulled commits changed frontend/ without a matching build.
# Services installed with install-service.sh (or started with start.sh --daemon) are
# restarted; otherwise restart start.sh yourself.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
require_venv

step "Pulling latest code"
# A local rebuild (below, on an earlier update) leaves server/frontend_build/ changed; it is build output only, so
# it is reset to the committed build before pulling.
if ! git diff --quiet -- server/frontend_build || [ -n "$(git ls-files --others --exclude-standard -- server/frontend_build)" ]; then
  git checkout -- server/frontend_build
  git clean -fdq -- server/frontend_build
  ok "local web app build reset to the committed one"
fi
OLD_HEAD="$(git rev-parse HEAD)"
git pull --ff-only
ok "$(git log -1 --format='%h %s')"

step "Web app"
if git diff --quiet "$OLD_HEAD" HEAD -- frontend; then
  ok "unchanged"
elif ! git diff --quiet "$OLD_HEAD" HEAD -- server/frontend_build; then
  ok "new build pulled with the code"
elif node_ok; then
  "$ROOT/scripts/build-frontend.sh"
else
  warn "frontend/ changed without a new build and Node.js is not installed: the previous build is served (install Node.js 22 and run ./scripts/build-frontend.sh)"
fi
check_frontend_build

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
