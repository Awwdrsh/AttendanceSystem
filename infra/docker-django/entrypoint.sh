#!/bin/sh
# Container entrypoint for the Django stack:  web | worker | gateway | manage <args...>
#   web:     wait for PostgreSQL, apply migrations and idempotent seeds, create the first admin if HAJIRI_ADMIN_EMAIL and
#            HAJIRI_ADMIN_PASSWORD are set and no such user exists, then serve the web app with waitress.
#   worker:  background jobs (start it after web has migrated the database).
#   gateway: device gateway on GATEWAY_PORT (default 8080).
set -eu
cd "${HAJIRI_SERVER_DIR:-/app/server}"
svc="${1:-web}"; [ $# -gt 0 ] && shift
export HAJIRI_SERVICE="$svc"
# Compose passes settings left blank in .env as empty strings. Django refuses an empty SECRET_KEY (the settings fall
# back to PASSWORD_PEPPER only when DJANGO_SECRET_KEY is unset), and an empty ALLOWED_HOSTS refuses every request.
[ -n "${DJANGO_SECRET_KEY:-}" ] || unset DJANGO_SECRET_KEY
[ -n "${ALLOWED_HOSTS:-}" ] || unset ALLOWED_HOSTS

wait_db() {
  python /app/scripts/django_ops.py wait-db 120 || { echo "database not reachable"; exit 1; }
}

case "$svc" in
  web)
    wait_db
    python manage.py migrate --noinput
    python manage.py seed_bs_calendar
    COMPANY_CODE="${COMPANY_CODE:-DEMO}"
    if [ "$COMPANY_CODE" = "DEMO" ]; then
      python manage.py seed_demo --database-name "$(python /app/scripts/django_ops.py db-name)" --yes
    else
      python /app/scripts/django_ops.py ensure-company "$COMPANY_CODE" "${COMPANY_NAME:-$COMPANY_CODE}" >/dev/null
    fi
    python manage.py seed_defaults --company "$COMPANY_CODE" || true
    if [ -n "${HAJIRI_ADMIN_EMAIL:-}" ] && [ -n "${HAJIRI_ADMIN_PASSWORD:-}" ] && ! python /app/scripts/django_ops.py has-user "$HAJIRI_ADMIN_EMAIL"; then
      # The password stays in the environment (HAJIRI_PASSWORD), never on a command line.
      HAJIRI_PASSWORD="$HAJIRI_ADMIN_PASSWORD" python manage.py create_admin --email "$HAJIRI_ADMIN_EMAIL" --company "$COMPANY_CODE" || true
    fi
    exec python -m waitress --host=0.0.0.0 --port="${PORT:-3000}" --threads="${WEB_THREADS:-8}" --ident=hajiri --channel-timeout=120 \
      config.wsgi:application
    ;;
  worker)
    wait_db
    exec python manage.py run_worker
    ;;
  gateway)
    wait_db
    # PORT belongs to the web app; the gateway listens on GATEWAY_PORT.
    exec env -u PORT python manage.py run_gateway --port "${GATEWAY_PORT:-8080}"
    ;;
  manage)
    exec python manage.py "$@"
    ;;
  *)
    exec "$svc" "$@"
    ;;
esac
