# API reference (generated from route files)

All routes are under `/api/v1` and need a session (cookie `hajiri_session` or `Authorization: Bearer <token>`) except `/auth/login`. Roles in brackets where a route is restricted; otherwise self-or-HR scoping applies.

## attendance  (`/attendance`)

- `12: /attendancer.post('/recompute',`
- `19: /attendancer.post('/recompute-range',`
- `29: /attendancer.get('/days',`
- `36: /attendancer.get('/punches',`
- `48: /attendancer.get('/exceptions',`
- `63: /attendancer.get('/today',`

## auth  (`/auth`)

- `25: /authr.post('/login',`
- `33: /authr.post('/logout',`
- `39: /authr.get('/me',`
- `41: /authr.post('/change-password',`
- `50: /authr.get('/users',`
- `57: /authr.post('/users',`
- `71: /authr.patch('/users/:id',`

## biometrics  (`/biometrics`)

- `47: /biometricsr.get('/transparency',`
- `67: /biometricsr.post('/consent',`
- `80: /biometricsr.get('/consent',`
- `90: /biometricsr.post('/consent/:id/revoke',`
- `110: /biometricsr.get('/templates',`
- `121: /biometricsr.get('/templates/unmapped',`
- `129: /biometricsr.post('/templates/:id/shred',`
- `140: /biometricsr.post('/employees/:id/shred-all',`
- `150: /biometricsr.get('/dsar',`
- `177: /biometricsr.post('/retention/run',`
- `188: /biometricsr.get('/stats',`

## dashboard  (`/dashboard`)

- `13: /dashboardr.get('/executive',`
- `66: /dashboardr.get('/me',`

## devices  (`/devices`)

- `20: /devicesr.get('/',`
- `32: /devicesr.post('/:id/activate',`
- `43: /devicesr.post('/:id/disable',`
- `51: /devicesr.get('/:id/mappings',`
- `63: /devicesr.post('/:id/map',`
- `103: /devicesr.get('/punches/unmapped',`
- `114: /devicesr.post('/:id/commands',`
- `124: /devicesr.get('/:id/commands',`
- `133: /devicesr.post('/:id/sync-users',`

## employees  (`/employees`)

- `76: /employeesr.get('/',`
- `90: /employeesr.get('/:id',`
- `102: /employeesr.get('/:id/sensitive',`
- `115: /employeesr.post('/',`
- `143: /employeesr.patch('/:id',`
- `197: /employeesr.post('/import',`
- `249: /employeesr.get('/:id/assignments',`
- `257: /employeesr.get('/:id/audit',`

## leave  (`/leave`)

- `79: /leaver.get('/types',`
- `83: /leaver.post('/types',`
- `94: /leaver.patch('/types/:id',`
- `117: /leaver.get('/balances',`
- `128: /leaver.get('/preview',`
- `144: /leaver.post('/requests',`
- `211: /leaver.get('/requests',`
- `232: /leaver.post('/requests/:id/cancel',`
- `261: /leaver.post('/adjust',`
- `273: /leaver.get('/ledger',`
- `288: /leaver.get('/team-calendar',`

## notifications  (`/notifications`)

- `9: /notificationsr.get('/',`
- `19: /notificationsr.post('/read-all',`
- `24: /notificationsr.post('/:id/read',`

## ot  (`/ot`)

- `44: /otr.post('/requests',`
- `84: /otr.get('/requests',`
- `100: /otr.post('/requests/:id/cancel',`
- `113: /otr.get('/slips',`
- `139: /otr.post('/slips/:id/approve',`
- `149: /otr.post('/slips/:id/reject',`
- `159: /otr.post('/slips/:id/comp-off',`
- `177: /otr.get('/register',`

## payroll  (`/payroll`)

