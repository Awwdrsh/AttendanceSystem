import { api, del, download, get, patch, post, put, type Query } from './client';
import { deviceHeaders } from '../lib/deviceToken';
import type * as T from './types';

export const auth = {
  login: (email: string, password: string) => post<{ user: T.AuthUser; token: string }>('/auth/login', { email, password }),
  logout: () => post<{ ok: boolean }>('/auth/logout'),
  me: () => get<{ user: T.AuthUser }>('/auth/me'),
  changePassword: (currentPassword: string, newPassword: string) => post<{ ok: boolean }>('/auth/change-password', { currentPassword, newPassword }),
  users: () => get<{ users: T.AppUserRow[] }>('/auth/users'),
  createUser: (b: { email: string; name: string; password: string; roles: T.Role[]; employeeId?: string }) => post<{ user: T.AuthUser }>('/auth/users', b),
  updateUser: (id: string, b: { roles?: T.Role[]; status?: 'active' | 'disabled'; employeeId?: string | null }) => patch<{ user: T.AppUserRow }>(`/auth/users/${id}`, b),
};

export const employees = {
  list: (q?: Query) => get<{ employees: T.Employee[] }>('/employees', q),
  get: (id: string) => get<{ employee: T.Employee }>(`/employees/${id}`),
  sensitive: (id: string) => get<{ pan: string | null; bankAccount: string | null }>(`/employees/${id}/sensitive`),
  create: (b: Record<string, unknown>) => post<{ id: string }>('/employees', b),
  update: (id: string, b: Record<string, unknown>) => patch<{ employee: T.Employee }>(`/employees/${id}`, b),
  importCsv: (csv: string, dryRun: boolean) => post<{ imported: number; valid?: number; errors: Array<{ row: number; message: string }> }>(`/employees/import${dryRun ? '?dryRun=true' : ''}`, { csv }),
  assignments: (id: string) => get<{ assignments: Array<Record<string, unknown>> }>(`/employees/${id}/assignments`),
  audit: (id: string) => get<{ events: T.AuditEvent[] }>(`/employees/${id}/audit`),
};

export const attendance = {
  days: (employeeId: string, from: string, to: string) => get<{ days: T.AttendanceDayRow[] }>('/attendance/days', { employeeId, from, to }),
  punches: (employeeId: string, from: string, to: string) => get<{ punches: T.Punch[] }>('/attendance/punches', { employeeId, from, to }),
  recompute: (employeeId: string, localDate: string) => post<{ day: T.EngineDay; version: number; changed: boolean; locked: boolean }>('/attendance/recompute', { employeeId, localDate }),
  recomputeRange: (employeeIds: string[], from: string, to: string) => post<{ queued: number }>('/attendance/recompute-range', { employeeIds, from, to }),
  exceptions: (from: string, to: string, status?: string) => get<{ exceptions: T.ExceptionRow[] }>('/attendance/exceptions', { from, to, status }),
  today: (date: string) => get<{ date: string; rows: T.TeamTodayRow[] }>('/attendance/today', { date }),
};

export const punch = {
  create: (b: Record<string, unknown>) => api<T.PunchResult>('POST', '/punch', b, { headers: deviceHeaders() }),
  today: () => get<T.TodayCard>('/punch/today'),
  remote: (q: { from: string; to: string; employeeId?: string }) => get<{ punches: T.RemotePunch[] }>('/punch/remote', q),
  photoUrl: (id: string) => `/api/v1/punch/photos/${id}`,
  manual: (b: { employeeId: string; localDate: string; time: string; direction: 'in' | 'out'; reason: string }) => post<{ duplicate: boolean; day?: T.EngineDay }>('/punch/manual', b),
};

