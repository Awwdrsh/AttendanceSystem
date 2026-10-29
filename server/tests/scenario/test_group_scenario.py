"""
A holding group's year in miniature, walked end to end through the JSON API and the pages, as the group's HR head
would do it, and every group figure checked against the company's own pages.

    Himalaya Holdings (HOLD)   the parent; the chair (Laxmi) is its admin and starts the group
    Ilam Tea Estates (TEA)     added by the chair from /group
    Mustang Treks (TREK)       added by the chair from /group

1.  The chair adds the two sister companies and makes Hari, an account with no employee record, the group's HR head
    (group_admin + group_hr). Hari's one sign-in now works in all three companies.
2.  Hari sets the policies once in the holding company (a day shift, a wellness leave type, a group holiday), copies
    them to the other two (preview first, then apply) and sees them in each company's own lists.
3.  Hari hires two people into each company, with each company's own copy of the shift, a salary assignment and the
    master data payroll needs. Maya manages in the holding company and runs the tea estate's field office too: Hari
    gives her access to the tea estate as a manager with her employee record there.
4.  Last BS month's attendance is uploaded per company (a CSV of in and out times, with absences and late arrivals)
    and recomputed; payroll is run, reviewed, approved and locked in each company.
5.  Nima moves from the trekking company to the tea estate from the first day of this month (salary carried).
6.  Leave: Maya approves leave in both of her companies with one sign-in, switching between them; one request in the
    trekking company is left waiting.
7.  The group dashboard, the consolidated reports and the people directory are read and every number is compared
    with the per-company pages (employees, payroll runs, Insights, approvals inbox) and with what the scenario
    put in.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.test import Client

from hajiri.attendance import api as attendance_api
from hajiri.company_settings.seed import create_company
from hajiri.core import auth
from hajiri.core.clock import npt_today
from hajiri.db.models import AttendanceDay, Company, Employee, EmployeeExit, LeaveRequest
from hajiri.group import services as group_services
from tests.web.factories import PW, make_employee, make_user, sign_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_dashboard(settings):
    settings.GROUP_DASHBOARD_CACHE_SECONDS = 0


# ---------------------------------------------------------------- a JSON client for one sign-in

class Api:
    """One signed-in session (Bearer token) of a person; `switch` changes the session's active company."""

    def __init__(self, email: str):
        self.token = auth.login(email, PW).token
        self.client = Client()

    def _h(self):
        return {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def call(self, method: str, path: str, body=None, expect: int | tuple = (200, 201), query=None):
        if method == 'get':
            r = self.client.get(path, query or {}, **self._h())
        else:
            r = getattr(self.client, method)(path, json.dumps(body if body is not None else {}),
                                             content_type='application/json', **self._h())
        expected = expect if isinstance(expect, tuple) else (expect,)
        assert r.status_code in expected, f'{method.upper()} {path} -> {r.status_code}: {r.content[:600]!r}'
        ct = r.headers.get('Content-Type', '')
        return r.json() if 'json' in ct else r

    def get(self, path, expect=200, **query):
        return self.call('get', path, expect=expect, query=query)

    def post(self, path, body=None, status=(200, 201)):
        return self.call('post', path, body, status)

    def put(self, path, body=None, status=200):
        return self.call('put', path, body, status)

    def switch(self, company) -> dict:
        return self.post('/api/v1/me/companies/switch', {'companyId': str(company.id)}, status=200)

    @property
    def user(self) -> auth.AuthUser:
        return auth.resolve_session(self.token)


def working_days(m: group_services.Month, weekly_off=(5,)) -> list[date]:
    """The days of a BS month that are not the weekly off (Python weekday 5 = Saturday, Hajiri's default)."""
    d, out = m.start, []
    while d <= m.end:
        if d.weekday() not in weekly_off:
            out.append(d)
        d += timedelta(days=1)
    return out


def next_working_day(d: date) -> date:
    while d.weekday() == 5:
        d += timedelta(days=1)
    return d


def row_of(rows, **match):
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    assert len(hits) == 1, f'expected one row with {match}, got {hits}'
    return hits[0]


def dec(v) -> Decimal:
    return Decimal(str(v))


# ---------------------------------------------------------------- the scenario

def test_a_holding_group_runs_three_companies_from_one_sign_in():
    today = npt_today()
    this = group_services.current_month(today)
    prev = group_services.month(*group_services._prev(this.year, this.month))
    joined = prev.start - timedelta(days=90)

    # ---- 1. The chair starts the group with two sister companies and names the group HR head.
    hold = Company.objects.get(pk=create_company('HOLD', 'Himalaya Holdings')['id'])
    make_user(hold, roles=['admin'], email='laxmi@himalaya.test', name='Laxmi Chair')
    chair = Api('laxmi@himalaya.test')
    tea_id = chair.post('/api/v1/group/companies', {'code': 'TEA', 'nameEn': 'Ilam Tea Estates'}, status=201)['id']
    trek_id = chair.post('/api/v1/group/companies', {'code': 'TREK', 'nameEn': 'Mustang Treks', 'pan': '600000003'},
                         status=201)['id']
    tea, trek = Company.objects.get(pk=tea_id), Company.objects.get(pk=trek_id)
    hold.refresh_from_db()
    assert hold.group_id and tea.group_id == hold.group_id == trek.group_id

    make_user(hold, roles=['employee'], email='hari@himalaya.test', name='Hari HR Head')
    chair.post('/api/v1/group/roles', {'email': 'hari@himalaya.test', 'roles': ['group_admin', 'group_hr']}, status=201)
    hari = Api('hari@himalaya.test')
    mine = hari.get('/api/v1/me/companies')
    assert sorted(c['code'] for c in mine['companies']) == ['HOLD', 'TEA', 'TREK']
    assert set(mine['groupRoles']) == {'group_admin', 'group_hr'}
    for c in mine['companies']:
        assert {'admin', 'hr'} <= set(c['roles']), c

    # ---- 2. Policies set once in the holding company, copied to the others.
    hari.switch(hold)
    holiday = next_working_day(today + timedelta(days=20))
    hari.post('/api/v1/shifts', {'code': 'DAY', 'nameEn': 'Day shift', 'start': '09:00', 'end': '17:00',
                                 'graceInMin': 10}, status=201)
    hari.post('/api/v1/leave/types', {'code': 'WELL', 'nameEn': 'Wellness leave', 'paid': True,
                                      'accrualBasis': 'none', 'carryForward': False}, status=201)
    hari.post('/api/v1/shifts/holidays', {'localDate': holiday.isoformat(), 'nameEn': 'Group foundation day'}, status=201)
    copy = {'sourceCompanyId': str(hold.id), 'targetCompanyIds': [tea_id, trek_id],
            'sections': ['shifts', 'leave_types', 'holidays']}
    preview = hari.post('/api/v1/group/policies/preview', copy, status=200)
    for t in preview['targets']:
        actions = {(s['key'], i['key']): i['action'] for s in t['sections'] for i in s['items']}
        assert actions[('shifts', 'DAY')] == 'create'
        assert actions[('leave_types', 'WELL')] == 'create'
        assert sum(1 for (sec, _), a in actions.items() if sec == 'holidays' and a == 'create') == 1
        assert actions[('leave_types', 'HOME')] == 'same'          # the statutory defaults already match
    applied = hari.post('/api/v1/group/policies/apply', copy, status=200)
    assert applied['totals']['create'] == preview['totals']['create'] == 6
    again = hari.post('/api/v1/group/policies/preview', copy, status=200)
    assert again['totals']['create'] == 0                           # copying twice changes nothing

    shift_of, well_of = {}, {}
    for c in (hold, tea, trek):
        hari.switch(c)
        shifts = hari.get('/api/v1/shifts')
        shifts = shifts.get('shifts', shifts) if isinstance(shifts, dict) else shifts
        day = [s for s in shifts if s['code'] == 'DAY']
        assert len(day) == 1 and str(day[0].get('start_time', day[0].get('start')))[:5] == '09:00', day
        shift_of[c.code] = day[0]['id']
        types = hari.get('/api/v1/leave/types')
        types = types.get('types', types) if isinstance(types, dict) else types
        well_of[c.code] = next(t['id'] for t in types if t['code'] == 'WELL')
        hols = hari.get('/api/v1/shifts/holidays', **{'from': today.isoformat()})
        hols = hols.get('holidays', hols) if isinstance(hols, dict) else hols
        assert [h['name_en'] for h in hols if h['local_date'] == holiday.isoformat()] == ['Group foundation day']
    assert len(set(shift_of.values())) == 3                         # each company has its own copy

    # ---- 3. Hiring into each company, a manager across two companies, salaries.
    people = {}         # code -> employee id
    pans = iter(f'7000000{n:02d}' for n in range(10, 99))

    def hire(company, code, name, basic, **extra):
        hari.switch(company)
        body = {'empCode': code, 'nameEn': name, 'joinDate': joined.isoformat(), 'shiftId': shift_of[company.code],
                'pan': next(pans), 'bankCode': 'NMB', 'bankBranch': 'Kathmandu', 'bankAccount': f'01234567{code[-3:]}',
                'ssfId': f'2{code[-3:]}0000000', 'gender': 'female', 'designation': 'Officer', **extra}
        eid = hari.post('/api/v1/employees', body, status=201)['id']
        people[code] = eid
        structures = hari.get('/api/v1/payroll/structures')['structures']
        version = structures[0]['versions'][0]['id']
        hari.post('/api/v1/payroll/assignments', {'employeeId': eid, 'structureVersionId': version,
                                                  'from': joined.isoformat(),
                                                  'baseAmounts': {'BASIC': f'{basic}.00', 'ALLOWANCE': '10000.00'}},
                  status=201)
        return eid

    maya_h = hire(hold, 'H-001', 'Maya Gurung', 60000, designation='Operations manager')
    hire(hold, 'H-002', 'Sita Shrestha', 40000, managerId=maya_h)
    maya_t = hire(tea, 'T-900', 'Maya Gurung', 20000, designation='Field office manager')
    hire(tea, 'T-001', 'Pema Lama', 30000, managerId=maya_t)
    hire(tea, 'T-002', 'Bikash Rai', 25000, managerId=maya_t)
    hire(trek, 'K-001', 'Nima Sherpa', 45000)
    hire(trek, 'K-002', 'Dawa Tamang', 35000)

    make_user(hold, roles=['employee', 'manager'], employee=Employee.objects.get(pk=maya_h), email='maya@himalaya.test')
    hari.switch(hold)
    hari.post('/api/v1/group/access', {'email': 'maya@himalaya.test', 'companyId': tea_id,
                                       'roles': ['manager', 'employee'], 'employeeId': maya_t}, status=(200, 201))
    for code, comp in (('H-002', hold), ('T-001', tea), ('K-002', trek)):
        make_user(comp, roles=['employee'], employee=Employee.objects.get(pk=people[code]),
                  email=f'{code.lower()}@himalaya.test')
    maya = Api('maya@himalaya.test')
    assert sorted((c['code'], tuple(sorted(c['roles']))) for c in maya.get('/api/v1/me/companies')['companies']) == [
        ('HOLD', ('employee', 'manager')), ('TEA', ('employee', 'manager'))]

    # ---- 4. Last month's attendance per company, then payroll.
    days = working_days(prev)
    absent = {'H-002': days[3:5], 'K-002': days[7:8]}                 # whole days with no punch
    late = {'T-002': days[0:3]}                                       # in at 09:30, 20 minutes after grace
    for company, codes in ((hold, ('H-001', 'H-002')), (tea, ('T-900', 'T-001', 'T-002')), (trek, ('K-001', 'K-002'))):
        hari.switch(company)
        lines = ['emp_code,date_ad,in_time,out_time,note']
        for code in codes:
            for d in days:
                if d in absent.get(code, ()):
                    continue
                lines.append(f"{code},{d.isoformat()},{'09:30' if d in late.get(code, ()) else '08:55'},17:05,")
        csv = '\n'.join(lines) + '\n'
        check = hari.post('/api/v1/attendance-tools/upload/check', {'csv': csv}, status=200)
        assert check['invalid'] == 0, check['rows'][:3]
        up = hari.post('/api/v1/attendance-tools/upload/import', {'csv': csv, 'reason': 'Monthly sheet from the site office'},
                       status=(200, 201))
        assert up['punches'] == 2 * (len(lines) - 1)
        # Days without a punch become Absent (or WeeklyOff) once the month is recomputed.
        for code in codes:
            r = attendance_api.recompute_range(hari.user, {'employeeIds': [people[code]], 'from': prev.start.isoformat(),
                                                           'to': prev.end.isoformat()})
            assert r['recomputed'] == (prev.end - prev.start).days + 1

    runs = {}
    for company in (hold, tea, trek):
        hari.switch(company)
        period = hari.post('/api/v1/payroll/periods', {'bsYear': prev.year, 'bsMonth': prev.month}, status=201)['period']
        run_id = hari.post('/api/v1/payroll/runs', {'periodId': period['id']}, status=201)['id']
        hari.post(f'/api/v1/payroll/runs/{run_id}/lock-attendance', status=200)
        pf = hari.post(f'/api/v1/payroll/runs/{run_id}/preflight', status=200)
        assert pf['ok'] is True, pf['blockers']
        hari.post(f'/api/v1/payroll/runs/{run_id}/compute', status=200)
        hari.post(f'/api/v1/payroll/runs/{run_id}/review', {'notes': 'checked against the site sheets'}, status=200)
        hari.post(f'/api/v1/payroll/runs/{run_id}/approve', status=200)
        hari.post(f'/api/v1/payroll/runs/{run_id}/lock', status=200)
        runs[company.code] = hari.get(f'/api/v1/payroll/runs/{run_id}')
        assert runs[company.code]['run']['status'] == 'locked'
    slips = {c: {s['employee_id']: s for s in r['payslips']} for c, r in runs.items()}
    assert len(slips['HOLD']) == 2 and len(slips['TEA']) == 3 and len(slips['TREK']) == 2
    # Two unpaid absences cut Sita's pay; Pema, with full attendance, is paid in full.
    assert dec(slips['HOLD'][people['H-002']]['lop_days']) == 2
    assert dec(slips['TEA'][people['T-001']]['lop_days']) == 0
    assert dec(slips['TREK'][people['K-002']]['lop_days']) == 1

    # ---- 5. Nima moves from the trekking company to the tea estate from the first day of this month.
    hari.switch(trek)
    moved = hari.post('/api/v1/group/transfers', {'employeeId': people['K-001'], 'toCompanyId': tea_id,
                                                  'effectiveDate': this.start.isoformat(), 'empCode': 'T-003',
                                                  'carrySalary': True, 'note': 'Tea estate guest house'},
                      status=(200, 201))
    nima_t = moved['toEmployeeId']
    assert Employee.objects.get(pk=nima_t).company_id == tea.id
    assert EmployeeExit.objects.get(employee_id=people['K-001']).kind == EmployeeExit.Kind.TRANSFER
    assert Employee.objects.get(pk=people['K-001']).exit_date == this.start - timedelta(days=1)

    # ---- 6. Leave: Maya approves in both of her companies with one sign-in.
    leave_day = next_working_day(today)
    for comp, code in ((hold, 'H-002'), (tea, 'T-001'), (trek, 'K-002')):
        hari.switch(comp)
        hari.post('/api/v1/leave/adjust', {'employeeId': people[code], 'leaveTypeId': well_of[comp.code],
                                           'deltaDays': 5, 'reason': 'Wellness days for the year'}, status=(200, 201))
    requested = {}
    for code in ('H-002', 'T-001', 'K-002'):
        me = Api(f'{code.lower()}@himalaya.test')
        comp = {'H': hold, 'T': tea, 'K': trek}[code[0]]
        requested[code] = me.post('/api/v1/leave/requests', {'leaveTypeId': well_of[comp.code], 'from': leave_day.isoformat(),
                                                             'to': leave_day.isoformat(), 'reason': 'Rest day'},
                                  status=201)['id']

    maya.switch(hold)
    inbox_h = maya.get('/api/v1/workflow/inbox')
    inbox_h = inbox_h.get('items', inbox_h) if isinstance(inbox_h, dict) else inbox_h
    assert len(inbox_h) == 1, inbox_h
    # The tea estate's request cannot be decided while she works in the holding company.
    tea_wf = str(LeaveRequest.objects.get(pk=requested['T-001']).workflow_instance_id)
    maya.post(f'/api/v1/workflow/{tea_wf}/approve', {}, status=(403, 404))
    maya.post(f"/api/v1/workflow/{inbox_h[0]['instanceId']}/approve", {}, status=200)
    maya.switch(tea)
    inbox_t = maya.get('/api/v1/workflow/inbox')
    inbox_t = inbox_t.get('items', inbox_t) if isinstance(inbox_t, dict) else inbox_t
    assert [str(i['instanceId']) for i in inbox_t] == [tea_wf]
    maya.post(f'/api/v1/workflow/{tea_wf}/approve', {}, status=200)
    assert LeaveRequest.objects.get(pk=requested['H-002']).status == LeaveRequest.Status.APPROVED
    assert LeaveRequest.objects.get(pk=requested['T-001']).status == LeaveRequest.Status.APPROVED
    assert LeaveRequest.objects.get(pk=requested['K-002']).status == LeaveRequest.Status.PENDING

    # ---- 7. The group dashboard and reports against each company's own pages.
    hari.switch(hold)
    dash = hari.get('/api/v1/group/dashboard')
    by_code = {r['code']: r for r in dash['companies']}
    assert set(by_code) == {'HOLD', 'TEA', 'TREK'}

    per_company = {}
    for comp in (hold, tea, trek):
        hari.switch(comp)
        emps = hari.get('/api/v1/employees', status='active', pageSize=100)
        insights = hari.get('/api/v1/insights', **{'from': prev.key, 'to': this.key})
        pending = hari.get('/api/v1/workflow/inbox')
        pending = len(pending.get('items', pending) if isinstance(pending, dict) else pending)
        per_company[comp.code] = {'employees': emps, 'insights': insights, 'pending': pending}

    expected_headcount = {'HOLD': 2, 'TEA': 4, 'TREK': 1}            # Nima now counts at the tea estate
    for code, row in by_code.items():
        pc = per_company[code]
        # The employee list still shows Nima at Trek (his exit waits for clearance), but past his last working day.
        employed = [e for e in pc['employees']['employees'] if not e['exit_date'] or e['exit_date'] >= today.isoformat()]
        assert row['headcount'] == expected_headcount[code] == len(employed), (code, row, pc['employees'])
        hc_this = row_of(pc['insights']['headcount'], month=this.key)
        assert row['headcount'] == hc_this['headcount']
        assert row['joiners'] == hc_this['joins'] and row['leavers'] == hc_this['exits']
        assert row['pendingApprovals'] == pc['pending']
        run = runs[code]
        assert row['payroll']['runId'] == run['run']['id'] and row['payroll']['payslips'] == len(run['payslips'])
        assert dec(row['payroll']['gross']) == sum(dec(s['gross']) for s in run['payslips'])
        assert dec(row['payroll']['net']) == sum(dec(s['net']) for s in run['payslips'])
        cost_prev = row_of(pc['insights']['cost'], month=prev.key)
        assert dec(row['payroll']['gross']) == dec(cost_prev['gross'])
        assert dec(row['payroll']['cost']) == dec(cost_prev['totalCost'])
    assert by_code['TEA']['joiners'] == 1 and by_code['TREK']['leavers'] == 0      # Nima left Trek last month
    assert dash['totals']['headcount'] == 7
    # Only Dawa's request is still waiting (he has no manager, so it is HR's); the pay runs were approved.
    assert [by_code[c]['pendingApprovals'] for c in ('HOLD', 'TEA', 'TREK')] == [0, 0, 1]
    assert dec(dash['totals']['payrollGross']) == sum(dec(s['gross']) for r in runs.values() for s in r['payslips'])
    leave_on_day = (by_code['HOLD']['onLeave'], by_code['TEA']['onLeave'])
    assert leave_on_day == ((1, 1) if leave_day == today else (0, 0))

    hari.switch(hold)

    def report(kind, **q):
        return hari.get(f'/api/v1/group/reports/{kind}', **q)['rows']

    # Payroll summary for last month: per company equal to the run's payslips, and the group total their sum.
    pay = report('payroll', bsYear=prev.year, bsMonth=prev.month)
    for code, run in runs.items():
        r = row_of(pay, code=code)
        assert r['payslips'] == len(run['payslips'])
        for col, field in (('gross', 'gross'), ('net', 'net'), ('tds', 'tds'), ('ssf_employee', 'employee_statutory'),
                           ('ssf_employer', 'employer_statutory')):
            assert dec(r[col]) == sum(dec(s[field]) for s in run['payslips']), (code, col)
    total = row_of(pay, company='Group total')
    for col in ('gross', 'net', 'tds', 'ssf_employee', 'ssf_employer', 'cost'):
        assert dec(total[col]) == sum(dec(row_of(pay, code=c)[col]) for c in runs), col

    # Attendance summary for last month: what was uploaded, and what each company's insights show.
    att = report('attendance', bsYear=prev.year, bsMonth=prev.month)
    n = len(days)
    expected_att = {'HOLD': (2 * n, 2 * n - 2, 2, 0), 'TEA': (3 * n, 3 * n, 0, 3), 'TREK': (2 * n, 2 * n - 1, 1, 0)}
    for code, (scheduled, present, absent_days, late_days) in expected_att.items():
        r = row_of(att, code=code)
        assert (r['scheduled'], r['present'], r['absent'], r['late']) == (scheduled, present, absent_days, late_days), (code, r)
        ins = row_of(per_company[code]['insights']['attendance'], month=prev.key)
        assert (ins['scheduledDays'], ins['presentDays'], ins['absentDays'], ins['lateDays']) == (
            scheduled, present, absent_days, late_days), (code, ins)
    assert row_of(att, company='Group total')['present'] == sum(e[1] for e in expected_att.values())

    # Headcount for this month: the transfer is a joiner at the tea estate; the group total is the sum.
    hc = report('headcount')
    for code in ('HOLD', 'TEA', 'TREK'):
        r = row_of(hc, code=code)
        ins = row_of(per_company[code]['insights']['headcount'], month=this.key)
        assert (r['end'], r['joiners'], r['leavers']) == (ins['headcount'], ins['joins'], ins['exits'])
    assert row_of(hc, company='Group total')['end'] == 7
    hc_prev = report('headcount', bsYear=prev.year, bsMonth=prev.month)
    assert row_of(hc_prev, code='TREK')['leavers'] == 1             # Nima's last day at Trek was last month

    # Leave this month: the approved wellness day per company, as in each company's ledger.
    lv = report('leave')
    for code, taken in (('HOLD', 1), ('TEA', 1), ('TREK', 0)):
        r = row_of(lv, code=code, leave_code='WELL')
        assert r['taken'] == (taken if this.start <= leave_day <= this.end else 0), (code, r)
        assert r['balance'] == 5 - (taken if leave_day <= this.end else 0)

    # The people directory lists everyone of the three companies, and nobody of another group.
    outsider = Company.objects.get(pk=create_company('OUT', 'Outside Ltd')['id'])
    make_employee(outsider, name='Not In The Group')
    c = Client()
    sign_in(c, 'hari@himalaya.test')
    page = c.get('/api/v1/group/people', {'pageSize': 100})
    assert page.status_code == 200
    html = page.content.decode()
    for name in ('Maya Gurung', 'Sita Shrestha', 'Pema Lama', 'Bikash Rai', 'Nima Sherpa', 'Dawa Tamang'):
        assert name in html, name
    assert 'Not In The Group' not in html
    overview = c.get('/api/v1/group/dashboard').content.decode()
    for name in ('Himalaya Holdings', 'Ilam Tea Estates', 'Mustang Treks'):
        assert name in overview
    assert 'Outside Ltd' not in overview
    # The payroll summary downloaded as CSV (the Group hub's Reports tab) has the same gross as the JSON report.
    import csv
    import io
    got = c.get('/api/v1/group/reports/payroll', {'bsYear': prev.year, 'bsMonth': prev.month, 'format': 'csv'})
    assert got.status_code == 200 and 'csv' in got['Content-Type']
    table = list(csv.DictReader(io.StringIO(got.content.decode('utf-8-sig'))))
    by_name = {r['Company']: r for r in table}
    for code, name in (('HOLD', 'Himalaya Holdings'), ('TEA', 'Ilam Tea Estates'), ('TREK', 'Mustang Treks')):
        assert dec(by_name[name]['Gross']) == dec(row_of(pay, code=code)['gross'])
    assert dec(by_name['Group total']['Gross']) == dec(total['gross'])

    # Maya's one sign-in shows each company's own team, never the other one's.
    maya.switch(hold)
    team_h = {e['emp_code'] for e in maya.get('/api/v1/employees')['employees']}
    maya.switch(tea)
    team_t = {e['emp_code'] for e in maya.get('/api/v1/employees')['employees']}
    assert 'H-002' in team_h and not team_h & {'T-001', 'T-002'}
    assert {'T-001', 'T-002'} <= team_t and 'H-002' not in team_t
    # Maya has no group role: no group dashboard.
    maya.get('/api/v1/group/dashboard', expect=403)
    # An attendance day of the holding company is not found from the tea estate.
    sita_day = AttendanceDay.objects.filter(employee_id=people['H-002']).values_list('local_date', flat=True).first()
    with pytest.raises(Exception) as e:
        attendance_api.days(maya.user, {'employeeId': people['H-002'], 'from': sita_day.isoformat(),
                                        'to': sita_day.isoformat()})
    assert type(e.value).__name__ in ('NotFoundError', 'ForbiddenError')
