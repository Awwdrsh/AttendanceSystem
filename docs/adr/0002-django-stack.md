# ADR 0002: One Django stack; the TypeScript stack removed

Date: 27 Oct 2026 (BS 2083 Kartik 11). Status: accepted. Supersedes the stack choices of ADR 0001.

## Decision

Hajiri runs on one Python stack: Django 6 with server-rendered pages (HTMX, a little Alpine.js), Django REST framework
for the JSON endpoints, `django.tasks` with the database backend for background work, and the device gateway as a
second Django process (`manage.py run_gateway`). The TypeScript monorepo (`apps/api`, `apps/web`, `apps/gateway`,
`packages/*`, pnpm, Node) is removed. The canonical scripts (`scripts/setup.sh`, `start.sh`, `stop.sh`, `run.sh`,
`install-service.sh`, `update.sh`) and Docker files (`infra/docker/`, `infra/docker-compose.yml`) are the Django ones.

## Why

- One language and one process model for the whole product; a small team maintains one codebase, not two.
- Django owns the schema with migrations; the ORM with company-scoped managers makes the multi-company rule (every
  query scoped to the active company) a property of the code rather than a review checklist.
- Server-rendered pages need no build step and no Node on the server.

## Consequences

- Existing installs keep their database (migration `0001_initial` adopts a schema built by TypeScript migration
  0030) and must keep `DATA_ENCRYPTION_KEY`, `PASSWORD_PEPPER` and `GATEWAY_SIGNING_KEY`: the settings now come only
  from `server/.env` and `server/gateway.env`, and `scripts/setup.sh` copies them over from the old `apps/*/.env`
  files once (README, "Moving from the TypeScript stack").
- Ciphertext, password-hash and punch-signature formats are unchanged, so data written by either stack stays readable.
- The engines' parity with the TypeScript engines is now checked only against fixtures recorded before the removal
  (`server/tests/parity/fixtures/`, the payroll golden files); the scripts that dumped them are gone with the TS code
  (commit 1bbe88b and earlier have them).
- The side-by-side comparison against a copy of a live database planned for the cutover was not done before the
  removal; the first payroll cycles on the Django stack should be checked against the previous month's results.
