import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import pg from 'pg';
import { migrate, MIGRATIONS_DIR } from '../../api/src/db/migrate.js';
import { FieldCipher } from '../../api/src/crypto.js';
import { createGatewayApp } from './app.js';
import { TemplateCipher } from './crypto.js';
import { Signer } from './punch/signer.js';
import { reconcileDay } from './reconcile.js';
import { COMMAND_ACK_TIMEOUT_MS } from './store/types.js';
import { PostgresStore } from './store/postgres.js';

const URL = process.env.DATABASE_URL_TEST ?? 'postgres://localhost:5433/hajiri_test_gateway';
const key = Buffer.alloc(32, 5);
const C = { company: '00000000-0000-7000-8000-000000000001', device: '00000000-0000-7000-8000-000000000021', emp: '00000000-0000-7000-8000-000000000011' };

let pool: pg.Pool; let store: PostgresStore; let app: ReturnType<typeof createGatewayApp>;

beforeAll(async () => {
  pool = new pg.Pool({ connectionString: URL });
  await pool.query('DROP SCHEMA public CASCADE; CREATE SCHEMA public;');
  await migrate(pool, MIGRATIONS_DIR, () => undefined);
  await pool.query(`INSERT INTO company (id, code, name_en) VALUES ($1, 'T', 'T')`, [C.company]);
  await pool.query(`INSERT INTO employee (id, company_id, emp_code, name_en) VALUES ($1, $2, 'E1', 'Sita')`, [C.emp, C.company]);
  await pool.query(`INSERT INTO device (id, company_id, serial_no, vendor, protocol, status) VALUES ($1, $2, 'PG-01', 'zkteco', 'adms', 'active')`, [C.device, C.company]);
  await pool.query(`INSERT INTO device_user_map (device_id, device_user_pin, employee_id, valid) VALUES ($1, '1', $2, daterange('2026-01-01', NULL))`, [C.device, C.emp]);
  store = new PostgresStore(URL, { maxPendingDevices: 1 });
  app = createGatewayApp({ store, signer: Signer.ephemeral(), cipher: new TemplateCipher(key) });
});
afterAll(async () => { await store.end(); await pool.end(); });

