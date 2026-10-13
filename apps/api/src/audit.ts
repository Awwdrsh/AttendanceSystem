import type { Queryable } from './db/pool.js';

export interface AuditEvent {
  actorId?: string | null;
  action: string;        // create | update | delete | approve | reject | login | lock | ...
  table: string;
  rowId?: string | null;
  before?: unknown;
  after?: unknown;
}

/** Append to the hash-chained audit log (plan §12.8). Call inside the same transaction as the mutation. */
export async function audit(db: Queryable, e: AuditEvent): Promise<void> {
  await db.query(
    'INSERT INTO audit_log (actor_id, action, table_name, row_id, before, after) VALUES ($1, $2, $3, $4, $5, $6)',
    [e.actorId ?? null, e.action, e.table, e.rowId ?? null,
      e.before === undefined ? null : JSON.stringify(e.before), e.after === undefined ? null : JSON.stringify(e.after)]);
}
