# API reference

All routes are under `/api/v1` and need a session (cookie `hajiri_session` or `Authorization: Bearer <token>`) except `POST /auth/login`.
Admin can call every route. "Signed-in" routes apply their own scoping: employees see their own records, managers their direct reports, HR the whole company.
Generated from the route files; every route here is exercised by the API test suite.

## auth

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/auth/login` | Public |
| POST | `/api/v1/auth/logout` | Signed-in |
| GET | `/api/v1/auth/me` | Signed-in |
| POST | `/api/v1/auth/change-password` | Signed-in |
| GET | `/api/v1/auth/users` | HR |
| POST | `/api/v1/auth/users` | Admin |
| PATCH | `/api/v1/auth/users/:id` | Admin |

## employees

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/employees` | HR, Manager |
| GET | `/api/v1/employees/:id` | Signed-in |
| GET | `/api/v1/employees/:id/sensitive` | HR |
| POST | `/api/v1/employees` | HR |
| PATCH | `/api/v1/employees/:id` | HR |
| POST | `/api/v1/employees/import` | HR |
| GET | `/api/v1/employees/:id/assignments` | Signed-in |
| GET | `/api/v1/employees/:id/audit` | Signed-in |

## attendance

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/attendance/recompute` | HR, Manager |
| POST | `/api/v1/attendance/recompute-range` | HR |
| GET | `/api/v1/attendance/days` | Signed-in |
| GET | `/api/v1/attendance/punches` | Signed-in |
| GET | `/api/v1/attendance/exceptions` | HR, Manager |
| GET | `/api/v1/attendance/today` | HR, Manager |

## devices

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/devices` | HR, Admin |
| POST | `/api/v1/devices/:id/activate` | HR, Admin |
| POST | `/api/v1/devices/:id/key` | HR, Admin |
| POST | `/api/v1/devices/:id/disable` | HR, Admin |
| GET | `/api/v1/devices/:id/mappings` | HR, Admin |
| POST | `/api/v1/devices/:id/map` | HR, Admin |
| GET | `/api/v1/devices/punches/unmapped` | HR, Admin |
| POST | `/api/v1/devices/:id/commands` | HR, Admin |
| GET | `/api/v1/devices/:id/commands` | HR, Admin |
| POST | `/api/v1/devices/:id/sync-users` | HR, Admin |

## shifts

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/shifts/holidays` | Signed-in |
| POST | `/api/v1/shifts/holidays` | HR |
| PATCH | `/api/v1/shifts/holidays/:id` | HR |
| DELETE | `/api/v1/shifts/holidays/:id` | HR |
| POST | `/api/v1/shifts/holidays/import-default` | HR |
| GET | `/api/v1/shifts/locations` | Signed-in |
| POST | `/api/v1/shifts/locations` | HR |
| PATCH | `/api/v1/shifts/locations/:id` | HR |
| DELETE | `/api/v1/shifts/locations/:id` | HR |
| GET | `/api/v1/shifts/calendar` | Signed-in |
| GET | `/api/v1/shifts/roster` | HR, Manager |
| PUT | `/api/v1/shifts/roster` | HR, Manager |
| DELETE | `/api/v1/shifts/roster` | HR, Manager |
| POST | `/api/v1/shifts/roster/copy-week` | HR, Manager |
| POST | `/api/v1/shifts/roster/publish` | HR, Manager |
| POST | `/api/v1/shifts/assign` | HR |
| GET | `/api/v1/shifts` | Signed-in |
| POST | `/api/v1/shifts` | HR |
| GET | `/api/v1/shifts/:id` | Signed-in |
| PATCH | `/api/v1/shifts/:id` | HR |
| DELETE | `/api/v1/shifts/:id` | HR |

## leave

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/leave/types` | Signed-in |
| POST | `/api/v1/leave/types` | HR |
| PATCH | `/api/v1/leave/types/:id` | HR |
| GET | `/api/v1/leave/balances` | Signed-in |
| GET | `/api/v1/leave/preview` | Signed-in |
| POST | `/api/v1/leave/requests` | Signed-in |
| GET | `/api/v1/leave/requests` | Signed-in |
| POST | `/api/v1/leave/requests/:id/cancel` | Signed-in |
| POST | `/api/v1/leave/accrual/run` | HR |
| POST | `/api/v1/leave/adjust` | HR |
| GET | `/api/v1/leave/ledger` | Signed-in |
| GET | `/api/v1/leave/team-calendar` | Signed-in |

## workflow

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/workflow/inbox` | Signed-in |
| GET | `/api/v1/workflow/:id` | Signed-in |
| POST | `/api/v1/workflow/:id/approve` | Signed-in |
| POST | `/api/v1/workflow/:id/reject` | Signed-in |
| POST | `/api/v1/workflow/bulk-approve` | Signed-in |

## regularisation

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/regularisation` | Signed-in |
| GET | `/api/v1/regularisation` | Signed-in |
| POST | `/api/v1/regularisation/:id/cancel` | Signed-in |

## ot

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/ot/requests` | Signed-in |
| GET | `/api/v1/ot/requests` | Signed-in |
| POST | `/api/v1/ot/requests/:id/cancel` | Signed-in |
| GET | `/api/v1/ot/slips` | Signed-in |
| POST | `/api/v1/ot/slips/:id/approve` | HR, Manager |
| POST | `/api/v1/ot/slips/:id/reject` | HR, Manager |
| POST | `/api/v1/ot/slips/:id/comp-off` | HR, Manager |
| GET | `/api/v1/ot/register` | HR |

## notifications

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/notifications` | Signed-in |
| POST | `/api/v1/notifications/read-all` | Signed-in |
| POST | `/api/v1/notifications/:id/read` | Signed-in |

