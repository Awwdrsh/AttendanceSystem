"""Settings, branches and org units in a group of companies: everything reads and writes the ACTIVE company, and a
branch or unit of another company is not found, even for the group admin until they switch."""
import pytest

from hajiri.company_settings import service
from hajiri.core.errors import NotFoundError
from hajiri.core.settings_store import get_setting
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def test_branches_and_units_of_another_company_are_not_found(world):
    _, beta_admin = world.session(world.admin_b)
    br = service.create_branch(beta_admin, {'code': 'BKT', 'nameEn': 'Bhaktapur'})
    unit = service.create_org_unit(beta_admin, {'kind': 'department', 'code': 'OPS', 'nameEn': 'Operations'})
    for person in (world.admin_a, world.admin_c, world.solo):        # group admin in Alpha, outsider, Alpha HR
        _, user = world.session(person)
        with pytest.raises(NotFoundError, match='branch not found'):
            service.update_branch(user, str(br['id']), {'nameEn': 'Mine now'})
        with pytest.raises(NotFoundError, match='org unit not found'):
            service.update_org_unit(user, str(unit['id']), {'nameEn': 'Mine now'})
        with pytest.raises(NotFoundError, match='branch not found'):
            service.create_org_unit(user, {'kind': 'team', 'code': 'T1', 'nameEn': 'T', 'branchId': str(br['id'])})
        with pytest.raises(NotFoundError, match='parent unit not found'):
            service.create_org_unit(user, {'kind': 'team', 'code': 'T2', 'nameEn': 'T', 'parentId': str(unit['id'])})
        assert str(br['id']) not in {str(b['id']) for b in service.list_branches(user)}
        assert str(unit['id']) not in {str(u['id']) for u in service.list_org_units(user)}
    _, switched = world.session(world.admin_a, world.beta)
    assert service.update_branch(switched, str(br['id']), {'nameEn': 'Bhaktapur Durbar'})['name_en'] == 'Bhaktapur Durbar'


def test_settings_and_the_header_follow_the_active_company(world):
    _, in_alpha = world.session(world.admin_a)
    _, in_beta = world.session(world.admin_a, world.beta)
    service.put_setting(in_beta, 'leave.policy', {'maxContinuousDays': 12})
    assert get_setting(str(world.beta.id), 'leave.policy')['maxContinuousDays'] == 12
    assert (get_setting(str(world.alpha.id), 'leave.policy') or {}).get('maxContinuousDays') != 12
    beta_value = next(s for s in service.list_settings(in_beta)['settings'] if s['key'] == 'leave.policy')['value']
    alpha_value = next(s for s in service.list_settings(in_alpha)['settings'] if s['key'] == 'leave.policy')['value']
    assert beta_value['maxContinuousDays'] == 12 and alpha_value['maxContinuousDays'] == 30
    assert service.company_header(in_beta)['company']['nameEn'] == 'Beta Foods'
    assert service.company_header(in_alpha)['company']['nameEn'] == 'Alpha Traders'
    profile = next(s for s in service.list_settings(in_beta)['settings'] if s['key'] == 'company.profile')
    assert profile['value']['nameEn'] == 'Beta Foods'
