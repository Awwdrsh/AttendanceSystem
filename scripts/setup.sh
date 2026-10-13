#!/usr/bin/env bash
# One-command setup on a fresh macOS or Ubuntu/Debian machine:
#   git clone <repo> hajiri && cd hajiri && ./scripts/setup.sh
# Installs Node, pnpm and PostgreSQL 18, creates the databases, writes .env files, installs dependencies,
# runs migrations and seeds. Safe to re-run: every step is idempotent.
#
# Options:
#   --no-demo      do not load the demo company/employees/device
#   --with-tests   run the full test suite at the end
#   --pg-port N    PostgreSQL port (default 5433 on macOS, 5432 on Linux)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
cd "$ROOT"

WITH_DEMO=1; WITH_TESTS=0; PG_PORT="${HAJIRI_PG_PORT:-$DEFAULT_PG_PORT}"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-demo) WITH_DEMO=0 ;;
    --with-tests) WITH_TESTS=1 ;;
    --pg-port) PG_PORT="$2"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) fail "unknown option $1" ;;
  esac
  shift
done

[ "$OS" = other ] && fail "Unsupported OS. Use macOS or Ubuntu/Debian, or run with Docker (see infra/docker-compose.yml)."
setup_path

# ------------------------------------------------------------------ package manager
if [ "$OS" = mac ]; then
  step "Homebrew"
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not found; installing (you may be asked for your password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    setup_path
  fi
  ok "brew $(brew --version | head -1 | awk '{print $2}')"
else
  step "APT"
  . /etc/os-release
  case "${ID:-}" in ubuntu|debian) ;; *) fail "Only Ubuntu/Debian are supported by this script (found ${ID:-unknown})" ;; esac
  sudo apt-get update -qq
  sudo apt-get install -y -qq curl ca-certificates gnupg lsb-release git >/dev/null
  ok "apt ready"
fi

# ------------------------------------------------------------------ node + pnpm
step "Node.js (>= 22)"
if [ "$(node_major)" -lt 22 ]; then
  if [ "$OS" = mac ]; then brew install node >/dev/null
  else
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
    sudo apt-get install -y -qq nodejs >/dev/null
  fi
fi
[ "$(node_major)" -ge 22 ] || fail "Node >= 22 required, found $(node --version)"
ok "node $(node --version)"

step "pnpm 12"
if ! command -v pnpm >/dev/null 2>&1 || [ "$(pnpm --version | cut -d. -f1)" -lt 12 ]; then
  if [ "$OS" = mac ]; then npm install -g pnpm@12 >/dev/null; else sudo npm install -g pnpm@12 >/dev/null; fi
fi
ok "pnpm $(pnpm --version)"

# ------------------------------------------------------------------ postgresql 18
step "PostgreSQL $PG_MAJOR on port $PG_PORT"
if [ "$OS" = mac ]; then
  brew list postgresql@$PG_MAJOR >/dev/null 2>&1 || brew install postgresql@$PG_MAJOR >/dev/null
  setup_path
  PGCONF="$(brew --prefix)/var/postgresql@$PG_MAJOR/postgresql.conf"
  if [ ! -f "$PGCONF" ]; then
    initdb --locale=C -E UTF-8 "$(brew --prefix)/var/postgresql@$PG_MAJOR" >/dev/null
  fi
  DB_USER="$(whoami)"; DB_AUTH=""
else
  if ! dpkg -s postgresql-$PG_MAJOR >/dev/null 2>&1; then
    sudo install -d /usr/share/postgresql-common/pgdg
    sudo curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
      | sudo tee /etc/apt/sources.list.d/pgdg.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq postgresql-$PG_MAJOR >/dev/null
  fi
  setup_path
  PGCONF="/etc/postgresql/$PG_MAJOR/main/postgresql.conf"
  DB_USER="hajiri"
fi

# Pin the port in postgresql.conf (idempotent).
if ! grep -Eq "^port = $PG_PORT\b" "$PGCONF"; then
  if [ "$OS" = mac ]; then sed -i '' -E "s/^#?port = [0-9]+/port = $PG_PORT/" "$PGCONF"
  else sudo sed -i -E "s/^#?port = [0-9]+/port = $PG_PORT/" "$PGCONF"; fi
  if [ "$OS" = mac ]; then brew services restart postgresql@$PG_MAJOR >/dev/null; else sudo systemctl restart postgresql; fi
fi
pg_service_start
wait_for_pg localhost "$PG_PORT" || fail "PostgreSQL did not come up on port $PG_PORT"
ok "postgres $(psql -p "$PG_PORT" -d postgres -Atc 'show server_version' 2>/dev/null || sudo -u postgres psql -p "$PG_PORT" -Atc 'show server_version')"

