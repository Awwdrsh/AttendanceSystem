export type Role = 'admin' | 'hr' | 'finance' | 'manager' | 'employee';

export interface AuthUser { id: string; companyId: string | null; employeeId: string | null; email: string; name: string; roles: Role[]; mustChangePassword: boolean }

export type AttendanceStatus = 'OffShift' | 'Holiday' | 'WeeklyOff' | 'OnLeave' | 'Present' | 'HalfDay' | 'HalfDayLeave' | 'Absent' | 'Incomplete' | 'WFH';

export interface Flag { kind: string; minutes?: number; workedMin?: number; rawMinutes?: number; punchId?: string }

export interface AttendanceDayRow {
  id: string; employee_id: string; local_date: string; bs_date: string | null; version: number; status: AttendanceStatus; shift_id: string | null;
  first_in: string | null; last_out: string | null; worked_min: number; break_min: number; late_min: number; early_min: number; ot_min: number;
  ot: { minutes: number; rawMinutes: number; multiplier: number; dayType: string; status: string } | Record<string, never>; flags: Flag[]; computed_at: string; locked_at: string | null;
}

/** Shape returned by the pure engine (POST /punch, /attendance/recompute). */
export interface EngineDay {
  status: AttendanceStatus; workedMin: number; lateMin: number; earlyMin: number; breakMin: number; firstInMs: number | null; lastOutMs: number | null;
  ot: { minutes: number; rawMinutes: number; multiplier: number; dayType: string; status: string }; flags: Flag[]; shiftCode: string | null; localDate: string;
}

export interface Punch { id: string; punched_at: string; local_date?: string; direction: 'in' | 'out' | 'unknown'; verify_mode: string; source: string; device_sn?: string; superseded?: boolean; supersedes_id?: string | null }

export interface Employee {
  id: string; company_id: string; emp_code: string; name_en: string; name_ne: string | null; gender: 'male' | 'female' | 'other' | null; dob: string | null; marital_status: 'single' | 'couple' | null;
  join_date: string | null; exit_date: string | null; designation: string | null; grade: string | null; phone: string | null; email: string | null; ot_eligible: boolean;
  weekly_off_days: number[]; status: 'active' | 'exited'; regime: 'ssf' | 'pf' | 'none'; ssf_id: string | null; pf_id: string | null; cit_id: string | null;
  bank_code: string | null; bank_branch: string | null; bank_account_last4: string | null; is_resident: boolean; remote_area_class: string | null; disability: boolean;
  festival_month: number | null; probation_end: string | null; has_pan: boolean; branch_id: string | null; org_unit_id: string | null; manager_id: string | null;
  employment_type: string | null; shift_id: string | null; created_at: string;
}

export interface Shift {
  id: string; code: string; name_en: string; name_ne: string | null; start: string; end: string; grace_in_min: number; grace_out_min: number; early_in_window_min: number;
  late_out_window_min: number; break_minutes: number; half_day_threshold_min: number; absent_threshold_min: number; pairing_mode: string; rounding_min: number;
  rounding_direction: string; crosses_midnight: boolean; assigned_employees: string;
}

export interface Holiday { id: string; branch_id: string | null; local_date: string; bs_date: string | null; name_en: string; name_ne: string | null; gender_scope: 'all' | 'female'; optional: boolean }
export interface Location { id: string; branch_id: string | null; name: string; lat: string; lng: string; radius_m: number }
export interface CalendarDay { date: string; bsDate: string | null; weekday: number; weeklyOff: boolean; holidays: Array<{ name_en: string; name_ne: string | null; gender_scope: string; optional: boolean }> }

export interface LeaveType {
  id: string; code: string; name_en: string; name_ne: string | null; paid: boolean; statutory: boolean; accrual_basis: string; accrual_days: string; accrual_per_worked_days: number | null;
  max_balance: string | null; carry_forward: boolean; encashable: boolean; certificate_after_days: number | null; allow_half_day: boolean; max_consecutive_days: number | null;
  min_service_days: number; negative_balance_limit: string; gender_scope: string; sandwich_rule: boolean; expiry_days: number | null; sort_order: number;
}
export interface LeaveBalance {
  leaveTypeId: string; code: string; nameEn: string; nameNe: string | null; paid: boolean; encashable: boolean; opening: number; accrued: number; taken: number; cancelled: number;
  lapsed: number; carried: number; adjustments: number; compOff: number; encashed: number; pending: number; balance: number; available: number; fiscalYear: string; explanation: string;
}
export interface LeavePreview { days: number; explanation: string; sandwichApplied: boolean; balance: number; available: number; balanceAfter: number }
export interface LeaveRequest {
  id: string; employee_id: string; emp_code: string; name_en: string; name_ne: string | null; leave_type_id: string; type_code: string; type_name: string; paid: boolean;
  from_date: string; to_date: string; half_day: string | null; days_requested: string; reason: string | null; attachment_key: string | null;
  status: 'pending' | 'approved' | 'rejected' | 'cancelled'; workflow_instance_id: string | null; created_at: string; decided_at: string | null;
}
export interface TeamCalendarDay { date: string; off: Array<{ employeeId: string; empCode: string; nameEn: string; type: string; status: string; halfDay: string | null }> }

