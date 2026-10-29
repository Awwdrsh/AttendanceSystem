"""Groups of companies: every shifts JSON endpoint works in the ACTIVE company only. A record of
another company is not found (404) for an outsider and for a group admin who is working in a sister company; the
owning company's admin reaches it. Group roles count as company roles where they are working."""
from __future__ import annotations

import json

import pytest
from django.test import Client

from hajiri.db.models import Holiday, Location, Roster, Shift, ShiftAssignment
from hajiri.shifts import services as svc
from tests.multicompany.conftest import world  # noqa: F401  (the group fixture)
from tests.web.factories import bearer, make_employee


@pytest.fixture
def beta(world):  # noqa: F811
    """Records of Beta Foods, made by Beta's admin: a shift with one person on it, a holiday, a location, a roster cell."""
    _, admin_b = world.session(world.admin_b)
    emp = make_employee(world.beta, name='Bina Beta')
    sid = svc.create_shift(admin_b, {'code': 'BDAY', 'nameEn': 'Beta day', 'start': '09:00', 'end': '17:00'})['id']
    spare = svc.create_shift(admin_b, {'code': 'BSPARE', 'nameEn': 'Beta spare', 'start': '07:00', 'end': '15:00'})['id']
    svc.assign_shift(admin_b, {'employeeId': str(emp.id), 'shiftId': sid, 'from': '2026-10-01'})
    hid = svc.create_holiday(admin_b, {'localDate': '2026-12-31', 'nameEn': 'Beta day out'})['id']
    lid = svc.create_location(admin_b, {'name': 'Beta yard', 'lat': 27.6, 'lng': 85.3})['id']
    svc.save_roster(admin_b, {'rows': [{'employeeId': str(emp.id), 'localDate': '2026-11-02', 'shiftId': sid}]})
    return {'shift': sid, 'spare': spare, 'holiday': hid, 'location': lid, 'employee': str(emp.id)}


def _api(world, person, company=None):  # noqa: F811
    token, _ = world.session(person, company)
    c, h = Client(), bearer(token)

    def call(method, path, body=None, **query):
        url = f'/api/v1/shifts{path}'
        if method == 'get':
            return c.get(url, query, **h)
        return getattr(c, method)(url, json.dumps(body if body is not None else {}), content_type='application/json', **h)
    return call


def _strangers(world):  # noqa: F811
    """The outsider (Gamma's admin) and the group admin working in Alpha."""
    return [world.signed_in(world.admin_c), world.signed_in(world.admin_a, world.alpha)]


def test_json_endpoints_of_another_company_are_not_found(world, beta):  # noqa: F811
    for call in (_api(world, world.admin_c), _api(world, world.admin_a, world.alpha)):
        assert call('get', f"/{beta['shift']}").status_code == 404
        assert call('patch', f"/{beta['shift']}", {'graceInMin': 1}).status_code == 404
        assert call('delete', f"/{beta['spare']}").status_code == 404
        assert call('patch', f"/holidays/{beta['holiday']}", {'nameEn': 'x'}).status_code == 404
        assert call('delete', f"/holidays/{beta['holiday']}").status_code == 404
        assert call('patch', f"/locations/{beta['location']}", {'radiusM': 300}).status_code == 404
        assert call('delete', f"/locations/{beta['location']}").status_code == 404
        assert call('get', '/calendar', **{'from': '2026-11-01', 'to': '2026-11-07', 'employeeId': beta['employee']}).status_code == 404
        assert call('put', '/roster', {'rows': [{'employeeId': beta['employee'], 'localDate': '2026-11-04', 'shiftId': None}]}).status_code == 404
        assert call('delete', '/roster', {'employeeId': beta['employee'], 'localDate': '2026-11-02'}).status_code == 404
        assert call('post', '/assign', {'employeeId': beta['employee'], 'shiftId': beta['shift'], 'from': '2026-11-01'}).status_code == 404
    assert Shift.objects.get(pk=beta['shift']).grace_in_min == 0
    assert Location.objects.filter(pk=beta['location']).exists() and Holiday.objects.filter(pk=beta['holiday']).exists()
    owner = _api(world, world.admin_b)
    assert owner('get', f"/{beta['shift']}").json()['shift']['assigned_employees'] == '1'
    assert owner('patch', f"/locations/{beta['location']}", {'radiusM': 300}).json() == {'ok': True}


def test_group_hr_manages_shifts_in_the_company_they_work_in(world, beta):  # noqa: F811
    in_alpha = _api(world, world.ghr, world.alpha)
    r = in_alpha('post', '', {'code': 'GHR', 'nameEn': 'Set by group HR', 'start': '08:00', 'end': '16:00'})
    assert r.status_code == 201
    assert Shift.objects.get(pk=r.json()['id']).company_id == world.alpha.id
    assert in_alpha('get', f"/{beta['shift']}").status_code == 404
    assert _api(world, world.ghr, world.beta)('patch', f"/{beta['shift']}", {'graceInMin': 3}).json()['ok'] is True
    # Group executives read the shifts but cannot change them.
    gexec = _api(world, world.gexec, world.beta)
    assert len(gexec('get', '').json()['shifts']) == 2
    assert gexec('post', '', {'code': 'NO', 'nameEn': 'No', 'start': '08:00', 'end': '16:00'}).status_code == 403


