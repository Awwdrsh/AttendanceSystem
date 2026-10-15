import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { ForbiddenError, NotFoundError } from '../../errors.js';
import { companyIdOf, hasRole } from '../auth/middleware.js';
import { uuid } from '../../validation.js';
import { DOCUMENT_MIMES, fileMeta, parseUpload, readFile, saveFile, sendStoredFile } from './store.js';

/** Purposes a signed-in user may upload for; each module attaches the upload to its own record. */
export const UPLOAD_PURPOSES = ['employee_document', 'company_document', 'expense_receipt', 'travel_bill', 'helpdesk_attachment',
  'training_certificate', 'profile_change', 'hiring_cv', 'avatar', 'tax_proof'] as const;

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
