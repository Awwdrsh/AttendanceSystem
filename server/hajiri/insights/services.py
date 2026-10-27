"""
Workforce insights for HR and executives (port of apps/api/src/modules/insights/service.ts and routes.ts): headcount,
joiners and leavers, attrition, demographics, people cost, overtime, leave and attendance trends by Bikram Sambat
month, filtered by branch and department, and every dataset as CSV.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from functools import cmp_to_key
from typing import Any

from engines.bs_calendar import BS_MONTH_NAMES_EN, ad_to_bs, bs_month_bounds

from hajiri.core.auth import AuthUser, company_id_of, has_role, MODULE_ROLES
from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError
from hajiri.core.schema import Obj, Str, Uuid, parse

FILTER_QUERY = Obj({
    'from': Str(regex=r'^\d{4}-\d{1,2}$', optional=True),
    'to': Str(regex=r'^\d{4}-\d{1,2}$', optional=True),
    'branchId': Uuid(optional=True),
    'orgUnitId': Uuid(optional=True),
})

# Axis abbreviations that stay distinct (Ashadh and Ashwin both start "Ash").
SHORT_MONTHS = ['Bai', 'Jes', 'Asr', 'Shr', 'Bha', 'Asw', 'Kar', 'Man', 'Pou', 'Mag', 'Fal', 'Cha']
IDENT = re.compile(r'^[a-z_][a-z0-9_]*$')


def js_round(n: float, d: int = 1) -> int | float:
    """Math.round(n * 10 ** d) / 10 ** d, with whole results as int (as JSON would show them)."""
    f = 10 ** d
    v = math.floor(n * f + 0.5) / f
    return int(v) if float(v).is_integer() else v


def _num(v: Any) -> int | float:
    v = float(v)
    return int(v) if v.is_integer() else v


def _require(user: AuthUser) -> None:
    roles = MODULE_ROLES['insights']
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


# ---------------------------------------------------------------- months

def _ym(s: str) -> tuple[int, int]:
    m = re.fullmatch(r'(\d{4})-(\d{1,2})', s)
    if not m:
        raise BadRequestError('months are written as YYYY-MM in the Bikram Sambat calendar')
    y, mo = int(m[1]), int(m[2])
    if mo < 1 or mo > 12:
        raise BadRequestError('month must be 1 to 12')
    return y, mo


def _key(y: int, m: int) -> str:
    return f'{y}-{m:02d}'


def _next(y: int, m: int) -> tuple[int, int]:
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _prev(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def months_between(frm: str | None, to: str | None, today: str) -> list[dict]:
    """BS months from `frm` to `to` inclusive (default: the last 12 months up to this one), at most 36."""
    now = ad_to_bs(today)
    end = _ym(to) if to else (now.year, now.month)
    start = _ym(frm) if frm else end
    if not frm:
        for _ in range(11):
            start = _prev(*start)
    out: list[dict] = []
    cur = start
    while cur[0] < end[0] or (cur[0] == end[0] and cur[1] <= end[1]):
        try:
            b = bs_month_bounds(cur[0], cur[1])
        except Exception:
            raise BadRequestError('that range is outside the supported calendar')
        name = BS_MONTH_NAMES_EN[cur[1] - 1]
        out.append({'key': _key(*cur), 'label': f'{name} {cur[0]}', 'short': f'{SHORT_MONTHS[cur[1] - 1]} {str(cur[0])[-2:]}',
                    'startAd': b['startAd'], 'endAd': b['endAd']})
        if len(out) > 36:
            raise BadRequestError('pick at most 36 months')
        cur = _next(*cur)
    if not out:
        raise BadRequestError('the start month is after the end month')
    return out


# ---------------------------------------------------------------- exit reasons

def exit_reason_source() -> str | None:
    """
    Exit reasons are optional data (they may live on the employee or in an offboarding table). Look for them at run
    time; without such data the voluntary/involuntary split is simply not shown.
    """
    col = one("""SELECT column_name FROM information_schema.columns
                  WHERE table_schema = current_schema() AND table_name = 'employee'
                    AND column_name IN ('exit_reason', 'exit_type', 'separation_type', 'separation_reason')
                  ORDER BY array_position(ARRAY['separation_type', 'exit_type', 'exit_reason', 'separation_reason'], column_name::text)
                  LIMIT 1""")
    if col and IDENT.match(col['column_name']):
        return f'e."{col["column_name"]}"::text'
    t = one("""SELECT c.table_name, c.column_name FROM information_schema.columns c
                WHERE c.table_schema = current_schema()
                  AND (c.table_name LIKE '%%exit%%' OR c.table_name LIKE '%%separation%%' OR c.table_name LIKE '%%offboarding%%'
                       OR c.table_name LIKE '%%resignation%%')
                  AND c.column_name IN ('separation_type', 'exit_type', 'reason_type', 'kind', 'type')
                  AND EXISTS (SELECT 1 FROM information_schema.columns x WHERE x.table_schema = c.table_schema
                                AND x.table_name = c.table_name AND x.column_name = 'employee_id')
                ORDER BY c.table_name LIMIT 1""")
    if t and IDENT.match(t['table_name']) and IDENT.match(t['column_name']):
        return f'(SELECT x."{t["column_name"]}"::text FROM "{t["table_name"]}" x WHERE x.employee_id = e.id LIMIT 1)'
    return None


def classify_exit(kind: str | None) -> str | None:
    if not kind:
        return None
    k = kind.lower()
    if re.search(r'involuntary|terminat|dismiss|layoff|lay_off|redundan|fired|misconduct', k):
        return 'involuntary'
    if re.search(r'resign|voluntary|quit', k):
        return 'voluntary'
    return 'other'


# ---------------------------------------------------------------- helpers

def _years_between(a: str, b: str) -> float:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days / 365.25


def _bands(items: list, labels: list[str], pick) -> list[dict]:
    m = {label: 0 for label in labels}
    for r in items:
        k = pick(r)
        m[k] = m.get(k, 0) + 1
    return [{'label': k, 'count': v} for k, v in m.items()]


def _locale_compare(a: str, b: str) -> int:
    ka, kb = a.casefold(), b.casefold()
    return (ka > kb) - (ka < kb) or (a > b) - (a < b)


def _iso(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)[:10]


# ---------------------------------------------------------------- filters

def filter_options(user: AuthUser) -> dict:
    """Branches and departments for the filter row."""
    _require(user)
    c = company_id_of(user)
    return {
        'branches': [{'id': str(r['id']), 'name': r['name']} for r in
                     rows('SELECT id, name_en AS name FROM branch WHERE company_id = %s ORDER BY name_en', [c])],
        'orgUnits': [{'id': str(r['id']), 'name': r['name'], 'kind': r['kind'], 'parentId': str(r['parentId']) if r['parentId'] else None}
                     for r in rows('SELECT id, name_en AS name, kind, parent_id AS "parentId" FROM org_unit WHERE company_id = %s ORDER BY name_en', [c])],
    }


def parse_filters(user: AuthUser, query: dict) -> dict:
    """The TS route's `filters`: query shape, then the branch and department must belong to the company."""
    _require(user)
    q = parse(FILTER_QUERY, {k: v for k, v in query.items() if k in ('from', 'to', 'branchId', 'orgUnitId') and v not in (None, '')})
    c = company_id_of(user)
    if q.get('branchId') and not one('SELECT 1 FROM branch WHERE id = %s AND company_id = %s', [q['branchId'], c]):
        raise BadRequestError('unknown branch')
    if q.get('orgUnitId') and not one('SELECT 1 FROM org_unit WHERE id = %s AND company_id = %s', [q['orgUnitId'], c]):
        raise BadRequestError('unknown department')
    return {'companyId': c, 'from': q.get('from'), 'to': q.get('to'), 'branchId': q.get('branchId'),
            'orgUnitId': q.get('orgUnitId'), 'today': npt_today().isoformat()}


