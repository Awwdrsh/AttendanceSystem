#!/usr/bin/env bash
# Stop the Django services started with ./scripts/start-django.sh --daemon
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib-django.sh"
cd "$ROOT"
for name in $DJANGO_SERVICES; do
  f="$RUN_DIR/django-$name.pid"
  if [ -f "$f" ]; then
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
  else
    warn "$name: no pid file"
  fi
done
