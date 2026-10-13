#!/usr/bin/env bash
# Shared helpers for Hajiri scripts. Source this; do not run it.

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

node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }
