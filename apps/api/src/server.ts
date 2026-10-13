import pino from 'pino';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { FieldCipher } from './crypto.js';
import { createPool } from './db/pool.js';
import { startWorker } from './worker.js';

const cfg = loadConfig();
const logger = pino({ level: cfg.LOG_LEVEL });
const db = createPool(cfg.DATABASE_URL);
const cipher = FieldCipher.fromBase64(cfg.DATA_ENCRYPTION_KEY);
const app = createApp({ db, cipher, pepper: cfg.PASSWORD_PEPPER, logger, secureCookie: cfg.COOKIE_SECURE }, {
  corsOrigins: cfg.CORS_ORIGINS.split(',').map((s) => s.trim()).filter(Boolean),
  ...(cfg.WEB_DIST ? { webDist: cfg.WEB_DIST } : {}),
});

const server = app.listen(cfg.PORT, () => logger.info({ port: cfg.PORT }, 'hajiri api listening'));
const worker = cfg.RUN_WORKER ? startWorker({ db, logger, cipher }) : null;

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, () => {
    logger.info({ sig }, 'shutting down');
    worker?.stop();
    server.close(() => db.end().then(() => process.exit(0)));
  });
}
