"""
The leave JSON API as the React app calls it (frontend/src/api/index.js `leave`, attendanceTools.js `leaveOrder`):
every path and method, the JSON field names and value kinds the pages read (numeric columns as strings like the pg
driver returned them, computed numbers as JSON numbers, dates as 'YYYY-MM-DD'), the status codes and the error shape
of the TS routes (modules/leave/routes.ts at 65a9079), the cookie session, and company isolation.
"""
from __future__ import annotations

import json
import re

import pytest
from django.test import Client

from hajiri.core.auth import SESSION_COOKIE
from tests.support.sql import execute

L = '/api/v1/leave'
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
GONE = '00000000-0000-7000-8000-00000000dead'


def _home(w, days=10):
    r = w.hr.post(f'{L}/adjust', {'employeeId': w.demo['sita'], 'leaveTypeId': w.t('HOME'), 'deltaDays': days,
                                  'reason': 'opening balance', 'kind': 'allocation'})
    assert r.status_code == 201, r.json()
    return r


# ---------------------------------------------------------------- every endpoint the React app calls is routed

def test_unauthenticated_calls_are_401_with_the_ts_error_shape(db):
    c = Client()
    for method, path in (('get', '/types'), ('post', '/types'), ('patch', f'/types/{GONE}'), ('post', '/types/order'),
                         ('get', '/balances'), ('get', '/preview'), ('get', '/requests'), ('post', '/requests'),
                         ('post', f'/requests/{GONE}/cancel'), ('post', '/accrual/run'), ('post', '/adjust'),
                         ('get', '/ledger'), ('get', '/team-calendar')):
        r = getattr(c, method)(f'{L}{path}', content_type='application/json') if method != 'get' else c.get(f'{L}{path}')
        assert r.status_code == 401, (method, path, r.status_code)
        assert isinstance(r.json()['error'], str)


# ---------------------------------------------------------------- types

