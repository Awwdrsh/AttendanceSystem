import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { ForbiddenError, HttpError, NotFoundError } from '../../errors.js';

class TooManyRequestsError extends HttpError { constructor(msg: string) { super(429, msg); } }
import { companyIdOf, hasRole } from '../auth/middleware.js';
import { uuid } from '../../validation.js';
import { DOCUMENT_MIMES, fileMeta, parseUpload, readFile, saveFile, sendStoredFile } from './store.js';

/** Purposes a signed-in user may upload for; each module attaches the upload to its own record. */
export const UPLOAD_PURPOSES = ['employee_document', 'company_document', 'expense_receipt', 'travel_bill', 'helpdesk_attachment',
  'training_certificate', 'profile_change', 'hiring_cv', 'avatar', 'tax_proof', 'grievance_attachment', 'task_attachment'] as const;

export const UPLOADS_PER_HOUR = 300;
export const UNATTACHED_BYTES_MAX = 40 * 1024 * 1024;

const uploadBody = z.object({
  purpose: z.enum(UPLOAD_PURPOSES),
  filename: z.string().min(1).max(200),
  dataUrl: z.string().min(10),
});

/**
 * POST /files uploads one encrypted file and returns its id; GET /files/:id serves it back to the uploader (or HR)
 * until it is attached. After attaching, the owning module serves it with its own access rules.
 */
export function filesRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  r.post('/', async (req, res) => {
    const b = uploadBody.parse(req.body);
    // Quota: unattached uploads are only purged after a day, so cap what one person can park in the meantime.
    const recent = await db.query<{ n: number; bytes: string }>(
      `SELECT count(*)::int AS n, coalesce(sum(bytes), 0)::text AS bytes FROM stored_file
        WHERE uploaded_by = $1 AND created_at > now() - interval '1 hour'`, [req.user!.id]);
    const parked = await db.query<{ bytes: string }>(
      `SELECT coalesce(sum(bytes), 0)::text AS bytes FROM stored_file WHERE uploaded_by = $1 AND attached_to IS NULL AND deleted_at IS NULL`, [req.user!.id]);
    if (recent.rows[0]!.n >= UPLOADS_PER_HOUR || Number(parked.rows[0]!.bytes) >= UNATTACHED_BYTES_MAX) {
      throw new TooManyRequestsError('Too many uploads. Finish the forms you have open or try again later.');
    }
    const file = parseUpload(b.dataUrl, { allowed: b.purpose === 'avatar' ? ['image/jpeg', 'image/png', 'image/webp'] : DOCUMENT_MIMES });
    const row = await saveFile(db, cipher, { companyId: companyIdOf(req), purpose: b.purpose, filename: b.filename, file, uploadedBy: req.user!.id });
    res.status(201).json({ file: { id: row.id, filename: row.filename, mime: row.mime, bytes: row.bytes, purpose: row.purpose } });
  });

  r.get('/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const meta = await fileMeta(db, companyId, id);
    if (!meta) throw new NotFoundError('file not found');
    if (meta.attached_to) throw new ForbiddenError('open this file from the page it belongs to');
    if (meta.uploaded_by !== req.user!.id && !hasRole(req.user!, 'hr')) throw new NotFoundError('file not found');
    sendStoredFile(res, await readFile(db, cipher, companyId, id), { download: req.query.download === '1' });
  });

  return r;
}
