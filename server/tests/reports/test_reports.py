"""Port of the People reports and report builder halves of apps/api/test/features-reports.test.ts, the CSV case of
qa-foundation.test.ts and the attendance reports (reports/routes.ts)."""
import pytest

from hajiri.core import auth
from hajiri.core.db import execute, one
from hajiri.core.errors import BadRequestError, ForbiddenError, NotFoundError, ValidationError
from hajiri.reports import service
from hajiri.reports.csv import Column, csv_cell, send_table, to_csv
from hajiri.reports.people import people_report
from tests.core.fixtures import PW
from tests.core.punches import insert_punch


@pytest.fixture
def p(demo, login_as):
    hr = login_as(['hr']).user
    hr2 = login_as(['hr']).user
    sita = login_as(['employee'], demo['sita']).user
    ram = login_as(['employee', 'manager'], demo['ram']).user
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute("""UPDATE employee SET join_date = '2024-04-13', dob = '1994-10-20', marital_status = 'single',
                 permanent_address = '{"province":"Bagmati","district":"Lalitpur","municipality":"Lalitpur Metropolitan City","ward":5}'
               WHERE id = %s""", [demo['sita']])
    execute("UPDATE employee SET join_date = '2022-01-01', dob = '1985-03-02', passport_expiry = %s::date + 20 WHERE id = %s",
            [service.npt_today(), demo['ram']])
    execute("INSERT INTO employee_family (employee_id, name, relation, is_nominee, nominee_share) VALUES (%s, 'Gita Sharma', 'mother', true, 100)", [demo['sita']])
    execute("INSERT INTO employee_emergency_contact (employee_id, name, relation, phone) VALUES (%s, 'Hari Sharma', 'father', '9800000000')", [demo['sita']])
    execute("INSERT INTO employee_education (employee_id, level, degree, institution, end_year) VALUES (%s, 'bachelor', 'BBA', 'Tribhuvan University', 2016)", [demo['sita']])
    c = str(one("INSERT INTO company (code, name_en) VALUES ('OTHERF', 'Other Co') RETURNING id")['id'])
    auth.create_user(company_id=c, email='hr@other.features', name='Other HR', password=PW, roles=['hr'])
    foreign = auth.resolve_session(auth.login('hr@other.features', PW).token)
    return {**demo, 'hr': hr, 'hr2': hr2, 'sita_user': sita, 'ram_user': ram, 'foreign': foreign}


def total(t):
    return sum(r['headcount'] for r in t.rows)


# ---------------------------------------------------------------- People record reports

def test_headcount_on_a_date_counts_only_people_employed_then(p):
    now = people_report(p['hr'], 'headcount-on-date', {})
    assert total(now) >= 2
    assert total(people_report(p['hr'], 'headcount-on-date', {'date': '2023-01-01'})) == total(now) - 1


def test_statistics_joiners_birthdays_nominees_contacts_addresses_education(p):
    stats = people_report(p['hr'], 'statistics', {})
    assert any(r['dimension'] == 'Province (permanent address)' and r['bucket'] == 'Bagmati' for r in stats.rows)
    assert all(r['share'].endswith('%') for r in stats.rows)
    jl = people_report(p['hr'], 'joiners-leavers', {'from': '2024-04-01', 'to': '2024-04-30'})
    assert [r['emp_code'] for r in jl.rows] == ['E001']
    bd = people_report(p['hr'], 'birthdays-anniversaries', {'from': '2026-10-15', 'days': '10'})
    assert any(r['occasion'] == 'Birthday' and r['emp_code'] == 'E001' and r['years'] == 32 for r in bd.rows)
    nom = people_report(p['hr'], 'nominees', {}).rows[0]
    assert (nom['nominee'], nom['share']) == ('Gita Sharma', '100.00')
    assert people_report(p['hr'], 'emergency-contacts', {}).rows[0]['contact'] == 'Hari Sharma'
    addr = next(r for r in people_report(p['hr'], 'addresses', {}).rows if r['emp_code'] == 'E001')
    assert 'Ward 5' in addr['permanent_address']
    ed = people_report(p['hr'], 'education', {}).rows[0]
    assert (ed['level'], ed['institution']) == ('Bachelor', 'Tribhuvan University')


def test_licence_register_never_shows_id_numbers_and_flags_expiring_documents(p):
    execute("UPDATE employee SET passport_no_enc = '\\x00'::bytea WHERE id = %s", [p['ram']])
    r = people_report(p['hr'], 'licence-register', {})
    row = next(x for x in r.rows if x['emp_code'] == 'E002' and x['document'] == 'Passport')
    assert (row['on_file'], row['state']) == ('Recorded', 'Expiring soon')
    import json
    assert 'passport_no' not in json.dumps(r.to_dict(), default=str)


