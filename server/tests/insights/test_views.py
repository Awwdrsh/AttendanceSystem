"""
The Insights page and the Reports hub through django.test.Client: the insights cases of engage.test.ts and
roles-flows.test.ts, and the People reports / report builder cases of features-reports.test.ts, run through the pages
that replace the React screens (InsightsPage, ReportsPage, ReportBuilderPage).
"""
import json
from urllib.parse import urlencode

import pytest

from hajiri.core import auth
from hajiri.core.clock import npt_today
from tests.support.sql import execute, one
from tests.core.fixtures import PW, Login
from tests.insights.helpers import client_for, engage_world
from tests.web.factories import htmx, hx_triggers, toasts

HX = htmx()


def hx_target(t):
    return {**htmx(), 'HTTP_HX_TARGET': t}


# ================================================================ insights

@pytest.fixture
def w(demo, login_as):
    return engage_world(demo, login_as)


def test_insights_page_is_for_hr_and_executives(w, login_as):
    r = client_for(w['hr']).get('/insights')
    assert r.status_code == 200
    body = r.content.decode()
    for text in ('Insights', 'Who works here', 'Attrition a year', 'Headcount', 'People cost by department', 'Joiners and leavers'):
        assert text in body
    assert r.context['d']['months'] and len(r.context['stats']) == 9
    assert client_for(login_as(['executive'])).get('/insights').status_code == 200
    assert client_for(w['ram']).get('/insights', **HX).status_code == 403
    assert client_for(login_as(['payroll_admin'])).get('/insights', **HX).status_code == 403
    assert client_for(w['sita']).get('/_/insights/export/headcount', **HX).status_code == 403


def test_insights_filters_reload_the_body_and_refuse_another_companys_department(w):
    c = client_for(w['hr'])
    r = c.get('/insights', {'orgUnitId': w['dept_a'], 'range': '6'}, **hx_target('insights-body'))
    assert r.status_code == 200
    assert '<html' not in r.content.decode()
    assert len(r.context['d']['months']) == 6
    assert [d['label'] for d in r.context['d']['departments']] == ['Engineering']
    bad = client_for(w['foreign']).get('/insights', {'orgUnitId': w['dept_a']})
    assert bad.status_code == 400 and 'unknown department' in bad.content.decode()
    # A range other than 6/12/24 falls back to 12 months.
    assert len(c.get('/insights', {'range': '99'}).context['d']['months']) == 12


def test_insights_kpi_hints_and_tones(w):
    from hajiri.insights.views import kpi_stats
    d = {'months': [{'label': 'Ashwin 2083'}, {'label': 'Kartik 2083'}], 'kpis': [
        {'key': 'exits', 'label': 'Left', 'value': 3, 'delta': 2, 'unit': '', 'goodWhen': 'down'},
        {'key': 'late', 'label': 'Late arrivals', 'value': 4.5, 'delta': -1.5, 'unit': '%', 'goodWhen': 'down'},
        {'key': 'cost', 'label': 'People cost, Kartik', 'value': 150000, 'delta': 5000, 'unit': 'NPR', 'goodWhen': 'neutral', 'vs': 'Ashwin'},
        {'key': 'hours', 'label': 'Average day', 'value': None, 'delta': None, 'unit': 'h', 'goodWhen': 'neutral'},
    ]}
    s = kpi_stats(d)
    assert s[0] == {'key': 'exits', 'label': 'Left', 'value': '3', 'hint': '+2 vs Ashwin', 'tone': 'warn'}
    assert s[1] == {'key': 'late', 'label': 'Late arrivals', 'value': '4.5%', 'hint': '−1.5 pts vs Ashwin', 'tone': 'ok'}
    assert s[2] == {'key': 'cost', 'label': 'People cost, Kartik', 'value': 'NPR 1,50,000', 'hint': '+NPR 5,000 vs Ashwin'}
    assert s[3] == {'key': 'hours', 'label': 'Average day', 'value': '—'}


