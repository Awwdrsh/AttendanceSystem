"""Insights and reports in a group of companies: every figure, filter option and saved report belongs to the ACTIVE
company; a branch, department or saved report of another company is unknown or not found, also for someone who could
switch to that company. Group HR and group executives see Insights in every company of the group."""
import pytest

from hajiri.db.models import Branch, OrgUnit, SavedReport
from hajiri.insights import services
from tests.multicompany.conftest import world  # noqa: F401
from tests.web.factories import bearer, make_employee


@pytest.fixture
def places(world):
    ab = Branch.objects.create(company=world.alpha, code='AHQ', name_en='Alpha Head Office')
    bb = Branch.objects.create(company=world.beta, code='BHQ', name_en='Beta Factory')
    au = OrgUnit.objects.create(company=world.alpha, kind='department', code='ASALES', name_en='Alpha Sales')
    bu = OrgUnit.objects.create(company=world.beta, kind='department', code='BPLANT', name_en='Beta Plant')
    for i in range(3):
        make_employee(world.beta, name=f'Beta worker {i}', branch=bb, org_unit=bu)
    make_employee(world.alpha, name='Alpha seller', branch=ab, org_unit=au)
    return {'ab': ab, 'bb': bb, 'au': au, 'bu': bu}


def _headcount(user, **q):
    return services.insights(user, q)['headcount'][-1]['headcount']


def test_figures_follow_the_active_company(world, places):
    from hajiri.db.models import Employee
    _, in_alpha = world.session(world.admin_a)
    _, in_beta = world.session(world.admin_a, world.beta)
    assert _headcount(in_alpha) == Employee.objects.for_company(world.alpha.id).count()
    assert _headcount(in_beta) == Employee.objects.for_company(world.beta.id).count()
    assert _headcount(in_beta, branchId=str(places['bb'].id)) == 3
    assert [d['label'] for d in services.insights(in_beta, {'orgUnitId': str(places['bu'].id)})['departments']] == ['Beta Plant']


def test_filters_of_another_company_are_unknown(world, places):
    _, in_alpha = world.session(world.admin_a)
    with pytest.raises(Exception, match='unknown branch'):
        services.insights(in_alpha, {'branchId': str(places['bb'].id)})
    with pytest.raises(Exception, match='unknown department'):
        services.insights(in_alpha, {'orgUnitId': str(places['bu'].id)})
    f = services.filter_options(in_alpha)
    assert [b['name'] for b in f['branches']] == ['Alpha Head Office']
    assert [o['name'] for o in f['orgUnits']] == ['Alpha Sales']


def test_group_roles_open_insights_in_every_company(world, places):
    for person in (world.ghr, world.gexec):
        for co in (world.alpha, world.beta):
            _, user = world.session(person, co)
            assert services.insights(user, {})['months']
    # A manager membership is not an insights role.
    _, dual_in_beta = world.session(world.dual, world.beta)
    with pytest.raises(Exception, match='requires role hr or executive'):
        services.insights(dual_in_beta, {})


def test_json_api_is_scoped_to_the_active_company(world, places):
    token_a, _ = world.session(world.admin_a)
    c = world.signed_in(world.admin_a)
    r = c.get('/api/v1/insights', {'branchId': str(places['bb'].id)}, **bearer(token_a))
    assert r.status_code == 400 and r.json() == {'error': 'unknown branch'}
    r = c.get('/api/v1/insights/filters', **bearer(token_a))
    assert r.status_code == 200 and [b['name'] for b in r.json()['branches']] == ['Alpha Head Office']
    token_b, _ = world.session(world.admin_b)
    r = c.get('/api/v1/insights', {'branchId': str(places['bb'].id)}, **bearer(token_b))
    assert r.status_code == 200 and r.json()['headcount'][-1]['headcount'] == 3
    r = c.get('/api/v1/insights/export/departments', {'orgUnitId': str(places['bu'].id)}, **bearer(token_b))
    assert r.status_code == 200 and r['Content-Type'].startswith('text/csv')
    assert 'Beta Plant,3' in r.content.decode()
    r = c.get('/api/v1/insights/export/departments', {'orgUnitId': str(places['bu'].id)}, **bearer(token_a))
    assert r.status_code == 400 and r.json() == {'error': 'unknown department'}


def test_json_api_needs_an_insights_role(world):
    token, _ = world.session(world.dual, world.beta)
    from django.test import Client
    r = Client().get('/api/v1/insights', **bearer(token))
    assert r.status_code == 403 and r.json()['error'] == 'requires role hr or executive'
    r = Client().get('/api/v1/insights')
    assert r.status_code == 401


def _queries(fn) -> int:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as ctx:
        fn()
    return len(ctx.captured_queries)


