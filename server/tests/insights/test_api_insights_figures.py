"""Field-by-field check of GET /api/v1/insights against the figures the old TS computeInsights returned: attendance,
overtime, leave and people cost from locked pay runs, with JSON numbers (never strings), nulls where the old service
gave null, and the cost tile's label. Another company sees none of it."""
from datetime import date, timedelta

import pytest

from tests.insights.helpers import client_for, engage_world
from tests.support.sql import execute, one


@pytest.fixture
def w(demo, login_as):
    return engage_world(demo, login_as)


def _day(start_ad: str, n: int) -> str:
    return (date.fromisoformat(start_ad) + timedelta(days=n)).isoformat()


def _seed(w, demo, prev):
    """Sita's attendance, overtime, leave and a locked pay run in the previous BS month; a draft run is ignored."""
    s = prev['startAd']
    days = [('Present', 480, 10, 60), ('Present', 420, 0, 0), ('Absent', 0, 0, 0)]
    for i, (status, worked, late, ot) in enumerate(days):
        execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, late_min, ot_min, inputs_hash)
                   VALUES (%s, %s, 1, %s, %s, %s, %s, 'h')""", [demo['sita'], _day(s, i), status, worked, late, ot])
    # A later version of the absent day replaces it (v_attendance_latest in the TS query).
    execute("""INSERT INTO attendance_day (employee_id, local_date, version, status, worked_min, late_min, ot_min, inputs_hash)
               VALUES (%s, %s, 2, 'Absent', 0, 0, 0, 'h2')""", [demo['sita'], _day(s, 2)])
    execute("""INSERT INTO ot_slip (employee_id, local_date, detected_min, approved_min, day_type, status)
               VALUES (%s, %s, 60, 90, 'working', 'approved')""", [demo['sita'], _day(s, 0)])
    execute("""INSERT INTO ot_slip (employee_id, local_date, detected_min, approved_min, day_type, status)
               VALUES (%s, %s, 60, 600, 'working', 'rejected')""", [demo['sita'], _day(s, 1)])
    lt = one("""INSERT INTO leave_type (company_id, code, name_en, accrual_basis, sort_order)
                VALUES (%s, 'TSTL', 'Test leave', 'none', 1) RETURNING id""", [demo['company_id']])
    execute("""INSERT INTO leave_ledger (employee_id, leave_type_id, delta_days, reason, effective_date)
               VALUES (%s, %s, -1.5, 'usage', %s)""", [demo['sita'], lt['id'], _day(s, 3)])
    y, m = (int(x) for x in prev['key'].split('-'))
    period = one("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days)
                    VALUES (%s, %s, %s, %s, %s, 30) RETURNING id""", [demo['company_id'], y, m, prev['startAd'], prev['endAd']])
    for status, gross in (('locked', '50000.50'), ('draft', '99999.00')):
        run = one("""INSERT INTO payroll_run (company_id, period_id, run_type, status) VALUES (%s, %s, %s, %s) RETURNING id""",
                  [demo['company_id'], period['id'], 'regular' if status == 'locked' else 'off_cycle', status])
        execute("""INSERT INTO payslip (run_id, employee_id, inputs_snapshot, bs_days_in_month, paid_days, gross, taxable, tds,
                                        employer_statutory, total_deductions, net)
                   VALUES (%s, %s, '{}', 30, 30, %s, %s, 1200.10, 5000.25, 6200.10, 43800.40)""", [run['id'], demo['sita'], gross, gross])


