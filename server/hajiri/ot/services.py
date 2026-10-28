"""
Overtime (port of apps/api/src/modules/ot/routes.ts): pre-approval requests that go through the approval workflow, and
the OT slips the attendance engine detects, which HR or the line manager approves, rejects or converts to substitute
leave. Only approved minutes reach payroll. Labour Act 2074 Sec 30(1) caps overtime at 4 h a day and 24 h a week;
settings never relax it.

Route-level functions take the signed-in user and the TS request body or query (camelCase keys) and return the TS
response shape, so the JSON API and the pages share every rule and message.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce, Now

from hajiri.attendance.service import enqueue_recompute
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role, manages
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import get_or_404
from hajiri.core.settings_store import get_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import AttendanceDay, Employee, OtRequest, OtSlip
from hajiri.leave.services import credit_comp_off
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import DecisionContext, assert_manages, cancel_workflow, register_handler, start_workflow

from .filters import OtRequestFilter, OtSlipFilter
from .serializers import (CAP_DAY_MIN, CAP_WEEK_MIN, OtApproveSlipIn, OtRegisterQueryIn, OtRejectSlipIn,
                          OtRequestIn, OtRequestsQueryIn, OtSlipsQueryIn)

__all__ = ['CAP_DAY_MIN', 'CAP_WEEK_MIN', 'SLIP_REVIEWERS', 'REGISTER_COLUMNS', 'week_start', 'apply_ot_decision',
           'past_window_days', 'create_request', 'requests_queryset', 'list_requests', 'cancel_request',
           'slips_queryset', 'list_slips', 'slip_for', 'approve_slip', 'reject_slip', 'comp_off_days', 'comp_off_slip',
           'register', 'register_csv']

ReqStatus = OtRequest.Status
SlipStatus = OtSlip.Status

SLIP_REVIEWERS = (*MODULE_ROLES['attendance'], 'manager')
REGISTER_COLUMNS = ('emp_code', 'name_en', 'local_date', 'bs_date', 'day_type', 'detected_min', 'approved_min',
                    'multiplier', 'status')
REQUEST_FIELDS = ('id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'requested_min', 'approved_min', 'reason',
                  'status', 'workflow_instance_id', 'created_at', 'decided_at')
SLIP_FIELDS = ('id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'detected_min', 'approved_min', 'multiplier',
               'day_type', 'status', 'posted_payslip_id', 'reviewed_by', 'reviewed_at', 'created_at')
# Pre-approvals that hold minutes of the weekly cap and block a second request for the same day.
OPEN_REQUEST = (ReqStatus.PENDING, ReqStatus.APPROVED)


def _require_role(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f"requires role {' or '.join(roles)}")


def _number(x: float | int):
    """A whole number as int, so JSON writes 1 rather than 1.0 (the TS API's numbers)."""
    return int(x) if float(x).is_integer() else x


def week_start(d: str) -> str:
    """The Sunday that starts the week of an ISO date."""
    day = date.fromisoformat(d)
    return (day - timedelta(days=(day.weekday() + 1) % 7)).isoformat()


def _with_employee(qs):
    return qs.annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'))


def _assert_in_company(company_id: str, employee_id: str) -> None:
    """An employee id from a query or body must be of the active company; another company's is not found."""
    if not Employee.objects.for_company(company_id).filter(pk=employee_id).exists():
        raise NotFoundError('employee not found')


def _latest_days():
    """attendance_day rows that are the newest computed version of their day (the v_attendance_latest view)."""
    newer = AttendanceDay.objects.filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'),
                                         version__gt=OuterRef('version'))
    return AttendanceDay.objects.exclude(Exists(newer))


# ---------------------------------------------------------------- decision handler (workflow)

