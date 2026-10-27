"""
Leave cases from other TS test files: attendance-extras.test.ts (ordered and clubbed leave types), security-selfservice
(hourly leave), workflow-qa (filing for a non-report; balances and ledger visibility), security-authz (long ranges) and
roles-flows (specialist roles and leave adjustments).
"""
import pytest

from hajiri.attendance.service import recompute_day
from hajiri.core.db import execute, one
from hajiri.leave.service import ledger_balance
from tests.core.punches import insert_punch

from .helpers import Api, instance_of

L = '/api/v1/leave'


def _adjust(w, employee_id, code, days):
    r = w.hr.post(f'{L}/adjust', {'employeeId': employee_id, 'leaveTypeId': w.t(code), 'deltaDays': days, 'reason': 'opening balance',
                                  'kind': 'allocation', 'effectiveDate': '2026-07-20'})
    assert r.status_code == 201, r.json()


def _approve(w, request_id, api=None):
    r = (api or w.ram).post(f'/api/v1/workflow/{instance_of(request_id)}/approve', {})
    assert r.status_code == 200, r.json()


# ---------------------------------------------------------------- ordered and clubbed leave types (sandwich across requests)

def test_ordered_and_clubbed_types(world):
    w, sita, hr, t = world, world.sita, world.hr, world.t
    sita_id = w.demo['sita']
    # without the sandwich rule, the Saturday between two leaves is not charged
    _adjust(w, sita_id, 'HOME', 10)
    _adjust(w, sita_id, 'SICK', 10)
    a = sita.post(f'{L}/requests', {'leaveTypeId': t('SICK'), 'from': '2026-08-28', 'to': '2026-08-28', 'reason': 'fever'})
    assert a.status_code == 201, a.json()
    p = sita.get(f'{L}/preview', {'leaveTypeId': t('HOME'), 'from': '2026-08-30', 'to': '2026-08-30'})
    assert p.json()['bridge'] is None
    sita.post(f"{L}/requests/{a.json()['id']}/cancel", {})

    # the type first in the order absorbs the gap when its sandwich rule is on, and it is posted on approval
    hr.patch(f"{L}/types/{t('HOME')}", {'sandwichRule': True})
    a = sita.post(f'{L}/requests', {'leaveTypeId': t('SICK'), 'from': '2026-08-28', 'to': '2026-08-28', 'reason': 'fever'})
    assert a.status_code == 201
    _approve(w, a.json()['id'])
    b = sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-08-30', 'to': '2026-08-30', 'reason': 'trip'})
    assert b.status_code == 201, b.json()
    br = b.json()['bridge']
    assert (br['days'], br['typeId'], br['from'], br['to']) == (1, t('HOME'), '2026-08-29', '2026-08-29')
    home = next(x for x in sita.get(f'{L}/balances').json()['balances'] if x['code'] == 'HOME')
    assert home['pending'] == 2
    before = ledger_balance(sita_id, t('HOME'))
    _approve(w, b.json()['id'])
    assert ledger_balance(sita_id, t('HOME')) == before - 2
    # a second request touching the same gap does not charge it again
    again = sita.get(f'{L}/preview', {'leaveTypeId': t('HOME'), 'from': '2026-08-27', 'to': '2026-08-27'})
    assert again.json()['bridge'] is None
    # cancelling gives the bridged day back
    sita.post(f"{L}/requests/{b.json()['id']}/cancel", {})
    assert ledger_balance(sita_id, t('HOME')) == before

    # when the earlier type has the sandwich rule and comes first, the gap is charged to it
    _adjust(w, sita_id, 'CASUAL', 6)
    h = sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-09-04', 'to': '2026-09-04', 'reason': 'trip'})
    assert h.status_code == 201, h.json()
    p = sita.get(f'{L}/preview', {'leaveTypeId': t('CASUAL'), 'from': '2026-09-06', 'to': '2026-09-06'}).json()
    assert p['bridge']['days'] == 1 and p['bridge']['typeId'] == t('HOME')
    sita.post(f"{L}/requests/{h.json()['id']}/cancel", {})

    # a clubbed type follows its main type: CASUAL clubbed with HOME takes HOME's order and sandwich rule
    hr.patch(f"{L}/types/{t('HOME')}", {'sandwichRule': False})
    hr.patch(f"{L}/types/{t('CASUAL')}", {'sandwichRule': True})
    s = sita.post(f'{L}/requests', {'leaveTypeId': t('SICK'), 'from': '2026-09-11', 'to': '2026-09-11', 'reason': 'cold'})
    assert s.status_code == 201
    p1 = sita.get(f'{L}/preview', {'leaveTypeId': t('CASUAL'), 'from': '2026-09-13', 'to': '2026-09-13'})
    assert p1.json()['bridge'] is None
    club = hr.patch(f"{L}/types/{t('CASUAL')}", {'clubWithId': t('HOME')})
    assert club.status_code == 200
    hr.patch(f"{L}/types/{t('HOME')}", {'sandwichRule': True})
    p2 = sita.get(f'{L}/preview', {'leaveTypeId': t('CASUAL'), 'from': '2026-09-13', 'to': '2026-09-13'}).json()
    assert p2['bridge']['days'] == 1 and p2['bridge']['typeId'] == t('CASUAL')
    sita.post(f"{L}/requests/{s.json()['id']}/cancel", {})

    # rejects clubbing chains, self-clubbing and other companies' types; order can be set in one call; employees cannot
    assert hr.patch(f"{L}/types/{t('HOME')}", {'clubWithId': t('HOME')}).json()['error'] == 'a leave type cannot be clubbed with itself'
    assert hr.patch(f"{L}/types/{t('SICK')}", {'clubWithId': t('CASUAL')}).json()['error'] == \
        'that leave type is itself clubbed with another; club with the main type instead'
    assert hr.patch(f"{L}/types/{t('HOME')}", {'clubWithId': t('SICK')}).json()['error'] == \
        'other leave types are clubbed with this one, so it cannot follow another type'
    ord_ = hr.post(f'{L}/types/order', {'ids': [t('SICK'), t('HOME')]})
    assert ord_.status_code == 200
    types = hr.get(f'{L}/types').json()['types']
    so = {x['code']: x['sort_order'] for x in types}
    assert so['SICK'] < so['HOME']
    assert sita.post(f'{L}/types/order', {'ids': [t('HOME')]}).status_code == 403
    bad = hr.post(f'{L}/types/order', {'ids': ['00000000-0000-7000-8000-00000000dead']})
    assert bad.status_code == 400 and bad.json()['error'] == 'a leave type in the list does not exist'
    hr.patch(f"{L}/types/{t('CASUAL')}", {'clubWithId': None})
    assert one('SELECT club_with_id FROM leave_type WHERE id = %s', [t('CASUAL')])['club_with_id'] is None


