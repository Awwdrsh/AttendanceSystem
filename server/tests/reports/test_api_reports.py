"""HTTP port of apps/api/test/reports.test.ts and the /reports halves of features-reports.test.ts and endpoints.test.ts:
the attendance reports, the People record reports and the report builder over /api/v1/reports, with company
isolation checks."""
import json

import pytest
from django.test import Client

from hajiri.attendance.service import recompute_day
from hajiri.core import auth
from tests.core.fixtures import PW, bearer
from tests.core.punches import insert_punch
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.support.sql import execute, one

R = '/api/v1/reports'


def get(path, token, **q):
    return Client().get(path, q, **bearer(token))


def post(path, token, body):
    return Client().post(path, json.dumps(body), content_type='application/json', **bearer(token))


@pytest.fixture
def t(demo, login_as):
    """Sita punched in late on 17 Aug 2026 (1 Bhadra 2083) and stayed late on the 18th."""
    for d, i, o in (('2026-08-17', '10:20', '18:00'), ('2026-08-18', '10:00', '19:30')):
        insert_punch(demo['sita'], d, i, key_prefix=f'r-{d}-a')
        insert_punch(demo['sita'], d, o, key_prefix=f'r-{d}-b')
        recompute_day(demo['sita'], d)
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute("""UPDATE employee SET join_date = '2024-04-13', permanent_address = '{"province":"Bagmati","ward":5}'
               WHERE id = %s""", [demo['sita']])
    execute("UPDATE employee SET join_date = '2022-01-01' WHERE id = %s", [demo['ram']])
    execute("""INSERT INTO employee_education (employee_id, level, degree, institution, end_year)
               VALUES (%s, 'bachelor', 'BBA', '=HYPERLINK("x")', 2016)""", [demo['sita']])
    c = str(one("INSERT INTO company (code, name_en) VALUES ('OTHERF', 'Other Co') RETURNING id")['id'])
    auth.create_user(company_id=c, email='hr@other.reports', name='Other HR', password=PW, roles=['hr'])
    return {**demo, 'hr': login_as(['hr']).token, 'hr2': login_as(['hr']).token,
            'sita_t': login_as(['employee'], demo['sita']).token, 'ram_t': login_as(['manager', 'employee'], demo['ram']).token,
            'foreign': auth.login('hr@other.reports', PW).token}


# ---------------------------------------------------------------- attendance reports

def test_monthly_register_covers_every_bs_day_and_exports_csv_with_a_bom(t):
    r = get(f'{R}/register', t['hr'], bsYear=2083, bsMonth=5)
    assert r.status_code == 200
    body = r.json()
    assert [c['key'] for c in body['columns']][:3] == ['emp_code', 'name_en', 'bs_date']
    assert len(body['rows']) == 2 * 31
    sita = [x for x in body['rows'] if x['emp_code'] == 'E001'][0]
    assert {k: sita[k] for k in ('bs_date', 'ad_date', 'arrival', 'departure', 'worked_hours', 'late_min', 'status')} == {
        'bs_date': '2083-05-01', 'ad_date': '2026-08-17', 'arrival': '10:20', 'departure': '18:00', 'worked_hours': '7.67',
        'late_min': 5, 'status': 'Present'}
    csv = get(f'{R}/register', t['hr'], bsYear=2083, bsMonth=5, format='csv')
    assert 'text/csv' in csv['Content-Type']
    assert csv['Content-Disposition'] == 'attachment; filename="register-2083-05.csv"'
    text = csv.content.decode('utf-8')
    assert text[0] == '﻿'
    assert text.split('\r\n')[0] == ('﻿Emp code,Name,Date (BS),Date (AD),Arrival,Departure,Worked (h),Break (min),OT (h),'
                                     'Late (min),Leave,Status')