def test_insights_exports_each_dataset_as_csv_and_refuses_unknown_names(w):
    c = client_for(w['hr'])
    csv = c.get('/_/insights/export/headcount')
    assert csv.status_code == 200 and 'text/csv' in csv['Content-Type']
    assert 'Headcount at month end' in csv.content.decode()
    assert csv['Content-Disposition'] == 'attachment; filename="insights-headcount.csv"'
    bad = c.get('/_/insights/export/constructor', **HX)
    assert bad.status_code == 400
    page = c.get('/_/insights/export/constructor')
    assert page.status_code == 302 and toasts(page)[0]['title'] == 'Download failed'
    assert c.get('/_/insights/export/attendance').status_code == 200
    # The CSV follows the page filters.
    eng = c.get('/_/insights/export/departments', {'orgUnitId': w['dept_a']}).content.decode()
    assert 'Engineering' in eng and 'Operations' not in eng


# ================================================================ reports

@pytest.fixture
def p(demo, login_as):
    """features-reports.test.ts beforeAll."""
    hr, hr2 = login_as(['hr']), login_as(['hr'])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['employee', 'manager'], demo['ram'])
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    execute("""UPDATE employee SET join_date = '2024-04-13', dob = '1994-10-20', marital_status = 'single',
                 permanent_address = '{"province":"Bagmati","district":"Lalitpur","municipality":"Lalitpur Metropolitan City","ward":5}'
               WHERE id = %s""", [demo['sita']])
    execute("UPDATE employee SET join_date = '2022-01-01', dob = '1985-03-02', passport_expiry = %s::date + 20 WHERE id = %s",
            [npt_today(), demo['ram']])
    execute("INSERT INTO employee_family (employee_id, name, relation, is_nominee, nominee_share) VALUES (%s, 'Gita Sharma', 'mother', true, 100)", [demo['sita']])
    execute("INSERT INTO employee_emergency_contact (employee_id, name, relation, phone) VALUES (%s, 'Hari Sharma', 'father', '9800000000')", [demo['sita']])
    execute("INSERT INTO employee_education (employee_id, level, degree, institution, end_year) VALUES (%s, 'bachelor', 'BBA', 'Tribhuvan University', 2016)", [demo['sita']])
    c = str(one("INSERT INTO company (code, name_en) VALUES ('OTHERF', 'Other Co') RETURNING id")['id'])
    auth.create_user(company_id=c, email='hr@other.features', name='Other HR', password=PW, roles=['hr'])
    tok = auth.login('hr@other.features', PW).token
    foreign = Login(tok, '', auth.resolve_session(tok), 'hr@other.features')
    return {**demo, 'hr': client_for(hr), 'hr2': client_for(hr2), 'sita_c': client_for(sita), 'ram_c': client_for(ram),
            'foreign_c': client_for(foreign)}


def view(c, key, q=None):
    return c.get(f'/_/insights/reports/{key}/view', q or {}, **hx_target('report-result'))


def rows_of(r):
    assert r.status_code == 200, r.content
    return r.context['table']['rows']


def total(r):
    return sum(x['headcount'] for x in rows_of(r))


def test_catalogue_lists_the_reports_for_hr_only(p):
    r = p['hr'].get('/reports')
    assert r.status_code == 200
    body = r.content.decode()
    for text in ('Every report opens on screen or downloads as a CSV file for Excel.', 'Monthly attendance register',
                 'Headcount on a date', 'Record completeness', 'Build a report', 'Open pay runs', 'Search reports'):
        assert text in body
    keys = [e.key for g in r.context['groups'] for e in g['entries']]
    assert 'time' in keys                      # the timesheets module provides its summary
    assert p['sita_c'].get('/reports', **HX).status_code == 403
    assert p['ram_c'].get('/reports', **HX).status_code == 403
    assert p['ram_c'].get('/reports/builder', **HX).status_code == 403


def test_catalogue_hides_switched_off_modules(p, demo):
    execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'features', '{"off": ["time", "hiring"]}')
               ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [demo['company_id']])
    from hajiri.features.service import invalidate_features
    invalidate_features()
    r = p['hr'].get('/reports')
    keys = [e.key for g in r.context['groups'] for e in g['entries']]
    assert 'time' not in keys and 'hiring' not in keys and 'register' in keys
    assert view(p['hr'], 'time').status_code == 404


def test_report_modal_opens_with_its_filters(p):
    r = p['hr'].get('/_/insights/reports/nominees/view', **HX)
    assert r.status_code == 200
    body = r.content.decode()
    assert 'data-modal-title="Nominees"' in body and 'All branches' in body and 'Everyone' in body
    assert r.context['vals'] == {'status': 'active'}
    assert p['hr'].get('/_/insights/reports/nope/view', **HX).status_code == 404
    assert p['hr'].get('/_/insights/reports/register/view', **HX).status_code == 404   # a CSV-only entry


