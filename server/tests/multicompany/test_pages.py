"""The group JSON endpoints behind the company switcher and the Group hub."""
import pytest
from django.urls import reverse

from hajiri.db.models import CompanyMembership, GroupMember


def test_group_roles_api(world):
    c = world.signed_in(world.admin_a)
    me = c.get('/api/v1/me/companies').json()
    assert me['groupRoles'] == ['group_admin'] and me['groupId'] == str(world.alpha.group_id)
    listed = c.get('/api/v1/group/roles').json()['items']
    assert {r['email'] for r in listed} == {'admin@alpha.test', 'ghr@beta.test', 'gexec@alpha.test'}
    r = c.post('/api/v1/group/roles', {'email': world.dual.email, 'roles': ['group_executive']}, content_type='application/json')
    assert r.status_code == 201 and r.json() == {'userId': world.dual.id, 'roles': ['group_executive']}
    bad = c.post('/api/v1/group/roles', {'email': world.dual.email, 'roles': ['admin']}, content_type='application/json')
    assert bad.status_code == 400
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 204
    assert c.delete(f'/api/v1/group/roles/{world.dual.id}').status_code == 404
    outsider = world.signed_in(world.admin_c)
    assert outsider.get('/api/v1/group/roles').status_code == 403
    assert outsider.delete(f'/api/v1/group/roles/{world.ghr.id}').status_code == 403


def test_group_overview_api(world):
    o = world.signed_in(world.ghr).get('/api/v1/group/overview').json()
    assert [c['code'] for c in o['companies']] == ['ALPHA', 'BETA'] and o['totals']['headcount'] >= 4
    alone = world.signed_in(world.admin_c).get('/api/v1/group/overview').json()   # a standalone company: itself only
    assert [c['code'] for c in alone['companies']] == ['GAMMA']
    assert world.signed_in(world.dual, world.beta).get('/api/v1/group/overview').json()['companies'][0]['code'] == 'ALPHA'