def test_every_attendance_report_answers_for_hr_and_employees_are_denied(t):
    for path, q in (('daily', {'date': '2026-08-17'}), ('late-early', {'from': '2026-08-17', 'to': '2026-08-18'}),
                    ('headcount', {}), ('exceptions-summary', {'from': '2026-08-17', 'to': '2026-08-18'}), ('devices', {}),
                    ('leave-balances', {}), ('ot', {'from': '2026-08-17', 'to': '2026-08-18'}),
                    ('punch-audit', {'employeeId': t['sita'], 'from': '2026-08-17', 'to': '2026-08-18'})):
        r = get(f'{R}/{path}', t['hr'], **q)
        assert r.status_code == 200, path
        assert isinstance(r.json()['rows'], list) and r.json()['columns'], path
    late = get(f'{R}/late-early', t['hr'], **{'from': '2026-08-17', 'to': '2026-08-18'}).json()['rows'][0]
    assert (late['emp_code'], late['late_days'], late['late_min']) == ('E001', 1, 5)
    refused = get(f'{R}/daily', t['sita_t'], date='2026-08-17')
    assert refused.status_code == 403 and refused.json() == {'error': 'requires role hr or manager'}
    assert get(f'{R}/headcount', t['sita_t']).status_code == 403
    assert Client().get(f'{R}/headcount').status_code == 401


def test_validation_unknown_reports_and_hr_only_reports(t):
    bad = get(f'{R}/daily', t['hr'])
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert get(f'{R}/daily', t['hr'], date='2026-08-17', format='xml').status_code == 400
    assert get(f'{R}/register', t['hr'], bsYear=2083, bsMonth=13).status_code == 400
    assert get(f'{R}/nope', t['hr']).json() == {'error': 'not found'}
    for name in ('leave-balances', 'devices'):
        assert get(f'{R}/{name}', t['ram_t']).json() == {'error': 'requires role hr'}
    rows = get(f'{R}/daily', t['ram_t'], date='2026-08-17').json()['rows']
    assert sorted(x['emp_code'] for x in rows) == ['E001', 'E002']


def test_punch_audit_is_scoped_to_the_employee_and_the_company(t):
    own = get(f'{R}/punch-audit', t['hr'], employeeId=t['sita'], **{'from': '2026-08-17', 'to': '2026-08-18'})
    assert len(own.json()['rows']) == 4 and own.json()['rows'][0]['time'] == '10:20'
    foreign = get(f'{R}/punch-audit', t['foreign'], employeeId=t['sita'], **{'from': '2026-08-17', 'to': '2026-08-18'})
    assert foreign.status_code == 404 and foreign.json() == {'error': 'employee not found'}
    assert get(f'{R}/punch-audit', t['sita_t'], employeeId=t['sita'], **{'from': '2026-08-17', 'to': '2026-08-18'}).status_code == 403


# ---------------------------------------------------------------- People record reports

