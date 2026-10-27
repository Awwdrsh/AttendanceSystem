"""The attendance hub pages for daily attendance and the attendance tools, through django.test.Client."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.db import execute, one, rows
from hajiri.leave.services import leave_type_by_code
from tests.punch.api import post, put, today
from tests.web.factories import htmx, hx_triggers, toasts


def browser(login) -> Client:
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    return c


@pytest.fixture
def people(demo, login_as):
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return {'hr': login_as(['hr']), 'sita': login_as(['employee'], demo['sita']), 'ram': login_as(['manager'], demo['ram'])}


def test_whos_in_page(demo, people):
    hr = browser(people['hr'])
    post(people['hr'].token, '/punch/manual', {'employeeId': demo['sita'], 'localDate': today(), 'time': '10:30', 'direction': 'in', 'reason': 'test data'})
    r = hr.get('/attendance')
    assert r.status_code == 200
    html = r.content.decode()
    assert "Who's in" in html and 'Sita Sharma' in html and 'Ram Thapa' in html
    for label in ('Everyone', 'From home', 'Not yet in', 'On leave', 'First in', 'Last out', 'Breaks', 'Worked'):
        assert label in html
    r = hr.get('/attendance', {'filter': 'notyet'}, **htmx('team-today'))
    body = r.content.decode()
    assert 'id="team-today"' in body and '<html' not in body
    assert 'Ram Thapa' in body and 'Sita Sharma' not in body
    # a manager sees their reports and themselves; employees are refused
    m = browser(people['ram']).get('/attendance').content.decode()
    assert 'Sita Sharma' in m
    assert browser(people['sita']).get('/attendance').status_code in (302, 403)


def test_exceptions_page_and_recalculate(demo, people):
    hr = browser(people['hr'])
    # A working day 3 to 7 days back: not the demo weekly off (Saturday) and not a company holiday.
    holidays = {str(h['local_date']) for h in rows('SELECT local_date FROM holiday WHERE company_id = %s', [demo['company_id']])}
    d = next(x for x in ((date.fromisoformat(today()) - timedelta(days=n)) for n in range(3, 8))
             if x.weekday() != 5 and x.isoformat() not in holidays).isoformat()
    post(people['hr'].token, '/punch/manual', {'employeeId': demo['sita'], 'localDate': d, 'time': '10:00', 'direction': 'in', 'reason': 'test data'})
    r = hr.get('/attendance/exceptions')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Exceptions' in html and 'Missing clock-out 1' in html and 'Sita Sharma' in html
    r = hr.post('/_/attendance_tools/recompute', {'exceptions-selected': [f"{demo['sita']}|{d}"]}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == '1 day recalculated.'
    assert 'hj:refresh-exceptions' in hx_triggers(r)
    # the tab is for the attendance team (AREA.attendance)
    assert browser(people['ram']).get('/attendance/exceptions').status_code in (302, 403)


def test_issues_page_and_nudges(demo, people):
    from hajiri.attendance.service import recompute_day
    recompute_day(demo['sita'], '2026-10-05')
    ram = browser(people['ram'])
    r = ram.get('/attendance/issues', {'mode': 'month', 'y': 2083, 'm': 6})
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Attendance issues, Ashwin 2083' in html and 'Sita Sharma' in html and 'Remind' in html
    assert 'Unexplained absences' in html
    b = bs_bounds(2083, 6)
    r = ram.post('/_/attendance_tools/nudge', {'target': 'employee', 'employee_id': demo['sita'], 'from': b[0], 'to': b[1]}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Reminder sent.'
    r = ram.post('/_/attendance_tools/nudge', {'target': 'employee', 'employee_id': demo['sita'], 'from': b[0], 'to': b[1]}, **htmx())
    assert toasts(r)[0] == {'message': 'Already reminded today.', 'tone': 'info'}
    r = ram.post('/_/attendance_tools/nudge', {'target': 'employee', 'employee_id': demo['ram'], 'from': b[0], 'to': b[1]}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['title'] == 'Reminder not sent'
    r = ram.get('/attendance/issues', {'mode': 'recent'}, **htmx('attendance-issues'))
    assert 'Last 14 days' in r.content.decode()


def bs_bounds(y, m):
    from engines.bs_calendar import bs_month_bounds
    b = bs_month_bounds(y, m)
    return b['startAd'], b['endAd']


def test_penalties_page_rules_apply_and_reverse(demo, people):
    hr = browser(people['hr'])
    home = str(leave_type_by_code(demo['company_id'], 'HOME')['id'])
    r = hr.get('/attendance/penalties', {'y': 2083, 'm': 5})
    assert r.status_code == 200
    html = r.content.decode()
    for s in ('Attendance rules', 'Late-arrival penalty', 'Close clock-in after the grace time', "Who's out shows", 'Save rules',
              'Late-arrival penalties, Bhadra 2083', 'The late-arrival penalty is off.', 'Nobody was late this month'):
        assert s in html, s
    r = hr.post('/_/attendance_tools/policy', {'lp_enabled': 'on', 'lp_lateCount': '3', 'lp_penaltyDays': '0.5', 'lp_minLateMinutes': '5',
                                              'lp_leaveTypeIds': [home], 'blockAfterMin': '0', 'whosOutScope': 'team'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Attendance rules saved.'
    b = bs_bounds(2083, 5)
    d0 = date.fromisoformat(b[0])
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(6) if (d0 + timedelta(days=i)).weekday() != 5][:3]
    for d in days:
        for t, direction in (('10:40', 'in'), ('18:00', 'out')):
            post(people['hr'].token, '/punch/manual', {'employeeId': demo['ram'], 'localDate': d, 'time': t, 'direction': direction, 'reason': 'test data'})
    html = hr.get('/attendance/penalties', {'y': 2083, 'm': 5}, **htmx('late-penalties')).content.decode()
    assert 'Ram Thapa' in html and 'To apply' in html and '0.5 days unpaid' in html
    r = hr.post('/_/attendance_tools/penalties/apply', {'bsYear': '2083', 'bsMonth': '5'}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == '1 penalty applied. The employees have been told.'
    pid = str(one("SELECT id FROM late_penalty WHERE employee_id = %s AND status = 'applied'", [demo['ram']])['id'])
    f = hr.get(f'/_/attendance_tools/penalties/{pid}/reverse', {'name': 'Ram Thapa'}, **htmx('hj-modal-body')).content.decode()
    assert 'Reverse penalty for Ram Thapa' in f
    r = hr.post(f'/_/attendance_tools/penalties/{pid}/reverse', {'reason': 'device clock was wrong'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Penalty reversed.'
    assert browser(people['ram']).get('/attendance/penalties').status_code in (302, 403)


def test_upload_page_check_and_import(demo, people):
    from django.core.files.uploadedfile import SimpleUploadedFile
    hr = browser(people['hr'])
    r = hr.get('/attendance/upload')
    html = r.content.decode()
    assert r.status_code == 200 and 'Upload attendance' in html and 'Choose CSV file' in html and 'No uploads yet' in html
    csv = 'emp_code,date_bs,in_time,out_time,note\nE001,2083-06-01,9:58,18:05,device offline\n'
    r = hr.post('/_/attendance_tools/upload/check', {'file': SimpleUploadedFile('aug.csv', csv.encode(), 'text/csv')}, **htmx('upload-check'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Add 1 row(s)' in body and 'Sita Sharma' in body and 'OK' in body
    r = hr.post('/_/attendance_tools/upload/import', {'csv': csv, 'reason': 'device outage', 'filename': 'aug.csv'}, **htmx('upload-check'))
    assert r.status_code == 200
    assert toasts(r)[0] == {'message': '2 punch(es) added for 1 day(s).', 'tone': 'ok', 'title': 'Attendance uploaded'}
    assert 'hj:refresh-upload-history' in hx_triggers(r)
    t = hr.get('/_/attendance_tools/upload/template')
    assert t['Content-Disposition'] == 'attachment; filename="attendance-upload-template.csv"'
    assert 'emp_code,date_bs' in t.content.decode('utf-8-sig')
    r = hr.post('/_/attendance_tools/upload/import', {'csv': csv, 'reason': 'x'}, **htmx('upload-check'))
    assert r.status_code == 400 and toasts(r)[0]['title'] == 'Not uploaded'


def test_my_attendance_page_and_day(demo, people):
    sita = browser(people['sita'])
    post(people['hr'].token, '/punch/manual', {'employeeId': demo['sita'], 'localDate': '2026-10-05', 'time': '10:00', 'direction': 'in', 'reason': 'test data'})
    r = sita.get('/me/attendance', {'y': 2083, 'm': 6})
    html = r.content.decode()
    assert r.status_code == 200
    for s in ('My attendance', 'Ashwin 2083', 'Present days', 'Missing clock-out', 'Overtime', 'in the Gregorian calendar', 'Sat'):
        assert s in html, s
    d = sita.get('/_/attendance_tools/day', {'date': '2026-10-05'}, **htmx('hj-modal-body')).content.decode()
    assert 'Clock-ins and clock-outs' in d and 'Clock-in' in d and 'Added by HR' in d and 'Request correction' in d
    c = sita.get('/_/attendance_tools/day', {'date': '2026-10-05', 'correct': '1'}, **htmx('hj-modal-body')).content.decode()
    assert 'Attendance correction' in c and '/_/regularisation/new' in c
    page = sita.get('/me/attendance', {'correct': '1'}).content.decode()
    assert f'date={today()}&amp;correct=1' in page and 'hx-trigger="load"' in page


def test_late_note_whos_out_and_payroll_check(demo, people):
    hr = people['hr']
    put(hr.token, '/attendance-tools/policy', {'latePenalty': {'enabled': True, 'lateCount': 3, 'penaltyDays': 0.5}})
    b = bs_bounds(2083, 5)
    post(hr.token, '/punch/manual', {'employeeId': demo['sita'], 'localDate': b[0], 'time': '10:40', 'direction': 'in', 'reason': 'test data'})
    note = browser(people['sita']).get('/_/attendance_tools/late-note', {'y': 2083, 'm': 5}).content.decode()
    assert '1 late day in Bhadra 2083' in note and '2 more late days this month would count as a penalty.' in note
    sick = str(leave_type_by_code(demo['company_id'], 'SICK')['id'])
    tomorrow = (date.fromisoformat(today()) + timedelta(days=1)).isoformat()
    execute("INSERT INTO leave_request (employee_id, leave_type_id, from_date, to_date, days_requested, status) VALUES (%s, %s, %s, %s, 1, 'approved')",
            [demo['sita'], sick, tomorrow, tomorrow])
    w = browser(people['ram']).get('/_/attendance_tools/whos-out').content.decode()
    assert "Who's out" in w and 'Sita Sharma (on leave)' in w and 'Sick' not in w
    p = browser(hr).get('/_/attendance_tools/payroll-check', {'bsYear': 2083, 'bsMonth': 6}).content.decode()
    assert 'Review and remind' in p and 'Late penalties' in p
    blocked = browser(people['sita']).get('/_/attendance_tools/late-blocked', {'message': 'Clock-in closed at 10:25.'}).content.decode()
    assert 'Clock-in is closed' in blocked and 'Raise a time request' in blocked


def test_nav_tabs(demo, people):
    html = browser(people['hr']).get('/attendance').content.decode()
    for label in ('Today', 'Work from home', 'Exceptions', 'Issues', 'Late penalties', 'Upload'):
        assert f'>{label}<' in html or label in html
