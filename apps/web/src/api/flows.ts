import { del, get, patch, post, put } from './client';

export type ApproverKind = 'manager' | 'manager2' | 'department_head' | 'branch_head' | 'role' | 'user' | 'group';
export type StepRole = 'hr' | 'admin' | 'payroll_admin' | 'attendance_admin' | 'recruiter';

export interface FlowStep { kind?: ApproverKind; role: string; userId?: string; groupId?: string; minAmount?: number; label?: string }
export interface FlowConditions { employeeIds: string[]; groupIds: string[]; designationIds: string[]; orgUnitIds: string[]; branchIds: string[] }
export interface ApprovalFlow { id: string; object_type: string; name: string; conditions: Partial<FlowConditions>; steps: FlowStep[]; priority: number; active: boolean; updated_at: string }
export interface FlowType { objectType: string; label: string; money: boolean; defaultSteps: FlowStep[]; flows: ApprovalFlow[] }
export interface UserGroup { id: string; name: string; description: string | null; members: Array<{ id: string; name: string }> }

export interface PreviewStep { label: string; minAmount: number | null; approvers: Array<{ id: string; name: string }>; notes: string[]; fallback: boolean }
export interface Preview { flow: { id: string; name: string } | null; steps: PreviewStep[] }

export interface VerifyProblem {
  employeeId: string; empCode: string; name: string; objectType: string; typeLabel: string; flow: string | null; step: number; stepLabel: string; problem: string; suggestion: string;
}

export interface Delegation {
  id: string; user_id: string; user_name: string; delegate_id: string; delegate_name: string; from_date: string; to_date: string; note: string | null; created_at: string;
}

/** Step as sent to the server (kind plus the field that kind needs). */
export interface StepInput { kind: ApproverKind; role?: StepRole; userId?: string; groupId?: string; minAmount?: number; label?: string }

export const flows = {
  config: () => get<{ types: FlowType[]; groups: UserGroup[] }>('/workflow/flows/config'),
  setDefault: (objectType: string, steps: StepInput[]) => put<{ steps: FlowStep[] }>(`/workflow/flows/defaults/${objectType}`, { steps }),
  create: (b: { objectType: string; name: string; conditions: FlowConditions; steps: StepInput[]; priority?: number; active?: boolean }) => post<{ id: string }>('/workflow/flows', b),
  update: (id: string, b: Partial<{ name: string; conditions: FlowConditions; steps: StepInput[]; priority: number; active: boolean }>) => patch<{ ok: boolean }>(`/workflow/flows/${id}`, b),
  remove: (id: string) => del<{ ok: boolean }>(`/workflow/flows/${id}`),
  createGroup: (b: { name: string; description?: string | null; userIds: string[] }) => post<{ id: string }>('/workflow/groups', b),
  updateGroup: (id: string, b: { name?: string; description?: string | null; userIds?: string[] }) => patch<{ ok: boolean }>(`/workflow/groups/${id}`, b),
  removeGroup: (id: string) => del<{ ok: boolean }>(`/workflow/groups/${id}`),
  preview: (q: { objectType: string; employeeId?: string; amount?: number }) => get<Preview>('/workflow/preview', q),
  verify: (objectType?: string) => get<{ checked: number; problems: VerifyProblem[] }>('/workflow/verify', objectType ? { objectType } : undefined),
  delegates: (all = false) => get<{ delegates: Delegation[] }>('/workflow/delegates', all ? { all: 1 } : undefined),
  addDelegate: (b: { delegateId: string; from: string; to: string; note?: string; userId?: string }) => post<{ id: string }>('/workflow/delegates', b),
  cancelDelegate: (id: string) => del<{ ok: boolean }>(`/workflow/delegates/${id}`),
};

/** Turn a stored step back into the form the editor uses. */
export function toStepInput(s: FlowStep): StepInput {
  const kind: ApproverKind = s.kind ?? (s.role === 'manager' ? 'manager' : 'role');
  const out: StepInput = { kind };
  if (kind === 'role') out.role = s.role as StepRole;
  if (s.userId) out.userId = s.userId;
  if (s.groupId) out.groupId = s.groupId;
  if (s.minAmount !== undefined) out.minAmount = s.minAmount;
  if (s.label) out.label = s.label;
  return out;
}