# ---------------------------------------------------------------- the computation

def compute_insights(f: dict) -> dict:
    """Everything the Insights page shows, for one company, filter and BS month range."""
    months = months_between(f.get('from'), f.get('to'), f['today'])
    first, last = months[0], months[-1]
    reason_expr = exit_reason_source()
    emps = rows(f"""
        WITH RECURSIVE units AS (
            SELECT id FROM org_unit WHERE id = %(unit)s::uuid AND company_id = %(c)s
            UNION SELECT o.id FROM org_unit o JOIN units ON o.parent_id = units.id)
        SELECT e.id, e.gender, e.dob, coalesce(e.join_date, (e.created_at AT TIME ZONE 'Asia/Kathmandu')::date) AS joined,
               CASE WHEN e.exit_date IS NOT NULL THEN e.exit_date WHEN e.status = 'exited' THEN (e.updated_at AT TIME ZONE 'Asia/Kathmandu')::date END AS exited,
               e.status, o.name_en AS unit, b.name_en AS branch, {reason_expr or 'NULL::text'} AS exit_kind
          FROM employee e
          LEFT JOIN LATERAL (SELECT a.branch_id, a.org_unit_id FROM employee_assignment a WHERE a.employee_id = e.id
                              ORDER BY (a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date) DESC, lower(a.valid) DESC LIMIT 1) a ON true
          LEFT JOIN org_unit o ON o.id = a.org_unit_id LEFT JOIN branch b ON b.id = a.branch_id
         WHERE e.company_id = %(c)s AND (%(branch)s::uuid IS NULL OR a.branch_id = %(branch)s::uuid)
           AND (%(unit)s::uuid IS NULL OR a.org_unit_id IN (SELECT id FROM units))""",
                {'c': f['companyId'], 'branch': f.get('branchId'), 'unit': f.get('orgUnitId')})
    for e in emps:
        e['id'] = str(e['id'])
        e['joined'] = _iso(e['joined'])
        e['exited'] = _iso(e['exited'])
        e['dob'] = _iso(e['dob'])
    ids = [e['id'] for e in emps]

    def active_at(ad: str) -> list[dict]:
        return [e for e in emps if e['joined'] <= ad and (not e['exited'] or e['exited'] > ad)]

    def before(ad: str) -> str:
        return (date.fromisoformat(ad) - timedelta(days=1)).isoformat()

    headcount = []
    for m in months:
        start = len(active_at(before(m['startAd'])))
        end = len(active_at(m['endAd']))
        joins = sum(1 for e in emps if m['startAd'] <= e['joined'] <= m['endAd'])
        exit_rows = [e for e in emps if e['exited'] and m['startAd'] <= e['exited'] <= m['endAd']]
        avg_hc = (start + end) / 2
        headcount.append({
            'month': m['key'], 'label': m['label'], 'short': m['short'], 'headcount': end, 'joins': joins,
            'exits': len(exit_rows), 'net': joins - len(exit_rows),
            'attritionPct': js_round(len(exit_rows) / avg_hc * 100, 2) if avg_hc > 0 else 0,
            'voluntary': sum(1 for e in exit_rows if classify_exit(e['exit_kind']) == 'voluntary'),
            'involuntary': sum(1 for e in exit_rows if classify_exit(e['exit_kind']) == 'involuntary'),
        })
    total_exits = sum(h['exits'] for h in headcount)
    avg_monthly = sum(h['attritionPct'] for h in headcount) / len(headcount)
    with_reason = [e for e in emps if e['exited'] and first['startAd'] <= e['exited'] <= last['endAd'] and classify_exit(e['exit_kind'])]
    attrition = {
        'exits': total_exits, 'monthlyAvgPct': js_round(avg_monthly, 2), 'annualisedPct': js_round(avg_monthly * 12, 1),
        'split': {
            'voluntary': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'voluntary'),
            'involuntary': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'involuntary'),
            'other': sum(1 for e in with_reason if classify_exit(e['exit_kind']) == 'other'),
        } if reason_expr and with_reason else None,
    }

    as_of = last['endAd'] if last['endAd'] < f['today'] else f['today']
    current = active_at(as_of)

    def tenure_band(e):
        y = _years_between(e['joined'], as_of)
        return 'Under 1 year' if y < 1 else '1–2 years' if y < 2 else '2–5 years' if y < 5 else '5–10 years' if y < 10 else '10+ years'

    def age_band(e):
        if not e['dob']:
            return 'Not recorded'
        y = _years_between(e['dob'], as_of)
        return 'Under 25' if y < 25 else '25–34' if y < 35 else '35–44' if y < 45 else '45–54' if y < 55 else '55 and over'

    tenure = _bands(current, ['Under 1 year', '1–2 years', '2–5 years', '5–10 years', '10+ years'], tenure_band)
    age = _bands(current, ['Under 25', '25–34', '35–44', '45–54', '55 and over', 'Not recorded'], age_band)
    gender = [g for g in _bands(current, ['Female', 'Male', 'Other', 'Not recorded'],
                                lambda e: {'female': 'Female', 'male': 'Male', 'other': 'Other'}.get(e['gender'], 'Not recorded'))
              if g['count'] > 0 or g['label'] != 'Other']

    def group_count(pick, none: str) -> list[dict]:
        m: dict[str, int] = {}
        for e in current:
            k = pick(e) or none
            m[k] = m.get(k, 0) + 1
        out = [{'label': k, 'count': v} for k, v in m.items()]
        out.sort(key=cmp_to_key(lambda a, b: (b['count'] - a['count']) or _locale_compare(a['label'], b['label'])))
        return out

    departments = group_count(lambda e: e['unit'], 'No department')
    branches = group_count(lambda e: e['branch'], 'No branch')

    mp = {'ids': ids, 'keys': [m['key'] for m in months], 'starts': [m['startAd'] for m in months],
          'ends': [m['endAd'] for m in months], 'c': f['companyId'], 'first': first['startAd'], 'last': last['endAd']}
    month_cte = 'm AS (SELECT * FROM unnest(%(keys)s::text[], %(starts)s::date[], %(ends)s::date[]) AS m(key, start_ad, end_ad))'

    att = rows(f"""
        WITH {month_cte}
        SELECT m.key,
               count(d.*) FILTER (WHERE d.status IN ('Present', 'HalfDay', 'Absent', 'WFH', 'Incomplete', 'OnLeave', 'HalfDayLeave'))::int AS scheduled,
               count(d.*) FILTER (WHERE d.status IN ('Present', 'HalfDay', 'WFH', 'Incomplete'))::int AS present,
               count(d.*) FILTER (WHERE d.status = 'Absent')::int AS absent,
               count(d.*) FILTER (WHERE d.late_min > 0)::int AS late,
               coalesce(sum(d.ot_min), 0)::int AS ot_min,
               avg(d.worked_min) FILTER (WHERE d.worked_min > 0)::text AS avg_worked
          FROM m LEFT JOIN v_attendance_latest d ON d.local_date BETWEEN m.start_ad AND m.end_ad AND d.employee_id = ANY(%(ids)s::uuid[])
         GROUP BY m.key""", mp)
    ot_approved = rows(f"""
        WITH {month_cte}
        SELECT m.key, coalesce(sum(s.approved_min), 0)::int AS min FROM m
          LEFT JOIN ot_slip s ON s.local_date BETWEEN m.start_ad AND m.end_ad AND s.status IN ('approved', 'posted')
                             AND s.employee_id = ANY(%(ids)s::uuid[])
         GROUP BY m.key""", mp)
    att_by = {r['key']: r for r in att}
    ot_by = {r['key']: r['min'] for r in ot_approved}
    attendance = []
    for m in months:
        a = att_by.get(m['key'])
        scheduled = a['scheduled'] if a else 0
        present = a['present'] if a else 0
        absent = a['absent'] if a else 0
        late = a['late'] if a else 0
        attendance.append({
            'month': m['key'], 'label': m['label'], 'short': m['short'], 'scheduledDays': scheduled, 'presentDays': present,
            'absentDays': absent, 'lateDays': late,
            'absenteeismPct': js_round(absent / scheduled * 100, 1) if scheduled else None,
            'latePct': js_round(late / present * 100, 1) if present else None,
            'avgWorkedHours': js_round(float(a['avg_worked']) / 60, 2) if a and a['avg_worked'] else None,
        })
    overtime = [{'month': m['key'], 'label': m['label'], 'short': m['short'],
                 'detectedHours': js_round(((att_by.get(m['key']) or {}).get('ot_min') or 0) / 60, 1),
                 'approvedHours': js_round((ot_by.get(m['key']) or 0) / 60, 1)} for m in months]

    leave_q = rows("""
        SELECT lt.name_en AS type, lt.code, coalesce(-sum(l.delta_days), 0)::text AS days
          FROM leave_ledger l JOIN leave_type lt ON lt.id = l.leave_type_id
         WHERE lt.company_id = %(c)s AND l.employee_id = ANY(%(ids)s::uuid[]) AND l.reason IN ('usage', 'cancellation')
           AND l.effective_date BETWEEN %(first)s AND %(last)s
         GROUP BY lt.name_en, lt.code, lt.sort_order ORDER BY lt.sort_order""", mp)
    avg_headcount = sum(h['headcount'] for h in headcount) / len(headcount)
    leave = [x for x in ({'type': r['type'], 'code': r['code'], 'days': js_round(float(r['days']), 1),
                          'perEmployee': js_round(float(r['days']) / avg_headcount, 2) if avg_headcount else 0} for r in leave_q)
             if x['days'] > 0]
    leave_monthly = rows(f"""
        WITH {month_cte}
        SELECT m.key, coalesce(-sum(l.delta_days), 0)::text AS days FROM m
          LEFT JOIN leave_ledger l ON l.effective_date BETWEEN m.start_ad AND m.end_ad AND l.reason IN ('usage', 'cancellation')
                                  AND l.employee_id = ANY(%(ids)s::uuid[])
         GROUP BY m.key""", mp)
    leave_by = {r['key']: float(r['days']) for r in leave_monthly}

    # People cost from locked (or later) pay runs only: drafts can still change.
    cost_q = rows("""
        SELECT (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) AS key, sum(p.gross)::text AS gross,
               sum(p.employer_statutory)::text AS employer, sum(p.net)::text AS net, sum(p.tds)::text AS tds,
               count(DISTINCT p.employee_id)::int AS people
          FROM payslip p JOIN payroll_run r ON r.id = p.run_id JOIN payroll_period pp ON pp.id = r.period_id
         WHERE r.company_id = %(c)s AND r.status IN ('locked', 'disbursed', 'filed') AND p.employee_id = ANY(%(ids)s::uuid[])
           AND (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) = ANY(%(keys)s::text[])
         GROUP BY 1""", mp)
    cost_by = {r['key']: r for r in cost_q}
    cost = []
    for m in months:
        c = cost_by.get(m['key'])
        gross = float(c['gross']) if c else 0.0
        employer = float(c['employer']) if c else 0.0
        cost.append({'month': m['key'], 'label': m['label'], 'short': m['short'], 'gross': js_round(gross, 2),
                     'employerContributions': js_round(employer, 2), 'totalCost': js_round(gross + employer, 2),
                     'net': js_round(float(c['net']) if c else 0, 2), 'tds': js_round(float(c['tds']) if c else 0, 2),
                     'people': c['people'] if c else 0, 'locked': bool(c)})
    unit_of = {e['id']: e['unit'] or 'No department' for e in emps}
    cost_dept_q = rows("""
        SELECT p.employee_id, sum(p.gross)::text AS gross, sum(p.employer_statutory)::text AS employer
          FROM payslip p JOIN payroll_run r ON r.id = p.run_id JOIN payroll_period pp ON pp.id = r.period_id
         WHERE r.company_id = %(c)s AND r.status IN ('locked', 'disbursed', 'filed') AND p.employee_id = ANY(%(ids)s::uuid[])
           AND (pp.bs_year || '-' || lpad(pp.bs_month::text, 2, '0')) = ANY(%(keys)s::text[])
         GROUP BY p.employee_id""", mp)
    dept_cost: dict[str, float] = {}
    for r in cost_dept_q:
        k = unit_of.get(str(r['employee_id']), 'No department')
        dept_cost[k] = dept_cost.get(k, 0) + float(r['gross']) + float(r['employer'])
    cost_by_department = sorted(({'label': k, 'totalCost': js_round(v, 2)} for k, v in dept_cost.items()),
                                key=lambda x: -x['totalCost'])

    # Headline tiles: the last month of the range against the one before it.
    last_h = headcount[-1]
    prev_h = headcount[-2] if len(headcount) > 1 else None
    # Pay is only final once a run is locked: compare the latest locked month with the locked month before it.
    locked_idx = [i for i, x in enumerate(cost) if x['locked']]
    last_c = cost[locked_idx[-1]] if locked_idx else None
    prev_c = cost[locked_idx[-2]] if len(locked_idx) > 1 else None
    last_a = attendance[-1]
    prev_a = attendance[-2] if len(attendance) > 1 else None

    def delta(a, b):
        return None if a is None or b is None else js_round(a - b, 2)

    leave_days = leave_by.get(last['key'], 0)
    prev_leave = leave_by.get(months[-2]['key'], 0) if prev_h else None
    g = lambda d, k: d[k] if d else None  # noqa: E731
    cost_kpi = {'key': 'cost', 'label': f"People cost, {last_c['label'].split(' ')[0]}" if last_c else 'People cost',
                'value': last_c['totalCost'] if last_c else None,
                'delta': delta(last_c['totalCost'], prev_c['totalCost']) if last_c and prev_c else None,
                'unit': 'NPR', 'goodWhen': 'neutral'}
    if prev_c:
        cost_kpi['vs'] = prev_c['label'].split(' ')[0]
    kpis = [
        {'key': 'headcount', 'label': 'Headcount', 'value': last_h['headcount'], 'delta': delta(last_h['headcount'], g(prev_h, 'headcount')), 'unit': '', 'goodWhen': 'neutral'},
        {'key': 'joins', 'label': 'Joined', 'value': last_h['joins'], 'delta': delta(last_h['joins'], g(prev_h, 'joins')), 'unit': '', 'goodWhen': 'neutral'},
        {'key': 'exits', 'label': 'Left', 'value': last_h['exits'], 'delta': delta(last_h['exits'], g(prev_h, 'exits')), 'unit': '', 'goodWhen': 'down'},
        {'key': 'attrition', 'label': 'Attrition a year', 'value': attrition['annualisedPct'], 'delta': None, 'unit': '%', 'goodWhen': 'down'},
        cost_kpi,
        {'key': 'absenteeism', 'label': 'Absenteeism', 'value': last_a['absenteeismPct'], 'delta': delta(last_a['absenteeismPct'], g(prev_a, 'absenteeismPct')), 'unit': '%', 'goodWhen': 'down'},
        {'key': 'late', 'label': 'Late arrivals', 'value': last_a['latePct'], 'delta': delta(last_a['latePct'], g(prev_a, 'latePct')), 'unit': '%', 'goodWhen': 'down'},
        {'key': 'hours', 'label': 'Average day', 'value': last_a['avgWorkedHours'], 'delta': delta(last_a['avgWorkedHours'], g(prev_a, 'avgWorkedHours')), 'unit': 'h', 'goodWhen': 'neutral'},
        {'key': 'leave', 'label': 'Leave taken', 'value': js_round(leave_days, 1), 'delta': delta(leave_days, prev_leave), 'unit': 'days', 'goodWhen': 'neutral'},
    ]

    return {
        'months': months, 'asOf': as_of, 'kpis': kpis, 'headcount': headcount, 'attrition': attrition, 'tenure': tenure,
        'age': age, 'gender': gender, 'departments': departments, 'branches': branches, 'cost': cost,
        'costByDepartment': cost_by_department, 'overtime': overtime, 'leave': leave,
        'leaveMonthly': [{'month': m['key'], 'label': m['label'], 'short': m['short'], 'days': js_round(leave_by.get(m['key'], 0), 1)}
                         for m in months],
        'attendance': attendance, 'exitReasonsAvailable': bool(reason_expr),
    }


def insights(user: AuthUser, query: dict) -> dict:
    """GET /insights: the filters checked against the company, then the computation."""
    return compute_insights(parse_filters(user, query))


# ---------------------------------------------------------------- CSV datasets

def _c(*pairs) -> list[dict]:
    return [{'key': k, 'header': h} for k, h in pairs]


DATASETS: dict[str, dict] = {
    'headcount': {'file': 'headcount', 'rows': lambda i: i['headcount'], 'columns': _c(
        ('label', 'Month'), ('headcount', 'Headcount at month end'), ('joins', 'Joined'), ('exits', 'Left'), ('net', 'Net change'),
        ('attritionPct', 'Attrition %'), ('voluntary', 'Voluntary exits'), ('involuntary', 'Involuntary exits'))},
    'tenure': {'file': 'tenure', 'rows': lambda i: i['tenure'], 'columns': _c(('label', 'Tenure'), ('count', 'Employees'))},
    'age': {'file': 'age', 'rows': lambda i: i['age'], 'columns': _c(('label', 'Age'), ('count', 'Employees'))},
    'gender': {'file': 'gender', 'rows': lambda i: i['gender'], 'columns': _c(('label', 'Gender'), ('count', 'Employees'))},
    'departments': {'file': 'departments', 'rows': lambda i: i['departments'], 'columns': _c(('label', 'Department'), ('count', 'Employees'))},
    'branches': {'file': 'branches', 'rows': lambda i: i['branches'], 'columns': _c(('label', 'Branch'), ('count', 'Employees'))},
    'cost': {'file': 'people-cost', 'rows': lambda i: i['cost'], 'columns': _c(
        ('label', 'Month'), ('gross', 'Gross pay'), ('employerContributions', 'Employer SSF/PF/gratuity'), ('totalCost', 'Total cost'),
        ('net', 'Net pay'), ('tds', 'TDS'), ('people', 'People paid'))},
    'costByDepartment': {'file': 'people-cost-by-department', 'rows': lambda i: i['costByDepartment'],
                         'columns': _c(('label', 'Department'), ('totalCost', 'Total cost'))},
    'overtime': {'file': 'overtime', 'rows': lambda i: i['overtime'], 'columns': _c(
        ('label', 'Month'), ('detectedHours', 'Overtime detected (h)'), ('approvedHours', 'Overtime approved (h)'))},
    'leave': {'file': 'leave-by-type', 'rows': lambda i: i['leave'], 'columns': _c(
        ('type', 'Leave type'), ('days', 'Days taken'), ('perEmployee', 'Days per employee'))},
    'attendance': {'file': 'attendance', 'rows': lambda i: i['attendance'], 'columns': _c(
        ('label', 'Month'), ('scheduledDays', 'Scheduled days'), ('presentDays', 'Days worked'), ('absentDays', 'Absent days'),
        ('absenteeismPct', 'Absenteeism %'), ('lateDays', 'Late days'), ('latePct', 'Late %'), ('avgWorkedHours', 'Average hours per day'))},
}


def export_dataset(user: AuthUser, name: str, query: dict) -> tuple[str, list[dict], list[dict]]:
    """GET /insights/export/:dataset -> (filename, columns, rows). The dataset name is checked before the filters."""
    _require(user)
    ds = DATASETS.get(name) if name in DATASETS else None
    if not ds:
        raise BadRequestError(f'unknown dataset; use one of {", ".join(DATASETS)}')
    data = insights(user, query)
    return f'insights-{ds["file"]}', ds['columns'], ds['rows'](data)
