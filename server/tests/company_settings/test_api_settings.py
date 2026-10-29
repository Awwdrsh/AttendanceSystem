"""HTTP port of apps/api/test/settings.test.ts over /api/v1/settings: settings, the header profile, branches and org
units, the policy preview, with company isolation checks."""
import json

import pytest
from django.test import Client

from tests.core.fixtures import bearer
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import one

S = '/api/v1/settings'


def send(method, path, token, body):
    return getattr(Client(), method)(path, json.dumps(body), content_type='application/json', **bearer(token))


def get(path, token):
    return Client().get(path, **bearer(token))


@pytest.fixture
def tok(demo, login_as):
    return {'hr': login_as(['hr']).token, 'emp': login_as(['employee'], demo['sita']).token}


def test_lists_all_keys_with_descriptions_and_locked_caps(tok):
    r = get(S, tok['hr'])
    assert r.status_code == 200
    keys = [s['key'] for s in r.json()['settings']]
    assert 'attendance.policy' in keys
    assert set(r.json()['settings'][0]) == {'key', 'description', 'value', 'updatedAt', 'updatedBy'}
    assert r.json()['locked']['otCapDayMin'] == 240 and r.json()['locked']['otCapWeekMin'] == 1440
    refused = get(S, tok['emp'])
    assert refused.status_code == 403 and refused.json() == {'error': 'requires role hr or admin'}
    assert Client().get(S).status_code == 401


def test_clamps_ot_caps_bumps_the_policy_version_and_audits(tok):
    r = send('put', f'{S}/attendance.policy', tok['hr'], {'otMinThresholdMin': 20, 'otCapDayMin': 600, 'otCapWeekMin': 9999})
    assert r.status_code == 200
    v = r.json()['value']
    assert r.json()['key'] == 'attendance.policy'
    assert (v['otMinThresholdMin'], v['otCapDayMin'], v['otCapWeekMin']) == (20, 240, 1440)
    assert v['version'].startswith('v-') and len(v['version']) == 14
    r2 = send('put', f'{S}/attendance.policy', tok['hr'], {'otMinThresholdMin': 25})
    assert r2.json()['value']['version'] != v['version']
    a = one("""SELECT before, after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'attendance.policy'
               ORDER BY id DESC LIMIT 1""")
    assert a['before']['otMinThresholdMin'] == 20 and a['after']['otMinThresholdMin'] == 25
    assert send('put', f'{S}/attendance.policy', tok['emp'], {}).status_code == 403


def test_rejects_unknown_keys_and_invalid_values(tok):
    nope = send('put', f'{S}/nope', tok['hr'], {})
    assert nope.status_code == 404 and nope.json() == {'error': 'unknown setting nope'}
    bad = send('put', f'{S}/notifications.policy', tok['hr'], {'quietHoursStart': '25:00'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'


def test_company_profile_and_header(tok):
    send('put', f'{S}/company.profile', tok['hr'], {'nameEn': 'Demo Pvt Ltd', 'nameNe': 'डेमो', 'pan': '123456789'})
    r = get(f'{S}/company', tok['emp'])
    assert r.status_code == 200
    assert r.json()['company']['nameEn'] == 'Demo Pvt Ltd' and r.json()['company']['code'] == 'DEMO'
    bs = r.json()['today']['bs']
    assert len(bs) == 10 and bs[4] == '-' and bs[7] == '-'


def test_branches_and_org_units(tok, demo):
    b = send('post', f'{S}/branches', tok['hr'], {'code': 'PKR', 'nameEn': 'Pokhara'})
    assert b.status_code == 201
    assert set(b.json()['branch']) == {'id', 'code', 'name_en', 'name_ne'}
    assert send('post', f'{S}/branches', tok['hr'], {'code': 'PKR', 'nameEn': 'Dup'}).status_code == 409
    bid = b.json()['branch']['id']
    p = send('patch', f'{S}/branches/{bid}', tok['hr'], {'nameEn': 'Pokhara Lakeside'})
    assert p.status_code == 200 and p.json()['branch']['name_en'] == 'Pokhara Lakeside'
    u = send('post', f'{S}/org-units', tok['hr'], {'kind': 'department', 'code': 'FIN', 'nameEn': 'Finance', 'branchId': bid})
    assert u.status_code == 201
    uid = u.json()['orgUnit']['id']
    p = send('patch', f'{S}/org-units/{uid}', tok['hr'], {'nameNe': 'वित्त'})
    assert p.status_code == 200 and p.json()['orgUnit']['name_ne'] == 'वित्त'
    assert send('patch', f'{S}/org-units/{uid}', tok['hr'], {'parentId': uid}).json() == {
        'error': 'an org unit cannot be its own parent'}
    assert len(get(f'{S}/branches', tok['hr']).json()['branches']) == 2
    assert [x['code'] for x in get(f'{S}/org-units', tok['hr']).json()['orgUnits']] == ['FIN']
    assert send('patch', f'{S}/branches/not-a-uuid', tok['hr'], {}).json() == {
        'error': 'validation', 'issues': [{'path': '', 'message': 'Invalid UUID'}]}
    assert get(f'{S}/branches', tok['emp']).status_code == 403
    assert send('post', f'{S}/org-units', tok['emp'], {'kind': 'team', 'code': 'X', 'nameEn': 'X'}).status_code == 403


def test_preview_reports_proposed_vs_current_without_writing(tok):
    before = one('SELECT count(*)::int AS n FROM attendance_day')['n']
    r = send('post', f'{S}/preview/attendance', tok['hr'], {'policy': {'otMinThresholdMin': 0}, 'from': '2026-09-21', 'to': '2026-09-22'})
    assert r.status_code == 200
    assert r.json()['range']['days'] == 2 and r.json()['employees'] == 2
    assert 'late marks' in r.json()['summary']
    assert one('SELECT count(*)::int AS n FROM attendance_day')['n'] == before
    too_long = send('post', f'{S}/preview/attendance', tok['hr'], {'policy': {}, 'from': '2026-01-01', 'to': '2026-12-31'})
    assert too_long.status_code == 400 and too_long.json() == {'error': 'preview range must be 1 to 31 days'}


def test_settings_branches_and_units_follow_the_active_company(world):
    beta, _ = world.session(world.admin_b)
    alpha, _ = world.session(world.admin_a)
    outsider, _ = world.session(world.admin_c)
    bid = send('post', f'{S}/branches', beta, {'code': 'BTB', 'nameEn': 'Beta branch'}).json()['branch']['id']
    uid = send('post', f'{S}/org-units', beta, {'kind': 'team', 'code': 'BT', 'nameEn': 'Beta team'}).json()['orgUnit']['id']
    for token in (alpha, outsider):
        assert send('patch', f'{S}/branches/{bid}', token, {'nameEn': 'Mine'}).json() == {'error': 'branch not found'}
        assert send('patch', f'{S}/org-units/{uid}', token, {'nameEn': 'Mine'}).json() == {'error': 'org unit not found'}
        assert send('post', f'{S}/org-units', token, {'kind': 'team', 'code': 'Z', 'nameEn': 'Z', 'branchId': bid}).json() == {
            'error': 'branch not found'}
        assert bid not in [b['id'] for b in get(f'{S}/branches', token).json()['branches']]
    assert get(f'{S}/company', alpha).json()['company']['code'] == 'ALPHA'
    switched, _ = world.session(world.admin_a, world.beta)
    assert get(f'{S}/company', switched).json()['company']['code'] == 'BETA'
    assert send('patch', f'{S}/branches/{bid}', switched, {'nameEn': 'Renamed'}).status_code == 200