def test_record_completeness_lists_what_is_missing_least_complete_first(p):
    r = people_report(p['hr'], 'record-completeness', {})
    assert r.rows[0]['complete'] <= r.rows[-1]['complete']
    assert 'Emergency contact' in next(x for x in r.rows if x['emp_code'] == 'E002')['missing']


def test_csv_shows_bs_dates_and_neutralises_formula_cells(p):
    execute("""UPDATE employee_education SET institution = '=HYPERLINK("x")' WHERE employee_id = %s""", [p['sita']])
    csv = send_table('csv', people_report(p['hr'], 'education', {'format': 'csv'}))
    assert 'text/csv' in csv['Content-Type']
    assert '"\'=HYPERLINK(""x"")"' in csv.content.decode()
    jl = send_table('csv', people_report(p['hr'], 'joiners-leavers', {'from': '2024-04-01', 'to': '2024-04-30', 'format': 'csv'}))
    assert '1 Baishakh 2081' in jl.content.decode()
    assert jl['Content-Disposition'] == 'attachment; filename="joiners-leavers-2024-04-01_2024-04-30.csv"'


def test_filters_by_branch_department_and_status(p):
    other = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'BRT', 'Biratnagar') RETURNING id", [p['company_id']])['id'])
    assert people_report(p['hr'], 'addresses', {'branchId': other}).rows == []
    assert len(people_report(p['hr'], 'addresses', {'branchId': p['branch_id']}).rows) >= 2
    assert people_report(p['hr'], 'addresses', {'status': 'exited'}).rows == []
    dept = str(one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'D', 'Dept') RETURNING id", [p['company_id']])['id'])
    team = str(one("INSERT INTO org_unit (company_id, kind, code, name_en, parent_id) VALUES (%s, 'team', 'T', 'Team', %s) RETURNING id", [p['company_id'], dept])['id'])
    execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s', [team, p['sita']])
    assert [r['emp_code'] for r in people_report(p['hr'], 'addresses', {'orgUnitId': dept}).rows] == ['E001']


def test_managers_get_only_the_team_reports_scoped_to_their_team(p):
    assert total(people_report(p['ram_user'], 'headcount-on-date', {})) == 2  # Ram and Sita
    for name in ('nominees', 'statistics'):
        with pytest.raises(ForbiddenError, match='HR only'):
            people_report(p['ram_user'], name, {})
    with pytest.raises(ForbiddenError):
        people_report(p['sita_user'], 'headcount-on-date', {})
    assert people_report(p['foreign'], 'nominees', {}).rows == []


def test_bad_input_is_rejected(p):
    with pytest.raises(BadRequestError, match='start date is after the end date'):
        people_report(p['hr'], 'joiners-leavers', {'from': '2024-05-01', 'to': '2024-04-01'})
    with pytest.raises(ValidationError):
        people_report(p['hr'], 'birthdays-anniversaries', {'days': '500'})
    with pytest.raises(ValidationError):
        people_report(p['hr'], 'addresses', {'branchId': 'not-a-uuid'})
    with pytest.raises(NotFoundError):
        people_report(p['hr'], 'nope', {})


# ---------------------------------------------------------------- ad-hoc report builder

def run(user, body, csv=False):
    return service.run_adhoc(user, body, csv=csv)


def test_lists_datasets_with_whitelisted_columns(p):
    r = service.builder_datasets(p['hr'])
    keys = [d['key'] for d in r['datasets']]
    assert {'employees', 'education', 'family', 'experience', 'career_events', 'leave_balances'} <= set(keys)
    emp = next(d for d in r['datasets'] if d['key'] == 'employees')
    assert 'pan' not in [c['key'] for c in emp['columns']]


def test_runs_with_columns_filters_and_sort(p):
    r = run(p['hr'], {'dataset': 'employees', 'columns': ['emp_code', 'name', 'age', 'province'],
                      'filters': [{'column': 'province', 'op': 'eq', 'value': 'bagmati'}], 'sort': {'column': 'name', 'dir': 'desc'}})
    assert len(r['rows']) == 1
    row = r['rows'][0]
    assert (row['emp_code'], row['name'], row['province']) == ('E001', 'Sita Sharma', 'Bagmati') and isinstance(row['age'], int)
    assert set(row) == {'emp_code', 'name', 'age', 'province'}
    assert r['total'] == 1 and r['limited'] is False
    allr = run(p['hr'], {'dataset': 'employees', 'columns': ['emp_code'], 'filters': [{'column': 'join_date', 'op': 'gte', 'value': '2023-01-01'}]})
    assert [x['emp_code'] for x in allr['rows']] == ['E001']
    nom = run(p['hr'], {'dataset': 'family', 'columns': ['emp_code', 'member', 'nominee'], 'filters': [{'column': 'nominee', 'op': 'eq', 'value': True}]})
    assert nom['rows'] == [{'emp_code': 'E001', 'member': 'Gita Sharma', 'nominee': True}]
    contains = run(p['hr'], {'dataset': 'education', 'columns': ['institution'], 'filters': [{'column': 'institution', 'op': 'contains', 'value': '%'}]})
    assert contains['rows'] == []  # % is matched literally


