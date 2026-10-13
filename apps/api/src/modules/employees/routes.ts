import { Router } from 'express';
import { z } from 'zod';
import type { Db } from '../../db/pool.js';

export function employeeRoutes(db: Db): Router {
  const r = Router();

  r.get('/', async (req, res) => {
    const q = z.object({ companyId: z.uuid() }).parse(req.query);
    const rows = await db.query(
      `SELECT id, emp_code, name_en, name_ne, gender, ot_eligible, weekly_off_days, status
         FROM employee WHERE company_id = $1 ORDER BY emp_code`, [q.companyId]);
    res.json({ employees: rows.rows });
  });

  r.post('/', async (req, res) => {
    const b = z.object({
      companyId: z.uuid(), empCode: z.string().min(1).max(32), nameEn: z.string().min(1), nameNe: z.string().optional(),
      gender: z.enum(['male', 'female', 'other']).optional(), otEligible: z.boolean().default(true),
      weeklyOffDays: z.array(z.number().int().min(0).max(6)).default([6]),
    }).parse(req.body);
    const rows = await db.query(
      `INSERT INTO employee (company_id, emp_code, name_en, name_ne, gender, ot_eligible, weekly_off_days)
       VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id, emp_code, name_en, name_ne, gender, ot_eligible, weekly_off_days, status`,
      [b.companyId, b.empCode, b.nameEn, b.nameNe ?? null, b.gender ?? null, b.otEligible, b.weeklyOffDays]);
    res.status(201).json(rows.rows[0]);
  });

  return r;
}
