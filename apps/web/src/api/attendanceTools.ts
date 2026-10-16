import { get, post, put } from './client';

export interface LatePenaltyRule { enabled: boolean; lateCount: number; minLateMinutes: number; penaltyDays: number; leaveTypeIds: string[] }
export type WhosOutScope = 'team' | 'department' | 'company';
export interface ExtrasPolicy { blockLateClockIn: boolean; blockAfterMin: number; whosOutScope: WhosOutScope; latePenalty: LatePenaltyRule }

export interface PenaltyLine {
  employeeId: string; empCode: string; name: string; lateDays: number; lateMinutes: number; lateDates: string[]; penaltyDays: number;
  allocations: Array<{ leaveTypeId: string; code: string; days: number }>; unpaidDays: number;
  applied: { id: string; penaltyDays: number; leaveDays: number; unpaidDays: number; appliedAt: string } | null;
}
export interface MyLate { bsYear: number; bsMonth: number; enabled: boolean; rule: { lateCount: number; penaltyDays: number; minLateMinutes: number }; lateDays: number; lateDates: string[]; projectedDays: number; applied: PenaltyLine['applied'] }

export interface PendingItem { instanceId: string; type: 'leave' | 'time'; requestId: string; from: string; to: string; label: string; createdAt: string; ageDays: number; waitingOn: Array<{ userId: string; name: string }> }
export interface IssueRow { employeeId: string; empCode: string; name: string; missing: string[]; absent: string[]; pending: PendingItem[] }
export interface IssueSummary { employees: number; missing: number; absent: number; pendingLeave: number; pendingTime: number; oldestPendingDays: number }

export interface UploadRow { line: number; empCode: string; date: string | null; dateBs: string | null; inTime: string | null; outTime: string | null; note: string | null; employeeId: string | null; name: string | null; overnight: boolean; errors: string[] }
export interface UploadHistory { id: string; filename: string | null; reason: string; rows_total: number; punches: number; duplicates: number; created_at: string; uploaded_by_name: string | null }

export type OutKind = 'leave' | 'travel' | 'wfh' | 'field';
export interface OutPerson { employeeId: string; name: string; designation: string | null; kind: OutKind; detail: string | null }

export const OUT_LABEL: Record<OutKind, string> = { leave: 'On leave', travel: 'Travelling', wfh: 'From home', field: 'In the field' };
export const OUT_COLOR: Record<OutKind, string> = { leave: 'blue', travel: 'grape', wfh: 'teal', field: 'orange' };

/** Set the leave order (Leave, Policies): the listed types in order. */
export const leaveOrder = (ids: string[]) => post<{ ok: boolean }>('/leave/types/order', { ids });

export const attendanceTools = {
  policy: () => get<{ policy: ExtrasPolicy }>('/attendance-tools/policy'),
  savePolicy: (p: ExtrasPolicy) => put<{ policy: ExtrasPolicy }>('/attendance-tools/policy', p),
  penalties: (bsYear: number, bsMonth: number) => get<{ rule: LatePenaltyRule; locked: boolean; lines: PenaltyLine[] }>('/attendance-tools/penalties', { bsYear, bsMonth }),
  myLate: (q: { bsYear?: number; bsMonth?: number } = {}) => get<MyLate>('/attendance-tools/penalties/me', q),
  applyPenalties: (bsYear: number, bsMonth: number, employeeIds?: string[]) => post<{ applied: number; skipped: number }>('/attendance-tools/penalties/apply', { bsYear, bsMonth, ...(employeeIds ? { employeeIds } : {}) }),
  reversePenalty: (id: string, reason: string) => post<{ ok: boolean }>(`/attendance-tools/penalties/${id}/reverse`, { reason }),
  issues: (from: string, to: string) => get<{ summary: IssueSummary; rows: IssueRow[] }>('/attendance-tools/issues', { from, to }),
  issueSummary: (bsYear: number, bsMonth: number) => get<IssueSummary & { bsYear: number; bsMonth: number; from: string; to: string }>('/attendance-tools/issues/summary', { bsYear, bsMonth }),
  nudgeEmployee: (employeeId: string, from: string, to: string) => post<{ sent: number; already: boolean }>('/attendance-tools/issues/nudge', { target: 'employee', employeeId, from, to }),
  nudgeApprover: (instanceId: string) => post<{ sent: number; already: boolean }>('/attendance-tools/issues/nudge', { target: 'approver', instanceId }),
  checkUpload: (csv: string) => post<{ errors: string[]; rows: UploadRow[]; valid: number; invalid: number }>('/attendance-tools/upload/check', { csv }),
  importUpload: (csv: string, reason: string, filename?: string) => post<{ id: string; rows: number; punches: number; duplicates: number; days: number }>('/attendance-tools/upload/import', { csv, reason, ...(filename ? { filename } : {}) }),
  uploads: () => get<{ uploads: UploadHistory[] }>('/attendance-tools/uploads'),
  whosOut: (days = 7) => get<{ scope: WhosOutScope; days: Array<{ date: string; people: OutPerson[] }> }>('/attendance-tools/whos-out', { days }),
};
