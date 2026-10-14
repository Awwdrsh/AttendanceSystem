import { beforeEach, describe, expect, it } from 'vitest';
import request from 'supertest';
import { affectedDates, createGatewayApp } from './app.js';
import { TemplateCipher } from './crypto.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';

const signer = Signer.ephemeral();
const cipher = new TemplateCipher(Buffer.alloc(32, 9));
let store: MemoryStore; let deviceId: string;
const post = (app: ReturnType<typeof createGatewayApp>, body: string, table = 'ATTLOG') =>
  request(app).post(`/iclock/cdata?SN=FIX-01&table=${table}`).set('Content-Type', 'text/plain').send(body);
const fp = () => `FP PIN=1\tFID=6\tSize=1\tValid=1\tTMP=${Buffer.from('finger').toString('base64')}\n`;

beforeEach(() => {
  store = new MemoryStore();
  deviceId = store.addDevice({ serialNo: 'FIX-01' }).id;
  store.mappings.set(`${deviceId}:1`, 'emp-1');
});

describe('biometric templates need an active consent', () => {
  it('stores with consent, drops after the consent is withdrawn, and drops for unmapped PINs', async () => {
    const app = createGatewayApp({ store, signer, cipher });
    const consentId = store.grantConsent('emp-1', 'fingerprint');
    await post(app, fp(), 'OPERLOG');
    expect(store.templates).toHaveLength(1);
    expect(store.templates[0]!.consentId).toBe(consentId);
    store.revokeConsent('emp-1', 'fingerprint');
    await post(app, fp(), 'OPERLOG');
    expect(store.templates).toHaveLength(1);
    await post(app, fp().replace('PIN=1', 'PIN=99'), 'OPERLOG');
    expect(store.templates).toHaveLength(1);
  });
  it('a fingerprint consent does not cover a face template', async () => {
    const app = createGatewayApp({ store, signer, cipher });
    store.grantConsent('emp-1', 'fingerprint');
    const face = `FACE PIN=1\tFID=0\tSize=1\tValid=1\tTMP=${Buffer.from('face').toString('base64')}\n`;
    await post(app, face, 'OPERLOG');
    expect(store.templates).toHaveLength(0);
    store.grantConsent('emp-1', 'face');
    await post(app, face, 'OPERLOG');
    expect(store.templates.map((t) => t.modality)).toEqual(['face']);
  });
});

describe('recompute dates for a punch', () => {
  it('an afternoon punch recomputes only its own day; a morning punch also recomputes the previous day', async () => {
    expect(affectedDates(new Date('2026-09-21T09:15:00Z'), '2026-09-21')).toEqual(['2026-09-21']);            // 15:00 NPT
    expect(affectedDates(new Date('2026-09-21T00:15:00Z'), '2026-09-21')).toEqual(['2026-09-20', '2026-09-21']); // 06:00 NPT
    expect(affectedDates(new Date('2026-10-01T00:15:00Z'), '2026-10-01')[0]).toBe('2026-09-30');
  });
  it('a night-shift punch-out at 06:10 queues the day the shift started', async () => {
    const app = createGatewayApp({ store, signer });
    await post(app, '1\t2026-09-21 06:10:00\t1\t1\t0\n');
    expect(store.recomputeQueue.map((q) => q.localDate)).toEqual(['2026-09-20', '2026-09-21']);
  });
});
