"""
Effective-dated career events (port of apps/api/src/modules/people/career.ts): hire, promotion, transfer,
confirmation, separation... Recording an event that is due applies it at once; a future event waits as 'scheduled'
and the daily job applies it on its date. Applying updates the employee row, splits the effective-dated assignment at
the event date and writes field history.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from django.db import transaction

from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.core.schema import Enum, IsoDate, Num, Obj, Str, Uuid, parse
from hajiri.notifications.service import notify, user_id_for_employee

from .history import record_field_changes

CAREER_KINDS = ('hire', 'confirmation', 'promotion', 'transfer', 'designation_change', 'grade_change', 'manager_change',
                'employment_type_change', 'salary_revision', 'separation', 'rehire', 'note', 'suspension',
                'leave_of_absence', 'acting', 'deputation')

CAREER_LABEL = {
    'hire': 'Joined', 'confirmation': 'Confirmed', 'promotion': 'Promoted', 'transfer': 'Transferred',
    'designation_change': 'Designation changed', 'grade_change': 'Grade changed', 'manager_change': 'Manager changed',
    'employment_type_change': 'Employment type changed', 'salary_revision': 'Salary revised',
    'separation': 'Left the company', 'rehire': 'Rejoined', 'note': 'Note',
    'suspension': 'Suspended', 'leave_of_absence': 'Leave of absence', 'acting': 'Acting assignment', 'deputation': 'Deputation',
}

# Temporary or record-only events: logged on the timeline, never applied to the permanent record.
TEMPORARY_KINDS = frozenset({'suspension', 'leave_of_absence', 'acting', 'deputation'})

EMPLOYMENT_TYPES = ('regular', 'work_based', 'time_based', 'casual', 'part_time')
SEPARATION_TYPES = ('resignation', 'termination', 'retirement', 'contract_end', 'death', 'other')

CAREER_DETAILS = Obj({
    'designationId': Uuid(optional=True),
    'designation': Str(trim=True, min=1, max=120, optional=True),
    'grade': Str(trim=True, min=1, max=40, optional=True),
    'branchId': Uuid(optional=True),
    'orgUnitId': Uuid(nullable=True, optional=True),
    'managerId': Uuid(nullable=True, optional=True),
    'employmentType': Enum(EMPLOYMENT_TYPES, optional=True),
    'separationType': Enum(SEPARATION_TYPES, optional=True),
    'revisionPercent': Num(min=-100, max=1000, optional=True),
    'reason': Str(trim=True, max=500, optional=True),
    'endDate': IsoDate(optional=True),
}, strict=True)

CAREER_BODY = Obj({
    'kind': Enum(CAREER_KINDS),
    'effectiveDate': IsoDate(),
    'details': Obj(CAREER_DETAILS.fields, strict=True, default=dict),
    'note': Str(trim=True, max=1000, optional=True),
})

EMPLOYMENT = {'regular': 'Regular', 'work_based': 'Work-based', 'time_based': 'Time-based', 'casual': 'Casual', 'part_time': 'Part-time'}


def _d(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _ev(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'company_id', 'employee_id', 'created_by'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['details'] = out.get('details') or {}
    return out


def _is_due(kind: str, effective: date, today: date) -> bool:
    """A separation takes effect the day after the last working day; everything else on its date."""
    return effective < today if kind == 'separation' else effective <= today


def _check_refs(company_id: str, employee_id: str, kind: str, d: dict) -> None:
    def owned(table: str, id_, label: str) -> None:
        if not id_:
            return
        if not one(f'SELECT 1 AS x FROM {table} WHERE id = %s AND company_id = %s', [id_, company_id]):
            raise BadRequestError(f'{label} does not exist in your company')
    owned('designation', d.get('designationId'), 'designation')
    owned('branch', d.get('branchId'), 'branch')
    owned('org_unit', d.get('orgUnitId'), 'department')
    owned('employee', d.get('managerId'), 'manager')
    if d.get('managerId') and d['managerId'] == str(employee_id):
        raise BadRequestError('an employee cannot be their own manager')

    def need(ok: bool, msg: str) -> None:
        if not ok:
            raise BadRequestError(msg)
    if kind == 'promotion':
        need(bool(d.get('designationId') or d.get('designation') or d.get('grade')), 'a promotion needs a new designation or grade')
    elif kind == 'designation_change':
        need(bool(d.get('designationId') or d.get('designation')), 'choose the new designation')
    elif kind == 'grade_change':
        need(bool(d.get('grade')), 'enter the new grade')
    elif kind == 'transfer':
        need('branchId' in d or 'orgUnitId' in d, 'a transfer needs a new branch or department')
    elif kind == 'manager_change':
        need('managerId' in d, 'choose the new manager')
    elif kind == 'employment_type_change':
        need(bool(d.get('employmentType')), 'choose the new employment type')
    elif kind == 'separation':
        need(bool(d.get('separationType')), 'choose the type of separation')


def record_career_event(*, company_id: str, employee_id: str, kind: str, effective_date, details: dict | None = None,
                        note: str | None = None, actor_id: str | None = None, today: date | None = None) -> dict:
    """Record a career event; applies it now when due. Call inside a transaction (it locks the employee row)."""
    today = today or npt_today()
    d = parse(CAREER_DETAILS, details or {})
    eff = _d(effective_date)
    emp = one('SELECT id, join_date, status FROM employee WHERE id = %s AND company_id = %s FOR UPDATE', [employee_id, company_id])
    if not emp:
        raise NotFoundError('employee not found')
    _check_refs(company_id, employee_id, kind, d)
    joined = emp['join_date']
    if joined and eff < joined and kind not in ('hire', 'note'):
        raise BadRequestError('the effective date is before the employee joined')
    if kind == 'separation':
        if one("SELECT 1 AS x FROM career_event WHERE employee_id = %s AND kind = 'separation' AND status = 'scheduled'", [employee_id]):
            raise ConflictError('a separation is already scheduled; cancel it first')
        # The exit date is known as soon as the separation is recorded (payroll settles up to it); the status changes on the day after.
        execute('UPDATE employee SET exit_date = %s, updated_at = now() WHERE id = %s', [eff, employee_id])
    ev = _ev(one("""INSERT INTO career_event (company_id, employee_id, kind, effective_date, details, note, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                 [company_id, employee_id, kind, eff, json.dumps(d), note, actor_id]))
    if kind in ('hire', 'note', 'salary_revision'):
        ev = _ev(one("UPDATE career_event SET status = 'applied', applied_at = now() WHERE id = %s RETURNING *", [ev['id']]))
    elif _is_due(ev['kind'], ev['effective_date'], today):
        ev = apply_career_event(ev, actor_id)
    return ev


