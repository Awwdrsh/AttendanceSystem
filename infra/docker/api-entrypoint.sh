#!/bin/sh
# API container entrypoint: wait for PostgreSQL, apply migrations and idempotent seeds, create the first admin if
# HAJIRI_ADMIN_EMAIL/HAJIRI_ADMIN_PASSWORD are set and no such user exists, then start the server.
set -eu
cd /app/apps/api
i=0
until node -e "const {Client}=require('pg');const c=new Client({connectionString:process.env.DATABASE_URL});c.connect().then(()=>c.end()).catch(()=>process.exit(1))" 2>/dev/null; do
  i=$((i+1)); [ "$i" -gt 60 ] && { echo "database not reachable"; exit 1; }
  sleep 2
done
node --import tsx src/db/migrate.ts
node --import tsx src/db/seed-bs-calendar.ts
COMPANY_CODE="${COMPANY_CODE:-DEMO}"
if [ "$COMPANY_CODE" = "DEMO" ]; then node --import tsx src/db/seed-demo.ts; fi
node --import tsx src/cli.ts seed-defaults --company "$COMPANY_CODE" || true
if [ -n "${HAJIRI_ADMIN_EMAIL:-}" ] && [ -n "${HAJIRI_ADMIN_PASSWORD:-}" ]; then
  node --import tsx src/cli.ts create-admin --email "$HAJIRI_ADMIN_EMAIL" --password "$HAJIRI_ADMIN_PASSWORD" --company "$COMPANY_CODE" 2>/dev/null || true
fi
exec node --import tsx src/server.ts
