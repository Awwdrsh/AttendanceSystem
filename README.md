# Hajiri · हाजिरी

Self-hosted multi-biometric attendance, HR and payroll platform for companies in Nepal. **plan.md** is the master plan
and specification; `docs/DEVELOPMENT.md` is the developer guide; `docs/api.md` lists every endpoint; `docs/gateway.md`
explains terminal onboarding; `docs/adr/` records decisions.

## What is included

| Area | Capability |
|---|---|
| Attendance capture | ZKTeco/eSSL ADMS push and Hikvision ISAPI push through a hardened device gateway; clock in and out from the browser, including work from home (per-employee permission or an approved work-from-home day, tagged Remote); manual and regularised punches; immutable punch log; unmapped-punch queue; offline-device alerts |
| Attendance processing | Pure, recomputable day engine (Frappe Shift Type semantics): pairing modes, grace, thresholds, night shifts, holidays (branch and gender aware), weekly offs, leave, WFH/on-duty, overtime detection with the Labour Act caps, versioned days, payroll locks, exceptions queue |
| Shifts and holidays | Shifts, effective-dated assignments, BS holiday calendar with the 2083 default list (roster and geofence APIs exist; the web app keeps them out of the menu) |
| Leave | Statutory and company leave types, double-entry ledger, working-day maths with sandwich rule, LWP split suggestion, approval workflow, monthly accrual (1 per 20 worked days, sick 12/yr, caps), substitute leave expiry, encashment |
| Approvals | Generic multi-step workflow with manager/HR/finance resolution, inbox, one-tap decisions, delegation, signed actions, SLA reminders and escalation |
| Payroll | Formula DSL and component graph, BS-month proration, SSF 11/20 and PF/gratuity regimes, CIT, versioned income-tax schedules (FY 2082/83 and 2083/84) with TDS projection and true-up, festival allowance, loans, arrears, supplementary, final settlement, Bonus Act run, maker-checker with two-approver threshold, locked runs, printable payslips, bank files (NMB, Nabil, NIC Asia, Everest, Prabhu), SSF sheet, ETDS annex, journal export |
| Self-service and management | Eight-item menu. Employees: Home (clock in/out, request work from home), My attendance, Time off, Payslips, Profile and privacy. Managers: Approvals, Attendance (who's in, fix a punch), Leave calendar. HR: Overview, Employees with CSV import, Attendance (exceptions, overtime), Leave (balances, policies, history), Payroll, Reports, Settings (rules, shifts, holidays, devices, users, setup checklist) |
| Security and privacy | Sessions with lockout and idle timeout, role-based access scoped to company and direct reports, field encryption for PAN, bank accounts and biometric templates, hash-chained audit log, append-only punches, consent records, crypto-shredding, DSAR export, transparency page |
| Operations | One-command install, start/stop, boot services, update script, Docker stack, English web app in the CoreValley visual identity (logo, hydro green, Manrope and JetBrains Mono, all assets self-hosted) with a command palette (Cmd+K), light and dark mode; every date is shown in Bikram Sambat (English month names) with a Nepali-month date picker and calendars |

## Install on a new machine (macOS or Ubuntu/Debian)

```sh
git clone <your-github-url> hajiri
cd hajiri
./scripts/setup.sh --company ACME --company-name "Acme Pvt. Ltd."
```

That installs Node.js, pnpm and PostgreSQL 18 if missing, creates the databases and your company, generates all secrets,
applies the schema, seeds statutory defaults, creates the admin user (credentials printed once into `.admin-credentials`)
and builds the web app. Re-running is safe. Omit `--company` to get a demo company with sample employees.

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
| `apps/web` | React web app for employees, managers, HR and finance |
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
- Bank, SSF and ETDS file layouts follow a single vendor source and need confirmation from each bank and portal.
- Email, SMS and push are delivered through webhook bridges; no built-in SMTP or SMS provider client.
- Passkeys/FIDO2, OpenBao key management, object storage for photos and documents, and the Expo mobile app are not
  built; the web app is responsive and covers the employee flows.
