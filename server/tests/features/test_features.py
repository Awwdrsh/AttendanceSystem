"""Port of the feature-switch half of apps/api/test/features-reports.test.ts. The per-module probes (time, expenses,
...) check each module's own gate, so here every switch is checked through assert_feature and require_feature; the
modules' own tests cover their pages."""
import pytest
from django.http import HttpResponse
from django.test import RequestFactory

from hajiri.core import auth
from tests.support.sql import one
from hajiri.core.errors import FeatureOffError, ForbiddenError, ValidationError
from hajiri.features.service import (
    FEATURE_KEYS, assert_feature, disabled_features, feature_on, get_features, invalidate_features, require_feature,
    set_features,
)
from tests.core.fixtures import PW


@pytest.fixture
def people(demo, login_as):
    hr = login_as(['hr']).user
    sita = login_as(['employee'], demo['sita']).user
    ram = login_as(['employee', 'manager'], demo['ram']).user
    c = str(one("INSERT INTO company (code, name_en) VALUES ('OTHERF', 'Other Co') RETURNING id")['id'])
    auth.create_user(company_id=c, email='hr@other.features', name='Other HR', password=PW, roles=['hr'])
    foreign = auth.resolve_session(auth.login('hr@other.features', PW).token)
    return {'hr': hr, 'sita': sita, 'ram': ram, 'foreign': foreign}


def test_everything_is_on_by_default_and_every_user_can_read_the_switches(people):
    r = get_features(people['sita'])
    assert sorted(r['enabled']) == sorted(FEATURE_KEYS)
    assert all(r['enabled'].values())
    f = r['features'][0]
    assert isinstance(f['key'], str) and isinstance(f['label'], str) and f['enabled'] is True


def test_only_hr_or_admin_can_change_them_and_unknown_keys_are_refused(people):
    with pytest.raises(ForbiddenError):
        set_features(people['sita'], {'enabled': {'time': False}})
    with pytest.raises(ForbiddenError):
        set_features(people['ram'], {'enabled': {'time': False}})
    with pytest.raises(ValidationError):
        set_features(people['hr'], {'enabled': {'payroll': False}})
    with pytest.raises(ValidationError):
        set_features(people['hr'], {'enabled': {'time': 'no'}})
    with pytest.raises(ValidationError):
        set_features(people['hr'], {'enabled': {}, 'extra': 1})


def _view(request):
    return HttpResponse('ok')


@pytest.mark.parametrize('key', FEATURE_KEYS)
def test_switching_a_feature_off_answers_feature_off_and_on_again_restores_it(people, key):
    gated = require_feature(key)(_view)
    req = RequestFactory().get('/x')
    req.hajiri_user = people['hr']
    assert gated(req).status_code == 200
    r = set_features(people['hr'], {'enabled': {key: False}})
    assert r['enabled'][key] is False
    with pytest.raises(FeatureOffError) as e:
        gated(req)
    assert e.value.status == 404 and e.value.message == 'feature off' and e.value.details == {'feature': key}
    # Another company is not affected.
    req.hajiri_user = people['foreign']
    assert gated(req).status_code == 200
    req.hajiri_user = people['hr']
    set_features(people['hr'], {'enabled': {key: True}})
    assert gated(req).status_code == 200


def test_only_the_keys_sent_change(people):
    set_features(people['hr'], {'enabled': {'tasks': False, 'kudos': False}})
    set_features(people['hr'], {'enabled': {'kudos': True}})
    assert disabled_features(people['hr'].company_id) == {'tasks'}
    stored = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'features'", [people['hr'].company_id])
    assert stored['value'] == {'off': ['tasks']}


def test_a_saved_change_is_audited_and_takes_effect_at_once(people):
    set_features(people['hr'], {'enabled': {'helpdesk': False}})
    a = one("SELECT after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'features' ORDER BY id DESC LIMIT 1")
    assert 'helpdesk' in a['after']['off']
    invalidate_features()
    with pytest.raises(FeatureOffError):
        assert_feature(people['sita'].company_id, 'helpdesk')
    assert feature_on(None, 'helpdesk')  # no company, no gate
    set_features(people['hr'], {'enabled': {'helpdesk': True}})
    assert feature_on(people['sita'].company_id, 'helpdesk')


def test_unknown_stored_keys_are_ignored(people):
    from tests.support.sql import execute
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["time", "bogus", 3]}')""",
            [people['hr'].company_id])
    assert disabled_features(people['hr'].company_id) == {'time'}
