#!/usr/bin/env bash
# One-command setup of the Django (Python) stack on a fresh macOS or Ubuntu/Debian machine:
#   git clone <repo> hajiri && cd hajiri && ./scripts/setup-django.sh
# Installs Python 3.13 and PostgreSQL 18, creates the database, writes server/.env and server/gateway.env (new secrets,
# or the ones the TypeScript stack already uses), builds server/.venv, applies migrations, seeds and creates the first
# admin user. Safe to re-run: every step is idempotent.
#
# Options:
#   --no-demo      do not load the demo company/employees/device
#   --with-tests   install the test tools and run the Python test suite at the end
#   --pg-port N    PostgreSQL port (default 5433 on macOS, 5432 on Linux)
#   --company CODE --company-name "Name"   create your real company instead of the demo one (implies --no-demo)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib-django.sh"
cd "$ROOT"

WITH_DEMO=1; WITH_TESTS=0; PG_PORT="${HAJIRI_PG_PORT:-$DEFAULT_PG_PORT}"; COMPANY_CODE="${HAJIRI_COMPANY_CODE:-DEMO}"; COMPANY_NAME="${HAJIRI_COMPANY_NAME:-My Company}"
while [ $# -gt 0 ]; do
  case "$1" in
    --no-demo) WITH_DEMO=0 ;;
    --with-tests) WITH_TESTS=1 ;;
    --pg-port) PG_PORT="$2"; shift ;;
    --company) COMPANY_CODE="$2"; shift ;;
    --company-name) COMPANY_NAME="$2"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) fail "unknown option $1" ;;
  esac
  shift
done

[ "$OS" = other ] && fail "Unsupported OS. Use macOS or Ubuntu/Debian, or run with Docker (see infra/docker-compose.django.yml)."
if [ "$COMPANY_CODE" != "DEMO" ]; then
  WITH_DEMO=0
  [[ "$COMPANY_CODE" =~ ^[A-Za-z0-9_-]{1,20}$ ]] || fail "--company must be 1-20 letters, digits, _ or -"
fi
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

# ------------------------------------------------------------------ python 3.13
step "Python $PY_VERSION"
if ! PY="$(find_python313)"; then
  if [ "$OS" = mac ]; then
    brew install python@$PY_VERSION >/dev/null
  else
    if ! sudo apt-get install -y -qq "python$PY_VERSION" "python$PY_VERSION-venv" >/dev/null 2>&1; then
      [ "${ID:-}" = ubuntu ] || fail "python$PY_VERSION is not available from APT on this Debian release; install Python $PY_VERSION and re-run"
      sudo apt-get install -y -qq software-properties-common >/dev/null
      sudo add-apt-repository -y ppa:deadsnakes/ppa >/dev/null
      sudo apt-get update -qq
      sudo apt-get install -y -qq "python$PY_VERSION" "python$PY_VERSION-venv" >/dev/null
    fi
  fi
  PY="$(find_python313)" || fail "Python $PY_VERSION not found after installing it"
fi
ok "$("$PY" --version) ($PY)"

# ------------------------------------------------------------------ postgresql 18
step "PostgreSQL $PG_MAJOR on port $PG_PORT"
if [ "$OS" = mac ]; then
  brew list postgresql@$PG_MAJOR >/dev/null 2>&1 || brew install postgresql@$PG_MAJOR >/dev/null
  setup_path
  PGCONF="$(brew --prefix)/var/postgresql@$PG_MAJOR/postgresql.conf"
  if [ ! -f "$PGCONF" ]; then
    initdb --locale=C -E UTF-8 "$(brew --prefix)/var/postgresql@$PG_MAJOR" >/dev/null
  fi
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

# ------------------------------------------------------------------ role + database
step "Database"
# An install that already has a database (this script ran before, or the TypeScript stack is set up) keeps it: both
# stacks share one database during the migration.
EXISTING_URL="$(django_database_url)"
if [ -n "$EXISTING_URL" ]; then
  DATABASE_URL="$EXISTING_URL"
  DB_NAME="$(pg_db_from_url "$DATABASE_URL")"
  ok "using the existing database $DB_NAME ($(pg_host_from_url "$DATABASE_URL"):$(pg_port_from_url "$DATABASE_URL"))"
