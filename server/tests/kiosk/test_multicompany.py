"""Kiosk pairing is per company: a tablet paired by Beta's HR only knows Beta's people, so an Alpha code and PIN or an
Alpha QR badge does not work there, even inside one group of companies."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from tests.multicompany.conftest import world  # noqa: F401 - the shared group fixture
from tests.web.factories import bearer, make_employee, make_user


def _post(url, body, **headers):
    return Client().post(url, json.dumps(body), content_type='application/json', **headers)


def _pair(world, company_admin, company=None) -> str:
    token, _ = world.session(company_admin, company)
    code = _post('/api/v1/field/kiosks', {'name': 'Gate', 'selfieRequired': False}, **bearer(token)).json()['pairingCode']
    return _post('/api/v1/kiosk/pair', {'code': code}).json()['key']


@pytest.fixture
def people(world):
    alpha_emp = make_employee(world.alpha, code='K1', name='Anil Alpha')
    beta_emp = make_employee(world.beta, code='K2', name='Bina Beta')
    alpha_user = make_user(world.alpha, employee=alpha_emp, email='anil@alpha.test')
    beta_user = make_user(world.beta, employee=beta_emp, email='bina@beta.test')
    for person in (alpha_user, beta_user):
        token, _ = world.session(person)
        r = Client().put('/api/v1/field/kiosk-pin', json.dumps({'pin': '4831'}), content_type='application/json', **bearer(token))
        assert r.status_code == 200, r.content
    return {'alpha': alpha_user, 'beta': beta_user}


def test_a_kiosk_only_knows_its_own_companys_people(world, people):
    key = _pair(world, world.admin_b)
    h = {'HTTP_X_KIOSK_KEY': key}
    assert Client().get('/api/v1/kiosk/session', **h).json()['company']['name'] == 'Beta Foods'
    ok = _post('/api/v1/kiosk/identify', {'empCode': 'K2', 'pin': '4831'}, **h)
    assert ok.status_code == 200 and ok.json()['employee']['name'] == 'Bina Beta'
    wrong = _post('/api/v1/kiosk/identify', {'empCode': 'K1', 'pin': '4831'}, **h)
    assert wrong.status_code == 400
    assert wrong.json()['error'] == 'Employee code or PIN is wrong. Set your kiosk PIN in the app under My attendance.'
    # the Alpha person's rotating badge is refused at the Beta kiosk
    token, _ = world.session(people['alpha'])
    qr = Client().get('/api/v1/field/kiosk-qr', **bearer(token)).json()['token']
    r = _post('/api/v1/kiosk/identify', {'qr': qr}, **h)
    assert r.status_code == 400 and r.json()['error'] == 'This QR code is not valid here.'


def test_hr_working_in_a_sister_company_pairs_a_tablet_for_that_company(world, people):
    """The group admin switched to Beta adds the kiosk: it belongs to Beta, and Beta's people punch at it."""
    key = _pair(world, world.admin_a, world.beta)
    h = {'HTTP_X_KIOSK_KEY': key}
    assert Client().get('/api/v1/kiosk/session', **h).json()['company']['name'] == 'Beta Foods'
    t = _post('/api/v1/kiosk/identify', {'empCode': 'K2', 'pin': '4831'}, **h).json()['ticket']
    p = _post('/api/v1/kiosk/punch', {'ticket': t}, **h)
    assert p.status_code == 201 and p.json()['name'] == 'Bina Beta'
    # Alpha's kiosk list does not show it; Beta's does
    alpha_token, _ = world.session(world.admin_a)
    beta_token, _ = world.session(world.admin_b)
    assert Client().get('/api/v1/field/kiosks', **bearer(alpha_token)).json()['kiosks'] == []
    assert [k['punches_today'] for k in Client().get('/api/v1/field/kiosks', **bearer(beta_token)).json()['kiosks']] == [1]