def log_career_event(*, company_id: str, employee_id: str, kind: str, effective_date, details: dict | None = None,
                     note: str | None = None, actor_id: str | None = None) -> None:
    """Record an event that another module has already put into effect (a probation confirmation, a completed exit),
    so it appears on the career timeline without being applied a second time."""
    d = parse(CAREER_DETAILS, details or {})
    execute("""INSERT INTO career_event (company_id, employee_id, kind, effective_date, details, note, status, applied_at, created_by)
               VALUES (%s, %s, %s, %s, %s, %s, 'applied', now(), %s)""",
            [company_id, employee_id, kind, _d(effective_date), json.dumps(d), note, actor_id])


def _snapshot(employee_id: str, on_date: date) -> dict:
    return one("""SELECT e.designation, e.designation_id, e.grade, e.status, e.exit_date, e.confirmed_on, a.branch_id, a.org_unit_id,
                         a.manager_id, a.employment_type
                    FROM employee e LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> %s::date WHERE e.id = %s""",
               [on_date, employee_id])


def split_assignment(employee_id: str, from_: date, changes: dict) -> None:
    """
    Split the employee's effective-dated assignment at `from_` and apply `changes` (branch_id, org_unit_id, manager_id,
    employment_type) to the period that starts there. Later periods (already planned) keep their own values.
    """
    c = one("""SELECT lower(valid) AS lower, upper(valid) AS upper, branch_id, org_unit_id, manager_id, employment_type, probation_end
                 FROM employee_assignment WHERE employee_id = %s AND valid @> %s::date""", [employee_id, from_])
    if not c:
        # Nothing covers the date (e.g. before the first assignment): open a period up to the next one, if any.
        nxt = one("""SELECT lower(valid) AS lower FROM employee_assignment WHERE employee_id = %s AND lower(valid) > %s::date
                     ORDER BY lower(valid) LIMIT 1""", [employee_id, from_])
        execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type)
                   VALUES (%s, daterange(%s::date, %s::date), %s, %s, %s, %s)""",
                [employee_id, from_, nxt['lower'] if nxt else None, changes.get('branch_id'), changes.get('org_unit_id'),
                 changes.get('manager_id'), changes.get('employment_type') or 'regular'])
        return
    merged = {k: (changes[k] if k in changes else c[k]) for k in ('branch_id', 'org_unit_id', 'manager_id')}
    merged['employment_type'] = changes.get('employment_type') or c['employment_type']
    if c['lower'] == from_:
        execute("""UPDATE employee_assignment SET branch_id = %s, org_unit_id = %s, manager_id = %s, employment_type = %s
                    WHERE employee_id = %s AND lower(valid) = %s::date""",
                [merged['branch_id'], merged['org_unit_id'], merged['manager_id'], merged['employment_type'], employee_id, from_])
        return
    execute('UPDATE employee_assignment SET valid = daterange(lower(valid), %s::date) WHERE employee_id = %s AND valid @> %s::date',
            [from_, employee_id, from_])
    execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type, probation_end)
               VALUES (%s, daterange(%s::date, %s::date), %s, %s, %s, %s, %s)""",
            [employee_id, from_, c['upper'], merged['branch_id'], merged['org_unit_id'], merged['manager_id'],
             merged['employment_type'], c['probation_end']])