def test_people_reports_json_csv_filters_and_scope(t):
    now = get(f'{R}/people/headcount-on-date', t['hr'])
    assert now.status_code == 200
    total = sum(r['headcount'] for r in now.json()['rows'])
    assert total >= 2
    past = get(f'{R}/people/headcount-on-date', t['hr'], date='2023-01-01')
    assert sum(r['headcount'] for r in past.json()['rows']) == total - 1
    stats = get(f'{R}/people/statistics', t['hr']).json()['rows']
    assert any(r['dimension'] == 'Province (permanent address)' and r['bucket'] == 'Bagmati' for r in stats)
    jl = get(f'{R}/people/joiners-leavers', t['hr'], **{'from': '2024-04-01', 'to': '2024-04-30'})
    assert [r['emp_code'] for r in jl.json()['rows']] == ['E001']
    csv = get(f'{R}/people/education', t['hr'], format='csv')
    assert 'text/csv' in csv['Content-Type']
    assert '"\'=HYPERLINK(""x"")"' in csv.content.decode()
    jl_csv = get(f'{R}/people/joiners-leavers', t['hr'], format='csv', **{'from': '2024-04-01', 'to': '2024-04-30'})
    assert '1 Baishakh 2081' in jl_csv.content.decode()
    other_branch = one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'BRT', 'Biratnagar') RETURNING id",
                       [t['company_id']])['id']
    assert get(f'{R}/people/addresses', t['hr'], branchId=str(other_branch)).json()['rows'] == []
    assert len(get(f'{R}/people/addresses', t['hr'], branchId=t['branch_id']).json()['rows']) >= 2
    assert get(f'{R}/people/addresses', t['hr'], status='exited').json()['rows'] == []
    # Managers get the team reports only; employees none; another company sees nothing.
    hc = get(f'{R}/people/headcount-on-date', t['ram_t'])
    assert hc.status_code == 200 and sum(r['headcount'] for r in hc.json()['rows']) == 2
    assert get(f'{R}/people/nominees', t['ram_t']).json() == {'error': 'this report is for HR only'}
    assert get(f'{R}/people/statistics', t['ram_t']).status_code == 403
    assert get(f'{R}/people/headcount-on-date', t['sita_t']).status_code == 403
    assert get(f'{R}/people/nominees', t['foreign']).json()['rows'] == []
    # Bad input.
    assert get(f'{R}/people/joiners-leavers', t['hr'], **{'from': '2024-05-01', 'to': '2024-04-01'}).status_code == 400
    assert get(f'{R}/people/birthdays-anniversaries', t['hr'], days=500).status_code == 400
    assert get(f'{R}/people/addresses', t['hr'], branchId='not-a-uuid').status_code == 400
    assert get(f'{R}/people/nope', t['hr']).status_code == 404


# ---------------------------------------------------------------- ad-hoc report builder

def test_builder_datasets_run_and_csv_are_hr_only(t):
    r = get(f'{R}/adhoc/datasets', t['hr'])
    assert r.status_code == 200
    keys = [d['key'] for d in r.json()['datasets']]
    assert {'employees', 'education', 'family', 'experience', 'career_events', 'leave_balances'} <= set(keys)
    assert 'eq' in r.json()['operators']
    assert get(f'{R}/adhoc/datasets', t['ram_t']).json() == {'error': 'requires role hr'}
    run = post(f'{R}/adhoc/run', t['hr'], {'dataset': 'employees', 'columns': ['emp_code', 'province'],
                                           'filters': [{'column': 'province', 'op': 'eq', 'value': 'bagmati'}]})
    assert run.status_code == 200
    assert run.json()['rows'] == [{'emp_code': 'E001', 'province': 'Bagmati'}]
    assert run.json()['total'] == 1 and run.json()['limited'] is False
    assert [c['key'] for c in run.json()['columns']] == ['emp_code', 'province']
    bad = post(f'{R}/adhoc/run', t['hr'], {'dataset': 'employees', 'columns': ['pan_enc']})
    assert bad.status_code == 400
    csv = Client().post(f'{R}/adhoc/run?format=csv', json.dumps({'dataset': 'employees', 'columns': ['emp_code', 'join_date']}),
                        content_type='application/json', **bearer(t['hr']))
    assert csv.status_code == 200 and 'text/csv' in csv['Content-Type']
    assert 'E001,1 Baishakh 2081' in csv.content.decode()
    assert post(f'{R}/adhoc/run', t['ram_t'], {'dataset': 'employees', 'columns': ['emp_code']}).status_code == 403
    assert post(f'{R}/adhoc/run', t['sita_t'], {'dataset': 'employees', 'columns': ['emp_code']}).status_code == 403
    assert post(f'{R}/adhoc/run', t['foreign'], {'dataset': 'employees', 'columns': ['emp_code']}).json()['rows'] == []


