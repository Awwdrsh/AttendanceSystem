import type { Db } from './pool.js';
import { seedPayrollDefaults } from '../modules/payroll/seed.js';

/**
 * Idempotent per-company defaults: statutory leave types (plan §6.2), approval workflows, company settings.
 * Payroll defaults (rule versions, salary components) are added by the payroll module's seed (src/modules/payroll/seed.ts).
 */
export async function seedDefaults(db: Db, companyId: string): Promise<Record<string, number>> {
  const leaveTypes = [
    // code, name_en, name_ne, paid, statutory, basis, accrual_days, per_worked_days, max_balance, carry, encashable, cert_after, half_day, gender, sandwich, expiry
    ['HOME', 'Home leave', 'घर बिदा', true, true, 'worked_days', 1, 20, 90, true, true, null, true, 'all', false, null],
    ['SICK', 'Sick leave', 'बिरामी बिदा', true, true, 'monthly', 1, null, 45, true, true, 3, true, 'all', false, null],
    ['MATERNITY', 'Maternity leave', 'प्रसूति बिदा', true, true, 'none', 0, null, null, false, false, null, false, 'female', false, null],
    ['PATERNITY', 'Maternity-care leave', 'प्रसूति स्याहार बिदा', true, true, 'none', 0, null, null, false, false, null, false, 'male', false, null],
    ['MOURNING', 'Mourning leave', 'किरिया बिदा', true, true, 'none', 0, null, null, false, false, null, false, 'all', false, null],
    ['SUBSTITUTE', 'Substitute leave', 'सट्टा बिदा', true, true, 'none', 0, null, null, false, false, null, true, 'all', false, 21],
    ['CASUAL', 'Casual leave', 'भैपरी बिदा', true, false, 'annual', 6, null, 6, false, false, null, true, 'all', false, null],
    ['LWP', 'Leave without pay', 'बेतलबी बिदा', false, false, 'none', 0, null, null, false, false, null, true, 'all', true, null],
    ['SPECIAL_UNPAID', 'Special unpaid leave', 'विशेष बेतलबी बिदा', false, true, 'none', 0, null, null, false, false, null, false, 'all', false, null],
  ] as const;
  let n = 0;
  for (const [i, t] of leaveTypes.entries()) {
    const r = await db.query(
      `INSERT INTO leave_type (company_id, code, name_en, name_ne, paid, statutory, accrual_basis, accrual_days, accrual_per_worked_days, max_balance,
         carry_forward, encashable, certificate_after_days, allow_half_day, gender_scope, sandwich_rule, expiry_days, sort_order)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18) ON CONFLICT (company_id, code) DO NOTHING`,
      [companyId, ...t, i]);
    n += r.rowCount ?? 0;
  }
  // Event-based statutory leave: paid days per event (maternity 60 of 98, maternity-care 15, mourning 13), not a balance.
  await db.query(
    `UPDATE leave_type SET event_days = v.days FROM (VALUES ('MATERNITY', 60), ('PATERNITY', 15), ('MOURNING', 13)) AS v(code, days)
      WHERE leave_type.company_id = $1 AND leave_type.code = v.code AND leave_type.event_days IS NULL`, [companyId]);

  const workflows: Array<[string, Array<{ role: string }>]> = [
    ['leave_request', [{ role: 'manager' }]],
    ['regularisation_request', [{ role: 'manager' }]],
    ['ot_request', [{ role: 'manager' }]],
    ['payroll_run', [{ role: 'hr' }]],
    ['timesheet', [{ role: 'manager' }]],
  ];
  let w = 0;
  for (const [type, steps] of workflows) {
    const r = await db.query(
      `INSERT INTO workflow_definition (company_id, object_type, steps) VALUES ($1, $2, $3) ON CONFLICT (company_id, object_type) DO NOTHING`,
      [companyId, type, JSON.stringify(steps)]);
    w += r.rowCount ?? 0;
  }

  const settings: Array<[string, unknown]> = [
    ['attendance.policy', { version: 'default-v1', otMinThresholdMin: 30, otRoundingMin: 15, requirePreapproval: true, holidayWork: 'substitute_leave', nightWindowEnabled: false, nightPremiumRate: 0, autoClockOutAfterMin: 0, regularisationBackdateDays: 30, regularisationMonthlyCap: 5, wfhPhotoRequired: true, wfhPhotoRetentionDays: 90 }],
    ['leave.policy', { leaveYear: 'fiscal', sandwichRule: false, probationRestriction: true, maxContinuousDays: 30 }],
    ['payroll.policy', { prorationBasis: 'bs_calendar_days', hourlyDivisor: 208, payDay: 25, rounding: 'half_up_rupee', separateApprover: false, encashmentDivisor: 'bs_days' }],
    ['payslip.policy', { language: 'both', numerals: 'latin', passwordProtect: true }],
    ['time.policy', { enabled: true, requireProject: false, roundingStep: 0, roundingMode: 'nearest', fridayReminder: true, autoStopHours: 12 }],
    ['notifications.policy', { quietHoursStart: '21:00', quietHoursEnd: '07:00', slaHours: 24, escalationHours: 48 }],
  ];
  let s = 0;
  for (const [key, value] of settings) {
    const r = await db.query('INSERT INTO company_setting (company_id, key, value) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', [companyId, key, JSON.stringify(value)]);
    s += r.rowCount ?? 0;
  }
  const payroll = await seedPayrollDefaults(db, companyId);
  return { leaveTypes: n, workflows: w, settings: s, ...payroll };
}
