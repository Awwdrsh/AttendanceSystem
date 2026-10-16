import { alreadySeeded, type DemoContext } from './context.js';
import { addDaysIso, pick } from './kit.js';

interface Category { id: string; code: string; name: string }
interface Template { id: string; code: string; name: string }

/** Documents on file (verified, waiting, expiring), letters issued and requested, and a handbook to acknowledge. */
export async function seedDocuments(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'documents', 'SELECT 1 FROM company_policy WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM employee_document WHERE company_id = $1 LIMIT 1')) return;
  const cats = (await kit.get<{ categories: Category[] }>(hr, '/documents/categories')).categories;
  const cat = (code: string) => cats.find((c) => c.code === code)!;
  const tpls = (await kit.get<{ templates: Template[] }>(hr, '/documents/templates')).templates;
  const tpl = (code: string) => tpls.find((t) => t.code === code)!;

  for (const [i, e] of ctx.employees.entries()) {
    const contract = await kit.uploadPdf(hr, 'employee_document', `contract-${e.code}.pdf`, `Employment contract - ${e.name}`,
      [`${ctx.companyName}`, `Position: ${e.designation ?? 'Staff'}`, `Joining date (AD): ${e.joinDate ?? ''}`, 'Signed by both parties.']);
    const d1 = await kit.post<{ document: { id: string } }>(hr, `/documents/employee/${e.id}`, { categoryId: cat('contract').id, title: 'Employment contract', fileId: contract });
    void d1;
    const cz = await kit.uploadPdf(hr, 'employee_document', `citizenship-${e.code}.pdf`, `Citizenship certificate - ${e.name}`, ['Copy kept on file by HR.']);
    await kit.post(hr, `/documents/employee/${e.id}`, { categoryId: cat('citizenship').id, title: 'Citizenship certificate (front and back)', fileId: cz });
    if (i % 2 === 0) {
      const ac = await kit.uploadPdf(hr, 'employee_document', `degree-${e.code}.pdf`, `Academic transcript - ${e.name}`, ['Bachelor degree transcript.']);
      await kit.post(hr, `/documents/employee/${e.id}`, { categoryId: cat('academic').id, title: 'Bachelor transcript', fileId: ac });
    }
    if (i === 1 || i === 5) {
      const pp = await kit.uploadPdf(hr, 'employee_document', `passport-${e.code}.pdf`, `Passport - ${e.name}`, ['Bio page.']);
      await kit.post(hr, `/documents/employee/${e.id}`, { categoryId: cat('passport').id, title: 'Passport bio page', fileId: pp, expiryDate: addDaysIso(ctx.today, i === 1 ? 45 : 12) });
    }
  }
  // HR uploads count as verified; a few employees uploaded their own and wait for HR.
  await db.query(`UPDATE employee_document SET status = 'verified', verified_at = now() WHERE company_id = $1 AND status = 'pending'`, [companyId]);
  for (const e of ctx.employees.slice(2, 5)) {
    const me = await ctx.as(e);
    const f = await kit.uploadPdf(me, 'employee_document', `pan-${e.code}.pdf`, `PAN card - ${e.name}`, ['Permanent Account Number card.']);
    await kit.post(me, `/documents/employee/${e.id}`, { categoryId: cat('pan_card').id, title: 'PAN card', fileId: f });
  }

  // Letters: appointment letters for the latest joiners, a salary certificate, and an experience letter request waiting for HR.
  for (const e of [...ctx.employees].sort((a, b) => (b.joinDate ?? '').localeCompare(a.joinDate ?? '')).slice(0, 3)) {
    await kit.post(hr, '/documents/letters', { templateId: tpl('appointment').id, employeeId: e.id, allowMissing: true });
  }
  const s = ctx.employees[4]!;
  await kit.post(hr, '/documents/letters', { templateId: tpl('salary_certificate').id, employeeId: s.id, purpose: 'Home loan application', addressedTo: 'Nabil Bank Limited', allowMissing: true });
  const asker = ctx.employees[6]!;
  await kit.post(await ctx.as(asker), '/documents/letter-requests', { templateId: tpl('salary_certificate').id, purpose: 'Visa application for a family trip', addressedTo: 'Embassy of Japan, Kathmandu' });
  const asker2 = ctx.employees[9]!;
  await kit.post(await ctx.as(asker2), '/documents/letter-requests', { templateId: tpl('to_whom_it_may_concern').id, purpose: 'Bank account opening' });

  // Company policies.
  const handbook = await kit.uploadPdf(hr, 'company_document', 'employee-handbook-2083.pdf', `${ctx.companyName} Employee Handbook 2083`,
    ['1. Working hours: Sunday to Friday, 9:30 to 18:00.', '2. Leave: apply in Hajiri at least two days ahead.', '3. Work from home: agree with your manager first.', '4. Code of conduct applies to everyone.']);
  const hb = await kit.post<{ policy: { id: string } }>(hr, '/documents/policies', { title: 'Employee Handbook 2083', description: 'Working hours, leave, work from home and conduct. Please read and acknowledge.', fileId: handbook, requiresAck: true });
  const it = await kit.uploadPdf(hr, 'company_document', 'it-acceptable-use.pdf', 'IT acceptable use policy', ['Use company laptops for work.', 'Lock your screen when away.', 'Report lost devices to IT within a day.']);
  await kit.post(hr, '/documents/policies', { title: 'IT acceptable use', description: 'How to use company laptops, accounts and data safely.', fileId: it, requiresAck: true });
  const hbId = hb.policy?.id ?? (hb as unknown as { id: string }).id;
  for (const e of ctx.employees.slice(0, Math.ceil(ctx.employees.length * 0.6))) {
    await kit.post(await ctx.as(e), `/documents/policies/${hbId}/acknowledge`, {});
  }
}

