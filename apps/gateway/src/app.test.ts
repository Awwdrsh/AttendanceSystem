import { describe, expect, it, beforeEach } from 'vitest';
import request from 'supertest';
import { createGatewayApp } from './app.js';
import { parseAttlog, parseDeviceCmdResults } from './adms/parse.js';
import { deviceTimeToUtc, idempotencyKey } from './punch/event.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';

let store: MemoryStore;
let app: ReturnType<typeof createGatewayApp>;
let deviceId: string;
const signer = Signer.ephemeral();

beforeEach(() => {
  store = new MemoryStore();
  deviceId = store.addDevice({ serialNo: 'DEMO-01' }).id;
  store.mappings.set(`${deviceId}:1`, 'emp-1');
  app = createGatewayApp({ store, signer });
});

const attlog = (sn: string, body: string, extra = '') =>
  request(app).post(`/iclock/cdata?SN=${sn}&table=ATTLOG${extra}`).set('Content-Type', 'text/plain').send(body);

describe('handshake', () => {
  it('unknown SN is stored as pending and gets no options', async () => {
    const r = await request(app).get('/iclock/cdata?SN=NEW-99&options=all&pushver=2.4.1');
    expect(r.status).toBe(200);
    expect(r.text).toBe('OK');
    expect(store.devices.get('NEW-99')).toMatchObject({ status: 'pending', pushVersion: '2.4.1' });
  });
  it('active device gets the options block with stamps', async () => {
    store.devices.get('DEMO-01')!.attlogStamp = '12345';
    const r = await request(app).get('/iclock/cdata?SN=DEMO-01&options=all&pushver=2.4.1');
    expect(r.text).toMatch(/^GET OPTION FROM: DEMO-01\n/);
    expect(r.text).toContain('ATTLOGStamp=12345');
    expect(r.text).toContain('Realtime=1');
    expect(r.text).toContain('Delay=10');
  });
});

describe('ATTLOG upload', () => {
  it('parses tab-separated records, resolves employees, signs and stores', async () => {
    const r = await attlog('DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\t0\n2\t2026-09-21 10:18:00\t0\t15\t0\n', '&Stamp=777');
    expect(r.status).toBe(200);
    expect(r.text).toBe('OK: 2');
    expect(store.punches).toHaveLength(2);
    const p = store.punches[0]!;
    expect(p).toMatchObject({ deviceUserPin: '1', employeeId: 'emp-1', direction: 'in', verifyMode: 'fingerprint', localDate: '2026-09-21' });
    // 10:17:03 NPT = 04:32:03 UTC
    expect(p.punchedAt.toISOString()).toBe('2026-09-21T04:32:03.000Z');
    expect(p.gatewaySignature.length).toBeGreaterThan(40);
    expect(store.punches[1]).toMatchObject({ employeeId: null, verifyMode: 'face' });
    expect(store.devices.get('DEMO-01')!.attlogStamp).toBe('777');
  });
  it('replayed buffered records are idempotent and near-duplicates within 5 s are ignored', async () => {
    const line = '1\t2026-09-21 10:17:03\t0\t1\t0\n';
    await attlog('DEMO-01', line);
    await attlog('DEMO-01', line);
    await attlog('DEMO-01', '1\t2026-09-21 10:17:06\t0\t1\t0\n');
    await attlog('DEMO-01', '1\t2026-09-21 10:17:09\t0\t1\t0\n');
    expect(store.punches).toHaveLength(2);
  });
  it('never dedupes across devices', async () => {
    store.addDevice({ serialNo: 'DEMO-02' });
    await attlog('DEMO-01', '1\t2026-09-21 10:17:03\t0\t1\t0\n');
    await attlog('DEMO-02', '1\t2026-09-21 10:17:03\t0\t1\t0\n');
    expect(store.punches).toHaveLength(2);
  });
  it('skips malformed lines but still acknowledges the good ones', async () => {
    const r = await attlog('DEMO-01', 'garbage\n1\tnot-a-time\t0\t1\n1\t2026-09-21 18:40:00\t1\t1\t0\n');
    expect(r.text).toBe('OK: 1');
    expect(store.punches).toHaveLength(1);
    expect(store.punches[0]!.direction).toBe('out');
  });
  it('rejects data from pending or unknown devices', async () => {
    await request(app).get('/iclock/cdata?SN=NEW-99&options=all');
    expect((await attlog('NEW-99', '1\t2026-09-21 10:17:03\t0\t1\t0\n')).status).toBe(403);
    expect((await attlog('GHOST', '1\t2026-09-21 10:17:03\t0\t1\t0\n')).status).toBe(403);
    expect(store.punches).toHaveLength(0);
  });
});

