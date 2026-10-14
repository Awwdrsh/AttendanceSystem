import { Router } from 'express';
import { z } from 'zod';
import { recomputeQueuedFor } from '../attendance/service.js';
import type { AppDeps } from '../../deps.js';
import { NotFoundError } from '../../errors.js';
import { act, canView, getInstance, inboxFor, summarise, timeline } from './service.js';

/** Approvals inbox and one-tap decisions (plan §5.8, §13.5). */
export function workflowRoutes({ db }: AppDeps): Router {
  const r = Router();

  r.get('/inbox', async (req, res) => {
    res.json({ items: await inboxFor(db, req.user!) });
  });

  r.get('/:id', async (req, res) => {
    const id = z.uuid().parse(req.params.id);
    const inst = await getInstance(db, id);
    // Only the subject, their line manager, HR, current approvers and people who already acted may read it (leave reasons can be medical).
    if (!inst || inst.company_id !== req.user!.companyId || !(await canView(db, req.user!, inst))) throw new NotFoundError('approval not found');
    res.json({ instance: inst, summary: await summarise(db, inst), actions: await timeline(db, inst) });
  });

  const decide = (decision: 'approve' | 'reject') => async (req: import('express').Request, res: import('express').Response) => {
    const id = z.uuid().parse(req.params.id);
    const b = z.object({ reason: z.string().max(1000).optional(), approvedMin: z.number().int().min(0).max(240).optional(), onBehalfOf: z.uuid().optional() }).parse(req.body ?? {});
    // Other companies' approvals do not exist from this user's point of view (no IDOR probing via 403 vs 404).
    const existing = await getInstance(db, id);
    if (!existing || existing.company_id !== req.user!.companyId) throw new NotFoundError('approval not found');
    const client = await db.connect();
    try {
      await client.query('BEGIN');
      const state = await act(client, {
        instanceId: id, user: req.user!, decision, reason: b.reason ?? null, onBehalfOf: b.onBehalfOf ?? null,
        payload: b.approvedMin !== undefined ? { approvedMin: b.approvedMin } : {},
      });
      await client.query('COMMIT');
      // An approval that changes attendance (leave, correction, work from home, overtime) shows up immediately.
      if (existing.employee_id) await recomputeQueuedFor(db, existing.employee_id);
      res.json({ state });
    } catch (e) { await client.query('ROLLBACK'); throw e; } finally { client.release(); }
  };
  r.post('/:id/approve', decide('approve'));
  r.post('/:id/reject', decide('reject'));

  r.post('/bulk-approve', async (req, res) => {
    const b = z.object({ ids: z.array(z.uuid()).min(1).max(200) }).parse(req.body);
    const results: Array<{ id: string; state?: string; error?: string }> = [];
    for (const id of b.ids) {
      const existing = await getInstance(db, id);
      if (!existing || existing.company_id !== req.user!.companyId) { results.push({ id, error: 'approval not found' }); continue; }
      const client = await db.connect();
      try {
        await client.query('BEGIN');
        const state = await act(client, { instanceId: id, user: req.user!, decision: 'approve' });
        await client.query('COMMIT');
        if (existing.employee_id) await recomputeQueuedFor(db, existing.employee_id);
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
