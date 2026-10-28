import { get, post, put } from './client';

export const OUT_LABEL = { leave: 'On leave', travel: 'Travelling', wfh: 'From home', field: 'In the field' };
export const OUT_COLOR = { leave: 'blue', travel: 'grape', wfh: 'teal', field: 'orange' };

/** Set the leave order (Leave, Policies): the listed types in order. */
export const leaveOrder = (ids) => post('/leave/types/order', { ids });

export const attendanceTools = {
  policy: () => get('/attendance-tools/policy'),
  savePolicy: (p) => put('/attendance-tools/policy', p),
  penalties: (bsYear, bsMonth) => get('/attendance-tools/penalties', { bsYear, bsMonth }),
  myLate: (q = {}) => get('/attendance-tools/penalties/me', q),
  applyPenalties: (bsYear, bsMonth, employeeIds) =>
    post('/attendance-tools/penalties/apply', { bsYear, bsMonth, ...(employeeIds ? { employeeIds } : {}) }),
  reversePenalty: (id, reason) => post(`/attendance-tools/penalties/${id}/reverse`, { reason }),
  issues: (from, to) => get('/attendance-tools/issues', { from, to }),
  issueSummary: (bsYear, bsMonth) => get('/attendance-tools/issues/summary', { bsYear, bsMonth }),
  nudgeEmployee: (employeeId, from, to) => post('/attendance-tools/issues/nudge', { target: 'employee', employeeId, from, to }),
  nudgeApprover: (instanceId) => post('/attendance-tools/issues/nudge', { target: 'approver', instanceId }),
  checkUpload: (csv) => post('/attendance-tools/upload/check', { csv }),
  importUpload: (csv, reason, filename) => post('/attendance-tools/upload/import', { csv, reason, ...(filename ? { filename } : {}) }),
  uploads: () => get('/attendance-tools/uploads'),
  whosOut: (days = 7) => get('/attendance-tools/whos-out', { days }),
};
