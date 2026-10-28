"""Pages and HTMX fragments: Fix a punch, the correction and work-from-home forms, the request list, the offline flush."""
from __future__ import annotations

import json

from django.test import Client

from hajiri.core import auth
from tests.support.sql import one, rows
from tests.regularisation.test_regularisation import days_ago, npt, workday_ago
from tests.web.factories import htmx, hx_triggers, toasts


def browser(login) -> Client:
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    return c


# ---------------------------------------------------------------- Fix a punch

def test_fix_page_for_hr_and_for_a_manager(reg):
    r = browser(reg['hr']).get('/attendance/fix')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'HR entries apply immediately and are written to the audit log.' in html
    assert 'Save punch' in html and 'Clock-in' in html and 'Clock-out' in html
    assert 'The employee can see this entry and your reason in their own punch log.' in html
    r = browser(reg['ram']).get('/attendance/fix')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'It is sent as an attendance correction and approved through the normal chain.' in html
    assert 'Send for approval' in html


def test_fix_page_is_not_for_employees(reg):
    r = browser(reg['sita']).get('/attendance/fix')
    assert r.status_code == 403
    assert 'Save punch' not in r.content.decode() and 'Send for approval' not in r.content.decode()
    r = browser(reg['sita']).post('/_/regularisation/fix', {}, **htmx())
    assert r.status_code == 403


