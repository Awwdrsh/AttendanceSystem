"""Groups of companies and punches: a person punches as the employee of the company they are working in, and another
company's photo or employee is not found."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from django.test import Client

from hajiri.core.clock import now, npt_today
from hajiri.db.models import Device, PunchEvent, PunchPhoto
from hajiri.punch.services import ParsedPhoto, store_punch_photo
from tests.multicompany.conftest import world  # noqa: F401 - the shared group fixture
from tests.punch.api import JPEG, now_iso
from tests.web.factories import bearer, make_employee


def _post(token, url, body):
    return Client().post(url, json.dumps(body), content_type='application/json', **bearer(token))


@pytest.fixture
def beta_photo(world):
    emp = make_employee(world.beta, code='B200', name='Bishnu Beta')
    import uuid
    punch = SimpleNamespace(id=uuid.uuid4(), punched_at=now(), local_date=npt_today(), direction='in')
    pid = str(uuid.uuid4())
    store_punch_photo(photo_id=pid, company_id=str(world.beta.id), employee_id=str(emp.id), punch=punch,
                      photo=ParsedPhoto('image/jpeg', JPEG))
    return {'photo': pid, 'emp': str(emp.id)}


def test_another_companys_photo_and_employee_are_not_found(world, beta_photo):
    url = f"/api/v1/punch/photos/{beta_photo['photo']}"
    manual = {'employeeId': beta_photo['emp'], 'localDate': npt_today().isoformat(), 'time': '09:00', 'direction': 'in',
              'reason': 'Device was down'}
    rng = {'from': npt_today().isoformat(), 'to': npt_today().isoformat(), 'employeeId': beta_photo['emp']}
    for person in (world.admin_c, world.admin_a):
        token, _ = world.session(person)
        r = Client().get(url, **bearer(token))
        assert r.status_code == 404 and r.json()['error'] == 'photo not found'
        r = _post(token, '/api/v1/punch/manual', manual)
        assert r.status_code == 404 and r.json()['error'] == 'employee not found'
        assert Client().get('/api/v1/punch/remote', rng, **bearer(token)).status_code == 404
    owner, _ = world.session(world.admin_b)
    r = Client().get(url, **bearer(owner))
    assert r.status_code == 200 and r.content == JPEG
    assert _post(owner, '/api/v1/punch/manual', manual).status_code == 201
    assert Client().get('/api/v1/punch/remote', rng, **bearer(owner)).status_code == 200
    assert PunchPhoto.objects.count() == 1


def test_a_punch_lands_on_the_employee_record_of_the_active_company(world):
    """dual is HR at Alpha (home employee) and an employee of Beta (membership): the same sign-in clocks in at both."""
    home, home_user = world.session(world.dual)
    there, there_user = world.session(world.dual, world.beta)
    assert home_user.employee_id != there_user.employee_id == str(world.dual_beta_employee.id)
    body = {'direction': 'in', 'clientTime': now_iso(), 'source': 'web'}
    assert _post(home, '/api/v1/punch', {**body, 'idempotencyKey': 'home-key-1'}).status_code == 201
    r = _post(there, '/api/v1/punch', {**body, 'idempotencyKey': 'beta-key-1'})
    assert r.status_code == 201, r.content
    beta_punch = PunchEvent.objects.get(id=r.json()['punch']['id'])
    assert str(beta_punch.employee_id) == there_user.employee_id
    assert Device.objects.get(pk=beta_punch.device_id).serial_no == 'WEB-BETA'
    assert PunchEvent.objects.filter(employee_id=home_user.employee_id).count() == 1
    # each company's "today" shows only its own punches
    assert len(Client().get('/api/v1/punch/today', **bearer(there)).json()['punches']) == 1
    assert len(Client().get('/api/v1/punch/today', **bearer(home)).json()['punches']) == 1


def test_from_home_page_lists_only_the_active_company(world, beta_photo):
    PunchEvent.objects.create(device_sn='WEB-BETA', vendor='kiosk', device_user_pin='B200', employee_id=beta_photo['emp'],
                              punched_at=now(), device_local_time='x', local_date=npt_today(), direction='in',
                              verify_mode='web', source='web', raw={'remote': True}, idempotency_key='beta-remote-1')
    html = world.signed_in(world.admin_a).get('/attendance/from-home').content.decode()
    assert 'Bishnu Beta' not in html
    html = world.signed_in(world.admin_b).get('/attendance/from-home').content.decode()
    assert 'Bishnu Beta' in html
