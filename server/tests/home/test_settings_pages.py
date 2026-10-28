"""apps/api/test/settings.test.ts through the Settings pages (Company & rules, SecurityPolicySettings), plus the
page rules of SettingsPage.tsx. The service-level port is tests/company_settings."""
import re

import pytest
from django.test import Client

from tests.support.sql import one, rows
from tests.web.factories import htmx, hx_triggers, toasts


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.fixture
def hr(login_as):
    return client_for(login_as(['hr']))


@pytest.fixture
def emp(login_as, demo):
    return client_for(login_as(['employee'], demo['sita']))


def stored(demo, key):
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [demo['company_id'], key])
    return r['value'] if r else None


def test_lists_every_section_for_hr_only(hr, emp):
    r = hr.get('/settings')
    html = r.content.decode()
    # The hub header says Settings; the page title folds into it (EmbeddedContext), its subtitle stays.
    assert r.status_code == 200 and 'How attendance, leave and pay are calculated.' in html
    for label in ('Attendance', 'Leave', 'Payroll', 'Time tracking', 'Notifications', 'Company', 'Branches and units', 'Security'):
        assert f'>{label}</a>' in html
    # The statutory caps are shown locked with their citation.
    assert '4 h a day, 24 h a week' in html and 'Labour Act Sec 30(1)' in html and '1.5 times basic' in html
    assert emp.get('/settings').status_code == 403
    assert emp.post('/_/home/settings/attendance', {}, **htmx()).status_code == 403
    assert Client().get('/settings')['Location'].startswith('/login')


