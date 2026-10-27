"""Employee page details checked against EmployeeDetailPage.tsx after the port."""
import pytest
from django.test import Client

from tests.people.conftest import build_world
from tests.web.factories import set_features_off, sign_in


@pytest.fixture
def world(db):
    return build_world()


def test_lifecycle_tab_says_feature_off_when_switched_off(world):
    w = world
    c = Client()
    sign_in(c, w.hr)
    r = c.get(f'/employees/{w.sita_id}?tab=lifecycle')
    assert r.status_code == 200 and b"/_/lifecycle/employees/" in r.content
    set_features_off(w.company, 'lifecycle')
    c2 = Client()
    sign_in(c2, w.hr)
    r = c2.get(f'/employees/{w.sita_id}?tab=lifecycle')
    assert r.status_code == 200 and b'feature off' in r.content and b"/_/lifecycle/employees/" not in r.content
