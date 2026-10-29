"""HTTP-level port of the insights block of apps/api/test/engage.test.ts and the insights checks of
roles-flows.test.ts: GET /api/v1/insights, /api/v1/insights/filters and /api/v1/insights/export/<dataset> with the
cookie session the React app uses, plus company isolation."""
import pytest
from django.test import Client

from engines.bs_calendar import BsDate, ad_to_bs, bs_to_ad
from tests.insights.helpers import client_for, engage_world
from tests.support.sql import execute


@pytest.fixture
def w(demo, login_as):
    return engage_world(demo, login_as)


def test_is_for_hr_and_executives_only(w, login_as):
    r = client_for(w['ram']).get('/api/v1/insights')
    assert r.status_code == 403 and r.json()['error'] == 'requires role hr or executive'
    assert client_for(w['sita']).get('/api/v1/insights/filters').status_code == 403
    assert client_for(w['sita']).get('/api/v1/insights/export/headcount').status_code == 403
    # roles-flows.test.ts: the payroll admin may not, the executive may.
    assert client_for(login_as(['payroll_admin'])).get('/api/v1/insights').status_code == 403
    assert client_for(login_as(['executive'])).get('/api/v1/insights').status_code == 200
    assert Client().get('/api/v1/insights').status_code == 401


def test_counts_headcount_joins_and_exits_by_bs_month_and_filters_by_department(w):
    today = w['today']
    bs = ad_to_bs(today)
    last_month_day = bs_to_ad(BsDate(bs.year - 1 if bs.month == 1 else bs.year, 12 if bs.month == 1 else bs.month - 1, 10))
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    execute("UPDATE employee SET status = 'exited', exit_date = %s WHERE id = %s", [today, w['extra'][1]])
    execute("UPDATE employee SET join_date = %s, gender = 'female', dob = '1995-01-01' WHERE id = %s", [last_month_day, w['extra'][2]])
    hr = client_for(w['hr'])
    r = hr.get('/api/v1/insights')
    assert r.status_code == 200
    body = r.json()
    assert len(body['months']) == 12
    cur, prev = body['headcount'][-1], body['headcount'][-2]
    assert cur['exits'] == 1
    assert cur['joins'] == 1  # Person 4 joined three days ago
    assert prev['joins'] == 1
    assert next(k for k in body['kpis'] if k['key'] == 'headcount')['value'] == cur['headcount']
    assert next(g for g in body['gender'] if g['label'] == 'Female')['count'] >= 1
    assert body['attrition']['split'] is None
    # Every key the Insights page reads.
    for key in ('months', 'asOf', 'kpis', 'headcount', 'attrition', 'tenure', 'age', 'gender', 'departments', 'branches',
                'cost', 'costByDepartment', 'overtime', 'leave', 'leaveMonthly', 'attendance', 'exitReasonsAvailable'):
        assert key in body
    assert set(body['months'][0]) >= {'key', 'label', 'short', 'startAd', 'endAd'}
    assert set(body['kpis'][0]) >= {'key', 'label', 'value', 'delta', 'unit', 'goodWhen'}
    assert set(body['cost'][0]) >= {'label', 'gross', 'employerContributions', 'totalCost', 'people', 'locked'}
    assert set(body['attendance'][0]) >= {'label', 'absenteeismPct', 'latePct', 'absentDays', 'lateDays', 'avgWorkedHours', 'presentDays'}
    assert set(body['overtime'][0]) >= {'label', 'detectedHours', 'approvedHours'}
    assert set(body['attrition']) >= {'exits', 'monthlyAvgPct', 'annualisedPct', 'split'}

    eng = hr.get('/api/v1/insights', {'orgUnitId': w['dept_a']})
    assert eng.status_code == 200 and [d['label'] for d in eng.json()['departments']] == ['Engineering']
    r = client_for(w['foreign']).get('/api/v1/insights', {'orgUnitId': w['dept_a']})
    assert r.status_code == 400 and r.json() == {'error': 'unknown department'}
    r = hr.get('/api/v1/insights', {'from': '2083-13'})
    assert r.status_code == 400 and r.json()['error'] == 'month must be 1 to 12'
    r = hr.get('/api/v1/insights', {'from': '2070-01', 'to': '2083-12'})
    assert r.status_code == 400 and r.json()['error'] == 'pick at most 36 months'


