import { del, get, patch, post, put } from './client';

export const GRIEVANCE_CATEGORY_LABEL = {
  harassment: 'Harassment or bullying',
  discrimination: 'Discrimination',
  safety: 'Health and safety',
  pay_benefits: 'Pay and benefits',
  management: 'Treatment by a manager',
  policy: 'Company policy or practice',
  other: 'Something else',
};
export const GRIEVANCE_STATUS_LABEL = {
  submitted: 'Submitted',
  under_review: 'Under review',
  hearing_scheduled: 'Hearing set',
  decided: 'Decided',
  appealed: 'Appealed',
  closed: 'Closed',
  withdrawn: 'Withdrawn',
};
export const OUTCOME_LABEL = { upheld: 'Upheld', partly_upheld: 'Partly upheld', not_upheld: 'Not upheld' };

export const CASE_CATEGORY_LABEL = {
  attendance: 'Attendance',
  conduct: 'Conduct',
  performance: 'Performance',
  policy: 'Policy breach',
  safety: 'Safety',
  misconduct: 'Misconduct',
  other: 'Other',
};
export const CASE_STATUS_LABEL = {
  open: 'Under investigation',
  show_cause: 'Waiting for response',
  responded: 'Response received',
  decided: 'Decided',
  withdrawn: 'Withdrawn',
};
export const KYE_SECTION_LABEL = {
  personal: 'Personal details',
  contact: 'Contact and address',
  emergency: 'Emergency contacts',
  family: 'Family and nominees',
  education: 'Education',
  experience: 'Previous employment',
  ids: 'ID documents',
  bank: 'Bank account',
  photo: 'Profile photo',
};
export const relations = {
  policy: () => get('/relations/policy'),
  savePolicy: (p) => put('/relations/policy', p),
  summary: () => get('/relations/summary'),

  committee: () => get('/relations/grievances/committee'),
  setCommittee: (userIds) => put('/relations/grievances/committee', { userIds }),
  fileGrievance: (b) => post('/relations/grievances', b),
  grievances: (scope, status) => get('/relations/grievances', { scope, status }),
  grievance: (id) => get(`/relations/grievances/${id}`),
  grievanceFileUrl: (id, fileId) => `/api/v1/relations/grievances/${id}/files/${fileId}`,
  addNote: (id, body, shared) => post(`/relations/grievances/${id}/notes`, { body, shared }),
  updateGrievance: (id, b) => patch(`/relations/grievances/${id}`, b),
  decide: (id, b) => post(`/relations/grievances/${id}/decision`, b),
  appeal: (id, reason) => post(`/relations/grievances/${id}/appeal`, { reason }),
  decideAppeal: (id, b) => post(`/relations/grievances/${id}/appeal-decision`, b),
  withdraw: (id) => post(`/relations/grievances/${id}/withdraw`),
  close: (id) => post(`/relations/grievances/${id}/close`),

  stages: () => get('/relations/disciplinary/stages'),
  saveStages: (stages) => put('/relations/disciplinary/stages', { stages }),
  cases: (q = {}) => get('/relations/disciplinary', q),
  myCases: () => get('/relations/disciplinary/mine'),
  case: (id) => get(`/relations/disciplinary/${id}`),
  openCase: (b) => post('/relations/disciplinary', b),
  showCause: (id, b) => post(`/relations/disciplinary/${id}/show-cause`, b),
  respond: (id, response) => post(`/relations/disciplinary/${id}/respond`, { response }),
  decideCase: (id, b) => post(`/relations/disciplinary/${id}/decide`, b),
  linkLetter: (id, letterId) => post(`/relations/disciplinary/${id}/letter`, { letterId }),
  withdrawCase: (id, reason) => post(`/relations/disciplinary/${id}/withdraw`, { reason }),

  milestones: (days) => get('/relations/milestones', { days }),
  separating: () => get('/relations/separating'),
  contract: (employeeId) => get(`/relations/employees/${employeeId}/contract`),
  setContract: (employeeId, contractEnd) => put(`/relations/employees/${employeeId}/contract`, { contractEnd }),

  assignments: (q = {}) => get('/relations/assignments', q),
  addAssignment: (b) => post('/relations/assignments', b),
  endAssignment: (id, b) => post(`/relations/assignments/${id}/end`, b),

  campaigns: () => get('/relations/kye/campaigns'),
  campaign: (id) => get(`/relations/kye/campaigns/${id}`),
  launchCampaign: (b) => post('/relations/kye/campaigns', b),
  nudge: (id) => post(`/relations/kye/campaigns/${id}/nudge`),
  closeCampaign: (id) => post(`/relations/kye/campaigns/${id}/close`),
  myKye: () => get('/relations/kye/mine'),
  respondKye: (id, status, note) => post(`/relations/kye/campaigns/${id}/respond`, { status, note }),
  completeness: () => get('/relations/completeness'),

  popups: () => get('/relations/popups'),
  dismissPopup: (id, acknowledge) => post(`/relations/popups/${id}/dismiss`, { acknowledge }),

  myDevices: () => get('/relations/devices/mine'),
  registerDevice: (token, label) => post('/relations/devices/register', { token, label }),
  removeDevice: (id) => del(`/relations/devices/mine/${id}`),
  devices: (status = 'all') => get('/relations/devices', { status }),
  decideDevice: (id, action) => post(`/relations/devices/${id}/${action}`),
};
