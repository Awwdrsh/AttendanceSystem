# Development guide

Hajiri is an in-house biometric attendance, HR and payroll platform for a Nepali company. **plan.md is the specification**;
reference its section numbers in commit messages and comments when encoding a rule (e.g. "plan §10.4").

## Layout
- `packages/bs-calendar` — Bikram Sambat table + AD/BS conversion. Pure, no deps. Extend the table in `src/data.ts`.
- `packages/attendance-engine` — `computeDay()` pure function over immutable punches (plan §10). No I/O, no clock.
- `apps/api` — Express 5 API. Owns the DB schema (`migrations/*.sql`, forward-only, checksummed).
- `apps/gateway` — device gateway (ZKTeco ADMS push now; Hikvision ISAPI next). The only thing terminals talk to.
- `scripts/` — install, start/stop, update and service scripts (see README).
- `docs/adr` — decisions; ADR 0001 records deviations from plan.md.

## Commands
```
pnpm install
pnpm test                               # all unit + integration tests (needs PostgreSQL with db hajiri_test)
pnpm --filter @hajiri/api db:migrate    # apply pending migrations
pnpm --filter @hajiri/api dev           # http://localhost:3000 with reload
pnpm --filter @hajiri/gateway dev       # http://localhost:8080 with reload
pnpm --filter @hajiri/gateway simulate -- --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
```
Local PostgreSQL 18 runs on port **5433** on macOS (so an existing PostgreSQL on 5432 is untouched) and 5432 on Linux.
Databases: `hajiri`, `hajiri_test`. Connection strings live in `apps/*/.env` (never committed).

## Rules that must not be broken
- `punch_event` and `audit_log` are append-only (DB triggers enforce it). Corrections are new rows.
- `attendance_day` is versioned; a new version is written only when `inputs_hash` changes; locked days are never rewritten.
- Never emit an ADMS `SHELL` command (DB CHECK + store guard + test).
- Money never goes through a JS float: NUMERIC comes back as strings; use decimal/integer-paisa arithmetic (plan §11.7).
- All statutory numbers come from `rule_version` rows (plan §6.13), never hard-coded, once the payroll module exists.
- Biometric templates are only ever stored envelope-encrypted (plan §9.1 decision 5); until that exists, `/iclock/fdata` discards.

## Adding a migration
Create `apps/api/migrations/NNNN_description.sql` (next number, forward-only). Applied files are checksummed; never edit one
after it has been applied, add a new file instead.

## Style
TypeScript strict, ESM, NodeNext imports with `.js` extensions, Vitest. SQL migrations are hand-written for PostgreSQL 18 features.