def test_query_shape_is_validated(w, demo):
    hr = client_for(w['hr'])
    assert hr.get('/api/v1/insights', {'from': '2083/01'}).status_code == 400
    assert hr.get('/api/v1/insights', {'branchId': 'nope'}).status_code == 400
    r = hr.get('/api/v1/insights', {'from': '2083-01', 'to': '2083-03'})
    assert r.status_code == 200
    assert [m['label'] for m in r.json()['months']] == ['Baishakh 2083', 'Jestha 2083', 'Ashadh 2083']
    # Empty values are what the page sends when a filter is cleared: ignored.
    assert hr.get('/api/v1/insights', {'branchId': '', 'orgUnitId': ''}).status_code == 200
    assert hr.get('/api/v1/insights', {'branchId': demo['branch_id']}).status_code == 200


def test_exports_each_dataset_as_csv_and_refuses_unknown_names(w):
    hr = client_for(w['hr'])
    csv = hr.get('/api/v1/insights/export/headcount')
    assert csv.status_code == 200
    assert 'text/csv' in csv['Content-Type']
    assert 'insights-headcount.csv' in csv['Content-Disposition']
    assert 'Headcount at month end' in csv.content.decode()
    r = hr.get('/api/v1/insights/export/constructor')
    assert r.status_code == 400 and r.json()['error'].startswith('unknown dataset; use one of headcount')
    assert hr.get('/api/v1/insights/export/attendance').status_code == 200
    for ds in ('tenure', 'age', 'gender', 'departments', 'branches', 'cost', 'costByDepartment', 'overtime', 'leave'):
        assert hr.get(f'/api/v1/insights/export/{ds}').status_code == 200, ds
    assert 'insights-people-cost.csv' in hr.get('/api/v1/insights/export/cost')['Content-Disposition']
    # The filters apply to the export too.
    r = client_for(w['foreign']).get('/api/v1/insights/export/departments', {'orgUnitId': w['dept_a']})
    assert r.status_code == 400 and r.json() == {'error': 'unknown department'}


def test_splits_voluntary_and_involuntary_exits_when_exit_reasons_exist(w):
    execute("UPDATE employee SET status = 'exited', exit_date = %s WHERE id = %s", [w['today'], w['extra'][1]])
    execute('ALTER TABLE employee ADD COLUMN IF NOT EXISTS separation_type text')
    try:
        execute("UPDATE employee SET separation_type = 'resignation' WHERE id = %s", [w['extra'][1]])
        r = client_for(w['hr']).get('/api/v1/insights')
        assert r.json()['attrition']['split'] == {'voluntary': 1, 'involuntary': 0, 'other': 0}
        assert r.json()['headcount'][-1]['voluntary'] == 1
    finally:
        execute('ALTER TABLE employee DROP COLUMN separation_type')


def test_filters_list_only_the_active_companys_branches_and_departments(w, demo):
    r = client_for(w['hr']).get('/api/v1/insights/filters')
    assert r.status_code == 200
    body = r.json()
    assert [b['name'] for b in body['branches']] == ['Head Office']
    assert body['branches'][0] == {'id': str(demo['branch_id']), 'name': 'Head Office'}
    assert body['orgUnits'][0] == {'id': w['dept_a'], 'name': 'Engineering', 'kind': 'department', 'parentId': None}
    r = client_for(w['foreign']).get('/api/v1/insights/filters')
    assert r.status_code == 200 and r.json() == {'branches': [], 'orgUnits': []}
    # Another company's branch is unknown.
    r = client_for(w['foreign']).get('/api/v1/insights', {'branchId': demo['branch_id']})
    assert r.status_code == 400 and r.json() == {'error': 'unknown branch'}
    # Its headcount does not include this company's people.
    r = client_for(w['foreign']).get('/api/v1/insights')
    assert r.status_code == 200 and r.json()['headcount'][-1]['headcount'] == 0
