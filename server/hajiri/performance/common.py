"""
Shared helpers of the performance module (port of apps/api/src/modules/performance/service.ts): who may see whom,
key result progress, the review score and the once-only reminder guard. Learning imports these too.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Iterable

from django.db import IntegrityError, connection, transaction
from django.db.models import IntegerField, Subquery
from django.db.models.functions import Now

from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import NPT, npt_today
from hajiri.core.errors import ForbiddenError
from hajiri.core.scoping import get_or_404
from hajiri.db.models import Employee, EmployeeAssignment, PerfGoal, PerfKeyResult, PerfReminderSent

MANAGER_DEPTH = 5


# ---------------------------------------------------------------- small helpers

def round_half_up(x: float, places: int = 0):
    """Rounding with halves going up (2.5 -> 3, 2.125 -> 2.13 at two places), the rounding the TS app used."""
    d = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(d) if places == 0 else float(d)


def round2(n: float) -> float:
    return round_half_up(n, 2)


def num(v: Any) -> float:
    """A database number (Decimal, int, str) as a float; None counts as 0."""
    if v is None:
        return 0.0
    return float(v)


def fmt_number(v) -> str:
    """A number for display without trailing zeros: 4 not 4.00, 4.25 as is."""
    d = v if isinstance(v, Decimal) else Decimal(str(v)) if isinstance(v, str) else Decimal(repr(num(v)))
    return format(d.normalize(), 'f')


_COLUMN_KEYS = {'created_by_id': 'created_by', 'calibrated_by_id': 'calibrated_by', 'added_by_id': 'added_by',
                'nominated_by_id': 'nominated_by', 'assigned_by_id': 'assigned_by'}


def plain_row(r: dict | None) -> dict | None:
    """A values() row with UUIDs as strings and the `<fk>_id` keys of db_column foreign keys (created_by, added_by,
    ...) under their column names: the shape the TS API answered with."""
    if r is None:
        return None
    return {_COLUMN_KEYS.get(k, k): str(v) if isinstance(v, uuid.UUID) else v for k, v in r.items()}


def plain_rows(qs: Iterable[dict]) -> list[dict]:
    return [plain_row(r) for r in qs]


class SubqueryCount(Subquery):
    """count(*) of a correlated queryset as an integer column (0 when nothing matches)."""
    template = '(SELECT count(*) FROM (%(subquery)s) _count)'
    output_field = IntegerField()


def npt_day_bounds(d: date | str) -> tuple[datetime, datetime]:
    """The instants a Nepal calendar day starts and ends (end exclusive), for `__gte` / `__lt` filters."""
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    start = datetime.combine(d, time.min, tzinfo=NPT)
    return start, start + timedelta(days=1)


def me(user: AuthUser) -> str:
    if not user.employee_id:
        raise ForbiddenError('this account is not linked to an employee')
    return user.employee_id


def employee_in_company(company_id: str, employee_id: str) -> dict:
    """The employee must belong to the active company; another company's employee is "not found"."""
    e = get_or_404(Employee, company_id, 'employee not found', pk=employee_id)
    return {'id': str(e.id), 'name_en': e.name_en, 'designation': e.designation, 'grade': e.grade}


def current_assignments(on: date | None = None):
    """Employee assignments in force on a Nepal date (today by default)."""
    return EmployeeAssignment.objects.filter(valid__contains=on or npt_today())


# ---------------------------------------------------------------- the manager chain

def manager_chain(employee_id: str) -> list[str]:
    """Line managers above an employee today (direct manager first), up to five levels, cycle-safe."""
    today = npt_today()
    # Raw SQL: a recursive CTE walking the manager links upwards, which a queryset cannot express.
    with connection.cursor() as cur:
        cur.execute("""
            WITH RECURSIVE chain(emp, manager_id, depth, path) AS (
              SELECT a.employee_id, a.manager_id, 1, ARRAY[a.employee_id] FROM employee_assignment a
               WHERE a.employee_id = %s AND a.valid @> %s::date AND a.manager_id IS NOT NULL
              UNION ALL
              SELECT a.employee_id, a.manager_id, c.depth + 1, c.path || a.employee_id FROM chain c
                JOIN employee_assignment a ON a.employee_id = c.manager_id AND a.valid @> %s::date
               WHERE a.manager_id IS NOT NULL AND c.depth < %s AND NOT a.manager_id = ANY(c.path)
            ) SELECT manager_id FROM chain ORDER BY depth""", [employee_id, today, today, MANAGER_DEPTH])
        return [str(r[0]) for r in cur.fetchall()]


