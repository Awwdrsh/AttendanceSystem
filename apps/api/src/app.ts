import express, { type Express, type NextFunction, type Request, type Response } from 'express';
import helmet from 'helmet';
import { pinoHttp } from 'pino-http';
import type { Logger } from 'pino';
import { ZodError } from 'zod';
import type { Db } from './db/pool.js';
import { attendanceRoutes } from './modules/attendance/routes.js';
import { NotFoundError } from './modules/attendance/service.js';
import { deviceRoutes } from './modules/devices/routes.js';
import { employeeRoutes } from './modules/employees/routes.js';

export function createApp(db: Db, logger?: Logger): Express {
  const app = express();
  app.disable('x-powered-by');
  app.use(helmet());
  if (logger) app.use(pinoHttp({ logger }));
  app.use(express.json({ limit: '1mb' }));

  app.get('/health', async (_req, res) => {
    try {
      await db.query('SELECT 1');
      res.json({ ok: true, db: 'up' });
    } catch {
      res.status(503).json({ ok: false, db: 'down' });
    }
  });

  app.use('/api/v1/employees', employeeRoutes(db));
  app.use('/api/v1/attendance', attendanceRoutes(db));
  app.use('/api/v1/devices', deviceRoutes(db));

  app.use((_req, res) => res.status(404).json({ error: 'not found' }));

  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    if (err instanceof ZodError) return res.status(400).json({ error: 'validation', issues: err.issues });
    if (err instanceof NotFoundError) return res.status(404).json({ error: err.message });
    (logger ?? console).error({ err, path: req.path }, 'unhandled error');
    return res.status(500).json({ error: 'internal error' });
  });

  return app;
}