export const leave = {
  types: () => get<{ types: T.LeaveType[] }>('/leave/types'),
  createType: (b: Record<string, unknown>) => post<{ id: string }>('/leave/types', b),
  updateType: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/leave/types/${id}`, b),
  balances: (employeeId?: string) => get<{ employeeId: string; balances: T.LeaveBalance[] }>('/leave/balances', { employeeId }),
  preview: (q: Query) => get<T.LeavePreview>('/leave/preview', q),
  request: (b: Record<string, unknown>) => post<{ id: string; days: number; explanation: string; teamOverlap: string[] }>('/leave/requests', b),
  requests: (q?: Query) => get<{ requests: T.LeaveRequest[] }>('/leave/requests', q),
  cancel: (id: string, reason?: string) => post<{ ok: boolean }>(`/leave/requests/${id}/cancel`, { reason }),
  adjust: (b: Record<string, unknown>) => post<{ id: string; balance: number }>('/leave/adjust', b),
  ledger: (employeeId?: string) => get<{ ledger: Array<Record<string, unknown>> }>('/leave/ledger', { employeeId }),
  teamCalendar: (from: string, to: string) => get<{ days: T.TeamCalendarDay[] }>('/leave/team-calendar', { from, to }),
};

export const workflow = {
  inbox: () => get<{ items: T.InboxItem[] }>('/workflow/inbox'),
  detail: (id: string) => get<T.WorkflowDetail>(`/workflow/${id}`),
  approve: (id: string, b?: { reason?: string; approvedMin?: number }) => post<{ state: string }>(`/workflow/${id}/approve`, b ?? {}),
  reject: (id: string, reason: string) => post<{ state: string }>(`/workflow/${id}/reject`, { reason }),
  bulkApprove: (ids: string[]) => post<{ results: Array<{ id: string; state?: string; error?: string }> }>('/workflow/bulk-approve', { ids }),
};

export const regularisation = {
  create: (b: Record<string, unknown>) => post<{ id: string; status: string }>('/regularisation', b),
  list: (q?: Query) => get<{ requests: T.RegularisationRequest[] }>('/regularisation', q),
  cancel: (id: string) => post<{ ok: boolean }>(`/regularisation/${id}/cancel`),
};

export const ot = {
  request: (b: Record<string, unknown>) => post<{ id: string }>('/ot/requests', b),
  requests: (q?: Query) => get<{ requests: T.OtRequest[] }>('/ot/requests', q),
  cancelRequest: (id: string) => post<{ ok: boolean }>(`/ot/requests/${id}/cancel`),
  slips: (q?: Query) => get<{ slips: T.OtSlip[] }>('/ot/slips', q),
  approveSlip: (id: string, approvedMin?: number) => post<{ ok: boolean }>(`/ot/slips/${id}/approve`, approvedMin !== undefined ? { approvedMin } : {}),
  rejectSlip: (id: string, reason: string) => post<{ ok: boolean }>(`/ot/slips/${id}/reject`, { reason }),
  compOff: (id: string) => post<{ ok: boolean; days: number; expiresOn: string }>(`/ot/slips/${id}/comp-off`),
  registerCsv: (from: string, to: string) => download(`/ot/register?from=${from}&to=${to}&format=csv`, `ot-register-${from}_${to}.csv`),
};

export const shifts = {
  list: () => get<{ shifts: T.Shift[] }>('/shifts'),
  create: (b: Record<string, unknown>) => post<{ id: string }>('/shifts', b),
  update: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/shifts/${id}`, b),
  remove: (id: string) => del<{ ok: boolean }>(`/shifts/${id}`),
  assign: (employeeId: string, shiftId: string, from: string) => post<{ ok: boolean }>('/shifts/assign', { employeeId, shiftId, from }),
  holidays: (q?: Query) => get<{ holidays: T.Holiday[] }>('/shifts/holidays', q),
  createHoliday: (b: Record<string, unknown>) => post<{ id: string }>('/shifts/holidays', b),
  updateHoliday: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/shifts/holidays/${id}`, b),
  deleteHoliday: (id: string) => del<{ ok: boolean }>(`/shifts/holidays/${id}`),
  importHolidays: (bsYear: number) => post<{ inserted: number; total: number }>('/shifts/holidays/import-default', { bsYear }),
  locations: () => get<{ locations: T.Location[] }>('/shifts/locations'),
  createLocation: (b: Record<string, unknown>) => post<{ id: string }>('/shifts/locations', b),
  updateLocation: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/shifts/locations/${id}`, b),
  deleteLocation: (id: string) => del<{ ok: boolean }>(`/shifts/locations/${id}`),
  calendar: (from: string, to: string, employeeId?: string) => get<{ days: T.CalendarDay[] }>('/shifts/calendar', { from, to, employeeId }),
  roster: (from: string, to: string, branchId?: string) => get<T.RosterData>('/shifts/roster', { from, to, branchId }),
  saveRoster: (rows: Array<{ employeeId: string; localDate: string; shiftId: string | null }>) => put<{ upserted: number }>('/shifts/roster', { rows }),
  clearRoster: (employeeId: string, localDate: string) => del<{ deleted: number }>('/shifts/roster', { employeeId, localDate }),
  copyWeek: (weekStart: string) => post<{ copied: number }>('/shifts/roster/copy-week', { weekStart }),
  publish: (from: string, to: string) => post<{ notified: number }>('/shifts/roster/publish', { from, to }),
};