def test_headcount_on_a_date_counts_only_people_employed_then(p):
    now = total(view(p['hr'], 'headcount-on-date'))
    assert now >= 2
    assert total(view(p['hr'], 'headcount-on-date', {'date': '2023-01-01'})) == now - 1


def test_statistics_joiners_birthdays_nominees_contacts_addresses_education(p):
    stats = rows_of(view(p['hr'], 'statistics'))
    assert any(r['dimension'] == 'Province (permanent address)' and r['bucket'] == 'Bagmati' for r in stats)
    jl = rows_of(view(p['hr'], 'joiners-leavers', {'from': '2024-04-01', 'to': '2024-04-30'}))
    assert [r['emp_code'] for r in jl] == ['E001']
    bd = rows_of(view(p['hr'], 'birthdays', {'from': '2026-10-15', 'days': '10'}))
    assert any(r['occasion'] == 'Birthday' and r['emp_code'] == 'E001' and r['years'] == 32 for r in bd)
    nom = rows_of(view(p['hr'], 'nominees'))[0]
    assert (nom['nominee'], nom['share']) == ('Gita Sharma', '100.00')
    assert rows_of(view(p['hr'], 'emergency'))[0]['contact'] == 'Hari Sharma'
    addr = next(r for r in rows_of(view(p['hr'], 'addresses')) if r['emp_code'] == 'E001')
    assert 'Ward 5' in addr['permanent_address']
    ed = rows_of(view(p['hr'], 'education'))[0]
    assert (ed['level'], ed['institution']) == ('Bachelor', 'Tribhuvan University')
    # Dates on screen are in Bikram Sambat.
    assert '1 Baishakh 2081' in view(p['hr'], 'joiners-leavers', {'from': '2024-04-01', 'to': '2024-04-30'}).content.decode()


def test_licence_register_never_shows_id_numbers_and_flags_expiring_documents(p):
    execute("UPDATE employee SET passport_no_enc = '\\x00'::bytea WHERE id = %s", [p['ram']])
    r = view(p['hr'], 'licences')
    row = next(x for x in rows_of(r) if x['emp_code'] == 'E002' and x['document'] == 'Passport')
    assert (row['on_file'], row['state']) == ('Recorded', 'Expiring soon')
    assert 'passport_no' not in r.content.decode() and '\\x00' not in r.content.decode()


def test_record_completeness_lists_what_is_missing_least_complete_first(p):
    out = rows_of(view(p['hr'], 'completeness'))
    assert out[0]['complete'] <= out[-1]['complete']
    assert 'Emergency contact' in next(x for x in out if x['emp_code'] == 'E002')['missing']


def test_report_csv_shows_bs_dates_and_neutralises_formula_cells(p):
    execute("""UPDATE employee_education SET institution = '=HYPERLINK("x")' WHERE employee_id = %s""", [p['sita']])
    csv = p['hr'].get('/_/insights/reports/education/view.csv')
    assert 'text/csv' in csv['Content-Type']
    assert '"\'=HYPERLINK(""x"")"' in csv.content.decode()
    assert csv['Content-Disposition'] == 'attachment; filename="education.csv"'
    jl = p['hr'].get('/_/insights/reports/joiners-leavers/view.csv', {'from': '2024-04-01', 'to': '2024-04-30'})
    assert '1 Baishakh 2081' in jl.content.decode()


def test_report_filters_by_branch_and_status(p):
    other = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'BRT', 'Biratnagar') RETURNING id", [p['company_id']])['id'])
    assert rows_of(view(p['hr'], 'addresses', {'branchId': other})) == []
    assert len(rows_of(view(p['hr'], 'addresses', {'branchId': p['branch_id']}))) >= 2
    assert rows_of(view(p['hr'], 'addresses', {'status': 'exited'})) == []


def test_reports_are_hr_pages_and_other_companies_see_nothing(p):
    assert view(p['ram_c'], 'headcount-on-date').status_code == 403
    assert view(p['sita_c'], 'headcount-on-date').status_code == 403
    assert rows_of(view(p['foreign_c'], 'nominees')) == []


