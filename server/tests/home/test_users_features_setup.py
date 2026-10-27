"""Settings -> Users & roles (UsersPage.tsx), Features (FeaturesPage.tsx) and Setup checklist (SetupPage.tsx)."""
import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.db import execute, one
from hajiri.core.users import list_users
from hajiri.features.service import disabled_features, invalidate_features
from tests.web.factories import PW, htmx, hx_triggers, make_employee, make_user, sign_in, signed_in_client, toasts


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


# ---------------------------------------------------------------- users & roles

def test_users_list_is_for_hr_and_never_shows_secrets(demo, login_as):
    """endpoints.test.ts 'GET /auth/users lists company users for HR only'."""
    hr = login_as(['hr'])
    login_as(['admin']); login_as(['employee'], demo['sita']); login_as(['manager', 'employee'], demo['ram'])
    users = list_users(hr.user)
    assert len(users) >= 4
    u = users[0]
    assert isinstance(str(u['id']), str) and isinstance(u['email'], str) and isinstance(u['roles'], list) and isinstance(u['status'], str)
    assert 'password_hash' not in u
    r = client_for(hr).get('/settings/users')
    html = r.content.decode()
    assert r.status_code == 200 and 'Users &amp; roles' in html or 'Who can sign in and what they can do.' in html
    assert 'password_hash' not in html and 'scrypt$' not in html
    # HR who is not an admin sees the roles as text and no admin buttons.
    assert 'Add user' not in html and 'Disable</button>' not in html
    emp = client_for(login_as(['employee'], None))
    assert emp.get('/settings/users').status_code == 403
    assert Client().get('/settings/users')['Location'].startswith('/login')
    assert Client().get('/settings/users', HTTP_ACCEPT='application/json').status_code == 401


