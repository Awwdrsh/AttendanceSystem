"""Port of apps/api/test/ot-qa.test.ts (overtime slip guards)."""
from __future__ import annotations

from hajiri.leave.services import ledger_balance

from .helpers import slip, sub_type


def test_only_the_line_manager_or_hr_decides_a_slip_the_employee_never_does(world, other_mgr):
    sid = slip(world.demo['sita'], '2026-10-01', 60)
    assert other_mgr.post(f'/api/v1/ot/slips/{sid}/approve', {}).status_code == 403
    assert world.sita.post(f'/api/v1/ot/slips/{sid}/approve', {}).status_code == 403
    assert world.ram.post(f'/api/v1/ot/slips/{sid}/approve', {'approvedMin': 30}).json()['approvedMin'] == 30


def test_a_slip_converted_to_substitute_leave_cannot_be_approved_rejected_or_converted_again(world):
    sid = slip(world.demo['sita'], '2026-10-03', 240, 'weekly_off')
    assert world.hr.post(f'/api/v1/ot/slips/{sid}/comp-off').json()['days'] == 0.5
    assert world.hr.post(f'/api/v1/ot/slips/{sid}/approve', {}).status_code == 409
    assert world.hr.post(f'/api/v1/ot/slips/{sid}/reject', {'reason': 'oops'}).status_code == 409
    assert world.hr.post(f'/api/v1/ot/slips/{sid}/comp-off').status_code == 409
    assert ledger_balance(world.demo['sita'], sub_type(world.company)) == 0.5


def test_a_manager_cannot_preapprove_ot_for_a_non_report_register_csv_opens_cleanly_in_excel(world, other_mgr):
    r = other_mgr.post('/api/v1/ot/requests', {'employeeId': world.demo['sita'], 'localDate': '2026-10-05', 'requestedMin': 60})
    assert r.status_code == 403
    csv = world.hr.get('/api/v1/ot/register', {'from': '2026-10-01', 'to': '2026-10-31', 'format': 'csv'})
    text = csv.content.decode('utf-8')
    assert text.startswith('﻿emp_code,')
    assert '\r\n' in text