/** Laptops, phones and ID cards, most of them handed out. */
export async function seedAssets(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'assets', "SELECT 1 FROM asset WHERE company_id = $1 AND tag = 'LAP-OLD-7'", 'SELECT 1 FROM asset WHERE company_id = $1 LIMIT 1')) return;
  await kit.get(hr, '/lifecycle/asset-types'); // creates the default types on first use
  const typeId = async (name: string) => {
    const r = await db.query<{ id: string }>('SELECT id FROM asset_type WHERE company_id = $1 AND lower(name) = lower($2)', [companyId, name]);
    if (r.rows[0]) return r.rows[0].id;
    return (await kit.post<{ type: { id: string } }>(hr, '/lifecycle/asset-types', { name })).type.id;
  };
  const laptop = await typeId('Laptop'); const phone = await typeId('Mobile phone'); const card = await typeId('ID card');
  const models = ['MacBook Pro 14 M3', 'Dell Latitude 7440', 'ThinkPad X1 Carbon Gen 11', 'MacBook Air 13 M2'];
  for (const [i, e] of ctx.employees.entries()) {
    const l = await kit.post<{ id: string }>(hr, '/lifecycle/assets', { tag: `LAP-${String(101 + i)}`, name: pick(models, i), typeId: laptop, serial: `SN${(734211 + i * 97).toString(36).toUpperCase()}`,
      condition: i % 5 === 0 ? 'new' : 'good', purchaseDate: addDaysIso(e.joinDate ?? ctx.today, -10), cost: pick(['185000.00', '160000.00', '210000.00', '145000.00'], i) });
    await kit.post(hr, `/lifecycle/assets/${l.id}/assign`, { employeeId: e.id, assignedOn: e.joinDate && e.joinDate < ctx.today ? e.joinDate : ctx.today });
    const c = await kit.post<{ id: string }>(hr, '/lifecycle/assets', { tag: `ID-${String(1001 + i)}`, name: 'Staff ID card', typeId: card, condition: 'good' });
    await kit.post(hr, `/lifecycle/assets/${c.id}/assign`, { employeeId: e.id, assignedOn: e.joinDate && e.joinDate < ctx.today ? e.joinDate : ctx.today });
    if (/sales|growth|success|manager|head/i.test(e.designation ?? '')) {
      const p = await kit.post<{ id: string }>(hr, '/lifecycle/assets', { tag: `PH-${String(201 + i)}`, name: pick(['Samsung Galaxy A55', 'iPhone 13'], i), typeId: phone, condition: 'good', cost: '65000.00' });
      await kit.post(hr, `/lifecycle/assets/${p.id}/assign`, { employeeId: e.id });
    }
  }
  await kit.post(hr, '/lifecycle/assets', { tag: 'LAP-SPARE-1', name: 'Dell Latitude 7440 (spare)', typeId: laptop, condition: 'good', notes: 'Kept for new joiners.' });
  await kit.post(hr, '/lifecycle/assets', { tag: 'LAP-OLD-7', name: 'MacBook Air 2017', typeId: laptop, condition: 'poor', notes: 'Battery worn out; to be disposed.' });
}

