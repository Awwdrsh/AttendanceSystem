"""Service-level rules of hajiri.regularisation.services that the TS route and handler define."""
from __future__ import annotations

import pytest

from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.regularisation import services
from hajiri.workflow import service as wf
from tests.regularisation.test_regularisation import days_ago, npt, workday_ago
from tests.web.factories import audit_actions


def _approve(login, request_id):
    inst = one('SELECT workflow_instance_id FROM regularisation_request WHERE id = %s', [request_id])['workflow_instance_id']
    return wf.decide(login.user, str(inst), 'approve')


def test_create_starts_the_workflow_links_it_and_audits(demo, reg):
    d = workday_ago(2)
    out = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'on_duty', 'reason': 'client site'})
    assert out['status'] == 'pending'
    rr = one('SELECT * FROM regularisation_request WHERE id = %s', [out['id']])
    assert str(rr['created_by']) == reg['sita'].user_id and rr['punch_time'] is None
    inst = wf.instance_for_object('regularisation_request', out['id'])
    assert inst['id'] == str(rr['workflow_instance_id']) and inst['state'] == 'pending'
    assert audit_actions(out['id']) == ['create']
    after = one("SELECT after FROM audit_log WHERE row_id = %s AND action = 'create'", [out['id']])['after']
    assert after == {'localDate': d, 'kind': 'on_duty', 'reason': 'client site', 'employeeId': demo['sita'], 'onBehalf': False}
    # The manager is told, with the TS title and body.
    n = one("""SELECT title, body FROM notification_outbox WHERE user_id = %s AND channel = 'in_app' ORDER BY created_at DESC LIMIT 1""",
            [reg['ram'].user_id])
    from hajiri.core.bsdate import bs_label
    assert n == {'title': 'Attendance correction to approve', 'body': f'on duty on {bs_label(d)}: client site'}


def test_duplicate_pending_request_is_a_conflict(reg):
    d = workday_ago(2)
    services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})
    with pytest.raises(ConflictError, match='a pending request of this kind already exists for that day'):
        services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})


def test_without_an_employee_link_employee_id_is_required(reg):
    with pytest.raises(BadRequestError, match='employeeId required'):
        services.create_request(reg['hr'].user, {'localDate': workday_ago(2), 'kind': 'wfh', 'reason': 'home'})


def test_another_companys_employee_is_not_found(reg, other_company):
    from tests.web.factories import make_employee
    e = make_employee(other_company)
    with pytest.raises(NotFoundError, match='employee not found'):
        services.create_request(reg['hr'].user, {'employeeId': str(e.id), 'localDate': workday_ago(2), 'kind': 'wfh', 'reason': 'x y'})


def test_hr_on_behalf_of_other_kinds_still_needs_approval(demo, reg):
    out = services.create_request(reg['hr'].user, {'employeeId': demo['ram'], 'localDate': workday_ago(2), 'kind': 'on_duty',
                                                   'reason': 'client site'})
    assert out['status'] == 'pending'


def test_hr_corrections_skip_the_backdate_window_and_the_cap(demo, reg):
    execute("""UPDATE company_setting SET value = value || '{"regularisationMonthlyCap": 0, "regularisationBackdateDays": 1}'
                WHERE company_id = %s AND key = 'attendance.policy'""", [demo['company_id']])
    assert services.policy(demo['company_id']) == {'backdateDays': 1, 'monthlyCap': 0}
    out = services.create_request(reg['hr'].user, {'employeeId': demo['sita'], 'localDate': days_ago(20), 'kind': 'on_duty',
                                                   'reason': 'device outage'})
    assert out['status'] == 'pending'
    with pytest.raises(BadRequestError, match='corrections can only be requested within 1 days'):
        services.create_request(reg['sita'].user, {'localDate': days_ago(20), 'kind': 'half_day', 'reason': 'x y z'})


def test_the_cap_counts_only_what_the_employee_filed_themself_and_not_wfh(demo, reg):
    execute("""UPDATE company_setting SET value = value || '{"regularisationMonthlyCap": 1}'
                WHERE company_id = %s AND key = 'attendance.policy'""", [demo['company_id']])
    d = workday_ago(1)
    # Filed by the manager for Sita: does not count against her.
    services.create_request(reg['ram'].user, {'employeeId': demo['sita'], 'localDate': d, 'kind': 'on_duty', 'reason': 'client'})
    services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'wfh', 'reason': 'home'})
    services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})
    with pytest.raises(BadRequestError, match='monthly limit of 1 corrections reached; contact HR'):
        services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'shift_change', 'shiftId': demo['shift_id'], 'reason': 'swap'})


