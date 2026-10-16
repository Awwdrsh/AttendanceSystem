import { alreadySeeded, type DemoContext, type DemoEmployee } from './context.js';
import { addDaysIso, nptInstant, pick, weekStart } from './kit.js';

const isWorkday = (iso: string) => new Date(`${iso}T00:00:00Z`).getUTCDay() !== 6; // Saturday is the weekly off

/** Approve a workflow instance as the given actor (the inbox's one-tap approve). */
async function approve(ctx: DemoContext, who: import('./kit.js').Actor, instanceId: string | null, reason?: string): Promise<void> {
  if (instanceId) await ctx.kit.post(who, `/workflow/${instanceId}/approve`, reason ? { reason } : {});
}

/** Clients, projects and activities; two weeks of time for the engineering and growth teams; timesheets in every state. */
export async function seedTime(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'time', 'SELECT 1 FROM timesheet WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM time_client WHERE company_id = $1 LIMIT 1')) return;
  const nabil = await kit.post<{ client: { id: string } }>(hr, '/time/clients', { name: 'Nabil Bank', notes: 'GPU cluster for fraud models.' });
  const kathmandu = await kit.post<{ client: { id: string } }>(hr, '/time/clients', { name: 'Kathmandu University AI Lab', notes: 'Research compute partnership.' });
  const proj = async (b: Record<string, unknown>) => (await kit.post<{ project: { id: string } }>(hr, '/time/projects', b)).project.id;
  const p1 = await proj({ code: 'NBL-GPU', name: 'Nabil GPU cluster rollout', clientId: nabil.client.id, color: '#12773a', billable: true, billRate: 4500, budgetHours: 400, allMembers: true });
  const p2 = await proj({ code: 'KU-LAB', name: 'KU AI Lab onboarding', clientId: kathmandu.client.id, color: '#1c7ed6', billable: true, billRate: 3000, budgetHours: 160, allMembers: true });
  const p3 = await proj({ code: 'CONSOLE', name: 'Cloud console v2', color: '#7048e8', billable: false, budgetHours: 600, allMembers: true });
  const p4 = await proj({ code: 'INTERNAL', name: 'Internal and admin', color: '#868e96', billable: false, allMembers: true });
  const act = async (name: string, projectId?: string) => (await kit.post<{ activity: { id: string } }>(hr, '/time/activities', { name, ...(projectId ? { projectId } : {}) })).activity.id;
  const dev = await act('Development'); const review = await act('Code review'); const meet = await act('Meetings'); const sup = await act('Support');
  const deploy = await act('Deployment', p1);

  const trackers = ctx.employees.filter((e) => e.managerId && /engineer|designer|sales|success|growth|reliab|network|technician|qa/i.test(e.designation ?? '')).slice(0, 8);
  const thisWeek = weekStart(ctx.today);
  const weeks = [addDaysIso(thisWeek, -14), addDaysIso(thisWeek, -7), thisWeek];
  for (const [i, e] of trackers.entries()) {
    const me = await ctx.as(e);
    for (const ws of weeks) {
      for (let d = 0; d < 7; d++) {
        const day = addDaysIso(ws, d);
        if (!isWorkday(day) || day > ctx.today) continue;
        const plan: Array<[string, string, string, string, string]> = [
          ['09:30', '12:30', pick([p1, p3, p2], i + d), pick([p1, p3, p2], i + d) === p1 && (i + d) % 3 === 2 ? deploy : dev, pick(['GPU node health checks', 'Billing API endpoints', 'Terraform for the new racks', 'Console usage charts', 'Load test for inference API'], i + d)],
          ['13:30', '15:00', pick([p3, p1], i), pick([review, dev], d), pick(['Reviewed pull requests', 'Pairing on the scheduler', 'Fixed flaky tests'], i + d)],
          ['15:15', '16:00', p4, meet, pick(['Team stand-up and planning', 'Weekly sync', 'Customer call notes'], d)],
          ['16:00', '17:45', pick([p2, p1, p3], i + d + 1), pick([sup, dev], i), pick(['Helped KU researchers with quota', 'Investigated latency alert', 'Documentation updates'], i * d)],
        ];
        for (const [start, end, projectId, activityId, note] of plan) {
          if (nptInstant(day, end).getTime() > Date.now()) continue;
          await kit.post(me, '/time/entries', { date: day, start, end, projectId, activityId, note });
        }
      }
    }
    // Two weeks ago: submitted and approved. Last week: submitted (most approved, some waiting). This week: still open.
    const t1 = await kit.post<{ timesheet: { id: string; workflow_instance_id?: string } }>(me, '/time/timesheets', { weekStart: weeks[0] });
    const t2 = await kit.post<{ timesheet: { id: string } }>(me, '/time/timesheets', { weekStart: weeks[1], note: i % 3 === 0 ? 'Worked late on Thursday for the Nabil cut-over.' : null });
    const mgr = await ctx.managerOf(e);
    const inst = async (id: string) => (await db.query<{ id: string }>(`SELECT id FROM workflow_instance WHERE object_type = 'timesheet' AND object_id = $1`, [id])).rows[0]?.id ?? null;
    await approve(ctx, mgr, await inst(t1.timesheet.id));
    if (i % 3 !== 0) await approve(ctx, mgr, await inst(t2.timesheet.id), 'Looks right, thanks.');
  }
}

