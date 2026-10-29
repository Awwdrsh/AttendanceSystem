"""
Groups of companies: every people page, fragment and JSON endpoint that takes an id treats an id of another company
as not found (404) -- for an outsider and for a group admin working in a sister company -- while the owner company's
admin is served. Also: the active company decides whose profile "my profile" is, and notices about an employee reach
the account that works as that employee in the group.
"""
from datetime import timedelta

import pytest

from hajiri.core.clock import npt_today
from hajiri.db.models import CareerEvent, NotificationOutbox
from hajiri.people import service as ppl
from tests.people.group import build_group
from tests.web.factories import bearer, make_user


@pytest.fixture
def g(db):
    return build_group()


@pytest.fixture
def beta(g):
    """Records of Beta: a unit, a designation, a scheduled career event and a pending change request."""
    b = g.session(g.admin_b)
    unit = ppl.create_unit(b, {'kind': 'department', 'code': 'OPS', 'nameEn': 'Operations'})
    designation = ppl.create_designation(b, {'code': 'CK', 'nameEn': 'Cook'})
    eid = str(g.beta_emp.id)
    ev = ppl.record_career(b, eid, {'kind': 'grade_change', 'effectiveDate': (npt_today() + timedelta(days=30)).isoformat(),
                                    'details': {'grade': 'G5'}})
    bina = make_user(g.beta, roles=['employee'], employee=g.beta_emp, email='bina@beta.test')
    req = ppl.request_change(g.session(bina), {'section': 'contact', 'value': {'personalPhone': '9800000001'}})
    return {'employee': eid, 'unit': unit['id'], 'designation': designation['id'], 'branch': str(g.beta_branch.id),
            'event': ev['id'], 'request': req['id'], 'bina': bina}


def _requests(x):
    e, u, d, br, ev, rq = x['employee'], x['unit'], x['designation'], x['branch'], x['event'], x['request']
    P = '/api/v1/people'
    return [
        ('get', f'{P}/{e}/card', None), ('get', f'{P}/{e}/profile', None),
        ('put', f'{P}/{e}/profile/personal', {'nationality': 'Nepali'}),
        ('get', f'{P}/{e}/history', None), ('get', f'{P}/{e}/career', None),
        ('post', f'{P}/{e}/career/{ev}/cancel', {}),
        ('get', f'{P}/change-requests/{rq}', None),
        ('patch', f'{P}/structure/branches/{br}', {'nameEn': 'Beta HQ'}),
        ('patch', f'{P}/structure/designations/{d}', {'code': 'CK', 'nameEn': 'Head cook'}),
        ('patch', f'{P}/structure/units/{u}', {'nameEn': 'Kitchen'}),
        ('delete', f'{P}/structure/units/{u}', None),
    ]


def _call(c, method, url, data):
    if method == 'get':
        return c.get(url)
    return getattr(c, method)(url, data or {}, content_type='application/json')


def test_services_scope_to_the_active_company(g, beta):
    a = g.session(g.admin_a)
    names = {p['name_en'] for p in ppl.directory(a)}
    assert 'Anil Alpha' in names and 'Bina Beta' not in names
    assert {u['name_en'] for u in ppl.structure(a)['units']} == set()
    assert ppl.change_requests(a, 'all') == []
    # The same person after switching to Beta sees Beta's people and requests, with the group admin's admin role there.
    in_beta = g.session(g.admin_a, g.beta)
    assert 'Bina Beta' in {p['name_en'] for p in ppl.directory(in_beta)}
    assert [r['id'] for r in ppl.change_requests(in_beta, 'all')] == [beta['request']]
    with pytest.raises(Exception) as e:
        ppl.update_unit(a, beta['unit'], {'nameEn': 'Hacked'})
    assert getattr(e.value, 'status', None) == 404


def test_career_notice_reaches_the_member_account(g):
    """An event applied to a Beta record held by a member goes to that member's account (company_membership)."""
    b = g.session(g.admin_b)
    ppl.record_career(b, str(g.dual_beta.id), {'kind': 'grade_change', 'effectiveDate': npt_today().isoformat(), 'details': {'grade': 'M2'}})
    assert CareerEvent.objects.get(employee=g.dual_beta, kind='grade_change').status == 'applied'
    assert NotificationOutbox.objects.filter(user_id=g.dual.id, company_id=g.beta.id, template='people.career_event').exists()


def test_json_api_is_scoped(g, beta):
    from hajiri.core import auth
    from tests.web.factories import PW
    token = auth.login(g.admin_a.email, PW).token
    r = g.client(g.admin_a).get(f"/api/v1/people/{beta['employee']}/card", **bearer(token))
    assert r.status_code == 404
    names = [p['name_en'] for p in g.client(g.admin_a).get('/api/v1/people/directory', **bearer(token)).json()['people']]
    assert 'Anil Alpha' in names and 'Bina Beta' not in names
    token_b = auth.login(g.admin_b.email, PW).token
    r = g.client(g.admin_b).get(f"/api/v1/people/{beta['employee']}/card", **bearer(token_b))
    assert r.status_code == 200 and r.json()['person']['name_en'] == 'Bina Beta'
    assert g.client(g.admin_b).get('/api/v1/people/org-chart', **bearer(token_b)).status_code == 200


def test_ids_of_another_company_are_not_found(g, beta):
    outsider = g.client(g.admin_c)
    sister = g.client(g.admin_a)                 # group admin, working in Alpha
    for method, url, data in _requests(beta):
        r = _call(outsider, method, url, data)
        assert r.status_code == 404, ('outsider', url, r.status_code, r.content[:200])
        r = _call(sister, method, url, data)
        assert r.status_code == 404, ('sister company', url, r.status_code, r.content[:200])
    owner = g.client(g.admin_b)
    for method, url, data in _requests(beta):
        r = _call(owner, method, url, data)
        assert r.status_code != 404 and r.status_code < 500, ('owner', url, r.status_code, r.content[:200])


def test_my_profile_follows_the_active_company(g):
    home = g.client(g.dual)
    alpha_code = g.session(g.dual).employee_id and ppl.card(g.session(g.dual), g.session(g.dual).employee_id)['person']['emp_code']
    def my_profile(c):
        me = c.get('/api/v1/auth/me').json()
        r = c.get(f"/api/v1/people/{(me.get('user') or me)['employeeId']}/card")
        assert r.status_code == 200, r.content
        return r.content
    body = my_profile(home)
    assert alpha_code.encode() in body and g.dual_beta.emp_code.encode() not in body
    beta = g.client(g.dual, g.beta)
    body = my_profile(beta)
    assert g.dual_beta.emp_code.encode() in body and alpha_code.encode() not in body
    # A change request in Beta is for the Beta employee record.
    r = beta.post('/api/v1/people/me/change-requests', {'section': 'contact', 'value': {'personalPhone': '9811111111'}},
                  content_type='application/json')
    assert r.status_code == 201, r.content
    req = ppl.my_requests(g.session(g.dual, g.beta))
    assert len(req) == 1 and req[0]['employee_id'] == str(g.dual_beta.id)
    assert ppl.my_requests(g.session(g.dual)) == []
    # The Alpha record is another company's while working in Beta.
    assert beta.get(f'/api/v1/people/{g.alpha_emp.id}/profile').status_code == 404