describe('command channel', () => {
  it('delivers queued commands once and records results', async () => {
    const id = await store.enqueueCommand(deviceId, 'DATA UPDATE USERINFO PIN=3\tName=Hari\tPri=0');
    const first = await request(app).get('/iclock/getrequest?SN=DEMO-01');
    expect(first.text).toBe(`C:${id}:DATA UPDATE USERINFO PIN=3\tName=Hari\tPri=0\n`);
    expect((await request(app).get('/iclock/getrequest?SN=DEMO-01')).text).toBe('OK');
    await request(app).post('/iclock/devicecmd?SN=DEMO-01').set('Content-Type', 'text/plain').send(`ID=${id}&Return=0&CMD=DATA`);
    expect(store.commands[0]).toMatchObject({ status: 'acked', returnCode: 0 });
  });
  it('marks failed results', async () => {
    const id = await store.enqueueCommand(deviceId, 'CHECK');
    await request(app).get('/iclock/getrequest?SN=DEMO-01');
    await request(app).post('/iclock/devicecmd').set('Content-Type', 'text/plain').send(`ID=${id}&Return=-1002&CMD=CHECK`);
    expect(store.commands[0]).toMatchObject({ status: 'failed', returnCode: -1002 });
  });
  it('SHELL can never be queued', async () => {
    await expect(store.enqueueCommand(deviceId, 'SHELL reboot')).rejects.toThrow(/forbidden/);
    await expect(store.enqueueCommand(deviceId, '  shell ls')).rejects.toThrow(/forbidden/);
  });
  it('pending devices get OK with no commands; unknown devices 404', async () => {
    await request(app).get('/iclock/cdata?SN=NEW-99&options=all');
    expect((await request(app).get('/iclock/getrequest?SN=NEW-99')).text).toBe('OK');
    expect((await request(app).get('/iclock/getrequest?SN=GHOST')).status).toBe(404);
  });
});

describe('helpers', () => {
  it('parseAttlog handles CRLF and optional workcode', () => {
    const { records, malformed } = parseAttlog('1\t2026-01-01 00:00:00\t0\t1\r\n2\t2026-01-01 00:00:01\t1\t15\t7\r\n');
    expect(records).toHaveLength(2);
    expect(records[1]!.workcode).toBe('7');
    expect(malformed).toEqual([]);
  });
  it('parseDeviceCmdResults handles multiple lines', () => {
    expect(parseDeviceCmdResults('ID=1&Return=0&CMD=DATA\nID=2&Return=-30&CMD=CHECK\n')).toEqual([
      { id: 1, returnCode: 0, cmd: 'DATA' }, { id: 2, returnCode: -30, cmd: 'CHECK' },
    ]);
  });
  it('idempotency key is per vendor/device/pin/time/mode and device time honours the offset', () => {
    const a = idempotencyKey({ vendor: 'zkteco', deviceSn: 'A', deviceUserPin: '1', deviceLocalTime: '2026-09-21 10:17:03', verifyMode: 'fingerprint' });
    const b = idempotencyKey({ vendor: 'zkteco', deviceSn: 'B', deviceUserPin: '1', deviceLocalTime: '2026-09-21 10:17:03', verifyMode: 'fingerprint' });
    expect(a).not.toBe(b);
    expect(deviceTimeToUtc('2026-09-21 00:00:00', 345).toISOString()).toBe('2026-09-20T18:15:00.000Z');
  });
  it('signer round-trips from a base64 PKCS8 key', async () => {
    const { generateKeyPairSync } = await import('node:crypto');
    const { privateKey } = generateKeyPairSync('ed25519');
    const b64 = privateKey.export({ type: 'pkcs8', format: 'der' }).toString('base64');
    const s = Signer.fromBase64Pkcs8(b64);
    expect(s.ephemeral).toBe(false);
    expect(s.sign('x')).toHaveLength(88);
  });
});
