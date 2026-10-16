import { alreadySeeded, type DemoContext } from './context.js';
import { addDaysIso, makePdf, nptInstant, pick, type Actor } from './kit.js';

const instanceOf = async (ctx: DemoContext, type: string, id: string) =>
  (await ctx.db.query<{ id: string }>('SELECT id FROM workflow_instance WHERE object_type = $1 AND object_id = $2', [type, id])).rows[0]?.id ?? null;
const approveAs = async (ctx: DemoContext, who: Actor, type: string, id: string) => {
  const inst = await instanceOf(ctx, type, id);
  if (inst) await ctx.kit.post(who, `/workflow/${inst}/approve`, {});
};

/** Company and team goals with key results and check-ins, a KPI library, competencies, a review cycle in self-review, 1-on-1s, feedback and journal notes. */
export async function seedPerformance(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId, today } = ctx;
  if (await alreadySeeded(ctx, 'performance', 'SELECT 1 FROM perf_journal WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM perf_goal WHERE company_id = $1 LIMIT 1')) return;
  const fyEnd = addDaysIso(today, 270);
  const goal = async (who: Actor, b: Record<string, unknown>) => (await kit.post<{ goal: { id: string }; keyResults?: Array<{ id: string }> }>(who, '/performance/goals', b));
  const c1 = await goal(hr, { scope: 'company', title: 'Grow GPU cloud revenue to NPR 12 crore this fiscal year', weight: 40, dueDate: fyEnd,
    description: 'Win enterprise and research customers in Nepal and keep the ones we have.',
    keyResults: [{ title: 'Annual recurring revenue (NPR crore)', measure: 'number', startValue: 4, targetValue: 12, currentValue: 6.5, unit: 'crore' },
      { title: 'Enterprise customers', measure: 'number', startValue: 3, targetValue: 10, currentValue: 5 }] });
  await goal(hr, { scope: 'company', title: 'Keep the platform up 99.95% of the time', weight: 30, dueDate: fyEnd,
    keyResults: [{ title: 'Monthly uptime', measure: 'percent', startValue: 99.5, targetValue: 99.95, currentValue: 99.91 }, { title: 'Incidents closed within SLA', measure: 'percent', startValue: 70, targetValue: 95, currentValue: 88 }] });
  await goal(hr, { scope: 'company', title: 'Make CoreValley a place people recommend', weight: 30, dueDate: fyEnd,
    keyResults: [{ title: 'eNPS', measure: 'number', startValue: 10, targetValue: 40, currentValue: 24 }, { title: 'Regretted attrition', measure: 'percent', startValue: 12, targetValue: 5, currentValue: 7 }] });

  const units = await kit.get<{ units: Array<{ id: string; code: string; kind: string; head_employee_id: string | null }> }>(hr, '/people/structure');
  const eng = units.units.find((u) => u.code === 'PRD');
  if (eng) await goal(hr, { scope: 'department', orgUnitId: eng.id, parentId: c1.goal.id, title: 'Launch inference endpoints for enterprise customers', weight: 50, dueDate: addDaysIso(today, 120),
    keyResults: [{ title: 'Customers live on inference endpoints', measure: 'number', startValue: 0, targetValue: 4, currentValue: 1 }, { title: 'p95 latency under 300 ms', measure: 'boolean', targetValue: 1, currentValue: 0 }] });

  const reports = ctx.employees.filter((e) => e.managerId).slice(0, 8);
  const titles = ['Cut GPU node provisioning time to under 10 minutes', 'Close three new enterprise deals', 'Automate the monthly billing run', 'Raise test coverage of the console to 80%',
    'Reduce support first-response time to 2 hours', 'Ship the mobile app beta', 'Document every runbook for on-call', 'Publish two case studies'];
  for (const [i, e] of reports.entries()) {
    const mgr = await ctx.managerOf(e);
    const g = await goal(mgr, { scope: 'individual', employeeId: e.id, parentId: i % 2 ? null : c1.goal.id, title: pick(titles, i), weight: 100, dueDate: addDaysIso(today, 90 + i * 10),
      keyResults: [{ title: 'Progress', measure: 'percent', startValue: 0, targetValue: 100, currentValue: 10 + ((i * 17) % 70) }] });
    const kr = (await db.query<{ id: string }>('SELECT id FROM perf_key_result WHERE goal_id = $1 LIMIT 1', [g.goal.id]).catch(() => ({ rows: [] as Array<{ id: string }> }))).rows[0];
    await kit.post(await ctx.as(e), `/performance/goals/${g.goal.id}/checkins`, { ...(kr ? { keyResultId: kr.id, value: 20 + ((i * 17) % 70) } : { progress: 25 }), confidence: 1 + (i % 3), comment: pick(['On track; the hard part is done.', 'Blocked on hardware delivery, catching up next week.', 'Good progress, two customers in pilot.'], i) });
  }

  await kit.post(hr, '/performance/kpis', { designation: 'Backend Engineer', title: 'Deployments without rollback', measure: 'percent', defaultTarget: 95, unit: '%' });
  await kit.post(hr, '/performance/kpis', { designation: 'Enterprise Sales', title: 'New annual contract value (NPR lakh)', measure: 'number', defaultTarget: 150, unit: 'lakh' });
  await kit.post(hr, '/performance/kpis', { designation: 'Customer Success', title: 'Customer satisfaction score', measure: 'percent', defaultTarget: 90, unit: '%' });
  const levels = (a: string, b: string, c: string) => [{ level: 1, text: a }, { level: 3, text: b }, { level: 5, text: c }];
  await kit.post(hr, '/performance/competencies', { kind: 'skill', name: 'Problem solving', description: 'Breaks problems down and finds sound solutions.', indicators: levels('Solves routine problems with help.', 'Solves most problems alone and explains the trade-offs.', 'Solves novel, cross-team problems and teaches others.') });
  await kit.post(hr, '/performance/competencies', { kind: 'attitude', name: 'Ownership', description: 'Takes responsibility for outcomes, not just tasks.', indicators: levels('Finishes assigned work.', 'Follows work through to the result and flags risks early.', 'Owns outcomes across teams and raises the bar.') });
  await kit.post(hr, '/performance/competencies', { kind: 'skill', name: 'Communication', description: 'Writes and speaks clearly for the audience.', indicators: levels('Shares updates when asked.', 'Writes clear updates and documents decisions.', 'Aligns stakeholders and handles difficult conversations well.') });
  await kit.post(hr, '/performance/competencies', { kind: 'knowledge', name: 'Cloud and GPU infrastructure', description: 'Understands how our platform works end to end.', designations: [...new Set(ctx.employees.filter((e) => e.orgCode === 'PRD').map((e) => e.designation).filter((d): d is string => !!d))],
    indicators: levels('Knows the main services.', 'Can change and operate the services safely.', 'Designs new parts of the platform.') });

  const cycle = await kit.post<{ cycle: { id: string } }>(hr, '/performance/cycles', { name: 'Mid-year review 2083', periodStart: addDaysIso(today, -180), periodEnd: today,
    goalsWeight: 60, competencyWeight: 40, peerEnabled: true, peerMin: 2, selfDue: addDaysIso(today, 10), managerDue: addDaysIso(today, 20), peerDue: addDaysIso(today, 20), calibrationDue: addDaysIso(today, 30) });
  await kit.post(hr, `/performance/cycles/${cycle.cycle.id}/launch`, {});
  // A few people already submitted their self review.
  const reviews = await db.query<{ id: string; employee_id: string }>('SELECT id, employee_id FROM perf_review WHERE cycle_id = $1', [cycle.cycle.id]);
  for (const [i, r] of reviews.rows.slice(0, 4).entries()) {
    const e = ctx.employees.find((x) => x.id === r.employee_id);
    if (!e?.userId) continue;
    const items = await db.query<{ id: string }>('SELECT id FROM perf_review_item WHERE review_id = $1', [r.id]);
    await kit.put(await ctx.as(e), `/performance/reviews/${r.id}/self`, {
      items: items.rows.map((it, k) => ({ id: it.id, rating: 3 + ((i + k) % 3), comment: pick(['Delivered what I planned.', 'Took on more than expected.', 'Still learning here.'], i + k) })),
      summary: 'A good half: shipped the main goal and helped the new joiners. I want to grow in system design next.', submit: i < 3,
    });
  }

  // 1-on-1s, feedback and journal notes from managers.
  for (const [i, e] of reports.slice(0, 5).entries()) {
    const mgr = await ctx.managerOf(e);
    if (!mgr.employeeId) continue;
    await kit.post(mgr, '/performance/one-on-ones', { withEmployeeId: e.id, scheduledAt: nptInstant(addDaysIso(today, 1 + i), '15:00').toISOString(), cadence: i % 2 ? 'biweekly' : 'weekly',
      agenda: ['How is the current goal going?', pick(['Workload and focus', 'Career growth', 'Feedback on the last release'], i)] });
    await kit.post(mgr, '/performance/journal', { employeeId: e.id, noteDate: addDaysIso(today, -7 - i), tag: i % 3 === 2 ? 'improvement' : 'positive',
      note: pick(['Handled the Nabil cut-over calmly at night and wrote a clear incident note.', 'Helped two new joiners set up their environment without being asked.', 'Missed the estimate on the billing task twice; talked about breaking work down.'], i) });
    const peer = reports[(i + 1) % reports.length]!;
    if (peer.userId && peer.id !== e.id) await kit.post(await ctx.as(peer), '/performance/feedback', { toEmployeeId: e.id, body: pick(['Thanks for the quick review on the scheduler change, it saved me a day.', 'Your demo for the KU team was very clear.', 'Great job keeping the on-call channel calm last week.'], i) });
  }
}

