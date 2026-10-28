"""Attendance actions in a group of companies: an employee of another company is not found, lists read the ACTIVE
company, and a member acts as the employee record of the company they work in."""
import pytest

from hajiri.attendance import api
from hajiri.core.errors import ForbiddenError, NotFoundError
from tests.multicompany.conftest import world  # noqa: F401  (fixture)

pytestmark = pytest.mark.django_db

DAY = '2026-09-21'


def test_an_employee_of_another_company_is_not_found(world):
    beta_emp = str(world.admin_b.employee_id)
    _, in_alpha = world.session(world.admin_a)
    with pytest.raises(NotFoundError):
        api.recompute(in_alpha, {'employeeId': beta_emp, 'localDate': DAY})
    with pytest.raises(NotFoundError):
        api.days(in_alpha, {'employeeId': beta_emp, 'from': DAY, 'to': DAY})
    with pytest.raises(NotFoundError):
        api.punches(in_alpha, {'employeeId': beta_emp, 'from': DAY, 'to': DAY})
    with pytest.raises(ForbiddenError, match='not in your company'):
        api.recompute_range(in_alpha, {'employeeIds': [beta_emp], 'from': DAY, 'to': DAY})
    _, in_beta = world.session(world.admin_a, world.beta)
    assert api.recompute(in_beta, {'employeeId': beta_emp, 'localDate': DAY})['changed'] is True


def test_team_today_lists_the_active_company(world):
    from hajiri.db.models import Employee
    _, in_alpha = world.session(world.admin_a)
    _, in_beta = world.session(world.admin_a, world.beta)
    for user, company in ((in_alpha, world.alpha), (in_beta, world.beta)):
        ids = {str(r['employee_id']) for r in api.today(user, {'date': DAY})['rows']}
        assert ids == {str(i) for i in Employee.objects.for_company(company.id).values_list('id', flat=True)}


def test_a_member_manages_with_the_record_of_the_company_they_work_in(world):
    from tests.web.factories import make_employee
    report = make_employee(world.beta, name='Reports to Dipak', manager=world.dual_beta_employee)
    _, dual_in_beta = world.session(world.dual, world.beta)
    assert {str(r['employee_id']) for r in api.today(dual_in_beta, {'date': DAY})['rows']} == {
        str(world.dual_beta_employee.id), str(report.id)}
    assert api.recompute(dual_in_beta, {'employeeId': str(report.id), 'localDate': DAY})['locked'] is False
