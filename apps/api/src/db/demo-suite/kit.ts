import type { AddressInfo } from 'node:net';
import type { Server } from 'node:http';
import { createApp } from '../../app.js';
import { randomToken, sha256Hex } from '../../crypto.js';
import type { FieldCipher } from '../../crypto.js';
import type { Db } from '../pool.js';

/**
 * Tools for the demo-suite seeder: it drives the real API in-process (so every record goes through the same validation,
 * workflows and side effects as a person clicking through the app) with short-lived sessions minted for demo users.
 */
export interface Actor { userId: string; employeeId: string | null; name: string; token: string }

export class ApiError extends Error {
  constructor(readonly status: number, readonly body: unknown, readonly route: string) {
    super(`${route} → ${status} ${typeof body === 'string' ? body : JSON.stringify(body)}`);
  }
}

export class Kit {
  private server: Server | null = null;
  private base = '';
  private tokens: string[] = [];

  constructor(readonly db: Db, readonly cipher: FieldCipher, readonly pepper: string) {}

  async start(): Promise<void> {
    const app = createApp({ db: this.db, cipher: this.cipher, pepper: this.pepper, secureCookie: false });
    await new Promise<void>((resolve) => { this.server = app.listen(0, '127.0.0.1', () => resolve()); });
    this.base = `http://127.0.0.1:${(this.server!.address() as AddressInfo).port}/api/v1`;
  }

  /** A one-hour session for a user; revoked again by stop(). */
  async actor(userId: string): Promise<Actor> {
    const u = await this.db.query<{ id: string; employee_id: string | null; name: string }>('SELECT id, employee_id, name FROM app_user WHERE id = $1', [userId]);
    if (!u.rows[0]) throw new Error(`user ${userId} not found`);
    const token = randomToken();
    await this.db.query(`INSERT INTO session (token_hash, user_id, expires_at, user_agent) VALUES ($1, $2, now() + interval '1 hour', 'demo-suite seeder')`, [sha256Hex(token), userId]);
    this.tokens.push(token);
    return { userId, employeeId: u.rows[0].employee_id, name: u.rows[0].name, token };
  }

  async call<T = Record<string, unknown>>(who: Actor | null, method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = {};
    if (who) headers.Authorization = `Bearer ${who.token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res: Response; let data: unknown;
    // A busy database can fail a request outright (lock table full, deadlock): the API rolls back and answers 500, so retry.
    for (let attempt = 1; ; attempt++) {
      res = await fetch(`${this.base}${path}`, { method, headers, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
      const text = await res.text();
      data = text;
      try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
      if (res.status < 500 || attempt >= 4) break;
      await new Promise((r) => setTimeout(r, 500 * 3 ** attempt));
    }
    if (!res.ok) throw new ApiError(res.status, data, `${method} ${path}`);
    return (data && typeof data === 'object' && !Array.isArray(data) ? created(data as Record<string, unknown>) : data) as T;
  }

  get = <T = Record<string, unknown>>(who: Actor | null, path: string) => this.call<T>(who, 'GET', path);
  post = <T = Record<string, unknown>>(who: Actor | null, path: string, body: unknown = {}) => this.call<T>(who, 'POST', path, body);
  put = <T = Record<string, unknown>>(who: Actor | null, path: string, body: unknown = {}) => this.call<T>(who, 'PUT', path, body);
  patch = <T = Record<string, unknown>>(who: Actor | null, path: string, body: unknown = {}) => this.call<T>(who, 'PATCH', path, body);

  /** Upload a generated one-page PDF and return the stored file id. */
  async uploadPdf(who: Actor, purpose: string, filename: string, title: string, lines: string[] = []): Promise<string> {
    const pdf = makePdf(title, lines);
    const r = await this.post<{ file: { id: string } }>(who, '/files', { purpose, filename, dataUrl: `data:application/pdf;base64,${pdf.toString('base64')}` });
    return r.file.id;
  }

  async stop(): Promise<void> {
    for (const t of this.tokens) await this.db.query('UPDATE session SET revoked_at = now() WHERE token_hash = $1 AND revoked_at IS NULL', [sha256Hex(t)]);
    this.tokens = [];
    await new Promise<void>((resolve) => (this.server ? this.server.close(() => resolve()) : resolve()));
    this.server = null;
  }
}

/**
 * Modules answer a create with `{ id }`, `{ thing: { id } }` or `{ thing: {...}, other }`. The seeder reads
 * `response.<anything>.id`: a missing key resolves to the response itself when it has an id, else to its first nested
 * object that has one.
 */
function created(body: Record<string, unknown>): Record<string, unknown> {
  return new Proxy(body, {
    get(t, p) {
      if (typeof p !== 'string' || p in t) return Reflect.get(t, p);
      if (typeof t.id === 'string') return t;
      return Object.values(t).find((v) => v && typeof v === 'object' && typeof (v as { id?: unknown }).id === 'string');
    },
  });
}

/** A small, valid one-page PDF with a title and a few lines of Helvetica text. */
export function makePdf(title: string, lines: string[] = []): Buffer {
  const esc = (s: string) => s.replace(/[^\x20-\x7e]/g, '?').replace(/([\\()])/g, '\\$1');
  const body = [`BT /F1 20 Tf 72 760 Td (${esc(title)}) Tj ET`, ...lines.map((l, i) => `BT /F1 11 Tf 72 ${724 - i * 18} Td (${esc(l)}) Tj ET`)].join('\n');
  const objs = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
    `<< /Length ${Buffer.byteLength(body, 'latin1')} >>\nstream\n${body}\nendstream`,
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
  ];
  let out = '%PDF-1.4\n';
  const offsets: number[] = [];
  objs.forEach((o, i) => { offsets.push(Buffer.byteLength(out, 'latin1')); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const xref = Buffer.byteLength(out, 'latin1');
  out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n${offsets.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('')}`;
  out += `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, 'latin1');
}

/** Deterministic pick so a re-run on a fresh copy produces the same demo. */
export const pick = <T>(list: readonly T[], i: number): T => list[((i % list.length) + list.length) % list.length]!;

export function addDaysIso(iso: string, n: number): string {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + n * 86_400_000).toISOString().slice(0, 10);
}

/** The Sunday on or before a date (Hajiri weeks run Sunday to Saturday). */
export function weekStart(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return addDaysIso(iso, -d.getUTCDay());
}

/** An instant for a Nepal-time date and HH:MM. */
export function nptInstant(iso: string, hhmm: string): Date {
  return new Date(Date.parse(`${iso}T${hhmm}:00+05:45`));
}