def apply_ot_decision(request_id: str, state: str, ctx: DecisionContext) -> None:
    """An approved pre-approval is capped at the request and at 4 h; the day is recomputed and the employee told."""
    o = (OtRequest.objects.select_for_update(of=('self',)).annotate(company_id=F('employee__company_id'))
         .filter(pk=request_id).first())
    if not o:
        raise NotFoundError('OT request not found')
    if o.status != ReqStatus.PENDING:
        raise ConflictError(f'request already {o.status}')
    approved = None
    if state == ReqStatus.APPROVED:
        raw = (ctx.payload or {}).get('approvedMin')
        asked = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else o.requested_min
        approved = _number(min(o.requested_min, max(0, asked), CAP_DAY_MIN))
    OtRequest.objects.filter(pk=o.pk).update(status=state, approved_min=approved, decided_at=Now())
    employee_id = str(o.employee_id)
    if state == ReqStatus.APPROVED:
        enqueue_recompute([(employee_id, o.local_date.isoformat())], 'ot_preapproved')
    outcome = f'{approved} minutes approved' if state == ReqStatus.APPROVED else 'rejected'
    notify(company_id=str(o.company_id), user_id=user_id_for_employee(employee_id), employee_id=employee_id,
           template=f'ot.{state}', title='Overtime approved' if state == ReqStatus.APPROVED else 'Overtime rejected',
           body=f"Overtime on {bs_label(o.local_date)}: {outcome}{f' ({ctx.reason})' if ctx.reason else ''}.",
           payload={'requestId': str(request_id)}, channels=['push'])


register_handler('ot_request', apply_ot_decision)


# ---------------------------------------------------------------- pre-approval requests

def past_window_days(company_id: str) -> int | float:
    """How many days back an employee may ask for overtime (attendance.policy otPastDatedDays, default 7)."""
    policy = get_setting(company_id, 'attendance.policy') or {}
    w = policy.get('otPastDatedDays') if isinstance(policy, dict) else None
    return w if isinstance(w, (int, float)) and not isinstance(w, bool) else 7


def _week_used(company_id: str, employee_id: str, local_date: str) -> int:
    """Minutes of the week already worked or held by open requests, leaving out the day being asked for."""
    ws = date.fromisoformat(week_start(local_date))
    week = {'employee_id': employee_id, 'local_date__gte': ws, 'local_date__lt': ws + timedelta(days=7)}
    day = date.fromisoformat(local_date)
    done = (_latest_days().for_company(company_id).filter(**week).exclude(local_date=day)
            .aggregate(n=Coalesce(Sum('ot_min'), 0))['n'])
    held = (OtRequest.objects.for_company(company_id).filter(status__in=OPEN_REQUEST, **week).exclude(local_date=day)
            .aggregate(n=Coalesce(Sum(Coalesce('approved_min', 'requested_min')), 0))['n'])
    return max(int(done), int(held))


def create_request(user: AuthUser, body: dict) -> dict:
    """POST /ot/requests -> {'id'}. The employee themself, their manager, or HR / attendance admin for anyone."""
    b = plain(validate(OtRequestIn, body))
    cid = company_id_of(user)
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    emp = get_or_404(Employee.objects.only('id', 'ot_eligible'), cid, 'employee not found', pk=employee_id)
    if employee_id != user.employee_id:
        assert_manages(user, employee_id, 'attendance')
    if not emp.ot_eligible:
        raise BadRequestError('employee grade is not overtime-eligible (Sec 31(2) basis must be documented)')
    if b['requestedMin'] > CAP_DAY_MIN:
        raise BadRequestError(f'overtime may not exceed {CAP_DAY_MIN} minutes (4 hours) per day (Sec 30(1))')
    used = _week_used(cid, employee_id, b['localDate'])
    if used + b['requestedMin'] > CAP_WEEK_MIN:
        raise BadRequestError(f'weekly overtime cap of 24 hours would be exceeded ({used} minutes already this week)')
    window = past_window_days(cid)
    age = (npt_today() - date.fromisoformat(b['localDate'])).days
    if age > window and not has_module(user, 'attendance'):
        raise BadRequestError(f'past-dated overtime requests are limited to {_number(window)} days')
    if OtRequest.objects.filter(employee_id=employee_id, local_date=b['localDate'], status__in=OPEN_REQUEST).exists():
        raise ConflictError('an overtime request already exists for that day')
    reason = b.get('reason')
    with transaction.atomic():
        req = OtRequest.objects.create(employee_id=employee_id, local_date=b['localDate'], requested_min=b['requestedMin'],
                                       reason=reason, created_by_id=user.id)
        rid = str(req.id)
        inst = start_workflow(company_id=cid, object_type='ot_request', object_id=rid, employee_id=employee_id,
                              title='Overtime pre-approval',
                              body=f"{b['requestedMin']} minutes on {b['localDate']}{f': {reason}' if reason else ''}")
        OtRequest.objects.filter(pk=req.pk).update(workflow_instance_id=inst['id'])
        audit('create', 'ot_request', actor_id=user.id, row_id=rid, after={**b, 'employeeId': employee_id})
    return {'id': rid}


