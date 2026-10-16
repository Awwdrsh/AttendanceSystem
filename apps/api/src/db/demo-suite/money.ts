import { currentFiscalYearStart } from '../../modules/payroll/tax-savings.js';
import { alreadySeeded, type DemoContext } from './context.js';
import { addDaysIso, pick } from './kit.js';

interface Cat { id: string; code: string }

/** Expense claims and trips in every state: draft, waiting for the manager, approved for payroll, a trip with an advance and its settlement. */
export async function seedExpenses(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'expenses', "SELECT 1 FROM travel_request WHERE company_id = $1 AND destination = 'Biratnagar'", 'SELECT 1 FROM expense_claim WHERE company_id = $1 LIMIT 1')) return;
  const policy = await kit.get<{ categories: Cat[] }>(hr, '/expenses/policy');
  const cat = (code: string) => policy.categories.find((c) => c.code === code)!.id;
  // Travel allowance rates by grade (the * row applies to every grade without its own).
  await kit.put(hr, '/expenses/tada-rates', { rates: [
    { grade: '*', destination: 'valley', daRate: '800.00', lodgingRate: '0.00' },
    { grade: '*', destination: 'city', daRate: '1500.00', lodgingRate: '3500.00' },
    { grade: '*', destination: 'rural', daRate: '1200.00', lodgingRate: '2500.00' },
    { grade: '*', destination: 'international', daRate: '60.00', lodgingRate: '150.00' },
    { grade: 'G7', destination: 'city', daRate: '2200.00', lodgingRate: '5000.00' },
  ] }).catch(() => undefined);

  const receipt = async (who: import('./kit.js').Actor, what: string, amount: string) =>
    (await kit.post<{ file: { id: string } }>(who, '/files', { purpose: 'expense_receipt', filename: `${what.toLowerCase().replace(/\W+/g, '-')}.pdf`,
      dataUrl: `data:application/pdf;base64,${(await import('./kit.js')).makePdf(`Receipt: ${what}`, [`Amount: NPR ${amount}`, 'Paid in cash', 'Thank you'])
        .toString('base64')}` })).file.id;

  const people = ctx.employees.filter((e) => e.managerId);
  const claims: Array<{ who: number; title: string; items: Array<[number, string, string, string, string?]>; then: 'draft' | 'submit' | 'approve' | 'reject' }> = [
    { who: 1, title: 'Client visit to Nabil Bank', items: [[-6, 'LOCAL_TRAVEL', 'Taxi to Kamaladi and back', '850.00'], [-6, 'MEALS', 'Lunch with the client team', '2400.00']], then: 'approve' },
    { who: 2, title: 'Internet for work from home (Bhadra)', items: [[-20, 'MOBILE', 'WorldLink fibre, Bhadra', '1500.00']], then: 'approve' },
    { who: 4, title: 'KU workshop supplies', items: [[-3, 'OFFICE', 'Printing handouts for 24 students', '1800.00'], [-3, 'LOCAL_TRAVEL', 'Bus to Dhulikhel and back', '400.00']], then: 'submit' },
    { who: 6, title: 'Datacenter night shift meals', items: [[-2, 'MEALS', 'Dinner during the rack cut-over', '950.00']], then: 'submit' },
    { who: 7, title: 'Fuel for site visits', items: [[-9, 'FUEL', 'Bhaisepati datacenter, two round trips', '0', '36']], then: 'approve' },
    { who: 8, title: 'Team lunch (not reimbursable)', items: [[-15, 'MEALS', 'Team lunch at Thamel', '6800.00']], then: 'reject' },
    { who: 3, title: 'Conference ticket', items: [[-1, 'OTHER', 'Kathmandu Cloud Summit ticket', '3500.00']], then: 'draft' },
  ];
  for (const c of claims) {
    const e = people[c.who % people.length]!;
    const me = await ctx.as(e);
    const items = [];
    for (const [back, code, description, amount, km] of c.items) {
      const date = addDaysIso(ctx.today, back);
      if (km) items.push({ expenseDate: date, categoryId: cat(code), description, km: Number(km) });
      else items.push({ expenseDate: date, categoryId: cat(code), description, amount, receiptFileId: Number(amount) >= 500 ? await receipt(me, description, amount) : null });
    }
    const r = await kit.post<{ claim: { id: string } }>(me, '/expenses/claims', { title: c.title, items, submit: c.then !== 'draft' });
    const id = r.claim.id;
    if (c.then === 'approve' || c.then === 'reject') {
      const mgr = await ctx.managerOf(e);
      await kit.post(mgr, `/expenses/claims/${id}/review`, c.then === 'approve' ? { decision: 'approve' } : { decision: 'reject', reason: 'Team lunches are covered by the team budget, not personal claims.' });
      if (c.then === 'approve') {
        const st = await db.query<{ status: string }>('SELECT status FROM expense_claim WHERE id = $1', [id]);
        if (st.rows[0]?.status === 'submitted' || st.rows[0]?.status === 'pending') await kit.post(hr, `/expenses/claims/${id}/review`, { decision: 'approve' });
      }
    }
  }

  // A trip to Pokhara approved with an advance and settled, and a trip to Biratnagar waiting for approval.
  const t1e = people[1 % people.length]!; const t1 = await ctx.as(t1e);
  const trip = await kit.post<{ travel: { id: string } }>(t1, '/expenses/travel', { destination: 'Pokhara', destinationCategory: 'city', purpose: 'Install the edge GPU node at Gandaki College and train their IT team.',
    fromDate: addDaysIso(ctx.today, 1), toDate: addDaysIso(ctx.today, 3), mode: 'bus', estimatedCost: '12000.00', advanceRequested: '8000.00' });
  const tid = trip.travel.id;
  const mgr1 = await ctx.managerOf(t1e);
  await kit.post(mgr1, `/expenses/travel/${tid}/review`, { decision: 'approve' });
  const tstatus = await db.query<{ status: string }>('SELECT status FROM travel_request WHERE id = $1', [tid]);
  if (tstatus.rows[0]?.status !== 'approved') await kit.post(hr, `/expenses/travel/${tid}/review`, { decision: 'approve', advanceApproved: '8000.00' });
  // The demo needs a finished trip: requests can only be raised for the future, so move this one back two weeks.
  await db.query('UPDATE travel_request SET from_date = $2, to_date = $3, created_at = now() - interval \'16 days\' WHERE id = $1', [tid, addDaysIso(ctx.today, -12), addDaysIso(ctx.today, -10)]);
  const bus = await receipt(t1, 'Bus tickets Kathmandu-Pokhara', '2400.00');
  const hotel = await receipt(t1, 'Hotel Lakeside, two nights', '6000.00');
  await kit.post(t1, `/expenses/travel/${tid}/settlement`, { actualFrom: addDaysIso(ctx.today, -12), actualTo: addDaysIso(ctx.today, -10), note: 'Trip went as planned.',
    bills: [{ kind: 'transport', billDate: addDaysIso(ctx.today, -12), description: 'Bus tickets both ways', amount: '2400.00', fileId: bus },
      { kind: 'lodging', billDate: addDaysIso(ctx.today, -10), description: 'Hotel, two nights', amount: '6000.00', fileId: hotel }] });

  const t2e = people[5 % people.length]!;
  await kit.post(await ctx.as(t2e), '/expenses/travel', { destination: 'Biratnagar', destinationCategory: 'city', purpose: 'Meet two hospitals interested in on-premise GPU servers.',
    fromDate: addDaysIso(ctx.today, 8), toDate: addDaysIso(ctx.today, 10), mode: 'air', estimatedCost: '22000.00', advanceRequested: '10000.00' });
  void pick;
}

/** Two employees declared their insurance and CIT for this fiscal year; HR has not checked them yet. */
export async function seedTaxSavings(ctx: DemoContext): Promise<void> {
  const { kit, db, companyId } = ctx;
  if ((await db.query('SELECT 1 FROM tax_declaration_request WHERE company_id = $1 LIMIT 1', [companyId])).rows.length) return;
  const fy = currentFiscalYearStart();
  for (const [k, e] of ctx.employees.slice(3, 5).entries()) {
    const me = await ctx.as(e);
    const proof = await kit.uploadPdf(me, 'tax_proof', `insurance-receipt-${e.code}.pdf`, 'Life insurance premium receipt', [`Policy holder: ${e.name}`, `Premium paid: NPR ${k ? '25,000' : '40,000'}`, 'Nepal Life Insurance']);
    await kit.post(me, '/payroll/tax-savings', { fiscalYearStart: fy, lifePremium: k ? '25000' : '40000', healthPremium: k ? '0' : '15000', citMonthly: k ? '3000' : '0',
      note: 'Receipt attached.', proofFileIds: [proof] });
  }
}
