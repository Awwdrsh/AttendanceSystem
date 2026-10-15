import type { FieldCipher } from './crypto.js';
import type { Db } from './db/pool.js';
import { documentsDailyJob } from './modules/documents/routes.js';
import { purgeUnattachedFiles } from './modules/files/store.js';
import { dailyTimeJobs } from './modules/timesheets/service.js';
import { remindTravelSettlements } from './modules/expenses/jobs.js';
import { closeStaleBreaks } from './modules/field/jobs.js';
import { helpdeskDaily } from './modules/helpdesk/service.js';
import { lifecycleDaily } from './modules/lifecycle/service.js';
import { purgeRejectedCandidates } from './modules/hiring/service.js';
import { applyDueCareerEvents } from './modules/people/career.js';
import { performanceReminders } from './modules/performance/jobs.js';

export interface ModuleJobDeps { db: Db; cipher: FieldCipher }
export type ModuleJob = (deps: ModuleJobDeps, now: Date) => Promise<unknown>;

/**
 * Daily jobs of the HR modules (expiry alerts, reminders, clean-up). The worker runs each once a day after the nightly
 * sweep and reports the result under its name; a failing job is logged and never stops the others.
 */
export const DAILY_MODULE_JOBS: Record<string, ModuleJob> = {
  filesPurged: (d, now) => purgeUnattachedFiles(d.db, now),
  timesheets: (d, now) => dailyTimeJobs(d.db, now),
  travelSettlementReminders: (d, now) => remindTravelSettlements(d.db, now),
  staleBreaksClosed: (d, now) => closeStaleBreaks(d.db, now),
  documents: (d, now) => documentsDailyJob(d.db, now),
  lifecycle: (d, now) => lifecycleDaily(d.db, now),
  helpdesk: (d, now) => helpdeskDaily(d.db, now),
  candidatesPurged: (d, now) => purgeRejectedCandidates(d.db, now),
  careerEventsApplied: (d, now) => applyDueCareerEvents(d.db, now),
  performanceReminders: (d, now) => performanceReminders(d.db, now),
};
