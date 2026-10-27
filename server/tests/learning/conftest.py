import pytest

from tests.core.fixtures import *  # noqa: F401,F403

from tests.performance.world import build


@pytest.fixture
def w(db):
    return build()
