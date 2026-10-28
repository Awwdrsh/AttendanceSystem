"""Reports in a group of companies: every report, the builder and saved reports read the ACTIVE company only, and a
saved report or an employee of another company is not found."""
import pytest

from hajiri.core.errors import NotFoundError
from hajiri.reports import service
from hajiri.reports.people import people_report
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db


def _codes(rows):
    return {r['emp_code'] for r in rows}


def test_reports_and_the_builder_read_only_the_active_company(world):
    from hajiri.db.models import Employee
    alpha_codes = set(Employee.objects.for_company(world.alpha.id).values_list('emp_code', flat=True))
    beta_codes = set(Employee.objects.for_company(world.beta.id).values_list('emp_code', flat=True))
    _, in_alpha = world.session(world.admin_a)
    _, in_beta = world.session(world.admin_a, world.beta)
    for user, codes in ((in_alpha, alpha_codes), (in_beta, beta_codes)):
        built = service.run_adhoc(user, {'dataset': 'employees', 'columns': ['emp_code']})
        assert _codes(built['rows']) == codes
        assert _codes(people_report(user, 'addresses', {}).rows) == codes
        assert sum(r['headcount'] for r in service.attendance_report(user, 'headcount', {}).rows) == len(codes)
        assert _codes(service.attendance_report(user, 'daily', {'date': '2026-09-21'}).rows) == codes


def test_saved_reports_of_another_company_are_not_found(world):
    _, in_alpha = world.session(world.admin_a)
    saved = service.create_saved(in_alpha, {'name': 'Staff', 'shared': True,
                                            'definition': {'dataset': 'employees', 'columns': ['emp_code']}})
    for person, company in ((world.admin_a, world.beta), (world.admin_b, None), (world.admin_c, None)):
        _, user = world.session(person, company)
        with pytest.raises(NotFoundError, match='report not found'):
            service.run_saved(user, str(saved['id']))
        with pytest.raises(NotFoundError):
            service.delete_saved(user, str(saved['id']))
        assert service.list_saved(user) == []
    _, solo = world.session(world.solo)
    assert [r['name'] for r in service.list_saved(solo)] == ['Staff']


def test_an_employee_of_another_company_is_not_found(world):
    _, in_alpha = world.session(world.admin_a)
    q = {'employeeId': str(world.admin_b.employee_id), 'from': '2026-09-01', 'to': '2026-09-30'}
    with pytest.raises(NotFoundError):
        service.attendance_report(in_alpha, 'punch-audit', q)
    with pytest.raises(NotFoundError):
        service.attendance_report(in_alpha, 'register', {'bsYear': '2083', 'bsMonth': '6',
                                                         'employeeId': str(world.admin_b.employee_id)})
