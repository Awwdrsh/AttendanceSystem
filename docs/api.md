# API reference

All routes are under `/api/v1` and need a session (cookie `hajiri_session` or `Authorization: Bearer <token>`) except `POST /auth/login`.
Admin can call every route. "Signed-in" routes apply their own scoping: employees see their own records, managers their direct reports, HR the whole company.
Generated from the route files; every route here is exercised by the API test suite.

## auth

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/auth/login` | Public |
| POST | `/api/v1/auth/login/2fa` | Public |
| POST | `/api/v1/auth/logout` | Signed-in |
| GET | `/api/v1/auth/me` | Signed-in |
| POST | `/api/v1/auth/change-password` | Signed-in |
| GET | `/api/v1/auth/2fa` | Signed-in |
| POST | `/api/v1/auth/2fa/setup` | Signed-in |
| POST | `/api/v1/auth/2fa/enable` | Signed-in |
| POST | `/api/v1/auth/2fa/recovery-codes` | Signed-in |
| POST | `/api/v1/auth/2fa/disable` | Signed-in |
| GET | `/api/v1/auth/sessions` | Signed-in |
| POST | `/api/v1/auth/sessions/revoke-others` | Signed-in |
| DELETE | `/api/v1/auth/sessions/:id` | Signed-in |
| POST | `/api/v1/auth/users/:id/2fa/reset` | Admin |
| GET | `/api/v1/auth/users` | HR |
| POST | `/api/v1/auth/users` | Admin |
| PATCH | `/api/v1/auth/users/:id` | Admin |

## kiosk

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/kiosk/pair` | Signed-in |
| GET | `/api/v1/kiosk/session` | Signed-in |
| POST | `/api/v1/kiosk/identify` | Signed-in |
| POST | `/api/v1/kiosk/punch` | Signed-in |

