import { del, get, patch, post, put } from './client';

export type Measure = 'number' | 'percent' | 'currency' | 'boolean';
export interface KeyResult { id: string; title: string; measure: Measure; start_value: number; target_value: number; current_value: number; unit: string | null; weight: number }
export interface Goal {
  id: string; scope: 'company' | 'department' | 'individual'; org_unit_id: string | null; org_unit_name: string | null; employee_id: string | null; employee_name: string | null; emp_code: string | null;
  parent_id: string | null; parent_title: string | null; title: string; description: string | null; weight: string; start_date: string | null; due_date: string | null;
  status: 'draft' | 'active' | 'completed' | 'cancelled'; progress: string; key_results: KeyResult[];
  last_checkin: { created_at: string; confidence: number | null; comment: string | null } | null;
}
export interface Checkin { id: string; key_result_id: string | null; key_result_title: string | null; value: string | null; progress: string | null; confidence: number | null; comment: string | null; created_at: string; author_name: string | null }
export interface Kpi { id: string; designation: string | null; title: string; description: string | null; measure: Measure; default_target: string; unit: string | null; active: boolean }
export interface Competency { id: string; kind: 'knowledge' | 'skill' | 'attitude'; name: string; description: string | null; indicators: Array<{ level: number; text: string }>; designations: string[]; grades: string[]; active: boolean }
export interface Colleague { id: string; emp_code: string; name_en: string; designation: string | null; department: string | null; manager_id: string | null }
export type Phase = 'draft' | 'self_review' | 'manager_review' | 'peer_review' | 'calibration' | 'released' | 'closed';
export interface Cycle {
  id: string; name: string; period_start: string; period_end: string; status: Phase; goals_weight: string; competency_weight: string; rating_labels: string[];
  peer_enabled: boolean; peer_min: number; self_due: string | null; manager_due: string | null; peer_due: string | null; calibration_due: string | null;
  filter: { orgUnitIds?: string[]; branchIds?: string[]; joinedBefore?: string }; participants?: number; self_done?: number; manager_done?: number;
}
export interface CycleReviewRow {
  id: string; employee_id: string; emp_code: string; name_en: string; designation: string | null; department: string | null; manager_name: string | null;
  self_submitted_at: string | null; manager_submitted_at: string | null; self_score: string | null; manager_score: string | null; final_rating: string | null;
  released_at: string | null; acknowledged_at: string | null; peers_done: number; peers_total: number; peers_pending: number;
}
export interface ReviewListRow {
  id: string; cycle_id: string; cycle_name: string; cycle_status: Phase; period_start: string; period_end: string; self_due: string | null; manager_due: string | null;
  rating_labels: string[]; employee_id: string; name_en: string; emp_code: string; designation: string | null; self_submitted_at: string | null; manager_submitted_at: string | null;
  released_at: string | null; acknowledged_at: string | null; final_rating: string | null; manager_score: string | null; self_score: string | null;
}
export interface ReviewItem { id: string; item_type: 'goal' | 'competency'; ref_id: string; title: string; detail: string | null; weight: string; self_rating: number | null; self_comment: string | null; manager_rating: number | null; manager_comment: string | null }
export interface Recommendation { incrementPct?: number | null; promotion?: boolean; pip?: boolean; trainingCourseIds?: string[]; notes?: string | null }
export interface PeerNomination { id: string; reviewer_name: string; reviewer_employee_id?: string; status: string; rating?: number | null; strengths?: string | null; improvements?: string | null }
export interface PeerAggregate { count: number; shown: boolean; averageRating?: number | null; strengths?: string[]; improvements?: string[] }
export interface ReviewDetail {
  role: 'self' | 'manager' | 'hr';
  cycle: Cycle;
  review: {
    id: string; employee_id: string; name_en: string; emp_code: string; designation: string | null; manager_name: string | null; self_summary: string | null; self_submitted_at: string | null;
    self_score: string | null; manager_summary: string | null; manager_submitted_at: string | null; manager_score?: string | null; final_rating: string | null; released_at: string | null;
    acknowledged_at: string | null; employee_comment: string | null; recommendation?: Recommendation; calibration_reason?: string | null;
  };
  items: ReviewItem[];
  peers: { nominations: PeerNomination[]; aggregate: PeerAggregate | null };
  journal: JournalEntry[];
  can: { editSelf: boolean; editManager: boolean; nominatePeers: boolean; calibrate: boolean; acknowledge: boolean };
}
export interface PeerRequest { id: string; status: 'approved' | 'submitted'; rating: number | null; strengths: string | null; improvements: string | null; submitted_at: string | null; employee_name: string; designation: string | null; cycle_name: string; cycle_status: Phase; peer_due: string | null; rating_labels: string[] }
export interface JournalEntry { id: string; note_date: string; tag: 'positive' | 'improvement' | 'neutral'; note: string; author_name: string; author_id?: string; created_at?: string }
export interface Feedback {
  id: string; status: 'requested' | 'given' | 'declined'; prompt: string | null; body: string | null; private_to_manager: boolean; created_at: string; given_at: string | null;
  giver_employee_id: string; giver_name: string; subject_employee_id: string; subject_name: string; requester_employee_id: string | null; requester_name: string | null;
}
export interface Meeting { id: string; manager_employee_id: string; manager_name: string; employee_id: string; employee_name: string; scheduled_at: string; cadence: 'none' | 'weekly' | 'biweekly' | 'monthly'; status: 'scheduled' | 'done' | 'cancelled'; side: 'manager' | 'employee'; open_actions: number; agenda_items: number }
export interface MeetingItem { id: string; kind: 'agenda' | 'action'; text: string; owner_employee_id: string | null; owner_name: string | null; done: boolean; created_at: string }
export interface MeetingDetail { meeting: Meeting & { shared_notes: string | null; private_notes: string | null }; items: MeetingItem[]; openActions: Array<{ id: string; text: string; owner_name: string | null; scheduled_at: string }> }

