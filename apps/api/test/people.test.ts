import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { createUser } from '../src/modules/auth/service.js';
import { applyDueCareerEvents } from '../src/modules/people/career.js';
import { nptToday } from '../src/modules/workflow/service.js';
import { auth, DEMO, freshDb, loginAs, testApp, TEST_PEPPER } from './setup-db.js';

/** People and core HR: structure, directory, org chart, full profile, career events, change requests, history. */
let db: Db; let app: ReturnType<typeof testApp>;
let hr: string; let hr2: string; let sita: string; let ram: string; let peer: string; let foreignHr: string; let peerId: string;
const addDays = (iso: string, n: number) => new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
const PNG = `data:image/png;base64,${Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.from('tiny-avatar')]).toString('base64')}`;
const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n%%EOF').toString('base64')}`;
const upload = async (tok: string, purpose: string, dataUrl: string, filename = 'f') =>
  (await request(app).post('/api/v1/files').set(auth(tok)).send({ purpose, filename, dataUrl })).body.file.id as string;

beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  hr2 = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee', 'manager'], DEMO.ram)).token;
  peerId = (await db.query<{ id: string }>(`INSERT INTO employee (company_id, emp_code, name_en, join_date) VALUES ($1, 'E077', 'Peer Person', '2025-01-01') RETURNING id`, [DEMO.companyId])).rows[0]!.id;
  await db.query(`INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES ($1, daterange('2025-01-01', NULL), $2, 'regular')`, [peerId, DEMO.branchId]);
  peer = (await loginAs(db, app, ['employee'], peerId)).token;
  await db.query('UPDATE employee_assignment SET manager_id = $2 WHERE employee_id = $1', [DEMO.sita, DEMO.ram]);
  await db.query(`UPDATE employee SET join_date = '2024-01-01' WHERE id IN ($1, $2)`, [DEMO.sita, DEMO.ram]);
  const c = (await db.query<{ id: string }>(`INSERT INTO company (code, name_en) VALUES ('OTHERP', 'Other Co') RETURNING id`)).rows[0]!.id;
  await createUser(db, { companyId: c, email: 'hr@other.people', name: 'Other HR', password: 'correct-horse-battery-staple', roles: ['hr'], pepper: TEST_PEPPER });
  foreignHr = (await request(app).post('/api/v1/auth/login').send({ email: 'hr@other.people', password: 'correct-horse-battery-staple' })).body.token;
});
afterAll(async () => { await db.end(); });

let deptId: string; let teamId: string; let designationId: string; let branch2: string;

describe('org structure', () => {
  it('HR builds departments, teams, branches and designations; employees cannot', async () => {
    const dept = await request(app).post('/api/v1/people/structure/units').set(auth(hr)).send({ kind: 'department', code: 'ENG', nameEn: 'Engineering', headEmployeeId: DEMO.ram });
    expect(dept.status).toBe(201);
    deptId = dept.body.unit.id;
    const team = await request(app).post('/api/v1/people/structure/units').set(auth(hr)).send({ kind: 'team', code: 'ENG-WEB', nameEn: 'Web', parentId: deptId });
    expect(team.status).toBe(201);
    teamId = team.body.unit.id;
    const des = await request(app).post('/api/v1/people/structure/designations').set(auth(hr)).send({ code: 'SE2', nameEn: 'Software Engineer II', grade: 'G5', jobLevel: 5, orgUnitId: deptId });
    expect(des.status).toBe(201);
    designationId = des.body.designation.id;
    const br = await request(app).post('/api/v1/people/structure/branches').set(auth(hr)).send({ code: 'PKR', nameEn: 'Pokhara', address: 'Lakeside' });
    expect(br.status).toBe(201);
    branch2 = br.body.branch.id;
    expect((await request(app).post('/api/v1/people/structure/units').set(auth(sita)).send({ kind: 'team', code: 'X', nameEn: 'X' })).status).toBe(403);
    expect((await request(app).post('/api/v1/people/structure/units').set(auth(ram)).send({ kind: 'team', code: 'X', nameEn: 'X' })).status).toBe(403);
  });

  it('rejects cycles, foreign references and deleting units that are in use', async () => {
    const cyc = await request(app).patch(`/api/v1/people/structure/units/${deptId}`).set(auth(hr)).send({ parentId: teamId });
    expect(cyc.status).toBe(400);
    const self = await request(app).patch(`/api/v1/people/structure/units/${deptId}`).set(auth(hr)).send({ parentId: deptId });
    expect(self.status).toBe(400);
    const foreign = await request(app).post('/api/v1/people/structure/units').set(auth(foreignHr)).send({ kind: 'team', code: 'F1', nameEn: 'F', parentId: deptId });
    expect(foreign.status).toBe(400);
    expect((await request(app).delete(`/api/v1/people/structure/units/${deptId}`).set(auth(hr))).status).toBe(409); // has a sub-unit
    expect((await request(app).delete(`/api/v1/people/structure/branches/${DEMO.branchId}`).set(auth(hr))).status).toBe(409);
    expect((await request(app).patch(`/api/v1/people/structure/units/${deptId}`).set(auth(foreignHr)).send({ nameEn: 'Hacked' })).status).toBe(404);
  });

  it('lists the structure with headcounts to any signed-in user of the company only', async () => {
    const s = await request(app).get('/api/v1/people/structure').set(auth(sita));
    expect(s.status).toBe(200);
    expect(s.body.units.find((u: { id: string }) => u.id === deptId)).toMatchObject({ name_en: 'Engineering', head_name: expect.any(String), headcount: 0 });
    const other = await request(app).get('/api/v1/people/structure').set(auth(foreignHr));
    expect(other.body.units).toHaveLength(0);
  });
});

