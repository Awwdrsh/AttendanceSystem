"""Service-level rules of hajiri.ot.services beyond the TS test cases: messages, scoping, audit and notifications."""
from __future__ import annotations

import pytest

from hajiri.core.bsdate import bs_label
from hajiri.db.models import (AttendanceDay, AuditLog, CompanySetting, Employee, LeaveLedger, NotificationOutbox,
                              OtRequest, OtSlip, WorkflowInstance)
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.ot import services
from hajiri.workflow import service as wf
from tests.web.factories import make_company, make_employee, make_user

from .helpers import days_ago, slip


def test_week_starts_on_sunday():
    assert services.week_start('2026-09-19') == '2026-09-13'   # Saturday
    assert services.week_start('2026-09-13') == '2026-09-13'   # Sunday
    assert services.week_start('2026-09-14') == '2026-09-13'   # Monday


def test_comp_off_days_round_like_math_round():
    assert services.comp_off_days(240) == 0.5
    assert services.comp_off_days(100) == 0.21
    assert services.comp_off_days(480) == 1
    assert services.comp_off_days(3) == 0.01  # 0.00625 * 100 = 0.625 -> 1


def test_request_messages(world):
    u = world.sita_login.user
    with pytest.raises(BadRequestError, match=r'^overtime may not exceed 240 minutes \(4 hours\) per day \(Sec 30\(1\)\)$'):
        services.create_request(u, {'localDate': days_ago(1), 'requestedMin': 241})
    with pytest.raises(ValidationError):
        services.create_request(u, {'localDate': days_ago(1), 'requestedMin': 0})
    with pytest.raises(BadRequestError, match=r'^past-dated overtime requests are limited to 7 days$'):
        services.create_request(u, {'localDate': days_ago(8), 'requestedMin': 30})
    services.create_request(u, {'localDate': days_ago(7), 'requestedMin': 30})
    with pytest.raises(ConflictError, match='^an overtime request already exists for that day$'):
        services.create_request(u, {'localDate': days_ago(7), 'requestedMin': 30})
    Employee.objects.filter(pk=world.demo['sita']).update(ot_eligible=False)
    with pytest.raises(BadRequestError, match=r'^employee grade is not overtime-eligible \(Sec 31\(2\) basis must be documented\)$'):
        services.create_request(u, {'localDate': days_ago(2), 'requestedMin': 30})


def test_past_window_follows_the_attendance_policy_and_hr_may_go_further(world):
    current = CompanySetting.objects.filter(company_id=world.company, key='attendance.policy').first()
    CompanySetting.objects.update_or_create(company_id=world.company, key='attendance.policy',
                                            defaults={'value': {**(current.value if current else {}), 'otPastDatedDays': 30}})
    services.create_request(world.sita_login.user, {'localDate': days_ago(20), 'requestedMin': 30})
    with pytest.raises(BadRequestError, match='limited to 30 days'):
        services.create_request(world.sita_login.user, {'localDate': days_ago(31), 'requestedMin': 30})
    assert services.create_request(world.hr_login.user, {'employeeId': world.demo['sita'], 'localDate': days_ago(60), 'requestedMin': 30})['id']


def test_weekly_cap_counts_pending_and_approved_requests(world):
    u = world.hr_login.user
    sita = world.demo['sita']
    for d in ('2026-09-13', '2026-09-14', '2026-09-15', '2026-09-16', '2026-09-17', '2026-09-18'):
        services.create_request(u, {'employeeId': sita, 'localDate': d, 'requestedMin': 240})
    with pytest.raises(BadRequestError, match=r'^weekly overtime cap of 24 hours would be exceeded \(1440 minutes already this week\)$'):
        services.create_request(u, {'employeeId': sita, 'localDate': '2026-09-19', 'requestedMin': 1})
    # The next week starts on Sunday 20 Sep.
    services.create_request(u, {'employeeId': sita, 'localDate': '2026-09-20', 'requestedMin': 60})


def test_create_opens_the_workflow_and_audits(world):
    rid = services.create_request(world.sita_login.user, {'localDate': days_ago(1), 'requestedMin': 90, 'reason': 'stock count'})['id']
    r = OtRequest.objects.values('workflow_instance_id', 'created_by', 'reason').get(pk=rid)
    assert r['created_by'] is not None and r['reason'] == 'stock count'
    inst = wf.get_instance(str(r['workflow_instance_id']))
    assert inst['object_type'] == 'ot_request' and inst['state'] == 'pending'
    a = AuditLog.objects.get(table_name='ot_request', row_id=rid, action='create')
    assert a.after == {'localDate': days_ago(1), 'requestedMin': 90, 'reason': 'stock count', 'employeeId': world.demo['sita']}
    n = list(NotificationOutbox.objects.filter(template='ot_request.pending', channel='in_app').values('title', 'body'))
    assert {'title': 'Overtime pre-approval', 'body': f'90 minutes on {days_ago(1)}: stock count'} in n


