"""
Home, notifications and settings pages (HomePage.tsx, NotificationsPage.tsx, SettingsPage.tsx, SetupPage.tsx,
FeaturesPage.tsx, UsersPage.tsx). The rules live in the owning modules (punch, dashboard, company_settings, features,
core.users); this module only shapes their answers for the pages, as the React components did.
"""
from __future__ import annotations

import re
from datetime import datetime

from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import now
from hajiri.core.errors import HttpError
from hajiri.web.format import fmt_date_long, fmt_time, minutes_to_hm
from hajiri.web.templatetags.hj import live_status

# ---------------------------------------------------------------- Home: today card

DIRECTION_LABEL = {'in': 'Clocked in', 'out': 'Clocked out'}
SOURCE_LABEL = {'web': 'Web', 'mobile': 'Mobile', 'device': 'Terminal', 'manual': 'Added by HR', 'regularisation': 'Correction',
                'kiosk': 'Kiosk', 'field': 'Field visit'}
CORRECTION = {'wfh': 'Work from home', 'missed_punch': 'Missed punch', 'on_duty': 'On duty elsewhere', 'half_day': 'Half day',
              'shift_change': 'Shift change'}


def direction_label(d: str) -> str:
    return DIRECTION_LABEL.get(d, 'Recorded')


def source_label(s: str) -> str:
    return SOURCE_LABEL.get(s, s)


def describe_flag(f: dict) -> str:
    """HomePage describeFlag."""
    from hajiri.attendance_tools.views import describe_flag as d
    return d(f)


def _ms(v) -> float:
    if isinstance(v, datetime):
        return v.timestamp() * 1000
    return datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp() * 1000


def worked_from_punches(punches: list[dict], now_ms: float) -> int:
    """Minutes between paired clock-ins and clock-outs, counting an open clock-in up to `now`."""
    total = 0.0
    open_at = None
    live = sorted((p for p in punches if not p.get('superseded')), key=lambda p: _ms(p['punched_at']))
    for p in live:
        ms = _ms(p['punched_at'])
        if p['direction'] == 'in' and open_at is None:
            open_at = ms
        elif p['direction'] == 'out' and open_at is not None:
            total += ms - open_at
            open_at = None
    if open_at is not None:
        total += max(0.0, now_ms - open_at)
    return int(total // 60_000)


def next_direction(punches: list[dict]) -> str:
    last = punches[-1] if punches else None
    if not last or last['direction'] == 'out':
        return 'in'
    return 'in' if len(punches) % 2 == 0 else 'out'


def today_card(today: dict, me: dict | None, shifts: list[dict], at: datetime | None = None) -> dict:
    """Everything the clock-in card draws, from GET /punch/today, GET /dashboard/me and GET /shifts."""
    from hajiri.core.clock import npt_today
    at = at or now()
    today_iso = npt_today().isoformat()
    punches = today.get('punches') or []
    for p in punches:
        p['label'] = direction_label(p['direction'])
        p['source_text'] = 'From home' if p.get('remote') else source_label(p.get('source') or '')
        p['time'] = fmt_time(p['punched_at'])
    date_iso = today.get('date') or (me or {}).get('date') or today_iso
    day = today.get('day')
    nd = next_direction(punches)
    wfh = (me or {}).get('workFromHome') or {}
    wfh_allowed, wfh_today = bool(wfh.get('allowed')), bool(wfh.get('approvedToday'))
    status = live_status(day['status'], on_date=date_iso, today=today_iso, last_out=day.get('last_out'),
                         first_in=day.get('first_in')) if day else None
    working = status == 'Working'
    flags = [f for f in ((day or {}).get('flags') or [])
             if not (working and str((f or {}).get('kind')) in ('MissingOut', 'UnpairedPunch'))]
    clocked_in = nd == 'out'
    last = punches[-1] if punches else None
    first_in = next((p for p in punches if p['direction'] == 'in' and not p.get('superseded')), None)
    now_ms = at.timestamp() * 1000
    if clocked_in:
        worked = worked_from_punches(punches, now_ms)
    else:
        wm = (day or {}).get('worked_min')
        worked = wm if wm is not None else worked_from_punches(punches, now_ms)
    shift = None
    if day and day.get('shift_id'):
        shift = next((s for s in shifts if str(s['id']) == str(day['shift_id'])), None)
    shift_line = (f"{shift['name_en'] or shift['code']} shift, {str(shift['start'])[:5]} to {str(shift['end'])[:5]}"
                  if shift else 'No shift assigned for today')
    if first_in and len(punches) > 1:
        shift_line += f". First clock-in {fmt_time(first_in['punched_at'])}"
    if clocked_in:
        state_line, tone = f"Clocked in at {fmt_time(last['punched_at'] if last else None)}", 'ok'
    elif last:
        state_line, tone = f"Clocked out at {fmt_time(last['punched_at'])}", 'idle'
    else:
        state_line, tone = 'Not clocked in yet', 'warn'
    return {
        'date': date_iso, 'date_label': fmt_date_long(date_iso), 'punches': punches, 'day': day, 'status': status,
        'late': (day or {}).get('late_min'), 'flags': [describe_flag(f) for f in flags], 'next_direction': nd,
        'clocked_in': clocked_in, 'worked': minutes_to_hm(worked), 'worked_label': 'Worked so far' if clocked_in else 'Worked today',
        'shift_line': shift_line, 'state_line': state_line, 'state_tone': tone, 'wfh_allowed': wfh_allowed,
        'wfh_today': wfh_today, 'home_camera': nd == 'in' and (wfh_allowed or wfh_today),
    }


# ---------------------------------------------------------------- Home: this month and pending requests

def _request_kind(k: str) -> str:
    known = {'leave': 'Time off', 'ot': 'Overtime', 'overtime': 'Overtime', 'regularisation': 'Attendance correction',
             'correction': 'Attendance correction'}
    return known.get(k) or (k[:1].upper() + k[1:].replace('_', ' '))


def describe_request(r: dict) -> dict:
    """Server details look like "2026-10-15 wfh", "2026-10-15 to 2026-10-17" or "2026-10-15 120 min"; say them in words."""
    detail = r.get('detail') or ''
    dates = [fmt_date_long(d) for d in re.findall(r'\d{4}-\d{2}-\d{2}', detail)]
    kind = r['kind']
    if kind == 'regularisation':
        k = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', detail, count=1)
        return {'title': CORRECTION.get(k) or _request_kind(kind), 'detail': dates[0] if dates else detail}
    if kind == 'leave':
        return {'title': 'Time off', 'detail': f'{dates[0]} to {dates[1]}' if len(dates) == 2 and dates[0] != dates[1]
                else (dates[0] if dates else detail)}
    if kind == 'ot':
        m = re.search(r'(\d+)\s*min', detail)
        return {'title': 'Overtime', 'detail': f"{dates[0] if dates else ''}{', ' + minutes_to_hm(int(m.group(1))) if m else ''}"}
    return {'title': _request_kind(kind), 'detail': detail}


def month_stats(month: dict) -> list[dict]:
    by = month.get('byStatus') or {}
    present = (by.get('Present') or 0) + (by.get('WFH') or 0) + (by.get('HalfDay') or 0)
    absent = by.get('Absent') or 0
    late, ot = month.get('lateMin') or 0, month.get('otMin') or 0
    return [
        {'label': 'Present days', 'value': present},
        {'label': 'Late', 'value': minutes_to_hm(late), **({'tone': 'warn'} if late > 0 else {})},
        {'label': 'Overtime', 'value': minutes_to_hm(ot)},
        {'label': 'Absent days', 'value': absent, **({'tone': 'bad'} if absent > 0 else {})},
    ]


# ---------------------------------------------------------------- Settings -> Setup checklist

def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def setup_counts(user: AuthUser) -> dict:
    """The numbers SetupPage.tsx loaded from eight endpoints (pay components may fail: then none)."""
    from hajiri.company_settings.services import company_header, list_branches
    from hajiri.devices.services import list_devices
    from hajiri.employees.services import list_employees
    from hajiri.leave.services import list_types
    from hajiri.shifts.services import holidays, shifts

    try:
        from hajiri.payroll.api import list_components
        comps = len(list_components(user)['components'])
    except HttpError:
        comps = 0
    devs = list_devices(user)
    return {
        'company': company_header(user)['company'], 'branches': len(list_branches(user)), 'holidays': len(holidays(user, {})),
        'shifts': len(shifts(user)), 'types': len(list_types(user)), 'comps': comps,
        'emps': len(list_employees(user, {'status': 'active'})),
        'devs': len([d for d in devs if d['vendor'] not in ('mobile', 'kiosk', 'manual')]),
        'pending': len([d for d in devs if d['status'] == 'pending']),
    }


def setup_steps(d: dict) -> list[dict]:
    p = _plural
    steps = [
        {'key': 'company', 'title': 'Company profile', 'done': bool(d['company'].get('nameEn')),
         'detail': d['company'].get('nameEn') or 'Add your company name, PAN and address.', 'go': '/settings', 'cta': 'Open settings'},
        {'key': 'branches', 'title': 'Branches', 'done': d['branches'] > 0,
         'detail': p(d['branches'], 'branch', 'branches') if d['branches'] > 0 else 'Add at least one branch or office.',
         'go': '/settings', 'cta': 'Open settings'},
        {'key': 'holidays', 'title': 'Holiday calendar', 'done': d['holidays'] > 0,
         'detail': p(d['holidays'], 'holiday', 'holidays') if d['holidays'] > 0 else 'Load the public holidays for this year.',
         'go': '/settings/holidays', 'cta': 'Open holidays', 'auto': {'label': 'Add public holidays', 'action': 'holidays'}},
        {'key': 'shifts', 'title': 'Shifts', 'done': d['shifts'] > 0,
         'detail': p(d['shifts'], 'shift', 'shifts') if d['shifts'] > 0 else 'Define working hours and grace periods.',
         'go': '/settings/shifts', 'cta': 'Open shifts',
         'auto': {'label': 'Create 10:00 to 18:00 shift', 'action': 'shift'} if d['shifts'] == 0 else None},
        {'key': 'leave', 'title': 'Leave policies', 'done': d['types'] > 0,
         'detail': f"{p(d['types'], 'leave type', 'leave types')}, statutory defaults included" if d['types'] > 0
         else 'Review the statutory leave types.', 'go': '/leave/policies', 'cta': 'Open leave types'},
        {'key': 'pay', 'title': 'Pay components and statutory rules', 'done': d['comps'] > 0,
         'detail': p(d['comps'], 'component', 'components') if d['comps'] > 0 else 'Set up basic pay, allowances and deductions.',
         'go': '/payroll/structures', 'cta': 'Open salary structures'},
        {'key': 'employees', 'title': 'Employees', 'done': d['emps'] > 0,
         'detail': f"{d['emps']} active" if d['emps'] > 0 else 'Add people one by one or import a CSV.', 'go': '/employees',
         'cta': 'Open employees'},
        {'key': 'devices', 'title': 'Attendance terminals', 'done': d['devs'] > 0 and d['pending'] == 0,
         'detail': f"{p(d['devs'], 'terminal', 'terminals')}, {d['pending']} waiting for approval" if d['devs'] > 0
         else 'Connect your biometric terminals.', 'go': '/settings/devices', 'cta': 'Open devices'},
        {'key': 'pilot', 'title': 'Pilot, then go live', 'done': False,
         'detail': 'Run one department for two pay cycles alongside your current method before switching everyone over.',
         'go': '/overview', 'cta': 'Open overview'},
    ]
    nxt = next((i for i, s in enumerate(steps) if not s['done']), -1)
    for i, s in enumerate(steps):
        s['n'] = i + 1
        s['is_next'] = i == nxt
        s.setdefault('auto', None)
    return steps


# The shift SetupPage.tsx creates in one click.
GENERAL_SHIFT = {'code': 'GEN', 'nameEn': 'General 10:00–18:00', 'start': '10:00', 'end': '18:00', 'graceInMin': 15, 'graceOutMin': 0,
                 'earlyInWindowMin': 60, 'lateOutWindowMin': 120, 'breakMinutes': 0, 'halfDayThresholdMin': 240,
                 'absentThresholdMin': 120, 'pairingMode': 'alternating', 'roundingMin': 0, 'roundingDirection': 'nearest'}
DEFAULT_HOLIDAY_YEAR = 2083


# ---------------------------------------------------------------- Settings -> Company & rules

HOLIDAY_WORK = [('substitute_leave', 'Substitute leave (Sec 42)'), ('pay', '1.5 times pay'), ('employee_choice', "Employee's choice")]

# GenericSettings field lists (SettingsPage.tsx), per tab: (setting key, title, description, fields).
GENERIC = {
    'leave': ('leave.policy', 'Leave', 'Defaults that apply to every leave type unless the type overrides them.', [
        {'key': 'leaveYear', 'label': 'Leave year', 'type': 'select', 'hint': 'When balances reset and carry forward.',
         'options': [('fiscal', 'Fiscal year'), ('anniversary', 'Joining anniversary')]},
        {'key': 'sandwichRule', 'label': 'Sandwich rule by default', 'type': 'boolean', 'hint': 'Count weekly offs and holidays between leave days.'},
        {'key': 'probationRestriction', 'label': 'No paid company leave during probation', 'type': 'boolean', 'hint': 'Statutory leave is still allowed.'},
        {'key': 'maxContinuousDays', 'label': 'Max continuous days', 'type': 'number', 'hint': 'Longest single leave request.', 'suffix': 'days'},
    ]),
    'payroll': ('payroll.policy', 'Payroll', 'How salaries are prorated, rounded and approved.', [
        {'key': 'prorationBasis', 'label': 'Proration basis', 'type': 'select', 'hint': 'Default: calendar days of the Nepali month (29 to 32).',
         'options': [('bs_calendar_days', 'Calendar days'), ('fixed_30', 'Fixed 30 days'), ('26_day', '26 working days')]},
        {'key': 'hourlyDivisor', 'label': 'Hourly divisor for overtime', 'type': 'number', 'hint': 'Default 208 (26 × 8); 240 is also common.'},
        {'key': 'payDay', 'label': 'Pay day', 'type': 'number', 'hint': 'Day of the Nepali month salaries are paid.'},
        {'key': 'rounding', 'label': 'Rounding', 'type': 'select', 'options': [('half_up_rupee', 'Nearest rupee'), ('paisa', 'Exact to the paisa')]},
        {'key': 'separateApprover', 'label': 'A different HR user approves', 'type': 'boolean', 'hint': 'The person who prepares a pay run cannot also approve it.'},
        {'key': 'publishOnLock', 'label': 'Publish payslips when a run is locked', 'type': 'boolean', 'hint': 'Otherwise HR publishes them from the pay run when ready.'},
        {'key': 'encashmentDivisor', 'label': 'Leave encashment divisor', 'type': 'select', 'options': [('bs_days', 'Days in the month'), ('fixed_30', 'Fixed 30 days')]},
    ]),
    'notify': ('notifications.policy', 'Notifications', 'When reminders and digests go out.', [
        {'key': 'quietHoursStart', 'label': 'Quiet hours start', 'type': 'text', 'hint': 'HH:MM. No push notifications during quiet hours.'},
        {'key': 'quietHoursEnd', 'label': 'Quiet hours end', 'type': 'text', 'hint': 'HH:MM'},
        {'key': 'slaHours', 'label': 'Remind approvers after', 'type': 'number', 'hint': 'Pending requests trigger a reminder after this long.', 'suffix': 'h'},
        {'key': 'escalationHours', 'label': 'Escalate to HR after', 'type': 'number', 'suffix': 'h'},
        {'key': 'digestTime', 'label': 'Manager digest time', 'type': 'text', 'hint': 'HH:MM. Daily summary sent to managers.'},
    ]),
    'company': ('company.profile', 'Company', 'Shown on payslips and statutory reports.', [
        {'key': 'nameEn', 'label': 'Company name', 'type': 'text'},
        {'key': 'address', 'label': 'Address', 'type': 'text'},
        {'key': 'pan', 'label': 'Company PAN', 'type': 'text', 'hint': 'Permanent account number from the Inland Revenue Department.'},
        {'key': 'ssfEmployerId', 'label': 'SSF employer ID', 'type': 'text', 'hint': 'Social Security Fund registration number.'},
    ]),
}

SETTINGS_TABS = [('attendance', 'Attendance'), ('leave', 'Leave'), ('payroll', 'Payroll'), ('time', 'Time tracking'),
                 ('notify', 'Notifications'), ('company', 'Company'), ('org', 'Branches and units'), ('security', 'Security')]

# Settings -> Security: roles that must use two-step sign-in (SecurityPolicySettings.tsx).
SECURITY_ROLES = [
    ('admin', 'Administrators', 'Can add users and change roles.'),
    ('hr', 'HR', 'See salaries, bank details and every record.'),
    ('manager', 'Managers', 'Approve leave and attendance for their team.'),
    ('employee', 'Everyone else', 'Their own attendance, leave and payslips.'),
]


def js_number(raw: str | None) -> float | int:
    """Mantine NumberInput onChange -> Number(v): an empty box is 0; whole numbers stay ints."""
    s = (raw or '').strip()
    if not s:
        return 0
    try:
        v = float(s)
    except ValueError:
        return float('nan')
    return int(v) if v.is_integer() else v


def generic_body(fields: list[dict], stored: dict, post) -> dict:
    """GenericSettings: the stored value with the edited fields, leaving out '' and missing values."""
    p = dict(stored or {})
    for f in fields:
        k = f['key']
        # A value the stored setting does not have yet is only sent once its control is changed (the TS state `p`):
        # an untouched empty box or an unticked switch is left out, so the server applies its default.
        if f['type'] == 'boolean':
            on = post.get(k) in ('on', 'true', '1')
            if on or k in p:
                p[k] = on
        elif f['type'] == 'number':
            raw = (post.get(k) or '').strip()
            if raw or k in p:
                p[k] = js_number(raw)
        else:
            p[k] = post.get(k, '')
    return {k: v for k, v in p.items() if v != ''}


ATTENDANCE_NUMBERS = ('otMinThresholdMin', 'otRoundingMin', 'nightPremiumRate', 'autoClockOutAfterMin',
                      'regularisationBackdateDays', 'regularisationMonthlyCap', 'wfhPhotoRetentionDays')


def attendance_body(stored: dict, post) -> dict:
    """AttendanceRules state `p`: the stored policy with every control's value."""
    p = dict(stored or {})
    for k in ATTENDANCE_NUMBERS:
        # Every box is posted by the page; a field left out keeps its stored value. An empty box for a value the stored
        # policy does not have is left out too, so the server applies its default, as the TS state did.
        if k in post and ((post.get(k) or '').strip() or k in p):
            p[k] = js_number(post.get(k))
    # Switches: a value the stored policy lacks is sent only when it differs from what the page showed (off, except
    # the photo switch, which shows on unless stored as false).
    for k, shown in (('requirePreapproval', False), ('nightWindowEnabled', False), ('wfhPhotoRequired', True)):
        on = post.get(k) in ('on', 'true', '1')
        if k in p or on != shown:
            p[k] = on
    hw = post.get('holidayWork')
    if hw and ('holidayWork' in p or hw != 'substitute_leave'):  # the select shows substitute_leave when unset
        p['holidayWork'] = hw
    pen = dict(p.get('latePenalty') or {'enabled': False, 'lateCount': 3, 'deductionDays': 0.5})
    pen['enabled'] = post.get('latePenalty.enabled') in ('on', 'true', '1')
    if pen['enabled']:  # disabled inputs are not posted: keep the stored numbers
        pen['lateCount'] = js_number(post.get('latePenalty.lateCount'))
        pen['deductionDays'] = js_number(post.get('latePenalty.deductionDays'))
    p['latePenalty'] = pen
    return p


def affects_me(user: AuthUser, roles: list[str]) -> bool:
    return not user.two_factor_enabled and any(r in roles for r in user.roles)


def is_admin(user: AuthUser) -> bool:
    return has_role(user, 'admin')