## employees

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/employees` | HR, Manager, ...read_all |
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
| POST | `/api/v1/attendance/recompute` | ...module_roles.attendance, Manager |
| POST | `/api/v1/attendance/recompute-range` | Signed-in |
| GET | `/api/v1/attendance/days` | Signed-in |
| GET | `/api/v1/attendance/punches` | Signed-in |
| GET | `/api/v1/attendance/exceptions` | ...module_roles.attendance, Manager |
| GET | `/api/v1/attendance/today` | ...module_roles.attendance, Manager, Executive |

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
| POST | `/api/v1/shifts/holidays` | Signed-in |
| PATCH | `/api/v1/shifts/holidays/:id` | Signed-in |
| DELETE | `/api/v1/shifts/holidays/:id` | Signed-in |
| POST | `/api/v1/shifts/holidays/import-default` | Signed-in |
| GET | `/api/v1/shifts/locations` | Signed-in |
| POST | `/api/v1/shifts/locations` | Signed-in |
| PATCH | `/api/v1/shifts/locations/:id` | Signed-in |
| DELETE | `/api/v1/shifts/locations/:id` | Signed-in |
| GET | `/api/v1/shifts/calendar` | Signed-in |
| GET | `/api/v1/shifts/roster` | ...module_roles.attendance, Manager |
| PUT | `/api/v1/shifts/roster` | ...module_roles.attendance, Manager |
| DELETE | `/api/v1/shifts/roster` | ...module_roles.attendance, Manager |
| POST | `/api/v1/shifts/roster/copy-week` | ...module_roles.attendance, Manager |
| POST | `/api/v1/shifts/roster/publish` | ...module_roles.attendance, Manager |
| POST | `/api/v1/shifts/assign` | Signed-in |
| GET | `/api/v1/shifts` | Signed-in |
| POST | `/api/v1/shifts` | Signed-in |
| GET | `/api/v1/shifts/:id` | Signed-in |
| PATCH | `/api/v1/shifts/:id` | Signed-in |
| DELETE | `/api/v1/shifts/:id` | Signed-in |

## leave

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/leave/types` | Signed-in |
| POST | `/api/v1/leave/types` | Signed-in |
| PATCH | `/api/v1/leave/types/:id` | Signed-in |
| POST | `/api/v1/leave/types/order` | HR |
| GET | `/api/v1/leave/balances` | Signed-in |
| GET | `/api/v1/leave/preview` | Signed-in |
| POST | `/api/v1/leave/requests` | Signed-in |
| GET | `/api/v1/leave/requests` | Signed-in |
| POST | `/api/v1/leave/requests/:id/cancel` | Signed-in |
| POST | `/api/v1/leave/accrual/run` | Signed-in |
| POST | `/api/v1/leave/adjust` | Signed-in |
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
| POST | `/api/v1/ot/slips/:id/approve` | ...module_roles.attendance, Manager |
| POST | `/api/v1/ot/slips/:id/reject` | ...module_roles.attendance, Manager |
| POST | `/api/v1/ot/slips/:id/comp-off` | ...module_roles.attendance, Manager |
| GET | `/api/v1/ot/register` | Signed-in |

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
| POST | `/api/v1/punch/manual` | Signed-in |

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
| GET | `/api/v1/payroll/rules` | Signed-in |
| POST | `/api/v1/payroll/rules` | Admin |
| POST | `/api/v1/payroll/rules/:id/approve` | Signed-in |
| GET | `/api/v1/payroll/components` | Signed-in |
| POST | `/api/v1/payroll/components` | Signed-in |
| PATCH | `/api/v1/payroll/components/:code` | Signed-in |
| GET | `/api/v1/payroll/structures` | Signed-in |
| POST | `/api/v1/payroll/structures` | Signed-in |
| POST | `/api/v1/payroll/structures/:id/versions` | Signed-in |
| GET | `/api/v1/payroll/assignments` | Signed-in |
| POST | `/api/v1/payroll/assignments` | Signed-in |
| GET | `/api/v1/payroll/declarations` | Signed-in |
| PUT | `/api/v1/payroll/declarations` | Signed-in |
| POST | `/api/v1/payroll/declarations/:employeeId/verify` | Signed-in |
| GET | `/api/v1/payroll/loans` | Signed-in |
| POST | `/api/v1/payroll/loans` | Signed-in |
| GET | `/api/v1/payroll/periods` | Signed-in |
| POST | `/api/v1/payroll/periods` | Signed-in |
| GET | `/api/v1/payroll/runs` | Signed-in |
| POST | `/api/v1/payroll/runs` | Signed-in |
| POST | `/api/v1/payroll/runs/bonus` | Signed-in |
| GET | `/api/v1/payroll/runs/:id` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/preflight` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/lock-attendance` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/inputs` | Signed-in |
| GET | `/api/v1/payroll/runs/:id/inputs` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/compute` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/review` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/approve` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/lock` | Signed-in |
| POST | `/api/v1/payroll/runs/:id/publish` | Signed-in |
| GET | `/api/v1/payroll/runs/:id/ssf-sheet.csv` | Signed-in |
| GET | `/api/v1/payroll/runs/:id/salary-sheet` | Signed-in |
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
| GET | `/api/v1/dashboard/executive` | HR, Manager, Executive |
| GET | `/api/v1/dashboard/me` | Signed-in |

## files

| Method | Path | Access |
|---|---|---|
| POST | `/api/v1/files` | Signed-in |
| GET | `/api/v1/files/:id` | Signed-in |

## people

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/people/directory` | Signed-in |
| GET | `/api/v1/people/summary` | Signed-in |
| GET | `/api/v1/people/org-chart` | Signed-in |
| GET | `/api/v1/people/structure` | Signed-in |
| POST | `/api/v1/people/structure/branches` | HR |
| PATCH | `/api/v1/people/structure/branches/:id` | HR |
| DELETE | `/api/v1/people/structure/branches/:id` | HR |
| POST | `/api/v1/people/structure/units` | HR |
| PATCH | `/api/v1/people/structure/units/:id` | HR |
| DELETE | `/api/v1/people/structure/units/:id` | HR |
| POST | `/api/v1/people/structure/designations` | HR |
| PATCH | `/api/v1/people/structure/designations/:id` | HR |
| DELETE | `/api/v1/people/structure/designations/:id` | HR |
| GET | `/api/v1/people/policy` | HR |
| PUT | `/api/v1/people/policy` | HR |
| POST | `/api/v1/people/me/change-requests` | Signed-in |
| GET | `/api/v1/people/me/change-requests` | Signed-in |
| POST | `/api/v1/people/me/change-requests/:id/cancel` | Signed-in |
| POST | `/api/v1/people/me/avatar` | Signed-in |
| DELETE | `/api/v1/people/me/avatar` | Signed-in |
| GET | `/api/v1/people/change-requests` | HR |
| GET | `/api/v1/people/change-requests/:id` | Signed-in |
| GET | `/api/v1/people/change-requests/:id/proof` | Signed-in |
| GET | `/api/v1/people/:id/card` | Signed-in |
| GET | `/api/v1/people/:id/avatar` | Signed-in |
| POST | `/api/v1/people/:id/avatar` | HR |
| DELETE | `/api/v1/people/:id/avatar` | HR |
| GET | `/api/v1/people/:id/profile` | Signed-in |
| PUT | `/api/v1/people/:id/profile/:section` | HR |
| GET | `/api/v1/people/:id/history` | Signed-in |
| GET | `/api/v1/people/:id/career` | Signed-in |
| POST | `/api/v1/people/:id/career` | HR |
| POST | `/api/v1/people/:id/career/:eventId/cancel` | HR |

