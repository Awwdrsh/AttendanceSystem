"""Port of apps/api/test/ot.test.ts."""
from __future__ import annotations

from datetime import date, timedelta

from hajiri.core.db import execute, one
from hajiri.leave.services import ledger_balance

from .helpers import days_ago, slip, sub_type


# ---------------------------------------------------------------- overtime pre-approval

def test_rejects_more_than_4_hours_a_day_and_more_than_24_hours_a_week(world):
    assert world.sita.post('/api/v1/ot/requests', {'localDate': days_ago(1), 'requestedMin': 300}).status_code == 400
    # Monday..Saturday of a past week each already carry 4 h of OT.
    base = date(2026, 9, 13)  # Sunday
    for i in range(6):
        execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, ot_min, inputs_hash)
                   VALUES (%s, %s, 1, 'Present', 240, 'h')""", [world.demo['sita'], (base + timedelta(days=i)).isoformat()])
    r = world.hr.post('/api/v1/ot/requests', {'employeeId': world.demo['sita'], 'localDate': '2026-09-19', 'requestedMin': 60})
    assert r.status_code == 400
    assert 'weekly overtime cap' in r.json()['error']


def test_manager_approves_a_smaller_amount_past_dated_window_applies_to_employees(world):
    d = days_ago(1)
    r = world.sita.post('/api/v1/ot/requests', {'localDate': d, 'requestedMin': 120, 'reason': 'month-end close'})
    assert r.status_code == 201
    rid = r.json()['id']
    inbox = world.ram.get('/api/v1/workflow/inbox')
    item = next(i for i in inbox.json()['items'] if i['objectId'] == rid)
    assert item['summary']['requested_min'] == 120
    ok = world.ram.post(f"/api/v1/workflow/{item['instanceId']}/approve", {'approvedMin': 60})
    assert ok.json()['state'] == 'approved'
    row = one('SELECT approved_min, status FROM ot_request WHERE id = %s', [rid])
    assert row == {'approved_min': 60, 'status': 'approved'}
    assert world.sita.post('/api/v1/ot/requests', {'localDate': days_ago(20), 'requestedMin': 30}).status_code == 400
    assert world.sita.post('/api/v1/ot/requests', {'localDate': d, 'requestedMin': 30}).status_code == 409


def test_ineligible_grades_cannot_request_ot(world):
    execute('UPDATE employee SET ot_eligible = false WHERE id = %s', [world.demo['ram']])
    assert world.ram.post('/api/v1/ot/requests', {'localDate': days_ago(1), 'requestedMin': 30}).status_code == 400
    execute('UPDATE employee SET ot_eligible = true WHERE id = %s', [world.demo['ram']])


# ---------------------------------------------------------------- overtime slips

def test_hr_approves_a_detected_slip_capped_at_detected_minutes_posted_slip_is_immutable(world):
    slip_id = slip(world.demo['ram'], '2026-09-21', 120)
    own = world.sita.get('/api/v1/ot/slips')
    assert len(own.json()['slips']) == 0
    a = world.hr.post(f'/api/v1/ot/slips/{slip_id}/approve', {'approvedMin': 200})
    assert a.json()['approvedMin'] == 120
    execute("UPDATE ot_slip SET status = 'posted' WHERE id = %s", [slip_id])
    assert world.hr.post(f'/api/v1/ot/slips/{slip_id}/reject', {'reason': 'already posted'}).status_code == 409


def test_comp_off_credits_substitute_leave_expiring_21_days_later(world):
    sid = slip(world.demo['ram'], '2026-09-26', 240, 'weekly_off')
    r = world.hr.post(f'/api/v1/ot/slips/{sid}/comp-off')
    assert r.status_code == 200
    assert r.json() == {'ok': True, 'days': 0.5, 'expiresOn': '2026-10-17'}
    assert ledger_balance(world.demo['ram'], sub_type(world.company)) == 0.5
    csv = world.hr.get('/api/v1/ot/register', {'from': '2026-09-01', 'to': '2026-09-30', 'format': 'csv'})
    text = csv.content.decode('utf-8')
    assert text.split('\r\n')[0] == '﻿emp_code,name_en,local_date,bs_date,day_type,detected_min,approved_min,multiplier,status'
    assert 'E002,Ram Thapa,2026-09-26' in text