def test_report_bad_input_is_rejected(p):
    r = view(p['hr'], 'joiners-leavers', {'from': '2024-05-01', 'to': '2024-04-01'})
    # TS: the table empties and a toast says why (toast.err(e, 'Could not run the report')).
    assert r.status_code == 422 and r.context['table'] is None
    t = toasts(r)[0]
    assert 'the start date is after the end date' in t['message'] and (t['tone'], t['title']) == ('err', 'Could not run the report')
    # The modal shows the same message inline.
    full = p['hr'].get('/_/insights/reports/joiners-leavers/view', {'from': '2024-05-01', 'to': '2024-04-01'}, **HX)
    assert 'the start date is after the end date' in full.content.decode()
    # Days ahead is kept between 1 and 92, as the number input does.
    assert view(p['hr'], 'birthdays', {'days': '500'}).context['vals']['days'] == '92'
    assert view(p['hr'], 'addresses', {'branchId': 'not-a-uuid'}).status_code == 422


def test_attendance_csv_exports(p):
    c = p['hr']
    r = c.get('/_/insights/reports/devices/csv')
    assert r.status_code == 200 and 'Punches today' in r.content.decode()
    assert r['Content-Disposition'] == 'attachment; filename="devices.csv"'
    reg = c.get('/_/insights/reports/register/csv', {'bs_year': '2083', 'bs_month': '6'})
    assert reg.status_code == 200 and 'Date (BS)' in reg.content.decode()
    assert reg['Content-Disposition'] == 'attachment; filename="register-2083-6.csv"'
    d = c.get('/_/insights/reports/daily/csv', {'date': '2026-10-20'})
    assert 'Arrival' in d.content.decode()
    missing = c.get('/_/insights/reports/ot/csv', {'from': '2026-10-01'})
    assert missing.status_code == 302 and toasts(missing)[0]['message'] == 'Pick the dates first, then export.'
    no_emp = c.get('/_/insights/reports/punches/csv', {'from': '2026-10-01', 'to': '2026-10-20'})
    assert toasts(no_emp)[0]['message'] == 'Pick an employee first, then export.'
    ok = c.get('/_/insights/reports/punches/csv', {'from': '2026-10-01', 'to': '2026-10-20', 'employee_id': p['sita']})
    assert ok.status_code == 200 and ok['Content-Disposition'] == f'attachment; filename="punches-{p["sita"]}.csv"'
    bad = c.get('/_/insights/reports/ot/csv', {'from': 'x', 'to': '2026-10-20'})
    assert bad.status_code == 302 and toasts(bad)[0]['title'] == 'Export failed'
    assert p['sita_c'].get('/_/insights/reports/devices/csv', **HX).status_code == 403


def test_time_report_runs_through_the_timesheets_module(p):
    r = view(p['hr'], 'time', {'from': '2026-10-01', 'to': '2026-10-20', 'groupBy': 'client'})
    assert r.status_code == 200 and r.context['table']['rows'] == []
    assert r.context['table']['columns'][0]['header'] == 'Client'


# ================================================================ report builder

def form(dataset, columns, filters=(), sort=None, include_exited=False, **extra):
    d = {'dataset': dataset, 'columns': list(columns)}
    for i, f in enumerate(filters):
        d[f'f{i}_column'], d[f'f{i}_op'] = f['column'], f['op']
        d[f'f{i}_value'] = ('true' if f['value'] else 'false') if isinstance(f.get('value'), bool) else f.get('value', '')
    if sort:
        d['sort_column'], d['sort_dir'] = sort
    if include_exited:
        d['include_exited'] = '1'
    d.update(extra)
    return d


def run(c, data):
    return c.post('/_/insights/builder/run', data, **HX)


def result_rows(r):
    assert r.status_code == 200, (r.status_code, toasts(r))
    return r.context['result']['rows']


def test_builder_page_lists_datasets_with_whitelisted_columns(p):
    r = p['hr'].get('/reports/builder')
    assert r.status_code == 200
    body = r.content.decode()
    assert 'Make your own list from employee records.' in body and 'Pick your columns and filters, then run' in body
    keys = [d['key'] for d in r.context['datasets']]
    assert {'employees', 'education', 'family', 'experience', 'career_events', 'leave_balances'} <= set(keys)
    assert 'pan' not in [c['key'] for c in r.context['ds']['columns']]
    assert [c['key'] for c in r.context['chosen']] == ['emp_code', 'name', 'designation', 'department', 'branch', 'join_date']


