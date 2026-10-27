"""apps/web/src/pages/hubs.tsx and App.tsx: every hub's tabs (path, label, order and who sees them), the landing tab,
and the catch-all route. Each slice registers its own tabs in its nav.py; this checks the whole set against the TS."""
import pytest
from django.test import Client

from hajiri.web import nav
from tests.web.factories import make_employee, set_features_off, signed_in_client

A = ('hr', 'attendance_admin')
HUB_TABS = {
    'reports': [('/reports', 'Catalogue'), ('/reports/builder', 'Build a report')],
    'attendance': [('/attendance', 'Today'), ('/attendance/from-home', 'Work from home'), ('/attendance/exceptions', 'Exceptions'),
                   ('/attendance/overtime', 'Overtime'), ('/attendance/timesheets', 'Timesheets'), ('/attendance/rosters', 'Rosters'),
                   ('/attendance/visits', 'Field visits'), ('/attendance/projects', 'Projects'), ('/attendance/kiosks', 'Kiosks'),
                   ('/attendance/issues', 'Issues'), ('/attendance/penalties', 'Late penalties'), ('/attendance/upload', 'Upload'),
                   ('/attendance/fix', 'Fix a punch')],
    'leave': [('/leave', 'Calendar'), ('/leave/balances', 'Balances'), ('/leave/policies', 'Policies'), ('/leave/history', 'Request history')],
    'payroll': [('/payroll', 'Pay runs'), ('/payroll/structures', 'Salary structures'), ('/payroll/assignments', 'Employee salaries'),
                ('/payroll/declarations', 'Tax declarations'), ('/payroll/loans', 'Loans & advances'), ('/payroll/reports', 'Reports'),
                ('/payroll/optimum-cit', 'Optimum CIT'), ('/payroll/held-pay', 'Held pay'), ('/payroll/opening-balances', 'Opening balances'),
                ('/payroll/rules', 'Tax & SSF rules')],
    'settings': [('/settings', 'Company & rules'), ('/settings/shifts', 'Shifts'), ('/settings/holidays', 'Holidays'),
                 ('/settings/devices', 'Devices'), ('/settings/locations', 'Locations'), ('/settings/users', 'Users & roles'),
                 ('/settings/approvals', 'Approval flows'), ('/settings/features', 'Features'), ('/settings/setup', 'Setup checklist')],
    'profile': [('/me/profile', 'Profile'), ('/me/profile/documents', 'Documents & letters'), ('/me/profile/security', 'Sign-in security'),
                ('/me/profile/conduct', 'Conduct'), ('/me/profile/privacy', 'Privacy & my data')],
    'my_attendance': [('/me/attendance', 'Attendance'), ('/me/attendance/roster', 'My roster'), ('/me/attendance/visits', 'Visits & breaks'),
                      ('/me/attendance/kiosk', 'Kiosk badge'), ('/me/attendance/devices', 'Clock-in devices')],
    'payslips': [('/me/payslips', 'Payslips'), ('/me/payslips/tax', 'Tax savings')],
    'claims': [('/me/claims', 'Expense claims'), ('/me/claims/travel', 'Travel')],
    'growth': [('/me/growth', 'Goals'), ('/me/growth/reviews', 'Reviews'), ('/me/growth/one-on-ones', '1-on-1s'),
               ('/me/growth/feedback', 'Feedback'), ('/me/growth/learning', 'Learning')],
    'timesheet': [('/me/timesheet', 'Timesheet')],
    'people': [('/people', 'Directory'), ('/people/org-chart', 'Org chart')],
    'engage': [('/engage', 'Feed'), ('/engage/surveys', 'Surveys & polls'), ('/engage/suggestions', 'Suggestions')],
    'employees': [('/employees', 'Directory'), ('/employees/structure', 'Org structure'), ('/employees/onboarding', 'Onboarding'),
                  ('/employees/exits', 'Exits'), ('/employees/documents', 'Documents & letters'), ('/employees/requests', 'Change requests')],
    'expenses': [('/expenses', 'Claims'), ('/expenses/travel', 'Travel'), ('/expenses/policies', 'Policies')],
    'performance': [('/performance', 'Goals'), ('/performance/reviews', 'Review cycles'), ('/performance/learning', 'Learning')],
    'hiring': [('/hiring', 'Jobs'), ('/hiring/pipeline', 'Candidates'), ('/hiring/interviews', 'Interviews'),
               ('/hiring/requisitions', 'Requisitions')],
    'help': [('/me/help', 'Questions'), ('/me/help/grievances', 'Grievances')],
    'relations': [('/relations', 'Grievances'), ('/relations/disciplinary', 'Disciplinary'), ('/relations/milestones', 'Milestones'),
                  ('/relations/assignments', 'Acting & deputation'), ('/relations/kye', 'Know your employee'),
                  ('/relations/devices', 'Clock-in devices')],
    # Single pages of App.tsx with a sidebar entry.
    'home': [('/', 'Home')], 'approvals': [('/approvals', 'Approvals')], 'overview': [('/overview', 'Overview')],
    'insights': [('/insights', 'Insights')], 'tasks': [('/tasks', 'Tasks')], 'helpdesk': [('/helpdesk', 'Help desk')],
    'notifications': [('/me/notifications', 'Notifications')], 'time_off': [('/me/leave', 'Time off')],
}
# Tab features (hubs.tsx `feature`, or the App.tsx FeatureRoute around the hub).
FEATURES = {'/attendance/timesheets': 'time', '/attendance/visits': 'field', '/attendance/projects': 'time', '/attendance/kiosks': 'field',
            '/me/profile/documents': 'documents', '/me/attendance/visits': 'field', '/me/attendance/kiosk': 'field',
            '/me/claims': 'expenses', '/me/claims/travel': 'expenses', '/me/growth': 'performance', '/me/growth/reviews': 'performance',
            '/me/growth/one-on-ones': 'performance', '/me/growth/feedback': 'performance', '/me/growth/learning': 'learning',
            '/me/timesheet': 'time', '/engage/surveys': 'surveys', '/engage/suggestions': 'suggestions', '/employees/onboarding': 'lifecycle',
            '/employees/exits': 'lifecycle', '/employees/documents': 'documents', '/expenses': 'expenses', '/expenses/travel': 'expenses',
            '/expenses/policies': 'expenses', '/performance': 'performance', '/performance/reviews': 'performance',
            '/performance/learning': 'learning', '/hiring': 'hiring', '/hiring/pipeline': 'hiring', '/hiring/interviews': 'hiring',
            '/hiring/requisitions': 'hiring', '/me/help': 'helpdesk', '/me/help/grievances': 'helpdesk', '/tasks': 'tasks',
            '/helpdesk': 'helpdesk'}


