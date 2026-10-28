import { del, get, patch, post, put } from './client';

export const PRIORITY_LABEL = { low: 'Low', normal: 'Normal', high: 'High', urgent: 'Urgent' };
export const PRIORITY_COLOR = { low: 'gray', normal: 'blue', high: 'orange', urgent: 'red' };

export const tasks = {
  boards: (all = false) => get('/tasks/boards', all ? { all: 1 } : {}),
  createBoard: (b) => post('/tasks/boards', b),
  board: (id) => get(`/tasks/boards/${id}`),
  updateBoard: (id, b) => patch(`/tasks/boards/${id}`, b),
  setMembers: (id, userIds) => put(`/tasks/boards/${id}/members`, { userIds }),
  people: () => get('/tasks/people'),
  create: (boardId, t) => post(`/tasks/boards/${boardId}/tasks`, t),
  get: (id) => get(`/tasks/${id}`),
  update: (id, t) => patch(`/tasks/${id}`, t),
  remove: (id) => del(`/tasks/${id}`),
  addChecklist: (id, text) => post(`/tasks/${id}/checklist`, { text }),
  updateChecklist: (itemId, b) => patch(`/tasks/checklist/${itemId}`, b),
  removeChecklist: (itemId) => del(`/tasks/checklist/${itemId}`),
  comment: (id, body) => post(`/tasks/${id}/comments`, { body }),
  attach: (id, fileId) => post(`/tasks/${id}/attachments`, { fileId }),
  removeAttachment: (attId) => del(`/tasks/attachments/${attId}`),
  attachmentUrl: (attId) => `/api/v1/tasks/attachments/${attId}/file`,
  todo: () => get('/tasks/my/todo'),
  report: (boardId) => get(`/tasks/reports/boards/${boardId}`),
};
