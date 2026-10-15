import { download, get, patch, post, put } from './client';

export type ClaimStatus = 'draft' | 'pending' | 'approved' | 'rejected' | 'cancelled' | 'paid';
export type TripStatus = 'pending' | 'approved' | 'rejected' | 'cancelled' | 'settling' | 'settled';
export type Destination = 'valley' | 'city' | 'rural' | 'international';
export type TravelMode = 'bus' | 'air' | 'office_vehicle' | 'own_vehicle' | 'other';
export type Scope = 'own' | 'team' | 'all';

export interface ExpenseCategory { id: string; code: string; name: string; kind: 'amount' | 'mileage'; receiptRequiredAbove: string | null; active: boolean; sortOrder: number }
export interface ExpenseLimit { id: string; categoryId: string; grade: string; perClaim: string | null; perMonth: string | null }
export interface TadaRate { grade: string; destination: Destination; daRate: string; lodgingRate: string }
export interface ExpenseSettings { mileageRatePerKm: string; limitMode: 'block' | 'warn'; claimWindowDays: number }
export interface ExpensePolicy {
  settings: ExpenseSettings; categories: ExpenseCategory[]; limits: ExpenseLimit[]; tadaRates: TadaRate[];
  destinations: Array<{ value: Destination; label: string }>; myGrade: string | null; grades: string[];
}

export interface CheckIssue { level: 'block' | 'warn'; code: string; message: string; item?: number }
export interface ClaimItemInput { expenseDate: string; categoryId: string; description: string; amount?: string; km?: number; receiptFileId?: string | null }

export interface ClaimRow {
  id: string; title: string; status: ClaimStatus; totalClaimed: string; totalApproved: string | null; submittedAt: string | null; decidedAt: string | null;
  paymentMethod: 'payroll' | 'cash' | null; paidAt: string | null; createdAt: string; employeeId: string; empCode: string; employeeName: string;
  items: number; firstDate: string | null; lastDate: string | null; payoutStatus: string | null; payBsYear: number | null; payBsMonth: number | null;
}
export interface ClaimItem {
  id: string; expenseDate: string; categoryId: string; categoryName: string; categoryKind: 'amount' | 'mileage'; description: string; amount: string; km: string | null;
  receiptFileId: string | null; receiptName: string | null; receiptMime: string | null; approvedAmount: string | null; adjustReason: string | null;
}
export interface Payout { id: string; code: 'EXP_REIMB' | 'TRAVEL_ADV' | 'EXP_RECOVERY'; amount: string; status: 'pending' | 'paid' | 'cash' | 'cancelled'; paidAmount: string | null; paidAt: string | null; bsYear: number | null; bsMonth: number | null; sourceType?: string }
export interface ClaimDetail {
  claim: { id: string; title: string; status: ClaimStatus; totalClaimed: string; totalApproved: string | null; paymentMethod: string | null; decisionNote: string | null;
    submittedAt: string | null; decidedAt: string | null; paidAt: string | null; createdAt: string; employee: { id: string; empCode: string; name: string; grade: string | null } };
  items: ClaimItem[]; payouts: Payout[]; issues: CheckIssue[]; canDecide: boolean;
}

export interface TadaQuote { days: number; nights: number; currency: 'NPR' | 'USD'; daRate: string; lodgingRate: string; fxRate: string | null; daRateNpr: string; lodgingRateNpr: string; daAmount: string; lodgingLimit: string; grade: string }
export interface TripRow {
  id: string; destination: string; destinationCategory: Destination; purpose: string; fromDate: string; toDate: string; mode: TravelMode; days: number;
  daAmount: string; lodgingLimit: string; estimatedCost: string; advanceRequested: string; advanceApproved: string | null; advanceStatus: 'none' | 'pending' | 'paid';
  advanceMethod: 'payroll' | 'cash' | null; status: TripStatus; createdAt: string; employeeId: string; empCode: string; employeeName: string;
  settlement: { id: string; status: string; net: string } | null;
}
export interface TravelBill { id: string; kind: 'transport' | 'lodging' | 'other'; billDate: string; description: string; amount: string; fileId: string | null; fileName: string | null }
export interface Settlement {
  id: string; status: 'pending' | 'approved' | 'rejected' | 'cancelled'; actualFrom: string; actualTo: string; days: number; nights: number; daAmount: string;
  lodgingClaimed: string; lodgingAllowed: string; otherBills: string; entitlement: string; advance: string; net: string; note: string | null; decisionNote: string | null; createdAt: string;
  bills: TravelBill[];
}
export interface TripDetail {
  trip: TripRow & { fxRate: string | null; nights: number; daRate: string; lodgingRate: string; decisionNote: string | null; destinationLabel: string;
    employee: { id: string; empCode: string; name: string; grade: string | null } };
  settlements: Settlement[]; payouts: Payout[]; canDecide: boolean; canDecideSettlement: boolean; canSettle: boolean;
}
export interface ReportResult<R> { columns: Array<{ key: string; header: string }>; rows: R[] }

