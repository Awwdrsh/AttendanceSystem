import { generateKeyPairSync, verify } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import request from 'supertest';
import { createGatewayApp } from './app.js';
import { TemplateCipher } from './crypto.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';

describe('operational endpoints', () => {
  it('GET /health reports an ephemeral signer and discarded templates by default', async () => {
    const app = createGatewayApp({ store: new MemoryStore(), signer: Signer.ephemeral() });
    const r = await request(app).get('/health');
    expect(r.status).toBe(200);
    expect(r.body).toEqual({ ok: true, signer: 'ephemeral', templates: 'discarded' });
  });
  it('GET /health reports a configured signer and encrypted templates when keys are set', async () => {
    const { privateKey } = generateKeyPairSync('ed25519');
    const signer = Signer.fromBase64Pkcs8(privateKey.export({ type: 'pkcs8', format: 'der' }).toString('base64'));
    const app = createGatewayApp({ store: new MemoryStore(), signer, cipher: new TemplateCipher(Buffer.alloc(32, 3)) });
    expect((await request(app).get('/health')).body).toEqual({ ok: true, signer: 'configured', templates: 'encrypted' });
  });
  it('GET /public-key serves the PEM that verifies the gateway signatures', async () => {
    const signer = Signer.ephemeral();
    const app = createGatewayApp({ store: new MemoryStore(), signer });
    const r = await request(app).get('/public-key');
    expect(r.status).toBe(200);
    expect(r.headers['content-type']).toMatch(/text\/plain/);
    expect(r.text).toMatch(/^-----BEGIN PUBLIC KEY-----\n[\s\S]+-----END PUBLIC KEY-----\n$/);
    const sig = signer.sign('canonical-event');
    expect(verify(null, Buffer.from('canonical-event'), r.text, Buffer.from(sig, 'base64'))).toBe(true);
  });
});
