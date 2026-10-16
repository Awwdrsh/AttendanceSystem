import { Router, type Request } from 'express';
import { z } from 'zod';
import type { AppDeps } from '../../deps.js';
import { BadRequestError } from '../../errors.js';
import { companyIdOf, requireModule } from '../auth/middleware.js';
import { toCsv } from '../reports/csv.js';
import { nptToday } from '../workflow/service.js';
import { computeInsights, DATASETS } from './service.js';

const filterQuery = z.object({
  from: z.string().regex(/^\d{4}-\d{1,2}$/).optional(),
  to: z.string().regex(/^\d{4}-\d{1,2}$/).optional(),
  branchId: z.uuid().optional(),
  orgUnitId: z.uuid().optional(),
});

/** Workforce insights for HR: headcount, attrition, demographics, people cost, overtime, leave and attendance trends. */
export function insightsRoutes({ db }: AppDeps): Router {
  const r = Router();
  r.use(requireModule('insights'));

  const filters = async (req: Request) => {
    const q = filterQuery.parse(req.query);
    const companyId = companyIdOf(req);
    if (q.branchId && !(await db.query('SELECT 1 FROM branch WHERE id = $1 AND company_id = $2', [q.branchId, companyId])).rows.length) throw new BadRequestError('unknown branch');
    if (q.orgUnitId && !(await db.query('SELECT 1 FROM org_unit WHERE id = $1 AND company_id = $2', [q.orgUnitId, companyId])).rows.length) throw new BadRequestError('unknown department');
    return { companyId, from: q.from, to: q.to, branchId: q.branchId ?? null, orgUnitId: q.orgUnitId ?? null, today: nptToday() };
  };

  /** Branches and departments for the filter row. */
  r.get('/filters', async (req, res) => {
    const companyId = companyIdOf(req);
    const [branches, units] = await Promise.all([
      db.query('SELECT id, name_en AS name FROM branch WHERE company_id = $1 ORDER BY name_en', [companyId]),
      db.query('SELECT id, name_en AS name, kind, parent_id AS "parentId" FROM org_unit WHERE company_id = $1 ORDER BY name_en', [companyId]),
    ]);
    res.json({ branches: branches.rows, orgUnits: units.rows });
  });

  r.get('/', async (req, res) => {
    res.json(await computeInsights(db, await filters(req)));
  });

  r.get('/export/:dataset', async (req, res) => {
    const name = String(req.params.dataset);
    const ds = Object.hasOwn(DATASETS, name) ? DATASETS[name] : undefined;
    if (!ds) throw new BadRequestError(`unknown dataset; use one of ${Object.keys(DATASETS).join(', ')}`);
    const data = await computeInsights(db, await filters(req));
    res.setHeader('Content-Type', 'text/csv; charset=utf-8');
    res.setHeader('Content-Disposition', `attachment; filename="insights-${ds.file}.csv"`);
    res.send(toCsv(ds.columns, ds.rows(data)));
  });

  return r;
}