/** Office location with a geofence, a reception kiosk, field staff with client visits, and breaks. */
export async function seedField(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if (await alreadySeeded(ctx, 'field', 'SELECT 1 FROM work_break WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM kiosk WHERE company_id = $1 LIMIT 1')) return;
  const locs = await kit.get<{ locations: Array<{ id: string }> }>(hr, '/field/locations');
  let hq = locs.locations[0]?.id;
  if (!hq) hq = (await kit.post<{ location: { id: string } }>(hr, '/field/locations', { name: 'Kathmandu HQ', address: 'Baluwatar, Kathmandu', branchId: ctx.employees[0]!.branchId, lat: 27.7286, lng: 85.3311, radiusM: 150 })).location.id;
  await kit.post(hr, '/field/locations', { name: 'Datacenter, Bhaisepati', address: 'Bhaisepati, Lalitpur', lat: 27.6466, lng: 85.3036, radiusM: 200 });
  await kit.post(hr, '/field/kiosks', { name: 'Reception tablet', branchId: ctx.employees[0]!.branchId, locationId: hq, selfieRequired: true });

  const fieldStaff = ctx.employees.filter((e) => /sales|success|growth|technician/i.test(e.designation ?? '')).slice(0, 4);
  for (const e of fieldStaff) await kit.put(hr, `/field/staff/${e.id}`, { fieldWork: true });
  const places: Array<[string, string, number, number, string]> = [
    ['Nabil Bank, Kamaladi', 'Quarterly business review', 27.7085, 85.3206, 'Agreed to extend the GPU contract by six months.'],
    ['Kathmandu University, Dhulikhel', 'Onboarding workshop for researchers', 27.6186, 85.5386, 'Ran the workshop for 24 students; follow-up on quotas next week.'],
    ['Leapfrog Technology, Charkhal', 'Product demo', 27.6986, 85.3192, 'Demoed inference endpoints; they want a trial.'],
    ['Datacenter, Bhaisepati', 'Rack installation check', 27.6466, 85.3036, 'Two new racks powered and tested.'],
    ['Himalayan Bank, Thamel', 'Contract negotiation', 27.7149, 85.3123, 'Sent the revised quote.'],
    ['Ncell office, Lainchaur', 'Partnership discussion', 27.7181, 85.3155, 'Next step: technical deep dive.'],
  ];
  const rows: unknown[][] = [];
  for (const [i, e] of fieldStaff.entries()) {
    for (let back = 1; back <= 6; back++) {
      const day = addDaysIso(ctx.today, -back);
      if (!isWorkday(day) || (back + i) % 2) continue;
      const [place, purpose, lat, lng, outcome] = pick(places, i + back);
      const start = nptInstant(day, pick(['10:15', '11:00', '14:00', '15:30'], i + back));
      const end = new Date(start.getTime() + (75 + ((i * 17 + back * 13) % 90)) * 60_000);
      rows.push([companyId, e.id, day, place, purpose, start, lat + 0.0003, lng - 0.0002, 12 + i, end, lat, lng, 9 + back, outcome]);
    }
  }
  for (const r of rows) {
    await db.query(
      `INSERT INTO field_visit (company_id, employee_id, local_date, place, purpose, started_at, start_lat, start_lng, start_accuracy_m, ended_at, end_lat, end_lng, end_accuracy_m, outcome)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)`, r);
  }
  // Breaks over the last few days (one ran over its limit), recorded as the app would.
  const office = ctx.employees.filter((e) => !fieldStaff.includes(e)).slice(0, 8);
  for (const [i, e] of office.entries()) {
    for (let back = 0; back <= 3; back++) {
      const day = addDaysIso(ctx.today, -back);
      if (!isWorkday(day)) continue;
      const lunchStart = nptInstant(day, pick(['13:00', '13:10', '13:20', '12:50'], i + back));
      const lunchMin = back === 0 && i === 2 ? 72 : 35 + ((i + back) % 4) * 5;
      if (back === 0 && nptInstant(day, '14:30').getTime() > Date.now()) continue;
      await db.query(
        `INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at, ended_at, minutes, over_limit, source)
         VALUES ($1, $2, $3, 'lunch', false, 60, $4, $5, $6, $7, 'web')`,
        [companyId, e.id, day, lunchStart, new Date(lunchStart.getTime() + lunchMin * 60_000), lunchMin, lunchMin > 60]);
      if ((i + back) % 2 === 0) {
        const tea = nptInstant(day, '16:00');
        if (tea.getTime() < Date.now()) {
          await db.query(
            `INSERT INTO work_break (company_id, employee_id, local_date, kind, paid, max_min, started_at, ended_at, minutes, over_limit, source)
             VALUES ($1, $2, $3, 'tea', true, 15, $4, $5, 12, false, 'web')`, [companyId, e.id, day, tea, new Date(tea.getTime() + 12 * 60_000)]);
        }
      }
    }
  }
}

