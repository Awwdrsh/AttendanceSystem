import { del, get, patch, post, put } from './client';

export const documents = {
  categories: () => get('/documents/categories'),
  createCategory: (b) => post('/documents/categories', b),
  updateCategory: (id, b) => patch(`/documents/categories/${id}`, b),

  mine: () => get('/documents/mine'),
  forEmployee: (employeeId, history = false) => get(`/documents/employee/${employeeId}`, history ? { history: 1 } : {}),
  queue: (days = 30) => get('/documents/queue', { days }),
  add: (employeeId, b) => post(`/documents/employee/${employeeId}`, b),
  verify: (id, decision, reason) => post(`/documents/${id}/verify`, reason ? { decision, reason } : { decision }),
  update: (id, b) => patch(`/documents/${id}`, b),
  remove: (id) => del(`/documents/${id}`),
  fileUrl: (id, download = false) => `/api/v1/documents/${id}/file${download ? '?download=1' : ''}`,

  placeholders: () => get('/documents/placeholders'),
  letterSettings: () => get('/documents/letter-settings'),
  saveLetterSettings: (b) => put('/documents/letter-settings', b),
  templates: () => get('/documents/templates'),
  createTemplate: (b) => post('/documents/templates', b),
  updateTemplate: (id, b) => patch(`/documents/templates/${id}`, b),
  preview: (b) => post('/documents/templates/preview', b),

  issue: (b) => post('/documents/letters', b),
  letters: (q) => get('/documents/letters', q),
  letterUrl: (id) => `/api/v1/documents/letters/${id}/html`,
  voidLetter: (id, reason) => post(`/documents/letters/${id}/void`, { reason }),

  requestLetter: (b) => post('/documents/letter-requests', b),
  requests: (q) => get('/documents/letter-requests', q),
  cancelRequest: (id) => post(`/documents/letter-requests/${id}/cancel`),

  policies: (archived = false) => get('/documents/policies', archived ? { archived: 1 } : {}),
  publishPolicy: (b) => post('/documents/policies', b),
  updatePolicy: (id, b) => patch(`/documents/policies/${id}`, b),
  policyUrl: (id) => `/api/v1/documents/policies/${id}/file`,
  acknowledge: (id) => post(`/documents/policies/${id}/acknowledge`),
  policyAcks: (id) => get(`/documents/policies/${id}/acks`),
  remindPolicy: (id) => post(`/documents/policies/${id}/remind`),
};

/** Date part of a timestamp in Nepal time, for showing as a BS date. */
export function nptDateOf(ts) {
  if (!ts) return null;
  const ms = Date.parse(ts);
  return Number.isNaN(ms) ? null : new Date(ms + 345 * 60_000).toISOString().slice(0, 10);
}

export function fileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
