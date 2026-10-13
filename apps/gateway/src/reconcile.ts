/**
 * Nightly reconciliation (plan §7.7, §15.3): ask each ADMS terminal to re-send yesterday's ATTLOG so the
 * idempotent ingest can fill any gap; Hikvision terminals are reconciled by AcsEvent search from the API side
 * (Appendix C) once a device-management credential store exists.
 *
 *   pnpm --filter @hajiri/gateway reconcile -- --all            # yesterday, every active ADMS device
 *   pnpm --filter @hajiri/gateway reconcile -- --sn DEMO-01 --date 2026-09-21
 * Run from cron at 01:00 NPT or from the API worker.
 */
import { localDateOf } from '@hajiri/attendance-engine';
import { loadConfig } from './config.js';
import { PostgresStore } from './store/postgres.js';
import type { Store } from './store/types.js';

export interface ReconcileResult { sn: string; date: string; stored: number; commandId: number | null; note: string }

export async function reconcileDay(store: Store, sn: string, date: string): Promise<ReconcileResult> {
  const device = await store.getDevice(sn);
  if (!device) return { sn, date, stored: 0, commandId: null, note: 'unknown device' };
  const stored = await store.countPunches(device.id, date);
  if (device.status !== 'active') return { sn, date, stored, commandId: null, note: `device ${device.status}` };
  if (device.protocol !== 'adms') return { sn, date, stored, commandId: null, note: 'reconciliation for this protocol runs from the API' };
  const commandId = await store.enqueueCommand(device.id, `DATA QUERY ATTLOG StartTime=${date} 00:00:00\tEndTime=${date} 23:59:59`);
  return { sn, date, stored, commandId, note: 'query queued; re-sent records are deduplicated on ingest' };
}

const isMain = process.argv[1]?.endsWith('reconcile.ts') || process.argv[1]?.endsWith('reconcile.js');
if (isMain) {
  const args = process.argv.slice(2);
  const opt = (n: string) => { const i = args.indexOf(`--${n}`); return i >= 0 ? args[i + 1] : undefined; };
  const cfg = loadConfig();
  if (!cfg.DATABASE_URL) { console.error('DATABASE_URL required'); process.exit(1); }
  const store = new PostgresStore(cfg.DATABASE_URL, { allowDangerousCommands: cfg.GATEWAY_ALLOW_DANGEROUS_COMMANDS });
  const date = opt('date') ?? localDateOf(Date.now() - 86_400_000);
  const sns = args.includes('--all') ? (await store.listDevices({ status: 'active', protocol: 'adms' })).map((d) => d.serialNo) : [opt('sn') ?? ''];
  for (const sn of sns.filter(Boolean)) console.log(JSON.stringify(await reconcileDay(store, sn, date)));
  await store.end();
}
