import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import request from 'supertest';
import type { Express } from 'express';
import type { Db } from '../src/db/pool.js';
import { issueTicket, verifyTicket } from '../src/modules/kiosk/tokens.js';
import { UPLOADS_PER_HOUR } from '../src/modules/files/routes.js';
import { DEMO, auth, freshDb, loginAs, testApp, testCipher } from './setup-db.js';

const PDF = `data:application/pdf;base64,${Buffer.from('%PDF-1.4\n%%EOF').toString('base64')}`;

describe('fixes from the wave-1 security review', () => {
  let db: Db; let app: Express;
  beforeAll(async () => { db = await freshDb(); app = testApp(db); });
  afterAll(async () => { await db.end(); });

  it('a kiosk ticket remembers that the person used a QR badge, and that cannot be forged', () => {
    const kiosk = '00000000-0000-7000-8000-0000000000aa';
    const qr = issueTicket(testCipher, kiosk, DEMO.ram, Date.now(), true);
    const pin = issueTicket(testCipher, kiosk, DEMO.ram, Date.now(), false);
    expect(verifyTicket(testCipher, qr, kiosk)?.viaQr).toBe(true);
    expect(verifyTicket(testCipher, pin, kiosk)?.viaQr).toBe(false);
    const parts = qr.split('.'); parts[3] = `P${parts[3]!.slice(1)}`;
    expect(verifyTicket(testCipher, parts.join('.'), kiosk)).toBeNull();
  });

  it('colleagues do not see grades in the directory', async () => {
    await db.query(`UPDATE employee SET grade = 'G7' WHERE id = $1`, [DEMO.ram]);
    const emp = await loginAs(db, app, ['employee'], DEMO.sita);
    const r = await request(app).get('/api/v1/people/directory').set(auth(emp.token));
    expect(r.status).toBe(200);
    expect(JSON.stringify(r.body)).not.toContain('G7');
    const card = await request(app).get(`/api/v1/people/${DEMO.ram}/card`).set(auth(emp.token));
    expect(JSON.stringify(card.body)).not.toContain('G7');
  });

  it('one person cannot park unlimited uploads', async () => {
    const u = await loginAs(db, app, ['employee'], DEMO.ram);
    await db.query(
      `INSERT INTO stored_file (company_id, purpose, filename, mime, bytes, sha256, uploaded_by)
       SELECT $1, 'expense_receipt', 'x.pdf', 'application/pdf', 10, 'x', $2 FROM generate_series(1, $3)`, [DEMO.companyId, u.userId, UPLOADS_PER_HOUR]);
    const r = await request(app).post('/api/v1/files').set(auth(u.token)).send({ purpose: 'expense_receipt', filename: 'b.pdf', dataUrl: PDF });
    expect(r.status).toBe(429);
  });
});