def test_types_list_create_patch_shapes(world):
    types = world.sita.get(f'{L}/types')
    assert types.status_code == 200
    home = next(t for t in types.json()['types'] if t['code'] == 'HOME')
    # snake_case columns as the pg rows; numeric(…) columns as strings (LeaveTypesPage reads these)
    for k in ('id', 'company_id', 'code', 'name_en', 'paid', 'statutory', 'accrual_basis', 'accrual_days', 'carry_forward',
              'encashable', 'allow_half_day', 'gender_scope', 'sandwich_rule', 'sort_order', 'allow_hourly', 'event_days',
              'max_balance', 'negative_balance_limit', 'club_with_id', 'accrual_per_worked_days'):
        assert k in home, k
    assert isinstance(home['accrual_days'], str) and isinstance(home['sort_order'], int) and isinstance(home['paid'], bool)

    assert world.sita.post(f'{L}/types', {'code': 'X', 'nameEn': 'X'}).status_code == 403
    bad = world.hr.post(f'{L}/types', {'code': '', 'nameEn': 'X'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert any(i['path'] == 'code' for i in bad.json()['issues'])
    made = world.hr.post(f'{L}/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'accrualDays': 1.5, 'accrualBasis': 'monthly'})
    assert made.status_code == 201 and set(made.json()) == {'id'}
    row = next(t for t in world.hr.get(f'{L}/types').json()['types'] if t['id'] == made.json()['id'])
    assert row['accrual_days'] == '1.50' and row['sort_order'] == 100 and row['paid'] is True

    ok = world.hr.patch(f"{L}/types/{made.json()['id']}", {'nameEn': 'Study'})
    assert ok.status_code == 200 and ok.json() == {'ok': True}
    assert world.hr.patch(f'{L}/types/{GONE}', {'nameEn': 'Y'}).status_code == 404
    assert world.hr.patch(f'{L}/types/not-a-uuid', {'nameEn': 'Y'}).status_code == 400
    assert world.sita.patch(f"{L}/types/{made.json()['id']}", {'nameEn': 'Z'}).status_code == 403
    locked = world.hr.patch(f"{L}/types/{world.t('SICK')}", {'accrualDays': 99})
    assert locked.status_code == 400 and 'locked' in locked.json()['error']


def test_types_order(world):
    r = world.hr.post(f'{L}/types/order', {'ids': [world.t('SICK'), world.t('HOME')]})
    assert r.status_code == 200 and r.json() == {'ok': True}
    by_code = {t['code']: t['sort_order'] for t in world.hr.get(f'{L}/types').json()['types']}
    assert (by_code['SICK'], by_code['HOME']) == (10, 20)
    assert world.hr.post(f'{L}/types/order', {'ids': []}).json()['error'] == 'validation'


# ---------------------------------------------------------------- balances, preview, requests

def test_balances_preview_request_list_cancel_shapes(world):
    _home(world)
    bal = world.sita.get(f'{L}/balances')
    assert bal.status_code == 200 and bal.json()['employeeId'] == world.demo['sita']
    home = next(b for b in bal.json()['balances'] if b['code'] == 'HOME')
    for k in ('leaveTypeId', 'code', 'nameEn', 'nameNe', 'paid', 'encashable', 'opening', 'accrued', 'taken', 'cancelled',
              'lapsed', 'carried', 'adjustments', 'compOff', 'encashed', 'pending', 'balance', 'available', 'fiscalYear',
              'explanation'):
        assert k in home, k
    assert home['balance'] == 10 and isinstance(home['available'], (int, float))

    q = {'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-22'}
    pv = world.sita.get(f'{L}/preview', q)
    assert pv.status_code == 200
    p = pv.json()
    assert p['days'] == 2 and p['balance'] == 10 and p['available'] == 10 and p['balanceAfter'] == 8
    assert 'explanation' in p and 'sandwichApplied' in p and 'bridge' in p
    assert world.sita.get(f'{L}/preview', {'from': '2026-09-21', 'to': '2026-09-22'}).json()['error'] == 'validation'
    assert world.sita.get(f'{L}/preview', {**q, 'leaveTypeId': GONE}).status_code == 404

    made = world.sita.post(f'{L}/requests', {**q, 'reason': 'family'})
    assert made.status_code == 201, made.json()
    assert set(made.json()) == {'id', 'days', 'explanation', 'sandwichApplied', 'bridge', 'teamOverlap'}
    assert made.json()['days'] == 2 and made.json()['teamOverlap'] == []
    back = world.sita.post(f'{L}/requests', {**q, 'from': '2026-09-23', 'to': '2026-09-22'})
    assert back.status_code == 400 and back.json()['error'] == 'to must be on or after from'
    clash = world.sita.post(f'{L}/requests', q)
    assert clash.status_code == 409

    listed = world.sita.get(f'{L}/requests')
    assert listed.status_code == 200
    row = listed.json()['requests'][0]
    assert list(row) == ['id', 'employee_id', 'emp_code', 'name_en', 'name_ne', 'leave_type_id', 'type_code', 'type_name',
                         'paid', 'from_date', 'to_date', 'half_day', 'days_requested', 'reason', 'attachment_key', 'status',
                         'workflow_instance_id', 'created_at', 'decided_at', 'from_time', 'to_time', 'hours',
                         'bridge_days', 'bridge_type_id']
    assert row['id'] == made.json()['id'] and row['type_code'] == 'HOME' and row['status'] == 'pending'
    assert row['days_requested'] == '2.00' and ISO.match(row['from_date']) and ISO.match(row['to_date'])
    assert row['created_at'].endswith('Z') and row['decided_at'] is None
    assert world.sita.get(f'{L}/requests', {'scope': 'all'}).status_code == 403
    assert world.sita.get(f'{L}/requests', {'status': 'bogus'}).json()['error'] == 'validation'
    assert world.hr.get(f'{L}/requests', {'status': 'pending'}).json()['requests'][0]['id'] == made.json()['id']

    # the React client sends {} when no reason is given (JSON.stringify drops undefined)
    cancel = world.sita.post(f"{L}/requests/{made.json()['id']}/cancel", {})
    assert cancel.status_code == 200 and cancel.json() == {'ok': True}
    again = world.sita.post(f"{L}/requests/{made.json()['id']}/cancel", {})
    assert again.status_code == 409 and again.json()['error'] == 'request is already cancelled'
    assert world.sita.post(f'{L}/requests/{GONE}/cancel', {}).status_code == 404


def test_shortfall_answers_error_and_suggestion(world):
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-23'})
    assert r.status_code == 400
    assert set(r.json()) == {'error', 'suggestion'} and r.json()['suggestion'] == {'paidDays': 0, 'lwpDays': 3}


# ---------------------------------------------------------------- adjust, ledger, accrual, team calendar

def test_adjust_and_ledger_shapes(world):
    adj = _home(world, 2.5)
    assert set(adj.json()) == {'id', 'balance', 'replacedAutomatic'} and adj.json()['balance'] == 2.5
    assert world.sita.post(f'{L}/adjust', {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'),
                                           'deltaDays': 1, 'reason': 'mine'}).status_code == 403
    zero = world.hr.post(f'{L}/adjust', {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'), 'deltaDays': 0,
                                         'reason': 'nothing'})
    assert zero.status_code == 400 and zero.json()['issues'][0]['message'] == 'deltaDays cannot be 0'
    as_text = world.hr.post(f'{L}/adjust', {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'), 'deltaDays': '2',
                                            'reason': 'text'})
    assert as_text.status_code == 400 and as_text.json()['error'] == 'validation'

    led = world.hr.get(f'{L}/ledger', {'employeeId': world.demo['sita']})
    assert led.status_code == 200
    line = next(x for x in led.json()['ledger'] if x['id'] == adj.json()['id'])
    assert set(line) == {'id', 'leave_type_id', 'code', 'posted_at', 'effective_date', 'delta_days', 'reason', 'ref_type',
                         'ref_id', 'expires_on', 'note'}
    assert line['delta_days'] == '2.50' and line['code'] == 'HOME' and line['reason'] == 'allocation'
    assert line['ref_type'] == 'hr_adjustment' and line['note'] == 'opening balance' and ISO.match(line['effective_date'])
    assert world.sita.get(f'{L}/ledger').json()['ledger'][0]['id'] == adj.json()['id']
    assert world.hr.get(f'{L}/ledger').status_code == 400        # HR without an employee record names one
    assert world.hr.get(f'{L}/ledger', {'employeeId': GONE}).status_code == 404


def test_accrual_run_shape(world):
    assert world.sita.post(f'{L}/accrual/run').status_code == 403
    r = world.hr.post(f'{L}/accrual/run')
    assert r.status_code == 200 and set(r.json()) == {'postings', 'months', 'annualPostings'}
    assert isinstance(r.json()['months'], list)


def test_team_calendar_shape(world):
    _home(world)
    world.sita.post(f'{L}/requests', {'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-22'})
    cal = world.ram.get(f'{L}/team-calendar', {'from': '2026-09-22', 'to': '2026-09-30'})
    assert cal.status_code == 200
    assert cal.json()['days'] == [{'date': '2026-09-22', 'off': [
        {'employeeId': world.demo['sita'], 'empCode': cal.json()['days'][0]['off'][0]['empCode'], 'nameEn':
         cal.json()['days'][0]['off'][0]['nameEn'], 'type': 'HOME', 'status': 'pending', 'halfDay': None}]}]
    assert world.ram.get(f'{L}/team-calendar', {'from': '2026-09-22'}).json()['error'] == 'validation'


# ---------------------------------------------------------------- the cookie session the React app uses

def test_cookie_session_reads_and_writes(world):
    c = Client()
    c.cookies[SESSION_COOKIE] = world.sita.h['HTTP_AUTHORIZATION'][len('Bearer '):]
    assert c.get(f'{L}/types').status_code == 200
    r = c.post(f'{L}/requests', json.dumps({'leaveTypeId': world.t('LWP'), 'from': '2026-10-05', 'to': '2026-10-05'}),
               content_type='application/json')
    assert r.status_code == 201, r.json()


# ---------------------------------------------------------------- company isolation

@pytest.fixture
def outsider(world, login_as):
    """HR of a second company (own types), signed in there."""
    from tests.web.factories import make_company
    other = make_company('OTHER', 'Other Co')
    execute("INSERT INTO leave_type (company_id, code, name_en, paid, statutory, accrual_basis, accrual_days, carry_forward,"
            " encashable, allow_half_day, min_service_days, negative_balance_limit, gender_scope, sandwich_rule, sort_order,"
            " allow_hourly) VALUES (%s, 'OWN', 'Own leave', true, false, 'none', 0, true, false, true, 0, 0, 'all', false, 1,"
            " false)", [str(other.id)])
    from tests.leave.helpers import Api
    return Api(login_as(['hr'], company_id=str(other.id)).token)


def test_another_companys_leave_is_not_found_or_hidden(world, outsider):
    _home(world)
    made = world.sita.post(f'{L}/requests', {'leaveTypeId': world.t('HOME'), 'from': '2026-09-21', 'to': '2026-09-21'})
    rid, sita, home = made.json()['id'], world.demo['sita'], world.t('HOME')
    assert [t['code'] for t in outsider.get(f'{L}/types').json()['types']] == ['OWN']
    assert outsider.get(f'{L}/requests').json()['requests'] == []
    assert outsider.get(f'{L}/team-calendar', {'from': '2026-09-01', 'to': '2026-09-30'}).json()['days'] == []
    assert outsider.patch(f'{L}/types/{home}', {'nameEn': 'Mine now'}).status_code == 404
    assert outsider.post(f'{L}/requests/{rid}/cancel', {}).status_code == 404
    assert outsider.get(f'{L}/balances', {'employeeId': sita}).status_code == 404
    assert outsider.get(f'{L}/ledger', {'employeeId': sita}).status_code == 404
    assert outsider.get(f'{L}/requests', {'employeeId': sita}).status_code == 404
    assert outsider.get(f'{L}/preview', {'employeeId': sita, 'leaveTypeId': home, 'from': '2026-09-28',
                                         'to': '2026-09-28'}).status_code == 404
    assert outsider.post(f'{L}/requests', {'employeeId': sita, 'leaveTypeId': home, 'from': '2026-09-28',
                                           'to': '2026-09-28'}).status_code == 404
    assert outsider.post(f'{L}/adjust', {'employeeId': sita, 'leaveTypeId': home, 'deltaDays': 5,
                                         'reason': 'gift'}).status_code == 404
    assert outsider.post(f'{L}/types/order', {'ids': [home]}).status_code == 400
    assert world.sita.get(f'{L}/requests').json()['requests'][0]['status'] == 'pending'
