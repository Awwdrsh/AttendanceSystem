import { del, get, post } from './client';
import type { AuthUser } from './types';

export type LoginResponse = { user: AuthUser; token: string } | { twoFactorRequired: true; challenge: string };

export interface TwoFactorStatus { enabled: boolean; enabledAt: string | null; recoveryCodesLeft: number; required: boolean; setupPending: boolean }
export interface SessionInfo { id: string; createdAt: string; lastSeenAt: string; expiresAt: string; ip: string | null; device: string; twoFactor: boolean; current: boolean }

export const security = {
  login: (email: string, password: string) => post<LoginResponse>('/auth/login', { email, password }),
  loginTwoFactor: (challenge: string, code: string) => post<{ user: AuthUser; token: string; usedRecoveryCode?: boolean }>('/auth/login/2fa', { challenge, code }),
  status: () => get<TwoFactorStatus>('/auth/2fa'),
  setup: () => post<{ secret: string; otpauthUri: string }>('/auth/2fa/setup'),
  enable: (code: string) => post<{ recoveryCodes: string[] }>('/auth/2fa/enable', { code }),
  recoveryCodes: (code: string) => post<{ recoveryCodes: string[] }>('/auth/2fa/recovery-codes', { code }),
  disable: (password: string, code: string) => post<{ ok: boolean }>('/auth/2fa/disable', { password, code }),
  sessions: () => get<{ sessions: SessionInfo[] }>('/auth/sessions'),
  revokeSession: (id: string) => del<{ ok: boolean }>(`/auth/sessions/${id}`),
  revokeOthers: () => post<{ revoked: number }>('/auth/sessions/revoke-others'),
  resetUser: (userId: string, reason: string) => post<{ ok: boolean }>(`/auth/users/${userId}/2fa/reset`, { reason }),
};

// ---------------------------------------------------------------- tax savings

export interface DeclarationValues { lifePremium: string; healthPremium: string; citMonthly: string; medicalCost: string; otherRetirementAnnual: string }
export interface TaxSavingsRequest {
  id: string; employee_id: string; fiscal_year_start: number; life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string;
  note: string | null; status: 'pending' | 'approved' | 'rejected' | 'cancelled'; created_at: string; decided_at: string | null; decision_note: string | null;
  proofs?: Array<{ id: string; filename: string; mime: string; bytes: number }>;
}
export interface TaxSummary {
  projectedAnnualTaxable: string; retirementDeduction: string; insuranceDeduction: string; taxableAfterDeductions: string; medicalCredit: string;
  annualTax: string; tdsThisMonth: string; remainingMonths: number; declaration: DeclarationValues;
}
export interface TaxEstimate {
  available: boolean; reason?: string; fiscalYearStart: number; bsYear?: number; bsMonth?: number;
  current?: TaxSummary; proposed?: TaxSummary; annualSaving?: string; monthlySaving?: string;
}

export const taxSavings = {
  get: (fiscalYearStart?: number) => get<{ fiscalYearStart: number; declaration: { life_premium: string; health_premium: string; cit_monthly: string; medical_cost: string; other_retirement_annual: string; status: string; verified_at: string | null } | null; pending: TaxSavingsRequest | null; history: TaxSavingsRequest[] }>(
    '/payroll/tax-savings', { fiscalYearStart }),
  submit: (b: DeclarationValues & { fiscalYearStart: number; note?: string; proofFileIds: string[] }) => post<{ request: TaxSavingsRequest }>('/payroll/tax-savings', b),
  cancel: (id: string) => post<{ ok: boolean }>(`/payroll/tax-savings/${id}/cancel`),
  estimate: (b: { fiscalYearStart?: number; proposed?: DeclarationValues; employeeId?: string }) => post<TaxEstimate>('/payroll/tax-savings/estimate', b),
  fileUrl: (requestId: string, fileId: string) => `/api/v1/payroll/tax-savings/${requestId}/files/${fileId}`,
};
