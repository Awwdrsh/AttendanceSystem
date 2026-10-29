"""Pages and HTMX fragments of the relations slice (pages/relations, components/relations), through django.test.Client."""
from datetime import timedelta

from hajiri.core.clock import npt_today
from tests.support.sql import one, rows
from hajiri.relations import services as S
from tests.web.factories import set_features_off

from .conftest import client_for


def test_home_card_is_empty_when_there_is_nothing(world):
    # The Home card reads these two lists and shows nothing when both are empty.
    c = client_for(world.sita)
    kye, cases = c.get('/api/v1/relations/kye/mine'), c.get('/api/v1/relations/disciplinary/mine')
    assert kye.status_code == cases.status_code == 200
    assert kye.json()['campaigns'] == [] and cases.json()['cases'] == [], (kye.content, cases.content)
