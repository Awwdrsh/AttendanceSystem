"""
Executive and employee home dashboards (port of apps/api/src/modules/dashboard/routes.ts; plan §3, §13.2: at most
6 KPI tiles, one trend, one "needs attention" list). The Overview page renders `executive`; the Home page (home
slice) uses `me`.
"""
from __future__ import annotations

from engines.bs_calendar import BsRangeError, ad_to_bs, bs_month_bounds

from hajiri.attendance.service import list_days
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, ForbiddenError

EXECUTIVE_ROLES = ('hr', 'manager', 'executive')


def _month(today: str) -> tuple[str, str, str | None]:
    """(start, end, 'YYYY-MM' BS label) of the BS month containing today; today itself outside the calendar table."""
    try:
        bs = ad_to_bs(today)
        b = bs_month_bounds(bs.year, bs.month)
        return str(b['startAd']), str(b['endAd']), f'{bs.year}-{bs.month:02d}'
    except (BsRangeError, ValueError):
        return today, today, None


def executive(user: AuthUser, today: str | None = None) -> dict:
    if not has_role(user, *EXECUTIVE_ROLES):
        raise ForbiddenError(f'requires role {" or ".join(EXECUTIVE_ROLES)}')
    cid = user.company_id
    today = today or npt_today().isoformat()
    try:
        bs = ad_to_bs(today)
        month_start = str(bs_month_bounds(bs.year, bs.month)['startAd'])
    except (BsRangeError, ValueError):
        month_start = today[:8] + '01'
    att = "('Present', 'HalfDay', 'HalfDayLeave', 'WFH', 'Incomplete')"
    k = one(f"""
        SELECT (SELECT count(*) FROM employee WHERE company_id = %(c)s AND status = 'active')::int AS headcount,
               (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = %(c)s AND v.local_date = %(t)s AND v.status IN {att})::int AS present,
               (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = %(c)s AND v.local_date = %(t)s AND v.late_min > 0)::int AS late,
               (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = %(c)s AND v.local_date = %(t)s AND v.status = 'Absent')::int AS absent,
               (SELECT count(*) FROM workflow_instance WHERE company_id = %(c)s AND state = 'pending')::int AS pending,
               (SELECT count(*) FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id WHERE e.company_id = %(c)s AND v.local_date BETWEEN %(m)s AND %(t)s AND v.locked_at IS NULL
                  AND (v.status IN ('Incomplete', 'Absent') OR v.flags @> '[{{"kind":"OTPreapprovalMissing"}}]' OR v.flags @> '[{{"kind":"OTCapExceeded"}}]'))::int AS exceptions""",
        {'c': cid, 't': today, 'm': month_start})
    trend = rows("""
        SELECT v.local_date::text AS local_date,
               round(100.0 * count(*) FILTER (WHERE v.status IN ('Present', 'HalfDay', 'HalfDayLeave', 'WFH'))
                     / nullif(count(*) FILTER (WHERE v.status NOT IN ('Holiday', 'WeeklyOff', 'OffShift')), 0), 1)::float8 AS present_pct
          FROM v_attendance_latest v JOIN employee e ON e.id = v.employee_id
         WHERE e.company_id = %(c)s AND v.local_date > %(t)s::date - 30 AND v.local_date <= %(t)s GROUP BY v.local_date ORDER BY v.local_date""",
        {'c': cid, 't': today})
    devices = rows("""
        SELECT serial_no, name, last_seen_at FROM device WHERE company_id = %s AND status = 'active' AND vendor NOT IN ('mobile', 'kiosk', 'manual')
           AND (last_seen_at IS NULL OR last_seen_at < now() - interval '120 seconds') ORDER BY last_seen_at NULLS FIRST""", [cid])
    unmapped = one("""SELECT count(*)::int AS n FROM punch_event p JOIN device d ON d.id = p.device_id WHERE d.company_id = %s
                        AND p.employee_id IS NULL AND NOT EXISTS (SELECT 1 FROM punch_event s WHERE s.supersedes_id = p.id)""", [cid])['n']
    ot = one("""SELECT count(*) FILTER (WHERE s.status = 'detected')::int AS n,
                       coalesce(sum(s.detected_min) FILTER (WHERE s.local_date >= %s), 0)::int AS minutes
                  FROM ot_slip s JOIN employee e ON e.id = s.employee_id WHERE e.company_id = %s""", [month_start, cid])
    leave_old = one("""SELECT count(*)::int AS n FROM leave_request lr JOIN employee e ON e.id = lr.employee_id
                        WHERE e.company_id = %s AND lr.status = 'pending' AND lr.created_at < now() - interval '24 hours'""", [cid])['n']
    last_run = one("""
        SELECT r.id, r.status, r.run_type, p.bs_year, p.bs_month, (SELECT count(*) FROM payslip s WHERE s.run_id = r.id)::int AS headcount,
               coalesce((SELECT sum(net) FROM payslip s WHERE s.run_id = r.id), 0)::text AS net,
               coalesce((SELECT sum(gross) FROM payslip s WHERE s.run_id = r.id), 0)::text AS gross
          FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.company_id = %s ORDER BY r.created_at DESC LIMIT 1""", [cid])
    attention = [{'kind': 'device_offline', 'label': f"{d['name'] or d['serial_no']} is offline", 'since': d['last_seen_at']} for d in devices]
    if unmapped > 0:
        attention.append({'kind': 'unmapped_punches', 'label': f'{unmapped} punches from unknown PINs', 'count': unmapped})
    if ot['n'] > 0:
        attention.append({'kind': 'ot_unapproved', 'label': f"{ot['n']} overtime slips awaiting review", 'count': ot['n']})
    if leave_old > 0:
        attention.append({'kind': 'leave_overdue', 'label': f'{leave_old} leave requests waiting over 24 h', 'count': leave_old})
    if last_run:
        last_run = {**last_run, 'id': str(last_run['id'])}
    return {
        'date': today,
        'tiles': [
            {'key': 'headcount', 'label': 'Active employees', 'value': k['headcount']},
            {'key': 'present', 'label': 'Present today', 'value': k['present']},
            {'key': 'late', 'label': 'Late today', 'value': k['late']},
            {'key': 'absent', 'label': 'Absent today', 'value': k['absent']},
            {'key': 'pendingApprovals', 'label': 'Pending approvals', 'value': k['pending']},
            {'key': 'exceptions', 'label': 'Open exceptions this month', 'value': k['exceptions']},
        ],
        'trend': [{'date': t['local_date'], 'presentPct': t['present_pct']} for t in trend],
        'needsAttention': attention,
        'otMinutesThisMonth': ot['minutes'],
        # Payroll totals are HR-only; managers get the attendance tiles.
        'lastPayrollRun': last_run if has_role(user, 'hr', 'payroll_admin', 'executive') else None,
    }


def me(user: AuthUser, today: str | None = None) -> dict:
    """The employee home: today, this BS month's summary, leave balances, unread notifications and pending requests."""
    employee_id = user.employee_id
    if not employee_id:
        raise BadRequestError('Your account is not linked to an employee record.')
    today = today or npt_today().isoformat()
    month_start, month_end, bs_label = _month(today)
    days = list_days(employee_id, today, today)
    month = rows("""SELECT status, count(*)::int AS n, coalesce(sum(late_min), 0)::int AS late, coalesce(sum(ot_min), 0)::int AS ot
                      FROM v_attendance_latest WHERE employee_id = %s AND local_date BETWEEN %s AND %s GROUP BY status""",
                 [employee_id, month_start, month_end])
    balances = rows("""
        SELECT lt.code, lt.name_en, lt.name_ne, coalesce(sum(l.delta_days), 0)::numeric(8,2)::text AS balance
          FROM leave_type lt JOIN employee e ON e.company_id = lt.company_id AND e.id = %s
          LEFT JOIN leave_ledger l ON l.employee_id = e.id AND l.leave_type_id = lt.id
         WHERE lt.accrual_basis <> 'none' OR lt.code = 'SUBSTITUTE'
         GROUP BY lt.id, lt.code, lt.name_en, lt.name_ne, lt.sort_order ORDER BY lt.sort_order""", [employee_id])
    unread = one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND channel = 'in_app' AND read_at IS NULL", [user.id])['n']
    pending = rows("""
        SELECT 'leave' AS kind, id, created_at, from_date::text || ' to ' || to_date::text AS detail FROM leave_request WHERE employee_id = %(e)s AND status = 'pending'
        UNION ALL SELECT 'regularisation', id, created_at, local_date::text || ' ' || kind FROM regularisation_request WHERE employee_id = %(e)s AND status = 'pending'
        UNION ALL SELECT 'ot', id, created_at, local_date::text || ' ' || requested_min || ' min' FROM ot_request WHERE employee_id = %(e)s AND status = 'pending'
        ORDER BY created_at DESC LIMIT 20""", {'e': employee_id})
    wfh = one("""SELECT e.work_from_home, EXISTS (SELECT 1 FROM regularisation_request r WHERE r.employee_id = e.id AND r.kind = 'wfh'
                        AND r.status = 'approved' AND r.local_date = %s) AS approved_today
                   FROM employee e WHERE e.id = %s""", [today, employee_id])
    summary: dict[str, int] = {}
    late_min = ot_min = 0
    for m in month:
        summary[m['status']] = m['n']
        late_min += m['late']
        ot_min += m['ot']
    return {
        'date': today, 'today': days[0] if days else None,
        'month': {'bs': bs_label, 'from': month_start, 'to': month_end, 'byStatus': summary, 'lateMin': late_min, 'otMin': ot_min},
        'leaveBalances': balances, 'unreadNotifications': unread, 'pendingRequests': pending,
        'workFromHome': {'allowed': bool(wfh and wfh['work_from_home']), 'approvedToday': bool(wfh and wfh['approved_today'])},
    }


# ---------------------------------------------------------------- Overview page helpers

def team_today(user: AuthUser, today: str) -> list[dict]:
    """The Team Today rows the Overview page draws (attendance.today); empty for a role that may not see them."""
    from hajiri.attendance import api
    try:
        return api.today(user, {'date': today})['rows']
    except ForbiddenError:
        return []


def first_shift(company_id: str) -> dict | None:
    """The company's first shift by code (shifts list order), for the arrivals ribbon's start line and grace."""
    return one('SELECT start_time, grace_in_min FROM shift WHERE company_id = %s ORDER BY code LIMIT 1', [company_id])


def _minute_of_day(at) -> int:
    from hajiri.core.clock import NPT
    t = at.astimezone(NPT)
    return t.hour * 60 + t.minute


def arrivals_ribbon(team: list[dict], shift_start: str = '10:00', grace: int = 15) -> dict:
    """ArrivalsRibbon.tsx geometry: one mark per first clock-in on a time axis, stacked within a few minutes."""
    sh, sm = (int(x) for x in shift_start.split(':')[:2])
    start = sh * 60 + sm
    arrivals = [r for r in team if r.get('first_in')]
    times = [_minute_of_day(r['first_in']) for r in arrivals]
    lo = (min([start - 60, *times]) - 10) // 30 * 30
    hi = -((-(max([start + 60, *times]) + 10)) // 30) * 30
    span = hi - lo

    def pos(m: int) -> float:
        return round((min(max(m, lo), hi) - lo) / span * 100, 3)

    bucket = max(2, round_half_up((hi - lo) / 60))
    stacks: dict[int, int] = {}
    marks = []
    from hajiri.web.format import fmt_time
    for r, m in zip(arrivals, times):
        b = m // bucket
        level = stacks.get(b, 0)
        stacks[b] = level + 1
        late = (r.get('late_min') or 0) > 0
        marks.append({'left': pos(b * bucket), 'bottom': 4 + level * 11, 'level': level, 'late': late,
                      'title': f"{r['name_en']}, {fmt_time(r['first_in'])}{' (late)' if late else ''}"})
    hours = []
    t = -(-lo // 30) * 30
    while t <= hi:
        hours.append({'left': pos(t), 'label': f'{t // 60:02d}:{t % 60:02d}'})
        t += 30
    max_level = max([3, *[mk['level'] + 1 for mk in marks]])
    return {'marks': marks, 'hours': hours, 'height': max_level * 11 + 8, 'shift_left': pos(start),
            'grace_width': round(grace / span * 100, 3), 'shift_start': shift_start}


def round_half_up(x: float) -> int:
    """Math.round."""
    import math
    return math.floor(x + 0.5)


def overview(user: AuthUser) -> dict:
    """Everything the Overview page (DashboardPage.tsx) shows."""
    from hajiri.core.clock import now
    from hajiri.web.format import fmt_date_long, greeting
    today = npt_today().isoformat()
    d = executive(user, today)
    tile = {t['key']: t['value'] for t in d['tiles']}
    team = team_today(user, today)
    shift = first_shift(user.company_id)
    start = shift['start_time'].strftime('%H:%M') if shift else '10:00'
    grace = shift['grace_in_min'] if shift and shift['grace_in_min'] is not None else 15
    clocked = [r for r in team if r.get('first_in')]
    recent = sorted(clocked, key=lambda r: r['first_in'], reverse=True)[:6]
    from hajiri.web.templatetags.hj import live_status
    recent = [{**r, 'live_status': live_status(r['status'], on_date=today, today=today, last_out=r.get('last_out'),
                                               first_in=r.get('first_in')) or 'NotComputed'} for r in recent]
    not_in = len([r for r in team if not r.get('first_in') and r.get('status') not in ('WeeklyOff', 'Holiday', 'OnLeave')])
    first_name = (user.name or '').split()[0] if (user.name or '').split() else ''
    stats = [
        {'label': 'In today', 'value': tile['present'], 'hint': f"of {tile['headcount']} active employees"},
        {'label': 'Late', 'value': tile['late'], 'hint': 'after shift grace', **({'tone': 'warn'} if tile['late'] > 0 else {})},
        {'label': 'Absent', 'value': tile['absent'], 'hint': f'{not_in} not clocked in yet'},
        {'label': 'Pending approvals', 'value': tile['pendingApprovals'], 'hint': 'leave, corrections, overtime'},
        {'label': 'Open exceptions', 'value': tile['exceptions'], 'hint': 'this payroll month'},
    ]
    attention_links = {
        'device_offline': ('/settings/devices', 'fingerprint', 'Check device'),
        'unmapped_punches': ('/settings/devices', 'fingerprint', 'Map PINs'),
        'ot_unapproved': ('/attendance/overtime', 'clock-hour-4', 'Review'),
        'leave_overdue': ('/approvals', 'inbox', 'Open approvals'),
    }
    attention = []
    for n in d['needsAttention']:
        to, ico, action = attention_links.get(n['kind'], ('/overview', 'alert-triangle', 'Open'))
        attention.append({**n, 'to': to, 'icon': ico, 'action': action})
    return {
        'title': f"{greeting(now())}{', ' + first_name if first_name else ''}",
        'subtitle': fmt_date_long(today),
        'today': today,
        'data': d,
        'stats': stats,
        'ribbon': arrivals_ribbon(team, start, grace),
        'trend': [{'date': p['date'], 'value': p['presentPct']} for p in d['trend'] if p['date'] != today],
        'attention': attention,
        'recent': recent,
        'clocked_count': len(clocked),
    }
