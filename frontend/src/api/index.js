import { api, del, download, get, patch, post, put } from './client';
import { deviceHeaders } from '../lib/deviceToken';
export const auth = {
  login: (email, password) => post('/auth/login', { email, password }),
  logout: () => post('/auth/logout'),
  me: () => get('/auth/me'),
  changePassword: (currentPassword, newPassword) => post('/auth/change-password', { currentPassword, newPassword }),
  users: () => get('/auth/users'),
  createUser: (b) => post('/auth/users', b),
  updateUser: (id, b) => patch(`/auth/users/${id}`, b),
};

export const employees = {
  list: (q) => get('/employees', q),
  get: (id) => get(`/employees/${id}`),
  sensitive: (id) => get(`/employees/${id}/sensitive`),
  create: (b) => post('/employees', b),
  update: (id, b) => patch(`/employees/${id}`, b),
  importCsv: (csv, dryRun) => post(`/employees/import${dryRun ? '?dryRun=true' : ''}`, { csv }),
  assignments: (id) => get(`/employees/${id}/assignments`),
  audit: (id) => get(`/employees/${id}/audit`),
};

export const attendance = {
  days: (employeeId, from, to) => get('/attendance/days', { employeeId, from, to }),
  punches: (employeeId, from, to) => get('/attendance/punches', { employeeId, from, to }),
  recompute: (employeeId, localDate) => post('/attendance/recompute', { employeeId, localDate }),
  recomputeRange: (employeeIds, from, to) => post('/attendance/recompute-range', { employeeIds, from, to }),
  exceptions: (from, to, status) => get('/attendance/exceptions', { from, to, status }),
  today: (date) => get('/attendance/today', { date }),
};

export const punch = {
  create: (b) => api('POST', '/punch', b, { headers: deviceHeaders() }),
  today: () => get('/punch/today'),
  remote: (q) => get('/punch/remote', q),
  photoUrl: (id) => `/api/v1/punch/photos/${id}`,
  manual: (b) => post('/punch/manual', b),
};

export const leave = {
  types: () => get('/leave/types'),
  createType: (b) => post('/leave/types', b),
  updateType: (id, b) => patch(`/leave/types/${id}`, b),
  balances: (employeeId) => get('/leave/balances', { employeeId }),
  preview: (q) => get('/leave/preview', q),
  request: (b) => post('/leave/requests', b),
  requests: (q) => get('/leave/requests', q),
  cancel: (id, reason) => post(`/leave/requests/${id}/cancel`, { reason }),
  adjust: (b) => post('/leave/adjust', b),
  ledger: (employeeId) => get('/leave/ledger', { employeeId }),
  teamCalendar: (from, to) => get('/leave/team-calendar', { from, to }),
};

export const workflow = {
  inbox: () => get('/workflow/inbox'),
  detail: (id) => get(`/workflow/${id}`),
  approve: (id, b) => post(`/workflow/${id}/approve`, b ?? {}),
  reject: (id, reason) => post(`/workflow/${id}/reject`, { reason }),
  bulkApprove: (ids) => post('/workflow/bulk-approve', { ids }),
};

export const regularisation = {
  create: (b) => post('/regularisation', b),
  list: (q) => get('/regularisation', q),
  cancel: (id) => post(`/regularisation/${id}/cancel`),
};

export const ot = {
  request: (b) => post('/ot/requests', b),
  requests: (q) => get('/ot/requests', q),
  cancelRequest: (id) => post(`/ot/requests/${id}/cancel`),
  slips: (q) => get('/ot/slips', q),
  approveSlip: (id, approvedMin) => post(`/ot/slips/${id}/approve`, approvedMin !== undefined ? { approvedMin } : {}),
  rejectSlip: (id, reason) => post(`/ot/slips/${id}/reject`, { reason }),
  compOff: (id) => post(`/ot/slips/${id}/comp-off`),
  registerCsv: (from, to) => download(`/ot/register?from=${from}&to=${to}&format=csv`, `ot-register-${from}_${to}.csv`),
};

export const shifts = {
  list: () => get('/shifts'),
  create: (b) => post('/shifts', b),
  update: (id, b) => patch(`/shifts/${id}`, b),
  remove: (id) => del(`/shifts/${id}`),
  assign: (employeeId, shiftId, from) => post('/shifts/assign', { employeeId, shiftId, from }),
  holidays: (q) => get('/shifts/holidays', q),
  createHoliday: (b) => post('/shifts/holidays', b),
  updateHoliday: (id, b) => patch(`/shifts/holidays/${id}`, b),
  deleteHoliday: (id) => del(`/shifts/holidays/${id}`),
  importHolidays: (bsYear) => post('/shifts/holidays/import-default', { bsYear }),
  locations: () => get('/shifts/locations'),
  createLocation: (b) => post('/shifts/locations', b),
  updateLocation: (id, b) => patch(`/shifts/locations/${id}`, b),
  deleteLocation: (id) => del(`/shifts/locations/${id}`),
  calendar: (from, to, employeeId) => get('/shifts/calendar', { from, to, employeeId }),
  roster: (from, to, branchId) => get('/shifts/roster', { from, to, branchId }),
  saveRoster: (rows) => put('/shifts/roster', { rows }),
  clearRoster: (employeeId, localDate) => del('/shifts/roster', { employeeId, localDate }),
  copyWeek: (weekStart) => post('/shifts/roster/copy-week', { weekStart }),
  publish: (from, to) => post('/shifts/roster/publish', { from, to }),
};

