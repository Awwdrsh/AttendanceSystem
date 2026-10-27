"""Shared fixtures for the platform tests: the demo data set (setup-db.ts freshDb) and signed-in users (loginAs)."""
import secrets
from dataclasses import dataclass

import pytest

from hajiri.core import auth

PW = 'correct-horse-battery-staple'


@dataclass
class Login:
    token: str
    user_id: str
    user: auth.AuthUser
    email: str


@pytest.fixture(autouse=True)
def platform_caches():
    from hajiri.features.service import invalidate_features
    from hajiri.integrations.auth_api import reset_throttle
    invalidate_features()
    reset_throttle()
    yield
    invalidate_features()


@pytest.fixture
def demo(db):
    """Company DEMO with a branch, the GEN shift, Sita and Ram, device DEMO-01, the BS calendar and the defaults."""
    from hajiri.company_settings.demo import DEMO, seed_demo
    seed_demo()
    return DEMO


@pytest.fixture
def login_as(demo):
    def make(roles, employee_id=None, email=None, company_id=None) -> Login:
        addr = email or f"{'-'.join(roles)}-{secrets.token_hex(4)}@test.local"
        u = auth.create_user(company_id=company_id or demo['company_id'], employee_id=employee_id, email=addr, name=addr,
                             password=PW, roles=list(roles))
        r = auth.login(addr, PW)
        return Login(r.token, u.id, auth.resolve_session(r.token), addr)
    return make


def bearer(token: str) -> dict:
    return {'HTTP_AUTHORIZATION': f'Bearer {token}'}
