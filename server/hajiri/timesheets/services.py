"""
Project time tracking and weekly timesheets (port of apps/api/src/modules/timesheets/service.ts and routes.ts).

Employees run a timer on the server clock or log time by hand against projects and activities; a week (Sunday to
Saturday) is submitted for approval through the workflow engine and is locked while it waits and once approved.
Managers see their team, HR (the attendance area) everyone; reports roll the time up by project, client, person,
activity or day with the company's rounding, billable value and budget burn.

Every function takes the signed-in AuthUser and the request body or query in the TS (camelCase) shape, validates it
with the same rules as the zod schemas and raises the TS errors with the TS messages. Rows come back in the TS JSON
shape (snake_case columns, ids as str).
"""
from __future__ import annotations

import json
import math
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from django.db import transaction

from engines.attendance import MIN_MS, local_date_of, local_time_ms, parse_hhmm, round_minutes
from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, company_id_of, has_module, has_role
from hajiri.core.bsdate import bs_label, bs_range
from hajiri.core.clock import now as _now
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (Any_, Arr, Bool, Enum, Hhmm, Int, IsoDate, Num, Obj, Str, Uuid, parse,
                                parse_uuid, safe_parse)
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.workflow.service import (DecisionContext, assert_manages, cancel_workflow, npt_today, register_handler,
                                     register_summariser, start_workflow)

# Days where tracked and attended time differ by more than this are flagged for the reviewer.
DISCREPANCY_MIN = 60
DAY_LIMIT_MIN = 1440
TIME_POLICY_KEY = 'time.policy'
ROUNDING_STEPS = (0, 5, 6, 10, 15)
STATUSES = ('submitted', 'approved', 'rejected', 'cancelled')
GROUP_BY = ('project', 'client', 'employee', 'activity', 'day')


# ---------------------------------------------------------------- small helpers

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(d: datetime) -> int:
    """Epoch milliseconds (Date.getTime())."""
    return (d - _EPOCH) // timedelta(milliseconds=1)


def _dt(ms: float) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _clean(r: dict | None) -> dict | None:
    """ids as str (the TS JSON shape)."""
    if r is None:
        return None
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in r.items()}


def _clean_all(rs: list[dict]) -> list[dict]:
    return [_clean(r) for r in rs]


def js_round(x: float) -> int:
    """Math.round: halves toward +infinity."""
    return math.floor(x + 0.5)


def to_fixed(x: float, digits: int) -> str:
    """Number.prototype.toFixed: the exact binary value, an exact tie goes to the larger magnitude."""
    q = Decimal(x).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)
    return f'{q:.{digits}f}'


def js_num(x: float | int) -> float | int:
    """A float as JSON.stringify would print it: 4.0 -> 4."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    return x


def fixed_num(x: float, digits: int = 2) -> float | int:
    """+(x).toFixed(digits)."""
    return js_num(float(to_fixed(x, digits)))


def _require_module(user: AuthUser, module: str) -> None:
    roles = MODULE_ROLES[module]
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _atomic():
    return transaction.atomic()


def _lock(employee_id: str) -> None:
    one('SELECT pg_advisory_xact_lock(hashtext(%s)) AS ok', [f'time_entry:{employee_id}'])


# ---------------------------------------------------------------- policy

def _rounding_step_ok(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v in ROUNDING_STEPS


TIME_POLICY_SCHEMA = Obj({
    'enabled': Bool(default=True),
    'requireProject': Bool(default=False),
    # Jibble-style rounding applied to each entry in approvals and reports; the raw minutes are always kept.
    'roundingStep': Any_(default=0, refine=(_rounding_step_ok, 'Invalid input'), transform=lambda v: int(v)),
    'roundingMode': Enum(('nearest', 'up', 'down'), default='nearest'),
    'fridayReminder': Bool(default=True),
    # A timer left running longer than this is stopped at this length and the employee is told.
    'autoStopHours': Int(min=1, max=24, default=12),
})


def default_policy() -> dict:
    return parse(TIME_POLICY_SCHEMA, {})


def load_time_policy(company_id: str) -> dict:
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [company_id, TIME_POLICY_KEY])
    value = r['value'] if r and r['value'] is not None else {}
    ok, parsed = safe_parse(TIME_POLICY_SCHEMA, value)
    return parsed if ok else default_policy()


def round_entry(minutes: int, policy: dict) -> int:
    return int(round_minutes(minutes, policy.get('roundingStep', 0), policy.get('roundingMode', 'nearest')))


# ---------------------------------------------------------------- weeks (Sunday to Saturday)

def _d(iso: str | date) -> date:
    return iso if isinstance(iso, date) else date.fromisoformat(str(iso)[:10])


def add_days_iso(iso: str | date, days: int) -> str:
    return (_d(iso) + timedelta(days=days)).isoformat()


def _js_day(iso: str | date) -> int:
    """getUTCDay(): Sunday 0 .. Saturday 6."""
    return (_d(iso).weekday() + 1) % 7


def week_start_of(iso: str | date) -> str:
    return add_days_iso(iso, -_js_day(iso))


def is_sunday(iso: str | date) -> bool:
    return _js_day(iso) == 0


def assert_week_open(employee_id: str, local_date: str) -> None:
    """Submitted and approved weeks are locked: their entries can no longer be added, changed or removed."""
    r = one("""SELECT status FROM timesheet WHERE employee_id = %s AND week_start = %s AND status IN ('submitted', 'approved')""",
            [employee_id, week_start_of(local_date)])
    if r:
        raise ConflictError("This week's timesheet is approved and locked." if r['status'] == 'approved'
                            else "This week's timesheet is waiting for approval. Withdraw it to make changes.")


def minutes_from_ms(ms: float) -> int:
    return max(1, js_round(ms / MIN_MS))


# ---------------------------------------------------------------- auto-stop

def auto_stop_stale_timers(now: datetime | None = None, employee_id: str | None = None) -> int:
    """
    Stop timers that have run longer than the company's limit, ending them exactly at the limit (so a forgotten timer
    never logs a whole night), and tell the employee. Optionally limited to one employee for the lazy check on read.
    """
    at = now or _now()
    with _atomic():
        stopped = rows(
            """WITH lim AS (
                 SELECT c.id AS company_id, coalesce((s.value->>'autoStopHours')::int, 12) AS hours
                   FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'time.policy')
               UPDATE time_entry t SET ended_at = t.started_at + make_interval(hours => lim.hours), duration_min = lim.hours * 60,
                      auto_stopped = true, updated_at = now()
                 FROM lim
                WHERE lim.company_id = t.company_id AND t.ended_at IS NULL AND t.duration_min IS NULL
                  AND t.started_at <= %s::timestamptz - make_interval(hours => lim.hours)
                  AND (%s::uuid IS NULL OR t.employee_id = %s::uuid)
                RETURNING t.id, t.employee_id, t.company_id, to_char(t.local_date, 'YYYY-MM-DD') AS local_date, lim.hours""",
            [at, employee_id, employee_id])
        for t in stopped:
            notify(company_id=str(t['company_id']), user_id=user_id_for_employee(str(t['employee_id'])),
                   employee_id=str(t['employee_id']), template='time.timer_auto_stopped', title='Timer stopped automatically',
                   body=(f"Your timer from {bs_label(t['local_date'])} ran for more than {t['hours']} hours and was stopped. "
                         'Check the entry and correct it if needed.'),
                   payload={'entryId': str(t['id'])})
    return len(stopped)


# ---------------------------------------------------------------- Friday reminder

def friday_timesheet_reminders(now: datetime | None = None) -> int:
    """On Fridays (Nepal time), remind people who track time and have not submitted this week's timesheet. Once per week."""
    at = now or _now()
    today = local_date_of(_ms(at))
    if _js_day(today) != 5:
        return 0
    week = week_start_of(today)
    with _atomic():
        people = rows(
            """SELECT DISTINCT t.employee_id, t.company_id, u.id AS user_id
                 FROM time_entry t
                 JOIN employee e ON e.id = t.employee_id AND e.status = 'active'
                 JOIN app_user u ON u.employee_id = t.employee_id AND u.status = 'active'
                 LEFT JOIN company_setting s ON s.company_id = t.company_id AND s.key = 'time.policy'
                WHERE t.local_date >= %s::date - 28
                  AND coalesce((s.value->>'enabled')::boolean, true) AND coalesce((s.value->>'fridayReminder')::boolean, true)
                  AND NOT EXISTS (SELECT 1 FROM timesheet ts WHERE ts.employee_id = t.employee_id AND ts.week_start = %s::date AND ts.status IN ('submitted', 'approved'))
                  AND NOT EXISTS (SELECT 1 FROM notification_outbox n WHERE n.user_id = u.id AND n.template = 'timesheet.reminder' AND n.payload->>'weekStart' = %s)""",
            [week, week, week])
        for x in people:
            notify(company_id=str(x['company_id']), user_id=str(x['user_id']), employee_id=str(x['employee_id']),
                   template='timesheet.reminder', title='Submit your timesheet',
                   body=(f'Your timesheet for {bs_range(week, add_days_iso(week, 6))} has not been submitted yet. '
                         'Check your hours and submit it before the week ends.'),
                   payload={'weekStart': week}, channels=('push',))
    return len(people)