def test_figures_match_the_old_service_field_by_field(w, demo):
    execute("UPDATE employee SET join_date = '2020-01-01' WHERE join_date IS NULL")
    hr = client_for(w['hr'])
    months = hr.get('/api/v1/insights').json()['months']
    prev = months[-2]
    _seed(w, demo, prev)
    body = hr.get('/api/v1/insights').json()

    att = body['attendance'][-2]
    assert att == {'month': prev['key'], 'label': prev['label'], 'short': prev['short'], 'scheduledDays': 3, 'presentDays': 2,
                   'absentDays': 1, 'lateDays': 1, 'absenteeismPct': 33.3, 'latePct': 50, 'avgWorkedHours': 7.5}
    # A month without attendance: zero counts and null rates, as the TS service returned.
    assert body['attendance'][-1]['absenteeismPct'] is None and body['attendance'][-1]['latePct'] is None
    assert body['attendance'][-1]['avgWorkedHours'] is None and body['attendance'][-1]['scheduledDays'] == 0

    assert body['overtime'][-2] == {'month': prev['key'], 'label': prev['label'], 'short': prev['short'],
                                    'detectedHours': 1, 'approvedHours': 1.5}

    assert body['leave'][0]['type'] == 'Test leave' and body['leave'][0]['code'] == 'TSTL' and body['leave'][0]['days'] == 1.5
    assert isinstance(body['leave'][0]['perEmployee'], (int, float))
    assert body['leaveMonthly'][-2] == {'month': prev['key'], 'label': prev['label'], 'short': prev['short'], 'days': 1.5}

    cost = body['cost'][-2]
    assert cost == {'month': prev['key'], 'label': prev['label'], 'short': prev['short'], 'gross': 50000.5,
                    'employerContributions': 5000.25, 'totalCost': 55000.75, 'net': 43800.4, 'tds': 1200.1,
                    'people': 1, 'locked': True}
    assert body['cost'][-1]['locked'] is False and body['cost'][-1]['gross'] == 0
    assert body['costByDepartment'] == [{'label': 'Engineering', 'totalCost': 55000.75}]

    kpis = {k['key']: k for k in body['kpis']}
    assert kpis['cost'] == {'key': 'cost', 'label': f"People cost, {prev['label'].split(' ')[0]}", 'value': 55000.75,
                            'delta': None, 'unit': 'NPR', 'goodWhen': 'neutral'}
    assert kpis['absenteeism']['value'] is None and kpis['absenteeism']['delta'] is None
    assert kpis['leave']['value'] == 0 and kpis['leave']['delta'] == -1.5
    assert kpis['attrition']['delta'] is None and kpis['attrition']['unit'] == '%'
    assert body['asOf'] == min(months[-1]['endAd'], w['today'])
    assert all(isinstance(m['startAd'], str) and len(m['startAd']) == 10 for m in body['months'])

    # The CSV carries the same numbers.
    csv = hr.get('/api/v1/insights/export/cost').content.decode()
    assert csv.startswith('\ufeff') and csv.splitlines()[0] == '\ufeffMonth,Gross pay,Employer SSF/PF/gratuity,Total cost,Net pay,TDS,People paid'
    assert f"{prev['label']},50000.5,5000.25,55000.75,43800.4,1200.1,1" in csv
    att_csv = hr.get('/api/v1/insights/export/attendance').content.decode()
    assert f"{prev['label']},3,2,1,33.3,1,50,7.5" in att_csv

    # Branch filter with no one placed there: empty figures, not this branch's people.
    other = one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'BR2', 'Second') RETURNING id", [demo['company_id']])
    b = hr.get('/api/v1/insights', {'branchId': str(other['id'])}).json()
    assert b['headcount'][-1]['headcount'] == 0 and b['cost'][-2]['locked'] is False and b['attendance'][-2]['scheduledDays'] == 0

    # Another company sees none of it.
    f = client_for(w['foreign']).get('/api/v1/insights').json()
    assert all(not c['locked'] for c in f['cost']) and f['costByDepartment'] == [] and f['leave'] == []
    assert all(a['scheduledDays'] == 0 for a in f['attendance']) and all(o['approvedHours'] == 0 for o in f['overtime'])


def test_bad_query_answers_the_old_validation_shape(w):
    hr = client_for(w['hr'])
    r = hr.get('/api/v1/insights', {'branchId': 'nope'})
    assert r.status_code == 400
    body = r.json()
    assert body['error'] == 'validation' and [i['path'] for i in body['issues']] == ['branchId']
    r = hr.get('/api/v1/insights/export/headcount', {'to': '2083/01'})
    assert r.status_code == 400 and [i['path'] for i in r.json()['issues']] == ['to']
    r = hr.get('/api/v1/insights', {'from': '2083-05', 'to': '2083-02'})
    assert r.status_code == 400 and r.json() == {'error': 'the start month is after the end month'}
