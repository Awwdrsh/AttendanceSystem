#!/usr/bin/env bash
# Stop services started with ./scripts/start.sh --daemon
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"
for name in api gateway; do
  f=".run/$name.pid"
  if [ -f "$f" ]; then
    pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
      # pnpm wraps the node process; kill the whole process group so children die too.
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
      ok "stopped $name (pid $pid)"
    else
      warn "$name not running"
    fi
    rm -f "$f"
  else
    warn "$name: no pid file"
  fi
done
