import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import { localTimeMs, localDateOf } from '@hajiri/attendance-engine';
import type { Db } from '../src/db/pool.js';
import { recomputeDay } from '../src/modules/attendance/service.js';
import { toCsv } from '../src/modules/reports/csv.js';
import { auth, DEMO, freshDb, loginAs, testApp } from './setup-db.js';

let db: Db; let app: ReturnType<typeof testApp>; let hr: string; let emp: string;
beforeAll(async () => {
  db = await freshDb(); app = testApp(db);
  hr = (await loginAs(db, app, ['hr'])).token;
  emp = (await loginAs(db, app, ['employee'], DEMO.sita)).token;
  for (const [d, i, o] of [['2026-08-17', '10:20', '18:00'], ['2026-08-18', '10:00', '19:30']] as const) {
    for (const [t, k] of [[i, 'a'], [o, 'b']] as const) {
      const at = new Date(localTimeMs(d, t));
      await db.query(`INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
        VALUES ($1, 'DEMO-01', 'zkteco', '1', $2, $3, $4, $5, 'unknown', 'fingerprint', 'zkteco', $6)`, [DEMO.deviceId, DEMO.sita, at, `${d} ${t}:00`, localDateOf(at.getTime()), `r-${d}-${k}`]);
    }
    await recomputeDay(db, DEMO.sita, d);
  }
});
afterAll(async () => { await db.end(); });

describe('reports', () => {
  it('monthly register covers every BS calendar day per employee and exports CSV with a BOM', async () => {
    const json = await request(app).get('/api/v1/reports/register').set(auth(hr)).query({ bsYear: 2083, bsMonth: 5 });
    expect(json.status).toBe(200);
    expect(json.body.rows).toHaveLength(2 * 31);
    const sitaRows = json.body.rows.filter((r: { emp_code: string }) => r.emp_code === 'E001');
    expect(sitaRows[0]).toMatchObject({ bs_date: '2083-05-01', ad_date: '2026-08-17', arrival: '10:20', departure: '18:00', worked_hours: '7.67', late_min: 5, status: 'Present' });
    const csv = await request(app).get('/api/v1/reports/register').set(auth(hr)).query({ bsYear: 2083, bsMonth: 5, format: 'csv' });
    expect(csv.headers['content-type']).toMatch(/text\/csv/);
    expect(csv.text.charCodeAt(0)).toBe(0xfeff);
    expect(csv.text.split('\r\n')[0]).toBe('\uFEFFEmp code,Name,नाम,Date (BS),Date (AD),Arrival,Departure,Worked (h),Break (min),OT (h),Late (min),Leave,Status');
  });
  it('daily, late-early, headcount, exceptions, devices and leave-balances respond; employees are denied', async () => {
    for (const [path, q] of [['daily', { date: '2026-08-17' }], ['late-early', { from: '2026-08-17', to: '2026-08-18' }], ['headcount', {}],
      ['exceptions-summary', { from: '2026-08-17', to: '2026-08-18' }], ['devices', {}], ['leave-balances', {}], ['ot', { from: '2026-08-17', to: '2026-08-18' }]] as const) {
      const r = await request(app).get(`/api/v1/reports/${path}`).set(auth(hr)).query(q);
      expect(r.status, path).toBe(200);
      expect(Array.isArray(r.body.rows), path).toBe(true);
    }
    const late = await request(app).get('/api/v1/reports/late-early').set(auth(hr)).query({ from: '2026-08-17', to: '2026-08-18' });
    expect(late.body.rows[0]).toMatchObject({ emp_code: 'E001', late_days: 1, late_min: 5 });
    expect((await request(app).get('/api/v1/reports/daily').set(auth(emp)).query({ date: '2026-08-17' })).status).toBe(403);
  });
  it('punch audit is scoped to the employee', async () => {
    const own = await request(app).get('/api/v1/reports/punch-audit').set(auth(hr)).query({ employeeId: DEMO.sita, from: '2026-08-17', to: '2026-08-18' });
    expect(own.body.rows).toHaveLength(4);
    expect(own.body.rows[0].time).toBe('10:20');
  });
  it('CSV escaping follows RFC 4180', () => {
    const csv = toCsv([{ key: 'a', header: 'A' }, { key: 'b', header: 'B' }], [{ a: 'x,y', b: 'say "hi"' }, { a: null, b: 'line\nbreak' }]);
    expect(csv).toBe('\uFEFFA,B\r\n"x,y","say ""hi"""\r\n,"line\nbreak"\r\n');
  });
});