# ---------------------------------------------------------------- hourly leave (security-selfservice.test.ts)

DAY = '2026-09-21'  # Monday, shift 10:00-18:00 (8 h)


def test_hourly_leave(world):
    w, sita, hr, ram = world, world.sita, world.hr, world.ram
    home, casual = w.t('HOME'), w.t('CASUAL')
    hr.post(f'{L}/adjust', {'employeeId': w.demo['sita'], 'leaveTypeId': home, 'deltaDays': 5, 'reason': 'opening balance', 'kind': 'allocation'})

    # home and sick leave allow hours by default; unpaid or event leave cannot
    types = hr.get(f'{L}/types').json()['types']
    by_code = {x['code']: x['allow_hourly'] for x in types}
    assert (by_code['HOME'], by_code['SICK'], by_code['CASUAL'], by_code['LWP']) == (True, True, False, False)
    lwp = next(x['id'] for x in types if x['code'] == 'LWP')
    r = hr.patch(f'{L}/types/{lwp}', {'allowHourly': True})
    assert r.status_code == 400 and r.json()['error'] == 'only paid leave with a balance can be taken by the hour'
    assert sita.patch(f'{L}/types/{casual}', {'allowHourly': True}).status_code == 403

    # previews hours as a fraction of the working day
    p = sita.get(f'{L}/preview', {'leaveTypeId': home, 'from': DAY, 'to': DAY, 'fromTime': '10:00', 'toTime': '12:00'})
    assert p.status_code == 200
    assert p.json()['days'] == 0.25 and p.json()['hours'] == 2
    assert '2 hour' in p.json()['explanation']
    assert p.json()['explanation'] == '2 hour(s) of a 8-hour working day = 0.25 day(s) will be deducted'

    # validates the window
    def send(b):
        return sita.post(f'{L}/requests', {'leaveTypeId': home, 'from': DAY, 'to': DAY, **b})
    assert send({'fromTime': '12:00', 'toTime': '11:00'}).status_code == 400
    assert 'within your shift' in send({'fromTime': '09:00', 'toTime': '11:00'}).json()['error']
    assert 'at least 30' in send({'fromTime': '10:00', 'toTime': '10:15'}).json()['error']
    assert 'full day' in send({'fromTime': '10:00', 'toTime': '18:00'}).json()['error']
    assert send({'fromTime': '10:00'}).status_code == 400
    assert send({'fromTime': '10:00', 'toTime': '11:00', 'halfDay': 'first_half'}).status_code == 400
    assert sita.post(f'{L}/requests', {'leaveTypeId': home, 'from': DAY, 'to': '2026-09-22', 'fromTime': '10:00', 'toTime': '11:00'}).status_code == 400
    assert 'by the hour' in send({'leaveTypeId': casual, 'fromTime': '10:00', 'toTime': '11:00'}).json()['error']
    sat = sita.post(f'{L}/requests', {'leaveTypeId': home, 'from': '2026-09-26', 'to': '2026-09-26', 'fromTime': '10:00', 'toTime': '11:00'})
    assert 'working day' in sat.json()['error']

    # is approved like any leave, deducts the fraction and excuses the late arrival
    insert_punch(w.demo['sita'], DAY, '12:05', key_prefix='hl')
    insert_punch(w.demo['sita'], DAY, '18:00', key_prefix='hl')
    recompute_day(w.demo['sita'], DAY)
    before = one('SELECT late_min, status FROM v_attendance_latest WHERE employee_id = %s AND local_date = %s', [w.demo['sita'], DAY])
    assert before['late_min'] == 110
    r = sita.post(f'{L}/requests', {'leaveTypeId': home, 'from': DAY, 'to': DAY, 'fromTime': '10:00', 'toTime': '12:00', 'reason': 'bank'})
    assert r.status_code == 201, r.json()
    assert r.json()['days'] == 0.25
    request_id = r.json()['id']
    items = ram.get('/api/v1/workflow/inbox').json()['items']
    item = next(i for i in items if i['objectId'] == request_id)
    s = item['summary']
    assert (s['fromTime'], s['toTime'], s['hours'], s['days']) == ('10:00', '12:00', 2, 0.25)
    # a second request overlapping the same day is refused
    assert sita.post(f'{L}/requests', {'leaveTypeId': home, 'from': DAY, 'to': DAY, 'fromTime': '14:00', 'toTime': '15:00'}).status_code == 409
    act = ram.post(f"/api/v1/workflow/{item['instanceId']}/approve", {})
    assert act.status_code == 200
    assert ledger_balance(w.demo['sita'], home) == pytest.approx(4.75, abs=1e-5)
    recompute_day(w.demo['sita'], DAY)
    after = one('SELECT late_min, status, flags FROM v_attendance_latest WHERE employee_id = %s AND local_date = %s', [w.demo['sita'], DAY])
    assert after['late_min'] == 0 and after['status'] == 'Present'
    assert {'kind': 'ShortLeave', 'minutes': 120} in after['flags']
    lst = sita.get(f'{L}/requests', {'scope': 'own'}).json()['requests']
    mine = next(x for x in lst if x['id'] == request_id)
    assert (mine['from_time'], mine['to_time'], mine['hours']) == ('10:00', '12:00', '2.00')
    cal = sita.get(f'{L}/team-calendar', {'from': DAY, 'to': DAY}).json()
    off = cal['days'][0]['off'][0]
    assert (off['fromTime'], off['toTime']) == ('10:00', '12:00')

    # cancelling an approved hourly leave gives the fraction back
    assert sita.post(f'{L}/requests/{request_id}/cancel', {}).status_code == 200
    assert ledger_balance(w.demo['sita'], home) == pytest.approx(5, abs=1e-5)


