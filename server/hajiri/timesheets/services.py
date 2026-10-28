"""
Project time tracking and weekly timesheets (port of apps/api/src/modules/timesheets/service.ts and routes.ts).

Employees run a timer on the server clock or log time by hand against projects and activities; a week (Sunday to
Saturday) is submitted for approval through the workflow engine and is locked while it waits and once approved.
Managers see their team, HR (the attendance area) everyone; reports roll the time up by project, client, person,
activity or day with the company's rounding, billable value and budget burn.

Every function takes the signed-in AuthUser and the request body or query in the TS (camelCase) shape, validates it
with the serializers in serializers.py (the zod rules and messages) and raises the TS errors with the TS messages.
Every query is scoped to the user's ACTIVE company; an id of another company is not found. Rows come back in the TS
JSON shape (snake_case columns, ids and calendar dates as strings).
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from django.contrib.postgres.expressions import ArraySubquery
from django.db import models, transaction
from django.db.models import Count, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce, JSONObject, Lower, Now

from engines.attendance import MIN_MS, local_date_of, local_time_ms, parse_hhmm, round_minutes
from hajiri.attendance.repo import latest_days
from hajiri.core.audit import audit
from hajiri.core.numbers import round_half_up, to_fixed, whole
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role, manages
from hajiri.core.bsdate import bs_label, bs_range
from hajiri.core.clock import NPT
from hajiri.core.clock import now as _now
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.core.settings_store import get_setting, put_setting, setting_subquery
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (
    Employee, NotificationOutbox, TimeActivity, TimeClient, TimeEntry, TimeProject, TimeProjectMember,
    Timesheet,
)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import (DecisionContext, assert_manages, cancel_workflow, npt_today, register_handler,
                                     register_summariser, start_workflow)

from .filters import ReportEntryFilter, TimesheetFilter
from .filters import apply as apply_filters
from .serializers import (  # noqa: F401  (GROUP_BY, ROUNDING_STEPS and STATUSES are read by the views)
    DAY_LIMIT_MIN, GROUP_BY, ROUNDING_STEPS, STATUSES, ActivityIn, ActivityPatchIn, ActivityQueryIn, ClientIn,
    ClientPatchIn, EntriesQueryIn, EntryIn, ProjectIn, ReportQueryIn, StopIn, SubmitIn, TeamWeekQueryIn, TimePolicyIn,
    TimerIn, TimesheetsQueryIn, WeekQueryIn,
)

# Days where tracked and attended time differ by more than this are flagged for the reviewer.
DISCREPANCY_MIN = 60
TIME_POLICY_KEY = 'time.policy'
DEFAULT_COLOR = '#12773a'

TS = Timesheet.Status
LOCKED = (TS.SUBMITTED, TS.APPROVED)


# ---------------------------------------------------------------- small helpers

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(d: datetime) -> int:
    """Epoch milliseconds: the time unit of the attendance engine (engines.attendance)."""
    return (d - _EPOCH) // timedelta(milliseconds=1)


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _clean(r: dict | None) -> dict | None:
    """A row in the TS JSON shape: UUIDs and calendar dates as strings (timestamps stay datetimes)."""
    if r is None:
        return None
    out = {}
    for k, v in r.items():
        if isinstance(v, uuid.UUID):
            v = str(v)
        elif isinstance(v, date) and not isinstance(v, datetime):
            v = v.isoformat()
        out[k] = v
    return out


def _clean_all(rs) -> list[dict]:
    return [_clean(r) for r in rs]





def fixed_num(x: float, digits: int = 2) -> float | int:
    """A number rounded to `digits` decimals for display and CSV."""
    return whole(float(to_fixed(x, digits)))


def _decimal(v: float | None) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _require_module(user: AuthUser, module: str) -> None:
    roles = MODULE_ROLES[module]
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _lock(employee_id: str) -> None:
    """Serialise one person's time writes (overlap and day-total checks) on their employee row. A no-key lock does not
    block inserts that reference the employee."""
    Employee.objects.select_for_update(no_key=True).filter(pk=employee_id).values_list('pk', flat=True).first()


def _plus_days(expr, days: int) -> ExpressionWrapper:
    return ExpressionWrapper(expr + Value(timedelta(days=days)), output_field=models.DateField())


def _sum(qs: models.QuerySet, field: str, group: str) -> Coalesce:
    """coalesce((SELECT sum(field) FROM qs), 0) for a correlated qs that is already narrowed to one `group` value."""
    return Coalesce(Subquery(qs.order_by().values(group).annotate(s=Sum(field)).values('s')[:1]), 0,
                    output_field=IntegerField())


# ---------------------------------------------------------------- policy

def default_policy() -> dict:
    return validate(TimePolicyIn, {})


def load_time_policy(company_id: str) -> dict:
    value = get_setting(company_id, TIME_POLICY_KEY, {})
    try:
        return validate(TimePolicyIn, value if isinstance(value, dict) else {})
    except ValidationError:
        return default_policy()


def round_entry(minutes: int, policy: dict) -> int:
    return int(round_minutes(minutes, policy.get('roundingStep', 0), policy.get('roundingMode', 'nearest')))


# ---------------------------------------------------------------- weeks (Sunday to Saturday)

def _d(iso: str | date) -> date:
    return iso if isinstance(iso, date) else date.fromisoformat(str(iso)[:10])


def add_days_iso(iso: str | date, days: int) -> str:
    return (_d(iso) + timedelta(days=days)).isoformat()


def _weekday(iso: str | date) -> int:
    """Sunday 0 .. Saturday 6."""
    return _d(iso).isoweekday() % 7


def week_start_of(iso: str | date) -> str:
    return add_days_iso(iso, -_weekday(iso))


def is_sunday(iso: str | date) -> bool:
    return _weekday(iso) == 0


def assert_week_open(employee_id: str, local_date: str) -> None:
    """Submitted and approved weeks are locked: their entries can no longer be added, changed or removed."""
    status = (Timesheet.objects.filter(employee_id=employee_id, week_start=week_start_of(local_date), status__in=LOCKED)
              .values_list('status', flat=True).first())
    if status:
        raise ConflictError("This week's timesheet is approved and locked." if status == TS.APPROVED
                            else "This week's timesheet is waiting for approval. Withdraw it to make changes.")


def minutes_from_ms(ms: float) -> int:
    return max(1, round_half_up(ms / MIN_MS))


# ---------------------------------------------------------------- auto-stop

def auto_stop_stale_timers(now: datetime | None = None, employee_id: str | None = None) -> int:
    """
    Stop timers that have run longer than their company's limit, ending them exactly at the limit (so a forgotten timer
    never logs a whole night), and tell the employee. Optionally limited to one employee for the lazy check on read.
    """
    at = now or _now()
    limit = Coalesce(setting_subquery(TIME_POLICY_KEY, 'autoStopHours', output_field=IntegerField()), 12)
    span = ExpressionWrapper(F('limit_hours') * Value(timedelta(hours=1)), output_field=models.DurationField())
    qs = (TimeEntry.objects.filter(ended_at__isnull=True, duration_min__isnull=True)
          .annotate(limit_hours=limit)
          .filter(started_at__lte=ExpressionWrapper(Value(at) - span, output_field=models.DateTimeField())))
    if employee_id:
        qs = qs.filter(employee_id=employee_id)
    with transaction.atomic():
        stale = list(qs.select_for_update(of=('self',)))
        for t in stale:
            t.ended_at = t.started_at + timedelta(hours=t.limit_hours)
            t.duration_min = t.limit_hours * 60
            t.auto_stopped = True
            t.updated_at = Now()
        if stale:
            TimeEntry.objects.bulk_update(stale, ['ended_at', 'duration_min', 'auto_stopped', 'updated_at'])
        for t in stale:
            emp = str(t.employee_id)
            notify(company_id=str(t.company_id), user_id=user_id_for_employee(emp), employee_id=emp,
                   template='time.timer_auto_stopped', title='Timer stopped automatically',
                   body=(f'Your timer from {bs_label(t.local_date.isoformat())} ran for more than {t.limit_hours} hours '
                         'and was stopped. Check the entry and correct it if needed.'),
                   payload={'entryId': str(t.id)})
    return len(stale)


# ---------------------------------------------------------------- Friday reminder

def friday_timesheet_reminders(now: datetime | None = None) -> int:
    """On Fridays (Nepal time), remind people who track time and have not submitted this week's timesheet. Once per week."""
    at = now or _now()
    today = local_date_of(_ms(at))
    if _weekday(today) != 5:
        return 0
    week = week_start_of(today)
    enabled = Coalesce(setting_subquery(TIME_POLICY_KEY, 'enabled', output_field=models.BooleanField()), True)
    remind = Coalesce(setting_subquery(TIME_POLICY_KEY, 'fridayReminder', output_field=models.BooleanField()), True)
    submitted = Timesheet.objects.filter(employee=OuterRef('employee_id'), week_start=week, status__in=LOCKED)
    people = (TimeEntry.objects.filter(local_date__gte=_d(week) - timedelta(days=28), employee__status=Employee.Status.ACTIVE)
              .annotate(tracking=enabled, reminding=remind).filter(tracking=True, reminding=True)
              .exclude(Exists(submitted))
              .order_by('employee_id').values_list('employee_id', 'company_id').distinct())
    sent = 0
    with transaction.atomic():
        for employee_id, company_id in people:
            emp = str(employee_id)
            # The account that works as this employee: the home account or a group member's membership record.
            user_id = user_id_for_employee(emp)
            if not user_id or NotificationOutbox.objects.filter(user_id=user_id, template='timesheet.reminder',
                                                                payload__weekStart=week).exists():
                continue
            notify(company_id=str(company_id), user_id=user_id, employee_id=emp,
                   template='timesheet.reminder', title='Submit your timesheet',
                   body=(f'Your timesheet for {bs_range(week, add_days_iso(week, 6))} has not been submitted yet. '
                         'Check your hours and submit it before the week ends.'),
                   payload={'weekStart': week}, channels=('push',))
            sent += 1
    return sent


