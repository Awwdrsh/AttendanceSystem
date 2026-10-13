import type { Logger } from 'pino';
import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';

export interface WorkerDeps { db: Db; logger: Logger; cipher: FieldCipher }

/**
 * Background worker (plan §10.3): drains recompute_queue, runs the nightly 3-day sweep, accrues leave on BS month end,
 * delivers notifications, detects offline devices. TODO: implemented by the Phase 2 module work.
 */
export function startWorker(deps: WorkerDeps): { stop: () => void } {
  deps.logger.info('worker: not implemented yet');
  return { stop: () => undefined };
}