# ---------------------------------------------------------------- authorisation (workflow-qa.test.ts, security-authz.test.ts)

@pytest.fixture
def others(world):
    """A manager who is not Sita's manager, and HR of a second company."""
    mgr = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E900', 'Other Manager') RETURNING id", [world.company])['id'])
    execute("INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')", [mgr])
    world.other_mgr = Api(world.login_as(['manager'], mgr).token)
    c2 = str(one("INSERT INTO company (code, name_en) VALUES ('OTHER', 'Other Co') RETURNING id")['id'])
    world.foreign_emp = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Foreign') RETURNING id", [c2])['id'])
    world.foreign = Api(world.login_as(['hr'], company_id=c2).token)
    return world


def test_admin_cannot_approve_own_request_manager_cannot_file_for_non_report(others):
    w = others
    admin = w.login_as(['admin'])
    admin_emp = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E904', 'Admin Person') RETURNING id", [w.company])['id'])
    execute("INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')", [admin_emp])
    execute('UPDATE app_user SET employee_id = %s WHERE id = %s', [admin_emp, admin.user_id])
    api = Api(admin.token)
    r = api.post(f'{L}/requests', {'leaveTypeId': w.t('LWP'), 'from': '2026-11-12', 'to': '2026-11-12'})
    assert r.status_code == 201, r.json()
    assert api.post(f"/api/v1/workflow/{instance_of(r.json()['id'])}/approve", {}).status_code == 403
    execute('UPDATE app_user SET employee_id = NULL WHERE id = %s', [admin.user_id])
    no = w.other_mgr.post(f'{L}/requests', {'employeeId': w.demo['sita'], 'leaveTypeId': w.t('LWP'), 'from': '2026-11-13', 'to': '2026-11-13'})
    assert no.status_code == 403 and no.json()['error'] == 'not allowed to act for this employee'
    assert w.ram.post(f'{L}/requests', {'employeeId': w.demo['sita'], 'leaveTypeId': w.t('LWP'), 'from': '2026-11-13',
                                        'to': '2026-11-13'}).status_code == 201