def test_policy_defaults(demo):
    execute("DELETE FROM company_setting WHERE company_id = %s AND key = 'attendance.policy'", [demo['company_id']])
    assert services.policy(demo['company_id']) == {'backdateDays': 30, 'monthlyCap': 5}


def test_approval_notifies_the_employee_and_rejection_carries_the_reason(demo, reg):
    from hajiri.core.bsdate import bs_label
    d = workday_ago(2)
    a = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'wfh', 'reason': 'worked from home'})
    _approve(reg['ram'], a['id'])
    n = rows("""SELECT template, title, body, payload, channel FROM notification_outbox WHERE user_id = %s AND template LIKE 'regularisation.%%'
                ORDER BY channel""", [reg['sita'].user_id])
    assert [x['channel'] for x in n] == ['in_app', 'push']
    assert n[0]['template'] == 'regularisation.approved'
    assert n[0]['title'] == 'Attendance correction approved'
    assert n[0]['body'] == f'Work from home for {bs_label(d)} was approved.'
    assert n[0]['payload'] == {'requestId': a['id']}
    b = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(d, '18:00'),
                                                   'punchDirection': 'out', 'reason': 'forgot'})
    inst = wf.instance_for_object('regularisation_request', b['id'])
    wf.decide(reg['ram'].user, inst['id'], 'reject', reason='you left at 17:00')
    rr = one('SELECT status, resulting_punch_id, decided_at FROM regularisation_request WHERE id = %s', [b['id']])
    assert rr['status'] == 'rejected' and rr['resulting_punch_id'] is None and rr['decided_at'] is not None
    rej = one("""SELECT title, body FROM notification_outbox WHERE template = 'regularisation.rejected' AND channel = 'in_app'""")
    assert rej == {'title': 'Attendance correction rejected', 'body': f'missed punch for {bs_label(d)} was rejected: you left at 17:00.'}


def test_approval_is_refused_once_payroll_has_locked_the_day(demo, reg):
    from hajiri.attendance.service import recompute_day
    d = workday_ago(2)
    out = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})
    recompute_day(demo['sita'], d)
    execute('UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = %s', [demo['sita'], d])
    with pytest.raises(ConflictError, match='this day is now locked by payroll; reject the request and let HR raise a supplementary correction'):
        _approve(reg['ram'], out['id'])
    assert one('SELECT status FROM regularisation_request WHERE id = %s', [out['id']])['status'] == 'pending'
    assert wf.instance_for_object('regularisation_request', out['id'])['state'] == 'pending'


def test_creating_on_a_locked_day_is_refused(demo, reg):
    from hajiri.attendance.service import recompute_day
    d = workday_ago(2)
    recompute_day(demo['sita'], d)
    execute('UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = %s', [demo['sita'], d])
    with pytest.raises(ConflictError, match='this day is locked by payroll; HR must raise a supplementary correction'):
        services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})


def test_the_handler_refuses_a_request_that_is_no_longer_pending(reg):
    out = services.create_request(reg['sita'].user, {'localDate': workday_ago(2), 'kind': 'half_day', 'reason': 'doctor'})
    execute("UPDATE regularisation_request SET status = 'cancelled' WHERE id = %s", [out['id']])
    with pytest.raises(ConflictError, match='request already cancelled'):
        services.apply_decision(out['id'], 'approved', wf.DecisionContext(reg['ram'].user_id, None))
    with pytest.raises(NotFoundError, match='regularisation request not found'):
        services.apply_decision('00000000-0000-7000-8000-00000000ffff', 'approved', wf.DecisionContext(reg['ram'].user_id, None))


def test_the_handler_is_registered():
    assert wf.handler_for('regularisation_request') is services.apply_decision


def test_list_scopes(demo, reg, login_as):
    d = workday_ago(2)
    mine = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'half_day', 'reason': 'doctor'})
    rams = services.create_request(reg['ram'].user, {'localDate': d, 'kind': 'on_duty', 'reason': 'client'})
    ids = lambda xs: {str(x['id']) for x in xs}  # noqa: E731
    assert ids(services.list_requests(reg['sita'].user, {})) == {mine['id']}
    assert ids(services.list_requests(reg['ram'].user, {})) == {mine['id'], rams['id']}
    assert ids(services.list_requests(reg['ram'].user, {'scope': 'own'})) == {rams['id']}
    assert ids(services.list_requests(reg['hr'].user, {})) == {mine['id'], rams['id']}
    assert ids(services.list_requests(reg['hr'].user, {'employeeId': demo['sita']})) == {mine['id']}
    assert ids(services.list_requests(reg['hr'].user, {'status': 'approved'})) == set()
    assert ids(services.list_requests(reg['hr'].user, {'from': days_ago(-1)})) == set()
    with pytest.raises(ForbiddenError, match='scope all requires HR'):
        services.list_requests(reg['ram'].user, {'scope': 'all'})
    att = login_as(['attendance_admin'])
    assert ids(services.list_requests(att.user, {})) == {mine['id'], rams['id']}
    row = services.list_requests(reg['hr'].user, {'employeeId': demo['sita']})[0]
    assert row['emp_code'] == 'E001' and row['name_en'] == 'Sita Sharma' and row['kind'] == 'half_day'


