"""The main flows end to end in the browser, as people do them (see test_browser.py for the set-up).

Each flow signs in through the login page and uses the pages' own buttons and forms; the API is only read to pick
free dates and to confirm the result. Problems recorded on the way (console errors, failed requests, red alerts)
fail the flow. They run before test_browser.py (file order), so the crawl also sees the records made here (the
locked pay run and the published payslips).
"""
from __future__ import annotations

import datetime
import re
import time

import pytest

from .browse import Tab
from .test_browser import email_of, report


@pytest.fixture
def tab(browser, base_url):
    tabs = []

    def make(label, who=None, users=None):
        t = Tab(browser, base_url, label)
        tabs.append(t)
        if who:
            t.sign_in(email_of(users, who), users['password'])
        return t
    yield make
    for t in tabs:
        t.close()


def ok(t: Tab):
    assert not t.problems, report(t)


def dialog(t: Tab):
    """The open drawer or modal."""
    return t.page.get_by_role('dialog').filter(visible=True).last


def pick_date(t: Tab, scope, label: str, bs: str):
    """A DateField: open it and type the Nepali date."""
    scope.get_by_role('button', name=re.compile(rf'^{re.escape(label)}')).first.click()
    box = t.page.get_by_label('Type a Nepali date').filter(visible=True).last
    box.fill(bs)
    box.press('Enter')


def choose(t: Tab, scope, label: str, option):
    """A Mantine Select: open it and click the option (text or regex)."""
    scope.get_by_label(re.compile(rf'^{re.escape(label)}')).first.click()
    t.page.get_by_role('option', name=option).filter(visible=True).first.click()


def working_days(t: Tab, start: datetime.date, days: int) -> list[dict]:
    cal = t.api_get(f'/api/v1/shifts/calendar?from={start}&to={start + datetime.timedelta(days=days)}')
    return [d for d in cal['days'] if not d['weeklyOff'] and not d['holidays']]


