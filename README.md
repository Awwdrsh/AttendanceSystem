# Hajiri

In-house multi-biometric attendance, HR and payroll platform for Nepal. See **plan.md** for the full master plan,
`docs/DEVELOPMENT.md` for the developer guide and `docs/adr/` for decisions.
Current state: **Phase 1** (attendance core, device gateway, employee master).

## Install on a new machine (macOS or Ubuntu/Debian)

```sh
git clone <your-github-url> hajiri
cd hajiri
./scripts/setup.sh
```

That installs Node.js, pnpm and PostgreSQL 18 if missing, creates the databases, writes the `.env` files with a fresh
signing key, installs dependencies, applies the schema and loads a demo company. Re-running it is safe.
Use `--no-demo` for a real deployment and `--with-tests` to verify the install.

## Run

| What | Command |
|---|---|
| Start everything in the foreground (Ctrl+C stops) | `./scripts/start.sh` |
| Start in the background | `./scripts/start.sh --daemon` then `./scripts/stop.sh` |
| Auto-start on boot and restart on crash | `./scripts/install-service.sh` (remove with `--uninstall`) |
| Update to the latest version from GitHub | `./scripts/update.sh` |

Services: API on http://localhost:3000 (`/health`, `/api/v1/...`) and the device gateway on http://localhost:8080.
Point ZKTeco/eSSL terminals at `http://<this-machine>:8080/iclock` (ADMS "cloud server" setting). New terminals show up
as *pending* and must be approved via `POST /api/v1/devices/:id/activate` before their punches are accepted.
Logs are written to `logs/`.

## Run with Docker instead
On a machine with Docker, no local Node or PostgreSQL is needed:
```sh
cp infra/.env.docker.example infra/.env.docker     # fill POSTGRES_PASSWORD, DATA_ENCRYPTION_KEY, PASSWORD_PEPPER, GATEWAY_SIGNING_KEY, admin password
docker compose -f infra/docker-compose.yml --env-file infra/.env.docker up -d --build
```
The API container applies migrations and seeds on every start and creates the admin user from the env file. The web
app is at http://localhost:3000, the device gateway at http://localhost:8080. Terminal setup: `docs/gateway.md`.

## Try it with the simulator

```sh
pnpm --filter @hajiri/gateway simulate -- --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03" --pin 1 --time "2026-09-21 18:40:00"
curl -s -X POST localhost:3000/api/v1/attendance/recompute -H 'content-type: application/json' \
  -d '{"employeeId":"00000000-0000-7000-8000-000000000011","localDate":"2026-09-21"}'
```

## Repository map
| Path | What |
|---|---|
| `packages/bs-calendar` | BS calendar table and conversion (plan §6.10) |
| `packages/attendance-engine` | pure `computeDay` / `computeOt` with worked-example and property tests (plan §10) |
| `apps/api` | Express API, SQL migrations (plan §9), attendance recompute, employee and device endpoints |
| `apps/gateway` | ZKTeco ADMS and Hikvision ISAPI gateway: idempotent signed punch ingest, encrypted template storage, raw archive, simulator (plan §7, Appendix B/C; see `docs/gateway.md`) |
| `scripts/` | setup, start/stop, update and service installers |
| `infra/` | Docker images, compose stack and entrypoint for container deployments |

## Manual developer setup
If you prefer not to use the scripts: install Node ≥ 22, pnpm 12 and PostgreSQL 18, create databases `hajiri` and
`hajiri_test`, copy `apps/*/.env.example` to `.env`, then `pnpm install`, `pnpm --filter @hajiri/api db:migrate`,
`db:seed:bs`, `db:seed:demo`, `pnpm test`.