def daily_time_jobs(now: datetime | None = None) -> dict:
    return {'autoStopped': auto_stop_stale_timers(now), 'reminded': friday_timesheet_reminders(now)}


# ---------------------------------------------------------------- approval

def apply_timesheet_decision(object_id: str, state: str, ctx: DecisionContext) -> None:
    t = Timesheet.objects.select_for_update().filter(pk=object_id).first()
    if not t:
        raise NotFoundError('timesheet not found')
    if t.status != TS.SUBMITTED:
        raise ConflictError(f'timesheet already {t.status}')
    Timesheet.objects.filter(pk=t.pk).update(status=state, decided_at=Now(), decided_by_id=ctx.actor_id,
                                              decision_comment=ctx.reason)
    emp = str(t.employee_id)
    ws = t.week_start.isoformat()
    verb = 'approved' if state == TS.APPROVED else 'returned for changes'
    notify(company_id=str(t.company_id), user_id=user_id_for_employee(emp), employee_id=emp, template=f'timesheet.{state}',
           title='Timesheet approved' if state == TS.APPROVED else 'Timesheet returned',
           body=f"Your timesheet for {bs_range(ws, add_days_iso(ws, 6))} was {verb}{f': {ctx.reason}' if ctx.reason else '.'}",
           payload={'timesheetId': str(object_id), 'weekStart': ws}, channels=('push',))


def _week_entries(employee_id, week_start: str) -> models.QuerySet:
    return TimeEntry.objects.filter(employee_id=employee_id, local_date__range=(week_start, add_days_iso(week_start, 6)))


def summarise_timesheet(object_id: str) -> dict:
    """Inbox card details for a timesheet approval."""
    t = Timesheet.objects.filter(pk=object_id).values('week_start', 'total_min', 'note', 'employee_id').first()
    if not t:
        return {}
    ws = t['week_start'].isoformat()
    by_project = list(_week_entries(t['employee_id'], ws).filter(duration_min__isnull=False)
                      .values(name=Coalesce(F('project__name'), Value('No project'), output_field=models.TextField()))
                      .annotate(min=Sum('duration_min')).order_by('-min')[:5])
    worked = (latest_days().filter(employee_id=t['employee_id'], local_date__range=(ws, add_days_iso(ws, 6)))
              .aggregate(m=Coalesce(Sum('worked_min'), 0))['m'])
    return {'weekStart': ws, 'weekEnd': add_days_iso(ws, 6), 'totalMin': t['total_min'], 'attendanceMin': worked,
            'note': t['note'], 'projects': by_project}


register_handler('timesheet', apply_timesheet_decision)
register_summariser('timesheet', lambda inst: summarise_timesheet(inst['object_id']))


# ---------------------------------------------------------------- access

def own_employee(user: AuthUser) -> str:
    """The signed-in person's own employee record (in the active company); time is always logged by the person who
    did the work."""
    if not user.employee_id:
        raise ForbiddenError('Only employees can track time. Link this account to an employee first.')
    return str(user.employee_id)


def readable_employee(user: AuthUser, employee_id: str | None) -> str:
    """Whose time a request may read: own by default; a manager their team; HR anyone in the company."""
    if not employee_id or employee_id == user.employee_id:
        return own_employee(user)
    if not Employee.objects.for_company(company_id_of(user)).filter(pk=employee_id).exists():
        raise NotFoundError('employee not found')
    assert_manages(user, employee_id, 'attendance')
    return employee_id


def enabled_policy(company_id: str) -> dict:
    p = load_time_policy(company_id)
    if not p['enabled']:
        raise ForbiddenError('Time tracking is turned off for your company.')
    return p


