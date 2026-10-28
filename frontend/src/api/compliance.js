import { del, download, get, post, put } from './client';

const qs = (o) => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== '') p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : '';
};

export const compliance = {
  optimumMine: () => get('/payroll/optimum-cit/me'),
  optimumAll: () => get('/payroll/optimum-cit'),
  applyCit: (b) => post('/payroll/optimum-cit/apply', b),

  openings: (fiscalYearStart) => get('/payroll/opening-balances', { fiscalYearStart }),
  saveOpening: (b) => put('/payroll/opening-balances', b),
  deleteOpening: (id) => del(`/payroll/opening-balances/${id}`),
  importOpenings: (csv) => post('/payroll/opening-balances/import', { csv }),

  holds: (status = 'open') => get('/payroll/holds', { status }),
  hold: (runId, employeeId, reason) => post(`/payroll/runs/${runId}/holds`, { employeeId, reason }),
  release: (id, mode, note) => post(`/payroll/holds/${id}/release`, { mode, ...(note ? { note } : {}) }),
  cancelHold: (id) => post(`/payroll/holds/${id}/cancel`),

  certificateUrl: (employeeId, kind, fiscalYearStart, purpose) =>
    `/api/v1/payroll/certificates/${employeeId}${qs({ kind, fiscalYearStart, purpose })}`,

  annualTax: (fiscalYearStart) => get('/payroll/reports/annual-tax', { fiscalYearStart }),
  annualTaxCsv: (fiscalYearStart) =>
    download(`/payroll/reports/annual-tax${qs({ fiscalYearStart, format: 'csv' })}`, `annual-tax-${fiscalYearStart}.csv`),
  taxDeposit: (bsYear, bsMonth) => get('/payroll/reports/tax-deposit', { bsYear, bsMonth }),
  retirementDeposit: (bsYear, bsMonth) => get('/payroll/reports/retirement-deposit', { bsYear, bsMonth }),
  monthCsv: (report, bsYear, bsMonth) =>
    download(
      `/payroll/reports/${report}${qs({ bsYear, bsMonth, format: 'csv' })}`,
      `${report}-${bsYear}-${String(bsMonth).padStart(2, '0')}.csv`,
    ),
  variance: (runId) => get('/payroll/reports/pay-variance', { runId }),
  varianceCsv: (runId) => download(`/payroll/reports/pay-variance${qs({ runId, format: 'csv' })}`, 'pay-variance.csv'),
  leaveLiability: (asOf) => get('/payroll/reports/leave-liability', asOf ? { asOf } : undefined),
  leaveLiabilityCsv: (asOf) => download(`/payroll/reports/leave-liability${qs({ asOf, format: 'csv' })}`, `leave-liability-${asOf}.csv`),
};