export const devices = {
  list: () => get<{ devices: T.Device[] }>('/devices'),
  activate: (id: string, b: { branchId?: string; name?: string }) => post<{ id: string }>(`/devices/${id}/activate`, b),
  disable: (id: string) => post<{ id: string }>(`/devices/${id}/disable`),
  mappings: (id: string) => get<{ mappings: T.DeviceMapping[] }>(`/devices/${id}/mappings`),
  map: (id: string, b: { pin: string; employeeId: string; from: string; syncToDevice?: boolean; replace?: boolean }) => post<{ ok: boolean; reattributed: number; dates: string[] }>(`/devices/${id}/map`, b),
  unmapped: () => get<{ punches: T.UnmappedPunch[] }>('/devices/punches/unmapped'),
  commands: (id: string) => get<{ commands: T.DeviceCommand[] }>(`/devices/${id}/commands`),
  command: (id: string, command: string) => post<{ id: number }>(`/devices/${id}/commands`, { command }),
  syncUsers: (id: string) => post<{ queued: number }>(`/devices/${id}/sync-users`),
  issueKey: (id: string) => post<{ key: string }>(`/devices/${id}/key`),
};

export const notifications = {
  list: (q?: Query) => get<{ notifications: T.NotificationRow[]; unread: number }>('/notifications', q),
  read: (id: string) => post<{ id: string }>(`/notifications/${id}/read`),
  readAll: () => post<{ marked: number }>('/notifications/read-all'),
};

export const dashboard = {
  executive: () => get<T.ExecutiveDashboard>('/dashboard/executive'),
  me: () => get<T.MyDashboard>('/dashboard/me'),
};

export const settings = {
  company: () => get<T.CompanyInfo>('/settings/company'),
  all: () => get<{ settings: T.Setting[]; locked: Record<string, unknown> }>('/settings'),
  put: (key: string, value: Record<string, unknown>) => put<{ key: string; value: Record<string, unknown> }>(`/settings/${key}`, value),
  previewAttendance: (policy: Record<string, unknown>, from: string, to: string) => post<{ summary: string; proposed: Record<string, number>; current: Record<string, number>; affectedEmployees: number; employees: number }>('/settings/preview/attendance', { policy, from, to }),
  branches: () => get<{ branches: T.Branch[] }>('/settings/branches'),
  createBranch: (b: { code: string; nameEn: string; nameNe?: string }) => post<{ branch: T.Branch }>('/settings/branches', b),
  orgUnits: () => get<{ orgUnits: T.OrgUnit[] }>('/settings/org-units'),
  createOrgUnit: (b: Record<string, unknown>) => post<{ orgUnit: T.OrgUnit }>('/settings/org-units', b),
};

export const reports = {
  json: <R = Record<string, unknown>>(name: string, q: Query) => get<{ rows?: R[]; [k: string]: unknown }>(`/reports/${name}`, q),
  csv: (name: string, q: Query, filename: string) => {
    const p = new URLSearchParams(); for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null) p.set(k, String(v)); p.set('format', 'csv');
    return download(`/reports/${name}?${p.toString()}`, filename);
  },
};

