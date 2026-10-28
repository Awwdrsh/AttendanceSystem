import { download, get, patch, post, put } from './client';

const E = '/expenses';
export const expenses = {
  policy: () => get(`${E}/policy`),
  saveSettings: (b) => put(`${E}/policy/settings`, b),
  addCategory: (b) => post(`${E}/categories`, b),
  updateCategory: (id, b) => patch(`${E}/categories/${id}`, b),
  setLimits: (id, limits) => put(`${E}/categories/${id}/limits`, { limits }),
  setTadaRates: (rates) => put(`${E}/tada-rates`, { rates }),

  check: (items, claimId) => post(`${E}/claims/check`, { items, ...(claimId ? { claimId } : {}) }),
  claims: (q = {}) => get(`${E}/claims`, q),
  claim: (id) => get(`${E}/claims/${id}`),
  createClaim: (b) => post(`${E}/claims`, b),
  updateClaim: (id, b) => put(`${E}/claims/${id}`, b),
  submitClaim: (id) => post(`${E}/claims/${id}/submit`),
  cancelClaim: (id) => post(`${E}/claims/${id}/cancel`),
  reviewClaim: (id, b) => post(`${E}/claims/${id}/review`, b),
  payClaimOutside: (id, note) => post(`${E}/claims/${id}/pay-outside`, { note }),
  receiptUrl: (claimId, itemId) => `/api/v1${E}/claims/${claimId}/items/${itemId}/receipt`,

  quote: (b) => post(`${E}/travel/quote`, b),
  trips: (q = {}) => get(`${E}/travel`, q),
  trip: (id) => get(`${E}/travel/${id}`),
  requestTravel: (b) => post(`${E}/travel`, b),
  cancelTrip: (id) => post(`${E}/travel/${id}/cancel`),
  reviewTrip: (id, b) => post(`${E}/travel/${id}/review`, b),
  payAdvanceOutside: (id, note) => post(`${E}/travel/${id}/advance-outside`, { note }),
  settle: (id, b) => post(`${E}/travel/${id}/settlement`, b),
  reviewSettlement: (id, b) => post(`${E}/settlements/${id}/review`, b),
  billUrl: (settlementId, billId) => `/api/v1${E}/settlements/${settlementId}/bills/${billId}/file`,

  spend: (q) => get(`${E}/reports/spend`, q),
  advances: () => get(`${E}/reports/advances`),
  pendingPayment: () => get(`${E}/reports/pending-payment`),
  downloadReport: (name, q = {}) =>
    download(
      `${E}/reports/${name}?${new URLSearchParams({ ...q, format: 'csv' }).toString()}`,
      `${name === 'spend' ? `expense-spend-by-${q.groupBy ?? 'category'}` : name === 'advances' ? 'outstanding-travel-advances' : 'expenses-pending-payment'}.csv`,
    ),
};
