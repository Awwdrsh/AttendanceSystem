"""
Groups of companies: the employee pages and the /api/v1/employees JSON API treat an employee of another company as
not found, and lists show only the active company's people.
"""
import pytest

from hajiri.core import auth
from hajiri.employees import service as emp
from tests.people.group import build_group
from tests.web.factories import PW, bearer, htmx


@pytest.fixture
def g(db):
    return build_group()


def _requests(eid):
    return [('get', f'/employees/{eid}', None), ('get', f'/employees/{eid}?tab=history', None),
            ('get', f'/_/employees/{eid}/edit', None), ('post', f'/_/employees/{eid}/edit', {'empCode': 'B100', 'nameEn': 'Bina B'}),
            ('get', f'/_/employees/{eid}/overview', None), ('post', f'/_/employees/{eid}/sensitive', {})]


def _call(c, method, url, data):
    return c.post(url, data or {}, **htmx()) if method == 'post' else c.get(url, **htmx())


def test_employee_of_another_company_is_not_found(g):
    eid = str(g.beta_emp.id)
    for person in (g.admin_c, g.admin_a):
        c = g.client(person)
        for method, url, data in _requests(eid):
            assert _call(c, method, url, data).status_code == 404, (person.email, url)
    owner = g.client(g.admin_b)
    for method, url, data in _requests(eid):
        assert _call(owner, method, url, data).status_code != 404, url


def test_lists_and_references_stay_in_the_active_company(g):
    a = g.session(g.admin_a)
    codes = {e['emp_code'] for e in emp.list_employees(a)}
    assert 'A100' in codes and 'B100' not in codes
    # A Beta branch, manager or shift cannot be used for an Alpha employee.
    with pytest.raises(Exception) as e:
        emp.update_employee(a, str(g.alpha_emp.id), {'branchId': str(g.beta_branch.id)})
    assert e.value.message == 'branch does not exist in your company'
    with pytest.raises(Exception) as e:
        emp.update_employee(a, str(g.alpha_emp.id), {'managerId': str(g.beta_emp.id)})
    assert e.value.message == 'manager does not exist in your company'
    # Switched to Beta, the group admin lists Beta's people.
    b = g.session(g.admin_a, g.beta)
    codes = {e['emp_code'] for e in emp.list_employees(b)}
    assert 'B100' in codes and 'A100' not in codes
    # The same employee code may exist in both companies.
    new_id = emp.create_employee(b, {'empCode': 'A100', 'nameEn': 'Another A100'})
    assert emp.get_employee(b, new_id)['company_id'] == str(g.beta.id)


def test_manager_by_membership_lists_their_beta_team(g):
    in_beta = g.session(g.dual, g.beta)
    assert in_beta.employee_id == str(g.dual_beta.id)
    assert [e['emp_code'] for e in emp.list_employees(in_beta) if e['emp_code'] == 'B100'] == ['B100']
    assert emp.get_employee(in_beta, str(g.beta_emp.id))['name_en'] == 'Bina Beta'


def test_json_api(g):
    tok_a = auth.login(g.admin_a.email, PW).token
    c = g.client(g.admin_a)
    r = c.get('/api/v1/employees', **bearer(tok_a))
    body = r.json()
    assert r.status_code == 200 and set(body) == {'items', 'total', 'page', 'pageSize'}
    assert 'A100' in {e['emp_code'] for e in body['items']} and 'B100' not in {e['emp_code'] for e in body['items']}
    assert c.get(f'/api/v1/employees/{g.beta_emp.id}', **bearer(tok_a)).status_code == 404
    assert c.get(f'/api/v1/employees/{g.beta_emp.id}/assignments', **bearer(tok_a)).status_code == 404
    assert c.patch(f'/api/v1/employees/{g.beta_emp.id}', {'grade': 'X'}, content_type='application/json',
                   **bearer(tok_a)).status_code == 404
    r = c.post('/api/v1/employees', {'empCode': 'API1', 'nameEn': 'From the API', 'joinDate': '2026-01-01'},
               content_type='application/json', **bearer(tok_a))
    assert r.status_code == 201
    new_id = r.json()['id']
    r = c.get(f'/api/v1/employees/{new_id}', **bearer(tok_a))
    assert r.status_code == 200 and r.json()['company_id'] == str(g.alpha.id)
    r = c.patch(f'/api/v1/employees/{new_id}', {'grade': 'G2'}, content_type='application/json', **bearer(tok_a))
    assert r.status_code == 200 and r.json()['grade'] == 'G2'
    r = c.post('/api/v1/employees', {'empCode': '', 'nameEn': 'x'}, content_type='application/json', **bearer(tok_a))
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert r.json()['issues'][0] == {'path': 'empCode', 'message': 'Too small: expected string to have >=1 characters'}
    r = c.post('/api/v1/employees/import?dryRun=true', 'emp_code,name_en\nCSV1,From CSV\n', content_type='text/csv', **bearer(tok_a))
    assert r.status_code == 200 and r.json()['valid'] == 1
    # An employee (no HR, no manager) may not list the master.
    tok_e = auth.login(g.ghr.email, PW).token        # group HR is HR in Alpha too: allowed
    assert g.client(g.ghr).get('/api/v1/employees', **bearer(tok_e)).status_code == 200
