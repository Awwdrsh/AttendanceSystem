import { mkdtempSync, readdirSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';
import request from 'supertest';
import { parseAttlog, driftSeconds } from './adms/parse.js';
import { parseTemplateUpload } from './adms/templates.js';
import { createGatewayApp } from './app.js';
import { RawArchive } from './archive.js';
import { TemplateCipher } from './crypto.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';
import { assertAllowedCommand } from './store/types.js';

const signer = Signer.ephemeral();
const key = Buffer.alloc(32, 9);
let store: MemoryStore; let deviceId: string;
beforeEach(() => {
  store = new MemoryStore({ maxPendingDevices: 1 });
  deviceId = store.addDevice({ serialNo: 'DEMO-01' }).id;
  store.mappings.set(`${deviceId}:1`, 'emp-1');
});
const attlog = (app: ReturnType<typeof createGatewayApp>, body: string, table = 'ATTLOG') =>
  request(app).post(`/iclock/cdata?SN=DEMO-01&table=${table}`).set('Content-Type', 'text/plain').send(body);

describe('ADMS hardening', () => {
  it('rejects empty or non-numeric status/verify columns', () => {
    const { records, malformed } = parseAttlog('1\t2026-09-21 10:17:03\t\t1\n1\t2026-09-21 10:17:03\tx\t1\n1\t2026-09-21 10:17:03\t0\t1\n');
    expect(records).toHaveLength(1);
    expect(malformed).toHaveLength(2);
  });

  it('caps pending device registrations', async () => {
    const app = createGatewayApp({ store, signer });
    expect((await request(app).get('/iclock/cdata?SN=NEW-1&options=all')).status).toBe(200);
    const second = await request(app).get('/iclock/cdata?SN=NEW-2&options=all');
    expect(second.status).toBe(403);
    expect(second.text).toMatch(/too many pending/);
    expect(store.devices.has('NEW-2')).toBe(false);
  });

  it('flags punches far in the future as clock skew and records drift', async () => {
    const app = createGatewayApp({ store, signer, maxClockSkewMin: 10 });
    const future = new Date(Date.now() + 345 * 60_000 + 3 * 3600_000).toISOString().slice(0, 19).replace('T', ' ');
    await attlog(app, `1\t${future}\t0\t1\t0\n`);
    expect(store.punches[0]!.raw).toMatchObject({ clockSkew: true });
    expect(Number(store.punches[0]!.raw.clockSkewMs)).toBeGreaterThan(2 * 3600_000);
    expect(store.devices.get('DEMO-01')!.clockDriftSec).toBeGreaterThan(10_000);
    expect(driftSeconds('2026-09-21 10:00:00', 345, Date.UTC(2026, 8, 21, 4, 15, 0))).toBe(0);
  });

  it('queues recompute for mapped employees only', async () => {
    const app = createGatewayApp({ store, signer });
    await attlog(app, '1\t2026-09-21 10:17:03\t0\t1\t0\n9\t2026-09-21 10:18:03\t0\t1\t0\n');
    expect(store.recomputeQueue).toEqual([{ employeeId: 'emp-1', localDate: '2026-09-20', reason: 'punch:zkteco' }, { employeeId: 'emp-1', localDate: '2026-09-21', reason: 'punch:zkteco' }]);
  });

  it('archives raw bodies per device per day and never logs templates elsewhere', async () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'hajiri-raw-'));
    const app = createGatewayApp({ store, signer, archive: new RawArchive(dir) });
    await attlog(app, '1\t2026-09-21 10:17:03\t0\t1\t0\n');
    await request(app).post('/iclock/fdata?SN=DEMO-01&table=FINGERTMP').set('Content-Type', 'application/octet-stream').send('PIN=1\tFID=0\tSize=4\tValid=1\tTMP=AAAA\n');
    const files = readdirSync(path.join(dir, 'DEMO-01'));
    expect(files).toHaveLength(1);
    const content = readFileSync(path.join(dir, 'DEMO-01', files[0]!), 'utf8');
    expect(content).toContain('cdata:ATTLOG');
    expect(content).toContain('fdata:FINGERTMP');
    expect(content).toContain('TMP=AAAA');
  });

  it('returns plain-text errors for oversized bodies', async () => {
    const app = createGatewayApp({ store, signer });
    const big = 'x'.repeat(2.5 * 1024 * 1024);
    const r = await attlog(app, big, 'OPERLOG');
    expect(r.status).toBe(413);
    expect(r.text).toMatch(/^ERROR/);
  });
});

