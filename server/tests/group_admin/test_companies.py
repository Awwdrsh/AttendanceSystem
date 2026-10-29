"""Companies of the group: list, add (seeded), edit the profile, attach a standalone company."""
import pytest

from hajiri.core import settings_store
from hajiri.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.db.models import AuditLog, Company, CompanyGroup, CompanyMembership, LeaveType, SalaryComponent, WorkflowDefinition
from hajiri.group_admin import services

from .conftest import actor


def test_lists_only_the_companies_of_the_group(world, admin):
    rows = services.list_companies(admin)
    assert [r['code'] for r in rows] == ['ALPHA', 'BETA']
    assert next(r for r in rows if r['code'] == 'ALPHA')['active'] is True
    assert [r['headcount'] for r in rows] == [4, 3]


@pytest.mark.parametrize('who', ['admin_b', 'ghr', 'gexec', 'solo', 'admin_c'])
def test_only_group_admins_administer_the_group(world, who):
    user = actor(world, getattr(world, who))
    with pytest.raises(ForbiddenError, match='requires role group_admin'):
        services.list_companies(user)
    if who == 'admin_c':
        return              # the admin of a company outside any group may start one (next test)
    with pytest.raises(ForbiddenError):
        services.create_company(user, {'code': 'NEW', 'nameEn': 'New Co'})


def test_the_admin_of_a_standalone_company_starts_a_group_of_their_own(world):
    """As on the Group page: the group is created around Gamma, Gita becomes its group admin, and nothing of
    Alpha's group is touched."""
    user = actor(world, world.admin_c)
    c = services.create_company(user, {'code': 'GAMMA2', 'nameEn': 'Gamma Two'})
    world.gamma.refresh_from_db()
    assert world.gamma.group_id and str(Company.objects.get(pk=c['id']).group_id) == str(world.gamma.group_id)
    assert world.gamma.group_id != world.alpha.group_id
    again = actor(world, world.admin_c)
    assert [r['code'] for r in services.list_companies(again)] == ['GAMMA', 'GAMMA2']
    assert 'GAMMA2' not in [r['code'] for r in services.list_companies(actor(world, world.admin_a))]
    # A plain HR user of a standalone company still cannot.
    from tests.web.factories import make_user
    hr = make_user(world.gamma, roles=['hr'], email='hr@gamma.test')
    with pytest.raises(ForbiddenError):
        services.create_company(actor(world, hr), {'code': 'GAMMA3', 'nameEn': 'Gamma Three'})


def test_add_a_company_seeds_the_defaults_and_stores_the_profile(world, admin):
    c = services.create_company(admin, {'code': 'delta', 'nameEn': 'Delta Hydro', 'nameNe': 'डेल्टा', 'pan': '601234567',
                                        'address': 'Butwal, Rupandehi'})
    company = Company.objects.get(pk=c['id'])
    assert company.code == 'DELTA' and str(company.group_id) == str(world.alpha.group_id)
    assert LeaveType.objects.for_company(company.id).filter(code='HOME').exists()
    assert WorkflowDefinition.objects.for_company(company.id).filter(object_type='leave_request').exists()
    assert SalaryComponent.objects.for_company(company.id).exists()
    profile = settings_store.get_setting(company.id, 'company.profile')
    assert profile == {'nameEn': 'Delta Hydro', 'nameNe': 'डेल्टा', 'pan': '601234567', 'address': 'Butwal, Rupandehi'}
    assert AuditLog.objects.filter(action='create', table_name='company', row_id=c['id']).exists()
    assert AuditLog.objects.filter(action='update', table_name='company_setting', row_id=f"{c['id']}:company.profile").exists()
    # It is a company of the group straight away.
    assert 'DELTA' in [r['code'] for r in services.list_companies(actor(world, world.admin_a))]