def requests_queryset(user: AuthUser, query: dict):
    """The requests GET /ot/requests may show this user, newest day first, as a values() queryset."""
    q = plain(validate(OtRequestsQueryIn, query))
    scope = q.get('scope') or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if scope == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError('scope all requires HR')
    cid = company_id_of(user)
    if q.get('employeeId'):
        _assert_in_company(cid, q['employeeId'])
        assert_manages(user, q['employeeId'], 'attendance')
    qs = OtRequestFilter(q, queryset=OtRequest.objects.for_company(cid)).qs
    me = user.employee_id
    if scope == 'own':
        qs = qs.filter(employee_id=me) if me else qs.none()
    elif scope == 'team':
        qs = qs.filter(Q(manages(me, F('employee_id'))) | Q(employee_id=me)) if me else qs.none()
    return _with_employee(qs).order_by('-local_date', '-id').values(*REQUEST_FIELDS)


def list_requests(user: AuthUser, query: dict) -> list[dict]:
    """GET /ot/requests -> the rows of {'requests'}. HR sees everyone, a manager their team, others their own."""
    return list(requests_queryset(user, query)[:500])


def cancel_request(user: AuthUser, request_id: str) -> dict:
    """POST /ot/requests/:id/cancel: a pending request is withdrawn and its approval ends."""
    rid = parse_uuid(request_id)
    o = get_or_404(OtRequest.objects.only('id', 'employee_id', 'status'), company_id_of(user), 'request not found', pk=rid)
    assert_manages(user, str(o.employee_id), 'attendance')
    if o.status != ReqStatus.PENDING:
        raise ConflictError(f'only pending requests can be cancelled (status {o.status})')
    with transaction.atomic():
        if not OtRequest.objects.filter(pk=rid, status=ReqStatus.PENDING).update(status=ReqStatus.CANCELLED, decided_at=Now()):
            raise ConflictError('request was decided in the meantime')
        cancel_workflow('ot_request', rid)
    return {'ok': True}


# ---------------------------------------------------------------- slips (detected by the attendance engine)

def slips_queryset(user: AuthUser, query: dict):
    """The slips GET /ot/slips may show this user (HR all, a manager their team and themselves, an employee their
    own), newest day first, as a values() queryset."""
    q = plain(validate(OtSlipsQueryIn, query))
    reviewer = has_role(user, *SLIP_REVIEWERS)
    if not reviewer:
        if not user.employee_id:
            raise ForbiddenError('no employee linked to this user')
        q['employeeId'] = user.employee_id         # an employee's own filter is ignored: always themselves
    cid = company_id_of(user)
    if q.get('employeeId'):
        _assert_in_company(cid, q['employeeId'])
    qs = OtSlipFilter(q, queryset=OtSlip.objects.for_company(cid)).qs
    if not has_module(user, 'attendance'):
        me = user.employee_id
        qs = qs.filter(Q(manages(me, F('employee_id'))) | Q(employee_id=me)) if me else qs.none()
    return _with_employee(qs).order_by('-local_date', '-id').values(*SLIP_FIELDS)


def list_slips(user: AuthUser, query: dict) -> list[dict]:
    """GET /ot/slips -> the rows of {'slips'}."""
    return list(slips_queryset(user, query)[:1000])


def slip_for(user: AuthUser, slip_id: str) -> OtSlip:
    """A slip this user may decide: same company, not their own, someone they manage, not posted or converted."""
    s = get_or_404(OtSlip, company_id_of(user), 'OT slip not found', pk=slip_id)
    if user.employee_id == str(s.employee_id):
        raise ForbiddenError('cannot decide your own overtime')
    assert_manages(user, str(s.employee_id), 'attendance')
    if s.status == SlipStatus.POSTED:
        raise ConflictError('slip already posted to payroll')
    if s.status == SlipStatus.COMP_OFF:
        raise ConflictError('slip already converted to substitute leave; the credit cannot be reversed here')
    return s


