import { del, download, get, post, put } from './client';

export interface OptimumCit {
  available: boolean; reason?: string;
  currentMonthly?: string; optimumMonthly?: string; currentAnnualTax?: string; optimumAnnualTax?: string; annualSaving?: string;
  deductionLimit?: string; retirementWithCurrent?: string; remainingMonths?: number;
  /** Why the suggestion stops: when not available, `reason` holds a sentence instead. */
}
export type OptimumReason = 'limit' | 'no_tax_left' | 'net_pay' | 'already_optimal';
export interface OptimumRow extends OptimumCit { employeeId: string; empCode: string; name: string }

export interface OpeningBalance {
  id: string; employee_id: string; emp_code: string; name_en: string; fiscal_year_start: number; gross: string; taxable: string; tds_paid: string;
  retirement_employee: string; retirement_employer: string; festival_paid: boolean; months: number; source: 'previous_employer' | 'go_live' | 'other'; note: string | null;
  updated_at: string; updated_by_name: string | null;
}
export interface OpeningBody {
  employeeId: string; fiscalYearStart: number; gross: string; taxable: string; tdsPaid: string; retirementEmployee: string; retirementEmployer: string;
  festivalPaid: boolean; months: number; source: OpeningBalance['source']; note?: string | null;
}

export interface Hold {
  id: string; employee_id: string; emp_code: string; name_en: string; run_id: string; run_type: string; run_status: string; bs_year: number; bs_month: number;
  amount: string; reason: string; status: 'held' | 'releasing' | 'released' | 'cancelled'; release_mode: 'payroll' | 'outside' | null; release_run_id: string | null;
  release_bs_year: number | null; release_bs_month: number | null; release_note: string | null; held_at: string; held_by_name: string | null; released_at: string | null;
}

export interface ReportResult<R> { columns: Array<{ key: string; header: string }>; rows: R[] }
export interface Variance { previous: { runId: string; bsYear: number; bsMonth: number } | null; rows: Array<{ empCode: string; name: string; previousNet: string; net: string; difference: string; percent: string; reasons: string }> }
export interface LeaveLiability { asOf: string; divisor: number; total: string; byDepartment: Array<{ department: string; amount: string }>; rows: Array<{ empCode: string; name: string; department: string; leaveType: string; balance: string; dailyRate: string; amount: string; note: string }> }

const qs = (o: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
};

export const compliance = {
  optimumMine: () => get<OptimumCit & { reason?: string }>('/payroll/optimum-cit/me'),
  optimumAll: () => get<{ fiscalYearStart: number; employees: OptimumRow[] }>('/payroll/optimum-cit'),
  applyCit: (b: { employeeId: string; monthlyCit: string; fromBsMonth?: number }) => post<{ ok: boolean }>('/payroll/optimum-cit/apply', b),

  openings: (fiscalYearStart: number) => get<{ fiscalYearStart: number; balances: OpeningBalance[] }>('/payroll/opening-balances', { fiscalYearStart }),
  saveOpening: (b: OpeningBody) => put<{ ok: boolean }>('/payroll/opening-balances', b),
  deleteOpening: (id: string) => del<{ ok: boolean }>(`/payroll/opening-balances/${id}`),
  importOpenings: (csv: string) => post<{ imported: number }>('/payroll/opening-balances/import', { csv }),

  holds: (status: 'open' | 'released' | 'cancelled' = 'open') => get<{ holds: Hold[] }>('/payroll/holds', { status }),
  hold: (runId: string, employeeId: string, reason: string) => post<{ id: string; amount: string }>(`/payroll/runs/${runId}/holds`, { employeeId, reason }),
  release: (id: string, mode: 'payroll' | 'outside', note?: string) => post<{ ok: boolean }>(`/payroll/holds/${id}/release`, { mode, ...(note ? { note } : {}) }),
  cancelHold: (id: string) => post<{ ok: boolean }>(`/payroll/holds/${id}/cancel`),

  certificateUrl: (employeeId: string | 'me', kind: 'tax' | 'salary', fiscalYearStart?: number, purpose?: string) =>
    `/api/v1/payroll/certificates/${employeeId}${qs({ kind, fiscalYearStart, purpose })}`,

  annualTax: (fiscalYearStart: number) => get<ReportResult<Record<string, string>>>('/payroll/reports/annual-tax', { fiscalYearStart }),
  annualTaxCsv: (fiscalYearStart: number) => download(`/payroll/reports/annual-tax${qs({ fiscalYearStart, format: 'csv' })}`, `annual-tax-${fiscalYearStart}.csv`),
  taxDeposit: (bsYear: number, bsMonth: number) => get<ReportResult<Record<string, string>>>('/payroll/reports/tax-deposit', { bsYear, bsMonth }),
  retirementDeposit: (bsYear: number, bsMonth: number) => get<ReportResult<Record<string, string>>>('/payroll/reports/retirement-deposit', { bsYear, bsMonth }),
  monthCsv: (report: 'tax-deposit' | 'retirement-deposit', bsYear: number, bsMonth: number) =>
    download(`/payroll/reports/${report}${qs({ bsYear, bsMonth, format: 'csv' })}`, `${report}-${bsYear}-${String(bsMonth).padStart(2, '0')}.csv`),
  variance: (runId: string) => get<Variance>('/payroll/reports/pay-variance', { runId }),
  varianceCsv: (runId: string) => download(`/payroll/reports/pay-variance${qs({ runId, format: 'csv' })}`, 'pay-variance.csv'),
  leaveLiability: (asOf?: string) => get<LeaveLiability>('/payroll/reports/leave-liability', asOf ? { asOf } : undefined),
  leaveLiabilityCsv: (asOf: string) => download(`/payroll/reports/leave-liability${qs({ asOf, format: 'csv' })}`, `leave-liability-${asOf}.csv`),
};