export const performance = {
  departments: () => get<{ departments: Array<{ id: string; code: string; name_en: string }> }>('/performance/departments'),
  colleagues: (q?: string) => get<{ colleagues: Colleague[] }>('/performance/colleagues', { q }),
  goals: (q: { scope: 'mine' | 'team' | 'company' | 'employee' | 'all'; employeeId?: string; status?: string }) => get<{ goals: Goal[] }>('/performance/goals', q),
  goal: (id: string) => get<{ goal: Goal; checkins: Checkin[]; children: Array<{ id: string; title: string; scope: string; progress: string; status: string }>; canEdit: boolean }>(`/performance/goals/${id}`),
  createGoal: (b: Record<string, unknown>) => post<{ id: string }>('/performance/goals', b),
  updateGoal: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/goals/${id}`, b),
  cancelGoal: (id: string) => del<{ ok: true }>(`/performance/goals/${id}`),
  addKeyResult: (goalId: string, b: Record<string, unknown>) => post<{ ok: true }>(`/performance/goals/${goalId}/key-results`, b),
  updateKeyResult: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/key-results/${id}`, b),
  deleteKeyResult: (id: string) => del<{ ok: true }>(`/performance/key-results/${id}`),
  checkin: (goalId: string, b: Record<string, unknown>) => post<{ ok: true }>(`/performance/goals/${goalId}/checkins`, b),
  kpis: (designation?: string) => get<{ kpis: Kpi[] }>('/performance/kpis', { designation }),
  createKpi: (b: Record<string, unknown>) => post<{ id: string }>('/performance/kpis', b),
  updateKpi: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/kpis/${id}`, b),
  deleteKpi: (id: string) => del<{ ok: true }>(`/performance/kpis/${id}`),
  applyKpi: (id: string, b: Record<string, unknown>) => post<{ id: string }>(`/performance/kpis/${id}/apply`, b),
  competencies: (q: { designation?: string; all?: '1' } = {}) => get<{ competencies: Competency[] }>('/performance/competencies', q),
  createCompetency: (b: Record<string, unknown>) => post<{ id: string }>('/performance/competencies', b),
  updateCompetency: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/competencies/${id}`, b),
  cycles: () => get<{ cycles: Cycle[] }>('/performance/cycles'),
  cycle: (id: string) => get<{ cycle: Cycle; reviews: CycleReviewRow[] }>(`/performance/cycles/${id}`),
  createCycle: (b: Record<string, unknown>) => post<{ id: string }>('/performance/cycles', b),
  updateCycle: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/cycles/${id}`, b),
  deleteCycle: (id: string) => del<{ ok: true }>(`/performance/cycles/${id}`),
  launchCycle: (id: string) => post<{ ok: true; participants: number }>(`/performance/cycles/${id}/launch`),
  advanceCycle: (id: string) => post<{ ok: true; status: Phase }>(`/performance/cycles/${id}/advance`),
  calibration: (id: string) => get<{ cycle: Cycle; reviews: Array<{ id: string; employee_id: string; name_en: string; emp_code: string; department: string; self_score: string | null; manager_score: string | null; final_rating: string | null; calibration_reason: string | null; recommendation: Recommendation }>; distribution: { overall: number[]; byDepartment: Record<string, number[]> } }>(`/performance/cycles/${id}/calibration`),
  reviews: (scope: 'mine' | 'team') => get<{ reviews: ReviewListRow[] }>('/performance/reviews', { scope }),
  review: (id: string) => get<ReviewDetail>(`/performance/reviews/${id}`),
  saveSelf: (id: string, b: Record<string, unknown>) => put<{ ok: true }>(`/performance/reviews/${id}/self`, b),
  saveManager: (id: string, b: Record<string, unknown>) => put<{ ok: true }>(`/performance/reviews/${id}/manager`, b),
  calibrate: (id: string, finalRating: number, reason: string) => post<{ ok: true }>(`/performance/reviews/${id}/calibrate`, { finalRating, reason }),
  acknowledge: (id: string, comment?: string) => post<{ ok: true }>(`/performance/reviews/${id}/acknowledge`, { comment }),
  printUrl: (id: string) => `/api/v1/performance/reviews/${id}/print`,
  nominatePeers: (reviewId: string, employeeIds: string[]) => post<{ ok: true; added: number }>(`/performance/reviews/${reviewId}/peers`, { employeeIds }),
  decidePeer: (id: string, approve: boolean) => post<{ ok: true }>(`/performance/peers/${id}/decision`, { approve }),
  removePeer: (id: string) => del<{ ok: true }>(`/performance/peers/${id}`),
  peerRequests: () => get<{ requests: PeerRequest[] }>('/performance/peer-requests'),
  submitPeer: (id: string, b: Record<string, unknown>) => put<{ ok: true }>(`/performance/peers/${id}`, b),
  journal: (employeeId: string) => get<{ entries: JournalEntry[] }>('/performance/journal', { employeeId }),
  addJournal: (b: Record<string, unknown>) => post<{ id: string }>('/performance/journal', b),
  deleteJournal: (id: string) => del<{ ok: true }>(`/performance/journal/${id}`),
  feedback: (box: 'received' | 'given' | 'requests' | 'about', employeeId?: string) => get<{ feedback: Feedback[] }>('/performance/feedback', { box, employeeId }),
  giveFeedback: (b: Record<string, unknown>) => post<{ ok: true }>('/performance/feedback', b),
  requestFeedback: (b: Record<string, unknown>) => post<{ ok: true }>('/performance/feedback/requests', b),
  answerFeedback: (id: string, b: Record<string, unknown>) => put<{ ok: true }>(`/performance/feedback/${id}`, b),
  declineFeedback: (id: string) => post<{ ok: true }>(`/performance/feedback/${id}/decline`),
  meetings: () => get<{ meetings: Meeting[]; reports: Array<{ id: string; name_en: string; designation: string | null }>; manager: { id: string; name_en: string; designation: string | null } | null }>('/performance/one-on-ones'),
  meeting: (id: string) => get<MeetingDetail>(`/performance/one-on-ones/${id}`),
  createMeeting: (b: Record<string, unknown>) => post<{ id: string }>('/performance/one-on-ones', b),
  updateMeeting: (id: string, b: Record<string, unknown>) => patch<{ ok: true; nextId: string | null }>(`/performance/one-on-ones/${id}`, b),
  addMeetingItem: (id: string, b: Record<string, unknown>) => post<{ id: string }>(`/performance/one-on-ones/${id}/items`, b),
  updateMeetingItem: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/performance/one-on-one-items/${id}`, b),
  deleteMeetingItem: (id: string) => del<{ ok: true }>(`/performance/one-on-one-items/${id}`),
};

