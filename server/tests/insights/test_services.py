"""Port of the insights block of apps/api/test/engage.test.ts and the insights checks of roles-flows.test.ts, plus the
pure month and exit helpers of modules/insights/service.ts."""
import pytest

from engines.bs_calendar import BsDate, ad_to_bs, bs_month_bounds, bs_to_ad
from tests.support.sql import execute
from hajiri.core.errors import BadRequestError, ForbiddenError, ValidationError
from hajiri.insights import services
from tests.insights.helpers import engage_world


@pytest.fixture
def w(demo, login_as):
    return engage_world(demo, login_as)


def test_is_for_hr_only(w, login_as):
    with pytest.raises(ForbiddenError, match='requires role hr or executive'):
        services.insights(w['ram'].user, {})
    with pytest.raises(ForbiddenError):
        services.filter_options(w['sita'].user)
    # roles-flows.test.ts: payroll admin may not, executive may.
    with pytest.raises(ForbiddenError):
        services.insights(login_as(['payroll_admin']).user, {})
    assert services.insights(login_as(['executive']).user, {})['months']


def test_counts_headcount_joins_and_exits_by_bs_month_and_filters_by_department(w):
    today = w['today']
    bs = ad_to_bs(today)
    last_month_day = bs_to_ad(BsDate(bs.year - 1 if bs.month == 1 else bs.year, 12 if bs.month == 1 else bs.month - 1, 10))
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    execute("UPDATE employee SET status = 'exited', exit_date = %s WHERE id = %s", [today, w['extra'][1]])
    execute("UPDATE employee SET join_date = %s, gender = 'female', dob = '1995-01-01' WHERE id = %s", [last_month_day, w['extra'][2]])
    r = services.insights(w['hr'].user, {})
    assert len(r['months']) == 12
    cur, prev = r['headcount'][-1], r['headcount'][-2]
    assert cur['exits'] == 1
    assert cur['joins'] == 1  # Person 4 joined three days ago
    assert prev['joins'] == 1
    assert next(k for k in r['kpis'] if k['key'] == 'headcount')['value'] == cur['headcount']
    assert next(g for g in r['gender'] if g['label'] == 'Female')['count'] >= 1
    assert r['attrition']['split'] is None
    eng = services.insights(w['hr'].user, {'orgUnitId': w['dept_a']})
    assert [d['label'] for d in eng['departments']] == ['Engineering']
    with pytest.raises(BadRequestError, match='unknown department'):
        services.insights(w['foreign'].user, {'orgUnitId': w['dept_a']})
    with pytest.raises(BadRequestError, match='month must be 1 to 12'):
        services.insights(w['hr'].user, {'from': '2083-13'})
    with pytest.raises(BadRequestError, match='pick at most 36 months'):
        services.insights(w['hr'].user, {'from': '2070-01', 'to': '2083-12'})


def test_exports_each_dataset_as_csv_and_refuses_unknown_names(w):
    name, cols, out = services.export_dataset(w['hr'].user, 'headcount', {})
    assert name == 'insights-headcount'
    assert 'Headcount at month end' in [c['header'] for c in cols]
    assert len(out) == 12
    with pytest.raises(BadRequestError, match='unknown dataset; use one of headcount, tenure'):
        services.export_dataset(w['hr'].user, 'constructor', {})
    assert services.export_dataset(w['hr'].user, 'attendance', {})[0] == 'insights-attendance'


def test_splits_voluntary_and_involuntary_exits_when_exit_reasons_exist(w):
    execute("UPDATE employee SET status = 'exited', exit_date = %s WHERE id = %s", [w['today'], w['extra'][1]])
    execute('ALTER TABLE employee ADD COLUMN IF NOT EXISTS separation_type text')
    try:
        execute("UPDATE employee SET separation_type = 'resignation' WHERE id = %s", [w['extra'][1]])
        r = services.insights(w['hr'].user, {})
        assert r['attrition']['split'] == {'voluntary': 1, 'involuntary': 0, 'other': 0}
        assert r['headcount'][-1]['voluntary'] == 1
        assert r['exitReasonsAvailable'] is True
    finally:
        execute('ALTER TABLE employee DROP COLUMN separation_type')


# ---------------------------------------------------------------- the rest of the TS route and service

def test_filters_list_the_companys_branches_and_departments(w):
    f = services.filter_options(w['hr'].user)
    assert [b['name'] for b in f['branches']] == ['Head Office']
    assert [o['name'] for o in f['orgUnits']] == ['Engineering', 'Operations']
    assert f['orgUnits'][0] == {'id': w['dept_a'], 'name': 'Engineering', 'kind': 'department', 'parentId': None}
    assert services.filter_options(w['foreign'].user) == {'branches': [], 'orgUnits': []}


def test_query_shape_and_branch_checks(w, demo):
    with pytest.raises(ValidationError):
        services.insights(w['hr'].user, {'from': '2083/01'})
    with pytest.raises(ValidationError):
        services.insights(w['hr'].user, {'branchId': 'nope'})
    with pytest.raises(BadRequestError, match='unknown branch'):
        services.insights(w['foreign'].user, {'branchId': demo['branch_id']})
    with pytest.raises(BadRequestError, match='the start month is after the end month'):
        services.insights(w['hr'].user, {'from': '2083-05', 'to': '2083-04'})
    r = services.insights(w['hr'].user, {'from': '2083-01', 'to': '2083-03'})
    assert [m['label'] for m in r['months']] == ['Baishakh 2083', 'Jestha 2083', 'Ashadh 2083']
    assert r['months'][0]['short'] == 'Bai 83' and r['months'][0]['key'] == '2083-01'
    assert r['months'][0]['startAd'] == bs_month_bounds(2083, 1)['startAd']
    # A branch filter keeps people placed there.
    assert services.insights(w['hr'].user, {'branchId': demo['branch_id']})['headcount'][-1]['headcount'] >= 7


