"""
Attendance hub pages for daily attendance and the attendance tools:
- /attendance (Who's in, TeamTodayPage.tsx), /attendance/exceptions (ExceptionsPage.tsx)
- /attendance/issues (AttendanceIssuesPage.tsx), /attendance/penalties (LatePenaltiesPage.tsx),
  /attendance/upload (AttendanceUploadPage.tsx)
- /me/attendance (MyAttendancePage.tsx) with the day modal and the late-penalty note
- fragments other slices embed: who's out (Home, HomeAttendanceExtras WhosOutCard), the late clock-in modal
  (LateClockInModal) and the payroll pre-flight check (PayrollAttendanceCheck).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from engines.bs_calendar import ad_to_bs, bs_month_bounds

from hajiri.attendance import api as att_api
from hajiri.attendance.service import list_days
from hajiri.core.auth import company_id_of
from hajiri.core.db import rows
from hajiri.core.errors import BadRequestError, HttpError
from hajiri.web.decorators import login_required, require_module, require_role
from hajiri.web.format import (ad_span_label, bs_month_grid, bs_month_label, fmt_date_long, fmt_date_short, fmt_time,
                               minutes_to_hm, next_month, prev_month, safe)
from hajiri.web.htmx import close_modal, htmx_target, refresh, toast
from hajiri.web.nav import area
from hajiri.web.templatetags.hj import live_status, status_label, tone

from . import services

ROLES = ('manager', *area('attendance'))

FLAG_LABEL = {
    'AutoClockOut': 'Auto clock-out', 'EarlyOut': 'Early out', 'HalfDayLeave': 'Half-day leave', 'HolidayWork': 'Worked on holiday',
    'Late': 'Late', 'MissingOut': 'Missing clock-out', 'OTCapExceeded': 'Over overtime cap',
    'OTPreapprovalMissing': 'Overtime not pre-approved', 'UnpaidLeave': 'Unpaid leave', 'UnpairedPunch': 'Unpaired punch',
}
SOURCE_LABEL = {'web': 'Web', 'mobile': 'Mobile', 'device': 'Terminal', 'manual': 'Added by HR', 'regularisation': 'Correction',
                'kiosk': 'Kiosk', 'field': 'Field visit'}


def source_label(s: str | None) -> str:
    return SOURCE_LABEL.get(s or '', s or '')


def flag_text(f: dict) -> str:
    """ExceptionsPage flagText."""
    return f"{FLAG_LABEL.get(f.get('kind'), f.get('kind'))}{', ' + minutes_to_hm(f['minutes']) if f.get('minutes') else ''}"


def describe_flag(f: dict) -> str:
    """HomePage describeFlag: a flag in words."""
    k = f.get('kind')
    return {
        'Late': f"Late by {f.get('minutes')} min",
        'EarlyOut': f"Left {f.get('minutes')} min early",
        'MissingOut': 'No clock-out yet',
        'UnpairedPunch': 'A clock-in or clock-out could not be paired',
        'OTPreapprovalMissing': f"{f.get('rawMinutes')} min overtime without pre-approval",
        'OTCapExceeded': f"Overtime cap exceeded by {f.get('minutes')} min",
        'HolidayWork': f"Worked on a holiday ({f.get('workedMin')} min)",
        'UnpaidLeave': 'Unpaid leave',
        'HalfDayLeave': 'Half-day leave',
        'AutoClockOut': 'Clocked out automatically; request a correction if this is wrong',
        'ClockSkew': 'Your device clock was off, so server time was used',
        'OutsideLocation': 'Clocked outside your work location (flagged for review)',
        'NoLocation': 'Location was not available',
        'Field': 'Recorded on a field visit',
        'Kiosk': 'Recorded at a kiosk',
    }.get(k, k)


def _today() -> str:
    return services.npt_today()


def _add_days(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


def _iso_or(v: str | None, default: str | None) -> str | None:
    if v:
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            return default
    return default


def _int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _bs_ym(request) -> tuple[int, int]:
    t = ad_to_bs(_today())
    y, m = _int(request.GET.get('y') or request.GET.get('bs_year'), t.year), _int(request.GET.get('m') or request.GET.get('bs_month'), t.month)
    if not 1 <= m <= 12:
        m = t.month
    try:
        bs_month_bounds(y, m)
    except Exception:  # noqa: BLE001 - outside the calendar table
        y, m = t.year, t.month
    return y, m


def _day_count(n) -> str:
    return f"{n} {'day' if n == 1 else 'days'}"


def _partial_or_page(request, target: str, partial: str, page: str, ctx: dict) -> HttpResponse:
    if htmx_target(request) == target:
        return render(request, partial, ctx)
    return render(request, page, ctx)


# ================================================================ Who's in (/attendance)

def _not_yet_in(r: dict) -> bool:
    return not r['first_in'] and r['status'] not in ('Holiday', 'WeeklyOff', 'OnLeave')


TODAY_FILTERS = ('all', 'in', 'late', 'absent', 'notyet', 'remote')


@require_GET
@require_role(*ROLES)
def today(request):
    user = request.hajiri_user
    t = _today()
    d = _iso_or(request.GET.get('date'), t)
    flt = request.GET.get('filter') if request.GET.get('filter') in TODAY_FILTERS else 'all'
    data = att_api.today(user, {'date': d})['rows']
    is_today = d == t
    now = datetime.now().astimezone()
    for r in data:
        r['employee_id'] = str(r['employee_id'])
        r['photo_id'] = str(r['photo_id']) if r['photo_id'] else None
        st = live_status(r['status'], on_date=d, today=t, last_out=r['last_out'], first_in=r['first_in'])
        r['live_status'] = st or ('Present' if r['first_in'] else 'NotComputed')
        if r['first_in'] and not r['last_out'] and is_today:
            r['so_far'] = minutes_to_hm(max(0, round((now - r['first_in']).total_seconds() / 60)))
        else:
            r['so_far'] = None

    def match(r):
        if flt == 'in':
            return bool(r['first_in'])
        if flt == 'late':
            return (r['late_min'] or 0) > 0
        if flt == 'absent':
            return r['status'] == 'Absent' or (not r['status'] and not r['first_in'])
        if flt == 'notyet':
            return _not_yet_in(r)
        if flt == 'remote':
            return bool(r['remote'])
        return True
    shown = [r for r in data if match(r)]
    counts = {
        'in': sum(1 for r in data if r['first_in']), 'late': sum(1 for r in data if (r['late_min'] or 0) > 0),
        'absent': sum(1 for r in data if r['status'] == 'Absent'), 'notyet': sum(1 for r in data if _not_yet_in(r)),
        'leave': sum(1 for r in data if r['status'] in ('OnLeave', 'HalfDayLeave')), 'remote': sum(1 for r in data if r['remote']),
    }
    notyet_label = 'Not yet in' if is_today else 'No punch'
    stats = [
        {'label': 'In', 'value': counts['in'], 'tone': 'ok', **({'hint': f'of {len(data)} on the team'} if data else {})},
        {'label': 'Late', 'value': counts['late'], 'tone': 'warn'},
        {'label': 'Absent', 'value': counts['absent'], 'tone': 'bad'},
        {'label': notyet_label, 'value': counts['notyet']},
        {'label': 'From home', 'value': counts['remote']},
        {'label': 'On leave', 'value': counts['leave']},
    ]
    filters = [('all', 'Everyone'), ('in', 'In'), ('late', 'Late'), ('absent', 'Absent'), ('notyet', notyet_label), ('remote', 'From home')]
    ctx = {'date': d, 'is_today': is_today, 'today': t, 'filter': flt, 'filters': filters, 'rows': shown, 'stats': stats,
           'count_label': f"{len(shown)} {'person' if len(shown) == 1 else 'people'}",
           'subtitle': f"{fmt_date_long(d)}{'. Statuses update as punches arrive.' if is_today else ''}",
           'empty_title': 'No one on your team yet' if flt == 'all' else 'No one matches this filter',
           'empty_hint': 'Employees who report to you will appear here.' if flt == 'all' else 'Switch to Everyone to see the whole team.'}
    return _partial_or_page(request, 'team-today', 'attendance_tools/partials/today.html', 'attendance_tools/today.html', ctx)


# ================================================================ Exceptions

EXC_FILTERS = ('all', 'missing', 'absent', 'late', 'ot')


def _exc_match(r: dict, f: str) -> bool:
    if f == 'missing':
        return r['status'] == 'Incomplete'
    if f == 'absent':
        return r['status'] == 'Absent'
    if f == 'ot':
        return any(str(x.get('kind', '')).startswith('OT') for x in r['flags'] or [])
    if f == 'late':
        return (r['late_min'] or 0) > 0
    return True


@require_GET
@require_module('attendance')
def exceptions(request):
    user = request.hajiri_user
    t = _today()
    from_ = _iso_or(request.GET.get('from'), _add_days(t, -7))
    to = _iso_or(request.GET.get('to'), t)
    flt = request.GET.get('filter') if request.GET.get('filter') in EXC_FILTERS else 'all'
    raw = att_api.exceptions(user, {'from': from_, 'to': to}) if from_ <= to else []
    # Today's open days are people still at work, not missing punches; leave them out until the day is over.
    all_rows = [r for r in raw if not (str(r['local_date']) == t and r['status'] == 'Incomplete')]
    still_in = len(raw) - len(all_rows)
    for r in all_rows:
        r['id'] = str(r['id'])
        r['employee_id'] = str(r['employee_id'])
        r['local_date'] = str(r['local_date'])
        r['flag_texts'] = [flag_text(f) for f in r['flags'] or []]
    shown = [r for r in all_rows if _exc_match(r, flt)]
    count = {f: sum(1 for r in all_rows if _exc_match(r, f)) for f in EXC_FILTERS}
    filters = [('all', f"All {count['all']}"), ('missing', f"Missing clock-out {count['missing']}"), ('absent', f"Absent {count['absent']}"),
               ('late', f"Late {count['late']}"), ('ot', f"Overtime flags {count['ot']}")]
    ctx = {'from': from_, 'to': to, 'filter': flt, 'filters': filters, 'rows': shown,
           'subtitle': 'Days with a missing punch, an absence or an overtime flag that payroll has not locked yet. Fix the punch, then '
                       f"recalculate the day.{f' {still_in} people still at work today are not listed.' if still_in > 0 else ''}",
           'empty_title': 'No exceptions in this range' if flt == 'all' else 'Nothing matches this filter',
           'empty_hint': 'Every day in this range calculated cleanly.' if flt == 'all' else 'Switch to All to see every exception in the range.'}
    return _partial_or_page(request, 'exceptions', 'attendance_tools/partials/exceptions.html', 'attendance_tools/exceptions.html', ctx)


@require_POST
@require_role(*ROLES)
def recompute(request):
    """Recalculate selected days (each value 'employee_id|YYYY-MM-DD')."""
    picked = [v.split('|', 1) for v in request.POST.getlist('exceptions-selected') if '|' in v]
    for emp, d in picked:
        att_api.recompute(request.hajiri_user, {'employeeId': emp, 'localDate': d})
    resp = refresh(HttpResponse(status=204), 'exceptions')
    return toast(request, resp, f"{len(picked)} {'day' if len(picked) == 1 else 'days'} recalculated.")


# ================================================================ Issues

@require_GET
@require_role(*ROLES)
def issues(request):
    user = request.hajiri_user
    mode = 'recent' if request.GET.get('mode') == 'recent' else 'month'
    y, m = _bs_ym(request)
    if mode == 'month':
        b = bs_month_bounds(y, m)
        from_, to = b['startAd'], b['endAd']
    else:
        to = _today()
        from_ = _add_days(to, -13)
    data = services.issues(user, {'from': from_, 'to': to})
    s = data['summary']
    for r in data['rows']:
        r['missing_list'] = ', '.join(fmt_date_short(x) for x in r['missing'])
        r['absent_list'] = ', '.join(fmt_date_short(x) for x in r['absent'])
        r['missing_label'] = _day_count(len(r['missing']))
        r['absent_label'] = _day_count(len(r['absent']))
        for p in r['pending']:
            span = fmt_date_short(p['from']) if p['from'] == p['to'] else f"{fmt_date_short(p['from'])} to {fmt_date_short(p['to'])}"
            p['text'] = f"{span}, waiting {_day_count(p['ageDays'])} on {', '.join(w['name'] for w in p['waitingOn']) or 'nobody'}"
    pending = s['pendingLeave'] + s['pendingTime']
    stats = [
        {'label': 'People with issues', 'value': s['employees']},
        {'label': 'Missing punches', 'value': s['missing'], **({'tone': 'warn'} if s['missing'] else {})},
        {'label': 'Unexplained absences', 'value': s['absent'], **({'tone': 'bad'} if s['absent'] else {})},
        {'label': 'Requests waiting', 'value': pending, **({'hint': f"oldest {s['oldestPendingDays']} days"} if s['oldestPendingDays'] else {})},
    ]
    title = f"Attendance issues{', ' + bs_month_label(y, m) if mode == 'month' else ''}"
    ctx = {'mode': mode, 'y': y, 'm': m, 'from': from_, 'to': to, 'rows': data['rows'], 'stats': stats, 'title': title}
    return _partial_or_page(request, 'attendance-issues', 'attendance_tools/partials/issues.html', 'attendance_tools/issues.html', ctx)


@require_POST
@require_role(*ROLES)
def nudge(request):
    p = request.POST
    body = ({'target': 'approver', 'instanceId': p.get('instance_id', '')} if p.get('target') == 'approver'
            else {'target': 'employee', 'employeeId': p.get('employee_id', ''), 'from': p.get('from', ''), 'to': p.get('to', '')})
    try:
        r = services.nudge(request.hajiri_user, body)
    except HttpError as e:
        resp = HttpResponse(status=e.status)
        return toast(request, resp, e.message, tone='err', title='Reminder not sent')
    resp = HttpResponse(status=204)
    if r['already']:
        return toast(request, resp, 'Already reminded today.', tone='info')
    return toast(request, resp, 'Reminder sent.' if r['sent'] == 1 else f"{r['sent']} reminders sent.")


# ================================================================ Late penalties and attendance rules

def _paid_types(company_id: str) -> list[dict]:
    return [dict(t, id=str(t['id'])) for t in rows(
        """SELECT id, code, name_en FROM leave_type WHERE company_id = %s AND paid AND code NOT IN ('MATERNITY', 'PATERNITY', 'MOURNING')
            ORDER BY sort_order, code""", [company_id])]


def _penalty_ctx(request, y: int, m: int) -> dict:
    user = request.hajiri_user
    data = services.penalties(user, {'bsYear': y, 'bsMonth': m})
    lines = data['lines']
    enabled = bool(data['rule']['enabled'])
    locked = data['locked']
    due = [ln for ln in lines if ln['penaltyDays'] > 0 and not ln['applied']]
    for ln in lines:
        a = ln['applied']
        ln['late_list'] = ', '.join(fmt_date_short(x) for x in ln['lateDates'])
        ln['penalty_label'] = _day_count(ln['penaltyDays']) if ln['penaltyDays'] else None
        leave = a['leaveDays'] if a else sum(x['days'] for x in ln['allocations'])
        unpaid = a['unpaidDays'] if a else ln['unpaidDays']
        ln['show_split'] = bool(ln['penaltyDays'] or a)
        ln['leave_label'] = (f"{_day_count(services._num(leave))} leave{'' if a else ' (' + ', '.join(x['code'] for x in ln['allocations']) + ')'}"
                             if leave else None)
        ln['unpaid_label'] = f'{_day_count(services._num(unpaid))} unpaid' if unpaid else None
        ln['due'] = ln in due
    unpaid_total = services._num(sum((ln['applied']['unpaidDays'] if ln['applied'] else ln['unpaidDays']) for ln in lines))
    stats = [
        {'label': 'People late', 'value': len(lines)},
        {'label': 'Penalties to apply', 'value': len(due) if enabled else 0, **({'tone': 'warn'} if due and enabled else {})},
        {'label': 'Applied', 'value': sum(1 for ln in lines if ln['applied'])},
        {'label': 'Unpaid days', 'value': unpaid_total},
    ]
    return {'y': y, 'm': m, 'month_label': bs_month_label(y, m), 'lines': lines, 'enabled': enabled, 'locked': locked, 'due': due,
            'stats': stats, 'selectable': enabled and not locked}


def _policy_ctx(request, policy: dict | None = None, error: str | None = None) -> dict:
    company_id = company_id_of(request.hajiri_user)
    return {'policy': policy or services.extras_policy(company_id), 'types': _paid_types(company_id), 'policy_error': error,
            'scopes': [('team', "Each person's team"), ('department', "Each person's department"), ('company', 'The whole company')]}


@require_GET
@require_module('attendance')
def penalties(request):
    y, m = _bs_ym(request)
    ctx = _penalty_ctx(request, y, m)
    if htmx_target(request) == 'late-penalties':
        return render(request, 'attendance_tools/partials/penalties.html', ctx)
    return render(request, 'attendance_tools/penalties.html', {**ctx, **_policy_ctx(request)})


def _num_or(v, default):
    try:
        f = float(v)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return default


@require_POST
@require_module('attendance')
def save_policy(request):
    p = request.POST
    body = {
        'blockLateClockIn': p.get('blockLateClockIn') == 'on', 'blockAfterMin': _num_or(p.get('blockAfterMin'), 0),
        'whosOutScope': p.get('whosOutScope') or 'team',
        'latePenalty': {'enabled': p.get('lp_enabled') == 'on', 'lateCount': _num_or(p.get('lp_lateCount'), 1),
                        'penaltyDays': _num_or(p.get('lp_penaltyDays'), 0.25), 'minLateMinutes': _num_or(p.get('lp_minLateMinutes'), 1),
                        'leaveTypeIds': [x for x in p.getlist('lp_leaveTypeIds') if x][:5]},
    }
    services.put_policy(request.hajiri_user, body)
    resp = refresh(HttpResponse(status=204), 'late-penalties')
    return toast(request, resp, 'Attendance rules saved.')


@require_POST
@require_module('attendance')
def apply_penalties(request):
    y, m = _int(request.POST.get('bsYear'), 0), _int(request.POST.get('bsMonth'), 0)
    ids = [x for x in request.POST.getlist('late-penalties-selected') if x]
    r = services.apply(request.hajiri_user, {'bsYear': y, 'bsMonth': m, **({'employeeIds': ids} if ids else {})})
    msg = (f"{r['applied']} penalt{'y' if r['applied'] == 1 else 'ies'} applied{', ' + str(r['skipped']) + ' skipped' if r['skipped'] else ''}. "
           'The employees have been told.')
    return toast(request, refresh(HttpResponse(status=204), 'late-penalties'), msg)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
def reverse_penalty(request, id):
    name = request.GET.get('name') or request.POST.get('name') or ''
    if request.method == 'GET':
        return render(request, 'attendance_tools/partials/reverse_form.html', {'id': id, 'name': name})
    reason = (request.POST.get('reason') or '').strip()
    services.reverse(request.hajiri_user, str(id), {'reason': reason})
    resp = refresh(close_modal(HttpResponse(status=204)), 'late-penalties')
    return toast(request, resp, 'Penalty reversed.')


# ================================================================ Upload

UPLOAD_MAX = 900_000


def _history(request) -> list[dict]:
    return services.uploads(request.hajiri_user)['uploads']


@require_GET
@require_module('attendance')
def upload(request):
    if htmx_target(request) == 'upload-history':
        return render(request, 'attendance_tools/partials/upload_history.html', {'uploads': _history(request)})
    return render(request, 'attendance_tools/upload.html', {'uploads': _history(request)})


@require_GET
@require_module('attendance')
def upload_template(request):
    from .api import template_response
    return template_response()


@require_POST
@require_module('attendance')
def upload_check(request):
    f = request.FILES.get('file')
    if not f:
        raise BadRequestError('Choose a CSV file.')
    if f.size > UPLOAD_MAX:
        resp = HttpResponse(status=400)
        return toast(request, resp, 'The file is too large. Upload at most 2,000 rows at a time.', tone='err')
    try:
        csv = f.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        return toast(request, HttpResponse(status=400), 'Save the file as CSV (UTF-8) and choose it again.', tone='err',
                     title='Could not read the file')
    try:
        check = services.upload_check(request.hajiri_user, {'csv': csv})
    except HttpError as e:
        return toast(request, HttpResponse(status=e.status), e.message, tone='err', title='Could not read the file')
    for r in check['rows']:
        r['date_label'] = fmt_date_short(r['date']) if r['date'] else None
    stats = [{'label': 'Rows', 'value': len(check['rows'])}, {'label': 'Ready', 'value': check['valid']},
             {'label': 'With errors', 'value': check['invalid'], **({'tone': 'bad'} if check['invalid'] else {})}]
    return render(request, 'attendance_tools/partials/upload_check.html', {**check, 'csv': csv, 'filename': f.name, 'stats': stats})


@require_POST
@require_module('attendance')
def upload_import(request):
    p = request.POST
    try:
        r = services.upload_import(request.hajiri_user, {'csv': p.get('csv', ''), 'reason': (p.get('reason') or '').strip(),
                                                         **({'filename': p['filename']} if p.get('filename') else {})})
    except HttpError as e:
        return toast(request, HttpResponse(status=e.status), e.message, tone='err', title='Not uploaded')
    msg = f"{r['punches']} punch(es) added for {r['days']} day(s){'; ' + str(r['duplicates']) + ' were already there' if r['duplicates'] else ''}."
    resp = render(request, 'attendance_tools/partials/upload_check_empty.html', {})
    refresh(resp, 'upload-history')
    return toast(request, resp, msg, title='Attendance uploaded')


# ================================================================ My attendance (/me/attendance)

@require_GET
@login_required
def my_attendance(request):
    user = request.hajiri_user
    t = _today()
    y, m = _bs_ym(request)
    b = bs_month_bounds(y, m)
    employee_id = user.employee_id
    days = list_days(employee_id, b['startAd'], b['endAd']) if employee_id else []
    by_date = {str(d['local_date']): d for d in days}
    holidays = _holidays(user, b['startAd'], b['endAd'])

    def status_of(d):
        return live_status(d['status'], on_date=str(d['local_date']), today=t, last_out=d['last_out'], first_in=d['first_in']) or d['status']
    statuses = [status_of(d) for d in days]

    def count(*s):
        return sum(1 for x in statuses if x in s)
    late_days = sum(1 for d in days if (d['late_min'] or 0) > 0)
    ot = sum(d['ot_min'] or 0 for d in days)
    missing, absent = count('Incomplete'), count('Absent')
    stats = [
        {'label': 'Present days', 'value': count('Present', 'WFH', 'HalfDay', 'Working')},
        {'label': 'Late days', 'value': late_days, **({'tone': 'warn'} if late_days > 0 else {})},
        {'label': 'On leave', 'value': count('OnLeave', 'HalfDayLeave')},
        {'label': 'Absent', 'value': absent, **({'tone': 'bad'} if absent > 0 else {})},
        {'label': 'Missing clock-out', 'value': missing, **({'tone': 'warn'} if missing > 0 else {})},
        {'label': 'Overtime', 'value': minutes_to_hm(ot)},
    ]
    cells = []
    grid = bs_month_grid(y, m)
    for i, c in enumerate(grid):
        d = by_date.get(c.ad)
        st = status_of(d) if d else None
        ad_day = int(c.ad[8:10])
        cells.append({'ad': c.ad, 'bs_day': c.bs.day, 'in_month': c.in_month, 'saturday': c.saturday, 'today': c.ad == t,
                      'holiday': holidays.get(c.ad), 'status': st, 'status_label': status_label(st) if st else None,
                      'tone': tone(st) if st else None, 'late_min': d['late_min'] if d else 0,
                      'times': (fmt_time(d['first_in']) + ('–' + fmt_time(d['last_out']) if d['last_out'] else '')) if d and d['first_in'] else None,
                      'ad_label': (['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][int(c.ad[5:7]) - 1] + f' {ad_day}')
                      if ad_day == 1 or i == 0 else str(ad_day)})
    tb = safe(t)
    ctx = {'y': y, 'm': m, 'month_label': bs_month_label(y, m), 'prev': prev_month(y, m), 'next': next_month(y, m),
           'this_month': None if tb and (tb.year, tb.month) == (y, m) else ({'year': tb.year, 'month': tb.month} if tb else None),
           'subtitle': f"{ad_span_label(b['startAd'], b['endAd'])} in the Gregorian calendar. Select a day to see its details or request a correction.",
           'stats': stats, 'cells': cells, 'late_note': _late_note(user, y, m), 'correct_date': t if request.GET.get('correct') else None}
    return render(request, 'attendance_tools/my_attendance.html', ctx)


def _holidays(user, from_: str, to: str) -> dict[str, str]:
    """The days' holidays for the signed-in employee (shifts calendar), names joined with ', '."""
    if not user.employee_id:
        return {}
    try:
        from hajiri.shifts.services import calendar
        cal = calendar(user, {'from': from_, 'to': to, 'employeeId': user.employee_id})
    except HttpError:
        return {}
    return {d['date']: ', '.join(h['name_en'] for h in d['holidays']) for d in cal['days'] if d['holidays']}


def _late_note(user, y: int, m: int) -> dict | None:
    """LatePenaltyNote: shown when the rule is on and the month has late days or an applied penalty."""
    if not user.employee_id:
        return None
    d = services.my_penalties(user, {'bsYear': y, 'bsMonth': m})
    if not d['enabled'] or (not d['lateDays'] and not d['applied']):
        return None
    r = d['rule']
    left = r['lateCount'] - (d['lateDays'] % r['lateCount'])
    a = d['applied']
    if a:
        tail = f"A penalty of {_day_count(a['penaltyDays'])} was applied: {a['leaveDays']} from leave and {a['unpaidDays']} unpaid."
    elif d['projectedDays']:
        tail = f"So far this is {_day_count(d['projectedDays'])}; HR applies it before payroll."
    else:
        tail = f"{left} more late {'day' if left == 1 else 'days'} this month would count as a penalty."
    return {'tone': 'warn' if a or d['projectedDays'] else 'info',
            'title': f"{d['lateDays']} late {'day' if d['lateDays'] == 1 else 'days'} in {bs_month_label(y, m)}",
            'text': f"Every {r['lateCount']} late days in a month count as {_day_count(r['penaltyDays'])} of leave, or unpaid days if no "
                    f'leave is left. {tail}'}


@require_GET
@login_required
def my_day(request):
    """The day modal of My attendance: status, times, flags, punches and the correction button."""
    user = request.hajiri_user
    d = _iso_or(request.GET.get('date'), None)
    if not d:
        raise BadRequestError('date: YYYY-MM-DD')
    t = _today()
    day = None
    punches = []
    if user.employee_id:
        found = list_days(user.employee_id, d, d)
        day = found[0] if found else None
        punches = att_api.punches(user, {'employeeId': user.employee_id, 'from': d, 'to': d})
    hol = _holidays(user, d, d).get(d)
    status = (live_status(day['status'], on_date=d, today=t, last_out=day['last_out'], first_in=day['first_in']) or day['status']) if day \
        else ('Holiday' if hol else None)
    for p in punches:
        p['time'] = fmt_time(p['punched_at'])
        p['dir_label'] = 'Clock-in' if p['direction'] == 'in' else 'Clock-out' if p['direction'] == 'out' else 'Unpaired'
        meta = ', '.join(x for x in [source_label(p['source']), p['verify_mode'], p['device_sn']] if x)
        p['meta'] = meta + (' (replaced)' if p['superseded'] else '')
    ctx = {'date': d, 'title': fmt_date_long(d), 'day': day, 'status': status, 'holiday': hol, 'punches': punches,
           'flags': [describe_flag(f) for f in (day['flags'] if day else []) or []],
           'correct': request.GET.get('correct') == '1' and bool(user.employee_id), 'employee_id': user.employee_id}
    return render(request, 'attendance_tools/partials/my_day.html', ctx)


