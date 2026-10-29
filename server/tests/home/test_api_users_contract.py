"""The /api/v1/auth/users endpoints as the React Users page (frontend/src/pages/hr/UsersPage.jsx) calls them through
client.js (session cookie, no CSRF token, JSON body), compared field by field with auth/routes.ts at 65a9079, plus the
calls the Setup checklist (pages/hr/SetupPage.jsx) makes and the wrappers it reads."""
import json
import re

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import AppUser, Company, Employee, Session
from tests.support.sql import execute
from tests.web.factories import PW, make_user, sign_in

ISO_Z = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
API = '/api/v1/auth/users'
# SELECT id, employee_id, email, name, roles, status, last_login_at, created_at, two_factor (+ membership, group support)
LIST_KEYS = {'id', 'employee_id', 'email', 'name', 'roles', 'status', 'last_login_at', 'created_at', 'two_factor', 'membership'}
# toUser(): the AuthUser shape
AUTH_USER_KEYS = {'id', 'companyId', 'employeeId', 'email', 'name', 'roles', 'mustChangePassword', 'twoFactorEnabled',
                  'mustSetUpTwoFactor'}
# UPDATE ... RETURNING id, email, name, roles, status, employee_id
PATCH_KEYS = {'id', 'email', 'name', 'roles', 'status', 'employee_id'}


def browser(user):
    c = Client(enforce_csrf_checks=True)
    sign_in(c, user)
    return c


def send(c, method, path, body=None, **headers):
    return getattr(c, method)(path, json.dumps(body) if body is not None else None, content_type='application/json',
                              **headers)


@pytest.fixture
def company(demo):
    return Company.objects.get(pk=demo['company_id'])


def test_the_list_has_every_column_the_page_reads_with_iso_timestamps(demo, company):
    admin = make_user(company, roles=['admin'])
    sita = Employee.objects.get(pk=demo['sita'])
    linked = make_user(company, roles=['employee'], employee=sita)
    never = make_user(company, roles=['hr'])
    browser(linked)          # gives `linked` a last sign-in
    r = browser(admin).get(API)
    assert r.status_code == 200
    rows = {u['id']: u for u in r.json()['users']}
    assert all(set(u) == LIST_KEYS for u in rows.values())
    lk = rows[linked.id]
    assert lk['employee_id'] == demo['sita'] and lk['roles'] == ['employee'] and lk['status'] == 'active'
    assert ISO_Z.match(lk['last_login_at']) and ISO_Z.match(lk['created_at'])
    assert lk['two_factor'] is False and lk['membership'] is False
    assert rows[never.id]['last_login_at'] is None and rows[never.id]['employee_id'] is None
    # ORDER BY created_at
    created = [u['created_at'] for u in r.json()['users']]
    assert created == sorted(created)


def test_hr_reads_the_list_but_only_an_admin_creates_or_changes(demo, company):
    hr = browser(make_user(company, roles=['hr']))
    emp = make_user(company, roles=['employee'])
    assert hr.get(API).status_code == 200
    denied = send(hr, 'post', API, {'email': 'x@test.local', 'name': 'X', 'password': 'a-long-enough-pass', 'roles': ['hr']})
    assert denied.status_code == 403 and denied.json() == {'error': 'requires role admin'}
    assert send(hr, 'patch', f'{API}/{emp.id}', {'status': 'disabled'}).json() == {'error': 'requires role admin'}
    assert send(hr, 'post', f'{API}/{emp.id}/2fa/reset', {'reason': 'lost phone'}).status_code == 403
    staff = browser(emp)
    assert staff.get(API).status_code == 403 and staff.get(API).json() == {'error': 'requires role hr'}
    assert Client().get(API).status_code == 401