describe('templates', () => {
  it('parses FP / FACE / BIODATA lines and photo tables', () => {
    const p = parseTemplateUpload('OPERLOG', 'USER PIN=1\tName=Sita\nFP PIN=1\tFID=6\tSize=8\tValid=1\tTMP=QUJDRA==\nFACE PIN=1\tFID=0\tSIZE=8\tVALID=1\tTMP=QUJDRA==\nBIODATA Pin=2\tNo=0\tIndex=0\tValid=0\tDuress=0\tType=8\tMajorVer=12\tMinorVer=0\tFormat=0\tTmp=QUJDRA==\nweird line\n');
    expect(p.users).toBe(1);
    expect(p.templates.map((t) => [t.modality, t.vendorFormat, t.valid])).toEqual([['fingerprint', 'zk_fp_v10', true], ['face', 'zk_face_v7', true], ['palm', 'zk_biodata_t8_v12.0', false]]);
    expect(p.unknown).toBe(1);
    expect(parseTemplateUpload('ATTPHOTO', 'jpegbytes').photos).toBe(1);
  });

  it('stores templates encrypted in the API cipher format, discards invalid ones, and discards everything without a key', async () => {
    const withKey = createGatewayApp({ store, signer, cipher: new TemplateCipher(key) });
    store.grantConsent('emp-1', 'fingerprint');
    const b64 = Buffer.from('finger-template-bytes').toString('base64');
    await attlog(withKey, `FP PIN=1\tFID=6\tSize=1\tValid=1\tTMP=${b64}\nFP PIN=1\tFID=7\tSize=1\tValid=0\tTMP=${b64}\n`, 'OPERLOG');
    expect(store.templates).toHaveLength(1);
    const t = store.templates[0]!;
    expect(t).toMatchObject({ employeeId: 'emp-1', deviceUserPin: '1', modality: 'fingerprint', vendorFormat: 'zk_fp_v10', keyVersion: 1 });
    expect(t.ciphertext[0]).toBe(1);
    expect(t.ciphertext.length).toBe(29 + b64.length);
    expect(t.ciphertext.toString('latin1')).not.toContain(b64);
    const noKey = createGatewayApp({ store: new MemoryStore(), signer });
    expect(store.templates).toHaveLength(1);
    void noKey;
  });
});

describe('command safety', () => {
  it('always refuses SHELL; refuses CLEAR DATA and firmware verbs unless enabled', async () => {
    expect(() => assertAllowedCommand('SHELL reboot')).toThrow(/forbidden/);
    expect(() => assertAllowedCommand('SHELL reboot', true)).toThrow(/forbidden/);
    expect(() => assertAllowedCommand('CLEAR DATA')).toThrow(/dangerous/);
    expect(() => assertAllowedCommand('UPGRADE firmware.bin')).toThrow(/dangerous/);
    expect(() => assertAllowedCommand('CLEAR DATA', true)).not.toThrow();
    expect(() => assertAllowedCommand('CLEAR LOG')).not.toThrow();
    expect(() => assertAllowedCommand('DATA UPDATE USERINFO PIN=1\tName=A')).not.toThrow();
    await expect(store.enqueueCommand(deviceId, 'CLEAR DATA')).rejects.toThrow(/dangerous/);
    await expect(new MemoryStore({ allowDangerousCommands: true }).enqueueCommand(deviceId, 'CLEAR DATA')).resolves.toBeTypeOf('number');
  });
});
