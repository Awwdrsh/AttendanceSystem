import { get, patch, post, put } from './client';

export const helpdesk = {
  categories: () => get('/helpdesk/categories'),
  createCategory: (b) => post('/helpdesk/categories', b),
  updateCategory: (id, b) => put(`/helpdesk/categories/${id}`, b),
  policy: () => get('/helpdesk/settings'),
  savePolicy: (p) => put('/helpdesk/settings', p),
  agents: () => get('/helpdesk/agents'),
  create: (b) => post('/helpdesk/tickets', b),
  mine: () => get('/helpdesk/tickets/mine'),
  queue: (q = {}) => get('/helpdesk/tickets', q),
  stats: () => get('/helpdesk/stats'),
  ticket: (id) => get(`/helpdesk/tickets/${id}`),
  reply: (id, b) => post(`/helpdesk/tickets/${id}/messages`, b),
  update: (id, b) => patch(`/helpdesk/tickets/${id}`, b),
  reopen: (id) => post(`/helpdesk/tickets/${id}/reopen`),
  close: (id) => post(`/helpdesk/tickets/${id}/close`),
  rate: (id, rating, comment) => post(`/helpdesk/tickets/${id}/rate`, { rating, comment }),
  fileUrl: (ticketId, fileId) => `/api/v1/helpdesk/tickets/${ticketId}/files/${fileId}`,
};

export const PRIORITY_LABEL = { low: 'Low', normal: 'Normal', high: 'High', urgent: 'Urgent' };
export const PRIORITY_COLOR = { low: 'gray', normal: 'blue', high: 'orange', urgent: 'red' };
export const STATUS_LABEL = { open: 'Open', in_progress: 'In progress', waiting: 'Waiting on you', resolved: 'Resolved', closed: 'Closed' };
export const STATUS_COLOR = { open: 'blue', in_progress: 'grape', waiting: 'yellow', resolved: 'teal', closed: 'gray' };
