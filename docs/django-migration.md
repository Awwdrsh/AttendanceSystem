# Migration to Django

Branch `django-rewrite`. This is the record of the move from the TypeScript stack to Django. **The TypeScript stack
was removed on 27 Oct 2026** (phase 9 below, ADR 0002): Django in `server/` is the only stack, and paths such as
`apps/api/...` or `packages/...` below refer to code that is now only in git history (commit 1bbe88b and earlier).
Decisions taken at the start (26 Oct 2026):

- **Everything moves to Python**, including the web app: Django templates with HTMX for partial updates, replacing
  React and Mantine. *Reversed on 28 Oct 2026 (phase 10): the React app is the web UI again, served by Django.* The CoreValley visual identity, command palette, light and dark mode and BS dates are kept.
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
   it. A database at an older TS migration is refused (it had to be brought to 0030 by the TS stack's `db:migrate`). A fresh Django database dumps
   byte-identical to the TS one (69 triggers, 409 checks, 799 indexes, exclusion constraints, punch partitions).
   Points to remember when writing services:
   - Foreign keys are `DO_NOTHING`; PostgreSQL applies the real `ON DELETE` rules.
   - A non-null text field with no database default is sent as `''` when left unset, and most of them have CHECK
     constraints, so set them explicitly.
   - Queries use the ORM (docs/django-conventions.md sections 2 and 12). The few raw statements left use a cursor
     with a `# Raw SQL:` comment; tests use `tests.support.sql` for independent checks of table contents.
   - Migration `0002` is state only (TextChoices on every CHECK-constrained column, related names, company
     managers): a fresh database dumps byte-identical to `hajiri_schema_ref` after it. Migration `0003` adds the
     tables for groups of companies (below); the dump differs from the TS schema only by those additions.
   - Do not wrap a step that counts failures (logins, code attempts) in `transaction.atomic`: raising the error
     would roll the count back.
   - Monthly `punch_event` partitions are created by a job (ported in phase 3), not by migrations.
3. **Platform.** *Done.* Settings and config, sign-in compatible with existing password hashes, sessions with lockout
   and idle timeout, two-step sign-in (required per role), roles and scoping (company, team, department, branch),
   field encryption with the same ciphertext format (byte-compatible both ways, tested against the TS code),
   hash-chained audit log, file storage, notifications (SMTP or email webhook, SMS, push and generic webhooks), feature
   switches. Background work runs on Django's task framework (`django.tasks` with the `django_tasks_db` database
   backend): a scheduler enqueues `hajiri.worker.tasks.run_job` with the TS cadences (recompute queue every tick,
   delivery 30 s, offline devices 60 s, SLA 10 min, auto clock-out and digest 5 min, nightly sweep, accrual, punch
   partitions and task-result pruning at 00:30 NPT, then the 13 daily module jobs of `module-jobs.ts` plus the kiosk
   heartbeat), so a single-machine install still needs only PostgreSQL. `manage.py run_worker` runs the scheduler and
   a task worker; larger installs run `run_scheduler` plus `db_worker` processes. `RUN_WORKER=true` starts both in
   the web process.
4. **Gateway.** *Done.* ADMS and ISAPI endpoints byte-compatible with the terminals; signed idempotent ingest;
   reconcile and the simulator (`manage.py run_gateway`, `gateway_simulate`, `gateway_reconcile`).
5. **Modules.** *Done.* Every TS module has a Django app with a service layer and pages; all 15 approval decision
   handlers are registered; every exported TS service function has a Python counterpart; all 122 React routes and
   hub tabs resolve to Django pages; JSON endpoints are kept for terminals, kiosk tablets and Bearer-token clients.
   All 78 TS test files (806 cases, including the cross-cutting security, roles-flows, QA and fixes suites, the
   gateway suites and the web `lib/` tests) are ported; the Python suite has 2,006 tests.