def test_admin_adds_a_user_who_must_change_the_password(acme):
    c, admin = signed_in_client(acme, roles=['admin'], employee=None)
    html = c.get('/settings/users').content.decode()
    assert 'Add user' in html
    form = c.get('/_/home/users/new', **htmx('hj-drawer-body')).content.decode()
    assert 'At least 12 characters. They will be asked to change it at first sign-in.' in form
    assert 'Payroll admin: Payroll, salary structures, loans and tax only' in form
    emp = make_employee(acme, name='Gita')
    r = c.post('/_/home/users/new', {'name': 'Gita K', 'email': 'gita@acme.test', 'password': 'short', 'roles': ['employee']}, **htmx())
    assert r.status_code == 400
    r = c.post('/_/home/users/new', {'name': 'Gita K', 'email': 'gita@acme.test', 'password': 'a-long-temporary-pass',
                                     'roles': ['employee', 'manager'], 'employee_id': str(emp.id)}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'User added.' and 'hj:refresh-users' in hx_triggers(r)
    row = one("SELECT roles, employee_id, must_change_password FROM app_user WHERE email = 'gita@acme.test'")
    assert sorted(row['roles']) == ['employee', 'manager'] and str(row['employee_id']) == str(emp.id) and row['must_change_password']
    # HR (not admin) cannot add users.
    hr, _ = signed_in_client(acme, roles=['hr'])
    assert hr.post('/_/home/users/new', {'name': 'X', 'email': 'x@acme.test', 'password': 'a-long-temporary-pass', 'roles': ['employee']},
                   **htmx()).status_code == 403


def test_admin_changes_roles_and_status(acme, other_company):
    c, admin = signed_in_client(acme, roles=['admin'], employee=None)
    target = make_user(acme, roles=['employee'])
    r = c.post(f'/_/home/users/{target.id}', {'roles_sent': '1', 'roles': ['employee', 'hr']}, **htmx())
    assert r.status_code == 204 and 'hj:refresh-users' in hx_triggers(r)
    assert sorted(one('SELECT roles FROM app_user WHERE id = %s', [target.id])['roles']) == ['employee', 'hr']
    # No role at all: not saved, with the TS hint.
    r = c.post(f'/_/home/users/{target.id}', {'roles_sent': '1'}, **htmx())
    assert toasts(r)[0]['message'] == 'A user needs at least one role. Disable the user instead.'
    assert sorted(one('SELECT roles FROM app_user WHERE id = %s', [target.id])['roles']) == ['employee', 'hr']
    sign_in(Client(), target)
    r = c.post(f'/_/home/users/{target.id}', {'status': 'disabled'}, **htmx())
    assert r.status_code == 204 and one('SELECT status FROM app_user WHERE id = %s', [target.id])['status'] == 'disabled'
    assert one('SELECT count(*)::int AS n FROM session WHERE user_id = %s AND revoked_at IS NULL', [target.id])['n'] == 0
    # Yourself and the last admin are protected; another company's user is not found.
    r = c.post(f'/_/home/users/{admin.id}', {'status': 'disabled'}, **htmx())
    assert r.status_code == 400 and 'you cannot disable your own account' in toasts(r)[0]['message']
    foreign = make_user(other_company, roles=['employee'])
    assert c.post(f'/_/home/users/{foreign.id}', {'status': 'disabled'}, **htmx()).status_code == 404
    hr, _ = signed_in_client(acme, roles=['hr'])
    assert hr.post(f'/_/home/users/{target.id}', {'status': 'active'}, **htmx()).status_code == 403
    html = c.get('/settings/users').content.decode()
    assert 'Activate</button>' in html and 'Roles for' in html


def test_admin_resets_two_step_sign_in(acme, other_company):
    c, admin = signed_in_client(acme, roles=['admin'], employee=None)
    target = make_user(acme, roles=['employee'], name='Hari')
    execute("UPDATE app_user SET totp_enabled_at = now(), totp_secret_enc = 'x' WHERE id = %s", [target.id])
    html = c.get('/settings/users').content.decode()
    assert f'/_/home/users/{target.id}/reset' in html
    form = c.get(f'/_/home/users/{target.id}/reset', **htmx('hj-modal-body')).content.decode()
    assert 'Hari is signed out everywhere' in form and 'Kept in the audit log.' in form
    assert c.post(f'/_/home/users/{target.id}/reset', {'reason': 'x'}, **htmx()).status_code == 400
    r = c.post(f'/_/home/users/{target.id}/reset', {'reason': 'Lost phone'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Two-step sign-in reset for Hari.'
    assert one('SELECT totp_enabled_at FROM app_user WHERE id = %s', [target.id])['totp_enabled_at'] is None
    a = one("SELECT after FROM audit_log WHERE action = 'two_factor_reset' AND row_id = %s", [target.id])
    assert a['after'] == {'reason': 'Lost phone'}
    assert c.post(f'/_/home/users/{admin.id}/reset', {'reason': 'Lost phone'}, **htmx()).status_code == 400
    foreign = make_user(other_company, roles=['employee'])
    assert c.get(f'/_/home/users/{foreign.id}/reset', **htmx()).status_code == 404
    hr, _ = signed_in_client(acme, roles=['hr'])
    assert hr.post(f'/_/home/users/{target.id}/reset', {'reason': 'Lost phone'}, **htmx()).status_code == 403


# ---------------------------------------------------------------- features

def test_features_page_switches_modules(acme):
    invalidate_features()
    c, _ = signed_in_client(acme, roles=['hr'])
    html = c.get('/settings/features').content.decode()
    assert 'Time tracking &amp; timesheets' in html and 'Engagement' in html
    assert 'Attendance, leave, payroll, people, approvals and reports are the core of Hajiri and are always on.' in html
    on = ['field', 'expenses', 'performance', 'learning', 'hiring', 'lifecycle', 'documents', 'helpdesk', 'tasks', 'kudos',
          'surveys', 'suggestions']  # time switched off
    r = c.post('/_/home/features', {'on': on}, **htmx())
    assert r.status_code == 204 and r['HX-Redirect'] == '/settings/features'
    assert toasts(r)[0] == {'message': 'Everyone sees the change the next time they open a page.', 'tone': 'ok', 'title': 'Features saved'}
    assert disabled_features(str(acme.id)) == frozenset({'time'})
    assert c.post('/_/home/features', {'on': on}, **htmx()).status_code == 400  # nothing changed
    emp, _ = signed_in_client(acme, roles=['employee'])
    assert emp.get('/settings/features').status_code == 403
    assert emp.post('/_/home/features', {'on': []}, **htmx()).status_code == 403
    # The Home page drops the timesheet card with the module.
    assert '/_/timesheets/home-timer' not in emp.get('/').content.decode()


# ---------------------------------------------------------------- setup checklist

def test_setup_checklist_and_its_one_click_defaults(demo, login_as):
    hr = client_for(login_as(['hr']))
    html = hr.get('/settings/setup').content.decode()
    assert 'Setup checklist' in html or 'Work from top to bottom.' in html
    assert 'Demo Company' in html and '1 branch' in html and '1 shift' in html and '2 active' in html
    assert 'Add public holidays' in html and 'Load the public holidays for this year.' in html
    assert 'Create 10:00 to 18:00 shift' not in html  # there is a shift already
    assert '1 terminal, 0 waiting for approval' in html
    r = hr.post('/_/home/setup/holidays', **htmx('setup-steps'))
    assert r.status_code == 200 and toasts(r)[0]['message'].endswith(' holidays added.')
    assert 'Add public holidays' not in r.content.decode()
    assert hr.post('/_/home/setup/nope', **htmx()).status_code == 400
    assert client_for(login_as(['employee'], demo['sita'])).get('/settings/setup').status_code == 403


def test_setup_creates_the_general_shift_when_there_is_none(acme):
    c, _ = signed_in_client(acme, roles=['hr'])
    html = c.get('/settings/setup').content.decode()
    assert 'Create 10:00 to 18:00 shift' in html and 'Define working hours and grace periods.' in html
    assert '2 of 9 steps done' in html  # the company name and the one employee
    r = c.post('/_/home/setup/shift', **htmx('setup-steps'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'General shift created.'
    s = one("SELECT code, name_en, start_time::text, grace_in_min, pairing_mode FROM shift WHERE company_id = %s", [acme.id])
    assert s == {'code': 'GEN', 'name_en': 'General 10:00–18:00', 'start_time': '10:00:00', 'grace_in_min': 15, 'pairing_mode': 'alternating'}