export interface InboxItem {
  instanceId: string; objectType: string; objectId: string; step: number; totalSteps: number; createdAt: string;
  employee: { id: string; empCode: string; nameEn: string; nameNe: string | null } | null; summary: Record<string, unknown>;
}
export interface WorkflowDetail { instance: { id: string; object_type: string; object_id: string; state: string; steps: Array<{ role: string }>; current_step: number; created_at: string }; summary: Record<string, unknown>; actions: Array<{ step: number; decision: string; reason: string | null; acted_at: string; actor_name: string; signature: string }> }

export interface RegularisationRequest {
  id: string; employee_id: string; emp_code: string; name_en: string; local_date: string; kind: string; punch_time: string | null; punch_direction: string | null; shift_id: string | null;
  reason: string; status: string; workflow_instance_id: string | null; resulting_punch_id: string | null; created_at: string; decided_at: string | null;
}
export interface OtRequest { id: string; employee_id: string; emp_code: string; name_en: string; local_date: string; requested_min: number; approved_min: number | null; reason: string | null; status: string; created_at: string }
export interface OtSlip { id: string; employee_id: string; emp_code: string; name_en: string; local_date: string; detected_min: number; approved_min: number | null; multiplier: string; day_type: string; status: string; created_at: string }

export interface Device {
  id: string; company_id: string | null; branch_id: string | null; serial_no: string; vendor: string; protocol: string; name: string | null; status: 'pending' | 'active' | 'disabled';
  fw_version: string | null; push_version: string | null; last_handshake_at: string | null; last_seen_at: string | null; created_at: string; offline: boolean; mapped_users: string; queued_commands: string;
}
export interface DeviceMapping { device_user_pin: string; employee_id: string; emp_code: string; name_en: string; valid: string }
export interface UnmappedPunch { id: string; device_id: string; device_sn: string; device_user_pin: string; punched_at: string; local_date: string; verify_mode: string; total_for_pin: string }
export interface DeviceCommand { id: number; command: string; status: string; return_code: number | null; queued_at: string; sent_at: string | null; acked_at: string | null }

export interface NotificationRow { id: string; template: string; title: string; body: string; payload: Record<string, unknown>; created_at: string; read_at: string | null }

export interface Tile { key: string; label: string; value: number }
export interface ExecutiveDashboard {
  date: string; tiles: Tile[]; trend: Array<{ date: string; presentPct: number | null }>;
  needsAttention: Array<{ kind: string; label: string; count?: number; since?: string | null }>; otMinutesThisMonth: number;
  lastPayrollRun: { id: string; status: string; run_type: string; bs_year: number; bs_month: number; headcount: number; net: string; gross: string } | null;
}
export interface MyDashboard {
  date: string; today: AttendanceDayRow | null; month: { bs: string | null; from: string; to: string; byStatus: Record<string, number>; lateMin: number; otMin: number };
  leaveBalances: Array<{ code: string; name_en: string; name_ne: string | null; balance: string }>; unreadNotifications: number;
  pendingRequests: Array<{ kind: string; id: string; created_at: string; detail: string }>;
}
export interface TodayCard { date: string; day: AttendanceDayRow | null; punches: Punch[] }
export interface PunchResult { duplicate: boolean; punch: Punch; day: EngineDay | AttendanceDayRow | null; flags?: string[]; location?: { inside: boolean | null; name: string | null; distanceM: number | null }; message?: string }

export interface Setting { key: string; description: string; value: Record<string, unknown>; updatedAt: string | null; updatedBy: string | null }
export interface CompanyInfo { company: { id: string; code: string; name_en: string; name_ne: string | null; nameEn: string; nameNe: string | null; logoDataUrl: string | null; address: string | null }; today: { ad: string; bs: string | null } }
export interface Branch { id: string; code: string; name_en: string; name_ne: string | null }
export interface OrgUnit { id: string; branch_id: string | null; parent_id: string | null; kind: string; code: string; name_en: string; name_ne: string | null }
export interface AppUserRow { id: string; employee_id: string | null; email: string; name: string; roles: Role[]; status: string; last_login_at: string | null; created_at: string }