export const payroll = {
  rules: (q?: Query) => get<{ rules: T.RuleVersion[] }>('/payroll/rules', q),
  approveRule: (id: string) => post<{ ok: boolean }>(`/payroll/rules/${id}/approve`),
  createRule: (b: Record<string, unknown>) => post<{ id: string }>('/payroll/rules', b),
  components: () => get<{ components: T.SalaryComponent[] }>('/payroll/components'),
  createComponent: (b: Record<string, unknown>) => post<{ code: string }>('/payroll/components', b),
  updateComponent: (code: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/payroll/components/${code}`, b),
  structures: () => get<{ structures: T.SalaryStructure[] }>('/payroll/structures'),
  createStructure: (b: { code: string; nameEn: string; components: Array<{ code: string }> }) => post<{ id: string; versionId: string }>('/payroll/structures', b),
  addVersion: (id: string, components: Array<{ code: string }>) => post<{ id: string; version: number }>(`/payroll/structures/${id}/versions`, { components }),
  assignments: (employeeId?: string) => get<{ assignments: T.SalaryAssignment[] }>('/payroll/assignments', { employeeId }),
  assign: (b: { employeeId: string; structureVersionId: string; from: string; baseAmounts: Record<string, string> }) => post<{ ok: boolean }>('/payroll/assignments', b),
  declaration: (fiscalYearStart: number, employeeId?: string) => get<{ declaration: T.TaxDeclaration | null; pending?: (T.TaxDeclaration & { proof_file_ids?: string[]; note?: string | null }) | null }>('/payroll/declarations', { fiscalYearStart, employeeId }),
  saveDeclaration: (b: Record<string, unknown>) => put<{ ok: boolean }>('/payroll/declarations', b),
  verifyDeclaration: (employeeId: string, fiscalYearStart: number, decision: 'verified' | 'rejected', reason?: string) => post<{ ok: boolean }>(`/payroll/declarations/${employeeId}/verify`, { fiscalYearStart, decision, ...(reason ? { reason } : {}) }),
  loans: (employeeId?: string) => get<{ loans: T.Loan[] }>('/payroll/loans', { employeeId }),
  createLoan: (b: Record<string, unknown>) => post<{ id: string; installments: number }>('/payroll/loans', b),
  periods: () => get<{ periods: T.PayrollPeriod[] }>('/payroll/periods'),
  createPeriod: (bsYear: number, bsMonth: number) => post<{ period: T.PayrollPeriod }>('/payroll/periods', { bsYear, bsMonth }),
  runs: () => get<{ runs: T.PayrollRun[] }>('/payroll/runs'),
  createRun: (b: Record<string, unknown>) => post<{ id: string }>('/payroll/runs', b),
  bonusRun: (b: { periodId: string; fiscalYearStart: number; netProfit: string }) => post<{ id: string; pool: string }>('/payroll/runs/bonus', b),
  run: (id: string) => get<T.RunDetail>(`/payroll/runs/${id}`),
  preflight: (id: string) => post<T.Preflight>(`/payroll/runs/${id}/preflight`),
  lockAttendance: (id: string) => post<{ locked: number }>(`/payroll/runs/${id}/lock-attendance`),
  inputs: (id: string) => get<{ inputs: Array<{ id: string; employee_id: string; emp_code: string; component_code: string; amount: string; note: string | null }> }>(`/payroll/runs/${id}/inputs`),
  addInputs: (id: string, inputs: Array<{ employeeId: string; componentCode: string; amount: string; note?: string }>) => post<{ added: number }>(`/payroll/runs/${id}/inputs`, { inputs }),
  compute: (id: string, overrideReason?: string) => post<{ payslips: number; totals: Record<string, string>; variance: T.Variance }>(`/payroll/runs/${id}/compute`, overrideReason ? { overrideReason } : {}),
  review: (id: string, notes?: string) => post<{ ok: boolean }>(`/payroll/runs/${id}/review`, { notes }),
  approve: (id: string) => post<{ ok: boolean; approved: boolean; signature: string }>(`/payroll/runs/${id}/approve`),
  lock: (id: string) => post<{ attendanceLocked: number }>(`/payroll/runs/${id}/lock`),
  publish: (id: string) => post<{ published: number }>(`/payroll/runs/${id}/publish`),
  ssfSheet: (id: string) => download(`/payroll/runs/${id}/ssf-sheet.csv`, `ssf-${id}.csv`),
  salarySheet: (id: string) => get<T.SalarySheet>(`/payroll/runs/${id}/salary-sheet`),
  salarySheetCsv: (id: string, filename: string) => download(`/payroll/runs/${id}/salary-sheet?format=csv`, filename),
  payslips: (q?: Query) => get<{ payslips: T.PayslipSummary[] }>('/payroll/payslips', q),
  payslip: (id: string) => get<T.PayslipDetail>(`/payroll/payslips/${id}`),
};

export const biometrics = {
  transparency: () => get<T.Transparency>('/biometrics/transparency'),
  consents: (employeeId: string) => get<{ consents: T.Consent[] }>('/biometrics/consent', { employeeId }),
  consent: (b: { employeeId: string; modality: string; noticeVersion: string; purpose?: string }) => post<{ consent: { id: string } }>('/biometrics/consent', b),
  revoke: (id: string) => post<{ ok: boolean; templatesShredded: number }>(`/biometrics/consent/${id}/revoke`),
  templates: (employeeId: string) => get<{ templates: T.TemplateMeta[] }>('/biometrics/templates', { employeeId }),
  shred: (id: string) => post<{ ok: boolean }>(`/biometrics/templates/${id}/shred`),
  shredAll: (employeeId: string) => post<{ ok: boolean; shredded: number }>(`/biometrics/employees/${employeeId}/shred-all`),
  dsar: (employeeId: string) => download(`/biometrics/dsar?employeeId=${employeeId}`, `my-data-${employeeId}.json`),
};
