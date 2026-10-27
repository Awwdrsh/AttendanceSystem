"""Rules of routes.ts that the TS tests did not reach directly: PATCH semantics, recompute fan-out, audit entries,
scoping and the error messages."""
from __future__ import annotations

import pytest

from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.shifts import services as svc
from tests.web.factories import audit_actions


@pytest.fixture
def hr(login_as):
    return login_as(['hr']).user


def _queue(emp=None):
    q = 'SELECT employee_id::text AS e, local_date::text AS d, reason FROM recompute_queue' + (' WHERE employee_id = %s' if emp else '') + ' ORDER BY 1, 2'
    return rows(q, [emp] if emp else [])


def test_shift_patch_changes_only_given_fields_and_recomputes_this_month(demo, hr):
    execute('DELETE FROM recompute_queue')
    r = svc.update_shift(hr, demo['shift_id'], {'graceInMin': 5})
    assert r == {'ok': True, 'recomputeQueued': 2}
    s = svc.get_shift(hr, demo['shift_id'])
    assert s['grace_in_min'] == 5 and s['early_in_window_min'] == 60 and s['late_out_window_min'] == 120  # no defaults applied
    q = _queue(demo['sita'])
    assert q and all(x['reason'] == 'shift_rules_changed' for x in q)
    assert q[0]['d'].endswith('-01')
    assert 'update' in audit_actions(demo['shift_id'])
    before = one("SELECT before FROM audit_log WHERE row_id = %s AND action = 'update'", [demo['shift_id']])['before']
    assert before['grace_in_min'] == 15 and before['code'] == 'GEN'


def test_shift_not_found_across_companies(demo, hr, login_as):
    c2 = one("INSERT INTO company (code, name_en) VALUES ('C9', 'C9') RETURNING id")
    other = login_as(['hr'], company_id=str(c2['id'])).user
    with pytest.raises(NotFoundError) as e:
        svc.get_shift(other, demo['shift_id'])
    assert e.value.message == 'shift not found'
    with pytest.raises(NotFoundError):
        svc.update_shift(other, demo['shift_id'], {'graceInMin': 1})
    with pytest.raises(NotFoundError):
        svc.delete_shift(other, demo['shift_id'])
    assert svc.shifts(other) == []


def test_delete_shift_with_history_is_refused_and_unused_shift_deletes(demo, hr):
    sid = svc.create_shift(hr, {'code': 'OLD', 'nameEn': 'Old', 'start': '06:00', 'end': '14:00'})['id']
    execute("INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, '2026-01-05', %s)", [demo['sita'], sid])
    with pytest.raises(ConflictError) as e:
        svc.delete_shift(hr, sid)
    assert e.value.message == 'shift has historical assignments or attendance; it cannot be deleted'
    execute('DELETE FROM roster WHERE shift_id = %s', [sid])
    assert svc.delete_shift(hr, sid) == {'ok': True}
    assert 'delete' in audit_actions(sid)
    with pytest.raises(NotFoundError):
        svc.delete_shift(hr, sid)


def test_shift_mutations_need_the_attendance_area(demo, login_as):
    att = login_as(['attendance_admin']).user
    assert svc.create_shift(att, {'code': 'A1', 'nameEn': 'A', 'start': '09:00', 'end': '17:00'})['id']
    emp = login_as(['employee'], demo['sita']).user
    with pytest.raises(ForbiddenError) as e:
        svc.create_shift(emp, {'code': 'A2', 'nameEn': 'A', 'start': '09:00', 'end': '17:00'})
    assert e.value.message == 'requires role hr or attendance_admin'
    assert any(s['code'] == 'GEN' for s in svc.shifts(emp))  # everyone may read the list


def test_duplicate_shift_code_is_a_database_conflict(demo, hr):
    from django.db import IntegrityError
    with pytest.raises(IntegrityError):
        svc.create_shift(hr, {'code': 'GEN', 'nameEn': 'Again', 'start': '09:00', 'end': '17:00'})


