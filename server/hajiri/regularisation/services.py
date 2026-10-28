"""
Attendance corrections (port of apps/api/src/modules/regularisation/routes.ts, plan §5.4): missed punches, work from
home, on duty, half day and shift change requests, approved through the workflow engine. An approved missed punch
becomes a real, attributable punch_event row; every approval queues a recompute of the day.

Also the HR "Fix a punch" entry (POST /punch/manual in the TS API), which the Fix a punch page uses for HR, and the
offline clock-in flush (lib/offlinePunch.ts), which files a missed-punch correction for a clock-in made without a
connection.

Bodies use the TS camelCase shape (validated with the serializers in serializers.py), so the JSON endpoints, the pages
and the tests share one code path. Every query is scoped to the user's ACTIVE company.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, time, timedelta, timezone

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.db.models.functions import Now

from engines.attendance import NPT_OFFSET_MIN

from hajiri.core import clock
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role, manages
from hajiri.core.bsdate import bs_label
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import is_uuid
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Company, Device, Employee, PunchEvent, RegularisationRequest, Shift
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import (DecisionContext, act, assert_manages, cancel_workflow, register_handler,
                                     start_workflow)

from .filters import RequestFilter
from .serializers import CorrectionQueryIn, HrPunchIn, OfflineClockIn, CorrectionIn

Kind = RegularisationRequest.Kind
Status = RegularisationRequest.Status

KINDS = tuple(Kind.values)
STATUSES = tuple(Status.values)
OBJECT_TYPE = 'regularisation_request'
NPT = clock.NPT
# Punch local dates and device times use the fixed +05:45 offset the attendance engine uses (engines.attendance), not
# the tz database, which puts Nepal on +05:30 before 1986; the two must attribute a punch to the same day.
PUNCH_TZ = timezone(timedelta(minutes=NPT_OFFSET_MIN))
UNIQUE_VIOLATION = '23505'


def _days_between(a: str, b: str) -> int:
    """b - a in whole days for two ISO dates."""
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def kind_label(kind: str) -> str:
    """'Work from home' or the kind with its first underscore as a space ('missed punch'), as the TS text."""
    return 'Work from home' if kind == Kind.WFH else str(kind).replace('_', ' ', 1)


def npt_today() -> str:
    return clock.npt_today().isoformat()


def _parse_instant(s: str) -> datetime:
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        raise BadRequestError('punchTime must be an ISO date-time')
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _npt_date(at: datetime) -> str:
    """The Nepal calendar date of an instant."""
    return at.astimezone(PUNCH_TZ).date().isoformat()


def _device_local_time(at: datetime) -> str:
    """'YYYY-MM-DD HH:MM:SS' in Nepal time, the device_local_time column."""
    return at.astimezone(PUNCH_TZ).strftime('%Y-%m-%d %H:%M:%S')


def _insert_punch(**fields) -> str | None:
    """Insert a punch_event; None when the idempotency key (with punched_at) is already there (INSERT ... ON CONFLICT
    DO NOTHING). A savepoint keeps the caller's transaction usable after the duplicate."""
    try:
        with transaction.atomic():
            return str(PunchEvent.objects.create(**fields).id)
    except IntegrityError as e:
        if getattr(e.__cause__, 'sqlstate', None) == UNIQUE_VIOLATION:
            return None
        raise


# ---------------------------------------------------------------- devices

def manual_device_id(company_id: str) -> str:
    """Device row used for HR-entered and regularised punches (one per company; vendor/protocol 'manual')."""
    code = Company.objects.filter(pk=company_id).values_list('code', flat=True).first()
    sn = f"MANUAL-{code if code else str(company_id)[:8]}"
    device, _ = Device.objects.update_or_create(
        serial_no=sn, defaults={'status': Device.Status.ACTIVE},
        create_defaults={'company_id': company_id, 'vendor': Device.Vendor.MANUAL, 'protocol': Device.Protocol.MANUAL,
                         'name': 'Manual entries', 'status': Device.Status.ACTIVE})
    return str(device.id)


# ---------------------------------------------------------------- decision handler