/** A morning and an evening shift for operations, next week's roster published, and a shift swap waiting for approval. */
export async function seedRoster(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  if ((await db.query(`SELECT 1 FROM shift WHERE company_id = $1 AND code = 'EARLY'`, [companyId])).rows.length) return;
  const early = await kit.post<{ shift: { id: string } } & { id?: string }>(hr, '/shifts', { code: 'EARLY', nameEn: 'Early 07:00-15:00', start: '07:00', end: '15:00', graceInMin: 10 });
  const late = await kit.post<{ shift: { id: string } } & { id?: string }>(hr, '/shifts', { code: 'LATE', nameEn: 'Late 13:00-21:00', start: '13:00', end: '21:00', graceInMin: 10 });
  const earlyId = early.shift?.id ?? early.id!; const lateId = late.shift?.id ?? late.id!;
  const gen = (await db.query<{ id: string }>(`SELECT id FROM shift WHERE company_id = $1 AND code = 'GEN'`, [companyId])).rows[0]?.id ?? null;
  const team: DemoEmployee[] = ctx.employees.filter((e) => /reliab|network|datacenter|technician|support|success|office/i.test(e.designation ?? '')).slice(0, 6);
  if (team.length < 2) return;
  const ws = addDaysIso(weekStart(ctx.today), 7);
  const rows: Array<{ employeeId: string; localDate: string; shiftId: string | null }> = [];
  for (const [i, e] of team.entries()) {
    for (let d = 0; d < 7; d++) {
      const day = addDaysIso(ws, d);
      rows.push({ employeeId: e.id, localDate: day, shiftId: d === 6 ? null : (i + d) % 3 === 0 ? lateId : (i + d) % 3 === 1 ? earlyId : gen });
    }
  }
  await kit.put(hr, '/shifts/roster', { rows });
  await kit.post(hr, '/shifts/roster/publish', { from: ws, to: addDaysIso(ws, 6) });
  // Swap: two people on different shifts the same day.
  for (let d = 0; d < 6; d++) {
    const day = addDaysIso(ws, d);
    const a = rows.find((r) => r.localDate === day && r.shiftId === earlyId);
    const b = rows.find((r) => r.localDate === day && r.shiftId === lateId);
    if (a && b) {
      const ea = team.find((t) => t.id === a.employeeId)!; const eb = team.find((t) => t.id === b.employeeId)!;
      const s = await kit.post<{ swap: { id: string } } & { id?: string }>(await ctx.as(ea), '/field/swaps', { colleagueId: eb.id, localDate: day, reason: 'Family function in the morning.' });
      await kit.post(await ctx.as(eb), `/field/swaps/${s.swap?.id ?? s.id}/accept`, { note: 'Happy to swap.' });
      break;
    }
  }
}
