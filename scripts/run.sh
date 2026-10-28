#!/usr/bin/env bash
# Entry point used by launchd/systemd: ./scripts/run.sh web|worker|gateway|scheduler|tasks[-N]
# (worker = scheduler and task worker in one process; scheduler + tasks are the WORKER_MODE=split processes).
# Waits for PostgreSQL, applies migrations (web only), then runs the service in the foreground.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
setup_path
svc="${1:-}"
case "$svc" in web|worker|gateway|scheduler|tasks|tasks-[0-9]*) ;; *) fail "usage: run.sh web|worker|gateway (or scheduler|tasks|tasks-N for WORKER_MODE=split)" ;; esac
require_venv
wait_for_django_db || fail "PostgreSQL not reachable ($(pg_host_from_url "$(django_database_url)"):$(pg_port_from_url "$(django_database_url)"))"
if [ "$svc" = web ]; then manage migrate --noinput; fi
service_exec "$svc"