/** Onboarding for the latest joiners (part done), a probation review waiting for HR, and a resignation in clearance. */
export async function seedLifecycle(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'lifecycle', 'SELECT 1 FROM employee_exit WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM lifecycle_case WHERE company_id = $1 LIMIT 1')) return;
  await kit.get(hr, '/lifecycle/templates');
  const newest = [...ctx.employees].sort((a, b) => (b.joinDate ?? '').localeCompare(a.joinDate ?? '')).slice(0, 2);
  for (const [k, e] of newest.entries()) {
    const c = await kit.post<{ id: string }>(hr, '/lifecycle/cases', { employeeId: e.id, anchorDate: k === 0 ? addDaysIso(ctx.today, 5) : addDaysIso(ctx.today, -12) });
    const detail = await kit.get<{ tasks: Array<{ id: string; assignee_type: string }> }>(hr, `/lifecycle/cases/${c.id}`);
    const done = k === 0 ? 2 : Math.ceil(detail.tasks.length * 0.6);
    for (const t of detail.tasks.slice(0, done)) await kit.patch(hr, `/lifecycle/tasks/${t.id}`, { status: 'done', note: 'Done' });
  }

  // Probation: the newest joiner's manager recommends confirmation; it waits for HR in Approvals.
  const onProbation = newest[1] ?? newest[0]!;
  await db.query('UPDATE employee SET probation_end = $2 WHERE id = $1 AND probation_end IS NULL', [onProbation.id, addDaysIso(ctx.today, 9)]);
  const mgr = await ctx.managerOf(onProbation);
  await kit.post(mgr, '/lifecycle/probation/reviews', { employeeId: onProbation.id, rating: 4, recommendation: 'confirm',
    comments: 'Settled in quickly, owns the dashboard work end to end and helps others. Recommend confirming on time.' });

  // A resignation accepted by HR, with the last working day in three weeks and clearance under way.
  const leaver = ctx.employees.find((e) => /network|technician|analyst/i.test(e.designation ?? '')) ?? ctx.employees[ctx.employees.length - 3]!;
  const exit = await kit.post<{ id: string }>(hr, '/lifecycle/exits', { employeeId: leaver.id, kind: 'resignation', lastWorkingDay: addDaysIso(ctx.today, 21),
    reasonCategory: 'Higher studies', reason: 'Joining a master\'s programme in Australia in February.' });
  const x = await kit.get<{ cases: Array<{ id: string; kind: string; employee_id: string }> }>(hr, '/lifecycle/cases?kind=offboarding');
  const cs = x.cases.find((c) => c.employee_id === leaver.id);
  if (cs) {
    const detail = await kit.get<{ tasks: Array<{ id: string }> }>(hr, `/lifecycle/cases/${cs.id}`);
    for (const t of detail.tasks.slice(0, 2)) await kit.patch(hr, `/lifecycle/tasks/${t.id}`, { status: 'done', note: 'Handover notes received.' });
  }
  void exit;
}

/** Help desk tickets in every state, with replies. */
export async function seedHelpdesk(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'helpdesk', "SELECT 1 FROM helpdesk_ticket WHERE company_id = $1 AND subject = 'Parking space for two-wheelers'", 'SELECT 1 FROM helpdesk_ticket WHERE company_id = $1 LIMIT 1')) return;
  const cats = (await kit.get<{ categories: Array<{ id: string; name: string }> }>(hr, '/helpdesk/categories')).categories;
  const cat = (re: RegExp) => (cats.find((c) => re.test(c.name)) ?? cats[0]!).id;
  const tickets: Array<{ who: number; cat: RegExp; subject: string; body: string; priority?: string; reply?: string; status?: string; rate?: number }> = [
    { who: 3, cat: /payroll/i, subject: 'SSF deduction looks higher this month', body: 'My SSF deduction in Bhadra is Rs 400 more than Shrawan. Was there a change in basic salary?', reply: 'Your basic was revised from Bhadra, so the 11% SSF share went up too. The revision letter is in your documents.', status: 'resolved', rate: 5 },
    { who: 5, cat: /leave|attendance/i, subject: 'Missed punch on Wednesday', body: 'The device at the door was offline in the morning, so my clock-in is missing. I arrived at 9:20.', reply: 'Please raise a missed punch correction from My attendance and I will approve it today.', status: 'waiting' },
    { who: 7, cat: /document/i, subject: 'Need an experience letter for my old role', body: 'Could I get a letter for my time in the support team before the transfer?', status: 'in_progress', reply: 'Drafting it now.' },
    { who: 8, cat: /it/i, subject: 'VPN keeps disconnecting from home', body: 'Since yesterday the VPN drops every 10 minutes when I work from home.', priority: 'high' },
    { who: 10, cat: /other/i, subject: 'Parking space for two-wheelers', body: 'Is there any plan to add more two-wheeler parking near the office? The lane is full by 9:30.' },
  ];
  for (const t of tickets) {
    const e = ctx.employees[t.who % ctx.employees.length]!;
    const me = await ctx.as(e);
    const r = await kit.post<{ ticket: { id: string } }>(me, '/helpdesk/tickets', { categoryId: cat(t.cat), subject: t.subject, body: t.body, priority: t.priority ?? 'normal' });
    const id = r.ticket?.id ?? (r as unknown as { id: string }).id;
    if (t.reply) await kit.post(hr, `/helpdesk/tickets/${id}/messages`, { body: t.reply });
    if (t.status) await kit.patch(hr, `/helpdesk/tickets/${id}`, { status: t.status });
    if (t.rate) await kit.post(me, `/helpdesk/tickets/${id}/rate`, { rating: t.rate, comment: 'Quick and clear, thank you.' });
  }
  void db; void companyId;
}