def test_add_a_company_validates_and_refuses_a_taken_code(world, admin):
    with pytest.raises(ValidationError):
        services.create_company(admin, {'code': 'X', 'nameEn': 'Too short'})
    with pytest.raises(ValidationError):
        services.create_company(admin, {'code': 'OKAY', 'nameEn': 'Bad PAN', 'pan': '12'})
    with pytest.raises(ConflictError):
        services.create_company(admin, {'code': 'gamma', 'nameEn': 'Taken'})


def test_edit_a_company_of_the_group(world, admin):
    services.update_company(admin, world.beta.id, {'nameEn': 'Beta Foods Ltd', 'pan': '300000001', 'address': 'Pokhara'})
    world.beta.refresh_from_db()
    assert world.beta.name_en == 'Beta Foods Ltd'
    p = settings_store.get_setting(world.beta.id, 'company.profile')
    assert p['nameEn'] == 'Beta Foods Ltd' and p['pan'] == '300000001' and p['address'] == 'Pokhara'
    services.update_company(admin, world.beta.id, {'pan': None})
    assert 'pan' not in settings_store.get_setting(world.beta.id, 'company.profile')
    assert AuditLog.objects.filter(action='update', table_name='company', row_id=str(world.beta.id)).exists()


def test_a_company_outside_the_group_is_not_found(world, admin):
    for fn in (lambda: services.update_company(admin, world.gamma.id, {'nameEn': 'Hijacked'}),
               lambda: services.get_company(admin, world.gamma.id),
               lambda: services.update_company(admin, 'not-a-uuid', {'nameEn': 'x'})):
        with pytest.raises(NotFoundError, match='company not found'):
            fn()
    world.gamma.refresh_from_db()
    assert world.gamma.name_en == 'Gamma Outside'


def test_attach_needs_admin_of_that_company(world, admin):
    with pytest.raises(NotFoundError):
        services.attach_company(admin, {'companyId': str(world.gamma.id)})
    assert services.attachable_companies(admin) == []
    CompanyMembership.objects.create(user_id=world.admin_a.id, company=world.gamma, roles=['admin'])
    admin = actor(world, world.admin_a)
    assert [c['code'] for c in services.attachable_companies(admin)] == ['GAMMA']
    services.attach_company(admin, {'companyId': str(world.gamma.id)})
    world.gamma.refresh_from_db()
    assert world.gamma.group_id == world.alpha.group_id
    assert AuditLog.objects.filter(action='update', table_name='company', row_id=str(world.gamma.id),
                                   after__groupId=str(world.alpha.group_id)).exists()
    with pytest.raises(ConflictError, match='already in the group'):
        services.attach_company(actor(world, world.admin_a), {'companyId': str(world.gamma.id)})


def test_a_company_of_another_group_cannot_be_attached(world, admin):
    other = CompanyGroup.objects.create(code='OTHER-GRP', name_en='Other group')
    Company.objects.filter(pk=world.gamma.pk).update(group=other)
    CompanyMembership.objects.create(user_id=world.admin_a.id, company=world.gamma, roles=['admin'])
    with pytest.raises(ConflictError, match='another group'):
        services.attach_company(actor(world, world.admin_a), {'companyId': str(world.gamma.id)})


# ---------------------------------------------------------------- pages

def test_companies_api(world):
    c = world.signed_in(world.admin_a)
    assert [x['code'] for x in c.get('/api/v1/group/companies').json()['items']] == ['ALPHA', 'BETA']
    r = c.post('/api/v1/group/companies', {'code': 'THETA', 'nameEn': 'Theta'}, content_type='application/json')
    assert r.status_code == 201 and r.json()['code'] == 'THETA'
    r = c.patch(f'/api/v1/group/companies/{world.gamma.id}', {'nameEn': 'x'}, content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'company not found'}
    r = world.signed_in(world.admin_b).get('/api/v1/group/companies')
    assert r.status_code == 403 and r.json()['error'] == 'requires role group_admin'
