import { D, Decimal } from '@hajiri/payroll-engine';
import { adToBs, bsMonthBounds } from '@hajiri/bs-calendar';
import type { Queryable } from '../../db/pool.js';
import { bsLabel, bsRange } from '../../bsdate.js';
import { BadRequestError, ConflictError, NotFoundError } from '../../errors.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { nptToday, registerHandler, registerSummariser, type DecisionContext } from '../workflow/service.js';

// ---------------------------------------------------------------- policy
export type LimitMode = 'block' | 'warn';
export interface ExpensePolicy { mileageRatePerKm: string; limitMode: LimitMode; claimWindowDays: number }
export type Destination = 'valley' | 'city' | 'rural' | 'international';
export const DESTINATIONS: Destination[] = ['valley', 'city', 'rural', 'international'];
export const DESTINATION_LABEL: Record<Destination, string> = { valley: 'Kathmandu valley', city: 'Other city', rural: 'Rural area', international: 'International' };

export async function expensePolicy(db: Queryable, companyId: string): Promise<ExpensePolicy> {
  const r = await db.query<{ value: Record<string, unknown> }>(`SELECT value FROM company_setting WHERE company_id = $1 AND key = 'expense.policy'`, [companyId]);
  const v = r.rows[0]?.value ?? {};
  const rate = typeof v.mileageRatePerKm === 'string' || typeof v.mileageRatePerKm === 'number' ? String(v.mileageRatePerKm) : '15';
  return {
    mileageRatePerKm: D(rate).isFinite() && D(rate).gte(0) ? D(rate).toFixed(2) : '15.00',
    limitMode: v.limitMode === 'warn' ? 'warn' : 'block',
    claimWindowDays: typeof v.claimWindowDays === 'number' ? v.claimWindowDays : 90,
  };
}

