"""The punch endpoints as the React app calls them (frontend/src/api/index.js `punch`): the session cookie from fetch()
with no CSRF token. Same-origin and Origin-less writes pass, a foreign Origin is refused with the TS message; another
company's photo or employee is not found."""
from __future__ import annotations

from hajiri.core import auth
from hajiri.core.auth import create_user
from tests.core.fixtures import PW
from tests.punch.api import CROSS_SITE, HOME, SELFIE, Browser, now_iso, today
from tests.support.sql import execute

HQ = "INSERT INTO location (company_id, name, lat, lng, radius_m) VALUES (%s, 'Head office', 27.7172, 85.3240, 150)"


def _body(key, **extra):
    return {'direction': 'in', 'clientTime': now_iso(), 'idempotencyKey': key, **extra}


def test_clock_in_today_remote_photo_and_manual_with_the_cookie(demo, login_as):
    from hajiri.db.models import Company
    execute(HQ, [demo['company_id']])
    execute('UPDATE employee SET work_from_home = true WHERE id = %s', [demo['sita']])
    sita = Browser(login_as(['employee'], demo['sita']).token)
    hr = Browser(login_as(['hr']).token)

    # a foreign Origin is refused before anything is stored
    evil = Browser(sita.c.cookies['hajiri_session'].value, origin='https://evil.example')
    r = evil.post('/punch', _body('evil-00001', **HOME, selfie=SELFIE))
    assert r.status == 403 and r.body == CROSS_SITE

    # clock-in from home with the photo, as the React clock-in card sends it
    r = sita.post('/punch', _body('home-00001', **HOME, selfie=SELFIE))
    assert r.status == 201, r.body
    assert r.body['remote'] is True and r.body['photoId'] and r.body['message'].endswith('from home')
    assert set(r.body) >= {'duplicate', 'punch', 'flags', 'location', 'day', 'remote', 'photoId', 'message'}
    photo_id = r.body['photoId']
    # an Origin-less replay is the same punch
    again = Browser(sita.c.cookies['hajiri_session'].value, origin=None).post('/punch', _body('home-00001', **HOME, selfie=SELFIE))
    assert again.status == 200 and again.body['duplicate'] is True

    t = sita.get('/punch/today')
    assert t.status == 200 and t.body['date'] == today()
    assert t.body['punches'][0]['remote'] is True and t.body['punches'][0]['photo_id'] == photo_id

    rem = hr.get('/punch/remote', {'from': today(), 'to': today()})
    assert rem.status == 200 and [p['photo_id'] for p in rem.body['punches']] == [photo_id]
    assert 'lat' not in rem.body['punches'][0] and rem.body['punches'][0]['location_shared'] is True
    assert hr.get('/punch/remote', {'from': today(), 'to': '2020-01-01'}).status == 400

    # the <img src="/api/v1/punch/photos/:id"> request carries the cookie
    ph = hr.get(f'/punch/photos/{photo_id}')
    assert ph.status == 200 and ph.headers['Content-Type'] == 'image/jpeg'
    assert ph.headers['Cache-Control'] == 'private, max-age=300'

    # HR manual entry
    body = {'employeeId': demo['ram'], 'localDate': '2026-09-21', 'time': '10:00', 'direction': 'in', 'reason': 'Device was down'}
    assert sita.post('/punch/manual', body).status == 403
    r = hr.post('/punch/manual', body)
    assert r.status == 201 and r.body['duplicate'] is False and r.body['punchId']
    assert hr.post('/punch/manual', body).body == {'duplicate': True}
    bad = hr.post('/punch/manual', {**body, 'reason': 'x'})
    assert bad.status == 400 and bad.body['error'] == 'validation' and bad.body['issues'][0]['path'] == 'reason'

    # another company: the photo and the employee are not found
    c = Company.objects.create(code='RCT2', name_en='Other Co')
    create_user(company_id=str(c.id), employee_id=None, email='hr@rct2.local', name='Other HR', password=PW, roles=['hr'])
    other = Browser(auth.login('hr@rct2.local', PW).token)
    assert other.get(f'/punch/photos/{photo_id}').status == 404
    assert other.post('/punch/manual', {**body, 'localDate': '2026-09-22'}).status == 404
    assert other.get('/punch/remote', {'from': today(), 'to': today()}).body['punches'] == []


def test_signed_out_is_401(db):
    from django.test import Client
    r = Client().get('/api/v1/punch/today')
    assert r.status_code == 401 and 'error' in r.json()
