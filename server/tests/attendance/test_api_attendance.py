"""HTTP port of the /api/v1/attendance assertions of apps/api/test/attendance.test.ts, auth.test.ts and
endpoints.test.ts: recompute (one day and a range), days, punches, the exceptions queue and Team Today, with manager
scope and company isolation."""
import json

import pytest
from django.test import Client

from tests.core.fixtures import bearer
from tests.core.punches import insert_punch
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import execute, one

A = '/api/v1/attendance'


def post(path, token, body=None, raw=None):
    data = raw if raw is not None else json.dumps(body or {})
    return Client().post(path, data, content_type='application/json', **bearer(token))


def get(path, token, **q):
    return Client().get(path, q, **bearer(token))


@pytest.fixture
def tok(demo, login_as):
    return {'hr': login_as(['hr']).token, 'sita': login_as(['employee'], demo['sita']).token,
            'ram': login_as(['manager'], demo['ram']).token, 'exec': login_as(['executive']).token,
            'att': login_as(['attendance_admin']).token}


def test_recompute_stores_versions_and_days_lists_the_latest(demo, tok):
    insert_punch(demo['sita'], '2026-09-21', '10:17')
    insert_punch(demo['sita'], '2026-09-21', '18:40')
    first = post(f'{A}/recompute', tok['hr'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'})
    assert first.status_code == 200
    body = first.json()
    assert body['changed'] is True and body['version'] == 1 and body['locked'] is False
    assert body['day']['status'] == 'Present' and body['day']['workedMin'] == 503 and body['day']['lateMin'] == 2
    assert body['day']['flags'] == [{'kind': 'Late', 'minutes': 2}]
    again = post(f'{A}/recompute', tok['hr'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'}).json()
    assert again['changed'] is False and again['version'] == 1
    insert_punch(demo['sita'], '2026-09-21', '19:00')
    third = post(f'{A}/recompute', tok['hr'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'}).json()
    assert third['version'] == 2
    assert any(f['kind'] == 'UnpairedPunch' and f.get('punchId') for f in third['day']['flags'])
    days = get(f'{A}/days', tok['hr'], employeeId=demo['sita'], **{'from': '2026-09-21', 'to': '2026-09-21'})
    assert days.status_code == 200
    assert len(days.json()['days']) == 1
    d = days.json()['days'][0]
    assert (d['version'], d['bs_date'], d['status']) == (2, '2083-06-05', 'Present')
    # The attendance admin may recompute too.
    assert post(f'{A}/recompute', tok['att'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'}).status_code == 200


def test_recompute_validates_input_and_roles(demo, tok):
    bad = post(f'{A}/recompute', tok['hr'], {'employeeId': 'nope', 'localDate': '2026-09-21'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert post(f'{A}/recompute', tok['hr'], {'employeeId': '00000000-0000-7000-8000-0000000000ff',
                                              'localDate': '2026-09-21'}).json() == {'error': 'employee not found'}
    broken = post(f'{A}/recompute', tok['hr'], raw='{"broken"')
    assert broken.status_code == 400 and broken.json() == {'error': 'invalid JSON body'}
    refused = post(f'{A}/recompute', tok['sita'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'})
    assert refused.status_code == 403
    assert refused.json() == {'error': 'requires role hr or attendance_admin or manager'}
    assert Client().post(f'{A}/recompute', '{}', content_type='application/json').status_code == 401


def test_recompute_range_inline_queued_and_refused(demo, tok):
    small = post(f'{A}/recompute-range', tok['hr'], {'employeeIds': [demo['sita']], 'from': '2026-09-01', 'to': '2026-09-03'})
    assert small.status_code == 200 and small.json() == {'queued': 0, 'recomputed': 3}
    big = post(f'{A}/recompute-range', tok['hr'], {'employeeIds': [demo['sita'], demo['ram']], 'from': '2026-09-01', 'to': '2026-10-02'})
    assert big.json() == {'queued': 64, 'recomputed': 0}
    empty = post(f'{A}/recompute-range', tok['hr'], {'employeeIds': [demo['sita']], 'from': '2026-09-03', 'to': '2026-09-01'})
    assert empty.status_code == 400 and empty.json() == {'error': 'range must be 1 to 62 days'}
    assert post(f'{A}/recompute-range', tok['hr'], {'employeeIds': [], 'from': '2026-09-01', 'to': '2026-09-01'}).status_code == 400
    assert post(f'{A}/recompute-range', tok['ram'], {'employeeIds': [demo['sita']], 'from': '2026-09-01',
                                                     'to': '2026-09-01'}).json() == {'error': 'requires role hr or attendance_admin'}


def test_punches_for_self_manager_and_hr(demo, tok):
    insert_punch(demo['sita'], '2026-09-01', '10:05')
    r = get(f'{A}/punches', tok['sita'], employeeId=demo['sita'], **{'from': '2026-09-01', 'to': '2026-09-02'})
    assert r.status_code == 200
    p = r.json()['punches']
    assert len(p) == 1 and p[0]['local_date'] == '2026-09-01' and p[0]['superseded'] is False
    assert set(p[0]) == {'id', 'punched_at', 'local_date', 'direction', 'verify_mode', 'source', 'device_sn', 'supersedes_id',
                         'superseded'}
    assert p[0]['punched_at'].endswith('Z')
    other = get(f'{A}/punches', tok['sita'], employeeId=demo['ram'], **{'from': '2026-09-01', 'to': '2026-09-02'})
    assert other.status_code == 403 and other.json() == {'error': 'not allowed to access this employee'}
    assert get(f'{A}/punches', tok['sita'], employeeId=demo['sita']).status_code == 400
    assert get(f'{A}/days', tok['ram'], employeeId=demo['sita'], **{'from': '2026-09-01', 'to': '2026-09-02'}).status_code == 403
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    assert len(get(f'{A}/punches', tok['ram'], employeeId=demo['sita'],
                   **{'from': '2026-09-01', 'to': '2026-09-02'}).json()['punches']) == 1
    assert get(f'{A}/days', tok['sita'], employeeId=demo['sita'], **{'from': '2026-09-01', 'to': '2026-09-02'}).json() == {'days': []}


def test_exceptions_queue_for_hr_and_a_managers_team(demo, tok):
    insert_punch(demo['sita'], '2026-09-29', '10:00')
    post(f'{A}/recompute', tok['hr'], {'employeeId': demo['sita'], 'localDate': '2026-09-29'})
    r = get(f'{A}/exceptions', tok['hr'], **{'from': '2026-09-29', 'to': '2026-09-29'})
    assert r.status_code == 200
    ex = r.json()['exceptions']
    assert [(e['emp_code'], e['status']) for e in ex] == [('E001', 'Incomplete')]
    assert {'id', 'employee_id', 'name_en', 'local_date', 'bs_date', 'worked_min', 'flags'} <= set(ex[0])
    assert get(f'{A}/exceptions', tok['hr'], status='Absent', **{'from': '2026-09-29', 'to': '2026-09-29'}).json() == {'exceptions': []}
    assert get(f'{A}/exceptions', tok['ram'], **{'from': '2026-09-29', 'to': '2026-09-29'}).json() == {'exceptions': []}
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    assert len(get(f'{A}/exceptions', tok['ram'], **{'from': '2026-09-29', 'to': '2026-09-29'}).json()['exceptions']) == 1
    assert get(f'{A}/exceptions', tok['sita'], **{'from': '2026-09-29', 'to': '2026-09-29'}).status_code == 403
    assert get(f'{A}/exceptions', tok['hr']).status_code == 400


def test_team_today(demo, tok):
    insert_punch(demo['sita'], '2026-09-21', '10:00')
    post(f'{A}/recompute', tok['hr'], {'employeeId': demo['sita'], 'localDate': '2026-09-21'})
    t = get(f'{A}/today', tok['hr'], date='2026-09-21')
    assert t.status_code == 200
    assert t.json()['date'] == '2026-09-21'
    assert [(r['emp_code'], r['status']) for r in t.json()['rows']] == [('E001', 'Incomplete'), ('E002', None)]
    assert {'employee_id', 'first_in', 'last_out', 'late_min', 'worked_min', 'remote', 'photo_id', 'on_break', 'break_min',
            'field_place'} <= set(t.json()['rows'][0])
    assert [r['emp_code'] for r in get(f'{A}/today', tok['ram'], date='2026-09-21').json()['rows']] == ['E002']
    assert len(get(f'{A}/today', tok['exec'], date='2026-09-21').json()['rows']) == 2
    assert get(f'{A}/today', tok['sita'], date='2026-09-21').status_code == 403
    assert get(f'{A}/today', tok['hr'], date='21-09-2026').status_code == 400


def test_another_companys_employees_are_not_found(world):
    beta_emp = world.dual_beta_employee.id
    alpha, _ = world.session(world.admin_a)                 # group admin working in Alpha
    outsider, _ = world.session(world.admin_c)
    q = {'employeeId': str(beta_emp), 'from': '2026-09-01', 'to': '2026-09-02'}
    for token in (alpha, outsider):
        assert get(f'{A}/days', token, **q).json() == {'error': 'employee not found'}
        assert get(f'{A}/punches', token, **q).status_code == 404
        assert post(f'{A}/recompute', token, {'employeeId': str(beta_emp), 'localDate': '2026-09-01'}).status_code == 404
        r = post(f'{A}/recompute-range', token, {'employeeIds': [str(beta_emp)], 'from': '2026-09-01', 'to': '2026-09-01'})
        assert r.status_code == 403 and r.json() == {'error': 'some employees are not in your company'}
        names = [x['name_en'] for x in get(f'{A}/today', token, date='2026-09-01').json()['rows']]
        assert 'Dipak at Beta' not in names
    beta, _ = world.session(world.admin_b)
    assert get(f'{A}/days', beta, **q).status_code == 200
    assert 'Dipak at Beta' in [x['name_en'] for x in get(f'{A}/today', beta, date='2026-09-01').json()['rows']]
    switched, _ = world.session(world.admin_a, world.beta)
    assert get(f'{A}/days', switched, **q).status_code == 200


def test_cookie_requests_from_the_react_app_work_and_other_sites_are_refused(demo, login_as):
    from tests.web.factories import make_user, sign_in
    from hajiri.db.models import Company
    c = Client()
    sign_in(c, make_user(Company.objects.get(pk=demo['company_id']), roles=['hr']))
    body = json.dumps({'employeeId': demo['sita'], 'localDate': '2026-09-21'})
    ok = c.post(f'{A}/recompute', body, content_type='application/json', HTTP_ORIGIN='http://testserver')
    assert ok.status_code == 200
    evil = c.post(f'{A}/recompute', body, content_type='application/json', HTTP_ORIGIN='http://evil.example')
    assert evil.status_code == 403 and evil.json() == {'error': 'cross-site request refused'}
    assert c.get(f'{A}/today', {'date': '2026-09-21'}, HTTP_ORIGIN='http://evil.example').status_code == 200
    assert one('SELECT count(*)::int AS n FROM attendance_day')['n'] >= 1
