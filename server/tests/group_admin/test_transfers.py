"""Transfers to a sister company: exit in the source, a new record in the target, history, leave, salary, account."""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.company_settings.seed import seed_defaults
from hajiri.core import auth, settings_store
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.db.models import (AppUser, AuditLog, Branch, CareerEvent, CompanyMembership, Employee, EmployeeAssignment,
                              EmployeeExit, LeaveLedger, LeaveType, LifecycleCase, LifecycleTask, NotificationOutbox,
                              SalaryAssignment, SalaryStructure, SalaryStructureVersion)
from hajiri.group_admin import services
from tests.web.factories import PW, htmx, make_employee, make_user, toasts

from .conftest import actor


@pytest.fixture
def mover(world):
    """Ram, an employee of Alpha (home account) with history, a leave balance, a salary and a PAN."""
    seed_defaults(str(world.alpha.id))
    joined = npt_today() - timedelta(days=800)
    emp = make_employee(world.alpha, code='A-007', name='Ram Thapa', join_date=joined, designation='Accountant',
                        confirmed_on=joined + timedelta(days=180), since=joined)
    c = field_cipher()
    Employee.objects.filter(pk=emp.pk).update(pan_enc=c.encrypt('123456789', f'employee:{emp.pk}:pan'),
                                              pan_bidx=c.blind_index('123456789'))
    user = make_user(world.alpha, roles=['employee', 'manager'], employee=emp, email='ram@alpha.test')
    CareerEvent.objects.create(company=world.alpha, employee=emp, kind='hire', effective_date=joined,
                               details={'designation': 'Accountant'}, status='applied')
    CareerEvent.objects.create(company=world.alpha, employee=emp, kind='promotion', effective_date=joined + timedelta(days=400),
                               details={'designation': 'Senior accountant', 'designationId': '01a00000-0000-7000-8000-000000000000'},
                               status='applied')
    home = LeaveType.objects.for_company(world.alpha.id).get(code='HOME')
    LeaveLedger.objects.create(employee=emp, leave_type=home, effective_date=joined, delta_days=Decimal('12.5'), reason='accrual')
    LeaveLedger.objects.create(employee=emp, leave_type=home, effective_date=joined, delta_days=Decimal('-2'), reason='usage')
    for company in (world.alpha, world.beta):
        s = SalaryStructure.objects.create(company=company, code='TRF', name_en='Transfer test')
        SalaryStructureVersion.objects.create(structure=s, version=1, components=[{'code': 'BASIC'}])
    sv = SalaryStructureVersion.objects.get(structure__company=world.alpha, structure__code='TRF')
    SalaryAssignment.objects.create(employee=emp, structure_version=sv, valid=DateRange(joined, None),
                                    base_amounts={'BASIC': '45000.00'})
    return emp, user


def _body(world, emp, **kw):
    return {'employeeId': str(emp.id), 'toCompanyId': str(world.beta.id),
            'effectiveDate': (npt_today() + timedelta(days=10)).isoformat(), **kw}


