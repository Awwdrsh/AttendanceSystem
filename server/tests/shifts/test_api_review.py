"""Contract review of /api/v1/shifts/...: the exact response shapes the React pages read (ShiftsPage, HolidaysPage,
RostersPage, SetupPage, TeamCalendarPage, MyAttendancePage, LeavePage, HomePage), field by field against the TS routes at
commit 65a9079 (node-postgres with DATE and INT8 parsed as text, NUMERIC as text), and the TS error messages."""
from __future__ import annotations

import pytest

from hajiri.db.models import Company
from tests.shifts.test_api_contract import _report_to, api  # noqa: F401 - the fixture
from tests.web.factories import make_employee

SHIFT_KEYS = {'id', 'code', 'name_en', 'name_ne', 'start', 'end', 'grace_in_min', 'grace_out_min', 'early_in_window_min',
              'late_out_window_min', 'break_minutes', 'half_day_threshold_min', 'absent_threshold_min', 'pairing_mode',
              'rounding_min', 'rounding_direction', 'crosses_midnight', 'assigned_employees'}


def test_shift_rows_have_the_ts_columns_and_types(api, demo):
    hr = api(['hr'])
    # SetupPage's one-click default and ShiftsPage's blank form send every field; nameNe '' is kept as ''.
    body = {'code': 'DAY', 'nameEn': 'Day', 'nameNe': '', 'start': '09:30', 'end': '17:30', 'graceInMin': 15, 'graceOutMin': 0,
            'earlyInWindowMin': 60, 'lateOutWindowMin': 120, 'breakMinutes': 30, 'halfDayThresholdMin': 240,
            'absentThresholdMin': 120, 'pairingMode': 'first_last', 'roundingMin': 5, 'roundingDirection': 'down'}
    sid = hr('post', '', body).json()['id']
    row = hr('get', f'/{sid}').json()['shift']
    assert set(row) == SHIFT_KEYS
    assert row == {'id': sid, 'code': 'DAY', 'name_en': 'Day', 'name_ne': '', 'start': '09:30:00', 'end': '17:30:00',
                   'grace_in_min': 15, 'grace_out_min': 0, 'early_in_window_min': 60, 'late_out_window_min': 120,
                   'break_minutes': 30, 'half_day_threshold_min': 240, 'absent_threshold_min': 120, 'pairing_mode': 'first_last',
                   'rounding_min': 5, 'rounding_direction': 'down', 'crosses_midnight': False, 'assigned_employees': '0'}
    listed = hr('get', '').json()['shifts']
    assert [s['code'] for s in listed] == sorted(s['code'] for s in listed)          # ORDER BY code
    gen = next(s for s in listed if s['id'] == demo['shift_id'])
    assert set(gen) == SHIFT_KEYS and gen['assigned_employees'] == '2'               # int8 count as text
    # ShiftsPage edit: the whole form goes back, PATCH applies only what was sent.
    assert hr('patch', f'/{sid}', {'graceInMin': 10}).json() == {'ok': True, 'recomputeQueued': 0}
    assert hr('get', f'/{sid}').json()['shift']['break_minutes'] == 30


def test_duplicate_shift_code_answers_409_already_exists(api, demo):
    hr = api(['hr'])
    body = {'code': 'DUP', 'nameEn': 'One', 'start': '09:00', 'end': '17:00'}
    assert hr('post', '', body).status_code == 201
    r = hr('post', '', body)
    assert r.status_code == 409 and r.json()['error'] == 'already exists'   # pg 23505 in the TS error handler


def test_holiday_rows_have_text_dates_and_the_ts_columns(api, demo):
    hr, sita = api(['hr']), api(['employee'], demo['sita'])
    created = hr('post', '/holidays', {'localDate': '2026-12-31', 'nameEn': 'Anniversary', 'nameNe': '', 'genderScope': 'all',
                                       'optional': False})
    assert created.status_code == 201 and set(created.json()) == {'id', 'recomputeQueued'}
    assert isinstance(created.json()['recomputeQueued'], int)
    hid = created.json()['id']
    rows = sita('get', '/holidays').json()['holidays']                    # everyone reads holidays (SetupPage, pickers)
    assert rows == [{'id': hid, 'branch_id': None, 'local_date': '2026-12-31', 'bs_date': rows[0]['bs_date'],
                     'name_en': 'Anniversary', 'name_ne': '', 'gender_scope': 'all', 'optional': False}]
    assert rows[0]['bs_date'] and rows[0]['bs_date'].startswith('2083-09-')
    # HolidaysPage edit sends {localDate, nameEn, nameNe, genderScope, optional}.
    assert hr('patch', f'/holidays/{hid}', {'localDate': '2027-01-01', 'nameEn': 'New year', 'nameNe': '', 'genderScope': 'female',
                                            'optional': True}).json() == {'ok': True}
    row = hr('get', '/holidays').json()['holidays'][0]
    assert (row['local_date'], row['name_en'], row['gender_scope'], row['optional']) == ('2027-01-01', 'New year', 'female', True)
    assert row['bs_date'].startswith('2083-09-')
    assert hr('post', '/holidays/import-default', {'bsYear': 2083}).json() == {'inserted': 42, 'total': 42}
    bad = hr('post', '/holidays/import-default', {'bsYear': 2082})
    assert bad.status_code == 400 and bad.json() == {'error': 'default list available for BS 2083 only; add others via POST /shifts/holidays'}
    assert hr('delete', f'/holidays/{hid}').json() == {'ok': True}


