import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';
import { localDateOf } from '@hajiri/attendance-engine';
import { purgeUnattachedFiles } from './modules/files/store.js';
import { runSurveyJobs } from './modules/engage/surveys.js';

export interface ModuleJobDeps { db: Db; cipher: FieldCipher }
export type ModuleJob = (deps: ModuleJobDeps, now: Date) => Promise<unknown>;

/**
 * Daily jobs of the HR modules (expiry alerts, reminders, clean-up). The worker runs each once a day after the nightly
 * sweep and reports the result under its name; a failing job is logged and never stops the others.
 */
export const DAILY_MODULE_JOBS: Record<string, ModuleJob> = {
  filesPurged: (d, now) => purgeUnattachedFiles(d.db, now),
  surveys: (d, now) => runSurveyJobs(d.db, d.cipher, localDateOf(now.getTime())),
};