export const devices = {
  list: () => get('/devices'),
  activate: (id, b) => post(`/devices/${id}/activate`, b),
  disable: (id) => post(`/devices/${id}/disable`),
  mappings: (id) => get(`/devices/${id}/mappings`),
  map: (id, b) => post(`/devices/${id}/map`, b),
  unmapped: () => get('/devices/punches/unmapped'),
  commands: (id) => get(`/devices/${id}/commands`),
  command: (id, command) => post(`/devices/${id}/commands`, { command }),
  syncUsers: (id) => post(`/devices/${id}/sync-users`),
  issueKey: (id) => post(`/devices/${id}/key`),
};

export const notifications = {
  list: (q) => get('/notifications', q),
  read: (id) => post(`/notifications/${id}/read`),
  readAll: () => post('/notifications/read-all'),
};

export const dashboard = {
  executive: () => get('/dashboard/executive'),
  me: () => get('/dashboard/me'),
};

export const settings = {
  company: () => get('/settings/company'),
  all: () => get('/settings'),
  put: (key, value) => put(`/settings/${key}`, value),
  previewAttendance: (policy, from, to) => post('/settings/preview/attendance', { policy, from, to }),
  branches: () => get('/settings/branches'),
  createBranch: (b) => post('/settings/branches', b),
  orgUnits: () => get('/settings/org-units'),
  createOrgUnit: (b) => post('/settings/org-units', b),
};

export const reports = {
  json: (name, q) => get(`/reports/${name}`, q),
  csv: (name, q, filename) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null) p.set(k, String(v));
    p.set('format', 'csv');
    return download(`/reports/${name}?${p.toString()}`, filename);
  },
};

export const payroll = {
  rules: (q) => get('/payroll/rules', q),
  approveRule: (id) => post(`/payroll/rules/${id}/approve`),
  createRule: (b) => post('/payroll/rules', b),
  components: () => get('/payroll/components'),
  createComponent: (b) => post('/payroll/components', b),
  updateComponent: (code, b) => patch(`/payroll/components/${code}`, b),
  structures: () => get('/payroll/structures'),
  createStructure: (b) => post('/payroll/structures', b),
  addVersion: (id, components) => post(`/payroll/structures/${id}/versions`, { components }),
  assignments: (employeeId) => get('/payroll/assignments', { employeeId }),
  assign: (b) => post('/payroll/assignments', b),
  declaration: (fiscalYearStart, employeeId) => get('/payroll/declarations', { fiscalYearStart, employeeId }),
  saveDeclaration: (b) => put('/payroll/declarations', b),
  verifyDeclaration: (employeeId, fiscalYearStart, decision, reason) =>
    post(`/payroll/declarations/${employeeId}/verify`, { fiscalYearStart, decision, ...(reason ? { reason } : {}) }),
  loans: (employeeId) => get('/payroll/loans', { employeeId }),
  createLoan: (b) => post('/payroll/loans', b),
  periods: () => get('/payroll/periods'),
  createPeriod: (bsYear, bsMonth) => post('/payroll/periods', { bsYear, bsMonth }),
  runs: () => get('/payroll/runs'),
  createRun: (b) => post('/payroll/runs', b),
  bonusRun: (b) => post('/payroll/runs/bonus', b),
  run: (id) => get(`/payroll/runs/${id}`),
  preflight: (id) => post(`/payroll/runs/${id}/preflight`),
  lockAttendance: (id) => post(`/payroll/runs/${id}/lock-attendance`),
  inputs: (id) => get(`/payroll/runs/${id}/inputs`),
  addInputs: (id, inputs) => post(`/payroll/runs/${id}/inputs`, { inputs }),
  compute: (id, overrideReason) => post(`/payroll/runs/${id}/compute`, overrideReason ? { overrideReason } : {}),
  review: (id, notes) => post(`/payroll/runs/${id}/review`, { notes }),
  approve: (id) => post(`/payroll/runs/${id}/approve`),
  lock: (id) => post(`/payroll/runs/${id}/lock`),
  publish: (id) => post(`/payroll/runs/${id}/publish`),
  ssfSheet: (id) => download(`/payroll/runs/${id}/ssf-sheet.csv`, `ssf-${id}.csv`),
  salarySheet: (id) => get(`/payroll/runs/${id}/salary-sheet`),
  salarySheetCsv: (id, filename) => download(`/payroll/runs/${id}/salary-sheet?format=csv`, filename),
  payslips: (q) => get('/payroll/payslips', q),
  payslip: (id) => get(`/payroll/payslips/${id}`),
};

export const biometrics = {
  transparency: () => get('/biometrics/transparency'),
  consents: (employeeId) => get('/biometrics/consent', { employeeId }),
  consent: (b) => post('/biometrics/consent', b),
  revoke: (id) => post(`/biometrics/consent/${id}/revoke`),
  templates: (employeeId) => get('/biometrics/templates', { employeeId }),
  shred: (id) => post(`/biometrics/templates/${id}/shred`),
  shredAll: (employeeId) => post(`/biometrics/employees/${employeeId}/shred-all`),
  dsar: (employeeId) => download(`/biometrics/dsar?employeeId=${employeeId}`, `my-data-${employeeId}.json`),
};
