import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { Writable } from 'node:stream';
import { fileURLToPath } from 'node:url';
import pino from 'pino';
import request from 'supertest';
import { beforeEach, describe, expect, it } from 'vitest';
import { parseDeviceCmdResults } from './adms/parse.js';
import { createGatewayApp, redactUrl } from './app.js';
import { RawArchive } from './archive.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';

const signer = Signer.ephemeral();
let store: MemoryStore; let a: string; let b: string;
beforeEach(() => {
  store = new MemoryStore();
  a = store.addDevice({ serialNo: 'DEV-A' }).id;
  b = store.addDevice({ serialNo: 'DEV-B' }).id;
  store.addDevice({ serialNo: 'DEV-OFF', status: 'disabled' });
  store.mappings.set(`${a}:1`, 'emp-1');
});
const nptNow = (offsetMs = 0) => new Date(Date.now() + 345 * 60_000 + offsetMs).toISOString().slice(0, 19).replace('T', ' ');

describe('command results', () => {
  it('are accepted only from an approved device and only for its own commands', async () => {
    const app = createGatewayApp({ store, signer });
    const id = await store.enqueueCommand(a, 'CHECK');
    await request(app).get('/iclock/getrequest?SN=DEV-A');
    const ack = (sn: string | null) => request(app).post(`/iclock/devicecmd${sn ? `?SN=${sn}` : ''}`).set('Content-Type', 'text/plain').send(`ID=${id}&Return=0&CMD=CHECK`);
    expect((await ack(null)).status).toBe(403);
    expect((await ack('NOBODY')).status).toBe(403);
    expect((await ack('DEV-OFF')).status).toBe(403);
    expect((await ack('DEV-B')).status).toBe(200);           // device B answers for a command id it never received
    expect(store.commands[0]!.status).toBe('sent');
    expect((await ack('DEV-A')).status).toBe(200);
    expect(store.commands[0]).toMatchObject({ status: 'acked', returnCode: 0, deviceId: a });
    expect(b).not.toBe(a);
  });
  it('ignores out-of-range ids and return codes', () => {
    expect(parseDeviceCmdResults('ID=99999999999999999999&Return=0\nID=-4&Return=0\nID=7&Return=99999999999\nID=8&Return=-1002')).toEqual([{ id: 8, returnCode: -1002, cmd: '' }]);
  });
});

describe('ADMS cursor and version injection', () => {
  it('a stamp with a line break never reaches the handshake options block', async () => {
    const app = createGatewayApp({ store, signer });
    await request(app).post(`/iclock/cdata?SN=DEV-A&table=ATTLOG&Stamp=${encodeURIComponent('5\nEncrypt=1')}`).set('Content-Type', 'text/plain').send(`1\t${nptNow()}\t0\t1\n`);
    await request(app).post(`/iclock/cdata?SN=DEV-A&table=OPERLOG&OpStamp=${encodeURIComponent('7\r\nRealtime=0')}`).set('Content-Type', 'text/plain').send('');
    const opts = await request(app).get('/iclock/cdata?SN=DEV-A&options=all');
    expect(opts.text).not.toMatch(/Encrypt=1|Realtime=0/);
    expect(opts.text).toContain('Stamp=0\n');
    await request(app).post('/iclock/cdata?SN=DEV-A&table=ATTLOG&Stamp=123456').set('Content-Type', 'text/plain').send('');
    expect((await request(app).get('/iclock/cdata?SN=DEV-A&options=all')).text).toContain('ATTLOGStamp=123456\n');
  });
  it('push versions are bounded and printable', async () => {
    const app = createGatewayApp({ store, signer });
    await request(app).get(`/iclock/cdata?SN=NEW-1&options=all&pushver=${encodeURIComponent('<script>x</script>'.repeat(20))}`);
    expect(store.devices.get('NEW-1')!.pushVersion).toBeUndefined();
    await request(app).get('/iclock/cdata?SN=NEW-2&options=all&pushver=2.4.1');
    expect(store.devices.get('NEW-2')!.pushVersion).toBe('2.4.1');
  });
});