def test_months_between_defaults_to_the_last_twelve_and_checks_the_calendar():
    ms = services.months_between(None, '2083-03', '2026-10-26')
    assert [m['key'] for m in ms][:2] == ['2082-04', '2082-05'] and ms[-1]['key'] == '2083-03' and len(ms) == 12
    assert services.months_between('2083-01', None, '2026-10-26')[0]['key'] == '2083-01'
    assert len(services.months_between('2081-01', '2083-12', '2026-10-26')) == 36
    with pytest.raises(BadRequestError, match='pick at most 36 months'):
        services.months_between('2081-01', '2084-01', '2026-10-26')
    with pytest.raises(BadRequestError, match='outside the supported calendar'):
        services.months_between('1900-01', '1900-02', '2026-10-26')
    with pytest.raises(BadRequestError, match='YYYY-MM'):
        services.months_between('2083-1-1', None, '2026-10-26')
    # Ashadh and Ashwin keep distinct axis labels.
    assert [m['short'] for m in services.months_between('2083-03', '2083-06', '2026-10-26')] == ['Asr 83', 'Shr 83', 'Bha 83', 'Asw 83']


def test_classify_exit():
    assert services.classify_exit(None) is None
    assert services.classify_exit('Resignation') == 'voluntary'
    assert services.classify_exit('terminated') == 'involuntary'
    assert services.classify_exit('involuntary') == 'involuntary'
    assert services.classify_exit('lay_off') == 'involuntary'
    assert services.classify_exit('retirement') == 'other'


def test_kpis_bands_and_people_cost_from_locked_runs_only(w, demo):
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    r = services.insights(w['hr'].user, {})
    keys = [k['key'] for k in r['kpis']]
    assert keys == ['headcount', 'joins', 'exits', 'attrition', 'cost', 'absenteeism', 'late', 'hours', 'leave']
    cost = next(k for k in r['kpis'] if k['key'] == 'cost')
    assert cost == {'key': 'cost', 'label': 'People cost', 'value': None, 'delta': None, 'unit': 'NPR', 'goodWhen': 'neutral'}
    assert [t['label'] for t in r['tenure']] == ['Under 1 year', '1–2 years', '2–5 years', '5–10 years', '10+ years']
    assert sum(t['count'] for t in r['tenure']) == r['headcount'][-1]['headcount']
    assert [a['label'] for a in r['age']][-1] == 'Not recorded'
    assert 'Other' not in [g['label'] for g in r['gender']]
    assert all(not c['locked'] for c in r['cost']) and r['costByDepartment'] == []
    assert r['leave'] == [] and all(m['days'] == 0 for m in r['leaveMonthly'])
    assert all(a['absenteeismPct'] is None for a in r['attendance'])
    # Departments: most people first, then by name.
    assert r['departments'][0] == {'label': 'Engineering', 'count': 5}


def test_attendance_overtime_and_leave_come_into_the_months(w, demo):
    from tests.support.sql import one
    today = w['today']
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    lt = one("INSERT INTO leave_type (company_id, code, name_en, accrual_basis, sort_order) VALUES (%s, 'TSTL', 'Test leave', 'none', 1) RETURNING id",
             [demo['company_id']])
    execute("INSERT INTO leave_ledger (employee_id, leave_type_id, delta_days, reason, effective_date) VALUES (%s, %s, -2, 'usage', %s)",
            [demo['sita'], lt['id'], today])
    r = services.insights(w['hr'].user, {})
    assert r['leave'] == [{'type': 'Test leave', 'code': 'TSTL', 'days': 2, 'perEmployee': r['leave'][0]['perEmployee']}]
    assert r['leaveMonthly'][-1]['days'] == 2
    assert next(k for k in r['kpis'] if k['key'] == 'leave')['value'] == 2
    assert next(k for k in r['kpis'] if k['key'] == 'leave')['delta'] == 2


def test_a_department_filter_includes_the_teams_below_it(w, demo):
    from hajiri.db.models import OrgUnit
    team = OrgUnit.objects.create(company_id=demo['company_id'], kind='team', code='ENGT', name_en='Eng Team', parent_id=w['dept_a'])
    execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s', [team.id, w['extra'][0]])
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    labels = {d['label']: d['count'] for d in services.insights(w['hr'].user, {'orgUnitId': w['dept_a']})['departments']}
    assert labels == {'Engineering': 4, 'Eng Team': 1}
    assert [d['label'] for d in services.insights(w['hr'].user, {'orgUnitId': str(team.id)})['departments']] == ['Eng Team']


def test_exit_records_give_the_voluntary_split(w, demo):
    from hajiri.db.models import EmployeeExit
    execute("UPDATE employee SET status = 'exited', exit_date = %s WHERE id = %s", [w['today'], w['extra'][1]])
    EmployeeExit.objects.create(company_id=demo['company_id'], employee_id=w['extra'][1], kind=EmployeeExit.Kind.TERMINATION,
                                voluntary=False, submitted_on=w['today'], proposed_lwd=w['today'])
    r = services.insights(w['hr'].user, {})
    assert r['attrition']['split'] == {'voluntary': 0, 'involuntary': 1, 'other': 0}
    assert r['headcount'][-1]['involuntary'] == 1
