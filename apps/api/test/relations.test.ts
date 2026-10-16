import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { relationsDaily } from '../src/modules/relations/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hrUser: string; let hr2: string; let sita: string; let sitaUser: string; let ram: string; let member: string; let memberUser: string; let outsider: string; let otherHr: string;
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;
const api = (token: string) => ({
  get: (p: string) => request(app).get(`/api/v1/relations${p}`).set(auth(token)),
  post: (p: string, b: object = {}) => request(app).post(`/api/v1/relations${p}`).set(auth(token)).send(b),
  put: (p: string, b: object) => request(app).put(`/api/v1/relations${p}`).set(auth(token)).send(b),
  patch: (p: string, b: object) => request(app).patch(`/api/v1/relations${p}`).set(auth(token)).send(b),
  del: (p: string) => request(app).delete(`/api/v1/relations${p}`).set(auth(token)),
});

async function newEmployee(code: string, extra: Record<string, unknown> = {}): Promise<string> {
  const row: Record<string, unknown> = { company_id: DEMO.companyId, emp_code: code, name_en: `Person ${code}`, join_date: '2020-01-01', ...extra };
  const cols = Object.keys(row); const vals = Object.values(row);
  const r = await db.query<{ id: string }>(`INSERT INTO employee (${cols.join(', ')}) VALUES (${cols.map((_, i) => `$${i + 1}`).join(', ')}) RETURNING id`, vals);
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2020-01-01', NULL), $2, 'regular')`, [r.rows[0]!.id, DEMO.branchId]);
  return r.rows[0]!.id;
}

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  ({ token: hr, userId: hrUser } = await loginAs(db, app, ['hr']));
  ({ token: hr2 } = await loginAs(db, app, ['hr']));
  ({ token: sita, userId: sitaUser } = await loginAs(db, app, ['employee'], DEMO.sita));
  ({ token: ram } = await loginAs(db, app, ['employee', 'manager'], DEMO.ram));
  ({ token: member, userId: memberUser } = await loginAs(db, app, ['employee'], await newEmployee('CM1')));
  ({ token: outsider } = await loginAs(db, app, ['employee'], await newEmployee('OUT1')));
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  const c = await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERREL', 'Other Co') RETURNING id`);
  await createUser(db, { companyId: c.rows[0]!.id, email: 'hr@other-rel.local', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  otherHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other-rel.local', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

describe('grievances', () => {
  let gid: string;

  it('HR names the committee; only HR can change it', async () => {
    expect((await api(sita).put('/grievances/committee', { userIds: [memberUser] })).status).toBe(403);
    expect((await api(hr).put('/grievances/committee', { userIds: [memberUser] })).status).toBe(200);
    expect((await api(member).get('/grievances/committee')).body.isMember).toBe(true);
    expect((await api(outsider).get('/grievances/committee')).body.members).toEqual([]);
  });

  it('an employee files a confidential grievance; text is encrypted and the notice reveals nothing', async () => {
    const file = (await request(app).post('/api/v1/files').set(auth(sita)).send({ purpose: 'grievance_attachment', filename: 'note.pdf', dataUrl: PDF })).body.file.id;
    const r = await api(sita).post('/grievances', { category: 'management', subject: 'Unfair shift allocation', description: 'My manager keeps giving me night shifts', concerns: 'Ram', fileIds: [file] });
    expect(r.status).toBe(201);
    expect(r.body.number).toBe(1);
    gid = r.body.id;
    const raw = await db.query('SELECT subject_enc, description_enc FROM grievance WHERE id = $1', [gid]);
    expect(raw.rows[0].description_enc.toString('latin1')).not.toContain('night shifts');
    const n = await db.query<{ user_id: string; body: string; title: string }>(`SELECT user_id, title, body FROM notification_outbox WHERE template = 'relations.grievance.new'`);
    expect(n.rows.map((x) => x.user_id)).toContain(memberUser);
    expect(n.rows.every((x) => !x.body.includes('night') && !x.title.includes('Sita'))).toBe(true);
  });

  it('the line manager, other employees and other companies never see it; committee and HR do (audited)', async () => {
    expect((await api(ram).get(`/grievances/${gid}`)).status).toBe(404);
    expect((await api(ram).get('/grievances?scope=handle')).status).toBe(403);
    expect((await api(outsider).get(`/grievances/${gid}`)).status).toBe(404);
    expect((await api(otherHr).get(`/grievances/${gid}`)).status).toBe(404);
    const m = await api(member).get(`/grievances/${gid}`);
    expect(m.status).toBe(200);
    expect(m.body.grievance.description).toContain('night shifts');
    expect(m.body.grievance.role).toBe('committee');
    const list = await api(member).get('/grievances?scope=handle');
    expect(list.body.grievances[0].description).toBeNull();
    const a = await db.query(`SELECT 1 FROM audit_log WHERE table_name = 'grievance' AND action = 'view' AND row_id = $1`, [gid]);
    expect(a.rows.length).toBeGreaterThan(0);
    expect((await api(sita).get('/grievances')).body.grievances[0].subject).toBe('Unfair shift allocation');
  });

  it('attachments are served only to people who can see the case', async () => {
    const files = (await api(sita).get(`/grievances/${gid}`)).body.files;
    expect((await api(member).get(`/grievances/${gid}/files/${files[0].id}`)).status).toBe(200);
    expect((await api(ram).get(`/grievances/${gid}/files/${files[0].id}`)).status).toBe(404);
  });

  it('internal notes stay with the committee; shared notes reach the employee', async () => {
    expect((await api(member).post(`/grievances/${gid}/notes`, { body: 'Check the roster history' })).status).toBe(201);
    expect((await api(member).post(`/grievances/${gid}/notes`, { body: 'We will meet you on Sunday', shared: true })).status).toBe(201);
    const seen = (await api(sita).get(`/grievances/${gid}`)).body.notes.map((n: { body: string }) => n.body);
    expect(seen).toEqual(['We will meet you on Sunday']);
    expect((await api(sita).post(`/grievances/${gid}/notes`, { body: 'Thank you' })).status).toBe(201);
  });

  it('HR can exclude a committee member with a conflict of interest', async () => {
    expect((await api(member).patch(`/grievances/${gid}`, { excludeUserIds: [hrUser] })).status).toBe(403);
    expect((await api(hr).patch(`/grievances/${gid}`, { excludeUserIds: [memberUser] })).status).toBe(200);
    expect((await api(member).get(`/grievances/${gid}`)).status).toBe(404);
    // Exclusions only grow: HR cannot lift one (an administrator can).
    expect((await api(hr).patch(`/grievances/${gid}`, { excludeUserIds: [] })).status).toBe(403);
    const admin = (await loginAs(db, app, ['admin'])).token;
    expect((await api(admin).patch(`/grievances/${gid}`, { excludeUserIds: [] })).status).toBe(200);
  });

  it('nobody can make themselves the only handler of a grievance', async () => {
    const handlers = (await db.query<{ id: string }>(
      `SELECT id FROM app_user WHERE company_id = $1 AND id <> $2 AND id <> $3 AND ('hr' = ANY(roles) OR 'admin' = ANY(roles) OR id IN (SELECT user_id FROM grievance_committee_member))`,
      [DEMO.companyId, hrUser, sitaUser])).rows.map((r) => r.id);
    expect(handlers.length).toBeGreaterThan(0);
    const r = await api(hr).patch(`/grievances/${gid}`, { excludeUserIds: handlers });
    expect(r.status).toBe(400);
  });

  it('the committee schedules a hearing and decides; the employee appeals once and HR (not the decider) hears it', async () => {
    expect((await api(sita).patch(`/grievances/${gid}`, { status: 'under_review' })).status).toBe(403);
    expect((await api(member).patch(`/grievances/${gid}`, { hearingAt: '2026-11-01T10:00:00+05:45', hearingPlace: 'Board room' })).status).toBe(200);
    expect((await api(sita).post(`/grievances/${gid}/decision`, { outcome: 'upheld', findings: 'x' })).status).toBe(403);
    expect((await api(hr).post(`/grievances/${gid}/decision`, { outcome: 'partly_upheld', findings: 'Shifts will rotate fairly from next month' })).status).toBe(200);
    expect((await api(member).post(`/grievances/${gid}/appeal`, { reason: 'x' })).status).toBe(403);
    expect((await api(sita).post(`/grievances/${gid}/appeal`, { reason: 'Rotation is not enough' })).status).toBe(200);
    expect((await api(sita).post(`/grievances/${gid}/appeal`, { reason: 'again' })).status).toBe(409);
    expect((await api(hr).post(`/grievances/${gid}/appeal-decision`, { outcome: 'decision_upheld', note: 'n' })).status).toBe(403);
    expect((await api(member).post(`/grievances/${gid}/appeal-decision`, { outcome: 'decision_upheld', note: 'n' })).status).toBe(403);
    const r = await api(hr2).post(`/grievances/${gid}/appeal-decision`, { outcome: 'decision_changed', newOutcome: 'upheld', note: 'Upheld in full' });
    expect(r.status).toBe(200);
    const g = (await api(sita).get(`/grievances/${gid}`)).body.grievance;
    expect(g).toMatchObject({ status: 'closed', outcome: 'upheld', appealOutcome: 'decision_changed', appealNote: 'Upheld in full' });
  });

  it('withdrawal before a decision; the daily job closes decided cases after the appeal window', async () => {
    const a = (await api(sita).post('/grievances', { category: 'other', subject: 'Parking', description: 'No parking space' })).body.id;
    expect((await api(sita).post(`/grievances/${a}/withdraw`)).status).toBe(200);
    expect((await api(sita).post(`/grievances/${a}/withdraw`)).status).toBe(409);
    const b = (await api(sita).post('/grievances', { category: 'safety', subject: 'Wet floor', description: 'Kitchen floor is always wet' })).body.id;
    await api(member).post(`/grievances/${b}/decision`, { outcome: 'upheld', findings: 'Mats ordered' });
    const out = await relationsDaily(db, new Date(Date.now() + 20 * 86_400_000));
    expect(out.grievancesClosed).toBeGreaterThanOrEqual(1);
    expect((await api(sita).get(`/grievances/${b}`)).body.grievance.status).toBe('closed');
  });

  it('validates input', async () => {
    expect((await api(sita).post('/grievances', { category: 'gossip', subject: 's', description: 'd' })).status).toBe(400);
    expect((await api(hr).post('/grievances', { category: 'other', subject: 's', description: 'd' })).status).toBe(400);
  });
});

describe('disciplinary action', () => {
  let caseId: string; let stages: Array<{ id: string; code: string; effect: string }>;

  it('seeds the default stages; HR edits them', async () => {
    stages = (await api(hr).get('/disciplinary/stages')).body.stages;
    expect(stages.map((s) => s.code)).toEqual(['verbal_warning', 'written_warning', 'final_warning', 'suspension', 'termination']);
    expect((await api(sita).put('/disciplinary/stages', { stages: [] })).status).toBe(403);
  });

  it('HR opens a case; the employee does not see it until asked to explain', async () => {
    expect((await api(ram).post('/disciplinary', { employeeId: DEMO.sita, category: 'attendance', incidentDate: '2026-10-01', incident: 'x' })).status).toBe(403);
    const r = await api(hr).post('/disciplinary', { employeeId: DEMO.sita, category: 'attendance', incidentDate: '2026-10-01', incident: 'Absent without notice for three days' });
    expect(r.status).toBe(201);
    caseId = r.body.id;
    expect((await api(sita).get('/disciplinary/mine')).body.cases).toEqual([]);
    expect((await api(sita).get(`/disciplinary/${caseId}`)).status).toBe(404);
    expect((await api(ram).get(`/disciplinary/${caseId}`)).status).toBe(404);
    expect((await api(otherHr).get(`/disciplinary/${caseId}`)).status).toBe(404);
  });

  it('show-cause notice, response by the employee, and no early decision without confirmation', async () => {
    expect((await api(hr).post(`/disciplinary/${caseId}/show-cause`, { text: 'Explain the absence', responseDue: '2020-01-01' })).status).toBe(400);
    expect((await api(hr).post(`/disciplinary/${caseId}/show-cause`, { text: 'Explain your absence on 1 to 3 Kartik', responseDue: '2099-01-01' })).status).toBe(200);
    expect((await api(hr).post(`/disciplinary/${caseId}/decide`, { stageId: stages[0]!.id, note: 'n' })).status).toBe(409);
    const mine = (await api(sita).get('/disciplinary/mine')).body.cases;
    expect(mine[0]).toMatchObject({ status: 'show_cause', showCause: 'Explain your absence on 1 to 3 Kartik', stageLetter: null });
    expect((await api(ram).post(`/disciplinary/${caseId}/respond`, { response: 'x' })).status).toBe(404);
    expect((await api(sita).post(`/disciplinary/${caseId}/respond`, { response: 'My mother was in hospital' })).status).toBe(200);
    expect((await api(sita).post(`/disciplinary/${caseId}/respond`, { response: 'again' })).status).toBe(409);
  });

  it('a suspension is decided with dates and goes on the career timeline; a warning letter can be linked', async () => {
    const susp = stages.find((s) => s.effect === 'suspension')!;
    expect((await api(hr).post(`/disciplinary/${caseId}/decide`, { stageId: susp.id, note: 'Three days' })).status).toBe(400);
    expect((await api(hr).post(`/disciplinary/${caseId}/decide`, { stageId: susp.id, note: 'Three days without pay', suspensionFrom: '2026-10-20', suspensionTo: '2026-10-22' })).status).toBe(200);
    const ev = await db.query(`SELECT kind, status, details FROM career_event WHERE employee_id = $1 AND kind = 'suspension'`, [DEMO.sita]);
    expect(ev.rows[0]).toMatchObject({ status: 'applied', details: { endDate: '2026-10-22' } });
    // Managers do not see suspensions on the career timeline; HR does.
    const asManager = (await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(ram))).body.events.map((e: { kind: string }) => e.kind);
    expect(asManager).not.toContain('suspension');
    const asHr = (await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(hr))).body.events.map((e: { kind: string }) => e.kind);
    expect(asHr).toContain('suspension');
    const tpl = (await request(app).get('/api/v1/documents/templates').set(auth(hr))).body.templates.find((t: { code: string }) => t.code === 'warning');
    const letter = await request(app).post('/api/v1/documents/letters').set(auth(hr)).send({ templateId: tpl.id, employeeId: DEMO.sita, purpose: 'unauthorised absence', allowMissing: true });
    expect(letter.status).toBe(201);
    expect((await api(hr).post(`/disciplinary/${caseId}/letter`, { letterId: letter.body.id })).status).toBe(200);
    const k = (await api(sita).get(`/disciplinary/${caseId}`)).body.case;
    expect(k).toMatchObject({ status: 'decided', stageName: 'Suspension', suspensionFrom: '2026-10-20', letterRef: letter.body.refNo });
  });

  it('a termination stage records the exit through the lifecycle module', async () => {
    const emp = await newEmployee('TRM1');
    const k = (await api(hr).post('/disciplinary', { employeeId: emp, category: 'misconduct', incidentDate: '2026-09-01', incident: 'Theft' })).body.id;
    const term = stages.find((s) => s.effect === 'termination')!;
    expect((await api(hr).post(`/disciplinary/${k}/decide`, { stageId: term.id, note: 'Gross misconduct' })).status).toBe(400);
    expect((await api(hr).post(`/disciplinary/${k}/decide`, { stageId: term.id, note: 'Gross misconduct', lastWorkingDay: '2026-10-30' })).status).toBe(200);
    const ex = await db.query(`SELECT kind, voluntary, status FROM employee_exit WHERE employee_id = $1`, [emp]);
    expect(ex.rows[0]).toMatchObject({ kind: 'termination', voluntary: false, status: 'accepted' });
  });

  it('withdrawal and the overdue reminder', async () => {
    const k = (await api(hr).post('/disciplinary', { employeeId: DEMO.ram, category: 'conduct', incidentDate: '2026-10-02', incident: 'Shouting' })).body.id;
    await api(hr).post(`/disciplinary/${k}/show-cause`, { text: 'Explain', responseDue: '2099-01-01' });
    await db.query(`UPDATE disciplinary_case SET response_due = '2026-01-01' WHERE id = $1`, [k]);
    const out = await relationsDaily(db);
    expect(out.overdueResponses).toBe(1);
    expect((await relationsDaily(db)).overdueResponses).toBe(0);
    expect((await api(hr).post(`/disciplinary/${k}/withdraw`, { reason: 'Misunderstanding' })).status).toBe(200);
    expect((await api(hr).get(`/disciplinary?employeeId=${DEMO.ram}`)).body.cases[0].status).toBe('withdrawn');
  });
});

