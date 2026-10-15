import { get, patch, post, put } from './client';

export type AssigneeType = 'hr' | 'manager' | 'employee' | 'user';
export type CaseKind = 'onboarding' | 'offboarding';

export interface TemplateTask { id?: string; title: string; description?: string | null; department?: string | null; assignee_type?: AssigneeType; assignee_user_id?: string | null; due_offset_days?: number; link?: string | null; sort?: number }
export interface Template { id: string; kind: CaseKind; name: string; description: string | null; branch_id: string | null; org_unit_id: string | null; branch_name: string | null; org_unit_name: string | null; is_default: boolean; active: boolean; tasks: TemplateTask[] }
export interface TemplateInput {
  kind: CaseKind; name: string; description?: string | null; branchId?: string | null; orgUnitId?: string | null; isDefault?: boolean; active?: boolean;
  tasks: Array<{ title: string; description?: string | null; department?: string | null; assigneeType: AssigneeType; assigneeUserId?: string | null; dueOffsetDays: number; link?: string | null }>;
}
export interface CaseSummary { id: string; kind: CaseKind; status: 'active' | 'completed' | 'cancelled'; anchor_date: string; created_at: string; completed_at: string | null; exit_id: string | null; employee_id: string; emp_code: string; name_en: string; designation: string | null; join_date: string | null; template_name: string | null; total: number; done: number; overdue: number; next_due: string | null }
export interface Task {
  id: string; case_id: string; employee_id: string; title: string; description: string | null; department: string | null; assignee_type: AssigneeType; assignee_user_id: string | null;
  assignee_name?: string | null; due_date: string | null; link: string | null; status: 'open' | 'done' | 'skipped'; note: string | null; done_at: string | null; done_by_name?: string | null;
  comments?: number; can_complete?: boolean; kind?: CaseKind; employee_name?: string; emp_code?: string; own?: boolean;
}
export interface CaseDetail { case: { id: string; kind: CaseKind; status: string; anchor_date: string; exit_id: string | null; employee_id: string }; employee: { id: string; emp_code: string; name_en: string; designation: string | null; join_date: string | null; exit_date: string | null }; tasks: Task[] }
export interface Comment { id: string; body: string; created_at: string; author_id: string; author_name: string }
export interface LifecyclePolicy { noticeDays: number; autoStartOnboarding: boolean; probationReminderDays: number[]; selfExitInterview: boolean }
export interface ProbationRow { id: string; emp_code: string; name_en: string; designation: string | null; join_date: string | null; probation_end: string; days_left: number; manager_name: string | null; last_review: { id: string; status: string; recommendation: string; created_at: string } | null }
export type ExitKind = 'resignation' | 'termination' | 'retirement' | 'contract_end' | 'death' | 'other';
export interface ExitRow {
  id: string; employee_id: string; kind: ExitKind; voluntary: boolean; reason_category: string | null; reason: string | null; submitted_on: string; notice_days: number;
  proposed_lwd: string; accepted_lwd: string | null; status: 'pending' | 'accepted' | 'rejected' | 'withdrawn' | 'cancelled' | 'completed'; emp_code: string; name_en: string; designation: string | null;
  join_date: string | null; case_id: string | null; tasks_total: number; tasks_done: number; assets_held: number; interview_done: boolean; created_at: string;
}
export interface Settlement { period: { id: string; bsYear: number; bsMonth: number } | null; run: { id: string; status: string; locked: boolean } | null; activeLoans: number; loanOutstanding: string; encashableLeaveDays: number; steps: string[] }
export interface ExitQuestion { id: string; sort: number; question: string; kind: 'text' | 'rating' | 'choice'; options: string[]; active: boolean }
export interface InterviewAnswer { questionId: string; question: string; kind: string; answer: string | number }
export interface AssetType { id: string; name: string }
export interface Asset { id: string; tag: string; name: string; serial: string | null; condition: string; status: 'available' | 'assigned' | 'retired'; purchase_date: string | null; cost: string | null; notes: string | null; type_id: string | null; type_name: string | null; assignment_id: string | null; employee_id: string | null; assigned_on: string | null; employee_name: string | null; emp_code: string | null }
export interface EmployeeLifecycle {
  employee: { id: string; name_en: string; join_date: string | null; probation_end: string | null; confirmed_on: string | null; status: string; exit_date: string | null };
  cases: Array<{ id: string; kind: CaseKind; status: string; anchor_date: string; total: number; done: number }>;
  probation: { end: string | null; confirmedOn: string | null; reviews: Array<{ id: string; rating: number; recommendation: string; extend_to: string | null; last_working_day: string | null; status: string; created_at: string; submitted_by_name: string }>; confirmations: Array<{ id: string; confirmed_on: string }> };
  exits: Array<{ id: string; kind: ExitKind; status: string; voluntary: boolean; reason_category: string | null; proposed_lwd: string; accepted_lwd: string | null }>;
  assets: Array<{ assignment_id: string; assigned_on: string; returned_on: string | null; id: string; tag: string; name: string; serial: string | null; type_name: string | null }>;
}

