"""
Employee lifecycle engine (port of apps/api/src/modules/lifecycle/service.ts): per-company defaults, onboarding and
exit checklists, probation decisions, exits and the daily job.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.core.schema import Arr, Bool, Int, Obj, safe_parse, parse
from hajiri.notifications.service import notify, notify_role, user_id_for_employee, user_ids_with_role
from hajiri.people.career import log_career_event

# ---------------------------------------------------------------- policy
POLICY_SCHEMA = Obj({
    # Notice an employee gives when resigning, in calendar days.
    'noticeDays': Int(min=0, max=180, default=30),
    # Start the default onboarding checklist when HR adds an employee.
    'autoStartOnboarding': Bool(default=True),
    # Days before probation ends on which the manager is reminded.
    'probationReminderDays': Arr(Int(min=1, max=90), max=4, default=lambda: [14, 7]),
    # Let leavers fill in the exit interview themselves.
    'selfExitInterview': Bool(default=True),
})


def lifecycle_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'lifecycle.policy'", [company_id])
    ok, out = safe_parse(POLICY_SCHEMA, (r and r['value']) or {})
    return out if ok else parse(POLICY_SCHEMA, {})


# ---------------------------------------------------------------- defaults
ONBOARDING = [
    ('Prepare the appointment letter and contract', 'hr', -3, 'HR', None, None),
    ('Set up the workstation, laptop and email', 'manager', -1, 'IT', None, 'Ask IT for a laptop, an email address and access to the tools the role needs.'),
    ('Create the Hajiri sign-in and assign a shift', 'hr', 0, 'HR', '/settings/users', None),
    ('Enrol fingerprint or face on the attendance terminal', 'hr', 0, 'HR', None, None),
    ('Complete your profile and bank details', 'employee', 0, None, '/me/profile', 'Check your contact details, emergency contact and bank account.'),
    ('Upload your citizenship, PAN and academic certificates', 'employee', 1, None, '/me/profile/documents', None),
    ('Assign the salary structure and SSF registration', 'hr', 2, 'HR', '/payroll/assignments', None),
    ('Welcome meeting: introduce the team and the role', 'manager', 1, None, None, None),
    ('Read the employee handbook and leave policy', 'employee', 7, None, None, None),
    ('Agree goals for the probation period', 'manager', 14, None, '/me/growth', None),
    ('First-month check-in', 'manager', 30, None, None, None),
]

OFFBOARDING = [
    ('Hand over work, files and passwords', 'manager', -3, 'Manager', None, None),
    ('Exit interview', 'hr', -2, 'HR', None, None),
    ('Return laptop and devices; remove email and system access', 'hr', 0, 'IT', None, None),
    ('Clear advances, loans and other dues', 'hr', 0, 'Finance', '/payroll/loans', None),
    ('Return ID card, keys and SIM', 'hr', 0, 'Admin', None, None),
    ('Run the final settlement in payroll', 'hr', 3, 'HR', '/payroll', None),
    ('Issue the experience and relieving letter', 'hr', 5, 'HR', None, None),
]

EXIT_QUESTIONS = [
    ('What is the main reason you are leaving?', 'choice', ['Better pay', 'Career growth', 'Work-life balance', 'Manager or team', 'Relocation',
                                                            'Further studies', 'Health or family', 'Other']),
    ('How would you rate your overall experience here?', 'rating', []),
    ('How would you rate the support from your manager?', 'rating', []),
    ('What did you like most about working here?', 'text', []),
    ('What should we improve?', 'text', []),
    ('Would you recommend us as a place to work?', 'choice', ['Yes', 'Maybe', 'No']),
]

ASSET_TYPES = ['Laptop', 'Desktop', 'Mobile phone', 'SIM card', 'ID card', 'Keys', 'Vehicle', 'Other']


def ensure_lifecycle_defaults(company_id: str) -> bool:
    """Idempotent per-company defaults; the marker row makes concurrent first visits seed only once."""
    n = execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'lifecycle.seeded', '{"version":1}')
                   ON CONFLICT DO NOTHING""", [company_id])
    if not n:
        return False
    for kind, name, tasks in (('onboarding', 'Standard onboarding', ONBOARDING), ('offboarding', 'Standard exit clearance', OFFBOARDING)):
        t = one("""INSERT INTO lifecycle_template (company_id, kind, name, description, is_default) VALUES (%s, %s, %s, %s, true)
                   ON CONFLICT (company_id, kind, name) DO NOTHING RETURNING id""",
                [company_id, kind, name, 'Checklist for every new joiner. Due dates count from the join date.' if kind == 'onboarding'
                 else 'Clearance for every leaver. Due dates count from the last working day.'])
        if not t:
            continue
        for i, (title, assignee, offset, dept, link, description) in enumerate(tasks):
            execute("""INSERT INTO lifecycle_template_task (template_id, sort, title, description, department, assignee_type, due_offset_days, link)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""", [t['id'], i, title, description, dept, assignee, offset, link])
    for i, (q, kind, options) in enumerate(EXIT_QUESTIONS):
        execute('INSERT INTO exit_interview_question (company_id, sort, question, kind, options) VALUES (%s, %s, %s, %s, %s)',
                [company_id, i, q, kind, json.dumps(options)])
    for name in ASSET_TYPES:
        execute('INSERT INTO asset_type (company_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING', [company_id, name])
    return True


