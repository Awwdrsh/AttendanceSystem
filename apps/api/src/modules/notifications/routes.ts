import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';

/** In-app inbox for the signed-in user (plan §13.10). */
export function notificationsRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/', async (req, res) => {
    const q = z.object({ limit: z.coerce.number().int().min(1).max(100).default(30), before: z.string().datetime().optional(), unreadOnly: z.coerce.boolean().optional() }).parse(req.query);
    const rows = await db.query(
      `SELECT id, template, title, body, payload, created_at, read_at FROM notification_outbox
        WHERE user_id = $1 AND channel = 'in_app' AND ($2::timestamptz IS NULL OR created_at < $2) AND (NOT $3 OR read_at IS NULL)
        ORDER BY created_at DESC LIMIT $4`, [req.user!.id, q.before ?? null, q.unreadOnly ?? false, q.limit]);
    const unread = await db.query<{ n: number }>(`SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = $1 AND channel = 'in_app' AND read_at IS NULL`, [req.user!.id]);
    res.json({ notifications: rows.rows, unread: unread.rows[0]!.n });
  });

  r.post('/read-all', async (req, res) => {
    const u = await db.query(`UPDATE notification_outbox SET read_at = now() WHERE user_id = $1 AND channel = 'in_app' AND read_at IS NULL`, [req.user!.id]);
    res.json({ marked: u.rowCount ?? 0 });
  });

  r.post('/:id/read', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const u = await db.query(`UPDATE notification_outbox SET read_at = coalesce(read_at, now()) WHERE id = $1 AND user_id = $2 RETURNING id, read_at`, [id, req.user!.id]);
    if (!u.rows[0]) return res.status(404).json({ error: 'notification not found' });
    return res.json(u.rows[0]);
  });

  return r;
}
