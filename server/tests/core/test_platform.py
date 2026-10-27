"""Account administration (qa-foundation.test.ts auth hardening, security-auth.test.ts account administration and
auth.test.ts user creation), the request schema library, error mapping, BS labels and the management commands."""
import io
from datetime import date, datetime, timezone

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from hajiri.core import auth
from hajiri.core.bsdate import bs_label, bs_month_name, bs_range
from hajiri.core.db import execute, one
from hajiri.core.errors import (
    BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError, error_body, from_db_error,
)
from hajiri.core.http import ApiEncoder
from hajiri.core.schema import Arr, Bool, Enum, Int, IsoDate, Obj, Str, Uuid, parse
from hajiri.core.users import admin_create_user, admin_update_user, list_users, reset_password
from tests.core.fixtures import PW

OTHER = '00000000-0000-7000-8000-00000000aaaa'


# ---------------------------------------------------------------- account administration

def test_the_last_active_admin_cannot_be_disabled_or_demoted_and_nobody_disables_themselves(login_as):
    admin = login_as(['admin'])
    with pytest.raises(BadRequestError, match='own account'):
        admin_update_user(admin.user, admin.user_id, {'status': 'disabled'})
    second = login_as(['admin'])
    assert admin_update_user(admin.user, second.user_id, {'roles': ['hr']})['roles'] == ['hr']
    with pytest.raises(BadRequestError, match='own admin role'):
        admin_update_user(admin.user, admin.user_id, {'roles': ['hr']})
    a = one("SELECT before, after FROM audit_log WHERE action = 'update' AND table_name = 'app_user' ORDER BY id DESC LIMIT 1")
    assert a['before']['roles'] == ['admin'] and a['after'] == {'roles': ['hr']}


def test_the_last_active_admin_stays(login_as):
    admin = login_as(['admin'])
    other = login_as(['admin'])
    admin_update_user(admin.user, other.user_id, {'status': 'disabled'})
    assert auth.resolve_session(other.token) is None  # disabling signs out everywhere
    # A session that still claims admin after its account was demoted cannot remove the only active admin either.
    stale = login_as(['admin'])
    execute("UPDATE app_user SET roles = '{hr}' WHERE id = %s", [stale.user_id])
    with pytest.raises(BadRequestError, match='last active admin'):
        admin_update_user(stale.user, admin.user_id, {'roles': ['hr']})
    with pytest.raises(BadRequestError, match='last active admin'):
        admin_update_user(stale.user, admin.user_id, {'status': 'disabled'})


def test_temporary_password_blocks_account_administration(demo):
    auth.create_user(company_id=demo['company_id'], email='temp-admin@test.local', name='t', password=PW, roles=['admin'],
                     must_change_password=True)
    u = auth.resolve_session(auth.login('temp-admin@test.local', PW).token)
    with pytest.raises(ForbiddenError, match='password change required'):
        list_users(u)
    with pytest.raises(ForbiddenError, match='password change required'):
        admin_create_user(u, {'email': 'x@test.local', 'name': 'x', 'password': 'another-long-password', 'roles': ['admin']})


def test_admin_creates_users_hr_cannot_and_duplicates_conflict(login_as):
    admin, hr = login_as(['admin']), login_as(['hr'])
    body = {'email': 'new@test.local', 'name': 'New', 'password': 'a-strong-passphrase-99', 'roles': ['manager']}
    with pytest.raises(ForbiddenError):
        admin_create_user(hr.user, body)
    u = admin_create_user(admin.user, body)
    assert u.must_change_password and u.roles == ('manager',)
    with pytest.raises(IntegrityError) as e, transaction.atomic():
        admin_create_user(admin.user, body)
    assert isinstance(from_db_error(e.value), ConflictError)
    assert [x['email'] for x in list_users(hr.user)].count('new@test.local') == 1
    with pytest.raises(BadRequestError, match='at least 12'):
        admin_create_user(admin.user, {**body, 'email': 'weak@test.local', 'password': 'short'})
    with pytest.raises(ValidationError):
        admin_create_user(admin.user, {**body, 'email': 'n@test.local', 'name': 'x' * 500})
    with pytest.raises(ValidationError):
        admin_create_user(admin.user, {**body, 'roles': ['root']})


