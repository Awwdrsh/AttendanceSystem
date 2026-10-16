import { del, get, patch, post, put } from './client';

export type GrievanceCategory = 'harassment' | 'discrimination' | 'safety' | 'pay_benefits' | 'management' | 'policy' | 'other';
export type GrievanceStatus = 'submitted' | 'under_review' | 'hearing_scheduled' | 'decided' | 'appealed' | 'closed' | 'withdrawn';
export type GrievanceOutcome = 'upheld' | 'partly_upheld' | 'not_upheld';

export const GRIEVANCE_CATEGORY_LABEL: Record<GrievanceCategory, string> = {
  harassment: 'Harassment or bullying', discrimination: 'Discrimination', safety: 'Health and safety', pay_benefits: 'Pay and benefits',
  management: 'Treatment by a manager', policy: 'Company policy or practice', other: 'Something else',
};
export const GRIEVANCE_STATUS_LABEL: Record<GrievanceStatus, string> = {
  submitted: 'Submitted', under_review: 'Under review', hearing_scheduled: 'Hearing set', decided: 'Decided', appealed: 'Appealed', closed: 'Closed', withdrawn: 'Withdrawn',
};
export const OUTCOME_LABEL: Record<GrievanceOutcome, string> = { upheld: 'Upheld', partly_upheld: 'Partly upheld', not_upheld: 'Not upheld' };

export interface Grievance {
  id: string; number: number; category: GrievanceCategory; status: GrievanceStatus; subject: string | null; description: string | null; concerns: string | null;
  employeeId: string; employeeName: string | null; empCode: string | null; hearingAt: string | null; hearingPlace: string | null; outcome: GrievanceOutcome | null;
  findings: string | null; decidedAt: string | null; appealReason: string | null; appealedAt: string | null; appealOutcome: 'decision_upheld' | 'decision_changed' | null;
  appealNote: string | null; appealDecidedAt: string | null; createdAt: string; updatedAt: string; role: 'complainant' | 'committee' | 'hr'; excludedUserIds: string[];
}
export interface GrievanceNote { id: string; authorName: string; mine: boolean; fromComplainant: boolean; shared: boolean; body: string; createdAt: string }
export interface GrievanceDetail { grievance: Grievance; notes: GrievanceNote[]; files: Array<{ id: string; filename: string; mime: string; bytes: number }>; canAppeal: boolean; appealUntil: string | null }
export interface CommitteeMember { user_id: string; name: string; email: string; added_at: string }

export type CaseCategory = 'attendance' | 'conduct' | 'performance' | 'policy' | 'safety' | 'misconduct' | 'other';
export const CASE_CATEGORY_LABEL: Record<CaseCategory, string> = {
  attendance: 'Attendance', conduct: 'Conduct', performance: 'Performance', policy: 'Policy breach', safety: 'Safety', misconduct: 'Misconduct', other: 'Other',
};
export type CaseStatus = 'open' | 'show_cause' | 'responded' | 'decided' | 'withdrawn';
export const CASE_STATUS_LABEL: Record<CaseStatus, string> = { open: 'Under investigation', show_cause: 'Waiting for response', responded: 'Response received', decided: 'Decided', withdrawn: 'Withdrawn' };
export interface Stage { id: string; code: string; name: string; severity: number; effect: 'none' | 'suspension' | 'termination'; letter_template_code: string | null; sort: number; active: boolean }
export interface DisciplinaryCase {
  id: string; number: number; employeeId: string; employeeName: string; empCode: string; category: CaseCategory; incidentDate: string; incident: string; status: CaseStatus;
  showCause: string | null; showCauseAt: string | null; responseDue: string | null; response: string | null; respondedAt: string | null; stageId: string | null;
  stageName: string | null; stageEffect: string | null; stageLetter: string | null; decisionNote: string | null; suspensionFrom: string | null; suspensionTo: string | null;
  letterId: string | null; letterRef: string | null; exitId: string | null; decidedAt: string | null; createdAt: string; updatedAt: string;
}

export interface Milestone { kind: 'service' | 'contract_end' | 'retirement' | 'probation_end' | 'assignment_end' | 'exit'; employeeId: string; empCode: string; name: string; date: string; detail: string }
export interface RelationsPolicy { retirementAge: number; serviceYears: number[]; registeredDevicesOnly: boolean; appealDays: number }

export interface TempAssignment {
  id: string; employee_id: string; employee_name: string; emp_code: string; kind: 'acting' | 'deputation'; designation: string | null; designation_id: string | null;
  branch_id: string | null; branch_name: string | null; org_unit_id: string | null; unit_name: string | null; start_date: string; end_date: string; note: string | null;
  status: 'active' | 'ended' | 'cancelled'; created_at: string;
}