- `24: /payrollr.get('/rules',`
- `33: /payrollr.post('/rules',`
- `43: /payrollr.post('/rules/:id/approve',`
- `52: /payrollr.get('/components',`
- `68: /payrollr.post('/components',`
- `78: /payrollr.patch('/components/:code',`
- `94: /payrollr.get('/structures',`
- `100: /payrollr.post('/structures',`
- `112: /payrollr.post('/structures/:id/versions',`
- `123: /payrollr.get('/assignments',`
- `131: /payrollr.post('/assignments',`
- `153: /payrollr.get('/declarations',`
- `162: /payrollr.put('/declarations',`
- `178: /payrollr.post('/declarations/:employeeId/verify',`
- `189: /payrollr.get('/loans',`
- `196: /payrollr.post('/loans',`
- `209: /payrollr.get('/periods',`
- `213: /payrollr.post('/periods',`
- `223: /payrollr.get('/runs',`
- `229: /payrollr.post('/runs',`
- `257: /payrollr.post('/runs/bonus',`
- `267: /payrollr.get('/runs/:id',`
- `276: /payrollr.post('/runs/:id/preflight',`
- `282: /payrollr.post('/runs/:id/lock-attendance',`
- `290: /payrollr.post('/runs/:id/inputs',`
- `315: /payrollr.get('/runs/:id/inputs',`
- `320: /payrollr.post('/runs/:id/compute',`
- `325: /payrollr.post('/runs/:id/review',`
- `330: /payrollr.post('/runs/:id/approve',`
- `334: /payrollr.post('/runs/:id/lock',`
- `335: /payrollr.post('/runs/:id/disburse',`
- `336: /payrollr.post('/runs/:id/file',`
- `337: /payrollr.post('/runs/:id/publish',`
- `340: /payrollr.post('/runs/:id/bank-file',`
- `361: /payrollr.get('/runs/:id/bank-files/:fileId',`
- `369: /payrollr.get('/runs/:id/ssf-sheet.csv',`
- `374: /payrollr.get('/runs/:id/etds.csv',`
- `381: /payrollr.get('/runs/:id/journal.:fmt',`
- `393: /payrollr.get('/payslips',`
- `422: /payrollr.get('/payslips/:id',`
- `427: /payrollr.get('/payslips/:id/html',`

## punch  (`/punch`)

- `34: /punchr.post('/',`
- `93: /punchr.get('/today',`
- `106: /punchr.post('/manual',`

## regularisation  (`/regularisation`)

- `73: /regularisationr.post('/',`
- `138: /regularisationr.get('/',`
- `155: /regularisationr.post('/:id/cancel',`

## reports  (`/reports`)

- `17: /reportsr.get('/daily',`
- `32: /reportsr.get('/register',`
- `57: /reportsr.get('/ot',`
- `69: /reportsr.get('/late-early',`
- `83: /reportsr.get('/leave-balances',`
- `103: /reportsr.get('/headcount',`
- `116: /reportsr.get('/exceptions-summary',`
- `129: /reportsr.get('/devices',`
- `141: /reportsr.get('/punch-audit',`

## settings  (`/settings`)

- `18: /settingsr.get('/company',`
- `31: /settingsr.get('/',`
- `43: /settingsr.put('/:key',`
- `62: /settingsr.post('/preview/attendance',`
- `107: /settingsr.get('/branches',`
- `111: /settingsr.post('/branches',`
- `117: /settingsr.patch('/branches/:id',`
- `128: /settingsr.get('/org-units',`
- `132: /settingsr.post('/org-units',`
- `139: /settingsr.patch('/org-units/:id',`

## shifts  (`/shifts`)

- `80: /shiftsr.get('/holidays',`
- `95: /shiftsr.post('/holidays',`
- `110: /shiftsr.patch('/holidays/:id',`
- `127: /shiftsr.delete('/holidays/:id',`
- `138: /shiftsr.post('/holidays/import-default',`
- `161: /shiftsr.get('/locations',`
- `166: /shiftsr.post('/locations',`
- `173: /shiftsr.patch('/locations/:id',`
- `182: /shiftsr.delete('/locations/:id',`
- `191: /shiftsr.get('/calendar',`
- `211: /shiftsr.get('/roster',`
- `230: /shiftsr.put('/roster',`
- `262: /shiftsr.delete('/roster',`
- `271: /shiftsr.post('/roster/copy-week',`
- `295: /shiftsr.post('/roster/publish',`
- `310: /shiftsr.post('/assign',`
- `329: /shiftsr.get('/',`
- `334: /shiftsr.post('/',`
- `346: /shiftsr.get('/:id',`
- `353: /shiftsr.patch('/:id',`
- `377: /shiftsr.delete('/:id',`

## workflow  (`/workflow`)

- `11: /workflowr.get('/inbox',`
- `15: /workflowr.get('/:id',`
- `39: /workflowr.post('/:id/approve',`
- `40: /workflowr.post('/:id/reject',`
- `42: /workflowr.post('/bulk-approve',`

