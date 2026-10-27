#!/usr/bin/env bash
# Entry point used by launchd/systemd for the Django stack: ./scripts/run-django.sh web|worker|gateway
# Waits for PostgreSQL, applies migrations (web only), then runs the service in the foreground.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib-django.sh"
cd "$ROOT"
setup_path
svc="${1:-}"
case "$svc" in web|worker|gateway) ;; *) fail "usage: run-django.sh web|worker|gateway" ;; esac
require_venv
wait_for_django_db || fail "PostgreSQL not reachable ($(pg_host_from_url "$(django_database_url)"):$(pg_port_from_url "$(django_database_url)"))"
if [ "$svc" = web ]; then manage migrate --noinput; fi
service_exec "$svc"
