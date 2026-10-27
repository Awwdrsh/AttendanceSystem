"""
Shared helpers of the performance module (port of apps/api/src/modules/performance/service.ts): who may see whom,
key result progress, the review score and the once-only reminder guard. Learning imports these too.
"""
from __future__ import annotations

import html
import math
from typing import Any, Iterable

from hajiri.core.auth import AuthUser, has_role
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ForbiddenError, NotFoundError


def js_round(x: float) -> int:
    """Math.round: halves go up (towards +infinity), unlike Python's round()."""
    return math.floor(x + 0.5)


def round2(n: float) -> float:
    return js_round(n * 100) / 100


def num(v: Any) -> float:
    """JS Number() of a database value (Decimal, int, str)."""
    if v is None:
        return 0.0
    return float(v)


def esc(s: Any) -> str:
    """Plain-text escaping for the printable pages."""
    return html.escape('' if s is None else str(s), quote=True).replace('&#x27;', '&#39;')


def me(user: AuthUser) -> str:
    if not user.employee_id:
        raise ForbiddenError('this account is not linked to an employee')
    return user.employee_id


def employee_in_company(company_id: str, employee_id: str) -> dict:
    """The employee must belong to the company; another company's employee is "not found"."""
    r = one('SELECT id, name_en, designation, grade FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def manager_chain(employee_id: str) -> list[str]:
    """Line managers above an employee today (direct manager first), up to five levels, cycle-safe."""
    r = rows("""
        WITH RECURSIVE chain(emp, manager_id, depth, path) AS (
          SELECT a.employee_id, a.manager_id, 1, ARRAY[a.employee_id] FROM employee_assignment a
           WHERE a.employee_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND a.manager_id IS NOT NULL
          UNION ALL
          SELECT a.employee_id, a.manager_id, c.depth + 1, c.path || a.employee_id FROM chain c
            JOIN employee_assignment a ON a.employee_id = c.manager_id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
           WHERE a.manager_id IS NOT NULL AND c.depth < 5 AND NOT a.manager_id = ANY(c.path)
        ) SELECT manager_id FROM chain ORDER BY depth""", [employee_id])
    return [str(x['manager_id']) for x in r]


def direct_manager(employee_id: str) -> str | None:
    r = one('SELECT manager_id FROM employee_assignment WHERE employee_id = %s AND valid @> (now() AT TIME ZONE \'Asia/Kathmandu\')::date', [employee_id])
    return str(r['manager_id']) if r and r['manager_id'] else None


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
    if k['measure'] == 'boolean':
        return 100 if cur >= 1 else 0
    start = num(k['start_value'])
    target = num(k['target_value'])
    if target == start:
        if cur == target:
            return 100
        if target > start:
            return 100 if cur >= target else 0
        return 100 if cur <= target else 0
    p = ((cur - start) / (target - start)) * 100
    return js_round(min(100, max(0, p)) * 100) / 100


def goal_progress(krs: list[dict]) -> float | None:
    """Weighted average of the key results; None when the goal has none (progress is then set by check-ins)."""
    if not krs:
        return None
    total = sum(num(k['weight']) for k in krs)
    if total <= 0:
        return None
    p = sum(key_result_progress(k) * num(k['weight']) for k in krs) / total
    return js_round(p * 100) / 100


def recompute_goal_progress(goal_id: str) -> None:
    krs = rows('SELECT measure, start_value, target_value, current_value, weight FROM perf_key_result WHERE goal_id = %s', [goal_id])
    p = goal_progress(krs)
    if p is not None:
        execute('UPDATE perf_goal SET progress = %s, updated_at = now() WHERE id = %s', [p, goal_id])


def overall_score(items: Iterable[dict], goals_weight: float, competency_weight: float) -> float | None:
    """
    Overall score on the 1-5 scale: the weighted goal average and the plain competency average, blended with the
    cycle's weights. A part with no ratings is left out and the other part carries the full weight.
    """
    items = list(items)
    goals = [i for i in items if i['item_type'] == 'goal' and i['rating'] is not None]
    comps = [i for i in items if i['item_type'] == 'competency' and i['rating'] is not None]
    w = lambda i: num(i['weight']) if num(i['weight']) > 0 else 1  # noqa: E731
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
    return execute('INSERT INTO perf_reminder_sent (key) VALUES (%s) ON CONFLICT DO NOTHING', [key]) > 0


# Employees whose manager chain (up to five levels) contains the given manager (one %s: the manager's employee id).
SUBORDINATES_CTE = """
  WITH RECURSIVE subs(emp, depth) AS (
    SELECT a.employee_id, 1 FROM employee_assignment a WHERE a.manager_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
    UNION
    SELECT a.employee_id, s.depth + 1 FROM subs s JOIN employee_assignment a ON a.manager_id = s.emp AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date WHERE s.depth < 5
  )"""


def label_of_rating(n, labels: list[str]) -> str:
    """'4 · Exceeds expectations' (parts.tsx ratingLabel); 'Not rated' when empty."""
    if n is None or n == '':
        return 'Not rated'
    v = num(n)
    lbl = labels[min(5, max(1, js_round(v))) - 1] if labels else ''
    return f'{fmt_number(v)} · {lbl}' if float(v).is_integer() else f'{v:.2f} · {lbl}'


def fmt_number(v) -> str:
    """JS Number(x).toString() for display: 4 not 4.0, 4.25 as is."""
    f = num(v)
    if f.is_integer():
        return str(int(f))
    return repr(f).rstrip('0').rstrip('.') if 'e' not in repr(f) else str(f)