export type KyeSection = 'personal' | 'contact' | 'emergency' | 'family' | 'education' | 'experience' | 'ids' | 'bank' | 'photo';
export const KYE_SECTION_LABEL: Record<KyeSection, string> = {
  personal: 'Personal details', contact: 'Contact and address', emergency: 'Emergency contacts', family: 'Family and nominees', education: 'Education',
  experience: 'Previous employment', ids: 'ID documents', bank: 'Bank account', photo: 'Profile photo',
};
export interface KyeCampaign {
  id: string; title: string; message: string | null; sections: KyeSection[]; audience_kind: 'all' | 'branch' | 'department'; audience_id: string | null; due_date: string;
  status: 'open' | 'closed'; created_at: string; closed_at: string | null; total: number; confirmed: number; changes_requested: number;
}
export interface KyeResponse { employee_id: string; name_en: string; emp_code: string; status: 'pending' | 'confirmed' | 'changes_requested'; note: string | null; responded_at: string | null; last_nudged_at: string | null; completeness: number | null; missing: KyeSection[] }
export interface Completeness { employeeId: string; empCode: string; name: string; missing: KyeSection[]; percent: number }
export interface MyKyeCampaign { id: string; title: string; message: string | null; sections: KyeSection[]; due_date: string; status: 'pending' | 'confirmed' | 'changes_requested'; responded_at: string | null }

export interface Popup { id: string; title: string; body: string; requiresAck: boolean; createdAt: string; authorName: string }
export interface Device { id: string; label: string; status: 'pending' | 'approved' | 'rejected' | 'revoked'; requested_at: string; decided_at: string | null; last_used_at: string | null }
export interface DeviceRow extends Device { employee_id: string; employee_name: string; emp_code: string; user_agent: string | null }