## reports

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/reports/daily` | HR, Manager |
| GET | `/api/v1/reports/register` | HR, Manager |
| GET | `/api/v1/reports/ot` | HR, Manager |
| GET | `/api/v1/reports/late-early` | HR, Manager |
| GET | `/api/v1/reports/leave-balances` | HR |
| GET | `/api/v1/reports/headcount` | HR, Manager |
| GET | `/api/v1/reports/exceptions-summary` | HR, Manager |
| GET | `/api/v1/reports/devices` | HR |
| GET | `/api/v1/reports/punch-audit` | HR, Manager |

## punch

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/punch` | Signed-in |
| GET | `/api/v1/punch/today` | Signed-in |
| GET | `/api/v1/punch/remote` | Signed-in |
| GET | `/api/v1/punch/photos/:id` | Signed-in |
| POST | `/api/v1/punch/manual` | HR |

## settings

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/settings/company` | HR, Admin |
| GET | `/api/v1/settings` | HR, Admin |
| PUT | `/api/v1/settings/:key` | HR, Admin |
| POST | `/api/v1/settings/preview/attendance` | HR, Admin |
| GET | `/api/v1/settings/branches` | HR, Admin |
| POST | `/api/v1/settings/branches` | HR, Admin |
| PATCH | `/api/v1/settings/branches/:id` | HR, Admin |
| GET | `/api/v1/settings/org-units` | HR, Admin |
| POST | `/api/v1/settings/org-units` | HR, Admin |
| PATCH | `/api/v1/settings/org-units/:id` | HR, Admin |

## payroll

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/payroll/rules` | HR |
| POST | `/api/v1/payroll/rules` | Admin |
| POST | `/api/v1/payroll/rules/:id/approve` | HR |
| GET | `/api/v1/payroll/components` | HR |
| POST | `/api/v1/payroll/components` | HR |
| PATCH | `/api/v1/payroll/components/:code` | HR |
| GET | `/api/v1/payroll/structures` | HR |
| POST | `/api/v1/payroll/structures` | HR |
| POST | `/api/v1/payroll/structures/:id/versions` | HR |
| GET | `/api/v1/payroll/assignments` | HR |
| POST | `/api/v1/payroll/assignments` | HR |
| GET | `/api/v1/payroll/declarations` | Signed-in |
| PUT | `/api/v1/payroll/declarations` | Signed-in |
| POST | `/api/v1/payroll/declarations/:employeeId/verify` | HR |
| GET | `/api/v1/payroll/loans` | HR |
| POST | `/api/v1/payroll/loans` | HR |
| GET | `/api/v1/payroll/periods` | HR |
| POST | `/api/v1/payroll/periods` | HR |
| GET | `/api/v1/payroll/runs` | HR |
| POST | `/api/v1/payroll/runs` | HR |
| POST | `/api/v1/payroll/runs/bonus` | HR |
| GET | `/api/v1/payroll/runs/:id` | HR |
| POST | `/api/v1/payroll/runs/:id/preflight` | HR |
| POST | `/api/v1/payroll/runs/:id/lock-attendance` | HR |
| POST | `/api/v1/payroll/runs/:id/inputs` | HR |
| GET | `/api/v1/payroll/runs/:id/inputs` | HR |
| POST | `/api/v1/payroll/runs/:id/compute` | HR |
| POST | `/api/v1/payroll/runs/:id/review` | HR |
| POST | `/api/v1/payroll/runs/:id/approve` | HR |
| POST | `/api/v1/payroll/runs/:id/lock` | HR |
| POST | `/api/v1/payroll/runs/:id/publish` | HR |
| GET | `/api/v1/payroll/runs/:id/ssf-sheet.csv` | HR |
| GET | `/api/v1/payroll/runs/:id/salary-sheet` | HR |
| GET | `/api/v1/payroll/payslips` | Signed-in |
| GET | `/api/v1/payroll/payslips/:id` | Signed-in |
| GET | `/api/v1/payroll/payslips/:id/html` | Signed-in |

## biometrics

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/biometrics/transparency` | Signed-in |
| POST | `/api/v1/biometrics/consent` | Signed-in |
| GET | `/api/v1/biometrics/consent` | Signed-in |
| POST | `/api/v1/biometrics/consent/:id/revoke` | Signed-in |
| GET | `/api/v1/biometrics/templates` | Signed-in |
| GET | `/api/v1/biometrics/templates/unmapped` | HR |
| POST | `/api/v1/biometrics/templates/:id/shred` | HR |
| POST | `/api/v1/biometrics/employees/:id/shred-all` | HR |
| GET | `/api/v1/biometrics/dsar` | Signed-in |
| POST | `/api/v1/biometrics/retention/run` | HR, Admin |
| GET | `/api/v1/biometrics/stats` | HR |

## dashboard

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/dashboard/executive` | HR, Manager |
| GET | `/api/v1/dashboard/me` | Signed-in |

## gateway (port 8080)

The device gateway receives punches from ZKTeco (ADMS/push) and Hikvision devices and forwards them to the API. See `apps/gateway`.
