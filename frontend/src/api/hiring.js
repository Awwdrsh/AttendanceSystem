import { get, patch, post, put } from './client';

const enc = encodeURIComponent;

export const hiring = {
  lookups: () => get('/hiring/lookups'),
  requisitions: () => get('/hiring/requisitions'),
  createRequisition: (b) => post('/hiring/requisitions', b),
  cancelRequisition: (id) => post(`/hiring/requisitions/${id}/cancel`),
  jobs: (status) => get('/hiring/jobs', { status }),
  job: (id) => get(`/hiring/jobs/${id}`),
  createJob: (b) => post('/hiring/jobs', b),
  updateJob: (id, b) => patch(`/hiring/jobs/${id}`, b),
  publishJob: (id) => post(`/hiring/jobs/${id}/publish`),
  closeJob: (id) => post(`/hiring/jobs/${id}/close`),
  board: (jobId, include = 'active') => get(`/hiring/jobs/${jobId}/applications`, { include }),
  addCandidate: (jobId, b) => post(`/hiring/jobs/${jobId}/applications`, b),
  application: (id) => get(`/hiring/applications/${id}`),
  cvUrl: (id) => `/api/v1/hiring/applications/${id}/cv`,
  exportUrl: (id) => `/api/v1/hiring/applications/${id}/export`,
  update: (id, b) => patch(`/hiring/applications/${id}`, b),
  move: (id, stage) => post(`/hiring/applications/${id}/move`, { stage }),
  reject: (id, reason, notifyCandidate) => post(`/hiring/applications/${id}/reject`, { reason, notifyCandidate }),
  reopen: (id) => post(`/hiring/applications/${id}/reopen`),
  note: (id, body) => post(`/hiring/applications/${id}/notes`, { body }),
  scheduleInterview: (id, b) => post(`/hiring/applications/${id}/interviews`, b),
  interviews: (q) => get('/hiring/interviews', q),
  updateInterview: (id, b) => patch(`/hiring/interviews/${id}`, b),
  scorecard: (id, b) => post(`/hiring/interviews/${id}/scorecard`, b),
  createOffer: (id, b) => post(`/hiring/applications/${id}/offers`, b),
  offerLetterUrl: (id) => `/api/v1/hiring/offers/${id}/letter`,
  respondOffer: (id, response) => post(`/hiring/offers/${id}/respond`, { response }),
  withdrawOffer: (id) => post(`/hiring/offers/${id}/withdraw`),
  hire: (id, b) => post(`/hiring/applications/${id}/hire`, b),
  reports: (q = {}) => get('/hiring/reports', q),
  policy: () => get('/hiring/policy'),
  setPolicy: (b) => put('/hiring/policy', b),
};

export const careers = {
  jobs: (q) => get('/public/careers/jobs', q),
  job: (id) => get(`/public/careers/jobs/${enc(id)}`),
  apply: (id, b) => post(`/public/careers/jobs/${enc(id)}/apply`, b),
};

export const WORK_MODE = { onsite: 'On site', remote: 'Remote', hybrid: 'Hybrid' };
export const EMPLOYMENT = {
  full_time: 'Full-time',
  part_time: 'Part-time',
  contract: 'Contract',
  internship: 'Internship',
  temporary: 'Temporary',
};
export const SOURCE_LABEL = {
  careers: 'Careers page',
  referral: 'Referral',
  manual: 'Added by HR',
  job_board: 'Job board',
  other: 'Other',
};
export const RECOMMENDATION = {
  strong_no: { label: 'Strong no', color: 'red' },
  no: { label: 'No', color: 'orange' },
  yes: { label: 'Yes', color: 'teal' },
  strong_yes: { label: 'Strong yes', color: 'green' },
};
export const MODE_LABEL = { in_person: 'In person', phone: 'Phone', video: 'Video' };

/** "2–5 years", "3+ years" or null when the job sets no experience range. */
export function experienceText(min, max) {
  if (min === null && max === null) return null;
  const f = (x) => String(Number(x));
  if (min !== null && max !== null) return `${f(min)}–${f(max)} years`;
  return min !== null ? `${f(min)}+ years` : `Up to ${f(max)} years`;
}
