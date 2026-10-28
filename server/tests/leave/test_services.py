"""Service functions other modules call (OT comp-off, final settlement, the worker hooks) and decision side effects."""
from tests.support.sql import execute, one, rows
from hajiri.leave import accrual, services
from hajiri.leave.service import encashable_balances, ledger_balance, post_encashment

from .helpers import instance_of

L = '/api/v1/leave'


def test_worker_hooks_are_registered(world):
    from hajiri.worker import registry
    assert registry.hook('leave.monthly_accrual') is accrual.run_monthly_accrual
    assert registry.hook('leave.catch_up') is accrual.run_leave_catch_up
    summary = accrual.run_monthly_accrual(world.company, 2083, 4)
    assert set(summary) == {'employees', 'postings', 'expired', 'bsYear', 'bsMonth'}


def test_encashable_balances_and_post_encashment(world):
    sita, t = world.demo['sita'], world.t
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-08-01', 12.5, 'adjustment')",
            [sita, t('HOME')])
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-08-01', 3, 'adjustment')",
            [sita, t('CASUAL')])
    got = encashable_balances(sita)
    assert got == [{'leaveTypeId': t('HOME'), 'code': 'HOME', 'days': 12.5}]  # casual is not encashable
    post_encashment(sita, t('HOME'), 12.5, ref_type='final_settlement', ref_id=sita)
    assert ledger_balance(sita, t('HOME')) == 0
    assert encashable_balances(sita) == []
    assert one("SELECT reason FROM leave_ledger WHERE ref_type = 'final_settlement'")['reason'] == 'encashment'


def test_rejection_notifies_the_employee_and_leaves_the_ledger(world):
    sita_user = one('SELECT id FROM app_user WHERE employee_id = %s', [world.demo['sita']])['id']
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': world.t('LWP'), 'from': '2026-11-12', 'to': '2026-11-13', 'reason': 'trip'})
    assert r.status_code == 201
    assert world.ram.post(f"/api/v1/workflow/{instance_of(r.json()['id'])}/reject", {'reason': 'busy week'}).json()['state'] == 'rejected'
    n = rows("SELECT channel, title, body FROM notification_outbox WHERE user_id = %s AND template = 'leave.rejected' ORDER BY channel", [sita_user])
    assert {x['channel'] for x in n} == {'in_app', 'push', 'sms'}
    assert n[0]['title'] == 'Leave rejected'
    assert n[0]['body'] == 'Leave without pay 26 Kartik 2083 to 27 Kartik 2083 was rejected: busy week.'
    assert one('SELECT status, decided_at FROM leave_request WHERE id = %s', [r.json()['id']])['status'] == 'rejected'
    assert rows("SELECT 1 AS x FROM leave_ledger WHERE ref_id = %s", [r.json()['id']]) == []


def test_policy_rules_probation_service_gender_and_max_run(world):
    sita, t = world.demo['sita'], world.t
    execute("UPDATE employee SET join_date = '2026-01-01', probation_end = '2026-12-31' WHERE id = %s", [sita])
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-08-01', 40, 'adjustment')",
            [sita, t('HOME')])
    # statutory leave is never blocked by probation; company leave is
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': t('CASUAL'), 'from': '2026-11-02', 'to': '2026-11-02'})
    assert r.status_code == 400 and r.json()['error'] == 'Casual leave cannot be taken during probation (ends 2026-12-31)'
    execute("UPDATE employee SET probation_end = NULL WHERE id = %s", [sita])
    execute('UPDATE leave_type SET min_service_days = 400 WHERE id = %s', [t('HOME')])
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-11-02', 'to': '2026-11-02'})
    assert r.status_code == 400 and r.json()['error'] == 'Home leave needs 400 days of service'
    execute('UPDATE leave_type SET min_service_days = 0 WHERE id = %s', [t('HOME')])
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': t('PATERNITY'), 'from': '2026-11-02', 'to': '2026-11-02'})
    assert r.status_code == 400 and r.json()['error'] == 'Maternity-care leave is not available for this employee'
    # the company's 30-day cap limits company leave only; statutory leave is not capped by policy
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': t('CASUAL'), 'from': '2026-11-01', 'to': '2026-12-15'})
    assert r.status_code == 400 and r.json()['error'] == 'Casual leave cannot exceed 30 consecutive days'
    r = world.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-11-01', 'to': '2026-12-15'})
    assert r.status_code == 201, r.json()
    execute("UPDATE employee SET status = 'exited' WHERE id = %s", [sita])
    r = world.hr.post(f'{L}/requests', {'employeeId': sita, 'leaveTypeId': t('HOME'), 'from': '2027-01-04', 'to': '2027-01-04'})
    assert r.status_code == 400 and r.json()['error'] == 'employee is not active'


def test_create_type_audit_and_lists(world):
    r = world.hr.post(f'{L}/types', {'code': 'STUDY', 'nameEn': 'Study leave', 'accrualBasis': 'annual', 'accrualDays': 3, 'allowHourly': True,
                                     'sortOrder': 55})
    assert r.status_code == 201
    row = one('SELECT * FROM leave_type WHERE id = %s', [r.json()['id']])
    assert row['allow_hourly'] and row['sort_order'] == 55 and row['carry_forward'] and not row['statutory']
    assert one("SELECT action FROM audit_log WHERE table_name = 'leave_type' AND row_id = %s", [r.json()['id']])['action'] == 'create'
    bad = world.hr.post(f'{L}/types', {'code': '', 'nameEn': 'x'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert world.sita.post(f'{L}/types', {'code': 'X', 'nameEn': 'x'}).status_code == 403
    assert world.hr.patch(f'{L}/types/00000000-0000-7000-8000-00000000dead', {'nameEn': 'x'}).status_code == 404
    # the ledger and request lists
    lst = world.hr.get(f'{L}/requests').json()['requests']
    assert lst == []
    assert world.sita.get(f'{L}/ledger').json() == {'ledger': []}