describe('milestones, acting and deputation', () => {
  it('lists service anniversaries, contract ends and retirements; HR sets contract end', async () => {
    const t = new Date(Date.now() + 5.75 * 3600_000);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    const in10 = iso(new Date(t.getTime() + 10 * 86_400_000));
    const joined = `${Number(in10.slice(0, 4)) - 5}${in10.slice(4)}`;
    const dob = `${Number(in10.slice(0, 4)) - 58}${in10.slice(4)}`;
    const a = await newEmployee('MS1', { join_date: joined, dob });
    expect((await api(sita).put(`/employees/${a}/contract`, { contractEnd: in10 })).status).toBe(403);
    expect((await api(hr).put(`/employees/${a}/contract`, { contractEnd: in10 })).status).toBe(200);
    const m = (await api(hr).get('/milestones?days=30')).body.milestones.filter((x: { employeeId: string }) => x.employeeId === a);
    expect(m.map((x: { kind: string }) => x.kind).sort()).toEqual(['contract_end', 'retirement', 'service']);
    expect(m.find((x: { kind: string }) => x.kind === 'service').detail).toBe('5 years of service');
    expect((await api(hr).get('/separating')).body.people.some((x: { employeeId: string }) => x.employeeId === a)).toBe(true);
    expect((await api(ram).get('/milestones')).status).toBe(403);
  });

  it('acting assignments show on the directory and end by themselves', async () => {
    const t = new Date(Date.now() + 5.75 * 3600_000).toISOString().slice(0, 10);
    expect((await api(hr).post('/assignments', { employeeId: DEMO.ram, kind: 'acting', startDate: t, endDate: t })).status).toBe(400);
    const r = await api(hr).post('/assignments', { employeeId: DEMO.ram, kind: 'acting', designation: 'Head of Operations', startDate: t, endDate: t });
    expect(r.status).toBe(201);
    expect((await api(hr).post('/assignments', { employeeId: DEMO.ram, kind: 'acting', designation: 'CEO', startDate: t, endDate: t })).status).toBe(409);
    const dir = (await request(app).get('/api/v1/people/directory').set(auth(sita))).body.people.find((p: { id: string }) => p.id === DEMO.ram);
    expect(dir.temp_assignment).toMatchObject({ kind: 'acting', label: 'Head of Operations' });
    const ev = await db.query(`SELECT kind FROM career_event WHERE employee_id = $1 AND kind = 'acting'`, [DEMO.ram]);
    expect(ev.rows.length).toBe(1);
    const des = await db.query(`SELECT designation FROM employee WHERE id = $1`, [DEMO.ram]);
    expect(des.rows[0].designation).not.toBe('Head of Operations');
    const out = await relationsDaily(db, new Date(Date.now() + 2 * 86_400_000));
    expect(out.assignmentsEnded).toBeGreaterThanOrEqual(1);
    expect((await api(ram).get(`/assignments?employeeId=${DEMO.ram}&status=ended`)).body.assignments.length).toBe(1);
    expect((await api(ram).get(`/assignments?employeeId=${DEMO.sita}`)).status).toBe(403);
  });

  it('deputation needs a destination; HR can end it early', async () => {
    expect((await api(hr).post('/assignments', { employeeId: DEMO.sita, kind: 'deputation', startDate: '2026-11-01', endDate: '2026-12-01' })).status).toBe(400);
    const r = await api(hr).post('/assignments', { employeeId: DEMO.sita, kind: 'deputation', branchId: DEMO.branchId, startDate: '2026-11-01', endDate: '2026-12-01' });
    expect(r.status).toBe(201);
    expect((await api(hr).post(`/assignments/${r.body.id}/end`, { cancel: true })).status).toBe(200);
    expect((await api(hr).post(`/assignments/${r.body.id}/end`, {})).status).toBe(409);
  });
});

