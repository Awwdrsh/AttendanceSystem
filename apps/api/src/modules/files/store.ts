import { createHash } from 'node:crypto';
import type { Response } from 'express';
import type { FieldCipher } from '../../crypto.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ForbiddenError, NotFoundError } from '../../errors.js';

export const FILE_MAX_BYTES = 4 * 1024 * 1024;

export type FileMime = 'application/pdf' | 'image/jpeg' | 'image/png' | 'image/webp';
export const DOCUMENT_MIMES: FileMime[] = ['application/pdf', 'image/jpeg', 'image/png', 'image/webp'];
export const IMAGE_MIMES: FileMime[] = ['image/jpeg', 'image/png', 'image/webp'];

/** Magic numbers: the declared type must match the bytes, so nothing but a real PDF or image is stored or served. */
const SIGNATURES: Record<FileMime, (b: Buffer) => boolean> = {
  'application/pdf': (b) => b.length > 5 && b.subarray(0, 5).toString('latin1') === '%PDF-',
  'image/jpeg': (b) => b.length > 3 && b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff,
  'image/png': (b) => b.length > 8 && b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])),
  'image/webp': (b) => b.length > 12 && b.subarray(0, 4).toString('latin1') === 'RIFF' && b.subarray(8, 12).toString('latin1') === 'WEBP',
};

export interface ParsedFile { mime: FileMime; data: Buffer }

/** Decode a `data:<mime>;base64,` upload, checking the allowed types, the signature and the size. */
export function parseUpload(dataUrl: string, opts: { allowed?: FileMime[]; maxBytes?: number } = {}): ParsedFile {
  const allowed = opts.allowed ?? DOCUMENT_MIMES;
  const max = Math.min(opts.maxBytes ?? FILE_MAX_BYTES, FILE_MAX_BYTES);
  const m = /^data:([a-z]+\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})$/.exec(dataUrl.trim());
  if (!m || !allowed.includes(m[1] as FileMime)) {
    throw new BadRequestError(`The file must be ${allowed.map((a) => a === 'application/pdf' ? 'a PDF' : a.replace('image/', '').toUpperCase()).join(', ')}.`);
  }
  const mime = m[1] as FileMime;
  const data = Buffer.from(m[2]!, 'base64');
  if (data.length === 0) throw new BadRequestError('The file is empty.');
  if (data.length > max) throw new BadRequestError(`The file is too large (max ${Math.round(max / 1024 / 1024 * 10) / 10} MB).`);
  if (!SIGNATURES[mime](data)) throw new BadRequestError('The file could not be read. Check that it is not damaged.');
  return { mime, data };
}