def daily_time_jobs(now: datetime | None = None) -> dict:
    return {'autoStopped': auto_stop_stale_timers(now), 'reminded': friday_timesheet_reminders(now)}


# ---------------------------------------------------------------- approval

def apply_timesheet_decision(object_id: str, state: str, ctx: DecisionContext) -> None:
    t = one("""SELECT employee_id, company_id, status, to_char(week_start, 'YYYY-MM-DD') AS week_start
                 FROM timesheet WHERE id = %s FOR UPDATE""", [object_id])
    if not t:
        raise NotFoundError('timesheet not found')
    if t['status'] != 'submitted':
        raise ConflictError(f"timesheet already {t['status']}")
    execute('UPDATE timesheet SET status = %s, decided_at = now(), decided_by = %s, decision_comment = %s WHERE id = %s',
            [state, ctx.actor_id, ctx.reason, object_id])
    emp = str(t['employee_id'])
    ws = t['week_start']
    verb = 'approved' if state == 'approved' else 'returned for changes'
    notify(company_id=str(t['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template=f'timesheet.{state}',
           title='Timesheet approved' if state == 'approved' else 'Timesheet returned',
           body=f"Your timesheet for {bs_range(ws, add_days_iso(ws, 6))} was {verb}{f': {ctx.reason}' if ctx.reason else '.'}",
           payload={'timesheetId': str(object_id), 'weekStart': ws}, channels=('push',))


def summarise_timesheet(object_id: str) -> dict:
    """Inbox card details for a timesheet approval."""
    t = one("""SELECT to_char(week_start, 'YYYY-MM-DD') AS week_start, total_min, note, employee_id FROM timesheet WHERE id = %s""",
            [object_id])
    if not t:
        return {}
    by_project = rows(
        """SELECT coalesce(p.name, 'No project') AS name, sum(e.duration_min)::int AS min
             FROM time_entry e LEFT JOIN time_project p ON p.id = e.project_id
            WHERE e.employee_id = %s AND e.local_date BETWEEN %s::date AND %s::date + 6 AND e.duration_min IS NOT NULL
            GROUP BY 1 ORDER BY 2 DESC LIMIT 5""", [t['employee_id'], t['week_start'], t['week_start']])
    worked = one("""SELECT coalesce(sum(worked_min), 0)::int AS min FROM v_attendance_latest
                     WHERE employee_id = %s AND local_date BETWEEN %s::date AND %s::date + 6""",
                 [t['employee_id'], t['week_start'], t['week_start']])
    return {'weekStart': t['week_start'], 'weekEnd': add_days_iso(t['week_start'], 6), 'totalMin': t['total_min'],
            'attendanceMin': worked['min'] if worked else 0, 'note': t['note'], 'projects': by_project}


register_handler('timesheet', apply_timesheet_decision)
register_summariser('timesheet', lambda inst: summarise_timesheet(inst['object_id']))


# ---------------------------------------------------------------- access

def own_employee(user: AuthUser) -> str:
    """The signed-in person's own employee record; time is always logged by the person who did the work."""
    if not user.employee_id:
        raise ForbiddenError('Only employees can track time. Link this account to an employee first.')
    return str(user.employee_id)


def readable_employee(user: AuthUser, employee_id: str | None) -> str:
    """Whose time a request may read: own by default; a manager their team; HR anyone in the company."""
    if not employee_id or employee_id == user.employee_id:
        return own_employee(user)
    if not one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id_of(user)]):
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


SCOPE_SQL = """(CASE %s::text WHEN 'own' THEN emp.id = %s::uuid
  WHEN 'team' THEN (emp.id = %s::uuid OR hj_manages(%s::uuid, emp.id, (now() AT TIME ZONE 'Asia/Kathmandu')::date))
  ELSE true END)"""


def _scope_params(scope: str, viewer: str | None) -> list:
    return [scope, viewer, viewer, viewer]


# ---------------------------------------------------------------- settings

