"""The React app in a real browser against a running Hajiri (pytest + Playwright).

Skipped unless HAJIRI_E2E_URL is set. Set up a throwaway database first (never the live one):

    createdb -p 5433 hajiri_e2e
    DATABASE_URL=postgres://localhost:5433/hajiri_e2e .venv/bin/python manage.py migrate
    DATABASE_URL=postgres://localhost:5433/hajiri_e2e .venv/bin/python manage.py seed_bs_calendar
    .venv/bin/python manage.py seed_demo_group --database postgres://localhost:5433/hajiri_e2e --yes
    DATABASE_URL=postgres://localhost:5433/hajiri_e2e .venv/bin/python manage.py shell < tests/e2e/seed_users.py > /tmp/e2e-users.json
    # web + worker on that database, then:
    HAJIRI_E2E_URL=http://localhost:8301 HAJIRI_E2E_USERS=/tmp/e2e-users.json .venv/bin/pytest -p no:cacheprovider tests/e2e

For each person (every company role in both demo companies, the group roles and the person who works in both
companies) it signs in through the login page and opens every route of the React router (frontend/src/App.jsx and
the hub tabs in pages/hubs.jsx), with real ids of the active company for the routes that take one. Every console
error, uncaught exception, failed same-origin request (HTTP >= 400) and visible error (red alert or toast,
"Something went wrong", a blank page) is a failure. Routes a role may not open are expected to send it elsewhere
(RequireRole / FeatureRoute) without any of those. test_actions.py drives the main flows end to end.
"""
from __future__ import annotations

import re

import pytest

from .browse import Tab

# Every route of the router, with the hub tabs (frontend/src/App.jsx, frontend/src/pages/hubs.jsx).
ROUTES = [
    '/', '/me/attendance', '/me/attendance/roster', '/me/attendance/visits', '/me/attendance/kiosk',
    '/me/attendance/devices', '/me/timesheet', '/me/claims', '/me/claims/travel', '/me/growth', '/me/growth/reviews',
    '/me/growth/one-on-ones', '/me/growth/feedback', '/me/growth/learning', '/me/help', '/me/help/grievances', '/tasks',
    '/people', '/people/org-chart', '/engage', '/engage/surveys', '/engage/suggestions', '/me/leave', '/me/payslips',
    '/me/payslips/tax', '/me/notifications', '/me/profile', '/me/profile/documents', '/me/profile/security',
    '/me/profile/conduct', '/me/profile/privacy', '/approvals',
    '/group', '/group/people', '/group/reports', '/group/companies', '/group/access', '/group/transfers',
    '/group/policies',
    '/attendance', '/attendance/from-home', '/attendance/exceptions', '/attendance/overtime', '/attendance/timesheets',
    '/attendance/rosters', '/attendance/visits', '/attendance/projects', '/attendance/kiosks', '/attendance/issues',
    '/attendance/penalties', '/attendance/upload', '/attendance/fix',
    '/leave', '/leave/balances', '/leave/policies', '/leave/history',
    '/expenses', '/expenses/travel', '/expenses/policies',
    '/performance', '/performance/reviews', '/performance/learning',
    '/hiring', '/hiring/pipeline', '/hiring/interviews', '/hiring/requisitions',
    '/overview', '/insights',
    '/employees', '/employees/structure', '/employees/onboarding', '/employees/exits', '/employees/documents',
    '/employees/requests',
    '/helpdesk',
    '/relations', '/relations/disciplinary', '/relations/milestones', '/relations/assignments', '/relations/kye',
    '/relations/devices',
    '/reports', '/reports/builder',
    '/settings', '/settings/shifts', '/settings/holidays', '/settings/devices', '/settings/locations',
    '/settings/users', '/settings/approvals', '/settings/features', '/settings/setup',
    '/payroll', '/payroll/structures', '/payroll/assignments', '/payroll/declarations', '/payroll/loans',
    '/payroll/reports', '/payroll/optimum-cit', '/payroll/held-pay', '/payroll/opening-balances', '/payroll/rules',
    # Addresses of the earlier layout (redirects).
    '/hr/dashboard', '/hr/employees', '/manager/approvals', '/manager/team', '/hr/exceptions', '/hr/ot-slips',
    '/manager/fix-punch', '/manager/calendar', '/hr/requests', '/hr/leave-adjust', '/hr/leave-types', '/hr/reports',
    '/hr/settings', '/hr/shifts', '/hr/holidays', '/hr/devices', '/hr/users', '/hr/setup', '/me/data',
    '/change-password', '/setup-two-step',
]
# Routes with an id; filled from the active company's records.
PARAM_ROUTES = ['/employees/{employee}', '/hr/employees/{employee}', '/tasks/{board}', '/me/payslips/{payslip}',
                '/payroll/runs/{run}']