def apply_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """Applies an approved regularisation (plan §5.4): a missed punch becomes a real, attributable punch_event row."""
    from hajiri.attendance.service import enqueue_recompute, is_attendance_locked

    rr = (RegularisationRequest.objects.select_for_update().select_related('employee')
          .filter(pk=parse_uuid(request_id)).first())
    if rr is None:
        raise NotFoundError('regularisation request not found')
    if rr.status != Status.PENDING:
        raise ConflictError(f'request already {rr.status}')
    employee_id, company_id = str(rr.employee_id), str(rr.employee.company_id)
    local_date = rr.local_date.isoformat()
    if state == Status.APPROVED:
        # Payroll may have locked the day after the request was filed; a correction must not slip into a locked period.
        if is_attendance_locked(employee_id, local_date):
            raise ConflictError('this day is now locked by payroll; reject the request and let HR raise a supplementary correction')
    RegularisationRequest.objects.filter(pk=rr.pk).update(status=state, decided_at=Now())
    if state == Status.APPROVED:
        if rr.kind == Kind.MISSED_PUNCH and rr.punch_time:
            device = Device.objects.get(pk=manual_device_id(company_id))
            at = rr.punch_time
            punch_id = _insert_punch(
                device=device, device_sn=device.serial_no, vendor=Device.Vendor.MANUAL, device_user_pin=rr.employee.emp_code,
                employee_id=employee_id, punched_at=at, device_local_time=_device_local_time(at), local_date=_npt_date(at),
                direction=rr.punch_direction or PunchEvent.Direction.UNKNOWN, verify_mode='manual', source='regularisation',
                raw={'requestId': str(rr.pk), 'approvedBy': ctx.actor_id},
                idempotency_key=hashlib.sha256(f'regularisation|{rr.pk}'.encode()).hexdigest())
            if punch_id:
                RegularisationRequest.objects.filter(pk=rr.pk).update(resulting_punch_id=punch_id)
        enqueue_recompute([(employee_id, local_date)], 'regularisation_approved')
    notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id,
           template=f'regularisation.{state}',
           title='Attendance correction approved' if state == Status.APPROVED else 'Attendance correction rejected',
           body=f"{kind_label(rr.kind)} for {bs_label(local_date)} was {state}{': ' + ctx.reason if ctx.reason else ''}.",
           payload={'requestId': str(rr.pk)}, channels=('push',))


register_handler(OBJECT_TYPE, apply_decision)


def assert_employee(user: AuthUser, employee_id) -> str:
    """The employee's id when they work in the user's ACTIVE company, else NotFoundError('employee not found')."""
    if not is_uuid(employee_id) or not Employee.objects.for_company(company_id_of(user)).filter(pk=employee_id).exists():
        raise NotFoundError('employee not found')
    return str(employee_id)


# ---------------------------------------------------------------- policy

def policy(company_id: str) -> dict:
    """{'backdateDays', 'monthlyCap'} from the company's attendance.policy (defaults 30 and 5)."""
    v = get_setting(company_id, 'attendance.policy', {})
    if not isinstance(v, dict):
        v = {}

    def num(k, d):
        x = v.get(k)
        return x if isinstance(x, (int, float)) and not isinstance(x, bool) else d
    return {'backdateDays': num('regularisationBackdateDays', 30), 'monthlyCap': num('regularisationMonthlyCap', 5)}


# ---------------------------------------------------------------- create

