import base64

import pytest

from hajiri.core import crypto


@pytest.fixture(autouse=True)
def _secrets(settings):
    """Fixed test secrets; the field cipher is cached, so it is reset around each test."""
    settings.DATA_ENCRYPTION_KEY = base64.b64encode(bytes(range(32))).decode()
    settings.PASSWORD_PEPPER = 'test-pepper-0123456789'
    crypto.field_cipher.cache_clear()
    yield
    crypto.field_cipher.cache_clear()


@pytest.fixture
def company(db):
    from hajiri.db.models import Company
    return Company.objects.create(code='ACME', name_en='Acme Pvt. Ltd.')