6. **Operations.** *Done except where noted below.* `scripts/*-django.sh` (setup, start/stop, run, boot service,
   update), `infra/docker-django/` and `infra/docker-compose.django.yml` (renamed to the canonical `scripts/*.sh`,
   `infra/docker/` and `infra/docker-compose.yml` in phase 9), `manage.py seed_demo` and
   `seed_demo_suite`, the CLI commands (`create_admin`, `reset_password`, `seed_defaults`, `seed_demo_suite`), PWA
   manifest and service worker, `check --deploy` clean with the production settings listed below.
7. **Idiomatic Django.** *Done (27 Oct 2026).* The literal port is being turned
   into idiomatic Django with standard libraries: ORM querysets with company scoping instead of raw SQL, DRF
   serializers and Django forms instead of the zod clone (`hajiri.core.schema`), DRF views with the
   project's authentication, permission and exception classes instead of `hajiri.core.http.api_view`,
   TextChoices, django.tasks, pyuca collation, Pillow, Django's email backend, django-htmx, django-filter and
   drf-spectacular (`/api/schema`, `/api/docs`). The rules are in docs/django-conventions.md; the help desk is the
   fully converted reference module. Every slice is converted and reviewed; `hajiri.core.schema`, the custom
   `api_view` (`hajiri.core.http`) and `hajiri.core.db` are deleted (the raw SQL test helpers moved to
   `tests/support/sql.py`). Migration `0005` brings the model state in line with the database (partial unique
   indexes as `UniqueConstraint(condition=...)` on ForeignKeys), lets `employee_exit.kind` be `transfer`, and makes
   personal task boards one per person and company.
   - Final review: raw SQL in `server/hajiri` is 14 statements, each with a `# Raw SQL:` reason (advisory locks,
     recursive org and manager chains, punch partition DDL and the catalog check, the BS calendar upsert, the health
     probe, and an optional install-specific column in Insights). Rounding and fixed-decimal display shared by pages,
     reports and charts live in `hajiri.core.numbers` (`round_half_up`, `to_fixed`, `whole`), replacing five
     hand-written copies; the file-type signature check is one function (`hajiri.files.store.has_signature`) used by
     uploads and clock-in photos.
8. **Groups of companies.** *Done (27 Oct 2026): switcher, group roles, /group dashboard, directory, consolidated
   reports, group administration (companies, access, transfers, policy copy).* One install runs several
   companies of a group from one sign-in. Tables: `company_group` and `company.group_id`; `company_membership`
   (access to another company with its own roles and employee record, `is_default` for the company a sign-in
   starts in); `group_member` (group roles: `group_admin`, `group_hr`, `group_executive`, which give admin, HR and
   the read-only executive view in every company of the group, including companies added later);
   `session.active_company_id`. Migrations `0003` and `0004` only add objects, so the TS stack keeps working on the
   same database (it only ever sees the home company). `app_user.company_id`, `roles` and `employee_id` stay the
   home company's.
   - `resolve_session` works in the session's active company when the person may still work there, else at home;
     `AuthUser.company_id`, `roles` and `employee_id` are the active company's, and `companies`, `group_id` and
     `group_roles` describe the rest. Leavers lose access per company (a membership whose employee left, or the home
     company and the group roles when the home employee left); they can still sign in while one company is left.
   - The layout always shows the active company's name and code (sidebar and mobile top bar); people with more
     than one company get a switcher that reloads the same page in the new company, or its hub's first page when
     the page showed a record. The Group hub (`/group`) shows headline figures per company and the group admin's
     group roles; admins add sister companies and give people of the group access to their company.
   - JSON: `/api/v1/me/companies` (with `groupId`, `groupRoles`), `/api/v1/me/companies/switch`,
     `/api/v1/group/overview`, `/api/v1/group/roles` (GET, POST) and `/api/v1/group/roles/<userId>` (DELETE).
   - Apps `hajiri.group` (group-wide views) and `hajiri.group_admin` (group administration) are registered and
     mounted before `hajiri.web`, ready for the group pages beyond the overview (combined payroll register,
     cross-company headcount trends).
   - `tests/multicompany` checks switching, roles on switch, leavers, group roles, and that ids of another company
     are not found on 40 pages, partials and JSON endpoints across the apps.
   - `tests/scenario/test_group_scenario.py` walks a holding group end to end through the JSON API and the pages: the
     admin of a standalone company starts the group with two sister companies and names a group HR head; policies
     (a shift, a leave type, a holiday) are set in one company, previewed and copied to the others; people are hired
     into each company with salary assignments; one manager gets access to a second company with her employee record
     there; last month's attendance is uploaded and recomputed and payroll is run, reviewed, approved and locked in
     each company; an employee is transferred; the manager approves leave in both companies from one sign-in. Every
     figure of the group dashboard and of the headcount, attendance, payroll and leave reports is then compared with
     the company's own pages (employees, pay runs, Insights, approvals) and with what the scenario put in.
   - Fixed by that walk (final review, 27 Oct 2026): `POST /api/v1/group/companies` now lets the admin of a company
     outside any group start one, as the Group page already did (it answered 403); the dashboard's headcount (and the
     old `/api/v1/group/overview`) counts people employed today, the definition the headcount report and Insights use,
     instead of every record whose status is still active (after a back-dated transfer the dashboard showed the
     person in both companies until the exit was completed).
