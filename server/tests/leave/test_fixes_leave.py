"""Port of apps/api/test/fixes-leave.test.ts: event-based statutory leave, annual allocation catch-up, leave over worked days."""
import re

import pytest

from tests.support.sql import execute, one
from hajiri.leave.accrual import allocate_annual_leave
from hajiri.leave.service import ledger_balance

from .helpers import Api, attendance

L = '/api/v1/leave'


def new_employee(company_id: str, code: str, gender: str, join_date: str | None) -> str:
    return str(one("INSERT INTO employee (company_id, emp_code, name_en, gender, join_date) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                   [company_id, code, f'Test {code}', gender, join_date])['id'])


@pytest.fixture
def fx(world):
    """As the TS beforeAll: Ram signs in as a plain employee, plus a stranger with no employee record."""
    execute("UPDATE app_user SET roles = '{employee}' WHERE id = %s", [world.ram_user_id])
    world.stranger = Api(world.login_as(['employee']).token)
    return world


def _code(balances, code):
    return next(b for b in balances['balances'] if b['code'] == code)


# ---------------------------------------------------------------- event-based statutory leave (Labour Act Sec 45-47)

def test_maternity_sixty_paid_days_per_event_rest_as_special_unpaid(fx):
    t = fx.t
    assert _code(fx.sita.get(f'{L}/balances').json(), 'MATERNITY')['available'] == 60
    too_long = fx.sita.post(f'{L}/requests', {'leaveTypeId': t('MATERNITY'), 'from': '2026-11-23', 'to': '2027-01-22'})
    assert too_long.status_code == 400
    assert re.search(r'60 day\(s\) per event', too_long.json()['error'])
    assert too_long.json()['suggestion']['remainderLeaveTypeId'] == t('SPECIAL_UNPAID')
    paid = fx.sita.post(f'{L}/requests', {'leaveTypeId': t('MATERNITY'), 'from': '2026-11-23', 'to': '2027-01-21'})
    assert paid.status_code == 201, paid.json()
    assert paid.json()['days'] == 60
    assert _code(fx.sita.get(f'{L}/balances').json(), 'MATERNITY')['available'] == 0
    # A second maternity request for the same event is refused; the remaining 38 days go in as special unpaid leave.
    more = fx.sita.post(f'{L}/requests', {'leaveTypeId': t('MATERNITY'), 'from': '2027-01-22', 'to': '2027-01-25'})
    assert more.status_code == 400
    assert '60 are already requested' in more.json()['error']
    rest = fx.sita.post(f'{L}/requests', {'leaveTypeId': t('SPECIAL_UNPAID'), 'from': '2027-01-22', 'to': '2027-02-28'})
    assert rest.status_code == 201, rest.json()
    assert rest.json()['days'] > 30


def test_maternity_care_and_mourning_for_men_capped_per_event(fx):
    t = fx.t
    assert fx.ram.post(f'{L}/requests', {'leaveTypeId': t('MATERNITY'), 'from': '2026-11-23', 'to': '2026-11-24'}).status_code == 400
    assert fx.ram.post(f'{L}/requests', {'leaveTypeId': t('PATERNITY'), 'from': '2026-11-23', 'to': '2026-12-08'}).status_code == 400  # 16 days
    care = fx.ram.post(f'{L}/requests', {'leaveTypeId': t('PATERNITY'), 'from': '2026-11-23', 'to': '2026-12-07'})
    assert care.status_code == 201, care.json()
    assert care.json()['days'] == 15
    mourning = fx.ram.post(f'{L}/requests', {'leaveTypeId': t('MOURNING'), 'from': '2027-03-01', 'to': '2027-03-13'})
    assert mourning.status_code == 201, mourning.json()
    assert mourning.json()['days'] == 13
    preview = fx.ram.get(f'{L}/preview', {'leaveTypeId': t('MOURNING'), 'from': '2027-03-14', 'to': '2027-03-15'}).json()
    assert preview['available'] == 0 and preview['balanceAfter'] == -2


def test_statutory_event_entitlements_cannot_be_edited(fx):
    assert fx.hr.patch(f"{L}/types/{fx.t('MATERNITY')}", {'eventDays': 90}).status_code == 400


# ---------------------------------------------------------------- annual allocation catch-up

def test_joiner_after_shrawan_gets_prorated_casual_once(fx):
    emp = new_employee(fx.company, 'J01', 'male', '2026-09-01')
    b = fx.hr.get(f'{L}/balances', {'employeeId': emp}).json()
    casual = _code(b, 'CASUAL')
    assert casual['accrued'] == 5 and casual['balance'] == 5
    fx.hr.get(f'{L}/balances', {'employeeId': emp})
    assert ledger_balance(emp, fx.t('CASUAL')) == 5


def test_hr_catch_up_grants_full_year_idempotently_respecting_opening(fx):
    veteran = new_employee(fx.company, 'V01', 'female', '2020-01-01')
    opened = new_employee(fx.company, 'V02', 'male', '2020-01-01')
    fx.hr.post(f'{L}/adjust', {'employeeId': opened, 'leaveTypeId': fx.t('CASUAL'), 'deltaDays': 3, 'reason': 'opening balance',
                               'kind': 'allocation', 'effectiveDate': '2026-07-20'})
    denied = fx.sita.post(f'{L}/accrual/run')
    assert denied.status_code == 403 and denied.json()['error'] == 'requires role hr or attendance_admin'
    run = fx.hr.post(f'{L}/accrual/run')
    assert run.status_code == 200
    assert {4, 5} <= {m['bsMonth'] for m in run.json()['months']}
    assert ledger_balance(veteran, fx.t('CASUAL')) == 6
    assert ledger_balance(opened, fx.t('CASUAL')) == 3
    again = fx.hr.post(f'{L}/accrual/run')
    assert again.json()['postings'] == 0
    assert ledger_balance(veteran, fx.t('CASUAL')) == 6
    assert allocate_annual_leave(fx.company, '2026-10-14') == 0


# ---------------------------------------------------------------- leave over days already worked

def test_full_day_on_worked_day_refused_half_day_allowed_hr_override(fx):
    sita = fx.demo['sita']
    for d in ('2026-10-05', '2026-10-06'):
        attendance(sita, d, 'Present', worked_min=480)
    fx.hr.post(f'{L}/adjust', {'employeeId': sita, 'leaveTypeId': fx.t('HOME'), 'deltaDays': 5, 'reason': 'opening balance', 'kind': 'allocation'})
    full = fx.sita.post(f'{L}/requests', {'leaveTypeId': fx.t('HOME'), 'from': '2026-10-05', 'to': '2026-10-05'})
    assert full.status_code == 409
    assert 'recorded as working' in full.json()['error']
    assert full.json()['error'].startswith('You were recorded as working on 19 Ashwin 2083 (1 day(s)).')
    assert fx.sita.post(f'{L}/requests', {'leaveTypeId': fx.t('HOME'), 'from': '2026-10-05', 'to': '2026-10-05',
                                          'halfDay': 'second_half'}).status_code == 201
    assert fx.sita.post(f'{L}/requests', {'leaveTypeId': fx.t('HOME'), 'from': '2026-10-06', 'to': '2026-10-06',
                                          'override': True}).status_code == 403
    assert fx.hr.post(f'{L}/requests', {'employeeId': sita, 'leaveTypeId': fx.t('HOME'), 'from': '2026-10-06', 'to': '2026-10-06',
                                        'override': True}).status_code == 201
    assert fx.stranger.get(f'{L}/balances', {'employeeId': sita}).status_code == 403


# ---------------------------------------------------------------- HR opening allocation and the automatic annual grant

def test_hr_allocation_replaces_automatic_grant(fx):
    emp = new_employee(fx.company, 'ALLOC1', 'female', '2025-01-01')
    assert allocate_annual_leave(fx.company, '2026-10-14', emp) > 0
    auto = ledger_balance(emp, fx.t('CASUAL'))
    assert auto > 0
    res = fx.hr.post(f'{L}/adjust', {'employeeId': emp, 'leaveTypeId': fx.t('CASUAL'), 'deltaDays': 6, 'kind': 'allocation',
                                     'reason': 'Opening balance', 'effectiveDate': '2026-07-17'})
    assert res.status_code == 201
    assert float(res.json()['replacedAutomatic']) == auto
    assert ledger_balance(emp, fx.t('CASUAL')) == 6
    # A second HR allocation is an intentional top-up and is not offset again; the automatic grant never comes back.
    fx.hr.post(f'{L}/adjust', {'employeeId': emp, 'leaveTypeId': fx.t('CASUAL'), 'deltaDays': 1, 'kind': 'allocation',
                               'reason': 'Top-up', 'effectiveDate': '2026-07-17'})
    assert allocate_annual_leave(fx.company, '2026-10-14', emp) == 0
    assert ledger_balance(emp, fx.t('CASUAL')) == 7