COMPANY_ROLES = ['admin', 'hr', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive', 'manager', 'employee']
PEOPLE = [f'{c}:{r}' for c in ('DEMO', 'DEMOSYS') for r in COMPANY_ROLES] + \
    ['group:group_admin', 'group:group_hr', 'group:group_executive', 'group:two_company']
# A sample opened again after switching to the sister company.
SWITCH_SAMPLE = ['/', '/me/profile', '/approvals', '/employees', '/employees/{employee}', '/attendance', '/leave',
                 '/payroll', '/settings', '/group', '/me/attendance', '/me/leave']


def email_of(users, who: str) -> str:
    scope, role = who.split(':')
    return users['group'][role] if scope == 'group' else users['companies'][scope][role]


def record_ids(tab: Tab) -> dict:
    """Real ids of the active company, as far as this person may read them."""
    ids = {}
    me = tab.api_get('/api/v1/auth/me') or {}
    user = me.get('user') or me
    emps = tab.api_get('/api/v1/employees')
    if emps and emps.get('employees'):
        ids['employee'] = emps['employees'][0]['id']
    boards = tab.api_get('/api/v1/tasks/boards')
    if boards and boards.get('boards'):
        ids['board'] = boards['boards'][0]['id']
    runs = tab.api_get('/api/v1/payroll/runs')
    if runs and runs.get('runs'):
        ids['run'] = runs['runs'][0]['id']
    if user.get('employeeId'):
        for fy in (2083, 2082):
            slips = tab.api_get(f"/api/v1/payroll/payslips?employeeId={user['employeeId']}&fiscalYearStart={fy}")
            if slips and slips.get('payslips'):
                ids['payslip'] = slips['payslips'][0]['id']
                break
    return ids


def visit_all(tab: Tab, routes: list[str]) -> dict:
    ids = record_ids(tab)
    visited = {}
    for r in routes:
        names = re.findall(r'{(\w+)}', r)
        if any(n not in ids for n in names):
            visited[r] = 'no id'
            continue
        route = r.format(**ids)
        tab.go(route)
        visited[r] = tab.path()
        # Leave the one-off pages so the next route starts from the app shell.
        if tab.path().startswith(('/change-password', '/setup-two-step')):
            tab.go('/')
    return visited


def report(tab: Tab) -> str:
    seen, lines = set(), []
    for p in tab.problems:
        s = str(p)
        if s not in seen:
            seen.add(s)
            lines.append(s)
    return f'{tab.label}: {len(lines)} problem(s)\n' + '\n'.join(lines)


@pytest.fixture
def tab(browser, base_url):
    tabs = []

    def make(label):
        t = Tab(browser, base_url, label)
        tabs.append(t)
        return t
    yield make
    for t in tabs:
        t.close()


def test_public_pages(tab, users):
    t = tab('public')
    t.go('/login', client_side=False)
    # Several companies share this install, so the bare /careers address names no company: the page says so.
    t.allow(404, r'^/api/v1/public/careers/jobs$')
    t.go('/careers', client_side=False)
    assert t.page.get_by_text('Careers page not available').is_visible()
    t.go('/careers/DEMO', client_side=False)
    jobs = t.api_get('/api/v1/public/careers/companies/DEMO/jobs') or {}
    assert jobs.get('jobs'), 'the demo company has open jobs'
    t.go(f"/careers/DEMO/{jobs['jobs'][0]['id']}", client_side=False)
    assert t.page.get_by_text(jobs['jobs'][0]['title']).first.is_visible()
    t.go(f"/careers/{jobs['jobs'][0]['id']}", client_side=False)
    # The kiosk tablet page before pairing.
    t.go('/kiosk', client_side=False)
    # A signed-out visit to an app page goes to the login page.
    t.go('/employees', client_side=False)
    assert t.path().startswith('/login')
    assert not t.problems, report(t)


@pytest.mark.parametrize('who', PEOPLE)
def test_every_route(tab, users, who):
    t = tab(who)
    t.sign_in(email_of(users, who), users['password'])
    visit_all(t, ROUTES + PARAM_ROUTES)
    assert not t.problems, report(t)


@pytest.mark.parametrize('who', ['group:group_admin', 'group:group_hr', 'group:group_executive', 'group:two_company'])
def test_switch_company_and_browse(tab, users, who):
    t = tab(who)
    t.sign_in(email_of(users, who), users['password'])
    sister = users['companies']['DEMOSYS']
    t.where = 'switch company'
    # The sidebar's switcher (the phone header has its own copy, off screen at this width).
    on_screen = [b for b in t.page.locator('.hj-company-switch').all() if (b.bounding_box() or {}).get('y', -1) >= 0]
    on_screen[0].click()
    t.page.get_by_role('menuitem', name=re.compile('DEMOSYS')).click()
    t.page.wait_for_function(
        "(id) => fetch('/api/v1/auth/me').then(r => r.json()).then(d => (d.user || d).companyId === id)", arg=sister['id'])
    t.page.wait_for_load_state('load')
    t.settle()
    me = t.api_get('/api/v1/auth/me')
    assert (me.get('user') or me)['companyId'] == sister['id']
    visit_all(t, SWITCH_SAMPLE)
    # Records shown are the sister company's.
    emps = t.api_get('/api/v1/employees')
    if emps:
        assert all(e.get('company_id', sister['id']) == sister['id'] for e in emps['employees'])
    assert not t.problems, report(t)
