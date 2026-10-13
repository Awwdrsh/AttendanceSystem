import { Router } from 'express';
import { z } from 'zod';
import type { Db } from '../../db/pool.js';
import { listDays, recomputeDay } from './service.js';

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'YYYY-MM-DD');

export function attendanceRoutes(db: Db): Router {
  const r = Router();

  r.post('/recompute', async (req, res) => {
    const body = z.object({ employeeId: z.uuid(), localDate: isoDate }).parse(req.body);
    const result = await recomputeDay(db, body.employeeId, body.localDate);
    res.json(result);
  });

  r.get('/days', async (req, res) => {
    const q = z.object({ employeeId: z.uuid(), from: isoDate, to: isoDate }).parse(req.query);
    res.json({ days: await listDays(db, q.employeeId, q.from, q.to) });
  });

  return r;
}