def create_request(user: AuthUser, body: dict) -> dict:
    """POST /regularisation: {'id', 'status': 'pending' | 'approved'}."""
    from hajiri.attendance.service import is_attendance_locked, recompute_queued_for

    b = plain(validate(CorrectionIn, body))
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    employee_id = str(employee_id)
    on_behalf = employee_id != (str(user.employee_id) if user.employee_id else None)
    company_id = company_id_of(user)
    assert_employee(user, employee_id)
    if on_behalf:
        assert_manages(user, employee_id, 'attendance')
    kind = b['kind']
    if kind == Kind.MISSED_PUNCH and (not b.get('punchTime') or not b.get('punchDirection')):
        raise BadRequestError('punchTime and punchDirection are required for a missed punch')
    if kind == Kind.SHIFT_CHANGE:
        if not b.get('shiftId'):
            raise BadRequestError('shiftId is required for a shift change')
        if not Shift.objects.for_company(company_id).filter(pk=b['shiftId']).exists():
            raise NotFoundError('shift not found')
    punch_at = None
    if b.get('punchTime'):
        # Night shifts: the OUT punch may fall on the next calendar day (plan §10.1 attribution to the start date); never earlier.
        punch_at = _parse_instant(b['punchTime'])
        diff = _days_between(b['localDate'], _npt_date(punch_at))
        if diff < 0 or diff > 1:
            raise BadRequestError('punchTime must be on the attendance date or the following day')
    today = npt_today()
    days_ahead = _days_between(today, b['localDate'])
    # Work-from-home days are planned ahead (up to 60 days); every other correction is for a day that has happened.
    if (days_ahead > 60) if kind == Kind.WFH else (days_ahead > 0):
        raise BadRequestError('work from home can be requested up to 60 days ahead' if kind == Kind.WFH
                              else 'cannot regularise a future date')
    pol = policy(company_id)
    is_hr = has_module(user, 'attendance')
    requests = RegularisationRequest.objects.for_company(company_id)
    if not is_hr:
        age_days = _days_between(b['localDate'], today)
        if age_days > pol['backdateDays']:
            raise BadRequestError(f"corrections can only be requested within {pol['backdateDays']} days")
        if kind != Kind.WFH:
            # The cap limits what an employee files for themself; HR/manager corrections (device outages) do not count against it.
            day = date.fromisoformat(b['localDate'])
            n = (requests.filter(employee_id=employee_id, local_date__year=day.year, local_date__month=day.month,
                                 status__in=(Status.PENDING, Status.APPROVED), created_by_id=user.id)
                 .exclude(kind=Kind.WFH).count())
            if n >= pol['monthlyCap']:
                raise BadRequestError(f"monthly limit of {pol['monthlyCap']} corrections reached; contact HR")
    if is_attendance_locked(employee_id, b['localDate']):
        raise ConflictError('this day is locked by payroll; HR must raise a supplementary correction')
    if requests.filter(employee_id=employee_id, local_date=b['localDate'], kind=kind, status=Status.PENDING).exists():
        raise ConflictError('a pending request of this kind already exists for that day')

    state = Status.PENDING
    with transaction.atomic():
        rr = RegularisationRequest.objects.create(
            employee_id=employee_id, local_date=b['localDate'], kind=kind, punch_time=punch_at,
            punch_direction=b.get('punchDirection'), shift_id=b.get('shiftId'), reason=b['reason'],
            attachment_key=b.get('attachmentKey'), created_by_id=user.id)
        rid = str(rr.pk)
        inst = start_workflow(company_id=company_id, object_type=OBJECT_TYPE, object_id=rid, employee_id=employee_id,
                              title='Attendance correction to approve',
                              body=f"{kind_label(kind)} on {bs_label(b['localDate'])}: {b['reason']}")
        RegularisationRequest.objects.filter(pk=rr.pk).update(workflow_instance_id=inst['id'])
        audit('create', 'regularisation_request', actor_id=user.id, row_id=rid,
              after={**b, 'employeeId': employee_id, 'onBehalf': on_behalf})
        # HR fixing a punch on behalf of a worker is approved immediately but still leaves a signed action (plan §3 "Fix punch (audited)").
        if is_hr and on_behalf and kind == Kind.MISSED_PUNCH:
            state = act(instance_id=inst['id'], user=user, decision='approve', reason='fixed by HR on behalf of the employee')
    if state == Status.APPROVED:
        recompute_queued_for(employee_id)
    return {'id': rid, 'status': Status.APPROVED.value if state == Status.APPROVED else Status.PENDING.value}


# ---------------------------------------------------------------- list

LIST_FIELDS = ('id', 'employee_id', 'local_date', 'kind', 'punch_time', 'punch_direction', 'shift_id', 'reason',
               'attachment_key', 'status', 'workflow_instance_id', 'resulting_punch_id', 'created_by_id', 'created_at',
               'decided_at')


def requests_queryset(user: AuthUser, query: dict):
    """The company's correction requests the user may list (own, team or, for the attendance area, all), filtered by
    ?employeeId=&status=&from=&to= (RequestFilter)."""
    q = plain(validate(CorrectionQueryIn, {k: v for k, v in query.items() if v not in (None, '')}))
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    company_id = company_id_of(user)
    if q.get('employeeId'):
        assert_employee(user, q['employeeId'])
        assert_manages(user, q['employeeId'], 'attendance')
    qs = RequestFilter(q, queryset=RegularisationRequest.objects.for_company(company_id)).qs
    me = user.employee_id
    if scope == 'own':
        qs = qs.filter(employee_id=me) if me else qs.none()
    elif scope == 'team':
        qs = qs.filter(Q(manages(me, F('employee_id'))) | Q(employee_id=me)) if me else qs.none()
    return qs


def list_requests(user: AuthUser, query: dict) -> list[dict]:
    """GET /regularisation: own, team or (attendance area) all requests, newest first, at most 500."""
    out = list(requests_queryset(user, query).order_by('-created_at', '-id')
               .values(*LIST_FIELDS, emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))[:500])
    for r in out:
        r['created_by'] = r.pop('created_by_id')
    return out


# ---------------------------------------------------------------- cancel

