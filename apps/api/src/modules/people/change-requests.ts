import type { FieldCipher } from '../../crypto.js';
import type { Queryable } from '../../db/pool.js';
import { ConflictError, ForbiddenError, NotFoundError } from '../../errors.js';
import { notify, userIdForEmployee } from '../notifications/service.js';
import { registerHandler, registerSummariser, startWorkflow } from '../workflow/service.js';
import { applySection, diffSection, SECTION_LABEL, type Section } from './profile.js';

/**
 * Profile change requests. An employee proposes a new value for one profile section (optionally with proof); HR
 * approves it through the approval inbox and it is applied with field history. With people.policy.makerChecker on,
 * HR's own edits of bank and ID details become requests too, and a different HR person must approve them.
 */
export const PROFILE_CHANGE = 'profile_change';

export interface ChangeRequestRow {
  id: string; company_id: string; employee_id: string; requested_by: string; section: Section; summary: { changes?: Array<{ label: string; from: string | null; to: string | null }> };
  reason: string | null; proof_file_id: string | null; status: 'pending' | 'approved' | 'rejected' | 'cancelled';
  decided_by: string | null; decided_at: Date | null; decision_reason: string | null; created_at: Date;
}

const aad = (companyId: string, employeeId: string) => `profile_change:${companyId}:${employeeId}`;

export function decryptPayload(cipher: FieldCipher, row: { company_id: string; employee_id: string; payload_enc: Buffer }): Record<string, unknown> {
  return JSON.parse(cipher.decrypt(row.payload_enc, aad(row.company_id, row.employee_id))) as Record<string, unknown>;
}

export async function createChangeRequest(db: Queryable, cipher: FieldCipher, r: {
  companyId: string; employeeId: string; requestedBy: string; section: Section; value: Record<string, unknown>; reason?: string | null; proofFileId?: string | null; byHr: boolean;
}): Promise<ChangeRequestRow> {
  const changes = await diffSection(db, cipher, r.employeeId, r.section, r.value);
  if (!changes.length) throw new ConflictError('nothing would change; the profile already has these details');
  const open = await db.query(`SELECT 1 FROM profile_change_request WHERE employee_id = $1 AND section = $2 AND status = 'pending'`, [r.employeeId, r.section]);
  if (open.rows[0]) throw new ConflictError(`a change to ${SECTION_LABEL[r.section].toLowerCase()} is already waiting for approval`);
  const ins = await db.query<ChangeRequestRow>(
    `INSERT INTO profile_change_request (company_id, employee_id, requested_by, section, payload_enc, summary, reason, proof_file_id)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id, company_id, employee_id, requested_by, section, summary, reason, proof_file_id, status, decided_by, decided_at, decision_reason, created_at`,
    [r.companyId, r.employeeId, r.requestedBy, r.section, cipher.encrypt(JSON.stringify(r.value), aad(r.companyId, r.employeeId)),
      JSON.stringify({ changes, byHr: r.byHr }), r.reason ?? null, r.proofFileId ?? null]);
  const row = ins.rows[0]!;
  const emp = await db.query<{ name_en: string }>('SELECT name_en FROM employee WHERE id = $1', [r.employeeId]);
  await startWorkflow(db, {
    companyId: r.companyId, objectType: PROFILE_CHANGE, objectId: row.id, employeeId: r.employeeId,
    title: r.byHr ? 'Second approval needed' : 'Profile change to review',
    body: `${emp.rows[0]?.name_en ?? 'An employee'}: ${SECTION_LABEL[r.section].toLowerCase()} (${changes.map((c) => c.label).join(', ')})`,
  });
  return row;
}

/** Wire the decision handler and the inbox summary. Needs the cipher, so the router calls it once at start. */
export function registerProfileChangeWorkflow(cipher: FieldCipher): void {
  registerHandler(PROFILE_CHANGE, async (db, objectId, state, ctx) => {
    const r = await db.query<ChangeRequestRow & { payload_enc: Buffer }>('SELECT * FROM profile_change_request WHERE id = $1 FOR UPDATE', [objectId]);
    const row = r.rows[0];
    if (!row) throw new NotFoundError('change request not found');
    if (row.status !== 'pending') throw new ConflictError(`this change request is already ${row.status}`);
    // Maker-checker: whoever proposed a change can never be the one who approves it.
    if (state === 'approved' && row.requested_by === ctx.actorId) throw new ForbiddenError('a different HR person must approve this change');
    if (state === 'approved') {
      await applySection(db, cipher, { companyId: row.company_id, employeeId: row.employee_id, section: row.section, value: decryptPayload(cipher, row), actorId: ctx.actorId, source: 'change_request', refId: row.id });
    }
    await db.query(`UPDATE profile_change_request SET status = $2, decided_by = $3, decided_at = now(), decision_reason = $4 WHERE id = $1`,
      [row.id, state, ctx.actorId, ctx.reason]);
    const recipients = new Set<string>([row.requested_by]);
    const empUser = await userIdForEmployee(db, row.employee_id);
    if (empUser) recipients.add(empUser);
    recipients.delete(ctx.actorId);
    for (const userId of recipients) {
      await notify(db, {
        companyId: row.company_id, userId, template: `${PROFILE_CHANGE}.${state}`,
        title: state === 'approved' ? 'Profile change approved' : 'Profile change not approved',
        body: `${SECTION_LABEL[row.section]}${state === 'rejected' && ctx.reason ? `: ${ctx.reason}` : state === 'approved' ? ' updated.' : '.'}`,
        payload: { requestId: row.id },
      });
    }
  });
  registerSummariser(PROFILE_CHANGE, async (db, inst) => {
    const r = await db.query<ChangeRequestRow>('SELECT section, summary, reason, proof_file_id, requested_by FROM profile_change_request WHERE id = $1', [inst.object_id]);
    const row = r.rows[0];
    if (!row) return {};
    const by = await db.query<{ name: string }>('SELECT name FROM app_user WHERE id = $1', [row.requested_by]);
    return {
      section: row.section, sectionLabel: SECTION_LABEL[row.section], changes: row.summary.changes ?? [], reason: row.reason,
      hasProof: !!row.proof_file_id, requestedBy: by.rows[0]?.name ?? null, byHr: (row.summary as { byHr?: boolean }).byHr ?? false,
    };
  });
}