## insights

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/insights/filters` | Signed-in |
| GET | `/api/v1/insights` | Signed-in |
| GET | `/api/v1/insights/export/:dataset` | Signed-in |

## relations

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/relations/policy` | HR |
| PUT | `/api/v1/relations/policy` | HR |
| GET | `/api/v1/relations/grievances/committee` | Signed-in |
| PUT | `/api/v1/relations/grievances/committee` | HR |
| POST | `/api/v1/relations/grievances` | Signed-in |
| GET | `/api/v1/relations/grievances` | Signed-in |
| GET | `/api/v1/relations/grievances/:id` | Signed-in |
| GET | `/api/v1/relations/grievances/:id/files/:fileId` | Signed-in |
| POST | `/api/v1/relations/grievances/:id/notes` | Signed-in |
| PATCH | `/api/v1/relations/grievances/:id` | Signed-in |
| POST | `/api/v1/relations/grievances/:id/decision` | Signed-in |
| POST | `/api/v1/relations/grievances/:id/appeal` | Signed-in |
| POST | `/api/v1/relations/grievances/:id/appeal-decision` | HR |
| POST | `/api/v1/relations/grievances/:id/withdraw` | Signed-in |
| POST | `/api/v1/relations/grievances/:id/close` | Signed-in |
| GET | `/api/v1/relations/disciplinary/stages` | Signed-in |
| PUT | `/api/v1/relations/disciplinary/stages` | HR |
| GET | `/api/v1/relations/disciplinary` | HR |
| GET | `/api/v1/relations/disciplinary/mine` | Signed-in |
| GET | `/api/v1/relations/disciplinary/:id` | Signed-in |
| POST | `/api/v1/relations/disciplinary` | HR |
| POST | `/api/v1/relations/disciplinary/:id/show-cause` | HR |
| POST | `/api/v1/relations/disciplinary/:id/respond` | Signed-in |
| POST | `/api/v1/relations/disciplinary/:id/decide` | HR |
| POST | `/api/v1/relations/disciplinary/:id/letter` | HR |
| POST | `/api/v1/relations/disciplinary/:id/withdraw` | HR |
| GET | `/api/v1/relations/milestones` | HR |
| GET | `/api/v1/relations/separating` | HR |
| GET | `/api/v1/relations/employees/:id/contract` | HR |
| PUT | `/api/v1/relations/employees/:id/contract` | HR |
| GET | `/api/v1/relations/assignments` | Signed-in |
| POST | `/api/v1/relations/assignments` | HR |
| POST | `/api/v1/relations/assignments/:id/end` | HR |
| GET | `/api/v1/relations/kye/campaigns` | HR |
| POST | `/api/v1/relations/kye/campaigns` | HR |
| GET | `/api/v1/relations/kye/campaigns/:id` | HR |
| POST | `/api/v1/relations/kye/campaigns/:id/nudge` | HR |
| POST | `/api/v1/relations/kye/campaigns/:id/close` | HR |
| GET | `/api/v1/relations/kye/mine` | Signed-in |
| POST | `/api/v1/relations/kye/campaigns/:id/respond` | Signed-in |
| GET | `/api/v1/relations/completeness` | HR |
| GET | `/api/v1/relations/popups` | Signed-in |
| POST | `/api/v1/relations/popups/:id/dismiss` | Signed-in |
| GET | `/api/v1/relations/devices/mine` | Signed-in |
| POST | `/api/v1/relations/devices/register` | Signed-in |
| DELETE | `/api/v1/relations/devices/mine/:id` | Signed-in |
| GET | `/api/v1/relations/devices` | HR |
| POST | `/api/v1/relations/devices/:id/:action` | HR |
| GET | `/api/v1/relations/summary` | HR |