def cancel_request(user: AuthUser, request_id: str) -> dict:
    """POST /regularisation/:id/cancel: only while pending."""
    rid = parse_uuid(str(request_id))
    # user.company_id, not company_id_of(): an account with no company gets "request not found", as before.
    rr = (RegularisationRequest.objects.for_company(user.company_id).filter(pk=rid)
          .values('employee_id', 'status').first())
    if rr is None:
        raise NotFoundError('request not found')
    assert_manages(user, str(rr['employee_id']), 'attendance')
    # Approved corrections already produced a punch/decision; they cannot be undone here (a new request or HR adjustment supersedes).
    if rr['status'] != Status.PENDING:
        raise ConflictError(f"only pending requests can be cancelled (status {rr['status']})")
    with transaction.atomic():
        # Conditional update: an approval landing at the same moment wins, and the cancel reports the conflict.
        n = (RegularisationRequest.objects.filter(pk=rid, status=Status.PENDING)
             .update(status=Status.CANCELLED, decided_at=Now()))
        if not n:
            raise ConflictError('request was decided in the meantime')
        cancel_workflow(OBJECT_TYPE, rid)
        audit('cancel', 'regularisation_request', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- HR manual punch (TS POST /punch/manual)

def manual_punch(user: AuthUser, body: dict) -> dict:
    """HR manual entry with reason (plan §5.2); audited; day recomputed. {'duplicate', 'punchId'?, 'day'?}."""
    from hajiri.attendance.service import is_attendance_locked, recompute_for_punch
    from hajiri.punch.services import ensure_pseudo_device

    if not has_module(user, 'attendance'):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES['attendance'])}")
    b = plain(validate(HrPunchIn, body))
    if b['employeeId'] == (str(user.employee_id) if user.employee_id else None) and not has_role(user, 'admin'):
        raise ForbiddenError('someone else must add punches to your own attendance')
    company_id = company_id_of(user)
    emp_code = (Employee.objects.for_company(company_id).filter(pk=b['employeeId'])
                .values_list('emp_code', flat=True).first())
    if emp_code is None:
        raise NotFoundError('employee not found')
    if is_attendance_locked(b['employeeId'], b['localDate']):
        raise ConflictError('this day is locked by payroll; raise a supplementary correction instead')
    at = datetime.combine(date.fromisoformat(b['localDate']), time.fromisoformat(b['time']), tzinfo=PUNCH_TZ)
    with transaction.atomic():
        device = ensure_pseudo_device(company_id, 'MANUAL')
        key = hashlib.sha256(f"manual|{b['employeeId']}|{b['localDate']}|{b['time']}|{b['direction']}".encode()).hexdigest()
        punch_id = _insert_punch(
            device_id=device['id'], device_sn=device['serialNo'], vendor=device['vendor'], device_user_pin=emp_code,
            employee_id=b['employeeId'], punched_at=at, device_local_time=_device_local_time(at), local_date=b['localDate'],
            direction=b['direction'], verify_mode='manual', source='manual',
            raw={'reason': b['reason'], 'enteredBy': user.id}, idempotency_key=key)
        if punch_id is None:
            return {'duplicate': True}
        audit('manual_punch', 'punch_event', actor_id=user.id, row_id=punch_id, after=b)
    result = recompute_for_punch(b['employeeId'], int(at.timestamp()) * 1000, punch_id)
    return {'duplicate': False, 'punchId': punch_id, 'day': result.day}


# ---------------------------------------------------------------- offline clock-ins (lib/offlinePunch.ts)

def offline_reason(direction: str, at: str | datetime) -> str:
    from hajiri.web.format import fmt_time
    return (f'Clocked {direction} at {fmt_time(at)} while offline (time from my phone); '
            'sent automatically when the connection came back.')


def flush_offline_punch(user: AuthUser, body: dict) -> dict:
    """One queued offline clock-in becomes a missed-punch correction for the signed-in user, with the phone's time."""
    p = validate(OfflineClockIn, body)
    if p.get('userId') and p['userId'] != user.id:
        # The queue is per account on a shared phone; another account's clock-in is never filed under this one.
        raise ForbiddenError('this clock-in was saved for another account')
    return create_request(user, {'localDate': p['localDate'], 'kind': Kind.MISSED_PUNCH.value, 'punchTime': p['at'],
                                 'punchDirection': p['direction'], 'reason': offline_reason(p['direction'], p['at'])})


# ---------------------------------------------------------------- helpers for the pages

def shift_options(company_id: str) -> list[dict]:
    """Shifts of the company for the 'Shift worked' select: '<name or code>, HH:MM to HH:MM'."""
    return [{'id': str(s['id']), 'label': f"{s['name_en'] or s['code']}, {s['start_time']:%H:%M} to {s['end_time']:%H:%M}"}
            for s in Shift.objects.for_company(company_id).order_by('code').values('id', 'code', 'name_en', 'start_time',
                                                                                  'end_time')]
