"""Field pages through django.test.Client: field visits and breaks, kiosks, locations, my roster and swaps, my visits,
the kiosk badge and the Home card."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.db import execute, one
from tests.punch.api import OFFICE, SELFIE, add_days, post, punch, put, today
from tests.web.factories import htmx, hx_triggers, toasts


def browser(login) -> Client:
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    return c


@pytest.fixture
def people(demo, login_as):
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    return {'hr': login_as(['hr']), 'sita': login_as(['employee'], demo['sita']), 'ram': login_as(['employee', 'manager'], demo['ram'])}


def test_locations_page_crud_rules_and_staff(demo, people):
    hr = browser(people['hr'])
    r = hr.get('/settings/locations')
    html = r.content.decode()
    assert r.status_code == 200
    for s in ('Work locations', 'No locations yet', 'Clocking in away from a location', "Don't check", 'Ask for a reason', 'Refuse',
              'Breaks', 'Lunch', 'Prayer', 'The first visit of the day clocks the person in', 'Staff', 'Field staff', 'Everyone', 'field/geo.js'):
        assert s in html, s
    f = hr.get('/_/field/locations/new', **htmx('hj-modal-body')).content.decode()
    assert 'Add location' in f and 'Use my current location' in f
    r = hr.post('/_/field/locations/new', {'name': 'Head office', 'address': 'Kathmandu', 'lat': '27.7172', 'lng': '85.324', 'radius_m': '150',
                                          'active': 'on'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Location added.'
    loc = str(one("SELECT id FROM location WHERE name = 'Head office'")['id'])
    t = hr.get('/settings/locations', **htmx('locations-table')).content.decode()
    assert 'Head office' in t and '150 m' in t and 'Open map' in t
    r = hr.post(f'/_/field/locations/{loc}/edit', {'name': 'HQ', 'lat': '27.7172', 'lng': '85.324', 'radius_m': '200'}, **htmx())
    assert toasts(r)[0]['message'] == 'Location saved.'
    assert one('SELECT radius_m, active FROM location WHERE id = %s', [loc]) == {'radius_m': 200, 'active': False}
    r = hr.post('/_/field/rules', {'geofenceMode': 'block', 'lunch_enabled': 'on', 'lunch_maxMin': '45', 'tea_enabled': 'on', 'tea_paid': 'on',
                                   'tea_maxMin': '15', 'fieldVisitClockIn': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Rules saved.'
    pol = one("SELECT value FROM company_setting WHERE key = 'field.policy' AND company_id = %s", [demo['company_id']])['value']
    assert pol['geofenceMode'] == 'block' and pol['breaks']['lunch'] == {'enabled': True, 'paid': False, 'maxMin': 45}
    assert pol['breaks']['prayer']['enabled'] is False
    r = hr.post(f"/_/field/staff/{demo['sita']}", {'which': 'field', 'field_work': 'on'}, **htmx())
    assert r.status_code == 204 and one('SELECT field_work FROM employee WHERE id = %s', [demo['sita']])['field_work'] is True
    r = hr.post(f"/_/field/staff/{demo['sita']}", {'which': 'locations', 'location_ids': [loc]}, **htmx())
    assert r.status_code == 204
    s = hr.get('/settings/locations', **htmx('field-staff')).content.decode()
    assert 'Sita Sharma' in s and 'Reset kiosk PIN and QR badge' in s
    r = hr.post(f"/_/field/staff/{demo['sita']}/kiosk-reset", **htmx())
    assert toasts(r)[0]['message'] == 'Kiosk PIN reset for Sita Sharma. They can set a new one in the app.'
    r = hr.post(f'/_/field/locations/{loc}/delete', **htmx())
    assert toasts(r)[0]['message'] == 'Location deleted.'
    # Settings is HR only
    assert browser(people['ram']).get('/settings/locations').status_code in (302, 403)


def test_kiosks_page(demo, people):
    hr = browser(people['hr'])
    html = hr.get('/attendance/kiosks').content.decode()
    assert 'Kiosks' in html and 'No kiosks yet' in html and 'Add kiosk' in html and '/kiosk' in html
    f = hr.get('/_/field/kiosks/new', **htmx('hj-modal-body')).content.decode()
    assert 'Take a photo with every punch' in f and 'Add and get code' in f
    r = hr.post('/_/field/kiosks/new', {'name': 'Lobby tablet', 'selfie_required': 'on'}, **htmx('hj-modal-body'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Pair Lobby tablet' in body and 'The code works once and expires at' in body
    assert 'hj:refresh-kiosks' in hx_triggers(r)
    kid = str(one("SELECT id FROM kiosk WHERE name = 'Lobby tablet'")['id'])
    t = hr.get('/attendance/kiosks', **htmx('kiosks')).content.decode()
    assert 'Waiting to pair' in t and 'Required' in t
    r = hr.post(f'/_/field/kiosks/{kid}/edit', {'name': 'Front desk'}, **htmx())
    assert toasts(r)[0]['message'] == 'Kiosk saved.'
    assert one('SELECT name, selfie_required FROM kiosk WHERE id = %s', [kid]) == {'name': 'Front desk', 'selfie_required': False}
    r = hr.post(f'/_/field/kiosks/{kid}/pairing-code', **htmx('hj-modal-body'))
    assert 'Pair Front desk' in r.content.decode()
    r = hr.post(f'/_/field/kiosks/{kid}/revoke', **htmx())
    assert toasts(r)[0]['message'] == 'Kiosk revoked.'
    assert 'Revoked' in hr.get('/attendance/kiosks', **htmx('kiosks')).content.decode()
    assert browser(people['ram']).get('/attendance/kiosks').status_code in (302, 403)


def test_field_visits_page_and_csv(demo, people):
    hr = browser(people['hr'])
    execute('UPDATE employee SET field_work = true WHERE id = %s', [demo['sita']])
    assert post(people['sita'].token, '/field/visits/start', {'place': 'Client A', 'purpose': 'Demo', 'photo': SELFIE, 'lat': 27.8, 'lng': 85.4}).status == 201
    html = hr.get('/attendance/visits').content.decode()
    assert 'Field visits' in html and 'Client A' in html and 'People in the field' in html and 'Arrival' in html
    b = hr.get('/attendance/visits', {'view': 'breaks'}, **htmx('field-visits')).content.decode()
    assert 'Breaks taken during the day.' in b and 'No breaks' in b and 'Over limit only' in b
    csv = hr.get('/attendance/visits', {'export': 'csv'})
    assert 'text/csv' in csv['Content-Type'] and 'Client A' in csv.content.decode('utf-8-sig')
    vid = str(one("SELECT id FROM field_visit WHERE place = 'Client A'")['id'])
    assert hr.get(f'/_/field/visits/{vid}/photo').status_code == 200
    m = browser(people['ram']).get('/attendance/visits').content.decode()
    assert 'Client A' in m


def test_my_roster_and_swaps_pages(demo, people, login_as):
    hari = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'E003', 'Hari Karki') RETURNING id", [demo['company_id']])['id'])
    execute("INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES (%s, daterange('2026-01-01', NULL), %s, 'regular')",
            [hari, demo['branch_id']])
    hari_login = login_as(['employee'], hari)
    night = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'NIGHT', 'Night', '22:00', '06:00') RETURNING id",
                    [demo['company_id']])['id'])
    day = add_days(today(), 3)
    execute('INSERT INTO roster (employee_id, local_date, shift_id) VALUES (%s, %s, %s), (%s, %s, %s)', [demo['sita'], day, demo['shift_id'], hari, day, night])
    sita = browser(people['sita'])
    html = sita.get('/me/attendance/roster').content.decode()
    assert 'My roster' in html and '(today)' in html and 'No swap requests' in html and 'Swap a shift' in html and 'Rostered' in html
    f = sita.get('/_/field/swaps/new', {'date': day}, **htmx('hj-modal-body')).content.decode()
    assert 'Hari Karki (Night 22:00–06:00)' in f and 'You are on General 10-18, 10:00 to 18:00.' in f
    r = sita.post('/_/field/swaps/new', {'colleague_id': hari, 'date': day, 'reason': 'Family event'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Swap request sent to your colleague.'
    h = browser(hari_login)
    page = h.get('/me/attendance/roster').content.decode()
    assert 'Asked of you' in page and 'Sita Sharma' in page and 'You would work General 10-18; they would work Night.' in page
    sid = str(one('SELECT id FROM shift_swap WHERE requester_id = %s', [demo['sita']])['id'])
    r = h.post(f'/_/field/swaps/{sid}/accept', **htmx())
    assert toasts(r)[0]['message'] == 'Accepted. It now goes to the manager for approval.'
    page = sita.get('/me/attendance/roster', **htmx('my-roster')).content.decode()
    assert 'Waiting for manager' in page and 'Cancel' in page
    r = sita.post(f'/_/field/swaps/{sid}/cancel', **htmx())
    assert toasts(r)[0]['message'] == 'Request cancelled.'


def test_my_visits_and_kiosk_badge_pages(demo, people):
    sita = browser(people['sita'])
    html = sita.get('/me/attendance/visits').content.decode()
    assert 'Visits and breaks' in html and 'Field visits are not turned on for you' in html and 'No breaks in this period' in html
    k = sita.get('/me/attendance/kiosk').content.decode()
    assert 'Kiosk PIN' in k and 'You have not set a PIN yet.' in k and 'Show my badge' in k
    r = sita.post('/_/field/kiosk-pin', {'pin': '4826', 'again': '4826'}, **htmx())
    assert r.status_code == 204 and r['HX-Refresh'] == 'true'
    assert 'Your PIN is set.' in sita.get('/me/attendance/kiosk').content.decode()
    r = sita.post('/_/field/kiosk-pin', {'pin': '1234', 'again': '1234'}, **htmx())
    assert r.status_code == 400
    b = sita.get('/_/field/kiosk-badge', **htmx('kiosk-badge'))
    assert r.status_code == 400 and b['Cache-Control'] == 'no-store'
    body = b.content.decode()
    assert 'E001' in body and 'Each code works once.' in body and '<svg' in body


def test_home_card_breaks_and_visits(demo, people):
    sita = browser(people['sita'])
    assert sita.get('/_/field/home').content == b''
    execute("INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'Head office', 27.7172, 85.3240, 150)", [demo['company_id']])
    assert punch(people['sita'].token, OFFICE).status == 201
    html = sita.get('/_/field/home').content.decode()
    assert 'Breaks' in html and 'Lunch' in html and '60 min, unpaid' in html
    r = sita.post('/_/field/breaks/start', {'kind': 'tea'}, **htmx())
    assert toasts(r)[0]['message'] == 'Tea break started.' and 'hj:refresh-home-field' in hx_triggers(r)
    assert 'End break' in sita.get('/_/field/home').content.decode()
    r = sita.post('/_/field/breaks/end', **htmx())
    assert toasts(r)[0]['message'].startswith('Break ended after ')
    execute('UPDATE employee SET field_work = true WHERE id = %s', [demo['sita']])
    f = sita.get('/_/field/visits/start', **htmx('hj-modal-body')).content.decode()
    assert 'Start a field visit' in f and 'data-geo' in f and 'Add a photo' in f
    r = sita.post('/_/field/visits/start', {'place': 'Client A', 'lat': '', 'lng': ''}, **htmx())
    msgs = [t['message'] for t in toasts(r)]
    assert msgs[0].startswith('Visit started at ') and msgs[1] == 'Location was not available, so the visit was saved without it.'
    vid = str(one("SELECT id FROM field_visit WHERE place = 'Client A'")['id'])
    assert 'End visit' in sita.get('/_/field/home').content.decode()
    r = sita.post(f'/_/field/visits/{vid}/end', {'outcome': 'Order placed'}, **htmx())
    assert toasts(r)[0]['message'] == 'Visit to Client A ended.'
    put(people['hr'].token, '/field/policy', {'fieldPhotoRequired': True})
    r = sita.post('/_/field/visits/start', {'place': 'Client B'}, **htmx())
    assert 'hj:visit-photo-required' in hx_triggers(r) and toasts(r)[0]['message'] == 'A photo of the place is needed to start a visit.'
