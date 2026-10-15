import type { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, isManagerOf } from '../auth/middleware.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { directManager, employeeInCompany, isAboveInChain, tx } from './service.js';

const CADENCE_DAYS: Record<string, number> = { weekly: 7, biweekly: 14, monthly: 30 };

interface MeetingRow { id: string; company_id: string; manager_employee_id: string; employee_id: string; scheduled_at: Date; cadence: string; status: string }

export function conversationRoutes(r: Router, { db }: AppDeps): void {
  const me = (req: { user?: { employeeId: string | null } }) => {
    const id = req.user?.employeeId;
    if (!id) throw new ForbiddenError('this account is not linked to an employee');
    return id;
  };

  // ---------------------------------------------------------------- manager's journal (private to the author and HR)
  r.get('/journal', async (req, res) => {
    const q = z.object({ employeeId: uuid, from: isoDate.optional(), to: isoDate.optional() }).parse(req.query);
    const u = req.user!; const companyId = companyIdOf(req);
    await employeeInCompany(db, companyId, q.employeeId);
    if (!(await isAboveInChain(db, u, q.employeeId))) throw new ForbiddenError('only the employee\'s managers and HR keep a journal');
    const rows = await db.query(
      `SELECT j.id, j.note_date, j.tag, j.note, j.created_at, j.author_id, u.name AS author_name FROM perf_journal j JOIN app_user u ON u.id = j.author_id
        WHERE j.employee_id = $1 AND j.company_id = $2 AND ($3 OR j.author_id = $4)
          AND ($5::date IS NULL OR j.note_date >= $5) AND ($6::date IS NULL OR j.note_date <= $6)
        ORDER BY j.note_date DESC, j.created_at DESC`, [q.employeeId, companyId, hasRole(u, 'hr'), u.id, q.from ?? null, q.to ?? null]);
    res.json({ entries: rows.rows });
  });

  r.post('/journal', async (req, res) => {
    const b = z.object({ employeeId: uuid, noteDate: isoDate, tag: z.enum(['positive', 'improvement', 'neutral']).default('neutral'), note: z.string().trim().min(1).max(4000) }).parse(req.body);
    const u = req.user!; const companyId = companyIdOf(req);
    await employeeInCompany(db, companyId, b.employeeId);
    if (!(await isAboveInChain(db, u, b.employeeId))) throw new ForbiddenError('only the employee\'s managers and HR keep a journal');
    const r0 = await db.query<{ id: string }>('INSERT INTO perf_journal (company_id, employee_id, author_id, note_date, tag, note) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
      [companyId, b.employeeId, u.id, b.noteDate, b.tag, b.note]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  r.delete('/journal/:id', async (req, res) => {
    const r0 = await db.query('DELETE FROM perf_journal WHERE id = $1 AND company_id = $2 AND author_id = $3', [uuid.parse(req.params.id), companyIdOf(req), req.user!.id]);
    if (!r0.rowCount) throw new NotFoundError('note not found');
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- continuous feedback
  const FEEDBACK_SELECT = `
    SELECT f.id, f.status, f.prompt, f.body, f.private_to_manager, f.created_at, f.given_at,
           f.giver_employee_id, g.name_en AS giver_name, f.subject_employee_id, s.name_en AS subject_name,
           f.requester_employee_id, rq.name_en AS requester_name
      FROM perf_feedback f JOIN employee g ON g.id = f.giver_employee_id JOIN employee s ON s.id = f.subject_employee_id
      LEFT JOIN employee rq ON rq.id = f.requester_employee_id`;

  /**
   * received: given feedback about me (not the private-to-manager ones). given: what I wrote. requests: feedback I was
   * asked for and requests I made. about: everything about an employee, for their managers and HR.
   */
  r.get('/feedback', async (req, res) => {
    const q = z.object({ box: z.enum(['received', 'given', 'requests', 'about']).default('received'), employeeId: uuid.optional() }).parse(req.query);
    const u = req.user!; const companyId = companyIdOf(req);
    if (q.box === 'about') {
      if (!q.employeeId) throw new BadRequestError('employeeId is required');
      await employeeInCompany(db, companyId, q.employeeId);
      if (!(await isAboveInChain(db, u, q.employeeId))) throw new ForbiddenError('only the employee\'s managers and HR see all feedback');
      const rows = await db.query(`${FEEDBACK_SELECT} WHERE f.company_id = $1 AND f.subject_employee_id = $2 AND f.status = 'given' ORDER BY f.given_at DESC`, [companyId, q.employeeId]);
      res.json({ feedback: rows.rows });
      return;
    }
    const emp = me(req);
    const where = {
      received: `f.subject_employee_id = $2 AND f.status = 'given' AND NOT f.private_to_manager`,
      given: `f.giver_employee_id = $2 AND f.status = 'given'`,
      requests: `((f.giver_employee_id = $2 AND f.status = 'requested') OR f.requester_employee_id = $2)`,
    }[q.box];
    const rows = await db.query(`${FEEDBACK_SELECT} WHERE f.company_id = $1 AND ${where} ORDER BY coalesce(f.given_at, f.created_at) DESC LIMIT 300`, [companyId, emp]);
    // A requester who is the subject sees who answered, but not the text of private-to-manager answers.
    const out = rows.rows.map((f: Record<string, unknown>) => (q.box === 'requests' && f.giver_employee_id !== emp && f.private_to_manager && f.subject_employee_id === emp ? { ...f, body: null } : f));
    res.json({ feedback: out });
  });

  r.post('/feedback', async (req, res) => {
    const b = z.object({ toEmployeeId: uuid, body: z.string().trim().min(1).max(4000), privateToManager: z.boolean().default(false) }).parse(req.body);
    const giver = me(req); const companyId = companyIdOf(req);
    if (b.toEmployeeId === giver) throw new BadRequestError('you cannot give feedback to yourself');
    await employeeInCompany(db, companyId, b.toEmployeeId);
    await tx(db, async (cx) => {
      await cx.query(`INSERT INTO perf_feedback (company_id, giver_employee_id, subject_employee_id, status, body, private_to_manager, given_at) VALUES ($1, $2, $3, 'given', $4, $5, now())`,
        [companyId, giver, b.toEmployeeId, b.body, b.privateToManager]);
      const to = b.privateToManager ? await directManager(cx, b.toEmployeeId) : b.toEmployeeId;
      if (to) await notify(cx, { companyId, userId: await userIdForEmployee(cx, to), employeeId: to, template: 'feedback.given', title: 'New feedback', body: b.privateToManager ? `${req.user!.name} shared feedback about a member of your team.` : `${req.user!.name} gave you feedback.`, payload: {} });
    });
    res.status(201).json({ ok: true });
  });

  r.post('/feedback/requests', async (req, res) => {
    const b = z.object({ fromEmployeeIds: z.array(uuid).min(1).max(10), aboutEmployeeId: uuid.optional(), prompt: z.string().trim().max(1000).optional() }).parse(req.body);
    const u = req.user!; const requester = me(req); const companyId = companyIdOf(req);
    const subject = b.aboutEmployeeId ?? requester;
    await employeeInCompany(db, companyId, subject);
    if (subject !== requester && !(await isAboveInChain(db, u, subject))) throw new ForbiddenError('you can ask for feedback about yourself or your team');
    await tx(db, async (cx) => {
      for (const from of new Set(b.fromEmployeeIds)) {
        if (from === subject) throw new BadRequestError('someone cannot give feedback about themselves');
        await employeeInCompany(cx, companyId, from);
        await cx.query(`INSERT INTO perf_feedback (company_id, giver_employee_id, subject_employee_id, requester_employee_id, status, prompt) VALUES ($1, $2, $3, $4, 'requested', $5)`,
          [companyId, from, subject, requester, b.prompt ?? null]);
        await notify(cx, { companyId, userId: await userIdForEmployee(cx, from), employeeId: from, template: 'feedback.requested', title: 'Feedback requested',
          body: `${u.name} asked for your feedback${subject !== requester ? ' about a colleague' : ''}${b.prompt ? `: ${b.prompt.slice(0, 140)}` : '.'}`, payload: {} });
      }
    });
    res.status(201).json({ ok: true });
  });

  const openRequest = async (companyId: string, id: string, giver: string) => {
    const r0 = await db.query<{ id: string; status: string; requester_employee_id: string | null; subject_employee_id: string }>(
      'SELECT id, status, requester_employee_id, subject_employee_id FROM perf_feedback WHERE id = $1 AND company_id = $2 AND giver_employee_id = $3', [id, companyId, giver]);
    if (!r0.rows[0]) throw new NotFoundError('feedback request not found');
    if (r0.rows[0].status !== 'requested') throw new ConflictError('this request was already answered');
    return r0.rows[0];
  };

  r.put('/feedback/:id', async (req, res) => {
    const b = z.object({ body: z.string().trim().min(1).max(4000), privateToManager: z.boolean().default(false) }).parse(req.body);
    const giver = me(req); const companyId = companyIdOf(req);
    const f = await openRequest(companyId, uuid.parse(req.params.id), giver);
    await tx(db, async (cx) => {
      await cx.query(`UPDATE perf_feedback SET status = 'given', body = $2, private_to_manager = $3, given_at = now() WHERE id = $1`, [f.id, b.body, b.privateToManager]);
      if (f.requester_employee_id) await notify(cx, { companyId, userId: await userIdForEmployee(cx, f.requester_employee_id), employeeId: f.requester_employee_id, template: 'feedback.answered', title: 'Feedback received', body: `${req.user!.name} answered your feedback request.`, payload: {} });
    });
    res.json({ ok: true });
  });

  r.post('/feedback/:id/decline', async (req, res) => {
    const giver = me(req); const companyId = companyIdOf(req);
    const f = await openRequest(companyId, uuid.parse(req.params.id), giver);
    await db.query(`UPDATE perf_feedback SET status = 'declined' WHERE id = $1`, [f.id]);
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- 1-on-1s (only the two people in the meeting)
  const loadMeeting = async (cx: Queryable, companyId: string, id: string, employeeId: string): Promise<MeetingRow & { side: 'manager' | 'employee' }> => {
    const r0 = await cx.query<MeetingRow>('SELECT id, company_id, manager_employee_id, employee_id, scheduled_at, cadence, status FROM perf_one_on_one WHERE id = $1 AND company_id = $2', [id, companyId]);
    const m = r0.rows[0];
    if (!m || (m.manager_employee_id !== employeeId && m.employee_id !== employeeId)) throw new NotFoundError('1-on-1 not found');
    return { ...m, side: m.manager_employee_id === employeeId ? 'manager' : 'employee' };
  };

  r.get('/one-on-ones', async (req, res) => {
    const q = z.object({ with: uuid.optional() }).parse(req.query);
    const emp = me(req); const companyId = companyIdOf(req);
    const rows = await db.query(
      `SELECT o.id, o.manager_employee_id, mg.name_en AS manager_name, o.employee_id, em.name_en AS employee_name, o.scheduled_at, o.cadence, o.status,
              CASE WHEN o.manager_employee_id = $2 THEN 'manager' ELSE 'employee' END AS side,
              (SELECT count(*)::int FROM perf_one_on_one_item i WHERE i.meeting_id = o.id AND i.kind = 'action' AND NOT i.done) AS open_actions,
              (SELECT count(*)::int FROM perf_one_on_one_item i WHERE i.meeting_id = o.id AND i.kind = 'agenda') AS agenda_items
         FROM perf_one_on_one o JOIN employee mg ON mg.id = o.manager_employee_id JOIN employee em ON em.id = o.employee_id
        WHERE o.company_id = $1 AND (o.manager_employee_id = $2 OR o.employee_id = $2)
          AND ($3::uuid IS NULL OR o.manager_employee_id = $3 OR o.employee_id = $3)
        ORDER BY o.status = 'scheduled' DESC, CASE WHEN o.status = 'scheduled' THEN o.scheduled_at END ASC, o.scheduled_at DESC LIMIT 200`, [companyId, emp, q.with ?? null]);
    // Who I can meet with: my direct reports and my own manager.
    const reports = await db.query(
      `SELECT e.id, e.name_en, e.designation FROM employee_assignment a JOIN employee e ON e.id = a.employee_id
        WHERE a.manager_id = $1 AND a.valid @> current_date AND e.status = 'active' ORDER BY e.name_en`, [emp]);
    const mgr = await directManager(db, emp);
    const manager = mgr ? (await db.query('SELECT id, name_en, designation FROM employee WHERE id = $1', [mgr])).rows[0] ?? null : null;
    res.json({ meetings: rows.rows, reports: reports.rows, manager });
  });

  r.post('/one-on-ones', async (req, res) => {
    const b = z.object({
      withEmployeeId: uuid, scheduledAt: z.iso.datetime({ offset: true }), cadence: z.enum(['none', 'weekly', 'biweekly', 'monthly']).default('none'),
      agenda: z.array(z.string().trim().min(1).max(1000)).max(20).default([]),
    }).parse(req.body);
    const u = req.user!; const emp = me(req); const companyId = companyIdOf(req);
    await employeeInCompany(db, companyId, b.withEmployeeId);
    let managerId: string; let employeeId: string;
    if (await isManagerOf(db, emp, b.withEmployeeId)) { managerId = emp; employeeId = b.withEmployeeId; }
    else if (await isManagerOf(db, b.withEmployeeId, emp)) { managerId = b.withEmployeeId; employeeId = emp; }
    else throw new ForbiddenError('1-on-1s are between a person and their direct manager');
    const id = await tx(db, async (cx) => {
      const ins = await cx.query<{ id: string }>(
        'INSERT INTO perf_one_on_one (company_id, manager_employee_id, employee_id, scheduled_at, cadence, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
        [companyId, managerId, employeeId, b.scheduledAt, b.cadence, u.id]);
      const mid = ins.rows[0]!.id;
      for (const t of b.agenda) await cx.query(`INSERT INTO perf_one_on_one_item (meeting_id, kind, text, added_by) VALUES ($1, 'agenda', $2, $3)`, [mid, t, u.id]);
      const other = b.withEmployeeId;
      await notify(cx, { companyId, userId: await userIdForEmployee(cx, other), employeeId: other, template: 'one_on_one.scheduled', title: '1-on-1 scheduled', body: `${u.name} scheduled a 1-on-1 with you.`, payload: { meetingId: mid } });
      return mid;
    });
    res.status(201).json({ id });
  });

  r.get('/one-on-ones/:id', async (req, res) => {
    const emp = me(req); const companyId = companyIdOf(req);
    const m = await loadMeeting(db, companyId, uuid.parse(req.params.id), emp);
    const row = await db.query<Record<string, unknown>>(
      `SELECT o.*, mg.name_en AS manager_name, em.name_en AS employee_name FROM perf_one_on_one o
         JOIN employee mg ON mg.id = o.manager_employee_id JOIN employee em ON em.id = o.employee_id WHERE o.id = $1`, [m.id]);
    const { manager_private_notes, employee_private_notes, ...rest } = row.rows[0]!;
    const items = await db.query(
      `SELECT i.id, i.kind, i.text, i.owner_employee_id, o.name_en AS owner_name, i.done, i.created_at, i.added_by FROM perf_one_on_one_item i
         LEFT JOIN employee o ON o.id = i.owner_employee_id WHERE i.meeting_id = $1 ORDER BY i.kind, i.created_at`, [m.id]);
    // Earlier open action items between the same two people, so nothing agreed is forgotten.
    const carried = await db.query(
      `SELECT i.id, i.text, i.owner_employee_id, o.name_en AS owner_name, p.scheduled_at FROM perf_one_on_one_item i JOIN perf_one_on_one p ON p.id = i.meeting_id
         LEFT JOIN employee o ON o.id = i.owner_employee_id
        WHERE p.manager_employee_id = $1 AND p.employee_id = $2 AND p.id <> $3 AND i.kind = 'action' AND NOT i.done ORDER BY p.scheduled_at`,
      [m.manager_employee_id, m.employee_id, m.id]);
    res.json({ meeting: { ...rest, side: m.side, private_notes: m.side === 'manager' ? manager_private_notes : employee_private_notes }, items: items.rows, openActions: carried.rows });
  });

  r.patch('/one-on-ones/:id', async (req, res) => {
    const b = z.object({
      scheduledAt: z.iso.datetime({ offset: true }).optional(), cadence: z.enum(['none', 'weekly', 'biweekly', 'monthly']).optional(),
      sharedNotes: z.string().max(8000).nullish(), privateNotes: z.string().max(8000).nullish(), status: z.enum(['scheduled', 'done', 'cancelled']).optional(),
    }).parse(req.body);
    const u = req.user!; const emp = me(req); const companyId = companyIdOf(req);
    const out = await tx(db, async (cx) => {
      const m = await loadMeeting(cx, companyId, uuid.parse(req.params.id), emp);
      const sets: string[] = []; const vals: unknown[] = [m.id];
      const put = (col: string, v: unknown) => { vals.push(v); sets.push(`${col} = $${vals.length}`); };
      if (b.scheduledAt !== undefined) put('scheduled_at', b.scheduledAt);
      if (b.cadence !== undefined) put('cadence', b.cadence);
      if (b.sharedNotes !== undefined) put('shared_notes', b.sharedNotes);
      if (b.privateNotes !== undefined) put(m.side === 'manager' ? 'manager_private_notes' : 'employee_private_notes', b.privateNotes);
      if (b.status !== undefined) put('status', b.status);
      if (sets.length) await cx.query(`UPDATE perf_one_on_one SET ${sets.join(', ')} WHERE id = $1`, vals);
      let nextId: string | null = null;
      const cadence = b.cadence ?? m.cadence;
      if (b.status === 'done' && m.status !== 'done' && CADENCE_DAYS[cadence]) {
        const base = new Date(b.scheduledAt ?? m.scheduled_at);
        const next = new Date(base.getTime() + CADENCE_DAYS[cadence]! * 86_400_000);
        const ins = await cx.query<{ id: string }>(
          'INSERT INTO perf_one_on_one (company_id, manager_employee_id, employee_id, scheduled_at, cadence, created_by) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
          [companyId, m.manager_employee_id, m.employee_id, next, cadence, u.id]);
        nextId = ins.rows[0]!.id;
      }
      return nextId;
    });
    res.json({ ok: true, nextId: out });
  });

  r.post('/one-on-ones/:id/items', async (req, res) => {
    const b = z.object({ kind: z.enum(['agenda', 'action']), text: z.string().trim().min(1).max(1000), ownerEmployeeId: uuid.nullish() }).parse(req.body);
    const emp = me(req); const companyId = companyIdOf(req);
    const m = await loadMeeting(db, companyId, uuid.parse(req.params.id), emp);
    if (b.ownerEmployeeId && b.ownerEmployeeId !== m.manager_employee_id && b.ownerEmployeeId !== m.employee_id) throw new BadRequestError('the owner must be one of the two people in the meeting');
    const r0 = await db.query<{ id: string }>('INSERT INTO perf_one_on_one_item (meeting_id, kind, text, owner_employee_id, added_by) VALUES ($1, $2, $3, $4, $5) RETURNING id',
      [m.id, b.kind, b.text, b.ownerEmployeeId ?? null, req.user!.id]);
    res.status(201).json({ id: r0.rows[0]!.id });
  });

  const itemMeeting = async (companyId: string, itemId: string, emp: string) => {
    const r0 = await db.query<{ meeting_id: string }>('SELECT meeting_id FROM perf_one_on_one_item WHERE id = $1', [itemId]);
    if (!r0.rows[0]) throw new NotFoundError('item not found');
    return loadMeeting(db, companyId, r0.rows[0].meeting_id, emp);
  };

  r.patch('/one-on-one-items/:id', async (req, res) => {
    const b = z.object({ done: z.boolean().optional(), text: z.string().trim().min(1).max(1000).optional() }).parse(req.body);
    const id = uuid.parse(req.params.id);
    await itemMeeting(companyIdOf(req), id, me(req));
    await db.query('UPDATE perf_one_on_one_item SET done = coalesce($2, done), text = coalesce($3, text) WHERE id = $1', [id, b.done ?? null, b.text ?? null]);
    res.json({ ok: true });
  });

  r.delete('/one-on-one-items/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    await itemMeeting(companyIdOf(req), id, me(req));
    await db.query('DELETE FROM perf_one_on_one_item WHERE id = $1', [id]);
    res.json({ ok: true });
  });
}