def test_saves_shares_runs_updates_and_deletes_named_reports(t):
    d = {'dataset': 'education', 'columns': ['emp_code', 'institution']}
    mine = post(f'{R}/adhoc/saved', t['hr'], {'name': 'Degrees', 'definition': d})
    assert mine.status_code == 201
    assert set(mine.json()['report']) == {'id', 'name', 'description', 'shared', 'definition', 'mine', 'updated_at'}
    mine_id = mine.json()['report']['id']
    shared_id = post(f'{R}/adhoc/saved', t['hr'], {'name': 'Shared degrees', 'shared': True, 'definition': d}).json()['report']['id']
    bad = post(f'{R}/adhoc/saved', t['hr'], {'name': 'Bad', 'definition': {'dataset': 'employees', 'columns': ['nope']}})
    assert get(f"{R}/adhoc/saved/{bad.json()['report']['id']}/run", t['hr']).status_code == 400
    listed = get(f'{R}/adhoc/saved', t['hr2']).json()['reports']
    assert [x['name'] for x in listed] == ['Shared degrees'] and listed[0]['mine'] is False
    assert get(f'{R}/adhoc/saved/{mine_id}/run', t['hr2']).json() == {'error': 'report not found'}
    ran = get(f'{R}/adhoc/saved/{shared_id}/run', t['hr2'])
    assert ran.status_code == 200 and len(ran.json()['rows']) > 0
    assert 'text/csv' in get(f'{R}/adhoc/saved/{shared_id}/run', t['hr2'], format='csv')['Content-Type']
    put = Client().put(f'{R}/adhoc/saved/{shared_id}', json.dumps({'name': 'Mine now', 'definition': d}),
                       content_type='application/json', **bearer(t['hr2']))
    assert put.status_code == 403 and put.json() == {'error': 'only the person who saved this report can change it'}
    assert Client().delete(f'{R}/adhoc/saved/{shared_id}', **bearer(t['hr2'])).status_code == 403
    assert get(f'{R}/adhoc/saved/{shared_id}/run', t['foreign']).status_code == 404
    assert Client().delete(f'{R}/adhoc/saved/{shared_id}', **bearer(t['foreign'])).status_code == 404
    upd = Client().put(f'{R}/adhoc/saved/{mine_id}', json.dumps({'name': 'Degrees v2', 'shared': False,
                                                                 'definition': {**d, 'columns': ['emp_code']}}),
                       content_type='application/json', **bearer(t['hr']))
    assert upd.status_code == 200 and upd.json()['report']['name'] == 'Degrees v2'
    gone = Client().delete(f'{R}/adhoc/saved/{mine_id}', **bearer(t['hr']))
    assert gone.status_code == 200 and gone.json() == {'ok': True}
    assert 'Degrees v2' not in [x['name'] for x in get(f'{R}/adhoc/saved', t['hr']).json()['reports']]
    assert post(f'{R}/adhoc/saved', t['hr'], {'name': '', 'definition': d}).status_code == 400
    assert get(f'{R}/adhoc/saved/not-a-uuid/run', t['hr']).status_code == 400


def test_reports_read_only_the_active_company(world):
    beta, _ = world.session(world.admin_b)
    alpha, _ = world.session(world.admin_a)
    outsider, _ = world.session(world.admin_c)
    sid = post(f'{R}/adhoc/saved', beta, {'name': 'Beta list', 'shared': True,
                                         'definition': {'dataset': 'employees', 'columns': ['name']}}).json()['report']['id']
    beta_names = [r['name'] for r in post(f'{R}/adhoc/run', beta, {'dataset': 'employees', 'columns': ['name']}).json()['rows']]
    assert 'Bimal Admin' in beta_names and 'Asha Admin' not in beta_names
    for token in (alpha, outsider):
        assert get(f'{R}/adhoc/saved/{sid}/run', token).status_code == 404
        assert sid not in [x['id'] for x in get(f'{R}/adhoc/saved', token).json()['reports']]
        assert Client().delete(f'{R}/adhoc/saved/{sid}', **bearer(token)).status_code == 404
    switched, _ = world.session(world.admin_a, world.beta)
    assert get(f'{R}/adhoc/saved/{sid}/run', switched).status_code == 200
    employee = world.dual_beta_employee.id
    q = {'employeeId': str(employee), 'from': '2026-08-01', 'to': '2026-08-31'}
    assert get(f'{R}/punch-audit', alpha, **q).status_code == 404
    assert get(f'{R}/punch-audit', beta, **q).status_code == 200