def test_locations_return_numeric_coordinates_as_text(api, demo):
    hr = api(['hr'])
    lid = hr('post', '/locations', {'name': 'HQ', 'lat': 27.7172, 'lng': 85.324, 'radiusM': 100}).json()['id']
    assert hr('get', '/locations').json() == {'locations': [
        {'id': lid, 'branch_id': None, 'name': 'HQ', 'lat': '27.717200', 'lng': '85.324000', 'radius_m': 100}]}


def test_calendar_days_shape(api, demo):
    hr, sita = api(['hr']), api(['employee'], demo['sita'])
    hr('post', '/holidays', {'localDate': '2026-10-31', 'nameEn': 'Company day'})
    days = sita('get', '/calendar', **{'from': '2026-10-30', 'to': '2026-11-01', 'employeeId': demo['sita']}).json()['days']
    assert [d['date'] for d in days] == ['2026-10-30', '2026-10-31', '2026-11-01']
    sat = days[1]
    assert set(sat) == {'date', 'bsDate', 'weekday', 'weeklyOff', 'holidays'}
    assert sat['weekday'] == 6 and sat['weeklyOff'] is True and sat['bsDate'].startswith('2083-07-')
    assert sat['holidays'] == [{'local_date': '2026-10-31', 'name_en': 'Company day', 'name_ne': None, 'gender_scope': 'all',
                                'optional': False, 'branch_id': None}]
    assert days[0]['holidays'] == [] and days[0]['weeklyOff'] is False
    bad = sita('get', '/calendar', **{'from': '2026-11-02', 'to': '2026-11-01'})
    assert bad.status_code == 400 and bad.json() == {'error': 'range must be 1 to 400 days'}


def test_forbidden_answers_use_the_workflow_helpers_message(api, demo):
    """The TS calendar and roster routes called workflow assertManages: 'not allowed to act for this employee'."""
    other = make_employee(Company.objects.get(pk=demo['company_id']), code='E904', name='Unrelated Manager')
    stranger = api(['manager'], str(other.id))
    sita = api(['employee'], demo['sita'])
    forbidden = {'error': 'not allowed to act for this employee'}
    r = sita('get', '/calendar', **{'from': '2026-10-01', 'to': '2026-10-07', 'employeeId': demo['ram']})
    assert r.status_code == 403 and r.json() == forbidden
    cell = {'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': None}
    r = stranger('put', '/roster', {'rows': [cell]})
    assert r.status_code == 403 and r.json() == forbidden
    r = stranger('delete', '/roster', {'employeeId': demo['sita'], 'localDate': '2026-11-02'})
    assert r.status_code == 403 and r.json() == forbidden
    r = sita('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr or attendance_admin or manager'}


def test_roster_page_round_trip(api, demo):
    """RostersPage: GET grid, PUT one cell (shift or off), DELETE a cell with a JSON body, copy week, publish."""
    _report_to(demo['sita'], demo['ram'])
    ram = api(['manager'], demo['ram'])
    week = {'from': '2026-11-01', 'to': '2026-11-07'}
    assert ram('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-02', 'shiftId': demo['shift_id']}]}).json() == {'upserted': 1}
    assert ram('put', '/roster', {'rows': [{'employeeId': demo['sita'], 'localDate': '2026-11-03', 'shiftId': None}]}).json() == {'upserted': 1}
    grid = ram('get', '/roster', **week).json()
    assert set(grid) == {'employees', 'roster'}
    sita_row = next(e for e in grid['employees'] if e['id'] == demo['sita'])
    assert sita_row['weekly_off_days'] == [6] and sita_row['default_shift_id'] == demo['shift_id'] and sita_row['emp_code'] == 'E001'
    assert sorted(grid['roster'], key=lambda r: r['local_date']) == [
        {'employee_id': demo['sita'], 'local_date': '2026-11-02', 'shift_id': demo['shift_id']},
        {'employee_id': demo['sita'], 'local_date': '2026-11-03', 'shift_id': None}]
    assert ram('delete', '/roster', {'employeeId': demo['sita'], 'localDate': '2026-11-03'}).json() == {'deleted': 1}
    assert ram('delete', '/roster', {'employeeId': demo['sita'], 'localDate': '2026-11-03'}).json() == {'deleted': 0}
    assert ram('post', '/roster/copy-week', {'weekStart': '2026-11-08'}).json() == {'copied': 1, 'skippedLocked': 0}
    assert ram('post', '/roster/publish', {'from': '2026-11-01', 'to': '2026-11-14'}).json() == {'notified': 1}


@pytest.mark.parametrize('path', ['/not-a-uuid', '/holidays/nope', '/locations/nope'])
def test_malformed_ids_are_zod_validation_errors(api, demo, path):
    r = api(['hr'])('delete', path)
    assert r.status_code == 400 and r.json()['error'] == 'validation'