else
  DB_AUTH=""
  if [ "$OS" = linux ]; then
    # Dedicated role with a generated password; stored only in server/.env. CREATEDB lets the test runner make its
    # own throw-away database.
    if ! sudo -u postgres psql -p "$PG_PORT" -Atc "select 1 from pg_roles where rolname='hajiri'" | grep -q 1; then
      DB_PASS="$(openssl rand -hex 16)"
      sudo -u postgres psql -p "$PG_PORT" -qc "CREATE ROLE hajiri LOGIN CREATEDB PASSWORD '$DB_PASS'"
      DB_AUTH="hajiri:$DB_PASS@"
    else
      fail "role hajiri exists but server/.env and apps/api/.env are missing; drop the role or restore the .env file"
    fi
    sudo -u postgres psql -p "$PG_PORT" -Atc "select 1 from pg_database where datname='hajiri'" | grep -q 1 \
      || sudo -u postgres createdb -p "$PG_PORT" -O hajiri hajiri
  else
    psql -p "$PG_PORT" -d postgres -Atc "select 1 from pg_database where datname='hajiri'" | grep -q 1 || createdb -p "$PG_PORT" hajiri
  fi
  DATABASE_URL="postgres://${DB_AUTH}localhost:$PG_PORT/hajiri"
  DB_NAME=hajiri
  ok "hajiri"
fi

# ------------------------------------------------------------------ python environment
step "Python environment (server/.venv)"
if [ -x "$VENV_PY" ] && ! "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' 2>/dev/null; then
  warn "server/.venv is not Python $PY_VERSION; rebuilding it"
  rm -rf "$VENV"
fi
if [ ! -x "$VENV_PY" ]; then
  "$PY" -m venv "$VENV"
  "$VENV_PY" -m pip install -q --upgrade pip >/dev/null 2>&1 || warn "could not upgrade pip; continuing with $("$VENV_PY" -m pip --version | awk '{print $2}')"
fi
REQS="$SERVER/requirements.txt"; [ "$WITH_TESTS" = 1 ] && REQS="$SERVER/requirements-dev.txt"
if ! "$VENV_PY" -m pip install -q -r "$REQS" >/tmp/hajiri-pip.log 2>&1; then
  tail -20 /tmp/hajiri-pip.log
  fail "pip install failed (full log: /tmp/hajiri-pip.log)"
fi
# The web server and the gateway are served by waitress (pure Python, works the same on macOS and Linux).
"$VENV_PY" -c 'import waitress' 2>/dev/null || "$VENV_PY" -m pip install -q 'waitress==3.0.2' >/dev/null
ok "$("$VENV_PY" --version), Django $("$VENV_PY" -c 'import django; print(django.get_version())') in server/.venv"

# ------------------------------------------------------------------ env files
step "Environment files"
TS_ENV="$ROOT/apps/api/.env"; TS_GW_ENV="$ROOT/apps/gateway/.env"
# The field-encryption key and the password pepper must be the ones already in use, or stored PAN, bank and ID numbers
# and every existing password stop working. Reuse them when the TypeScript stack (or an earlier run) has them.
pick_secret() {   # pick_secret KEY BYTES
  local v
  v="$(env_get "$WEB_ENV" "$1")"; [ -n "$v" ] || v="$(env_get "$TS_ENV" "$1")"
  [ -n "$v" ] || v="$(ops secret "$2")"
  echo "$v"
}
NEW_WEB=0; [ -f "$WEB_ENV" ] || NEW_WEB=1
touch "$WEB_ENV"; chmod 600 "$WEB_ENV"
env_has_value "$WEB_ENV" DATABASE_URL || env_set "$WEB_ENV" DATABASE_URL "$DATABASE_URL"
DEK="$(pick_secret DATA_ENCRYPTION_KEY 32)"; env_has_value "$WEB_ENV" DATA_ENCRYPTION_KEY || env_set "$WEB_ENV" DATA_ENCRYPTION_KEY "$DEK"
PEPPER="$(pick_secret PASSWORD_PEPPER 24)"; env_has_value "$WEB_ENV" PASSWORD_PEPPER || env_set "$WEB_ENV" PASSWORD_PEPPER "$PEPPER"
env_has_value "$WEB_ENV" DJANGO_SECRET_KEY || env_set "$WEB_ENV" DJANGO_SECRET_KEY "$(ops django-secret)"
env_has_value "$WEB_ENV" PORT || env_set "$WEB_ENV" PORT 3000
env_has_value "$WEB_ENV" LOG_LEVEL || env_set "$WEB_ENV" LOG_LEVEL info
env_has_value "$WEB_ENV" COOKIE_SECURE || env_set "$WEB_ENV" COOKIE_SECURE false
# The worker runs as its own service (start-django.sh, install-service-django.sh), not inside the web process.
env_has_value "$WEB_ENV" RUN_WORKER || env_set "$WEB_ENV" RUN_WORKER false
if ! env_has_value "$WEB_ENV" ALLOWED_HOSTS; then
  HOSTS="localhost,127.0.0.1"
  SHORT="$(hostname -s 2>/dev/null || hostname)"; [ -n "$SHORT" ] && HOSTS="$HOSTS,$SHORT,$SHORT.local"
  if [ "$OS" = mac ]; then IPS="$(ipconfig getifaddr en0 2>/dev/null || true) $(ipconfig getifaddr en1 2>/dev/null || true)"
  else IPS="$(hostname -I 2>/dev/null || true)"; fi
  for ip in $IPS; do case "$ip" in *:*) ;; *) HOSTS="$HOSTS,$ip" ;; esac; done
  env_set "$WEB_ENV" ALLOWED_HOSTS "$HOSTS"
