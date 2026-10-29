"""The pay run wizard's attendance check (PayrollAttendanceCheck) reads the month's issue summary: payroll admins
open that wizard, so they read the summary too (found by the browser tests, tests/e2e). The issue list itself stays
with attendance."""
from django.test import Client

from tests.core.fixtures import bearer


def test_payroll_admin_reads_the_issue_summary_but_not_the_issue_list(login_as, demo):
    pay = login_as(['payroll_admin'])
    r = Client().get('/api/v1/attendance-tools/issues/summary?bsYear=2083&bsMonth=5', **bearer(pay.token))
    assert r.status_code == 200, r.json()
    assert {'bsYear', 'bsMonth', 'from', 'to'} <= set(r.json())
    lst = Client().get('/api/v1/attendance-tools/issues?from=2026-08-17&to=2026-09-16', **bearer(pay.token))
    assert lst.status_code == 403


def test_employees_and_recruiters_still_cannot_read_the_summary(login_as, demo):
    for roles in (['employee'], ['recruiter'], ['executive']):
        r = Client().get('/api/v1/attendance-tools/issues/summary?bsYear=2083&bsMonth=5', **bearer(login_as(roles).token))
        assert r.status_code == 403, roles
