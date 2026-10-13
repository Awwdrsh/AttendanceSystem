import { Router } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { NotFoundError } from '../../errors.js';
import { act, getInstance, inboxFor, summarise, timeline } from './service.js';

/** Approvals inbox and one-tap decisions (plan §5.8, §13.5). */
export function workflowRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/inbox', async (req, res) => {
    res.json({ items: await inboxFor(db, req.user!) });
  });

  r.get('/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const inst = await getInstance(db, id);
    if (!inst || inst.company_id !== req.user!.companyId) throw new NotFoundError('approval not found');
    res.json({ instance: inst, summary: await summarise(db, inst), actions: await timeline(db, inst) });
  });

  const decide = (decision: 'approve' | 'reject') => async (req: import('express').Request, res: import('express').Response) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ reason: z.string().max(1000).optional(), approvedMin: z.number().int().min(0).max(240).optional(), onBehalfOf: z.uuid().optional() }).parse(req.body ?? {});
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const state = await act(client, {
        instanceId: id, user: req.user!, decision, reason: b.reason ?? null, onBehalfOf: b.onBehalfOf ?? null,
        payload: b.approvedMin !== undefined ? { approvedMin: b.approvedMin } : {},
      });
      await client.query('COMMIT');
      res.json({ state });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  };
  r.post('/:id/approve', decide('approve'));
  r.post('/:id/reject', decide('reject'));

  r.post('/bulk-approve', async (req, res) => {
    const b = z.object({ ids: z.array(z.uuid()).min(1).max(200) }).parse(req.body);
    const results: Array<{ id: string; state?: string; error?: string }> = [];
    for (const id of b.ids) {
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const state = await act(client, { instanceId: id, user: req.user!, decision: 'approve' });
        await client.query('COMMIT');
        results.push({ id, state });
      } catch (e) {
        await client.query('ROLLBACK');
        results.push({ id, error: (e as Error).message });
      } finally { client.release(); }
    }
    res.json({ results });
  });

  return r;
}
