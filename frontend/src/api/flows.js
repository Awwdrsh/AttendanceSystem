import { del, get, patch, post, put } from './client';

/** Step as sent to the server (kind plus the field that kind needs). */
export const flows = {
  config: () => get('/workflow/flows/config'),
  setDefault: (objectType, steps) => put(`/workflow/flows/defaults/${objectType}`, { steps }),
  create: (b) => post('/workflow/flows', b),
  update: (id, b) => patch(`/workflow/flows/${id}`, b),
  remove: (id) => del(`/workflow/flows/${id}`),
  createGroup: (b) => post('/workflow/groups', b),
  updateGroup: (id, b) => patch(`/workflow/groups/${id}`, b),
  removeGroup: (id) => del(`/workflow/groups/${id}`),
  preview: (q) => get('/workflow/preview', q),
  verify: (objectType) => get('/workflow/verify', objectType ? { objectType } : undefined),
  delegates: (all = false) => get('/workflow/delegates', all ? { all: 1 } : undefined),
  addDelegate: (b) => post('/workflow/delegates', b),
  cancelDelegate: (id) => del(`/workflow/delegates/${id}`),
};

/** Turn a stored step back into the form the editor uses. */
export function toStepInput(s) {
  const kind = s.kind ?? (s.role === 'manager' ? 'manager' : 'role');
  const out = { kind };
  if (kind === 'role') out.role = s.role;
  if (s.userId) out.userId = s.userId;
  if (s.groupId) out.groupId = s.groupId;
  if (s.minAmount !== undefined) out.minAmount = s.minAmount;
  if (s.label) out.label = s.label;
  return out;
}