def test_assign_checks_company_and_recomputes_today_when_backdated(demo, hr):
    with pytest.raises(NotFoundError) as e:
        svc.assign_shift(hr, {'employeeId': demo['sita'], 'shiftId': '00000000-0000-7000-8000-0000000000ff', 'from': '2026-10-01'})
    assert e.value.message == 'employee or shift not found'
    execute('DELETE FROM recompute_queue')
    svc.assign_shift(hr, {'employeeId': demo['sita'], 'shiftId': demo['shift_id'], 'from': '2026-02-01'})
    from hajiri.core.clock import npt_today
    assert [x['d'] for x in _queue(demo['sita'])] == [npt_today().isoformat()]
    assert 'assign' in audit_actions(demo['sita'])
    got = rows('SELECT valid::text AS v FROM shift_assignment WHERE employee_id = %s ORDER BY lower(valid)', [demo['sita']])
    assert [g['v'] for g in got] == ['[2026-01-01,2026-02-01)', '[2026-02-01,)']
    # assigning again from an earlier date replaces the later assignment
    svc.assign_shift(hr, {'employeeId': demo['sita'], 'shiftId': demo['shift_id'], 'from': '2026-01-15'})
    got = rows('SELECT valid::text AS v FROM shift_assignment WHERE employee_id = %s ORDER BY lower(valid)', [demo['sita']])
    assert [g['v'] for g in got] == ['[2026-01-01,2026-01-15)', '[2026-01-15,)']


def test_holiday_update_recomputes_old_and_new_dates_and_keeps_untouched_fields(demo, hr):
    h = svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Anniversary', 'genderScope': 'female', 'optional': True})
    execute('DELETE FROM recompute_queue')
    assert svc.update_holiday(hr, h['id'], {'localDate': '2027-01-01'}) == {'ok': True}
    row = one('SELECT local_date::text AS d, bs_date, name_en, gender_scope, optional FROM holiday WHERE id = %s', [h['id']])
    assert row == {'d': '2027-01-01', 'bs_date': '2083-09-17', 'name_en': 'Anniversary', 'gender_scope': 'female', 'optional': True}
    assert sorted({x['d'] for x in _queue()}) == ['2026-12-31', '2027-01-01']
    assert {x['reason'] for x in _queue()} == {'holiday_changed'}
    assert audit_actions(h['id']) == ['create', 'update']
    with pytest.raises(NotFoundError):
        svc.update_holiday(hr, '00000000-0000-7000-8000-0000000000ff', {'nameEn': 'x'})


def test_holiday_delete_recomputes_the_date(demo, hr):
    h = svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Anniversary'})
    execute('DELETE FROM recompute_queue')
    assert svc.delete_holiday(hr, h['id']) == {'ok': True}
    assert [(x['d'], x['reason']) for x in _queue()] == [('2026-12-31', 'holiday_removed'), ('2026-12-31', 'holiday_removed')]
    with pytest.raises(NotFoundError) as e:
        svc.delete_holiday(hr, h['id'])
    assert e.value.message == 'holiday not found'


def test_holiday_branch_must_belong_to_the_company(demo, hr):
    with pytest.raises(NotFoundError) as e:
        svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'x', 'branchId': '00000000-0000-7000-8000-0000000000ff'})
    assert e.value.message == 'branch not found'
    # a branch holiday and a company-wide one with the same name may coexist
    svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'x'})
    svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'x', 'branchId': demo['branch_id']})
    assert len(svc.holidays(hr, {'branchId': demo['branch_id']})) == 2
    assert len(svc.holidays(hr, {'from': '2027-01-01'})) == 0


def test_import_only_for_2083(demo, hr):
    with pytest.raises(BadRequestError) as e:
        svc.import_default_holidays(hr, {'bsYear': 2084})
    assert e.value.message == 'default list available for BS 2083 only; add others via POST /shifts/holidays'
    r = svc.import_default_holidays(hr, {'bsYear': 2083})
    assert r['inserted'] == 42
    a = one("SELECT after FROM audit_log WHERE action = 'import' AND table_name = 'holiday'")['after']
    assert a == {'bsYear': 2083, 'inserted': 42}
    teej = one("SELECT gender_scope, optional FROM holiday WHERE name_en = 'Haritalika Teej'")
    assert teej == {'gender_scope': 'female', 'optional': False}