def test_builder_runs_with_columns_filters_and_sort(p):
    r = run(p['hr'], form('employees', ['emp_code', 'name', 'age', 'province'],
                          [{'column': 'province', 'op': 'eq', 'value': 'bagmati'}], sort=('name', 'desc')))
    out = result_rows(r)
    assert [{k: v for k, v in x.items() if k != 'age'} for x in out] == [{'emp_code': 'E001', 'name': 'Sita Sharma', 'province': 'Bagmati'}]
    assert isinstance(out[0]['age'], int)
    assert r.context['result']['total'] == 1 and '1 row' in r.content.decode()
    all_ = result_rows(run(p['hr'], form('employees', ['emp_code'], [{'column': 'join_date', 'op': 'gte', 'value': '2023-01-01'}])))
    assert [x['emp_code'] for x in all_] == ['E001']
    nom = result_rows(run(p['hr'], form('family', ['emp_code', 'member', 'nominee'], [{'column': 'nominee', 'op': 'eq', 'value': True}])))
    assert nom == [{'emp_code': 'E001', 'member': 'Gita Sharma', 'nominee': True}]
    assert result_rows(run(p['hr'], form('education', ['institution'], [{'column': 'institution', 'op': 'contains', 'value': '%'}]))) == []


def test_builder_refuses_anything_outside_the_whitelist(p):
    attempts = [
        form('employees', ['emp_code; DROP TABLE employee']),
        form('employees', ['pan_enc']),
        form('app_user', ['email']),
        form('employees', ['emp_code'], sort=('name_en desc, (select 1)', 'asc')),
        form('employees', ['emp_code'], sort=('name', 'desc; drop')),
        form('employees', ['emp_code'], [{'column': 'name', 'op': 'like', 'value': 'x'}]),
        form('employees', ['emp_code'], [{'column': 'age', 'op': 'gt', 'value': '1 OR 1=1'}]),
        form('employees', ['emp_code'], [{'column': 'dob', 'op': 'lt', 'value': "2020-01-01' OR '1'='1"}]),
        form('employees', []),
    ]
    for a in attempts:
        r = run(p['hr'], a)
        assert r.status_code == 204, a
        t = toasts(r)[0]
        assert (t['tone'], t['title']) == ('err', 'Could not run the report'), a
    quoted = run(p['hr'], form('employees', ['emp_code'], [{'column': 'name', 'op': 'eq', 'value': "x' OR '1'='1"}]))
    assert result_rows(quoted) == []
    assert one('SELECT count(*)::int AS n FROM employee')['n'] > 0


def test_builder_exports_csv_with_bs_dates_and_is_hr_only(p):
    data = form('employees', ['emp_code', 'join_date', 'pan_on_file'])
    r = p['hr'].post('/_/insights/builder/export', data, **HX)
    assert r.status_code == 204 and r['HX-Redirect'].startswith('/_/insights/builder/csv?')
    csv = p['hr'].get(r['HX-Redirect'])
    assert 'text/csv' in csv['Content-Type']
    text = csv.content.decode()
    assert 'Join date' in text and 'E001,1 Baishakh 2081,No' in text
    assert csv['Content-Disposition'] == 'attachment; filename="Employees.csv"'
    bad = p['hr'].post('/_/insights/builder/export', form('employees', ['nope']), **HX)
    assert bad.status_code == 204 and toasts(bad)[0]['title'] == 'Export failed' and 'HX-Redirect' not in bad
    assert run(p['ram_c'], form('employees', ['emp_code'])).status_code == 403
    assert run(p['sita_c'], form('employees', ['emp_code'])).status_code == 403
    assert result_rows(run(p['foreign_c'], form('employees', ['emp_code']))) == []