def test_every_hub_has_the_ts_tabs_in_order(db):
    items = nav.all_items()
    got = {}
    for it in items:
        got.setdefault(it.hub, []).append((nav.url_of(it.url_name), it.label))
    for hub, tabs in HUB_TABS.items():
        assert got.get(hub) == tabs, hub
    assert set(got) == set(HUB_TABS)
    for it in items:
        assert it.feature == FEATURES.get(nav.url_of(it.url_name)), it.label


def test_tab_visibility_matches_the_ts_roles(db):
    """Tab roles from hubs.tsx (AREA.attendance on Exceptions, Overtime, Projects, Kiosks, Late penalties, Upload; HR on Suggestions
    and expense Policies) on top of the App.tsx route guard of the hub."""
    def visible(user, path):
        return any(nav.url_of(it.url_name) == path and nav.item_visible(user, it, set()) for it in nav.all_items())
    from hajiri.core.auth import AuthUser

    def u(*roles, emp='e'):
        return AuthUser(id='u', company_id='c', employee_id=emp, email='x', name='x', roles=roles, must_change_password=False,
                        two_factor_enabled=False, must_set_up_two_factor=False)
    mgr, att, hr, emp = u('manager'), u('attendance_admin'), u('hr'), u('employee')
    for p in ('/attendance/exceptions', '/attendance/overtime', '/attendance/projects', '/attendance/kiosks', '/attendance/penalties',
              '/attendance/upload', '/leave/balances', '/leave/policies', '/leave/history'):
        assert not visible(mgr, p) and visible(att, p) and visible(hr, p), p
    for p in ('/attendance', '/attendance/rosters', '/attendance/fix', '/leave'):
        assert visible(mgr, p) and visible(att, p) and not visible(emp, p), p
    assert visible(hr, '/engage/suggestions') and not visible(mgr, '/engage/suggestions') and visible(emp, '/engage/surveys')
    assert not visible(u('hr', emp=None), '/me/profile/conduct') and visible(u('hr', emp=None), '/me/profile/security')
    assert visible(u('payroll_admin'), '/payroll/rules') and not visible(att, '/payroll')


def test_landing_tabs_and_refusals(acme):
    hr, _ = signed_in_client(acme, roles=['hr'])
    for hub in ('settings', 'payroll', 'employees', 'relations', 'reports'):
        assert hr.get(HUB_TABS[hub][0][0]).status_code == 200, hub
    emp, _ = signed_in_client(acme, roles=['employee'])
    r = emp.get('/settings')
    assert r.status_code == 403 and b'Not available for your role' in r.content
    # A switched-off module's page goes home (FeatureRoute) and its tab disappears.
    set_features_off(acme, 'tasks')
    assert emp.get('/tasks')['Location'] == '/'
    assert b'href="/tasks"' not in emp.get('/').content


def test_unknown_addresses_go_home(acme):
    c, _ = signed_in_client(acme, roles=['employee'])
    r = c.get('/no/such/page')
    assert r.status_code == 302 and r['Location'] == '/'
    assert c.get('/_/no/such/fragment').status_code == 404
    anon = Client().get('/no/such/page')
    assert anon.status_code == 302 and anon['Location'].startswith('/login')
