"""Group services: correct sums for two companies, Gamma (outside the group) never counted, access refused to people
who may not use group views, pay hidden where a company hides it, and a query count that does not grow with the
number of companies."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.settings_store import put_setting
from hajiri.db.models import AuditLog, CompanySetting, GroupMember
from hajiri.group import services
from tests.web.factories import make_company, make_employee


def _by_code(d):
    return {c['code']: c for c in d['companies']}


# ---------------------------------------------------------------- access

def test_scope_is_the_group_companies_the_person_may_see(world):
    _, ghr = world.session(world.ghr)
    s = services.group_scope(ghr)
    assert [c.code for c in s.companies] == ['ALPHA', 'BETA']
    assert s.group_id == str(world.alpha.group_id)
    _, admin_a = world.session(world.admin_a, world.beta)          # working in Beta: same group, same scope
    assert [c.code for c in services.group_scope(admin_a).companies] == ['ALPHA', 'BETA']


@pytest.mark.parametrize('who', ['solo', 'admin_b', 'admin_c', 'dual'])
def test_people_outside_group_views_are_refused(world, who):
    _, user = world.session(getattr(world, who))
    assert not services.can_view(user)
    with pytest.raises(ForbiddenError):
        services.group_scope(user)
    with pytest.raises(ForbiddenError):
        services.dashboard(user)
    with pytest.raises(ForbiddenError):
        services.report(user, 'headcount', services.period_of('month'))


def test_dual_with_hr_in_both_companies_may_use_group_views(world):
    from hajiri.db.models import CompanyMembership
    CompanyMembership.objects.filter(user_id=world.dual.id).update(roles=['hr'])
    _, dual = world.session(world.dual)
    assert services.can_view(dual)
    assert [c.code for c in services.group_scope(dual).companies] == ['ALPHA', 'BETA']


def test_a_company_outside_the_scope_is_not_found(world):
    _, ghr = world.session(world.ghr)
    with pytest.raises(NotFoundError):
        services.report(ghr, 'headcount', services.period_of('month'), str(world.gamma.id))


# ---------------------------------------------------------------- dashboard

def test_dashboard_sums_per_company_and_for_the_group(world, figures):
    _, ghr = world.session(world.ghr)
    d = services.dashboard(ghr, figures.today)
    rows = _by_code(d)
    assert set(rows) == {'ALPHA', 'BETA'}                                 # Gamma never appears
    a, b = rows['ALPHA'], rows['BETA']
    assert (a['headcount'], a['joiners'], a['leavers']) == (6, 1, 1)
    assert (b['headcount'], b['joiners'], b['leavers']) == (4, 0, 0)
    assert (a['present'], a['late'], a['absent'], a['onLeave']) == (2, 1, 1, 0)
    assert (b['present'], b['late'], b['absent'], b['onLeave']) == (1, 0, 0, 1)
    assert (a['pendingApprovals'], b['pendingApprovals']) == (1, 2)
    assert a['exceptions'] == 1 and b['exceptions'] == 0                  # the absence today is open
    assert a['otMinutes'] == 90 and b['otMinutes'] == 0
    assert a['payroll']['gross'] == '80000.00' and a['payroll']['net'] == '70200.00'
    assert a['payroll']['cost'] == '96000.00' and a['payroll']['payslips'] == 2
    assert (a['payroll']['bsYear'], a['payroll']['bsMonth']) == (figures.prev.year, figures.prev.month)
    assert b['payroll']['cost'] == '48000.00'
    t = d['totals']
    assert (t['headcount'], t['present'], t['pendingApprovals'], t['onLeave']) == (10, 3, 3, 1)
    assert t['payrollCost'] == '144000.00' and t['payHidden'] == 0
    assert d['groupName'] == 'Alpha Traders group'


def test_needs_attention_names_company_and_page(world, figures):
    _, ghr = world.session(world.ghr)
    items = services.dashboard(ghr, figures.today)['needsAttention']
    kinds = {(i['companyCode'], i['kind']): i for i in items}
    assert ('BETA', 'devices_offline') in kinds and kinds[('BETA', 'devices_offline')]['next'] == '/settings/devices'
    assert ('BETA', 'documents_expiring') in kinds
    assert kinds[('ALPHA', 'probation_due')]['count'] == 1
    assert ('ALPHA', 'payroll_not_run') not in kinds and ('BETA', 'payroll_not_run') not in kinds
    assert not any(i['companyCode'] == 'GAMMA' for i in items)
    assert kinds[('BETA', 'approvals')]['label'] == '2 approvals waiting'
    assert kinds[('BETA', 'devices_offline')]['active'] is True             # ghr's home is Beta


def test_payroll_not_run_is_flagged(world, figures):
    # A company added to the group later, with people and no pay run yet; group HR reaches it at once.
    from hajiri.core import companies
    _, admin_a = world.session(world.admin_a)
    delta = companies.create_group_company(admin_a, {'code': 'DELTA', 'name_en': 'Delta Ltd'})
    from hajiri.db.models import Company
    make_employee(Company.objects.get(pk=delta['id']), name='Dev Delta')
    _, ghr = world.session(world.ghr)
    d = services.dashboard(ghr, figures.today)
    flagged = [i for i in d['needsAttention'] if i['kind'] == 'payroll_not_run']
    assert [i['companyCode'] for i in flagged] == ['DELTA']
    assert flagged[0]['label'] == f'Payroll for {figures.prev.label} not run' and flagged[0]['next'] == '/payroll'
    assert _by_code(d)['DELTA']['payroll'] is None and _by_code(d)['DELTA']['headcount'] == 1


def test_attrition_trend_for_the_group(world, figures):
    _, ghr = world.session(world.ghr)
    trend = services.dashboard(ghr, figures.today)['attrition']
    assert len(trend) == 12 and trend[-1]['key'] == figures.this.key
    last = trend[-1]
    # Start of month: 6 in Alpha (4 + leaver + probation; the joiner starts on day 1) and 4 in Beta; end: 6 + 4.
    assert (last['start'], last['end'], last['exits']) == (10, 10, 1)
    assert last['attritionPct'] == 10.0
    assert last['byCompany'][str(world.beta.id)] == 0.0


def test_executive_pay_hidden_by_company_policy(world, figures):
    _, gexec = world.session(world.gexec)
    assert _by_code(services.dashboard(gexec, figures.today))['ALPHA']['payroll']['cost'] == '96000.00'
    put_setting(world.alpha.id, services.POLICY_KEY, {services.HIDE_PAY: True})
    d = services.dashboard(gexec, figures.today)
    rows = _by_code(d)
    assert rows['ALPHA']['payroll'] is None and not rows['ALPHA']['paySeen']
    assert rows['BETA']['payroll']['cost'] == '48000.00'
    assert d['totals']['payrollCost'] == '48000.00' and d['totals']['payHidden'] == 1
    # HR of the group still sees it.
    _, ghr = world.session(world.ghr)
    assert _by_code(services.dashboard(ghr, figures.today))['ALPHA']['payroll']['cost'] == '96000.00'
    # And the payroll report leaves Alpha out for the executive.
    t = services.report(gexec, 'payroll', services.period_of('month', bs_year=figures.prev.year,
                                                            bs_month=figures.prev.month))
    assert {r['code'] for r in t.rows} == {'BETA'} and t.extra['hidden'] == ['Alpha Traders']


def test_set_pay_policy_is_the_active_company_admins_and_audited(world):
    _, admin_a = world.session(world.admin_a)
    assert services.set_pay_policy(admin_a, True) == {services.HIDE_PAY: True}
    assert CompanySetting.objects.get(company=world.alpha, key=services.POLICY_KEY).value == {services.HIDE_PAY: True}
    assert AuditLog.objects.filter(table_name='company_setting', row_id=str(world.alpha.id), action='update').exists()
    _, gexec = world.session(world.gexec)
    with pytest.raises(ForbiddenError):
        services.set_pay_policy(gexec, False)


def test_dashboard_query_count_does_not_grow_with_companies(world, figures):
    ids2 = [str(world.alpha.id), str(world.beta.id)]
    with CaptureQueriesContext(connection) as two:
        services.figures(ids2, figures.today)
    with CaptureQueriesContext(connection) as one:
        services.figures(ids2[:1], figures.today)
    third = make_company('DELTA', 'Delta Ltd')
    make_employee(third, name='Dev')
    with CaptureQueriesContext(connection) as three:
        services.figures([*ids2, str(third.id)], figures.today)
    assert len(one) == len(two) == len(three) <= 8


def test_dashboard_is_cached_until_refreshed(world, figures, settings):
    settings.GROUP_DASHBOARD_CACHE_SECONDS = 60
    cache.clear()
    _, ghr = world.session(world.ghr)
    first = services.dashboard(ghr, figures.today)
    make_employee(world.alpha, name='New Person')
    with CaptureQueriesContext(connection) as q:
        again = services.dashboard(ghr, figures.today)
    assert again['totals']['headcount'] == first['totals']['headcount']
    assert not any('employee' in x['sql'] and 'COUNT' in x['sql'] for x in q.captured_queries)
    fresh = services.dashboard(ghr, figures.today, fresh=True)
    assert fresh['totals']['headcount'] == first['totals']['headcount'] + 1
    cache.clear()


# ---------------------------------------------------------------- reports

def _rows(t):
    return {(r.get('month'), r['company']): r for r in t.rows}


def test_headcount_report(world, figures):
    _, ghr = world.session(world.ghr)
    t = services.report(ghr, 'headcount', services.period_of('month'))
    rows = {r['company']: r for r in t.rows}
    assert list(rows) == ['Alpha Traders', 'Beta Foods', 'Group total']
    assert rows['Alpha Traders'] == {**rows['Alpha Traders'], 'start': 6, 'joiners': 1, 'leavers': 1, 'end': 6, 'net': 0}
    assert rows['Group total']['end'] == 10 and rows['Group total']['start'] == 10
    assert t.extra['title'] == 'Headcount' and t.extra['period'] == figures.this.label


def test_attendance_report_by_month(world, figures):
    _, ghr = world.session(world.ghr)
    t = services.report(ghr, 'attendance', services.period_of('month'))
    r = _rows(t)
    a = r[(figures.this.label, 'Alpha Traders')]
    assert (a['scheduled'], a['present'], a['absent'], a['late']) == (3, 2, 1, 1)
    assert a['rate'] == 66.7 and a['late_hours'] == 0.2
    tot = r[(figures.this.label, 'Group total')]
    assert (tot['scheduled'], tot['present']) == (4, 3) and tot['rate'] == 75.0
    assert not any(row['company'] == 'Gamma Outside' for row in t.rows)


def test_leave_report(world, figures):
    _, ghr = world.session(world.ghr)
    t = services.report(ghr, 'leave', services.period_of('month'))
    assert [(r['company'], r['leave_code'], r['taken'], r['balance']) for r in t.rows] == [('Alpha Traders', 'GTEST', 2.0, 8.0)]


def test_overtime_report(world, figures):
    _, ghr = world.session(world.ghr)
    t = services.report(ghr, 'overtime', services.period_of('month'))
    a = _rows(t)[(figures.this.label, 'Alpha Traders')]
    assert (a['slips'], a['people'], a['detected_hours'], a['approved_hours']) == (1, 1, 2.0, 1.5)
    assert _rows(t)[(figures.this.label, 'Group total')]['approved_hours'] == 1.5


def test_payroll_report_per_company_per_month(world, figures):
    _, ghr = world.session(world.ghr)
    p = services.period_of('month', bs_year=figures.prev.year, bs_month=figures.prev.month)
    r = _rows(services.report(ghr, 'payroll', p))
    a = r[(figures.prev.label, 'Alpha Traders')]
    assert (a['payslips'], a['gross'], a['ssf_employee'], a['ssf_employer'], a['tds'], a['net'], a['cost']) == (
        2, '80000.00', '8800.00', '16000.00', '1000.00', '70200.00', '96000.00')
    tot = r[(figures.prev.label, 'Group total')]
    assert (tot['gross'], tot['net'], tot['tds']) == ('120000.00', '105300.00', '1500.00')
    one = services.report(ghr, 'payroll', p, str(world.beta.id))
    assert [x['company'] for x in one.rows] == ['Beta Foods']          # one company: no total row


def test_fiscal_year_period_and_attrition_report(world, figures):
    _, ghr = world.session(world.ghr)
    p = services.period_of('fy')
    assert p.months[0].month == 4 and p.months[-1].key == figures.this.key
    assert p.label.startswith('Fiscal year ')
    t = services.report(ghr, 'attrition', p)
    a = _rows(t)[(figures.this.label, 'Alpha Traders')]
    assert (a['start'], a['leavers'], a['end']) == (6, 1, 6) and a['rate'] == 16.67
    assert len([r for r in t.rows if r['company'] == 'Group total']) == len(p.months)


def test_directory_and_breakdowns(world, figures):
    _, ghr = world.session(world.ghr)
    qs = services.directory(ghr)
    names = set(qs.values_list('name_en', flat=True))
    assert 'Gita Admin' not in names and {'Asha Admin', 'Bina On Leave', 'Lalit Leaver'} <= names
    b = services.breakdowns(qs.filter(status='active'))
    assert {r['label']: r['value'] for r in b['company']} == {'Alpha Traders': 6, 'Beta Foods': 4}
    gender = {(r['company'], r['label']): r['value'] for r in b['gender']}
    assert gender[('Beta Foods', 'Female')] == 1 and gender[('Alpha Traders', 'Female')] == 1
    types = services.breakdown_totals(b['employment_type'])
    assert {r['label']: r['value'] for r in types} == {'Regular': 9, 'Part time': 1}
    with CaptureQueriesContext(connection) as q:
        services.breakdowns(qs)
    assert len(q) == len(services.DIMENSIONS)


def test_group_role_holder_working_in_a_company_outside_the_group_sees_nothing_there(world):
    # A group HR person who also has access to Gamma (a membership) and works there: no group views in Gamma.
    from hajiri.db.models import CompanyMembership
    gamma_emp = make_employee(world.gamma, name='Gopal at Gamma')
    CompanyMembership.objects.create(user_id=world.ghr.id, company=world.gamma, roles=['hr'], employee=gamma_emp)
    _, ghr = world.session(world.ghr, world.gamma)
    assert not services.can_view(ghr)
    with pytest.raises(ForbiddenError):
        services.group_scope(ghr)


def test_revoked_group_role_loses_group_views(world):
    GroupMember.objects.filter(user_id=world.gexec.id).delete()
    _, gexec = world.session(world.gexec)
    assert not services.can_view(gexec)


def test_months_helpers(world):
    t = services.current_month(services.npt_today())
    months = services.last_months(services.npt_today(), 3)
    assert months[-1] == t and len(months) == 3 and months[0].start < months[1].start
    assert months[1].end + timedelta(days=1) == months[2].start