fi
if [ "$NEW_WEB" = 1 ]; then
  if [ -f "$TS_ENV" ]; then ok "wrote server/.env (database and secrets shared with apps/api/.env)"
  else ok "wrote server/.env (new encryption key, pepper and Django secret generated)"; fi
else
  ok "server/.env exists (kept; missing keys added)"
fi
if [ -f "$TS_ENV" ]; then
  for k in DATA_ENCRYPTION_KEY PASSWORD_PEPPER; do
    a="$(env_get "$TS_ENV" "$k")"; b="$(env_get "$WEB_ENV" "$k")"
    [ -z "$a" ] || [ "$a" = "$b" ] || fail "$k differs between apps/api/.env and server/.env; both stacks must use the same value"
  done
fi

NEW_GW=0; [ -f "$GW_ENV" ] || NEW_GW=1
touch "$GW_ENV"; chmod 600 "$GW_ENV"
env_has_value "$GW_ENV" PORT || env_set "$GW_ENV" PORT "$(env_get "$TS_GW_ENV" PORT 8080)"
if ! env_has_value "$GW_ENV" GATEWAY_SIGNING_KEY; then
  # Keep the TS gateway's key when there is one: what devices and integrations verified stays verifiable.
  SK="$(env_get "$TS_GW_ENV" GATEWAY_SIGNING_KEY)"; [ -n "$SK" ] || SK="$(ops signing-key)"
  env_set "$GW_ENV" GATEWAY_SIGNING_KEY "$SK"
fi
# The gateway must share the web app's data-encryption key so templates it stores can be read by the web app.
[ "$(env_get "$GW_ENV" DATA_ENCRYPTION_KEY)" = "$(env_get "$WEB_ENV" DATA_ENCRYPTION_KEY)" ] || env_set "$GW_ENV" DATA_ENCRYPTION_KEY "$(env_get "$WEB_ENV" DATA_ENCRYPTION_KEY)"
env_has_value "$GW_ENV" GATEWAY_MAX_PENDING_DEVICES || env_set "$GW_ENV" GATEWAY_MAX_PENDING_DEVICES 50
env_has_value "$GW_ENV" GATEWAY_MAX_CLOCK_SKEW_MIN || env_set "$GW_ENV" GATEWAY_MAX_CLOCK_SKEW_MIN 10
env_has_value "$GW_ENV" GATEWAY_RAW_DIR || env_set "$GW_ENV" GATEWAY_RAW_DIR data/raw
env_has_value "$GW_ENV" GATEWAY_ALLOW_DANGEROUS_COMMANDS || env_set "$GW_ENV" GATEWAY_ALLOW_DANGEROUS_COMMANDS false
if [ "$NEW_GW" = 1 ]; then ok "wrote server/gateway.env (signing key $( [ -n "$(env_get "$TS_GW_ENV" GATEWAY_SIGNING_KEY)" ] && echo 'shared with apps/gateway/.env' || echo 'generated'); encryption key shared with the web app)"
else ok "server/gateway.env exists (kept; missing keys added)"; fi
for f in "$WEB_ENV" "$GW_ENV"; do chmod 600 "$f"; done