# ================================================================ fragments for other slices

@require_GET
@login_required
def whos_out(request):
    """Home: who is away today and over the next week (WhosOutCard). Empty when nobody is."""
    data = services.whos_out_for(request.hajiri_user, {'days': 7})
    t = _today()
    days = data['days']
    today_people = next((d['people'] for d in days if d['date'] == t), [])
    later = [d for d in days if d['date'] != t and d['people']]
    if not today_people and not later:
        return HttpResponse('')
    for p in today_people:
        p['label'] = services.OUT_LABEL[p['kind']]
        p['tone'] = services.OUT_TONE[p['kind']]
        p['tip'] = f"{p['label']}{': ' + p['detail'] if p['detail'] else ''}"
    lines = [{'date': fmt_date_short(d['date']),
              'text': ', '.join(f"{p['name']} ({services.OUT_LABEL[p['kind']].lower()})" for p in d['people'])} for d in later]
    scope = {'company': 'the company', 'department': 'your department'}.get(data['scope'], 'your team')
    return render(request, 'attendance_tools/partials/whos_out.html', {'today_people': today_people, 'later': lines, 'scope': scope})


@require_GET
@login_required
def late_note(request):
    """My attendance's late-penalty note for a BS month (LatePenaltyNote), for other pages to embed."""
    y, m = _bs_ym(request)
    return render(request, 'attendance_tools/partials/late_note.html', {'late_note': _late_note(request.hajiri_user, y, m)})


@require_GET
@login_required
def late_blocked(request):
    """LateClockInModal: why a late clock-in was refused, with the time request one tap away."""
    return render(request, 'attendance_tools/partials/late_blocked.html', {'message': request.GET.get('message', '')[:400]})


@require_GET
@require_module('attendance')
def payroll_check(request):
    """PayrollAttendanceCheck: the month's open attendance issues in numbers, for the payroll pre-flight."""
    y, m = _int(request.GET.get('bsYear') or request.GET.get('y'), 0), _int(request.GET.get('bsMonth') or request.GET.get('m'), 0)
    s = services.issue_summary(request.hajiri_user, {'bsYear': y, 'bsMonth': m})
    pending = s['pendingLeave'] + s['pendingTime']
    clean = not s['missing'] and not s['absent'] and not pending
    text = ('No open attendance issues this month.' if clean else
            f"{s['employees']} people have open attendance issues: {s['missing']} missing punch(es), {s['absent']} unexplained absence(s), "
            f'{pending} request(s) waiting.')
    return render(request, 'attendance_tools/partials/payroll_check.html', {'clean': clean, 'text': text})