def test_builder_form_redraws_after_structural_changes(p):
    c = p['hr']
    base = form('employees', ['emp_code', 'name'])
    r = c.post('/_/insights/builder/form', {**base, 'action': 'add_column', 'add_column': 'age'}, **HX)
    assert [x['key'] for x in r.context['chosen']] == ['emp_code', 'name', 'age']
    r = c.post('/_/insights/builder/form', {**base, 'action': 'remove_column:name'}, **HX)
    assert [x['key'] for x in r.context['chosen']] == ['emp_code'] and 'Pick at least one column' not in r.content.decode()
    r = c.post('/_/insights/builder/form', {**form('employees', []), 'action': 'add_filter'}, **HX)
    assert 'Pick at least one column' in r.content.decode()
    assert [(f['column'], f['op']) for f in r.context['filters']] == [('emp_code', 'eq')]
    r = c.post('/_/insights/builder/form', {**form('employees', ['emp_code'], [{'column': 'ot_eligible', 'op': 'contains', 'value': 'x'}]),
                                             'action': 'filter_column:0'}, **HX)
    f = r.context['filters'][0]
    assert (f['op'], f['value'], [o for o, _ in f['ops']]) == ('eq', True, ['eq', 'neq'])
    r = c.post('/_/insights/builder/form', {**form('employees', ['emp_code'], [{'column': 'dob', 'op': 'gt', 'value': ''}]), 'action': 'op'}, **HX)
    assert dict(r.context['filters'][0]['ops'])['gt'] == 'is after'
    r = c.post('/_/insights/builder/form', {**base, 'dataset': 'education', 'action': 'dataset'}, **HX)
    assert r.context['ds']['key'] == 'education' and 'hx-swap-oob' in r.content.decode()
    many = form('employees', ['emp_code'], [{'column': 'name', 'op': 'eq', 'value': str(i)} for i in range(15)])
    r = c.post('/_/insights/builder/form', {**many, 'action': 'add_filter'}, **HX)
    assert len(r.context['filters']) == 15 and r.context['can_add_filter'] is False
    assert 'No filters: every current employee is included.' in c.post('/_/insights/builder/form', {**base, 'action': 'x'}, **HX).content.decode()
    assert 'No filters: every employee is included.' in c.post('/_/insights/builder/form', {**form('employees', ['emp_code'], include_exited=True), 'action': 'x'}, **HX).content.decode()


def save(c, name, definition, *, shared=False, current_id='', as_new=False):
    data = {'name': name, 'definition': json.dumps(definition), 'current_id': current_id}
    if shared:
        data['shared'] = '1'
    if as_new:
        data['as_new'] = '1'
    return c.post('/_/insights/builder/save', data, **HX)


def saved_names(c):
    return [s['name'] for s in c.get('/_/insights/builder/saved', **HX).context['saved']]


def saved_id(c, name):
    return next(s['id'] for s in c.get('/_/insights/builder/saved', **HX).context['saved'] if s['name'] == name)


def open_saved(c, sid):
    return c.get('/reports/builder', {'saved': sid}, **hx_target('builder'))


def test_builder_saves_shares_runs_updates_and_deletes_named_reports(p):
    hr, hr2 = p['hr'], p['hr2']
    d = {'dataset': 'education', 'columns': ['emp_code', 'institution']}
    dlg = hr.get('/_/insights/builder/save?' + urlencode(form('education', ['emp_code', 'institution']), doseq=True), **HX)
    assert 'Save as a new report' in dlg.content.decode() and json.loads(dlg.context['definition'])['columns'] == ['emp_code', 'institution']
    r = save(hr, 'Degrees', d)
    assert r.status_code == 200 and toasts(r)[0]['message'] == '"Degrees" saved'
    assert 'hj:modal-close' in json.dumps(hx_triggers(r)) or hx_triggers(r)
    save(hr, 'Shared degrees', d, shared=True)
    assert save(hr, 'Bad', {'dataset': 'employees', 'columns': ['nope']}).status_code == 200  # validated again when run
    bad = open_saved(hr, saved_id(hr, 'Bad'))
    assert bad.status_code == 200 and toasts(bad)[0]['title'] == 'Could not run the report'

    assert saved_names(hr2) == ['Shared degrees']
    mine, shared = saved_id(hr, 'Degrees'), saved_id(hr, 'Shared degrees')
    assert open_saved(hr2, mine).status_code == 404
    ran = open_saved(hr2, shared)
    assert ran.status_code == 200 and len(ran.context['result']['rows']) > 0
    assert ran.context['save_label'] == 'Save' and 'id="builder-save-label" hx-swap-oob' in ran.content.decode()
    assert hr2.post(f'/_/insights/builder/saved/{shared}/delete', **HX).status_code == 403
    assert open_saved(p['foreign_c'], shared).status_code == 404
    # Someone else's shared report is saved as a new one, never changed.
    save(hr2, 'Mine now', d, current_id=shared)
    assert sorted(saved_names(hr2)) == ['Mine now', 'Shared degrees']

    opened = open_saved(hr, mine)
    assert opened.context['save_label'] == 'Save changes'
    dlg = hr.get('/_/insights/builder/save', {'current_id': mine, **form('education', ['emp_code'])}, **HX)
    assert 'Save as new' in dlg.content.decode() and dlg.context['name'] == 'Degrees'
    upd = save(hr, 'Degrees v2', {**d, 'columns': ['emp_code']}, current_id=mine)
    assert toasts(upd)[0]['message'] == '"Degrees v2" saved'
    assert 'Degrees v2' in saved_names(hr) and 'Degrees' not in saved_names(hr)
    copy = save(hr, 'Degrees copy', d, current_id=mine, as_new=True)
    assert copy.status_code == 200 and {'Degrees v2', 'Degrees copy'} <= set(saved_names(hr))
    gone = hr.post(f'/_/insights/builder/saved/{mine}/delete', {'current_id': mine}, **HX)
    assert gone.status_code == 200 and toasts(gone)[0]['message'] == 'Report deleted'
    assert 'Degrees v2' not in saved_names(hr)
    # The saved row is audited on create and delete.
    acts = [r['action'] for r in __import__('tests.support.sql', fromlist=['rows']).rows(
        "SELECT action FROM audit_log WHERE table_name = 'saved_report' AND row_id = %s ORDER BY id", [mine])]
    assert acts == ['create', 'delete']


