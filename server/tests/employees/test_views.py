"""Employees hub pages through django.test.Client, and the two-step gate of security-selfservice.test.ts."""
from django.test import Client

from hajiri.core.db import one
from tests.people.conftest import build_world
from tests.web.factories import htmx, hx_triggers, make_user, require_two_factor_for, sign_in, toasts

import pytest


@pytest.fixture
def world(db):
    return build_world()


def client_for(user):
    c = Client()
    sign_in(c, user)
    return c


def test_list_is_hr_only_with_search_and_status(world):
    w = world
    h = client_for(w.hr)
    r = h.get('/employees')
    assert r.status_code == 200 and b'Sita Sharma' in r.content and b'3 people' in r.content
    r = h.get('/employees?status=exited')
    assert b'No exited employees' in r.content
    r = h.get('/employees?employees-q=zzz', **htmx('employees'))
    assert r.status_code == 200 and b'No employees match your search' in r.content and b'Try a different name or code.' in r.content
    assert client_for(w.sita).get('/employees').status_code in (302, 403)
    assert h.get('/hr/employees').status_code == 302


def test_add_edit_and_detail_tabs(world):
    w = world
    h = client_for(w.hr)
    assert b'Add employee' in h.get('/_/employees/new', **htmx('hj-drawer-body')).content
    r = h.post('/_/employees/new', {'empCode': 'E900', 'nameEn': 'New Person', 'regime': 'ssf', 'otEligible': 'on', 'isResident': 'on',
                                   'weeklyOffDays': ['6'], 'employmentType': 'regular', 'branchId': w.branch_id}, **htmx())
    assert r.status_code in (200, 204) and r.headers.get('HX-Redirect', '').startswith('/employees/')
    new_id = r.headers['HX-Redirect'].rsplit('/', 1)[1]
    r = h.post(f'/_/employees/{new_id}/edit', {'empCode': 'E900', 'nameEn': 'New Person', 'phone': '9801', 'regime': 'ssf',
                                              'weeklyOffDays': ['5', '6'], 'employmentType': 'regular'},
               **htmx(current_url=f'http://testserver/employees/{new_id}?tab=overview'))
    # The TS page reloads the employee after saving: the whole page reloads on the same tab, so the header is current.
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Changes saved.'
    assert r.headers['HX-Redirect'] == f'/employees/{new_id}?tab=overview'
    r = h.post(f'/_/employees/{new_id}/edit', {'empCode': 'E900', 'nameEn': 'New Name', 'regime': 'ssf', 'weeklyOffDays': ['5', '6'],
                                              'employmentType': 'regular'}, **htmx(current_url='http://evil.example/x?y=1'))
    assert r.headers['HX-Redirect'] == f'/employees/{new_id}'
    assert b'New Name' in h.get(f'/employees/{new_id}').content
    assert list(one('SELECT weekly_off_days FROM employee WHERE id = %s', [new_id])['weekly_off_days']) == [5, 6]
    for tab in ('overview', 'profile', 'career', 'leave', 'history', 'lifecycle', 'relations', 'pay', 'documents', 'biometrics', 'changes', 'audit'):
        r = h.get(f'/employees/{w.sita_id}?tab={tab}')
        assert r.status_code == 200, tab
    assert b'Friday, Saturday' in h.get(f'/employees/{new_id}').content
    r = h.post(f'/_/employees/{w.sita_id}/edit', {'empCode': 'E001', 'nameEn': 'Sita Sharma', 'pan': '12'}, **htmx())
    assert r.status_code == 400
    assert client_for(w.foreign_hr).get(f'/employees/{w.sita_id}').status_code == 404


def test_reveal_sensitive_is_audited(world):
    w = world
    from hajiri.employees import service as emp
    emp.update_employee(w.hr, w.sita_id, {'pan': '123456789'})
    r = client_for(w.hr).post(f'/_/employees/{w.sita_id}/sensitive', **htmx())
    assert b'123456789' in r.content
    assert one("SELECT 1 AS x FROM audit_log WHERE action = 'read_sensitive' AND row_id = %s", [w.sita_id])


def test_csv_import_check_then_import(world):
    w = world
    h = client_for(w.hr)
    csv = 'emp_code,name_en\nE010,Hari Lal\n'
    r = h.post('/_/employees/import', {'csv': csv, 'action': 'check'}, **htmx())
    assert r.status_code == 200 and b'1 row valid' in r.content
    r = h.post('/_/employees/import', {'csv': 'emp_code,name_en,email\nE011,X,bad\n', 'action': 'check'}, **htmx())
    assert b'1 row needs fixing before you can import' in r.content
    r = h.post('/_/employees/import', {'csv': csv, 'action': 'import'}, **htmx())
    # As EmployeesPage.tsx: the modal stays open with the result, the list reloads.
    assert r.status_code == 200 and toasts(r)[0]['message'] == '1 employees imported.' and 'hj:refresh-employees' in hx_triggers(r)
    assert b'1 row valid' in r.content
    # Importing the same file again skips the code that now exists and says so.
    r = h.post('/_/employees/import', {'csv': csv, 'action': 'import'}, **htmx())
    assert toasts(r)[0]['message'] == '0 employees imported.' and b'emp_code E010 already exists; skipped' in r.content


def test_hr_must_set_up_two_step_before_the_employee_list(world):
    w = world
    hr = make_user(w.company, roles=['hr'], email='needs2fa@test.local')
    c = client_for(hr)
    require_two_factor_for(w.company, 'hr')
    r = c.get('/employees')
    assert r.status_code == 302 and 'setup-two-step' in r['Location']
    # Employees are not affected.
    assert client_for(w.sita).get('/me/profile').status_code == 200