describe('PostgresStore end to end', () => {
  it('stores punches, dedupes, queues recompute, records drift', async () => {
    const r = await request(app).post('/iclock/cdata?SN=PG-01&table=ATTLOG&Stamp=5').set('Content-Type', 'text/plain')
      .send('1\t2026-09-21 10:17:03\t0\t1\t0\n1\t2026-09-21 10:17:03\t0\t1\t0\n1\t2026-09-21 10:17:05\t0\t1\t0\n7\t2026-09-21 10:30:00\t0\t15\t0\n');
    expect(r.text).toBe('OK: 4');
    const punches = await pool.query('SELECT device_user_pin, employee_id, verify_mode, local_date FROM punch_event ORDER BY punched_at');
    expect(punches.rows).toEqual([{ device_user_pin: '1', employee_id: C.emp, verify_mode: 'fingerprint', local_date: '2026-09-21' }, { device_user_pin: '7', employee_id: null, verify_mode: 'face', local_date: '2026-09-21' }]);
    const q = await pool.query('SELECT employee_id, local_date::text, reason FROM recompute_queue');
    expect(q.rows.sort((a, b) => a.local_date.localeCompare(b.local_date))).toEqual([{ employee_id: C.emp, local_date: '2026-09-20', reason: 'punch:zkteco' }, { employee_id: C.emp, local_date: '2026-09-21', reason: 'punch:zkteco' }]);
    const dev = await pool.query('SELECT attlog_stamp, clock_drift_sec, last_seen_at FROM device WHERE serial_no = $1', ['PG-01']);
    expect(dev.rows[0].attlog_stamp).toBe('5');
    expect(dev.rows[0].last_seen_at).not.toBeNull();
    expect(await store.countPunches(C.device, '2026-09-21')).toBe(2);
  });

  it('encrypts templates so the API FieldCipher can decrypt them; plaintext absent', async () => {
    await pool.query(`INSERT INTO consent_record (employee_id, purpose, modality, notice_version) VALUES ($1, 'attendance', 'fingerprint', 'v1')`, [C.emp]);
    const b64 = Buffer.from('real-template-bytes').toString('base64');
    await request(app).post('/iclock/fdata?SN=PG-01&table=FINGERTMP').set('Content-Type', 'application/octet-stream').send(`PIN=1\tFID=6\tSize=1\tValid=1\tTMP=${b64}\n`);
    const rows = await pool.query<{ employee_id: string; ciphertext: Buffer; aad_hash: string; modality: string }>('SELECT employee_id, ciphertext, aad_hash, modality FROM biometric_template');
    expect(rows.rows).toHaveLength(1);
    const t = rows.rows[0]!;
    expect(t.employee_id).toBe(C.emp);
    expect(t.ciphertext.toString('latin1')).not.toContain(b64);
    expect(new FieldCipher(key).decrypt(t.ciphertext, `template:${C.emp}:fingerprint:attendance`)).toBe(b64);
    expect(() => new FieldCipher(key).decrypt(t.ciphertext, 'wrong')).toThrow();
  });

  it('pending cap and dangerous command refusal apply in Postgres too', async () => {
    expect((await request(app).get('/iclock/cdata?SN=PG-NEW-1&options=all')).status).toBe(200);
    expect((await request(app).get('/iclock/cdata?SN=PG-NEW-2&options=all')).status).toBe(403);
    await expect(store.enqueueCommand(C.device, 'CLEAR DATA')).rejects.toThrow(/dangerous/);
    await expect(pool.query(`INSERT INTO device_command (device_id, command) VALUES ($1, 'SHELL ls')`, [C.device])).rejects.toThrow(/no_shell/);
  });

  it('reconcileDay queues a DATA QUERY ATTLOG for the device', async () => {
    const res = await reconcileDay(store, 'PG-01', '2026-09-21');
    expect(res).toMatchObject({ stored: 2, commandId: expect.any(Number) });
    const cmd = await pool.query('SELECT command, status FROM device_command WHERE id = $1', [res.commandId]);
    expect(cmd.rows[0].command).toBe('DATA QUERY ATTLOG StartTime=2026-09-21 00:00:00\tEndTime=2026-09-21 23:59:59');
    const poll = await request(app).get('/iclock/getrequest?SN=PG-01');
    expect(poll.text).toContain(`C:${res.commandId}:DATA QUERY ATTLOG`);
    await request(app).post('/iclock/devicecmd?SN=PG-01').set('Content-Type', 'text/plain').send(`ID=${res.commandId}&Return=0&CMD=DATA`);
    expect((await pool.query('SELECT status FROM device_command WHERE id = $1', [res.commandId])).rows[0].status).toBe('acked');
    expect((await reconcileDay(store, 'NOPE', '2026-09-21')).note).toBe('unknown device');
  });

  it('re-sends an unacknowledged command once after the ack timeout, then fails it (Postgres)', async () => {
    const id = await store.enqueueCommand(C.device, 'INFO');
    const t0 = new Date('2026-09-21T04:00:00Z');
    expect(await store.popQueuedCommands(C.device, t0)).toEqual([{ id, command: 'INFO' }]);
    expect(await store.popQueuedCommands(C.device, new Date(t0.getTime() + 1000))).toEqual([]);
    const t1 = new Date(t0.getTime() + COMMAND_ACK_TIMEOUT_MS + 1000);
    expect(await store.popQueuedCommands(C.device, t1)).toEqual([{ id, command: 'INFO' }]);
    const t2 = new Date(t1.getTime() + COMMAND_ACK_TIMEOUT_MS + 1000);
    expect(await store.popQueuedCommands(C.device, t2)).toEqual([]);
    const row = await pool.query('SELECT status, attempts FROM device_command WHERE id = $1', [id]);
    expect(row.rows[0]).toEqual({ status: 'failed', attempts: 2 });
    await store.ackCommand(C.device, id, 0);
    expect((await pool.query('SELECT status FROM device_command WHERE id = $1', [id])).rows[0].status).toBe('failed');
  });

  it('a newline-smuggled command is rejected before it reaches the database', async () => {
    await expect(store.enqueueCommand(C.device, 'CHECK\nSHELL rm -rf /')).rejects.toThrow(/single line/);
  });
});