# ---------------------------------------------------------------- reminders
def remind_once(company_id: str, kind: str, ref_id: str, key: str) -> bool:
    """Record a reminder; False when it was already sent (callers skip the notification)."""
    return execute('INSERT INTO lifecycle_reminder (company_id, kind, ref_id, key) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
                   [company_id, kind, ref_id, key]) > 0


# ---------------------------------------------------------------- checklists
def task_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'case_id', 'company_id', 'employee_id', 'assignee_user_id', 'done_by'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    return out


def manager_user_of(employee_id: str) -> str | None:
    r = one("""SELECT u.id FROM employee_assignment a JOIN app_user u ON u.employee_id = a.manager_id AND u.status = 'active'
                WHERE a.employee_id = %s AND (a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date OR lower(a.valid) > (now() AT TIME ZONE 'Asia/Kathmandu')::date) ORDER BY lower(a.valid) LIMIT 1""",
            [employee_id])
    return str(r['id']) if r else None


def responsible_users(t: dict) -> list[str]:
    """Users who should act on a task: HR, the line manager (HR when there is none), the employee, or the named user."""
    kind = t['assignee_type']
    if kind == 'hr':
        return user_ids_with_role(t['company_id'], 'hr')
    if kind == 'manager':
        m = manager_user_of(t['employee_id'])
        return [m] if m else user_ids_with_role(t['company_id'], 'hr')
    if kind == 'employee':
        u = user_id_for_employee(t['employee_id'])
        return [u] if u else []
    return [str(t['assignee_user_id'])] if t.get('assignee_user_id') else user_ids_with_role(t['company_id'], 'hr')


def manages_now_or_soon(manager_employee_id: str, employee_id: str) -> bool:
    """The employee's current line manager, or the manager they will have once they join (pre-joining onboarding)."""
    return bool(one("""SELECT 1 AS x FROM employee_assignment WHERE employee_id = %s AND manager_id = %s
                        AND (valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date OR lower(valid) > (now() AT TIME ZONE 'Asia/Kathmandu')::date) LIMIT 1""", [employee_id, manager_employee_id]))


def can_complete_task(user: AuthUser, t: dict) -> bool:
    """HR may complete anything; otherwise only the person the task is for."""
    if user.company_id != str(t['company_id']):
        return False
    if has_role(user, 'hr'):
        return True
    kind = t['assignee_type']
    if kind == 'hr':
        return False
    if kind == 'employee':
        return user.employee_id == str(t['employee_id'])
    if kind == 'manager':
        return bool(user.employee_id) and manages_now_or_soon(user.employee_id, str(t['employee_id']))
    return str(t.get('assignee_user_id')) == user.id