/** A course catalogue with a mandatory safety course, sessions, enrolments (requested, attended, completed) and certificates. */
export async function seedLearning(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId, today } = ctx;
  if (await alreadySeeded(ctx, 'learning', "SELECT 1 FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id WHERE c.company_id = $1 AND c.code = 'K8S-201'", 'SELECT 1 FROM lrn_course WHERE company_id = $1 LIMIT 1')) return;
  const course = async (b: Record<string, unknown>) => (await kit.post<{ course: { id: string } }>(hr, '/learning/courses', b)).course.id;
  const fire = await course({ code: 'FIRE-01', title: 'Fire safety and evacuation', category: 'Health and safety', delivery: 'classroom', durationHours: 2, mandatory: true, validityMonths: 12, passMark: 70,
    description: 'Fire extinguisher use, evacuation routes and assembly points for the Baluwatar office.' });
  await kit.put(hr, `/learning/courses/${fire}/questions`, { questions: [
    { prompt: 'Where is the assembly point for the Baluwatar office?', options: ['Car park in front of the building', 'Rooftop', 'Basement'], correctIndex: 0 },
    { prompt: 'Which extinguisher do you use on an electrical fire?', options: ['Water', 'CO2', 'Foam'], correctIndex: 1 },
    { prompt: 'What do you do first when the alarm sounds?', options: ['Finish your email', 'Leave by the nearest exit', 'Take the lift'], correctIndex: 1 },
  ] });
  const k8s = await course({ code: 'K8S-201', title: 'Kubernetes for GPU workloads', category: 'Technical', delivery: 'blended', durationHours: 12, cost: '15000.00', source: 'internal' });
  const comm = await course({ code: 'COMM-101', title: 'Clear writing for customers', category: 'Communication', delivery: 'online', durationHours: 4, source: 'external', provider: 'Coursera', url: 'https://www.coursera.org', cost: '4500.00' });
  await course({ code: 'POSH-01', title: 'Respect at work (anti-harassment)', category: 'Legal and compliance', delivery: 'online', durationHours: 1, mandatory: true, validityMonths: 24 });

  const fireSession = await kit.post<{ session: { id: string } }>(hr, `/learning/courses/${fire}/sessions`, { startsAt: nptInstant(addDaysIso(today, 1), '14:00').toISOString(), endsAt: nptInstant(addDaysIso(today, 1), '16:00').toISOString(), venue: 'Meeting room 2', trainer: 'Nepal Fire Safety Services', capacity: 30 });
  const k8sSession = await kit.post<{ session: { id: string } }>(hr, `/learning/courses/${k8s}/sessions`, { startsAt: nptInstant(addDaysIso(today, 9), '10:00').toISOString(), endsAt: nptInstant(addDaysIso(today, 10), '17:00').toISOString(), venue: 'Training room', trainer: 'Ram Thapa', capacity: 12 });

  const all = ctx.employees.map((e) => e.id);
  await kit.post(hr, '/learning/enrollments/assign', { courseId: fire, sessionId: fireSession.session.id, employeeIds: all.slice(0, 12) });
  // The session took place three weeks ago (sessions are scheduled ahead, so it is moved back once people are enrolled).
  await kit.patch(hr, `/learning/sessions/${fireSession.session.id}`, { startsAt: nptInstant(addDaysIso(today, -20), '14:00').toISOString(), endsAt: nptInstant(addDaysIso(today, -20), '16:00').toISOString(), status: 'completed' });
  const enr = await db.query<{ id: string; employee_id: string }>('SELECT id, employee_id FROM lrn_enrollment WHERE course_id = $1 ORDER BY employee_id', [fire]);
  for (const [i, en] of enr.rows.entries()) {
    if (i > 8) break;
    await kit.post(hr, `/learning/enrollments/${en.id}/attendance`, { attended: true });
    await kit.post(hr, `/learning/enrollments/${en.id}/complete`, {});
    if (i % 3 === 0) {
      const f = (await kit.post<{ file: { id: string } }>(hr, '/files', { purpose: 'training_certificate', filename: 'fire-safety-certificate.pdf',
        dataUrl: `data:application/pdf;base64,${makePdf('Certificate of completion', ['Fire safety and evacuation', `Issued to employee ${en.employee_id.slice(0, 8)}`, 'Nepal Fire Safety Services']).toString('base64')}` })).file.id;
      await kit.post(hr, `/learning/enrollments/${en.id}/certificate`, { fileId: f, issuedOn: addDaysIso(today, -20), expiresOn: addDaysIso(today, i === 0 ? 20 : 345) });
    }
  }
  const engineers = ctx.employees.filter((e) => e.orgCode === 'PRD' || /engineer/i.test(e.designation ?? ''));
  for (const [i, e] of engineers.slice(0, 4).entries()) {
    if (!e.userId) continue;
    await kit.post(await ctx.as(e), '/learning/enrollments/self', { courseId: k8s, sessionId: k8sSession.session.id, reason: pick(['I run GPU jobs every day and want to schedule them better.', 'Needed for the inference endpoints goal.'], i) });
  }
  const self = await db.query<{ id: string; employee_id: string }>(`SELECT id, employee_id FROM lrn_enrollment WHERE course_id = $1`, [k8s]);
  for (const en of self.rows.slice(0, 2)) {
    const e = ctx.employees.find((x) => x.id === en.employee_id)!;
    await approveAs(ctx, await ctx.managerOf(e), 'training_nomination', en.id).catch(() => undefined);
  }
  const sales = ctx.employees.filter((e) => /sales|success|growth/i.test(e.designation ?? ''));
  if (sales.length) await kit.post(hr, '/learning/enrollments/assign', { courseId: comm, employeeIds: sales.map((e) => e.id) });
}

