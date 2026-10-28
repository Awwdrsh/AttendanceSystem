"""
Effective-dated career events (port of apps/api/src/modules/people/career.ts): hire, promotion, transfer,
confirmation, separation... Recording an event that is due applies it at once; a future event waits as 'scheduled'
and the daily job applies it on its date. Applying updates the employee row, splits the effective-dated assignment at
the event date and writes field history.
"""
from __future__ import annotations

from datetime import date, datetime

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange
from django.db.models import F, FilteredRelation, Q
from django.db.models.functions import Now

from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, NotFoundError
from hajiri.core.validation import plain, validate
from hajiri.db.models import Branch, CareerEvent, Designation, Employee, EmployeeAssignment, OrgUnit
from hajiri.notifications.service import notify

from .accounts import user_id_for_employee
from .history import record_field_changes
from .serializers import EMPLOYMENT_TYPES, SEPARATION_TYPES, CareerDetailsIn, CareerIn  # noqa: F401

Kind = CareerEvent.Kind
EventStatus = CareerEvent.Status
CAREER_KINDS = tuple(Kind.values)

CAREER_LABEL = {
    'hire': 'Joined', 'confirmation': 'Confirmed', 'promotion': 'Promoted', 'transfer': 'Transferred',
    'designation_change': 'Designation changed', 'grade_change': 'Grade changed', 'manager_change': 'Manager changed',
    'employment_type_change': 'Employment type changed', 'salary_revision': 'Salary revised',
    'separation': 'Left the company', 'rehire': 'Rejoined', 'note': 'Note',
    'suspension': 'Suspended', 'leave_of_absence': 'Leave of absence', 'acting': 'Acting assignment', 'deputation': 'Deputation',
}

# Temporary or record-only events: logged on the timeline, never applied to the permanent record.
TEMPORARY_KINDS = frozenset({Kind.SUSPENSION, Kind.LEAVE_OF_ABSENCE, Kind.ACTING, Kind.DEPUTATION})
# Events that are a record only: applied as soon as they are written.
RECORD_KINDS = frozenset({Kind.HIRE, Kind.NOTE, Kind.SALARY_REVISION})

EMPLOYMENT = {'regular': 'Regular', 'work_based': 'Work-based', 'time_based': 'Time-based', 'casual': 'Casual', 'part_time': 'Part-time'}
_EVENT_COLS = ('id', 'company_id', 'employee_id', 'kind', 'effective_date', 'details', 'before', 'note', 'status', 'applied_at',
               'created_by', 'created_at')


def parse_details(details: dict | None) -> dict:
    return plain(validate(CareerDetailsIn, details or {}))


def parse_career(body: dict) -> dict:
    return plain(validate(CareerIn, body))


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


def _event(pk) -> dict | None:
    return _ev(CareerEvent.objects.filter(pk=pk).values(*_EVENT_COLS).first())


def _is_due(kind: str, effective: date, today: date) -> bool:
    """A separation takes effect the day after the last working day; everything else on its date."""
    return effective < today if kind == Kind.SEPARATION else effective <= today