def test_the_cookie_session_of_the_pages_is_scoped_the_same_way(world, beta):  # noqa: F811
    """The React pages call the same endpoints with the session cookie."""
    def j(c, method, url, body=None):
        if method == 'get':
            return c.get(url)
        return getattr(c, method)(url, body if body is not None else {}, content_type='application/json')
    S = '/api/v1/shifts'
    for c in _strangers(world):
        assert j(c, 'get', f"{S}/{beta['shift']}").status_code == 404
        assert j(c, 'patch', f"{S}/{beta['shift']}", {'nameEn': 'X'}).status_code == 404
        assert j(c, 'delete', f"{S}/{beta['spare']}").status_code == 404
        assert j(c, 'patch', f"{S}/holidays/{beta['holiday']}", {'nameEn': 'Moved'}).status_code == 404
        assert j(c, 'delete', f"{S}/holidays/{beta['holiday']}").status_code == 404
        r = j(c, 'put', f'{S}/roster', {'rows': [{'employeeId': beta['employee'], 'localDate': '2026-11-03', 'shiftId': None}]})
        assert r.status_code == 404
    assert Shift.objects.filter(pk__in=[beta['shift'], beta['spare']]).count() == 2
    assert Holiday.objects.get(pk=beta['holiday']).name_en == 'Beta day out'
    assert list(Roster.objects.filter(employee_id=beta['employee']).values_list('shift_id', flat=True)) == [Shift.objects.get(pk=beta['shift']).pk]
    owner = world.signed_in(world.admin_b)
    assert j(owner, 'get', f"{S}/{beta['shift']}").status_code == 200
    assert j(owner, 'delete', f"{S}/{beta['spare']}").status_code == 200


def test_assigning_or_rostering_across_companies_is_not_found(world, beta):  # noqa: F811
    _, admin_a = world.session(world.admin_a, world.alpha)
    alpha_shift = svc.create_shift(admin_a, {'code': 'ADAY', 'nameEn': 'Alpha day', 'start': '10:00', 'end': '18:00'})['id']
    c = world.signed_in(world.admin_a, world.alpha)
    r = c.post('/api/v1/shifts/assign', {'employeeId': beta['employee'], 'shiftId': alpha_shift, 'from': '2026-11-01'},
               content_type='application/json')
    assert r.status_code == 404
    r = c.put('/api/v1/shifts/roster', {'rows': [{'employeeId': world.solo.employee_id, 'localDate': '2026-11-02',
                                                  'shiftId': beta['shift']}]}, content_type='application/json')
    assert r.status_code in (400, 404), r.content
    assert not ShiftAssignment.objects.filter(shift_id=alpha_shift).exists()
    assert not Roster.objects.filter(shift_id=beta['shift']).exclude(employee_id=beta['employee']).exists()


def test_lists_show_only_the_active_company(world, beta):  # noqa: F811
    alpha = _api(world, world.admin_a, world.alpha)
    assert alpha('get', '').json() == {'shifts': []}
    assert alpha('get', '/holidays').json() == {'holidays': []}
    assert alpha('get', '/locations').json() == {'locations': []}
    grid = alpha('get', '/roster', **{'from': '2026-11-01', 'to': '2026-11-07'}).json()
    assert grid['roster'] == [] and beta['employee'] not in {e['id'] for e in grid['employees']}
    assert alpha('post', '/roster/publish', {'from': '2026-11-01', 'to': '2026-11-07'}).json() == {'notified': 0}
    assert alpha('post', '/roster/copy-week', {'weekStart': '2026-11-08'}).json() == {'copied': 0, 'skippedLocked': 0}
    page = world.signed_in(world.admin_a, world.alpha)        # the cookie session of the pages
    assert 'Beta day' not in page.get('/api/v1/shifts').content.decode()
    assert 'Beta day out' not in page.get('/api/v1/shifts/holidays').content.decode()
    week = {'from': '2026-11-01', 'to': '2026-11-07'}
    assert 'Bina Beta' not in page.get('/api/v1/shifts/roster', week).content.decode()
    # The same group admin, switched to Beta, works there.
    in_beta = _api(world, world.admin_a, world.beta)
    assert [s['code'] for s in in_beta('get', '').json()['shifts']] == ['BDAY', 'BSPARE']
    assert 'Bina Beta' in world.signed_in(world.admin_a, world.beta).get('/api/v1/shifts/roster', week).content.decode()