describe('career events', () => {
  it('a promotion today updates the designation and grade and records history', async () => {
    const ev = await request(app).post(`/api/v1/people/${DEMO.sita}/career`).set(auth(hr))
      .send({ kind: 'promotion', effectiveDate: nptToday(), details: { designationId, reason: 'Great year' }, note: 'Annual review' });
    expect(ev.status).toBe(201);
    expect(ev.body.event.status).toBe('applied');
    const e = (await db.query('SELECT designation, designation_id, grade FROM employee WHERE id = $1', [DEMO.sita])).rows[0];
    expect(e).toMatchObject({ designation: 'Software Engineer II', designation_id: designationId, grade: 'G5' });
    const hist = await request(app).get(`/api/v1/people/${DEMO.sita}/history`).set(auth(sita));
    expect(hist.status).toBe(200);
    expect(hist.body.history.map((h: { field: string }) => h.field)).toEqual(expect.arrayContaining(['Designation', 'Grade']));
  });

  it('a transfer splits the effective-dated assignment at its date', async () => {
    const date = addDays(nptToday(), -3);
    const ev = await request(app).post(`/api/v1/people/${DEMO.sita}/career`).set(auth(hr)).send({ kind: 'transfer', effectiveDate: date, details: { branchId: branch2, orgUnitId: teamId } });
    expect(ev.status).toBe(201);
    const rows = (await db.query(`SELECT lower(valid)::text AS lo, upper(valid)::text AS hi, branch_id, org_unit_id, manager_id FROM employee_assignment WHERE employee_id = $1 ORDER BY lower(valid)`, [DEMO.sita])).rows;
    const last = rows.at(-1);
    expect(last).toMatchObject({ lo: date, hi: null, branch_id: branch2, org_unit_id: teamId, manager_id: DEMO.ram });
    expect(rows.at(-2).hi).toBe(date);
  });

  it('a future event is scheduled, then applied by the daily job on its date', async () => {
    const date = addDays(nptToday(), 5);
    const ev = await request(app).post(`/api/v1/people/${DEMO.sita}/career`).set(auth(hr)).send({ kind: 'grade_change', effectiveDate: date, details: { grade: 'G6' } });
    expect(ev.body.event.status).toBe('scheduled');
    expect((await db.query('SELECT grade FROM employee WHERE id = $1', [DEMO.sita])).rows[0].grade).toBe('G5');
    expect(await applyDueCareerEvents(db, new Date())).toBe(0);
    expect(await applyDueCareerEvents(db, new Date(Date.now() + 6 * 86_400_000))).toBe(1);
    expect((await db.query('SELECT grade FROM employee WHERE id = $1', [DEMO.sita])).rows[0].grade).toBe('G6');
  });

  it('a scheduled separation sets the exit date at once, can be cancelled, and validates input', async () => {
    const lwd = addDays(nptToday(), 20);
    const bad = await request(app).post(`/api/v1/people/${peerId}/career`).set(auth(hr)).send({ kind: 'separation', effectiveDate: lwd, details: {} });
    expect(bad.status).toBe(400);
    const ev = await request(app).post(`/api/v1/people/${peerId}/career`).set(auth(hr)).send({ kind: 'separation', effectiveDate: lwd, details: { separationType: 'resignation', reason: 'Studies abroad' } });
    expect(ev.status).toBe(201);
    expect(ev.body.event.status).toBe('scheduled');
    expect((await db.query('SELECT exit_date, status FROM employee WHERE id = $1', [peerId])).rows[0]).toMatchObject({ exit_date: lwd, status: 'active' });
    const dup = await request(app).post(`/api/v1/people/${peerId}/career`).set(auth(hr)).send({ kind: 'separation', effectiveDate: lwd, details: { separationType: 'resignation' } });
    expect(dup.status).toBe(409);
    const cancel = await request(app).post(`/api/v1/people/${peerId}/career/${ev.body.event.id}/cancel`).set(auth(hr));
    expect(cancel.status).toBe(200);
    expect((await db.query('SELECT exit_date FROM employee WHERE id = $1', [peerId])).rows[0].exit_date).toBeNull();
    const self = await request(app).post(`/api/v1/people/${peerId}/career`).set(auth(hr)).send({ kind: 'manager_change', effectiveDate: nptToday(), details: { managerId: peerId } });
    expect(self.status).toBe(400);
    const early = await request(app).post(`/api/v1/people/${peerId}/career`).set(auth(hr)).send({ kind: 'grade_change', effectiveDate: '2020-01-01', details: { grade: 'X' } });
    expect(early.status).toBe(400);
  });

  it('timeline: self and manager can read (manager without reasons), peers cannot, only HR records', async () => {
    const own = await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(sita));
    expect(own.status).toBe(200);
    const promo = own.body.events.find((e: { kind: string }) => e.kind === 'promotion');
    expect(promo).toMatchObject({ label: 'Promoted', summary: expect.stringContaining('Software Engineer II'), reason: 'Great year' });
    const mgr = await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(ram));
    expect(mgr.status).toBe(200);
    expect(mgr.body.events.find((e: { kind: string }) => e.kind === 'promotion').reason).toBeNull();
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(peer))).status).toBe(403);
    expect((await request(app).post(`/api/v1/people/${DEMO.sita}/career`).set(auth(ram)).send({ kind: 'note', effectiveDate: nptToday() })).status).toBe(403);
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/career`).set(auth(foreignHr))).status).toBe(404);
  });
});

describe('profile sections', () => {
  it('HR edits sections directly; ID numbers are encrypted and masked unless revealed', async () => {
    const put = await request(app).put(`/api/v1/people/${DEMO.sita}/profile/ids`).set(auth(hr)).send({ citizenshipNo: '27-01-75-12345', citizenshipDistrict: 'Kathmandu', passportNo: 'PA1234567' });
    expect(put.status).toBe(200);
    expect(put.body.changes.find((c: { label: string }) => c.label === 'Citizenship number').to).toBe('•••• 2345');
    const raw = (await db.query('SELECT citizenship_no_enc FROM employee WHERE id = $1', [DEMO.sita])).rows[0].citizenship_no_enc as Buffer;
    expect(raw.toString('latin1')).not.toContain('12345');
    const masked = await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).set(auth(sita));
    expect(masked.body.profile.ids.citizenshipNo).toBe('•••• 2345');
    const revealed = await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).query({ reveal: '1' }).set(auth(hr));
    expect(revealed.body.profile.ids.passportNo).toBe('PA1234567');
    const audited = await db.query(`SELECT 1 FROM audit_log WHERE action = 'read_sensitive' AND table_name = 'employee_profile' AND row_id = $1`, [DEMO.sita]);
    expect(audited.rows.length).toBe(1);
  });

  it('validates list sections (nominee shares must total 100%)', async () => {
    const bad = await request(app).put(`/api/v1/people/${DEMO.sita}/profile/family`).set(auth(hr)).send({ members: [
      { name: 'Hari', relation: 'father', isNominee: true, nomineeShare: 60 }, { name: 'Gita', relation: 'mother', isNominee: true, nomineeShare: 30 }] });
    expect(bad.status).toBe(400);
    const ok = await request(app).put(`/api/v1/people/${DEMO.sita}/profile/family`).set(auth(hr)).send({ members: [
      { name: 'Hari', relation: 'father', isNominee: true, nomineeShare: 60 }, { name: 'Gita', relation: 'mother', isNominee: true, nomineeShare: 40 }] });
    expect(ok.status).toBe(200);
    const unknown = await request(app).put(`/api/v1/people/${DEMO.sita}/profile/salary`).set(auth(hr)).send({});
    expect(unknown.status).toBe(404);
    const extra = await request(app).put(`/api/v1/people/${DEMO.sita}/profile/personal`).set(auth(hr)).send({ status: 'exited' });
    expect(extra.status).toBe(400);
  });

  it('only the employee and HR read a profile', async () => {
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).set(auth(ram))).status).toBe(403);
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).set(auth(peer))).status).toBe(403);
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).set(auth(foreignHr))).status).toBe(404);
    expect((await request(app).put(`/api/v1/people/${DEMO.sita}/profile/personal`).set(auth(sita)).send({ bloodGroup: 'O+' })).status).toBe(403);
  });
});

describe('profile change requests', () => {
  let reqId: string;
  it('an employee proposes a change with proof; HR approves it from the inbox and it is applied', async () => {
    const proof = await upload(sita, 'profile_change', PDF, 'citizenship.pdf');
    const r = await request(app).post('/api/v1/people/me/change-requests').set(auth(sita))
      .send({ section: 'contact', value: { personalPhone: '+977 9800000001', permanentAddress: { district: 'Lalitpur', municipality: 'Lalitpur Metropolitan', ward: 5 } }, reason: 'Moved', proofFileId: proof });
    expect(r.status).toBe(201);
    reqId = r.body.request.id;
    const dup = await request(app).post('/api/v1/people/me/change-requests').set(auth(sita)).send({ section: 'contact', value: { personalPhone: '+977 9800000002' } });
    expect(dup.status).toBe(409);
    const inbox = await request(app).get('/api/v1/workflow/inbox').set(auth(hr));
    const item = inbox.body.items.find((i: { objectId: string }) => i.objectId === reqId);
    expect(item.summary).toMatchObject({ sectionLabel: 'Contact and address', hasProof: true });
    expect(item.summary.changes.map((c: { label: string }) => c.label)).toEqual(expect.arrayContaining(['Personal phone', 'Permanent address']));
    // The employee's manager is not an approver of profile changes.
    const mgrInbox = await request(app).get('/api/v1/workflow/inbox').set(auth(ram));
    expect(mgrInbox.body.items.find((i: { objectId: string }) => i.objectId === reqId)).toBeUndefined();
    expect((await request(app).get(`/api/v1/people/change-requests/${reqId}/proof`).set(auth(hr))).status).toBe(200);
    expect((await request(app).get(`/api/v1/people/change-requests/${reqId}/proof`).set(auth(peer))).status).toBe(404);
    const ok = await request(app).post(`/api/v1/workflow/${item.instanceId}/approve`).set(auth(hr)).send({});
    expect(ok.status).toBe(200);
    const p = await request(app).get(`/api/v1/people/${DEMO.sita}/profile`).set(auth(sita));
    expect(p.body.profile.contact).toMatchObject({ personalPhone: '+977 9800000001', permanentAddress: { district: 'Lalitpur', ward: 5 } });
    const mine = await request(app).get('/api/v1/people/me/change-requests').set(auth(sita));
    expect(mine.body.requests.find((x: { id: string }) => x.id === reqId).status).toBe('approved');
    const hist = await request(app).get(`/api/v1/people/${DEMO.sita}/history`).set(auth(hr));
    expect(hist.body.history.some((h: { field: string; source: string }) => h.field === 'Personal phone' && h.source === 'change_request')).toBe(true);
  });

  it('a request that changes nothing is refused; an employee can cancel a pending request', async () => {
    const same = await request(app).post('/api/v1/people/me/change-requests').set(auth(sita)).send({ section: 'contact', value: { personalPhone: '+977 9800000001' } });
    expect(same.status).toBe(409);
    const r = await request(app).post('/api/v1/people/me/change-requests').set(auth(sita)).send({ section: 'skills', value: { skills: ['TypeScript'], languages: ['English', 'Nepali'] } });
    expect(r.status).toBe(201);
    expect((await request(app).post(`/api/v1/people/me/change-requests/${r.body.request.id}/cancel`).set(auth(peer))).status).toBe(404);
    expect((await request(app).post(`/api/v1/people/me/change-requests/${r.body.request.id}/cancel`).set(auth(sita))).status).toBe(200);
    const inst = await db.query(`SELECT state FROM workflow_instance WHERE object_id = $1`, [r.body.request.id]);
    expect(inst.rows[0].state).toBe('cancelled');
  });

  it('a rejected bank change is not applied and the employee is told why', async () => {
    const r = await request(app).post('/api/v1/people/me/change-requests').set(auth(sita)).send({ section: 'bank', value: { bankCode: 'NABIL', bankAccount: '0123456789012' } });
    expect(r.status).toBe(201);
    expect(JSON.stringify(r.body.request.summary)).not.toContain('0123456789012');
    const stored = (await db.query('SELECT payload_enc FROM profile_change_request WHERE id = $1', [r.body.request.id])).rows[0].payload_enc as Buffer;
    expect(stored.toString('latin1')).not.toContain('0123456789012');
    const inst = (await db.query(`SELECT id FROM workflow_instance WHERE object_id = $1`, [r.body.request.id])).rows[0].id;
    const rej = await request(app).post(`/api/v1/workflow/${inst}/reject`).set(auth(hr)).send({ reason: 'Account name does not match' });
    expect(rej.status).toBe(200);
    expect((await db.query('SELECT bank_code FROM employee WHERE id = $1', [DEMO.sita])).rows[0].bank_code).not.toBe('NABIL');
    const n = await db.query(`SELECT body FROM notification_outbox WHERE template = 'profile_change.rejected' ORDER BY created_at DESC LIMIT 1`);
    expect(n.rows[0].body).toContain('Account name does not match');
  });

  it('maker-checker: HR bank edits wait for a different HR person', async () => {
    expect((await request(app).put('/api/v1/people/policy').set(auth(hr)).send({ makerChecker: true })).status).toBe(200);
    const edit = await request(app).put(`/api/v1/people/${DEMO.ram}/profile/bank`).set(auth(hr)).send({ bankCode: 'NIC', bankAccount: '9988776655' });
    expect(edit.status).toBe(202);
    expect(edit.body.pending).toBe(true);
    const viaEmployees = await request(app).patch(`/api/v1/employees/${DEMO.ram}`).set(auth(hr)).send({ bankAccount: '1111222233' });
    expect(viaEmployees.status).toBe(409);
    const inst = (await db.query(`SELECT id FROM workflow_instance WHERE object_id = $1`, [edit.body.request.id])).rows[0].id;
    const self = await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(hr)).send({});
    expect(self.status).toBe(403);
    expect((await db.query('SELECT bank_code FROM employee WHERE id = $1', [DEMO.ram])).rows[0].bank_code).not.toBe('NIC');
    const second = await request(app).post(`/api/v1/workflow/${inst}/approve`).set(auth(hr2)).send({});
    expect(second.status).toBe(200);
    expect((await db.query('SELECT bank_code, bank_account_last4 FROM employee WHERE id = $1', [DEMO.ram])).rows[0]).toMatchObject({ bank_code: 'NIC', bank_account_last4: '6655' });
    // Non-sensitive sections still apply at once.
    const direct = await request(app).put(`/api/v1/people/${DEMO.ram}/profile/personal`).set(auth(hr)).send({ bloodGroup: 'B+' });
    expect(direct.status).toBe(200);
    await request(app).put('/api/v1/people/policy').set(auth(hr)).send({ makerChecker: false });
  });
});

describe('directory, org chart, avatar and summary', () => {
  it('everyone sees the directory without personal data; search and filters work', async () => {
    const d = await request(app).get('/api/v1/people/directory').set(auth(peer));
    expect(d.status).toBe(200);
    const s = d.body.people.find((p: { id: string }) => p.id === DEMO.sita);
    expect(s).toMatchObject({ designation: 'Software Engineer II', unit_name: 'Web', manager_name: expect.any(String) });
    expect(JSON.stringify(d.body)).not.toContain('9800000001');
    const q = await request(app).get('/api/v1/people/directory').query({ search: 'software%' }).set(auth(peer));
    expect(q.body.people).toHaveLength(0); // % is literal
    const byUnit = await request(app).get('/api/v1/people/directory').query({ orgUnitId: teamId }).set(auth(peer));
    expect(byUnit.body.people.map((p: { id: string }) => p.id)).toEqual([DEMO.sita]);
    const other = await request(app).get('/api/v1/people/directory').set(auth(foreignHr));
    expect(other.body.people).toHaveLength(0);
  });

  it('org chart links people to active managers; the card lists direct reports', async () => {
    const c = await request(app).get('/api/v1/people/org-chart').set(auth(sita));
    expect(c.body.nodes.find((n: { id: string }) => n.id === DEMO.sita).manager_id).toBe(DEMO.ram);
    const card = await request(app).get(`/api/v1/people/${DEMO.ram}/card`).set(auth(sita));
    expect(card.body.reports.map((r: { id: string }) => r.id)).toContain(DEMO.sita);
    expect((await request(app).get(`/api/v1/people/${DEMO.ram}/card`).set(auth(foreignHr))).status).toBe(404);
  });

  it('an employee sets their own photo; colleagues can see it; only images are accepted', async () => {
    const fileId = await upload(sita, 'avatar', PNG, 'me.png');
    expect((await request(app).post('/api/v1/people/me/avatar').set(auth(sita)).send({ fileId })).status).toBe(200);
    const img = await request(app).get(`/api/v1/people/${DEMO.sita}/avatar`).set(auth(peer));
    expect(img.status).toBe(200);
    expect(img.headers['content-type']).toContain('image/png');
    expect((await request(app).get(`/api/v1/people/${DEMO.sita}/avatar`).set(auth(foreignHr))).status).toBe(404);
    const pdf = await request(app).post('/api/v1/files').set(auth(sita)).send({ purpose: 'avatar', filename: 'x.pdf', dataUrl: PDF });
    expect(pdf.status).toBe(400);
    // Someone else's upload cannot be attached as your photo.
    const theirs = await upload(peer, 'avatar', PNG, 'p.png');
    expect((await request(app).post('/api/v1/people/me/avatar').set(auth(sita)).send({ fileId: theirs })).status).toBe(403);
    const replaced = await upload(sita, 'avatar', PNG, 'me2.png');
    await request(app).post('/api/v1/people/me/avatar').set(auth(sita)).send({ fileId: replaced });
    expect((await db.query('SELECT deleted_at FROM stored_file WHERE id = $1', [fileId])).rows[0].deleted_at).not.toBeNull();
  });

  it('summary counts the company', async () => {
    const s = await request(app).get('/api/v1/people/summary').set(auth(sita));
    expect(s.status).toBe(200);
    expect(s.body.active).toBeGreaterThanOrEqual(3);
    expect(s.body.byUnit.find((u: { id: string }) => u.id === teamId).count).toBe(1);
  });
});

describe('employees module integration', () => {
  it('creating an employee records a hire event and links the designation to the catalogue', async () => {
    const c = await request(app).post('/api/v1/employees').set(auth(hr)).send({ empCode: 'E500', nameEn: 'New Joiner', joinDate: nptToday(), designation: 'software engineer ii', branchId: DEMO.branchId });
    expect(c.status).toBe(201);
    const e = (await db.query('SELECT designation_id FROM employee WHERE id = $1', [c.body.id])).rows[0];
    expect(e.designation_id).toBe(designationId);
    const ev = await db.query(`SELECT kind, status FROM career_event WHERE employee_id = $1`, [c.body.id]);
    expect(ev.rows).toEqual([{ kind: 'hire', status: 'applied' }]);
  });

  it('HR edits through the employee form are written to field history', async () => {
    const p = await request(app).patch(`/api/v1/employees/${peerId}`).set(auth(hr)).send({ phone: '9801111111', managerId: DEMO.ram });
    expect(p.status).toBe(200);
    const h = await request(app).get(`/api/v1/people/${peerId}/history`).set(auth(hr));
    const fields = h.body.history.map((x: { field: string }) => x.field);
    expect(fields).toEqual(expect.arrayContaining(['Phone', 'Manager']));
    expect(h.body.history.find((x: { field: string }) => x.field === 'Manager').new_value).toBe((await db.query('SELECT name_en FROM employee WHERE id = $1', [DEMO.ram])).rows[0].name_en);
    expect((await request(app).get(`/api/v1/people/${peerId}/history`).set(auth(ram))).status).toBe(403);
  });
});