def test_add_user_returns_the_auth_user_and_the_errors_the_drawer_shows(demo, company):
    c = browser(make_user(company, roles=['admin']))
    body = {'email': 'New.Person@Test.Local', 'name': '  New Person ', 'password': 'temporary-passphrase-1',
            'roles': ['employee', 'payroll_admin']}
    r = send(c, 'post', API, body)
    assert r.status_code == 201
    u = r.json()['user']
    assert set(u) == AUTH_USER_KEYS
    assert u['email'] == 'new.person@test.local' and u['name'] == 'New Person' and u['companyId'] == demo['company_id']
    assert u['employeeId'] is None and u['roles'] == ['employee', 'payroll_admin']
    assert u['mustChangePassword'] is True and u['twoFactorEnabled'] is False
    # with an employee link (the EmployeePicker value)
    r2 = send(c, 'post', API, {**body, 'email': 'linked@test.local', 'employeeId': demo['ram']})
    assert r2.status_code == 201 and r2.json()['user']['employeeId'] == demo['ram']
    # same address in any case: unique violation
    dup = send(c, 'post', API, {**body, 'email': 'NEW.person@test.local'})
    assert dup.status_code == 409 and dup.json()['error'] == 'already exists'
    short = send(c, 'post', API, {**body, 'email': 'short@test.local', 'password': 'short'})
    assert short.status_code == 400 and '12' in short.json()['error']
    bad = send(c, 'post', API, {'email': 'not-an-email', 'name': '', 'password': 'temporary-passphrase-1', 'roles': []})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert {i['path'] for i in bad.json()['issues']} >= {'email', 'name', 'roles'}
    missing_emp = send(c, 'post', API, {**body, 'email': 'ghost@test.local', 'employeeId': '00000000-0000-4000-8000-000000000000'})
    assert missing_emp.status_code == 404 and missing_emp.json() == {'error': 'employee not found'}


def test_role_and_status_changes_return_the_updated_row(demo, company):
    c = browser(make_user(company, roles=['admin']))
    target = make_user(company, roles=['employee'])
    token = auth.login(target.email, PW).token
    r = send(c, 'patch', f'{API}/{target.id}', {'roles': ['employee', 'manager']})
    assert r.status_code == 200
    assert r.json() == {'user': {'id': target.id, 'email': target.email, 'name': target.name,
                                 'roles': ['employee', 'manager'], 'status': 'active', 'employee_id': None}}
    off = send(c, 'patch', f'{API}/{target.id}', {'status': 'disabled'})
    assert off.status_code == 200 and set(off.json()['user']) == PATCH_KEYS and off.json()['user']['status'] == 'disabled'
    # Disabling signs them out everywhere.
    from hajiri.core.crypto import sha256_hex
    assert Session.objects.get(token_hash=sha256_hex(token)).revoked_at is not None
    on = send(c, 'patch', f'{API}/{target.id}', {'status': 'active'})
    assert on.json()['user']['status'] == 'active'
    # An empty body changes nothing and still answers with the row.
    same = send(c, 'patch', f'{API}/{target.id}', {})
    assert same.status_code == 200 and same.json()['user']['roles'] == ['employee', 'manager']
    assert send(c, 'patch', f'{API}/not-a-uuid', {'status': 'active'}).status_code == 400


def test_two_step_reset_from_the_users_page(demo, company):
    admin = make_user(company, roles=['admin'])
    c = browser(admin)
    target = make_user(company, roles=['employee'])
    execute("UPDATE app_user SET totp_enabled_at = now() WHERE id = %s", [target.id])
    assert next(u for u in c.get(API).json()['users'] if u['id'] == target.id)['two_factor'] is True
    short = send(c, 'post', f'{API}/{target.id}/2fa/reset', {'reason': ' x '})
    assert short.status_code == 400 and short.json()['error'] == 'validation'
    ok = send(c, 'post', f'{API}/{target.id}/2fa/reset', {'reason': 'Lost phone'})
    assert ok.status_code == 200 and ok.json() == {'ok': True}
    assert AppUser.objects.get(pk=target.id).totp_enabled_at is None
    own = send(c, 'post', f'{API}/{admin.id}/2fa/reset', {'reason': 'Lost phone'})
    assert own.status_code == 400 and 'own sign-in security page' in own.json()['error']
    other = Company.objects.exclude(pk=company.pk).first()
    if other is None:
        other = Company.objects.create(code='OTHR', name_en='Other Co')
    stranger = make_user(other, roles=['employee'])
    nf = send(c, 'post', f'{API}/{stranger.id}/2fa/reset', {'reason': 'Lost phone'})
    assert nf.status_code == 404 and nf.json() == {'error': 'user not found'}