export interface RuleVersion { id: string; rule_key: string; jurisdiction: string; valid_from_ad: string; valid_to_ad: string | null; valid_from_bs: string; valid_to_bs: string | null; value: unknown; source_url: string | null; note: string | null; approved_by: string | null; approved_at: string | null; locked: boolean }
export interface SalaryComponent { code: string; company_id: string; name_en: string; name_ne: string | null; type: string; formula: string | null; condition: string | null; depends_on_payment_days: boolean; taxable: boolean; statutory: boolean; rounding: string; sort_order: number; active: boolean }
export interface SalaryStructure { id: string; code: string; name_en: string; versions: Array<{ id: string; version: number; components: Array<{ code: string }>; frozenAt: string | null }> | null }
export interface SalaryAssignment { employee_id: string; emp_code: string; name_en: string; structure_version_id: string; structure_code: string; version: number; valid: string; base_amounts: Record<string, string>; created_at: string }
export interface TaxDeclaration { id: string; employee_id: string; fiscal_year_start: number; life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string; status: string; verified_at: string | null }
export interface Loan { id: string; employee_id: string; emp_code: string; kind: string; principal: string; emi: string; start_bs_year: number; start_bs_month: number; installments: number; status: string; note: string | null; repaid: string }
export interface PayrollPeriod { id: string; bs_year: number; bs_month: number; start_ad: string; end_ad: string; days: number; attendance_locked_at: string | null }
export interface PayrollRun {
  id: string; period_id: string; bs_year?: number; bs_month?: number; run_type: string; status: string; totals: Record<string, string> | null; maker_id: string | null; checker_id: string | null;
  computed_at: string | null; approved_at: string | null; locked_at: string | null; disbursed_at: string | null; filed_at: string | null; created_at: string; notes?: string | null;
  preflight?: Preflight | null; variance?: Variance | null; references_run_id?: string | null; params?: Record<string, unknown>; approval_signature?: string | null;
}
export interface Preflight { ok: boolean; blockers: Array<{ code: string; message: string; count?: number; employees?: string[] }>; warnings: Array<{ code: string; message: string; count?: number; employees?: string[] }>; info: Record<string, unknown> }
export interface Variance { flagged: number; previousNet: string; byEmployee: Array<{ employeeId: string; empCode: string; net: string; previousNet: string | null; flags: string[] }> }
export interface PayslipSummary { id: string; employee_id?: string; emp_code?: string; name_en?: string; run_id?: string; run_type?: string; bs_year?: number; bs_month?: number; paid_days: string; lop_days: string; ot_hours: string; gross: string; tds: string; employee_statutory?: string; employer_statutory?: string; total_deductions: string; net: string; flags?: string[]; published_at: string | null; locked_at?: string | null }
export interface PayslipLine { code: string; type: string; name_en: string; name_ne: string | null; amount: string; basis: unknown; sort_order: number }
export interface PayslipDetail { payslip: Record<string, unknown> & { id: string; employee_id: string; emp_code: string; name_en: string; bs_year: number; bs_month: number; paid_days: string; lop_days: string; ot_hours: string; gross: string; taxable: string; tds: string; employee_statutory: string; employer_statutory: string; total_deductions: string; net: string; explain: Array<{ code?: string; text?: string; line?: string; [k: string]: unknown }>; ytd: Record<string, string>; published_at: string | null }; lines: PayslipLine[]; tax: unknown }
export interface RunDetail { run: PayrollRun; period: PayrollPeriod; payslips: PayslipSummary[]; bankFiles: Array<{ id: string; bank_code: string; format: string; filename: string; checksum: string; created_at: string }> }
export interface BankFileResult { id: string; filename: string; rows: number; checksum: string; warnings: string[] }

export interface Consent { id: string; purpose: string; modality: string; notice_version: string; granted_at: string; revoked_at: string | null }
export interface TemplateMeta { id: string; modality: string; vendor_format: string; device_user_pin: string | null; device_sn: string | null; key_version: number; consent_id: string | null; created_at: string; shredded_at: string | null }
export interface Transparency { stored: { en: string[]; ne: string[] }; notStored: { en: string[]; ne: string[] }; retention: unknown; access: { en: string[]; ne: string[] }; withdraw: { en: string; ne: string }; legalBasis: { en: string; ne: string } }

export interface ExceptionRow { id: string; employee_id: string; emp_code: string; name_en: string; name_ne: string | null; local_date: string; bs_date: string | null; status: AttendanceStatus; worked_min: number; late_min: number; early_min: number; ot_min: number; flags: Flag[] }
export interface TeamTodayRow { employee_id: string; emp_code: string; name_en: string; name_ne: string | null; status: AttendanceStatus | null; first_in: string | null; last_out: string | null; late_min: number | null; worked_min: number | null; flags: Flag[] | null }
export interface RosterData { employees: Array<{ id: string; emp_code: string; name_en: string; name_ne: string | null; weekly_off_days: number[]; default_shift_id: string | null }>; roster: Array<{ employee_id: string; local_date: string; shift_id: string | null }> }
export interface AuditEvent { id: number; occurred_at: string; actor_id: string | null; action: string; table_name: string; row_id: string | null }
