"""The group administration JSON the React app's Group hub calls: the form option endpoints (attachable companies,
unlinked employees, transfer targets and their options, policy sections), the cookie session's Origin check, and
company isolation (a company outside the group is not found, people without the group role get 403)."""
from __future__ import annotations

import json

import pytest

from hajiri.company_settings.seed import seed_defaults
from hajiri.db.models import Branch
from tests.web.factories import make_company, make_employee, make_user

from .conftest import client_in
from .test_transfers import _body, mover  # noqa: F401  (the fixture)

pytestmark = pytest.mark.django_db
J = 'application/json'


def test_writes_from_another_site_are_refused(world):
    c = world.signed_in(world.admin_a)
    r = c.post('/api/v1/group/companies', json.dumps({'code': 'ZETA', 'nameEn': 'Zeta'}), content_type=J,
               HTTP_ORIGIN='https://evil.test')
    assert r.status_code == 403 and r.json()['error'] == 'cross-site request refused'
    ok = c.post('/api/v1/group/companies', json.dumps({'code': 'ZETA', 'nameEn': 'Zeta'}), content_type=J,
                HTTP_ORIGIN='http://testserver')
    assert ok.status_code == 201 and ok.json()['code'] == 'ZETA'
    # Reads pass whatever the origin.
    assert c.get('/api/v1/group/companies', HTTP_ORIGIN='https://evil.test').status_code == 200


def test_companies_list_get_patch_and_validation(world):
    c = world.signed_in(world.admin_a)
    items = c.get('/api/v1/group/companies').json()['items']
    assert [x['code'] for x in items] == ['ALPHA', 'BETA'] and {'headcount', 'pan', 'active'} <= set(items[0])
    one = c.get(f'/api/v1/group/companies/{world.beta.id}').json()
    assert one['nameEn'] == 'Beta Foods'
    r = c.patch(f'/api/v1/group/companies/{world.beta.id}', json.dumps({'pan': '123456789', 'address': 'Pokhara'}),
                content_type=J)
    assert r.status_code == 200 and r.json()['pan'] == '123456789' and r.json()['address'] == 'Pokhara'
    bad = c.patch(f'/api/v1/group/companies/{world.beta.id}', json.dumps({'pan': '12'}), content_type=J)
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert c.get(f'/api/v1/group/companies/{world.gamma.id}').status_code == 404
    bad_code = c.post('/api/v1/group/companies', json.dumps({'code': '!', 'nameEn': 'X'}), content_type=J)
    assert bad_code.status_code == 400
    taken = c.post('/api/v1/group/companies', json.dumps({'code': 'gamma', 'nameEn': 'Again'}), content_type=J)
    assert taken.status_code == 409


def test_attachable_and_attach(world):
    # Asha also administers a standalone company: only that one can be attached.
    delta = make_company('DELTA', 'Delta Standalone')
    from hajiri.db.models import CompanyMembership
    CompanyMembership.objects.create(user_id=world.admin_a.id, company=delta, roles=['admin'])
    c = world.signed_in(world.admin_a)
    items = c.get('/api/v1/group/companies/attachable').json()['items']
    assert [x['code'] for x in items] == ['DELTA']
    assert c.post('/api/v1/group/companies/attach', json.dumps({'companyId': str(world.gamma.id)}),
                  content_type=J).status_code == 404
    r = c.post('/api/v1/group/companies/attach', json.dumps({'companyId': str(delta.id)}), content_type=J)
    assert r.status_code == 200 and r.json()['groupId'] == str(world.alpha.group_id)
    again = c.post('/api/v1/group/companies/attach', json.dumps({'companyId': str(delta.id)}), content_type=J)
    assert again.status_code == 409
    assert world.signed_in(world.ghr).get('/api/v1/group/companies/attachable').status_code == 403


