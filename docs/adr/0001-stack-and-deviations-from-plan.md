# ADR 0001: Stack choice and deviations from plan.md

Date: 2026-10-13. Status: accepted.

## Context
plan.md §8.4 recommends a NestJS 12 modular monolith, a Go device gateway, PostgreSQL 18, React 19 and an Expo app.
The owner asked for a MERN-style stack with Postgres and said to pick whatever suits biometric integration best.

## Decision
| Layer | plan.md | Chosen | Why |
|---|---|---|---|
| API | NestJS 12 | **Express 5 + TypeScript** | Same runtime and language the plan argues for (largest non-PHP pool in Nepal); less framework to learn for a small team. Modules live under `apps/api/src/modules/*` so a later move to NestJS is mechanical. |
| Device gateway | Go 1.27 | **Express 5 + TypeScript** (separate service, `apps/gateway`) | ADMS and ISAPI are plain HTTP push; one language for the whole team. Plan §8.4 itself allows a Node gateway for the lean path. Kept as a separate process so it can still be the only thing on the biometric VLAN, and can be rewritten in Go later without touching the API. |
| Language | TypeScript | **TypeScript** | Payroll money math and shared types between API, gateway and React. |
| Database | PostgreSQL 18 | **PostgreSQL 18.6** (Homebrew, port 5433 locally) | Temporal `WITHOUT OVERLAPS` keys, `uuidv7()`, partitioning, as in plan §8.5. |
| Front end | React 19 | React 19 + Vite (next step) | Unchanged. |
| Mobile | Expo | Expo (Phase 2) | Unchanged. |

Everything else in plan.md (data model §9, computation §10, security §12, roadmap §14) stands.

## Consequences
- Two Node services share one database schema owned by `apps/api/migrations`.
- Decimal money handling must be explicit (plan §11.7); `pg` is configured to return NUMERIC as strings.
- The attendance engine is a dependency-free package (`packages/attendance-engine`) so it can be reused by a worker, the API, or tests without I/O.
