# Migration to Django

Branch `django-rewrite`. The TypeScript stack stays in place and keeps working until the Django version passes the same
checks; the new code lives in `server/`. Decisions taken at the start (26 Oct 2026):

- **Everything moves to Python**, including the web app: Django templates with HTMX for partial updates, replacing
  React and Mantine. The CoreValley visual identity, command palette, light and dark mode and BS dates are kept.
- **Django owns the schema** from now on: models for the existing tables, an initial migration that is fake-applied on
  existing databases (`migrate --fake-initial`), so no data moves. Triggers and functions that the SQL migrations
  create (append-only punches, hash-chained audit log) are carried over as `RunSQL` operations.
- **Side by side:** both stacks read the same `.env` and the same database, so modules can be compared one at a time.
  `DATA_ENCRYPTION_KEY` and `PASSWORD_PEPPER` must not change, or stored PAN, bank and ID numbers and existing
  passwords stop working.

## Scale

| Part | TS today | Django target |
|---|---|---|
| Engines (BS calendar, attendance, payroll) | 3.2k lines, property and golden tests | `server/engines/`, pure Python, no Django imports |
| API (36 modules, worker, CLI, seeds) | 38k lines, 28 SQL migrations (242 tables incl. punch partitions), 52 test files | one Django app per module under `server/hajiri/` |
| Device gateway (ZKTeco ADMS, Hikvision ISAPI) | 2k lines | Django app served as its own process on :8080 |
| Web app | 24k lines of React | Django templates, HTMX, a little Alpine.js |

## Phases

Each phase ends with its tests green and is committed on its own.

1. **Engines.** Port the three packages. Parity is checked against JSON dumped from the TS code (`tests/parity/`),
   in addition to the ported unit, golden and property tests. *BS calendar and attendance done (attendance: 3,007 seeded parity cases match exactly, hashes included); payroll done (28 golden
   fixtures exact; 672 payslips, 5,490 formulas and 1,500 TDS cases match the TS output).* The stored `inputs_hash` covers the shift object including its id, so services must pass
   `Shift(id=<row id>, ...)` or recomputed hashes will not match the database. Same-instant punches are ordered as
   Node's `localeCompare` orders ASCII ids.
2. **Schema.** *Done.* `tools/gen_models.py` turns `inspectdb` output for a database built by the TS migrations
   into `hajiri/db/models/` (180 models, one file per domain, in a single app labelled `hajiri` so cross-domain foreign
   keys need no migration ordering), adding array and range types and `db_default`s from the catalog. Migration
   `0001_initial` holds the models as state and, on the database side, runs `0001_schema.sql` (a `pg_dump
   --schema-only` of TS migration 0030) on an empty database, or adopts a database already at 0030 without touching
   it. A database at an older TS migration is refused until `pnpm db:migrate` has run. A fresh Django database dumps
   byte-identical to the TS one (69 triggers, 409 checks, 799 indexes, exclusion constraints, punch partitions).
   Points to remember when writing services:
   - Foreign keys are `DO_NOTHING`; PostgreSQL applies the real `ON DELETE` rules.
   - A non-null text field with no database default is sent as `''` when left unset, and most of them have CHECK
     constraints, so set them explicitly.
   - Raw SQL goes through `hajiri.core.db` (`rows`, `one`, `execute`), which decodes json/jsonb; a bare cursor
     returns them as text.
   - Do not wrap a step that counts failures (logins, code attempts) in `transaction.atomic`: raising the error
     would roll the count back.
   - Monthly `punch_event` partitions are created by a job (ported in phase 3), not by migrations.
3. **Platform.** *Done.* Settings and config, sign-in compatible with existing password hashes, sessions with lockout
   and idle timeout, two-step sign-in (required per role), roles and scoping (company, team, department, branch),
   field encryption with the same ciphertext format (byte-compatible both ways, tested against the TS code),
   hash-chained audit log, file storage, notifications (SMTP or email webhook, SMS, push and generic webhooks), feature
   switches. The background worker (`hajiri/worker`) is a database-backed queue with the TS cadences (recompute queue
   every tick, delivery 30 s, offline devices 60 s, SLA 10 min, auto clock-out and digest 5 min, nightly sweep,
   accrual and punch partitions at 00:30 NPT, then the 13 daily module jobs of `module-jobs.ts` plus the kiosk
   heartbeat), so a single-machine install still needs only PostgreSQL. `RUN_WORKER=true` starts it in the web process.
4. **Gateway.** *Done.* ADMS and ISAPI endpoints byte-compatible with the terminals; signed idempotent ingest;
   reconcile and the simulator (`manage.py run_gateway`, `gateway_simulate`, `gateway_reconcile`).
5. **Modules.** *Done.* Every TS module has a Django app with a service layer and pages; all 15 approval decision
   handlers are registered; every exported TS service function has a Python counterpart; all 122 React routes and
   hub tabs resolve to Django pages; JSON endpoints are kept for terminals, kiosk tablets and Bearer-token clients.
   All 78 TS test files (806 cases, including the cross-cutting security, roles-flows, QA and fixes suites, the
   gateway suites and the web `lib/` tests) are ported; the Python suite has 2,006 tests.