def _check_refs(company_id: str, employee_id: str, kind: str, d: dict) -> None:
    for model, id_, label in ((Designation, d.get('designationId'), 'designation'), (Branch, d.get('branchId'), 'branch'),
                              (OrgUnit, d.get('orgUnitId'), 'department'), (Employee, d.get('managerId'), 'manager')):
        if id_ and not model.objects.for_company(company_id).filter(pk=id_).exists():
            raise BadRequestError(f'{label} does not exist in your company')
    if d.get('managerId') and d['managerId'] == str(employee_id):
        raise BadRequestError('an employee cannot be their own manager')

    def need(ok: bool, msg: str) -> None:
        if not ok:
            raise BadRequestError(msg)
    if kind == Kind.PROMOTION:
        need(bool(d.get('designationId') or d.get('designation') or d.get('grade')), 'a promotion needs a new designation or grade')
    elif kind == Kind.DESIGNATION_CHANGE:
        need(bool(d.get('designationId') or d.get('designation')), 'choose the new designation')
    elif kind == Kind.GRADE_CHANGE:
        need(bool(d.get('grade')), 'enter the new grade')
    elif kind == Kind.TRANSFER:
        need('branchId' in d or 'orgUnitId' in d, 'a transfer needs a new branch or department')
    elif kind == Kind.MANAGER_CHANGE:
        need('managerId' in d, 'choose the new manager')
    elif kind == Kind.EMPLOYMENT_TYPE_CHANGE:
        need(bool(d.get('employmentType')), 'choose the new employment type')
    elif kind == Kind.SEPARATION:
        need(bool(d.get('separationType')), 'choose the type of separation')


def record_career_event(*, company_id: str, employee_id: str, kind: str, effective_date, details: dict | None = None,
                        note: str | None = None, actor_id: str | None = None, today: date | None = None) -> dict:
    """Record a career event; applies it now when due. Call inside a transaction (it locks the employee row)."""
    today = today or npt_today()
    d = parse_details(details)
    eff = _d(effective_date)
    emp = (Employee.objects.for_company(company_id).select_for_update().filter(pk=employee_id)
           .values('id', 'join_date', 'status').first())
    if not emp:
        raise NotFoundError('employee not found')
    _check_refs(company_id, employee_id, kind, d)
    joined = emp['join_date']
    if joined and eff < joined and kind not in (Kind.HIRE, Kind.NOTE):
        raise BadRequestError('the effective date is before the employee joined')
    if kind == Kind.SEPARATION:
        if CareerEvent.objects.filter(employee_id=employee_id, kind=Kind.SEPARATION, status=EventStatus.SCHEDULED).exists():
            raise ConflictError('a separation is already scheduled; cancel it first')
        # The exit date is known as soon as the separation is recorded (payroll settles up to it); the status changes on the day after.
        Employee.objects.filter(pk=employee_id).update(exit_date=eff, updated_at=Now())
    created = CareerEvent.objects.create(company_id=company_id, employee_id=employee_id, kind=kind, effective_date=eff,
                                         details=d, note=note, created_by_id=actor_id)
    if kind in RECORD_KINDS:
        CareerEvent.objects.filter(pk=created.pk).update(status=EventStatus.APPLIED, applied_at=Now())
        return _event(created.pk)
    ev = _event(created.pk)
    if _is_due(ev['kind'], ev['effective_date'], today):
        ev = apply_career_event(ev, actor_id)
    return ev


def log_career_event(*, company_id: str, employee_id: str, kind: str, effective_date, details: dict | None = None,
                     note: str | None = None, actor_id: str | None = None) -> None:
    """Record an event that another module has already put into effect (a probation confirmation, a completed exit),
    so it appears on the career timeline without being applied a second time."""
    CareerEvent.objects.create(company_id=company_id, employee_id=employee_id, kind=kind, effective_date=_d(effective_date),
                               details=parse_details(details), note=note, status=EventStatus.APPLIED, applied_at=Now(),
                               created_by_id=actor_id)


def _snapshot(employee_id: str, on_date: date) -> dict:
    """The employee's permanent record and their assignment on a date (a LEFT JOIN on the period covering it)."""
    return (Employee.objects.filter(pk=employee_id)
            .annotate(asg=FilteredRelation('employee_assignments', condition=Q(employee_assignments__valid__contains=on_date)))
            .values('designation', 'designation_ref_id', 'grade', 'status', 'exit_date', 'confirmed_on',
                    branch_id=F('asg__branch_id'), org_unit_id=F('asg__org_unit_id'), manager_id=F('asg__manager_id'),
                    employment_type=F('asg__employment_type'))
            .get())