def test_employee_of_another_company_is_not_found_and_user_without_employee_needs_one(world):
    co = make_company()
    stranger = make_employee(co)
    with pytest.raises(NotFoundError, match='^employee not found$'):
        services.create_request(world.hr_login.user, {'employeeId': str(stranger.id), 'localDate': days_ago(1), 'requestedMin': 30})
    with pytest.raises(BadRequestError, match='^employeeId required$'):
        services.create_request(world.hr_login.user, {'localDate': days_ago(1), 'requestedMin': 30})


def test_decision_caps_at_request_and_notifies(world):
    rid = services.create_request(world.sita_login.user, {'localDate': days_ago(1), 'requestedMin': 120})['id']
    inst = OtRequest.objects.get(pk=rid).workflow_instance_id
    assert wf.decide(world.ram_login.user, str(inst), 'approve', approved_min=200) == 'approved'
    assert OtRequest.objects.values('approved_min', 'status').get(pk=rid) == {'approved_min': 120, 'status': 'approved'}
    n = NotificationOutbox.objects.get(template='ot.approved', channel='push')
    assert n.title == 'Overtime approved'
    assert n.body == f'Overtime on {bs_label(days_ago(1))}: 120 minutes approved.'
    assert n.payload == {'requestId': rid}


def test_rejection_notifies_with_the_reason(world):
    rid = services.create_request(world.sita_login.user, {'localDate': days_ago(2), 'requestedMin': 60})['id']
    inst = OtRequest.objects.get(pk=rid).workflow_instance_id
    assert wf.decide(world.ram_login.user, str(inst), 'reject', reason='not needed') == 'rejected'
    assert OtRequest.objects.values('approved_min', 'status').get(pk=rid) == {'approved_min': None, 'status': 'rejected'}
    n = NotificationOutbox.objects.get(template='ot.rejected', channel='in_app')
    assert n.body == f'Overtime on {bs_label(days_ago(2))}: rejected (not needed).'


def test_request_scopes(world, other_mgr):
    services.create_request(world.sita_login.user, {'localDate': days_ago(1), 'requestedMin': 60})
    services.create_request(world.hr_login.user, {'employeeId': world.demo['ram'], 'localDate': days_ago(1), 'requestedMin': 30})
    names = lambda u, q=None: sorted(r['emp_code'] for r in services.list_requests(u, q or {}))  # noqa: E731
    assert names(world.hr_login.user) == ['E001', 'E002']
    assert names(world.ram_login.user) == ['E001', 'E002']          # team (Sita) and himself
    assert names(world.sita_login.user) == ['E001']
    with pytest.raises(ForbiddenError, match='^scope all requires HR$'):
        services.list_requests(world.ram_login.user, {'scope': 'all'})
    with pytest.raises(ForbiddenError, match='^not allowed to act for this employee$'):
        services.list_requests(world.sita_login.user, {'employeeId': world.demo['ram']})
    assert names(world.hr_login.user, {'status': 'approved'}) == []


def test_cancel_rules(world, other_mgr):
    rid = services.create_request(world.sita_login.user, {'localDate': days_ago(1), 'requestedMin': 60})['id']
    stranger = make_user(make_company(), roles=('hr',))
    with pytest.raises(NotFoundError, match='^request not found$'):
        services.cancel_request(stranger, rid)
    ram_user = world.ram_login.user
    assert services.cancel_request(ram_user, rid) == {'ok': True}
    assert OtRequest.objects.get(pk=rid).status == 'cancelled'
    assert WorkflowInstance.objects.get(object_type='ot_request', object_id=rid).state == 'cancelled'
    with pytest.raises(ConflictError, match=r'^only pending requests can be cancelled \(status cancelled\)$'):
        services.cancel_request(world.sita_login.user, rid)