def test_transfer_moves_the_employee_to_the_sister_company(world, mover):
    emp, user = mover
    hr = actor(world, world.ghr, world.alpha)
    eff = npt_today() + timedelta(days=10)
    r = services.transfer_employee(hr, _body(world, emp, empCode='B-101', carrySalary=True, note='Opening the Pokhara office'))
    new = Employee.objects.get(pk=r['toEmployeeId'])
    # The new record in Beta: same person, joins on the transfer date, confirmation kept.
    assert new.company_id == world.beta.id and new.emp_code == 'B-101' and new.join_date == eff
    assert new.name_en == 'Ram Thapa' and new.designation == 'Accountant' and new.confirmed_on == emp.confirmed_on
    assert field_cipher().decrypt(bytes(new.pan_enc), f'employee:{new.pk}:pan') == '123456789'
    assert EmployeeAssignment.objects.get(employee=new).valid.lower == eff
    # Source: an accepted exit the day before, through the usual exit flow (offboarding with final settlement).
    x = EmployeeExit.objects.get(employee=emp)
    assert x.status == 'accepted' and x.accepted_lwd == eff - timedelta(days=1) and x.reason_category == 'Transfer'
    assert x.kind in ('transfer', 'other') and x.reason.startswith('Transfer to Beta Foods')
    emp.refresh_from_db()
    assert emp.exit_date == eff - timedelta(days=1)
    case = LifecycleCase.objects.get(exit_id=x.id)
    assert case.kind == 'offboarding'
    assert LifecycleTask.objects.filter(case=case, title__icontains='final settlement').exists()
    # History: copied to the new record (without the old company's ids), plus a transfer event on both sides.
    events = list(CareerEvent.objects.filter(employee=new).order_by('effective_date', 'created_at').values_list('kind', 'details', 'note'))
    assert [e[0] for e in events] == ['hire', 'promotion', 'transfer']
    assert events[1][1] == {'designation': 'Senior accountant'} and events[1][2].startswith('At Alpha Traders')
    assert 'Opening the Pokhara office' in events[2][2]
    assert CareerEvent.objects.filter(employee=emp, kind='transfer').exists()
    # Salary carried on the target's structure with the same code.
    sa = SalaryAssignment.objects.get(employee=new)
    assert sa.structure_version.structure.company_id == world.beta.id and sa.base_amounts == {'BASIC': '45000.00'}
    assert r['salaryCarried'] is True and r['serviceStart'] == emp.join_date.isoformat()
    # The account: home and employee link move to Beta, employee access to Alpha with the old record until it ends.
    u = AppUser.objects.get(pk=user.id)
    assert u.company_id == world.beta.id and u.employee_id == new.id and u.roles == ['employee']
    ms = CompanyMembership.objects.get(user_id=user.id, company=world.alpha)
    assert ms.employee_id == emp.id and ms.roles == ['employee', 'manager']
    session = auth.resolve_session(auth.login('ram@alpha.test', PW).token)
    assert session.company_id == str(world.beta.id) and session.employee_id == str(new.id)
    assert {c.code for c in session.companies} == {'ALPHA', 'BETA'}
    # Audit on both sides, notifications to the person and the target's HR.
    out = AuditLog.objects.get(action='transfer_out', row_id=str(emp.id))
    assert out.after['toCompanyId'] == str(world.beta.id) and out.after['exitId'] == str(x.id)
    assert AuditLog.objects.filter(action='transfer_in', row_id=str(new.id)).exists()
    assert AuditLog.objects.filter(action='update', table_name='app_user', row_id=user.id).exists()
    assert NotificationOutbox.objects.filter(user_id=user.id, template='group.transfer').exists()
    assert NotificationOutbox.objects.filter(template='group.transfer_in', company_id=world.beta.id).exists()
    # The transfer is listed.
    listed = services.list_transfers(hr)
    assert listed[0]['name'] == 'Ram Thapa' and listed[0]['toCompany'] == 'Beta Foods'


def test_leave_carries_over_only_when_the_target_allows_it(world, mover):
    emp, _ = mover
    hr = actor(world, world.ghr, world.alpha)
    with pytest.raises(BadRequestError, match='does not carry leave balances'):
        services.transfer_employee(hr, _body(world, emp, carryLeave=True))
    admin = actor(world, world.admin_a)
    services.set_transfer_rule(admin, {'companyId': str(world.beta.id), 'carryLeaveBalance': True})
    assert settings_store.get_setting(world.beta.id, 'group.transfer') == {'carryLeaveBalance': True}
    r = services.transfer_employee(hr, _body(world, emp, carryLeave=True))
    assert r['leaveCarried'] == [{'code': 'HOME', 'name': 'Home leave', 'days': '10.5'}]
    from django.db.models import Sum
    assert LeaveLedger.objects.filter(employee=emp).aggregate(s=Sum('delta_days'))['s'] == 0
    target = LeaveLedger.objects.get(employee_id=r['toEmployeeId'])
    assert target.delta_days == Decimal('10.5') and target.reason == 'carry_forward'
    assert target.leave_type.company_id == world.beta.id


