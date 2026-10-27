import pytest

from tests.core.fixtures import *  # noqa: F401,F403
from tests.web.fixtures import *  # noqa: F401,F403


@pytest.fixture(autouse=True)
def _kiosk_rate_limits():
    from hajiri.kiosk.tokens import reset_rate_limits
    reset_rate_limits()
    yield
    reset_rate_limits()
