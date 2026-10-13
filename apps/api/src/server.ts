import pino from 'pino';
import { createApp } from './app.js';
import { loadConfig } from './config.js';
import { createPool } from './db/pool.js';

const cfg = loadConfig();
const logger = pino({ level: cfg.LOG_LEVEL });
const db = createPool(cfg.DATABASE_URL);
const app = createApp(db, logger);

const server = app.listen(cfg.PORT, () => logger.info({ port: cfg.PORT }, 'hajiri api listening'));

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, () => {
    logger.info({ sig }, 'shutting down');
    server.close(() => db.end().then(() => process.exit(0)));
  });
}
