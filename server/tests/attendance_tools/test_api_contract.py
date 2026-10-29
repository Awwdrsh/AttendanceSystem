"""The exact JSON the React attendance-tools pages read (LatePenaltiesPage, AttendanceIssuesPage, AttendanceUploadPage,
PayrollAttendanceCheck, the Home cards), key for key as apps/api/src/modules/attendance-tools/routes.ts returned it
(git show 65a9079:...), through the browser's cookie session."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from engines.bs_calendar import bs_month_bounds

from hajiri.attendance.service import recompute_day
from hajiri.leave.services import leave_type_by_code
from tests.punch.api import Browser, now_iso, post
from tests.support.sql import execute, one

Y, M = 2083, 5
INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
LINE = {'employeeId', 'empCode', 'name', 'lateDays', 'lateMinutes', 'lateDates', 'penaltyDays', 'allocations', 'unpaidDays',
        'applied'}


def test_rules_and_penalty_shapes(demo, login_as):
    hr_login = login_as(['hr'])
    hr = Browser(hr_login.token)
    ram = Browser(login_as(['employee'], demo['ram']).token)
    home = str(leave_type_by_code(demo['company_id'], 'HOME')['id'])

    p = hr.get('/attendance-tools/policy').body
    assert p == {'policy': {'blockLateClockIn': False, 'blockAfterMin': 0, 'whosOutScope': 'team',
                            'latePenalty': {'enabled': False, 'lateCount': 3, 'minLateMinutes': 1, 'penaltyDays': 0.5,
                                            'leaveTypeIds': []}}}
    # the page sends the whole draft back
    draft = {**p['policy'], 'latePenalty': {**p['policy']['latePenalty'], 'enabled': True, 'penaltyDays': 1,
                                            'minLateMinutes': 5, 'leaveTypeIds': [home]}}
    saved = hr.put('/attendance-tools/policy', draft)
    assert saved.status == 200 and saved.body == {'policy': draft}
    assert hr.get('/attendance-tools/policy').body == {'policy': draft}
    assert hr.put('/attendance-tools/policy', {**draft, 'latePenalty': {**draft['latePenalty'], 'penaltyDays': 0.3}}).status == 400

    start = date.fromisoformat(bs_month_bounds(Y, M)['startAd'])
    days = [d for d in (start + timedelta(days=i) for i in range(8)) if d.weekday() != 5][:3]
    for d in days:
        for time, direction in (('10:40', 'in'), ('18:00', 'out')):
            assert hr.post('/punch/manual', {'employeeId': demo['ram'], 'localDate': d.isoformat(), 'time': time,
                                             'direction': direction, 'reason': 'test data'}).status == 201
    assert post(hr_login.token, '/leave/adjust', {'employeeId': demo['ram'], 'leaveTypeId': home, 'deltaDays': 0.25,
                                                  'reason': 'opening', 'kind': 'allocation',
                                                  'effectiveDate': start.isoformat()}).status in (200, 201)

    pv = hr.get('/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M}).body
    assert set(pv) == {'rule', 'locked', 'lines'} and pv['rule'] == draft['latePenalty'] and pv['locked'] is False
    line = next(x for x in pv['lines'] if x['employeeId'] == demo['ram'])
    assert set(line) == LINE
    assert line['lateDays'] == 3 and line['lateMinutes'] == 75 and line['lateDates'] == [d.isoformat() for d in days]
    assert line['penaltyDays'] == 1 and line['unpaidDays'] == 0.75 and line['applied'] is None
    assert line['allocations'] == [{'leaveTypeId': home, 'code': 'HOME', 'days': 0.25}]

    me = ram.get('/attendance-tools/penalties/me', {'bsYear': Y, 'bsMonth': M}).body
    assert me == {'bsYear': Y, 'bsMonth': M, 'enabled': True, 'rule': {'lateCount': 3, 'penaltyDays': 1, 'minLateMinutes': 5},
                  'lateDays': 3, 'lateDates': [d.isoformat() for d in days], 'projectedDays': 1, 'applied': None}
    assert set(ram.get('/attendance-tools/penalties/me').body) == set(me)     # this month by default

    a = hr.post('/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M})
    assert a.status == 200 and a.body == {'applied': 1, 'skipped': 0}
    line = next(x for x in hr.get('/attendance-tools/penalties', {'bsYear': Y, 'bsMonth': M}).body['lines']
                if x['employeeId'] == demo['ram'])
    ap = line['applied']
    assert set(ap) == {'id', 'penaltyDays', 'leaveDays', 'unpaidDays', 'appliedAt'} and INSTANT.match(ap['appliedAt'])
    assert (ap['penaltyDays'], ap['leaveDays'], ap['unpaidDays']) == (1, 0.25, 0.75)
    assert ram.get('/attendance-tools/penalties/me', {'bsYear': Y, 'bsMonth': M}).body['applied'] == ap
    assert hr.post('/attendance-tools/penalties/apply', {'bsYear': Y, 'bsMonth': M}).body == {'applied': 0, 'skipped': 1}
    assert hr.post(f"/attendance-tools/penalties/{ap['id']}/reverse", {'reason': 'device clock was wrong'}).body == {'ok': True}
    again = hr.post(f"/attendance-tools/penalties/{ap['id']}/reverse", {'reason': 'device clock was wrong'})
    assert again.status == 409 and again.body == {'error': 'this penalty is already reversed'}


def test_late_clock_in_refusal_shape(demo, login_as):
    """HomePage reads e.details.code == 'late_blocked' from the 409 of POST /punch and shows e.message."""
    hr = Browser(login_as(['hr']).token)
    sita = Browser(login_as(['employee'], demo['sita']).token)
    assert hr.put('/attendance-tools/policy', {'blockLateClockIn': True, 'blockAfterMin': 0}).status == 200
    # a shift that started long ago today, so any clock-in now is late (unless it is a day off)
    now_npt = datetime.now(timezone.utc) + timedelta(minutes=345)
    if now_npt.hour < 1:
        return
    execute("UPDATE shift SET start_time = '00:01', end_time = '23:59', grace_in_min = 0 WHERE id = %s", [demo['shift_id']])
    execute("UPDATE employee SET weekly_off_days = '{}' WHERE id = %s", [demo['sita']])
    execute('DELETE FROM holiday')
    r = sita.post('/punch', {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': 'late-block-01', 'source': 'web'})
    assert r.status == 409, r.body
    assert set(r.body) == {'error', 'details'} and r.body['error'].startswith('Clock-in closed at 00:01')
    assert r.body['details']['code'] == 'late_blocked' and r.body['details']['shiftStart'] == '00:01'
    assert set(r.body['details']) == {'code', 'shiftStart', 'closedAt', 'localDate'}


def test_issue_upload_and_whos_out_shapes(demo, login_as):
    hr_login = login_as(['hr'])
    hr = Browser(hr_login.token)
    sita_login = login_as(['employee'], demo['sita'])
    sita = Browser(sita_login.token)
    ram_login = login_as(['manager'], demo['ram'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    home = str(leave_type_by_code(demo['company_id'], 'HOME')['id'])
    post(hr_login.token, '/leave/adjust', {'employeeId': demo['sita'], 'leaveTypeId': home, 'deltaDays': 10, 'reason': 'opening',
                                           'kind': 'allocation', 'effectiveDate': '2026-07-20'})
    recompute_day(demo['sita'], '2026-10-05')
    lv = post(sita_login.token, '/leave/requests', {'leaveTypeId': home, 'from': '2026-10-07', 'to': '2026-10-07',
                                                    'reason': 'errand'})
    assert lv.status == 201, lv.body

    iss = hr.get('/attendance-tools/issues', {'from': '2026-10-01', 'to': '2026-10-10'}).body
    assert set(iss['summary']) == {'employees', 'missing', 'absent', 'pendingLeave', 'pendingTime', 'oldestPendingDays'}
    row = next(x for x in iss['rows'] if x['employeeId'] == demo['sita'])
    assert set(row) == {'employeeId', 'empCode', 'name', 'missing', 'absent', 'pending'} and '2026-10-05' in row['absent']
    pend = row['pending'][0]
    assert set(pend) == {'instanceId', 'type', 'requestId', 'from', 'to', 'label', 'createdAt', 'ageDays', 'waitingOn'}
    assert (pend['type'], pend['requestId'], pend['from'], pend['to']) == ('leave', lv.body['id'], '2026-10-07', '2026-10-07')
    assert isinstance(pend['ageDays'], int) and INSTANT.match(pend['createdAt']) and pend['label']
    assert {'userId': ram_login.user_id, 'name': ram_login.email} in pend['waitingOn']

    s = hr.get('/attendance-tools/issues/summary', {'bsYear': 2083, 'bsMonth': 6}).body
    assert set(s) == {'bsYear', 'bsMonth', 'from', 'to', 'employees', 'missing', 'absent', 'pendingLeave', 'pendingTime',
                      'oldestPendingDays'}
    assert s['pendingLeave'] == 1 and re.match(r'^\d{4}-\d{2}-\d{2}$', s['from'])

    n = hr.post('/attendance-tools/issues/nudge', {'target': 'employee', 'employeeId': demo['sita'], 'from': '2026-10-01',
                                                   'to': '2026-10-10'})
    assert n.body == {'sent': 1, 'already': False}
    n = hr.post('/attendance-tools/issues/nudge', {'target': 'approver', 'instanceId': pend['instanceId']})
    assert n.body == {'sent': 1, 'already': False}

    csv = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\nE404,2083-06-01,25:00,,\n'
    chk = hr.post('/attendance-tools/upload/check', {'csv': csv}).body
    assert set(chk) == {'errors', 'rows', 'valid', 'invalid'} and (chk['valid'], chk['invalid']) == (1, 1)
    good, bad = chk['rows']
    assert set(good) == {'line', 'empCode', 'date', 'dateBs', 'inTime', 'outTime', 'note', 'employeeId', 'name', 'overnight',
                         'errors'}
    assert good == {'line': 2, 'empCode': 'E001', 'date': good['date'], 'dateBs': '2083-06-01', 'inTime': '09:58',
                    'outTime': '18:05', 'note': 'device offline', 'employeeId': demo['sita'], 'name': good['name'],
                    'overnight': False, 'errors': []}
    assert re.match(r'^\d{4}-\d{2}-\d{2}$', good['date'])
    assert bad['employeeId'] is None and bad['note'] is None and bad['outTime'] is None and len(bad['errors']) == 2
    refused = hr.post('/attendance-tools/upload/import', {'csv': csv, 'reason': 'device outage'})
    assert refused.status == 400 and refused.body['details'] == {'rows': [{'line': 3, 'errors': bad['errors']}]}
    empty = hr.post('/attendance-tools/upload/check', {'csv': 'x,y\n1,2\n'}).body
    assert empty['rows'] == [] and len(empty['errors']) == 3

    one_row = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\n'
    imp = hr.post('/attendance-tools/upload/import', {'csv': one_row, 'reason': 'device outage', 'filename': 'aug.csv'})
    assert imp.status == 201 and set(imp.body) == {'id', 'rows', 'punches', 'duplicates', 'days'}
    assert (imp.body['rows'], imp.body['punches'], imp.body['duplicates'], imp.body['days']) == (1, 2, 0, 1)
    up = hr.get('/attendance-tools/uploads').body['uploads'][0]
    assert set(up) == {'id', 'filename', 'reason', 'rows_total', 'punches', 'duplicates', 'created_at', 'uploaded_by_name'}
    assert up['rows_total'] == 1 and up['filename'] == 'aug.csv' and INSTANT.match(up['created_at'])

    today = (datetime.now(timezone.utc) + timedelta(minutes=345)).date()
    tomorrow = (today + timedelta(days=1)).isoformat()
    execute("INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status, half_day) "
            "VALUES (%s, %s, %s, %s, 0.5, 'approved', 'first_half')", [demo['sita'], home, tomorrow, tomorrow])
    w = Browser(ram_login.token).get('/attendance-tools/whos-out', {'days': 3}).body
    assert w['scope'] == 'team' and [d['date'] for d in w['days']] == [(today + timedelta(days=i)).isoformat() for i in range(3)]
    d = next(x for x in w['days'] if x['date'] == tomorrow)
    assert d['people'] == [{'employeeId': demo['sita'], 'name': one('SELECT name_en FROM employee WHERE id = %s', [demo['sita']])['name_en'],
                            'designation': d['people'][0]['designation'], 'kind': 'leave', 'detail': 'First half'}]
