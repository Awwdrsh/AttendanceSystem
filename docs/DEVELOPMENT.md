# Development guide

Hajiri is an in-house biometric attendance, HR and payroll platform for companies in Nepal. **plan.md is the
specification**; reference its section numbers in commit messages and comments when encoding a rule (e.g. "plan
§10.4"). The code is one Django 6 project in `server/` (Python 3.13). **docs/django-conventions.md is the rulebook**
for models, scoping, forms, serializers, views, HTMX, templates, background work and tests; follow it literally. The
help desk (`server/hajiri/helpdesk/`) is the worked example of every pattern.

## Layout

| Path | What |
|---|---|
| `server/config/` | `settings.py` (reads `server/.env`), `urls.py` (the web app), `gateway_urls.py` (the device gateway's own URLconf), WSGI |
| `server/engines/` | `bs_calendar`, `attendance` (`compute_day`, OT), `payroll` (formula DSL, component graph, tax): pure Python, no Django, no I/O, no clock |
| `server/hajiri/db/` | every model (one file per domain, one app labelled `hajiri`), TextChoices, company-scoped managers and the migrations |
| `server/hajiri/core/` | sign-in, sessions, roles and the active company, groups of companies, crypto, audit log, errors, DRF authentication, permission and exception classes, settings store, collation (pyuca), images (Pillow), mail |
| `server/hajiri/web/` | middleware, layout, navigation, decorators, shared components and template tags |
| `server/hajiri/<module>/` | one app per module: `services.py`, `forms.py`, `serializers.py`, `views.py`, `api.py`, `jobs.py`, `urls.py`, `nav.py`, templates |
| `server/hajiri/gateway/` | the device gateway (ZKTeco ADMS, Hikvision ISAPI), served by `manage.py run_gateway`; see `docs/gateway.md` |
| `server/hajiri/worker/` | the scheduler and job registry on `django.tasks` (`django_tasks_db` backend) |
| `server/hajiri/group/`, `server/hajiri/group_admin/` | group-wide views and group administration (the only code that reads across companies) |
| `server/demo/` | demo seeders (`seed_demo_suite`, `seed_demo_group`) |
| `server/templates/`, `server/static/` | base layout, components, CSS (CoreValley identity), `app.js`, vendored libraries, PWA files |
| `server/tests/` | pytest suites, one folder per app; `tests/parity/fixtures/` and `tests/engines/payroll/golden/` are fixed records |
| `server/tools/gen_models.py` | the generator used once for the initial models (do not run it over `hajiri/db/models`) |
| `scripts/` | install, start/stop, run, boot services, update (see README); `django_ops.py` helpers; `django_routes.py` URL map |
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

## Commands

Run from `server/`:

```sh
DJANGO_DEBUG=true .venv/bin/python manage.py runserver 3000          # web app with reload
.venv/bin/python manage.py run_worker                                 # background jobs (scheduler + task worker)
.venv/bin/python manage.py run_gateway --memory                       # gateway on :8080 with an in-memory store
.venv/bin/python manage.py gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
.venv/bin/python manage.py check && .venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python manage.py spectacular --file ../docs/openapi.yaml --validate   # after changing a JSON endpoint
.venv/bin/python ../scripts/django_routes.py --all                    # every URL, page and partial
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
  not found on pages, partials and JSON endpoints. Add a case there for every new page or endpoint that takes an id.
- Use `django.test.Client`; never start a server on a fixed port in a test.

## Conventions (summary; the full rules are in docs/django-conventions.md)

- **ORM first.** Querysets with `select_related`/`prefetch_related`, `annotate`/`aggregate`, `F`/`Q`/`Exists`/
  `Subquery`/`Window`, `select_for_update` for locks, `bulk_create`/`bulk_update`, `transaction.atomic` for multi-step
  writes. Raw SQL only where the ORM cannot express it well (recursive CTEs and the like), with a one-line comment
  saying why. Database functions such as `hj_manages` are ORM `Func` expressions.
- **Scope every query to the active company** with the company-scoped managers (`Model.objects.for_company(...)`).
  `request.hajiri_user.company_id`, `roles` and `employee_id` are those of the active company; an id of another company
  behaves as not found. Only the group apps read across companies, and only across the companies the person may see.
- **Validation**: DRF serializers for JSON endpoints and the service contract, Django forms for page posts.
- **JSON endpoints** are DRF views with the project's authentication, permission and exception classes; they appear in
  `/api/docs`. Pages and HTMX partials are views rendering templates (django-htmx).
- **Enums** are TextChoices on the models; queries use them, never string literals.
- **Background work** is a `django.tasks` task (`@task`, `.enqueue()`); periodic jobs are registered with
  `hajiri.worker.registry` (`daily_job`, `periodic_job`) and run by the scheduler.
- **Standard libraries**: Pillow for images, pyuca for sorting names, segno for QR codes, the `csv` module, Django's
  storage and email backends, django-filter for list filters, Django pagination.
- Every mutation writes an audit entry (`hajiri.core.audit`); access is checked in the service (HR company-wide,
  managers only their reports, employees themselves).
- **Dates**: business dates are Nepal dates (`hajiri.core.clock.npt_today()`); the database session runs in UTC, so
  never use `current_date`, `now()::date`, `date.today()` or a UTC date for a business date. Every date shown to people
  is Bikram Sambat with English month names (`hajiri.core.bsdate`, the `hj` template filters).
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

Python 3.13 with type hints on public functions; module and function docstrings say what the code is for. Templates
extend `base.html` and use the shared components in `server/templates/components/`. No inline scripts (the
Content-Security-Policy forbids them); page behaviour lives in `server/static/js/app.js` or an app's static folder.
