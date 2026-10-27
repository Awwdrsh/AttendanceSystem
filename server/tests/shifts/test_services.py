"""Ports of apps/api/test/shifts.test.ts and shifts-qa.test.ts against the service layer (the routes' bodies and
answers are unchanged; HTTP status codes become the typed errors that carry them)."""
from __future__ import annotations

import pytest

from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.shifts import services as svc


@pytest.fixture
def hr(login_as):
    return login_as(['hr']).user


def _night(hr) -> str:
    return svc.create_shift(hr, {'code': 'NIGHT', 'nameEn': 'Night', 'start': '22:00', 'end': '06:00', 'breakMinutes': 30})['id']


# ---------------------------------------------------------------- shifts.test.ts

def test_creates_a_night_shift_and_reports_crosses_midnight(demo, hr):
    night = _night(hr)
    found = next(s for s in svc.shifts(hr) if s['code'] == 'NIGHT')
    assert found['id'] == night
    assert found['crosses_midnight'] is True and found['assigned_employees'] == 0
    with pytest.raises(ValidationError) as e:
        svc.create_shift(hr, {'code': 'BAD', 'nameEn': 'Bad', 'start': '25:00', 'end': '06:00'})
    assert e.value.status == 400 and e.value.issues[0]['path'] == 'start' and e.value.issues[0]['message'] == 'HH:MM'
    # defaults from the zod schema
    assert found['late_out_window_min'] == 360 and found['pairing_mode'] == 'alternating' and found['break_minutes'] == 30


def test_assigns_effective_dated_and_refuses_to_delete_a_shift_in_use(demo, hr):
    night = _night(hr)
    assert svc.assign_shift(hr, {'employeeId': demo['ram'], 'shiftId': night, 'from': '2026-10-01'}) == {'ok': True}
    got = rows('SELECT shift_id, valid::text AS valid FROM shift_assignment WHERE employee_id = %s ORDER BY lower(valid)', [demo['ram']])
    assert len(got) == 2
    assert str(got[0]['shift_id']) == demo['shift_id'] and got[0]['valid'] == '[2026-01-01,2026-10-01)'
    assert str(got[1]['shift_id']) == night and got[1]['valid'] == '[2026-10-01,)'
    with pytest.raises(ConflictError) as e:
        svc.delete_shift(hr, night)
    assert e.value.status == 409 and 'assigned to 1 employee' in e.value.message


def test_roster_upsert_queues_recompute_and_refuses_locked_days(demo, hr):
    night = _night(hr)
    execute('DELETE FROM recompute_queue')
    r = svc.save_roster(hr, {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-10-20', 'shiftId': night},
                                      {'employeeId': demo['sita'], 'localDate': '2026-10-21', 'shiftId': None}]})
    assert r['upserted'] == 2
    q = rows('SELECT local_date::text AS d FROM recompute_queue WHERE employee_id = %s ORDER BY local_date', [demo['sita']])
    assert [x['d'] for x in q] == ['2026-10-20', '2026-10-21']
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at)
               VALUES (%s, '2026-10-22', 1, 'Present', 'h', now())""", [demo['sita']])
    with pytest.raises(ConflictError) as e:
        svc.save_roster(hr, {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-10-22', 'shiftId': night}]})
    assert e.value.status == 409 and e.value.message == 'attendance for 2026-10-22 is locked by payroll'
    copy = svc.copy_week(hr, {'weekStart': '2026-10-25'})
    assert copy['copied'] == 2
    got = rows("SELECT local_date::text AS d, shift_id FROM roster WHERE employee_id = %s AND local_date >= '2026-10-25' ORDER BY 1", [demo['sita']])
    assert [(x['d'], x['shift_id'] and str(x['shift_id'])) for x in got] == [('2026-10-27', night), ('2026-10-28', None)]


def test_holiday_import_is_idempotent_and_the_calendar_marks_saturdays(demo, hr):
    assert svc.import_default_holidays(hr, {'bsYear': 2083}) == {'inserted': 42, 'total': 42}
    assert svc.import_default_holidays(hr, {'bsYear': 2083})['inserted'] == 0
    cal = svc.calendar(hr, {'from': '2026-10-17', 'to': '2026-10-24'})
    sat = next(d for d in cal['days'] if d['date'] == '2026-10-17')
    assert sat['weeklyOff'] is True and sat['bsDate'] == '2083-06-31' and sat['weekday'] == 6
    assert 'Dashain' in sat['holidays'][0]['name_en']
    h = svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'})
    assert h['id'] and h['recomputeQueued'] == 2


def test_locations_crud(demo, hr):
    c = svc.create_location(hr, {'name': 'HQ', 'lat': 27.7172, 'lng': 85.324, 'radiusM': 100})
    assert c['id']
    assert len(svc.locations(hr)) == 1
    assert svc.delete_location(hr, c['id']) == {'ok': True}
    assert svc.locations(hr) == []


# ---------------------------------------------------------------- shifts-qa.test.ts

@pytest.fixture
def qa(demo, login_as):
    """HR, Ram as Sita's manager and an unrelated manager (E903)."""
    hr = login_as(['hr']).user
    ram = login_as(['manager'], demo['ram']).user
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    m = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E903', 'Unrelated Manager') RETURNING id", [demo['company_id']])
    execute("INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')", [m['id']])
    other = login_as(['manager'], str(m['id'])).user
    return {**demo, 'hr': hr, 'mgr': ram, 'other': other}


