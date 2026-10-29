#!/usr/bin/env bash
# Shared helpers for the Hajiri scripts (setup, start, stop, run, install-service, update). Source this; do not run it.
# Every path is quoted: the checkout may live in a folder whose name has spaces.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_MAJOR=18

if [ -t 1 ]; then BOLD=$'\e[1m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RED=$'\e[31m'; RESET=$'\e[0m'; else BOLD=; GREEN=; YELLOW=; RED=; RESET=; fi
step()  { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()    { printf '%s  ✓ %s%s\n' "$GREEN" "$*" "$RESET"; }
warn()  { printf '%s  ! %s%s\n' "$YELLOW" "$*" "$RESET"; }
fail()  { printf '%s  ✗ %s%s\n' "$RED" "$*" "$RESET" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) OS=mac ;;
  Linux)  OS=linux ;;
  *)      OS=other ;;
esac

# Default PostgreSQL port: 5433 on macOS (so an existing Homebrew PG on 5432 is left alone), 5432 on Linux.
if [ "$OS" = mac ]; then DEFAULT_PG_PORT=5433; else DEFAULT_PG_PORT=5432; fi

# Put Homebrew and PostgreSQL 18 binaries on PATH when present.
setup_path() {
  if [ "$OS" = mac ]; then
    for p in /opt/homebrew/bin /usr/local/bin; do [ -d "$p" ] && case ":$PATH:" in *":$p:"*) ;; *) PATH="$p:$PATH";; esac; done
    if command -v brew >/dev/null 2>&1; then
      local pgbin; pgbin="$(brew --prefix postgresql@$PG_MAJOR 2>/dev/null)/bin"
      [ -d "$pgbin" ] && PATH="$pgbin:$PATH"
    fi
  elif [ -d "/usr/lib/postgresql/$PG_MAJOR/bin" ]; then
    PATH="/usr/lib/postgresql/$PG_MAJOR/bin:$PATH"
  fi
  export PATH
}

# Read one KEY from a .env file without exporting anything into this shell (each app loads its own .env itself).
#   env_get FILE KEY [DEFAULT]
env_get() {
  local f="$1" key="$2" def="${3:-}" line
  if [ -f "$f" ]; then
    line="$(grep -E "^${key}=" "$f" | tail -1 || true)"
    if [ -n "$line" ]; then echo "${line#*=}"; return 0; fi
  fi
  echo "$def"
}

# Parse host/port/db out of postgres://[user[:pw]@]host[:port]/db
pg_host_from_url() { local u="${1#*://}"; u="${u##*@}"; u="${u%%/*}"; echo "${u%%:*}"; }
pg_port_from_url() { local u="${1#*://}"; u="${u##*@}"; u="${u%%/*}"; case "$u" in *:*) echo "${u##*:}";; *) echo 5432;; esac; }
pg_db_from_url()   { local u="${1#*://}"; u="${u#*/}"; echo "${u%%\?*}"; }

pg_service_start() {
  if [ "$OS" = mac ]; then
    brew services start postgresql@$PG_MAJOR >/dev/null
  else
    sudo systemctl enable --now postgresql >/dev/null 2>&1 || sudo service postgresql start
  fi
}

wait_for_pg() {
  local host="$1" port="$2" i
  for i in $(seq 1 40); do
    pg_isready -h "$host" -p "$port" -q 2>/dev/null && return 0
    sleep 0.5
  done
  return 1
}


# True when FILE has KEY= with a non-empty value.
env_has_value() { local v; v="$(env_get "$1" "$2")"; [ -n "$v" ]; }

# Set KEY=VALUE in FILE: replace an existing line (even an empty one) or append. Works on macOS and Linux.
env_set() {
  local f="$1" key="$2" val="$3" tmp
  tmp="$(mktemp)"
  if [ -f "$f" ] && grep -qE "^${key}=" "$f"; then
    awk -v k="$key" -v v="$val" 'BEGIN{FS=OFS="="} $1==k && !done {print k "=" v; done=1; next} {print}' "$f" > "$tmp" && cat "$tmp" > "$f"
  else
    printf '%s=%s\n' "$key" "$val" >> "$f"
  fi
  rm -f "$tmp"
}

# ------------------------------------------------------------------ the Django stack