describe('Know Your Employee', () => {
  let cid: string;
  it('HR launches a campaign; employees confirm; HR sees progress and nudges', async () => {
    expect((await api(sita).post('/kye/campaigns', { title: 'x', sections: ['personal'], dueDate: '2099-01-01' })).status).toBe(403);
    const r = await api(hr).post('/kye/campaigns', { title: 'Annual record check', sections: ['personal', 'contact', 'family'], dueDate: '2099-01-01' });
    expect(r.status).toBe(201);
    expect(r.body.employees).toBeGreaterThanOrEqual(3);
    cid = r.body.id;
    const mine = (await api(sita).get('/kye/mine')).body;
    expect(mine.campaigns[0]).toMatchObject({ id: cid, status: 'pending' });
    expect(mine.completeness.missing).toContain('bank');
    expect((await api(sita).post(`/kye/campaigns/${cid}/respond`, { status: 'confirmed' })).status).toBe(200);
    const detail = (await api(hr).get(`/kye/campaigns/${cid}`)).body;
    expect(detail.responses.find((x: { employee_id: string }) => x.employee_id === DEMO.sita).status).toBe('confirmed');
    const n1 = (await api(hr).post(`/kye/campaigns/${cid}/nudge`)).body.nudged;
    const n2 = (await api(hr).post(`/kye/campaigns/${cid}/nudge`)).body.nudged;
    expect(n1).toBeGreaterThan(0);
    expect(n2).toBe(0);
    expect((await api(hr).get('/kye/campaigns')).body.campaigns[0].confirmed).toBe(1);
    expect((await api(hr).post(`/kye/campaigns/${cid}/close`)).status).toBe(200);
    expect((await api(ram).post(`/kye/campaigns/${cid}/respond`, { status: 'confirmed' })).status).toBe(404);
  });

  it('record completeness report is HR only', async () => {
    const r = await api(hr).get('/completeness');
    expect(r.status).toBe(200);
    expect(r.body.employees.find((x: { employeeId: string }) => x.employeeId === DEMO.sita).percent).toBeLessThan(100);
    expect((await api(sita).get('/completeness')).status).toBe(403);
  });
});