export const relations = {
  policy: () => get<{ policy: RelationsPolicy }>('/relations/policy'),
  savePolicy: (p: RelationsPolicy) => put<{ policy: RelationsPolicy }>('/relations/policy', p),
  summary: () => get<{ openGrievances: number; openCases: number; pendingDevices: number; openKye: number }>('/relations/summary'),

  committee: () => get<{ members: CommitteeMember[]; isMember: boolean }>('/relations/grievances/committee'),
  setCommittee: (userIds: string[]) => put<{ ok: boolean }>('/relations/grievances/committee', { userIds }),
  fileGrievance: (b: { category: GrievanceCategory; subject: string; description: string; concerns?: string | null; fileIds: string[] }) => post<{ id: string; number: number }>('/relations/grievances', b),
  grievances: (scope: 'mine' | 'handle', status?: string) => get<{ grievances: Grievance[] }>('/relations/grievances', { scope, status }),
  grievance: (id: string) => get<GrievanceDetail>(`/relations/grievances/${id}`),
  grievanceFileUrl: (id: string, fileId: string) => `/api/v1/relations/grievances/${id}/files/${fileId}`,
  addNote: (id: string, body: string, shared: boolean) => post<{ id: string }>(`/relations/grievances/${id}/notes`, { body, shared }),
  updateGrievance: (id: string, b: { status?: 'under_review' | 'hearing_scheduled'; hearingAt?: string | null; hearingPlace?: string | null; excludeUserIds?: string[] }) => patch<{ ok: boolean }>(`/relations/grievances/${id}`, b),
  decide: (id: string, b: { outcome: GrievanceOutcome; findings: string }) => post<{ ok: boolean }>(`/relations/grievances/${id}/decision`, b),
  appeal: (id: string, reason: string) => post<{ ok: boolean }>(`/relations/grievances/${id}/appeal`, { reason }),
  decideAppeal: (id: string, b: { outcome: 'decision_upheld' | 'decision_changed'; note: string; newOutcome?: GrievanceOutcome }) => post<{ ok: boolean }>(`/relations/grievances/${id}/appeal-decision`, b),
  withdraw: (id: string) => post<{ ok: boolean }>(`/relations/grievances/${id}/withdraw`),
  close: (id: string) => post<{ ok: boolean }>(`/relations/grievances/${id}/close`),

  stages: () => get<{ stages: Stage[] }>('/relations/disciplinary/stages'),
  saveStages: (stages: Array<Omit<Stage, 'id' | 'sort' | 'letter_template_code'> & { id?: string; letterTemplateCode?: string | null }>) => put<{ ok: boolean }>('/relations/disciplinary/stages', { stages }),
  cases: (q: { employeeId?: string; status?: string } = {}) => get<{ cases: DisciplinaryCase[] }>('/relations/disciplinary', q),
  myCases: () => get<{ cases: DisciplinaryCase[] }>('/relations/disciplinary/mine'),
  case: (id: string) => get<{ case: DisciplinaryCase }>(`/relations/disciplinary/${id}`),
  openCase: (b: { employeeId: string; category: CaseCategory; incidentDate: string; incident: string }) => post<{ id: string; number: number }>('/relations/disciplinary', b),
  showCause: (id: string, b: { text: string; responseDue: string }) => post<{ ok: boolean }>(`/relations/disciplinary/${id}/show-cause`, b),
  respond: (id: string, response: string) => post<{ ok: boolean }>(`/relations/disciplinary/${id}/respond`, { response }),
  decideCase: (id: string, b: { stageId: string | null; note: string; suspensionFrom?: string; suspensionTo?: string; lastWorkingDay?: string; force?: boolean }) => post<{ ok: boolean }>(`/relations/disciplinary/${id}/decide`, b),
  linkLetter: (id: string, letterId: string) => post<{ ok: boolean }>(`/relations/disciplinary/${id}/letter`, { letterId }),
  withdrawCase: (id: string, reason: string) => post<{ ok: boolean }>(`/relations/disciplinary/${id}/withdraw`, { reason }),

  milestones: (days: number) => get<{ milestones: Milestone[]; policy: RelationsPolicy }>('/relations/milestones', { days }),
  separating: () => get<{ people: Milestone[] }>('/relations/separating'),
  contract: (employeeId: string) => get<{ contractEnd: string | null; retirementDate: string | null; retirementAge: number }>(`/relations/employees/${employeeId}/contract`),
  setContract: (employeeId: string, contractEnd: string | null) => put<{ ok: boolean }>(`/relations/employees/${employeeId}/contract`, { contractEnd }),

  assignments: (q: { employeeId?: string; status?: 'active' | 'ended' | 'cancelled' | 'all' } = {}) => get<{ assignments: TempAssignment[] }>('/relations/assignments', q),
  addAssignment: (b: { employeeId: string; kind: 'acting' | 'deputation'; designationId?: string | null; designation?: string | null; branchId?: string | null; orgUnitId?: string | null; startDate: string; endDate: string; note?: string | null }) =>
    post<{ id: string }>('/relations/assignments', b),
  endAssignment: (id: string, b: { endDate?: string; cancel?: boolean }) => post<{ ok: boolean }>(`/relations/assignments/${id}/end`, b),

  campaigns: () => get<{ campaigns: KyeCampaign[] }>('/relations/kye/campaigns'),
  campaign: (id: string) => get<{ campaign: KyeCampaign; responses: KyeResponse[] }>(`/relations/kye/campaigns/${id}`),
  launchCampaign: (b: { title: string; message?: string | null; sections: KyeSection[]; audienceKind: 'all' | 'branch' | 'department'; audienceId?: string | null; dueDate: string }) =>
    post<{ id: string; employees: number }>('/relations/kye/campaigns', b),
  nudge: (id: string) => post<{ nudged: number }>(`/relations/kye/campaigns/${id}/nudge`),
  closeCampaign: (id: string) => post<{ ok: boolean }>(`/relations/kye/campaigns/${id}/close`),
  myKye: () => get<{ campaigns: MyKyeCampaign[]; completeness: Completeness | null }>('/relations/kye/mine'),
  respondKye: (id: string, status: 'confirmed' | 'changes_requested', note?: string | null) => post<{ ok: boolean }>(`/relations/kye/campaigns/${id}/respond`, { status, note }),
  completeness: () => get<{ employees: Completeness[] }>('/relations/completeness'),

  popups: () => get<{ popups: Popup[] }>('/relations/popups'),
  dismissPopup: (id: string, acknowledge: boolean) => post<{ ok: boolean }>(`/relations/popups/${id}/dismiss`, { acknowledge }),

  myDevices: () => get<{ devices: Device[]; required: boolean }>('/relations/devices/mine'),
  registerDevice: (token: string, label: string) => post<{ id: string; status: string }>('/relations/devices/register', { token, label }),
  removeDevice: (id: string) => del<{ ok: boolean }>(`/relations/devices/mine/${id}`),
  devices: (status: 'pending' | 'approved' | 'rejected' | 'revoked' | 'all' = 'all') => get<{ devices: DeviceRow[] }>('/relations/devices', { status }),
  decideDevice: (id: string, action: 'approve' | 'reject' | 'revoke') => post<{ ok: boolean }>(`/relations/devices/${id}/${action}`),
};
