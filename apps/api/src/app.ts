import express, { type Express, type NextFunction, type Request, type Response } from 'express';
import helmet from 'helmet';
import { pinoHttp } from 'pino-http';
import { ZodError } from 'zod';
import type { AppDeps } from './deps.js';
import { HttpError } from './errors.js';
import { sessionLoader, requireAuth, requirePasswordChanged } from './modules/auth/middleware.js';
import { authRoutes } from './modules/auth/routes.js';
import { attendanceRoutes } from './modules/attendance/routes.js';
import { deviceRoutes } from './modules/devices/routes.js';
import { employeeRoutes } from './modules/employees/routes.js';
import { shiftsRoutes } from './modules/shifts/routes.js';
import { leaveRoutes } from './modules/leave/routes.js';
import { workflowRoutes } from './modules/workflow/routes.js';
import { regularisationRoutes } from './modules/regularisation/routes.js';
import { otRoutes } from './modules/ot/routes.js';
import { notificationsRoutes } from './modules/notifications/routes.js';
import { reportsRoutes } from './modules/reports/routes.js';
import { punchRoutes } from './modules/punch/routes.js';
import { settingsRoutes } from './modules/settings/routes.js';
import { payrollRoutes } from './modules/payroll/routes.js';
import { biometricsRoutes } from './modules/biometrics/routes.js';
import { dashboardRoutes } from './modules/dashboard/routes.js';

export interface AppOptions {
  corsOrigins?: string[];
  webDist?: string;
}

export function createApp(deps: AppDeps, opts: AppOptions = {}): Express {
  const { db, logger } = deps;
  const app = express();
  app.disable('x-powered-by');
  app.set('trust proxy', 'loopback');
  app.use(opts.webDist
    ? helmet({
      contentSecurityPolicy: {
        directives: {
          defaultSrc: ["'self'"],
          scriptSrc: ["'self'"],
          styleSrc: ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com'],
          fontSrc: ["'self'", 'https://fonts.gstatic.com', 'data:'],
          imgSrc: ["'self'", 'data:', 'blob:'],
          connectSrc: ["'self'"],
          frameAncestors: ["'none'"],
          objectSrc: ["'none'"],
        },
      },
    })
    : helmet({ contentSecurityPolicy: false }));
  if (logger) app.use(pinoHttp({ logger, autoLogging: { ignore: (req) => req.url === '/health' } }));
  app.use(express.json({ limit: '2mb' }));
  app.use(express.text({ type: 'text/csv', limit: '10mb' }));

  if (opts.corsOrigins?.length) {
    const allowed = new Set(opts.corsOrigins);
    app.use((req, res, next) => {
      const origin = req.headers.origin;
      if (origin && allowed.has(origin)) {
        res.setHeader('Access-Control-Allow-Origin', origin);
        res.setHeader('Vary', 'Origin');
        res.setHeader('Access-Control-Allow-Credentials', 'true');
        res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
        res.setHeader('Access-Control-Allow-Methods', 'GET,POST,PUT,PATCH,DELETE,OPTIONS');
      }
      if (req.method === 'OPTIONS') return res.sendStatus(204);
      return next();
    });
  }

  app.get('/health', async (_req, res) => {
    try { await db.query('SELECT 1'); res.json({ ok: true, db: 'up' }); }
    catch { res.status(503).json({ ok: false, db: 'down' }); }
  });

  app.use(sessionLoader(db));

  const api = express.Router();
  api.use('/auth', authRoutes(db, { secureCookie: deps.secureCookie, ...(deps.pepper ? { pepper: deps.pepper } : {}) }));
  api.use(requireAuth);                       // everything below needs a session; roles are checked per route
  api.use(requirePasswordChanged);            // accounts flagged must_change_password can only use /auth
  api.use('/employees', employeeRoutes(deps));
  api.use('/attendance', attendanceRoutes(deps));
  api.use('/devices', deviceRoutes(deps));
  api.use('/shifts', shiftsRoutes(deps));
  api.use('/leave', leaveRoutes(deps));
  api.use('/workflow', workflowRoutes(deps));
  api.use('/regularisation', regularisationRoutes(deps));
  api.use('/ot', otRoutes(deps));
  api.use('/notifications', notificationsRoutes(deps));
  api.use('/reports', reportsRoutes(deps));
  api.use('/punch', punchRoutes(deps));
  api.use('/settings', settingsRoutes(deps));
  api.use('/payroll', payrollRoutes(deps));
  api.use('/biometrics', biometricsRoutes(deps));
  api.use('/dashboard', dashboardRoutes(deps));
  app.use('/api/v1', api);

  if (opts.webDist) {
    app.use(express.static(opts.webDist, { index: false, maxAge: '1h' }));
    app.get(/^\/(?!api\/|health).*/, (_req, res) => res.sendFile('index.html', { root: opts.webDist }));
  }

  app.use((_req, res) => res.status(404).json({ error: 'not found' }));

  app.use((err: unknown, req: Request, res: Response, _next: NextFunction) => {
    if (err instanceof ZodError) return res.status(400).json({ error: 'validation', issues: err.issues.map((i) => ({ path: i.path.join('.'), message: i.message })) });
    if (err instanceof HttpError) return res.status(err.status).json({ error: err.message, ...(err.details !== undefined ? { details: err.details } : {}) });
    const e = err as { type?: string; status?: number; code?: string; message?: string; constraint?: string; detail?: string };
    if (e.type === 'entity.parse.failed') return res.status(400).json({ error: 'invalid JSON body' });
    if (e.type === 'entity.too.large') return res.status(413).json({ error: 'request body too large' });
    switch (e.code) {
      case '23505': return res.status(409).json({ error: 'already exists', constraint: e.constraint });
      case '23P01': return res.status(409).json({ error: 'overlaps an existing period', constraint: e.constraint });
      case '23503': return res.status(400).json({ error: 'referenced record does not exist', constraint: e.constraint });
      case '23514': return res.status(400).json({ error: 'value not allowed', constraint: e.constraint });
      case '22P02': case '22007': case '22008': return res.status(400).json({ error: 'invalid value format' });
      case 'P0001': return res.status(409).json({ error: e.message?.replace(/^error: /, '') ?? 'operation not allowed' });
    }
    (logger ?? console).error({ err, path: req.path }, 'unhandled error');
    return res.status(500).json({ error: 'internal error' });
  });

  return app;
}