def test_cancel_rules(demo, reg, other_company):
    out = services.create_request(reg['sita'].user, {'localDate': workday_ago(2), 'kind': 'half_day', 'reason': 'doctor'})
    from tests.web.factories import make_user
    stranger = make_user(other_company, roles=['hr'])
    with pytest.raises(NotFoundError, match='request not found'):
        services.cancel_request(stranger, out['id'])
    assert services.cancel_request(reg['sita'].user, out['id']) == {'ok': True}
    assert wf.instance_for_object('regularisation_request', out['id'])['state'] == 'cancelled'
    assert audit_actions(out['id'])[-1] == 'cancel'
    with pytest.raises(ConflictError, match=r'only pending requests can be cancelled \(status cancelled\)'):
        services.cancel_request(reg['sita'].user, out['id'])


# ---------------------------------------------------------------- HR manual punch (TS POST /punch/manual)

def test_manual_punch_saves_audits_and_recomputes(demo, reg):
    d = workday_ago(2)
    body = {'employeeId': demo['sita'], 'localDate': d, 'time': '10:00', 'direction': 'in', 'reason': 'device offline'}
    r = services.manual_punch(reg['hr'].user, body)
    assert r['duplicate'] is False and r['day'].status == 'Incomplete'
    p = one('SELECT source, verify_mode, device_sn, direction, raw FROM punch_event WHERE id = %s', [r['punchId']])
    assert (p['source'], p['verify_mode'], p['device_sn'], p['direction']) == ('manual', 'manual', 'MANUAL-DEMO', 'in')
    assert p['raw'] == {'reason': 'device offline', 'enteredBy': reg['hr'].user_id}
    assert audit_actions(r['punchId']) == ['manual_punch']
    assert services.manual_punch(reg['hr'].user, body) == {'duplicate': True}
    out = services.manual_punch(reg['hr'].user, {**body, 'time': '18:00', 'direction': 'out'})
    assert (out['day'].status, out['day'].worked_min) == ('Present', 480)


def test_manual_punch_guards(demo, reg, login_as, other_company):
    d = workday_ago(2)
    with pytest.raises(ForbiddenError, match='requires role'):
        services.manual_punch(reg['ram'].user, {'employeeId': demo['sita'], 'localDate': d, 'time': '10:00', 'direction': 'in',
                                                'reason': 'nope'})
    me = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E950', 'HR Person') RETURNING id",
                 [demo['company_id']])['id'])
    me2 = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E951', 'Admin Person') RETURNING id",
                  [demo['company_id']])['id'])
    hr_self = login_as(['hr'], me)
    with pytest.raises(ForbiddenError, match='someone else must add punches to your own attendance'):
        services.manual_punch(hr_self.user, {'employeeId': me, 'localDate': d, 'time': '10:00', 'direction': 'in',
                                             'reason': 'mine'})
    admin_self = login_as(['admin'], me2)
    assert services.manual_punch(admin_self.user, {'employeeId': me2, 'localDate': d, 'time': '10:00', 'direction': 'in',
                                                   'reason': 'admin may'})['duplicate'] is False
    from tests.web.factories import make_employee
    e = make_employee(other_company)
    with pytest.raises(NotFoundError, match='employee not found'):
        services.manual_punch(reg['hr'].user, {'employeeId': str(e.id), 'localDate': d, 'time': '10:00', 'direction': 'in',
                                               'reason': 'other company'})
    from hajiri.attendance.service import recompute_day
    d2 = workday_ago(3)
    recompute_day(demo['ram'], d2)
    execute('UPDATE attendance_day SET locked_at = now() WHERE employee_id = %s AND local_date = %s', [demo['ram'], d2])
    with pytest.raises(ConflictError, match='this day is locked by payroll; raise a supplementary correction instead'):
        services.manual_punch(reg['hr'].user, {'employeeId': demo['ram'], 'localDate': d2, 'time': '10:00', 'direction': 'in',
                                               'reason': 'locked'})


# ---------------------------------------------------------------- offline clock-ins