def test_without_carry_the_balance_stays_for_the_final_settlement(world, mover):
    emp, _ = mover
    r = services.transfer_employee(actor(world, world.ghr, world.alpha), _body(world, emp))
    assert not LeaveLedger.objects.filter(employee_id=r['toEmployeeId']).exists()
    from django.db.models import Sum
    assert LeaveLedger.objects.filter(employee=emp).aggregate(s=Sum('delta_days'))['s'] == Decimal('10.5')


def test_nothing_targets_a_company_outside_the_group(world, mover):
    emp, _ = mover
    hr = actor(world, world.ghr, world.alpha)
    with pytest.raises(NotFoundError, match='company not found'):
        services.transfer_employee(hr, _body(world, emp, toCompanyId=str(world.gamma.id)))
    with pytest.raises(NotFoundError, match='company not found'):
        services.target_options(hr, str(world.gamma.id))
    assert not Employee.objects.filter(company=world.gamma, name_en='Ram Thapa').exists()
    assert not EmployeeExit.objects.filter(employee=emp).exists()


def test_the_employee_must_be_of_the_active_company(world, mover):
    emp, _ = mover
    hr_in_beta = actor(world, world.ghr, world.beta)
    with pytest.raises(NotFoundError, match='employee not found'):
        services.transfer_employee(hr_in_beta, {**_body(world, emp), 'toCompanyId': str(world.alpha.id)})
    with pytest.raises(BadRequestError, match='sister company other than'):
        services.transfer_employee(actor(world, world.ghr, world.alpha), _body(world, emp, toCompanyId=str(world.alpha.id)))


def test_target_ids_are_checked_in_the_target_company(world, mover):
    emp, _ = mover
    hr = actor(world, world.ghr, world.alpha)
    alpha_branch = Branch.objects.create(company=world.alpha, code='KTM', name_en='Kathmandu')
    with pytest.raises(NotFoundError, match='branch not found'):
        services.transfer_employee(hr, _body(world, emp, branchId=str(alpha_branch.id)))
    alpha_boss = make_employee(world.alpha)
    with pytest.raises(NotFoundError, match='manager not found'):
        services.transfer_employee(hr, _body(world, emp, managerId=str(alpha_boss.id)))
    beta_branch = Branch.objects.create(company=world.beta, code='PKR', name_en='Pokhara')
    r = services.transfer_employee(hr, _body(world, emp, branchId=str(beta_branch.id)))
    assert EmployeeAssignment.objects.get(employee_id=r['toEmployeeId']).branch_id == beta_branch.id


def test_who_may_transfer(world, mover):
    emp, _ = mover
    for person in (world.solo, world.gexec, world.admin_b):
        with pytest.raises(ForbiddenError, match='requires role group_hr'):
            services.transfer_employee(actor(world, person), _body(world, emp))
    # Group admins may; nobody transfers themselves.
    admin = actor(world, world.admin_a)
    me = Employee.objects.get(pk=admin.employee_id)
    with pytest.raises(ForbiddenError, match='your own transfer'):
        services.transfer_employee(admin, _body(world, me))
    services.transfer_employee(admin, _body(world, emp))


def test_conflicts(world, mover):
    emp, _ = mover
    hr = actor(world, world.ghr, world.alpha)
    make_employee(world.beta, code='A-007')
    with pytest.raises(ConflictError, match='A-007 is already used in Beta Foods'):
        services.transfer_employee(hr, _body(world, emp))
    with pytest.raises(BadRequestError, match='within a year'):
        services.transfer_employee(hr, _body(world, emp, empCode='X1', effectiveDate=(npt_today() + timedelta(days=400)).isoformat()))
    with pytest.raises(BadRequestError, match='days in the past'):
        services.transfer_employee(hr, _body(world, emp, empCode='X1', effectiveDate=(npt_today() - timedelta(days=60)).isoformat()))
    services.transfer_employee(hr, _body(world, emp, empCode='X1'))
    with pytest.raises((BadRequestError, ConflictError)):
        services.transfer_employee(hr, _body(world, emp, empCode='X2'))
    # Someone who already has an employee record in the target cannot get a second one there.
    dual_alpha = Employee.objects.get(pk=world.dual.employee_id)
    with pytest.raises(ConflictError, match='already has an employee record in Beta Foods'):
        services.transfer_employee(hr, _body(world, dual_alpha))


