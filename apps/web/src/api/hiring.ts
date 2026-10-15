import { get, patch, post, put } from './client';

export type WorkMode = 'onsite' | 'remote' | 'hybrid';
export type EmploymentType = 'full_time' | 'part_time' | 'contract' | 'internship' | 'temporary';
export type Recommendation = 'strong_no' | 'no' | 'yes' | 'strong_yes';

export interface JobQuestion {
  id: string; kind: 'yes_no' | 'number' | 'text'; label: string; required: boolean;
  knockout?: { equals?: 'yes' | 'no'; min?: number }; preferred?: { equals?: 'yes' | 'no'; min?: number };
}

export interface Requisition {
  id: string; title: string; designation: string | null; org_unit_id: string | null; branch_id: string | null; headcount: number; filled: number;
  salary_min: string | null; salary_max: string | null; kind: 'new' | 'replacement'; justification: string; status: string; created_at: string;
  decided_at: string | null; decision_reason: string | null; department: string | null; branch: string | null; raised_by: string; raised_by_name: string | null;
  replacing_name: string | null; jobs: number;
}

export interface Job {
  id: string; requisition_id: string | null; title: string; description: string; requirements: string; category: string | null; location: string | null;
  work_mode: WorkMode; employment_type: EmploymentType; experience_min: string | null; experience_max: string | null; salary_min: string | null; salary_max: string | null;
  show_salary: boolean; openings: number; deadline: string | null; org_unit_id: string | null; branch_id: string | null; hiring_manager_id: string | null;
  stages: string[]; questions: JobQuestion[]; status: 'draft' | 'open' | 'closed'; published_at: string | null; closed_at: string | null; created_at: string;
  department: string | null; hiring_manager_name: string | null;
  applications?: number; active?: number; new_this_week?: number; hired?: number;
}

export interface BoardCard {
  id: string; stage: string; status: string; source: string; score: number | null; knocked_out: boolean; rating: number | null; tags: string[];
  experience_years: string | null; current_employer: string | null; created_at: string; stage_changed_at: string; rejected_reason: string | null;
  owner_user_id: string | null; owner_name: string | null; candidate_id: string; name: string; email: string | null; phone: string | null; has_cv: boolean;
  other_applications: number; next_interview: string | null;
}

export interface ScoreItem { label: string; points: number; max: number; note: string }

export interface Interview {
  id: string; application_id?: string; title: string; starts_at: string; duration_min: number; mode: 'in_person' | 'phone' | 'video'; location: string | null;
  panel: string[]; panel_names: string[] | null; status: 'scheduled' | 'completed' | 'cancelled';
  candidate?: string; job?: string; job_id?: string; on_panel?: boolean; my_scorecard?: boolean; scorecards?: number;
}

export interface Scorecard { id: string; interview_id: string; reviewer_id: string; reviewer: string; ratings: Record<string, number>; recommendation: Recommendation; notes: string | null; submitted_at: string }

export interface Offer { id: string; designation: string; salary: string; join_date: string; expires_on: string | null; notes: string | null; status: string; created_at: string; decided_at: string | null; responded_at: string | null }

export interface HiringEvent { id: string; kind: string; body: string | null; data: Record<string, unknown>; created_at: string; actor: string | null }

export interface ApplicationDetail {
  access: 'full' | 'panel';
  application: {
    id: string; job_id: string; candidate_id: string; stage: string; status: string; source: string; cover_note: string | null; answers: Record<string, string | number>;
    experience_years: string | null; current_employer: string | null; score: number | null; score_detail: ScoreItem[]; knocked_out: boolean; rating: number | null;
    tags: string[]; owner_user_id: string | null; owner_name: string | null; rejected_reason: string | null; closed_at: string | null; hired_employee_id: string | null;
    consent_at: string | null; created_at: string; has_cv: boolean; name: string; email: string | null; phone: string | null; purged_at: string | null;
  };
  job: { id: string; title: string; stages: string[]; questions: JobQuestion[]; status: string; requisitionId: string | null };
  otherApplications: Array<{ id: string; stage: string; status: string; created_at: string; title: string }>;
  events: HiringEvent[]; interviews: Interview[]; scorecards: Scorecard[]; offers: Offer[];
}

export interface Lookups {
  users: Array<{ id: string; name: string; roles: string[] }>;
  orgUnits: Array<{ id: string; name_en: string; kind: string }>;
  branches: Array<{ id: string; name_en: string }>;
  shifts: Array<{ id: string; code: string; name_en: string }>;
}

export interface HiringReport {
  totals: { applications: number; active: number; hired: number; rejected: number };
  funnel: Array<{ jobId: string; title: string; status: string; total: number; rejected: number; stages: Array<{ stage: string; reached: number }> }>;
  sources: Array<{ source: string; applications: number; hired: number }>;
  timeToHire: { averageDays: number | null; medianDays: number | null; hires: number };
}

export interface HiringPolicy { retentionDays: number; careersEnabled: boolean }

export interface PublicJob {
  id: string; title: string; description: string; requirements: string; category: string | null; location: string | null; work_mode: WorkMode;
  employment_type: EmploymentType; experience_min: string | null; experience_max: string | null; salary_min: string | null; salary_max: string | null;
  openings: number; deadline: string | null; published_at: string | null; department: string | null;
  questions?: Array<Pick<JobQuestion, 'id' | 'kind' | 'label' | 'required'>>;
}

const enc = encodeURIComponent;

