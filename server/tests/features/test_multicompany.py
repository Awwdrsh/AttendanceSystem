"""Feature switches belong to one company: switching a module off in the active company leaves the others alone."""
import pytest

from hajiri.features.service import disabled_features, get_features, set_features
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def test_switches_change_only_the_active_company(world):
    _, in_alpha = world.session(world.admin_a)
    set_features(in_alpha, {'enabled': {'tasks': False}})
    assert disabled_features(str(world.alpha.id)) == {'tasks'}
    assert disabled_features(str(world.beta.id)) == frozenset()
    _, in_beta = world.session(world.admin_a, world.beta)
    assert get_features(in_beta)['enabled']['tasks'] is True
    set_features(in_beta, {'enabled': {'kudos': False}})
    assert disabled_features(str(world.beta.id)) == {'kudos'}
    assert disabled_features(str(world.alpha.id)) == {'tasks'}
