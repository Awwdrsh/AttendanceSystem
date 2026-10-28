"""Port of apps/api/test/leave-qa.test.ts: leave guards."""
import json
import threading

import pytest
from django.db import connection
from django.test import Client

from tests.support.sql import execute, one, rows
from hajiri.leave.accrual import run_monthly_accrual
from hajiri.leave.service import ledger_balance

from .helpers import attendance, instance_of

L = '/api/v1/leave'


@pytest.fixture
def qa(world):
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, effective_date, delta_days, reason) VALUES (%s, %s, '2026-07-17', 20, 'allocation')",
            [world.demo['sita'], world.t('HOME')])
    return world


def test_half_day_must_be_single_date_and_from_after_to_rejected(qa):
    t = qa.t
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-01', 'to': '2026-12-02', 'halfDay': 'first_half'})
    assert r.status_code == 400 and r.json()['error'] == 'half-day leave must be a single date'
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-02', 'to': '2026-12-01'})
    assert r.status_code == 400 and r.json()['error'] == 'to must be on or after from'


def test_leave_cannot_start_before_joining_or_end_after_exit(qa):
    sita = qa.demo['sita']
    execute("UPDATE employee SET join_date = '2026-06-01' WHERE id = %s", [sita])
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': qa.t('LWP'), 'from': '2026-05-31', 'to': '2026-06-01'})
    assert r.status_code == 400 and r.json()['error'] == 'leave cannot start before the joining date 2026-06-01'
    execute("UPDATE employee SET exit_date = '2026-12-15' WHERE id = %s", [sita])
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': qa.t('LWP'), 'from': '2026-12-14', 'to': '2026-12-16'})
    assert r.status_code == 400 and r.json()['error'] == 'leave cannot end after the exit date 2026-12-15'
    execute('UPDATE employee SET exit_date = NULL WHERE id = %s', [sita])


def test_payroll_locked_day_blocks_requests_approval_and_cancellation(qa):
    sita, t = qa.demo['sita'], qa.t
    attendance(sita, '2026-12-07', 'Present', locked=True)
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-06', 'to': '2026-12-08'})
    assert r.status_code == 409
    # A request filed before the lock, then the day gets locked: approval must refuse and leave the ledger untouched.
    r2 = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-09', 'to': '2026-12-09'})
    assert r2.status_code == 201, r2.json()
    attendance(sita, '2026-12-09', 'Present', locked=True)
    inst = instance_of(r2.json()['id'])
    ap = qa.ram.post(f'/api/v1/workflow/{inst}/approve', {})
    assert ap.status_code == 409
    assert ap.json()['error'] == 'the leave falls in a payroll-locked period; reject it and let HR raise an adjustment'
    assert ledger_balance(sita, t('HOME')) == 20
    assert one('SELECT state FROM workflow_instance WHERE id = %s', [inst]) == {'state': 'pending'}
    rj = qa.ram.post(f'/api/v1/workflow/{inst}/reject', {'reason': 'period closed'})
    assert rj.json()['state'] == 'rejected'


def test_cancelling_approved_leave_in_a_locked_period_is_refused(qa):
    sita, t = qa.demo['sita'], qa.t
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-10', 'to': '2026-12-10'})
    assert r.status_code == 201
    assert qa.ram.post(f"/api/v1/workflow/{instance_of(r.json()['id'])}/approve", {}).json()['state'] == 'approved'
    execute("UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = '2026-12-10'", [sita])
    if not rows("SELECT 1 AS x FROM attendance_day WHERE employee_id = %s AND local_date = '2026-12-10'", [sita]):
        attendance(sita, '2026-12-10', 'OnLeave', locked=True)
    c = qa.sita.post(f"{L}/requests/{r.json()['id']}/cancel", {})
    assert c.status_code == 409 and c.json()['error'] == 'leave falls in a payroll-locked period; ask HR for an adjustment'
    assert ledger_balance(sita, t('HOME')) == 19


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_requests_exactly_one_is_accepted(demo, login_as):
    from tests.leave.helpers import type_id
    sita = login_as(['employee'], demo['sita'])
    lwp = type_id(demo['company_id'], 'LWP')
    body = json.dumps({'leaveTypeId': lwp, 'from': '2026-12-20', 'to': '2026-12-21', 'reason': 'race'})
    codes: list[int] = []
    barrier = threading.Barrier(5)

    def go():
        try:
            barrier.wait()
            r = Client().post(f'{L}/requests', body, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {sita.token}')
            codes.append(r.status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=go) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(codes).count(201) == 1
    assert sorted(codes).count(409) == 4


def test_every_usage_has_matching_reversal_on_cancel(qa):
    sita, t = qa.demo['sita'], qa.t
    r = qa.sita.post(f'{L}/requests', {'leaveTypeId': t('HOME'), 'from': '2026-12-22', 'to': '2026-12-23'})
    assert r.status_code == 201
    qa.ram.post(f"/api/v1/workflow/{instance_of(r.json()['id'])}/approve", {})
    assert ledger_balance(sita, t('HOME')) == 18
    assert qa.sita.post(f"{L}/requests/{r.json()['id']}/cancel", {}).status_code == 200
    got = rows('SELECT reason, delta_days::text FROM leave_ledger WHERE ref_id = %s ORDER BY posted_at', [r.json()['id']])
    assert got == [{'reason': 'usage', 'delta_days': '-2.00'}, {'reason': 'cancellation', 'delta_days': '2.00'}]
    assert ledger_balance(sita, t('HOME')) == 20
    again = qa.sita.post(f"{L}/requests/{r.json()['id']}/cancel", {})
    assert again.status_code == 409 and again.json()['error'] == 'request is already cancelled'


def test_weekly_off_other_than_saturday_is_honoured(qa):
    ram = qa.demo['ram']
    execute("UPDATE employee SET weekly_off_days = '{0}' WHERE id = %s", [ram])
    # HOME has no sandwich rule (LWP does by default), so the Sunday in the middle is simply excluded.
    p = qa.hr.get(f'{L}/preview', {'employeeId': ram, 'leaveTypeId': qa.t('HOME'), 'from': '2026-12-05', 'to': '2026-12-07'}).json()
    assert (p['days'], p['excludedDays'], p['sandwichApplied']) == (2, ['2026-12-06'], False), p
    execute("UPDATE employee SET weekly_off_days = '{6}' WHERE id = %s", [ram])


def test_accrual_twice_no_double_posting_skips_later_joiners(qa):
    ram = qa.demo['ram']
    execute("UPDATE employee SET join_date = '2026-09-01' WHERE id = %s", [ram])

    def ram_rows():
        return len(rows("SELECT 1 AS x FROM leave_ledger WHERE employee_id = %s AND reason IN ('accrual', 'allocation')", [ram]))
    before = ram_rows()
    a = run_monthly_accrual(qa.company, 2083, 4)  # Shrawan 2083 ends 16 Aug 2026, before Ram joined
    assert ram_rows() == before
    b = run_monthly_accrual(qa.company, 2083, 4)
    assert b['postings'] == 0
    assert a['bsMonth'] == 4
    execute('UPDATE employee SET join_date = NULL WHERE id = %s', [ram])
