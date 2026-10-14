import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { audit } from '../../audit.js';
import { ForbiddenError, NotFoundError } from '../../errors.js';
import { uuid } from '../../validation.js';
import { hasRole, requireRole } from '../auth/middleware.js';
import type { AuthUser } from '../auth/service.js';
import type { Queryable } from '../../db/pool.js';

/**
 * Biometric consent, template lifecycle (crypto-shredding), transparency and data-subject access
 * (plan §6.11 Privacy Act 2075, §9.1 decisions 5-6, §12.3, §12.11, §13.13).
 * Ciphertext never leaves the database through this module.
 */

const RETENTION = {
  templates: { en: 'Biometric templates: until 30 days after exit, then cryptographically shredded', ne: 'बायोमेट्रिक टेम्प्लेट: बहिर्गमनको ३० दिन पछिसम्म, त्यसपछि नष्ट' },
  photos: { en: 'Attendance photos: 90 days (only on devices where photo capture is enabled)', ne: 'हाजिरी फोटो: ९० दिन (फोटो खिच्ने सुविधा सक्षम भएका यन्त्रमा मात्र)' },
  attendance: { en: 'Attendance records: 5 years', ne: 'हाजिरी अभिलेख: ५ वर्ष' },
  payroll: { en: 'Payroll records: 7 years', ne: 'तलब अभिलेख: ७ वर्ष' },
};

async function shredTemplates(db: Queryable, where: string, params: unknown[]): Promise<number> {
  const r = await db.query(`UPDATE biometric_template SET ciphertext = '\\x'::bytea, shredded_at = now() WHERE shredded_at IS NULL AND ${where}`, params);
  return r.rowCount ?? 0;
}