def test_users_of_one_company_are_invisible_to_another_companys_admin(login_as):
    admin = login_as(['admin'])
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHER', 'Other Co')", [OTHER])
    o = login_as(['admin'], company_id=OTHER)
    with pytest.raises(NotFoundError, match='user not found'):
        admin_update_user(o.user, admin.user_id, {'status': 'disabled'})
    assert admin.user_id not in [str(x['id']) for x in list_users(o.user)]


def test_cannot_link_a_user_to_an_employee_of_another_company(demo, login_as):
    other = one("INSERT INTO company (code, name_en) VALUES ('SEC1', 'Other') RETURNING id")['id']
    foreign = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'F1', 'Foreign') RETURNING id", [other])['id'])
    admin = login_as(['admin'])
    body = {'email': 'link@test.local', 'name': 'Link', 'password': 'a-long-enough-password', 'roles': ['employee']}
    with pytest.raises(NotFoundError, match='employee not found'):
        admin_create_user(admin.user, {**body, 'employeeId': foreign})
    u = admin_create_user(admin.user, body)
    with pytest.raises(NotFoundError):
        admin_update_user(admin.user, u.id, {'employeeId': foreign})
    assert one('SELECT employee_id FROM app_user WHERE id = %s', [u.id])['employee_id'] is None
    assert str(admin_update_user(admin.user, u.id, {'employeeId': demo['sita']})['employee_id']) == demo['sita']
    assert admin_update_user(admin.user, u.id, {'employeeId': None})['employee_id'] is None


def test_reset_password_clears_lockout_and_sessions(demo):
    auth.create_user(company_id=demo['company_id'], email='r@test.local', name='r', password=PW, roles=['employee'])
    tok = auth.login('r@test.local', PW).token
    execute("UPDATE app_user SET failed_logins = 10, locked_until = now() + interval '1 hour' WHERE email = 'r@test.local'")
    reset_password('R@test.local', 'brand-new-passphrase')
    assert auth.resolve_session(tok) is None
    assert auth.login('r@test.local', 'brand-new-passphrase').token
    with pytest.raises(NotFoundError):
        reset_password('nobody@test.local', 'brand-new-passphrase')


# ---------------------------------------------------------------- schema library

def test_schema_defaults_optional_strict_and_issues():
    s = Obj({'a': Int(min=1, default=5), 'b': Str(optional=True), 'c': Str(nullish=True), 'd': Bool(default=False),
             'e': Arr(Enum(('x', 'y')), max=2, default=list)}, strict=True)
    assert parse(s, {}) == {'a': 5, 'd': False, 'e': []}
    assert parse(s, {'c': None, 'b': 'z', 'a': 2.0}) == {'a': 2, 'b': 'z', 'c': None, 'd': False, 'e': []}
    with pytest.raises(ValidationError) as e:
        parse(s, {'a': 0, 'e': ['x', 'q'], 'zz': 1})
    paths = {i['path'] for i in e.value.issues}
    assert paths == {'', 'a', 'e.1'}
    assert parse(Obj({'k': Str()}), {'k': 'v', 'extra': 1}) == {'k': 'v'}  # unknown keys are stripped
    with pytest.raises(ValidationError):
        parse(Int(), True)  # a boolean is not a number
    with pytest.raises(ValidationError):
        parse(Int(), 1.5)


def test_schema_dates_and_uuids():
    assert parse(IsoDate(), '2026-02-28') == '2026-02-28'
    for bad in ('2026-13-40', '2026-02-30', '26-01-01', 20260101):
        with pytest.raises(ValidationError):
            parse(IsoDate(), bad)
    assert parse(Uuid(), '00000000-0000-7000-8000-00000000AAAA') == '00000000-0000-7000-8000-00000000aaaa'
    with pytest.raises(ValidationError):
        parse(Uuid(), 'nope')
    assert parse(Int(coerce=True), '42') == 42
    assert parse(Str(trim=True, min=1), '  x ') == 'x'


def test_error_bodies_and_database_errors(db):
    assert error_body(ValidationError([{'path': 'a', 'message': 'Required'}])) == {'error': 'validation', 'issues': [{'path': 'a', 'message': 'Required'}]}
    assert error_body(NotFoundError('employee not found')) == {'error': 'employee not found'}
    execute("INSERT INTO company (code, name_en) VALUES ('DUP', 'A')")
    with pytest.raises(IntegrityError) as e, transaction.atomic():
        execute("INSERT INTO company (code, name_en) VALUES ('DUP', 'B')")
    err = from_db_error(e.value)
    assert isinstance(err, ConflictError) and error_body(err) == {'error': 'already exists', 'constraint': 'company_code_key'}
    from django.db import DatabaseError
    with pytest.raises(DatabaseError) as e2, transaction.atomic():
        execute("SELECT 'x'::uuid")
    assert from_db_error(e2.value).message == 'invalid value format'


