import { del, get, patch, post, put } from './client';

/** People and core HR: structure, directory, org chart, profile sections, career events, change requests. */

export const SECTION_LABEL = {
  personal: 'Personal details',
  contact: 'Contact and address',
  bank: 'Bank account',
  ids: 'ID documents',
  emergency: 'Emergency contacts',
  family: 'Family and nominees',
  education: 'Education',
  experience: 'Previous employment',
  skills: 'Skills and languages',
};

export const avatarUrl = (employeeId) => `/api/v1/people/${employeeId}/avatar`;
export const proofUrl = (requestId) => `/api/v1/people/change-requests/${requestId}/proof`;

export const people = {
  directory: (q = {}) => get('/people/directory', q),
  summary: () => get('/people/summary'),
  orgChart: () => get('/people/org-chart'),
  card: (id) => get(`/people/${id}/card`),

  structure: () => get('/people/structure'),
  createBranch: (b) => post('/people/structure/branches', b),
  updateBranch: (id, b) => patch(`/people/structure/branches/${id}`, b),
  deleteBranch: (id) => del(`/people/structure/branches/${id}`),
  createUnit: (b) => post('/people/structure/units', b),
  updateUnit: (id, b) => patch(`/people/structure/units/${id}`, b),
  deleteUnit: (id) => del(`/people/structure/units/${id}`),
  createDesignation: (b) => post('/people/structure/designations', b),
  updateDesignation: (id, b) => patch(`/people/structure/designations/${id}`, b),
  deleteDesignation: (id) => del(`/people/structure/designations/${id}`),

  policy: () => get('/people/policy'),
  setPolicy: (p) => put('/people/policy', p),

  profile: (id, reveal = false) => get(`/people/${id}/profile`, reveal ? { reveal: '1' } : undefined),
  saveSection: (id, section, value) => put(`/people/${id}/profile/${section}`, value),
  history: (id) => get(`/people/${id}/history`),
  career: (id) => get(`/people/${id}/career`),
  recordCareer: (id, b) => post(`/people/${id}/career`, b),
  cancelCareer: (id, eventId) => post(`/people/${id}/career/${eventId}/cancel`),

  requestChange: (b) => post('/people/me/change-requests', b),
  myRequests: () => get('/people/me/change-requests'),
  cancelRequest: (id) => post(`/people/me/change-requests/${id}/cancel`),
  requests: (status = 'pending') => get('/people/change-requests', { status }),
  request: (id, reveal = false) => get(`/people/change-requests/${id}`, reveal ? { reveal: '1' } : undefined),

  setMyAvatar: (fileId) => post('/people/me/avatar', { fileId }),
  removeMyAvatar: () => del('/people/me/avatar'),
  setAvatar: (id, fileId) => post(`/people/${id}/avatar`, { fileId }),
  removeAvatar: (id) => del(`/people/${id}/avatar`),
};