def test_calendar_range_and_employee_scope(demo, hr, login_as):
    with pytest.raises(BadRequestError) as e:
        svc.calendar(hr, {'from': '2026-10-10', 'to': '2026-10-01'})
    assert e.value.message == 'range must be 1 to 400 days'
    with pytest.raises(BadRequestError):
        svc.calendar(hr, {'from': '2026-01-01', 'to': '2027-02-05'})
    execute("UPDATE employee SET weekly_off_days = '{0,6}' WHERE id = %s", [demo['sita']])
    cal = svc.calendar(hr, {'from': '2026-10-18', 'to': '2026-10-18', 'employeeId': demo['sita']})
    assert cal['days'][0]['weeklyOff'] is True and cal['days'][0]['weekday'] == 0
    ram = login_as(['employee'], demo['ram']).user
    with pytest.raises(ForbiddenError):
        svc.calendar(ram, {'from': '2026-10-18', 'to': '2026-10-18', 'employeeId': demo['sita']})
    assert svc.calendar(ram, {'from': '2026-10-18', 'to': '2026-10-18', 'employeeId': demo['ram']})['days'][0]['weeklyOff'] is False
    with pytest.raises(NotFoundError) as e:
        svc.calendar(hr, {'from': '2026-10-18', 'to': '2026-10-18', 'employeeId': '00000000-0000-7000-8000-0000000000ff'})
    assert e.value.message == 'employee not found'


def test_clear_roster_and_publish(demo, hr):
    svc.save_roster(hr, {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': None},
                                  {'employeeId': demo['sita'], 'localDate': '2026-11-03', 'shiftId': demo['shift_id']}]})
    from hajiri.core import auth
    svc_user = auth.create_user(company_id=demo['company_id'], employee_id=demo['sita'], email='sita@test.local', name='Sita',
                                password='correct-horse-battery-staple', roles=['employee'])
    r = svc.publish_roster(hr, {'from': '2026-11-01', 'to': '2026-11-07'})
    assert r == {'notified': 1}
    n = rows("SELECT user_id, channel, title, body, payload FROM notification_outbox WHERE template = 'roster.published' ORDER BY channel")
    assert [x['channel'] for x in n] == ['in_app', 'push']
    assert str(n[0]['user_id']) == svc_user.id and n[0]['title'] == 'Roster published'
    assert n[0]['body'] == 'Your shifts for 15 Kartik 2083 to 21 Kartik 2083 have been published (2 days).'
    assert n[0]['payload'] == {'from': '2026-11-01', 'to': '2026-11-07'}
    assert svc.clear_roster(hr, {'employeeId': demo['sita'], 'localDate': '2026-11-02'}) == {'deleted': 1}
    assert svc.clear_roster(hr, {'employeeId': demo['sita'], 'localDate': '2026-11-02'}) == {'deleted': 0}
    assert 'publish' in [a['action'] for a in rows("SELECT action FROM audit_log WHERE table_name = 'roster'")]


def test_roster_needs_manager_or_attendance_role(demo, login_as):
    emp = login_as(['employee'], demo['sita']).user
    for fn, arg in ((svc.roster, {'from': '2026-11-01', 'to': '2026-11-07'}), (svc.copy_week, {'weekStart': '2026-11-01'}),
                    (svc.publish_roster, {'from': '2026-11-01', 'to': '2026-11-07'})):
        with pytest.raises(ForbiddenError) as e:
            fn(emp, arg)
        assert e.value.message == 'requires role hr or attendance_admin or manager'
    with pytest.raises(ForbiddenError) as e:
        svc.team_swaps(emp, None, None)
    assert e.value.message == 'only managers and HR see team swaps'