def get_settings(user: AuthUser) -> dict:
    return {'policy': load_time_policy(company_id_of(user))}


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    value = parse(TIME_POLICY_SCHEMA, body)
    with _atomic():
        before = load_time_policy(company_id)
        execute("""INSERT INTO company_setting (company_id, key, value, updated_by) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by""",
                [company_id, TIME_POLICY_KEY, json.dumps(value), user.id])
        audit('update', 'company_setting', actor_id=user.id, row_id=TIME_POLICY_KEY, before=before, after=value)
    return {'policy': value}


# ---------------------------------------------------------------- clients

NAME = lambda **kw: Str(trim=True, min=1, max=120, **kw)  # noqa: E731
NOTES = lambda: Str(trim=True, max=500, nullish=True)  # noqa: E731


def list_clients(user: AuthUser, all_: bool = False) -> list[dict]:
    return _clean_all(rows(
        """SELECT c.id, c.name, c.notes, c.archived,
                  (SELECT count(*)::int FROM time_project p WHERE p.client_id = c.id AND p.status = 'active') AS active_projects
             FROM time_client c WHERE c.company_id = %s AND (%s::boolean OR NOT c.archived) ORDER BY c.archived, lower(c.name)""",
        [company_id_of(user), bool(all_) and has_module(user, 'attendance')]))


def create_client(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    b = parse(Obj({'name': NAME(), 'notes': NOTES()}), body)
    with _atomic():
        c = one('INSERT INTO time_client (company_id, name, notes) VALUES (%s, %s, %s) RETURNING id, name, notes, archived',
                [company_id_of(user), b['name'], b.get('notes') or None])
        audit('create', 'time_client', actor_id=user.id, row_id=str(c['id']), after=b)
    return _clean(c)


def update_client(user: AuthUser, client_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    cid = parse_uuid(client_id)
    b = parse(Obj({'name': NAME(optional=True), 'notes': NOTES(), 'archived': Bool(optional=True)}), body)
    with _atomic():
        c = one("""UPDATE time_client SET name = coalesce(%s, name), notes = CASE WHEN %s::boolean THEN %s ELSE notes END,
                          archived = coalesce(%s, archived)
                    WHERE id = %s AND company_id = %s RETURNING id, name, notes, archived""",
                [b.get('name'), 'notes' in b, b.get('notes') or None, b.get('archived'), cid, company_id_of(user)])
        if not c:
            raise NotFoundError('client not found')
        audit('update', 'time_client', actor_id=user.id, row_id=cid, after=b)
    return _clean(c)


# ---------------------------------------------------------------- projects

PROJECT_FIELDS = {
    'code': Str(trim=True, regex=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$', regex_message='letters, digits, - or _ (max 20)'),
    'name': NAME(),
    'clientId': Uuid(nullish=True),
    'color': Str(regex=r'^#[0-9a-fA-F]{6}$', optional=True),
    'billable': Bool(optional=True),
    'billRate': Num(min=0, max=1_000_000, nullish=True),
    'budgetHours': Num(min=0, max=1_000_000, nullish=True, refine=(lambda v: v > 0, 'Too small: expected number to be >0')),
    'allMembers': Bool(optional=True),
    'memberIds': Arr(Uuid(), max=1000, optional=True),
    'status': Enum(('active', 'archived'), optional=True),
}


def _partial(fields: dict) -> dict:
    """zod .partial(): every field may be left out."""
    import copy
    out = {}
    for k, s in fields.items():
        s2 = copy.copy(s)
        s2.optional = True
        out[k] = s2
    return out


PROJECT_BODY = Obj(PROJECT_FIELDS)
PROJECT_PATCH = Obj(_partial(PROJECT_FIELDS))


def list_projects(user: AuthUser, all_: bool = False) -> list[dict]:
    """Employees see the active projects they may log to; HR with all=1 sees every project with its members."""
    hr = has_module(user, 'attendance')
    everything = hr and bool(all_)
    members = ("""(SELECT coalesce(json_agg(json_build_object('id', e.id, 'name', e.name_en, 'empCode', e.emp_code) ORDER BY e.name_en), '[]')
                   FROM time_project_member m JOIN employee e ON e.id = m.employee_id WHERE m.project_id = p.id)"""
               if hr else "'[]'::json")
    return _clean_all(rows(
        f"""SELECT p.id, p.code, p.name, p.color, p.billable, p.bill_rate, p.budget_hours, p.all_members, p.status, p.client_id,
                   c.name AS client_name,
                   (SELECT coalesce(sum(t.duration_min), 0)::int FROM time_entry t WHERE t.project_id = p.id) AS tracked_min,
                   {members} AS members
              FROM time_project p LEFT JOIN time_client c ON c.id = p.client_id
             WHERE p.company_id = %s AND (%s::boolean OR p.status = 'active')
               AND (%s::boolean OR p.all_members OR EXISTS (SELECT 1 FROM time_project_member m WHERE m.project_id = p.id AND m.employee_id = %s::uuid))
             ORDER BY p.status, lower(p.name)""", [company_id_of(user), everything, everything, user.employee_id]))


def get_project(user: AuthUser, project_id: str) -> dict:
    """One project as list_projects(all=1) shows it, for the edit form (HR)."""
    _require_module(user, 'attendance')
    pid = parse_uuid(project_id)
    for p in list_projects(user, True):
        if p['id'] == pid:
            return p
    raise NotFoundError('project not found')


def _set_members(company_id: str, project_id: str, ids: list[str]) -> None:
    unique = list(dict.fromkeys(ids))
    if unique:
        ok = one('SELECT count(*)::int AS n FROM employee WHERE company_id = %s AND id = ANY(%s::uuid[])', [company_id, unique])
        if ok['n'] != len(unique):
            raise BadRequestError('Some members are not employees of this company.')
    execute('DELETE FROM time_project_member WHERE project_id = %s', [project_id])
    if unique:
        execute('INSERT INTO time_project_member (project_id, employee_id) SELECT %s, unnest(%s::uuid[])', [project_id, unique])


def _check_client(company_id: str, client_id: str | None) -> None:
    if not client_id:
        return
    if not one('SELECT 1 AS x FROM time_client WHERE id = %s AND company_id = %s', [client_id, company_id]):
        raise BadRequestError('Client not found.')


def create_project(user: AuthUser, body: dict) -> str:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(PROJECT_BODY, body)
    with _atomic():
        _check_client(company_id, b.get('clientId'))
        r = one("""INSERT INTO time_project (company_id, client_id, code, name, color, billable, bill_rate, budget_hours, all_members, status)
                   VALUES (%s, %s, upper(%s), %s, coalesce(%s, '#12773a'), coalesce(%s, true), %s, %s, coalesce(%s, true), coalesce(%s, 'active'))
                   RETURNING id""",
                [company_id, b.get('clientId'), b['code'], b['name'], b.get('color'), b.get('billable'), b.get('billRate'),
                 b.get('budgetHours'), b.get('allMembers'), b.get('status')])
        pid = str(r['id'])
        if 'memberIds' in b:
            _set_members(company_id, pid, b['memberIds'])
        audit('create', 'time_project', actor_id=user.id, row_id=pid, after=b)
    return pid


def update_project(user: AuthUser, project_id: str, body: dict) -> None:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    pid = parse_uuid(project_id)
    b = parse(PROJECT_PATCH, body)
    with _atomic():
        cur = one('SELECT * FROM time_project WHERE id = %s AND company_id = %s FOR UPDATE', [pid, company_id])
        if not cur:
            raise NotFoundError('project not found')
        _check_client(company_id, b.get('clientId'))
        execute("""UPDATE time_project SET code = coalesce(upper(%s), code), name = coalesce(%s, name),
                          client_id = CASE WHEN %s::boolean THEN %s::uuid ELSE client_id END, color = coalesce(%s, color),
                          billable = coalesce(%s, billable),
                          bill_rate = CASE WHEN %s::boolean THEN %s::numeric ELSE bill_rate END,
                          budget_hours = CASE WHEN %s::boolean THEN %s::numeric ELSE budget_hours END,
                          all_members = coalesce(%s, all_members), status = coalesce(%s, status), updated_at = now()
                    WHERE id = %s""",
                [b.get('code'), b.get('name'), 'clientId' in b, b.get('clientId'), b.get('color'), b.get('billable'),
                 'billRate' in b, b.get('billRate'), 'budgetHours' in b, b.get('budgetHours'), b.get('allMembers'),
                 b.get('status'), pid])
        if 'memberIds' in b:
            _set_members(company_id, pid, b['memberIds'])
        audit('update', 'time_project', actor_id=user.id, row_id=pid, before=_clean(cur), after=b)


# ---------------------------------------------------------------- activities

def list_activities(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'projectId': Uuid(optional=True), 'all': Enum(('0', '1'), optional=True)}), query or {})
    everything = q.get('all') == '1' and has_module(user, 'attendance')
    return _clean_all(rows(
        """SELECT a.id, a.name, a.project_id, p.name AS project_name, a.archived FROM time_activity a
             LEFT JOIN time_project p ON p.id = a.project_id
            WHERE a.company_id = %s AND (%s::boolean OR NOT a.archived) AND (%s::uuid IS NULL OR a.project_id IS NULL OR a.project_id = %s::uuid)
            ORDER BY a.project_id NULLS FIRST, lower(a.name)""",
        [company_id_of(user), everything, q.get('projectId'), q.get('projectId')]))


def create_activity(user: AuthUser, body: dict) -> dict:
    _require_module(user, 'attendance')
    company_id = company_id_of(user)
    b = parse(Obj({'name': Str(trim=True, min=1, max=80), 'projectId': Uuid(nullish=True)}), body)
    if b.get('projectId') and not one('SELECT 1 AS x FROM time_project WHERE id = %s AND company_id = %s', [b['projectId'], company_id]):
        raise BadRequestError('Project not found.')
    with _atomic():
        a = one('INSERT INTO time_activity (company_id, project_id, name) VALUES (%s, %s, %s) RETURNING id, name, project_id, archived',
                [company_id, b.get('projectId'), b['name']])
        audit('create', 'time_activity', actor_id=user.id, row_id=str(a['id']), after=b)
    return _clean(a)


def update_activity(user: AuthUser, activity_id: str, body: dict) -> dict:
    _require_module(user, 'attendance')
    aid = parse_uuid(activity_id)
    b = parse(Obj({'name': Str(trim=True, min=1, max=80, optional=True), 'archived': Bool(optional=True)}), body)
    with _atomic():
        a = one("""UPDATE time_activity SET name = coalesce(%s, name), archived = coalesce(%s, archived)
                    WHERE id = %s AND company_id = %s RETURNING id, name, project_id, archived""",
                [b.get('name'), b.get('archived'), aid, company_id_of(user)])
        if not a:
            raise NotFoundError('activity not found')
    return _clean(a)


# ---------------------------------------------------------------- entries: shared checks

def check_work(company_id: str, employee_id: str, policy: dict, project_id: str | None, activity_id: str | None) -> dict:
    """Project and activity checks shared by the timer and manual entries. Returns the project's default billable flag."""
    if not project_id:
        if policy['requireProject']:
            raise BadRequestError('Pick a project for this time.')
        if activity_id and not one("""SELECT 1 AS x FROM time_activity WHERE id = %s AND company_id = %s AND project_id IS NULL
                                       AND NOT archived""", [activity_id, company_id]):
            raise BadRequestError('That activity is not available without a project.')
        return {'billable': False}
    proj = one('SELECT id, status, all_members, billable, name FROM time_project WHERE id = %s AND company_id = %s',
               [project_id, company_id])
    if not proj:
        raise BadRequestError('Project not found.')
    if proj['status'] != 'active':
        raise BadRequestError(f"{proj['name']} is archived.")
    if not proj['all_members'] and not one('SELECT 1 AS x FROM time_project_member WHERE project_id = %s AND employee_id = %s',
                                           [project_id, employee_id]):
        raise ForbiddenError(f"You are not a member of {proj['name']}.")
    if activity_id and not one("""SELECT 1 AS x FROM time_activity WHERE id = %s AND company_id = %s
                                   AND (project_id IS NULL OR project_id = %s) AND NOT archived""",
                               [activity_id, company_id, project_id]):
        raise BadRequestError('That activity does not belong to this project.')
    return {'billable': proj['billable']}


def day_total(employee_id: str, local_date: str, exclude_id: str | None) -> int:
    r = one("""SELECT coalesce(sum(duration_min), 0)::int AS min FROM time_entry
                WHERE employee_id = %s AND local_date = %s AND (%s::uuid IS NULL OR id <> %s::uuid)""",
            [employee_id, local_date, exclude_id, exclude_id])
    return r['min'] if r else 0


def assert_no_overlap(employee_id: str, start: datetime, end: datetime, exclude_id: str | None) -> None:
    if one("""SELECT 1 AS x FROM time_entry WHERE employee_id = %s AND started_at IS NOT NULL AND (%s::uuid IS NULL OR id <> %s::uuid)
                AND tstzrange(started_at, coalesce(ended_at, 'infinity'::timestamptz), '[)') && tstzrange(%s, %s, '[)')""",
           [employee_id, exclude_id, exclude_id, start, end]):
        raise ConflictError('This time overlaps another entry or your running timer.')


ENTRY_COLS = """e.id, e.employee_id, to_char(e.local_date, 'YYYY-MM-DD') AS local_date, e.started_at, e.ended_at, e.duration_min, e.project_id,
  p.name AS project_name, p.code AS project_code, p.color AS project_color, c.name AS client_name, e.activity_id, a.name AS activity_name,
  e.note, e.billable, e.source, e.auto_stopped, e.created_at, e.updated_at"""
ENTRY_FROM = """time_entry e LEFT JOIN time_project p ON p.id = e.project_id LEFT JOIN time_client c ON c.id = p.client_id
  LEFT JOIN time_activity a ON a.id = e.activity_id"""


def entry_by_id(entry_id: str) -> dict | None:
    return _clean(one(f'SELECT {ENTRY_COLS} FROM {ENTRY_FROM} WHERE e.id = %s', [entry_id]))


MANUAL_FIELDS = {
    'date': IsoDate(),
    'start': Hhmm(optional=True),
    'end': Hhmm(optional=True),
    'durationMin': Int(min=1, max=DAY_LIMIT_MIN, optional=True),
    'projectId': Uuid(nullish=True),
    'activityId': Uuid(nullish=True),
    'note': Str(trim=True, max=500, nullish=True),
    'billable': Bool(optional=True),
}
MANUAL_ENTRY = Obj(MANUAL_FIELDS)
MANUAL_PATCH = Obj(_partial(MANUAL_FIELDS))


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
        return {'startedAt': _dt(s), 'endedAt': _dt(e), 'durationMin': js_round((e - s) / 60_000)}
    if not b.get('durationMin'):
        raise BadRequestError('Give a start and end time or a duration.')
    return {'startedAt': None, 'endedAt': None, 'durationMin': b['durationMin']}


# ---------------------------------------------------------------- timer (server clock only)

def _running_for(employee_id: str) -> dict | None:
    return _clean(one(f'SELECT {ENTRY_COLS} FROM {ENTRY_FROM} WHERE e.employee_id = %s AND e.ended_at IS NULL AND e.duration_min IS NULL',
                      [employee_id]))


def _last_entry(employee_id: str) -> dict | None:
    return _clean(one(f"""SELECT {ENTRY_COLS} FROM {ENTRY_FROM} WHERE e.employee_id = %s AND e.duration_min IS NOT NULL
                          ORDER BY coalesce(e.ended_at, e.created_at) DESC LIMIT 1""", [employee_id]))


def timer_state(user: AuthUser) -> dict:
    employee_id = own_employee(user)
    auto_stop_stale_timers(_now(), employee_id)
    return {'running': _running_for(employee_id), 'last': _last_entry(employee_id),
            'todayMin': day_total(employee_id, npt_today(), None), 'serverTime': _now()}


TIMER_BODY = Obj({'projectId': Uuid(nullish=True), 'activityId': Uuid(nullish=True),
                  'note': Str(trim=True, max=500, nullish=True), 'billable': Bool(optional=True)})


def _start_timer(user: AuthUser, b: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    policy = enabled_policy(company_id)
    auto_stop_stale_timers(_now(), employee_id)
    with _atomic():
        _lock(employee_id)
        if _running_for(employee_id):
            raise ConflictError('A timer is already running. Stop it first.')
        now = _now()
        today = local_date_of(_ms(now))
        assert_week_open(employee_id, today)
        work = check_work(company_id, employee_id, policy, b.get('projectId'), b.get('activityId'))
        assert_no_overlap(employee_id, now, now + timedelta(seconds=1), None)
        r = one("""INSERT INTO time_entry (company_id, employee_id, local_date, started_at, project_id, activity_id, note, billable, source, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'timer', %s) RETURNING id""",
                [company_id, employee_id, today, now, b.get('projectId'), b.get('activityId'), b.get('note') or None,
                 b['billable'] if b.get('billable') is not None else work['billable'], user.id])
        return entry_by_id(str(r['id']))


def start_timer(user: AuthUser, body: dict | None = None) -> dict:
    return _start_timer(user, parse(TIMER_BODY, body or {}))


def resume_timer(user: AuthUser) -> dict:
    """Start again on whatever was tracked last (Jibble's "resume")."""
    employee_id = own_employee(user)
    last = one("""SELECT project_id, activity_id, note, billable FROM time_entry WHERE employee_id = %s AND duration_min IS NOT NULL
                   ORDER BY coalesce(ended_at, created_at) DESC LIMIT 1""", [employee_id])
    if not last:
        raise BadRequestError('There is nothing to resume yet. Start a new timer.')
    last = _clean(last)
    return _start_timer(user, {'projectId': last['project_id'], 'activityId': last['activity_id'], 'note': last['note'],
                               'billable': last['billable']})


def stop_timer(user: AuthUser, body: dict | None = None) -> dict:
    employee_id = own_employee(user)
    b = parse(Obj({'note': Str(trim=True, max=500, nullish=True)}), body or {})
    auto_stop_stale_timers(_now(), employee_id)
    with _atomic():
        t = one('SELECT id, started_at FROM time_entry WHERE employee_id = %s AND ended_at IS NULL AND duration_min IS NULL FOR UPDATE',
                [employee_id])
        if not t:
            raise ConflictError('No timer is running.')
        # The entry ends now (so the next timer can start straight away); anything under a minute counts as one minute.
        started_ms = _ms(t['started_at'])
        end_ms = max(_ms(_now()), started_ms + 1)
        execute("""UPDATE time_entry SET ended_at = %s, duration_min = least(%s, 1440),
                          note = CASE WHEN %s::boolean THEN %s ELSE note END, updated_at = now() WHERE id = %s""",
                [_dt(end_ms), minutes_from_ms(end_ms - started_ms), 'note' in b, b.get('note') or None, t['id']])
        return entry_by_id(str(t['id']))


# ---------------------------------------------------------------- entries

def list_entries(user: AuthUser, query: dict) -> list[dict]:
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate(), 'employeeId': Uuid(optional=True)}), query)
    if q['to'] < q['from']:
        raise BadRequestError('from must be before to')
    employee_id = readable_employee(user, q.get('employeeId'))
    return _clean_all(rows(
        f"""SELECT {ENTRY_COLS} FROM {ENTRY_FROM} WHERE e.employee_id = %s AND e.local_date BETWEEN %s AND %s
             ORDER BY e.local_date DESC, e.started_at DESC NULLS LAST, e.created_at DESC LIMIT 2000""",
        [employee_id, q['from'], q['to']]))


def get_entry(user: AuthUser, entry_id: str) -> dict:
    """One of the signed-in person's own entries (for the edit form)."""
    employee_id = own_employee(user)
    e = entry_by_id(parse_uuid(entry_id))
    if not e or e['employee_id'] != employee_id:
        raise NotFoundError('entry not found')
    return e


def add_entry(user: AuthUser, body: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    b = parse(MANUAL_ENTRY, body)
    policy = enabled_policy(company_id)
    span = resolve_span(b)
    with _atomic():
        _lock(employee_id)
        assert_week_open(employee_id, b['date'])
        work = check_work(company_id, employee_id, policy, b.get('projectId'), b.get('activityId'))
        if span['startedAt'] and span['endedAt']:
            assert_no_overlap(employee_id, span['startedAt'], span['endedAt'], None)
        if day_total(employee_id, b['date'], None) + span['durationMin'] > DAY_LIMIT_MIN:
            raise BadRequestError('A day cannot have more than 24 hours of time.')
        r = one("""INSERT INTO time_entry (company_id, employee_id, local_date, started_at, ended_at, duration_min, project_id, activity_id,
                                           note, billable, source, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s) RETURNING id""",
                [company_id, employee_id, b['date'], span['startedAt'], span['endedAt'], span['durationMin'], b.get('projectId'),
                 b.get('activityId'), b.get('note') or None,
                 b['billable'] if b.get('billable') is not None else work['billable'], user.id])
    return entry_by_id(str(r['id']))


def update_entry(user: AuthUser, entry_id: str, body: dict) -> dict:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    eid = parse_uuid(entry_id)
    b = parse(MANUAL_PATCH, body)
    policy = enabled_policy(company_id)
    with _atomic():
        _lock(employee_id)
        cur = one("""SELECT employee_id, to_char(local_date, 'YYYY-MM-DD') AS local_date, started_at, ended_at, duration_min, project_id,
                            activity_id, note, billable
                       FROM time_entry WHERE id = %s AND company_id = %s FOR UPDATE""", [eid, company_id])
        cur = _clean(cur)
        if not cur or cur['employee_id'] != employee_id:
            raise NotFoundError('entry not found')
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
        note = (b.get('note') or None) if 'note' in b else cur['note']
        execute("""UPDATE time_entry SET local_date = %s, started_at = %s, ended_at = %s, duration_min = %s, project_id = %s, activity_id = %s,
                          note = %s, billable = %s, auto_stopped = CASE WHEN %s::boolean THEN false ELSE auto_stopped END, updated_at = now()
                    WHERE id = %s""",
                [day, span['startedAt'], span['endedAt'], span['durationMin'], project_id, activity_id, note, billable,
                 time_changed, eid])
    return entry_by_id(eid)


def delete_entry(user: AuthUser, entry_id: str) -> None:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    eid = parse_uuid(entry_id)
    with _atomic():
        cur = _clean(one("""SELECT employee_id, to_char(local_date, 'YYYY-MM-DD') AS local_date FROM time_entry
                             WHERE id = %s AND company_id = %s FOR UPDATE""", [eid, company_id]))
        if not cur or cur['employee_id'] != employee_id:
            raise NotFoundError('entry not found')
        assert_week_open(employee_id, cur['local_date'])
        execute('DELETE FROM time_entry WHERE id = %s', [eid])


# ---------------------------------------------------------------- weeks and timesheets

TS_COLS = """t.id, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, t.status, t.total_min, t.note, t.submitted_at, t.decided_at,
  t.decision_comment, t.workflow_instance_id, u.name AS decided_by_name"""


def week_view(company_id: str, employee_id: str, week_start: str) -> dict:
    """One employee's week: entries per day, rounded totals and the comparison with attended time."""
    policy = load_time_policy(company_id)
    week_end = add_days_iso(week_start, 6)
    entries = _clean_all(rows(
        f"""SELECT {ENTRY_COLS} FROM {ENTRY_FROM} WHERE e.employee_id = %s AND e.local_date BETWEEN %s AND %s
             ORDER BY e.local_date, e.started_at NULLS LAST, e.created_at""", [employee_id, week_start, week_end]))
    att = rows("""SELECT to_char(local_date, 'YYYY-MM-DD') AS local_date, worked_min, status FROM v_attendance_latest
                   WHERE employee_id = %s AND local_date BETWEEN %s AND %s""", [employee_id, week_start, week_end])
    att_by = {a['local_date']: a for a in att}
    # The latest submission decides the week's state; earlier returned or withdrawn ones are history.
    ts = _clean_all(rows(f"""SELECT {TS_COLS} FROM timesheet t LEFT JOIN app_user u ON u.id = t.decided_by
                              WHERE t.employee_id = %s AND t.week_start = %s ORDER BY t.submitted_at DESC, t.id DESC""",
                         [employee_id, week_start]))
    today = npt_today()
    days = []
    for i in range(7):
        d = add_days_iso(week_start, i)
        lst = [e for e in entries if e['local_date'] == d]
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
            'running': any(e['duration_min'] is None for e in entries),
            'timesheet': ts[0] if ts else None, 'history': ts[1:],
            'rounding': {'step': policy['roundingStep'], 'mode': policy['roundingMode']}}


def week(user: AuthUser, query: dict | None = None) -> dict:
    q = parse(Obj({'weekStart': IsoDate(optional=True), 'date': IsoDate(optional=True), 'employeeId': Uuid(optional=True)}), query or {})
    employee_id = readable_employee(user, q.get('employeeId'))
    if employee_id == user.employee_id:
        auto_stop_stale_timers(_now(), employee_id)
    return week_view(company_id_of(user), employee_id, week_start_of(q.get('weekStart') or q.get('date') or npt_today()))


def submit_timesheet(user: AuthUser, body: dict) -> str:
    """Submit a week for approval. Each submission is its own row and approval; returned weeks can be fixed and sent again."""
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    b = parse(Obj({'weekStart': IsoDate(), 'note': Str(trim=True, max=500, nullish=True)}), body)
    if not is_sunday(b['weekStart']):
        raise BadRequestError('weekStart must be a Sunday')
    if b['weekStart'] > npt_today():
        raise BadRequestError('You cannot submit a week that has not started.')
    enabled_policy(company_id)
    with _atomic():
        _lock(employee_id)
        t = one("""SELECT coalesce(sum(duration_min), 0)::int AS min, count(*)::int AS n,
                          count(*) FILTER (WHERE duration_min IS NULL)::int AS running
                     FROM time_entry WHERE employee_id = %s AND local_date BETWEEN %s::date AND %s::date + 6""",
                [employee_id, b['weekStart'], b['weekStart']])
        if t['running']:
            raise ConflictError('Stop your running timer before submitting this week.')
        if not t['n']:
            raise BadRequestError('There is no time logged in this week.')
        assert_week_open(employee_id, b['weekStart'])
        r = one("""INSERT INTO timesheet (company_id, employee_id, week_start, status, total_min, note, submitted_by)
                   VALUES (%s, %s, %s, 'submitted', %s, %s, %s) RETURNING id""",
                [company_id, employee_id, b['weekStart'], t['min'], b.get('note') or None, user.id])
        ts_id = str(r['id'])
        inst = start_workflow(company_id=company_id, object_type='timesheet', object_id=ts_id, employee_id=employee_id,
                              title='Timesheet to approve',
                              body=f"{user.name} submitted {to_fixed(t['min'] / 60, 1)} h for {bs_range(b['weekStart'], add_days_iso(b['weekStart'], 6))}.")
        execute('UPDATE timesheet SET workflow_instance_id = %s WHERE id = %s', [inst['id'], ts_id])
        audit('submit', 'timesheet', actor_id=user.id, row_id=ts_id, after={'weekStart': b['weekStart'], 'totalMin': t['min']})
    return ts_id


def withdraw_timesheet(user: AuthUser, timesheet_id: str) -> None:
    company_id = company_id_of(user)
    employee_id = own_employee(user)
    tid = parse_uuid(timesheet_id)
    with _atomic():
        cur = _clean(one('SELECT employee_id, status FROM timesheet WHERE id = %s AND company_id = %s FOR UPDATE', [tid, company_id]))
        if not cur or cur['employee_id'] != employee_id:
            raise NotFoundError('timesheet not found')
        if cur['status'] != 'submitted':
            raise ConflictError(f"Only a timesheet waiting for approval can be withdrawn (it is {cur['status']}).")
        execute("UPDATE timesheet SET status = 'cancelled', decided_at = now() WHERE id = %s", [tid])
        cancel_workflow('timesheet', tid)
        audit('withdraw', 'timesheet', actor_id=user.id, row_id=tid)


def list_timesheets(user: AuthUser, query: dict | None = None) -> list[dict]:
    """Timesheets for review: own, a manager's team or (HR) everyone, with tracked vs attended hours for each week."""
    q = parse(Obj({'status': Enum(STATUSES, optional=True), 'from': IsoDate(optional=True), 'to': IsoDate(optional=True),
                   'scope': Enum(('own', 'team', 'all'), optional=True), 'employeeId': Uuid(optional=True)}), query or {})
    scope = scope_of(user, q.get('scope'))
    if q.get('employeeId'):
        readable_employee(user, q['employeeId'])
    out = _clean_all(rows(
        f"""SELECT t.id, t.employee_id, emp.emp_code, emp.name_en, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, t.status, t.total_min,
                   t.note, t.submitted_at, t.decided_at, t.decision_comment, t.workflow_instance_id, w.state AS workflow_state,
                   du.name AS decided_by_name,
                   (SELECT coalesce(sum(a.worked_min), 0)::int FROM v_attendance_latest a
                     WHERE a.employee_id = t.employee_id AND a.local_date BETWEEN t.week_start AND t.week_start + 6) AS attendance_min,
                   (SELECT coalesce(sum(te.duration_min) FILTER (WHERE te.billable), 0)::int FROM time_entry te
                     WHERE te.employee_id = t.employee_id AND te.local_date BETWEEN t.week_start AND t.week_start + 6) AS billable_min
              FROM timesheet t JOIN employee emp ON emp.id = t.employee_id
              LEFT JOIN workflow_instance w ON w.id = t.workflow_instance_id LEFT JOIN app_user du ON du.id = t.decided_by
             WHERE t.company_id = %s AND (%s::text IS NULL OR t.status = %s) AND (%s::date IS NULL OR t.week_start >= %s::date - 6)
               AND (%s::date IS NULL OR t.week_start <= %s) AND (%s::uuid IS NULL OR t.employee_id = %s::uuid) AND {SCOPE_SQL}
             ORDER BY t.week_start DESC, t.submitted_at DESC, emp.name_en LIMIT 500""",
        [company_id_of(user), q.get('status'), q.get('status'), q.get('from'), q.get('from'), q.get('to'), q.get('to'),
         q.get('employeeId'), q.get('employeeId'), *_scope_params(scope, user.employee_id)]))
    for t in out:
        t['diff_min'] = t['total_min'] - t['attendance_min']
    return out


def timesheet_detail(user: AuthUser, timesheet_id: str) -> dict:
    company_id = company_id_of(user)
    tid = parse_uuid(timesheet_id)
    row = _clean(one("""SELECT t.employee_id, to_char(t.week_start, 'YYYY-MM-DD') AS week_start, e.emp_code, e.name_en
                          FROM timesheet t JOIN employee e ON e.id = t.employee_id WHERE t.id = %s AND t.company_id = %s""",
                     [tid, company_id]))
    if not row:
        raise NotFoundError('timesheet not found')
    assert_manages(user, row['employee_id'], 'attendance')
    sub = _clean(one(f'SELECT {TS_COLS} FROM timesheet t LEFT JOIN app_user u ON u.id = t.decided_by WHERE t.id = %s', [tid]))
    return {'employee': {'id': row['employee_id'], 'empCode': row['emp_code'], 'name': row['name_en']},
            **week_view(company_id, row['employee_id'], row['week_start']), 'submission': sub}


def team_week(user: AuthUser, query: dict | None = None) -> dict:
    """Who on the team has (not) submitted a week: the manager's at-a-glance view."""
    q = parse(Obj({'weekStart': IsoDate(optional=True), 'scope': Enum(('team', 'all'), optional=True)}), query or {})
    scope = scope_of(user, q.get('scope') or ('all' if has_module(user, 'attendance') else 'team'))
    ws = week_start_of(q.get('weekStart') or npt_today())
    people = _clean_all(rows(
        f"""SELECT emp.id AS employee_id, emp.emp_code, emp.name_en,
                   (SELECT coalesce(sum(te.duration_min), 0)::int FROM time_entry te
                     WHERE te.employee_id = emp.id AND te.local_date BETWEEN %s::date AND %s::date + 6) AS tracked_min,
                   (SELECT coalesce(sum(a.worked_min), 0)::int FROM v_attendance_latest a
                     WHERE a.employee_id = emp.id AND a.local_date BETWEEN %s::date AND %s::date + 6) AS attendance_min,
                   t.id AS timesheet_id, t.status
              FROM employee emp
              LEFT JOIN LATERAL (SELECT id, status FROM timesheet ts WHERE ts.employee_id = emp.id AND ts.week_start = %s
                                  ORDER BY ts.submitted_at DESC, ts.id DESC LIMIT 1) t ON true
             WHERE emp.company_id = %s AND emp.status = 'active' AND {SCOPE_SQL} AND (%s::text <> 'team' OR emp.id <> %s::uuid)
             ORDER BY emp.name_en""",
        [ws, ws, ws, ws, ws, company_id_of(user), *_scope_params(scope, user.employee_id), scope, user.employee_id]))
    # emp.id <> NULL is NULL (not true) in SQL: a manager without an employee record sees nobody, as in TS.
    return {'weekStart': ws, 'weekEnd': add_days_iso(ws, 6), 'people': people}


# ---------------------------------------------------------------- reports

REPORT_QUERY = Obj({
    'from': IsoDate(), 'to': IsoDate(), 'groupBy': Enum(GROUP_BY, default='project'),
    'employeeId': Uuid(optional=True), 'projectId': Uuid(optional=True), 'clientId': Uuid(optional=True),
    'scope': Enum(('own', 'team', 'all'), optional=True), 'format': Enum(('json', 'csv'), optional=True),
})


def _report_entries(user: AuthUser, q: dict) -> list[dict]:
    if q['to'] < q['from']:
        raise BadRequestError('from must be before to')
    if (_d(q['to']) - _d(q['from'])).days > 400:
        raise BadRequestError('Pick a range of at most 400 days.')
    scope = scope_of(user, q.get('scope'))
    if q.get('employeeId'):
        readable_employee(user, q['employeeId'])
    return _clean_all(rows(
        f"""SELECT e.id, e.employee_id, emp.emp_code, emp.name_en, to_char(e.local_date, 'YYYY-MM-DD') AS local_date, e.duration_min,
                   e.billable, e.note, e.project_id, p.code AS project_code, p.name AS project_name, p.bill_rate, p.budget_hours,
                   p.client_id, c.name AS client_name, e.activity_id, a.name AS activity_name, e.started_at, e.ended_at
              FROM time_entry e JOIN employee emp ON emp.id = e.employee_id
              LEFT JOIN time_project p ON p.id = e.project_id LEFT JOIN time_client c ON c.id = p.client_id
              LEFT JOIN time_activity a ON a.id = e.activity_id
             WHERE e.company_id = %s AND e.duration_min IS NOT NULL AND e.local_date BETWEEN %s AND %s
               AND (%s::uuid IS NULL OR e.employee_id = %s::uuid) AND (%s::uuid IS NULL OR e.project_id = %s::uuid)
               AND (%s::uuid IS NULL OR p.client_id = %s::uuid)
               AND {SCOPE_SQL}
             ORDER BY e.local_date, emp.name_en, e.started_at NULLS LAST""",
        [company_id_of(user), q['from'], q['to'], q.get('employeeId'), q.get('employeeId'), q.get('projectId'),
         q.get('projectId'), q.get('clientId'), q.get('clientId'), *_scope_params(scope, user.employee_id)]))


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
    q = parse(REPORT_QUERY, query)
    policy = load_time_policy(company_id_of(user))
    entries = _report_entries(user, q)
    gb = q['groupBy']
    groups: dict[str, dict] = {}
    for e in entries:
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
    burn: dict[str, int] = {}
    ids = [k for k in groups if k != 'none'] if gb == 'project' else []
    if ids:
        for x in rows("""SELECT project_id, sum(duration_min)::int AS min FROM time_entry
                          WHERE project_id = ANY(%s::uuid[]) AND duration_min IS NOT NULL GROUP BY 1""", [ids]):
            burn[str(x['project_id'])] = x['min']
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
            'billable_amount': js_num(js_round(g['amount'] * 100) / 100),
            'budget_hours': js_num(bh) if bh is not None else None,
            'budget_used_pct': js_num(js_round(((burn.get(g['key'], 0)) / 60 / bh) * 1000) / 10) if bh else None,
        })
    columns = summary_columns(gb)
    hours = sum(x['hours'] for x in out)
    billable = sum(x['billable_hours'] for x in out)
    amount = sum(x['billable_amount'] for x in out)
    return {'columns': columns, 'rows': out,
            'totals': {'hours': fixed_num(hours), 'billableHours': fixed_num(billable), 'billableAmount': js_num(js_round(amount * 100) / 100)},
            'rounding': {'step': policy['roundingStep'], 'mode': policy['roundingMode']},
            'filename': f"time-by-{gb}-{q['from']}-to-{q['to']}", 'format': q.get('format')}


ENTRY_REPORT_COLUMNS = [
    {'key': 'date', 'header': 'Date (AD)'}, {'key': 'emp_code', 'header': 'Employee code'}, {'key': 'name', 'header': 'Employee'},
    {'key': 'client', 'header': 'Client'}, {'key': 'project', 'header': 'Project'}, {'key': 'activity', 'header': 'Activity'},
    {'key': 'start', 'header': 'Start'}, {'key': 'end', 'header': 'End'}, {'key': 'minutes', 'header': 'Minutes tracked'},
    {'key': 'hours', 'header': 'Hours (rounded)'}, {'key': 'billable', 'header': 'Billable'}, {'key': 'note', 'header': 'Note'},
]


def _npt_hhmm(d: datetime | None) -> str:
    if not d:
        return ''
    return _dt(_ms(d) + 345 * 60_000).strftime('%H:%M')


def report_entries(user: AuthUser, query: dict) -> dict:
    """GET /reports/entries: {columns, rows, filename}."""
    q = parse(REPORT_QUERY, query)
    policy = load_time_policy(company_id_of(user))
    out = [{
        'date': e['local_date'], 'emp_code': e['emp_code'], 'name': e['name_en'], 'client': e['client_name'] or '',
        'project': e['project_name'] or '', 'activity': e['activity_name'] or '', 'start': _npt_hhmm(e['started_at']),
        'end': _npt_hhmm(e['ended_at']), 'minutes': e['duration_min'], 'hours': fixed_num(round_entry(e['duration_min'], policy) / 60),
        'billable': 'Yes' if e['billable'] else 'No', 'note': e['note'] or '',
    } for e in _report_entries(user, q)]
    return {'columns': ENTRY_REPORT_COLUMNS, 'rows': out, 'filename': f"time-entries-{q['from']}-to-{q['to']}", 'format': q.get('format')}

