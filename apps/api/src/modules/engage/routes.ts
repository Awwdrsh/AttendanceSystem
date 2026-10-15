import { adToBs, bsMonthBounds } from '@hajiri/bs-calendar';
import { Router, type Request } from 'express';
import { z } from 'zod';
import { audit } from '../../audit.js';
import type { AppDeps } from '../../deps.js';
import { BadRequestError, ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { isoDate, uuid } from '../../validation.js';
import { companyIdOf, hasRole, requireRole } from '../auth/middleware.js';
import { notify } from '../notifications/service.js';
import { csvCell } from '../reports/csv.js';
import { nptToday } from '../workflow/service.js';
import {
  audienceMembers, audienceSql, audienceTargetExists, celebrationTargetOk, celebrations, ensureDefaultValues, inTx, isHr, placementOf, type AudienceKind,
} from './service.js';
import {
  answerSchema, checkAnswers, isOpen, notifyOpened, questionSchema, questionsOf, respondentKey, surveyResults, type SurveyRow,
} from './surveys.js';

const REACTIONS = ['like', 'love', 'clap', 'celebrate', 'laugh'] as const;
type TargetType = 'kudos' | 'announcement' | 'celebration';
const targetType = z.enum(['kudos', 'announcement', 'celebration']);

interface AnnouncementRow {
  id: string; company_id: string; author_id: string; author_employee_id: string | null; title: string; body: string; audience_kind: AudienceKind;
  audience_id: string | null; pinned: boolean; requires_ack: boolean; expires_on: string | null; created_at: Date; updated_at: Date; deleted_at: Date | null;
}

const announcementBody = z.object({
  title: z.string().trim().min(1).max(160),
  body: z.string().trim().min(1).max(8000),
  audienceKind: z.enum(['all', 'branch', 'department', 'reports']).default('all'),
  audienceId: z.uuid().nullish(),
  pinned: z.boolean().default(false),
  requiresAck: z.boolean().default(false),
  expiresOn: isoDate.nullish(),
});
const announcementPatch = z.object({
  title: z.string().trim().min(1).max(160).optional(),
  body: z.string().trim().min(1).max(8000).optional(),
  pinned: z.boolean().optional(),
  requiresAck: z.boolean().optional(),
  expiresOn: isoDate.nullish(),
}).strict();

const surveyBody = z.object({
  kind: z.enum(['survey', 'poll']).default('survey'),
  title: z.string().trim().min(1).max(160),
  description: z.string().trim().max(2000).nullish(),
  anonymous: z.boolean().default(true),
  minGroup: z.number().int().min(3).max(50).default(5),
  audienceKind: z.enum(['all', 'branch', 'department']).default('all'),
  audienceId: z.uuid().nullish(),
  opensOn: isoDate,
  closesOn: isoDate,
  recurrence: z.enum(['none', 'weekly', 'monthly']).default('none'),
  questions: z.array(questionSchema).min(1).max(30),
}).superRefine((s, ctx) => {
  if (s.closesOn < s.opensOn) ctx.addIssue({ code: 'custom', message: 'the closing day must be on or after the opening day', path: ['closesOn'] });
  if (s.kind === 'poll' && (s.questions.length !== 1 || !['single', 'multi'].includes(s.questions[0]!.kind))) {
    ctx.addIssue({ code: 'custom', message: 'a poll has exactly one choice question', path: ['questions'] });
  }
});

/** BS month that contains an AD date, as AD bounds. */
function bsMonthOf(ad: string): { year: number; month: number; startAd: string; endAd: string } {
  const b = adToBs(ad);
  const m = bsMonthBounds(b.year, b.month);
  return { year: b.year, month: b.month, startAd: m.startAd, endAd: m.endAd };
}

/** In-memory guard against flooding the anonymous suggestion box (identity is never stored with a suggestion). */
const suggestionHits = new Map<string, number[]>();
function suggestionAllowed(userId: string, now = Date.now()): boolean {
  const recent = (suggestionHits.get(userId) ?? []).filter((t) => now - t < 86_400_000);
  if (recent.length >= 5) { suggestionHits.set(userId, recent); return false; }
  recent.push(now);
  suggestionHits.set(userId, recent);
  return true;
}

type SurveyListRow = SurveyRow & { responses?: number; question_count?: number };
function publicSurvey(s: SurveyListRow) {
  return {
    id: s.id, kind: s.kind, title: s.title, description: s.description, anonymous: s.anonymous, minGroup: s.min_group, audienceKind: s.audience_kind,
    audienceId: s.audience_id, status: s.status, opensOn: s.opens_on, closesOn: s.closes_on, recurrence: s.recurrence, seriesId: s.series_id,
    createdAt: s.created_at, publishedAt: s.published_at, closedAt: s.closed_at,
    ...(s.responses !== undefined ? { responses: s.responses } : {}), ...(s.question_count !== undefined ? { questionCount: s.question_count } : {}),
  };
}

/**
 * Engagement: announcements, kudos, celebrations, surveys and polls, and the anonymous suggestion box. Everyone in the
 * company reads the feed; posting announcements is for HR (anyone) and managers (their own team); surveys are run by HR.
 */
export function engageRoutes({ db, cipher }: AppDeps): Router {
  const r = Router();
  const me = (req: Request) => ({ user: req.user!, companyId: companyIdOf(req) });

  // ---------------------------------------------------------------- reactions and comments (shared)

  async function reactionSummary(companyId: string, userId: string, type: TargetType, ids: string[]) {
    const counts = new Map<string, Record<string, number>>();
    const mine = new Map<string, string[]>();
    const comments = new Map<string, number>();
    if (!ids.length) return { counts, mine, comments };
    const rr = await db.query<{ target_id: string; emoji: string; n: number; mine: boolean }>(
      `SELECT target_id, emoji, count(*)::int AS n, bool_or(user_id = $4) AS mine FROM engage_reaction
        WHERE company_id = $1 AND target_type = $2 AND target_id = ANY($3::text[]) GROUP BY target_id, emoji`, [companyId, type, ids, userId]);
    for (const x of rr.rows) {
      counts.set(x.target_id, { ...(counts.get(x.target_id) ?? {}), [x.emoji]: x.n });
      if (x.mine) mine.set(x.target_id, [...(mine.get(x.target_id) ?? []), x.emoji]);
    }
    const cc = await db.query<{ target_id: string; n: number }>(
      `SELECT target_id, count(*)::int AS n FROM engage_comment
        WHERE company_id = $1 AND target_type = $2 AND target_id = ANY($3::text[]) AND deleted_at IS NULL GROUP BY target_id`, [companyId, type, ids]);
    for (const x of cc.rows) comments.set(x.target_id, x.n);
    return { counts, mine, comments };
  }

  async function visibleAnnouncement(req: Request, id: string): Promise<AnnouncementRow | null> {
    const { user, companyId } = me(req);
    const params: unknown[] = [id, companyId, user.id];
    const cond = audienceSql('a', await placementOf(db, user.employeeId), params, true);
    const a = await db.query<AnnouncementRow>(
      `SELECT a.* FROM announcement a WHERE a.id = $1 AND a.company_id = $2 AND a.deleted_at IS NULL
          AND (${isHr(user) ? 'true' : 'false'} OR a.author_id = $3 OR ${cond})`, params);
    return a.rows[0] ?? null;
  }

  /** May this user see (and so react to or comment on) the target? */
  async function canSeeTarget(req: Request, type: TargetType, id: string): Promise<boolean> {
    const { companyId } = me(req);
    if (type === 'celebration') return celebrationTargetOk(db, companyId, id);
    if (!/^[0-9a-f-]{36}$/.test(id)) return false;
    if (type === 'kudos') return (await db.query('SELECT 1 FROM kudos WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL', [id, companyId])).rows.length > 0;
    return (await visibleAnnouncement(req, id)) !== null;
  }

  r.post('/reactions', async (req, res) => {
    const b = z.object({ targetType, targetId: z.string().min(1).max(120), emoji: z.enum(REACTIONS) }).parse(req.body);
    const { user, companyId } = me(req);
    if (!(await canSeeTarget(req, b.targetType, b.targetId))) throw new NotFoundError('not found');
    const del = await db.query('DELETE FROM engage_reaction WHERE company_id = $1 AND target_type = $2 AND target_id = $3 AND user_id = $4 AND emoji = $5',
      [companyId, b.targetType, b.targetId, user.id, b.emoji]);
    if (!del.rowCount) {
      await db.query('INSERT INTO engage_reaction (company_id, target_type, target_id, user_id, emoji) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING',
        [companyId, b.targetType, b.targetId, user.id, b.emoji]);
    }
    const s = await reactionSummary(companyId, user.id, b.targetType, [b.targetId]);
    res.json({ reacted: !del.rowCount, reactions: s.counts.get(b.targetId) ?? {}, mine: s.mine.get(b.targetId) ?? [] });
  });

  r.get('/comments', async (req, res) => {
    const q = z.object({ targetType, targetId: z.string().min(1).max(120) }).parse(req.query);
    const { user, companyId } = me(req);
    if (!(await canSeeTarget(req, q.targetType, q.targetId))) throw new NotFoundError('not found');
    const c = await db.query<{ id: string; body: string; created_at: Date; user_id: string; name: string }>(
      `SELECT c.id, c.body, c.created_at, c.user_id, coalesce(e.name_en, u.name) AS name
         FROM engage_comment c JOIN app_user u ON u.id = c.user_id LEFT JOIN employee e ON e.id = u.employee_id
        WHERE c.company_id = $1 AND c.target_type = $2 AND c.target_id = $3 AND c.deleted_at IS NULL ORDER BY c.created_at LIMIT 200`,
      [companyId, q.targetType, q.targetId]);
    res.json({ comments: c.rows.map((x) => ({ id: x.id, body: x.body, createdAt: x.created_at, name: x.name, canDelete: x.user_id === user.id || isHr(user) })) });
  });

  r.post('/comments', async (req, res) => {
    const b = z.object({ targetType, targetId: z.string().min(1).max(120), body: z.string().trim().min(1).max(1000) }).parse(req.body);
    const { user, companyId } = me(req);
    if (!(await canSeeTarget(req, b.targetType, b.targetId))) throw new NotFoundError('not found');
    const c = await db.query<{ id: string }>('INSERT INTO engage_comment (company_id, target_type, target_id, user_id, body) VALUES ($1, $2, $3, $4, $5) RETURNING id',
      [companyId, b.targetType, b.targetId, user.id, b.body]);
    // Tell the people a kudos or celebration is about that someone wrote to them.
    const recipients: string[] = [];
    if (b.targetType === 'kudos') {
      const k = await db.query<{ user_id: string }>(
        `SELECT u.id AS user_id FROM kudos_recipient kr JOIN app_user u ON u.employee_id = kr.employee_id AND u.status = 'active' WHERE kr.kudos_id = $1
         UNION SELECT giver_id FROM kudos WHERE id = $1`, [b.targetId]);
      recipients.push(...k.rows.map((x) => x.user_id));
    } else if (b.targetType === 'celebration') {
      const u = await db.query<{ id: string }>(`SELECT id FROM app_user WHERE employee_id = $1 AND status = 'active'`, [b.targetId.split(':')[1]]);
      recipients.push(...u.rows.map((x) => x.id));
    }
    for (const uid of new Set(recipients)) {
      if (uid === user.id) continue;
      await notify(db, { companyId, userId: uid, template: 'engage.comment', title: b.targetType === 'celebration' ? 'Someone sent you a wish' : 'New comment on kudos',
        body: `${user.name}: ${b.body.slice(0, 140)}`, payload: { targetType: b.targetType, targetId: b.targetId } });
    }
    res.status(201).json({ id: c.rows[0]!.id });
  });

  r.delete('/comments/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const c = await db.query<{ user_id: string }>('SELECT user_id FROM engage_comment WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL', [id, companyId]);
    if (!c.rows[0]) throw new NotFoundError('comment not found');
    if (c.rows[0].user_id !== user.id && !isHr(user)) throw new ForbiddenError('you can only remove your own comments');
    await db.query('UPDATE engage_comment SET deleted_at = now() WHERE id = $1', [id]);
    if (c.rows[0].user_id !== user.id) await audit(db, { actorId: user.id, action: 'delete', table: 'engage_comment', rowId: id });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- announcements

  async function announcementsFor(req: Request, opts: { before?: Date; limit: number; pinnedOnly?: boolean; includeExpired?: boolean }) {
    const { user, companyId } = me(req);
    const params: unknown[] = [companyId, user.id, opts.before ?? new Date(Date.now() + 60_000), opts.limit];
    const cond = audienceSql('a', await placementOf(db, user.employeeId), params, true);
    const rows = await db.query<AnnouncementRow & { author_name: string; acked: boolean; audience_name: string | null }>(
      `SELECT a.*, coalesce(e.name_en, u.name) AS author_name,
              EXISTS (SELECT 1 FROM announcement_ack k WHERE k.announcement_id = a.id AND k.user_id = $2) AS acked,
              coalesce(b.name_en, o.name_en) AS audience_name
         FROM announcement a JOIN app_user u ON u.id = a.author_id LEFT JOIN employee e ON e.id = a.author_employee_id
         LEFT JOIN branch b ON a.audience_kind = 'branch' AND b.id = a.audience_id
         LEFT JOIN org_unit o ON a.audience_kind = 'department' AND o.id = a.audience_id
        WHERE a.company_id = $1 AND a.deleted_at IS NULL AND a.created_at < $3
          ${opts.includeExpired ? '' : 'AND (a.expires_on IS NULL OR a.expires_on >= current_date)'}
          ${opts.pinnedOnly ? 'AND a.pinned' : ''}
          AND (${opts.includeExpired ? 'true' : 'false'} OR a.author_id = $2 OR ${cond})
        ORDER BY a.created_at DESC LIMIT $4`, params);
    const s = await reactionSummary(companyId, user.id, 'announcement', rows.rows.map((x) => x.id));
    return rows.rows.map((a) => ({
      type: 'announcement' as const, id: a.id, createdAt: a.created_at, title: a.title, body: a.body, authorName: a.author_name,
      audienceKind: a.audience_kind, audienceId: a.audience_id,
      audienceLabel: a.audience_kind === 'all' ? 'Everyone' : a.audience_kind === 'reports' ? `${a.author_name}'s team` : a.audience_name ?? '—',
      pinned: a.pinned, requiresAck: a.requires_ack, acked: a.acked, expiresOn: a.expires_on,
      reactions: s.counts.get(a.id) ?? {}, mine: s.mine.get(a.id) ?? [], comments: s.comments.get(a.id) ?? 0,
      canEdit: a.author_id === user.id || isHr(user),
    }));
  }

  r.get('/announcements', async (req, res) => {
    const q = z.object({ before: z.iso.datetime().optional(), limit: z.coerce.number().int().min(1).max(50).default(20), all: z.enum(['1', '0']).optional() }).parse(req.query);
    // HR may list every announcement (including expired ones and other teams') to manage them.
    const all = q.all === '1' && isHr(req.user!);
    res.json({ announcements: await announcementsFor(req, { ...(q.before ? { before: new Date(q.before) } : {}), limit: q.limit, includeExpired: all }) });
  });

  r.post('/announcements', async (req, res) => {
    const b = announcementBody.parse(req.body);
    const { user, companyId } = me(req);
    if (!hasRole(user, 'hr', 'manager')) throw new ForbiddenError('only HR and managers post announcements');
    if (!isHr(user) && b.audienceKind !== 'reports') throw new ForbiddenError('managers post announcements to their own team');
    if (b.audienceKind === 'reports' && !user.employeeId) throw new BadRequestError('this account has no team');
    if (!(await audienceTargetExists(db, companyId, b.audienceKind, b.audienceId))) throw new BadRequestError('pick the branch or department this is for');
    if (b.expiresOn && b.expiresOn < nptToday()) throw new BadRequestError('the end date has already passed');
    const audienceId = b.audienceKind === 'all' || b.audienceKind === 'reports' ? null : b.audienceId ?? null;
    const out = await inTx(db, async (c) => {
      const a = await c.query<{ id: string }>(
        `INSERT INTO announcement (company_id, author_id, author_employee_id, title, body, audience_kind, audience_id, pinned, requires_ack, expires_on)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id`,
        [companyId, user.id, user.employeeId, b.title, b.body, b.audienceKind, audienceId, b.pinned && isHr(user), b.requiresAck, b.expiresOn ?? null]);
      const id = a.rows[0]!.id;
      const members = await audienceMembers(c, companyId, b.audienceKind, audienceId, user.employeeId);
      let notified = 0;
      for (const m of members) {
        if (!m.user_id || m.user_id === user.id) continue;
        await notify(c, { companyId, userId: m.user_id, employeeId: m.employee_id, template: 'engage.announcement',
          title: b.requiresAck ? `Please read: ${b.title}` : b.title, body: b.body.slice(0, 200), payload: { announcementId: id } });
        notified++;
      }
      await audit(c, { actorId: user.id, action: 'create', table: 'announcement', rowId: id, after: { title: b.title, audience: b.audienceKind } });
      return { id, notified };
    });
    res.status(201).json(out);
  });

  r.patch('/announcements/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = announcementPatch.parse(req.body);
    const { user } = me(req);
    const a = await visibleAnnouncement(req, id);
    if (!a) throw new NotFoundError('announcement not found');
    if (a.author_id !== user.id && !isHr(user)) throw new ForbiddenError('only the author or HR can edit this');
    await db.query(
      `UPDATE announcement SET title = coalesce($2, title), body = coalesce($3, body), pinned = coalesce($4, pinned),
              requires_ack = coalesce($5, requires_ack), expires_on = CASE WHEN $6::boolean THEN $7::date ELSE expires_on END, updated_at = now()
        WHERE id = $1`,
      [id, b.title ?? null, b.body ?? null, b.pinned === undefined ? null : b.pinned && isHr(user), b.requiresAck ?? null, b.expiresOn !== undefined, b.expiresOn ?? null]);
    await audit(db, { actorId: user.id, action: 'update', table: 'announcement', rowId: id, before: { title: a.title }, after: b });
    res.json({ ok: true });
  });

  r.delete('/announcements/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user } = me(req);
    const a = await visibleAnnouncement(req, id);
    if (!a) throw new NotFoundError('announcement not found');
    if (a.author_id !== user.id && !isHr(user)) throw new ForbiddenError('only the author or HR can remove this');
    await db.query('UPDATE announcement SET deleted_at = now() WHERE id = $1', [id]);
    await audit(db, { actorId: user.id, action: 'delete', table: 'announcement', rowId: id, before: { title: a.title } });
    res.json({ ok: true });
  });

  r.post('/announcements/:id/ack', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const a = await visibleAnnouncement(req, id);
    if (!a) throw new NotFoundError('announcement not found');
    if (!a.requires_ack) throw new BadRequestError('this announcement does not ask for confirmation');
    await db.query('INSERT INTO announcement_ack (announcement_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING', [id, req.user!.id]);
    res.json({ ok: true });
  });

  /** Who has and has not confirmed reading, for the author and HR. */
  r.get('/announcements/:id/acks', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const a = await visibleAnnouncement(req, id);
    if (!a) throw new NotFoundError('announcement not found');
    if (a.author_id !== user.id && !isHr(user)) throw new ForbiddenError('only the author or HR can see confirmations');
    const members = await audienceMembers(db, companyId, a.audience_kind, a.audience_id, a.author_employee_id);
    const acks = await db.query<{ user_id: string; acked_at: Date }>('SELECT user_id, acked_at FROM announcement_ack WHERE announcement_id = $1', [id]);
    const ackBy = new Map(acks.rows.map((x) => [x.user_id, x.acked_at]));
    const names = await db.query<{ id: string; name_en: string; emp_code: string }>('SELECT id, name_en, emp_code FROM employee WHERE id = ANY($1::uuid[])', [members.map((m) => m.employee_id)]);
    const nameOf = new Map(names.rows.map((n) => [n.id, n]));
    const people = members.filter((m) => m.user_id && m.user_id !== a.author_id).map((m) => ({
      employeeId: m.employee_id, name: nameOf.get(m.employee_id)?.name_en ?? '', empCode: nameOf.get(m.employee_id)?.emp_code ?? '',
      ackedAt: ackBy.get(m.user_id!) ?? null,
    })).sort((x, y) => Number(!!x.ackedAt) - Number(!!y.ackedAt) || x.name.localeCompare(y.name));
    res.json({ audience: people.length, acked: people.filter((p) => p.ackedAt).length, people });
  });

  // ---------------------------------------------------------------- company values and settings

  r.get('/values', async (req, res) => {
    const { companyId } = me(req);
    await ensureDefaultValues(db, companyId);
    const v = await db.query(`SELECT id, name, description, emoji, active, sort_order AS "sortOrder" FROM engage_value WHERE company_id = $1 ORDER BY sort_order, name`, [companyId]);
    const s = await db.query<{ kudos_monthly_limit: number | null }>('SELECT kudos_monthly_limit FROM engage_setting WHERE company_id = $1', [companyId]);
    res.json({ values: v.rows, kudosMonthlyLimit: s.rows[0]?.kudos_monthly_limit ?? null });
  });

  const valueBody = z.object({
    name: z.string().trim().min(1).max(40), description: z.string().trim().max(200).nullish(), emoji: z.string().trim().min(1).max(8).default('⭐'),
    active: z.boolean().default(true), sortOrder: z.number().int().min(0).max(999).default(0),
  });

  r.post('/values', requireRole('hr'), async (req, res) => {
    const b = valueBody.parse(req.body);
    const { user, companyId } = me(req);
    const v = await db.query<{ id: string }>('INSERT INTO engage_value (company_id, name, description, emoji, active, sort_order) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id',
      [companyId, b.name, b.description ?? null, b.emoji, b.active, b.sortOrder]);
    await audit(db, { actorId: user.id, action: 'create', table: 'engage_value', rowId: v.rows[0]!.id, after: b });
    res.status(201).json({ id: v.rows[0]!.id });
  });

  r.patch('/values/:id', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({
      name: z.string().trim().min(1).max(40).optional(), description: z.string().trim().max(200).nullish(), emoji: z.string().trim().min(1).max(8).optional(),
      active: z.boolean().optional(), sortOrder: z.number().int().min(0).max(999).optional(),
    }).parse(req.body);
    const { user, companyId } = me(req);
    const u = await db.query(
      `UPDATE engage_value SET name = coalesce($3, name), description = CASE WHEN $4::boolean THEN $5 ELSE description END, emoji = coalesce($6, emoji),
              active = coalesce($7, active), sort_order = coalesce($8, sort_order) WHERE id = $1 AND company_id = $2`,
      [id, companyId, b.name ?? null, b.description !== undefined, b.description ?? null, b.emoji ?? null, b.active ?? null, b.sortOrder ?? null]);
    if (!u.rowCount) throw new NotFoundError('value not found');
    await audit(db, { actorId: user.id, action: 'update', table: 'engage_value', rowId: id, after: b });
    res.json({ ok: true });
  });

  r.put('/settings', requireRole('hr'), async (req, res) => {
    const b = z.object({ kudosMonthlyLimit: z.number().int().min(1).max(100).nullable() }).parse(req.body);
    const { user, companyId } = me(req);
    await db.query(`INSERT INTO engage_setting (company_id, kudos_monthly_limit) VALUES ($1, $2)
                    ON CONFLICT (company_id) DO UPDATE SET kudos_monthly_limit = EXCLUDED.kudos_monthly_limit, updated_at = now()`, [companyId, b.kudosMonthlyLimit]);
    await audit(db, { actorId: user.id, action: 'update', table: 'engage_setting', rowId: companyId, after: b });
    res.json({ ok: true });
  });

  // ---------------------------------------------------------------- kudos

  /** Colleagues to pick from when giving kudos: name, code and designation only. */
  r.get('/colleagues', async (req, res) => {
    const { companyId } = me(req);
    const e = await db.query(
      `SELECT id, name_en AS name, emp_code AS "empCode", designation FROM employee WHERE company_id = $1 AND status = 'active' ORDER BY name_en`, [companyId]);
    res.json({ colleagues: e.rows });
  });

  async function kudosItems(companyId: string, userId: string, isHrUser: boolean, where: string, params: unknown[], limit: number) {
    const all = [companyId, ...params, limit];
    const k = await db.query<{ id: string; created_at: Date; message: string; giver_id: string; giver_employee_id: string; giver_name: string; giver_designation: string | null;
      value_id: string | null; value_name: string | null; value_emoji: string | null; recipients: Array<{ employeeId: string; name: string; designation: string | null }> | null }>(
      `SELECT k.id, k.created_at, k.message, k.giver_id, k.giver_employee_id, g.name_en AS giver_name, g.designation AS giver_designation,
              k.value_id, v.name AS value_name, v.emoji AS value_emoji,
              (SELECT json_agg(json_build_object('employeeId', e.id, 'name', e.name_en, 'designation', e.designation) ORDER BY e.name_en)
                 FROM kudos_recipient kr JOIN employee e ON e.id = kr.employee_id WHERE kr.kudos_id = k.id) AS recipients
         FROM kudos k JOIN employee g ON g.id = k.giver_employee_id LEFT JOIN engage_value v ON v.id = k.value_id
        WHERE k.company_id = $1 AND k.deleted_at IS NULL ${where}
        ORDER BY k.created_at DESC LIMIT $${all.length}`, all);
    const s = await reactionSummary(companyId, userId, 'kudos', k.rows.map((x) => x.id));
    return k.rows.map((x) => ({
      type: 'kudos' as const, id: x.id, createdAt: x.created_at, message: x.message,
      giver: { employeeId: x.giver_employee_id, name: x.giver_name, designation: x.giver_designation },
      recipients: x.recipients ?? [], value: x.value_id ? { id: x.value_id, name: x.value_name, emoji: x.value_emoji } : null,
      reactions: s.counts.get(x.id) ?? {}, mine: s.mine.get(x.id) ?? [], comments: s.comments.get(x.id) ?? 0,
      canDelete: x.giver_id === userId || isHrUser,
    }));
  }

  r.get('/kudos', async (req, res) => {
    const q = z.object({ before: z.iso.datetime().optional(), limit: z.coerce.number().int().min(1).max(50).default(20), employeeId: z.uuid().optional() }).parse(req.query);
    const { user, companyId } = me(req);
    const params: unknown[] = [q.before ? new Date(q.before) : new Date(Date.now() + 60_000)];
    let where = 'AND k.created_at < $2';
    if (q.employeeId) { params.push(q.employeeId); where += ' AND (k.giver_employee_id = $3 OR EXISTS (SELECT 1 FROM kudos_recipient kr WHERE kr.kudos_id = k.id AND kr.employee_id = $3))'; }
    res.json({ kudos: await kudosItems(companyId, user.id, isHr(user), where, params, q.limit) });
  });

  r.post('/kudos', async (req, res) => {
    const b = z.object({ recipientIds: z.array(z.uuid()).min(1).max(10), valueId: z.uuid().nullish(), message: z.string().trim().min(3).max(1000) }).parse(req.body);
    const { user, companyId } = me(req);
    const giver = user.employeeId;
    if (!giver) throw new ForbiddenError('only employees can give kudos');
    const recipients = [...new Set(b.recipientIds)];
    if (recipients.includes(giver)) throw new BadRequestError('You cannot give kudos to yourself.');
    const emps = await db.query<{ id: string }>(`SELECT id FROM employee WHERE id = ANY($1::uuid[]) AND company_id = $2 AND status = 'active'`, [recipients, companyId]);
    if (emps.rows.length !== recipients.length) throw new BadRequestError('Pick colleagues from your company.');
    if (b.valueId) {
      const v = await db.query('SELECT 1 FROM engage_value WHERE id = $1 AND company_id = $2 AND active', [b.valueId, companyId]);
      if (!v.rows.length) throw new BadRequestError('Pick one of the company values.');
    }
    const out = await inTx(db, async (c) => {
      // Serialise per giver so two quick clicks cannot both pass the monthly limit.
      await c.query('SELECT pg_advisory_xact_lock(hashtext($1))', [`kudos:${giver}`]);
      const lim = await c.query<{ kudos_monthly_limit: number | null }>('SELECT kudos_monthly_limit FROM engage_setting WHERE company_id = $1', [companyId]);
      const limit = lim.rows[0]?.kudos_monthly_limit ?? null;
      if (limit) {
        const m = bsMonthOf(nptToday());
        const used = await c.query<{ n: number }>(
          `SELECT count(*)::int AS n FROM kudos WHERE giver_employee_id = $1 AND deleted_at IS NULL AND (created_at AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN $2 AND $3`,
          [giver, m.startAd, m.endAd]);
        if (used.rows[0]!.n >= limit) throw new ConflictError(`You have given all ${limit} kudos for this month. You can give more next month.`);
      }
      const k = await c.query<{ id: string }>('INSERT INTO kudos (company_id, giver_id, giver_employee_id, value_id, message) VALUES ($1, $2, $3, $4, $5) RETURNING id',
        [companyId, user.id, giver, b.valueId ?? null, b.message]);
      const id = k.rows[0]!.id;
      for (const e of recipients) await c.query('INSERT INTO kudos_recipient (kudos_id, employee_id) VALUES ($1, $2)', [id, e]);
      const users = await c.query<{ id: string; employee_id: string }>(`SELECT id, employee_id FROM app_user WHERE employee_id = ANY($1::uuid[]) AND status = 'active'`, [recipients]);
      for (const u of users.rows) {
        await notify(c, { companyId, userId: u.id, employeeId: u.employee_id, template: 'engage.kudos', title: `${user.name} gave you kudos`,
          body: b.message.slice(0, 200), payload: { kudosId: id }, channels: ['push'] });
      }
      return { id };
    });
    res.status(201).json(out);
  });

  r.delete('/kudos/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const k = await db.query<{ giver_id: string }>('SELECT giver_id FROM kudos WHERE id = $1 AND company_id = $2 AND deleted_at IS NULL', [id, companyId]);
    if (!k.rows[0]) throw new NotFoundError('kudos not found');
    if (k.rows[0].giver_id !== user.id && !isHr(user)) throw new ForbiddenError('only the giver or HR can remove kudos');
    await db.query('UPDATE kudos SET deleted_at = now() WHERE id = $1', [id]);
    await audit(db, { actorId: user.id, action: 'delete', table: 'kudos', rowId: id });
    res.json({ ok: true });
  });

  /** Most appreciated people, most generous givers and value counts for a BS month (default: this month). */
  r.get('/leaderboard', async (req, res) => {
    const q = z.object({ bsYear: z.coerce.number().int().min(2000).max(2100).optional(), bsMonth: z.coerce.number().int().min(1).max(12).optional() }).parse(req.query);
    const { companyId } = me(req);
    const cur = bsMonthOf(nptToday());
    const year = q.bsYear ?? cur.year; const month = q.bsMonth ?? cur.month;
    let bounds: { startAd: string; endAd: string };
    try { bounds = bsMonthBounds(year, month); } catch { throw new BadRequestError('that month is outside the supported calendar'); }
    const params = [companyId, bounds.startAd, bounds.endAd];
    const inMonth = `k.company_id = $1 AND k.deleted_at IS NULL AND (k.created_at AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN $2 AND $3`;
    const [received, given, values, total] = await Promise.all([
      db.query(`SELECT e.id AS "employeeId", e.name_en AS name, e.designation, count(*)::int AS count
                  FROM kudos k JOIN kudos_recipient kr ON kr.kudos_id = k.id JOIN employee e ON e.id = kr.employee_id
                 WHERE ${inMonth} GROUP BY e.id ORDER BY count DESC, e.name_en LIMIT 10`, params),
      db.query(`SELECT e.id AS "employeeId", e.name_en AS name, e.designation, count(*)::int AS count
                  FROM kudos k JOIN employee e ON e.id = k.giver_employee_id WHERE ${inMonth} GROUP BY e.id ORDER BY count DESC, e.name_en LIMIT 10`, params),
      db.query(`SELECT v.id, v.name, v.emoji, count(k.id)::int AS count FROM engage_value v LEFT JOIN kudos k ON k.value_id = v.id AND ${inMonth}
                 WHERE v.company_id = $1 GROUP BY v.id ORDER BY count DESC, v.sort_order`, params),
      db.query<{ n: number }>(`SELECT count(*)::int AS n FROM kudos k WHERE ${inMonth}`, params),
    ]);
    res.json({ bsYear: year, bsMonth: month, total: total.rows[0]!.n, received: received.rows, given: given.rows, values: values.rows });
  });

  // ---------------------------------------------------------------- feed, celebrations, preferences, home card

  r.get('/feed', async (req, res) => {
    const q = z.object({ before: z.iso.datetime().optional(), limit: z.coerce.number().int().min(1).max(50).default(20) }).parse(req.query);
    const { user, companyId } = me(req);
    const before = q.before ? new Date(q.before) : new Date(Date.now() + 60_000);
    const [kudos, anns] = await Promise.all([
      kudosItems(companyId, user.id, isHr(user), 'AND k.created_at < $2', [before], q.limit),
      announcementsFor(req, { before, limit: q.limit }),
    ]);
    const items = [...kudos, ...anns].sort((a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()).slice(0, q.limit);
    res.json({ items, nextBefore: items.length === q.limit ? new Date(items[items.length - 1]!.createdAt).toISOString() : null });
  });

  r.get('/celebrations', async (req, res) => {
    const q = z.object({ days: z.coerce.number().int().min(1).max(60).default(14) }).parse(req.query);
    const { user, companyId } = me(req);
    const list = await celebrations(db, companyId, nptToday(), q.days);
    const s = await reactionSummary(companyId, user.id, 'celebration', list.map((c) => c.targetId));
    res.json({ celebrations: list.map((c) => ({ ...c, reactions: s.counts.get(c.targetId) ?? {}, mine: s.mine.get(c.targetId) ?? [], comments: s.comments.get(c.targetId) ?? 0, isMe: c.employeeId === user.employeeId })) });
  });

  r.get('/me/prefs', async (req, res) => {
    const { user } = me(req);
    if (!user.employeeId) return res.json({ showBirthday: false, hasBirthday: false });
    const p = await db.query<{ show_birthday: boolean; dob: string | null }>(
      'SELECT coalesce(p.show_birthday, false) AS show_birthday, e.dob FROM employee e LEFT JOIN engage_pref p ON p.employee_id = e.id WHERE e.id = $1', [user.employeeId]);
    return res.json({ showBirthday: p.rows[0]?.show_birthday ?? false, hasBirthday: !!p.rows[0]?.dob });
  });

  r.put('/me/prefs', async (req, res) => {
    const b = z.object({ showBirthday: z.boolean() }).parse(req.body);
    const { user } = me(req);
    if (!user.employeeId) throw new ForbiddenError('only employees have celebration settings');
    await db.query(`INSERT INTO engage_pref (employee_id, show_birthday) VALUES ($1, $2)
                    ON CONFLICT (employee_id) DO UPDATE SET show_birthday = EXCLUDED.show_birthday, updated_at = now()`, [user.employeeId, b.showBirthday]);
    res.json({ ok: true });
  });

  /** The Company card on Home: the latest pinned announcement, today's celebrations and the first survey waiting for an answer. */
  r.get('/home', async (req, res) => {
    const { companyId } = me(req);
    const [pinned, todays, surveys] = await Promise.all([
      announcementsFor(req, { limit: 1, pinnedOnly: true }),
      celebrations(db, companyId, nptToday(), 1),
      openSurveysFor(req),
    ]);
    const waiting = surveys.filter((s) => !s.answered);
    res.json({
      announcement: pinned[0] ?? null,
      celebrations: todays.filter((c) => c.daysAway === 0).map((c) => ({ kind: c.kind, name: c.name, employeeId: c.employeeId, years: c.years, targetId: c.targetId })),
      survey: waiting[0] ? { id: waiting[0].id, title: waiting[0].title, kind: waiting[0].kind, closesOn: waiting[0].closesOn } : null,
      surveysWaiting: waiting.length,
    });
  });

  // ---------------------------------------------------------------- surveys and polls

  async function surveyInCompany(id: string, companyId: string): Promise<SurveyRow> {
    const s = await db.query<SurveyRow>('SELECT * FROM survey WHERE id = $1 AND company_id = $2', [id, companyId]);
    if (!s.rows[0]) throw new NotFoundError('survey not found');
    return s.rows[0];
  }

  async function inAudience(req: Request, s: SurveyRow): Promise<boolean> {
    const { user } = me(req);
    if (!user.employeeId) return false;
    const params: unknown[] = [s.id, user.employeeId];
    const cond = audienceSql('s', await placementOf(db, user.employeeId), params, false);
    const x = await db.query(`SELECT 1 FROM survey s JOIN employee e ON e.id = $2 AND e.status = 'active' AND e.company_id = s.company_id WHERE s.id = $1 AND ${cond}`, params);
    return x.rows.length > 0;
  }

  async function hasAnswered(surveyId: string, userId: string): Promise<boolean> {
    return (await db.query('SELECT 1 FROM survey_response WHERE survey_id = $1 AND respondent_key = $2', [surveyId, respondentKey(cipher, surveyId, userId)])).rows.length > 0;
  }

  async function openSurveysFor(req: Request) {
    const { user, companyId } = me(req);
    if (!user.employeeId) return [];
    const params: unknown[] = [companyId, nptToday()];
    const cond = audienceSql('s', await placementOf(db, user.employeeId), params, false);
    const s = await db.query<SurveyRow & { question_count: number }>(
      `SELECT s.*, (SELECT count(*)::int FROM survey_question q WHERE q.survey_id = s.id) AS question_count FROM survey s
        WHERE s.company_id = $1 AND s.status = 'published' AND s.opens_on <= $2::date AND s.closes_on >= $2::date AND ${cond}
        ORDER BY s.closes_on, s.created_at`, params);
    const keys = s.rows.map((x) => respondentKey(cipher, x.id, user.id));
    const done = await db.query<{ survey_id: string }>('SELECT survey_id FROM survey_response WHERE respondent_key = ANY($1::text[])', [keys]);
    const answered = new Set(done.rows.map((d) => d.survey_id));
    return s.rows.map((x) => ({
      id: x.id, kind: x.kind, title: x.title, description: x.description, anonymous: x.anonymous, opensOn: x.opens_on, closesOn: x.closes_on,
      questionCount: x.question_count, answered: answered.has(x.id),
    }));
  }

  /** Surveys and polls open to me now, with whether I have answered. */
  r.get('/surveys', async (req, res) => {
    res.json({ surveys: await openSurveysFor(req) });
  });

  r.get('/surveys/manage', requireRole('hr'), async (req, res) => {
    const { companyId } = me(req);
    const s = await db.query<SurveyListRow>(
      `SELECT s.*, (SELECT count(*)::int FROM survey_response r WHERE r.survey_id = s.id) AS responses,
              (SELECT count(*)::int FROM survey_question q WHERE q.survey_id = s.id) AS question_count
         FROM survey s WHERE s.company_id = $1 ORDER BY s.created_at DESC LIMIT 200`, [companyId]);
    res.json({ surveys: s.rows.map(publicSurvey) });
  });

  /** One survey with its questions: HR any time, others only while it is open to them. */
  r.get('/surveys/:id', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (!isHr(user) && !(isOpen(s, nptToday()) && (await inAudience(req, s)))) throw new NotFoundError('survey not found');
    const questions = await questionsOf(db, id);
    res.json({
      survey: publicSurvey(s), answered: await hasAnswered(id, user.id),
      questions: questions.map((q) => ({ id: q.id, kind: q.kind, prompt: q.prompt, options: q.options, required: q.required })),
    });
  });

  async function writeQuestions(c: Parameters<Parameters<typeof inTx>[1]>[0], surveyId: string, questions: z.infer<typeof surveyBody>['questions']) {
    for (const [i, q] of questions.entries()) {
      await c.query('INSERT INTO survey_question (survey_id, position, kind, prompt, options, required) VALUES ($1, $2, $3, $4, $5, $6)',
        [surveyId, i + 1, q.kind, q.prompt, JSON.stringify(q.kind === 'single' || q.kind === 'multi' ? q.options : []), q.required]);
    }
  }

  r.post('/surveys', requireRole('hr'), async (req, res) => {
    const b = surveyBody.parse(req.body);
    const { user, companyId } = me(req);
    if (!(await audienceTargetExists(db, companyId, b.audienceKind, b.audienceId))) throw new BadRequestError('pick the branch or department this is for');
    const id = await inTx(db, async (c) => {
      const s = await c.query<{ id: string }>(
        `INSERT INTO survey (company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id, opens_on, closes_on, recurrence, created_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id`,
        [companyId, b.kind, b.title, b.description ?? null, b.anonymous, b.minGroup, b.audienceKind, b.audienceKind === 'all' ? null : b.audienceId,
          b.opensOn, b.closesOn, b.recurrence, user.id]);
      await writeQuestions(c, s.rows[0]!.id, b.questions);
      await audit(c, { actorId: user.id, action: 'create', table: 'survey', rowId: s.rows[0]!.id, after: { title: b.title, kind: b.kind, anonymous: b.anonymous } });
      return s.rows[0]!.id;
    });
    res.status(201).json({ id });
  });

  /** Drafts can be changed freely; once published only the closing day may move (questions and anonymity are fixed). */
  r.put('/surveys/:id', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (s.status === 'closed') throw new ConflictError('this survey is closed');
    if (s.status === 'published') {
      const b = z.object({ closesOn: isoDate }).strict().parse(req.body);
      if (b.closesOn < s.opens_on || b.closesOn < nptToday()) throw new BadRequestError('the new closing day must be today or later');
      await db.query('UPDATE survey SET closes_on = $2, reminded_at = NULL WHERE id = $1', [id, b.closesOn]);
      await audit(db, { actorId: user.id, action: 'update', table: 'survey', rowId: id, after: b });
      return res.json({ ok: true });
    }
    const b = surveyBody.parse(req.body);
    if (!(await audienceTargetExists(db, companyId, b.audienceKind, b.audienceId))) throw new BadRequestError('pick the branch or department this is for');
    await inTx(db, async (c) => {
      await c.query(
        `UPDATE survey SET kind = $2, title = $3, description = $4, anonymous = $5, min_group = $6, audience_kind = $7, audience_id = $8,
                opens_on = $9, closes_on = $10, recurrence = $11 WHERE id = $1`,
        [id, b.kind, b.title, b.description ?? null, b.anonymous, b.minGroup, b.audienceKind, b.audienceKind === 'all' ? null : b.audienceId, b.opensOn, b.closesOn, b.recurrence]);
      await c.query('DELETE FROM survey_question WHERE survey_id = $1', [id]);
      await writeQuestions(c, id, b.questions);
      await audit(c, { actorId: user.id, action: 'update', table: 'survey', rowId: id, after: { title: b.title } });
    });
    return res.json({ ok: true });
  });

  r.delete('/surveys/:id', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (s.status !== 'draft') throw new ConflictError('only drafts can be deleted; close a published survey instead');
    await inTx(db, async (c) => {
      await c.query('DELETE FROM survey_question WHERE survey_id = $1', [id]);
      await c.query('DELETE FROM survey WHERE id = $1', [id]);
      await audit(c, { actorId: user.id, action: 'delete', table: 'survey', rowId: id, before: { title: s.title } });
    });
    res.json({ ok: true });
  });

  r.post('/surveys/:id/publish', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (s.status !== 'draft') throw new ConflictError(`this survey is already ${s.status}`);
    if (s.closes_on < nptToday()) throw new BadRequestError('the closing day has passed; change the dates first');
    const out = await inTx(db, async (c) => {
      const u = await c.query<SurveyRow>(`UPDATE survey SET status = 'published', published_at = now() WHERE id = $1 AND status = 'draft' RETURNING *`, [id]);
      if (!u.rows[0]) throw new ConflictError('this survey was just published');
      const notified = u.rows[0].opens_on <= nptToday() ? await notifyOpened(c, u.rows[0]) : 0;
      await audit(c, { actorId: user.id, action: 'publish', table: 'survey', rowId: id });
      return { notified };
    });
    res.json(out);
  });

  r.post('/surveys/:id/close', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (s.status !== 'published') throw new ConflictError('only an open survey can be closed');
    // Closing by hand ends the series too: a recurring pulse stops here.
    await db.query(`UPDATE survey SET status = 'closed', closed_at = now(), recurrence = 'none' WHERE id = $1`, [id]);
    await audit(db, { actorId: user.id, action: 'close', table: 'survey', rowId: id });
    res.json({ ok: true });
  });

  r.post('/surveys/:id/respond', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ answers: z.array(answerSchema).max(30) }).parse(req.body);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (!isOpen(s, nptToday()) || !(await inAudience(req, s))) throw new NotFoundError('this survey is not open to you');
    const rows = checkAnswers(await questionsOf(db, id), b.answers);
    const p = await placementOf(db, user.employeeId);
    await inTx(db, async (c) => {
      const ins = await c.query<{ id: string }>(
        `INSERT INTO survey_response (survey_id, respondent_key, employee_id, org_unit_id, branch_id, submitted_on)
         VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (survey_id, respondent_key) DO NOTHING RETURNING id`,
        [id, respondentKey(cipher, id, user.id), s.anonymous ? null : user.employeeId, p.orgUnitId, p.branchId, nptToday()]);
      if (!ins.rows[0]) throw new ConflictError('You have already answered this.');
      for (const a of rows) {
        await c.query('INSERT INTO survey_answer (response_id, question_id, value_num, choices, text_value) VALUES ($1, $2, $3, $4, $5)',
          [ins.rows[0].id, a.questionId, a.value, a.choices, a.text]);
      }
    });
    // Polls show how everyone voted straight after voting (subject to the anonymity threshold).
    if (s.kind === 'poll') {
      const members = await audienceMembers(db, companyId, s.audience_kind, s.audience_id, null);
      return res.status(201).json({ ok: true, results: await surveyResults(db, s, members.length) });
    }
    return res.status(201).json({ ok: true });
  });

  /** Results: HR always; for polls, anyone who has voted. */
  r.get('/surveys/:id/results', async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { user, companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    if (!isHr(user) && (s.kind !== 'poll' || !(await hasAnswered(id, user.id)))) throw new NotFoundError('results are not available to you');
    const members = await audienceMembers(db, companyId, s.audience_kind, s.audience_id, null);
    res.json({ survey: publicSurvey(s), results: await surveyResults(db, s, members.length) });
  });

  /** Aggregated results as CSV (never one row per anonymous person). */
  r.get('/surveys/:id/results.csv', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const { companyId } = me(req);
    const s = await surveyInCompany(id, companyId);
    const members = await audienceMembers(db, companyId, s.audience_kind, s.audience_id, null);
    const out = await surveyResults(db, s, members.length);
    const lines: string[][] = [['Survey', s.title], ['Responses', String(out.responses)], ['Audience', String(out.audience)], ['Anonymous', s.anonymous ? 'Yes' : 'No'], []];
    if (out.hidden) lines.push([`Results are hidden until at least ${s.min_group} people answer.`]);
    lines.push(['Question', 'Type', 'Answer', 'Count', 'Average', 'eNPS', 'Name']);
    for (const q of out.questions) {
      for (const d of q.distribution ?? []) lines.push([q.prompt, q.kind, d.label, String(d.count), '', '', '']);
      if (q.average !== undefined) lines.push([q.prompt, q.kind, 'Average', '', q.average === null ? '' : String(q.average), q.enps ? String(q.enps.score) : '', '']);
      for (const g of q.breakdown ?? []) lines.push([q.prompt, q.kind, `Group: ${g.group}`, String(g.responses), g.average === null ? '' : String(g.average), '', '']);
      for (const t of q.texts ?? []) lines.push([q.prompt, q.kind, t.text, '', '', '', t.name ?? '']);
    }
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', 'attachment; filename="survey-results.csv"');
    res.send('﻿' + lines.map((l) => l.map(csvCell).join(',')).join('\r\n') + '\r\n');
  });

  // ---------------------------------------------------------------- anonymous suggestion box

  r.post('/suggestions', async (req, res) => {
    const b = z.object({ category: z.enum(['general', 'workplace', 'process', 'benefits', 'culture', 'other']).default('general'), body: z.string().trim().min(5).max(3000) }).parse(req.body);
    const { user, companyId } = me(req);
    if (!suggestionAllowed(user.id)) throw new ConflictError('You have sent several suggestions today. Please try again tomorrow.');
    // Deliberately no author and no time of day: only the company and the day it arrived.
    await db.query('INSERT INTO suggestion (company_id, category, body, received_on) VALUES ($1, $2, $3, $4)', [companyId, b.category, b.body, nptToday()]);
    res.status(201).json({ ok: true });
  });

  /** Answered suggestions HR chose to share with everyone. */
  r.get('/suggestions/public', async (req, res) => {
    const { companyId } = me(req);
    const s = await db.query(
      `SELECT id, category, body, received_on AS "receivedOn", reply, replied_at AS "repliedAt" FROM suggestion
        WHERE company_id = $1 AND is_public AND status = 'answered' ORDER BY replied_at DESC LIMIT 50`, [companyId]);
    res.json({ suggestions: s.rows });
  });

  r.get('/suggestions', requireRole('hr'), async (req, res) => {
    const q = z.object({ status: z.enum(['new', 'reviewed', 'answered', 'archived']).optional() }).parse(req.query);
    const { companyId } = me(req);
    const s = await db.query(
      `SELECT id, category, body, received_on AS "receivedOn", status, reply, is_public AS "isPublic", replied_at AS "repliedAt" FROM suggestion
        WHERE company_id = $1 AND ($2::text IS NULL OR status = $2) ORDER BY received_on DESC, status LIMIT 300`, [companyId, q.status ?? null]);
    res.json({ suggestions: s.rows });
  });

  r.patch('/suggestions/:id', requireRole('hr'), async (req, res) => {
    const id = uuid.parse(req.params.id);
    const b = z.object({ status: z.enum(['new', 'reviewed', 'answered', 'archived']).optional(), reply: z.string().trim().min(1).max(3000).optional(), isPublic: z.boolean().optional() }).parse(req.body);
    const { user, companyId } = me(req);
    const status = b.reply ? 'answered' : b.status ?? null;
    const u = await db.query(
      `UPDATE suggestion SET status = coalesce($3, status), reply = coalesce($4, reply), is_public = coalesce($5, is_public),
              replied_by = CASE WHEN $4::text IS NOT NULL THEN $6::uuid ELSE replied_by END,
              replied_at = CASE WHEN $4::text IS NOT NULL THEN now() ELSE replied_at END
        WHERE id = $1 AND company_id = $2`,
      [id, companyId, status, b.reply ?? null, b.isPublic ?? null, user.id]);
    if (!u.rowCount) throw new NotFoundError('suggestion not found');
    await audit(db, { actorId: user.id, action: 'update', table: 'suggestion', rowId: id, after: { status, isPublic: b.isPublic, replied: !!b.reply } });
    res.json({ ok: true });
  });

  return r;
}
