# Hajiri · हाजिरी

Self-hosted multi-biometric attendance, HR and payroll platform for companies in Nepal. **plan.md** is the master plan
and specification; `docs/DEVELOPMENT.md` is the developer guide; `docs/api.md` lists every endpoint; `docs/gateway.md`
explains terminal onboarding; `docs/adr/` records decisions.

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
| Security and privacy | Sessions with lockout and idle timeout, two-step sign-in with authenticator apps and recovery codes (can be required per role), active sessions, specialist roles (payroll, attendance, recruiter, executive), scoping to company, team, department and branch, field encryption for PAN, bank, ID numbers and files, hash-chained audit log, append-only punches, consent records, crypto-shredding, DSAR export, transparency page |
| Operations | One-command install, start/stop, boot services, update script, Docker stack, installable web app (PWA), per-company feature switches, English web app in the CoreValley visual identity with a command palette (Cmd+K), light and dark mode; every date is shown in Bikram Sambat (English month names) |

## Install on a new machine (macOS or Ubuntu/Debian)

```sh
git clone <your-github-url> hajiri
cd hajiri
./scripts/setup.sh --company ACME --company-name "Acme Pvt. Ltd."
```

That installs Node.js, pnpm and PostgreSQL 18 if missing, creates the databases and your company, generates all secrets,
applies the schema, seeds statutory defaults, creates the admin user (credentials printed once into `.admin-credentials`)
and builds the web app. Re-running is safe. Omit `--company` to get a demo company with sample employees; then
`DATABASE_URL=<demo database> pnpm --filter @hajiri/api cli seed-demo-suite --yes` fills every HR module with sample data
(it refuses to run without an explicit database).

## Run

| What | Command |
|---|---|
| Start everything (foreground, Ctrl+C stops) | `./scripts/start.sh` |
| Start in the background | `./scripts/start.sh --daemon`, stop with `./scripts/stop.sh` |
| Auto-start on boot and restart on crash | `./scripts/install-service.sh` (remove with `--uninstall`) |
| Update to the latest version from GitHub | `./scripts/update.sh` |
| Run with Docker instead | see `infra/docker-compose.yml` |

Open http://localhost:3000 and sign in with the admin credentials, then follow the setup checklist. Point terminals at
`http://<this-machine>:8080` (ZKTeco ADMS) or `http://<this-machine>:8080/hikvision/event?sn=<serial>` (Hikvision); new
terminals appear as pending under Devices and must be approved before their punches count.

## Repository map

| Path | What |
|---|---|
| `packages/bs-calendar` | Bikram Sambat table (2000–2090 BS, i.e. 1943–2034 AD; 2084 onward projected), cross-checked against three converters and Nepali Patro |
| `packages/attendance-engine` | pure `computeDay` / `computeOt` with worked-example and property tests |
| `packages/payroll-engine` | pure payroll computation, formula DSL, tax schedules, 28 golden fixtures |
| `apps/api` | Express API, SQL migrations, all HR/attendance/payroll modules, background worker |
| `apps/gateway` | device gateway (ADMS, ISAPI), signed idempotent ingest, encrypted templates, simulator |
| `apps/web` | React web app for employees, managers and HR |
| `scripts/` | setup, start/stop, run, update, service installer |
| `infra/` | Docker images and compose stack |

## Verification

`pnpm test` runs every suite (engine, calendar, payroll engine, gateway, API integration tests against PostgreSQL).

## Known gaps and assumptions

These are documented in code comments and plan.md section 18, and need a decision or real hardware:

- Statutory interpretation choices to confirm with a chartered accountant: employer SSF contribution counted as taxable
  income; SST waiver for SSF, PF and CIT contributors; disability deduction; remote-area figures.
- Golden payroll fixtures are engine snapshots, not accountant-signed; a two-cycle parallel run is still required before
  payroll go-live (plan section 15).
- BS 2084 to 2090 month lengths are projections and must be verified against the official calendar when it is published. The built-in public holiday list covers BS 2083 only; add later years under Settings, Holidays.
- Terminal protocol details (ZKTeco status/verify codes, template layouts, Hikvision field names) come from community
  sources and must be verified on purchased units; the ZKTeco comm key is not validated (VLAN and serial allow-list are
  the controls).
- The SSF sheet layout follows a single vendor source and needs confirmation against the SSF portal.
- Email, SMS and push are delivered through webhook bridges; no built-in SMTP or SMS provider client.
- Passkeys/FIDO2, OpenBao key management and a native mobile app are not built; the web app is responsive, installable
  as an app on phones (PWA) and covers the employee and manager flows. Uploaded files are stored encrypted in PostgreSQL.
- New tax rules (donations, medical credit, remote area) and optimum CIT follow the Income Tax Act as read here and are
  marked "confirm with your accountant" in the app.
