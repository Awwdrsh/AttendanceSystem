"""The multi-company world of tests/multicompany (Alpha and Beta in one group, Gamma outside) for group administration."""
import io

import pytest

from tests.multicompany.conftest import World, world  # noqa: F401


def actor(world, person, company=None):
    """The AuthUser of a person working in a company (their home by default)."""
    return world.session(person, company)[1]


def client_in(world, person, company):
    """A signed-in test Client working in `company` (switched through the JSON switcher the React app uses)."""
    c = world.signed_in(person)
    r = c.post('/api/v1/me/companies/switch', {'companyId': str(company.id)}, content_type='application/json')
    assert r.status_code == 200, r.content
    return c


def png_bytes(size=(600, 300), color=(200, 30, 30, 255)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGBA', size, color).save(buf, format='PNG')
    return buf.getvalue()


@pytest.fixture
def admin(world):
    """The group admin (Asha), working in Alpha."""
    return actor(world, world.admin_a)