def wait_until(fn, timeout=15.0, every=0.3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(every)
    return fn()


def test_apply_for_leave_and_the_manager_approves(tab, users):
    emp = tab('leave: employee', 'DEMO:employee', users)
    p = emp.page
    mine = emp.api_get('/api/v1/leave/requests') or {'requests': []}
    taken = {r['from_date'] for r in mine['requests'] if r['status'] in ('pending', 'approved')}
    day = next(d for d in working_days(emp, datetime.date.today() + datetime.timedelta(days=10), 60)
               if d['date'] not in taken)
    emp.go('/me/leave')
    emp.where = 'request leave'
    p.get_by_role('button', name='Request time off').click()
    d = dialog(emp)
    choose(emp, d, 'Type', re.compile('^Leave without pay'))
    pick_date(emp, d, 'From', day['bsDate'])
    d.get_by_label('Reason').fill('Family function in Pokhara (browser test)')
    d.get_by_role('button', name='Send request').click()
    req = wait_until(lambda: next((r for r in (emp.api_get('/api/v1/leave/requests') or {'requests': []})['requests']
                                   if r['from_date'] == day['date'] and r['status'] == 'pending'), None))
    assert req, 'the leave request was saved'
    emp.settle()
    ok(emp)

    mgr = tab('leave: manager', 'DEMO:manager', users)
    mgr.go('/approvals')
    mgr.where = 'approve leave'
    name = emp.api_get('/api/v1/auth/me')
    row = mgr.page.locator('div.mantine-Group-root').filter(has_text='Leave').filter(
        has=mgr.page.get_by_role('button', name='Approve')).filter(has_text=re.compile('just now|min ago'))
    row.first.get_by_role('button', name='Approve').click()
    mgr.settle()

    def status():
        r = next((r for r in emp.api_get('/api/v1/leave/requests')['requests'] if r['id'] == req['id']), None)
        return r and r['status']
    # A flow with a second step (HR) still shows it pending: HR approves it from the same inbox.
    if wait_until(lambda: status() == 'approved', timeout=6) is not True and status() == 'pending':
        hr = tab('leave: hr', 'DEMO:hr', users)
        hr.go('/approvals')
        hr.page.locator('div.mantine-Group-root').filter(has_text='Leave').filter(
            has=hr.page.get_by_role('button', name='Approve')).first.get_by_role('button', name='Approve').click()
        hr.settle()
        ok(hr)
    assert wait_until(lambda: status() == 'approved'), f'leave approved (now {status()}, {name})'
    ok(mgr)


def test_clock_in_from_home_page(tab, users):
    t = tab('clock in', 'DEMO:manager', users)
    # The office in Kathmandu; away from a work location the server asks for a reason and the app opens a box for it.
    t.context.grant_permissions(['geolocation'], origin=t.base)
    t.context.set_geolocation({'latitude': 27.7172, 'longitude': 85.3240, 'accuracy': 20})
    t.allow(400, r'^/api/v1/punch$')
    before = len((t.api_get('/api/v1/punch/today') or {}).get('punches', []))
    t.go('/')
    t.where = 'clock in'
    p = t.page
    btn = p.get_by_role('button', name=re.compile(r'^(Clock in|Clock out|At the office\? Clock in without a photo)$'))
    with p.expect_response(lambda r: r.url.endswith('/api/v1/punch') and r.request.method == 'POST') as resp:
        btn.filter(visible=True).last.click()
    if resp.value.status == 400:
        reason = p.get_by_role('dialog').filter(has_text='outside your work location')
        reason.get_by_label(re.compile('^Reason')).fill('Visiting a client (browser test)')
        reason.get_by_role('button', name=re.compile('^Clock (in|out)$')).click()
    t.settle()
    after = wait_until(lambda: len((t.api_get('/api/v1/punch/today') or {}).get('punches', [])) > before)
    assert after, f'a punch was recorded today; {report(t)}'
    ok(t)


def test_hr_adds_an_employee(tab, users):
    t = tab('add employee', 'DEMO:hr', users)
    code = f'E2E{int(time.time()) % 100000}'
    t.go('/employees')
    t.where = 'add employee'
    t.page.get_by_role('button', name='Add employee').first.click()
    d = dialog(t)
    d.get_by_label('Full name').fill(f'Browser Test {code}')
    d.get_by_label('Employee code').fill(code)
    d.get_by_role('button', name='Add employee').click()
    t.settle()
    found = wait_until(lambda: any(e['emp_code'] == code for e in t.api_get('/api/v1/employees')['employees']))
    assert found, 'the new employee is in the directory'
    # The new record opens.
    emp = next(e for e in t.api_get('/api/v1/employees')['employees'] if e['emp_code'] == code)
    t.go(f"/employees/{emp['id']}")
    assert t.page.get_by_text(f'Browser Test {code}').first.is_visible()
    ok(t)


def test_payroll_run_for_a_month_locked_and_published(tab, users):
    t = tab('payroll', 'DEMO:hr', users)
    p = t.page
    cal = working_days(t, datetime.date.today(), 1)
    bs_year, bs_month = (int(x) for x in cal[0]['bsDate'].split('-')[:2]) if cal else (2083, 7)
    # The month before this one that has no locked regular run yet.
    periods = {(r['bs_year'], r['bs_month']): r for r in (t.api_get('/api/v1/payroll/periods') or {'periods': []})['periods']}
    runs = (t.api_get('/api/v1/payroll/runs') or {'runs': []})['runs']
    y, m = bs_year, bs_month
    while True:
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        per = periods.get((y, m))
        if not per or not any(r.get('period_id') == per['id'] and r['run_type'] == 'regular' and r.get('locked_at')
                              for r in runs):
            break
    # The employee needs a salary for the run to pay them: HR assigns one from the month's first day.
    emp_id = users['companies']['DEMO']['employee_id']
    has = (t.api_get(f'/api/v1/payroll/assignments?employeeId={emp_id}') or {}).get('assignments')
    if has:
        # valid is the daterange text '[2026-07-17,)'; a salary that starts after the month pays nobody in it.
        start = datetime.date.fromisoformat(has[-1]['valid'][1:11])
        day = t.api_get(f'/api/v1/shifts/calendar?from={start}&to={start}')['days'][0]
        if tuple(int(x) for x in day['bsDate'].split('-')[:2]) > (y, m):
            has = None
    if not has:
        person = next(e for e in t.api_get('/api/v1/employees')['employees'] if e['id'] == emp_id)
        t.go('/payroll/assignments')
        t.where = 'assign salary'
        p.get_by_role('button', name='Assign salary').first.click()
        d = dialog(t)
        picker = d.get_by_label(re.compile('^Employee')).first
        picker.click()
        picker.fill(person['emp_code'])
        p.get_by_role('option', name=re.compile(re.escape(person['emp_code']))).filter(visible=True).first.click()
        choose(t, d, 'Structure version', re.compile('.'))
        pick_date(t, d, 'Effective from', f'{y}-{m:02d}-01')
        d.get_by_label('BASIC', exact=True).fill('30000')
        d.get_by_label('DA', exact=True).fill('15000')
        d.get_by_role('button', name='Assign salary').click()
        t.settle()
        assert wait_until(lambda: (t.api_get(f'/api/v1/payroll/assignments?employeeId={emp_id}') or {}).get('assignments'))
    t.go('/payroll')
    t.where = f'new pay run {y}-{m}'
    p.get_by_role('button', name='New pay run').click()
    d = dialog(t)
    d.get_by_label(re.compile('^Pay period')).first.click()
    p.get_by_role('option').filter(visible=True).nth(m - 1).click()
    if y != bs_year:
        d.get_by_label('Year', exact=True).fill(str(y))
        p.get_by_role('option', name=str(y), exact=True).click()
    # A run for that month already open from an earlier try: the server says so, and the hub opens it.
    t.allow(409, r'^/api/v1/payroll/runs$')
    d.get_by_role('button', name='Create pay run').click()
    try:
        p.wait_for_url(re.compile(r'/payroll/runs/[0-9a-f-]{36}'), timeout=10000)
    except Exception:  # noqa: BLE001 - reopen the existing run
        runs = t.api_get('/api/v1/payroll/runs')['runs']
        per = next(r for r in t.api_get('/api/v1/payroll/periods')['periods'] if (r['bs_year'], r['bs_month']) == (y, m))
        run = next(r for r in runs if r.get('period_id') == per['id'] and r['run_type'] == 'regular')
        t.go(f"/payroll/runs/{run['id']}")
    run_id = t.path().split('/')[3].split('?')[0]
    t.settle()

    def run():
        return next(r for r in t.api_get('/api/v1/payroll/runs')['runs'] if r['id'] == run_id)

    def click(name, **kw):
        t.where = f'pay run: {name}'
        b = p.get_by_role('button', name=name, **kw).filter(visible=True).first
        b.click()
        t.settle()

    def shown(name, **kw):
        b = p.get_by_role('button', name=name, **kw).filter(visible=True)
        return b.count() > 0 and b.first.is_enabled()

    if shown('Lock attendance'):
        click('Lock attendance')
    if shown(re.compile('^Run checks')):
        click(re.compile('^Run checks'))
    if shown('Calculate anyway'):
        click('Calculate anyway')
        d = dialog(t)
        d.get_by_label('Reason').fill('Browser test: demo data has open items')
        d.get_by_role('button', name=re.compile('Calculate|Continue|Override')).last.click()
        t.settle()
    elif shown('Continue to inputs'):
        click('Continue to inputs')
    # The wizard moves to the step of the run's status by itself; press whichever next button it shows.
    order = ['Lock run', 'Approve run', 'Mark reviewed', 'Continue to approval', 'Calculate payslips',
             'Continue to review']
    for _ in range(12):
        if run().get('locked_at'):
            break
        for name in order:
            if shown(name, exact=True):
                click(name, exact=True)
                break
        else:
            t.page.wait_for_timeout(500)
    assert run().get('locked_at'), f"the run is locked (status {run()['status']})"
    assert int(run()['totals']['headcount']) >= 1, run()['totals']
    p.get_by_role('button', name=re.compile('^(Publish payslips|Published|Continue to salary sheet)$')).filter(visible=True).first.wait_for()
    if shown('Continue to salary sheet'):
        click('Continue to salary sheet')
    if shown('Publish payslips'):
        click('Publish payslips')
    p.get_by_role('button', name='Published', exact=True).wait_for()
    ok(t)

    # The employee sees the payslip.
    emp = tab('payroll: employee', 'DEMO:employee', users)
    emp.go('/me/payslips')
    rows = emp.page.locator('table tbody tr')
    if rows.count():
        rows.first.click()
        emp.settle()
        assert '/me/payslips/' in emp.path()
    ok(emp)


def test_raise_a_help_desk_ticket(tab, users):
    t = tab('help desk', 'DEMO:employee', users)
    subject = f'Browser test question {int(time.time())}'
    t.go('/me/help')
    t.where = 'ask HR'
    t.page.get_by_role('button', name='Ask HR').first.click()
    d = dialog(t)
    d.get_by_label(re.compile('^What is it about')).first.click()
    t.page.get_by_role('option').filter(visible=True).first.click()
    d.get_by_label('Subject').fill(subject)
    d.get_by_label('Details').fill('How many casual leave days do I get this year?')
    d.get_by_role('button', name='Send to HR').click()
    t.settle()
    assert wait_until(lambda: any(x['subject'] == subject
                                  for x in t.api_get('/api/v1/helpdesk/tickets/mine')['tickets']))
    assert t.page.get_by_text(subject).first.is_visible()
    ok(t)

    hr = tab('help desk: hr', 'DEMO:hr', users)
    hr.go('/helpdesk')
    assert hr.page.get_by_text(subject).first.is_visible(), 'HR sees the ticket in the queue'
    ok(hr)


def test_submit_an_expense_claim(tab, users):
    t = tab('expense claim', 'DEMO:employee', users)
    title = f'Client lunch {int(time.time())}'
    today = working_days(t, datetime.date.today(), 0)
    cal = t.api_get(f'/api/v1/shifts/calendar?from={datetime.date.today()}&to={datetime.date.today()}')['days'][0]
    t.go('/me/claims')
    t.where = 'new claim'
    t.page.get_by_role('button', name='New claim').first.click()
    d = dialog(t)
    d.get_by_label('What is this claim for?').fill(title)
    pick_date(t, d, 'Date', cal['bsDate'])
    choose(t, d, 'Category', 'Meals')
    d.get_by_label('Description').fill('Lunch with the Lalitpur client')
    d.get_by_label('Amount (NPR)').fill('350')
    submit = d.get_by_role('button', name='Submit for approval')
    submit.wait_for()
    wait_until(lambda: submit.is_enabled(), timeout=10)
    submit.click()
    t.settle()
    claim = wait_until(lambda: next((c for c in t.api_get('/api/v1/expenses/claims?scope=own')['claims']
                                     if c['title'] == title), None))
    assert claim and claim['status'] in ('submitted', 'pending', 'in_approval', 'approved'), claim
    del today
    ok(t)


def test_group_dashboard(tab, users):
    t = tab('group dashboard', 'group:group_admin', users)
    t.go('/group')
    for c in ('Demo Company', 'Demo Systems'):
        assert t.page.get_by_text(re.compile(c)).first.is_visible(), c
    t.go('/group/people')
    t.go('/group/reports')
    ok(t)