export const lifecycle = {
  policy: () => get<{ policy: LifecyclePolicy }>('/lifecycle/settings'),
  savePolicy: (p: LifecyclePolicy) => put<{ policy: LifecyclePolicy }>('/lifecycle/settings', p),
  templates: (kind?: CaseKind) => get<{ templates: Template[] }>('/lifecycle/templates', { kind }),
  createTemplate: (b: TemplateInput) => post<{ id: string }>('/lifecycle/templates', b),
  updateTemplate: (id: string, b: TemplateInput) => put<{ id: string }>(`/lifecycle/templates/${id}`, b),
  cases: (q: { kind?: CaseKind; status?: string } = {}) => get<{ cases: CaseSummary[] }>('/lifecycle/cases', q),
  startOnboarding: (b: { employeeId: string; templateId?: string | null; anchorDate?: string }) => post<{ id: string }>('/lifecycle/cases', b),
  case: (id: string) => get<CaseDetail>(`/lifecycle/cases/${id}`),
  cancelCase: (id: string) => post<{ ok: boolean }>(`/lifecycle/cases/${id}/cancel`),
  addTask: (caseId: string, b: { title: string; assigneeType: AssigneeType; assigneeUserId?: string | null; dueDate?: string | null; department?: string | null }) => post<{ task: Task }>(`/lifecycle/cases/${caseId}/tasks`, b),
  updateTask: (id: string, b: { status?: 'open' | 'done' | 'skipped'; note?: string | null; dueDate?: string | null }) => patch<{ task: Task; caseCompleted: boolean }>(`/lifecycle/tasks/${id}`, b),
  comments: (taskId: string) => get<{ comments: Comment[] }>(`/lifecycle/tasks/${taskId}/comments`),
  addComment: (taskId: string, body: string) => post<{ comment: Comment }>(`/lifecycle/tasks/${taskId}/comments`, { body }),
  myTasks: () => get<{ tasks: Task[] }>('/lifecycle/my-tasks'),
  employeeSummary: (id: string) => get<EmployeeLifecycle>(`/lifecycle/employees/${id}/summary`),
  probation: (within = 60) => get<{ employees: ProbationRow[] }>('/lifecycle/probation', { within }),
  submitReview: (b: { employeeId: string; rating: number; recommendation: 'confirm' | 'extend' | 'terminate'; extendTo?: string | null; lastWorkingDay?: string | null; comments: string }) => post<{ id: string }>('/lifecycle/probation/reviews', b),
  confirm: (employeeId: string, confirmedOn: string) => post<{ id: string }>('/lifecycle/probation/confirm', { employeeId, confirmedOn }),
  exits: (status?: string) => get<{ exits: ExitRow[] }>('/lifecycle/exits', { status }),
  myExits: () => get<{ exits: ExitRow[]; policy: LifecyclePolicy }>('/lifecycle/exits/mine'),
  resign: (b: { proposedLastWorkingDay: string; reasonCategory?: string | null; reason?: string | null }) => post<{ id: string; noticeDays: number; shortfallDays: number }>('/lifecycle/exits/resign', b),
  recordExit: (b: { employeeId: string; kind: ExitKind; lastWorkingDay: string; voluntary?: boolean; reasonCategory?: string | null; reason?: string | null }) => post<{ id: string }>('/lifecycle/exits', b),
  exit: (id: string) => get<{ exit: ExitRow; blockers: { openTasks: number; assetsHeld: number }; settlement: Settlement | null }>(`/lifecycle/exits/${id}`),
  updateExit: (id: string, b: { lastWorkingDay?: string; voluntary?: boolean; reasonCategory?: string | null; reason?: string | null }) => patch<{ ok: boolean }>(`/lifecycle/exits/${id}`, b),
  withdraw: (id: string) => post<{ ok: boolean }>(`/lifecycle/exits/${id}/withdraw`),
  cancelExit: (id: string, reason: string) => post<{ ok: boolean }>(`/lifecycle/exits/${id}/cancel`, { reason }),
  completeExit: (id: string, skipOpenTasks = false) => post<{ ok: boolean }>(`/lifecycle/exits/${id}/complete`, { skipOpenTasks }),
  questions: () => get<{ questions: ExitQuestion[] }>('/lifecycle/exit-questions'),
  saveQuestions: (questions: Array<{ id?: string; question: string; kind: ExitQuestion['kind']; options: string[]; active: boolean }>) => put<{ ok: boolean }>('/lifecycle/exit-questions', { questions }),
  interview: (exitId: string) => get<{ interview: { answers: InterviewAnswer[]; byEmployee: boolean; createdAt: string; recordedBy: string } | null }>(`/lifecycle/exits/${exitId}/interview`),
  submitInterview: (exitId: string, answers: Array<{ questionId: string; answer: string | number }>) => post<{ ok: boolean }>(`/lifecycle/exits/${exitId}/interview`, { answers }),
  assetTypes: () => get<{ types: AssetType[] }>('/lifecycle/asset-types'),
  createAssetType: (name: string) => post<{ type: AssetType }>('/lifecycle/asset-types', { name }),
  assets: (q: { status?: string; employeeId?: string; search?: string } = {}) => get<{ assets: Asset[] }>('/lifecycle/assets', q),
  myAssets: () => get<{ assets: Asset[] }>('/lifecycle/assets/mine'),
  createAsset: (b: { tag: string; name: string; typeId?: string | null; serial?: string | null; condition?: string; purchaseDate?: string | null; cost?: string | null; notes?: string | null }) => post<{ id: string }>('/lifecycle/assets', b),
  updateAsset: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/lifecycle/assets/${id}`, b),
  assignAsset: (id: string, b: { employeeId: string; assignedOn?: string; notes?: string | null }) => post<{ ok: boolean }>(`/lifecycle/assets/${id}/assign`, b),
  returnAsset: (id: string, b: { condition: string; returnedOn?: string; notes?: string | null }) => post<{ ok: boolean }>(`/lifecycle/assets/${id}/return`, b),
};

export const EXIT_KIND_LABEL: Record<ExitKind, string> = { resignation: 'Resignation', termination: 'Termination', retirement: 'Retirement', contract_end: 'Contract ended', death: 'Death', other: 'Other' };
export const ASSIGNEE_LABEL: Record<AssigneeType, string> = { hr: 'HR', manager: 'Manager', employee: 'Employee', user: 'Named person' };
export const RESIGN_REASONS = ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation', 'Further studies', 'Health or family', 'Other'];