def _name_of(table: str, id_) -> str | None:
    if not id_:
        return None
    r = one(f'SELECT name_en AS n FROM {table} WHERE id = %s', [str(id_)])
    return r['n'] if r else None


def _s(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def apply_career_event(ev: dict, actor_id: str | None) -> dict:
    """Apply a scheduled event (locks it). Returns the updated row."""
    e = _ev(one('SELECT * FROM career_event WHERE id = %s FOR UPDATE', [ev['id']]))
    if not e or e['status'] != 'scheduled':
        return e or ev
    d = e['details']
    eff = e['effective_date']
    before = _snapshot(e['employee_id'], eff)
    emp_sets: dict = {}
    assignment: dict = {}
    if e['kind'] in TEMPORARY_KINDS:
        pass  # acting, deputation, suspension and leave of absence change nothing permanent
    elif d.get('designationId'):
        des = one('SELECT name_en, grade FROM designation WHERE id = %s', [d['designationId']])
        emp_sets['designation_id'] = d['designationId']
        emp_sets['designation'] = (des and des['name_en']) or d.get('designation')
        if not d.get('grade') and des and des['grade']:
            emp_sets['grade'] = des['grade']
    elif d.get('designation'):
        emp_sets['designation'] = d['designation']
        emp_sets['designation_id'] = None
    if e['kind'] not in TEMPORARY_KINDS:
        if d.get('grade'):
            emp_sets['grade'] = d['grade']
        if 'branchId' in d:
            assignment['branch_id'] = d['branchId']
        if 'orgUnitId' in d:
            assignment['org_unit_id'] = d['orgUnitId']
        if 'managerId' in d:
            assignment['manager_id'] = d['managerId']
        if d.get('employmentType'):
            assignment['employment_type'] = d['employmentType']
    if e['kind'] == 'confirmation':
        emp_sets['confirmed_on'] = eff
    if e['kind'] == 'separation':
        emp_sets['status'] = 'exited'
        emp_sets['exit_date'] = eff
    if e['kind'] == 'rehire':
        emp_sets['status'] = 'active'
        emp_sets['exit_date'] = None
    if emp_sets:
        cols = list(emp_sets)
        execute(f"UPDATE employee SET {', '.join(f'{c} = %s' for c in cols)}, updated_at = now() WHERE id = %s",
                [emp_sets[c] for c in cols] + [e['employee_id']])
    # Sign-in ends by itself once the exit date has passed (auth checks employee.exit_date).
    if assignment:
        split_assignment(e['employee_id'], eff, assignment)

    after = _snapshot(e['employee_id'], eff)
    changes: list[dict] = []

    def push(field: str, label: str, frm, to) -> None:
        if frm != to:
            changes.append({'field': field, 'label': label, 'from': frm, 'to': to})
    push('designation', 'Designation', before['designation'], after['designation'])
    push('grade', 'Grade', before['grade'], after['grade'])
    push('branch_id', 'Branch', _name_of('branch', before['branch_id']), _name_of('branch', after['branch_id']))
    push('org_unit_id', 'Department', _name_of('org_unit', before['org_unit_id']), _name_of('org_unit', after['org_unit_id']))
    push('manager_id', 'Manager', _name_of('employee', before['manager_id']), _name_of('employee', after['manager_id']))
    et_b, et_a = before['employment_type'], after['employment_type']
    push('employment_type', 'Employment type', EMPLOYMENT.get(et_b, et_b) if et_b else None, EMPLOYMENT.get(et_a, et_a) if et_a else None)
    push('confirmed_on', 'Confirmed on', _s(before['confirmed_on']), _s(after['confirmed_on']))
    push('status', 'Status', before['status'], after['status'])
    company_id = e['company_id']
    record_field_changes(company_id=company_id, employee_id=e['employee_id'], changes=changes, source='career_event',
                         ref_id=e['id'], actor_id=actor_id)
    snap = {k: (str(v) if k.endswith('_id') and v is not None else _s(v) if isinstance(v, date) else v) for k, v in before.items()}
    r = _ev(one("UPDATE career_event SET status = 'applied', applied_at = now(), before = %s WHERE id = %s RETURNING *",
                [json.dumps(snap), e['id']]))
    user_id = user_id_for_employee(e['employee_id'])
    if user_id and e['kind'] != 'separation':
        notify(company_id=company_id, user_id=user_id, template='people.career_event', title=CAREER_LABEL[e['kind']],
               body=career_sentence(e['kind'], after), payload={'eventId': e['id']})
    return r


def career_sentence(kind: str, s: dict) -> str:
    if kind in ('promotion', 'designation_change'):
        return f"Your designation is now {s['designation'] or '—'}{', grade ' + s['grade'] if s['grade'] else ''}."
    if kind == 'grade_change':
        return f"Your grade is now {s['grade'] or '—'}."
    if kind == 'confirmation':
        return 'Your employment has been confirmed. Congratulations!'
    if kind == 'transfer':
        return 'Your branch or department has changed. See your profile for details.'
    if kind == 'manager_change':
        return 'Your manager has changed. See your profile for details.'
    return 'Your employment record was updated.'


def apply_due_career_events(now: datetime | None = None) -> int:
    """Daily job: apply every scheduled event that has become due, each in its own transaction."""
    today = npt_today(now)
    due = rows("""SELECT * FROM career_event WHERE status = 'scheduled'
                   AND (effective_date < %s::date OR (kind <> 'separation' AND effective_date = %s::date))
                 ORDER BY effective_date, created_at""", [today, today])
    n = 0
    for ev in due:
        with transaction.atomic():
            apply_career_event(_ev(ev), None)
        n += 1
    return n


def cancel_career_event(company_id: str, employee_id: str, event_id: str) -> None:
    """Cancel a scheduled event. A cancelled separation clears the exit date it set."""
    ev = one('SELECT * FROM career_event WHERE id = %s AND employee_id = %s AND company_id = %s FOR UPDATE',
             [event_id, employee_id, company_id])
    if not ev:
        raise NotFoundError('event not found')
    if ev['status'] != 'scheduled':
        raise ConflictError('only a scheduled event can be cancelled; record a new event to change an applied one')
    execute("UPDATE career_event SET status = 'cancelled' WHERE id = %s", [event_id])
    if ev['kind'] == 'separation':
        execute("UPDATE employee SET exit_date = NULL, updated_at = now() WHERE id = %s AND status = 'active'", [employee_id])