9. **TypeScript removal.** *Done (27 Oct 2026).* `apps/`, `packages/`, `node_modules/`, the pnpm workspace files,
   `tsconfig.base.json`, the TS parity dump scripts, the TS scripts (`setup.sh`, `start.sh`, `stop.sh`, `run.sh`,
   `update.sh`, `install-service.sh` and the Node helpers of `lib.sh`) and `infra/docker/` + `infra/docker-compose.yml`
   of the TS stack are gone. The Django versions took the canonical names: `scripts/{setup,start,stop,run,update,
   install-service}.sh` with one `lib.sh`, `infra/docker/` and `infra/docker-compose.yml`. Boot services, pid files and
   logs lost their `django-` prefix (`com.hajiri.web`, `hajiri-web.service`, `.run/web.pid`, `logs/web.log`);
   `install-service.sh` removes any other `com.hajiri.*` / `hajiri-*` unit (the TS `api` and `gateway` units and the
   earlier `django-*` names), and `update.sh` reinstalls units found under an old name.
   - Settings come only from `server/.env` (and `server/gateway.env` for the gateway); `config/settings.py` no longer
     reads `apps/api/.env`. `setup.sh` copies `DATABASE_URL`, `DATA_ENCRYPTION_KEY` and `PASSWORD_PEPPER` from a
     leftover `apps/api/.env`, and `PORT` and `GATEWAY_SIGNING_KEY` from `apps/gateway/.env`, into the server files
     once (README, "Moving from the TypeScript stack"). The development install had its keys moved the same way and
     the gateway's raw archive copied to `server/data/raw`.
   - The crypto round-trip test that ran the TS code (`verify-crypto-parity.ts`) is gone; the fixtures recorded from
     the TS code still check that Python reads what the TS API wrote. The engine parity fixtures are a fixed record.
   - The default test database is `hajiri_test` (it was `hajiri_django_test` while the TS suite owned that name).
   - `docs/api.md` is generated from the drf-spectacular schema (`docs/openapi.yaml`, live at `/api/docs`).
   - Not done before the removal: the side-by-side comparison of both stacks against a copy of a live database.
     Compare the first Django payroll cycles and attendance days of a live install with the previous month's results.
