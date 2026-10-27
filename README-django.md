# Hajiri on Django · हाजिरी

The Python stack of Hajiri: the same attendance, HR and payroll platform as the TypeScript one described in `README.md`,
rebuilt on Django 6 with server-rendered pages (HTMX and a little Alpine.js), a background worker and the device gateway,
all in `server/`. It reads the same database and the same secrets, so both stacks can run against one installation
during the migration (`docs/django-migration.md`). Once the Django stack is in use, the TypeScript scripts and apps can go.

| Part | Where | Runs as |
|---|---|---|
| Web app (pages, HTMX partials, the JSON endpoints devices and integrations use) | `server/hajiri/<module>/` | `waitress` serving `config.wsgi`, port 3000 |
| Background worker (recompute queue, notifications, reminders, nightly sweep, punch partitions) | `server/hajiri/worker/` | `manage.py run_worker` |
| Device gateway (ZKTeco ADMS push, Hikvision ISAPI push) | `server/hajiri/gateway/` | `manage.py run_gateway`, port 8080 |
| Engines (Bikram Sambat calendar, attendance day, payroll) | `server/engines/` | pure Python, no Django |
| Demo data | `server/demo/` | `manage.py seed_demo_suite` |

## Install on a new machine (macOS or Ubuntu/Debian)

```sh
git clone <your-github-url> hajiri
cd hajiri
./scripts/setup-django.sh --company ACME --company-name "Acme Pvt. Ltd."
```

That installs Python 3.13 and PostgreSQL 18 if missing, creates the database and your company, writes
`server/.env` and `server/gateway.env` with new secrets, builds `server/.venv`, applies the schema, seeds the BS calendar
and the statutory defaults, and creates the admin user (credentials written once into `.admin-credentials`; the admin
must change the password at first sign-in). Re-running is safe: every step is idempotent and existing files and
secrets are kept.

| Option | Effect |
|---|---|
| `--company CODE --company-name "Name"` | create your real company (code: 1–20 letters, digits, `_` or `-`) instead of the demo one |
| `--no-demo` | skip the demo company's sample employees and device |
| `--pg-port N` | PostgreSQL port (default 5433 on macOS, so an existing PostgreSQL on 5432 is left alone; 5432 on Linux) |
| `--with-tests` | install the test tools and run the Python test suite at the end |
| `HAJIRI_ADMIN_EMAIL=…` | the admin's email (default `admin@hajiri.local`) |

**Moving from the TypeScript stack.** Run `setup-django.sh` in the same checkout. It finds `apps/api/.env` and reuses
its `DATABASE_URL`, `DATA_ENCRYPTION_KEY` and `PASSWORD_PEPPER`, and the gateway's `GATEWAY_SIGNING_KEY` from
`apps/gateway/.env`. These must never change: a different key or pepper makes stored PAN, bank and ID numbers and every
existing password unreadable, and the script stops if `server/.env` and `apps/api/.env` disagree. The database is
adopted as it is (`migrate` recognises a schema built by the TS migrations; a database at an older TS migration is
refused until `pnpm --filter @hajiri/api db:migrate` has run). Both stacks default to ports 3000 and 8080, so run one at a
time, or give the Django stack other ports with `PORT` in `server/.env` and `server/gateway.env`.

## Run

