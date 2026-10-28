"""The multi-company world of tests/multicompany (Alpha and Beta in one group, Gamma outside) for group administration."""
import io

import pytest

from tests.multicompany.conftest import World, world  # noqa: F401


def actor(world, person, company=None):
    """The AuthUser of a person working in a company (their home by default)."""
    return world.session(person, company)[1]


def png_bytes(size=(600, 300), color=(200, 30, 30, 255)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGBA', size, color).save(buf, format='PNG')
    return buf.getvalue()


@pytest.fixture
def admin(world):
    """The group admin (Asha), working in Alpha."""
    return actor(world, world.admin_a)