# ------------------------------------------------------------------ role + databases
step "Databases"
if [ "$OS" = linux ]; then
  # Dedicated role with a generated password; stored only in the .env files.
  if ! sudo -u postgres psql -p "$PG_PORT" -Atc "select 1 from pg_roles where rolname='hajiri'" | grep -q 1; then
    DB_PASS="$(openssl rand -hex 16)"
    sudo -u postgres psql -p "$PG_PORT" -qc "CREATE ROLE hajiri LOGIN PASSWORD '$DB_PASS'"
    DB_AUTH="hajiri:$DB_PASS@"
  else
    existing="$(grep -h '^DATABASE_URL=' apps/api/.env 2>/dev/null | head -1 | cut -d= -f2-)"
    [ -n "$existing" ] || fail "role hajiri exists but apps/api/.env is missing; drop the role or restore the .env file"
    DB_AUTH="$(echo "$existing" | sed -E 's#^postgres://([^@]*@).*#\1#')"
  fi
  for db in hajiri hajiri_test; do
    sudo -u postgres psql -p "$PG_PORT" -Atc "select 1 from pg_database where datname='$db'" | grep -q 1 \
      || sudo -u postgres createdb -p "$PG_PORT" -O hajiri "$db"
  done
else
  for db in hajiri hajiri_test; do
    psql -p "$PG_PORT" -d postgres -Atc "select 1 from pg_database where datname='$db'" | grep -q 1 || createdb -p "$PG_PORT" "$db"
  done
fi
DATABASE_URL="postgres://${DB_AUTH}localhost:$PG_PORT/hajiri"
DATABASE_URL_TEST="postgres://${DB_AUTH}localhost:$PG_PORT/hajiri_test"
ok "hajiri, hajiri_test"

# ------------------------------------------------------------------ env files
step "Environment files"
if [ ! -f apps/api/.env ]; then
  cat > apps/api/.env <<ENV
PORT=3000
DATABASE_URL=$DATABASE_URL
DATABASE_URL_TEST=$DATABASE_URL_TEST
LOG_LEVEL=info
ENV
  ok "wrote apps/api/.env"
else ok "apps/api/.env exists (kept)"; fi

if [ ! -f apps/gateway/.env ]; then
  SIGNING_KEY="$(node -e "console.log(require('node:crypto').generateKeyPairSync('ed25519').privateKey.export({type:'pkcs8',format:'der'}).toString('base64'))")"
  cat > apps/gateway/.env <<ENV
PORT=8080
DATABASE_URL=$DATABASE_URL
LOG_LEVEL=info
GATEWAY_SIGNING_KEY=$SIGNING_KEY
ENV
  ok "wrote apps/gateway/.env (new signing key generated)"
else
  if ! grep -q '^GATEWAY_SIGNING_KEY=' apps/gateway/.env; then
    SIGNING_KEY="$(node -e "console.log(require('node:crypto').generateKeyPairSync('ed25519').privateKey.export({type:'pkcs8',format:'der'}).toString('base64'))")"
    echo "GATEWAY_SIGNING_KEY=$SIGNING_KEY" >> apps/gateway/.env
    ok "apps/gateway/.env exists; added a signing key"
  else ok "apps/gateway/.env exists (kept)"; fi
fi

# ------------------------------------------------------------------ dependencies + schema
step "Dependencies"
pnpm install --frozen-lockfile 2>&1 | grep -E "^(Done|Already)" || true
ok "node_modules ready"

step "Database schema and seeds"
pnpm --filter @hajiri/api --silent db:migrate
pnpm --filter @hajiri/api --silent db:seed:bs
if [ "$WITH_DEMO" = 1 ]; then pnpm --filter @hajiri/api --silent db:seed:demo; ok "demo data loaded"; fi
ok "schema up to date"

if [ "$WITH_TESTS" = 1 ]; then
  step "Tests"
  pnpm -r --silent test
fi

printf '\n%sHajiri is installed.%s\n\n' "$BOLD" "$RESET"
echo "  Start everything:         ./scripts/start.sh"
echo "  Run in the background:    ./scripts/start.sh --daemon   (stop with ./scripts/stop.sh)"
echo "  Auto-start on boot:       ./scripts/install-service.sh"
echo "  Update from GitHub:       ./scripts/update.sh"
echo
echo "  API:      http://localhost:3000/health"
echo "  Gateway:  http://localhost:8080/health   (point terminals at http://<this-machine>:8080/iclock)"
