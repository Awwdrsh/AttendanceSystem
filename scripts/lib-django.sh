#!/usr/bin/env bash
# Shared helpers for the Django-stack scripts (*-django.sh). Source this; do not run it.
# Builds on lib.sh (OS detection, PostgreSQL helpers, .env helpers). Every path is quoted: the checkout may live in a
# folder whose name has spaces.

source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PY_VERSION=3.13
SERVER="$ROOT/server"
VENV="$SERVER/.venv"
VENV_PY="$VENV/bin/python"
WEB_ENV="$SERVER/.env"          # Django reads this first, then apps/api/.env
GW_ENV="$SERVER/gateway.env"    # the gateway reads this first, then apps/gateway/.env
OPS="$ROOT/scripts/django_ops.py"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/logs"
DJANGO_SERVICES="web worker gateway"

# A Python 3.13 interpreter (never 3.14: the Homebrew 3.14 build on the development Mac cannot create a venv with pip).
find_python313() {
  local c
  for c in "python$PY_VERSION" /opt/homebrew/bin/python$PY_VERSION /usr/local/bin/python$PY_VERSION /usr/bin/python$PY_VERSION; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
      command -v "$c"; return 0
    fi
  done
  if [ "$OS" = mac ] && command -v brew >/dev/null 2>&1; then
    c="$(brew --prefix python@$PY_VERSION 2>/dev/null)/bin/python$PY_VERSION"
    [ -x "$c" ] && { echo "$c"; return 0; }
  fi
  return 1
}

# manage.py with the venv's Python; Django loads server/.env (and apps/api/.env) itself.
manage() { "$VENV_PY" "$SERVER/manage.py" "$@"; }
ops() { "$VENV_PY" "$OPS" "$@"; }

# The live DATABASE_URL: server/.env, else apps/api/.env (the order Django reads them), else the environment.
django_database_url() {
  local u
  u="$(env_get "$WEB_ENV" DATABASE_URL)"
  [ -n "$u" ] || u="$(env_get "$ROOT/apps/api/.env" DATABASE_URL)"
  [ -n "$u" ] || u="${DATABASE_URL:-}"
  echo "$u"
}

web_port()     { local p; p="$(env_get "$WEB_ENV" PORT)"; echo "${p:-3000}"; }
gateway_port() { local p; p="$(env_get "$GW_ENV" PORT)"; [ -n "$p" ] || p="$(env_get "$ROOT/apps/gateway/.env" PORT)"; echo "${p:-8080}"; }
web_threads()  { local t; t="$(env_get "$WEB_ENV" WEB_THREADS)"; echo "${t:-8}"; }

# Wait for PostgreSQL named by the live DATABASE_URL (pg_isready when installed, else a Python connection attempt).
wait_for_django_db() {
  local url host port
  url="$(django_database_url)"
  [ -n "$url" ] || fail "DATABASE_URL missing (server/.env). Run ./scripts/setup-django.sh first."
  host="$(pg_host_from_url "$url")"; port="$(pg_port_from_url "$url")"
  if command -v pg_isready >/dev/null 2>&1; then
    wait_for_pg "$host" "$port" && return 0
    return 1
  fi
  ops wait-db 30
}

# The command line of each long-running service. All run from server/ in the foreground.
#   web:     production WSGI server (waitress), the in-process worker off (the worker service runs it)
#   worker:  background jobs (recompute queue, notifications, reminders, nightly sweep)
#   gateway: device gateway (ZKTeco ADMS, Hikvision ISAPI)
service_exec() {
  local svc="$1"
  cd "$SERVER" || exit 1
  case "$svc" in
    web)     RUN_WORKER=false exec "$VENV_PY" -m waitress --host="${HAJIRI_HOST:-0.0.0.0}" --port="$(web_port)" --threads="$(web_threads)" \
               --ident=hajiri --channel-timeout=120 config.wsgi:application ;;
    worker)  exec "$VENV_PY" manage.py run_worker ;;
    gateway) exec "$VENV_PY" manage.py run_gateway ;;
    *) fail "unknown service $svc (web, worker or gateway)" ;;
  esac
}

require_venv() {
  [ -x "$VENV_PY" ] || fail "server/.venv missing. Run ./scripts/setup-django.sh first."
}

# Logs hold employee names and request details: owner-only.
secure_logs() {
  mkdir -p "$LOG_DIR" "$RUN_DIR"
  chmod 700 "$LOG_DIR"
  local f
  for f in "$LOG_DIR"/*.log; do [ -f "$f" ] && chmod 600 "$f"; done
  return 0
}

pid_running() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }
