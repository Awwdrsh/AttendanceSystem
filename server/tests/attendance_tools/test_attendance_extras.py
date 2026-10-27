"""Port of apps/api/test/attendance-extras.test.ts (through the JSON API, as the TS test). Each TS describe block is one
test with its steps in order; the shared beforeAll is the `x` fixture."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from engines.attendance import local_time_ms
from engines.bs_calendar import bs_month_bounds

from hajiri.attendance.service import recompute_day
from hajiri.attendance_tools.services import allocate_penalty, assert_clock_in_allowed, penalty_for, penalty_unpaid_days
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ConflictError
from hajiri.leave.services import ledger_balance, leave_type_by_code
from tests.punch.api import get, patch, post, put

OTHER_EMP = '00000000-0000-7000-8000-0000000000e3'


@pytest.fixture
def x(demo, login_as):
    hr = login_as(['hr']).token
    s = login_as(['employee'], demo['sita'])
    m = login_as(['manager'], demo['ram'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute("INSERT INTO employee (id, company_id, emp_code, name_en, gender) VALUES (%s, %s, 'E003', 'Hari Karki', 'male')", [OTHER_EMP, demo['company_id']])
    execute("INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES (%s, daterange('2026-01-01', NULL), %s, 'regular')",
            [OTHER_EMP, demo['branch_id']])
    outsider = login_as(['employee'], OTHER_EMP).token
    t = {code: str(leave_type_by_code(demo['company_id'], code)['id']) for code in ('HOME', 'SICK', 'CASUAL', 'LWP')}

    def adjust(employee_id, code, days):
        return post(hr, '/leave/adjust', {'employeeId': employee_id, 'leaveTypeId': t[code], 'deltaDays': days, 'reason': 'opening balance',
                                          'kind': 'allocation', 'effectiveDate': '2026-07-20'})

    def approve(request_id, token=None):
        inst = one('SELECT workflow_instance_id FROM leave_request WHERE id = %s', [request_id])['workflow_instance_id']
        r = post(token or m.token, f'/workflow/{inst}/approve', {})
        assert r.status == 200, r.body
    return {'hr': hr, 'sita': s.token, 'sita_user': s.user_id, 'ram': m.token, 'ram_user': m.user_id, 'outsider': outsider, 't': t,
            'adjust': adjust, 'approve': approve}


def test_ordered_and_clubbed_leave_types(demo, x):
    hr, sita, t, adjust, approve = x['hr'], x['sita'], x['t'], x['adjust'], x['approve']
    # without the sandwich rule, the Saturday between two leaves is not charged
    adjust(demo['sita'], 'HOME', 10)
    adjust(demo['sita'], 'SICK', 10)
    a = post(sita, '/leave/requests', {'leaveTypeId': t['SICK'], 'from': '2026-08-28', 'to': '2026-08-28', 'reason': 'fever'})
    assert a.status == 201
    p = get(sita, '/leave/preview', {'leaveTypeId': t['HOME'], 'from': '2026-08-30', 'to': '2026-08-30'})
    assert p.body['bridge'] is None
    post(sita, f"/leave/requests/{a.body['id']}/cancel", {})

    # the type first in the order absorbs the gap when its sandwich rule is on, and it is posted on approval
    patch(hr, f"/leave/types/{t['HOME']}", {'sandwichRule': True})
    a = post(sita, '/leave/requests', {'leaveTypeId': t['SICK'], 'from': '2026-08-28', 'to': '2026-08-28', 'reason': 'fever'})
    assert a.status == 201
    approve(a.body['id'])
    b = post(sita, '/leave/requests', {'leaveTypeId': t['HOME'], 'from': '2026-08-30', 'to': '2026-08-30', 'reason': 'trip'})
    assert b.status == 201
    assert {k: b.body['bridge'][k] for k in ('days', 'typeId', 'from', 'to')} == {'days': 1, 'typeId': t['HOME'], 'from': '2026-08-29', 'to': '2026-08-29'}
    bal = get(sita, '/leave/balances')
    assert next(z for z in bal.body['balances'] if z['code'] == 'HOME')['pending'] == 2
    before = ledger_balance(demo['sita'], t['HOME'])
    approve(b.body['id'])
    assert ledger_balance(demo['sita'], t['HOME']) == before - 2
    again = get(sita, '/leave/preview', {'leaveTypeId': t['HOME'], 'from': '2026-08-27', 'to': '2026-08-27'})
    assert again.body['bridge'] is None
    post(sita, f"/leave/requests/{b.body['id']}/cancel", {})
    assert ledger_balance(demo['sita'], t['HOME']) == before

    # when the earlier type has the sandwich rule and comes first, the gap is charged to it
    adjust(demo['sita'], 'CASUAL', 6)
    h = post(sita, '/leave/requests', {'leaveTypeId': t['HOME'], 'from': '2026-09-04', 'to': '2026-09-04', 'reason': 'trip'})
    assert h.status == 201
    p = get(sita, '/leave/preview', {'leaveTypeId': t['CASUAL'], 'from': '2026-09-06', 'to': '2026-09-06'})
    assert p.body['bridge']['days'] == 1 and p.body['bridge']['typeId'] == t['HOME']
    post(sita, f"/leave/requests/{h.body['id']}/cancel", {})

    # a clubbed type follows its main type
    patch(hr, f"/leave/types/{t['HOME']}", {'sandwichRule': False})
    patch(hr, f"/leave/types/{t['CASUAL']}", {'sandwichRule': True})
    s = post(sita, '/leave/requests', {'leaveTypeId': t['SICK'], 'from': '2026-09-11', 'to': '2026-09-11', 'reason': 'cold'})
    assert s.status == 201
    p1 = get(sita, '/leave/preview', {'leaveTypeId': t['CASUAL'], 'from': '2026-09-13', 'to': '2026-09-13'})
    assert p1.body['bridge'] is None
    assert patch(hr, f"/leave/types/{t['CASUAL']}", {'clubWithId': t['HOME']}).status == 200
    patch(hr, f"/leave/types/{t['HOME']}", {'sandwichRule': True})
    p2 = get(sita, '/leave/preview', {'leaveTypeId': t['CASUAL'], 'from': '2026-09-13', 'to': '2026-09-13'})
    assert p2.body['bridge']['days'] == 1 and p2.body['bridge']['typeId'] == t['CASUAL']
    post(sita, f"/leave/requests/{s.body['id']}/cancel", {})

    # rejects clubbing chains, self-clubbing and other companies' types; order can be set in one call; employees cannot
    assert patch(hr, f"/leave/types/{t['HOME']}", {'clubWithId': t['HOME']}).status == 400
    assert patch(hr, f"/leave/types/{t['SICK']}", {'clubWithId': t['CASUAL']}).status == 400
    assert patch(hr, f"/leave/types/{t['HOME']}", {'clubWithId': t['SICK']}).status == 400
    assert post(hr, '/leave/types/order', {'ids': [t['SICK'], t['HOME']]}).status == 200
    types = get(hr, '/leave/types').body['types']

    def so(code):
        return next(z for z in types if z['code'] == code)['sort_order']
    assert so('SICK') < so('HOME')
    assert post(sita, '/leave/types/order', {'ids': [t['HOME']]}).status == 403
    assert post(hr, '/leave/types/order', {'ids': ['00000000-0000-7000-8000-00000000dead']}).status == 400
    patch(hr, f"/leave/types/{t['CASUAL']}", {'clubWithId': None})


def test_late_arrival_penalty(demo, x):
    hr, sita, ram, t, adjust = x['hr'], x['sita'], x['ram'], x['t'], x['adjust']
    Y, M = 2083, 5
    start = bs_month_bounds(Y, M)['startAd']
    late_dates = []
    i = 0
    while len(late_dates) < 4:
        d = (date.fromisoformat(start) + timedelta(days=i)).isoformat()
        i += 1
        if date.fromisoformat(d).weekday() == 5:
            continue
        late_dates.append(d)
    for d in late_dates:
        for time, direction in (('10:40', 'in'), ('18:00', 'out')):
            r = post(hr, '/punch/manual', {'employeeId': demo['ram'], 'localDate': d, 'time': time, 'direction': direction, 'reason': 'test data'})
            assert r.status == 201
    adjust(demo['ram'], 'HOME', 0.25)

    # pure rules
    assert penalty_for(2, {'lateCount': 3, 'penaltyDays': 0.5}) == 0
    assert penalty_for(7, {'lateCount': 3, 'penaltyDays': 0.5}) == 1
    assert allocate_penalty(1, [{'leaveTypeId': 'a', 'code': 'A', 'available': 0.4}, {'leaveTypeId': 'b', 'code': 'B', 'available': 5}]) == \
        {'allocations': [{'leaveTypeId': 'a', 'code': 'A', 'days': 0.25}, {'leaveTypeId': 'b', 'code': 'B', 'days': 0.75}], 'unpaid': 0}
    assert allocate_penalty(1, [{'leaveTypeId': 'a', 'code': 'A', 'available': 0}]) == {'allocations': [], 'unpaid': 1}

    # HR sets the rule; only paid balance leave types are accepted; employees cannot change it
    assert put(hr, '/attendance-tools/policy', {'latePenalty': {'enabled': True, 'leaveTypeIds': [t['LWP']]}}).status == 400
    assert put(sita, '/attendance-tools/policy', {}).status == 403
    ok = put(hr, '/attendance-tools/policy', {'latePenalty': {'enabled': True, 'lateCount': 3, 'penaltyDays': 0.5, 'minLateMinutes': 5,
                                                              'leaveTypeIds': [t['HOME']]}})
    assert ok.status == 200
    assert ok.body['policy']['latePenalty']['enabled'] is True and ok.body['policy']['latePenalty']['lateCount'] == 3

    # preview shows late days and the split; the employee sees their own count; managers see their team only
    r = get(hr, '/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M})
    assert r.status == 200
    line = next(ln for ln in r.body['lines'] if ln['employeeId'] == demo['ram'])
    assert (line['lateDays'], line['penaltyDays'], line['unpaidDays'], line['applied']) == (4, 0.5, 0.25, None)
    assert line['lateDates'] == late_dates
    assert line['allocations'] == [{'leaveTypeId': t['HOME'], 'code': 'HOME', 'days': 0.25}]
    me = get(ram, '/attendance-tools/penalties/me', {'bsYear': Y, 'bsMonth': M})
    assert (me.body['lateDays'], me.body['projectedDays'], me.body['enabled']) == (4, 0.5, True)
    mgr = get(ram, '/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M})
    assert not any(ln['employeeId'] == demo['ram'] for ln in mgr.body['lines'])
    assert get(sita, '/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M}).status == 403

    # applying posts the leave deduction and unpaid days for payroll, once; reversing restores the balance
    before = ledger_balance(demo['ram'], t['HOME'])
    assert post(ram, '/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M}).status == 403
    a = post(hr, '/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M})
    assert a.status == 200
    assert a.body['applied'] == 1
    assert ledger_balance(demo['ram'], t['HOME']) == before - 0.25
    assert penalty_unpaid_days(demo['ram'], Y, M) == 0.25
    assert len(rows("SELECT 1 AS x FROM notification_outbox WHERE user_id = %s AND template = 'attendance.late_penalty'", [x['ram_user']])) == 1
    again = post(hr, '/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M})
    assert again.body['applied'] == 0
    lst = get(hr, '/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M})
    pid = next(ln for ln in lst.body['lines'] if ln['employeeId'] == demo['ram'])['applied']['id']
    assert post(hr, f'/attendance-tools/penalties/{pid}/reverse', {'reason': 'x'}).status == 400
    rev = post(hr, f'/attendance-tools/penalties/{pid}/reverse', {'reason': 'device clock was wrong'})
    assert rev.status == 200
    assert ledger_balance(demo['ram'], t['HOME']) == before
    assert penalty_unpaid_days(demo['ram'], Y, M) == 0
    assert post(hr, f'/attendance-tools/penalties/{pid}/reverse', {'reason': 'twice over'}).status == 409

    # a locked pay run freezes the month
    b = bs_month_bounds(Y, M)
    per = one("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES (%s, %s, %s, %s, %s, %s)
                 ON CONFLICT (company_id, bs_year, bs_month) DO UPDATE SET days = EXCLUDED.days RETURNING id""",
              [demo['company_id'], Y, M, b['startAd'], b['endAd'], b['days']])
    execute("INSERT INTO payroll_run (company_id, period_id, run_type, status, locked_at) VALUES (%s, %s, 'regular', 'locked', now())",
            [demo['company_id'], per['id']])
    assert post(hr, '/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M}).status == 409


def test_blocking_clock_in_after_the_grace_time(demo, x):
    hr, t = x['hr'], x['t']
    day = '2026-11-02'  # a Monday
    c, s = demo['company_id'], demo['sita']

    def at(hhmm):
        return local_time_ms(day, hhmm)
    # is off by default
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=at('11:30'), direction='in') is None

    # refuses a first clock-in after start + grace (+ extra minutes), never a clock-out
    put(hr, '/attendance-tools/policy', {'blockLateClockIn': True, 'blockAfterMin': 10})
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=at('10:20'), direction='in') is None
    with pytest.raises(ConflictError) as e:
        assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=at('10:26'), direction='in')
    assert e.value.status == 409
    assert {k: e.value.details[k] for k in ('code', 'closedAt', 'shiftStart')} == {'code': 'late_blocked', 'closedAt': '10:25', 'shiftStart': '10:00'}
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=at('10:26'), direction='out') is None

    # weekly offs, approved first-half leave and a second clock-in are not blocked
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=local_time_ms('2026-11-07', '13:00'), direction='in') is None
    execute("""INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, half_day, days_requested, status)
               VALUES (%s, %s, '2026-11-03', '2026-11-03', 'first_half', 0.5, 'approved')""", [s, t['HOME']])
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=local_time_ms('2026-11-03', '14:00'), direction='in') is None
    post(hr, '/punch/manual', {'employeeId': s, 'localDate': day, 'time': '09:55', 'direction': 'in', 'reason': 'test data'})
    assert assert_clock_in_allowed(company_id=c, employee_id=s, at_ms=at('14:00'), direction='in') is None
    put(hr, '/attendance-tools/policy', {'blockLateClockIn': False})


CSV = ('emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\nE002,2083-06-01,22:00,06:00,night\n'
       'E999,2083-06-01,10:00,18:00,\nE001,2083-13-01,10:00,,\nE001,2083-06-01,10:00,18:00,dup\n')


def test_manual_attendance_upload(demo, x):
    hr, ram = x['hr'], x['ram']
    # checks every row first and reports errors per line; only HR
    assert post(ram, '/attendance-tools/upload/check', {'csv': CSV}).status == 403
    r = post(hr, '/attendance-tools/upload/check', {'csv': CSV})
    assert r.status == 200
    assert (r.body['valid'], r.body['invalid']) == (2, 3)
    assert r.body['rows'][1]['empCode'] == 'E002' and r.body['rows'][1]['overnight'] is True and r.body['rows'][1]['errors'] == []
    assert 'No employee with code E999' in r.body['rows'][2]['errors'][0]
    assert 'not a valid BS date' in r.body['rows'][3]['errors'][0]
    assert 'more than once' in r.body['rows'][4]['errors'][0]
    bad = post(hr, '/attendance-tools/upload/import', {'csv': CSV, 'reason': 'device outage'})
    assert bad.status == 400
    assert len(bad.body['details']['rows']) == 3
    no_header = post(hr, '/attendance-tools/upload/check', {'csv': 'a,b\n1,2\n'})
    assert len(no_header.body['errors']) > 0

    # imports manual punches, recomputes the days, and a second upload adds nothing
    good = '\n'.join(CSV.split('\n')[:3])
    r = post(hr, '/attendance-tools/upload/import', {'csv': good, 'reason': 'device outage', 'filename': 'aug.csv'})
    assert r.status == 201
    assert (r.body['rows'], r.body['punches'], r.body['duplicates']) == (2, 4, 0)
    day = one("SELECT status FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-17'", [demo['sita']])
    assert day['status'] == 'Present'
    again = post(hr, '/attendance-tools/upload/import', {'csv': good, 'reason': 'device outage'})
    assert (again.body['punches'], again.body['duplicates']) == (0, 4)
    hist = get(hr, '/attendance-tools/uploads')
    assert len(hist.body['uploads']) == 2
    tpl = get(hr, '/attendance-tools/upload/template')
    assert 'emp_code,date_bs,in_time,out_time,note' in tpl.text


def test_attendance_issues_and_nudges(demo, x):
    hr, sita, ram, t, outsider = x['hr'], x['sita'], x['ram'], x['t'], x['outsider']
    x['adjust'](demo['sita'], 'HOME', 10)
    recompute_day(demo['sita'], '2026-10-05')
    lv = post(sita, '/leave/requests', {'leaveTypeId': t['HOME'], 'from': '2026-10-07', 'to': '2026-10-07', 'reason': 'errand'})
    assert lv.status == 201
    leave_id = lv.body['id']

    # lists absences and pending requests with who they wait on; managers see their team
    r = get(ram, '/attendance-tools/issues', {'from': '2026-10-01', 'to': '2026-10-10'})
    assert r.status == 200
    s = next(z for z in r.body['rows'] if z['employeeId'] == demo['sita'])
    assert '2026-10-05' in s['absent']
    assert s['pending'][0]['type'] == 'leave' and s['pending'][0]['requestId'] == leave_id
    assert x['ram_user'] in [w_['userId'] for w_ in s['pending'][0]['waitingOn']]
    assert not any(z['employeeId'] == OTHER_EMP for z in r.body['rows'])
    assert get(sita, '/attendance-tools/issues', {'from': '2026-10-01', 'to': '2026-10-10'}).status == 403
    assert get(hr, '/attendance-tools/issues', {'from': '2026-01-01', 'to': '2026-10-10'}).status == 400

    # the pre-payroll summary counts them
    r = get(hr, '/attendance-tools/issues/summary', {'bsYear': 2083, 'bsMonth': 6})
    assert r.status == 200
    assert r.body['absent'] > 0
    assert r.body['pendingLeave'] == 1

    # nudges the employee and the approver, once a day each
    e1 = post(ram, '/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01', 'to': '2026-10-10'})
    assert (e1.body['sent'], e1.body['already']) == (1, False)
    e2 = post(ram, '/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01', 'to': '2026-10-10'})
    assert (e2.body['sent'], e2.body['already']) == (0, True)
    n = rows("SELECT channel, title FROM notification_outbox WHERE user_id = %s AND template = 'attendance.nudge' AND channel = 'in_app'", [x['sita_user']])
    assert len(n) == 1
    inst = one('SELECT workflow_instance_id FROM leave_request WHERE id = %s', [leave_id])['workflow_instance_id']
    a = post(hr, '/attendance-tools/issues/nudge', {'target': 'approver', 'instanceId': str(inst)})
    assert a.body['sent'] == 1
    assert len(rows("SELECT 1 AS x FROM notification_outbox WHERE user_id = %s AND template = 'attendance.nudge_approver' AND channel = 'in_app'",
                    [x['ram_user']])) == 1
    # an outsider manager-less employee cannot nudge anyone
    assert post(outsider, '/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01',
                                                             'to': '2026-10-10'}).status == 403


