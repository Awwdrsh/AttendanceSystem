# Development guide

Hajiri is an in-house biometric attendance, HR and payroll platform for companies in Nepal. **plan.md is the
specification**; reference its section numbers in commit messages and comments when encoding a rule (e.g. "plan
§10.4"). The backend is one Django 6 project in `server/` (Python 3.13): a JSON API under `/api/v1/` with the paths,
bodies and response shapes of the original TypeScript API. The web app is the React app in `frontend/` (React 19,
Mantine, react-router, Vite; **plain JavaScript, never TypeScript**), whose production build Django serves from
`server/frontend_build/`. **docs/django-conventions.md is the rulebook** for models, scoping, serializers, DRF views,
background work and tests; follow it literally. The help desk (`server/hajiri/helpdesk/`) is the worked example.

## Layout

| Path | What |
|---|---|
| `server/config/` | `settings.py` (reads `server/.env`), `urls.py` (the JSON API, then the React app's `index.html` for every other address), `gateway_urls.py` (the device gateway's own URLconf), WSGI |
| `server/engines/` | `bs_calendar`, `attendance` (`compute_day`, OT), `payroll` (formula DSL, component graph, tax): pure Python, no Django, no I/O, no clock |
| `server/hajiri/db/` | every model (one file per domain, one app labelled `hajiri`), TextChoices, company-scoped managers and the migrations |
| `server/hajiri/core/` | sign-in, sessions, roles and the active company, groups of companies, crypto, audit log, errors, DRF authentication, permission and exception classes, settings store, collation (pyuca), images (Pillow), mail |
| `server/hajiri/web/` | middleware (security headers and CSP, WhiteNoise, CORS, the session and its Origin check, JSON errors), the view that serves the React app, the OpenAPI schema and docs, shared formatting and CSV helpers |
| `server/hajiri/<module>/` | one app per module: `services.py`, `serializers.py`, `api.py` (DRF views), `jobs.py`, `urls.py`; templates only for printable HTML the API returns |
| `frontend/` | the React web app (`src/`, `public/` for the PWA files and icons); `vite.config.js` builds it into `server/frontend_build/` |
| `server/frontend_build/` | the web app's production build, **committed** so installs need no Node.js; regenerate with `scripts/build-frontend.sh` |
| `server/hajiri/gateway/` | the device gateway (ZKTeco ADMS, Hikvision ISAPI), served by `manage.py run_gateway`; see `docs/gateway.md` |
| `server/hajiri/worker/` | the scheduler and job registry on `django.tasks` (`django_tasks_db` backend) |
| `server/hajiri/group/`, `server/hajiri/group_admin/` | group-wide views and group administration (the only code that reads across companies) |
| `server/demo/` | demo seeders (`seed_demo_suite`, `seed_demo_group`) |
| `server/tests/` | pytest suites, one folder per app; `tests/parity/fixtures/` and `tests/engines/payroll/golden/` are fixed records |
| `server/tools/gen_models.py` | the generator used once for the initial models (do not run it over `hajiri/db/models`) |
| `scripts/` | install, start/stop, run, boot services, update (see README); `build-frontend.sh` (npm ci and vite build into `server/frontend_build/`); `django_ops.py` helpers; `django_routes.py` URL map |
| `infra/` | Docker image and compose stack |
| `docs/adr` | decisions; ADR 0001 records deviations from plan.md, ADR 0002 the move to Django |

## Set up a development machine

```sh
./scripts/setup.sh --with-tests              # PostgreSQL 18, server/.venv, server/.env, schema, demo data, tests
# or by hand:
cd server
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt waitress==3.0.2
.venv/bin/python manage.py migrate && .venv/bin/python manage.py seed_bs_calendar
```

Use Python 3.13 (never the Homebrew 3.14 on the development Mac: its `pyexpat` is broken and `venv` cannot install pip).
PostgreSQL 18 runs on port **5433** on macOS (an existing PostgreSQL on 5432 is untouched) and 5432 on Linux. The
database is `hajiri`; its connection string and secrets live in `server/.env` (never committed).

## The web app (frontend/)

Node.js 20.19+ or 22.12+ is needed only to work on the web app; installs use the committed build.

```sh
# two terminals: Django on :3000, Vite with hot reload on :5173 (proxies /api and /health to :3000)
cd server && DJANGO_DEBUG=true .venv/bin/python manage.py runserver 3000
cd frontend && npm ci && npm run dev                                  # open http://localhost:5173
cd frontend && npx vitest run                                         # unit tests (lib/, api/client)
./scripts/build-frontend.sh                                           # production build into server/frontend_build/
```

- Plain JavaScript (`.js`/`.jsx`): no TypeScript, no `.ts`/`.tsx` files, no type annotations.
- The app calls `/api/v1/...` with the session cookie (`src/api/client.js`); it sends no CSRF token. Cookie requests
  that change something are accepted when their `Origin` (or `Referer`) is this site or a `CORS_ORIGINS` entry, as the
  TS `sameOriginGuard` did (`hajiri.web.middleware.SameOriginMiddleware`, `hajiri.core.api.SameOriginAuthentication`).
- After changing `frontend/`, run `./scripts/build-frontend.sh` and commit `server/frontend_build/` with the change
  (installs serve exactly what is committed; `update.sh` rebuilds only when Node.js is present and a pull brought
  source changes without a build).
- Django answers `index.html` (no-cache) for every address outside `/api/` and `/static/`; files under `/assets/`
  are content-hashed and cached for a year; `sw.js` and the manifest are revalidated on every load.

## Commands

Run from `server/`:

```sh
DJANGO_DEBUG=true .venv/bin/python manage.py runserver 3000          # API (and the built web app) with reload
.venv/bin/python manage.py run_worker                                 # background jobs (scheduler + task worker)
.venv/bin/python manage.py run_gateway --memory                       # gateway on :8080 with an in-memory store
.venv/bin/python manage.py gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
.venv/bin/python manage.py check && .venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py spectacular --file ../docs/openapi.yaml --validate   # after changing a JSON endpoint
.venv/bin/python ../scripts/api_reference.py                   # then docs/api.md from that schema
.venv/bin/python ../scripts/django_routes.py --all                    # every URL
```

## Tests

```sh
DJANGO_TEST_DATABASE=hajiri_test .venv/bin/pytest -q                       # everything
DJANGO_TEST_DATABASE=hajiri_t_leave .venv/bin/pytest -q tests/leave --reuse-db  # one app, keep the database
```

- The test runner creates and drops the database named by `DJANGO_TEST_DATABASE` (default `hajiri_test`); never point
  it at a database you want to keep. Every pytest process running at the same time needs its own name.
- Creating several test databases at the same moment runs PostgreSQL out of lock slots ("out of shared memory"):
  create them one after another (`--create-db` on a small folder), then run the shards in parallel with `--reuse-db`.
- The engines must keep exact parity with `tests/parity/fixtures/` and the payroll golden files, which were recorded
  from the original TypeScript engines. They are a fixed record: never regenerate them from the Python code.
- `tests/multicompany` checks the active company, switching, group roles, leavers, and that ids of another company are
  not found on the JSON endpoints. Add a case there for every new endpoint that takes an id.
- API tests use `django.test.Client` against `/api/v1/...` (`tests/<app>/test_api_*.py`), porting the HTTP-level
  assertions of the TS tests (`git show 65a9079:apps/api/test/<x>.test.ts`). `tests/web/test_spa.py` checks that the
  React app is served.
- Use `django.test.Client`; never start a server on a fixed port in a test.
- Browser tests (`tests/e2e/`, pytest + Playwright, skipped unless `HAJIRI_E2E_URL` is set) drive the built React
  app on a running server: for every company role of the two demo companies, the group roles and the person who
  works in both, they sign in through the login page and open every route of the router, failing on any console
  error, uncaught exception, failed request or red alert; `test_actions.py` runs the main flows (leave and its
  approval, clock-in, a new employee, a locked and published pay run, a help-desk ticket, an expense claim, the
  group dashboard). Use a throwaway database, never the live one:

  ```sh
  .venv/bin/pip install -r requirements-dev.txt && .venv/bin/playwright install chromium
  createdb -p 5433 hajiri_e2e
  E2E=postgres://localhost:5433/hajiri_e2e
  DATABASE_URL=$E2E .venv/bin/python manage.py migrate && DATABASE_URL=$E2E .venv/bin/python manage.py seed_bs_calendar
  .venv/bin/python manage.py seed_demo_group --database $E2E --yes   # not with DATABASE_URL set: it counts as live
  DATABASE_URL=$E2E .venv/bin/python manage.py shell < tests/e2e/seed_users.py > /tmp/e2e-users.json   # resets passwords
  # start the web server and the worker with DATABASE_URL=$E2E (any free port), then:
  HAJIRI_E2E_URL=http://localhost:8301 HAJIRI_E2E_USERS=/tmp/e2e-users.json .venv/bin/pytest -p no:cacheprovider tests/e2e
  ```

## Conventions (summary; the full rules are in docs/django-conventions.md)

- **ORM first.** Querysets with `select_related`/`prefetch_related`, `annotate`/`aggregate`, `F`/`Q`/`Exists`/
  `Subquery`/`Window`, `select_for_update` for locks, `bulk_create`/`bulk_update`, `transaction.atomic` for multi-step
  writes. Raw SQL only where the ORM cannot express it well (recursive CTEs and the like), with a one-line comment
  saying why. Database functions such as `hj_manages` are ORM `Func` expressions.
- **Scope every query to the active company** with the company-scoped managers (`Model.objects.for_company(...)`).
  `request.hajiri_user.company_id`, `roles` and `employee_id` are those of the active company; an id of another company
  behaves as not found. Only the group apps read across companies, and only across the companies the person may see.
- **Validation**: DRF serializers for the JSON endpoints and the service contract (the TS zod schemas).
- **JSON endpoints** are DRF views with the project's authentication, permission and exception classes; they appear in
  `/api/docs`. Paths, methods, query parameters, body keys, response fields and status codes are the TS API's: the
  React app was written against it.
- **Enums** are TextChoices on the models; queries use them, never string literals.
- **Background work** is a `django.tasks` task (`@task`, `.enqueue()`); periodic jobs are registered with
  `hajiri.worker.registry` (`daily_job`, `periodic_job`) and run by the scheduler.
- **Standard libraries**: Pillow for images, pyuca for sorting names, segno for QR codes, the `csv` module, Django's
  storage and email backends, django-filter for list filters, Django pagination.
- Every mutation writes an audit entry (`hajiri.core.audit`); access is checked in the service (HR company-wide,
  managers only their reports, employees themselves).
- **Dates**: business dates are Nepal dates (`hajiri.core.clock.npt_today()`); the database session runs in UTC, so
  never use `current_date`, `now()::date`, `date.today()` or a UTC date for a business date. Every date shown to people
  is Bikram Sambat with English month names (`hajiri.core.bsdate` on the server, `src/lib/bs.js` in the web app).
- **Money** is `numeric` in SQL and `Decimal` in Python; never a float.
- English UI throughout. Payroll is run entirely by HR: no finance role, no bank or accounting export.

## Rules that must not be broken

- `punch_event` and `audit_log` are append-only (database triggers enforce it). Corrections are new rows.
- `attendance_day` is versioned; a new version is written only when `inputs_hash` changes; locked days are never
  rewritten. Services pass `Shift(id=<row id>, ...)` to the engine, or recomputed hashes will not match the database.
- Never emit an ADMS `SHELL` command, and never a multi-line command (database CHECK, store guard and tests).
  Dangerous verbs need `GATEWAY_ALLOW_DANGEROUS_COMMANDS`.
- Serial numbers are validated at the gateway edge and sanitised before touching the file system.
- All statutory numbers come from `rule_version` rows (plan §6.13), never hard-coded.
- Biometric templates are only ever stored envelope-encrypted (plan §9.1 decision 5).
- `DATA_ENCRYPTION_KEY` and `PASSWORD_PEPPER` never change on an existing install.
- Do not wrap a step that counts failures (sign-in attempts, code attempts) in `transaction.atomic`: raising the
  error would roll the count back.

## Changing the schema

Edit the models in `server/hajiri/db/models/` and run `manage.py makemigrations`; check the generated migration, and
keep `makemigrations --check --dry-run` clean. Triggers, functions and constraints the ORM cannot declare go in a
`RunSQL` operation with its reverse. Never edit a migration that has been applied anywhere; add a new one. Monthly
`punch_event` partitions are created by a job (`ensure_punch_partitions`), not by migrations.

## Style

Python 3.13 with type hints on public functions; module and function docstrings say what the code is for. The web
app is plain JavaScript formatted with Prettier (`npm run format`). No inline scripts in HTML: the
Content-Security-Policy (`script-src 'self'`) forbids them.