6. **Operations.** *Done except where noted below.* `scripts/*-django.sh` (setup, start/stop, run, boot service,
   update), `infra/docker-django/` and `infra/docker-compose.django.yml`, `manage.py seed_demo` and
   `seed_demo_suite`, the CLI commands (`create_admin`, `reset_password`, `seed_defaults`, `seed_demo_suite`), PWA
   manifest and service worker, `check --deploy` clean with the production settings listed below.
7. **Cutover.** *Not started.* Run both stacks against a copy of the live database and compare outputs (attendance
   days, payroll runs, reports); then remove `apps/`, `packages/` and the Node toolchain.

## Differences from the TS app and remaining gaps

Checked on 27 Oct 2026 by comparing README "What is included" row by row, every exported TS function, every TS route
handler's service calls, the React routes, tabs and buttons, the decision handlers, the worker jobs and the CLI against
the Django code. Nothing in the capability list is missing. What still differs or is unverified:

- **Not run on real infrastructure.** The Docker image and compose file, `install-service-django.sh` (launchd and
  systemd units) and the Linux branches of `setup-django.sh` have not been run: there is no Docker on the development
  Mac and the service script installs real boot agents. Run them on a test machine before relying on them.
- **Cutover (phase 7)** has not been done: no side-by-side comparison against a copy of a live database yet.
- **UI adaptations** (same data and actions, different controls): Mantine's searchable multi-select and dropdown
  menus are checkbox lists or plain buttons; a few filters reload the whole page instead of one table; names sort
  with Python `casefold()` rather than the browser's `localeCompare`, so the order of accented names can differ.
- **Profile photos** are shrunk in the browser to a 320 px square JPEG before upload, as `AvatarEditor.tsx` did
  (`Hajiri.squareJpeg` in `static/js/app.js`). The server does not resize: an upload sent without the page (for
  example with JavaScript off) is stored as sent, which the TS server also did. Server-side resizing would need Pillow.
- **BS calendar cross-check** against the `@remotemerge/nepali-date-converter` JS library is not run in Python; the
  day-by-day parity test against the TS table (which is cross-checked there) covers it.
- **Smoke coverage:** a signed-in walk of 683 of the 758 URL patterns per role returned no 500s; the other 75 need
  objects the smoke data did not have and are covered only by their slices' view tests.
- **Same as TS, by design:** regularisation keeps its own `manual_device_id` (TS `regularisation/routes.ts` had its
  own `manualDeviceId`, with a shorter device name); the TS module's never-called JSON routes (key-result and KPI
  edit/delete, asset edit and history, org-unit and branch edit through `/settings`) have services but no page,
  because the React app never offered them.
- The statutory and hardware assumptions in README "Known gaps and assumptions" apply unchanged.

## Environment notes

- Business dates in SQL. The Django connection runs in UTC (Django sets the session TimeZone itself), while the TS pool
  ran in Asia/Kathmandu, so SQL `current_date` was a Nepal date there and is a UTC date here: a day behind between
  18:15 and 24:00 UTC. Raw SQL uses `(now() AT TIME ZONE 'Asia/Kathmandu')::date` where the TS used `current_date`,
  or passes `hajiri.core.clock.npt_today()` as a parameter (also as the third argument of `hj_manages`). Never use
  `current_date`, `now()::date`, `date.today()` or a UTC date for a business date, in code or in tests.
- Running the whole suite: each pytest process needs its own `DJANGO_TEST_DATABASE`. Creating several test databases
  at the same moment runs Postgres out of lock slots ("out of shared memory"), so create them one after another
  (`--create-db --reuse-db` on a small folder) and then run the shards in parallel with `--reuse-db`.
- `manage.py check --deploy` is clean with DJANGO_DEBUG=false, a long DJANGO_SECRET_KEY, COOKIE_SECURE=true,
  ALLOWED_HOSTS, SECURE_SSL_REDIRECT=true, SECURE_HSTS_SECONDS, SECURE_HSTS_INCLUDE_SUBDOMAINS=true and
  SECURE_HSTS_PRELOAD=true. The HTTPS settings are off by default because most installs run on a LAN over plain
  HTTP; TRUST_X_FORWARDED_PROTO=true adds SECURE_PROXY_SSL_HEADER behind a reverse proxy. CORS_ORIGINS (as in the TS
  API) and CSRF_TRUSTED_ORIGINS are read from the environment; CORS origins are trusted for CSRF too.

- Python 3.13. The Homebrew Python 3.14 on the development Mac has a broken `pyexpat` (built against a newer expat than
  the system one), which stops `venv` from installing pip.
- Tests use their own database, `hajiri_django_test` (`DJANGO_TEST_DATABASE`); `hajiri_test` belongs to the TS suite.
- `hajiri_schema_ref` is a scratch database built by the TS migrations, used by `tools/gen_models.py`.
- `cd server && python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/pytest`.
