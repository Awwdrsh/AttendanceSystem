"""The Work from home page (/attendance/from-home, RemoteCheckinsPage.tsx) through django.test.Client."""
from __future__ import annotations

from django.test import Client

from hajiri.core import auth
from tests.support.sql import execute
from tests.punch.api import HOME, SELFIE, punch
from tests.web.factories import htmx


def browser(login) -> Client:
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    return c


def test_from_home_page(demo, login_as):
    hr = login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['manager'], demo['ram'])
    page = browser(hr).get('/attendance/from-home').content.decode()
    assert 'Work from home' in page and 'Nobody clocked in from home' in page and 'People from home' in page
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['sita']])
    execute("INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'Head office', 27.7172, 85.3240, 150)", [demo['company_id']])
    r = punch(sita.token, {**HOME, 'selfie': SELFIE, 'reason': 'Plumber'})
    assert r.status == 201
    html = browser(hr).get('/attendance/from-home').content.decode()
    assert 'Sita Sharma' in html and 'Clocked in' in html and 'Location shared, within 30 m' in html and '/api/v1/punch/photos/' in html
    assert '“Plumber”' in html
    week = browser(hr).get('/attendance/from-home', {'span': 'week', 'dir': 'all'}, **htmx('from-home')).content.decode()
    assert 'id="from-home"' in week and '<html' not in week
    # a manager who does not manage Sita sees nobody; an employee is refused
    assert 'Sita Sharma' not in browser(ram).get('/attendance/from-home').content.decode()
    assert browser(sita).get('/attendance/from-home').status_code in (302, 403)
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    assert 'Sita Sharma' in browser(ram).get('/attendance/from-home').content.decode()


def test_arrivals_ribbon(db):
    from django.template.loader import render_to_string

    from hajiri.punch.ribbon import arrivals_ribbon
    r = arrivals_ribbon([{'id': 'a', 'name': 'Sita', 'at': '2026-10-25T04:10:00Z', 'late': False},   # 09:55 NPT
                         {'id': 'b', 'name': 'Ram', 'at': '2026-10-25T04:46:00Z', 'late': True},     # 10:31 NPT
                         {'id': 'c', 'name': 'Hari', 'at': None, 'late': False}])
    assert len(r['marks']) == 2 and r['hours'][0]['label'] == '08:30' and r['hours'][-1]['label'] == '11:30'
    assert r['marks'][1]['tip'] == 'Ram, 10:31 (late)'
    html = render_to_string('punch/partials/arrivals_ribbon.html', {'r': r})
    assert 'Shift start 10:00 and grace' in html and 'Sita, 09:55' in html
