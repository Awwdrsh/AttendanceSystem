"""
Test data shared by every slice (the TS tests' DEMO company, loginAs and friends).

Plain functions, usable from any test that has the `db` fixture:

    from tests.web.factories import make_company, make_employee, make_user, signed_in_client, PW

    def test_hr_sees_the_list(db):
        co = make_company()
        client, user = signed_in_client(co, roles=['hr'])
        assert client.get('/employees').status_code == 200

and as pytest fixtures (`from tests.web.fixtures import *` in a slice's conftest.py): company, other_company,
employee, hr_client, employee_client, manager_setup, make_client.
"""
from __future__ import annotations

import itertools
import json
from datetime import date, timedelta

from django.test import Client

from hajiri.core import auth
from hajiri.core.db import execute, one

PW = 'correct-horse-battery-staple'
_seq = itertools.count(1)


def _n() -> int:
    return next(_seq)


def make_company(code: str | None = None, name: str = 'Acme Pvt. Ltd.'):
    from hajiri.db.models import Company
    return Company.objects.create(code=code or f'C{_n():04d}', name_en=name)


def make_employee(company, *, code: str | None = None, name: str | None = None, manager=None, branch=None, org_unit=None,
                  employment_type: str = 'regular', since: date | None = None, **fields):
    """An employee with a current assignment (needed for manager scoping). manager: an Employee to report to."""
    from hajiri.db.models import Employee
    n = _n()
    emp = Employee.objects.create(company=company, emp_code=code or f'E{n:04d}', name_en=name or f'Employee {n}', **fields)
    start = since or (date.today() - timedelta(days=365))
    execute("""INSERT INTO employee_assignment (employee_id, valid, org_unit_id, branch_id, manager_id, employment_type)
               VALUES (%s, daterange(%s, NULL), %s, %s, %s, %s)""",
            [emp.id, start, getattr(org_unit, 'id', org_unit), getattr(branch, 'id', branch), getattr(manager, 'id', manager),
             employment_type])
    return emp


def make_user(company, *, roles=('employee',), employee=None, email: str | None = None, password: str = PW,
              name: str | None = None, must_change_password: bool = False) -> auth.AuthUser:
    n = _n()
    return auth.create_user(company_id=str(company.id) if company else None, employee_id=str(employee.id) if employee else None,
                            email=email or f'user{n}@test.local', name=name or (employee.name_en if employee else f'User {n}'),
                            password=password, roles=list(roles), must_change_password=must_change_password)


def sign_in(client: Client, user_or_email, password: str = PW, *, user_agent: str | None = None, ip: str | None = None) -> str:
    """Start a real session (the same rows the TS API uses) and put its cookie on the test client; returns the token."""
    email = user_or_email if isinstance(user_or_email, str) else user_or_email.email
    result = auth.login(email, password, auth.SessionMeta(ip=ip, user_agent=user_agent))
    if result.two_factor_required:
        raise AssertionError('two-step sign-in is on for this user; sign in through /login/2fa instead')
    client.cookies[auth.SESSION_COOKIE] = result.token
    return result.token


def signed_in_client(company, *, roles=('employee',), employee=True, client: Client | None = None, **user_kw):
    """(client, user) for a new user with these roles; employee=True links a new employee, or pass an Employee."""
    emp = make_employee(company) if employee is True else (employee or None)
    user = make_user(company, roles=roles, employee=emp, **user_kw)
    c = client or Client()
    sign_in(c, user)
    return c, user


def bearer(token: str) -> dict:
    """Headers for a Bearer-token request: client.get(url, **bearer(token))."""
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}


def htmx(target: str | None = None, current_url: str | None = None) -> dict:
    """Headers HTMX sends: client.post(url, data, **htmx('table-id'))."""
    h = {'HTTP_HX_REQUEST': 'true'}
    if target:
        h['HTTP_HX_TARGET'] = target
    if current_url:
        h['HTTP_HX_CURRENT_URL'] = current_url
    return h


def hx_triggers(response) -> dict:
    """The HX-Trigger events of a response as a dict ({} if none)."""
    raw = response.headers.get('HX-Trigger')
    return json.loads(raw) if raw else {}


def toasts(response) -> list[dict]:
    """Toasts a response shows: from HX-Trigger, or from the flash cookie set for the next page."""
    items = hx_triggers(response).get('hj:toast', {}).get('items', [])
    if items:
        return items
    from django.core import signing

    from hajiri.web.htmx import _SALT, FLASH_COOKIE
    c = response.cookies.get(FLASH_COOKIE)
    return signing.loads(c.value, salt=_SALT) if c and c.value else []


def set_features_off(company, *keys: str) -> None:
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""",
            [company.id, json.dumps({'off': list(keys)})])
    # The features app caches the switches for 30 s; a test changes them behind its back.
    try:
        from hajiri.features.service import invalidate_features
    except ImportError:
        return
    invalidate_features(str(company.id))


def require_two_factor_for(company, *roles: str) -> None:
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'security.policy', %s)
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""",
            [company.id, json.dumps({'require2faRoles': list(roles)})])


def audit_actions(row_id=None) -> list[str]:
    from hajiri.core.db import rows
    q = 'SELECT action FROM audit_log' + (' WHERE row_id = %s' if row_id else '') + ' ORDER BY id'
    return [r['action'] for r in rows(q, [str(row_id)] if row_id else [])]


def user_row(user) -> dict:
    return one('SELECT * FROM app_user WHERE id = %s', [user.id])