_ASSIGNMENT_COLS = ('valid', 'branch_id', 'org_unit_id', 'manager_id', 'employment_type', 'probation_end')


def split_assignment(employee_id: str, from_: date, changes: dict) -> None:
    """
    Split the employee's effective-dated assignment at `from_` and apply `changes` (branch_id, org_unit_id, manager_id,
    employment_type) to the period that starts there. Later periods (already planned) keep their own values.
    """
    periods = EmployeeAssignment.objects.filter(employee_id=employee_id)
    c = periods.filter(valid__contains=from_).values(*_ASSIGNMENT_COLS).first()
    if not c:
        # Nothing covers the date (e.g. before the first assignment): open a period up to the next one, if any.
        nxt = (periods.filter(valid__startswith__gt=from_).order_by('valid__startswith')
               .values_list('valid', flat=True).first())
        EmployeeAssignment.objects.create(
            employee_id=employee_id, valid=DateRange(from_, nxt.lower if nxt else None), branch_id=changes.get('branch_id'),
            org_unit_id=changes.get('org_unit_id'), manager_id=changes.get('manager_id'),
            employment_type=changes.get('employment_type') or EmployeeAssignment.EmploymentType.REGULAR)
        return
    merged = {k: (changes[k] if k in changes else c[k]) for k in ('branch_id', 'org_unit_id', 'manager_id')}
    merged['employment_type'] = changes.get('employment_type') or c['employment_type']
    if c['valid'].lower == from_:
        periods.filter(valid__startswith=from_).update(**merged)
        return
    periods.filter(valid__contains=from_).update(valid=DateRange(c['valid'].lower, from_))
    EmployeeAssignment.objects.create(employee_id=employee_id, valid=DateRange(from_, c['valid'].upper),
                                      probation_end=c['probation_end'], **merged)