export async function employeeOf(db: Queryable, companyId: string, employeeId: string): Promise<{ id: string; name_en: string; emp_code: string; grade: string | null; status: string }> {
  const r = await db.query<{ id: string; name_en: string; emp_code: string; grade: string | null; status: string }>(
    'SELECT id, name_en, emp_code, grade, status FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

export const money = (d: Decimal | string | number): string => D(d).toDecimalPlaces(2).toFixed(2);
export const rs = (d: Decimal | string | number): string => `Rs ${Number(D(d).toFixed(2)).toLocaleString('en-IN', { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`;

function daysBetween(from: string, to: string): number {
  return Math.round((new Date(`${to}T00:00:00Z`).getTime() - new Date(`${from}T00:00:00Z`).getTime()) / 86_400_000);
}

// ---------------------------------------------------------------- claim checks
export interface ClaimItemInput { expenseDate: string; categoryId: string; description: string; amount?: string | undefined; km?: number | undefined; receiptFileId?: string | null | undefined }
export interface CheckIssue { level: 'block' | 'warn'; code: string; message: string; item?: number }
interface CategoryRow { id: string; code: string; name: string; kind: 'amount' | 'mileage'; receipt_required_above: string | null; active: boolean }

/** Server-side amounts: mileage lines are km × the company rate; amount lines must carry an amount. */
export async function normaliseItems(db: Queryable, companyId: string, items: ClaimItemInput[]): Promise<Array<ClaimItemInput & { amount: string; category: CategoryRow }>> {
  const policy = await expensePolicy(db, companyId);
  const cats = await db.query<CategoryRow>('SELECT id, code, name, kind, receipt_required_above::text, active FROM expense_category WHERE company_id = $1', [companyId]);
  const byId = new Map(cats.rows.map((c) => [c.id, c]));
  return items.map((it, i) => {
    const category = byId.get(it.categoryId);
    if (!category) throw new BadRequestError(`Line ${i + 1}: choose a valid category.`);
    if (!category.active) throw new BadRequestError(`Line ${i + 1}: ${category.name} is no longer available.`);
    if (category.kind === 'mileage') {
      if (!it.km || it.km <= 0) throw new BadRequestError(`Line ${i + 1}: enter the distance in km.`);
      return { ...it, category, amount: money(D(it.km).times(policy.mileageRatePerKm)) };
    }
    if (!it.amount || !D(it.amount).gt(0)) throw new BadRequestError(`Line ${i + 1}: enter an amount above zero.`);
    return { ...it, km: undefined, category, amount: money(it.amount) };
  });
}

/**
 * Limits by grade (grade row, else the '*' row), receipts above the category threshold, the claim window and the
 * monthly total across the employee's other open or paid claims in the same BS month.
 */
export async function checkClaim(db: Queryable, companyId: string, employeeId: string, items: Array<{ expenseDate: string; amount: string; receiptFileId?: string | null | undefined; category: CategoryRow }>, excludeClaimId: string | null): Promise<CheckIssue[]> {
  const policy = await expensePolicy(db, companyId);
  const emp = await employeeOf(db, companyId, employeeId);
  const issues: CheckIssue[] = [];
  const today = nptToday();
  items.forEach((it, i) => {
    if (it.expenseDate > today) issues.push({ level: 'block', code: 'future_date', message: `Line ${i + 1}: the date is in the future.`, item: i });
    else if (daysBetween(it.expenseDate, today) > policy.claimWindowDays) issues.push({ level: 'block', code: 'too_old', message: `Line ${i + 1}: expenses older than ${policy.claimWindowDays} days cannot be claimed.`, item: i });
    const thr = it.category.receipt_required_above;
    if (thr !== null && D(it.amount).gt(thr) && !it.receiptFileId) {
      issues.push({ level: 'block', code: 'receipt_required', message: `Line ${i + 1}: attach a receipt${D(thr).gt(0) ? ` for ${it.category.name} above ${rs(thr)}` : ` for ${it.category.name}`}.`, item: i });
    }
  });
  const catIds = [...new Set(items.map((i) => i.category.id))];
  const limits = await db.query<{ category_id: string; grade: string; per_claim: string | null; per_month: string | null }>(
    `SELECT category_id, grade, per_claim::text, per_month::text FROM expense_limit WHERE company_id = $1 AND category_id = ANY($2) AND grade IN ($3, '*')`,
    [companyId, catIds, emp.grade ?? '*']);
  const limitFor = (catId: string) => limits.rows.find((l) => l.category_id === catId && l.grade === emp.grade) ?? limits.rows.find((l) => l.category_id === catId && l.grade === '*');
  const level = policy.limitMode;
  for (const catId of catIds) {
    const lim = limitFor(catId);
    if (!lim) continue;
    const mine = items.filter((i) => i.category.id === catId);
    const name = mine[0]!.category.name;
    const total = mine.reduce((a, i) => a.plus(i.amount), D(0));
    if (lim.per_claim !== null && total.gt(lim.per_claim)) {
      issues.push({ level, code: 'per_claim_limit', message: `${name}: ${rs(total)} is over the limit of ${rs(lim.per_claim)} per claim.` });
    }
    if (lim.per_month !== null) {
      const months = new Map<string, Decimal>();
      for (const i of mine) {
        const b = adToBs(i.expenseDate); const k = `${b.year}-${b.month}`;
        months.set(k, (months.get(k) ?? D(0)).plus(i.amount));
      }
      for (const [k, amt] of months) {
        const [y, m] = k.split('-').map(Number) as [number, number];
        const bounds = bsMonthBounds(y, m);
        const other = await db.query<{ total: string }>(
          `SELECT coalesce(sum(coalesce(i.approved_amount, i.amount)), 0)::text AS total FROM expense_item i JOIN expense_claim c ON c.id = i.claim_id
            WHERE c.employee_id = $1 AND i.category_id = $2 AND c.status IN ('pending', 'approved', 'paid') AND i.expense_date BETWEEN $3 AND $4
              AND ($5::uuid IS NULL OR c.id <> $5)`, [employeeId, catId, bounds.startAd, bounds.endAd, excludeClaimId]);
        const monthTotal = amt.plus(other.rows[0]!.total);
        if (monthTotal.gt(lim.per_month)) {
          issues.push({ level, code: 'per_month_limit', message: `${name}: ${rs(monthTotal)} in ${bsLabel(bounds.startAd).replace(/^\d+ /, '')} is over the monthly limit of ${rs(lim.per_month)}.` });
        }
      }
    }
  }
  return issues;
}

// ---------------------------------------------------------------- travel allowance
export interface TadaQuote { days: number; nights: number; currency: 'NPR' | 'USD'; daRate: string; lodgingRate: string; fxRate: string | null; daRateNpr: string; lodgingRateNpr: string; daAmount: string; lodgingLimit: string; grade: string }

/** Daily allowance for every calendar day of the trip and lodging for every night, at the grade's rate (else the default). */
export async function quoteTada(db: Queryable, companyId: string, employeeId: string, dest: Destination, from: string, to: string, fxRate: string | null): Promise<TadaQuote> {
  if (to < from) throw new BadRequestError('The return date must be on or after the start date.');
  const emp = await employeeOf(db, companyId, employeeId);
  const r = await db.query<{ grade: string; da_rate: string; lodging_rate: string }>(
    `SELECT grade, da_rate::text, lodging_rate::text FROM tada_rate WHERE company_id = $1 AND destination = $2 AND grade IN ($3, '*')
      ORDER BY (grade = '*') LIMIT 1`, [companyId, dest, emp.grade ?? '*']);
  const rate = r.rows[0] ?? { grade: '*', da_rate: '0', lodging_rate: '0' };
  const international = dest === 'international';
  if (international && (!fxRate || !D(fxRate).gt(0))) throw new BadRequestError('Enter the US dollar exchange rate for an international trip.');
  const fx = international ? D(fxRate!) : D(1);
  const days = daysBetween(from, to) + 1;
  if (days > 90) throw new BadRequestError('A single trip can be at most 90 days.');
  const nights = days - 1;
  const daNpr = D(rate.da_rate).times(fx); const lodgingNpr = D(rate.lodging_rate).times(fx);
  return {
    days, nights, currency: international ? 'USD' : 'NPR', daRate: money(rate.da_rate), lodgingRate: money(rate.lodging_rate), fxRate: international ? D(fxRate!).toFixed(4) : null,
    daRateNpr: money(daNpr), lodgingRateNpr: money(lodgingNpr), daAmount: money(daNpr.times(days)), lodgingLimit: money(lodgingNpr.times(nights)), grade: rate.grade,
  };
}

// ---------------------------------------------------------------- payouts
export type PayoutCode = 'EXP_REIMB' | 'TRAVEL_ADV' | 'EXP_RECOVERY';

export async function createPayout(db: Queryable, p: { companyId: string; employeeId: string; sourceType: 'expense_claim' | 'travel_advance' | 'travel_settlement'; sourceId: string; code: PayoutCode; amount: Decimal; note: string }): Promise<void> {
  if (!p.amount.gt(0)) return;
  await db.query(
    `INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note) VALUES ($1, $2, $3, $4, $5, $6, $7)
     ON CONFLICT (source_type, source_id, component_code) WHERE parent_payout_id IS NULL DO NOTHING`,
    [p.companyId, p.employeeId, p.sourceType, p.sourceId, p.code, money(p.amount), p.note.slice(0, 200)]);
}

async function tell(db: Queryable, companyId: string, employeeId: string, template: string, title: string, body: string, payload: Record<string, unknown>): Promise<void> {
  const userId = await userIdForEmployee(db, employeeId);
  if (!userId) return;
  await notify(db, { companyId, userId, employeeId, template, title, body, payload, channels: ['push'] });
}

// ---------------------------------------------------------------- workflow handlers
export async function applyClaimDecision(db: Queryable, claimId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ company_id: string; employee_id: string; title: string; status: string }>('SELECT company_id, employee_id, title, status FROM expense_claim WHERE id = $1 FOR UPDATE', [claimId]);
  const c = r.rows[0];
  if (!c) throw new NotFoundError('claim not found');
  if (c.status !== 'pending') throw new ConflictError(`claim already ${c.status}`);
  if (state === 'rejected') {
    await db.query(`UPDATE expense_claim SET status = 'rejected', decided_at = now(), decision_note = $2, updated_at = now() WHERE id = $1`, [claimId, ctx.reason]);
    await tell(db, c.company_id, c.employee_id, 'expense_claim.rejected', 'Expense claim rejected', `"${c.title}" was rejected${ctx.reason ? `: ${ctx.reason}` : ''}.`, { claimId });
    return;
  }
  await db.query('UPDATE expense_item SET approved_amount = coalesce(approved_amount, amount) WHERE claim_id = $1', [claimId]);
  const t = await db.query<{ total: string }>('SELECT coalesce(sum(approved_amount), 0)::text AS total FROM expense_item WHERE claim_id = $1', [claimId]);
  const total = D(t.rows[0]!.total);
  await db.query(`UPDATE expense_claim SET status = $2, total_approved = $3, decided_at = now(), decision_note = $4, payment_method = 'payroll', updated_at = now() WHERE id = $1`,
    [claimId, total.gt(0) ? 'approved' : 'rejected', money(total), ctx.reason]);
  if (!total.gt(0)) {
    await tell(db, c.company_id, c.employee_id, 'expense_claim.rejected', 'Expense claim not paid', `"${c.title}" was approved for nothing${ctx.reason ? `: ${ctx.reason}` : ''}.`, { claimId });
    return;
  }
  await createPayout(db, { companyId: c.company_id, employeeId: c.employee_id, sourceType: 'expense_claim', sourceId: claimId, code: 'EXP_REIMB', amount: total, note: `Claim: ${c.title}` });
  await tell(db, c.company_id, c.employee_id, 'expense_claim.approved', 'Expense claim approved',
    `"${c.title}" was approved for ${rs(total)}. It will be paid with your next salary.`, { claimId });
}

export async function applyTravelDecision(db: Queryable, travelId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ company_id: string; employee_id: string; destination: string; status: string; from_date: string; to_date: string; advance_requested: string; advance_approved: string | null }>(
    'SELECT company_id, employee_id, destination, status, from_date::text, to_date::text, advance_requested::text, advance_approved::text FROM travel_request WHERE id = $1 FOR UPDATE', [travelId]);
  const t = r.rows[0];
  if (!t) throw new NotFoundError('travel request not found');
  if (t.status !== 'pending') throw new ConflictError(`travel request already ${t.status}`);
  const when = bsRange(t.from_date, t.to_date);
  if (state === 'rejected') {
    await db.query(`UPDATE travel_request SET status = 'rejected', decided_at = now(), decision_note = $2 WHERE id = $1`, [travelId, ctx.reason]);
    await tell(db, t.company_id, t.employee_id, 'travel_request.rejected', 'Travel request rejected', `Your trip to ${t.destination} (${when}) was rejected${ctx.reason ? `: ${ctx.reason}` : ''}.`, { travelId });
    return;
  }
  const advance = D(t.advance_approved ?? t.advance_requested);
  await db.query(`UPDATE travel_request SET status = 'approved', decided_at = now(), decision_note = $2, advance_approved = $3,
                    advance_status = CASE WHEN $3::numeric > 0 THEN 'pending' ELSE 'none' END, advance_method = CASE WHEN $3::numeric > 0 THEN 'payroll' END WHERE id = $1`,
    [travelId, ctx.reason, money(advance)]);
  await createPayout(db, { companyId: t.company_id, employeeId: t.employee_id, sourceType: 'travel_advance', sourceId: travelId, code: 'TRAVEL_ADV', amount: advance, note: `Travel advance: ${t.destination}` });
  await tell(db, t.company_id, t.employee_id, 'travel_request.approved', 'Travel request approved',
    `Your trip to ${t.destination} (${when}) was approved${advance.gt(0) ? ` with an advance of ${rs(advance)}` : ''}.`, { travelId });
}