def test_whos_out(demo, x):
    hr, ram, outsider, t = x['hr'], x['ram'], x['outsider'], x['t']
    today = (datetime.now(timezone.utc) + timedelta(minutes=345)).date().isoformat()
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    execute("INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status) VALUES (%s, %s, %s, %s, 1, 'approved')",
            [demo['sita'], t['SICK'], tomorrow, tomorrow])
    r = get(ram, '/attendance-tools/whos-out', {'days': 3})
    assert r.status == 200
    assert r.body['scope'] == 'team'
    d = next(z for z in r.body['days'] if z['date'] == tomorrow)
    assert len(d['people']) == 1
    assert d['people'][0]['employeeId'] == demo['sita'] and d['people'][0]['kind'] == 'leave' and d['people'][0]['detail'] is None
    assert 'Sick' not in r.text
    other = get(outsider, '/attendance-tools/whos-out', {'days': 3})
    assert sum(len(z['people']) for z in other.body['days']) == 0
    put(hr, '/attendance-tools/policy', {'whosOutScope': 'company'})
    co = get(outsider, '/attendance-tools/whos-out', {'days': 3})
    assert sum(len(z['people']) for z in co.body['days']) > 0


# ================================================================ route rules not covered by the TS tests

def test_penalty_self_rules(demo, login_as):
    """Nobody but an administrator applies or reverses their own late penalty (routes.ts selfOf)."""
    hr = login_as(['hr']).token
    put(hr, '/attendance-tools/policy', {'latePenalty': {'enabled': True}})
    me = login_as(['hr'], demo['ram']).token
    r = post(me, '/attendance-tools/penalties/apply', {'bsYear': 2083, 'bsMonth': 5, 'employeeIds': [demo['ram']]})
    assert r.status == 403 and r.body['error'] == 'someone else must apply your own late penalty'
    put(hr, '/attendance-tools/policy', {'latePenalty': {'enabled': False}})
    r = post(hr, '/attendance-tools/penalties/apply', {'bsYear': 2083, 'bsMonth': 5})
    assert r.status == 409 and r.body['error'] == 'turn the late-arrival penalty on first'


def test_upload_rejects_own_attendance(demo, login_as):
    me = login_as(['hr'], demo['sita']).token
    r = post(me, '/attendance-tools/upload/check', {'csv': 'emp_code,date_bs,in_time\nE001,2083-06-01,10:00\n'})
    assert r.body['rows'][0]['errors'] == ['You cannot upload your own attendance; ask another HR person.']


def test_nudge_yourself_and_nothing_to_fix(demo, login_as):
    from hajiri.core.auth import create_user  # noqa: F401
    mgr = login_as(['manager'], demo['ram']).token
    r = post(mgr, '/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['ram'], 'from': '2026-10-01', 'to': '2026-10-10'})
    assert r.status == 400 and r.body['error'] == 'you cannot nudge yourself'
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    r = post(mgr, '/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01', 'to': '2026-10-10'})
    assert r.status == 409 and r.body['error'] == 'there is nothing for this employee to fix in this period'
