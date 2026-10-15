import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Express } from 'express';
import type { Db } from '../src/db/pool.js';
import { attachFile, purgeUnattachedFiles } from '../src/modules/files/store.js';
import { DEMO, freshDb, loginAs, testApp } from './setup-db.js';

const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').toString('base64')}`;

describe('file store', () => {
  let db: Db; let app: Express; let hr: string; let emp: string;
  beforeAll(async () => {
    db = await freshDb(); app = testApp(db);
    hr = (await loginAs(db, app, ['hr'])).token;
    emp = (await loginAs(db, app, ['employee'], DEMO.ram)).token;
  });
  afterAll(async () => { await db.end(); });

  it('stores an encrypted PDF and serves it back to the uploader', async () => {
    const up = await request(app).post('/api/v1/files').set('Authorization', `Bearer ${emp}`).send({ purpose: 'expense_receipt', filename: 'bill.pdf', dataUrl: PDF });
    expect(up.status).toBe(201);
    const raw = await db.query('SELECT data_enc FROM stored_file WHERE id = $1', [up.body.file.id]);
    expect(raw.rows[0].data_enc.toString('latin1')).not.toContain('%PDF');
    const get = await request(app).get(`/api/v1/files/${up.body.file.id}`).set('Authorization', `Bearer ${emp}`);
    expect(get.status).toBe(200);
    expect(get.headers['content-type']).toContain('application/pdf');
    expect(get.headers['content-security-policy']).toContain('sandbox');
  });

  it('rejects a file whose bytes do not match the type', async () => {
    const bad = `data:application/pdf;base64,${Buffer.from('<html><script>alert(1)</script>').toString('base64')}`;
    const r = await request(app).post('/api/v1/files').set('Authorization', `Bearer ${emp}`).send({ purpose: 'expense_receipt', filename: 'x.pdf', dataUrl: bad });
    expect(r.status).toBe(400);
    const html = await request(app).post('/api/v1/files').set('Authorization', `Bearer ${emp}`).send({ purpose: 'expense_receipt', filename: 'x.html', dataUrl: 'data:text/html;base64,PGh0bWw+' });
    expect(html.status).toBe(400);
  });

  it('only the uploader attaches, and attached files are served by their module only', async () => {
    const up = await request(app).post('/api/v1/files').set('Authorization', `Bearer ${emp}`).send({ purpose: 'expense_receipt', filename: 'b.pdf', dataUrl: PDF });
    const id = up.body.file.id as string;
    const other = await request(app).get(`/api/v1/files/${id}`).set('Authorization', `Bearer ${(await loginAs(db, app, ['manager'])).token}`);
    expect(other.status).toBe(404);
    const owner = (await db.query('SELECT uploaded_by, company_id FROM stored_file WHERE id = $1', [id])).rows[0];
    await expect(attachFile(db, { companyId: owner.company_id, fileId: id, userId: owner.uploaded_by, purpose: 'travel_bill', attachedTo: 'x:1' })).rejects.toThrow();
    await attachFile(db, { companyId: owner.company_id, fileId: id, userId: owner.uploaded_by, purpose: 'expense_receipt', attachedTo: 'x:1' });
    const after = await request(app).get(`/api/v1/files/${id}`).set('Authorization', `Bearer ${emp}`);
    expect(after.status).toBe(403);
    void hr;
  });

  it('purges uploads never attached within a day', async () => {
    const n = await purgeUnattachedFiles(db, new Date(Date.now() + 2 * 86_400_000));
    expect(n).toBeGreaterThan(0);
    const left = await db.query('SELECT count(*)::int AS n FROM stored_file WHERE attached_to IS NULL AND data_enc IS NOT NULL');
    expect(left.rows[0].n).toBe(0);
  });
});
