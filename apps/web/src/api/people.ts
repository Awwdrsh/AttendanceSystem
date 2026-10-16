import { del, get, patch, post, put } from './client';

/** People and core HR: structure, directory, org chart, profile sections, career events, change requests. */

export interface DirectoryPerson {
  id: string; emp_code: string; name_en: string; designation: string | null; grade: string | null; work_email: string | null; work_phone: string | null;
  join_date: string | null; has_avatar: boolean; branch_id: string | null; branch_name: string | null; org_unit_id: string | null; unit_name: string | null;
  manager_id: string | null; manager_name: string | null;
  temp_assignment?: { kind: 'acting' | 'deputation'; label: string | null; until: string } | null;
}
export interface PersonCard extends DirectoryPerson { skills: string[]; languages: string[]; status: string }
export interface OrgNode { id: string; name_en: string; designation: string | null; has_avatar: boolean; unit_name: string | null; manager_id: string | null }
export interface PeopleSummary {
  active: number; joinedLast30Days: number; onProbation: number; leaving: number;
  byUnit: Array<{ id: string; name: string; count: number }>; byBranch: Array<{ id: string; name: string; count: number }>;
}

export interface StructBranch { id: string; code: string; name_en: string; address: string | null; phone: string | null; head_employee_id: string | null; head_name: string | null; headcount: number }
export interface StructUnit {
  id: string; code: string; name_en: string; kind: 'department' | 'team'; parent_id: string | null; branch_id: string | null; description: string | null;
  head_employee_id: string | null; head_name: string | null; headcount: number;
}
export interface StructDesignation { id: string; code: string; name_en: string; grade: string | null; job_level: number | null; org_unit_id: string | null; description: string | null; active: boolean; headcount: number }
export interface Structure { branches: StructBranch[]; units: StructUnit[]; designations: StructDesignation[] }

export type Section = 'personal' | 'contact' | 'bank' | 'ids' | 'emergency' | 'family' | 'education' | 'experience' | 'skills';
export const SECTION_LABEL: Record<Section, string> = {
  personal: 'Personal details', contact: 'Contact and address', bank: 'Bank account', ids: 'ID documents', emergency: 'Emergency contacts',
  family: 'Family and nominees', education: 'Education', experience: 'Previous employment', skills: 'Skills and languages',
};

export interface Address { province?: string | null; district?: string | null; municipality?: string | null; ward?: number | null; street?: string | null }
export interface EmergencyContact { name: string; relation: string; phone: string; altPhone?: string | null; address?: string | null }
export interface FamilyMember { name: string; relation: string; dob?: string | null; occupation?: string | null; phone?: string | null; isDependent: boolean; isNominee: boolean; nomineeShare?: number | null }
export interface Education { level: string; degree?: string | null; field?: string | null; institution: string; board?: string | null; startYear?: number | null; endYear?: number | null; result?: string | null }
export interface Experience { organisation: string; designation?: string | null; fromDate?: string | null; toDate?: string | null; responsibilities?: string | null; reasonForLeaving?: string | null }

export interface Profile {
  personal: { gender: string | null; dob: string | null; maritalStatus: string | null; bloodGroup: string | null; nationality: string | null };
  contact: { personalEmail: string | null; personalPhone: string | null; workPhone: string | null; permanentAddress: Address | null; temporaryAddress: Address | null };
  bank: { bankCode: string | null; bankBranch: string | null; bankAccount: string | null };
  ids: {
    pan: string | null; citizenshipNo: string | null; citizenshipDistrict: string | null; citizenshipIssuedOn: string | null; passportNo: string | null; passportExpiry: string | null;
    licenceNo: string | null; licenceExpiry: string | null; ssfId: string | null; citId: string | null; pfId: string | null;
  };
  emergency: { contacts: EmergencyContact[] };
  family: { members: FamilyMember[] };
  education: { items: Education[] };
  experience: { items: Experience[] };
  skills: { skills: string[]; languages: string[] };
}

export interface FieldChange { field: string; label: string; from: string | null; to: string | null }
export interface ChangeRequest {
  id: string; employee_id: string; requested_by: string; section: Section; sectionLabel: string; summary: { changes?: FieldChange[]; byHr?: boolean };
  reason: string | null; proof_file_id: string | null; status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  decided_at: string | null; decision_reason: string | null; created_at: string;
  employee_name?: string; emp_code?: string; requested_by_name?: string | null; instance_id?: string | null;
}

export type CareerKind = 'hire' | 'confirmation' | 'promotion' | 'transfer' | 'designation_change' | 'grade_change' | 'manager_change'
  | 'employment_type_change' | 'salary_revision' | 'separation' | 'rehire' | 'note' | 'suspension' | 'leave_of_absence' | 'acting' | 'deputation';