def test_offline_clock_in_becomes_a_missed_punch_correction_with_the_phone_time(demo, reg):
    d = workday_ago(1)
    at = npt(d, '09:58')
    out = services.flush_offline_punch(reg['sita'].user, {'id': 'x1', 'direction': 'in', 'at': at, 'localDate': d,
                                                          'userId': reg['sita'].user_id})
    assert out['status'] == 'pending'
    rr = one('SELECT kind, punch_direction, reason FROM regularisation_request WHERE id = %s', [out['id']])
    assert rr == {'kind': 'missed_punch', 'punch_direction': 'in',
                  'reason': 'Clocked in at 09:58 while offline (time from my phone); sent automatically when the connection came back.'}
    with pytest.raises(ForbiddenError, match='this clock-in was saved for another account'):
        services.flush_offline_punch(reg['sita'].user, {'direction': 'out', 'at': at, 'localDate': d, 'userId': reg['ram'].user_id})


def test_manual_device_is_reused(demo):
    a = services.manual_device_id(demo['company_id'])
    assert services.manual_device_id(demo['company_id']) == a
    assert one('SELECT serial_no, vendor, protocol FROM device WHERE id = %s', [a]) == {
        'serial_no': 'MANUAL-DEMO', 'vendor': 'manual', 'protocol': 'manual'}


def test_an_approved_night_shift_out_punch_lands_on_the_next_day_and_is_attributed(demo, reg):
    """applyRegularisationDecision: the punch_event carries the punch's own Nepal date and local time, the request id and
    the approver; the day of the request is recomputed."""
    d = workday_ago(3)
    nxt = (__import__('datetime').date.fromisoformat(d) + __import__('datetime').timedelta(days=1)).isoformat()
    out = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(nxt, '06:00'),
                                                     'punchDirection': 'out', 'reason': 'night shift out'})
    _approve(reg['ram'], out['id'])
    rr = one('SELECT status, resulting_punch_id FROM regularisation_request WHERE id = %s', [out['id']])
    assert rr['status'] == 'approved'
    p = one("""SELECT local_date::text AS local_date, device_local_time::text AS dlt, vendor, verify_mode, source, device_user_pin,
                      direction, raw FROM punch_event WHERE id = %s""", [rr['resulting_punch_id']])
    assert p['local_date'] == nxt
    assert p['dlt'].startswith(f'{nxt} 06:00:00')
    assert (p['vendor'], p['verify_mode'], p['source'], p['device_user_pin'], p['direction']) == \
        ('manual', 'manual', 'regularisation', 'E001', 'out')
    assert p['raw'] == {'requestId': out['id'], 'approvedBy': reg['ram'].user_id}
    # The approval queued the request's own date, and the approve call drained the queue at once (recomputeQueuedFor).
    assert one('SELECT 1 AS x FROM recompute_queue WHERE employee_id = %s', [demo['sita']]) is None
    assert one('SELECT 1 AS x FROM attendance_day WHERE employee_id = %s AND local_date = %s', [demo['sita'], d]) is not None


def test_a_rejected_request_queues_no_recompute_and_makes_no_punch(demo, reg):
    d = workday_ago(3)
    out = services.create_request(reg['sita'].user, {'localDate': d, 'kind': 'missed_punch', 'punchTime': npt(d, '18:00'),
                                                     'punchDirection': 'out', 'reason': 'forgot'})
    execute('DELETE FROM recompute_queue WHERE employee_id = %s', [demo['sita']])
    inst = wf.instance_for_object('regularisation_request', out['id'])
    wf.decide(reg['ram'].user, inst['id'], 'reject', reason='no')
    assert one('SELECT 1 AS x FROM recompute_queue WHERE employee_id = %s', [demo['sita']]) is None
    assert one("SELECT 1 AS x FROM punch_event WHERE employee_id = %s AND source = 'regularisation'", [demo['sita']]) is None


def test_hr_on_behalf_missed_punch_is_recomputed_at_once(demo, reg):
    """recomputeQueuedFor after an HR fix: the queue for that employee is drained and the day reflects the punch."""
    d = workday_ago(4)
    out = services.create_request(reg['hr'].user, {'employeeId': demo['sita'], 'localDate': d, 'kind': 'missed_punch',
                                                   'punchTime': npt(d, '10:00'), 'punchDirection': 'in', 'reason': 'device offline'})
    assert out['status'] == 'approved'
    assert one('SELECT 1 AS x FROM recompute_queue WHERE employee_id = %s AND local_date = %s', [demo['sita'], d]) is None
    day = one('SELECT status FROM attendance_day WHERE employee_id = %s AND local_date = %s', [demo['sita'], d])
    assert day is not None and day['status'] == 'Incomplete'
    after = one("SELECT after FROM audit_log WHERE row_id = %s AND action = 'create'", [out['id']])['after']
    assert after['onBehalf'] is True and after['employeeId'] == demo['sita']
