"""Groups of companies: corrections are company data. An id of another company is not found (even for a group admin
until they switch there), lists show only the active company, and people who reach a company through a membership or
a group role work there with their roles and employee record of that company."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core.clock import npt_today
from hajiri.db.models import RegularisationRequest, Shift
from hajiri.regularisation import services
from tests.core.fixtures import bearer
from tests.multicompany.conftest import world  # noqa: F401 - the shared fixture
from tests.web.factories import make_employee

URL = '/api/v1/regularisation'


def yesterday() -> str:
    return (npt_today() - timedelta(days=1)).isoformat()


@pytest.fixture
def beta_request(world):  # noqa: F811
    """A pending on-duty correction in Beta, filed by Beta's admin for their own employee record."""
    _, user = world.session(world.admin_b)
    out = services.create_request(user, {'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'client site'})
    return out['id']


def api(world, person, company=None):  # noqa: F811
    token, _ = world.session(person, company)
    return bearer(token)


def post(headers, url, body=None):
    r = Client().post(url, json.dumps(body or {}), content_type='application/json', **headers)
    return r, (r.json() if r.content else {})


def beta_employee(world):  # noqa: F811
    return str(world.admin_b.employee_id)


# ---------------------------------------------------------------- not found across companies

@pytest.mark.parametrize('who', ['admin_c', 'admin_a'])
def test_cancelling_another_companys_request_is_not_found(world, beta_request, who):  # noqa: F811
    r, body = post(api(world, getattr(world, who)), f'{URL}/{beta_request}/cancel')
    assert r.status_code == 404 and body['error'] == 'request not found'
    page = world.signed_in(getattr(world, who)).post(f'{URL}/{beta_request}/cancel', {}, content_type='application/json')
    assert page.status_code == 404
    assert RegularisationRequest.objects.get(pk=beta_request).status == 'pending'


def test_the_owner_company_may_cancel(world, beta_request):  # noqa: F811
    r, body = post(api(world, world.admin_b), f'{URL}/{beta_request}/cancel')
    assert r.status_code == 200 and body == {'ok': True}


def test_listing_another_companys_employee_is_not_found(world):  # noqa: F811
    r = Client().get(URL, {'employeeId': beta_employee(world)}, **api(world, world.admin_a))
    assert r.status_code == 404 and r.json()['error'] == 'employee not found'


@pytest.mark.parametrize('who', ['admin_c', 'admin_a'])
def test_filing_for_another_companys_employee_is_not_found(world, who):  # noqa: F811
    r, body = post(api(world, getattr(world, who)), URL,
                   {'employeeId': beta_employee(world), 'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'client'})
    assert r.status_code == 404 and body['error'] == 'employee not found'
    assert not RegularisationRequest.objects.filter(employee_id=beta_employee(world)).exists()


def test_a_shift_of_another_company_is_not_found(world):  # noqa: F811
    shift = Shift.objects.create(company=world.beta, code='BX', name_en='Beta X', start_time='09:00', end_time='17:00')
    _, user = world.session(world.admin_a)
    from hajiri.core.errors import NotFoundError
    with pytest.raises(NotFoundError, match='shift not found'):
        services.create_request(user, {'localDate': yesterday(), 'kind': 'shift_change', 'shiftId': str(shift.id),
                                       'reason': 'swap'})


# ---------------------------------------------------------------- working in a second company

def test_a_member_files_with_their_employee_record_of_the_active_company(world):  # noqa: F811
    r, body = post(api(world, world.dual, world.beta), URL, {'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'site'})
    assert r.status_code == 201
    assert str(RegularisationRequest.objects.get(pk=body['id']).employee_id) == str(world.dual_beta_employee.id)
    # At home (Alpha) the same person files for their Alpha record.
    r, body = post(api(world, world.dual), URL, {'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'site'})
    assert str(RegularisationRequest.objects.get(pk=body['id']).employee_id) == str(world.dual.employee_id)


def test_group_hr_fixes_punches_in_every_company_of_the_group(world):  # noqa: F811
    alpha_worker = make_employee(world.alpha, name='Alpha Worker')
    _, user = world.session(world.ghr, world.alpha)
    out = services.manual_punch(user, {'employeeId': str(alpha_worker.id), 'localDate': yesterday(), 'time': '10:00',
                                       'direction': 'in', 'reason': 'device offline'})
    assert out['duplicate'] is False
    # Outside the group there is nothing to reach.
    r, body = post(api(world, world.ghr, world.alpha), URL,
                   {'employeeId': str(world.admin_c.employee_id), 'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'x y'})
    assert r.status_code == 404


def test_a_group_admin_cancels_after_switching_to_that_company(world, beta_request):  # noqa: F811
    r = world.signed_in(world.admin_a, world.beta).post(f'{URL}/{beta_request}/cancel', {}, content_type='application/json')
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_lists_show_only_the_active_company(world, beta_request):  # noqa: F811
    r = Client().get(URL, **api(world, world.admin_a))
    assert r.status_code == 200 and beta_request not in {x['id'] for x in r.json()['requests']}
    r = Client().get(URL, **api(world, world.admin_a, world.beta))
    assert beta_request in {x['id'] for x in r.json()['requests']}
    # The Requests page's cookie session, with the scope it sends.
    body = world.signed_in(world.admin_a).get(URL, {'scope': 'all'}).content.decode()
    assert 'client site' not in body
    body = world.signed_in(world.admin_a, world.beta).get(URL, {'scope': 'all'}).content.decode()
    assert 'client site' in body


def test_hr_cannot_fix_a_punch_of_another_company(world):  # noqa: F811
    c = world.signed_in(world.admin_a)
    body = {'employeeId': beta_employee(world), 'localDate': yesterday(), 'kind': 'missed_punch',
            'punchTime': f'{yesterday()}T10:00:00+05:45', 'punchDirection': 'in', 'reason': 'device offline'}
    r = c.post(URL, body, content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'employee not found'}
    r = c.post('/api/v1/punch/manual', {'employeeId': beta_employee(world), 'localDate': yesterday(), 'time': '10:00',
                                        'direction': 'in', 'reason': 'device offline'}, content_type='application/json')
    assert r.status_code == 404


def test_a_correction_for_another_companys_employee_is_not_found(world):  # noqa: F811
    c = world.signed_in(world.admin_a)
    body = {'localDate': yesterday(), 'kind': 'on_duty', 'reason': 'client visit'}
    assert c.post(URL, {**body, 'employeeId': beta_employee(world)}, content_type='application/json').status_code == 404
    r = c.post(URL, {**body, 'employeeId': world.admin_a.employee_id}, content_type='application/json')
    assert r.status_code == 201, r.content


def test_the_shift_select_offers_only_the_active_companys_shifts(world):  # noqa: F811
    Shift.objects.create(company=world.beta, code='BX', name_en='Beta Late', start_time='14:00', end_time='22:00')
    Shift.objects.create(company=world.alpha, code='AX', name_en='Alpha Early', start_time='06:00', end_time='14:00')
    # The correction form's shift select is filled from the shifts list of the active company.
    names = {s['name_en'] for s in world.signed_in(world.admin_a).get('/api/v1/shifts').json()['shifts']}
    assert 'Alpha Early' in names and 'Beta Late' not in names