export interface CareerEvent {
  id: string; kind: CareerKind; label: string; effectiveDate: string; status: 'scheduled' | 'applied'; summary: string;
  note: string | null; reason: string | null; createdBy: string | null; createdAt: string;
}
export interface CareerDetails {
  designationId?: string; designation?: string; grade?: string; branchId?: string; orgUnitId?: string | null; managerId?: string | null;
  employmentType?: string; separationType?: string; revisionPercent?: number; reason?: string; endDate?: string;
}
export interface HistoryRow { id: string; field: string; old_value: string | null; new_value: string | null; source: string; changed_at: string; changed_by_name: string | null }

export const avatarUrl = (employeeId: string) => `/api/v1/people/${employeeId}/avatar`;
export const proofUrl = (requestId: string) => `/api/v1/people/change-requests/${requestId}/proof`;

export const people = {
  directory: (q: { search?: string; orgUnitId?: string; branchId?: string } = {}) => get<{ people: DirectoryPerson[] }>('/people/directory', q),
  summary: () => get<PeopleSummary>('/people/summary'),
  orgChart: () => get<{ nodes: OrgNode[] }>('/people/org-chart'),
  card: (id: string) => get<{ person: PersonCard; reports: Array<{ id: string; name_en: string; designation: string | null; has_avatar: boolean }> }>(`/people/${id}/card`),

  structure: () => get<Structure>('/people/structure'),
  createBranch: (b: Record<string, unknown>) => post<{ branch: StructBranch }>('/people/structure/branches', b),
  updateBranch: (id: string, b: Record<string, unknown>) => patch<{ branch: StructBranch }>(`/people/structure/branches/${id}`, b),
  deleteBranch: (id: string) => del<{ ok: boolean }>(`/people/structure/branches/${id}`),
  createUnit: (b: Record<string, unknown>) => post<{ unit: StructUnit }>('/people/structure/units', b),
  updateUnit: (id: string, b: Record<string, unknown>) => patch<{ unit: StructUnit }>(`/people/structure/units/${id}`, b),
  deleteUnit: (id: string) => del<{ ok: boolean }>(`/people/structure/units/${id}`),
  createDesignation: (b: Record<string, unknown>) => post<{ designation: StructDesignation }>('/people/structure/designations', b),
  updateDesignation: (id: string, b: Record<string, unknown>) => patch<{ designation: StructDesignation }>(`/people/structure/designations/${id}`, b),
  deleteDesignation: (id: string) => del<{ ok: boolean }>(`/people/structure/designations/${id}`),

  policy: () => get<{ policy: { makerChecker: boolean } }>('/people/policy'),
  setPolicy: (p: { makerChecker: boolean }) => put<{ policy: { makerChecker: boolean } }>('/people/policy', p),

  profile: (id: string, reveal = false) => get<{ profile: Profile; pending: Array<{ id: string; section: Section }> }>(`/people/${id}/profile`, reveal ? { reveal: '1' } : undefined),
  saveSection: (id: string, section: Section, value: unknown) => put<{ pending: boolean; changes?: FieldChange[]; request?: ChangeRequest }>(`/people/${id}/profile/${section}`, value),
  history: (id: string) => get<{ history: HistoryRow[] }>(`/people/${id}/history`),
  career: (id: string) => get<{ events: CareerEvent[] }>(`/people/${id}/career`),
  recordCareer: (id: string, b: { kind: CareerKind; effectiveDate: string; details: CareerDetails; note?: string }) => post<{ event: { id: string; status: string } }>(`/people/${id}/career`, b),
  cancelCareer: (id: string, eventId: string) => post<{ ok: boolean }>(`/people/${id}/career/${eventId}/cancel`),

  requestChange: (b: { section: Section; value: unknown; reason?: string; proofFileId?: string }) => post<{ request: ChangeRequest }>('/people/me/change-requests', b),
  myRequests: () => get<{ requests: ChangeRequest[] }>('/people/me/change-requests'),
  cancelRequest: (id: string) => post<{ ok: boolean }>(`/people/me/change-requests/${id}/cancel`),
  requests: (status: 'pending' | 'approved' | 'rejected' | 'cancelled' | 'all' = 'pending') => get<{ requests: ChangeRequest[] }>('/people/change-requests', { status }),
  request: (id: string, reveal = false) => get<{ request: ChangeRequest; changes: FieldChange[] }>(`/people/change-requests/${id}`, reveal ? { reveal: '1' } : undefined),

  setMyAvatar: (fileId: string) => post<{ ok: boolean }>('/people/me/avatar', { fileId }),
  removeMyAvatar: () => del<{ ok: boolean }>('/people/me/avatar'),
  setAvatar: (id: string, fileId: string) => post<{ ok: boolean }>(`/people/${id}/avatar`, { fileId }),
  removeAvatar: (id: string) => del<{ ok: boolean }>(`/people/${id}/avatar`),
};