def can_view_case(user: AuthUser, c: dict) -> bool:
    """HR, the employee, their line manager, and anyone with a task in the case may see it."""
    if user.company_id != str(c['company_id']):
        return False
    if has_role(user, 'hr'):
        return True
    if user.employee_id == str(c['employee_id']):
        return True
    if user.employee_id and has_role(user, 'manager') and manages_now_or_soon(user.employee_id, str(c['employee_id'])):
        return True
    return bool(one('SELECT 1 AS x FROM lifecycle_task WHERE case_id = %s AND assignee_type = %s AND assignee_user_id = %s LIMIT 1',
                    [c['id'], 'user', user.id]))


def start_case(*, company_id: str, employee_id: str, kind: str, anchor_date, started_by: str | None,
               template_id: str | None = None, exit_id: str | None = None) -> str:
    """
    Open a checklist for an employee from a template (the best match for their branch and department when none is
    given). Due dates count from the anchor date: the join date for onboarding, the last working day for exits.
    """
    ensure_lifecycle_defaults(company_id)
    if template_id:
        if not one('SELECT 1 AS x FROM lifecycle_template WHERE id = %s AND company_id = %s AND kind = %s AND active',
                   [template_id, company_id, kind]):
            raise BadRequestError('checklist template not found')
    else:
        t = one("""SELECT t.id FROM lifecycle_template t
                     LEFT JOIN employee_assignment a ON a.employee_id = %s AND a.valid @> greatest((now() AT TIME ZONE 'Asia/Kathmandu')::date, %s::date)
                    WHERE t.company_id = %s AND t.kind = %s AND t.active
                      AND (t.branch_id IS NULL OR t.branch_id = a.branch_id) AND (t.org_unit_id IS NULL OR t.org_unit_id = a.org_unit_id)
                    ORDER BY (t.org_unit_id IS NOT NULL) DESC, (t.branch_id IS NOT NULL) DESC, t.is_default DESC, t.created_at LIMIT 1""",
                [employee_id, anchor_date, company_id, kind])
        template_id = str(t['id']) if t else None
    if one('SELECT 1 AS x FROM lifecycle_case WHERE employee_id = %s AND kind = %s AND status = %s', [employee_id, kind, 'active']):
        raise ConflictError('onboarding is already in progress for this employee' if kind == 'onboarding'
                            else 'an exit clearance is already in progress for this employee')
    case_id = str(one("""INSERT INTO lifecycle_case (company_id, employee_id, kind, template_id, anchor_date, started_by, exit_id)
                         VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                      [company_id, employee_id, kind, template_id, anchor_date, started_by, exit_id])['id'])
    if template_id:
        execute("""INSERT INTO lifecycle_task (case_id, company_id, employee_id, sort, title, description, department, assignee_type,
                                              assignee_user_id, due_date, link)
                   SELECT %s, %s, %s, sort, title, description, department, assignee_type, assignee_user_id, %s::date + due_offset_days, link
                     FROM lifecycle_template_task WHERE template_id = %s ORDER BY sort""",
                [case_id, company_id, employee_id, anchor_date, template_id])
    audit('start', 'lifecycle_case', actor_id=started_by, row_id=case_id,
          after={'kind': kind, 'employeeId': str(employee_id), 'templateId': template_id})
    emp = one('SELECT name_en FROM employee WHERE id = %s', [employee_id])
    name = emp['name_en'] if emp else 'An employee'
    # Tell each person once that they have something to do, with their own count.
    per_user: dict[str, int] = {}
    for t in rows('SELECT * FROM lifecycle_task WHERE case_id = %s', [case_id]):
        for u in responsible_users(task_row(t)):
            per_user[u] = per_user.get(u, 0) + 1
    own_user = user_id_for_employee(str(employee_id))
    for user_id, n in per_user.items():
        if user_id == started_by:
            continue
        own = own_user == user_id
        if kind == 'onboarding':
            title = 'Welcome! Your getting-started checklist' if own else f'Onboarding for {name}'
        else:
            title = 'Your exit checklist' if own else f'Exit clearance for {name}'
        notify(company_id=company_id, user_id=user_id, template=f'lifecycle.{kind}.started', title=title,
               body=f"{n} {'task is' if n == 1 else 'tasks are'} waiting for you.", payload={'caseId': case_id})
    return case_id


def complete_case_if_done(case_id: str) -> bool:
    """Mark a case completed once nothing is left open."""
    return execute("""UPDATE lifecycle_case SET status = 'completed', completed_at = now()
                       WHERE id = %s AND status = 'active' AND NOT EXISTS (SELECT 1 FROM lifecycle_task WHERE case_id = %s AND status = 'open')""",
                   [case_id, case_id]) > 0


# ---------------------------------------------------------------- probation
def apply_confirmation(*, company_id: str, employee_id: str, confirmed_on, review_id: str | None, actor_id: str | None) -> str:
    """The confirmation effect of an approved probation review (or a direct HR confirmation): stamps the employee as
    confirmed, keeps a confirmation record and logs it on the career timeline."""
    execute('UPDATE employee SET confirmed_on = %s, updated_at = now() WHERE id = %s', [confirmed_on, employee_id])
    r = one("""INSERT INTO employee_confirmation (company_id, employee_id, confirmed_on, review_id, created_by)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""", [company_id, employee_id, confirmed_on, review_id, actor_id])
    on = confirmed_on.isoformat() if isinstance(confirmed_on, date) else str(confirmed_on)
    audit('confirm', 'employee', actor_id=actor_id, row_id=employee_id, after={'confirmedOn': on, 'reviewId': review_id})
    log_career_event(company_id=company_id, employee_id=employee_id, kind='confirmation', effective_date=on,
                     note='Confirmed after the probation review' if review_id else None, actor_id=actor_id)
    return str(r['id'])


def _iso(d) -> str | None:
    if d is None:
        return None
    return d.isoformat() if isinstance(d, date) else str(d)


def apply_probation_decision(review_id: str, state: str, ctx) -> None:
    rv = one('SELECT * FROM probation_review WHERE id = %s FOR UPDATE', [review_id])
    if not rv:
        raise NotFoundError('probation review not found')
    if rv['status'] != 'pending':
        raise ConflictError(f"review already {rv['status']}")
    company_id, employee_id = str(rv['company_id']), str(rv['employee_id'])
    execute('UPDATE probation_review SET status = %s, decided_at = now() WHERE id = %s', [state, review_id])
    emp = one('SELECT name_en FROM employee WHERE id = %s', [employee_id])
    name = emp['name_en'] if emp else 'the employee'
    outcome = 'was not approved'
    if state == 'approved':
        today = npt_today()
        if rv['recommendation'] == 'confirm':
            on = rv['probation_end'] if rv['probation_end'] and rv['probation_end'] > today else today
            apply_confirmation(company_id=company_id, employee_id=employee_id, confirmed_on=on, review_id=str(review_id), actor_id=ctx.actor_id)
            outcome = f'confirmed from {bs_label(on)}'
            own = user_id_for_employee(employee_id)
            if own:
                notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.confirmed',
                       title='Congratulations, your employment is confirmed',
                       body=f'Your probation is complete. You are confirmed from {bs_label(on)}.', payload={'reviewId': str(review_id)},
                       channels=('push',))
        elif rv['recommendation'] == 'extend':
            execute('UPDATE employee SET probation_end = %s, updated_at = now() WHERE id = %s', [rv['extend_to'], employee_id])
            audit('extend_probation', 'employee', actor_id=ctx.actor_id, row_id=employee_id,
                  before={'probationEnd': _iso(rv['probation_end'])}, after={'probationEnd': _iso(rv['extend_to'])})
            outcome = f"probation extended to {bs_label(rv['extend_to'])}"
            own = user_id_for_employee(employee_id)
            if own:
                notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.probation_extended',
                       title='Probation extended',
                       body=f"Your probation now ends on {bs_label(rv['extend_to'])}. Your manager will talk you through the next steps.",
                       payload={'reviewId': str(review_id)})
        else:
            record_exit(company_id=company_id, employee_id=employee_id, kind='termination', voluntary=False,
                        reason_category='Probation not successful', reason='Probation review', last_working_day=rv['last_working_day'],
                        actor_id=ctx.actor_id)
            outcome = f"employment ends on {bs_label(rv['last_working_day'])}"
    body = (f'Your review was approved: {outcome}.' if state == 'approved'
            else f"Your review was not approved: {ctx.reason or 'no reason given'}.")
    notify(company_id=company_id, user_id=str(rv['submitted_by']), template=f'lifecycle.probation_review.{state}',
           title=f'Probation review for {name}', body=body, payload={'reviewId': str(review_id)})


# ---------------------------------------------------------------- exits
EXIT_KINDS = ('resignation', 'termination', 'retirement', 'contract_end', 'death', 'other')


def record_exit(*, company_id: str, employee_id: str, kind: str, voluntary: bool, last_working_day, actor_id: str | None,
                reason_category: str | None = None, reason: str | None = None, template_id: str | None = None) -> str:
    """
    An accepted exit: the last working day becomes the employee's exit date (sign-in stops the day after) and the exit
    clearance checklist starts. Used for HR-initiated exits and terminations after a failed probation.
    """
    if one('SELECT 1 AS x FROM employee_exit WHERE employee_id = %s AND status IN (%s, %s)', [employee_id, 'pending', 'accepted']):
        raise ConflictError('an exit is already in progress for this employee')
    r = one("""INSERT INTO employee_exit (company_id, employee_id, kind, voluntary, reason_category, reason, submitted_on, notice_days,
                                         proposed_lwd, accepted_lwd, status, initiated_by, decided_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, %s, 'accepted', %s, now()) RETURNING id""",
            [company_id, employee_id, kind, voluntary, reason_category, reason, npt_today(), last_working_day, last_working_day, actor_id])
    exit_id = str(r['id'])
    accept_exit(exit_id, last_working_day, actor_id, template_id)
    return exit_id


def accept_exit(exit_id: str, lwd, actor_id: str | None, template_id: str | None = None) -> None:
    ex = one("""UPDATE employee_exit SET status = 'accepted', accepted_lwd = %s, decided_at = coalesce(decided_at, now()) WHERE id = %s
                RETURNING company_id, employee_id, kind""", [lwd, exit_id])
    company_id, employee_id = str(ex['company_id']), str(ex['employee_id'])
    execute('UPDATE employee SET exit_date = %s, updated_at = now() WHERE id = %s', [lwd, employee_id])
    audit('accept_exit', 'employee_exit', actor_id=actor_id, row_id=exit_id, after={'lastWorkingDay': _iso(lwd), 'kind': ex['kind']})
    if not one('SELECT 1 AS x FROM lifecycle_case WHERE employee_id = %s AND kind = %s AND status = %s', [employee_id, 'offboarding', 'active']):
        start_case(company_id=company_id, employee_id=employee_id, kind='offboarding', template_id=template_id, anchor_date=lwd,
                   started_by=actor_id, exit_id=exit_id)
    own = user_id_for_employee(employee_id)
    if own and ex['kind'] != 'death':
        notify(company_id=company_id, user_id=own, employee_id=employee_id, template='lifecycle.exit.accepted',
               title='Resignation accepted' if ex['kind'] == 'resignation' else 'Your last working day',
               body=f'Your last working day is {bs_label(lwd)}. Your exit checklist is on Home.', payload={'exitId': exit_id},
               channels=('push',))


def apply_resignation_decision(exit_id: str, state: str, ctx) -> None:
    ex = one('SELECT status, proposed_lwd, accepted_lwd, employee_id, company_id FROM employee_exit WHERE id = %s FOR UPDATE', [exit_id])
    if not ex:
        raise NotFoundError('resignation not found')
    if ex['status'] != 'pending':
        raise ConflictError(f"resignation already {ex['status']}")
    if state == 'approved':
        accept_exit(str(exit_id), ex['accepted_lwd'] or ex['proposed_lwd'], ctx.actor_id)
        return
    execute("UPDATE employee_exit SET status = 'rejected', decided_at = now() WHERE id = %s", [exit_id])
    own = user_id_for_employee(str(ex['employee_id']))
    if own:
        notify(company_id=str(ex['company_id']), user_id=own, employee_id=str(ex['employee_id']), template='lifecycle.exit.rejected',
               title='Resignation not accepted', body=f"{ctx.reason or 'Please talk to your manager or HR.'}", payload={'exitId': str(exit_id)})


def clearance_blockers(exit_id: str, employee_id: str) -> dict:
    """Open clearance tasks and assets still held, which stop an exit from being completed."""
    t = one("""SELECT count(*)::int AS n FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id
                WHERE c.exit_id = %s AND c.status = 'active' AND t.status = 'open'""", [exit_id])
    a = one('SELECT count(*)::int AS n FROM asset_assignment WHERE employee_id = %s AND returned_on IS NULL', [employee_id])
    return {'openTasks': t['n'], 'assetsHeld': a['n']}


def complete_exit(exit_id: str, actor_id: str | None, *, skip_open_tasks: bool = False) -> None:
    """Finish an exit: the employee becomes exited (sign-in already stops after the last working day)."""
    ex = one('SELECT status, employee_id, company_id, accepted_lwd, kind, reason FROM employee_exit WHERE id = %s FOR UPDATE', [exit_id])
    if not ex:
        raise NotFoundError('exit not found')
    if ex['status'] != 'accepted':
        raise ConflictError(f"exit is {ex['status']}")
    if ex['accepted_lwd'] > npt_today():
        raise ConflictError(f"the last working day is {bs_label(ex['accepted_lwd'])}; complete the exit after it")
    employee_id = str(ex['employee_id'])
    b = clearance_blockers(exit_id, employee_id)
    if b['assetsHeld']:
        n = b['assetsHeld']
        raise ConflictError(f"{n} company {'asset is' if n == 1 else 'assets are'} still with the employee; record the return (or mark lost) first")
    if b['openTasks'] and not skip_open_tasks:
        n = b['openTasks']
        raise ConflictError(f"{n} clearance {'task is' if n == 1 else 'tasks are'} still open")
    if b['openTasks']:
        execute("""UPDATE lifecycle_task t SET status = 'skipped', done_by = %s, done_at = now(), note = coalesce(note, 'Skipped when the exit was completed')
                     FROM lifecycle_case c WHERE c.id = t.case_id AND c.exit_id = %s AND t.status = 'open'""", [actor_id, exit_id])
    execute("UPDATE lifecycle_case SET status = 'completed', completed_at = now() WHERE exit_id = %s AND status = 'active'", [exit_id])
    execute("UPDATE employee_exit SET status = 'completed', completed_at = now() WHERE id = %s", [exit_id])
    execute("UPDATE employee SET status = 'exited', exit_date = %s, updated_at = now() WHERE id = %s", [ex['accepted_lwd'], employee_id])
    audit('complete_exit', 'employee_exit', actor_id=actor_id, row_id=str(exit_id), after={'skippedTasks': b['openTasks']})
    log_career_event(company_id=str(ex['company_id']), employee_id=employee_id, kind='separation', effective_date=ex['accepted_lwd'],
                     details={'separationType': ex['kind']}, note=ex['reason'], actor_id=actor_id)


BS_MONTHS = ['Baishakh', 'Jestha', 'Ashadh', 'Shrawan', 'Bhadra', 'Ashwin', 'Kartik', 'Mangsir', 'Poush', 'Magh', 'Falgun', 'Chaitra']


def settlement_status(company_id: str, employee_id: str, lwd) -> dict:
    """What HR still needs to do in payroll for a leaver (final settlement run, loans, leave encashment)."""
    period = one('SELECT id, bs_year, bs_month FROM payroll_period WHERE company_id = %s AND start_ad <= %s::date AND end_ad >= %s::date',
                 [company_id, lwd, lwd]) if lwd else None
    run = one("""SELECT id, status, locked_at FROM payroll_run WHERE company_id = %s AND run_type = 'final_settlement'
                  AND params->'employeeIds' ? %s::text ORDER BY created_at DESC LIMIT 1""", [company_id, str(employee_id)])
    loans = one("""SELECT count(*)::int AS n, coalesce(sum(l.principal - coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)), 0)::text AS outstanding
                     FROM loan l WHERE l.employee_id = %s AND l.status = 'active'""", [employee_id])
    leave = one("""SELECT coalesce(sum(g.delta_days), 0)::text AS days FROM leave_ledger g JOIN leave_type t ON t.id = g.leave_type_id
                    WHERE g.employee_id = %s AND t.encashable""", [employee_id])
    steps: list[str] = []
    if not run:
        steps.append(f"Create a final settlement run for {BS_MONTHS[period['bs_month'] - 1]} {period['bs_year']} (Payroll, New run, Final settlement)."
                     if period else 'Create the payroll period that contains the last working day, then a final settlement run for this employee.')
    elif not run['locked_at']:
        steps.append('Finish and lock the final settlement run.')
    if loans['n']:
        n = loans['n']
        steps.append(f"Recover {n} active {'loan or advance' if n == 1 else 'loans or advances'} in the settlement.")
    days = float(leave['days'])
    if days > 0:
        steps.append(f'Encashable leave of {days:.1f} days is paid out in the settlement.')
    return {
        'period': {'id': str(period['id']), 'bsYear': period['bs_year'], 'bsMonth': period['bs_month']} if period else None,
        'run': {'id': str(run['id']), 'status': run['status'], 'locked': bool(run['locked_at'])} if run else None,
        'activeLoans': loans['n'], 'loanOutstanding': loans['outstanding'], 'encashableLeaveDays': int(days) if days.is_integer() else days,
        'steps': steps,
    }


def auto_start_onboarding(company_id: str, employee_id: str, join_date, actor_id: str | None) -> str | None:
    """
    Called when HR adds an employee: opens the default onboarding checklist when the company has switched that on.
    Runs in the caller's transaction behind a savepoint, so a checklist problem never stops the employee being created.
    """
    from hajiri.features.service import feature_on
    policy = lifecycle_policy(company_id)
    if not policy['autoStartOnboarding'] or not feature_on(company_id, 'lifecycle'):
        return None
    try:
        with transaction.atomic():
            return start_case(company_id=company_id, employee_id=str(employee_id), kind='onboarding', anchor_date=join_date,
                              started_by=actor_id)
    except Exception:  # noqa: BLE001 - as the TS savepoint: any failure leaves the employee created without a checklist
        return None


# ---------------------------------------------------------------- daily job
def lifecycle_daily(now: datetime | None = None) -> dict:
    today = npt_today(now)
    out = {'overdueReminders': 0, 'probationReminders': 0, 'exitsCompleted': 0, 'exitsFlagged': 0}

    # 1. Overdue checklist tasks: one reminder per task to whoever should act.
    overdue = rows("""SELECT t.*, e.name_en FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id JOIN employee e ON e.id = t.employee_id
                       WHERE t.status = 'open' AND c.status = 'active' AND t.due_date < %s::date ORDER BY t.due_date LIMIT 2000""", [today])
    for t in overdue:
        t = task_row(t)
        if not remind_once(t['company_id'], 'task_overdue', t['id'], 'overdue'):
            continue
        for user_id in responsible_users(t):
            notify(company_id=t['company_id'], user_id=user_id, template='lifecycle.task.overdue', title='Checklist task overdue',
                   body=f'"{t["title"]}" for {t["name_en"]} was due on {bs_label(t["due_date"])}.', payload={'caseId': t['case_id'], 'taskId': t['id']})
        out['overdueReminders'] += 1

    # 2. Probation ending: remind the manager (HR when there is none) at each configured threshold.
    for c in rows('SELECT id FROM company'):
        cid = str(c['id'])
        policy = lifecycle_policy(cid)
        max_days = max([0, *policy['probationReminderDays']])
        if not max_days:
            continue
        due = rows("""SELECT e.id, e.name_en, e.probation_end, (e.probation_end - %s::date) AS days_left FROM employee e
                       WHERE e.company_id = %s AND e.status = 'active' AND e.probation_end IS NOT NULL AND e.confirmed_on IS NULL
                         AND (e.exit_date IS NULL OR e.exit_date >= %s::date)
                         AND e.probation_end >= %s::date AND e.probation_end <= %s::date + %s::int
                         AND NOT EXISTS (SELECT 1 FROM probation_review r WHERE r.employee_id = e.id AND r.status = 'pending')""",
                   [today, cid, today, today, today, max_days])
        for e in due:
            threshold = next((d for d in sorted(policy['probationReminderDays']) if e['days_left'] <= d), None)
            if threshold is None:
                continue
            if not remind_once(cid, 'probation', str(e['id']), f"{e['probation_end'].isoformat()}:{threshold}"):
                continue
            mgr = manager_user_of(str(e['id']))
            targets = [mgr] if mgr else user_ids_with_role(cid, 'hr')
            n = e['days_left']
            for user_id in targets:
                notify(company_id=cid, user_id=user_id, template='lifecycle.probation.ending',
                       title=f"Probation ends in {n} {'day' if n == 1 else 'days'}",
                       body=f"{e['name_en']}'s probation ends on {bs_label(e['probation_end'])}. Submit a probation review to confirm, extend or end it.",
                       payload={'employeeId': str(e['id'])}, channels=('push',))
            out['probationReminders'] += 1

    # 3. Exits past their last working day: complete when clearance is done, otherwise tell HR once.
    exits = rows("""SELECT x.id, x.company_id, x.employee_id, e.name_en FROM employee_exit x JOIN employee e ON e.id = x.employee_id
                     WHERE x.status = 'accepted' AND x.accepted_lwd < %s::date""", [today])
    for x in exits:
        xid, cid, eid = str(x['id']), str(x['company_id']), str(x['employee_id'])
        b = clearance_blockers(xid, eid)
        if not b['openTasks'] and not b['assetsHeld']:
            try:
                with transaction.atomic():
                    complete_exit(xid, None)
                out['exitsCompleted'] += 1
            except Exception:  # noqa: BLE001 - as the TS job: a failure is rolled back and retried tomorrow
                pass
            continue
        if not remind_once(cid, 'exit_pending', xid, 'clearance'):
            continue
        t, a = b['openTasks'], b['assetsHeld']
        notify_role(cid, 'hr', template='lifecycle.exit.clearance_pending', title=f"Exit clearance pending for {x['name_en']}",
                    body=f"The last working day has passed. {t} clearance {'task is' if t == 1 else 'tasks are'} open and "
                         f"{a} {'asset is' if a == 1 else 'assets are'} not returned.", payload={'exitId': xid})
        out['exitsFlagged'] += 1
    return out


def diff_days(a, b) -> int:
    """Calendar days from b to a."""
    to_d = (lambda v: v if isinstance(v, date) else date.fromisoformat(str(v)[:10]))
    return (to_d(a) - to_d(b)).days


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