def test_balances_ledger_visibility(others):
    w = others
    assert w.sita.get(f'{L}/balances', {'employeeId': w.demo['ram']}).status_code == 403
    assert w.sita.get(f'{L}/ledger', {'employeeId': w.demo['ram']}).status_code == 403
    assert w.other_mgr.get(f'{L}/balances', {'employeeId': w.demo['sita']}).status_code == 403
    assert w.ram.get(f'{L}/balances', {'employeeId': w.demo['sita']}).status_code == 200
    r = w.foreign.get(f'{L}/balances', {'employeeId': w.demo['sita']})
    assert r.status_code == 404 and r.json()['error'] == 'employee not found'
    assert w.hr.get(f'{L}/balances', {'employeeId': w.foreign_emp}).status_code == 404
    assert w.foreign.get(f'{L}/ledger', {'employeeId': w.demo['sita']}).status_code == 404


def test_very_long_date_ranges_are_refused(world):
    sick = next(x for x in world.sita.get(f'{L}/types').json()['types'] if x['code'] == 'SICK')
    r = world.sita.get(f'{L}/preview', {'leaveTypeId': sick['id'], 'from': '0001-01-01', 'to': '9999-12-31'})
    assert r.status_code == 400


# ---------------------------------------------------------------- specialist roles (roles-flows.test.ts)

def test_specialist_roles_and_leave_adjustments(world):
    w = world
    someone = str(one("INSERT INTO employee (company_id, emp_code, name_en, join_date) VALUES (%s, 'RF01', 'Someone', '2025-01-01') RETURNING id",
                      [w.company])['id'])
    body = {'employeeId': someone, 'leaveTypeId': w.t('HOME'), 'deltaDays': 1, 'reason': 'opening balance'}
    payroll = Api(w.login_as(['payroll_admin']).token)
    attendance = Api(w.login_as(['attendance_admin']).token)
    executive = Api(w.login_as(['executive']).token)
    r = payroll.post(f'{L}/adjust', {**body, 'reason': 'test'})
    assert r.status_code == 403 and r.json()['error'] == 'requires role hr or attendance_admin'
    assert attendance.post(f'{L}/adjust', body).status_code < 300
    assert attendance.get(f'{L}/requests', {'scope': 'all'}).status_code == 200
    assert executive.post(f'{L}/adjust', {**body, 'reason': 'x'}).status_code == 403
    r = w.sita.get(f'{L}/requests', {'scope': 'all'})
    assert r.status_code == 403 and r.json()['error'] == 'scope all requires HR'