// ---------------------------------------------------------------- learning
export interface Course {
  id: string; code: string; title: string; description: string | null; category: string | null; delivery: 'online' | 'classroom' | 'blended'; source: 'internal' | 'external';
  provider: string | null; duration_hours: string | null; url: string | null; cost: string; mandatory: boolean; mandatory_for: string[]; validity_months: number | null;
  pass_mark: number | null; active: boolean; questions?: number; completed?: number; enrolled?: number; next_session?: string | null;
}
export interface Session { id: string; course_id: string; starts_at: string; ends_at: string; venue: string | null; trainer: string | null; capacity: number | null; status: 'scheduled' | 'completed' | 'cancelled'; taken: number }
export interface Question { id?: string; prompt: string; options: string[]; correct_index?: number }
export interface Enrollment {
  id: string; course_id: string; code: string; title: string; delivery: Course['delivery']; mandatory: boolean; pass_mark: number | null; validity_months: number | null;
  session_id: string | null; starts_at: string | null; ends_at: string | null; venue: string | null; employee_id: string; name_en: string; emp_code: string;
  source: 'self' | 'assigned' | 'review' | 'mandatory'; status: 'pending_approval' | 'enrolled' | 'completed' | 'rejected' | 'cancelled' | 'no_show';
  attended: boolean | null; completed_at: string | null; score: string | null; passed: boolean | null; feedback_rating: number | null; feedback_comment: string | null;
  certificate_file_id: string | null; certificate_issued_on: string | null; certificate_expires_on: string | null; cost: string; reason: string | null; created_at: string;
  questions: number; attempts: number;
}