describe('sign-in pop-up notices', () => {
  it('HR marks an announcement as a pop-up; each person sees it once, or until they acknowledge', async () => {
    const a = await request(app).post('/api/v1/engage/announcements').set(auth(hr)).send({ title: 'Office closed Friday', body: 'Tihar', popup: true });
    const b = await request(app).post('/api/v1/engage/announcements').set(auth(hr)).send({ title: 'New leave policy', body: 'Read it', popup: true, requiresAck: true });
    const p1 = (await api(sita).get('/popups')).body.popups.map((x: { id: string }) => x.id);
    expect(p1).toEqual(expect.arrayContaining([a.body.id, b.body.id]));
    expect((await api(sita).post(`/popups/${a.body.id}/dismiss`)).status).toBe(200);
    expect((await api(sita).post(`/popups/${b.body.id}/dismiss`)).status).toBe(400);
    expect((await api(sita).post(`/popups/${b.body.id}/dismiss`, { acknowledge: true })).status).toBe(200);
    expect((await api(sita).get('/popups')).body.popups).toEqual([]);
    expect((await api(ram).get('/popups')).body.popups.length).toBe(2);
    const acks = await request(app).get(`/api/v1/engage/announcements/${b.body.id}/acks`).set(auth(hr));
    expect(acks.body.acked).toBe(1);
    // A manager cannot make pop-ups.
    const m = await request(app).post('/api/v1/engage/announcements').set(auth(ram)).send({ title: 'Team lunch', body: 'Friday', audienceKind: 'reports', popup: true });
    expect(m.status).toBe(201);
    expect((await db.query('SELECT popup FROM announcement WHERE id = $1', [m.body.id])).rows[0].popup).toBe(false);
  });
});