10. **The React web app returns.** *28 Oct 2026.* The server-rendered pages (Django templates, HTMX partials under
   `/_/...`, Alpine.js, `static/js/app.js`, `hajiri.css`, the vendored htmx and Alpine, django-htmx) are removed. The
   web app is the original React app again, in `frontend/` as plain JavaScript (React 19, Mantine, react-router,
   Vite), and Django is its backend: the JSON API under `/api/v1/` keeps the TS paths, bodies and response shapes it
   was written against.
   - `vite build` writes `server/frontend_build/`, which is committed, so setup, start, update and the boot services
     need no Node.js; `scripts/build-frontend.sh` rebuilds it, `update.sh` rebuilds only when Node.js is present and
     pulled commits changed `frontend/` without a build, and the Docker image builds it in a Node stage.
   - Django answers `index.html` (no-cache) for every address outside `/api/` and `/static/` (`hajiri.web.views.spa`,
     like the TS `app.get(/^\/(?!api\/|health).*/)`); WhiteNoise serves the build at the site root (`/assets/*`
     cached for a year, `sw.js` and the manifest revalidated). Responses carry the TS helmet policy
     (`script-src 'self'`, `style-src 'self' 'unsafe-inline'`, `img-src 'self' data: blob:`, `frame-ancestors 'none'`,
     `script-src-attr 'none'`); `upgrade-insecure-requests` only on HTTPS installs. Unknown `/api/` addresses and
     server errors answer the TS JSON (`{"error": "not found"}`, `{"error": "internal error"}`).
   - Cookie requests are checked like the TS `sameOriginGuard` (Origin, else Referer, must be this site or a
     `CORS_ORIGINS` entry) by `SameOriginMiddleware` and `SameOriginAuthentication`, the DRF default; Django's CSRF
     token middleware is gone because the React app never sent a token.
   - Kept: the HTML the API returns (certificates, offer letters, review print-outs, letters, payslips, salary
     sheets), every JSON endpoint, the gateway, `/health`, `/api/schema`, `/api/docs`.
   - The screens for groups of companies, which existed only as server-rendered pages, were rebuilt in the React
     app on 28 to 29 Oct 2026: the company switcher (sidebar and command palette) and the Group hub (`/group`:
     overview, people, reports, companies, access, transfers, policies) on `/api/v1/me/companies` and
     `/api/v1/group/...`.
11. **Final state (29 Oct 2026).** See "Architecture now" below.

## Architecture now

```
browser ── React SPA (frontend/, plain JavaScript; built into server/frontend_build/, committed)
   │           cookie session, same-origin fetch to /api/v1/...
   ▼
waitress :3000 ── Django (server/config, server/hajiri/<app>/)
   ├─ WhiteNoise: /assets/*, sw.js, manifest, icons from server/frontend_build/; /static/
   ├─ /api/v1/...  DRF views (hajiri.<app>.api) on the service layer (hajiri.<app>.services), company-scoped ORM
   ├─ /api/schema, /api/docs (drf-spectacular), /health
   └─ every other address: index.html (hajiri.web.views.spa); React Router shows the page
worker ── manage.py run_worker (django.tasks scheduler + task worker), same database
gateway :8080 ── manage.py run_gateway (ZKTeco ADMS, Hikvision ISAPI), config/gateway_urls.py
PostgreSQL 18
```

- **No TypeScript anywhere.** The repository has no `.ts`/`.tsx` file, no `tsconfig` and no TypeScript dependency
  (`frontend/package.json`: React, Mantine, react-router, dayjs, Vite, Vitest, Prettier). There are no
  server-rendered pages: the only Django templates are the printable HTML documents the API returns (certificate,
  offer letter, review print-out).
- **Install and run need no Node.js.** `setup.sh`, `start.sh`, `update.sh` and the boot services serve the
  committed `server/frontend_build/` (the Docker image builds it in a Node stage); Node.js 20.19+ or 22.12+ is needed
  only to rebuild it.
- **Develop:** Django with reload on :3000 (`cd server && DJANGO_DEBUG=true .venv/bin/python manage.py runserver
  3000`) and Vite on :5173 (`cd frontend && npm ci && npm run dev`, which proxies `/api` and `/health` to :3000).
- **Build:** `./scripts/build-frontend.sh` (`npm ci` + `vite build` into `server/frontend_build/`); commit the build
  with the source change, and restart the web process after a rebuild on a running server (`update.sh` does).