def scope_of(user: AuthUser, asked: str | None) -> str:
    s = asked or ('all' if has_module(user, 'attendance') else 'team' if has_role(user, 'manager') else 'own')
    if s == 'all' and not has_module(user, 'attendance'):
        raise ForbiddenError("Only HR can see everyone's time.")
    if s == 'team' and not has_role(user, *MODULE_ROLES['attendance'], 'manager'):
        raise ForbiddenError("Only managers can see their team's time.")
    return s


def in_scope(qs: models.QuerySet, scope: str, viewer: str | None, field: str = 'employee_id') -> models.QuerySet:
    """Rows of the people a scope covers: 'own' the viewer, 'team' the viewer and whoever they manage today, 'all'
    everyone (the caller has already scoped qs to the company). Without an employee record 'own' and 'team' are
    nobody."""
    if scope == 'all':
        return qs
    if not viewer:
        return qs.none()
    if scope == 'own':
        return qs.filter(**{field: viewer})
    return qs.filter(Q(**{field: viewer}) | manages(viewer, F(field)))


# ---------------------------------------------------------------- settings

def get_settings(user: AuthUser) -> dict:
    return {'policy': load_time_policy(company_id_of(user))}


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    value = validate(TimePolicyIn, body)
    with transaction.atomic():
        before = load_time_policy(company_id)
        put_setting(company_id, TIME_POLICY_KEY, value, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=TIME_POLICY_KEY, before=before, after=value)
    return {'policy': value}


# ---------------------------------------------------------------- clients

CLIENT_FIELDS = ('id', 'name', 'notes', 'archived')


def list_clients(user: AuthUser, all_: bool = False) -> list[dict]:
    qs = TimeClient.objects.for_company(company_id_of(user))
    if not (all_ and has_module(user, 'attendance')):
        qs = qs.filter(archived=False)
    active = TimeProject.objects.filter(client=OuterRef('pk'), status=TimeProject.Status.ACTIVE)
    qs = qs.annotate(active_projects=Coalesce(
        Subquery(active.order_by().values('client').annotate(n=Count('id')).values('n')[:1]), 0))
    return _clean_all(qs.order_by('archived', Lower('name')).values(*CLIENT_FIELDS, 'active_projects'))


