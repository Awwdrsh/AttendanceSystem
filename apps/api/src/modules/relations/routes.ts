import { Router, type Request } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import { bsLabel } from '../../bsdate.js';
import type { AppDeps } from '../../deps.js';
import type { Db, Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { audienceSql, placementOf } from '../engage/service.js';
import { attachFile, readFile, sendStoredFile } from '../files/store.js';
import { recordExit } from '../lifecycle/service.js';
import { notify, userIdForEmployee, userIdsWithRole } from '../notifications/service.js';
import { logCareerEvent } from '../people/career.js';
import {
  caseAad, completeness, decText, deviceTokenHash, encText, ensureStages, GRIEVANCE_CATEGORIES, grievanceAad, grievanceHandlers, grievanceNoteAad, grievanceRole,
  isCommitteeMember, isHr, KYE_SECTIONS, logSuspension, milestones, newId, nextNumber, relationsPolicy, relationsPolicySchema, SECTION_LABEL, today,
  type GrievanceRow, type GrievanceRole,
} from './service.js';

async function tx<T>(db: Db, fn: (c: Queryable) => Promise<T>): Promise<T> {
  const c = await db.connect();
  try {
    await c.query('BEGIN');
    const out = await fn(c);
    await c.query('COMMIT');
    return out;
  } catch (e) { await c.query('ROLLBACK'); throw e; } finally { c.release(); }
}

const hr = requireRole('hr');
const text = (max: number) => z.string().trim().min(1).max(max);

async function employeeInCompany(db: Queryable, companyId: string, employeeId: string): Promise<{ id: string; name_en: string; emp_code: string; status: string }> {
  const r = await db.query<{ id: string; name_en: string; emp_code: string; status: string }>('SELECT id, name_en, emp_code, status FROM employee WHERE id = $1 AND company_id = $2', [employeeId, companyId]);
  if (!r.rows[0]) throw new NotFoundError('employee not found');
  return r.rows[0];
}

/**
 * Employee relations (Featurestudy: grievances, disciplinary action, milestones, acting and deputation, Know Your
 * Employee, login pop-ups, registered devices). Grievances are heard by a committee that HR names; they are never
 * routed to the line manager. Free text is encrypted, and every look at a case by someone other than the person it
 * is about is audited.
 */
export function relationsRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();

  // ============================================================ policy
  r.get('/policy', hr, async (req, res) => { res.json({ policy: await relationsPolicy(db, companyIdOf(req)) }); });
  r.put('/policy', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const p = relationsPolicySchema.parse(req.body);
    p.serviceYears = [...new Set(p.serviceYears)].sort((a, b) => a - b);
    await db.query(`INSERT INTO company_setting (company_id, key, value) VALUES ($1, 'relations.policy', $2) ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value`, [companyId, JSON.stringify(p)]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: null, after: { key: 'relations.policy', ...p } });
    res.json({ policy: p });
  });

  // ============================================================ grievances

  r.get('/grievances/committee', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const member = await isCommitteeMember(db, companyId, u.id);
    if (!isHr(u) && !member) return res.json({ members: [], isMember: false });
    const rows = await db.query(
      `SELECT m.user_id, u.name, u.email, m.added_at FROM grievance_committee_member m JOIN app_user u ON u.id = m.user_id WHERE m.company_id = $1 ORDER BY u.name`, [companyId]);
    res.json({ members: rows.rows, isMember: member });
  });

  r.put('/grievances/committee', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ userIds: z.array(uuid).max(20) }).parse(req.body);
    const ids = [...new Set(b.userIds)];
    const ok = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE company_id = $1 AND status = 'active' AND id = ANY($2::uuid[])`, [companyId, ids]);
    if (ok.rows.length !== ids.length) throw new BadRequestError('every member must be an active user in your company');
    await tx(db, async (c) => {
      await c.query('DELETE FROM grievance_committee_member WHERE company_id = $1 AND NOT (user_id = ANY($2::uuid[]))', [companyId, ids]);
      for (const id of ids) await c.query('INSERT INTO grievance_committee_member (company_id, user_id, added_by) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING', [companyId, id, req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'grievance_committee_member', after: { userIds: ids } });
    });
    res.json({ ok: true, members: ids.length });
  });

  r.post('/grievances', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    if (!u.employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const b = z.object({
      category: z.enum(GRIEVANCE_CATEGORIES), subject: text(160), description: text(8000), concerns: z.string().trim().max(500).nullish(),
      fileIds: z.array(uuid).max(5).default([]),
    }).parse(req.body);
    const out = await tx(db, async (c) => {
      const id = await newId(c);
      const number = await nextNumber(c, 'grievance', companyId);
      await c.query(
        `INSERT INTO grievance (id, company_id, number, employee_id, user_id, category, subject_enc, description_enc, concerns_enc)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
        [id, companyId, number, u.employeeId, u.id, b.category, encText(cipher, b.subject, grievanceAad(companyId, id, 'subject')),
          encText(cipher, b.description, grievanceAad(companyId, id, 'description')), b.concerns ? encText(cipher, b.concerns, grievanceAad(companyId, id, 'concerns')) : null]);
      for (const fileId of b.fileIds) {
        await attachFile(c, { companyId, fileId, userId: u.id, purpose: 'grievance_attachment', attachedTo: `grievance:${id}` });
        await c.query('INSERT INTO grievance_file (grievance_id, file_id) VALUES ($1, $2)', [id, fileId]);
      }
      await audit(c, { actorId: u.id, action: 'create', table: 'grievance', rowId: id, after: { number, category: b.category } });
      // The notice says only that a grievance arrived: no names, no subject.
      for (const userId of await grievanceHandlers(c, { company_id: companyId, user_id: u.id, excluded_user_ids: [] })) {
        await notify(c, { companyId, userId, template: 'relations.grievance.new', title: `New grievance G-${number}`, body: 'A confidential grievance was filed. Open Employee relations, Grievances to review it.', payload: { grievanceId: id } });
      }
      return { id, number };
    });
    res.status(201).json(out);
  });

  function grievanceView(g: GrievanceRow & { employee_name?: string; emp_code?: string }, role: GrievanceRole) {
    const d = (blob: Buffer | null, field: string) => decText(cipher, blob, grievanceAad(g.company_id, g.id, field));
    return {
      id: g.id, number: g.number, category: g.category, status: g.status, subject: d(g.subject_enc, 'subject'), description: d(g.description_enc, 'description'),
      concerns: d(g.concerns_enc, 'concerns'), employeeId: g.employee_id, employeeName: g.employee_name ?? null, empCode: g.emp_code ?? null,
      hearingAt: g.hearing_at, hearingPlace: g.hearing_place, outcome: g.outcome, findings: d(g.findings_enc, 'findings'), decidedAt: g.decided_at,
      appealReason: d(g.appeal_reason_enc, 'appeal_reason'), appealedAt: g.appealed_at, appealOutcome: g.appeal_outcome, appealNote: d(g.appeal_note_enc, 'appeal_note'),
      appealDecidedAt: g.appeal_decided_at, createdAt: g.created_at, updatedAt: g.updated_at, role,
      excludedUserIds: role === 'complainant' ? [] : g.excluded_user_ids,
    };
  }

  async function loadGrievance(req: Request, id: string, opts: { lock?: Queryable } = {}): Promise<{ g: GrievanceRow & { employee_name: string; emp_code: string }; role: GrievanceRole }> {
    const q = opts.lock ?? db;
    const r = await q.query<GrievanceRow & { employee_name: string; emp_code: string }>(
      `SELECT g.*, e.name_en AS employee_name, e.emp_code FROM grievance g JOIN employee e ON e.id = g.employee_id WHERE g.id = $1 AND g.company_id = $2 ${opts.lock ? 'FOR UPDATE OF g' : ''}`,
      [id, companyIdOf(req)]);
    const g = r.rows[0];
    const role = g ? await grievanceRole(q, req.user!, g) : null;
    if (!g || !role) throw new NotFoundError('grievance not found');
    return { g, role };
  }

  r.get('/grievances', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const q = z.object({ scope: z.enum(['mine', 'handle']).default('mine'), status: z.string().max(40).optional() }).parse(req.query);
    if (q.scope === 'mine') {
      const rows = await db.query<GrievanceRow & { employee_name: string; emp_code: string }>(
        `SELECT g.*, e.name_en AS employee_name, e.emp_code FROM grievance g JOIN employee e ON e.id = g.employee_id WHERE g.company_id = $1 AND g.user_id = $2 ORDER BY g.created_at DESC`, [companyId, u.id]);
      return res.json({ grievances: rows.rows.map((g) => grievanceView(g, 'complainant')) });
    }
    const member = await isCommitteeMember(db, companyId, u.id);
    if (!isHr(u) && !member) throw new ForbiddenError('only HR and the grievance committee handle grievances');
    const rows = await db.query<GrievanceRow & { employee_name: string; emp_code: string }>(
      `SELECT g.*, e.name_en AS employee_name, e.emp_code FROM grievance g JOIN employee e ON e.id = g.employee_id
        WHERE g.company_id = $1 AND g.user_id <> $2 AND NOT ($2 = ANY(g.excluded_user_ids)) AND ($3::text IS NULL OR g.status = $3)
        ORDER BY (g.status IN ('decided', 'closed', 'withdrawn')), g.created_at DESC LIMIT 500`, [companyId, u.id, q.status ?? null]);
    const role: GrievanceRole = isHr(u) ? 'hr' : 'committee';
    // Lists show the subject only; the full text opens (and is audited) per case.
    res.json({ grievances: rows.rows.map((g) => { const v = grievanceView(g, role); return { ...v, description: null, concerns: null, findings: null, appealReason: null, appealNote: null }; }) });
  });

  r.get('/grievances/:id', async (req, res) => {
    const { g, role } = await loadGrievance(req, uuid.parse(req.params.id));
    const notes = await db.query<{ id: string; author_id: string; author_name: string; shared: boolean; body_enc: Buffer; created_at: Date }>(
      `SELECT n.id, n.author_id, u.name AS author_name, n.shared, n.body_enc, n.created_at FROM grievance_note n JOIN app_user u ON u.id = n.author_id
        WHERE n.grievance_id = $1 ${role === 'complainant' ? 'AND n.shared' : ''} ORDER BY n.created_at`, [g.id]);
    const files = await db.query<{ id: string; filename: string; mime: string; bytes: number }>(
      `SELECT f.id, f.filename, f.mime, f.bytes FROM grievance_file gf JOIN stored_file f ON f.id = gf.file_id WHERE gf.grievance_id = $1 AND f.deleted_at IS NULL`, [g.id]);
    if (role !== 'complainant') await audit(db, { actorId: req.user!.id, action: 'view', table: 'grievance', rowId: g.id });
    const p = await relationsPolicy(db, g.company_id);
    const appealUntil = g.decided_at ? new Date(g.decided_at.getTime() + p.appealDays * 86_400_000) : null;
    res.json({
      grievance: grievanceView(g, role),
      notes: notes.rows.map((n) => ({ id: n.id, authorName: n.author_id === g.user_id ? 'You' : n.author_name, mine: n.author_id === req.user!.id, fromComplainant: n.author_id === g.user_id,
        shared: n.shared, body: decText(cipher, n.body_enc, grievanceNoteAad(g.id, n.id)), createdAt: n.created_at })),
      files: files.rows,
      canAppeal: role === 'complainant' && g.status === 'decided' && !g.appealed_at && !!appealUntil && appealUntil.getTime() > Date.now(),
      appealUntil,
    });
  });

  r.get('/grievances/:id/files/:fileId', async (req, res) => {
    const { g, role } = await loadGrievance(req, uuid.parse(req.params.id));
    const fileId = uuid.parse(req.params.fileId);
    const link = await db.query('SELECT 1 FROM grievance_file WHERE grievance_id = $1 AND file_id = $2', [g.id, fileId]);
    if (!link.rows.length) throw new NotFoundError('file not found');
    if (role !== 'complainant') await audit(db, { actorId: req.user!.id, action: 'view_file', table: 'grievance', rowId: g.id, after: { fileId } });
    sendStoredFile(res, await readFile(db, cipher, g.company_id, fileId), { download: req.query.download === '1' });
  });

  r.post('/grievances/:id/notes', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ body: text(4000), shared: z.boolean().default(false) }).parse(req.body);
    const out = await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (['closed', 'withdrawn'].includes(g.status)) throw new ConflictError(`this grievance is ${g.status}`);
      const shared = role === 'complainant' ? true : b.shared;
      const noteId = await newId(c);
      await c.query('INSERT INTO grievance_note (id, grievance_id, author_id, shared, body_enc) VALUES ($1, $2, $3, $4, $5)',
        [noteId, g.id, req.user!.id, shared, encText(cipher, b.body, grievanceNoteAad(g.id, noteId))]);
      await c.query('UPDATE grievance SET updated_at = now() WHERE id = $1', [g.id]);
      if (role === 'complainant') {
        for (const userId of await grievanceHandlers(c, g)) await notify(c, { companyId: g.company_id, userId, template: 'relations.grievance.note', title: `G-${g.number}: new message from the employee`, body: 'Open the grievance to read it.', payload: { grievanceId: g.id } });
      } else if (shared) {
        await notify(c, { companyId: g.company_id, userId: g.user_id, employeeId: g.employee_id, template: 'relations.grievance.note', title: `Your grievance G-${g.number}: new message`, body: 'The grievance committee sent you a message.', payload: { grievanceId: g.id } });
      }
      await audit(c, { actorId: req.user!.id, action: 'note', table: 'grievance', rowId: g.id, after: { shared } });
      return { id: noteId };
    });
    res.status(201).json(out);
  });

  /** Committee or HR: acknowledge, schedule a hearing, or exclude someone with a conflict of interest (HR only). */
  r.patch('/grievances/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({
      status: z.enum(['under_review', 'hearing_scheduled']).optional(), hearingAt: z.iso.datetime({ offset: true }).nullish(), hearingPlace: z.string().trim().max(200).nullish(),
      excludeUserIds: z.array(uuid).max(20).optional(),
    }).strict().parse(req.body);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role === 'complainant') throw new ForbiddenError('the committee handles this grievance');
      if (!['submitted', 'under_review', 'hearing_scheduled'].includes(g.status)) throw new ConflictError(`this grievance is ${g.status}`);
      if (b.excludeUserIds !== undefined && role !== 'hr') throw new ForbiddenError('only HR can exclude people from a case');
      if (b.excludeUserIds?.includes(req.user!.id)) throw new BadRequestError('you cannot exclude yourself; ask another HR person');
      let status = b.status ?? g.status;
      if (b.hearingAt) status = 'hearing_scheduled';
      if (status === 'hearing_scheduled' && !(b.hearingAt ?? g.hearing_at)) throw new BadRequestError('set the hearing date and time');
      await c.query(
        `UPDATE grievance SET status = $2, hearing_at = CASE WHEN $3::boolean THEN $4::timestamptz ELSE hearing_at END,
                hearing_place = CASE WHEN $5::boolean THEN $6 ELSE hearing_place END, excluded_user_ids = coalesce($7::uuid[], excluded_user_ids), updated_at = now()
          WHERE id = $1`,
        [g.id, status, b.hearingAt !== undefined, b.hearingAt ?? null, b.hearingPlace !== undefined, b.hearingPlace ?? null, b.excludeUserIds ?? null]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'grievance', rowId: g.id, before: { status: g.status }, after: { status, hearingAt: b.hearingAt, excluded: b.excludeUserIds } });
      if (b.hearingAt) {
        await notify(c, { companyId: g.company_id, userId: g.user_id, employeeId: g.employee_id, template: 'relations.grievance.hearing', title: `Hearing set for your grievance G-${g.number}`,
          body: `The committee will hear your grievance on ${bsLabel(b.hearingAt)}${b.hearingPlace ? ` at ${b.hearingPlace}` : ''}.`, payload: { grievanceId: g.id }, channels: ['push'] });
      } else if (status === 'under_review' && g.status === 'submitted') {
        await notify(c, { companyId: g.company_id, userId: g.user_id, employeeId: g.employee_id, template: 'relations.grievance.review', title: `Your grievance G-${g.number} is under review`, body: 'The grievance committee has started reviewing it.', payload: { grievanceId: g.id } });
      }
    });
    res.json({ ok: true });
  });

  r.post('/grievances/:id/decision', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ outcome: z.enum(['upheld', 'partly_upheld', 'not_upheld']), findings: text(8000) }).parse(req.body);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role === 'complainant') throw new ForbiddenError('the committee decides this grievance');
      if (!['submitted', 'under_review', 'hearing_scheduled'].includes(g.status)) throw new ConflictError(`this grievance is ${g.status}`);
      await c.query(`UPDATE grievance SET status = 'decided', outcome = $2, findings_enc = $3, decided_by = $4, decided_at = now(), updated_at = now() WHERE id = $1`,
        [g.id, b.outcome, encText(cipher, b.findings, grievanceAad(g.company_id, g.id, 'findings')), req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'decide', table: 'grievance', rowId: g.id, after: { outcome: b.outcome } });
      const p = await relationsPolicy(c, g.company_id);
      await notify(c, { companyId: g.company_id, userId: g.user_id, employeeId: g.employee_id, template: 'relations.grievance.decided', title: `Decision on your grievance G-${g.number}`,
        body: `The committee has decided. Read the findings; you can appeal within ${p.appealDays} days.`, payload: { grievanceId: g.id }, channels: ['push'] });
    });
    res.json({ ok: true });
  });

  r.post('/grievances/:id/appeal', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ reason: text(4000) }).parse(req.body);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role !== 'complainant') throw new ForbiddenError('only the person who filed the grievance can appeal');
      if (g.status !== 'decided') throw new ConflictError('you can appeal once a decision has been made');
      if (g.appealed_at) throw new ConflictError('this grievance has already been appealed');
      const p = await relationsPolicy(c, g.company_id);
      if (Date.now() - g.decided_at!.getTime() > p.appealDays * 86_400_000) throw new ConflictError(`the ${p.appealDays}-day appeal window has passed`);
      await c.query(`UPDATE grievance SET status = 'appealed', appeal_reason_enc = $2, appealed_at = now(), updated_at = now() WHERE id = $1`,
        [g.id, encText(cipher, b.reason, grievanceAad(g.company_id, g.id, 'appeal_reason'))]);
      await audit(c, { actorId: req.user!.id, action: 'appeal', table: 'grievance', rowId: g.id });
      // Appeals are heard by HR, not by the committee that decided.
      for (const userId of (await userIdsWithRole(c, g.company_id, 'hr')).filter((x) => x !== g.user_id && !g.excluded_user_ids.includes(x))) {
        await notify(c, { companyId: g.company_id, userId, template: 'relations.grievance.appeal', title: `Grievance G-${g.number} appealed`, body: 'The employee appealed the decision. HR hears appeals.', payload: { grievanceId: g.id } });
      }
    });
    res.json({ ok: true });
  });

  r.post('/grievances/:id/appeal-decision', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ outcome: z.enum(['decision_upheld', 'decision_changed']), note: text(4000), newOutcome: z.enum(['upheld', 'partly_upheld', 'not_upheld']).optional() }).parse(req.body);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role !== 'hr') throw new ForbiddenError('HR hears appeals');
      if (g.status !== 'appealed') throw new ConflictError('this grievance has no open appeal');
      if (g.decided_by === req.user!.id) throw new ForbiddenError('someone other than the person who decided must hear the appeal');
      if (b.outcome === 'decision_changed' && !b.newOutcome) throw new BadRequestError('choose the new outcome');
      await c.query(
        `UPDATE grievance SET status = 'closed', appeal_outcome = $2, appeal_note_enc = $3, appeal_decided_by = $4, appeal_decided_at = now(),
                outcome = coalesce($5, outcome), updated_at = now() WHERE id = $1`,
        [g.id, b.outcome, encText(cipher, b.note, grievanceAad(g.company_id, g.id, 'appeal_note')), req.user!.id, b.outcome === 'decision_changed' ? b.newOutcome : null]);
      await audit(c, { actorId: req.user!.id, action: 'appeal_decide', table: 'grievance', rowId: g.id, after: { outcome: b.outcome, newOutcome: b.newOutcome } });
      await notify(c, { companyId: g.company_id, userId: g.user_id, employeeId: g.employee_id, template: 'relations.grievance.appeal_decided', title: `Appeal decided: G-${g.number}`,
        body: b.outcome === 'decision_upheld' ? 'The original decision stands. Read the note for the reasons.' : 'The decision was changed on appeal. Read the note for details.', payload: { grievanceId: g.id }, channels: ['push'] });
    });
    res.json({ ok: true });
  });

  r.post('/grievances/:id/withdraw', async (req, res) => {
    const id = uuid.parse(req.params.id);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role !== 'complainant') throw new ForbiddenError('only the person who filed it can withdraw a grievance');
      if (!['submitted', 'under_review', 'hearing_scheduled'].includes(g.status)) throw new ConflictError(`this grievance is ${g.status}`);
      await c.query(`UPDATE grievance SET status = 'withdrawn', updated_at = now() WHERE id = $1`, [g.id]);
      await audit(c, { actorId: req.user!.id, action: 'withdraw', table: 'grievance', rowId: g.id });
      for (const userId of await grievanceHandlers(c, g)) await notify(c, { companyId: g.company_id, userId, template: 'relations.grievance.withdrawn', title: `Grievance G-${g.number} withdrawn`, body: 'The employee withdrew the grievance.', payload: { grievanceId: g.id } });
    });
    res.json({ ok: true });
  });

  r.post('/grievances/:id/close', async (req, res) => {
    const id = uuid.parse(req.params.id);
    await tx(db, async (c) => {
      const { g, role } = await loadGrievance(req, id, { lock: c });
      if (role === 'committee') throw new ForbiddenError('HR or the employee closes a decided grievance');
      if (g.status !== 'decided') throw new ConflictError('only a decided grievance can be closed');
      await c.query(`UPDATE grievance SET status = 'closed', updated_at = now() WHERE id = $1`, [g.id]);
      await audit(c, { actorId: req.user!.id, action: 'close', table: 'grievance', rowId: g.id });
    });
    res.json({ ok: true });
  });

  // ============================================================ disciplinary action

  r.get('/disciplinary/stages', async (req, res) => {
    const companyId = companyIdOf(req);
    await ensureStages(db, companyId);
    const rows = await db.query('SELECT id, code, name, severity, effect, letter_template_code, sort, active FROM disciplinary_stage WHERE company_id = $1 ORDER BY sort, severity', [companyId]);
    res.json({ stages: rows.rows });
  });

  r.put('/disciplinary/stages', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({ stages: z.array(z.object({
      id: uuid.optional(), code: z.string().regex(/^[a-z0-9_]{2,40}$/), name: text(80), severity: z.number().int().min(1).max(9),
      effect: z.enum(['none', 'suspension', 'termination']), letterTemplateCode: z.string().trim().max(40).nullish(), active: z.boolean().default(true),
    })).min(1).max(20) }).parse(req.body);
    await ensureStages(db, companyId);
    await tx(db, async (c) => {
      for (const [i, s] of b.stages.entries()) {
        if (s.id) {
          const u = await c.query(`UPDATE disciplinary_stage SET code = $3, name = $4, severity = $5, effect = $6, letter_template_code = $7, sort = $8, active = $9 WHERE id = $1 AND company_id = $2`,
            [s.id, companyId, s.code, s.name, s.severity, s.effect, s.letterTemplateCode ?? null, i, s.active]);
          if (!u.rowCount) throw new NotFoundError('stage not found');
        } else {
          await c.query(`INSERT INTO disciplinary_stage (company_id, code, name, severity, effect, letter_template_code, sort, active) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
            [companyId, s.code, s.name, s.severity, s.effect, s.letterTemplateCode ?? null, i, s.active]);
        }
      }
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'disciplinary_stage', after: { count: b.stages.length } });
    });
    res.json({ ok: true });
  });

  interface CaseRow {
    id: string; company_id: string; number: number; employee_id: string; category: string; incident_date: string; incident_enc: Buffer; status: string;
    show_cause_enc: Buffer | null; show_cause_at: Date | null; response_due: string | null; response_enc: Buffer | null; responded_at: Date | null; stage_id: string | null;
    decision_note_enc: Buffer | null; suspension_from: string | null; suspension_to: string | null; exit_id: string | null; letter_id: string | null; opened_by: string | null;
    decided_by: string | null; decided_at: Date | null; created_at: Date; updated_at: Date; employee_name: string; emp_code: string; stage_name: string | null;
    stage_effect: string | null; stage_letter: string | null; letter_ref: string | null;
  }
  const CASE_SELECT = `SELECT k.*, k.incident_date::text AS incident_date, k.response_due::text AS response_due, k.suspension_from::text AS suspension_from,
      k.suspension_to::text AS suspension_to, e.name_en AS employee_name, e.emp_code, s.name AS stage_name, s.effect AS stage_effect,
      s.letter_template_code AS stage_letter, l.ref_no AS letter_ref
    FROM disciplinary_case k JOIN employee e ON e.id = k.employee_id LEFT JOIN disciplinary_stage s ON s.id = k.stage_id LEFT JOIN issued_letter l ON l.id = k.letter_id`;

  function caseView(k: CaseRow, forEmployee: boolean) {
    const d = (blob: Buffer | null, field: string) => decText(cipher, blob, caseAad(k.company_id, k.id, field));
    return {
      id: k.id, number: k.number, employeeId: k.employee_id, employeeName: k.employee_name, empCode: k.emp_code, category: k.category, incidentDate: k.incident_date,
      incident: d(k.incident_enc, 'incident'), status: k.status, showCause: d(k.show_cause_enc, 'show_cause'), showCauseAt: k.show_cause_at, responseDue: k.response_due,
      response: d(k.response_enc, 'response'), respondedAt: k.responded_at, stageId: k.stage_id, stageName: k.stage_name, stageEffect: k.stage_effect,
      stageLetter: forEmployee ? null : k.stage_letter, decisionNote: d(k.decision_note_enc, 'decision'), suspensionFrom: k.suspension_from, suspensionTo: k.suspension_to,
      letterId: k.letter_id, letterRef: k.letter_ref, exitId: forEmployee ? null : k.exit_id, decidedAt: k.decided_at, createdAt: k.created_at, updatedAt: k.updated_at,
    };
  }

  async function loadCase(c: Queryable, req: Request, id: string, lock = false): Promise<CaseRow> {
    const r = await c.query<CaseRow>(`${CASE_SELECT} WHERE k.id = $1 AND k.company_id = $2 ${lock ? 'FOR UPDATE OF k' : ''}`, [id, companyIdOf(req)]);
    if (!r.rows[0]) throw new NotFoundError('case not found');
    return r.rows[0];
  }

  r.get('/disciplinary', hr, async (req, res) => {
    const q = z.object({ employeeId: uuid.optional(), status: z.string().max(20).optional() }).parse(req.query);
    const rows = await db.query<CaseRow>(`${CASE_SELECT} WHERE k.company_id = $1 AND ($2::uuid IS NULL OR k.employee_id = $2) AND ($3::text IS NULL OR k.status = $3)
      ORDER BY (k.status IN ('decided', 'withdrawn')), k.created_at DESC LIMIT 500`, [companyIdOf(req), q.employeeId ?? null, q.status ?? null]);
    res.json({ cases: rows.rows.map((k) => caseView(k, false)) });
  });

  /** The employee's own cases, once HR has asked them to explain or has decided; investigations in progress stay with HR. */
  r.get('/disciplinary/mine', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) return res.json({ cases: [] });
    const rows = await db.query<CaseRow>(`${CASE_SELECT} WHERE k.company_id = $1 AND k.employee_id = $2 AND k.status IN ('show_cause', 'responded', 'decided') ORDER BY k.created_at DESC`,
      [companyIdOf(req), u.employeeId]);
    res.json({ cases: rows.rows.map((k) => caseView(k, true)) });
  });

  r.get('/disciplinary/:id', async (req, res) => {
    const k = await loadCase(db, req, uuid.parse(req.params.id));
    const u = req.user!;
    const own = u.employeeId === k.employee_id;
    if (!isHr(u) && !(own && ['show_cause', 'responded', 'decided'].includes(k.status))) throw new NotFoundError('case not found');
    if (!own) await audit(db, { actorId: u.id, action: 'view', table: 'disciplinary_case', rowId: k.id });
    res.json({ case: caseView(k, own && !isHr(u)) });
  });

  r.post('/disciplinary', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      employeeId: uuid, category: z.enum(['attendance', 'conduct', 'performance', 'policy', 'safety', 'misconduct', 'other']), incidentDate: isoDate, incident: text(8000),
    }).parse(req.body);
    const emp = await employeeInCompany(db, companyId, b.employeeId);
    if (emp.status !== 'active') throw new ConflictError('this employee has left');
    if (b.incidentDate > today()) throw new BadRequestError('the incident date is in the future');
    if (req.user!.employeeId === b.employeeId) throw new ForbiddenError('you cannot open a case about yourself');
    const out = await tx(db, async (c) => {
      const id = await newId(c);
      const number = await nextNumber(c, 'disciplinary_case', companyId);
      await c.query(`INSERT INTO disciplinary_case (id, company_id, number, employee_id, category, incident_date, incident_enc, opened_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
        [id, companyId, number, b.employeeId, b.category, b.incidentDate, encText(cipher, b.incident, caseAad(companyId, id, 'incident')), req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'disciplinary_case', rowId: id, after: { employeeId: b.employeeId, category: b.category, number } });
      return { id, number };
    });
    res.status(201).json(out);
  });

  r.post('/disciplinary/:id/show-cause', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ text: text(8000), responseDue: isoDate }).parse(req.body);
    if (b.responseDue < today()) throw new BadRequestError('the response deadline has already passed');
    await tx(db, async (c) => {
      const k = await loadCase(c, req, id, true);
      if (k.status !== 'open') throw new ConflictError(k.status === 'show_cause' ? 'a show-cause notice was already sent' : `this case is ${k.status}`);
      await c.query(`UPDATE disciplinary_case SET status = 'show_cause', show_cause_enc = $2, show_cause_at = now(), response_due = $3, updated_at = now() WHERE id = $1`,
        [k.id, encText(cipher, b.text, caseAad(k.company_id, k.id, 'show_cause')), b.responseDue]);
      await audit(c, { actorId: req.user!.id, action: 'show_cause', table: 'disciplinary_case', rowId: k.id, after: { responseDue: b.responseDue } });
      const userId = await userIdForEmployee(c, k.employee_id);
      if (userId) await notify(c, { companyId: k.company_id, userId, employeeId: k.employee_id, template: 'relations.disciplinary.show_cause', title: 'Please respond to a notice from HR',
        body: `HR has asked you to explain an incident. Respond by ${bsLabel(b.responseDue)} under Profile, Conduct.`, payload: { caseId: k.id }, channels: ['push'] });
    });
    res.json({ ok: true });
  });

  r.post('/disciplinary/:id/respond', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ response: text(8000) }).parse(req.body);
    await tx(db, async (c) => {
      const k = await loadCase(c, req, id, true);
      if (req.user!.employeeId !== k.employee_id) throw new NotFoundError('case not found');
      if (k.status !== 'show_cause') throw new ConflictError(k.status === 'responded' ? 'you have already responded' : 'this case is not waiting for your response');
      await c.query(`UPDATE disciplinary_case SET status = 'responded', response_enc = $2, responded_at = now(), updated_at = now() WHERE id = $1`,
        [k.id, encText(cipher, b.response, caseAad(k.company_id, k.id, 'response'))]);
      await audit(c, { actorId: req.user!.id, action: 'respond', table: 'disciplinary_case', rowId: k.id, after: { late: !!k.response_due && k.response_due < today() } });
      for (const userId of await userIdsWithRole(c, k.company_id, 'hr')) {
        if (userId === req.user!.id) continue;
        await notify(c, { companyId: k.company_id, userId, template: 'relations.disciplinary.responded', title: `Case D-${k.number}: response received`, body: `${k.employee_name} responded to the show-cause notice.`, payload: { caseId: k.id } });
      }
    });
    res.json({ ok: true });
  });

  r.post('/disciplinary/:id/decide', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({
      stageId: uuid.nullable(), note: text(8000), suspensionFrom: isoDate.optional(), suspensionTo: isoDate.optional(), lastWorkingDay: isoDate.optional(),
      /** Decide before the response deadline (e.g. the employee admitted the incident in a hearing). */
      force: z.boolean().default(false),
    }).parse(req.body);
    await tx(db, async (c) => {
      const k = await loadCase(c, req, id, true);
      if (!['open', 'show_cause', 'responded'].includes(k.status)) throw new ConflictError(`this case is ${k.status}`);
      if (req.user!.employeeId === k.employee_id) throw new ForbiddenError('you cannot decide a case about yourself');
      if (k.status === 'show_cause' && k.response_due && k.response_due >= today() && !b.force) {
        throw new ConflictError(`the employee has until ${bsLabel(k.response_due)} to respond; decide after that, or confirm you want to decide now`);
      }
      let stage: { id: string; name: string; effect: string } | null = null;
      if (b.stageId) {
        const s = await c.query<{ id: string; name: string; effect: string }>('SELECT id, name, effect FROM disciplinary_stage WHERE id = $1 AND company_id = $2 AND active', [b.stageId, k.company_id]);
        stage = s.rows[0] ?? null;
        if (!stage) throw new BadRequestError('choose an active stage');
      }
      let exitId: string | null = null;
      if (stage?.effect === 'suspension') {
        if (!b.suspensionFrom || !b.suspensionTo) throw new BadRequestError('enter the suspension dates');
        if (b.suspensionTo < b.suspensionFrom) throw new BadRequestError('the suspension ends before it starts');
        await logSuspension(c, { companyId: k.company_id, employeeId: k.employee_id, from: b.suspensionFrom, to: b.suspensionTo, stageName: stage.name, actorId: req.user!.id });
      }
      if (stage?.effect === 'termination') {
        if (!b.lastWorkingDay) throw new BadRequestError('enter the last working day');
        exitId = await recordExit(c, { companyId: k.company_id, employeeId: k.employee_id, kind: 'termination', voluntary: false, reasonCategory: 'disciplinary',
          reason: `Disciplinary case D-${k.number}`, lastWorkingDay: b.lastWorkingDay, actorId: req.user!.id });
      }
      await c.query(
        `UPDATE disciplinary_case SET status = 'decided', stage_id = $2, decision_note_enc = $3, suspension_from = $4, suspension_to = $5, exit_id = $6,
                decided_by = $7, decided_at = now(), updated_at = now() WHERE id = $1`,
        [k.id, stage?.id ?? null, encText(cipher, b.note, caseAad(k.company_id, k.id, 'decision')), stage?.effect === 'suspension' ? b.suspensionFrom : null,
          stage?.effect === 'suspension' ? b.suspensionTo : null, exitId, req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'decide', table: 'disciplinary_case', rowId: k.id, after: { stage: stage?.name ?? 'no action', exitId } });
      const userId = await userIdForEmployee(c, k.employee_id);
      if (userId) await notify(c, { companyId: k.company_id, userId, employeeId: k.employee_id, template: 'relations.disciplinary.decided', title: 'Outcome of a disciplinary case',
        body: stage ? `HR decided: ${stage.name}. See Profile, Conduct for details.` : 'HR closed the case with no action. See Profile, Conduct.', payload: { caseId: k.id }, channels: ['push'] });
    });
    res.json({ ok: true });
  });

  /** Link a letter issued under Documents (e.g. a warning letter) to the case. */
  r.post('/disciplinary/:id/letter', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ letterId: uuid }).parse(req.body);
    await tx(db, async (c) => {
      const k = await loadCase(c, req, id, true);
      const l = await c.query<{ employee_id: string }>('SELECT employee_id FROM issued_letter WHERE id = $1 AND company_id = $2', [b.letterId, k.company_id]);
      if (!l.rows[0] || l.rows[0].employee_id !== k.employee_id) throw new BadRequestError('that letter was not issued to this employee');
      await c.query('UPDATE disciplinary_case SET letter_id = $2, updated_at = now() WHERE id = $1', [k.id, b.letterId]);
      await audit(c, { actorId: req.user!.id, action: 'link_letter', table: 'disciplinary_case', rowId: k.id, after: { letterId: b.letterId } });
    });
    res.json({ ok: true });
  });

  r.post('/disciplinary/:id/withdraw', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ reason: text(1000) }).parse(req.body);
    await tx(db, async (c) => {
      const k = await loadCase(c, req, id, true);
      if (!['open', 'show_cause', 'responded'].includes(k.status)) throw new ConflictError(`this case is ${k.status}`);
      await c.query(`UPDATE disciplinary_case SET status = 'withdrawn', decision_note_enc = $2, decided_by = $3, decided_at = now(), updated_at = now() WHERE id = $1`,
        [k.id, encText(cipher, b.reason, caseAad(k.company_id, k.id, 'decision')), req.user!.id]);
      await audit(c, { actorId: req.user!.id, action: 'withdraw', table: 'disciplinary_case', rowId: k.id });
      if (k.status !== 'open') {
        const userId = await userIdForEmployee(c, k.employee_id);
        if (userId) await notify(c, { companyId: k.company_id, userId, employeeId: k.employee_id, template: 'relations.disciplinary.withdrawn', title: 'A disciplinary case was withdrawn', body: 'HR withdrew the case. No action will be taken.', payload: { caseId: k.id } });
      }
    });
    res.json({ ok: true });
  });

  // ============================================================ milestones

  r.get('/milestones', hr, async (req, res) => {
    const q = z.object({ days: z.coerce.number().int().min(1).max(366).default(60) }).parse(req.query);
    const companyId = companyIdOf(req);
    res.json({ milestones: await milestones(db, companyId, today(), q.days), policy: await relationsPolicy(db, companyId) });
  });

  /** Employees with an exit date ahead, plus contract ends and retirements due in the next 180 days. */
  r.get('/separating', hr, async (req, res) => {
    const list = await milestones(db, companyIdOf(req), today(), 180);
    res.json({ people: list.filter((m) => m.kind === 'exit' || m.kind === 'contract_end' || m.kind === 'retirement') });
  });

  r.get('/employees/:id/contract', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const r0 = await db.query<{ contract_end: string | null; dob: string | null }>('SELECT contract_end::text, dob::text FROM employee WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!r0.rows[0]) throw new NotFoundError('employee not found');
    const p = await relationsPolicy(db, companyId);
    const dob = r0.rows[0].dob;
    const retirement = dob ? `${Number(dob.slice(0, 4)) + p.retirementAge}${dob.slice(4)}` : null;
    res.json({ contractEnd: r0.rows[0].contract_end, retirementDate: retirement, retirementAge: p.retirementAge });
  });

  r.put('/employees/:id/contract', hr, async (req, res) => {
    const companyId = companyIdOf(req); const id = uuid.parse(req.params.id);
    const b = z.object({ contractEnd: isoDate.nullable() }).parse(req.body);
    const before = await db.query<{ contract_end: string | null; join_date: string | null }>('SELECT contract_end::text, join_date::text FROM employee WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!before.rows[0]) throw new NotFoundError('employee not found');
    if (b.contractEnd && before.rows[0].join_date && b.contractEnd < before.rows[0].join_date) throw new BadRequestError('the contract cannot end before the join date');
    await db.query('UPDATE employee SET contract_end = $2, updated_at = now() WHERE id = $1', [id, b.contractEnd]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'employee', rowId: id, before: { contractEnd: before.rows[0].contract_end }, after: { contractEnd: b.contractEnd } });
    res.json({ ok: true });
  });

  // ============================================================ acting and deputation

  const ASSIGNMENT_SELECT = `SELECT t.id, t.employee_id, e.name_en AS employee_name, e.emp_code, t.kind, t.designation, t.designation_id, t.branch_id, b.name_en AS branch_name,
      t.org_unit_id, o.name_en AS unit_name, t.start_date::text AS start_date, t.end_date::text AS end_date, t.note, t.status, t.created_at
    FROM employee_temp_assignment t JOIN employee e ON e.id = t.employee_id LEFT JOIN branch b ON b.id = t.branch_id LEFT JOIN org_unit o ON o.id = t.org_unit_id`;

  r.get('/assignments', async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const q = z.object({ employeeId: uuid.optional(), status: z.enum(['active', 'ended', 'cancelled', 'all']).default('active') }).parse(req.query);
    // HR sees everyone; anyone else only their own (managers see their team's on the directory card).
    if (!isHr(u) && q.employeeId !== u.employeeId) throw new ForbiddenError('HR manages acting and deputation assignments');
    const rows = await db.query(`${ASSIGNMENT_SELECT} WHERE t.company_id = $1 AND ($2::uuid IS NULL OR t.employee_id = $2) AND ($3 = 'all' OR t.status = $3) ORDER BY t.start_date DESC LIMIT 500`,
      [companyId, q.employeeId ?? null, q.status]);
    res.json({ assignments: rows.rows });
  });

  r.post('/assignments', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      employeeId: uuid, kind: z.enum(['acting', 'deputation']), designationId: uuid.nullish(), designation: z.string().trim().max(120).nullish(),
      branchId: uuid.nullish(), orgUnitId: uuid.nullish(), startDate: isoDate, endDate: isoDate, note: z.string().trim().max(500).nullish(),
    }).parse(req.body);
    if (b.endDate < b.startDate) throw new BadRequestError('the end date is before the start date');
    const emp = await employeeInCompany(db, companyId, b.employeeId);
    if (emp.status !== 'active') throw new ConflictError('this employee has left');
    let designation = b.designation ?? null;
    if (b.designationId) {
      const d = await db.query<{ name_en: string }>('SELECT name_en FROM designation WHERE id = $1 AND company_id = $2', [b.designationId, companyId]);
      if (!d.rows[0]) throw new BadRequestError('designation does not exist in your company');
      designation = d.rows[0].name_en;
    }
    for (const [table, refId, label] of [['branch', b.branchId, 'branch'], ['org_unit', b.orgUnitId, 'department']] as const) {
      if (refId && !(await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [refId, companyId])).rows.length) throw new BadRequestError(`${label} does not exist in your company`);
    }
    if (b.kind === 'acting' && !designation) throw new BadRequestError('choose the position they are acting in');
    if (b.kind === 'deputation' && !b.branchId && !b.orgUnitId) throw new BadRequestError('choose where they are deputed to');
    const out = await tx(db, async (c) => {
      const overlap = await c.query(`SELECT 1 FROM employee_temp_assignment WHERE employee_id = $1 AND kind = $2 AND status = 'active' AND daterange(start_date, end_date, '[]') && daterange($3::date, $4::date, '[]')`,
        [b.employeeId, b.kind, b.startDate, b.endDate]);
      if (overlap.rows.length) throw new ConflictError(`an ${b.kind === 'acting' ? 'acting assignment' : 'deputation'} already covers part of these dates`);
      const ins = await c.query<{ id: string }>(
        `INSERT INTO employee_temp_assignment (company_id, employee_id, kind, designation, designation_id, branch_id, org_unit_id, start_date, end_date, note, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) RETURNING id`,
        [companyId, b.employeeId, b.kind, designation, b.designationId ?? null, b.branchId ?? null, b.orgUnitId ?? null, b.startDate, b.endDate, b.note ?? null, req.user!.id]);
      await logCareerEvent(c, { companyId, employeeId: b.employeeId, kind: b.kind, effectiveDate: b.startDate,
        details: { endDate: b.endDate, ...(designation ? { designation } : {}), ...(b.branchId ? { branchId: b.branchId } : {}), ...(b.orgUnitId ? { orgUnitId: b.orgUnitId } : {}) },
        note: b.note ?? null, actorId: req.user!.id });
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'employee_temp_assignment', rowId: ins.rows[0]!.id, after: { kind: b.kind, employeeId: b.employeeId, startDate: b.startDate, endDate: b.endDate } });
      const userId = await userIdForEmployee(c, b.employeeId);
      if (userId) await notify(c, { companyId, userId, employeeId: b.employeeId, template: 'relations.assignment.new', title: b.kind === 'acting' ? `Acting as ${designation}` : 'Deputation',
        body: `From ${bsLabel(b.startDate)} to ${bsLabel(b.endDate)}.`, payload: { assignmentId: ins.rows[0]!.id } });
      return { id: ins.rows[0]!.id };
    });
    res.status(201).json(out);
  });

  r.post('/assignments/:id/end', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ endDate: isoDate.optional(), cancel: z.boolean().default(false) }).parse(req.body);
    const companyId = companyIdOf(req);
    const t = await db.query<{ status: string; start_date: string; end_date: string }>(
      'SELECT status, start_date::text, end_date::text FROM employee_temp_assignment WHERE id = $1 AND company_id = $2', [id, companyId]);
    const row = t.rows[0];
    if (!row) throw new NotFoundError('assignment not found');
    if (row.status !== 'active') throw new ConflictError(`this assignment is ${row.status}`);
    if (b.cancel) {
      await db.query(`UPDATE employee_temp_assignment SET status = 'cancelled', ended_at = now() WHERE id = $1`, [id]);
    } else {
      const end = b.endDate ?? today();
      if (end < row.start_date) throw new BadRequestError('the end date is before the start date; cancel it instead');
      await db.query(`UPDATE employee_temp_assignment SET end_date = $2, status = CASE WHEN $2::date < $3::date THEN 'ended' ELSE status END,
                      ended_at = CASE WHEN $2::date < $3::date THEN now() END WHERE id = $1`, [id, end, today()]);
    }
    await audit(db, { actorId: req.user!.id, action: b.cancel ? 'cancel' : 'end', table: 'employee_temp_assignment', rowId: id, after: { endDate: b.endDate } });
    res.json({ ok: true });
  });

  // ============================================================ Know Your Employee

  async function audienceEmployees(c: Queryable, companyId: string, kind: string, audienceId: string | null): Promise<string[]> {
    const r0 = await c.query<{ id: string }>(
      `WITH RECURSIVE units AS (SELECT id FROM org_unit WHERE id = $3 UNION SELECT o.id FROM org_unit o JOIN units u ON o.parent_id = u.id)
       SELECT e.id FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> current_date
        WHERE e.company_id = $1 AND e.status = 'active'
          AND ($2 = 'all' OR ($2 = 'branch' AND a.branch_id = $3) OR ($2 = 'department' AND a.org_unit_id IN (SELECT id FROM units)))`, [companyId, kind, audienceId]);
    return r0.rows.map((x) => x.id);
  }

  r.get('/kye/campaigns', hr, async (req, res) => {
    const rows = await db.query(
      `SELECT c.id, c.title, c.message, c.sections, c.audience_kind, c.audience_id, c.due_date::text AS due_date, c.status, c.created_at, c.closed_at,
              count(r.*)::int AS total, count(r.*) FILTER (WHERE r.status = 'confirmed')::int AS confirmed,
              count(r.*) FILTER (WHERE r.status = 'changes_requested')::int AS changes_requested
         FROM kye_campaign c LEFT JOIN kye_response r ON r.campaign_id = c.id WHERE c.company_id = $1 GROUP BY c.id ORDER BY c.created_at DESC`, [companyIdOf(req)]);
    res.json({ campaigns: rows.rows });
  });

  r.post('/kye/campaigns', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      title: text(160), message: z.string().trim().max(2000).nullish(), sections: z.array(z.enum(KYE_SECTIONS)).min(1),
      audienceKind: z.enum(['all', 'branch', 'department']).default('all'), audienceId: uuid.nullish(), dueDate: isoDate,
    }).parse(req.body);
    if (b.dueDate < today()) throw new BadRequestError('the due date has already passed');
    if (b.audienceKind !== 'all') {
      const table = b.audienceKind === 'branch' ? 'branch' : 'org_unit';
      if (!b.audienceId || !(await db.query(`SELECT 1 FROM ${table} WHERE id = $1 AND company_id = $2`, [b.audienceId, companyId])).rows.length) throw new BadRequestError(`choose the ${b.audienceKind}`);
    }
    const out = await tx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO kye_campaign (company_id, title, message, sections, audience_kind, audience_id, due_date, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id`,
        [companyId, b.title, b.message ?? null, [...new Set(b.sections)], b.audienceKind, b.audienceKind === 'all' ? null : b.audienceId, b.dueDate, req.user!.id]);
      const id = ins.rows[0]!.id;
      const emps = await audienceEmployees(c, companyId, b.audienceKind, b.audienceKind === 'all' ? null : b.audienceId ?? null);
      for (const e of emps) {
        await c.query('INSERT INTO kye_response (campaign_id, employee_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [id, e]);
        const u = await userIdForEmployee(c, e);
        if (u) await notify(c, { companyId, userId: u, employeeId: e, template: 'relations.kye.new', title: `Please review your profile: ${b.title}`, body: `Check your details and confirm by ${bsLabel(b.dueDate)}.`, payload: { campaignId: id } });
      }
      await audit(c, { actorId: req.user!.id, action: 'create', table: 'kye_campaign', rowId: id, after: { title: b.title, employees: emps.length } });
      return { id, employees: emps.length };
    });
    res.status(201).json(out);
  });

  r.get('/kye/campaigns/:id', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const camp = await db.query(`SELECT id, title, message, sections, audience_kind, due_date::text AS due_date, status FROM kye_campaign WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (!camp.rows[0]) throw new NotFoundError('campaign not found');
    const resp = await db.query<{ employee_id: string; name_en: string; emp_code: string; status: string; note: string | null; responded_at: Date | null; last_nudged_at: Date | null }>(
      `SELECT r.employee_id, e.name_en, e.emp_code, r.status, r.note, r.responded_at, r.last_nudged_at FROM kye_response r JOIN employee e ON e.id = r.employee_id
        WHERE r.campaign_id = $1 ORDER BY (r.status = 'pending') DESC, e.name_en`, [id]);
    const comp = new Map((await completeness(db, companyId)).map((x) => [x.employeeId, x]));
    res.json({ campaign: camp.rows[0], responses: resp.rows.map((x) => ({ ...x, completeness: comp.get(x.employee_id)?.percent ?? null, missing: comp.get(x.employee_id)?.missing ?? [] })) });
  });

  r.post('/kye/campaigns/:id/nudge', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const companyId = companyIdOf(req);
    const camp = await db.query<{ title: string; due_date: string; status: string }>(`SELECT title, due_date::text, status FROM kye_campaign WHERE id = $1 AND company_id = $2`, [id, companyId]);
    if (!camp.rows[0]) throw new NotFoundError('campaign not found');
    if (camp.rows[0].status !== 'open') throw new ConflictError('this campaign is closed');
    // At most one nudge a day per person.
    const pending = await db.query<{ employee_id: string }>(
      `UPDATE kye_response SET last_nudged_at = now() WHERE campaign_id = $1 AND status = 'pending' AND (last_nudged_at IS NULL OR last_nudged_at < now() - interval '20 hours') RETURNING employee_id`, [id]);
    let sent = 0;
    for (const p of pending.rows) {
      const u = await userIdForEmployee(db, p.employee_id);
      if (u) { await notify(db, { companyId, userId: u, employeeId: p.employee_id, template: 'relations.kye.nudge', title: `Reminder: ${camp.rows[0].title}`, body: `Please review and confirm your profile by ${bsLabel(camp.rows[0].due_date)}.`, payload: { campaignId: id } }); sent++; }
    }
    res.json({ nudged: sent });
  });

  r.post('/kye/campaigns/:id/close', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const u = await db.query(`UPDATE kye_campaign SET status = 'closed', closed_at = now() WHERE id = $1 AND company_id = $2 AND status = 'open'`, [id, companyIdOf(req)]);
    if (!u.rowCount) throw new NotFoundError('open campaign not found');
    await audit(db, { actorId: req.user!.id, action: 'close', table: 'kye_campaign', rowId: id });
    res.json({ ok: true });
  });

  /** The employee's open review requests and how complete their record is. */
  r.get('/kye/mine', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) return res.json({ campaigns: [], completeness: null });
    const companyId = companyIdOf(req);
    const rows = await db.query(
      `SELECT c.id, c.title, c.message, c.sections, c.due_date::text AS due_date, r.status, r.responded_at FROM kye_response r JOIN kye_campaign c ON c.id = r.campaign_id
        WHERE r.employee_id = $1 AND c.company_id = $2 AND c.status = 'open' ORDER BY c.due_date`, [u.employeeId, companyId]);
    const comp = (await completeness(db, companyId, u.employeeId))[0] ?? null;
    res.json({ campaigns: rows.rows, completeness: comp, sectionLabels: SECTION_LABEL });
  });

  r.post('/kye/campaigns/:id/respond', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    if (!u.employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const b = z.object({ status: z.enum(['confirmed', 'changes_requested']), note: z.string().trim().max(1000).nullish() }).parse(req.body);
    const r0 = await db.query(
      `UPDATE kye_response r SET status = $3, note = $4, responded_at = now() FROM kye_campaign c
        WHERE r.campaign_id = $1 AND r.employee_id = $2 AND c.id = r.campaign_id AND c.status = 'open' AND c.company_id = $5`,
      [id, u.employeeId, b.status, b.note ?? null, companyIdOf(req)]);
    if (!r0.rowCount) throw new NotFoundError('nothing to confirm for this campaign');
    res.json({ ok: true });
  });

  r.get('/completeness', hr, async (req, res) => {
    const q = z.object({ employeeId: uuid.optional() }).parse(req.query);
    res.json({ employees: await completeness(db, companyIdOf(req), q.employeeId), sectionLabels: SECTION_LABEL });
  });

  // ============================================================ sign-in pop-ups

  r.get('/popups', async (req, res) => {
    const u = req.user!;
    const companyId = companyIdOf(req);
    const params: unknown[] = [companyId, u.id];
    const cond = audienceSql('a', await placementOf(db, u.employeeId), params, true);
    const rows = await db.query<{ id: string; title: string; body: string; requires_ack: boolean; created_at: Date; author_name: string }>(
      `SELECT a.id, a.title, a.body, a.requires_ack, a.created_at, coalesce(e.name_en, au.name) AS author_name
         FROM announcement a JOIN app_user au ON au.id = a.author_id LEFT JOIN employee e ON e.id = a.author_employee_id
        WHERE a.company_id = $1 AND a.deleted_at IS NULL AND a.popup AND a.author_id <> $2
          AND (a.expires_on IS NULL OR a.expires_on >= current_date) AND (a.popup_until IS NULL OR a.popup_until >= current_date)
          AND NOT EXISTS (SELECT 1 FROM announcement_popup_seen s WHERE s.announcement_id = a.id AND s.user_id = $2)
          AND NOT EXISTS (SELECT 1 FROM announcement_ack k WHERE k.announcement_id = a.id AND k.user_id = $2)
          AND ${cond}
        ORDER BY a.created_at DESC LIMIT 5`, params);
    res.json({ popups: rows.rows.map((x) => ({ id: x.id, title: x.title, body: x.body, requiresAck: x.requires_ack, createdAt: x.created_at, authorName: x.author_name })) });
  });

  r.post('/popups/:id/dismiss', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const u = req.user!;
    const params: unknown[] = [id, companyIdOf(req)];
    const cond = audienceSql('a', await placementOf(db, u.employeeId), params, true);
    const a = await db.query<{ requires_ack: boolean }>(`SELECT a.requires_ack FROM announcement a WHERE a.id = $1 AND a.company_id = $2 AND a.deleted_at IS NULL AND a.popup AND ${cond}`, params);
    if (!a.rows[0]) throw new NotFoundError('notice not found');
    const b = z.object({ acknowledge: z.boolean().default(false) }).parse(req.body ?? {});
    if (a.rows[0].requires_ack && !b.acknowledge) throw new BadRequestError('confirm that you have read this notice');
    await db.query('INSERT INTO announcement_popup_seen (announcement_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [id, u.id]);
    if (a.rows[0].requires_ack) await db.query('INSERT INTO announcement_ack (announcement_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [id, u.id]);
    res.json({ ok: true });
  });

  // ============================================================ registered devices

  r.get('/devices/mine', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) return res.json({ devices: [], required: false });
    const companyId = companyIdOf(req);
    const rows = await db.query(`SELECT id, label, status, requested_at, decided_at, last_used_at FROM registered_device WHERE employee_id = $1 AND company_id = $2 AND status <> 'revoked' ORDER BY requested_at DESC`,
      [u.employeeId, companyId]);
    res.json({ devices: rows.rows, required: (await relationsPolicy(db, companyId)).registeredDevicesOnly });
  });

  r.post('/devices/register', async (req, res) => {
    const u = req.user!;
    if (!u.employeeId) throw new BadRequestError('Your account is not linked to an employee record.');
    const companyId = companyIdOf(req);
    const b = z.object({ token: z.string().min(16).max(200).regex(/^[A-Za-z0-9_-]+$/), label: text(80) }).parse(req.body);
    const count = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM registered_device WHERE employee_id = $1 AND status IN ('pending', 'approved')`, [u.employeeId]);
    if (count.rows[0]!.n >= 3) throw new ConflictError('you already have three devices; remove one first');
    const ua = String(req.headers['user-agent'] ?? '').slice(0, 200) || null;
    const ins = await db.query<{ id: string }>(
      `INSERT INTO registered_device (company_id, employee_id, user_id, token_hash, label, user_agent) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (token_hash) DO NOTHING RETURNING id`,
      [companyId, u.employeeId, u.id, deviceTokenHash(b.token), b.label, ua]);
    if (!ins.rows[0]) throw new ConflictError('this device is already registered');
    for (const userId of await userIdsWithRole(db, companyId, 'hr')) {
      if (userId === u.id) continue;
      await notify(db, { companyId, userId, template: 'relations.device.new', title: 'New clock-in device to approve', body: `${u.name} registered "${b.label}".`, payload: { deviceId: ins.rows[0].id } });
    }
    res.status(201).json({ id: ins.rows[0].id, status: 'pending' });
  });

  r.delete('/devices/mine/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const u = await db.query(`UPDATE registered_device SET status = 'revoked', decided_at = now() WHERE id = $1 AND employee_id = $2 AND status <> 'revoked'`, [id, req.user!.employeeId]);
    if (!u.rowCount) throw new NotFoundError('device not found');
    res.json({ ok: true });
  });

  r.get('/devices', hr, async (req, res) => {
    const q = z.object({ status: z.enum(['pending', 'approved', 'rejected', 'revoked', 'all']).default('all') }).parse(req.query);
    const rows = await db.query(
      `SELECT d.id, d.employee_id, e.name_en AS employee_name, e.emp_code, d.label, d.user_agent, d.status, d.requested_at, d.decided_at, d.last_used_at
         FROM registered_device d JOIN employee e ON e.id = d.employee_id WHERE d.company_id = $1 AND ($2 = 'all' OR d.status = $2)
        ORDER BY (d.status = 'pending') DESC, d.requested_at DESC LIMIT 500`, [companyIdOf(req), q.status]);
    res.json({ devices: rows.rows });
  });

  r.post('/devices/:id/:action', hr, async (req, res) => {
    const id = uuid.parse(req.params.id);
    const action = z.enum(['approve', 'reject', 'revoke']).parse(req.params.action);
    const companyId = companyIdOf(req);
    const next = { approve: 'approved', reject: 'rejected', revoke: 'revoked' }[action];
    const from = action === 'revoke' ? ['approved', 'pending'] : ['pending'];
    const u = await db.query<{ employee_id: string; label: string }>(
      `UPDATE registered_device SET status = $3, decided_by = $4, decided_at = now() WHERE id = $1 AND company_id = $2 AND status = ANY($5::text[]) RETURNING employee_id, label`,
      [id, companyId, next, req.user!.id, from]);
    if (!u.rows[0]) throw new ConflictError('this device cannot be changed that way');
    await audit(db, { actorId: req.user!.id, action, table: 'registered_device', rowId: id });
    const userId = await userIdForEmployee(db, u.rows[0].employee_id);
    if (userId && action !== 'revoke') await notify(db, { companyId, userId, employeeId: u.rows[0].employee_id, template: 'relations.device.decided', title: action === 'approve' ? 'Device approved' : 'Device not approved', body: `"${u.rows[0].label}" ${action === 'approve' ? 'can now be used to clock in' : 'was not approved for clock-in'}.`, payload: { deviceId: id } });
    res.json({ ok: true });
  });

  // ============================================================ HR overview counts
  r.get('/summary', hr, async (req, res) => {
    const companyId = companyIdOf(req);
    const u = req.user!;
    const [g, d, dev, kye] = await Promise.all([
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM grievance WHERE company_id = $1 AND status IN ('submitted', 'under_review', 'hearing_scheduled', 'appealed') AND user_id <> $2 AND NOT ($2 = ANY(excluded_user_ids))`, [companyId, u.id]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM disciplinary_case WHERE company_id = $1 AND status IN ('open', 'show_cause', 'responded')`, [companyId]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM registered_device WHERE company_id = $1 AND status = 'pending'`, [companyId]),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM kye_campaign WHERE company_id = $1 AND status = 'open'`, [companyId]),
    ]);
    res.json({ openGrievances: g.rows[0]!.n, openCases: d.rows[0]!.n, pendingDevices: dev.rows[0]!.n, openKye: kye.rows[0]!.n, isHr: hasRole(u, 'hr') });
  });

  return r;
}