def test_builder_export_names_the_file_after_the_saved_report(p):
    hr = p['hr']
    save(hr, 'Degrees', {'dataset': 'education', 'columns': ['emp_code']})
    sid = saved_id(hr, 'Degrees')
    r = hr.post('/_/insights/builder/export', {**form('education', ['emp_code']), 'current_id': sid}, **HX)
    csv = hr.get(r['HX-Redirect'])
    assert csv['Content-Disposition'] == 'attachment; filename="Degrees.csv"'
    assert one("SELECT after FROM audit_log WHERE table_name = 'adhoc_report' AND action = 'export' ORDER BY id DESC LIMIT 1")['after']['dataset'] == 'education'


def test_expense_reports_run_through_the_expenses_module(p):
    r = view(p['hr'], 'spend', {'from': '2026-10-01', 'to': '2026-10-20', 'groupBy': 'department'})
    assert r.status_code == 200 and r.context['table']['columns'][0]['header'] == 'Department'
    assert rows_of(view(p['hr'], 'advances')) == []
    assert rows_of(view(p['hr'], 'pending-pay')) == []
    csv = p['hr'].get('/_/insights/reports/advances/view.csv')
    assert csv['Content-Disposition'] == 'attachment; filename="travel-advances.csv"' and 'Destination' in csv.content.decode()


# ================================================================ audit additions

def test_catalogue_csv_buttons_toast_in_place_and_then_download(p):
    """ReportsPage dl(): missing inputs are a toast.info, a failure is toast.err(e, 'Export failed'); the page stays."""
    c = p['hr']
    missing = c.get('/_/insights/reports/ot/csv', {'from': '2026-10-01'}, **HX)
    assert missing.status_code == 204 and 'HX-Redirect' not in missing
    assert (toasts(missing)[0]['message'], toasts(missing)[0]['tone']) == ('Pick the dates first, then export.', 'info')
    no_emp = c.get('/_/insights/reports/punches/csv', {'from': '2026-10-01', 'to': '2026-10-20'}, **HX)
    assert no_emp.status_code == 204 and toasts(no_emp)[0]['message'] == 'Pick an employee first, then export.'
    bad = c.get('/_/insights/reports/ot/csv', {'from': 'x', 'to': '2026-10-20'}, **HX)
    assert bad.status_code == 204 and (toasts(bad)[0]['tone'], toasts(bad)[0]['title']) == ('err', 'Export failed')
    ok = c.get('/_/insights/reports/ot/csv', {'from': '2026-10-01', 'to': '2026-10-20'}, **HX)
    assert ok.status_code == 204 and ok['HX-Redirect'].startswith('/_/insights/reports/ot/csv?')
    csv = c.get(ok['HX-Redirect'])
    assert csv.status_code == 200 and csv['Content-Disposition'] == 'attachment; filename="ot-2026-10-01_2026-10-20.csv"'
    # The catalogue buttons are wired for this.
    body = c.get('/reports').content.decode()
    assert 'hx-get="/_/insights/reports/ot/csv" hx-swap="none"' in body