def test_unlinked_employees_for_the_access_form(world):
    free = make_employee(world.beta, name='Free Person')
    c = world.signed_in(world.admin_a)
    items = c.get('/api/v1/group/access/employees', {'companyId': str(world.beta.id)}).json()['items']
    names = {i['name'] for i in items}
    assert 'Free Person' in names and 'Bimal Admin' not in names and 'Dipak at Beta' not in names
    kept = c.get('/api/v1/group/access/employees', {'companyId': str(world.beta.id),
                                                     'keep': str(world.dual_beta_employee.id)}).json()['items']
    assert 'Dipak at Beta' in {i['name'] for i in kept}
    assert c.get('/api/v1/group/access/employees', {'companyId': str(world.gamma.id)}).status_code == 404
    assert c.get('/api/v1/group/access/employees', {'companyId': 'junk'}).status_code == 404
    assert c.get('/api/v1/group/access/employees').status_code == 400
    assert world.signed_in(world.ghr).get('/api/v1/group/access/employees',
                                          {'companyId': str(world.beta.id)}).status_code == 403
    assert free.company_id == world.beta.id


def test_access_grant_with_employee_invite_and_revoke(world):
    c = world.signed_in(world.admin_a)
    emp = make_employee(world.beta, name='Sita at Beta')
    r = c.post('/api/v1/group/access', json.dumps({'email': world.solo.email, 'companyId': str(world.beta.id),
                                                   'roles': ['hr'], 'employeeId': str(emp.id)}), content_type=J)
    assert r.status_code == 200 and r.json()['employeeId'] == str(emp.id)
    rows = [x for x in c.get('/api/v1/group/access').json()['items'] if x['email'] == world.solo.email]
    assert {(x['companyCode'], x['source']) for x in rows} == {('ALPHA', 'home'), ('BETA', 'membership')}
    gamma_emp = make_employee(world.gamma, name='Gamma Person')
    foreign = c.post('/api/v1/group/access', json.dumps({'email': world.solo.email, 'companyId': str(world.beta.id),
                                                         'roles': ['hr'], 'employeeId': str(gamma_emp.id)}),
                     content_type=J)
    assert foreign.status_code == 404
    outsider = c.post('/api/v1/group/access', json.dumps({'email': world.admin_c.email, 'companyId': str(world.beta.id),
                                                          'roles': ['hr']}), content_type=J)
    assert outsider.status_code == 404
    inv = c.post('/api/v1/group/access/invite', json.dumps({'email': 'new@beta.test', 'name': 'New Person',
                                                            'companyId': str(world.beta.id), 'roles': ['employee'],
                                                            'password': 'A-long-Temp-pass-2026!'}), content_type=J)
    assert inv.status_code == 201 and inv.json()['email'] == 'new@beta.test'
    assert c.delete(f'/api/v1/group/access/{world.solo.id}/{world.beta.id}').status_code == 204
    home = c.delete(f'/api/v1/group/access/{world.solo.id}/{world.alpha.id}')
    assert home.status_code == 400
    assert world.signed_in(world.admin_b).get('/api/v1/group/access').status_code == 403


def test_transfer_targets_and_options(world, mover):
    seed_defaults(str(world.beta.id))
    Branch.objects.create(company=world.beta, code='PKR', name_en='Pokhara')
    Branch.objects.create(company=world.gamma, code='GAM', name_en='Gamma branch')
    c = client_in(world, world.ghr, world.alpha)
    targets = c.get('/api/v1/group/transfers/targets').json()['items']
    assert [t['code'] for t in targets] == ['BETA'] and targets[0]['carryLeaveBalance'] is False
    opts = c.get(f'/api/v1/group/transfers/targets/{world.beta.id}').json()
    assert opts['company']['name'] == 'Beta Foods'
    assert 'Pokhara' in {b['name'] for b in opts['branches']} and 'Gamma branch' not in {b['name'] for b in opts['branches']}
    assert {'orgUnits', 'managers', 'shifts'} <= set(opts)
    assert c.get(f'/api/v1/group/transfers/targets/{world.gamma.id}').status_code == 404
    assert c.get(f'/api/v1/group/transfers/targets/{world.alpha.id}').status_code == 400
    assert world.signed_in(world.solo).get('/api/v1/group/transfers/targets').status_code == 403
    # Transfer rules: group HR reads them, only group admins change them.
    assert {r['name'] for r in c.get('/api/v1/group/transfer-rules').json()['items']} == {'Alpha Traders', 'Beta Foods'}
    put = c.put('/api/v1/group/transfer-rules', json.dumps({'companyId': str(world.beta.id), 'carryLeaveBalance': True}),
                content_type=J)
    assert put.status_code == 403
    admin = world.signed_in(world.admin_a)
    ok = admin.put('/api/v1/group/transfer-rules', json.dumps({'companyId': str(world.beta.id), 'carryLeaveBalance': True}),
                   content_type=J)
    assert ok.status_code == 200 and ok.json()['carryLeaveBalance'] is True
    assert admin.put('/api/v1/group/transfer-rules', json.dumps({'companyId': str(world.gamma.id),
                                                                 'carryLeaveBalance': True}),
                     content_type=J).status_code == 404
    # The transfer itself, with the form's answer.
    emp, _ = mover
    r = c.post('/api/v1/group/transfers', json.dumps(_body(world, emp, carryLeave=True)), content_type=J)
    assert r.status_code == 201 and r.json()['leaveCarried'] and r.json()['toCompanyId'] == str(world.beta.id)