export async function applySettlementDecision(db: Queryable, settlementId: string, state: 'approved' | 'rejected', ctx: DecisionContext): Promise<void> {
  const r = await db.query<{ company_id: string; employee_id: string; status: string; travel_request_id: string; entitlement: string; advance: string; net: string; destination: string }>(
    `SELECT s.company_id, s.employee_id, s.status, s.travel_request_id, s.entitlement::text, s.advance::text, s.net::text, t.destination
       FROM travel_settlement s JOIN travel_request t ON t.id = s.travel_request_id WHERE s.id = $1 FOR UPDATE OF s`, [settlementId]);
  const s = r.rows[0];
  if (!s) throw new NotFoundError('settlement not found');
  if (s.status !== 'pending') throw new ConflictError(`settlement already ${s.status}`);
  if (state === 'rejected') {
    await db.query(`UPDATE travel_settlement SET status = 'rejected', decided_at = now(), decision_note = $2 WHERE id = $1`, [settlementId, ctx.reason]);
    await db.query(`UPDATE travel_request SET status = 'approved' WHERE id = $1 AND status = 'settling'`, [s.travel_request_id]);
    await tell(db, s.company_id, s.employee_id, 'travel_settlement.rejected', 'Travel settlement returned', `Your settlement for ${s.destination} was rejected${ctx.reason ? `: ${ctx.reason}` : ''}. You can correct and submit it again.`, { settlementId });
    return;
  }
  // An advance that payroll has not picked up yet is never paid: it is cancelled and the whole entitlement is settled instead.
  let advance = D(s.advance);
  const adv = await db.query<{ id: string }>(
    `UPDATE expense_payout SET status = 'cancelled' WHERE source_type = 'travel_advance' AND source_id = $1 AND status = 'pending' AND payroll_run_id IS NULL RETURNING id`, [s.travel_request_id]);
  if (adv.rows.length) {
    advance = D(0);
    await db.query(`UPDATE travel_request SET advance_status = 'none' WHERE id = $1`, [s.travel_request_id]);
  }
  const net = D(s.entitlement).minus(advance);
  await db.query(`UPDATE travel_settlement SET status = 'approved', decided_at = now(), decision_note = $2, advance = $3, net = $4 WHERE id = $1`, [settlementId, ctx.reason, money(advance), money(net)]);
  await db.query(`UPDATE travel_request SET status = 'settled' WHERE id = $1`, [s.travel_request_id]);
  if (net.gt(0)) await createPayout(db, { companyId: s.company_id, employeeId: s.employee_id, sourceType: 'travel_settlement', sourceId: settlementId, code: 'EXP_REIMB', amount: net, note: `Travel settlement: ${s.destination}` });
  if (net.lt(0)) await createPayout(db, { companyId: s.company_id, employeeId: s.employee_id, sourceType: 'travel_settlement', sourceId: settlementId, code: 'EXP_RECOVERY', amount: net.neg(), note: `Unspent travel advance: ${s.destination}` });
  await tell(db, s.company_id, s.employee_id, 'travel_settlement.approved', 'Travel settlement approved',
    net.gt(0) ? `${rs(net)} for your trip to ${s.destination} will be paid with your next salary.`
      : net.lt(0) ? `${rs(net.neg())} of unspent advance for ${s.destination} will be recovered from your next salary.`
        : `Your trip to ${s.destination} is settled with nothing more to pay or recover.`, { settlementId });
}

