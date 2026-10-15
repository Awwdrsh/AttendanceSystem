import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import type { Queryable } from '../../db/pool.js';
import { audit } from '../../audit.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { attachFile, readFile, sendStoredFile } from '../files/store.js';
import { notify, userIdsWithRole } from '../notifications/service.js';
import { ensureHelpdeskDefaults, handlers, helpdeskPolicy, helpdeskPolicySchema, PRIORITIES, roleOnTicket, STATUSES, type TicketRow } from './service.js';

const uuid = z.uuid();
const fileIds = z.array(z.uuid()).max(5).default([]);

const TICKET_SELECT = `SELECT t.*, c.confidential, c.owner_user_id, c.name AS category_name,
    ru.name AS requester_name, au.name AS assignee_name, e.emp_code, e.name_en AS employee_name
  FROM helpdesk_ticket t JOIN helpdesk_category c ON c.id = t.category_id JOIN app_user ru ON ru.id = t.requester_user_id
  LEFT JOIN app_user au ON au.id = t.assignee_user_id LEFT JOIN employee e ON e.id = t.employee_id`;

/** Help desk: employees raise questions and requests; HR (and named owners) answer them against an SLA. */
export function helpdeskRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const tx = async <T>(fn: (c: Queryable) => Promise<T>): Promise<T> => {
    const client = await db.connect();
    try { await client.query('BEGIN'); const out = await fn(client); await client.query('COMMIT'); return out; }
    catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  };
  r.use(async (req, _res, next) => { if (req.user?.companyId) await ensureHelpdeskDefaults(db, req.user.companyId); next(); });

  const slaDue = async (companyId: string, priority: typeof PRIORITIES[number], from: Date) => {
    const p = await helpdeskPolicy(db, companyId);
    return new Date(from.getTime() + p.slaHours[priority] * 3_600_000);
  };

  const loadTicket = async (req: Request, id: string): Promise<{ t: TicketRow & Record<string, unknown>; role: 'agent' | 'requester' }> => {
    const rows = await db.query<TicketRow & Record<string, unknown>>(`${TICKET_SELECT} WHERE t.id = $1`, [id]);
    const t = rows.rows[0];
    const role = t ? roleOnTicket(req.user!, t) : null;
    if (!t || !role) throw new NotFoundError('ticket not found');
    return { t, role };
  };

  /** Attach uploads to a message; only the uploader's own unattached help desk uploads are accepted. */
  const attach = async (c: Queryable, companyId: string, userId: string, messageId: string, ids: string[]) => {
    for (const fileId of ids) await attachFile(c, { companyId, fileId, userId, purpose: 'helpdesk_attachment', attachedTo: `helpdesk_message:${messageId}` });
  };

  // ------------------------------------------------------------ settings and categories
  r.get('/settings', async (req, res) => { res.json({ policy: await helpdeskPolicy(db, companyIdOf(req)) }); });
  r.put('/settings', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const policy = helpdeskPolicySchema.parse(req.body);
    await db.query(
      `INSERT INTO company_setting (company_id, key, value, updated_by) VALUES ($1, 'helpdesk.policy', $2, $3)
       ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by`, [companyId, JSON.stringify(policy), req.user!.id]);
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'company_setting', rowId: 'helpdesk.policy', after: policy });
    res.json({ policy });
  });

  r.get('/categories', async (req, res) => {
    const hr = hasRole(req.user!, 'hr');
    const rows = await db.query(
      `SELECT c.id, c.name, c.description, c.confidential, c.sort, c.active, ${hr ? 'c.owner_user_id, u.name AS owner_name' : 'NULL AS owner_user_id, NULL AS owner_name'}
         FROM helpdesk_category c LEFT JOIN app_user u ON u.id = c.owner_user_id
        WHERE c.company_id = $1 AND (c.active OR $2) ORDER BY c.sort, c.name`, [companyIdOf(req), hr]);
    res.json({ categories: rows.rows });
  });
  const categoryBody = z.object({
    name: z.string().trim().min(1).max(80), description: z.string().max(300).nullish(), confidential: z.boolean().default(false),
    ownerUserId: z.uuid().nullish(), sort: z.number().int().min(0).max(999).default(0), active: z.boolean().default(true),
  });
  const checkOwner = async (companyId: string, ownerUserId: string | null | undefined, confidential: boolean) => {
    if (!ownerUserId) return;
    const u = await db.query<{ roles: string[] }>(`SELECT roles FROM app_user WHERE id = $1 AND company_id = $2 AND status = 'active'`, [ownerUserId, companyId]);
    if (!u.rows[0]) throw new BadRequestError('the owner must be an active user in your company');
    if (confidential && !u.rows[0].roles.some((x) => x === 'hr' || x === 'admin')) throw new BadRequestError('a confidential category can only be owned by HR');
  };
  r.post('/categories', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const b = categoryBody.parse(req.body);
    await checkOwner(companyId, b.ownerUserId, b.confidential);
    const ins = await db.query<{ id: string }>(
      'INSERT INTO helpdesk_category (company_id, name, description, confidential, owner_user_id, sort, active) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id',
      [companyId, b.name, b.description ?? null, b.confidential, b.ownerUserId ?? null, b.sort, b.active])
      .catch((err: { code?: string }) => { if (err.code === '23505') throw new ConflictError('a category with this name already exists'); throw err; });
    await audit(db, { actorId: req.user!.id, action: 'create', table: 'helpdesk_category', rowId: ins.rows[0]!.id, after: b });
    res.status(201).json({ id: ins.rows[0]!.id });
  });
  r.put('/categories/:id', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const id = uuid.parse(req.params.id);
    const b = categoryBody.parse(req.body);
    await checkOwner(companyId, b.ownerUserId, b.confidential);
    const u = await db.query('UPDATE helpdesk_category SET name = $3, description = $4, confidential = $5, owner_user_id = $6, sort = $7, active = $8 WHERE id = $1 AND company_id = $2 RETURNING id',
      [id, companyId, b.name, b.description ?? null, b.confidential, b.ownerUserId ?? null, b.sort, b.active])
      .catch((err: { code?: string }) => { if (err.code === '23505') throw new ConflictError('a category with this name already exists'); throw err; });
    if (!u.rows.length) throw new NotFoundError('category not found');
    await audit(db, { actorId: req.user!.id, action: 'update', table: 'helpdesk_category', rowId: id, after: b });
    res.json({ ok: true });
  });

  /** People HR can assign tickets to (HR for confidential categories). */
  r.get('/agents', requireRole('hr'), async (req, res) => {
    const rows = await db.query(
      `SELECT id, name, email, roles, ('hr' = ANY(roles) OR 'admin' = ANY(roles)) AS is_hr FROM app_user WHERE company_id = $1 AND status = 'active' ORDER BY name`, [companyIdOf(req)]);
    res.json({ agents: rows.rows });
  });

  // ------------------------------------------------------------ tickets
  r.post('/tickets', async (req, res) => {
    const companyId = companyIdOf(req);
    const b = z.object({
      categoryId: z.uuid(), subject: z.string().trim().min(3).max(160), body: z.string().trim().min(1).max(5000),
      priority: z.enum(PRIORITIES).default('normal'), fileIds,
    }).parse(req.body);
    const cat = await db.query<{ id: string; confidential: boolean; owner_user_id: string | null; name: string }>(
      'SELECT id, confidential, owner_user_id, name FROM helpdesk_category WHERE id = $1 AND company_id = $2 AND active', [b.categoryId, companyId]);
    const c0 = cat.rows[0];
    if (!c0) throw new BadRequestError('choose a category');
    const now = new Date();
    const due = await slaDue(companyId, b.priority, now);
    const out = await tx(async (c) => {
      // Ticket numbers are sequential per company; the advisory lock keeps two submissions from taking the same one.
      await c.query(`SELECT pg_advisory_xact_lock(hashtext('helpdesk:' || $1::text))`, [companyId]);
      const n = await c.query<{ n: number }>('SELECT coalesce(max(number), 0) + 1 AS n FROM helpdesk_ticket WHERE company_id = $1', [companyId]);
      const ins = await c.query<{ id: string; number: number }>(
        `INSERT INTO helpdesk_ticket (company_id, number, requester_user_id, employee_id, category_id, subject, priority, sla_due_at, assignee_user_id)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING id, number`,
        [companyId, n.rows[0]!.n, req.user!.id, req.user!.employeeId, b.categoryId, b.subject, b.priority, due, c0.confidential ? null : c0.owner_user_id]);
      const t = ins.rows[0]!;
      const msg = await c.query<{ id: string }>('INSERT INTO helpdesk_message (ticket_id, company_id, author_user_id, body, file_ids) VALUES ($1, $2, $3, $4, $5) RETURNING id',
        [t.id, companyId, req.user!.id, b.body, b.fileIds]);
      await attach(c, companyId, req.user!.id, msg.rows[0]!.id, b.fileIds);
      const targets = await handlers(c, { company_id: companyId, assignee_user_id: c0.confidential ? null : c0.owner_user_id, owner_user_id: c0.owner_user_id, confidential: c0.confidential });
      for (const userId of targets) {
        if (userId === req.user!.id) continue;
        await notify(c, { companyId, userId, template: 'helpdesk.ticket.new', title: `New ticket #${t.number}: ${c0.name}`,
          body: c0.confidential ? 'A confidential ticket was raised. Open the help desk to read it.' : b.subject, payload: { ticketId: t.id }, channels: ['push'] });
      }
      return t;
    });
    res.status(201).json({ id: out.id, number: out.number, slaDueAt: due });
  });

  r.get('/tickets/mine', async (req, res) => {
    const rows = await db.query(
      `${TICKET_SELECT} WHERE t.company_id = $1 AND t.requester_user_id = $2 ORDER BY t.status IN ('resolved', 'closed'), t.updated_at DESC LIMIT 200`, [companyIdOf(req), req.user!.id]);
    res.json({ tickets: rows.rows.map((t) => ({ ...t, owner_user_id: undefined })) });
  });

  /** The queue: HR sees everything; others see the non-confidential tickets assigned to them or in categories they own. */
  r.get('/tickets', async (req, res) => {
    const companyId = companyIdOf(req);
    const q = z.object({
      status: z.enum([...STATUSES, 'active']).optional(), categoryId: z.uuid().optional(), priority: z.enum(PRIORITIES).optional(),
      assignee: z.union([z.uuid(), z.literal('me'), z.literal('none')]).optional(), breached: z.enum(['1', 'true']).optional(), search: z.string().max(80).optional(),
    }).parse(req.query);
    const hr = hasRole(req.user!, 'hr');
    const assignee = q.assignee === 'me' ? req.user!.id : q.assignee;
    const search = q.search?.trim();
    const num = search && /^#?\d+$/.test(search) ? Number(search.replace('#', '')) : null;
    const rows = await db.query(
      `${TICKET_SELECT}
        WHERE t.company_id = $1
          AND ($2 OR (NOT c.confidential AND (t.assignee_user_id = $3 OR c.owner_user_id = $3)))
          AND ($4::text IS NULL OR t.status = $4 OR ($4 = 'active' AND t.status IN ('open', 'in_progress', 'waiting')))
          AND ($5::uuid IS NULL OR t.category_id = $5) AND ($6::text IS NULL OR t.priority = $6)
          AND ($7::text IS NULL OR ($7 = 'none' AND t.assignee_user_id IS NULL) OR t.assignee_user_id::text = $7)
          AND (NOT $8 OR (t.status IN ('open', 'in_progress', 'waiting') AND t.sla_due_at < now()))
          AND ($9::text IS NULL OR t.subject ILIKE '%' || $9 || '%' OR ru.name ILIKE '%' || $9 || '%' OR t.number = $10::int)
        ORDER BY t.status IN ('resolved', 'closed'), (t.status IN ('open', 'in_progress', 'waiting') AND t.sla_due_at < now()) DESC,
                 CASE t.priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, t.sla_due_at LIMIT 500`,
      [companyId, hr, req.user!.id, q.status ?? null, q.categoryId ?? null, q.priority ?? null, assignee ?? null, !!q.breached,
        search && num === null ? search.replace(/[\\%_]/g, (ch) => `\\${ch}`) : null, num]);
    res.json({ tickets: rows.rows });
  });

  r.get('/stats', requireRole('hr'), async (req, res) => {
    const companyId = companyIdOf(req);
    const s = await db.query<Record<string, string | number | null>>(
      `SELECT count(*) FILTER (WHERE status IN ('open', 'in_progress', 'waiting'))::int AS open,
              count(*) FILTER (WHERE status = 'open' AND assignee_user_id IS NULL)::int AS unassigned,
              count(*) FILTER (WHERE status IN ('open', 'in_progress', 'waiting') AND sla_due_at < now())::int AS overdue,
              count(*) FILTER (WHERE breached_at IS NOT NULL AND created_at > now() - interval '90 days')::int AS breached_90d,
              count(*) FILTER (WHERE resolved_at > now() - interval '30 days')::int AS resolved_30d,
              round(avg(extract(epoch FROM resolved_at - created_at) / 3600) FILTER (WHERE resolved_at > now() - interval '90 days')::numeric, 1)::text AS avg_resolution_hours,
              round(avg(extract(epoch FROM first_response_at - created_at) / 3600) FILTER (WHERE first_response_at > now() - interval '90 days')::numeric, 1)::text AS avg_first_response_hours,
              round(avg(rating) FILTER (WHERE rating IS NOT NULL AND created_at > now() - interval '180 days')::numeric, 2)::text AS avg_rating,
              count(*) FILTER (WHERE rating IS NOT NULL AND created_at > now() - interval '180 days')::int AS ratings
         FROM helpdesk_ticket WHERE company_id = $1`, [companyId]);
    const byCat = await db.query(
      `SELECT c.name, count(t.*) FILTER (WHERE t.status IN ('open', 'in_progress', 'waiting'))::int AS open, count(t.*)::int AS total
         FROM helpdesk_category c LEFT JOIN helpdesk_ticket t ON t.category_id = c.id WHERE c.company_id = $1 GROUP BY c.id ORDER BY c.sort`, [companyId]);
    res.json({ stats: s.rows[0], byCategory: byCat.rows });
  });

  r.get('/tickets/:id', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    const msgs = await db.query<{ id: string; internal: boolean; file_ids: string[] }>(
      `SELECT m.id, m.body, m.internal, m.file_ids, m.created_at, m.author_user_id, u.name AS author_name, (m.author_user_id = $2) AS mine
         FROM helpdesk_message m JOIN app_user u ON u.id = m.author_user_id WHERE m.ticket_id = $1 AND (NOT m.internal OR $3) ORDER BY m.created_at`,
      [t.id, req.user!.id, role === 'agent']);
    const ids = [...new Set(msgs.rows.flatMap((m) => m.file_ids))];
    const files = ids.length ? (await db.query('SELECT id, filename, mime, bytes FROM stored_file WHERE id = ANY($1) AND company_id = $2 AND deleted_at IS NULL', [ids, t.company_id])).rows : [];
    const policy = await helpdeskPolicy(db, t.company_id);
    const canReopen = role === 'requester' && t.status === 'resolved' && !!t.resolved_at && (Date.now() - new Date(t.resolved_at).getTime()) <= policy.reopenDays * 86_400_000;
    res.json({
      ticket: { ...t, owner_user_id: role === 'agent' ? t.owner_user_id : undefined, overdue: ['open', 'in_progress', 'waiting'].includes(t.status) && new Date(t.sla_due_at).getTime() < Date.now() },
      role, messages: msgs.rows, files, canReopen,
    });
  });

  r.post('/tickets/:id/messages', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    const b = z.object({ body: z.string().trim().min(1).max(5000), internal: z.boolean().default(false), fileIds }).parse(req.body);
    if (b.internal && role !== 'agent') throw new ForbiddenError('only the help desk team can add internal notes');
    if (t.status === 'closed') throw new ConflictError('this ticket is closed; raise a new ticket instead');
    const policy = await helpdeskPolicy(db, t.company_id);
    if (t.status === 'resolved' && role === 'requester') {
      const within = t.resolved_at && Date.now() - new Date(t.resolved_at).getTime() <= policy.reopenDays * 86_400_000;
      if (!within) throw new ConflictError('this ticket was resolved too long ago; raise a new ticket instead');
    }
    const id = await tx(async (c) => {
      const ins = await c.query<{ id: string }>('INSERT INTO helpdesk_message (ticket_id, company_id, author_user_id, body, internal, file_ids) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
        [t.id, t.company_id, req.user!.id, b.body, b.internal, b.fileIds]);
      await attach(c, t.company_id, req.user!.id, ins.rows[0]!.id, b.fileIds);
      if (role === 'agent' && !b.internal) {
        // A public answer from the team counts as the first response and moves a new ticket along.
        await c.query(`UPDATE helpdesk_ticket SET first_response_at = coalesce(first_response_at, now()), status = CASE WHEN status = 'open' THEN 'in_progress' ELSE status END, updated_at = now() WHERE id = $1`, [t.id]);
        if (t.requester_user_id !== req.user!.id) {
          await notify(c, { companyId: t.company_id, userId: t.requester_user_id, template: 'helpdesk.ticket.reply', title: `Reply on ticket #${t.number}`, body: b.body.slice(0, 200), payload: { ticketId: t.id }, channels: ['push'] });
        }
      } else if (role === 'requester') {
        // The requester answering (or writing again after a resolution) puts the ticket back in the team's hands.
        await c.query(`UPDATE helpdesk_ticket SET status = CASE WHEN status IN ('waiting', 'resolved') THEN 'in_progress' ELSE status END, resolved_at = CASE WHEN status = 'resolved' THEN NULL ELSE resolved_at END, updated_at = now() WHERE id = $1`, [t.id]);
        for (const userId of await handlers(c, t)) {
          if (userId !== req.user!.id) await notify(c, { companyId: t.company_id, userId, template: 'helpdesk.ticket.reply', title: `New message on ticket #${t.number}`, body: t.confidential ? 'Open the help desk to read it.' : b.body.slice(0, 200), payload: { ticketId: t.id } });
        }
      } else {
        await c.query('UPDATE helpdesk_ticket SET updated_at = now() WHERE id = $1', [t.id]);
      }
      return ins.rows[0]!.id;
    });
    res.status(201).json({ id });
  });

  r.patch('/tickets/:id', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    if (role !== 'agent') throw new ForbiddenError('only the help desk team can change a ticket');
    const b = z.object({
      status: z.enum(['open', 'in_progress', 'waiting', 'resolved', 'closed']).optional(), priority: z.enum(PRIORITIES).optional(),
      assigneeUserId: z.uuid().nullable().optional(), categoryId: z.uuid().optional(),
    }).parse(req.body);
    const hr = hasRole(req.user!, 'hr');
    if ((b.assigneeUserId !== undefined || b.categoryId !== undefined) && !hr) throw new ForbiddenError('only HR can reassign or move a ticket');
    let confidential = t.confidential;
    if (b.categoryId) {
      const c = await db.query<{ confidential: boolean }>('SELECT confidential FROM helpdesk_category WHERE id = $1 AND company_id = $2', [b.categoryId, t.company_id]);
      if (!c.rows[0]) throw new BadRequestError('category not found');
      confidential = c.rows[0].confidential;
    }
    const newAssignee = b.assigneeUserId === undefined ? t.assignee_user_id : b.assigneeUserId;
    if (newAssignee) {
      const u = await db.query<{ roles: string[] }>(`SELECT roles FROM app_user WHERE id = $1 AND company_id = $2 AND status = 'active'`, [newAssignee, t.company_id]);
      if (!u.rows[0]) throw new BadRequestError('the assignee must be an active user in your company');
      if (confidential && !u.rows[0].roles.some((x) => x === 'hr' || x === 'admin')) throw new BadRequestError('confidential tickets can only be assigned to HR');
      if (newAssignee === t.requester_user_id) throw new BadRequestError('a ticket cannot be assigned to the person who raised it');
    }
    const priority = b.priority ?? t.priority;
    const due = b.priority && b.priority !== t.priority ? await slaDue(t.company_id, priority, new Date(t.created_at)) : null;
    await tx(async (c) => {
      await c.query(
        `UPDATE helpdesk_ticket SET status = coalesce($2, status), priority = $3, assignee_user_id = $4, category_id = coalesce($5, category_id),
           sla_due_at = coalesce($6, sla_due_at), breached_at = CASE WHEN $6::timestamptz IS NOT NULL AND $6::timestamptz > now() THEN NULL ELSE breached_at END,
           resolved_at = CASE WHEN $2 = 'resolved' AND status <> 'resolved' THEN now() WHEN $2 IN ('open', 'in_progress', 'waiting') THEN NULL ELSE resolved_at END,
           closed_at = CASE WHEN $2 = 'closed' THEN now() WHEN $2 IS NOT NULL THEN NULL ELSE closed_at END,
           updated_at = now() WHERE id = $1`,
        [t.id, b.status ?? null, priority, newAssignee, b.categoryId ?? null, due]);
      await audit(c, { actorId: req.user!.id, action: 'update', table: 'helpdesk_ticket', rowId: t.id, before: { status: t.status, priority: t.priority, assignee: t.assignee_user_id }, after: b });
      if (b.assigneeUserId && b.assigneeUserId !== t.assignee_user_id && b.assigneeUserId !== req.user!.id) {
        await notify(c, { companyId: t.company_id, userId: b.assigneeUserId, template: 'helpdesk.ticket.assigned', title: `Ticket #${t.number} assigned to you`, body: confidential ? 'A confidential ticket. Open the help desk to read it.' : t.subject, payload: { ticketId: t.id }, channels: ['push'] });
      }
      if (b.status === 'resolved' && t.status !== 'resolved' && t.requester_user_id !== req.user!.id) {
        await notify(c, { companyId: t.company_id, userId: t.requester_user_id, template: 'helpdesk.ticket.resolved', title: `Ticket #${t.number} resolved`, body: 'Tell us how we did, or reply if you still need help.', payload: { ticketId: t.id }, channels: ['push'] });
      }
    });
    res.json({ ok: true });
  });

  r.post('/tickets/:id/reopen', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    if (role !== 'requester' && !hasRole(req.user!, 'hr')) throw new ForbiddenError('only the person who raised the ticket can reopen it');
    if (t.status !== 'resolved') throw new ConflictError(t.status === 'closed' ? 'this ticket is closed; raise a new ticket instead' : 'this ticket is still open');
    const policy = await helpdeskPolicy(db, t.company_id);
    if (role === 'requester' && (!t.resolved_at || Date.now() - new Date(t.resolved_at).getTime() > policy.reopenDays * 86_400_000)) throw new ConflictError(`tickets can be reopened within ${policy.reopenDays} days of being resolved`);
    await tx(async (c) => {
      await c.query(`UPDATE helpdesk_ticket SET status = 'in_progress', resolved_at = NULL, rating = NULL, rating_comment = NULL, updated_at = now() WHERE id = $1`, [t.id]);
      for (const userId of await handlers(c, t)) if (userId !== req.user!.id) await notify(c, { companyId: t.company_id, userId, template: 'helpdesk.ticket.reopened', title: `Ticket #${t.number} reopened`, body: t.confidential ? 'Open the help desk to read it.' : t.subject, payload: { ticketId: t.id } });
      await audit(c, { actorId: req.user!.id, action: 'reopen', table: 'helpdesk_ticket', rowId: t.id });
    });
    res.json({ ok: true });
  });

  r.post('/tickets/:id/close', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    if (role === 'requester' && !['resolved', 'open', 'in_progress', 'waiting'].includes(t.status)) throw new ConflictError('this ticket is already closed');
    if (t.status === 'closed') throw new ConflictError('this ticket is already closed');
    await db.query(`UPDATE helpdesk_ticket SET status = 'closed', closed_at = now(), resolved_at = coalesce(resolved_at, now()), updated_at = now() WHERE id = $1`, [t.id]);
    await audit(db, { actorId: req.user!.id, action: 'close', table: 'helpdesk_ticket', rowId: t.id });
    res.json({ ok: true });
  });

  r.post('/tickets/:id/rate', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    if (t.requester_user_id !== req.user!.id) throw new ForbiddenError('only the person who raised the ticket can rate it');
    void role;
    const b = z.object({ rating: z.number().int().min(1).max(5), comment: z.string().trim().max(1000).nullish() }).parse(req.body);
    if (!['resolved', 'closed'].includes(t.status)) throw new ConflictError('you can rate a ticket once it is resolved');
    if (t.rating !== null) throw new ConflictError('you have already rated this ticket');
    await db.query('UPDATE helpdesk_ticket SET rating = $2, rating_comment = $3, updated_at = now() WHERE id = $1', [t.id, b.rating, b.comment ?? null]);
    if (b.rating <= 2) {
      const targets = new Set([...(t.assignee_user_id ? [t.assignee_user_id] : []), ...(await userIdsWithRole(db, t.company_id, 'hr'))]);
      for (const userId of targets) await notify(db, { companyId: t.company_id, userId, template: 'helpdesk.ticket.low_rating', title: `Ticket #${t.number} was rated ${b.rating} of 5`, body: b.comment ?? 'No comment.', payload: { ticketId: t.id } });
    }
    res.json({ ok: true });
  });

  r.get('/tickets/:id/files/:fileId', async (req, res) => {
    const { t, role } = await loadTicket(req, uuid.parse(req.params.id));
    const fileId = uuid.parse(req.params.fileId);
    const m = await db.query('SELECT 1 FROM helpdesk_message WHERE ticket_id = $1 AND $2 = ANY(file_ids) AND (NOT internal OR $3) LIMIT 1', [t.id, fileId, role === 'agent']);
    if (!m.rows.length) throw new NotFoundError('file not found');
    sendStoredFile(res, await readFile(db, cipher, t.company_id, fileId), { download: req.query.download === '1' });
  });

  return r;
}