def approve_slip(user: AuthUser, slip_id: str, body: dict | None = None) -> dict:
    """POST /ot/slips/:id/approve -> {'ok', 'approvedMin'}, capped at the detected minutes and 4 h."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    b = validate(OtApproveSlipIn, body or {})
    s = slip_for(user, sid)
    asked = b.get('approvedMin')
    approved = min(s.detected_min if asked is None else asked, s.detected_min, CAP_DAY_MIN)
    with transaction.atomic():
        # Conditional update: a second approval (double click, two approvers at once) is a conflict, not a silent re-approval.
        if not (OtSlip.objects.filter(pk=sid, status__in=(SlipStatus.DETECTED, SlipStatus.REJECTED))
                .update(status=SlipStatus.APPROVED, approved_min=approved, reviewed_by_id=user.id, reviewed_at=Now())):
            raise ConflictError(f'slip is already {s.status}')
        audit('approve', 'ot_slip', actor_id=user.id, row_id=sid, after={'approvedMin': approved})
    return {'ok': True, 'approvedMin': approved}


def reject_slip(user: AuthUser, slip_id: str, body: dict | None) -> dict:
    """POST /ot/slips/:id/reject {reason} -> {'ok'}."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    b = validate(OtRejectSlipIn, body)
    s = slip_for(user, sid)
    with transaction.atomic():
        if not (OtSlip.objects.filter(pk=sid, status__in=(SlipStatus.DETECTED, SlipStatus.APPROVED))
                .update(status=SlipStatus.REJECTED, approved_min=0, reviewed_by_id=user.id, reviewed_at=Now())):
            raise ConflictError(f'slip is already {s.status}')
        audit('reject', 'ot_slip', actor_id=user.id, row_id=sid, after={'reason': b['reason'], 'employeeId': str(s.employee_id)})
    return {'ok': True}


def comp_off_days(minutes: int) -> float:
    """Substitute leave for overtime minutes: a working day is 8 h, rounded half up to 0.01 day."""
    return float((Decimal(minutes) / 480).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def comp_off_slip(user: AuthUser, slip_id: str) -> dict:
    """POST /ot/slips/:id/comp-off -> {'ok', 'days', 'expiresOn'}: substitute leave instead of pay (Sec 42: used within 21 days)."""
    _require_role(user, *SLIP_REVIEWERS)
    sid = parse_uuid(slip_id)
    s = slip_for(user, sid)
    minutes = s.detected_min if s.approved_min is None else s.approved_min
    days = comp_off_days(minutes)
    expires = (s.local_date + timedelta(days=21)).isoformat()
    with transaction.atomic():
        # Claim the slip first so a double click cannot credit substitute leave twice.
        if not (OtSlip.objects.filter(pk=sid).exclude(status__in=(SlipStatus.COMP_OFF, SlipStatus.POSTED, SlipStatus.REJECTED))
                .update(status=SlipStatus.COMP_OFF)):
            raise ConflictError('slip was already converted, posted or rejected')
        ledger_id = credit_comp_off(str(s.employee_id), days, expires, ref_type='ot_slip', ref_id=sid,
                                    note=f'comp-off for {minutes} min OT on {s.local_date.isoformat()}', posted_by=user.id)
        OtSlip.objects.filter(pk=sid).update(approved_min=minutes, comp_off_ledger_id=ledger_id, reviewed_by_id=user.id,
                                             reviewed_at=Now())
        audit('comp_off', 'ot_slip', actor_id=user.id, row_id=sid, after={'days': _number(days), 'expires': expires})
    return {'ok': True, 'days': _number(days), 'expiresOn': expires}


# ---------------------------------------------------------------- inspector-ready register (plan §5.10)

def register(user: AuthUser, query: dict) -> tuple[str, list[dict]]:
    """GET /ot/register -> (format, rows). Every slip in the range with the BS date of its attendance day."""
    _require_role(user, *MODULE_ROLES['attendance'])
    q = validate(OtRegisterQueryIn, query)
    bs_date = Subquery(_latest_days().filter(employee=OuterRef('employee'), local_date=OuterRef('local_date'))
                       .values('bs_date')[:1])
    data = list(_with_employee(OtSlip.objects.for_company(company_id_of(user)))
                .filter(local_date__range=(q['from'], q['to'])).annotate(bs_date=bs_date)
                .order_by('local_date', 'employee__emp_code').values(*REGISTER_COLUMNS))
    return q['format'], data


def register_csv(data: list[dict]) -> str:
    """The register as the TS wrote it: BOM, header row, CRLF line ends, formula-safe cells."""
    from hajiri.web.csv import to_csv
    return to_csv([{'key': c, 'header': c} for c in REGISTER_COLUMNS], data)