registerHandler('expense_claim', applyClaimDecision);
registerHandler('travel_request', applyTravelDecision);
registerHandler('travel_settlement', applySettlementDecision);

registerSummariser('expense_claim', async (db, inst) => {
  const r = await db.query<{ title: string; total_claimed: string; items: number; receipts: number; categories: string | null }>(
    `SELECT c.title, c.total_claimed::text, (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id) AS items,
            (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id AND i.receipt_file_id IS NOT NULL) AS receipts,
            (SELECT string_agg(DISTINCT ec.name, ', ') FROM expense_item i JOIN expense_category ec ON ec.id = i.category_id WHERE i.claim_id = c.id) AS categories
       FROM expense_claim c WHERE c.id = $1`, [inst.object_id]);
  const c = r.rows[0];
  return c ? { title: c.title, amount: c.total_claimed, items: c.items, receipts: c.receipts, categories: c.categories } : {};
});
registerSummariser('travel_request', async (db, inst) => {
  const r = await db.query<{ destination: string; destination_category: Destination; purpose: string; from_date: string; to_date: string; days: number; da_amount: string; estimated_cost: string; advance_requested: string; mode: string }>(
    'SELECT destination, destination_category, purpose, from_date::text, to_date::text, days, da_amount::text, estimated_cost::text, advance_requested::text, mode FROM travel_request WHERE id = $1', [inst.object_id]);
  const t = r.rows[0];
  return t ? { destination: t.destination, destinationCategory: DESTINATION_LABEL[t.destination_category], purpose: t.purpose, from: t.from_date, to: t.to_date, days: t.days,
    dailyAllowance: t.da_amount, estimatedCost: t.estimated_cost, advance: t.advance_requested, mode: t.mode } : {};
});
registerSummariser('travel_settlement', async (db, inst) => {
  const r = await db.query<{ destination: string; actual_from: string; actual_to: string; entitlement: string; advance: string; net: string; bills: number }>(
    `SELECT t.destination, s.actual_from::text, s.actual_to::text, s.entitlement::text, s.advance::text, s.net::text,
            (SELECT count(*)::int FROM travel_bill b WHERE b.settlement_id = s.id) AS bills
       FROM travel_settlement s JOIN travel_request t ON t.id = s.travel_request_id WHERE s.id = $1`, [inst.object_id]);
  const s = r.rows[0];
  return s ? { destination: s.destination, from: s.actual_from, to: s.actual_to, entitlement: s.entitlement, advance: s.advance, net: s.net, bills: s.bills } : {};
});