def create_client(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = validate(ClientIn, body)
    with transaction.atomic():
        c = TimeClient.objects.create(company_id=company_id_of(user), name=b['name'], notes=b.get('notes') or None)
        audit('create', 'time_client', actor_id=user.id, row_id=str(c.id), after=b)
    return {'id': str(c.id), 'name': c.name, 'notes': c.notes, 'archived': False}


def update_client(user: AuthUser, client_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    cid = parse_uuid(client_id)
    b = validate(ClientPatchIn, body)
    changes: dict[str, Any] = {}
    if b.get('name') is not None:
        changes['name'] = b['name']
    if 'notes' in b:
        changes['notes'] = b['notes'] or None
    if b.get('archived') is not None:
        changes['archived'] = b['archived']
    with transaction.atomic():
        qs = TimeClient.objects.for_company(company_id_of(user)).filter(pk=cid)
        if not (qs.update(**changes) if changes else qs.exists()):
            raise NotFoundError('client not found')
        audit('update', 'time_client', actor_id=user.id, row_id=cid, after=b)
        return _clean(qs.values(*CLIENT_FIELDS).first())


# ---------------------------------------------------------------- projects

PROJECT_FIELDS = ('id', 'code', 'name', 'color', 'billable', 'bill_rate', 'budget_hours', 'all_members', 'status',
                  'client_id')


def projects(user: AuthUser, all_: bool = False) -> models.QuerySet:
    """Employees see the active projects they may log to; HR with all=1 sees every project; HR also sees members."""
    hr = has_module(user, 'attendance')
    everything = hr and bool(all_)
    tracked = _sum(TimeEntry.objects.filter(project=OuterRef('pk')), 'duration_min', 'project')
    if hr:
        members = ArraySubquery(TimeProjectMember.objects.filter(project=OuterRef('pk')).order_by('employee__name_en')
                                .values(json=JSONObject(id='employee_id', name='employee__name_en',
                                                        empCode='employee__emp_code')))
    else:
        members = Value([], output_field=models.JSONField())
    qs = (TimeProject.objects.for_company(company_id_of(user))
          .annotate(client_name=F('client__name'), tracked_min=tracked, members=members))
    if not everything:
        on_project = TimeProjectMember.objects.filter(project=OuterRef('pk'), employee_id=user.employee_id)
        qs = qs.filter(Q(all_members=True) | Exists(on_project), status=TimeProject.Status.ACTIVE)
    return qs.order_by('status', Lower('name'))


def _project_rows(qs: models.QuerySet) -> list[dict]:
    return _clean_all(qs.values(*PROJECT_FIELDS, 'client_name', 'tracked_min', 'members'))


def list_projects(user: AuthUser, all_: bool = False) -> list[dict]:
    return _project_rows(projects(user, all_))


def get_project(user: AuthUser, project_id: str) -> dict:
    """One project as list_projects(all=1) shows it, for the edit form (HR)."""
    _require_module(user, 'attendance')
    found = _project_rows(projects(user, True).filter(pk=parse_uuid(project_id)))
    if not found:
        raise NotFoundError('project not found')
    return found[0]


def _set_members(company_id: str, project_id: str, ids: list[str]) -> None:
    unique = list(dict.fromkeys(ids))
    if unique and Employee.objects.for_company(company_id).filter(pk__in=unique).count() != len(unique):
        raise BadRequestError('Some members are not employees of this company.')
    TimeProjectMember.objects.filter(project_id=project_id).delete()
    TimeProjectMember.objects.bulk_create([TimeProjectMember(project_id=project_id, employee_id=e) for e in unique])


def _check_client(company_id: str, client_id: str | None) -> None:
    if client_id and not TimeClient.objects.for_company(company_id).filter(pk=client_id).exists():
        raise BadRequestError('Client not found.')


def create_project(user: AuthUser, body: dict) -> str:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = plain(validate(ProjectIn, body))
    with transaction.atomic():
        _check_client(company_id, b.get('clientId'))
        p = TimeProject.objects.create(
            company_id=company_id, client_id=b.get('clientId'), code=b['code'].upper(), name=b['name'],
            color=b.get('color') or DEFAULT_COLOR, billable=b.get('billable', True), bill_rate=_decimal(b.get('billRate')),
            budget_hours=_decimal(b.get('budgetHours')), all_members=b.get('allMembers', True),
            status=b.get('status') or TimeProject.Status.ACTIVE)
        pid = str(p.id)
        if 'memberIds' in b:
            _set_members(company_id, pid, b['memberIds'])
        audit('create', 'time_project', actor_id=user.id, row_id=pid, after=b)
    return pid


def update_project(user: AuthUser, project_id: str, body: dict) -> None:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    pid = parse_uuid(project_id)
    b = plain(validate(ProjectIn, body, partial=True))
    changes: dict[str, Any] = {'updated_at': Now()}
    for key, col in (('code', 'code'), ('name', 'name'), ('color', 'color'), ('billable', 'billable'),
                     ('allMembers', 'all_members'), ('status', 'status')):
        if b.get(key) is not None:
            changes[col] = b[key].upper() if key == 'code' else b[key]
    if 'clientId' in b:
        changes['client_id'] = b['clientId']
    if 'billRate' in b:
        changes['bill_rate'] = _decimal(b['billRate'])
    if 'budgetHours' in b:
        changes['budget_hours'] = _decimal(b['budgetHours'])
    with transaction.atomic():
        qs = TimeProject.objects.for_company(company_id).filter(pk=pid)
        cur = qs.select_for_update().values().first()
        if not cur:
            raise NotFoundError('project not found')
        _check_client(company_id, b.get('clientId'))
        qs.update(**changes)
        if 'memberIds' in b:
            _set_members(company_id, pid, b['memberIds'])
        audit('update', 'time_project', actor_id=user.id, row_id=pid, before=_clean(cur), after=b)


# ---------------------------------------------------------------- activities

ACTIVITY_FIELDS = ('id', 'name', 'project_id', 'archived')


def list_activities(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(ActivityQueryIn, query or {}))
    qs = TimeActivity.objects.for_company(company_id_of(user)).annotate(project_name=F('project__name'))
    if not (q.get('all') == '1' and has_module(user, 'attendance')):
        qs = qs.filter(archived=False)
    if q.get('projectId'):
        qs = qs.filter(Q(project__isnull=True) | Q(project_id=q['projectId']))
    qs = qs.order_by(F('project_id').asc(nulls_first=True), Lower('name'))
    return _clean_all(qs.values('id', 'name', 'project_id', 'project_name', 'archived'))


def create_activity(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = plain(validate(ActivityIn, body))
    if b.get('projectId') and not TimeProject.objects.for_company(company_id).filter(pk=b['projectId']).exists():
        raise BadRequestError('Project not found.')
    with transaction.atomic():
        a = TimeActivity.objects.create(company_id=company_id, project_id=b.get('projectId'), name=b['name'])
        audit('create', 'time_activity', actor_id=user.id, row_id=str(a.id), after=b)
    return {'id': str(a.id), 'name': a.name, 'project_id': b.get('projectId'), 'archived': False}


def update_activity(user: AuthUser, activity_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    aid = parse_uuid(activity_id)
    b = validate(ActivityPatchIn, body)
    changes = {k: b[k] for k in ('name', 'archived') if b.get(k) is not None}
    with transaction.atomic():
        qs = TimeActivity.objects.for_company(company_id_of(user)).filter(pk=aid)
        if not (qs.update(**changes) if changes else qs.exists()):
            raise NotFoundError('activity not found')
        return _clean(qs.values(*ACTIVITY_FIELDS).first())


# ---------------------------------------------------------------- entries: shared checks

def check_work(company_id: str, employee_id: str, policy: dict, project_id: str | None, activity_id: str | None) -> dict:
    """Project and activity checks shared by the timer and manual entries. Returns the project's default billable flag."""
    usable = TimeActivity.objects.for_company(company_id).filter(pk=activity_id, archived=False) if activity_id else None
    if not project_id:
        if policy['requireProject']:
            raise BadRequestError('Pick a project for this time.')
        if usable is not None and not usable.filter(project__isnull=True).exists():
            raise BadRequestError('That activity is not available without a project.')
        return {'billable': False}
    proj = TimeProject.objects.for_company(company_id).filter(pk=project_id).first()
    if not proj:
        raise BadRequestError('Project not found.')
    if proj.status != TimeProject.Status.ACTIVE:
        raise BadRequestError(f'{proj.name} is archived.')
    if not proj.all_members and not TimeProjectMember.objects.filter(project=proj, employee_id=employee_id).exists():
        raise ForbiddenError(f'You are not a member of {proj.name}.')
    if usable is not None and not usable.filter(Q(project__isnull=True) | Q(project=proj)).exists():
        raise BadRequestError('That activity does not belong to this project.')
    return {'billable': proj.billable}


def day_total(employee_id: str, local_date: str, exclude_id: str | None) -> int:
    qs = TimeEntry.objects.filter(employee_id=employee_id, local_date=local_date)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    return qs.aggregate(m=Coalesce(Sum('duration_min'), 0))['m']


def assert_no_overlap(employee_id: str, start: datetime, end: datetime, exclude_id: str | None) -> None:
    """[start, end) against every timed entry; a running timer reaches to infinity."""
    qs = TimeEntry.objects.filter(Q(ended_at__isnull=True) | Q(ended_at__gt=start), employee_id=employee_id,
                                  started_at__isnull=False, started_at__lt=end)
    if exclude_id:
        qs = qs.exclude(pk=exclude_id)
    if qs.exists():
        raise ConflictError('This time overlaps another entry or your running timer.')


ENTRY_FIELDS = ('id', 'employee_id', 'local_date', 'started_at', 'ended_at', 'duration_min', 'project_id', 'project_name',
                'project_code', 'project_color', 'client_name', 'activity_id', 'activity_name', 'note', 'billable',
                'source', 'auto_stopped', 'created_at', 'updated_at')


def entries() -> models.QuerySet:
    """Time entries with their project, client and activity names (ENTRY_COLS in the TS code)."""
    return TimeEntry.objects.annotate(project_name=F('project__name'), project_code=F('project__code'),
                                      project_color=F('project__color'), client_name=F('project__client__name'),
                                      activity_name=F('activity__name'))


def _entry_rows(qs: models.QuerySet) -> list[dict]:
    return _clean_all(qs.values(*ENTRY_FIELDS))


def _first(qs: models.QuerySet) -> dict | None:
    found = _entry_rows(qs[:1])
    return found[0] if found else None


def entry_by_id(entry_id: str) -> dict | None:
    return _first(entries().filter(pk=entry_id))


def resolve_span(b: dict, now_ms: int | None = None) -> dict:
    """Resolve start/end or a bare duration into the stored shape, checking the clock and the day."""
    now_ms = _ms(_now()) if now_ms is None else now_ms
    if b['date'] > local_date_of(now_ms):
        raise BadRequestError('You cannot log time for a future day.')
    if b.get('start') or b.get('end'):
        if not b.get('start') or not b.get('end'):
            raise BadRequestError('Give both a start and an end time, or just a duration.')
        if parse_hhmm(b['end']) <= parse_hhmm(b['start']):
            raise BadRequestError('The end time must be after the start time.')
        s = local_time_ms(b['date'], b['start'])
        e = local_time_ms(b['date'], b['end'])
        if e > now_ms:
            raise BadRequestError('You cannot log time that has not happened yet.')
        return {'startedAt': _dt(s), 'endedAt': _dt(e), 'durationMin': round_half_up((e - s) / 60_000)}
    if not b.get('durationMin'):
        raise BadRequestError('Give a start and end time or a duration.')
    return {'startedAt': None, 'endedAt': None, 'durationMin': b['durationMin']}


# ---------------------------------------------------------------- timer (server clock only)

def _running(employee_id: str) -> models.QuerySet:
    return TimeEntry.objects.filter(employee_id=employee_id, ended_at__isnull=True, duration_min__isnull=True)


def _done(employee_id: str) -> models.QuerySet:
    """Finished entries, the most recent first."""
    return (entries().filter(employee_id=employee_id, duration_min__isnull=False)
            .order_by(Coalesce('ended_at', 'created_at').desc(), '-id'))


def timer_state(user: AuthUser) -> dict:
    employee_id = own_employee(user)
    auto_stop_stale_timers(_now(), employee_id)
    return {'running': _first(entries().filter(pk__in=_running(employee_id).values('pk'))),
            'last': _first(_done(employee_id)),
            'todayMin': day_total(employee_id, npt_today(), None), 'serverTime': _now()}


def _start_timer(user: AuthUser, b: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    policy = enabled_policy(company_id)
    auto_stop_stale_timers(_now(), employee_id)
    with transaction.atomic():
        _lock(employee_id)
        if _running(employee_id).exists():
            raise ConflictError('A timer is already running. Stop it first.')
        now = _now()
        today = local_date_of(_ms(now))
        assert_week_open(employee_id, today)
        work = check_work(company_id, employee_id, policy, b.get('projectId'), b.get('activityId'))
        assert_no_overlap(employee_id, now, now + timedelta(seconds=1), None)
        t = TimeEntry.objects.create(
            company_id=company_id, employee_id=employee_id, local_date=today, started_at=now,
            project_id=b.get('projectId'), activity_id=b.get('activityId'), note=b.get('note') or None,
            billable=b['billable'] if b.get('billable') is not None else work['billable'],
            source=TimeEntry.Source.TIMER, created_by_id=user.id)
        return entry_by_id(t.id)


def start_timer(user: AuthUser, body: dict | None = None) -> dict:
    return _start_timer(user, plain(validate(TimerIn, body or {})))


def resume_timer(user: AuthUser) -> dict:
    """Start again on whatever was tracked last (Jibble's "resume")."""
    last = _clean(_done(own_employee(user)).values('project_id', 'activity_id', 'note', 'billable').first())
    if not last:
        raise BadRequestError('There is nothing to resume yet. Start a new timer.')
    return _start_timer(user, {'projectId': last['project_id'], 'activityId': last['activity_id'], 'note': last['note'],
                               'billable': last['billable']})


def stop_timer(user: AuthUser, body: dict | None = None) -> dict:
    employee_id = own_employee(user)
    b = validate(StopIn, body or {})
    auto_stop_stale_timers(_now(), employee_id)
    with transaction.atomic():
        t = _running(employee_id).select_for_update().values('id', 'started_at').first()
        if not t:
            raise ConflictError('No timer is running.')
        # The entry ends now (so the next timer can start straight away); anything under a minute counts as one minute.
        started_ms = _ms(t['started_at'])
        end_ms = max(_ms(_now()), started_ms + 1)
        changes: dict[str, Any] = {'ended_at': _dt(end_ms), 'updated_at': Now(),
                                   'duration_min': min(minutes_from_ms(end_ms - started_ms), DAY_LIMIT_MIN)}
        if 'note' in b:
            changes['note'] = b['note'] or None
        TimeEntry.objects.filter(pk=t['id']).update(**changes)
        return entry_by_id(t['id'])


# ---------------------------------------------------------------- entries

def list_entries(user: AuthUser, query: dict) -> list[dict]:
    q = plain(validate(EntriesQueryIn, query))
    if q['to'] < q['from']:
        raise BadRequestError('from must be before to')
    employee_id = readable_employee(user, q.get('employeeId'))
    qs = (entries().for_company(company_id_of(user)).filter(employee_id=employee_id, local_date__range=(q['from'], q['to']))
          .order_by('-local_date', F('started_at').desc(nulls_last=True), '-created_at'))
    return _entry_rows(qs[:2000])


def get_entry(user: AuthUser, entry_id: str) -> dict:
    """One of the signed-in person's own entries (for the edit form)."""
    employee_id = own_employee(user)
    e = _first(entries().for_company(company_id_of(user)).filter(pk=parse_uuid(entry_id), employee_id=employee_id))
    if not e:
        raise NotFoundError('entry not found')
    return e


def add_entry(user: AuthUser, body: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    b = plain(validate(EntryIn, body))
    policy = enabled_policy(company_id)
    span = resolve_span(b)
    with transaction.atomic():
        _lock(employee_id)
        assert_week_open(employee_id, b['date'])
        work = check_work(company_id, employee_id, policy, b.get('projectId'), b.get('activityId'))
        if span['startedAt'] and span['endedAt']:
            assert_no_overlap(employee_id, span['startedAt'], span['endedAt'], None)
        if day_total(employee_id, b['date'], None) + span['durationMin'] > DAY_LIMIT_MIN:
            raise BadRequestError('A day cannot have more than 24 hours of time.')
        t = TimeEntry.objects.create(
            company_id=company_id, employee_id=employee_id, local_date=b['date'], started_at=span['startedAt'],
            ended_at=span['endedAt'], duration_min=span['durationMin'], project_id=b.get('projectId'),
            activity_id=b.get('activityId'), note=b.get('note') or None,
            billable=b['billable'] if b.get('billable') is not None else work['billable'],
            source=TimeEntry.Source.MANUAL, created_by_id=user.id)
    return entry_by_id(t.id)


def _own_entry_for_update(user: AuthUser, entry_id: str, *fields: str) -> dict:
    """The person's own entry in the active company, locked; anyone else's is not found."""
    employee_id = own_employee(user)
    cur = _clean(TimeEntry.objects.for_company(company_id_of(user)).select_for_update()
                 .filter(pk=entry_id).values('employee_id', 'local_date', *fields).first())
    if not cur or cur['employee_id'] != employee_id:
        raise NotFoundError('entry not found')
    return cur


def update_entry(user: AuthUser, entry_id: str, body: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    eid = parse_uuid(entry_id)
    b = plain(validate(EntryIn, body, partial=True))
    policy = enabled_policy(company_id)
    with transaction.atomic():
        _lock(employee_id)
        cur = _own_entry_for_update(user, eid, 'started_at', 'ended_at', 'duration_min', 'project_id', 'activity_id',
                                    'note', 'billable')
        assert_week_open(employee_id, cur['local_date'])
        project_changed = 'projectId' in b and b['projectId'] != cur['project_id']
        project_id = b['projectId'] if 'projectId' in b else cur['project_id']
        activity_id = b['activityId'] if 'activityId' in b else (None if project_changed else cur['activity_id'])
        work = check_work(company_id, employee_id, policy, project_id, activity_id)
        time_changed = any(k in b for k in ('date', 'start', 'end', 'durationMin'))
        span = {'startedAt': cur['started_at'], 'endedAt': cur['ended_at'], 'durationMin': cur['duration_min']}
        day = cur['local_date']
        if time_changed:
            if cur['duration_min'] is None:
                raise BadRequestError('Stop the timer before changing its times.')
            day = b.get('date') or cur['local_date']
            assert_week_open(employee_id, day)
            span = resolve_span({'date': day, 'start': b.get('start'), 'end': b.get('end'), 'durationMin': b.get('durationMin')})
            if span['startedAt'] and span['endedAt']:
                assert_no_overlap(employee_id, span['startedAt'], span['endedAt'], eid)
            if day_total(employee_id, day, eid) + (span['durationMin'] or 0) > DAY_LIMIT_MIN:
                raise BadRequestError('A day cannot have more than 24 hours of time.')
        billable = b['billable'] if b.get('billable') is not None else (work['billable'] if project_changed else cur['billable'])
        changes: dict[str, Any] = {
            'local_date': day, 'started_at': span['startedAt'], 'ended_at': span['endedAt'],
            'duration_min': span['durationMin'], 'project_id': project_id, 'activity_id': activity_id,
            'note': (b.get('note') or None) if 'note' in b else cur['note'], 'billable': billable, 'updated_at': Now()}
        if time_changed:
            changes['auto_stopped'] = False
        TimeEntry.objects.filter(pk=eid).update(**changes)
    return entry_by_id(eid)


def delete_entry(user: AuthUser, entry_id: str) -> None:
    employee_id = own_employee(user)
    eid = parse_uuid(entry_id)
    with transaction.atomic():
        cur = _own_entry_for_update(user, eid)
        assert_week_open(employee_id, cur['local_date'])
        TimeEntry.objects.filter(pk=eid).delete()


# ---------------------------------------------------------------- weeks and timesheets

TIMESHEET_FIELDS = ('id', 'week_start', 'status', 'total_min', 'note', 'submitted_at', 'decided_at', 'decision_comment',
                    'workflow_instance_id', 'decided_by_name')


def _submissions(company_id: str) -> models.QuerySet:
    return Timesheet.objects.for_company(company_id).annotate(decided_by_name=F('decided_by__name'))


def week_view(company_id: str, employee_id: str, week_start: str) -> dict:
    """One employee's week: entries per day, rounded totals and the comparison with attended time."""
    policy = load_time_policy(company_id)
    week_end = add_days_iso(week_start, 6)
    week_entries = _entry_rows(
        entries().for_company(company_id).filter(employee_id=employee_id, local_date__range=(week_start, week_end))
        .order_by('local_date', F('started_at').asc(nulls_last=True), 'created_at'))
    att_by = {a['local_date']: a for a in _clean_all(
        latest_days().filter(employee_id=employee_id, local_date__range=(week_start, week_end))
        .values('local_date', 'worked_min', 'status'))}
    # The latest submission decides the week's state; earlier returned or withdrawn ones are history.
    ts = _clean_all(_submissions(company_id).filter(employee_id=employee_id, week_start=week_start)
                    .order_by('-submitted_at', '-id').values(*TIMESHEET_FIELDS))
    today = npt_today()
    days = []
    for i in range(7):
        d = add_days_iso(week_start, i)
        lst = [e for e in week_entries if e['local_date'] == d]
        done = [e for e in lst if e['duration_min'] is not None]
        tracked = sum(e['duration_min'] or 0 for e in done)
        rounded = sum(round_entry(e['duration_min'] or 0, policy) for e in done)
        billable = sum(round_entry(e['duration_min'] or 0, policy) for e in done if e['billable'])
        a = att_by.get(d)
        attendance = a['worked_min'] if a else None
        diff = None if attendance is None else tracked - attendance
        if tracked > 0 and not attendance and d < today:
            flag = 'no_attendance'
        elif diff is not None and tracked > 0 and abs(diff) > DISCREPANCY_MIN:
            flag = 'over' if diff > 0 else 'under'
        else:
            flag = None
        days.append({'date': d, 'entries': lst, 'trackedMin': tracked, 'roundedMin': rounded, 'billableMin': billable,
                     'attendanceMin': attendance, 'attendanceStatus': a['status'] if a else None, 'diffMin': diff, 'flag': flag})
    totals = {
        'trackedMin': sum(d['trackedMin'] for d in days),
        'roundedMin': sum(d['roundedMin'] for d in days),
        'billableMin': sum(d['billableMin'] for d in days),
        'attendanceMin': sum(d['attendanceMin'] or 0 for d in days),
        'flaggedDays': sum(1 for d in days if d['flag']),
    }
    return {'weekStart': week_start, 'weekEnd': week_end, 'days': days, 'totals': totals,
            'running': any(e['duration_min'] is None for e in week_entries),
            'timesheet': ts[0] if ts else None, 'history': ts[1:],
            'rounding': {'step': policy['roundingStep'], 'mode': policy['roundingMode']}}


def week(user: AuthUser, query: dict | None = None) -> dict:
    q = plain(validate(WeekQueryIn, query or {}))
    employee_id = readable_employee(user, q.get('employeeId'))
    if employee_id == user.employee_id:
        auto_stop_stale_timers(_now(), employee_id)
    return week_view(company_id_of(user), employee_id, week_start_of(q.get('weekStart') or q.get('date') or npt_today()))


def submit_timesheet(user: AuthUser, body: dict) -> str:
    """Submit a week for approval. Each submission is its own row and approval; returned weeks can be fixed and sent again."""
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    b = validate(SubmitIn, body)
    ws = b['weekStart']
    if not is_sunday(ws):
        raise BadRequestError('weekStart must be a Sunday')
    if ws > npt_today():
        raise BadRequestError('You cannot submit a week that has not started.')
    enabled_policy(company_id)
    with transaction.atomic():
        _lock(employee_id)
        t = _week_entries(employee_id, ws).aggregate(min=Coalesce(Sum('duration_min'), 0), n=Count('id'),
                                                     running=Count('id', filter=Q(duration_min__isnull=True)))
        if t['running']:
            raise ConflictError('Stop your running timer before submitting this week.')
        if not t['n']:
            raise BadRequestError('There is no time logged in this week.')
        assert_week_open(employee_id, ws)
        sheet = Timesheet.objects.create(company_id=company_id, employee_id=employee_id, week_start=ws, status=TS.SUBMITTED,
                                         total_min=t['min'], note=b.get('note') or None, submitted_by_id=user.id)
        ts_id = str(sheet.id)
        inst = start_workflow(company_id=company_id, object_type='timesheet', object_id=ts_id, employee_id=employee_id,
                              title='Timesheet to approve',
                              body=f"{user.name} submitted {to_fixed(t['min'] / 60, 1)} h for {bs_range(ws, add_days_iso(ws, 6))}.")
        Timesheet.objects.filter(pk=sheet.pk).update(workflow_instance_id=inst['id'])
        audit('submit', 'timesheet', actor_id=user.id, row_id=ts_id, after={'weekStart': ws, 'totalMin': t['min']})
    return ts_id


def withdraw_timesheet(user: AuthUser, timesheet_id: str) -> None:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    tid = parse_uuid(timesheet_id)
    with transaction.atomic():
        cur = Timesheet.objects.for_company(company_id).select_for_update().filter(pk=tid).values('employee_id', 'status').first()
        if not cur or str(cur['employee_id']) != employee_id:
            raise NotFoundError('timesheet not found')
        if cur['status'] != TS.SUBMITTED:
            raise ConflictError(f"Only a timesheet waiting for approval can be withdrawn (it is {cur['status']}).")
        Timesheet.objects.filter(pk=tid).update(status=TS.CANCELLED, decided_at=Now())
        cancel_workflow('timesheet', tid)
        audit('withdraw', 'timesheet', actor_id=user.id, row_id=tid)


TIMESHEET_LIST_FIELDS = ('id', 'employee_id', 'emp_code', 'name_en', 'week_start', 'status', 'total_min', 'note',
                         'submitted_at', 'decided_at', 'decision_comment', 'workflow_instance_id', 'workflow_state',
                         'decided_by_name', 'attendance_min', 'billable_min')


def timesheets_queryset(user: AuthUser, query: dict | None = None) -> models.QuerySet:
    """Timesheets for review: own, a manager's team or (HR) everyone, with tracked vs attended hours for each week."""
    q = plain(validate(TimesheetsQueryIn, query or {}))
    scope = scope_of(user, q.get('scope'))
    if q.get('employeeId'):
        readable_employee(user, q['employeeId'])
    in_week = {'employee_id': OuterRef('employee_id'), 'local_date__gte': OuterRef('week_start'),
               'local_date__lte': _plus_days(OuterRef('week_start'), 6)}
    qs = _submissions(company_id_of(user)).annotate(
        emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), workflow_state=F('workflow_instance__state'),
        attendance_min=_sum(latest_days().filter(**in_week), 'worked_min', 'employee_id'),
        billable_min=_sum(TimeEntry.objects.filter(billable=True, **in_week), 'duration_min', 'employee_id'))
    qs = apply_filters(TimesheetFilter, {'status': q.get('status'), 'week_from': q.get('from'), 'week_to': q.get('to'),
                                         'employee': q.get('employeeId')}, qs)
    return in_scope(qs, scope, user.employee_id).order_by('-week_start', '-submitted_at', 'employee__name_en')


def timesheet_values(qs: models.QuerySet) -> models.QuerySet:
    """A timesheets_queryset as dict rows, still lazy (so a page can be cut from it)."""
    return qs.values(*TIMESHEET_LIST_FIELDS)


def timesheet_rows(values) -> list[dict]:
    """Rows of timesheet_values in the TS shape, with the difference between logged and attended minutes."""
    out = _clean_all(values)
    for t in out:
        t['diff_min'] = t['total_min'] - t['attendance_min']
    return out


def list_timesheets(user: AuthUser, query: dict | None = None) -> list[dict]:
    return timesheet_rows(timesheet_values(timesheets_queryset(user, query))[:500])


def timesheet_detail(user: AuthUser, timesheet_id: str) -> dict:
    company_id = company_id_of(user)
    tid = parse_uuid(timesheet_id)
    row = _clean(Timesheet.objects.for_company(company_id).filter(pk=tid)
                 .values('employee_id', 'week_start', emp_code=F('employee__emp_code'), name_en=F('employee__name_en')).first())
    if not row:
        raise NotFoundError('timesheet not found')
    assert_manages(user, row['employee_id'], 'attendance')
    sub = _clean(_submissions(company_id).filter(pk=tid).values(*TIMESHEET_FIELDS).first())
    return {'employee': {'id': row['employee_id'], 'empCode': row['emp_code'], 'name': row['name_en']},
            **week_view(company_id, row['employee_id'], row['week_start']), 'submission': sub}


def team_week(user: AuthUser, query: dict | None = None) -> dict:
    """Who on the team has (not) submitted a week: the manager's at-a-glance view."""
    q = validate(TeamWeekQueryIn, query or {})
    scope = scope_of(user, q.get('scope') or ('all' if has_module(user, 'attendance') else 'team'))
    ws = week_start_of(q.get('weekStart') or npt_today())
    in_week = {'employee_id': OuterRef('pk'), 'local_date__range': (ws, add_days_iso(ws, 6))}
    latest = Timesheet.objects.filter(employee=OuterRef('pk'), week_start=ws).order_by('-submitted_at', '-id')
    qs = (Employee.objects.for_company(company_id_of(user)).filter(status=Employee.Status.ACTIVE)
          .annotate(tracked_min=_sum(TimeEntry.objects.filter(**in_week), 'duration_min', 'employee_id'),
                    attendance_min=_sum(latest_days().filter(**in_week), 'worked_min', 'employee_id'),
                    timesheet_id=Subquery(latest.values('id')[:1]), timesheet_status=Subquery(latest.values('status')[:1])))
    qs = in_scope(qs, scope, user.employee_id, field='id')
    if scope == 'team':
        qs = qs.exclude(pk=user.employee_id)
    people = [_clean({'employee_id': p['id'], 'emp_code': p['emp_code'], 'name_en': p['name_en'],
                      'tracked_min': p['tracked_min'], 'attendance_min': p['attendance_min'],
                      'timesheet_id': p['timesheet_id'], 'status': p['timesheet_status']})
              for p in qs.order_by('name_en').values('id', 'emp_code', 'name_en', 'tracked_min', 'attendance_min',
                                                     'timesheet_id', 'timesheet_status')]
    return {'weekStart': ws, 'weekEnd': add_days_iso(ws, 6), 'people': people}


# ---------------------------------------------------------------- reports

REPORT_FIELDS = ('id', 'employee_id', 'emp_code', 'name_en', 'local_date', 'duration_min', 'billable', 'note', 'project_id',
                 'project_code', 'project_name', 'bill_rate', 'budget_hours', 'client_id', 'client_name', 'activity_id',
                 'activity_name', 'started_at', 'ended_at')


def report_queryset(user: AuthUser, q: dict) -> models.QuerySet:
    """The finished entries a report covers (q is a validated ReportQueryIn)."""
    if q['to'] < q['from']:
        raise BadRequestError('from must be before to')
    if (_d(q['to']) - _d(q['from'])).days > 400:
        raise BadRequestError('Pick a range of at most 400 days.')
    scope = scope_of(user, q.get('scope'))
    if q.get('employeeId'):
        readable_employee(user, q['employeeId'])
    qs = (entries().for_company(company_id_of(user))
          .filter(duration_min__isnull=False, local_date__range=(q['from'], q['to']))
          .annotate(emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), bill_rate=F('project__bill_rate'),
                    budget_hours=F('project__budget_hours'), client_id=F('project__client_id')))
    qs = apply_filters(ReportEntryFilter, {'employee': q.get('employeeId'), 'project': q.get('projectId'),
                                           'client': q.get('clientId')}, qs)
    return in_scope(qs, scope, user.employee_id).order_by('local_date', 'employee__name_en',
                                                           F('started_at').asc(nulls_last=True))


def _report_entries(user: AuthUser, q: dict) -> list[dict]:
    return _clean_all(report_queryset(user, q).values(*REPORT_FIELDS))


def summary_columns(group_by: str) -> list[dict]:
    cols = [{'key': 'label', 'header': group_by[0].upper() + group_by[1:]}]
    if group_by in ('project', 'employee'):
        cols.append({'key': 'code', 'header': 'Code'})
    cols += [{'key': 'hours', 'header': 'Hours'}, {'key': 'billable_hours', 'header': 'Billable hours'},
             {'key': 'non_billable_hours', 'header': 'Non-billable hours'},
             {'key': 'billable_amount', 'header': 'Billable amount (NPR)'}]
    if group_by == 'project':
        cols += [{'key': 'budget_hours', 'header': 'Budget hours'}, {'key': 'budget_used_pct', 'header': 'Budget used %'}]
    cols.append({'key': 'entries', 'header': 'Entries'})
    return cols


def report_summary(user: AuthUser, query: dict) -> dict:
    """GET /reports/summary: {columns, rows, totals, rounding, filename}."""
    q = plain(validate(ReportQueryIn, query))
    company_id = company_id_of(user)
    policy = load_time_policy(company_id)
    gb = q['groupBy']
    groups: dict[str, dict] = {}
    # Each entry is rounded on its own (the company's rounding), so the roll-up runs over the entries.
    for e in _report_entries(user, q):
        if gb == 'project':
            key, label, code = e['project_id'] or 'none', e['project_name'] or 'No project', e['project_code']
            budget = float(e['budget_hours']) if e['budget_hours'] else None
        elif gb == 'client':
            key, label, code, budget = e['client_id'] or 'none', e['client_name'] or 'No client', None, None
        elif gb == 'employee':
            key, label, code, budget = e['employee_id'], e['name_en'], e['emp_code'], None
        elif gb == 'activity':
            key, label, code, budget = e['activity_id'] or 'none', e['activity_name'] or 'No activity', None, None
        else:
            key, label, code, budget = e['local_date'], e['local_date'], None, None
        g = groups.get(key) or {'key': key, 'label': label, 'code': code, 'trackedMin': 0, 'roundedMin': 0, 'billableMin': 0,
                                'amount': 0.0, 'budgetHours': budget, 'entries': 0}
        rounded = round_entry(e['duration_min'], policy)
        g['trackedMin'] += e['duration_min']
        g['roundedMin'] += rounded
        g['entries'] += 1
        if e['billable']:
            g['billableMin'] += rounded
            g['amount'] += (rounded / 60) * float(e['bill_rate']) if e['bill_rate'] else 0
        groups[key] = g
    # Budget burn is all-time hours on the project against its budget, not just this range.
    ids = [k for k in groups if k != 'none'] if gb == 'project' else []
    burn = {str(x['project_id']): x['min'] for x in
            TimeEntry.objects.for_company(company_id).filter(project_id__in=ids, duration_min__isnull=False)
            .values('project_id').annotate(min=Sum('duration_min'))} if ids else {}
    vals = list(groups.values())
    if gb == 'day':
        vals.sort(key=lambda g: g['key'])
    else:
        vals.sort(key=lambda g: -g['roundedMin'])
    out = []
    for g in vals:
        bh = g['budgetHours']
        out.append({
            'key': g['key'], 'label': g['label'], 'code': g['code'], 'entries': g['entries'],
            'tracked_hours': fixed_num(g['trackedMin'] / 60), 'hours': fixed_num(g['roundedMin'] / 60),
            'billable_hours': fixed_num(g['billableMin'] / 60),
            'non_billable_hours': fixed_num((g['roundedMin'] - g['billableMin']) / 60),
            'billable_amount': whole(round_half_up(g['amount'] * 100) / 100),
            'budget_hours': whole(bh) if bh is not None else None,
            'budget_used_pct': whole(round_half_up(((burn.get(g['key'], 0)) / 60 / bh) * 1000) / 10) if bh else None,
        })
    hours = sum(x['hours'] for x in out)
    billable = sum(x['billable_hours'] for x in out)
    amount = sum(x['billable_amount'] for x in out)
    return {'columns': summary_columns(gb), 'rows': out,
            'totals': {'hours': fixed_num(hours), 'billableHours': fixed_num(billable), 'billableAmount': whole(round_half_up(amount * 100) / 100)},
            'rounding': {'step': policy['roundingStep'], 'mode': policy['roundingMode']},
            'filename': f"time-by-{gb}-{q['from']}-to-{q['to']}", 'format': q.get('format')}


ENTRY_REPORT_COLUMNS = [
    {'key': 'date', 'header': 'Date (AD)'}, {'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name', 'header': 'Employee'},
    {'key': 'client', 'header': 'Client'}, {'key': 'project', 'header': 'Project'}, {'key': 'activity', 'header': 'Activity'},
    {'key': 'start', 'header': 'Start'}, {'key': 'end', 'header': 'End'}, {'key': 'minutes', 'header': 'Minutes tracked'},
    {'key': 'hours', 'header': 'Hours (rounded)'}, {'key': 'billable', 'header': 'Billable'}, {'key': 'note', 'header': 'Note'},
]


def _npt_hhmm(d: datetime | None) -> str:
    return d.astimezone(NPT).strftime('%H:%M') if d else ''


def report_entries(user: AuthUser, query: dict) -> dict:
    """GET /reports/entries: {columns, rows, filename}."""
    q = plain(validate(ReportQueryIn, query))
    policy = load_time_policy(company_id_of(user))
    out = [{
        'date': e['local_date'], 'emp_code': e['emp_code'], 'name': e['name_en'], 'client': e['client_name'] or '',
        'project': e['project_name'] or '', 'activity': e['activity_name'] or '', 'start': _npt_hhmm(e['started_at']),
        'end': _npt_hhmm(e['ended_at']), 'minutes': e['duration_min'], 'hours': fixed_num(round_entry(e['duration_min'], policy) / 60),
        'billable': 'Yes' if e['billable'] else 'No', 'note': e['note'] or '',
    } for e in _report_entries(user, q)]
    return {'columns': ENTRY_REPORT_COLUMNS, 'rows': out, 'filename': f"time-entries-{q['from']}-to-{q['to']}", 'format': q.get('format')}