def test_slip_guards_and_messages(world):
    ram_slip = slip(world.demo['ram'], '2026-09-21', 90)
    with pytest.raises(ForbiddenError, match='^cannot decide your own overtime$'):
        services.approve_slip(world.ram_login.user, ram_slip, {})
    with pytest.raises(ForbiddenError, match='^requires role hr or attendance_admin or manager$'):
        services.approve_slip(world.sita_login.user, ram_slip, {})
    with pytest.raises(NotFoundError, match='^OT slip not found$'):
        services.approve_slip(make_user(make_company(), roles=('hr',)), ram_slip, {})
    hr = world.hr_login.user
    assert services.approve_slip(hr, ram_slip, {}) == {'ok': True, 'approvedMin': 90}
    with pytest.raises(ConflictError, match='^slip is already approved$'):
        services.approve_slip(hr, ram_slip, {'approvedMin': 30})
    with pytest.raises(ValidationError):
        services.reject_slip(hr, ram_slip, {'reason': 'no'})
    assert services.reject_slip(hr, ram_slip, {'reason': 'not authorised'}) == {'ok': True}
    s = OtSlip.objects.get(pk=ram_slip)
    assert s.status == 'rejected' and s.approved_min == 0 and str(s.reviewed_by_id) == hr.id
    a = AuditLog.objects.get(table_name='ot_slip', row_id=ram_slip, action='reject')
    assert a.after == {'reason': 'not authorised', 'employeeId': world.demo['ram']}
    with pytest.raises(ConflictError, match='^slip is already rejected$'):
        services.reject_slip(hr, ram_slip, {'reason': 'twice over'})
    with pytest.raises(ConflictError, match='^slip was already converted, posted or rejected$'):
        services.comp_off_slip(hr, ram_slip)
    # A rejected slip can still be approved after all.
    assert services.approve_slip(hr, ram_slip, {'approvedMin': 45})['approvedMin'] == 45
    OtSlip.objects.filter(pk=ram_slip).update(status=OtSlip.Status.POSTED)
    with pytest.raises(ConflictError, match='^slip already posted to payroll$'):
        services.comp_off_slip(hr, ram_slip)


def test_comp_off_uses_approved_minutes_and_audits(world):
    sid = slip(world.demo['ram'], '2026-09-26', 240, 'weekly_off')
    hr = world.hr_login.user
    services.approve_slip(hr, sid, {'approvedMin': 120})
    assert services.comp_off_slip(hr, sid) == {'ok': True, 'days': 0.25, 'expiresOn': '2026-10-17'}
    s = OtSlip.objects.get(pk=sid)
    assert s.status == 'comp_off' and s.approved_min == 120
    led = LeaveLedger.objects.get(pk=s.comp_off_ledger_id)
    assert float(led.delta_days) == 0.25 and led.expires_on.isoformat() == '2026-10-17'
    assert led.note == 'comp-off for 120 min OT on 2026-09-26' and led.ref_type == 'ot_slip'
    a = AuditLog.objects.get(table_name='ot_slip', row_id=sid, action='comp_off')
    assert a.after == {'days': 0.25, 'expires': '2026-10-17'}
    with pytest.raises(ConflictError, match='^slip already converted to substitute leave; the credit cannot be reversed here$'):
        services.approve_slip(hr, sid, {})


def test_slip_list_scopes(world, other_mgr):
    slip(world.demo['sita'], '2026-09-21', 60)
    slip(world.demo['ram'], '2026-09-22', 60)
    codes = lambda u, q=None: sorted(r['emp_code'] for r in services.list_slips(u, q or {}))  # noqa: E731
    assert codes(world.hr_login.user) == ['E001', 'E002']
    assert codes(world.ram_login.user) == ['E001', 'E002']
    assert codes(world.sita_login.user) == ['E001']
    # An employee's employeeId filter is ignored: always their own.
    assert codes(world.sita_login.user, {'employeeId': world.demo['ram']}) == ['E001']
    assert codes(world.hr_login.user, {'status': 'approved'}) == []
    assert codes(world.hr_login.user, {'from': '2026-09-22'}) == ['E002']
    nobody = make_user(make_company(), roles=('employee',))
    with pytest.raises(ForbiddenError, match='^no employee linked to this user$'):
        services.list_slips(nobody, {})


def test_register_needs_the_attendance_area_and_lists_bs_dates(world):
    sid = slip(world.demo['ram'], '2026-09-26', 60)
    AttendanceDay.objects.create(employee_id=world.demo['ram'], local_date='2026-09-26', version=1, status='Present',
                                 ot_min=60, inputs_hash='h', bs_date='2083-06-10')
    with pytest.raises(ForbiddenError, match='^requires role hr or attendance_admin$'):
        services.register(world.ram_login.user, {'from': '2026-09-01', 'to': '2026-09-30'})
    fmt_, data = services.register(world.hr_login.user, {'from': '2026-09-01', 'to': '2026-09-30'})
    assert fmt_ == 'json' and len(data) == 1 and data[0]['emp_code'] == 'E002' and data[0]['status'] == 'detected'
    assert data[0]['bs_date'] == '2083-06-10'
    # A newer computed version of the day supplies the BS date (v_attendance_latest).
    AttendanceDay.objects.create(employee_id=world.demo['ram'], local_date='2026-09-26', version=2, status='Present',
                                 ot_min=60, inputs_hash='h2', bs_date='2083-06-10b')
    assert [r['bs_date'] for r in services.register(world.hr_login.user, {'from': '2026-09-01', 'to': '2026-09-30'})[1]] == ['2083-06-10b']
    assert sid
