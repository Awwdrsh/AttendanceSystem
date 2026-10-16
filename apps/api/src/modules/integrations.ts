import { onCandidateHired } from './hiring/service.js';
import { autoStartOnboarding } from './lifecycle/service.js';
import { recordCareerEvent } from './people/career.js';

/**
 * Links between modules that would otherwise import each other: a hired candidate gets a "Joined" event on their
 * career timeline and, when the company has auto-start on, an onboarding checklist. Runs in the hire transaction.
 */
let wired = false;
export function wireModules(): void {
  if (wired) return;
  wired = true;
  onCandidateHired(async (db, e) => {
    const emp = await db.query<{ designation: string | null }>('SELECT designation FROM employee WHERE id = $1', [e.employeeId]);
    const designation = emp.rows[0]?.designation;
    await recordCareerEvent(db, { companyId: e.companyId, employeeId: e.employeeId, kind: 'hire', effectiveDate: e.joinDate,
      details: designation ? { designation } : {}, note: 'Hired through the careers pipeline', actorId: e.actorId });
    await autoStartOnboarding(db, e.companyId, e.employeeId, e.joinDate, e.actorId);
  });
}
