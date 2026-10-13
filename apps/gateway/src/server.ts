import pino from 'pino';
import { createGatewayApp } from './app.js';
import { loadConfig } from './config.js';
import { Signer } from './punch/signer.js';
import { MemoryStore } from './store/memory.js';
import { PostgresStore } from './store/postgres.js';

const cfg = loadConfig();
const logger = pino({ level: cfg.LOG_LEVEL });
const signer = cfg.GATEWAY_SIGNING_KEY ? Signer.fromBase64Pkcs8(cfg.GATEWAY_SIGNING_KEY) : Signer.ephemeral();
if (signer.ephemeral) logger.warn('GATEWAY_SIGNING_KEY not set: using an ephemeral signing key (dev only)');

let store: MemoryStore | PostgresStore;
if (cfg.DATABASE_URL) {
  store = new PostgresStore(cfg.DATABASE_URL);
} else {
  const mem = new MemoryStore();
  const d = mem.addDevice({ serialNo: 'DEMO-01' });
  mem.mappings.set(`${d.id}:1`, 'demo-employee-1');
  logger.warn('DATABASE_URL not set: using in-memory store with device DEMO-01 (PIN 1 mapped)');
  store = mem;
}

const app = createGatewayApp({ store, signer, logger });
const server = app.listen(cfg.PORT, () => logger.info({ port: cfg.PORT }, 'hajiri device gateway listening'));

for (const sig of ['SIGINT', 'SIGTERM'] as const) {
  process.on(sig, () => server.close(() => { const p = store instanceof PostgresStore ? store.end() : Promise.resolve(); p.then(() => process.exit(0)); }));
}