def subordinate_ids(manager_id: str) -> list[str]:
    """Employees whose manager chain (up to five levels) contains the given manager today."""
    today = npt_today()
    # Raw SQL: a recursive CTE walking the manager links downwards, which a queryset cannot express.
    with connection.cursor() as cur:
        cur.execute("""
            WITH RECURSIVE subs(emp, depth) AS (
              SELECT a.employee_id, 1 FROM employee_assignment a WHERE a.manager_id = %s AND a.valid @> %s::date
              UNION
              SELECT a.employee_id, s.depth + 1 FROM subs s
                JOIN employee_assignment a ON a.manager_id = s.emp AND a.valid @> %s::date WHERE s.depth < %s
            ) SELECT DISTINCT emp FROM subs""", [manager_id, today, today, MANAGER_DEPTH])
        return [str(r[0]) for r in cur.fetchall()]


def direct_manager(employee_id: str) -> str | None:
    m = current_assignments().filter(employee_id=employee_id).values_list('manager_id', flat=True).first()
    return str(m) if m else None


def can_see_employee(user: AuthUser, employee_id: str) -> bool:
    """HR, the employee, or anyone in their manager chain."""
    if has_role(user, 'hr'):
        return True
    if not user.employee_id:
        return False
    if user.employee_id == str(employee_id):
        return True
    return user.employee_id in manager_chain(str(employee_id))


def is_above_in_chain(user: AuthUser, employee_id: str) -> bool:
    """HR, or someone in the employee's manager chain (not the employee)."""
    if has_role(user, 'hr'):
        return True
    if not user.employee_id or user.employee_id == str(employee_id):
        return False
    return user.employee_id in manager_chain(str(employee_id))


# ---------------------------------------------------------------- progress and scores

def key_result_progress(k: dict) -> float:
    """Progress of one key result in percent, clamped to 0..100. Works for decreasing targets (e.g. cut defects 20 -> 5)."""
    cur = num(k['current_value'])
    if k['measure'] == PerfKeyResult.Measure.BOOLEAN:
        return 100 if cur >= 1 else 0
    start = num(k['start_value'])
    target = num(k['target_value'])
    if target == start:
        if cur == target:
            return 100
        if target > start:
            return 100 if cur >= target else 0
        return 100 if cur <= target else 0
    return round2(min(100, max(0, ((cur - start) / (target - start)) * 100)))


def goal_progress(krs: list[dict]) -> float | None:
    """Weighted average of the key results; None when the goal has none (progress is then set by check-ins)."""
    if not krs:
        return None
    total = sum(num(k['weight']) for k in krs)
    if total <= 0:
        return None
    return round2(sum(key_result_progress(k) * num(k['weight']) for k in krs) / total)


def recompute_goal_progress(goal_id: str) -> None:
    krs = list(PerfKeyResult.objects.filter(goal_id=goal_id).values('measure', 'start_value', 'target_value', 'current_value', 'weight'))
    p = goal_progress(krs)
    if p is not None:
        PerfGoal.objects.filter(pk=goal_id).update(progress=p, updated_at=Now())


def overall_score(items: Iterable[dict], goals_weight: float, competency_weight: float) -> float | None:
    """
    Overall score on the 1-5 scale: the weighted goal average and the plain competency average, blended with the
    cycle's weights. A part with no ratings is left out and the other part carries the full weight.
    """
    items = list(items)
    goals = [i for i in items if i['item_type'] == 'goal' and i['rating'] is not None]
    comps = [i for i in items if i['item_type'] == 'competency' and i['rating'] is not None]

    def w(i):
        return num(i['weight']) if num(i['weight']) > 0 else 1
    gw = sum(w(i) for i in goals)
    g = sum(i['rating'] * w(i) for i in goals) / gw if goals else None
    c = sum(i['rating'] for i in comps) / len(comps) if comps else None
    if g is None and c is None:
        return None
    if g is None:
        return round2(c)
    if c is None:
        return round2(g)
    total = goals_weight + competency_weight
    if total <= 0:
        return round2((g + c) / 2)
    return round2((g * goals_weight + c * competency_weight) / total)


def first_time(key: str) -> bool:
    """Once-only guard for reminders: True the first time a key is seen."""
    try:
        with transaction.atomic():
            _, created = PerfReminderSent.objects.get_or_create(key=key)
    except IntegrityError:          # another worker recorded it between the read and the insert
        return False
    return created


def label_of_rating(n, labels: list[str]) -> str:
    """'4 · Exceeds expectations' (parts.tsx ratingLabel); 'Not rated' when empty."""
    if n is None or n == '':
        return 'Not rated'
    v = num(n)
    lbl = labels[min(5, max(1, round_half_up(v))) - 1] if labels else ''
    return f'{fmt_number(v)} · {lbl}' if float(v).is_integer() else f'{v:.2f} · {lbl}'
