#!/usr/bin/env bash
# Stop the services started with ./scripts/start.sh --daemon
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
# Every service with a pid file, whatever WORKER_MODE was when it started; then a word for the expected ones without.
stopped=" "
for f in "$RUN_DIR"/*.pid; do
  [ -f "$f" ] || continue
  name="$(basename "$f" .pid)"; name="${name#django-}"   # older installs wrote .run/django-<service>.pid
  stopped="$stopped$name "
  pid="$(cat "$f")"
  if kill -0 "$pid" 2>/dev/null; then
    # The pid is the bash wrapper or the exec'd Python process; stop its children too.
    pkill -TERM -P "$pid" 2>/dev/null || true
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
    ok "stopped $name (pid $pid)"
  else
    warn "$name not running"
  fi
  rm -f "$f"
done
for name in $DJANGO_SERVICES; do
  case "$stopped" in *" $name "*) ;; *) warn "$name: no pid file" ;; esac
done