def test_refuses_anything_outside_the_whitelist(p):
    attempts = [
        {'dataset': 'employees', 'columns': ['emp_code; DROP TABLE employee']},
        {'dataset': 'employees', 'columns': ['pan_enc']},
        {'dataset': 'app_user', 'columns': ['email']},
        {'dataset': 'employees', 'columns': ['emp_code'], 'sort': {'column': 'name_en desc, (select 1)', 'dir': 'asc'}},
        {'dataset': 'employees', 'columns': ['emp_code'], 'sort': {'column': 'name', 'dir': 'desc; drop'}},
        {'dataset': 'employees', 'columns': ['emp_code'], 'filters': [{'column': 'name', 'op': 'like', 'value': 'x'}]},
        {'dataset': 'employees', 'columns': ['emp_code'], 'filters': [{'column': 'age', 'op': 'gt', 'value': '1 OR 1=1'}]},
        {'dataset': 'employees', 'columns': ['emp_code'], 'filters': [{'column': 'dob', 'op': 'lt', 'value': "2020-01-01' OR '1'='1"}]},
        {'dataset': 'employees', 'columns': ['emp_code'], 'extra': 'x'},
    ]
    for a in attempts:
        with pytest.raises(BadRequestError):
            run(p['hr'], a)
    quoted = run(p['hr'], {'dataset': 'employees', 'columns': ['emp_code'], 'filters': [{'column': 'name', 'op': 'eq', 'value': "x' OR '1'='1"}]})
    assert quoted['rows'] == []
    assert one('SELECT count(*)::int AS n FROM employee')['n'] > 0


def test_exports_csv_with_bs_dates_and_is_hr_only(p):
    csv = run(p['hr'], {'dataset': 'employees', 'columns': ['emp_code', 'join_date', 'pan_on_file']}, csv=True)
    text = csv.content.decode()
    assert 'text/csv' in csv['Content-Type'] and 'Join date' in text
    assert 'E001,1 Baishakh 2081,No' in text
    assert one("SELECT after FROM audit_log WHERE action = 'export' AND table_name = 'adhoc_report'")['after']['rows'] == 2
    for u in ('ram_user', 'sita_user'):
        with pytest.raises(ForbiddenError):
            run(p[u], {'dataset': 'employees', 'columns': ['emp_code']})
    assert run(p['foreign'], {'dataset': 'employees', 'columns': ['emp_code']})['rows'] == []


def test_saves_shares_runs_updates_and_deletes_named_reports(p):
    d = {'dataset': 'education', 'columns': ['emp_code', 'institution']}
    mine = service.create_saved(p['hr'], {'name': 'Degrees', 'definition': d})
    shared = service.create_saved(p['hr'], {'name': 'Shared degrees', 'shared': True, 'definition': d})
    # A saved definition must parse, but its columns are checked again when it runs.
    bad = service.create_saved(p['hr'], {'name': 'Bad', 'definition': {'dataset': 'employees', 'columns': ['nope']}})
    with pytest.raises(BadRequestError):
        service.run_saved(p['hr'], str(bad['id']))
    assert [x['name'] for x in service.list_saved(p['hr2'])] == ['Shared degrees']
    with pytest.raises(NotFoundError):
        service.run_saved(p['hr2'], str(mine['id']))
    ran = service.run_saved(p['hr2'], str(shared['id']))
    assert len(ran['rows']) > 0
    with pytest.raises(ForbiddenError):
        service.update_saved(p['hr2'], str(shared['id']), {'name': 'Mine now', 'definition': d})
    with pytest.raises(ForbiddenError):
        service.delete_saved(p['hr2'], str(shared['id']))
    with pytest.raises(NotFoundError):
        service.run_saved(p['foreign'], str(shared['id']))
    upd = service.update_saved(p['hr'], str(mine['id']), {'name': 'Degrees v2', 'shared': False, 'definition': {**d, 'columns': ['emp_code']}})
    assert upd['name'] == 'Degrees v2'
    service.delete_saved(p['hr'], str(mine['id']))
    assert 'Degrees v2' not in [x['name'] for x in service.list_saved(p['hr'])]
    with pytest.raises(ValidationError):
        service.create_saved(p['hr'], {'name': '', 'definition': d})


# ---------------------------------------------------------------- attendance reports