const E = '/expenses';
export const expenses = {
  policy: () => get<ExpensePolicy>(`${E}/policy`),
  saveSettings: (b: ExpenseSettings) => put<{ settings: ExpenseSettings }>(`${E}/policy/settings`, b),
  addCategory: (b: { code: string; name: string; kind: 'amount' | 'mileage'; receiptRequiredAbove: string | null }) => post<{ id: string }>(`${E}/categories`, b),
  updateCategory: (id: string, b: Partial<{ name: string; kind: 'amount' | 'mileage'; receiptRequiredAbove: string | null; active: boolean; sortOrder: number }>) => patch<{ ok: boolean }>(`${E}/categories/${id}`, b),
  setLimits: (id: string, limits: Array<{ grade: string; perClaim: string | null; perMonth: string | null }>) => put<{ ok: boolean }>(`${E}/categories/${id}/limits`, { limits }),
  setTadaRates: (rates: TadaRate[]) => put<{ ok: boolean }>(`${E}/tada-rates`, { rates }),

  check: (items: ClaimItemInput[], claimId?: string) => post<{ amounts: string[]; total: string; issues: CheckIssue[] }>(`${E}/claims/check`, { items, ...(claimId ? { claimId } : {}) }),
  claims: (q: { scope?: Scope; status?: ClaimStatus } = {}) => get<{ claims: ClaimRow[] }>(`${E}/claims`, q),
  claim: (id: string) => get<ClaimDetail>(`${E}/claims/${id}`),
  createClaim: (b: { title: string; items: ClaimItemInput[]; submit?: boolean }) => post<{ id: string; status: ClaimStatus; total: string; issues: CheckIssue[] }>(`${E}/claims`, b),
  updateClaim: (id: string, b: { title: string; items: ClaimItemInput[]; submit?: boolean }) => put<{ id: string; status: ClaimStatus; total: string; issues: CheckIssue[] }>(`${E}/claims/${id}`, b),
  submitClaim: (id: string) => post<{ id: string; status: ClaimStatus }>(`${E}/claims/${id}/submit`),
  cancelClaim: (id: string) => post<{ id: string; status: ClaimStatus }>(`${E}/claims/${id}/cancel`),
  reviewClaim: (id: string, b: { decision: 'approve' | 'reject'; reason?: string; items?: Array<{ id: string; approvedAmount: string; reason?: string }> }) => post<{ state: string }>(`${E}/claims/${id}/review`, b),
  payClaimOutside: (id: string, note: string) => post<{ id: string; status: ClaimStatus }>(`${E}/claims/${id}/pay-outside`, { note }),
  receiptUrl: (claimId: string, itemId: string) => `/api/v1${E}/claims/${claimId}/items/${itemId}/receipt`,

  quote: (b: { destinationCategory: Destination; fromDate: string; toDate: string; fxRate?: string | null }) => post<{ quote: TadaQuote }>(`${E}/travel/quote`, b),
  trips: (q: { scope?: Scope; status?: TripStatus } = {}) => get<{ trips: TripRow[] }>(`${E}/travel`, q),
  trip: (id: string) => get<TripDetail>(`${E}/travel/${id}`),
  requestTravel: (b: { destination: string; destinationCategory: Destination; purpose: string; fromDate: string; toDate: string; mode: TravelMode; estimatedCost?: string; advanceRequested?: string; fxRate?: string | null }) =>
    post<{ id: string; status: TripStatus; quote: TadaQuote }>(`${E}/travel`, b),
  cancelTrip: (id: string) => post<{ id: string; status: TripStatus }>(`${E}/travel/${id}/cancel`),
  reviewTrip: (id: string, b: { decision: 'approve' | 'reject'; reason?: string; advanceApproved?: string }) => post<{ state: string }>(`${E}/travel/${id}/review`, b),
  payAdvanceOutside: (id: string, note: string) => post<{ id: string }>(`${E}/travel/${id}/advance-outside`, { note }),
  settle: (id: string, b: { actualFrom: string; actualTo: string; bills: Array<{ kind: TravelBill['kind']; billDate: string; description: string; amount: string; fileId?: string | null }>; note?: string }) =>
    post<{ id: string; daAmount: string; lodgingAllowed: string; otherBills: string; entitlement: string; advance: string; net: string }>(`${E}/travel/${id}/settlement`, b),
  reviewSettlement: (id: string, b: { decision: 'approve' | 'reject'; reason?: string }) => post<{ state: string }>(`${E}/settlements/${id}/review`, b),
  billUrl: (settlementId: string, billId: string) => `/api/v1${E}/settlements/${settlementId}/bills/${billId}/file`,

  spend: (q: { from: string; to: string; groupBy: 'category' | 'department' | 'month' | 'employee' }) => get<ReportResult<{ group: string; items: number; amount: string }>>(`${E}/reports/spend`, q),
  advances: () => get<ReportResult<{ id: string; empCode: string; employeeName: string; destination: string; trip: string; advance: string; advanceStatus: string; status: string }>>(`${E}/reports/advances`),
  pendingPayment: () => get<ReportResult<{ id: string; empCode: string; employeeName: string; note: string; kind: string; amount: string; inRun: string }>>(`${E}/reports/pending-payment`),
  downloadReport: (name: 'spend' | 'advances' | 'pending-payment', q: Record<string, string> = {}) =>
    download(`${E}/reports/${name}?${new URLSearchParams({ ...q, format: 'csv' }).toString()}`, `${name === 'spend' ? `expense-spend-by-${q.groupBy ?? 'category'}` : name === 'advances' ? 'outstanding-travel-advances' : 'expenses-pending-payment'}.csv`),
};