def _s(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def apply_career_event(ev: dict, actor_id: str | None) -> dict:
    """Apply a scheduled event (locks it). Returns the updated row."""
    e = _ev(CareerEvent.objects.select_for_update().filter(pk=ev['id']).values(*_EVENT_COLS).first())
    if not e or e['status'] != EventStatus.SCHEDULED:
        return e or ev
    d = e['details']
    eff = e['effective_date']
    before = _snapshot(e['employee_id'], eff)
    emp_sets: dict = {}
    assignment: dict = {}
    if e['kind'] in TEMPORARY_KINDS:
        pass  # acting, deputation, suspension and leave of absence change nothing permanent
    elif d.get('designationId'):
        des = Designation.objects.filter(pk=d['designationId']).values('name_en', 'grade').first()
        emp_sets['designation_ref_id'] = d['designationId']
        emp_sets['designation'] = (des and des['name_en']) or d.get('designation')
        if not d.get('grade') and des and des['grade']:
            emp_sets['grade'] = des['grade']
    elif d.get('designation'):
        emp_sets['designation'] = d['designation']
        emp_sets['designation_ref_id'] = None
    if e['kind'] not in TEMPORARY_KINDS:
        if d.get('grade'):
            emp_sets['grade'] = d['grade']
        for key, col in (('branchId', 'branch_id'), ('orgUnitId', 'org_unit_id'), ('managerId', 'manager_id')):
            if key in d:
                assignment[col] = d[key]
        if d.get('employmentType'):
            assignment['employment_type'] = d['employmentType']
    if e['kind'] == Kind.CONFIRMATION:
        emp_sets['confirmed_on'] = eff
    if e['kind'] == Kind.SEPARATION:
        emp_sets['status'] = Employee.Status.EXITED
        emp_sets['exit_date'] = eff
    if e['kind'] == Kind.REHIRE:
        emp_sets['status'] = Employee.Status.ACTIVE
        emp_sets['exit_date'] = None
    if emp_sets:
        Employee.objects.filter(pk=e['employee_id']).update(**emp_sets, updated_at=Now())
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
    for col, label, model in (('branch_id', 'Branch', Branch), ('org_unit_id', 'Department', OrgUnit), ('manager_id', 'Manager', Employee)):
        push(col, label, _name(model, before[col]), _name(model, after[col]))
    et_b, et_a = before['employment_type'], after['employment_type']
    push('employment_type', 'Employment type', EMPLOYMENT.get(et_b, et_b) if et_b else None, EMPLOYMENT.get(et_a, et_a) if et_a else None)
    push('confirmed_on', 'Confirmed on', _s(before['confirmed_on']), _s(after['confirmed_on']))
    push('status', 'Status', before['status'], after['status'])
    company_id = e['company_id']
    record_field_changes(company_id=company_id, employee_id=e['employee_id'], changes=changes, source='career_event',
                         ref_id=e['id'], actor_id=actor_id)
    snap = {('designation_id' if k == 'designation_ref_id' else k):
            (str(v) if k.endswith('_id') and v is not None else _s(v) if isinstance(v, date) else v) for k, v in before.items()}
    CareerEvent.objects.filter(pk=e['id']).update(status=EventStatus.APPLIED, applied_at=Now(), before=snap)
    r = _event(e['id'])
    user_id = user_id_for_employee(e['employee_id'])
    if user_id and e['kind'] != Kind.SEPARATION:
        notify(company_id=company_id, user_id=user_id, template='people.career_event', title=CAREER_LABEL[e['kind']],
               body=career_sentence(e['kind'], after), payload={'eventId': e['id']})
    return r


def _name(model, id_) -> str | None:
    """name_en of a branch, unit or employee; None when there is none (or it no longer exists)."""
    if not id_:
        return None
    return model.objects.filter(pk=id_).values_list('name_en', flat=True).first()


def career_sentence(kind: str, s: dict) -> str:
    if kind in (Kind.PROMOTION, Kind.DESIGNATION_CHANGE):
        return f"Your designation is now {s['designation'] or '—'}{', grade ' + s['grade'] if s['grade'] else ''}."
    if kind == Kind.GRADE_CHANGE:
        return f"Your grade is now {s['grade'] or '—'}."
    if kind == Kind.CONFIRMATION:
        return 'Your employment has been confirmed. Congratulations!'
    if kind == Kind.TRANSFER:
        return 'Your branch or department has changed. See your profile for details.'
    if kind == Kind.MANAGER_CHANGE:
        return 'Your manager has changed. See your profile for details.'
    return 'Your employment record was updated.'


def apply_due_career_events(now: datetime | None = None) -> int:
    """Daily job: apply every scheduled event that has become due, each in its own transaction."""
    today = npt_today(now)
    due = (CareerEvent.objects.filter(status=EventStatus.SCHEDULED)
           .filter(Q(effective_date__lt=today) | (~Q(kind=Kind.SEPARATION) & Q(effective_date=today)))
           .order_by('effective_date', 'created_at').values(*_EVENT_COLS))
    n = 0
    for ev in due:
        with transaction.atomic():
            apply_career_event(_ev(ev), None)
        n += 1
    return n


def cancel_career_event(company_id: str, employee_id: str, event_id: str) -> None:
    """Cancel a scheduled event. A cancelled separation clears the exit date it set."""
    ev = (CareerEvent.objects.for_company(company_id).select_for_update().filter(pk=event_id, employee_id=employee_id)
          .values('kind', 'status').first())
    if not ev:
        raise NotFoundError('event not found')
    if ev['status'] != EventStatus.SCHEDULED:
        raise ConflictError('only a scheduled event can be cancelled; record a new event to change an applied one')
    CareerEvent.objects.filter(pk=event_id).update(status=EventStatus.CANCELLED)
    if ev['kind'] == Kind.SEPARATION:
        Employee.objects.filter(pk=employee_id, status=Employee.Status.ACTIVE).update(exit_date=None, updated_at=Now())