def test_roster_rows_must_use_shifts_of_the_same_company(qa):
    c2 = one("INSERT INTO company (code, name_en) VALUES ('C3', 'C3') RETURNING id")
    sh = one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'Z', 'Z', '09:00', '17:00') RETURNING id", [c2['id']])
    with pytest.raises(NotFoundError) as e:
        svc.save_roster(qa['hr'], {'rows': [{'employeeId': qa['sita'], 'localDate': '2026-11-02', 'shiftId': str(sh['id'])}]})
    assert e.value.status == 404 and e.value.message == f"shift {sh['id']} not found"
    assert rows('SELECT 1 FROM roster WHERE employee_id = %s', [qa['sita']]) == []


def test_a_manager_rosters_only_direct_reports_and_sees_only_them(qa):
    with pytest.raises(ForbiddenError):
        svc.save_roster(qa['other'], {'rows': [{'employeeId': qa['sita'], 'localDate': '2026-11-02', 'shiftId': qa['shift_id']}]})
    assert svc.save_roster(qa['mgr'], {'rows': [{'employeeId': qa['sita'], 'localDate': '2026-11-02', 'shiftId': qa['shift_id']}]}) == {'upserted': 1}
    view = svc.roster(qa['other'], {'from': '2026-11-01', 'to': '2026-11-07'})
    assert qa['sita'] not in [e['id'] for e in view['employees']]
    assert view['roster'] == []
    own = svc.roster(qa['mgr'], {'from': '2026-11-01', 'to': '2026-11-07'})
    ids = [e['id'] for e in own['employees']]
    assert qa['sita'] in ids and qa['ram'] in ids   # a manager also sees their own row
    assert len(own['roster']) == 1 and own['roster'][0]['local_date'] == '2026-11-02'


def test_copy_week_skips_payroll_locked_target_days(qa):
    svc.save_roster(qa['mgr'], {'rows': [{'employeeId': qa['sita'], 'localDate': '2026-11-02', 'shiftId': qa['shift_id']}]})
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at)
               VALUES (%s, '2026-11-09', 1, 'Present', 'h', now())""", [qa['sita']])
    assert svc.copy_week(qa['hr'], {'weekStart': '2026-11-08'}) == {'copied': 0, 'skippedLocked': 1}
    assert rows("SELECT 1 FROM roster WHERE employee_id = %s AND local_date = '2026-11-09'", [qa['sita']]) == []


def test_duplicate_company_wide_holidays_are_refused_and_the_default_import_stays_idempotent(qa):
    hr = qa['hr']
    svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'})
    with pytest.raises(ConflictError) as e:
        svc.create_holiday(hr, {'localDate': '2026-12-31', 'nameEn': 'Company anniversary'})
    assert e.value.message == 'this holiday already exists for that date'
    assert svc.import_default_holidays(hr, {'bsYear': 2083})['inserted'] == 42
    assert svc.import_default_holidays(hr, {'bsYear': 2083})['inserted'] == 0
    assert one("SELECT count(*)::text AS n FROM holiday WHERE company_id = %s AND local_date = '2026-10-17'", [qa['company_id']])['n'] == '1'
    assert one("SELECT bs_date FROM holiday WHERE company_id = %s AND local_date = '2026-10-17'", [qa['company_id']])['bs_date'] == '2083-06-31'


def test_a_holiday_added_by_a_manager_is_refused_hr_only(qa):
    with pytest.raises(ForbiddenError) as e:
        svc.create_holiday(qa['mgr'], {'localDate': '2026-12-31', 'nameEn': 'x'})
    assert e.value.status == 403 and e.value.message == 'requires role hr or attendance_admin'