export const learning = {
  courses: () => get<{ courses: Course[] }>('/learning/courses'),
  course: (id: string) => get<{ course: Course; sessions: Session[]; questions: Question[]; questionCount: number }>(`/learning/courses/${id}`),
  createCourse: (b: Record<string, unknown>) => post<{ id: string }>('/learning/courses', b),
  updateCourse: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/learning/courses/${id}`, b),
  saveQuestions: (id: string, questions: Array<{ prompt: string; options: string[]; correctIndex: number }>) => put<{ ok: true }>(`/learning/courses/${id}/questions`, { questions }),
  addSession: (courseId: string, b: Record<string, unknown>) => post<{ id: string }>(`/learning/courses/${courseId}/sessions`, b),
  updateSession: (id: string, b: Record<string, unknown>) => patch<{ ok: true }>(`/learning/sessions/${id}`, b),
  enrollments: (q: { scope: 'mine' | 'team' | 'all'; courseId?: string; status?: string }) => get<{ enrollments: Enrollment[] }>('/learning/enrollments', q),
  selfEnroll: (b: Record<string, unknown>) => post<{ id: string }>('/learning/enrollments/self', b),
  assign: (b: Record<string, unknown>) => post<{ added: number; skipped: number }>('/learning/enrollments/assign', b),
  assignMandatory: (courseId: string) => post<{ ok: true; added: number }>(`/learning/courses/${courseId}/assign-mandatory`),
  cancel: (id: string) => post<{ ok: true }>(`/learning/enrollments/${id}/cancel`),
  attendance: (id: string, attended: boolean) => post<{ ok: true }>(`/learning/enrollments/${id}/attendance`, { attended }),
  complete: (id: string) => post<{ ok: true }>(`/learning/enrollments/${id}/complete`),
  assessment: (id: string) => get<{ title: string; passMark: number | null; questions: Question[]; attempts: Array<{ score: string; passed: boolean; created_at: string }>; attemptsLeft: number }>(`/learning/enrollments/${id}/assessment`),
  attempt: (id: string, answers: number[]) => post<{ score: number; passed: boolean; correct: number; total: number; attemptsLeft: number }>(`/learning/enrollments/${id}/attempts`, { answers }),
  feedback: (id: string, rating: number, comment?: string) => post<{ ok: true }>(`/learning/enrollments/${id}/feedback`, { rating, comment }),
  attachCertificate: (id: string, b: Record<string, unknown>) => post<{ ok: true }>(`/learning/enrollments/${id}/certificate`, b),
  certificateFileUrl: (id: string) => `/api/v1/learning/enrollments/${id}/certificate-file`,
  certificateUrl: (id: string) => `/api/v1/learning/enrollments/${id}/certificate`,
  expiring: (days = 60) => get<{ enrollments: Enrollment[] }>('/learning/expiring', { days }),
  costReport: (from: string, to: string) => get<{ byCourse: Array<{ id: string; code: string; title: string; seats: number; completed: number; cost: string; rating: string | null }>; byDepartment: Array<{ department: string; seats: number; cost: string }>; total: string }>('/learning/reports/cost', { from, to }),
};