/** A hiring request, two open jobs on the careers page, candidates across the pipeline, interviews with scorecards, and an offer. */
export async function seedHiring(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId, today } = ctx;
  if (await alreadySeeded(ctx, 'hiring', 'SELECT 1 FROM hiring_offer WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM hiring_requisition WHERE company_id = $1 LIMIT 1')) return;
  const units = await kit.get<{ units: Array<{ id: string; code: string }> }>(hr, '/people/structure');
  const eng = units.units.find((u) => u.code === 'PRD')?.id ?? null;
  const sal = units.units.find((u) => u.code === 'SAL')?.id ?? null;
  const engManager = ctx.employees.find((e) => ctx.employees.some((x) => x.managerId === e.id) && e.orgCode === 'PRD') ?? ctx.employees[1]!;
  const mgrActor = engManager.userId ? await ctx.as(engManager) : hr;

  const req = await kit.post<{ requisition: { id: string } }>(mgrActor, '/hiring/requisitions', { title: 'Senior Backend Engineer', designation: 'Backend Engineer', orgUnitId: eng, headcount: 2,
    salaryMin: '180000', salaryMax: '260000', kind: 'new', justification: 'Two enterprise customers go live on inference endpoints next quarter; the team needs more backend capacity.' });
  await approveAs(ctx, hr, 'job_requisition', req.requisition.id);
  await kit.post(hr, '/hiring/requisitions', { title: 'Sales Development Representative', orgUnitId: sal, headcount: 1, kind: 'new', justification: 'Outbound pipeline for banks and hospitals outside the valley.' });

  const job = await kit.post<{ job: { id: string } }>(hr, '/hiring/jobs', {
    requisitionId: req.requisition.id, title: 'Senior Backend Engineer', category: 'Engineering', location: 'Kathmandu', workMode: 'hybrid', employmentType: 'full_time',
    experienceMin: 4, experienceMax: 10, salaryMin: '180000', salaryMax: '260000', showSalary: true, openings: 2, deadline: addDaysIso(today, 25), orgUnitId: eng, hiringManagerId: engManager.id,
    description: 'Build the APIs and schedulers behind Nepal\'s first GPU cloud.\n\nYou will design services that place customer workloads on GPUs, meter usage and bill it accurately.',
    requirements: '- 4+ years building backend services in Go, TypeScript or Python\n- Solid PostgreSQL and API design\n- Comfortable with Linux and containers\n- Kubernetes experience is a plus',
    questions: [
      { id: 'q1', kind: 'number', label: 'Years of backend experience', required: true, knockout: { op: 'gte', value: 3 } },
      { id: 'q2', kind: 'yes_no', label: 'Can you work from our Kathmandu office three days a week?', required: true, knockout: { op: 'eq', value: true } },
      { id: 'q3', kind: 'text', label: 'Link to your GitHub or portfolio', required: false },
    ],
  }).catch(async () => kit.post<{ job: { id: string } }>(hr, '/hiring/jobs', { requisitionId: req.requisition.id, title: 'Senior Backend Engineer', category: 'Engineering', location: 'Kathmandu',
    workMode: 'hybrid', experienceMin: 4, experienceMax: 10, salaryMin: '180000', salaryMax: '260000', showSalary: true, openings: 2, deadline: addDaysIso(today, 25), orgUnitId: eng,
    description: 'Build the APIs and schedulers behind Nepal\'s first GPU cloud.', requirements: '4+ years building backend services.' }));
  await kit.post(hr, `/hiring/jobs/${job.job.id}/publish`, {});
  const job2 = await kit.post<{ job: { id: string } }>(hr, '/hiring/jobs', { title: 'Customer Success Associate', category: 'Customer success', location: 'Kathmandu', workMode: 'onsite', employmentType: 'full_time',
    experienceMin: 1, experienceMax: 3, deadline: addDaysIso(today, 18), orgUnitId: sal, description: 'Help research labs and enterprises get the most from CoreValley.', requirements: '- Clear written English\n- 1+ year in a customer-facing role' });
  await kit.post(hr, `/hiring/jobs/${job2.job.id}/publish`, {});

  const cv = async (name: string) => (await kit.post<{ file: { id: string } }>(hr, '/files', { purpose: 'hiring_cv', filename: `${name.replace(/\s+/g, '-')}-CV.pdf`,
    dataUrl: `data:application/pdf;base64,${makePdf(`${name} - Curriculum Vitae`, ['Backend engineer', 'Experience: Go, PostgreSQL, Kubernetes', 'Education: BE Computer, Pulchowk Campus']).toString('base64')}` })).file.id;
  const people: Array<[string, string, number, string, string]> = [
    ['Aashish Pandey', 'aashish.pandey@example.com', 6, 'Leapfrog Technology', 'linkedin'],
    ['Pooja Shrestha', 'pooja.shrestha@example.com', 5, 'F1Soft International', 'careers_page'],
    ['Nabin Gautam', 'nabin.gautam@example.com', 8, 'Fusemachines', 'referral'],
    ['Srijana Rai', 'srijana.rai@example.com', 4, 'Cloud Factory', 'careers_page'],
    ['Ujjwal Karki', 'ujjwal.karki@example.com', 2, 'Freelance', 'job_board'],
    ['Riya Thapa', 'riya.thapa@example.com', 7, 'Daraz Nepal', 'linkedin'],
  ];
  const apps: string[] = [];
  for (const [name, email, years, employer, source] of people) {
    const a = await kit.post<{ application: { id: string } } & { id?: string }>(hr, `/hiring/jobs/${job.job.id}/applications`, {
      name, email, phone: `98${String(10000000 + name.length * 1234567).slice(0, 8)}`, source, cvFileId: await cv(name), experienceYears: years, currentEmployer: employer,
      answers: { q1: years, q2: years !== 2, q3: `https://github.com/${name.split(' ')[0]!.toLowerCase()}` },
    }).catch(async (e: Error) => { if (/source/.test(e.message)) return kit.post<{ application: { id: string } } & { id?: string }>(hr, `/hiring/jobs/${job.job.id}/applications`, { name, email, cvFileId: await cv(name), experienceYears: years, currentEmployer: employer, answers: { q1: years, q2: true } }); throw e; });
    apps.push(a.application?.id ?? a.id!);
  }
  const stages = (await db.query<{ stages: string[] }>('SELECT stages FROM hiring_job WHERE id = $1', [job.job.id])).rows[0]!.stages;
  const stage = (re: RegExp) => stages.find((s) => re.test(s)) ?? stages[1]!;
  await kit.post(hr, `/hiring/applications/${apps[0]}/move`, { stage: stage(/screen/i) });
  await kit.post(hr, `/hiring/applications/${apps[0]}/move`, { stage: stage(/interview/i) });
  await kit.post(hr, `/hiring/applications/${apps[1]}/move`, { stage: stage(/screen/i) });
  await kit.post(hr, `/hiring/applications/${apps[2]}/move`, { stage: stage(/screen/i) });
  await kit.post(hr, `/hiring/applications/${apps[2]}/move`, { stage: stage(/interview/i) });
  await kit.post(hr, `/hiring/applications/${apps[2]}/move`, { stage: stage(/offer/i) });
  await kit.post(hr, `/hiring/applications/${apps[4]}/reject`, { reason: 'Less than three years of backend experience.', notifyCandidate: true });

  const panel = [hr.userId, ...(engManager.userId ? [engManager.userId] : [])];
  const iv1 = await kit.post<{ interview: { id: string } } & { id?: string }>(hr, `/hiring/applications/${apps[0]}/interviews`, { title: 'Technical interview', date: addDaysIso(today, 2), time: '11:00', durationMin: 60, mode: 'video', location: 'Google Meet', panel });
  void iv1;
  const iv2 = await kit.post<{ interview: { id: string } } & { id?: string }>(hr, `/hiring/applications/${apps[2]}/interviews`, { title: 'System design', date: addDaysIso(today, 1), time: '14:00', durationMin: 60, mode: 'in_person', location: 'Meeting room 1', panel });
  const ivId = iv2.interview?.id ?? iv2.id!;
  // It happened last week (interviews can only be booked ahead, so move it back before the panel scores it).
  await db.query('UPDATE hiring_interview SET starts_at = $2 WHERE id = $1', [ivId, nptInstant(addDaysIso(today, -5), '14:00')]);
  await kit.post(hr, `/interviews/${ivId}/scorecard`.replace('/interviews', '/hiring/interviews'), { ratings: { 'System design': 5, 'Coding': 4, 'Communication': 4, 'Culture add': 5 }, recommendation: 'strong_yes', notes: 'Designed the GPU scheduler cleanly and asked sharp questions about billing.' });
  if (engManager.userId) await kit.post(mgrActor, `/hiring/interviews/${ivId}/scorecard`, { ratings: { 'System design': 4, 'Coding': 5, 'Communication': 4 }, recommendation: 'yes', notes: 'Strong coder; would pair well with the platform team.' });
  const offer = await kit.post<{ offer: { id: string } } & { id?: string }>(hr, `/hiring/applications/${apps[2]}/offers`, { designation: 'Senior Backend Engineer', salary: '235000', joinDate: addDaysIso(today, 30), expiresOn: addDaysIso(today, 10), notes: 'Includes laptop and hybrid work.' });
  void offer;
}