export const hiring = {
  lookups: () => get<Lookups>('/hiring/lookups'),
  requisitions: () => get<{ requisitions: Requisition[] }>('/hiring/requisitions'),
  createRequisition: (b: Record<string, unknown>) => post<{ id: string }>('/hiring/requisitions', b),
  cancelRequisition: (id: string) => post<{ ok: boolean }>(`/hiring/requisitions/${id}/cancel`),
  jobs: (status?: string) => get<{ jobs: Job[]; companyCode: string | null }>('/hiring/jobs', { status }),
  job: (id: string) => get<{ job: Job; stageCounts: Record<string, number> }>(`/hiring/jobs/${id}`),
  createJob: (b: Record<string, unknown>) => post<{ id: string }>('/hiring/jobs', b),
  updateJob: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/hiring/jobs/${id}`, b),
  publishJob: (id: string) => post<{ ok: boolean }>(`/hiring/jobs/${id}/publish`),
  closeJob: (id: string) => post<{ ok: boolean }>(`/hiring/jobs/${id}/close`),
  board: (jobId: string, include: 'active' | 'all' = 'active') => get<{ applications: BoardCard[]; stages: string[] }>(`/hiring/jobs/${jobId}/applications`, { include }),
  addCandidate: (jobId: string, b: Record<string, unknown>) => post<{ id: string }>(`/hiring/jobs/${jobId}/applications`, b),
  application: (id: string) => get<ApplicationDetail>(`/hiring/applications/${id}`),
  cvUrl: (id: string) => `/api/v1/hiring/applications/${id}/cv`,
  exportUrl: (id: string) => `/api/v1/hiring/applications/${id}/export`,
  update: (id: string, b: { rating?: number | null; tags?: string[]; ownerUserId?: string | null }) => patch<{ ok: boolean }>(`/hiring/applications/${id}`, b),
  move: (id: string, stage: string) => post<{ ok: boolean }>(`/hiring/applications/${id}/move`, { stage }),
  reject: (id: string, reason: string, notifyCandidate: boolean) => post<{ ok: boolean; emailed: boolean }>(`/hiring/applications/${id}/reject`, { reason, notifyCandidate }),
  reopen: (id: string) => post<{ ok: boolean }>(`/hiring/applications/${id}/reopen`),
  note: (id: string, body: string) => post<{ ok: boolean }>(`/hiring/applications/${id}/notes`, { body }),
  scheduleInterview: (id: string, b: Record<string, unknown>) => post<{ id: string }>(`/hiring/applications/${id}/interviews`, b),
  interviews: (q: { scope: 'mine' | 'all'; from?: string; to?: string }) => get<{ interviews: Interview[] }>('/hiring/interviews', q),
  updateInterview: (id: string, b: Record<string, unknown>) => patch<{ ok: boolean }>(`/hiring/interviews/${id}`, b),
  scorecard: (id: string, b: { ratings: Record<string, number>; recommendation: Recommendation; notes?: string | null }) => post<{ ok: boolean }>(`/hiring/interviews/${id}/scorecard`, b),
  createOffer: (id: string, b: Record<string, unknown>) => post<{ id: string }>(`/hiring/applications/${id}/offers`, b),
  offerLetterUrl: (id: string) => `/api/v1/hiring/offers/${id}/letter`,
  respondOffer: (id: string, response: 'accepted' | 'declined') => post<{ ok: boolean }>(`/hiring/offers/${id}/respond`, { response }),
  withdrawOffer: (id: string) => post<{ ok: boolean }>(`/hiring/offers/${id}/withdraw`),
  hire: (id: string, b: Record<string, unknown>) => post<{ employeeId: string }>(`/hiring/applications/${id}/hire`, b),
  reports: (q: { from?: string; to?: string } = {}) => get<HiringReport>('/hiring/reports', q),
  policy: () => get<{ policy: HiringPolicy }>('/hiring/policy'),
  setPolicy: (b: HiringPolicy) => put<{ policy: HiringPolicy }>('/hiring/policy', b),
};

export const careers = {
  jobs: (q: { company?: string | null; q?: string | undefined; workMode?: string | null; category?: string | null }) =>
    get<{ company: { name: string; code: string }; jobs: PublicJob[]; categories: string[] }>('/public/careers/jobs', q),
  job: (id: string) => get<{ company: { name: string; code: string }; job: PublicJob }>(`/public/careers/jobs/${enc(id)}`),
  apply: (id: string, b: Record<string, unknown>) => post<{ ok: boolean; message: string }>(`/public/careers/jobs/${enc(id)}/apply`, b),
};

export const WORK_MODE: Record<WorkMode, string> = { onsite: 'On site', remote: 'Remote', hybrid: 'Hybrid' };
export const EMPLOYMENT: Record<EmploymentType, string> = { full_time: 'Full-time', part_time: 'Part-time', contract: 'Contract', internship: 'Internship', temporary: 'Temporary' };
export const SOURCE_LABEL: Record<string, string> = { careers: 'Careers page', referral: 'Referral', manual: 'Added by HR', job_board: 'Job board', other: 'Other' };
export const RECOMMENDATION: Record<Recommendation, { label: string; color: string }> = {
  strong_no: { label: 'Strong no', color: 'red' }, no: { label: 'No', color: 'orange' }, yes: { label: 'Yes', color: 'teal' }, strong_yes: { label: 'Strong yes', color: 'green' },
};
export const MODE_LABEL: Record<Interview['mode'], string> = { in_person: 'In person', phone: 'Phone', video: 'Video' };

/** "2–5 years", "3+ years" or null when the job sets no experience range. */
export function experienceText(min: string | null, max: string | null): string | null {
  if (min === null && max === null) return null;
  const f = (x: string) => String(Number(x));
  if (min !== null && max !== null) return `${f(min)}–${f(max)} years`;
  return min !== null ? `${f(min)}+ years` : `Up to ${f(max!)} years`;
}
