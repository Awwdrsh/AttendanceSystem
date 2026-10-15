import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';
import { purgeUnattachedFiles } from './modules/files/store.js';
import { remindTravelSettlements } from './modules/expenses/jobs.js';

export interface ModuleJobDeps { db: Db; cipher: FieldCipher }
export type ModuleJob = (deps: ModuleJobDeps, now: Date) => Promise<unknown>;

/**
 * Daily jobs of the HR modules (expiry alerts, reminders, clean-up). The worker runs each once a day after the nightly
 * sweep and reports the result under its name; a failing job is logged and never stops the others.
 */
export const DAILY_MODULE_JOBS: Record<string, ModuleJob> = {
  filesPurged: (d, now) => purgeUnattachedFiles(d.db, now),
  travelSettlementReminders: (d, now) => remindTravelSettlements(d.db, now),
};