def test_location_update_and_scoping(demo, hr):
    c = svc.create_location(hr, {'name': 'HQ', 'lat': 27.7, 'lng': 85.3})
    assert svc.update_location(hr, c['id'], {'radiusM': 300}) == {'ok': True}
    loc = svc.locations(hr)[0]
    assert loc['radius_m'] == 300 and loc['name'] == 'HQ'
    with pytest.raises(NotFoundError) as e:
        svc.update_location(hr, '00000000-0000-7000-8000-0000000000ff', {'name': 'x'})
    assert e.value.message == 'location not found'
    with pytest.raises(NotFoundError):
        svc.delete_location(hr, '00000000-0000-7000-8000-0000000000ff')
    assert audit_actions(c['id']) == ['create', 'update']


def test_manager_copy_week_and_publish_cover_their_team_only(demo, login_as):
    """copy-week and publish: HR acts on the company, a line manager only on their reports (hj_manages)."""
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    hr = login_as(['hr']).user
    ram = login_as(['manager'], demo['ram']).user
    svc.save_roster(hr, {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']},
                                  {'employeeId': demo['ram'], 'localDate': '2026-11-03', 'shiftId': None}]})
    # Ram manages Sita, not himself: only Sita's row is copied and notified.
    assert svc.copy_week(ram, {'weekStart': '2026-11-08'}) == {'copied': 1, 'skippedLocked': 0}
    got = rows("SELECT employee_id::text AS e, local_date::text AS d FROM roster WHERE local_date >= '2026-11-08'")
    assert got == [{'e': demo['sita'], 'd': '2026-11-09'}]
    assert {x['reason'] for x in _queue(demo['sita']) if x['d'] == '2026-11-09'} == {'roster_copied'}
    a = one("SELECT after FROM audit_log WHERE action = 'copy_week' AND table_name = 'roster'")['after']
    assert a == {'weekStart': '2026-11-08', 'copied': 1, 'skippedLocked': 0}
    assert svc.publish_roster(ram, {'from': '2026-11-01', 'to': '2026-11-07'}) == {'notified': 1}
    assert svc.publish_roster(hr, {'from': '2026-11-01', 'to': '2026-11-07'}) == {'notified': 2}
    emps = {str(r['employee_id']) for r in rows("SELECT employee_id FROM notification_outbox WHERE template = 'roster.published'")}
    assert emps == {demo['sita'], demo['ram']}


def test_holiday_patch_branch_can_be_set_and_cleared(demo, hr):
    h = svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Branch day', 'branchId': demo['branch_id']})
    assert str(one('SELECT branch_id FROM holiday WHERE id = %s', [h['id']])['branch_id']) == demo['branch_id']
    svc.update_holiday(hr, h['id'], {'nameEn': 'Renamed'})   # branch untouched when not sent
    assert str(one('SELECT branch_id FROM holiday WHERE id = %s', [h['id']])['branch_id']) == demo['branch_id']
    svc.update_holiday(hr, h['id'], {'branchId': None})
    assert one('SELECT branch_id, name_en FROM holiday WHERE id = %s', [h['id']]) == {'branch_id': None, 'name_en': 'Renamed'}
    with pytest.raises(NotFoundError) as e:
        svc.update_holiday(hr, h['id'], {'branchId': '00000000-0000-7000-8000-0000000000ff'})
    assert e.value.message == 'branch not found'


def test_roster_save_checks_employee_company_and_is_all_or_nothing(demo, hr):
    with pytest.raises(NotFoundError) as e:
        svc.save_roster(hr, {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']},
                                      {'employeeId': '00000000-0000-7000-8000-0000000000ff', 'localDate': '2026-11-02', 'shiftId': None}]})
    assert e.value.message == 'employee 00000000-0000-7000-8000-0000000000ff not found'
    assert rows('SELECT 1 FROM roster') == []   # the first row was rolled back
    from hajiri.core.errors import ValidationError
    with pytest.raises(ValidationError):
        svc.save_roster(hr, {'rows': []})