def test_insights_page_and_api_do_not_query_per_employee(world, places):
    """The filters, the figures and the CSV export run a fixed number of queries however many people the company has."""
    c = world.signed_in(world.admin_b)
    token, _ = world.session(world.admin_b)
    calls = [lambda: c.get('/api/v1/insights/filters'), lambda: c.get('/api/v1/insights', **bearer(token)),
             lambda: c.get('/api/v1/insights/export/departments', **bearer(token))]
    for call in calls:
        call()  # warm caches (settings, features)
    before = [_queries(call) for call in calls]
    for i in range(6):
        make_employee(world.beta, name=f'More beta {i}', branch=places['bb'], org_unit=places['bu'])
    assert [_queries(call) for call in calls] == before
    assert all(n < 40 for n in before[:3]), before


def test_save_form_reads_the_dialog_like_the_page_did():
    from django.http import QueryDict
    from hajiri.core.errors import BadRequestError
    from hajiri.insights.forms import SaveReportForm
    f = SaveReportForm(QueryDict('name=+A+&shared=0&as_new=on'))
    assert f.body() == {'name': 'A', 'description': None, 'shared': False, 'definition': {}}
    assert f.cleaned_data['as_new'] is False
    f = SaveReportForm(QueryDict('name=A&shared=on&as_new=1&definition=%5B%5D'))
    assert f.body()['shared'] is True and f.body()['definition'] == [] and f.cleaned_data['as_new'] is True
    with pytest.raises(BadRequestError, match='definition is not valid'):
        SaveReportForm(QueryDict('name=A&definition=%7Bnope')).body()


def test_page_refuses_a_foreign_branch_and_shows_only_this_companys_filters(world, places):
    alpha_admin = world.signed_in(world.admin_a)
    r = alpha_admin.get('/api/v1/insights', {'branchId': str(places['bb'].id)})
    assert r.status_code == 400 and b'unknown branch' in r.content
    r = alpha_admin.get('/api/v1/insights/filters')
    assert r.status_code == 200
    assert b'Alpha Head Office' in r.content and b'Beta Factory' not in r.content
    beta_admin = world.signed_in(world.admin_b)
    r = beta_admin.get('/api/v1/insights', {'branchId': str(places['bb'].id)})
    assert r.status_code == 200
    # The group admin, once switched to Beta, uses Beta's filters.
    r = world.signed_in(world.admin_a, world.beta).get('/api/v1/insights', {'orgUnitId': str(places['bu'].id)})
    assert r.status_code == 200


def test_report_view_lists_only_this_companys_branches(world, places):
    # The report filters list the active company's branches (settings.branches(), as the Reports page loads them).
    c = world.signed_in(world.admin_a)
    r = c.get('/api/v1/settings/branches')
    assert r.status_code == 200
    assert b'Alpha Head Office' in r.content and b'Beta Factory' not in r.content


def test_saved_reports_of_another_company_are_not_found(world, places):
    _, in_beta = world.session(world.admin_b)
    from hajiri.reports import service as reports
    ds = reports.adhoc_datasets()['datasets'][0]
    saved = reports.create_saved(in_beta, {'name': 'Beta list', 'shared': True,
                                           'definition': {'dataset': ds['key'], 'columns': ds['defaultColumns'][:1]}})
    sid = str(saved['id'])
    for c in (world.signed_in(world.admin_a), world.signed_in(world.admin_c)):
        assert c.get(f'/api/v1/reports/adhoc/saved/{sid}/run').status_code == 404
        assert c.delete(f'/api/v1/reports/adhoc/saved/{sid}').status_code == 404
        assert all(str(s['id']) != sid for s in c.get('/api/v1/reports/adhoc/saved').json()['reports'])
    assert SavedReport.objects.filter(pk=sid).exists()
    assert world.signed_in(world.admin_b).get(f'/api/v1/reports/adhoc/saved/{sid}/run').status_code == 200


def test_saved_report_ids_of_another_company_cannot_be_overwritten(world, places):
    """Saving over another company's report is not found; saving a new one files it in the active company."""
    from hajiri.reports import service as reports
    _, in_beta = world.session(world.admin_b)
    ds = reports.adhoc_datasets()['datasets'][0]
    theirs = reports.create_saved(in_beta, {'name': 'Beta only', 'shared': True,
                                            'definition': {'dataset': ds['key'], 'columns': ds['defaultColumns'][:1]}})
    import json
    c = world.signed_in(world.admin_a)
    body = {'name': 'Alpha copy', 'shared': True, 'definition': {'dataset': ds['key'], 'columns': ds['defaultColumns'][:1]}}
    assert c.put(f"/api/v1/reports/adhoc/saved/{theirs['id']}", json.dumps(body), content_type='application/json').status_code == 404
    r = c.post('/api/v1/reports/adhoc/saved', json.dumps(body), content_type='application/json')
    assert r.status_code == 201, r.content
    assert SavedReport.objects.get(pk=theirs['id']).name == 'Beta only'
    mine = SavedReport.objects.get(name='Alpha copy')
    assert str(mine.company_id) == str(world.alpha.id) and mine.shared