def test_policy_sections_preview_and_apply(world):
    seed_defaults(str(world.alpha.id))
    c = world.signed_in(world.admin_a)
    sections = c.get('/api/v1/group/policies/sections').json()['items']
    keys = [s['key'] for s in sections]
    assert 'leave_types' in keys and all(s['label'] for s in sections)
    body = {'sourceCompanyId': str(world.alpha.id), 'targetCompanyIds': [str(world.beta.id)],
            'sections': ['leave_types'], 'onConflict': 'skip'}
    p = c.post('/api/v1/group/policies/preview', json.dumps(body), content_type=J)
    assert p.status_code == 200 and p.json()['targets'][0]['name'] == 'Beta Foods'
    assert set(p.json()['totals']) == {'create', 'overwrite', 'keep', 'same', 'skip'}
    a = c.post('/api/v1/group/policies/apply', json.dumps({**body, 'onConflict': 'overwrite'}), content_type=J)
    assert a.status_code == 200 and a.json()['onConflict'] == 'overwrite'
    gamma = c.post('/api/v1/group/policies/preview', json.dumps({**body, 'targetCompanyIds': [str(world.gamma.id)]}),
                   content_type=J)
    assert gamma.status_code == 404
    unknown = c.post('/api/v1/group/policies/preview', json.dumps({**body, 'sections': ['nope']}), content_type=J)
    assert unknown.status_code == 400
    assert world.signed_in(world.ghr).get('/api/v1/group/policies/sections').status_code == 403


def test_standalone_admin_starts_a_group(world):
    c = world.signed_in(world.admin_c)
    assert c.get('/api/v1/group/companies').status_code == 403
    r = c.post('/api/v1/group/companies', json.dumps({'code': 'GAMMA2', 'nameEn': 'Gamma Two'}), content_type=J)
    assert r.status_code == 201
    me = c.get('/api/v1/me/companies').json()
    assert me['groupRoles'] == ['group_admin'] and {x['code'] for x in me['companies']} == {'GAMMA', 'GAMMA2'}
    # Gamma's new group does not reach Alpha's.
    assert {x['code'] for x in c.get('/api/v1/group/companies').json()['items']} == {'GAMMA', 'GAMMA2'}
    plain = make_user(world.gamma, roles=['employee'], email='plain@gamma.test')
    assert plain.company_id == str(world.gamma.id)


def test_company_logo_as_a_data_url(world):
    import base64

    from .conftest import png_bytes
    c = world.signed_in(world.admin_a)
    url = 'data:image/png;base64,' + base64.b64encode(png_bytes((40, 20))).decode()
    r = c.post('/api/v1/group/companies', json.dumps({'code': 'LOGO', 'nameEn': 'Logo Ltd', 'logoDataUrl': url}),
               content_type=J)
    assert r.status_code == 201
    got = c.get(f"/api/v1/group/companies/{r.json()['id']}").json()
    assert got['logoDataUrl'] == url
    cleared = c.patch(f"/api/v1/group/companies/{r.json()['id']}", json.dumps({'logoDataUrl': None}), content_type=J)
    assert cleared.status_code == 200 and cleared.json()['logoDataUrl'] is None
    bad = c.post('/api/v1/group/companies', json.dumps({'code': 'LOGO2', 'nameEn': 'X', 'logoDataUrl': 'data:text/html,hi'}),
                 content_type=J)
    assert bad.status_code == 400
