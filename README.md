# Hajiri · हाजिरी

Self-hosted multi-biometric attendance, HR and payroll platform for companies in Nepal, built on Django 6 with
server-rendered pages (HTMX and a little Alpine.js), a background worker and a device gateway, all in `server/`. One
install can run several companies of a group from one sign-in ([Groups of companies](#groups-of-companies)).

**plan.md** is the master plan and specification; `docs/DEVELOPMENT.md` is the developer guide; `docs/api.md` and
`/api/docs` describe the JSON endpoints; `docs/gateway.md` explains terminal onboarding; `docs/adr/` records decisions;
`docs/django-migration.md` records the move from the original TypeScript stack.

| Part | Where | Runs as |
|---|---|---|
| Web app (pages, HTMX partials, the JSON endpoints devices and integrations use) | `server/hajiri/<module>/` | `waitress` serving `config.wsgi`, port 3000 |
| Background worker (recompute queue, notifications, reminders, nightly sweep, punch partitions) | `server/hajiri/worker/` | `manage.py run_worker` (scheduler + task worker), or `run_scheduler` plus `db_worker` processes |
| Device gateway (ZKTeco ADMS push, Hikvision ISAPI push) | `server/hajiri/gateway/` | `manage.py run_gateway`, port 8080 |
| Engines (Bikram Sambat calendar, attendance day, payroll) | `server/engines/` | pure Python, no Django |
| Demo data | `server/demo/` | `manage.py seed_demo_suite`, `manage.py seed_demo_group` |

## What is included

| Area | Capability |
|---|---|
| Attendance capture | ZKTeco/eSSL ADMS push and Hikvision ISAPI push through a hardened device gateway; clock in and out from the browser, including work from home with an encrypted photo at clock-in; field visits with GPS; shared clock-in tablets (kiosk mode: pairing code, employee PIN or rotating QR badge, selfie, server time, PIN lockout); geofenced locations (off / flag / block); breaks; optional registered devices; offline clock-in queued as a correction request; manual punches and CSV attendance upload; immutable punch log; offline-device alerts |
| Attendance processing | Pure, recomputable day engine (Frappe Shift Type semantics): pairing modes, grace, thresholds, night shifts, holidays (branch and gender aware), weekly offs, leave and hourly leave, WFH/on-duty, overtime with the Labour Act caps, versioned days, payroll locks, exceptions queue, late-arrival penalties, optional block on late clock-in, attendance issues with reminders |
| Shifts and rosters | Shifts, effective-dated assignments, weekly roster grid with publishing, shift swaps (colleague accepts, manager approves), BS holiday calendar with the 2083 default list |
| Time tracking | Clients, projects and activities, server-clock timer, manual entries, weekly timesheets approved by the manager, comparison with attendance, rounding, billable hours and budget reports |
| Leave | Statutory and company leave types in a set order with clubbing, double-entry ledger, sandwich rule across types, half-day and hourly leave, accrual, substitute leave, encashment, leave liability estimate, who's out |
| Approvals | One inbox for every request type; approval flows per request type by line manager, manager's manager, department or branch head, role, named user or user group, with conditions and amount thresholds; delegation and approver absence; who-approves preview on request forms; flow verification report; signed actions, reminders and escalation |
| Payroll and tax | Formula DSL and component graph, BS-month proration, SSF/PF/gratuity regimes, CIT with optimum-CIT suggestions, versioned income-tax schedules with TDS projection and true-up, opening balances for mid-year joiners, employee tax-saving declarations (insurance, CIT, retirement fund, medical credit, donations) approved by HR, festival allowance, loans, arrears, supplementary and final settlement, Bonus Act run, expense and travel reimbursements, held pay, HR approval, locked runs, salary sheet, payslips with optional publishing, tax and salary certificates, tax deposit, retirement fund deposit, pay variance and annual tax reports. Payroll is run entirely by HR; there is no finance role and no bank or accounting export |
| People | Org structure (branches, departments, designations, heads), full employee profiles (family and nominees, education, experience, emergency contacts, encrypted ID numbers), effective-dated career events, profile change requests with maker-checker, field-level change history, directory and org chart, acting and deputation, milestones (anniversaries, contract ends, retirement), Know Your Employee reviews |
| Documents | Employee documents with verification, versions and expiry alerts; letter templates with placeholders, numbered and immutable issued letters, letter requests; company policies with acknowledgement |
| Joining and leaving | Onboarding checklists (started automatically, including hires from the careers pipeline), probation reviews and confirmation, resignations and HR-initiated exits, exit clearance, confidential exit interviews, asset register |
| Employee relations | HR help desk with response targets; confidential grievances heard by a committee with appeal; disciplinary cases with show-cause notices and warning letters |
| Expenses and travel | Expense claims with receipts and limits by grade, travel requests with daily allowance (TADA) rates and advances, settlement, all paid or recovered through payroll |
| Performance and learning | Goals and key results, KPI library, competencies (knowledge, skill, attitude), review cycles with self, manager and peer review, calibration and release, manager's journal, feedback, 1-on-1s, course catalogue, sessions, nominations, quizzes and certificates with expiry |
| Hiring | Requisitions, job openings, public careers page with applications (PDF CV, anti-abuse), pipeline board, rule-based scoring, interviews with scorecards, offers and offer letters, hire into an employee record, candidate emails and data retention |
| Engagement | Announcements (including sign-in pop-ups), kudos with company values, birthdays (opt-in) and anniversaries, anonymous surveys with eNPS, polls, suggestion box, task boards |
| Insights and reports | Workforce analytics (headcount, attrition, people cost, overtime, leave, absence), report catalogue by category, people record reports, ad-hoc report builder with saved reports, CSV everywhere with formula neutralisation |
| Groups of companies | Several companies of a group in one install and one sign-in: company switcher, access to sister companies with their own roles and employee record, group roles (admin, HR, read-only executive), consolidated dashboard, group directory with headcount breakdowns, consolidated reports, adding and attaching companies, transfers between sister companies, policy templates copied across companies ([below](#groups-of-companies)) |
| Security and privacy | Sessions with lockout and idle timeout, two-step sign-in with authenticator apps and recovery codes (can be required per role), active sessions, specialist roles (payroll, attendance, recruiter, executive), scoping to company, team, department and branch, field encryption for PAN, bank, ID numbers and files, hash-chained audit log, append-only punches, consent records, crypto-shredding, DSAR export, transparency page |
| Operations | One-command install, start/stop, boot services, update script, Docker stack, installable web app (PWA), per-company feature switches, English web app in the CoreValley visual identity with a command palette (Cmd+K), light and dark mode; every date is shown in Bikram Sambat (English month names) |

## Install on a new machine (macOS or Ubuntu/Debian)

```sh
git clone <your-github-url> hajiri
cd hajiri
./scripts/setup.sh --company ACME --company-name "Acme Pvt. Ltd."
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

### Moving from the TypeScript stack

Hajiri was first built as a TypeScript monorepo (`apps/api`, `apps/web`, `apps/gateway`, `packages/*`); that stack has
been removed and the app reads only `server/.env` and `server/gateway.env`. An install that ran it keeps its database and
must keep its secrets:

1. Stop the old services (`./scripts/stop.sh` of the old checkout, or remove its `com.hajiri.api` / `hajiri-api` boot
   units; `install-service.sh` below removes them too).
2. `git pull`. Git removes the tracked TypeScript files but leaves the ignored `apps/api/.env` and `apps/gateway/.env`.
3. Run `./scripts/setup.sh`. It copies `DATABASE_URL`, `DATA_ENCRYPTION_KEY` and `PASSWORD_PEPPER` from
   `apps/api/.env` into `server/.env`, and the gateway's `PORT` and `GATEWAY_SIGNING_KEY` from `apps/gateway/.env` into
   `server/gateway.env`, unless those files already set them (it warns when a value differs). To do it by hand, copy
   these lines unchanged into the two files (owner-only, `chmod 600`). A different key or pepper makes stored PAN, bank
   and ID numbers and every existing password unreadable; a different signing key leaves earlier punch signatures
   unverifiable.
4. The database is adopted as it is: `migrate` recognises a schema built by the TypeScript migrations (it must be at
   migration 0030, the last one; an older database has to be brought up to 0030 by the old stack first).
5. `./scripts/install-service.sh` installs the boot services and removes any older `com.hajiri.*` / `hajiri-*` units.
   Once the app runs, delete `apps/`, `packages/` and `node_modules/`. The gateway's raw archive moves from
   `apps/gateway/data/raw` to `server/data/raw` (copy it over to keep the forensic record).

## Run

| What | Command |
|---|---|
| Start everything (foreground, Ctrl+C stops) | `./scripts/start.sh` |
| Start in the background | `./scripts/start.sh --daemon` (logs in `logs/<service>.log`), stop with `./scripts/stop.sh` |
| Auto-start on boot and restart on crash | `./scripts/install-service.sh` (remove with `--uninstall`) |
| One service in the foreground (what launchd and systemd run) | `./scripts/run.sh web\|worker\|gateway` (`scheduler\|tasks\|tasks-N` with `WORKER_MODE=split`) |
| Update to the latest version from GitHub | `./scripts/update.sh` |
| Run with Docker instead | see [Docker](#docker) |

`start.sh` starts PostgreSQL if it is not running, applies pending migrations, then starts the web app, the worker and
the gateway, and checks `/health` on both ports. Open http://localhost:3000, sign in with the admin credentials, and
follow the setup checklist. Point terminals at `http://<this-machine>:8080` (ZKTeco ADMS) or
`http://<this-machine>:8080/hikvision/event?sn=<serial>` (Hikvision); new terminals appear as pending under Devices and
must be approved before their punches count (`docs/gateway.md` has the full onboarding procedure).

The boot services are launchd user agents on macOS (`~/Library/LaunchAgents/com.hajiri.{web,worker,gateway}.plist`)
and systemd units on Linux (`hajiri-{web,worker,gateway}.service`; `sudo systemctl status hajiri-web`,
`sudo journalctl -u hajiri-web -f`). Each waits for PostgreSQL; the web service applies migrations before it starts
serving.

**Background work.** Jobs run on Django's task framework with the database backend (`django_tasks_db`), so nothing
but PostgreSQL is needed. A scheduler enqueues each periodic and daily job as a task at its cadence (and never queues
a job that is still waiting); task workers run them, each job under its own advisory lock. By default
(`WORKER_MODE=combined`) both live in the one `worker` service (`manage.py run_worker`). With `WORKER_MODE=split` in
`server/.env` the scripts run a `scheduler` service (`manage.py run_scheduler`, exactly one) and `TASK_WORKERS` task
workers (`tasks`, `tasks-2`, … running `manage.py db_worker`) instead; `start.sh`, `stop.sh`, `install-service.sh`
(which removes the other mode's units) and `update.sh` follow the setting.

## Configuration

Django reads `server/.env`; a variable already set in the environment wins. The gateway reads `server/.env` and then
`server/gateway.env`, whose values win for the gateway's own keys. Both files hold secrets: `setup.sh` makes them
readable by the owner only. Never commit them.

`server/.env`

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgres://[user[:password]@]host[:port]/hajiri` |
| `DATA_ENCRYPTION_KEY` | base64 of 32 bytes: field encryption (PAN, bank, ID numbers, files, templates). Same value in `server/gateway.env`; never change it on an existing install |
| `PASSWORD_PEPPER` | password-hash pepper; never change it on an existing install |
| `DJANGO_SECRET_KEY` | Django's signing key (falls back to the pepper) |
| `PORT` | web port for the scripts (default 3000) |
| `WEB_THREADS` | waitress worker threads (default 8) |
| `ALLOWED_HOSTS` | host names and addresses people use to reach the web app (setup writes localhost, this machine's name and its LAN addresses) |
| `COOKIE_SECURE` | `true` once the site is served over HTTPS |
| `RUN_WORKER` | `false`: the worker runs as its own service |
| `WORKER_MODE` | `combined` (default: one worker process) or `split` (a scheduler plus task workers) |
| `TASK_WORKERS` | with `WORKER_MODE=split`, how many task-worker processes to run (default 1) |
| `LOG_LEVEL` | `info` (also `debug`, `warn`, `error`) |
| `SMTP_URL` | outgoing email through Django's SMTP backend (`smtp://user:password@host:587`); without it emails are logged |
| `SMS_WEBHOOK_URL` | outgoing SMS through a webhook bridge |
| `CORS_ORIGINS`, `CSRF_TRUSTED_ORIGINS` | other origins allowed to call the API with credentials, comma-separated |
| `DJANGO_DEBUG` | `true` only on a development machine |

`server/gateway.env`

| Variable | Purpose |
|---|---|
| `PORT` | gateway port (default 8080) |
| `GATEWAY_SIGNING_KEY` | Ed25519 key (base64 PKCS#8 DER) that signs every stored punch; keep it when moving an install |
| `DATA_ENCRYPTION_KEY` | same value as the web app; templates are discarded without it |
| `GATEWAY_MAX_PENDING_DEVICES` | cap on unapproved serials (default 50) |
| `GATEWAY_MAX_CLOCK_SKEW_MIN` | punches further in the future are flagged `clock_skew` (default 10) |
| `GATEWAY_RAW_DIR` | raw archive directory; a relative path is relative to `server/` (default `data/raw`) |
| `GATEWAY_ALLOW_DANGEROUS_COMMANDS` | `CLEAR DATA` and firmware verbs are refused unless `true`; `SHELL` is never allowed |

For HTTPS, put a reverse proxy (Caddy, nginx) in front of ports 3000 and 8080, set `COOKIE_SECURE=true`, add the
public host name to `ALLOWED_HOSTS` and set `TRUST_X_FORWARDED_PROTO=true`. `manage.py check --deploy` is clean with
`DJANGO_DEBUG=false`, a long `DJANGO_SECRET_KEY`, `COOKIE_SECURE=true`, `SECURE_SSL_REDIRECT=true`,
`SECURE_HSTS_SECONDS`, `SECURE_HSTS_INCLUDE_SUBDOMAINS=true` and `SECURE_HSTS_PRELOAD=true`.

## Groups of companies

Hajiri can run several companies of one group (a holding company and its sister companies) from one install, one
database and one sign-in. Each company keeps its own people, attendance, leave, payroll, documents and settings;
nothing of one company is visible from another, except in the group views below and only to the people allowed there.

- **The active company.** Every page works in one company at a time, shown with its name and code in the sidebar (and
  the mobile top bar). People with access to more than one company get a company switcher: it reloads the same page in
  the other company (or its hub's first page when the page showed one record). Roles and the employee record are those
  of the active company, so the same person can be HR in one company and an employee in another. Every query is scoped
  to the active company: an id of another company is "not found", on pages and JSON alike.
- **Access to a sister company.** A group admin gives a person of the group access to another company, with their
  roles there and, if they work there, their employee record there (Group, Access). People who leave lose access per
  company: the access whose employee record has ended stops counting, and the person can still sign in while they keep
  one company.
- **Group roles** give access to every company of the group, including companies added later: `group_admin` (admin in
  every company, plus group administration), `group_hr` (HR in every company, plus transfers) and `group_executive`
  (the read-only executive view everywhere).
- **Group dashboard** (`/group`): per company and for the group, in one screen: headcount (people employed today, as
  in Insights and the headcount report), joiners, leavers and
  probation reviews due; attendance today (present, late, absent, on leave) and open exceptions; approvals waiting;
  overtime this month; offline clock-in devices; expiring documents; and the latest locked pay run. Pay figures follow each company's policy: an admin can hide that company's pay from
  executives in group views.
- **Group directory** (`/group/people`): everyone in the companies the person may see, filterable by company, branch,
  department and employment type, with headcount by company, branch, department, gender and employment type.
- **Consolidated reports** (`/group/reports`): headcount, attendance summary, leave balances and usage, overtime,
  payroll summary (gross, SSF and PF, TDS and net of locked runs per BS month) and attrition, with the company as a
  column and a group total; on screen or as CSV.
- **Starting a group.** The admin of a company that is not in a group yet adds its first sister company (Group page, or
  `POST /api/v1/group/companies`): the group is created around their company and they become its first group admin.
- **Group administration** (group admins): add a company (seeded with the statutory defaults) or attach an existing
  standalone company you administer (Group, Companies); manage people's access and invite new people (Group, Access);
  copy policy templates (leave types, salary components and structures, approval flows, holidays, shifts and settings
  sections) from one company to others, with a preview of what would be created, overwritten or kept (Group, Policies).
- **Transfers between sister companies** (group HR and admins, Group, Transfers): the employment ends in the source
  company through the normal exit flow, a new employee record starts in the target on the effective date with the
  career history copied, leave balances carry over when the target company's rule allows it, the salary structure
  carries over when the target has one with the same code, and the person's sign-in follows. Both sides are audited
  and HR of the target is notified.
- **JSON** for Bearer-token clients: `/api/v1/me/companies` and `/api/v1/me/companies/switch`,
  `/api/v1/group/dashboard`, `/api/v1/group/reports/<kind>`, `/api/v1/group/roles`, and the group administration
  endpoints under `/api/v1/group/` (companies, access, transfers, transfer rules, policies); see `/api/docs`.

Payroll stays per company (each company runs, approves and locks its own pay runs, entirely by HR); the group views
only read locked results.

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
| `seed_demo_group --database <url> --yes [--sister DEMOSYS] [--sister-name N] [--group-name N] [--no-suite] [--only a,b]` | a demo group of two companies, both filled with sample data ([below](#a-demo-group-of-companies)) |
| `run_worker [--once --jobs a,b]` | the background worker: scheduler and task worker (forever, or one pass of the named jobs in this process) |
| `run_scheduler [--once]` | only the scheduler: enqueue the due jobs as tasks (`--once`: one tick, print the task ids) |
| `db_worker [--batch]` | a task worker (`django_tasks_db`) that runs queued tasks; run as many as needed next to one scheduler |
| `ensure_punch_partitions` | create the coming months' `punch_event` partitions (the worker does this nightly) |
| `run_gateway [--port N] [--memory] [--check]` | the device gateway; `--check` builds it from the environment and exits; `--memory` runs it with an in-memory store |
| `gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"` | a simulated terminal (`--vendor hikvision`, `--template`, `--poll N`) |
| `gateway_reconcile --all [--date YYYY-MM-DD]` | ask every active ADMS terminal to re-send a day's log; run it from cron at 01:00 |
| `spectacular --file ../docs/openapi.yaml` | regenerate the OpenAPI schema of the JSON endpoints |

Daily reconciliation from cron:
`0 1 * * * cd "<repo>/server" && .venv/bin/python manage.py gateway_reconcile --all >> ../logs/reconcile.log 2>&1`

## Demo data

`setup.sh` without `--company` loads the small demo set (company DEMO, Sita and Ram, device DEMO-01). On a database
that holds a fuller company (at least eight active employees and an HR or admin user who has set their password),
`seed_demo_suite` fills every HR module with realistic sample data: org structure and full profiles, career history,
documents, letters and policies, assets, onboarding and an exit, help-desk tickets, time tracking and timesheets, field
visits, breaks and a kiosk, a published roster with a shift swap, expense claims and trips, tax-saving declarations,
goals and a review cycle, courses and certificates, a hiring pipeline with an offer, announcements, kudos, a pulse
survey and a poll, and suggestions. It never touches payroll runs.

```sh
server/.venv/bin/python server/manage.py seed_demo_suite --database postgres://localhost:5433/hajiri_demo --yes
# the same, as a plain Python module:
cd server && .venv/bin/python -m demo --database postgres://localhost:5433/hajiri_demo --yes
```

It refuses to run unless the target database is named with `--database` and confirmed with `--yes`, and it refuses the
live database: the `DATABASE_URL` in the environment or `server/.env`, compared by host, port and database name.
Records are made through the same service functions the pages use, as the HR user, the employees and their managers,
so every rule, workflow, audit entry and notification applies. Every employee without an account gets one with a
random password (reset one with `reset_password` to sign in as that person). Sections run in order, each in one
transaction, and each is recorded when it finishes (company setting `demo.suite`), so running the command again only
retries the sections that failed; `--only documents,engage` limits a run. A section whose module is switched off under
Settings, Features fails with "feature off".

### A demo group of companies

`seed_demo_group` sets up [a group of companies](#groups-of-companies) on a demo database:

```sh
server/.venv/bin/python server/manage.py seed_demo_group --database postgres://localhost:5433/hajiri_demo --yes
cd server && .venv/bin/python -m demo group --database postgres://localhost:5433/hajiri_demo --yes   # the same
```

- **Main company** (`--company`, default DEMO, created with the development seed when missing): staffed with fourteen
  people in four departments when it has fewer than eight, and its HR officer `laxmi@corevalley.test`.
- **Sister company** (`--sister DEMOSYS "Demo Systems Pvt. Ltd."`): added by that HR officer through the same service
  as the Group page's *Add a company*, so it gets the standard defaults; she becomes the group admin. It has its own
  twelve people and HR officer (`sabina@demosys.corevalley.test`).
- **One person in both companies**: the finance manager, Rajesh Karki, has an employee record in each company and one
  sign-in; the company switcher in the sidebar moves him between them.
- **A group executive**, `bhim@corevalley.test`, sees every company of the group on the Group overview, read-only.
- Then the demo suite runs for each company (`--no-suite` skips it, `--only` limits it).

Every account has a random password: set one with `reset_password --email <email>` to sign in as that person. The same
safety rules as `seed_demo_suite` apply (an explicit `--database`, `--yes`, never the live database), and running it
again adds only what is missing.

## Docker

```sh
cp infra/docker/env.example infra/docker/.env     # fill in the secrets
docker compose -f infra/docker-compose.yml --env-file infra/docker/.env up -d --build
```

One image (`infra/docker/Dockerfile`, Python 3.13) runs as four services: `postgres` (PostgreSQL 18), `web` (port
3000), `worker` and `gateway` (port 8080). The `split` profile adds a `scheduler` and `tasks` workers to run in place of
the worker:
`docker compose -f infra/docker-compose.yml --env-file infra/docker/.env --profile split up -d --build --scale worker=0 --scale tasks=3`.
On every start the web container waits for the database, applies migrations and the idempotent seeds (the demo company
when `COMPANY_CODE=DEMO`, otherwise your company), and creates the admin named in `HAJIRI_ADMIN_EMAIL` /
`HAJIRI_ADMIN_PASSWORD` if it does not exist; the worker and the gateway start once the web container is healthy. The
gateway's raw archive is the `gateway-raw` volume; the database is `pgdata`. Admin commands run through the same image:
`docker compose -f infra/docker-compose.yml run --rm -e HAJIRI_PASSWORD=<new> web manage reset_password --email admin@hajiri.local`.

## Repository map

| Path | What |
|---|---|
| `server/config/` | settings (reads `server/.env`), the web URLconf, the gateway URLconf, WSGI |
| `server/engines/` | `bs_calendar` (2000–2090 BS, i.e. 1943–2034 AD; 2084 onward projected), `attendance` (the day engine), `payroll` (formula DSL, tax schedules, 28 golden fixtures); pure Python, checked against fixtures recorded from the original engines |
| `server/hajiri/db/` | models for every table (TextChoices for every constrained column, company-scoped managers) and the migrations |
| `server/hajiri/core/`, `server/hajiri/web/` | crypto, sessions and roles, companies and groups, audit log, clock, errors, DRF base classes; middleware, layout, navigation |
| `server/hajiri/<module>/` | one app per module: services, forms, serializers, views, templates, `urls.py`, `nav.py` |
| `server/hajiri/group/`, `server/hajiri/group_admin/` | group views and group administration |
| `server/demo/` | the demo seeders: the demo suite, the demo roster and the demo group of companies |
| `server/tests/` | pytest suites, one folder per app; `tests/parity/` holds the engine fixtures |
| `scripts/` | `setup.sh`, `start.sh`, `stop.sh`, `run.sh`, `install-service.sh`, `update.sh` (shared helpers in `lib.sh`), `django_ops.py` (helpers they use), `django_routes.py` (URL map) |
| `infra/` | Docker image (`infra/docker/`) and compose stack (`infra/docker-compose.yml`) |
| `docs/` | developer guide, API reference and schema, gateway guide, ADRs, migration record |

## Verification

```sh
cd server
DJANGO_TEST_DATABASE=hajiri_test .venv/bin/pytest -q          # the test runner creates and drops this database
.venv/bin/python manage.py check && .venv/bin/python manage.py makemigrations --check --dry-run
```

The suite covers the engines (property tests, golden fixtures and exact parity with fixtures recorded from the original
TypeScript engines), the gateway, every module's services and pages, the JSON endpoints, roles and scoping, and the
groups of companies (`tests/multicompany`, `tests/group`, `tests/group_admin`, and `tests/scenario`, which walks a
holding group of three companies end to end: policies copied, hiring, a manager in two companies, attendance upload
and payroll in each company, a transfer, leave approvals, and every group figure checked against the company pages). See `docs/DEVELOPMENT.md` for running it in parallel.

## Operations notes

- The web process does not run the background worker; `start.sh`, the boot services and the Docker stack run it as its
  own process (or, split, as a scheduler and task workers). Several workers or schedulers (or a web process started
  with `RUN_WORKER=true` where the WSGI entry point starts one) never do the same work twice: each scheduler tick and
  each job takes a PostgreSQL advisory lock. Finished task results are pruned daily.
- Static files are served by the web process itself (WhiteNoise), so a single machine needs no web server in front.
- Logs are written to `logs/<service>.log` (`web`, `worker` or `scheduler` and `tasks*`, `gateway`; owner-only: they
  hold employee names and request details).
- Back up the database and the gateway's raw archive (`server/data/raw`), and keep `server/.env` and
  `server/gateway.env` in the password manager: without `DATA_ENCRYPTION_KEY` a database backup cannot be read.

## Known gaps and assumptions

These are documented in code comments and plan.md section 18, and need a decision or real hardware:

- Statutory interpretation choices to confirm with a chartered accountant: employer SSF contribution counted as taxable
  income; SST waiver for SSF, PF and CIT contributors; disability deduction; remote-area figures.
- Golden payroll fixtures are engine snapshots, not accountant-signed; a two-cycle parallel run is still required before
  payroll go-live (plan section 15).
- BS 2084 to 2090 month lengths are projections and must be verified against the official calendar when it is
  published. The built-in public holiday list covers BS 2083 only; add later years under Settings, Holidays.
- Terminal protocol details (ZKTeco status/verify codes, template layouts, Hikvision field names) come from community
  sources and must be verified on purchased units; the ZKTeco comm key is not validated (VLAN and serial allow-list are
  the controls).
- The SSF sheet layout follows a single vendor source and needs confirmation against the SSF portal.
- SMS and push are delivered through webhook bridges; there is no built-in SMS provider client.
- The Docker image, the boot-service script and the Linux branches of `setup.sh` have not been run on real
  infrastructure yet (`docs/django-migration.md`); try them on a test machine first.
- Passkeys/FIDO2, OpenBao key management and a native mobile app are not built; the web app is responsive, installable
  as an app on phones (PWA) and covers the employee and manager flows. Uploaded files are stored encrypted in PostgreSQL.
- New tax rules (donations, medical credit, remote area) and optimum CIT follow the Income Tax Act as read here and are
  marked "confirm with your accountant" in the app.
