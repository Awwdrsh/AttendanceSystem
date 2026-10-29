import { del, download, get, patch, post, put, qs } from './client';

/** My companies and the company switcher (every signed-in person). */
export const myCompanies = {
  list: () => get('/me/companies'),
  switchTo: (companyId) => post('/me/companies/switch', { companyId }),
};

/** Group views and group administration (/api/v1/group/...). The server checks every group role itself. */
export const group = {
  payPolicy: () => get('/group/pay-policy'),
  setPayPolicy: (hidePayFromExecutives) => put('/group/pay-policy', { hidePayFromExecutives }),
  dashboard: (refresh) => get('/group/dashboard', refresh ? { refresh: 1 } : undefined),

  people: (q) => get('/group/people', q),
  peopleCsv: (q) => download(`/group/people${qs({ ...q, page: undefined, pageSize: undefined, format: 'csv' })}`, 'group-people.csv'),
  breakdownsCsv: (q) =>
    download(
      `/group/people${qs({ ...q, page: undefined, pageSize: undefined, format: 'csv', export: 'breakdowns' })}`,
      'group-headcount-breakdowns.csv',
    ),

  reportCatalog: () => get('/group/reports'),
  report: (kind, q) => get(`/group/reports/${kind}`, q),
  reportCsv: (kind, q) => download(`/group/reports/${kind}${qs({ ...q, format: 'csv' })}`, `group-${kind}.csv`),

  companies: () => get('/group/companies'),
  company: (id) => get(`/group/companies/${id}`),
  createCompany: (b) => post('/group/companies', b),
  updateCompany: (id, b) => patch(`/group/companies/${id}`, b),
  attachable: () => get('/group/companies/attachable'),
  attach: (companyId) => post('/group/companies/attach', { companyId }),

  access: (companyId) => get('/group/access', companyId ? { companyId } : undefined),
  unlinkedEmployees: (companyId, keep) => get('/group/access/employees', { companyId, keep }),
  grantAccess: (b) => post('/group/access', b),
  invite: (b) => post('/group/access/invite', b),
  revokeAccess: (userId, companyId) => del(`/group/access/${userId}/${companyId}`),

  groupRoles: () => get('/group/roles'),
  grantGroupRoles: (email, roles) => post('/group/roles', { email, roles }),
  revokeGroupRoles: (userId) => del(`/group/roles/${userId}`),

  transfers: () => get('/group/transfers'),
  transferTargets: () => get('/group/transfers/targets'),
  transferTarget: (companyId) => get(`/group/transfers/targets/${companyId}`),
  transfer: (b) => post('/group/transfers', b),
  transferRules: () => get('/group/transfer-rules'),
  setTransferRule: (companyId, carryLeaveBalance) => put('/group/transfer-rules', { companyId, carryLeaveBalance }),

  policySections: () => get('/group/policies/sections'),
  previewPolicies: (b) => post('/group/policies/preview', b),
  applyPolicies: (b) => post('/group/policies/apply', b),
};
