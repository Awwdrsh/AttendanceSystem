"""Port of the sign-in, session and two-step behaviour covered by apps/api/test/auth*.test.ts."""
import time
from datetime import timedelta

import pytest

from hajiri.core import auth
from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError, ForbiddenError, UnauthorizedError
from hajiri.core.totp import totp_at, TOTP_STEP_SECONDS

PW = 'a long enough passphrase'


@pytest.fixture
def user(company):
    return auth.create_user(company_id=str(company.id), email='Sita@Example.com', name='Sita', password=PW, roles=['employee'])


def test_login_creates_a_session_that_resolves(user):
    assert user.email == 'sita@example.com'
    r = auth.login('SITA@example.com', PW, auth.SessionMeta(ip='10.0.0.1', user_agent='Firefox/130 (Windows)'))
    assert r.token and not r.two_factor_required
    assert auth.resolve_session(r.token).id == user.id
    auth.logout(r.token)
    assert auth.resolve_session(r.token) is None


def test_wrong_password_locks_after_ten_tries(user):
    for _ in range(auth.MAX_FAILED):
        with pytest.raises(UnauthorizedError, match='invalid email or password'):
            auth.login(user.email, 'wrong password!!')
    with pytest.raises(UnauthorizedError, match='temporarily locked'):
        auth.login(user.email, PW)


def test_unknown_email_is_the_same_error(db):
    with pytest.raises(UnauthorizedError, match='invalid email or password'):
        auth.login('nobody@example.com', PW)


def test_idle_session_is_revoked(user):
    token = auth.login(user.email, PW).token
    execute("UPDATE session SET last_seen_at = now() - interval '61 minutes'")
    assert auth.resolve_session(token) is None
    assert one('SELECT revoked_at IS NOT NULL AS r FROM session')['r']


def test_leaver_cannot_sign_in(company):
    from hajiri.db.models import Employee
    emp = Employee.objects.create(company=company, emp_code='E9', name_en='Left', exit_date='2020-01-01')
    auth.create_user(company_id=str(company.id), employee_id=str(emp.id), email='left@example.com', name='Left', password=PW, roles=['employee'])
    with pytest.raises(UnauthorizedError, match='closed when you left'):
        auth.login('left@example.com', PW)


def test_change_password_signs_out_everywhere(user):
    token = auth.login(user.email, PW).token
    with pytest.raises(BadRequestError):
        auth.change_password(user.id, PW, PW)
    auth.change_password(user.id, PW, PW + ' two')
    assert auth.resolve_session(token) is None
    assert auth.login(user.email, PW + ' two').token


def test_other_sessions(user):
    a = auth.login(user.email, PW, auth.SessionMeta(user_agent='Mozilla/5.0 (iPhone) Safari/604')).token
    b = auth.login(user.email, PW).token
    listed = auth.list_sessions(user.id, a)
    assert [s.current for s in listed] == [True, False] and listed[0].device == 'Safari on iOS'
    with pytest.raises(BadRequestError):
        auth.revoke_session(user.id, listed[0].id, a)
    assert auth.revoke_other_sessions(user.id, a) == 1
    assert auth.resolve_session(b) is None and auth.resolve_session(a)


def _code(secret: str, offset_steps: int = 0) -> str:
    return totp_at(secret, time.time() + offset_steps * TOTP_STEP_SECONDS)


def test_two_step_enrolment_login_and_replay(user):
    setup = auth.begin_setup(user)
    assert setup['otpauth_uri'].startswith('otpauth://totp/Hajiri:sita%40example.com?secret=')
    codes = auth.confirm_setup(user, _code(setup['secret']))
    assert len(codes) == 10

    first = auth.login(user.email, PW)
    assert first.two_factor_required and first.token is None
    # The step used to confirm cannot be replayed; the next step works.
    with pytest.raises(UnauthorizedError, match='not correct'):
        auth.complete_two_factor_login(first.challenge, _code(setup['secret']))
    done = auth.complete_two_factor_login(first.challenge, _code(setup['secret'], 1))
    assert done.method == 'totp' and auth.resolve_session(done.token).two_factor_enabled

    # A recovery code works once.
    second = auth.login(user.email, PW)
    assert auth.complete_two_factor_login(second.challenge, codes[0].lower()).method == 'recovery'
    third = auth.login(user.email, PW)
    with pytest.raises(UnauthorizedError):
        auth.complete_two_factor_login(third.challenge, codes[0])


def test_challenge_allows_five_tries(user):
    setup = auth.begin_setup(user)
    auth.confirm_setup(user, _code(setup['secret']))
    ch = auth.login(user.email, PW).challenge
    for _ in range(auth.CHALLENGE_MAX_ATTEMPTS):
        with pytest.raises(UnauthorizedError, match='not correct'):
            auth.complete_two_factor_login(ch, '000000')
    with pytest.raises(UnauthorizedError, match='expired'):
        auth.complete_two_factor_login(ch, _code(setup['secret'], 1))


def test_required_two_factor_cannot_be_turned_off(user, company):
    execute("INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'security.policy', '{\"require2faRoles\": [\"employee\"]}')", [company.id])
    token = auth.login(user.email, PW).token
    assert auth.resolve_session(token).must_set_up_two_factor
    setup = auth.begin_setup(user)
    auth.confirm_setup(user, _code(setup['secret']))
    with pytest.raises(ForbiddenError):
        auth.disable_two_factor(user, PW, _code(setup['secret'], 1))