PY_VERSION=3.13
SERVER="$ROOT/server"
VENV="$SERVER/.venv"
VENV_PY="$VENV/bin/python"
WEB_ENV="$SERVER/.env"          # the web app, the worker and manage.py read this
GW_ENV="$SERVER/gateway.env"    # the gateway's own settings (port, signing key), on top of server/.env
OPS="$ROOT/scripts/django_ops.py"
RUN_DIR="$ROOT/.run"
LOG_DIR="$ROOT/logs"
# Background work runs as one `worker` process (scheduler + task worker, the default) or, with WORKER_MODE=split in
# server/.env, as a `scheduler` process plus TASK_WORKERS `tasks` processes (django_tasks_db's db_worker).
ALL_DJANGO_SERVICES="web worker scheduler tasks gateway"
worker_mode() { local m; m="$(env_get "$WEB_ENV" WORKER_MODE)"; case "$m" in split) echo split ;; *) echo combined ;; esac; }
task_workers() {
  local n; n="$(env_get "$WEB_ENV" TASK_WORKERS)"
  case "$n" in ''|*[!0-9]*) echo 1 ;; 0) echo 1 ;; *) echo "$n" ;; esac
}
# The services this install runs, in start order (gateway first, web last).
django_services() {
  if [ "$(worker_mode)" = split ]; then
    local out="gateway scheduler" i
    for i in $(seq 1 "$(task_workers)"); do out="$out tasks$( [ "$i" = 1 ] || echo "-$i")"; done
    echo "$out web"
  else
    echo "gateway worker web"
  fi
}
DJANGO_SERVICES="$(django_services)"

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

# ------------------------------------------------------------------ the web app (React, frontend/)
# Built into server/frontend_build/ and committed, so installs need only Python. Node.js is needed only to rebuild it.
FRONTEND="$ROOT/frontend"
FRONTEND_BUILD="$SERVER/frontend_build"

# Node.js new enough for Vite 8 (20.19+ or 22.12+).
node_ok() {
  command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1 || return 1
  node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit((a===20&&b>=19)||(a===22&&b>=12)||a>=23?0:1)' 2>/dev/null
}

# Warn (do not fail) when the committed build is missing: every page would answer "the web app has not been built".
check_frontend_build() {
  if [ -f "$FRONTEND_BUILD/index.html" ]; then
    ok "web app build present (server/frontend_build)"
  else
    warn "server/frontend_build/index.html is missing: restore it with 'git checkout -- server/frontend_build' or build it with ./scripts/build-frontend.sh (needs Node.js)"
  fi
}

# manage.py with the venv's Python; Django loads server/.env itself.
manage() { "$VENV_PY" "$SERVER/manage.py" "$@"; }
ops() { "$VENV_PY" "$OPS" "$@"; }

# The live DATABASE_URL: server/.env, else the environment.
django_database_url() {
  local u
  u="$(env_get "$WEB_ENV" DATABASE_URL)"
  [ -n "$u" ] || u="${DATABASE_URL:-}"
  echo "$u"
}

web_port()     { local p; p="$(env_get "$WEB_ENV" PORT)"; echo "${p:-3000}"; }
gateway_port() { local p; p="$(env_get "$GW_ENV" PORT)"; echo "${p:-8080}"; }
web_threads()  { local t; t="$(env_get "$WEB_ENV" WEB_THREADS)"; echo "${t:-8}"; }

# Wait for PostgreSQL named by the live DATABASE_URL (pg_isready when installed, else a Python connection attempt).
wait_for_django_db() {
  local url host port
  url="$(django_database_url)"
  [ -n "$url" ] || fail "DATABASE_URL missing (server/.env). Run ./scripts/setup.sh first."
  host="$(pg_host_from_url "$url")"; port="$(pg_port_from_url "$url")"
  if command -v pg_isready >/dev/null 2>&1; then
    wait_for_pg "$host" "$port" && return 0
    return 1
  fi
  ops wait-db 30
}

# The command line of each long-running service. All run from server/ in the foreground.
#   web:     production WSGI server (waitress), the in-process worker off (the worker service runs it)
#   worker:    background jobs (recompute queue, notifications, reminders, nightly sweep): the scheduler and a task
#              worker in one process
#   scheduler: only the scheduler, which enqueues the due jobs as django.tasks tasks (WORKER_MODE=split; run one)
#   tasks:     a django_tasks_db task worker that runs the queued tasks (WORKER_MODE=split; tasks-2, tasks-3 ... more)
#   gateway:   device gateway (ZKTeco ADMS, Hikvision ISAPI)
service_exec() {
  local svc="$1"
  cd "$SERVER" || exit 1
  case "$svc" in
    web)       RUN_WORKER=false exec "$VENV_PY" -m waitress --host="${HAJIRI_HOST:-0.0.0.0}" --port="$(web_port)" --threads="$(web_threads)" \
                 --ident=hajiri --channel-timeout=120 config.wsgi:application ;;
    worker)    exec "$VENV_PY" manage.py run_worker ;;
    scheduler) exec "$VENV_PY" manage.py run_scheduler ;;
    tasks|tasks-[0-9]*) exec "$VENV_PY" manage.py db_worker --queue-name default --interval 1 ;;
    gateway)   exec "$VENV_PY" manage.py run_gateway ;;
    *) fail "unknown service $svc (web, worker, scheduler, tasks or gateway)" ;;
  esac
}

# Is this a background-work service (one of worker, scheduler, tasks, tasks-N)?
is_background_service() { case "$1" in worker|scheduler|tasks|tasks-[0-9]*) return 0 ;; *) return 1 ;; esac; }

require_venv() {
  [ -x "$VENV_PY" ] || fail "server/.venv missing. Run ./scripts/setup.sh first."
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