# ------------------------------------------------------------------ schema + seeds
step "Database schema and seeds"
manage migrate --noinput >/tmp/hajiri-migrate.log 2>&1 || { tail -20 /tmp/hajiri-migrate.log; fail "migrate failed (full log: /tmp/hajiri-migrate.log)"; }
manage seed_bs_calendar >/dev/null
if [ "$COMPANY_CODE" != "DEMO" ]; then
  ops ensure-company "$COMPANY_CODE" "$COMPANY_NAME" >/dev/null
  ok "company $COMPANY_CODE ready"
fi
if [ "$WITH_DEMO" = 1 ]; then
  manage seed_demo --database-name "$(ops db-name)" --yes >/dev/null
  ok "demo data loaded"
fi
if ops has-company "$COMPANY_CODE"; then
  manage seed_defaults --company "$COMPANY_CODE" >/dev/null
else
  # --no-demo without --company: create the demo company itself (no people) so there is somewhere to sign in.
  ops ensure-company "$COMPANY_CODE" "$COMPANY_NAME" >/dev/null
  manage seed_defaults --company "$COMPANY_CODE" >/dev/null
fi
ok "schema up to date; defaults seeded"

step "Admin user"
ADMIN_EMAIL="${HAJIRI_ADMIN_EMAIL:-admin@hajiri.local}"
[[ "$ADMIN_EMAIL" =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+$ ]] || fail "HAJIRI_ADMIN_EMAIL is not a valid email"
if ops has-user "$ADMIN_EMAIL"; then
  ok "$ADMIN_EMAIL exists (reset with: server/.venv/bin/python server/manage.py reset_password --email $ADMIN_EMAIL --password <new>)"
else
  ADMIN_PASSWORD="$(ops password)"
  # Passed in the environment, not on the command line, so it never shows up in the process list.
  HAJIRI_PASSWORD="$ADMIN_PASSWORD" manage create_admin --email "$ADMIN_EMAIL" --company "$COMPANY_CODE" >/dev/null
  ( umask 077; printf 'email=%s\npassword=%s\n' "$ADMIN_EMAIL" "$ADMIN_PASSWORD" > "$ROOT/.admin-credentials" )
  ok "created $ADMIN_EMAIL  (temporary password in .admin-credentials; you will be asked to change it at first sign-in)"
fi

step "Checks"
manage check >/dev/null && ok "Django configuration"
manage run_gateway --check | sed 's/^/  /'

if [ "$WITH_TESTS" = 1 ]; then
  step "Tests"
  (cd "$SERVER" && DJANGO_TEST_DATABASE="${DJANGO_TEST_DATABASE:-hajiri_django_test}" "$VENV/bin/pytest" -q)
fi

WEB_PORT="$(web_port)"; GW_PORT="$(gateway_port)"
printf '\n%sHajiri (Django) is installed.%s\n\n' "$BOLD" "$RESET"
echo "  Start everything:         ./scripts/start-django.sh"
echo "  Run in the background:    ./scripts/start-django.sh --daemon   (stop with ./scripts/stop-django.sh)"
echo "  Auto-start on boot:       ./scripts/install-service-django.sh"
echo "  Update from GitHub:       ./scripts/update-django.sh"
echo
echo "  Web app:  http://localhost:$WEB_PORT  (login with .admin-credentials)"
echo "  Health:   http://localhost:$WEB_PORT/health"
echo "  Gateway:  http://localhost:$GW_PORT/health   (point terminals at http://<this-machine>:$GW_PORT/iclock)"
if [ -f "$ROOT/apps/api/.env" ] && [ "$WEB_PORT" = "$(env_get "$ROOT/apps/api/.env" PORT 3000)" ]; then
  echo
  warn "the TypeScript API uses port $WEB_PORT too: run one stack at a time, or set PORT in server/.env (and in server/gateway.env)"
fi