describe('punch time sanity', () => {
  it('rejects reset clocks and punches days ahead; keeps small skews flagged', async () => {
    const app = createGatewayApp({ store, signer, maxClockSkewMin: 10 });
    const r = await request(app).post('/iclock/cdata?SN=DEV-A&table=ATTLOG').set('Content-Type', 'text/plain')
      .send(`1\t2000-01-01 00:00:05\t0\t1\n1\t${nptNow(3 * 86_400_000)}\t0\t1\n1\t${nptNow(2 * 3600_000)}\t0\t1\n1\t${nptNow(-60_000)}\t1\t1\n`);
    expect(r.text).toBe('OK: 4');                             // acknowledged so the terminal does not resend forever
    expect(store.punches).toHaveLength(2);
    expect(store.punches.filter((p) => p.raw.clockSkew)).toHaveLength(1);
    expect(store.punches.every((p) => p.localDate >= '2020-01-01')).toBe(true);
  });
});

describe('Hikvision authentication', () => {
  const event = () => ({ eventType: 'AccessControllerEvent', dateTime: `${nptNow().replace(' ', 'T')}+05:45`, AccessControllerEvent: { majorEventType: 5, subEventType: 75, employeeNoString: '1' } });
  it('nothing from an unauthenticated sender is archived or stored', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'hajiri-sec-'));
    const hik = store.addDevice({ serialNo: 'HIK-1', vendor: 'hikvision', protocol: 'isapi', commKeyHash: createHash('sha256').update('s3cret').digest('hex') });
    store.mappings.set(`${hik.id}:1`, 'emp-1');
    const app = createGatewayApp({ store, signer, archive: new RawArchive(dir) });
    expect((await request(app).post('/hikvision/event?sn=HIK-1&key=wrong').send(event())).status).toBe(403);
    expect((await request(app).post('/hikvision/event?sn=HIK-1').send(event())).status).toBe(403);
    expect(readdirSync(dir)).toEqual([]);
    expect(store.punches).toHaveLength(0);
    expect((await request(app).post('/hikvision/event?sn=HIK-1').set('x-device-key', 's3cret').send(event())).status).toBe(200);
    expect(store.punches).toHaveLength(1);
    expect(readdirSync(dir)).toEqual(['HIK-1']);
  });
  it('device keys never appear in request logs', async () => {
    const lines: string[] = [];
    const sink = new Writable({ write(chunk, _e, cb) { lines.push(String(chunk)); cb(); } });
    store.addDevice({ serialNo: 'HIK-2', vendor: 'hikvision', protocol: 'isapi', commKeyHash: createHash('sha256').update('topsecret-key').digest('hex') });
    const app = createGatewayApp({ store, signer, logger: pino({ level: 'info' }, sink) });
    await request(app).post('/hikvision/event?sn=HIK-2&key=topsecret-key').set('x-device-key', 'topsecret-key').send({ eventType: 'heartbeat' });
    await new Promise((r) => setTimeout(r, 20));
    expect(lines.join('')).toContain('/hikvision/event?sn=HIK-2&key=[redacted]');
    expect(lines.join('')).not.toContain('topsecret-key');
    expect(redactUrl('/x?a=1&KEY=abc&token=t&b=2')).toBe('/x?a=1&KEY=[redacted]&token=[redacted]&b=2');
  });
});

describe('startup', () => {
  it('refuses to start in production with an ephemeral signing key', () => {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const cwd = mkdtempSync(path.join(tmpdir(), 'hajiri-boot-'));           // no .env here
    const tsx = path.join(here, '..', 'node_modules', 'tsx', 'dist', 'loader.mjs');
    let out = '';
    try {
      execFileSync(process.execPath, ['--import', tsx, path.join(here, 'server.ts')], { cwd, env: { PATH: process.env.PATH, NODE_ENV: 'production', PORT: '18099' }, timeout: 20_000, stdio: 'pipe' });
    } catch (e) { out = String((e as { stderr?: Buffer }).stderr ?? ''); }
    expect(out).toMatch(/GATEWAY_SIGNING_KEY is required/);
  });
});
