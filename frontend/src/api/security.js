import { del, get, post } from './client';
export const security = {
  login: (email, password) => post('/auth/login', { email, password }),
  loginTwoFactor: (challenge, code) => post('/auth/login/2fa', { challenge, code }),
  status: () => get('/auth/2fa'),
  setup: () => post('/auth/2fa/setup'),
  enable: (code) => post('/auth/2fa/enable', { code }),
  recoveryCodes: (code) => post('/auth/2fa/recovery-codes', { code }),
  disable: (password, code) => post('/auth/2fa/disable', { password, code }),
  sessions: () => get('/auth/sessions'),
  revokeSession: (id) => del(`/auth/sessions/${id}`),
  revokeOthers: () => post('/auth/sessions/revoke-others'),
  resetUser: (userId, reason) => post(`/auth/users/${userId}/2fa/reset`, { reason }),
};

// ---------------------------------------------------------------- tax savings

export const taxSavings = {
  get: (fiscalYearStart) => get('/payroll/tax-savings', { fiscalYearStart }),
  submit: (b) => post('/payroll/tax-savings', b),
  cancel: (id) => post(`/payroll/tax-savings/${id}/cancel`),
  estimate: (b) => post('/payroll/tax-savings/estimate', b),
  fileUrl: (requestId, fileId) => `/api/v1/payroll/tax-savings/${requestId}/files/${fileId}`,
};