def test_cookie_writes_from_another_site_are_refused(demo, company):
    c = browser(make_user(company, roles=['admin']))
    target = make_user(company, roles=['employee'])
    r = send(c, 'patch', f'{API}/{target.id}', {'status': 'disabled'}, HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert AppUser.objects.get(pk=target.id).status == 'active'
    same = send(c, 'patch', f'{API}/{target.id}', {'status': 'disabled'}, HTTP_ORIGIN='http://testserver')
    assert same.status_code == 200


def test_setup_checklist_reads_every_wrapper_it_counts(demo, company):
    """SetupPage.jsx: company.company.nameEn and the length of branches/holidays/shifts/types/components/employees/
    devices lists, device vendor and status, the import's `inserted`, and the one-click GEN shift body."""
    c = browser(make_user(company, roles=['admin']))
    co = c.get('/api/v1/settings/company')
    assert co.status_code == 200 and isinstance(co.json()['company']['nameEn'], str)
    for path, key in (('/api/v1/settings/branches', 'branches'), ('/api/v1/shifts/holidays', 'holidays'),
                      ('/api/v1/shifts', 'shifts'), ('/api/v1/leave/types', 'types'),
                      ('/api/v1/payroll/components', 'components'), ('/api/v1/employees?status=active', 'employees'),
                      ('/api/v1/devices', 'devices')):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code, r.content[:200])
        assert isinstance(r.json()[key], list), path
    devs = c.get('/api/v1/devices').json()['devices']
    assert devs and all({'vendor', 'status'} <= set(d) for d in devs)


GEN_SHIFT = {'code': 'GEN2', 'nameEn': 'General 10:00–18:00', 'start': '10:00', 'end': '18:00', 'graceInMin': 15,
             'graceOutMin': 0, 'earlyInWindowMin': 60, 'lateOutWindowMin': 120, 'breakMinutes': 0,
             'halfDayThresholdMin': 240, 'absentThresholdMin': 120, 'pairingMode': 'alternating', 'roundingMin': 0,
             'roundingDirection': 'nearest'}


def _one_click_defaults(c, **headers):
    imp = send(c, 'post', '/api/v1/shifts/holidays/import-default', {'bsYear': 2083}, **headers)
    assert imp.status_code == 200, imp.content[:300]
    assert isinstance(imp.json()['inserted'], int)
    gen = send(c, 'post', '/api/v1/shifts', GEN_SHIFT, **headers)
    assert gen.status_code == 201, gen.content[:300]


def test_setup_one_click_defaults_answer_what_the_page_reads(demo, company):
    """The import's `inserted` and the GEN shift body, with a Bearer token (shape only)."""
    token = auth.login(make_user(company, roles=['admin']).email, PW).token
    _one_click_defaults(Client(), HTTP_AUTHORIZATION=f'Bearer {token}')


def test_setup_one_click_defaults_from_the_browser(demo, company):
    _one_click_defaults(browser(make_user(company, roles=['admin'])))


def test_home_page_shift_label_fields(demo, company):
    """HomePage.jsx finds today's shift by id in /shifts and shows name_en || code, start.slice(0, 5), end.slice(0, 5)."""
    emp = make_user(company, roles=['employee'], employee=Employee.objects.get(pk=demo['sita']))
    r = browser(emp).get('/api/v1/shifts')
    assert r.status_code == 200
    gen = next(s for s in r.json()['shifts'] if s['id'] == demo['shift_id'])
    assert isinstance(gen['code'], str) and 'name_en' in gen
    assert re.match(r'^\d{2}:\d{2}', gen['start']) and re.match(r'^\d{2}:\d{2}', gen['end'])