def test_clamps_ot_caps_bumps_the_version_and_audits(hr, demo):
    base = {'otMinThresholdMin': '20', 'otRoundingMin': '15', 'requirePreapproval': 'on', 'holidayWork': 'pay',
            'autoClockOutAfterMin': '0', 'regularisationBackdateDays': '30', 'regularisationMonthlyCap': '5',
            'wfhPhotoRequired': 'on', 'wfhPhotoRetentionDays': '90', 'nightPremiumRate': '0'}
    r = hr.post('/_/home/settings/attendance', base, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Attendance rules saved. They apply to future recalculations.'
    v = stored(demo, 'attendance.policy')
    assert (v['otMinThresholdMin'], v['otCapDayMin'], v['otCapWeekMin'], v['holidayWork']) == (20, 240, 1440, 'pay')
    assert re.fullmatch(r'v-[0-9a-f]{12}', v['version'])
    # The page shows what was saved.
    assert 'value="20"' in hr.get('/settings').content.decode()
    r2 = hr.post('/_/home/settings/attendance', {**base, 'otMinThresholdMin': '25'}, **htmx())
    assert r2.status_code == 204 and stored(demo, 'attendance.policy')['version'] != v['version']
    a = one("SELECT before, after FROM audit_log WHERE table_name = 'company_setting' AND row_id = 'attendance.policy' ORDER BY id DESC LIMIT 1")
    assert a['before']['otMinThresholdMin'] == 20 and a['after']['otMinThresholdMin'] == 25
    # Switches left off are saved off; the late penalty numbers are kept while it is off.
    hr.post('/_/home/settings/attendance', {**base, 'requirePreapproval': '', 'latePenalty.enabled': 'on',
                                            'latePenalty.lateCount': '4', 'latePenalty.deductionDays': '1'}, **htmx())
    v = stored(demo, 'attendance.policy')
    assert v['requirePreapproval'] is False and v['latePenalty'] == {'enabled': True, 'lateCount': 4, 'deductionDays': 1}
    hr.post('/_/home/settings/attendance', base, **htmx())
    assert stored(demo, 'attendance.policy')['latePenalty'] == {'enabled': False, 'lateCount': 4, 'deductionDays': 1}


def test_invalid_values_are_refused_with_a_toast(hr, demo):
    seeded = stored(demo, 'notifications.policy')
    r = hr.post('/_/home/settings/notify', {'quietHoursStart': '25:00', 'quietHoursEnd': '07:00', 'slaHours': '24',
                                            'escalationHours': '48', 'digestTime': '10:45'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['tone'] == 'err' and 'quietHoursStart' in toasts(r)[0]['message']
    assert stored(demo, 'notifications.policy') == seeded
    assert hr.post('/_/home/settings/nope', {}, **htmx()).status_code == 400
    ok = hr.post('/_/home/settings/notify', {'quietHoursStart': '22:00', 'quietHoursEnd': '06:00', 'slaHours': '12',
                                             'escalationHours': '48', 'digestTime': '10:45'}, **htmx())
    assert ok.status_code == 204 and toasts(ok)[0]['message'] == 'Settings saved.'
    assert stored(demo, 'notifications.policy')['slaHours'] == 12


def test_company_profile_and_the_header(hr, demo, login_as):
    r = hr.post('/_/home/settings/company', {'nameEn': 'Demo Pvt Ltd', 'address': '', 'pan': '123456789', 'ssfEmployerId': ''}, **htmx())
    assert r.status_code == 204
    v = stored(demo, 'company.profile')
    assert v['nameEn'] == 'Demo Pvt Ltd' and v['pan'] == '123456789' and 'address' not in v  # '' is left out (GenericSettings)
    from hajiri.company_settings.services import company_header
    h = company_header(login_as(['employee'], demo['ram']).user)
    assert h['company']['nameEn'] == 'Demo Pvt Ltd' and re.fullmatch(r'\d{4}-\d{2}-\d{2}', h['today']['bs'])
    # A bad PAN is refused.
    assert hr.post('/_/home/settings/company', {'nameEn': 'Demo', 'pan': '12'}, **htmx()).status_code == 400
    html = hr.get('/settings?tab=company').content.decode()
    assert 'value="Demo Pvt Ltd"' in html and 'Permanent account number from the Inland Revenue Department.' in html


def test_leave_and_payroll_sections(hr, demo):
    r = hr.post('/_/home/settings/leave', {'leaveYear': 'anniversary', 'sandwichRule': 'on', 'maxContinuousDays': '20'}, **htmx())
    assert r.status_code == 204
    assert stored(demo, 'leave.policy') == {'leaveYear': 'anniversary', 'sandwichRule': True, 'probationRestriction': False,
                                            'maxContinuousDays': 20}
    r = hr.post('/_/home/settings/payroll', {'prorationBasis': 'fixed_30', 'hourlyDivisor': '240', 'payDay': '28',
                                             'rounding': 'paisa', 'encashmentDivisor': 'bs_days'}, **htmx())
    assert r.status_code == 204 and stored(demo, 'payroll.policy')['hourlyDivisor'] == 240
    html = hr.get('/settings?tab=payroll').content.decode()
    assert 'Default 208 (26 × 8); 240 is also common.' in html and '<option value="fixed_30" selected>' in html


def test_branches_and_org_units(hr, demo):
    html = hr.get('/settings?tab=org').content.decode()
    assert 'Head Office' in html and 'No departments or teams yet' in html
    r = hr.post('/_/home/settings/branches', {'code': 'pkr', 'name': 'Pokhara'}, **htmx())
    assert r.status_code == 200 and 'Pokhara' in r.content.decode() and '>PKR<' in r.content.decode()
    dup = hr.post('/_/home/settings/branches', {'code': 'PKR', 'name': 'Dup'}, **htmx())
    assert dup.status_code == 409
    r = hr.post('/_/home/settings/units', {'kind': 'department', 'code': 'FIN', 'name': 'Finance'}, **htmx())
    assert r.status_code == 200 and 'Finance' in r.content.decode() and 'Department' in r.content.decode()
    assert len(rows('SELECT id FROM branch WHERE company_id = %s', [demo['company_id']])) == 2


def test_preview_reports_counts_without_writing(hr):
    before = one('SELECT count(*)::int AS n FROM attendance_day')['n']
    r = hr.post('/_/home/settings/attendance/preview', {'otMinThresholdMin': '0'}, **htmx())
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Effect on the last 30 days' in html and 'late marks' in html and 'employees would change.' in html
    assert one('SELECT count(*)::int AS n FROM attendance_day')['n'] == before


def test_security_policy(hr, demo, login_as):
    html = hr.get('/settings?tab=security').content.decode()
    assert 'Two-step sign-in' in html and 'Administrators' in html and 'Everyone else' in html
    r = hr.post('/_/home/settings/security', {'require2faRoles': ['manager', 'manager']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Security settings saved.'
    assert stored(demo, 'security.policy') == {'require2faRoles': ['manager']}
    # Including your own role sends you to set up two-step sign-in.
    r = hr.post('/_/home/settings/security', {'require2faRoles': ['hr']}, **htmx())
    assert r.status_code == 204 and r['HX-Redirect'] == '/setup-two-step'
    assert hr.get('/settings')['Location'] == '/setup-two-step'
    assert hr.post('/_/home/settings/security', {'require2faRoles': ['root']}, **htmx()).status_code == 403  # gated now


def test_time_tracking_tab_loads_the_timesheet_settings(hr):
    html = hr.get('/settings?tab=time').content.decode()
    assert 'hx-get="/_/timesheets/settings"' in html
    frag = hr.get('/settings?tab=leave', **htmx('settings-panel'))
    assert frag.status_code == 200 and 'Leave year' in frag.content.decode() and '<html' not in frag.content.decode()