def test_attendance_reports_answer_for_hr_and_are_refused_to_employees(p):
    assert service.attendance_report(p['hr'], 'headcount', {}).rows
    with pytest.raises(ForbiddenError):
        service.attendance_report(p['sita_user'], 'headcount', {})


def test_daily_register_ot_late_and_exceptions(p):
    from hajiri.attendance.service import recompute_day
    insert_punch(p['sita'], '2026-09-21', '10:17')
    insert_punch(p['sita'], '2026-09-21', '18:40')
    recompute_day(p['sita'], '2026-09-21')
    daily = service.attendance_report(p['hr'], 'daily', {'date': '2026-09-21'})
    sita = next(r for r in daily.rows if r['emp_code'] == 'E001')
    assert (sita['status'], sita['arrival'], sita['departure'], sita['worked_hours']) == ('Present', '10:17', '18:40', '8.38')
    assert sita['flags'] == '[{"kind":"Late","minutes":2}]'
    assert next(r for r in daily.rows if r['emp_code'] == 'E002')['status'] == 'NotComputed'
    reg = service.attendance_report(p['hr'], 'register', {'bsYear': '2083', 'bsMonth': '6', 'employeeId': p['sita']})
    assert len(reg.rows) == 31 and reg.filename == 'register-2083-06'
    day = next(r for r in reg.rows if r['ad_date'] == '2026-09-21')
    assert (day['bs_date'], day['status'], day['late_min']) == ('2083-06-05', 'Present', 2)
    late = service.attendance_report(p['hr'], 'late-early', {'from': '2026-09-01', 'to': '2026-09-30'})
    assert late.rows[0]['late_days'] == 1
    ex = service.attendance_report(p['hr'], 'exceptions-summary', {'from': '2026-09-21', 'to': '2026-09-21'})
    assert ex.rows[0]['late'] == 1
    assert service.attendance_report(p['hr'], 'ot', {'from': '2026-09-01', 'to': '2026-09-30'}).rows == []


def test_manager_scope_and_hr_only_reports(p):
    from hajiri.attendance.service import recompute_day
    insert_punch(p['sita'], '2026-09-21', '10:00')
    recompute_day(p['sita'], '2026-09-21')
    rows_ = service.attendance_report(p['ram_user'], 'daily', {'date': '2026-09-21'}).rows
    assert sorted(r['emp_code'] for r in rows_) == ['E001', 'E002']
    for name in ('leave-balances', 'devices'):
        with pytest.raises(ForbiddenError):
            service.attendance_report(p['ram_user'], name, {})
    assert service.attendance_report(p['hr'], 'devices', {}).rows[0]['serial_no'] == 'DEMO-01'
    lb = service.attendance_report(p['hr'], 'leave-balances', {})
    assert {r['leave_type'] for r in lb.rows} >= {'HOME', 'SICK', 'CASUAL'}


def test_punch_audit_is_tenant_scoped(p):
    insert_punch(p['sita'], '2026-09-21', '10:00')
    r = service.attendance_report(p['hr'], 'punch-audit', {'employeeId': p['sita'], 'from': '2026-09-01', 'to': '2026-09-30'})
    assert [x['time'] for x in r.rows] == ['10:00']
    with pytest.raises(NotFoundError):
        service.attendance_report(p['foreign'], 'punch-audit', {'employeeId': p['sita'], 'from': '2026-09-01', 'to': '2026-09-30'})


# ---------------------------------------------------------------- CSV

def test_neutralises_spreadsheet_formula_injection_in_csv_cells():
    csv = to_csv([Column('a', 'A')], [{'a': '=HYPERLINK("http://evil")'}, {'a': '+1'}, {'a': '-2+cmd|x'}, {'a': '@x'}, {'a': 'safe'}, {'a': '-2.50'}])
    assert '"\'=HYPERLINK(""http://evil"")"' in csv
    assert "'+1" in csv
    assert "'-2+cmd|x" in csv
    assert '\r\n-2.50\r\n' in csv  # a plain negative number stays a number
    assert "'@x" in csv
    assert '\r\nsafe\r\n' in csv
    assert csv.startswith('﻿A\r\n')
    assert csv_cell(None) == '' and csv_cell(True) == 'true' and csv_cell('a,b') == '"a,b"' and csv_cell('\tx') == "'\tx"


def test_numbers_round_like_to_fixed():
    """Hours, leave liability and statistics shares use Number.prototype.toFixed, which rounds exact ties up."""
    from decimal import Decimal
    from hajiri.reports.csv import _to_fixed, hours
    assert hours(7.5) == '0.13' and hours(Decimal('7.5')) == '0.13'
    assert hours(None) == '0.00' and hours(0) == '0.00' and hours(90) == '1.50'
    assert _to_fixed(6.25, 1) == '6.3' and _to_fixed(1.005, 2) == '1.00' and _to_fixed(-0.125, 2) == '-0.13'