## attendance-tools

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/attendance-tools/policy` | Signed-in |
| PUT | `/api/v1/attendance-tools/policy` | Signed-in |
| GET | `/api/v1/attendance-tools/penalties` | ...module_roles.attendance, Manager |
| GET | `/api/v1/attendance-tools/penalties/me` | Signed-in |
| POST | `/api/v1/attendance-tools/penalties/apply` | Signed-in |
| POST | `/api/v1/attendance-tools/penalties/:id/reverse` | Signed-in |
| GET | `/api/v1/attendance-tools/issues` | ...module_roles.attendance, Manager |
| GET | `/api/v1/attendance-tools/issues/summary` | Signed-in |
| POST | `/api/v1/attendance-tools/issues/nudge` | ...module_roles.attendance, Manager |
| GET | `/api/v1/attendance-tools/upload/template` | Signed-in |
| POST | `/api/v1/attendance-tools/upload/check` | Signed-in |
| POST | `/api/v1/attendance-tools/upload/import` | Signed-in |
| GET | `/api/v1/attendance-tools/uploads` | Signed-in |
| GET | `/api/v1/attendance-tools/whos-out` | Signed-in |

## tasks

| Method | Path | Access |
|---|---|---|
| GET | `/api/v1/tasks/boards` | Signed-in |
| POST | `/api/v1/tasks/boards` | Signed-in |
| GET | `/api/v1/tasks/boards/:id` | Signed-in |
| PATCH | `/api/v1/tasks/boards/:id` | Signed-in |
| PUT | `/api/v1/tasks/boards/:id/members` | Signed-in |
| GET | `/api/v1/tasks/people` | Signed-in |
| POST | `/api/v1/tasks/boards/:id/tasks` | Signed-in |
| GET | `/api/v1/tasks/my/todo` | Signed-in |
| GET | `/api/v1/tasks/:id` | Signed-in |
| PATCH | `/api/v1/tasks/:id` | Signed-in |
| DELETE | `/api/v1/tasks/:id` | Signed-in |
| POST | `/api/v1/tasks/:id/checklist` | Signed-in |
| PATCH | `/api/v1/tasks/checklist/:itemId` | Signed-in |
| DELETE | `/api/v1/tasks/checklist/:itemId` | Signed-in |
| POST | `/api/v1/tasks/:id/comments` | Signed-in |
| POST | `/api/v1/tasks/:id/attachments` | Signed-in |
| GET | `/api/v1/tasks/attachments/:attId/file` | Signed-in |
| DELETE | `/api/v1/tasks/attachments/:attId` | Signed-in |
| GET | `/api/v1/tasks/reports/boards/:id` | Signed-in |

## gateway (port 8080)

The device gateway receives punches from ZKTeco (ADMS/push) and Hikvision devices and forwards them to the API. See `apps/gateway`.