def test_api_encoder_matches_json_stringify():
    import json
    from decimal import Decimal
    out = json.loads(json.dumps({'t': datetime(2026, 9, 21, 4, 32, 1, 123456, tzinfo=timezone.utc), 'd': date(2026, 9, 21),
                                 'n': Decimal('100.00')}, cls=ApiEncoder))
    assert out == {'t': '2026-09-21T04:32:01.123Z', 'd': '2026-09-21', 'n': '100.00'}


# ---------------------------------------------------------------- BS labels

def test_bs_labels():
    assert bs_label('2026-09-21') == '5 Ashwin 2083'
    assert bs_label(date(2024, 4, 13)) == '1 Baishakh 2081'
    assert bs_label(None) == '' and bs_label('1800-01-01') == '1800-01-01'
    assert bs_range('2026-09-21', '2026-09-21') == '5 Ashwin 2083'
    assert bs_range('2026-09-21', '2026-10-19') == '5 Ashwin 2083 to 2 Kartik 2083'
    assert bs_month_name(2083, 6) == 'Ashwin 2083'


# ---------------------------------------------------------------- management commands

def test_cli_create_company_admin_reset_and_seed(db):
    out = io.StringIO()
    call_command('create_company', '--code', 'ACME', '--name', 'Acme Pvt. Ltd.', stdout=out)
    assert 'company ACME created' in out.getvalue()
    with pytest.raises(CommandError, match='already exists'):
        call_command('create_company', '--code', 'ACME', '--name', 'Again', stdout=io.StringIO())
    with pytest.raises(CommandError, match='at least 12'):
        call_command('create_admin', '--email', 'a@acme.np', '--password', 'short', '--company', 'ACME', stdout=io.StringIO())
    with pytest.raises(CommandError, match='not found'):
        call_command('create_admin', '--email', 'a@acme.np', '--password', 'long-enough-passphrase', '--company', 'NOPE', stdout=io.StringIO())
    call_command('create_admin', '--email', 'a@acme.np', '--password', 'long-enough-passphrase', '--company', 'ACME', stdout=out)
    u = one("SELECT roles, must_change_password FROM app_user WHERE email = 'a@acme.np'")
    assert u['roles'] == ['admin'] and u['must_change_password'] is True
    call_command('reset_password', '--email', 'a@acme.np', '--password', 'another-long-passphrase', stdout=out)
    assert auth.login('a@acme.np', 'another-long-passphrase').token
    call_command('seed_defaults', '--company', 'ACME', stdout=out)
    assert "'leaveTypes': 0" in out.getvalue().splitlines()[-1]


def test_cli_password_from_environment(db, monkeypatch):
    call_command('create_company', '--code', 'ENVCO', '--name', 'Env Co', '--no-seed', stdout=io.StringIO())
    monkeypatch.setenv('HAJIRI_PASSWORD', 'from-the-environment-1')
    call_command('create_admin', '--email', 'env@acme.np', '--company', 'ENVCO', stdout=io.StringIO())
    assert auth.login('env@acme.np', 'from-the-environment-1').token


def test_cli_seed_bs_calendar_and_demo_guard(db, settings):
    out = io.StringIO()
    call_command('seed_bs_calendar', stdout=out)
    assert one("SELECT bs_date FROM bs_calendar WHERE ad_date = '2026-09-21'")['bs_date'] == '2083-06-05'
    with pytest.raises(CommandError, match='Confirm with'):
        call_command('seed_demo', '--yes', stdout=io.StringIO())
    call_command('seed_demo', '--yes', '--database-name', settings.DATABASES['default']['NAME'], stdout=out)
    assert one("SELECT name_en FROM company WHERE code = 'DEMO'")['name_en'] == 'Demo Company'


def test_cli_run_worker_once(demo):
    out = io.StringIO()
    call_command('run_worker', '--once', '--jobs', 'queue,digest', stdout=out)
    assert '"queue"' in out.getvalue()
