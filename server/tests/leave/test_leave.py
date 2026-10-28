"""Port of apps/api/test/leave.test.ts: day computation, requests and approvals, accrual and substitute expiry."""
import pytest

from hajiri.attendance.service import recompute_day
from tests.support.sql import execute, one, rows
from hajiri.leave.accrual import expire_substitute_leave, run_monthly_accrual
from hajiri.leave.service import credit_comp_off, ledger_balance

from .helpers import attendance, instance_of

L = '/api/v1/leave'


def _policy(company, **kv):
    import json
    execute("UPDATE company_setting SET value = value || %s::jsonb WHERE company_id = %s AND key = 'leave.policy'",
            [json.dumps(kv), company])


# ---------------------------------------------------------------- leave day computation (plan §10.6 example F)

def test_saturday_between_sandwich_off_two_days_on_three(world):
    q = {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'), 'from': '2026-08-28', 'to': '2026-08-30'}
    off = world.hr.get(f'{L}/preview', q)
    assert off.status_code == 200
    assert off.json()['days'] == 2 and off.json()['sandwichApplied'] is False and off.json()['excludedDays'] == ['2026-08-29']
    _policy(world.company, sandwichRule=True)
    on = world.hr.get(f'{L}/preview', q)
    assert on.json()['days'] == 3 and on.json()['sandwichApplied'] is True
    _policy(world.company, sandwichRule=False)


def test_holidays_are_excluded_gender_aware(world):
    execute("INSERT INTO holiday (company_id, local_date, name_en, gender_scope) VALUES (%s, '2026-09-14', 'Teej', 'female')", [world.company])
    female = world.hr.get(f'{L}/preview', {'employeeId': world.demo['sita'], 'leaveTypeId': world.t('HOME'), 'from': '2026-09-13', 'to': '2026-09-15'})
    male = world.hr.get(f'{L}/preview', {'employeeId': world.demo['ram'], 'leaveTypeId': world.t('HOME'), 'from': '2026-09-13', 'to': '2026-09-15'})
    assert female.json()['days'] == 2
    assert male.json()['days'] == 3


# ---------------------------------------------------------------- leave requests and approvals

def test_requests_and_approvals(world):
    """The TS describe block runs its cases in order on one database; they are steps of one test here."""
    sita, ram, hr, t = world.sita, world.ram, world.hr, world.t

    # insufficient balance returns a split suggestion
    r = sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-09-21', 'to': '2026-09-23', 'reason': 'family'})
    assert r.status_code == 400
    assert r.json()['suggestion'] == {'paidDays': 0, 'lwpDays': 3}
    assert 'unpaid leave' in r.json()['error']

    # employee requests, manager sees it in the inbox with balance-after and approves; day becomes OnLeave
    adj = hr.post(f'{L}/adjust', {'employeeId': world.demo['sita'], 'leaveTypeId': t('HOME'), 'deltaDays': 10, 'reason': 'opening balance', 'kind': 'allocation'})
    assert adj.status_code == 201
    assert adj.json()['balance'] == 10
    r = sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-09-21', 'to': '2026-09-22', 'reason': 'family'})
    assert r.status_code == 201, r.json()
    assert r.json()['days'] == 2 and r.json()['sandwichApplied'] is False
    request_id = r.json()['id']
    bal = sita.get(f'{L}/balances')
    home = next(b for b in bal.json()['balances'] if b['code'] == 'HOME')
    assert (home['balance'], home['pending'], home['available']) == (10, 2, 8)
    assert 'Opening' in home['explanation']

    inbox = ram.get('/api/v1/workflow/inbox')
    items = inbox.json()['items']
    assert len(items) == 1
    item = items[0]
    assert item['objectType'] == 'leave_request' and item['objectId'] == request_id and item['employee']['id'] == world.demo['sita']
    s = item['summary']
    assert (s['leaveType'], s['days'], s['balanceNow'], s['balanceAfter']) == ('HOME', 2, 10, 8)
    instance_id = item['instanceId']
    assert rows("SELECT 1 AS x FROM notification_outbox WHERE user_id = %s AND template = 'leave_request.pending'", [world.ram_user_id])

    ok = ram.post(f'/api/v1/workflow/{instance_id}/approve', {})
    assert ok.status_code == 200
    assert ok.json()['state'] == 'approved'
    assert ledger_balance(world.demo['sita'], t('HOME')) == 8
    assert recompute_day(world.demo['sita'], '2026-09-21').day.status == 'OnLeave'
    act = one('SELECT signature FROM approval_action WHERE instance_id = %s', [instance_id])
    assert len(act['signature']) == 64
    # The approval recomputes the covered days before responding, so nothing is left for the worker.
    assert rows("SELECT 1 AS x FROM recompute_queue WHERE employee_id = %s AND local_date = '2026-09-22'", [world.demo['sita']]) == []
    assert one("SELECT status FROM v_attendance_latest WHERE employee_id = %s AND local_date = '2026-09-22'", [world.demo['sita']])['status'] == 'OnLeave'

    # nobody approves their own request; rejecting needs a reason; double decision is a conflict
    r = sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-09-24', 'to': '2026-09-24'})
    assert r.status_code == 201, r.json()
    inst = instance_of(r.json()['id'])
    assert sita.post(f'/api/v1/workflow/{inst}/approve', {}).status_code == 403
    assert ram.post(f'/api/v1/workflow/{inst}/reject', {}).status_code == 400
    assert ram.post(f'/api/v1/workflow/{inst}/reject', {'reason': 'coverage'}).json()['state'] == 'rejected'
    assert ram.post(f'/api/v1/workflow/{inst}/approve', {}).status_code == 409
    assert ledger_balance(world.demo['sita'], t('HOME')) == 8

    # overlapping request is rejected; cancelling an approved request reverses the ledger
    assert sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-09-22', 'to': '2026-09-23'}).status_code == 409
    c = sita.post(f'{L}/requests/{request_id}/cancel', {'reason': 'plans changed'})
    assert c.status_code == 200 and c.json() == {'ok': True}
    assert ledger_balance(world.demo['sita'], t('HOME')) == 10
    assert recompute_day(world.demo['sita'], '2026-09-21').day.status == 'Absent'

    # sick leave beyond 3 days needs a certificate; LWP is always allowed
    no_cert = sita.post(f'{L}/requests', {'leaveTypeId': t('SICK'), 'from': '2026-10-04', 'to': '2026-10-08'})
    assert no_cert.status_code == 400
    assert 'certificate' in no_cert.json()['error']
    lwp = sita.post(f'{L}/requests', {'leaveTypeId': t('LWP'), 'from': '2026-10-04', 'to': '2026-10-08', 'reason': 'travel'})
    assert lwp.status_code == 201, lwp.json()
    assert lwp.json()['days'] == 5  # Sun 4 Oct .. Thu 8 Oct 2026, no Saturday

    # team calendar lists who is off
    cal = hr.get(f'{L}/team-calendar', {'from': '2026-10-04', 'to': '2026-10-08'})
    day = next(d for d in cal.json()['days'] if d['date'] == '2026-10-05')
    first = day['off'][0]
    assert (first['employeeId'], first['type'], first['status']) == (world.demo['sita'], 'LWP', 'pending')

    # statutory entitlement fields are locked
    r = hr.patch(f"{L}/types/{t('HOME')}", {'maxBalance': 200})
    assert r.status_code == 400
    assert r.json()['error'] == 'statutory leave entitlement fields are locked: maxBalance (Labour Act 2074 Ch. 9)'
    assert hr.patch(f"{L}/types/{t('HOME')}", {'nameNe': 'घर बिदा'}).status_code == 200


# ---------------------------------------------------------------- accrual (plan §10.6 example E) and substitute expiry

def test_accrual_cap_and_substitute_expiry(world):
    ram, t = world.demo['ram'], world.t
    dates = ['2026-07-19', '2026-07-20', '2026-07-21', '2026-07-22', '2026-07-23', '2026-07-26', '2026-07-27', '2026-07-28',
             '2026-07-29', '2026-07-30', '2026-08-02', '2026-08-03', '2026-08-04', '2026-08-05', '2026-08-06', '2026-08-09',
             '2026-08-10', '2026-08-11', '2026-08-12', '2026-08-13', '2026-08-16', '2026-07-17']
    for d in dates:
        attendance(ram, d, 'Present')
    for d in ('2026-08-14', '2026-08-15', '2026-07-18'):
        attendance(ram, d, 'Absent')
    # 22 present days in Shrawan 2083 -> 1.10 home, 1.0 sick, casual allocated; idempotent
    first = run_monthly_accrual(world.company, 2083, 4)
    assert first['postings'] >= 3
    assert ledger_balance(ram, t('HOME')) == pytest.approx(1.1, abs=0.005)
    assert ledger_balance(ram, t('SICK')) == 1
    assert ledger_balance(ram, t('CASUAL')) == 6
    again = run_monthly_accrual(world.company, 2083, 4)
    assert again['postings'] == 0
    assert ledger_balance(ram, t('HOME')) == pytest.approx(1.1, abs=0.005)

    # sick leave cap of 45 expires the excess
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-08-01', 44.5, 'adjustment')",
            [ram, t('SICK')])
    run_monthly_accrual(world.company, 2083, 5)
    assert ledger_balance(ram, t('SICK')) == 45

    # unused substitute leave lapses after its expiry date
    credit_comp_off(ram, 1, '2026-09-01', ref_type='test', ref_id=ram)
    assert ledger_balance(ram, t('SUBSTITUTE')) == 1
    assert expire_substitute_leave(world.company, '2026-10-01') == 1
    assert ledger_balance(ram, t('SUBSTITUTE')) == 0
    assert expire_substitute_leave(world.company, '2026-10-01') == 0


def test_cap_on_encashable_type_tells_hr(world):
    """Encashable excess above the cap is flagged to HR for payout (Sec 49(3))."""
    ram, t = world.demo['ram'], world.t
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-08-01', 45, 'adjustment')",
            [ram, t('SICK')])
    run_monthly_accrual(world.company, 2083, 5)
    assert ledger_balance(ram, t('SICK')) == 45
    n = rows("SELECT title, body FROM notification_outbox WHERE template = 'leave.cap_exceeded' AND employee_id = %s", [ram])
    assert n and n[0]['title'] == 'Leave balance above cap'
    assert n[0]['body'] == 'Sick leave balance exceeded 45.00 days by 1.00; excess should be paid out (Sec 49(3)).'