def test_a_member_moves_with_their_membership(world):
    """Dipak works in Beta by membership: moved to Alpha, his home, the new record becomes his home employee."""
    hr = actor(world, world.ghr, world.beta)
    old_alpha = world.dual.employee_id
    Employee.objects.filter(pk=old_alpha).update(status='exited', exit_date=npt_today() - timedelta(days=5))
    AppUser.objects.filter(pk=world.dual.id).update(employee=None)
    r = services.transfer_employee(hr, {**_body(world, world.dual_beta_employee), 'toCompanyId': str(world.alpha.id)})
    assert AppUser.objects.get(pk=world.dual.id).employee_id == Employee.objects.get(pk=r['toEmployeeId']).id
    assert CompanyMembership.objects.get(user_id=world.dual.id, company=world.beta).employee_id == world.dual_beta_employee.id


# ---------------------------------------------------------------- pages and JSON

def test_transfer_pages(world, mover):
    emp, _ = mover
    c = world.signed_in(world.ghr, world.alpha)
    html = c.get('/group/transfers').content.decode()
    assert 'New transfer' in html and 'Beta Foods' in html and 'Gamma Outside' not in html
    opts = c.get(f'/_/group-admin/transfers/target?to_company_id={world.beta.id}', **htmx()).content.decode()
    assert 'does not carry leave balances' in opts
    assert c.get(f'/_/group-admin/transfers/target?to_company_id={world.gamma.id}', **htmx()).status_code == 404
    r = c.post('/_/group-admin/transfers/new', {'employee_id': str(emp.id), 'to_company_id': str(world.gamma.id),
                                                'effective_date': (npt_today() + timedelta(days=3)).isoformat()}, **htmx())
    assert r.status_code == 422
    r = c.post('/_/group-admin/transfers/new', {'employee_id': str(emp.id), 'to_company_id': str(world.beta.id),
                                                'effective_date': (npt_today() + timedelta(days=3)).isoformat()}, **htmx())
    assert r.status_code == 204 and r['HX-Redirect'] == '/group/transfers'
    assert 'Ram Thapa moves to Beta Foods' in toasts(r)[0]['message']
    assert 'Ram Thapa' in c.get('/group/transfers').content.decode()
    # Group admins set the transfer rules; group HR cannot.
    r = c.post('/_/group-admin/transfers/rules', {'company_id': str(world.beta.id), 'carry_leave_balance': 'on'}, **htmx())
    assert r.status_code == 403
    a = world.signed_in(world.admin_a)
    assert a.post('/_/group-admin/transfers/rules', {'company_id': str(world.beta.id), 'carry_leave_balance': 'on'}, **htmx()).status_code == 204
    assert services.carries_leave(world.beta.id)
    assert a.post('/_/group-admin/transfers/rules', {'company_id': str(world.gamma.id)}, **htmx()).status_code == 400


def test_transfer_api(world, mover):
    emp, _ = mover
    c = world.signed_in(world.ghr, world.alpha)
    r = c.post('/api/v1/group/transfers', _body(world, emp, toCompanyId=str(world.gamma.id)), content_type='application/json')
    assert r.status_code == 404 and r.json() == {'error': 'company not found'}
    r = c.post('/api/v1/group/transfers', _body(world, emp), content_type='application/json')
    assert r.status_code == 201 and r.json()['toCompany'] == 'Beta Foods'
    assert c.get('/api/v1/group/transfers').json()['items'][0]['fromEmpCode'] == 'A-007'
    assert world.signed_in(world.solo).get('/api/v1/group/transfers').status_code == 403