def test_report_modal_csv_toasts_in_place_and_then_downloads(p):
    c = p['hr']
    bad = c.get('/_/insights/reports/joiners-leavers/view.csv', {'from': '2024-05-01', 'to': '2024-04-01'}, **HX)
    assert bad.status_code == 204 and (toasts(bad)[0]['title'], toasts(bad)[0]['tone']) == ('Export failed', 'err')
    assert 'the start date is after the end date' in toasts(bad)[0]['message']
    ok = c.get('/_/insights/reports/nominees/view.csv', {'status': 'all'}, **HX)
    assert ok.status_code == 204 and ok['HX-Redirect'] == '/_/insights/reports/nominees/view.csv?status=all'
    csv = c.get(ok['HX-Redirect'])
    assert csv['Content-Disposition'] == 'attachment; filename="nominees.csv"' and 'Gita Sharma' in csv.content.decode()
    # Without HTMX a refused export goes back to the catalogue with the toast.
    page = c.get('/_/insights/reports/joiners-leavers/view.csv', {'from': '2024-05-01', 'to': '2024-04-01'})
    assert page.status_code == 302 and toasts(page)[0]['title'] == 'Export failed'
    modal = c.get('/_/insights/reports/nominees/view', **HX).content.decode()
    assert 'hx-get="/_/insights/reports/nominees/view.csv" hx-include="#report-filters" hx-swap="none"' in modal


def test_builder_save_trims_the_name(p):
    """SaveModal sends name.trim() and description.trim() || null."""
    hr = p['hr']
    r = save(hr, '  Degrees  ', {'dataset': 'education', 'columns': ['emp_code']})
    assert toasts(r)[0]['message'] == '"Degrees" saved'
    s = next(x for x in hr.get('/_/insights/builder/saved', **HX).context['saved'] if x['name'] == 'Degrees')
    assert s['description'] is None
    # A blank name is refused by the service.
    blank = save(hr, '   ', {'dataset': 'education', 'columns': ['emp_code']})
    assert blank.status_code == 400 and toasts(blank)[0]['tone'] == 'err'


def test_insights_error_shows_the_message_and_the_empty_state(w):
    """InsightsPage: an Alert with the error, and with no data the 'No insights yet' empty state."""
    r = client_for(w['foreign']).get('/insights', {'orgUnitId': w['dept_a']})
    body = r.content.decode()
    assert r.status_code == 400 and 'unknown department' in body and 'No insights yet' in body


def test_breaks_and_visits_appear_once_a_module_provides_them(p, monkeypatch):
    """The field CSVs (ReportsPage fieldCsv) are looked up at run time; with a source they show and export."""
    import sys
    import types
    from hajiri.insights import catalog
    seen = {}

    def list_breaks(user, query):
        seen['q'] = dict(query)
        return {'columns': [{'key': 'emp_code', 'header': 'Employee code'}, {'key': 'date', 'header': 'Date', 'type': 'date'}],
                'rows': [{'emp_code': 'E001', 'date': '2024-04-13'}]}
    fake = types.ModuleType('tests_fake_field')
    fake.list_breaks = list_breaks
    monkeypatch.setitem(sys.modules, 'tests_fake_field', fake)
    keys = [e.key for g in p['hr'].get('/reports').context['groups'] for e in g['entries']]
    assert 'breaks' not in keys or catalog.resolve(catalog.CSV_SOURCES['breaks']) is not None
    monkeypatch.setitem(catalog.CSV_SOURCES, 'breaks', ('tests_fake_field', 'list_breaks'))
    monkeypatch.setitem(catalog.CSV_SOURCES, 'visits', ('tests_fake_field', 'missing_fn'))
    keys = [e.key for g in p['hr'].get('/reports').context['groups'] for e in g['entries']]
    assert 'breaks' in keys and 'visits' not in keys
    r = p['hr'].get('/_/insights/reports/breaks/csv', {'from': '2026-10-01', 'to': '2026-10-20'})
    assert r.status_code == 200 and r['Content-Disposition'] == 'attachment; filename="breaks-2026-10-01_2026-10-20.csv"'
    assert seen['q'] == {'format': 'csv', 'from': '2026-10-01', 'to': '2026-10-20'}
    assert 'E001' in r.content.decode()
    assert p['hr'].get('/_/insights/reports/visits/csv', {'from': '2026-10-01', 'to': '2026-10-20'}, **HX).status_code == 404