- **The contract.** The React app was written against the TS API (commit `65a9079`); Django keeps its paths,
  methods, query parameters, bodies, JSON field names, status codes and error JSON. Checked on 29 Oct 2026: all 665
  routes of the TS route files (`apps/api/src/modules/*/routes.ts` and the routers they mount, with the `app.ts`
  prefixes) resolve to a Django view that has a handler for their method, and every API call in `frontend/src` does
  too (`tests/web/test_frontend_api_paths.py` keeps checking the second). The React app calls all but these TS routes,
  which Django has anyway: `GET /biometrics/stats`, `GET /biometrics/templates/unmapped`, `POST
  /biometrics/retention/run`, `GET /engage/kudos`, `GET /files/:id`, `GET /lifecycle/assets/:id/history`, `GET
  /payroll/compliance/years`, `GET /reports/adhoc/saved/:id/run`, `PATCH /settings/branches/:id`, `PATCH
  /settings/org-units/:id`, `POST /leave/accrual/run`. Django adds `/api/v1/me/companies...` and `/api/v1/group/...`
  for groups of companies.
- **Tests:** API tests per app (`tests/<app>/test_api_*.py`, ported from `apps/api/test/*.test.ts`, with company
  isolation in `tests/multicompany`), the engines' parity fixtures, the SPA serving tests (`tests/web`), the web
  app's unit tests (`cd frontend && npx vitest run`) and the browser tests (`tests/e2e`, Playwright: every route for
  every role of two demo companies and the group roles, plus the main flows).

## Differences from the TS app and remaining gaps

Checked on 27 Oct 2026 by comparing README "What is included" row by row, every exported TS function, every TS route
handler's service calls, the React routes, tabs and buttons, the decision handlers, the worker jobs and the CLI against
the Django code. Nothing in the capability list is missing. What still differs or is unverified:

- **Not run on real infrastructure.** The Docker image and compose file, `install-service.sh` (launchd and
  systemd units) and the Linux branches of `setup.sh` have not been run: there is no Docker on the development
  Mac and the service script installs real boot agents. Run them on a test machine before relying on them.
- **Side-by-side comparison** against a copy of a live database was not done before the TS stack was removed
  (phase 9).
- **Sorting.** Names sort with the Unicode Collation Algorithm (`hajiri.core.collation`, pyuca) like the browser's
  `localeCompare`; a few modules still use `casefold()`.
- **Profile photos** are shrunk in the browser to a 320 px square JPEG before upload by the React app
  (`AvatarEditor`); `hajiri.core.images.square_jpeg` (Pillow) does the same on the server for uploads sent without it.
- **BS calendar cross-check** against the `@remotemerge/nepali-date-converter` JS library is not run in Python; the
  day-by-day parity test against the TS table (which is cross-checked there) covers it.
- **Browser coverage (29 Oct 2026):** the Playwright crawl signs in as 21 people (all 8 company roles in both demo
  companies, the three group roles and a person in both companies), opens every React route and hub tab with real
  ids, and switches company for the group accounts: no console errors, failed requests or error alerts. Seven action
  flows pass (leave and approval, clock-in, new employee, pay run locked and published, help-desk ticket, expense
  claim, group dashboard). Routes whose records the demo data lacks are covered only by the API tests.
- **Same as TS, by design:** regularisation keeps its own `manual_device_id` (TS `regularisation/routes.ts` had its
  own `manualDeviceId`, with a shorter device name); the TS routes the React app never called (listed under
  "Architecture now") have Django endpoints but no screen, because the React app never offered them.
- The statutory and hardware assumptions in README "Known gaps and assumptions" apply unchanged.

### Remaining gaps after the React return (29 Oct 2026)

State: 2,697 tests pass in six shards (`hajiri_t_cr_1..6`; 33 skipped, 32 of them the browser tests without
`HAJIRI_E2E_URL`), plus the tests added after the run (they pass on targeted runs); `npx vitest run` 51 of 51; `vite
build` clean and identical to the committed build; `manage.py check` clean, `makemigrations --check` no changes.
Fixed in the final review: a gateway query-count test failed by the time of day (batches at different hours cost one
statement more per line when a punch also touches the previous day); a file WhiteNoise listed at startup and a
rebuild deleted now answers 404 instead of 500.