| What | Command |
|---|---|
| Start everything (foreground, Ctrl+C stops) | `./scripts/start-django.sh` |
| Start in the background | `./scripts/start-django.sh --daemon` (logs in `logs/django-*.log`), stop with `./scripts/stop-django.sh` |
| Auto-start on boot and restart on crash | `./scripts/install-service-django.sh` (remove with `--uninstall`; `--replace-ts` removes the TypeScript services first) |
| One service in the foreground (what launchd and systemd run) | `./scripts/run-django.sh web\|worker\|gateway` |
| Update to the latest version from GitHub | `./scripts/update-django.sh` |
| Run with Docker instead | see [Docker](#docker) |

`start-django.sh` starts PostgreSQL if it is not running, applies pending migrations, then starts the web app, the
worker and the gateway, and checks `/health` on both ports. Open http://localhost:3000, sign in with the admin
credentials, and follow the setup checklist. Point terminals at `http://<this-machine>:8080` (ZKTeco ADMS) or
`http://<this-machine>:8080/hikvision/event?sn=<serial>` (Hikvision); new terminals appear as pending under Devices and
must be approved before their punches count (`docs/gateway.md` has the full onboarding procedure).

The boot services are launchd user agents on macOS (`~/Library/LaunchAgents/com.hajiri.django-{web,worker,gateway}.plist`)
and systemd units on Linux (`hajiri-django-{web,worker,gateway}.service`; `sudo systemctl status hajiri-django-web`,
`sudo journalctl -u hajiri-django-web -f`). Each waits for PostgreSQL; the web service applies migrations before it
starts serving.

## Configuration

Django reads `server/.env`, then `apps/api/.env` for anything not set there; the real environment always wins. The
gateway reads `server/gateway.env`, then `apps/gateway/.env`. Both files hold secrets: `setup-django.sh` makes them
readable by the owner only. Never commit them.

`server/.env`

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgres://[user[:password]@]host[:port]/hajiri` |
| `DATA_ENCRYPTION_KEY` | base64 of 32 bytes: field encryption (PAN, bank, ID numbers, files, templates). Same value as the TS API and the gateway |
| `PASSWORD_PEPPER` | password-hash pepper. Same value as the TS API |
| `DJANGO_SECRET_KEY` | Django's signing key (falls back to the pepper) |
| `PORT` | web port for the scripts (default 3000) |
| `WEB_THREADS` | waitress worker threads (default 8) |
| `ALLOWED_HOSTS` | host names and addresses people use to reach the web app (setup writes localhost, this machine's name and its LAN addresses) |
| `COOKIE_SECURE` | `true` once the site is served over HTTPS |
| `RUN_WORKER` | `false`: the worker runs as its own service |
| `LOG_LEVEL` | `info` (also `debug`, `warn`, `error`) |
| `SMTP_URL`, `SMS_WEBHOOK_URL` | outgoing email and SMS |
| `DJANGO_DEBUG` | `true` only on a development machine |

`server/gateway.env`

| Variable | Purpose |
|---|---|
| `PORT` | gateway port (default 8080) |
| `GATEWAY_SIGNING_KEY` | Ed25519 key (base64 PKCS#8 DER) that signs every stored punch; keep the TS gateway's key when moving over |
| `DATA_ENCRYPTION_KEY` | same value as the web app; templates are discarded without it |
| `GATEWAY_MAX_PENDING_DEVICES` | cap on unapproved serials (default 50) |
| `GATEWAY_MAX_CLOCK_SKEW_MIN` | punches further in the future are flagged `clock_skew` (default 10) |
| `GATEWAY_RAW_DIR` | raw archive directory; a relative path is relative to `server/` (default `data/raw`) |
| `GATEWAY_ALLOW_DANGEROUS_COMMANDS` | `CLEAR DATA` and firmware verbs are refused unless `true`; `SHELL` is never allowed |

For HTTPS, put a reverse proxy (Caddy, nginx) in front of ports 3000 and 8080, set `COOKIE_SECURE=true`, and add the
public host name to `ALLOWED_HOSTS`.

## Admin commands

Run from the repository root as `server/.venv/bin/python server/manage.py <command>` (or `cd server && .venv/bin/python manage.py …`).

| Command | What it does |
|---|---|
| `create_admin --email a@b.c [--name N] [--company DEMO]` | a new admin who must change the password at first sign-in; the password comes from `--password` or, better, `HAJIRI_PASSWORD` in the environment (kept out of the process list) |
| `reset_password --email a@b.c` | new password (from `--password` or `HAJIRI_PASSWORD`), clears the lockout, signs the user out everywhere |
| `create_company --code ACME --name "Acme Pvt. Ltd."` | a company with its default leave types, workflows and settings |
| `seed_defaults [--company DEMO]` | leave types, workflows, settings, rule versions, salary components (idempotent) |
| `seed_bs_calendar` | refresh the Bikram Sambat month table |
| `seed_demo --database-name <name> --yes` | the small demo set: company DEMO, a branch, a shift, two employees and a ZKTeco device |
| `seed_demo_suite --database <url> --yes [--company DEMO] [--only a,b]` | sample data for every HR module ([below](#demo-data)) |
| `run_worker [--once --jobs a,b]` | the background worker (forever, or one pass of the named jobs) |
| `ensure_punch_partitions` | create the coming months' `punch_event` partitions (the worker does this nightly) |
| `run_gateway [--port N] [--memory] [--check]` | the device gateway; `--check` builds it from the environment and exits; `--memory` runs it with an in-memory store |
| `gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"` | a simulated terminal (`--vendor hikvision`, `--template`, `--poll N`) |
| `gateway_reconcile --all [--date YYYY-MM-DD]` | ask every active ADMS terminal to re-send a day's log; run it from cron at 01:00 |

Daily reconciliation from cron:
`0 1 * * * cd "<repo>/server" && .venv/bin/python manage.py gateway_reconcile --all >> ../logs/reconcile.log 2>&1`

## Demo data

`setup-django.sh` without `--company` loads the small demo set (company DEMO, Sita and Ram, device DEMO-01). On a
database that holds a fuller company (at least eight active employees and an HR or admin user who has set their
password), `seed_demo_suite` fills every HR module with realistic sample data: org structure and full profiles, career
history, documents, letters and policies, assets, onboarding and an exit, help-desk tickets, time tracking and
timesheets, field visits, breaks and a kiosk, a published roster with a shift swap, expense claims and trips, tax-saving
declarations, goals and a review cycle, courses and certificates, a hiring pipeline with an offer, announcements,
kudos, a pulse survey and a poll, and suggestions. It never touches payroll runs.

```sh
server/.venv/bin/python server/manage.py seed_demo_suite --database postgres://localhost:5433/hajiri_demo --yes
# the same, until 'demo' is listed in INSTALLED_APPS:
cd server && .venv/bin/python -m demo --database postgres://localhost:5433/hajiri_demo --yes
```

It refuses to run unless the target database is named with `--database` and confirmed with `--yes`, and it refuses the
live database: the `DATABASE_URL` in the environment, `server/.env` or `apps/api/.env`, compared by host, port and
database name. Records are made through the same service functions the pages use, as the HR user, the employees and
their managers, so every rule, workflow, audit entry and notification applies. Every employee without an account gets
one with a random password (reset one with `reset_password` to sign in as that person). Sections run in order, each in
one transaction, and each is recorded when it finishes (company setting `demo.suite`), so running the command again
only retries the sections that failed; `--only documents,engage` limits a run. A section whose module is switched off
under Settings, Features fails with "feature off".

## Docker

```sh
cp infra/docker-django/env.example infra/docker-django/.env     # fill in the secrets
docker compose -f infra/docker-compose.django.yml --env-file infra/docker-django/.env up -d --build
```

One image (`infra/docker-django/Dockerfile`, Python 3.13) runs as four services: `postgres` (PostgreSQL 18), `web`
(port 3000), `worker` and `gateway` (port 8080). On every start the web container waits for the database, applies
migrations and the idempotent seeds (the demo company when `COMPANY_CODE=DEMO`, otherwise your company), and creates the
admin named in `HAJIRI_ADMIN_EMAIL` / `HAJIRI_ADMIN_PASSWORD` if it does not exist; the worker and the gateway start once
the web container is healthy. The gateway's raw archive is the `gateway-raw` volume; the database is `pgdata`. Admin
commands run through the same image:
`docker compose -f infra/docker-compose.django.yml run --rm -e HAJIRI_PASSWORD=<new> web manage reset_password --email admin@hajiri.local`.

## Development

```sh
cd server
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt waitress==3.0.2
.venv/bin/python manage.py migrate
DJANGO_DEBUG=true .venv/bin/python manage.py runserver 3000      # development server with reload
DJANGO_TEST_DATABASE=hajiri_django_test .venv/bin/pytest -q      # the test runner creates and drops this database
```

Use Python 3.13: the Homebrew Python 3.14 on the development Mac cannot create a venv with pip. Tests never use the
`hajiri_test` database, which belongs to the TS suite. Conventions for views, templates, HTMX, forms, permissions,
tables, CSV and tests are in `docs/django-conventions.md`; the migration plan and its decisions are in
`docs/django-migration.md`. Print the URL map with `server/.venv/bin/python scripts/django_routes.py [--all]`.

| Path | What |
|---|---|
| `server/config/` | settings (reads the env files), the web URLconf, the gateway URLconf, WSGI |
| `server/engines/` | `bs_calendar`, `attendance`, `payroll`: pure ports of the TS packages, checked for exact parity |
| `server/hajiri/db/` | models for every table and the initial migration (the TS schema as of migration 0030) |
| `server/hajiri/core/`, `server/hajiri/web/` | crypto, sessions and roles, audit log, clock, errors; middleware, layout, navigation |
| `server/hajiri/<module>/` | one app per module: services, views, templates, `urls.py`, `nav.py` |
| `server/demo/` | the demo-suite seeder |
| `server/tests/` | pytest suites, one folder per app; `tests/parity/` checks the engines against the TS output |
| `scripts/*-django.sh`, `scripts/django_ops.py` | install, start/stop, run, service, update; helpers they use |
| `infra/docker-django/`, `infra/docker-compose.django.yml` | Docker image and stack |

## Operations notes

- The web process does not run the background worker; `start-django.sh`, the boot services and the Docker stack run it
  as its own process. Several workers (or a web process started with `RUN_WORKER=true` where the WSGI entry point starts
  one) never do the same work twice: each tick takes a PostgreSQL advisory lock.
- Static files are served by the web process itself (WhiteNoise), so a single machine needs no web server in front.
- Logs are written to `logs/django-{web,worker,gateway}.log` (owner-only; they hold employee names and request details).
- Back up the database and the gateway's raw archive (`server/data/raw`), and keep `server/.env` and
  `server/gateway.env` in the password manager: without `DATA_ENCRYPTION_KEY` a database backup cannot be read.

## JSON endpoints

Pages and HTMX partials use the same addresses as the React app's routes (partials under `/_/<app>/…`). JSON stays only
where something other than a browser page calls it: devices, the kiosk tablets' background calls and Bearer-token
integrations. Regenerate this list with `server/.venv/bin/python scripts/django_routes.py`.

### JSON endpoints (134)

| Path | URL name | View |
|---|---|---|
| `/api/v1/auth/login` | `integrations:api_login` | `hajiri.integrations.auth_api.login` |
| `/api/v1/auth/login/2fa` | `integrations:api_login_2fa` | `hajiri.integrations.auth_api.login_2fa` |
| `/api/v1/auth/logout` | `integrations:api_logout` | `hajiri.integrations.auth_api.logout` |
| `/api/v1/auth/me` | `integrations:api_me` | `hajiri.integrations.auth_api.me` |
| `/api/v1/auth/change-password` | `integrations:api_change_password` | `hajiri.integrations.auth_api.change_password` |
| `/api/v1/workflow/inbox` | `workflow:api_inbox` | `hajiri.workflow.views.api_inbox` |
| `/api/v1/workflow/preview` | `workflow:api_preview` | `hajiri.workflow.views.api_preview` |
| `/api/v1/workflow/bulk-approve` | `workflow:api_bulk_approve` | `hajiri.workflow.views.api_bulk_approve` |
| `/api/v1/workflow/<str:id>` | `workflow:api_detail` | `hajiri.workflow.views.api_detail` |
| `/api/v1/workflow/<str:id>/approve` | `workflow:api_approve` | `hajiri.workflow.views._api_decide.<locals>.view` |
| `/api/v1/workflow/<str:id>/reject` | `workflow:api_reject` | `hajiri.workflow.views._api_decide.<locals>.view` |
| `/api/v1/leave/types` | `leave:api_types` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/types/order` | `leave:api_types_order` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/types/<str:id>` | `leave:api_type` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/balances` | `leave:api_balances` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/preview` | `leave:api_preview` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/requests` | `leave:api_requests` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/requests/<str:id>/cancel` | `leave:api_cancel` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/accrual/run` | `leave:api_accrual_run` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/adjust` | `leave:api_adjust` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/ledger` | `leave:api_ledger` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/leave/team-calendar` | `leave:api_team_calendar` | `hajiri.leave.views._api.<locals>.deco.<locals>.view` |
| `/api/v1/regularisation` | `regularisation:api_requests` | `hajiri.regularisation.views.api_requests` |
| `/api/v1/regularisation/<str:id>/cancel` | `regularisation:api_cancel` | `hajiri.regularisation.views.api_cancel` |
| `/api/v1/documents/<uuid:doc_id>/file` | `documents:api_doc_file` | `hajiri.documents.views.doc_file` |
| `/api/v1/documents/letters/<uuid:letter_id>/html` | `documents:api_letter_html` | `hajiri.documents.views.letter_html` |
| `/api/v1/documents/policies/<uuid:policy_id>/file` | `documents:api_policy_file` | `hajiri.documents.views.policy_file` |
| `/api/v1/public/careers/jobs` | `careers:api_jobs` | `hajiri.careers.views.api_jobs` |
| `/api/v1/public/careers/jobs/<str:job_id>` | `careers:api_job` | `hajiri.careers.views.api_job` |
| `/api/v1/public/careers/jobs/<str:job_id>/apply` | `careers:api_apply` | `hajiri.careers.views.api_apply` |
| `/api/v1/ot/requests` | `ot:api_requests` | `hajiri.ot.views.api_requests` |
| `/api/v1/ot/requests/<str:id>/cancel` | `ot:api_request_cancel` | `hajiri.ot.views.api_request_cancel` |
| `/api/v1/ot/slips` | `ot:api_slips` | `hajiri.ot.views.api_slips` |
| `/api/v1/ot/slips/<str:id>/approve` | `ot:api_slip_approve` | `hajiri.ot.views.api_slip_approve` |
| `/api/v1/ot/slips/<str:id>/reject` | `ot:api_slip_reject` | `hajiri.ot.views.api_slip_reject` |
| `/api/v1/ot/slips/<str:id>/comp-off` | `ot:api_slip_comp_off` | `hajiri.ot.views.api_slip_comp_off` |
| `/api/v1/ot/register` | `ot:api_register` | `hajiri.ot.views.api_register` |
| `/api/v1/attendance-tools/policy` | `attendance_tools:api_policy` | `hajiri.attendance_tools.api.policy` |
| `/api/v1/attendance-tools/penalties` | `attendance_tools:api_penalties` | `hajiri.attendance_tools.api.penalties` |
| `/api/v1/attendance-tools/penalties/me` | `attendance_tools:api_penalties_me` | `hajiri.attendance_tools.api.penalties_me` |
| `/api/v1/attendance-tools/penalties/apply` | `attendance_tools:api_penalties_apply` | `hajiri.attendance_tools.api.penalties_apply` |
| `/api/v1/attendance-tools/penalties/<str:id>/reverse` | `attendance_tools:api_penalty_reverse` | `hajiri.attendance_tools.api.penalty_reverse` |
| `/api/v1/attendance-tools/issues` | `attendance_tools:api_issues` | `hajiri.attendance_tools.api.issues` |
| `/api/v1/attendance-tools/issues/summary` | `attendance_tools:api_issues_summary` | `hajiri.attendance_tools.api.issues_summary` |
| `/api/v1/attendance-tools/issues/nudge` | `attendance_tools:api_nudge` | `hajiri.attendance_tools.api.nudge` |
| `/api/v1/attendance-tools/upload/template` | `attendance_tools:api_upload_template` | `hajiri.attendance_tools.api.upload_template` |
| `/api/v1/attendance-tools/upload/check` | `attendance_tools:api_upload_check` | `hajiri.attendance_tools.api.upload_check` |
| `/api/v1/attendance-tools/upload/import` | `attendance_tools:api_upload_import` | `hajiri.attendance_tools.api.upload_import` |
| `/api/v1/attendance-tools/uploads` | `attendance_tools:api_uploads` | `hajiri.attendance_tools.api.uploads` |
| `/api/v1/attendance-tools/whos-out` | `attendance_tools:api_whos_out` | `hajiri.attendance_tools.api.whos_out` |
| `/api/v1/punch` | `punch:api_punch` | `hajiri.punch.api.punch` |
| `/api/v1/punch/today` | `punch:api_today` | `hajiri.punch.api.today` |
| `/api/v1/punch/remote` | `punch:api_remote` | `hajiri.punch.api.remote` |
| `/api/v1/punch/photos/<str:id>` | `punch:photo` | `hajiri.punch.api.photo` |
| `/api/v1/punch/manual` | `punch:api_manual` | `hajiri.punch.api.manual` |
| `/api/v1/field/policy` | `field:api_policy` | `hajiri.field.api.policy` |
| `/api/v1/field/me` | `field:api_me` | `hajiri.field.api.me` |
| `/api/v1/field/breaks/start` | `field:api_breaks_start` | `hajiri.field.api.breaks_start` |
| `/api/v1/field/breaks/end` | `field:api_breaks_end` | `hajiri.field.api.breaks_end` |
| `/api/v1/field/breaks` | `field:api_breaks` | `hajiri.field.api.breaks` |
| `/api/v1/field/visits/start` | `field:api_visits_start` | `hajiri.field.api.visits_start` |
| `/api/v1/field/visits/<str:id>/end` | `field:api_visit_end` | `hajiri.field.api.visit_end` |
| `/api/v1/field/visits/<str:id>/photo` | `field:api_visit_photo` | `hajiri.field.api.visit_photo` |
| `/api/v1/field/visits` | `field:api_visits` | `hajiri.field.api.visits` |
| `/api/v1/field/locations` | `field:api_locations` | `hajiri.field.api.locations` |
| `/api/v1/field/locations/<str:id>` | `field:api_location` | `hajiri.field.api.location` |
| `/api/v1/field/staff` | `field:api_staff` | `hajiri.field.api.staff` |
| `/api/v1/field/staff/<str:employee_id>` | `field:api_staff_update` | `hajiri.field.api.staff_update` |
| `/api/v1/field/staff/<str:employee_id>/kiosk-reset` | `field:api_staff_kiosk_reset` | `hajiri.field.api.staff_kiosk_reset` |
| `/api/v1/field/kiosks` | `field:api_kiosks` | `hajiri.field.api.kiosks` |
| `/api/v1/field/kiosks/<str:id>` | `field:api_kiosk` | `hajiri.field.api.kiosk` |
| `/api/v1/field/kiosks/<str:id>/pairing-code` | `field:api_kiosk_pairing_code` | `hajiri.field.api.kiosk_pairing_code` |
| `/api/v1/field/kiosks/<str:id>/revoke` | `field:api_kiosk_revoke` | `hajiri.field.api.kiosk_revoke` |
| `/api/v1/field/kiosks/<str:id>/unlock` | `field:api_kiosk_unlock` | `hajiri.field.api.kiosk_unlock` |
| `/api/v1/field/kiosk-pin` | `field:api_kiosk_pin` | `hajiri.field.api.kiosk_pin` |
| `/api/v1/field/kiosk-qr` | `field:api_kiosk_qr` | `hajiri.field.api.kiosk_qr` |
| `/api/v1/field/my-roster` | `field:api_my_roster` | `hajiri.field.api.my_roster` |
| `/api/v1/field/swap-colleagues` | `field:api_swap_colleagues` | `hajiri.field.api.swap_colleagues` |
| `/api/v1/field/swaps` | `field:api_swaps` | `hajiri.field.api.swaps` |
| `/api/v1/field/swaps/<str:id>/accept` | `field:api_swap_accept` | `hajiri.field.api.swap_accept` |
| `/api/v1/field/swaps/<str:id>/decline` | `field:api_swap_decline` | `hajiri.field.api.swap_decline` |
| `/api/v1/field/swaps/<str:id>/cancel` | `field:api_swap_cancel` | `hajiri.field.api.swap_cancel` |
| `/api/v1/kiosk/pair` | `kiosk:api_pair` | `hajiri.kiosk.api.pair` |
| `/api/v1/kiosk/session` | `kiosk:api_session` | `hajiri.kiosk.api.session` |
| `/api/v1/kiosk/identify` | `kiosk:api_identify` | `hajiri.kiosk.api.identify` |
| `/api/v1/kiosk/punch` | `kiosk:api_punch` | `hajiri.kiosk.api.punch` |
| `/api/v1/payroll/rules` | `payroll:api_rules` | `hajiri.payroll.api_views.rules` |
| `/api/v1/payroll/rules/<str:id>/approve` | `payroll:api_rule_approve` | `hajiri.payroll.api_views.rule_approve` |
| `/api/v1/payroll/components` | `payroll:api_components` | `hajiri.payroll.api_views.components` |
| `/api/v1/payroll/components/<str:code>` | `payroll:api_component` | `hajiri.payroll.api_views.component` |
| `/api/v1/payroll/structures` | `payroll:api_structures` | `hajiri.payroll.api_views.structures` |
| `/api/v1/payroll/structures/<str:id>/versions` | `payroll:api_structure_versions` | `hajiri.payroll.api_views.structure_versions` |
| `/api/v1/payroll/assignments` | `payroll:api_assignments` | `hajiri.payroll.api_views.assignments` |
| `/api/v1/payroll/declarations` | `payroll:api_declarations` | `hajiri.payroll.api_views.declarations` |
| `/api/v1/payroll/declarations/<str:employee_id>/verify` | `payroll:api_declaration_verify` | `hajiri.payroll.api_views.declaration_verify` |
| `/api/v1/payroll/loans` | `payroll:api_loans` | `hajiri.payroll.api_views.loans` |
| `/api/v1/payroll/periods` | `payroll:api_periods` | `hajiri.payroll.api_views.periods` |
| `/api/v1/payroll/runs` | `payroll:api_runs` | `hajiri.payroll.api_views.runs` |
| `/api/v1/payroll/runs/bonus` | `payroll:api_bonus_run` | `hajiri.payroll.api_views.bonus_run` |
| `/api/v1/payroll/runs/<str:id>` | `payroll:api_run` | `hajiri.payroll.api_views.run` |
| `/api/v1/payroll/runs/<str:id>/preflight` | `payroll:api_run_preflight` | `hajiri.payroll.api_views.run_preflight` |
| `/api/v1/payroll/runs/<str:id>/lock-attendance` | `payroll:api_run_lock_attendance` | `hajiri.payroll.api_views.run_lock_attendance` |
| `/api/v1/payroll/runs/<str:id>/inputs` | `payroll:api_run_inputs` | `hajiri.payroll.api_views.run_inputs` |
| `/api/v1/payroll/runs/<str:id>/compute` | `payroll:api_run_compute` | `hajiri.payroll.api_views.run_compute` |
| `/api/v1/payroll/runs/<str:id>/review` | `payroll:api_run_review` | `hajiri.payroll.api_views.run_review` |
| `/api/v1/payroll/runs/<str:id>/approve` | `payroll:api_run_approve` | `hajiri.payroll.api_views.run_approve` |
| `/api/v1/payroll/runs/<str:id>/lock` | `payroll:api_run_lock` | `hajiri.payroll.api_views.run_lock` |
| `/api/v1/payroll/runs/<str:id>/publish` | `payroll:api_run_publish` | `hajiri.payroll.api_views.run_publish` |
| `/api/v1/payroll/runs/<str:id>/holds` | `payroll:api_run_holds` | `hajiri.payroll.api_views.run_holds` |
| `/api/v1/payroll/runs/<str:id>/ssf-sheet.csv` | `payroll:api_run_ssf_sheet` | `hajiri.payroll.api_views.run_ssf_sheet` |
| `/api/v1/payroll/runs/<str:id>/salary-sheet` | `payroll:api_run_salary_sheet` | `hajiri.payroll.api_views.run_salary_sheet` |
| `/api/v1/payroll/payslips` | `payroll:api_payslips` | `hajiri.payroll.api_views.payslips` |
| `/api/v1/payroll/payslips/<str:id>` | `payroll:api_payslip` | `hajiri.payroll.api_views.payslip` |
| `/api/v1/payroll/payslips/<str:id>/html` | `payroll:api_payslip_html` | `hajiri.payroll.api_views.payslip_html` |
| `/api/v1/payroll/tax-savings` | `payroll:api_tax_savings` | `hajiri.payroll.api_views.tax_savings_root` |
| `/api/v1/payroll/tax-savings/estimate` | `payroll:api_tax_savings_estimate` | `hajiri.payroll.api_views.tax_savings_estimate` |
| `/api/v1/payroll/tax-savings/<str:id>/cancel` | `payroll:api_tax_savings_cancel` | `hajiri.payroll.api_views.tax_savings_cancel` |
| `/api/v1/payroll/tax-savings/<str:id>/files/<str:file_id>` | `payroll:api_tax_savings_file` | `hajiri.payroll.api_views.tax_savings_file` |
| `/api/v1/payroll/opening-balances` | `payroll:api_opening_balances` | `hajiri.payroll.api_views.opening_balances` |
| `/api/v1/payroll/opening-balances/import` | `payroll:api_opening_import` | `hajiri.payroll.api_views.opening_import` |
| `/api/v1/payroll/opening-balances/<str:id>` | `payroll:api_opening_balance` | `hajiri.payroll.api_views.opening_balance` |
| `/api/v1/payroll/optimum-cit` | `payroll:api_optimum_all` | `hajiri.payroll.api_views.optimum_all` |
| `/api/v1/payroll/optimum-cit/me` | `payroll:api_optimum_me` | `hajiri.payroll.api_views.optimum_me` |
| `/api/v1/payroll/optimum-cit/apply` | `payroll:api_optimum_apply` | `hajiri.payroll.api_views.optimum_apply` |
| `/api/v1/payroll/holds` | `payroll:api_holds` | `hajiri.payroll.api_views.holds` |
| `/api/v1/payroll/holds/<str:id>/release` | `payroll:api_hold_release` | `hajiri.payroll.api_views.hold_release` |
| `/api/v1/payroll/holds/<str:id>/cancel` | `payroll:api_hold_cancel` | `hajiri.payroll.api_views.hold_cancel` |
| `/api/v1/payroll/certificates/<str:employee>` | `payroll:api_certificate` | `hajiri.payroll.api_views.certificate` |
| `/api/v1/payroll/reports/annual-tax` | `payroll:api_report_annual_tax` | `hajiri.payroll.api_views.report_annual_tax` |
| `/api/v1/payroll/reports/tax-deposit` | `payroll:api_report_tax_deposit` | `hajiri.payroll.api_views.report_tax_deposit` |
| `/api/v1/payroll/reports/retirement-deposit` | `payroll:api_report_retirement_deposit` | `hajiri.payroll.api_views.report_retirement_deposit` |
| `/api/v1/payroll/reports/pay-variance` | `payroll:api_report_pay_variance` | `hajiri.payroll.api_views.report_pay_variance` |
| `/api/v1/payroll/reports/leave-liability` | `payroll:api_report_leave_liability` | `hajiri.payroll.api_views.report_leave_liability` |
| `/api/v1/payroll/compliance/years` | `payroll:api_compliance_years` | `hajiri.payroll.api_views.compliance_years` |

### Device gateway, port 8080 (7)

| Path | URL name | View |
|---|---|---|
| `/health` | `gateway:health` | `hajiri.gateway.views.health` |
| `/public-key` | `gateway:public_key` | `hajiri.gateway.views.public_key` |
| `/iclock/cdata` | `gateway:cdata` | `hajiri.gateway.views._route.<locals>.view` |
| `/iclock/getrequest` | `gateway:getrequest` | `hajiri.gateway.views._route.<locals>.view` |
| `/iclock/devicecmd` | `gateway:devicecmd` | `hajiri.gateway.views._route.<locals>.view` |
| `/iclock/fdata` | `gateway:fdata` | `hajiri.gateway.views._route.<locals>.view` |
| `/hikvision/event` | `gateway:hikvision_event` | `hajiri.gateway.views._route.<locals>.view` |
