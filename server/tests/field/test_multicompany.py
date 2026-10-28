"""Groups of companies and the field module: a location, kiosk, visit, swap or employee of another company is not found
(404) for the outsider and for the group admin working in their own company, and works for that company's admin.
Kiosks belong to the company whose HR paired them."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core.clock import now, npt_today
from hajiri.db.models import Employee, FieldVisit, Kiosk, Location, ShiftSwap
from tests.multicompany.conftest import world  # noqa: F401 - the shared group fixture
from tests.web.factories import bearer, htmx, make_employee


def _json(method, token, url, body=None):
    c = Client()
    kw = bearer(token)
    if method == 'get':
        return c.get(url, **kw)
    return getattr(c, method)(url, json.dumps(body or {}), content_type='application/json', **kw)


@pytest.fixture
def beta(world):
    """Records of Beta Foods, plus the three callers: outsider (Gamma), group admin in Alpha, Beta's admin."""
    b = world.beta
    emp = make_employee(b, code='B100', name='Bina Beta', field_work=True)
    peer = make_employee(b, code='B101', name='Binod Beta')
    loc = Location.objects.create(company=b, name='Beta depot', lat=27.7, lng=85.3, radius_m=150)
    kiosk = Kiosk.objects.create(company=b, name='Beta gate')
    visit = FieldVisit.objects.create(company=b, employee=emp, local_date=npt_today(), place='Beta client',
                                      started_at=now() - timedelta(minutes=30), ended_at=now())
    swap = ShiftSwap.objects.create(company=b, requester=emp, colleague=peer, local_date=npt_today() + timedelta(days=3))
    tokens = {'outsider': world.session(world.admin_c)[0], 'group_admin': world.session(world.admin_a)[0],
              'owner': world.session(world.admin_b)[0]}
    return {'emp': str(emp.id), 'loc': str(loc.id), 'kiosk': str(kiosk.id), 'visit': str(visit.id), 'swap': str(swap.id),
            'tokens': tokens}


FOREIGN = ('outsider', 'group_admin')


@pytest.mark.parametrize('method,url,body', [
    ('put', '/api/v1/field/locations/{loc}', {'name': 'X', 'lat': 27.7, 'lng': 85.3}),
    ('delete', '/api/v1/field/locations/{loc}', None),
    ('put', '/api/v1/field/kiosks/{kiosk}', {'name': 'X'}),
    ('post', '/api/v1/field/kiosks/{kiosk}/pairing-code', None),
    ('post', '/api/v1/field/kiosks/{kiosk}/revoke', None),
    ('post', '/api/v1/field/kiosks/{kiosk}/unlock', None),
    ('put', '/api/v1/field/staff/{emp}', {'fieldWork': False}),
    ('post', '/api/v1/field/staff/{emp}/kiosk-reset', None),
    ('get', '/api/v1/field/visits/{visit}/photo', None),
])
def test_another_companys_records_are_not_found(beta, method, url, body):
    url = url.format(**beta)
    for who in FOREIGN:
        r = _json(method, beta['tokens'][who], url, body)
        assert r.status_code == 404, (who, url, r.content)
    owner = _json(method, beta['tokens']['owner'], url, body)
    if url.endswith('/photo'):
        assert owner.status_code == 404 and owner.json()['error'] == 'this visit has no photo'
    else:
        assert owner.status_code in (200, 201), owner.content


def test_lists_show_only_the_active_company(world, beta):
    for url, key in (('/api/v1/field/locations', 'locations'), ('/api/v1/field/kiosks', 'kiosks'),
                     ('/api/v1/field/staff', 'staff')):
        ids = {r['id'] for r in _json('get', beta['tokens']['group_admin'], url).json()[key]}
        assert not ids & {beta['loc'], beta['kiosk'], beta['emp']}, url
        ids = {r['id'] for r in _json('get', beta['tokens']['owner'], url).json()[key]}
        assert ids & {beta['loc'], beta['kiosk'], beta['emp']}, url
    rng = {'from': npt_today().isoformat(), 'to': npt_today().isoformat()}
    r = Client().get('/api/v1/field/visits', rng, **bearer(beta['tokens']['group_admin']))
    assert r.json()['visits'] == []
    r = Client().get('/api/v1/field/visits', rng, **bearer(beta['tokens']['owner']))
    assert [v['id'] for v in r.json()['visits']] == [beta['visit']]


def test_a_switch_moves_the_group_admin_into_the_other_company(world, beta):
    token, _ = world.session(world.admin_a, world.beta)
    assert _json('put', token, f"/api/v1/field/locations/{beta['loc']}", {'name': 'Renamed', 'lat': 27.7, 'lng': 85.3}).status_code == 200
    assert Location.objects.get(pk=beta['loc']).name == 'Renamed'
    # a kiosk HR adds belongs to the company they are working in
    k = _json('post', token, '/api/v1/field/kiosks', {'name': 'Beta lobby'}).json()
    assert str(Kiosk.objects.get(pk=k['id']).company_id) == str(world.beta.id)


def test_swaps_and_colleagues_stay_inside_the_company(world, beta):
    """dual works in Alpha (home) and Beta (member): in Alpha a Beta swap or colleague is not found."""
    token, user = world.session(world.dual)
    for action in ('accept', 'decline', 'cancel'):
        assert _json('post', token, f"/api/v1/field/swaps/{beta['swap']}/{action}").status_code == 404
    r = _json('post', token, '/api/v1/field/swaps', {'colleagueId': beta['emp'], 'localDate': (npt_today() + timedelta(days=2)).isoformat()})
    assert r.status_code == 404 and r.json()['error'] == 'colleague not found'


def test_pages_answer_404_for_another_companys_ids(world, beta):
    c = world.signed_in(world.admin_a)
    for url in (f"/_/field/locations/{beta['loc']}/edit", f"/_/field/kiosks/{beta['kiosk']}/edit",
                f"/_/field/visits/{beta['visit']}/photo"):
        assert c.get(url, **htmx()).status_code == 404, url
    for url in (f"/_/field/locations/{beta['loc']}/delete", f"/_/field/kiosks/{beta['kiosk']}/revoke",
                f"/_/field/staff/{beta['emp']}/kiosk-reset"):
        assert c.post(url, **htmx()).status_code == 404, url
    assert Location.objects.filter(pk=beta['loc']).exists()
    assert Kiosk.objects.get(pk=beta['kiosk']).status != Kiosk.Status.REVOKED
    owner = world.signed_in(world.admin_b)
    assert owner.get(f"/_/field/kiosks/{beta['kiosk']}/edit", **htmx()).status_code == 200
    assert owner.get(f"/_/field/locations/{beta['loc']}/edit", **htmx()).status_code == 200


def test_my_day_follows_the_active_company(world):
    """dual's field work is on in Beta only: their Beta employee record answers there, the Alpha one at home."""
    Employee.objects.filter(pk=world.dual_beta_employee.id).update(field_work=True)
    home, _ = world.session(world.dual)
    there, _ = world.session(world.dual, world.beta)
    assert _json('get', home, '/api/v1/field/me').json()['fieldWork'] is False
    assert _json('get', there, '/api/v1/field/me').json()['fieldWork'] is True
