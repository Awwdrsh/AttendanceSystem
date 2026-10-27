"""The pages that replace ShiftsPage.tsx, HolidaysPage.tsx and RostersPage.tsx, through django.test.Client."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core.db import execute, one, rows
from tests.web.factories import hx_triggers, htmx, make_company, sign_in, signed_in_client, toasts


@pytest.fixture
def client_for():
    def make(login):
        c = Client()
        sign_in(c, login.email)
        return c
    return make


@pytest.fixture
def hr(demo, login_as, client_for):
    return client_for(login_as(['hr']))


SHIFT = {'code': 'night', 'name_en': 'Night', 'name_ne': '', 'start': '22:00', 'end': '06:00', 'grace_in_min': 10, 'grace_out_min': 0,
         'early_in_window_min': 60, 'late_out_window_min': 120, 'break_minutes': 30, 'half_day_threshold_min': 240,
         'absent_threshold_min': 120, 'pairing_mode': 'alternating', 'rounding_min': 0, 'rounding_direction': 'nearest'}


# ---------------------------------------------------------------- Settings -> Shifts

def test_shift_list_shows_hours_rules_and_counts(hr):
    r = hr.get('/settings/shifts')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'General 10-18' in html and 'GEN' in html and '10:00 – 18:00' in html and '15 min grace' in html
    assert '4 h / 2 h' in html
    assert 'Working hours and the rules used to mark people late, half day or absent.' in html
    assert '2 employee(s) are on this shift. Move them to another shift first, otherwise the delete is refused.' in html


def test_empty_company_sees_the_empty_state(db):
    c, _ = signed_in_client(make_company(), roles=['hr'])
    html = c.get('/settings/shifts').content.decode()
    assert 'No shifts yet' in html and 'Most companies start with a general 10:00 – 18:00 shift.' in html


def test_new_shift_form_and_save(hr):
    r = hr.get('/_/shifts/new', **htmx('hj-drawer-body'))
    html = r.content.decode()
    assert r.status_code == 200 and 'data-modal-title="New shift"' in html and 'Save shift' in html and 'value="10:00"' in html
    r = hr.post('/_/shifts/new', SHIFT, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Shift saved.'
    t = hx_triggers(r)
    assert 'hj:refresh-shifts' in t and 'hj:close-modal' in t
    s = one("SELECT code, start_time::text AS st, end_time::text AS et, break_minutes, grace_in_min FROM shift WHERE code = 'NIGHT'")
    assert s == {'code': 'NIGHT', 'st': '22:00:00', 'et': '06:00:00', 'break_minutes': 30, 'grace_in_min': 10}
    html = hr.get('/settings/shifts').content.decode()
    assert 'next day' in html and '10 min grace, 30 min break' in html
    assert 'Nobody is on this shift. Past attendance keeps its data.' in html


def test_invalid_shift_is_shown_in_the_form(hr):
    r = hr.post('/_/shifts/new', {**SHIFT, 'start': '25:00'}, **htmx())
    assert r.status_code == 422 and 'HH:MM' in r.content.decode()
    assert not one("SELECT 1 AS x FROM shift WHERE code = 'NIGHT'")


def test_duplicate_code_is_a_toast(hr):
    r = hr.post('/_/shifts/new', {**SHIFT, 'code': 'GEN'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'already exists'


def test_edit_shift(hr, demo):
    r = hr.get(f"/_/shifts/{demo['shift_id']}/edit", **htmx('hj-drawer-body'))
    assert 'Edit General 10-18' in r.content.decode() and 'value="GEN"' in r.content.decode()
    r = hr.post(f"/_/shifts/{demo['shift_id']}/edit", {**SHIFT, 'code': 'GEN', 'name_en': 'General', 'start': '09:30', 'end': '17:30'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Shift saved.'
    assert one('SELECT name_en, start_time::text AS s FROM shift WHERE id = %s', [demo['shift_id']]) == {'name_en': 'General', 's': '09:30:00'}


def test_assign_shift_from_the_modal(hr, demo):
    night = hr.post('/_/shifts/new', SHIFT, **htmx())
    sid = str(one("SELECT id FROM shift WHERE code = 'NIGHT'")['id'])
    r = hr.get(f'/_/shifts/{sid}/assign', **htmx('hj-modal-body'))
    assert 'Assign Night' in r.content.decode() and 'Starting from' in r.content.decode()
    r = hr.post(f'/_/shifts/{sid}/assign', {'employee_id': demo['ram'], 'from_date': '2026-10-01'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Shift assigned.'
    assert night.status_code == 204
    got = rows('SELECT valid::text AS v FROM shift_assignment WHERE employee_id = %s ORDER BY lower(valid)', [demo['ram']])
    assert [g['v'] for g in got] == ['[2026-01-01,2026-10-01)', '[2026-10-01,)']
    r = hr.post(f'/_/shifts/{sid}/assign', {'employee_id': '', 'from_date': '2026-10-01'}, **htmx())
    assert r.status_code == 422


def test_delete_shift_in_use_is_refused_with_the_reason(hr, demo):
    r = hr.post(f"/_/shifts/{demo['shift_id']}/delete", **htmx())
    assert r.status_code == 409
    assert toasts(r)[0]['message'] == 'shift is assigned to 2 employee(s) and 0 future roster day(s); reassign first'
    hr.post('/_/shifts/new', SHIFT, **htmx())
    sid = str(one("SELECT id FROM shift WHERE code = 'NIGHT'")['id'])
    r = hr.post(f'/_/shifts/{sid}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Shift deleted.' and 'hj:refresh-shifts' in hx_triggers(r)


def test_shift_pages_are_hr_only(demo, login_as, client_for):
    emp = client_for(login_as(['employee'], demo['sita']))
    assert emp.get('/settings/shifts', **htmx()).status_code == 403
    assert emp.post('/_/shifts/new', SHIFT, **htmx()).status_code == 403
    assert emp.post(f"/_/shifts/{demo['shift_id']}/delete", **htmx()).status_code == 403
    att = client_for(login_as(['attendance_admin']))
    assert att.get('/settings/shifts', **htmx()).status_code == 403   # the Settings hub is HR only
    assert att.post('/_/shifts/new', SHIFT, **htmx()).status_code == 204  # the API allowed the attendance team


# ---------------------------------------------------------------- Settings -> Holidays

def test_holiday_import_and_the_bs_year_view(hr):
    html = hr.get('/settings/holidays?year=2083').content.decode()
    assert 'No holidays for 2083 yet' in html and 'Import the government list for 2083' in html
    r = hr.post('/_/shifts/holidays/import', {'bs_year': '2083'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '42 of 42 public holidays added.'
    assert 'hj:refresh-holidays' in hx_triggers(r)
    assert toasts(hr.post('/_/shifts/holidays/import', {'bs_year': '2083'}, **htmx()))[0]['message'] == '0 of 42 public holidays added.'
    html = hr.get('/settings/holidays?year=2083').content.decode()
    observed = sum(1 for _ in rows('SELECT 1 FROM holiday WHERE NOT optional'))
    assert f'42 holidays, {observed} observed by everyone' in html
    assert 'Dashain (Phulpati)' in html and 'Ashwin 2083' in html and 'Kartik 2083' in html
    assert 'Women only' in html and 'Optional' in html and '17 Oct 2026' in html
    html = hr.get('/settings/holidays?year=2084').content.decode()
    assert 'No holidays for 2084 yet' in html and 'Add the days your company observes this year.' in html
    part = hr.get('/settings/holidays?year=2083', **htmx('holidays'))
    assert part.status_code == 200 and '<html' not in part.content.decode() and 'id="holidays"' in part.content.decode()


def test_import_for_another_year_is_refused(hr):
    r = hr.post('/_/shifts/holidays/import', {'bs_year': '2084'}, **htmx())
    assert r.status_code == 400
    assert toasts(r)[0]['message'] == 'default list available for BS 2083 only; add others via POST /shifts/holidays'


def test_add_edit_and_delete_a_holiday(hr):
    r = hr.get('/_/shifts/holidays/new', **htmx('hj-modal-body'))
    assert 'New holiday' in r.content.decode() and 'Women only (Teej, Jitiya)' in r.content.decode()
    r = hr.post('/_/shifts/holidays/new', {'local_date': '2026-12-31', 'name_en': 'Company anniversary', 'gender_scope': 'all'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Holiday saved. Affected days are being recalculated.'
    h = one("SELECT id, optional, bs_date FROM holiday WHERE name_en = 'Company anniversary'")
    assert h['optional'] is False and h['bs_date'] == '2083-09-16'
    r = hr.post('/_/shifts/holidays/new', {'local_date': '2026-12-31', 'name_en': 'Company anniversary', 'gender_scope': 'all'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'this holiday already exists for that date'
    r = hr.get(f"/_/shifts/holidays/{h['id']}/edit", **htmx('hj-modal-body'))
    assert 'Edit holiday' in r.content.decode() and 'Company anniversary' in r.content.decode()
    r = hr.post(f"/_/shifts/holidays/{h['id']}/edit", {'local_date': '2026-12-31', 'name_en': 'Founders day', 'gender_scope': 'female',
                                                        'optional': 'on'}, **htmx())
    assert r.status_code == 204
    assert one('SELECT name_en, gender_scope, optional FROM holiday WHERE id = %s', [h['id']]) == \
        {'name_en': 'Founders day', 'gender_scope': 'female', 'optional': True}
    assert 'Delete Founders day?' in hr.get('/settings/holidays?year=2083').content.decode()
    r = hr.post(f"/_/shifts/holidays/{h['id']}/delete", **htmx())
    assert r.status_code == 204 and 'hj:refresh-holidays' in hx_triggers(r)
    assert not one('SELECT 1 AS x FROM holiday WHERE id = %s', [h['id']])
    r = hr.post('/_/shifts/holidays/new', {'local_date': '', 'name_en': ''}, **htmx())
    assert r.status_code == 422


def test_a_manager_cannot_add_a_holiday(demo, login_as, client_for):
    ram = client_for(login_as(['manager'], demo['ram']))
    r = ram.post('/_/shifts/holidays/new', {'local_date': '2026-12-31', 'name_en': 'x', 'gender_scope': 'all'}, **htmx())
    assert r.status_code == 403
    r = ram.post('/_/shifts/holidays/import', {'bs_year': '2083'}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'requires role hr or attendance_admin'


# ---------------------------------------------------------------- Attendance -> Rosters

def test_roster_grid_shows_the_week_and_normal_shifts(hr, demo):
    r = hr.get('/attendance/rosters?week=2026-11-04')   # any day: the week starts on Sunday 1 Nov
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Sita Sharma' in html and 'Ram Thapa' in html and 'Normal (GEN)' in html and 'Weekly off' in html
    assert 'GEN 10:00–18:00' in html and '"week": "2026-11-01"' in html
    assert 'Copy the roster of' in html and 'Cells already set this week are overwritten.' in html
    assert 'No shift swaps' in html
    assert 'Sita Sharma on 2026-11-07' in html


def test_roster_cell_change_saves_and_redraws(hr, demo):
    r = hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-02', 'value': 'off', 'week': '2026-11-01'},
                **htmx('roster-grid'))
    assert r.status_code == 200 and 'id="roster-grid"' in r.content.decode() and '<html' not in r.content.decode()
    assert one("SELECT shift_id FROM roster WHERE employee_id = %s AND local_date = '2026-11-02'", [demo['sita']]) == {'shift_id': None}
    assert '<option value="off" selected>' in r.content.decode()
    r = hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-02', 'value': demo['shift_id'], 'week': '2026-11-01'}, **htmx())
    assert str(one("SELECT shift_id FROM roster WHERE employee_id = %s AND local_date = '2026-11-02'", [demo['sita']])['shift_id']) == demo['shift_id']
    r = hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-02', 'value': '', 'week': '2026-11-01'}, **htmx())
    assert r.status_code == 200
    assert not one("SELECT 1 AS x FROM roster WHERE employee_id = %s AND local_date = '2026-11-02'", [demo['sita']])
    grid = hr.get('/_/shifts/roster?week=2026-11-01', **htmx('roster-grid'))
    assert grid.status_code == 200 and 'Normal (GEN)' in grid.content.decode()


def test_locked_cell_is_a_toast(hr, demo):
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, inputs_hash, locked_at)
               VALUES (%s, '2026-11-03', 1, 'Present', 'h', now())""", [demo['sita']])
    r = hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-03', 'value': 'off', 'week': '2026-11-01'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'attendance for 2026-11-03 is locked by payroll'