/** Keep a file name printable and short; never trust it for anything but display. */
export function cleanFilename(name: string): string {
  const s = name.normalize('NFC').replace(/[\u0000-\u001f\u007f"\\/<>:|?*]+/g, '_').replace(/\s+/g, ' ').trim();
  return (s || 'file').slice(0, 200);
}

const aad = (companyId: string, id: string) => `stored_file:${companyId}:${id}`;

export interface StoredFileRow {
  id: string; company_id: string; purpose: string; filename: string; mime: FileMime; bytes: number; sha256: string;
  uploaded_by: string | null; attached_to: string | null; deleted_at: Date | null; created_at: Date;
}

export async function saveFile(db: Queryable, cipher: FieldCipher, f: {
  companyId: string; purpose: string; filename: string; file: ParsedFile; uploadedBy: string | null; attachedTo?: string | null;
}): Promise<StoredFileRow> {
  const id = (await db.query<{ id: string }>('SELECT uuidv7() AS id')).rows[0]!.id;
  const sha = createHash('sha256').update(f.file.data).digest('hex');
  const r = await db.query<StoredFileRow>(
    `INSERT INTO stored_file (id, company_id, purpose, filename, mime, bytes, sha256, data_enc, uploaded_by, attached_to, attached_at)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::text, CASE WHEN $10::text IS NULL THEN NULL ELSE now() END)
     RETURNING id, company_id, purpose, filename, mime, bytes, sha256, uploaded_by, attached_to, deleted_at, created_at`,
    [id, f.companyId, f.purpose, cleanFilename(f.filename), f.file.mime, f.file.data.length, sha,
      cipher.encrypt(f.file.data.toString('base64'), aad(f.companyId, id)), f.uploadedBy, f.attachedTo ?? null]);
  return r.rows[0]!;
}

export async function fileMeta(db: Queryable, companyId: string, id: string): Promise<StoredFileRow | null> {
  const r = await db.query<StoredFileRow>(
    `SELECT id, company_id, purpose, filename, mime, bytes, sha256, uploaded_by, attached_to, deleted_at, created_at
       FROM stored_file WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL`, [id, companyId]);
  return r.rows[0] ?? null;
}

/**
 * Attach an uploaded file to a record (e.g. `expense_claim:<id>`). Only the uploader may attach their own upload, only
 * once, and only for the expected purpose; returns the file row. Call inside the module's transaction.
 */
export async function attachFile(db: Queryable, f: { companyId: string; fileId: string; userId: string; purpose: string | string[]; attachedTo: string }): Promise<StoredFileRow> {
  const meta = await fileMeta(db, f.companyId, f.fileId);
  if (!meta) throw new BadRequestError('The attached file was not found. Upload it again.');
  const purposes = Array.isArray(f.purpose) ? f.purpose : [f.purpose];
  if (!purposes.includes(meta.purpose)) throw new BadRequestError('This file was uploaded for something else. Upload it again.');
  if (meta.attached_to && meta.attached_to !== f.attachedTo) throw new BadRequestError('This file is already attached to another record.');
  if (meta.uploaded_by !== f.userId) throw new ForbiddenError('You can only attach files you uploaded.');
  await db.query('UPDATE stored_file SET attached_to = $2, attached_at = coalesce(attached_at, now()) WHERE id = $1', [f.fileId, f.attachedTo]);
  return { ...meta, attached_to: f.attachedTo };
}

export async function readFile(db: Queryable, cipher: FieldCipher, companyId: string, id: string): Promise<StoredFileRow & { data: Buffer }> {
  const r = await db.query<StoredFileRow & { data_enc: Buffer | null }>(
    `SELECT id, company_id, purpose, filename, mime, bytes, sha256, uploaded_by, attached_to, deleted_at, created_at, data_enc
       FROM stored_file WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL`, [id, companyId]);
  const row = r.rows[0];
  if (!row || !row.data_enc) throw new NotFoundError('file not found');
  const { data_enc, ...meta } = row;
  return { ...meta, data: Buffer.from(cipher.decrypt(data_enc, aad(companyId, id)), 'base64') };
}

/** Soft-delete: the bytes are wiped, the row stays as a record that a file existed. */
export async function deleteFile(db: Queryable, companyId: string, id: string): Promise<void> {
  await db.query('UPDATE stored_file SET data_enc = NULL, deleted_at = now() WHERE id = $1 AND company_id = $2', [id, companyId]);
}

/** Send a decrypted file. PDFs and images open in the browser unless `download` is set; nothing is ever sniffed as HTML. */
export function sendStoredFile(res: Response, f: { filename: string; mime: string; data: Buffer }, opts: { download?: boolean } = {}): void {
  const ascii = f.filename.replace(/[^\x20-\x7e]/g, '_').replace(/"/g, "'");
  res.setHeader('Content-Type', f.mime);
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Cache-Control', 'private, no-store');
  res.setHeader('Content-Security-Policy', "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox");
  res.setHeader('Content-Disposition', `${opts.download ? 'attachment' : 'inline'}; filename="${ascii}"; filename*=UTF-8''${encodeURIComponent(f.filename)}`);
  res.send(f.data);
}

/** Nightly: drop uploads that were never attached to anything within a day. */
export async function purgeUnattachedFiles(db: Queryable, now = new Date()): Promise<number> {
  const r = await db.query(
    `UPDATE stored_file SET data_enc = NULL, deleted_at = $1
      WHERE attached_to IS NULL AND deleted_at IS NULL AND created_at < $1::timestamptz - interval '1 day'`, [now]);
  return r.rowCount ?? 0;
}
