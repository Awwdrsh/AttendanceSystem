import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Db } from '../src/db/pool.js';
import { auth, DEMO, freshDb, loginAs, testApp, testCipher } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let sita: string; let ram: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  sita = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  ram = (await loginAs(db, app, ['employee'], DEMO.ram)).token;
});
afterAll(async () => { await db.end(); });

async function insertTemplate(employeeId: string, modality: string): Promise<string> {
  const aad = `template:${employeeId}:${modality}:attendance`;
  const r = await db.query<{ id: string }>(
    `INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
     VALUES ($1, '1', $2, 'zk_fp_v10', $3, 1, 'h', $4) RETURNING id`,
    [employeeId, modality, testCipher.encrypt(Buffer.from('template-bytes').toString('base64'), aad), DEMO.deviceId]);
  return r.rows[0]!.id;
}

describe('biometrics: consent, templates, shredding', () => {
  it('employee grants consent, sees template metadata only, and revocation shreds that modality', async () => {
    const consent = await request(app).post('/api/v1/biometrics/consent').set(auth(sita)).send({ employeeId: DEMO.sita, modality: 'fingerprint', noticeVersion: 'v1' });
    expect(consent.status).toBe(201);
    await insertTemplate(DEMO.sita, 'fingerprint');
    await insertTemplate(DEMO.sita, 'face');
    const list = await request(app).get('/api/v1/biometrics/templates').set(auth(sita)).query({ employeeId: DEMO.sita });
    expect(list.body.templates).toHaveLength(2);
    expect(JSON.stringify(list.body)).not.toContain('ciphertext');
    const revoke = await request(app).post(`/api/v1/biometrics/consent/${consent.body.consent.id}/revoke`).set(auth(sita));
    expect(revoke.body).toEqual({ ok: true, templatesShredded: 1 });
    const rows = await db.query<{ modality: string; len: number; shredded: boolean }>(
      `SELECT modality, length(ciphertext) AS len, shredded_at IS NOT NULL AS shredded FROM biometric_template WHERE employee_id = $1 ORDER BY modality`, [DEMO.sita]);
    expect(rows.rows).toEqual([{ modality: 'face', len: expect.any(Number), shredded: false }, { modality: 'fingerprint', len: 0, shredded: true }]);
    expect(rows.rows[0]!.len).toBeGreaterThan(29);
    const audits = await db.query(`SELECT action FROM audit_log WHERE action IN ('consent_granted', 'consent_revoked') ORDER BY id`);
    expect(audits.rows.map((a) => a.action)).toEqual(['consent_granted', 'consent_revoked']);
  });

  it('an employee cannot see, consent for, or shred another employee; HR can', async () => {
    const id = await insertTemplate(DEMO.ram, 'face');
    expect((await request(app).get('/api/v1/biometrics/templates').set(auth(sita)).query({ employeeId: DEMO.ram })).status).toBe(403);
    expect((await request(app).post('/api/v1/biometrics/consent').set(auth(sita)).send({ employeeId: DEMO.ram, modality: 'face', noticeVersion: 'v1' })).status).toBe(403);
    expect((await request(app).post(`/api/v1/biometrics/templates/${id}/shred`).set(auth(sita))).status).toBe(403);
    expect((await request(app).post(`/api/v1/biometrics/templates/${id}/shred`).set(auth(ram))).status).toBe(403);
    const hrShred = await request(app).post(`/api/v1/biometrics/templates/${id}/shred`).set(auth(hr));
    expect(hrShred.body).toEqual({ ok: true, shredded: 1 });
    expect((await request(app).post(`/api/v1/biometrics/templates/${id}/shred`).set(auth(hr))).body.shredded).toBe(0);
  });

  it('DSAR export contains only the requester\'s own data and is audited', async () => {
    await db.query(`INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
                    VALUES ($1, 'DEMO-01', 'zkteco', '2', $2, '2026-09-21T04:32:03Z', '2026-09-21 10:17:03', '2026-09-21', 'in', 'face', 'zkteco', 'dsar-k1')`, [DEMO.deviceId, DEMO.ram]);
    expect((await request(app).get('/api/v1/biometrics/dsar').set(auth(sita)).query({ employeeId: DEMO.ram })).status).toBe(403);
    const own = await request(app).get('/api/v1/biometrics/dsar').set(auth(ram)).query({ employeeId: DEMO.ram });
    expect(own.status).toBe(200);
    expect(own.body.employee.id).toBe(DEMO.ram);
    expect(own.body.punches).toHaveLength(1);
    expect(JSON.stringify(own.body)).not.toContain(DEMO.sita);
    expect(own.body.employee).not.toHaveProperty('pan_enc');
    const a = await db.query(`SELECT 1 FROM audit_log WHERE action = 'dsar_export' AND row_id = $1`, [DEMO.ram]);
    expect(a.rows).toHaveLength(1);
  });

  it('transparency page is bilingual and available to any authenticated user', async () => {
    expect((await request(app).get('/api/v1/biometrics/transparency')).status).toBe(401);
    const t = await request(app).get('/api/v1/biometrics/transparency').set(auth(sita));
    expect(t.status).toBe(200);
    expect(t.body.retention.templates.en).toMatch(/30 days/);
    expect(t.body.retention.templates.ne).toMatch(/३०/);
    expect(t.body.withdraw.ne.length).toBeGreaterThan(10);
  });

  it('retention run shreds templates of employees exited more than 30 days ago', async () => {
    await db.query(`UPDATE employee SET exit_date = current_date - 45, status = 'exited' WHERE id = $1`, [DEMO.ram]);
    const id = await insertTemplate(DEMO.ram, 'palm');
    const run = await request(app).post('/api/v1/biometrics/retention/run').set(auth(hr)).send({});
    expect(run.body).toMatchObject({ employees: 1, templatesShredded: 1 });
    const row = await db.query('SELECT shredded_at FROM biometric_template WHERE id = $1', [id]);
    expect(row.rows[0].shredded_at).not.toBeNull();
    expect((await request(app).post('/api/v1/biometrics/retention/run').set(auth(sita)).send({})).status).toBe(403);
  });
});