- **`server/frontend_build/` is not committed yet.** Commit it only after rebuilding it from the final `frontend/src`
  (it matches the current source).
- **Group screens not in the React app yet:** choosing which company a fresh sign-in starts in, the switch hiding a
  company's pay from executives for a company admin without a group role, and giving or removing access from the
  company Users page (group admins do it in Group, Access). The JSON endpoints exist.
- **Vestigial `nav.py` modules.** The 40 `hajiri/<app>/nav.py` files and `NavItem` in `hajiri.web.nav` describe the
  retired server-rendered pages (their `url_name`s no longer exist); only `ROLE_LABEL`, the role help texts,
  `feature_on` and `workflow.nav.BADGES` are still used. They can be deleted with that cleanup.
- **TS routes without a screen** (listed under "Architecture now") are served but not exercised by the React app.
- **Not run on real infrastructure** (Docker, boot services, Linux setup) and **no side-by-side comparison** with a
  copy of a live database, as below.

### Remaining gaps after the final review (27 Oct 2026)

State at the end of the review: 2,824 tests pass in six shards (`hajiri_t_critic_1..6`), `manage.py check` is
clean and `makemigrations --check` finds no changes. Also fixed: the navigation's cached URL lookups are cleared when
`ROOT_URLCONF` changes (a layout test with its own URLconf left `/group` without its tabs for later tests).

Honest list of what is not idiomatic yet or not verified; none of it changes what the app does.

- **Output compatibility kept on purpose.** Some code still reproduces how the TypeScript app wrote values, because
  stored data or terminals depend on it: the attendance policy version is `JSON.stringify` with sorted keys
  (`company_settings.schemas`, part of `attendance_day.inputs_hash`); the gateway's `jsutil` matches what ZKTeco and
  Hikvision terminals were answered; the audit log stores values as the TS code serialised them. Display rounding
  (halves up) is now one helper, but `punch` and `performance` keep their own decimal rounding (it rounds the printed
  value, 1.005 to 1.01, which their tests require).
- **Per-item queries.** The Approvals badge (`workflow.inbox_count`) works out the approver of each pending item one
  at a time (3 or 4 queries per item); batching it means rewriting the delegation, absence and HR-fallback rules.
- **Shared validation fields.** Each slice still has its own strict JSON fields (StrictSerializer, JsonBool,
  NumberField and similar); they are not merged into `hajiri.core.validation`. Not done either: `notify_many` and a
  bulk `user_id_for_employee`, a multi-field `order_by` in `hajiri.web.tables`.
- **Tests read tables in plain SQL** in about 99 files (`tests/support/sql.py`); application code does not.
- **Exits wait for clearance.** An accepted exit (including the source side of a transfer) keeps the employee's
  status `active` until clearance is done and the nightly job or HR completes it, so the company's employee list
  still shows the person, with the exit date, after the last working day. Headcounts (dashboard, reports, Insights)
  already leave them out.
- **The `group.policy` setting** (hide pay from executives in group views) is changed only on `/group` by an admin; it
  is not on the general company settings pages or API.
- **Optional database checks not added:** that a `device_user_map` employee belongs to the device's company, a data
  migration for old active devices with no company, and expression unique constraints on time-tracking clients,
  projects and activities.
- **Letters** escape apostrophes as `&#x27;` (Django's escaping) instead of `&#39;`; they look the same.
- **Not run on real infrastructure** (Docker, boot services, Linux setup) and **no side-by-side comparison** with a
  copy of a live database (above).

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
- Tests use their own database, `hajiri_test` by default (`DJANGO_TEST_DATABASE`).
- Email goes through Django's email framework: `SMTP_URL` sets EMAIL_BACKEND (STARTTLS when offered), without it
  the console backend logs messages. `TASKS_BACKEND` can point the task framework at another backend.
- `hajiri_schema_ref` is a scratch database built by the TS migrations, used by `tools/gen_models.py`.
- `cd server && python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/pytest`.