def test_admin_is_treated_as_hr_on_the_fix_page(demo, reg, login_as):
    """useAuth().hasRole('hr') is true for admin, so an admin saves the punch at once."""
    admin = login_as(['admin'])
    html = browser(admin).get('/attendance/fix').content.decode()
    assert 'Save punch' in html
    r = browser(admin).post('/_/regularisation/fix', {'employee_id': demo['sita'], 'local_date': workday_ago(2), 'time': '10:00',
                                                      'direction': 'in', 'reason': 'device offline'}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'].startswith('Punch saved.')
    assert one("SELECT 1 AS x FROM regularisation_request WHERE employee_id = %s", [demo['sita']]) is None


def test_legacy_address_redirects(reg):
    r = browser(reg['hr']).get('/manager/fix-punch?x=1')
    assert r.status_code in (301, 302) and r['Location'] == '/attendance/fix?x=1'


def test_hr_saves_a_punch_and_sees_the_day(demo, reg):
    c = browser(reg['hr'])
    d = workday_ago(2)
    data = {'employee_id': demo['sita'], 'local_date': d, 'time': '10:00', 'direction': 'in', 'reason': 'device offline'}
    r = c.post('/_/regularisation/fix', data, **htmx())
    assert r.status_code == 200
    assert toasts(r)[0]['message'] == 'Punch saved. The day is now missing punch.'
    assert 'device offline' not in r.content.decode()           # the reason is cleared
    assert demo['sita'] in r.content.decode()                   # who and when stay
    r = c.post('/_/regularisation/fix', data, **htmx())
    assert toasts(r)[0]['message'] == 'That punch already existed.'
    r = c.post('/_/regularisation/fix', {**data, 'time': '18:00', 'direction': 'out'}, **htmx())
    assert toasts(r)[0]['message'] == 'Punch saved. The day is now present.'


def test_manager_fix_sends_a_correction_for_approval(demo, reg):
    d = workday_ago(2)
    r = browser(reg['ram']).post('/_/regularisation/fix', {'employee_id': demo['sita'], 'local_date': d, 'time': '18:00',
                                                            'direction': 'out', 'reason': 'forgot to clock out'}, **htmx())
    assert r.status_code == 200
    assert toasts(r)[0]['message'] == 'Correction request sent for approval.'
    rr = one('SELECT kind, status, punch_direction, punch_time FROM regularisation_request WHERE employee_id = %s', [demo['sita']])
    assert (rr['kind'], rr['status'], rr['punch_direction']) == ('missed_punch', 'pending', 'out')
    from hajiri.web.format import fmt_time
    assert fmt_time(rr['punch_time']) == '18:00'


def test_fix_form_errors(demo, reg, other_company):
    c = browser(reg['hr'])
    r = c.post('/_/regularisation/fix', {'employee_id': demo['sita'], 'local_date': workday_ago(2), 'time': '25:00',
                                          'direction': 'in', 'reason': 'x'}, **htmx())
    assert r.status_code == 422
    html = r.content.decode()
    assert 'Use HH:MM, for example 18:05' in html
    # A manager filing for someone outside their team gets the service's refusal as a toast.
    other = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E777', 'Not Ram''s') RETURNING id", [demo['company_id']])
    r = browser(reg['ram']).post('/_/regularisation/fix', {'employee_id': str(other['id']), 'local_date': workday_ago(2), 'time': '18:00',
                                                            'direction': 'out', 'reason': 'not my report'}, **htmx())
    assert r.status_code == 403
    assert toasts(r)[0]['message'] == 'not allowed to act for this employee'


# ---------------------------------------------------------------- correction form

def test_correction_form_get_and_post(demo, reg):
    c = browser(reg['sita'])
    d = workday_ago(2)
    r = c.get(f"/_/regularisation/new?employee_id={demo['sita']}&date={d}", **htmx())
    assert r.status_code == 200
    html = r.content.decode()
    assert 'What needs correcting?' in html and 'I forgot to clock in or out' in html and 'General 10-18, 10:00 to 18:00' in html
    assert 'Your manager sees this when reviewing the request.' in html
    r = c.post('/_/regularisation/new', {'employee_id': demo['sita'], 'date': d, 'kind': 'missed_punch', 'direction': 'out',
                                         'time': '18:00', 'reason': 'forgot'}, **htmx())
    assert r.status_code == 200
    assert toasts(r)[0]['message'] == 'Correction request sent. Your manager will see it in their inbox.'
    ev = hx_triggers(r)
    assert ev.get('hj:close-modal') and ev.get('hj:refresh-regularisation-requests') and ev.get('hj:regularisation-filed')
    r = c.post('/_/regularisation/new', {'local_date': d, 'kind': 'shift_change', 'shift_id': demo['shift_id'], 'time': '',
                                         'reason': 'swapped with Ram'}, **htmx())
    assert r.status_code == 200
    kinds = sorted(x['kind'] for x in rows('SELECT kind FROM regularisation_request WHERE employee_id = %s', [demo['sita']]))
    assert kinds == ['missed_punch', 'shift_change']


def test_correction_form_validation(demo, reg):
    c = browser(reg['sita'])
    r = c.post('/_/regularisation/new', {'local_date': workday_ago(2), 'kind': 'missed_punch', 'direction': 'out', 'time': '9:3',
                                         'reason': 'ab'}, **htmx())
    assert r.status_code == 422
    assert 'Use HH:MM, e.g. 09:30' in r.content.decode()
    # A rule only the service knows comes back as a toast.
    r = c.post('/_/regularisation/new', {'local_date': days_ago(-3), 'kind': 'on_duty', 'reason': 'future visit'}, **htmx())
    assert r.status_code == 400
    assert toasts(r)[0]['message'] == 'cannot regularise a future date'


# ---------------------------------------------------------------- work from home

def test_wfh_request(demo, reg):
    c = browser(reg['sita'])
    r = c.get('/_/regularisation/wfh', **htmx())
    assert r.status_code == 200 and 'Ask your manager to approve a day at home.' in r.content.decode()
    r = c.post('/_/regularisation/wfh', {'local_date': days_ago(-3), 'reason': 'waiting for a delivery'}, **htmx())
    assert r.status_code == 200
    assert toasts(r)[0]['message'] == 'Request sent to your manager.'
    assert one("SELECT kind FROM regularisation_request WHERE employee_id = %s", [demo['sita']])['kind'] == 'wfh'
    r = c.post('/_/regularisation/wfh', {'local_date': days_ago(-3), 'reason': 'x'}, **htmx())
    assert r.status_code == 422


# ---------------------------------------------------------------- list and cancel

def test_request_list_and_cancel(demo, reg):
    from hajiri.regularisation import services
    out = services.create_request(reg['sita'].user, {'localDate': workday_ago(2), 'kind': 'half_day', 'reason': 'doctor visit'})
    r = browser(reg['sita']).get('/_/regularisation/list', **htmx())
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Half day' in html and 'doctor visit' in html and f'/_/regularisation/{out["id"]}/cancel' in html
    r = browser(reg['ram']).get('/_/regularisation/list', **htmx())
    assert 'Sita Sharma' in r.content.decode()
    r = browser(reg['sita']).post(f'/_/regularisation/{out["id"]}/cancel', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Request cancelled.'
    r = browser(reg['sita']).post(f'/_/regularisation/{out["id"]}/cancel', **htmx())
    assert r.status_code == 409
    assert toasts(r)[0]['message'] == 'only pending requests can be cancelled (status cancelled)'
    r = browser(reg['sita']).get('/_/regularisation/list?scope=all', **htmx())
    assert r.status_code == 403


# ---------------------------------------------------------------- offline flush

def test_offline_flush(demo, reg):
    c = browser(reg['sita'])
    d = workday_ago(1)
    p = {'id': 'abc', 'direction': 'out', 'at': npt(d, '18:04'), 'localDate': d, 'userId': reg['sita'].user_id}
    r = c.post('/_/regularisation/offline', json.dumps(p), content_type='application/json', HTTP_ACCEPT='application/json',
               HTTP_ORIGIN='http://testserver')
    assert r.status_code == 201 and r.json()['status'] == 'pending'
    r = c.post('/_/regularisation/offline', json.dumps(p), content_type='application/json', HTTP_ACCEPT='application/json',
               HTTP_ORIGIN='http://testserver')
    assert r.status_code == 409
    assert r.json()['error'] == 'a pending request of this kind already exists for that day'


def test_offline_flush_url_is_on_every_page(reg):
    r = browser(reg['sita']).get('/')
    assert 'data-offline-flush-url="/_/regularisation/offline"' in r.content.decode()


def test_fix_a_punch_tab_is_in_the_attendance_hub(reg):
    html = browser(reg['ram']).get('/attendance/fix').content.decode()
    assert 'Fix a punch' in html