describe('registered clock-in devices', () => {
  const token = 'dev-token-abcdefghijklmnopqrstuvwxyz';
  const punch = (t: string, headers: Record<string, string> = {}) => request(app).post('/api/v1/punch').set(auth(t)).set(headers)
    .send({ direction: 'in', clientTime: new Date().toISOString(), idempotencyKey: `k-${Math.random().toString(36).slice(2)}`, source: 'web' });

  it('off by default: punches work without a device', async () => {
    const r = await punch(sita);
    expect(r.status).not.toBe(403);
  });

  it('when required, only approved devices may punch', async () => {
    const pol = (await api(hr).get('/policy')).body.policy;
    expect((await api(hr).put('/policy', { ...pol, registeredDevicesOnly: true })).status).toBe(200);
    const blocked = await punch(sita);
    expect(blocked.status).toBe(403);
    expect(blocked.body.details.code).toBe('device_not_registered');
    const reg = await api(sita).post('/devices/register', { token, label: 'My phone' });
    expect(reg.status).toBe(201);
    expect((await api(sita).post('/devices/register', { token, label: 'Again' })).status).toBe(409);
    expect((await punch(sita, { 'X-Device-Token': token })).status).toBe(403);
    expect((await api(sita).post(`/devices/${reg.body.id}/approve`)).status).toBe(403);
    expect((await api(hr).post(`/devices/${reg.body.id}/approve`)).status).toBe(200);
    expect((await punch(sita, { 'X-Device-Token': token })).status).not.toBe(403);
    // Another employee cannot use Sita's device token.
    expect((await punch(ram, { 'X-Device-Token': token })).status).toBe(403);
    expect((await api(sita).get('/devices/mine')).body).toMatchObject({ required: true });
    expect((await api(hr).post(`/devices/${reg.body.id}/revoke`)).status).toBe(200);
    expect((await punch(sita, { 'X-Device-Token': token })).status).toBe(403);
    await api(hr).put('/policy', { ...pol, registeredDevicesOnly: false });
  });

  it('summary is HR only', async () => {
    expect((await api(hr).get('/summary')).status).toBe(200);
    expect((await api(sita).get('/summary')).status).toBe(403);
    void sitaUser;
  });
});