def test_copy_week_and_publish(hr, demo):
    hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-10-27', 'value': 'off', 'week': '2026-10-25'}, **htmx())
    r = hr.post('/_/shifts/roster/copy-week', {'week': '2026-11-01'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '1 cell copied.' and 'hj:refresh-roster-grid' in hx_triggers(r)
    assert one("SELECT 1 AS x FROM roster WHERE employee_id = %s AND local_date = '2026-11-03'", [demo['sita']])
    r = hr.post('/_/shifts/roster/publish', {'week': '2026-11-01'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Roster published. 1 person was notified.'
    r = hr.post('/_/shifts/roster/publish', {'week': '2026-12-06'}, **htmx())
    assert toasts(r)[0]['message'] == 'Roster published. 0 people were notified.'


def test_managers_roster_their_team_only(demo, login_as, client_for):
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    ram = client_for(login_as(['manager'], demo['ram']))
    html = ram.get('/attendance/rosters?week=2026-11-01').content.decode()
    assert 'Sita Sharma' in html and 'Ram Thapa' in html
    m = one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E903', 'Unrelated Manager') RETURNING id", [demo['company_id']])
    execute("INSERT INTO employee_assignment (employee_id, valid, employment_type) VALUES (%s, daterange('2026-01-01', NULL), 'regular')", [m['id']])
    other = client_for(login_as(['manager'], str(m['id'])))
    html = other.get('/attendance/rosters?week=2026-11-01').content.decode()
    assert 'Sita Sharma' not in html and 'Unrelated Manager' in html
    r = other.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-02', 'value': 'off', 'week': '2026-11-01'}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'not allowed to access this employee'
    emp = client_for(login_as(['employee'], demo['sita']))
    assert emp.get('/attendance/rosters', **htmx()).status_code == 403


def test_swaps_are_listed_with_their_status(hr, demo):
    execute("""INSERT INTO shift_swap (company_id, requester_id, colleague_id, local_date, requester_shift_id, status, reason)
               VALUES (%s, %s, %s, '2026-11-05', %s, 'pending', 'family function')""",
            [demo['company_id'], demo['sita'], demo['ram'], demo['shift_id']])
    html = hr.get('/attendance/rosters?week=2026-11-01').content.decode()
    assert 'Sita Sharma ⇄ Ram Thapa' in html and 'GEN ⇄ Off' in html and 'Waiting for approval' in html
    assert '“family function”' in html and 'data-tip="Open"' in html


def test_navigation_tabs(hr):
    html = hr.get('/settings/shifts').content.decode()
    assert '/settings/holidays' in html


def test_a_refused_cell_goes_back_to_its_saved_value(hr, demo):
    """RostersPage: a failed save leaves the select on the saved value and the select is disabled while saving."""
    hr.post('/_/shifts/roster/cell', {'employee_id': demo['sita'], 'date': '2026-11-03', 'value': 'off', 'week': '2026-11-01'}, **htmx())
    html = hr.get('/_/shifts/roster?week=2026-11-01', **htmx('roster-grid')).content.decode()
    assert 'aria-label="Sita Sharma on 2026-11-03"' in html
    i = html.index('aria-label="Sita Sharma on 2026-11-03"')
    cell = html[html.rindex('<select', 0, i):html.index('>', i)]
    assert 'data-value="off"' in cell and 'hx-on::response-error="this.value = this.dataset.value"' in cell and 'hx-disabled-elt="this"' in cell