async function employeeInCompany(db: Queryable, employeeId: string, companyId: string | null): Promise<boolean> {
  const r = await db.query('SELECT 1 FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  return r.rows.length > 0;
}

/**
 * Consent, templates and data exports are the employee's own business or HR's: managers may see attendance, never
 * biometric records (plan §12.11 data minimisation). Cross-company ids are indistinguishable from missing ones (404).
 */
async function assertSelfOrHr(db: Queryable, user: AuthUser, employeeId: string): Promise<void> {
  if (!(await employeeInCompany(db, employeeId, user.companyId))) throw new NotFoundError('employee not found');
  if (hasRole(user, 'hr') || user.employeeId === employeeId) return;
  throw new ForbiddenError('not allowed to access this employee');
}

export function biometricsRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/transparency', (_req, res) => {
    res.json({
      stored: {
        en: ['Face, fingerprint or palm template produced by the terminal (encrypted; the original image is not kept)', 'Punch time, device and verification method', 'Location only at the moment of a mobile punch'],
        ne: ['यन्त्रले बनाएको अनुहार, औंठाछाप वा हत्केलाको टेम्प्लेट (इन्क्रिप्ट गरिएको; मूल तस्बिर राखिँदैन)', 'हाजिरीको समय, यन्त्र र प्रमाणीकरण विधि', 'मोबाइल हाजिरीको क्षणमा मात्र स्थान'],
      },
      notStored: { en: ['Continuous location tracking', 'Productivity scores or lateness rankings'], ne: ['निरन्तर स्थान ट्र्याकिङ', 'उत्पादकता अंक वा ढिलाइको श्रेणी'] },
      retention: RETENTION,
      access: {
        en: ['You: your own punches, attendance, corrections and this record', 'Your manager: your attendance status and requests', 'HR: your profile, attendance and payroll; sensitive fields are decrypted on demand and every read is logged'],
        ne: ['तपाईं: आफ्नै हाजिरी, सुधार र यो अभिलेख', 'तपाईंको प्रबन्धक: तपाईंको हाजिरी स्थिति र अनुरोध', 'मानव संसाधन: प्रोफाइल र हाजिरी; संवेदनशील क्षेत्र आवश्यक पर्दा मात्र खोलिन्छ र हरेक पहुँच लग हुन्छ', 'वित्त: तलब डेटा मात्र'],
      },
      withdraw: {
        en: 'You may withdraw biometric consent at any time from My data; your templates are shredded and you punch with card or PIN instead. Contact HR to correct any record (Privacy Act 2075 s.28).',
        ne: 'तपाईंले जुनसुकै बेला "मेरो डेटा" बाट बायोमेट्रिक सहमति फिर्ता लिन सक्नुहुन्छ; टेम्प्लेट नष्ट गरिन्छ र कार्ड वा पिनबाट हाजिरी गर्न सकिन्छ। अभिलेख सच्याउन मानव संसाधनमा सम्पर्क गर्नुहोस् (गोपनीयता ऐन २०७५ दफा २८)।',
      },
      legalBasis: { en: 'Privacy Act 2075 s.23 (consent and notice), s.25 (security), s.28 (correction)', ne: 'गोपनीयता ऐन २०७५ दफा २३, २५, २८' },
    });
  });

  r.post('/consent', async (req, res) => {
    const b = z.object({
      employeeId: uuid, purpose: z.string().min(1).max(200).default('attendance'), modality: z.enum(['face', 'fingerprint', 'palm', 'card']),
      noticeVersion: z.string().min(1).max(40), signature: z.string().max(4000).optional(),
    }).parse(req.body);
    await assertSelfOrHr(db, req.user!, b.employeeId);
    const row = await db.query<{ id: string; granted_at: Date }>(
      `INSERT INTO consent_record (employee_id, purpose, modality, notice_version, signature, recorded_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id, granted_at`,
      [b.employeeId, b.purpose, b.modality, b.noticeVersion, b.signature ?? null, req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'consent_granted', table: 'consent_record', rowId: row.rows[0]!.id, after: { employeeId: b.employeeId, modality: b.modality, noticeVersion: b.noticeVersion } });
    res.status(201).json({ consent: { id: row.rows[0]!.id, grantedAt: row.rows[0]!.granted_at } });
  });

  r.get('/consent', async (req, res) => {
    const q = z.object({ employeeId: uuid }).parse(req.query);
    await assertSelfOrHr(db, req.user!, q.employeeId);
    const rows = await db.query(
      `SELECT c.id, c.purpose, c.modality, c.notice_version, c.granted_at, c.revoked_at FROM consent_record c JOIN employee e ON e.id = c.employee_id
        WHERE c.employee_id = $1 AND e.company_id = $2 ORDER BY c.granted_at DESC`, [q.employeeId, req.user!.companyId]);
    res.json({ consents: rows.rows });
  });

  /** Withdrawal shreds that modality's templates immediately (plan §12.11 crypto-shredding). */
  r.post('/consent/:id/revoke', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const c = await db.query<{ employee_id: string; modality: string; revoked_at: Date | null }>(
      `SELECT c.employee_id, c.modality, c.revoked_at FROM consent_record c JOIN employee e ON e.id = c.employee_id WHERE c.id = $1 AND e.company_id = $2`, [id, req.user!.companyId]);
    const row = c.rows[0];
    if (!row) throw new NotFoundError('consent not found');
    await assertSelfOrHr(db, req.user!, row.employee_id);
    const client = await db.connect();
    let shredded = 0;
    try {
      await client.query('BEGIN');
      if (!row.revoked_at) await client.query('UPDATE consent_record SET revoked_at = now() WHERE id = $1', [id]);
      shredded = await shredTemplates(client, 'employee_id = $1 AND modality = $2', [row.employee_id, row.modality]);
      await audit(client, { actorId: req.user!.id, action: 'consent_revoked', table: 'consent_record', rowId: id, after: { employeeId: row.employee_id, modality: row.modality, templatesShredded: shredded } });
      await client.query('COMMIT');
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
    res.json({ ok: true, templatesShredded: shredded });
  });

  /** Metadata only; ciphertext is never returned. */
  r.get('/templates', async (req, res) => {
    const q = z.object({ employeeId: uuid }).parse(req.query);
    await assertSelfOrHr(db, req.user!, q.employeeId);
    const rows = await db.query(
      `SELECT t.id, t.modality, t.vendor_format, t.device_user_pin, d.serial_no AS device_sn, t.key_version, t.consent_id, t.created_at, t.shredded_at
         FROM biometric_template t JOIN employee e ON e.id = t.employee_id LEFT JOIN device d ON d.id = t.enrolled_on_device_id
        WHERE t.employee_id = $1 AND e.company_id = $2 ORDER BY t.created_at DESC`, [q.employeeId, req.user!.companyId]);
    res.json({ templates: rows.rows });
  });

  /** Templates uploaded for a device PIN that is not mapped yet (so HR can map or shred them). */
  r.get('/templates/unmapped', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT t.id, t.modality, t.vendor_format, t.device_user_pin, d.serial_no AS device_sn, t.created_at
         FROM biometric_template t JOIN device d ON d.id = t.enrolled_on_device_id
        WHERE t.employee_id IS NULL AND t.shredded_at IS NULL AND d.company_id = $1 ORDER BY t.created_at DESC LIMIT 500`, [req.user!.companyId]);
    res.json({ templates: rows.rows });
  });

  r.post('/templates/:id/shred', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const t = await db.query<{ employee_id: string | null }>(
      `SELECT t.employee_id FROM biometric_template t LEFT JOIN employee e ON e.id = t.employee_id LEFT JOIN device d ON d.id = t.enrolled_on_device_id
        WHERE t.id = $1 AND (e.company_id = $2 OR d.company_id = $2)`, [id, req.user!.companyId]);
    if (!t.rows[0]) throw new NotFoundError('template not found');
    const n = await shredTemplates(db, 'id = $1', [id]);
    await audit(db, { actorId: req.user!.id, action: 'template_shred', table: 'biometric_template', rowId: id, after: { employeeId: t.rows[0].employee_id } });
    res.json({ ok: true, shredded: n });
  });

  r.post('/employees/:id/shred-all', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    if (!(await employeeInCompany(db, id, req.user!.companyId))) throw new NotFoundError('employee not found');
    const n = await shredTemplates(db, 'employee_id = $1', [id]);
    await db.query('UPDATE consent_record SET revoked_at = coalesce(revoked_at, now()) WHERE employee_id = $1', [id]);
    await audit(db, { actorId: req.user!.id, action: 'template_shred_all', table: 'biometric_template', rowId: id, after: { shredded: n } });
    res.json({ ok: true, shredded: n });
  });

  /** Data-subject access request export: the employee's own data, nothing else (plan §12.11 DSAR). */
  r.get('/dsar', async (req, res) => {
    const q = z.object({ employeeId: uuid }).parse(req.query);
    await assertSelfOrHr(db, req.user!, q.employeeId);
    const emp = await db.query(
      `SELECT id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, exit_date, designation, grade, phone, email, ot_eligible, weekly_off_days,
              status, regime, ssf_id, pf_id, cit_id, bank_code, bank_branch, bank_account_last4, is_resident, remote_area_class, disability, (pan_enc IS NOT NULL) AS has_pan
         FROM employee WHERE id = $1 AND company_id = $2`, [q.employeeId, req.user!.companyId]);
    if (!emp.rows[0]) throw new NotFoundError('employee not found');
    const [consents, templates, attendance, punches, leave, notifications, requests] = await Promise.all([
      db.query('SELECT id, purpose, modality, notice_version, granted_at, revoked_at FROM consent_record WHERE employee_id = $1 ORDER BY granted_at', [q.employeeId]),
      db.query('SELECT id, modality, vendor_format, device_user_pin, created_at, shredded_at FROM biometric_template WHERE employee_id = $1 ORDER BY created_at', [q.employeeId]),
      db.query('SELECT local_date, bs_date, status, first_in, last_out, worked_min, late_min, early_min, ot_min, flags FROM v_attendance_latest WHERE employee_id = $1 ORDER BY local_date', [q.employeeId]),
      db.query('SELECT punched_at, local_date, direction, verify_mode, source, device_sn FROM punch_event WHERE employee_id = $1 ORDER BY punched_at', [q.employeeId]),
      db.query('SELECT effective_date, delta_days, reason, note, leave_type_id FROM leave_ledger WHERE employee_id = $1 ORDER BY effective_date', [q.employeeId]),
      db.query('SELECT created_at, channel, template, title, body FROM notification_outbox WHERE employee_id = $1 ORDER BY created_at', [q.employeeId]),
      db.query(`SELECT 'leave' AS kind, from_date::text AS from_date, to_date::text AS to_date, status, reason, created_at FROM leave_request WHERE employee_id = $1
                UNION ALL SELECT 'regularisation', local_date::text, local_date::text, status, reason, created_at FROM regularisation_request WHERE employee_id = $1
                UNION ALL SELECT 'overtime', local_date::text, local_date::text, status, reason, created_at FROM ot_request WHERE employee_id = $1 ORDER BY created_at`, [q.employeeId]),
    ]);
    await audit(db, { actorId: req.user!.id, action: 'dsar_export', table: 'employee', rowId: q.employeeId });
    res.json({
      generatedAt: new Date().toISOString(), employee: emp.rows[0], consents: consents.rows, templates: templates.rows, attendance: attendance.rows,
      punches: punches.rows, leaveLedger: leave.rows, requests: requests.rows, notifications: notifications.rows, retention: RETENTION,
    });
  });

  /** Retention schedule (plan §12.11): shred templates 30 days after exit; attendance-photo hook for when photos exist. */
  r.post('/retention/run', requireRole('hr', 'admin'), async (req, res) => {
    const b = z.object({ templateGraceDays: z.number().int().min(0).max(365).default(30) }).parse(req.body ?? {});
    const exited = await db.query<{ id: string }>(
      `SELECT e.id FROM employee e WHERE e.company_id = $1 AND e.exit_date IS NOT NULL AND e.exit_date < current_date - $2::int
          AND EXISTS (SELECT 1 FROM biometric_template t WHERE t.employee_id = e.id AND t.shredded_at IS NULL)`, [req.user!.companyId, b.templateGraceDays]);
    let shredded = 0;
    for (const e of exited.rows) shredded += await shredTemplates(db, 'employee_id = $1', [e.id]);
    await audit(db, { actorId: req.user!.id, action: 'retention_run', table: 'biometric_template', after: { employees: exited.rows.length, shredded, photosDeleted: 0 } });
    res.json({ employees: exited.rows.length, templatesShredded: shredded, photosDeleted: 0 });
  });

  r.get('/stats', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT t.modality, count(*) FILTER (WHERE t.shredded_at IS NULL)::int AS active, count(*) FILTER (WHERE t.shredded_at IS NOT NULL)::int AS shredded,
              count(*) FILTER (WHERE t.employee_id IS NULL AND t.shredded_at IS NULL)::int AS unmapped
         FROM biometric_template t LEFT JOIN employee e ON e.id = t.employee_id LEFT JOIN device d ON d.id = t.enrolled_on_device_id
        WHERE e.company_id = $1 OR d.company_id = $1 GROUP BY t.modality ORDER BY t.modality`, [req.user!.companyId]);
    res.json({ byModality: rows.rows });
  });

  return r;
}
