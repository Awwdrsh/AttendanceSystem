"""The people of the demo companies: departments, employees with designations and line managers, and the HR officer
who signs in. `staff_company` puts a roster into a company (adding only what is missing), so the demo suite has a
company shaped like the one it was written for: four departments, line managers and at least eight active people."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date, time

from django.db import transaction
from django.db.backends.postgresql.psycopg_any import DateRange

from hajiri.core.auth import create_user
from hajiri.db.models import AppUser, Branch, Employee, EmployeeAssignment, OrgUnit, Shift, ShiftAssignment

# code, name, gender, designation, department, manager's code, join date
Person = tuple[str, str, str, str, str, str | None, str]


@dataclass(frozen=True)
class Roster:
    units: tuple[tuple[str, str], ...]          # (code, name) of each department
    people: tuple[Person, ...]
    hr_code: str                                # the employee who is the HR officer
    hr_first: str                               # their sign-in is <hr_first>@<domain>
    hr_roles: tuple[str, ...]


UNITS = (('PRD', 'Engineering'), ('SAL', 'Sales & growth'), ('FIN', 'Finance'), ('OPS', 'Operations'))

# The main demo company: the DEMO data set's Sita and Ram plus twelve more.
MAIN = Roster(units=UNITS, hr_code='E012', hr_first='laxmi', hr_roles=('employee', 'hr', 'admin'), people=(
    ('E001', 'Sita Sharma', 'female', 'Head of Engineering', 'PRD', None, '2024-01-15'),
    ('E002', 'Ram Thapa', 'male', 'Backend Engineer', 'PRD', 'E001', '2024-03-01'),
    ('E003', 'Anita Gurung', 'female', 'Platform Engineer', 'PRD', 'E001', '2024-06-10'),
    ('E004', 'Bikash Shrestha', 'male', 'Site Reliability Engineer', 'PRD', 'E001', '2025-01-05'),
    ('E005', 'Sunita Rai', 'female', 'Network Technician', 'PRD', 'E001', '2025-02-01'),
    ('E006', 'Prakash Adhikari', 'male', 'Sales Manager', 'SAL', None, '2024-02-01'),
    ('E007', 'Kiran KC', 'male', 'Enterprise Sales', 'SAL', 'E006', '2024-08-12'),
    ('E008', 'Manisha Tamang', 'female', 'Customer Success', 'SAL', 'E006', '2025-04-20'),
    ('E009', 'Rajesh Karki', 'male', 'Finance Manager', 'FIN', None, '2024-01-20'),
    ('E010', 'Sarita Bhattarai', 'female', 'Accountant', 'FIN', 'E009', '2025-05-11'),
    ('E011', 'Deepak Magar', 'male', 'Office Administrator', 'OPS', 'E009', '2025-06-01'),
    ('E012', 'Laxmi Poudel', 'female', 'HR Officer', 'OPS', 'E009', '2024-04-01'),
    ('E013', 'Nabin Basnet', 'male', 'Frontend Engineer', 'PRD', 'E001', '2026-08-01'),
    ('E014', 'Puja Lama', 'female', 'Support Engineer', 'PRD', 'E001', '2026-09-15'),
))

# The sister company of the demo group: datacenter hardware and networking. Its finance manager, Rajesh Karki, is the
# main company's finance manager too (one sign-in, an employee record in each company).
SISTER = Roster(units=UNITS, hr_code='S010', hr_first='sabina', hr_roles=('employee', 'hr'), people=(
    ('S001', 'Binod Maharjan', 'male', 'Head of Engineering', 'PRD', None, '2024-02-01'),
    ('S002', 'Sushma Khadka', 'female', 'Network Engineer', 'PRD', 'S001', '2024-05-10'),
    ('S003', 'Roshan Shakya', 'male', 'Datacenter Technician', 'PRD', 'S001', '2024-09-01'),
    ('S004', 'Asmita Joshi', 'female', 'Support Engineer', 'PRD', 'S001', '2025-03-15'),
    ('S005', 'Suman Bajracharya', 'male', 'Backend Engineer', 'PRD', 'S001', '2025-07-01'),
    ('S006', 'Gita Neupane', 'female', 'Sales Manager', 'SAL', None, '2024-03-01'),
    ('S007', 'Hari Dahal', 'male', 'Enterprise Sales', 'SAL', 'S006', '2024-11-11'),
    ('S008', 'Rajesh Karki', 'male', 'Finance Manager', 'FIN', None, '2024-06-01'),
    ('S009', 'Mina Ghimire', 'female', 'Accountant', 'FIN', 'S008', '2025-01-20'),
    ('S010', 'Sabina Chaudhary', 'female', 'HR Officer', 'OPS', 'S008', '2024-04-15'),
    ('S011', 'Kamal Oli', 'male', 'Office Administrator', 'OPS', 'S008', '2025-08-01'),
    ('S012', 'Rekha Bista', 'female', 'Customer Success', 'SAL', 'S006', '2026-07-20'),
))
SHARED = ('E009', 'S008')       # (main code, sister code) of the person who works in both companies

ASSIGNED_FROM = date(2024, 1, 1)


def head_office(company_id: str) -> tuple[str, str]:
    """The company's head office branch and general shift, created when missing (as the DEMO seed has them)."""
    branch = Branch.objects.for_company(company_id).filter(code='HQ').first() or Branch.objects.create(
        company_id=company_id, code='HQ', name_en='Head Office')
    shift = Shift.objects.for_company(company_id).filter(code='GEN').first() or Shift.objects.create(
        company_id=company_id, code='GEN', name_en='General 10-18', start_time=time(10), end_time=time(18), grace_in_min=15,
        early_in_window_min=60, late_out_window_min=120, half_day_threshold_min=240, absent_threshold_min=120)
    return str(branch.pk), str(shift.pk)


@transaction.atomic
def staff_company(company_id: str, roster: Roster, *, branch_id: str | None = None, shift_id: str | None = None) -> dict:
    """Put a roster into a company: its departments, its people (an employee already there under the same code keeps
    their record and gets the roster's designation, join date and gender), each new person's assignment and shift
    from 2024, and everyone's department and line manager. Returns {'units': {code: id}, 'employees': {code: id}}."""
    if branch_id is None or shift_id is None:
        branch_id, shift_id = head_office(company_id)
    units = {}
    for code, name in roster.units:
        unit, _ = OrgUnit.objects.for_company(company_id).get_or_create(
            code=code, defaults={'company_id': company_id, 'kind': OrgUnit.Kind.DEPARTMENT, 'name_en': name})
        units[code] = str(unit.pk)
    have = dict(Employee.objects.for_company(company_id).filter(emp_code__in=[p[0] for p in roster.people])
                .values_list('emp_code', 'id'))
    ids: dict[str, str] = {}
    since = DateRange(ASSIGNED_FROM, None)
    for code, name, gender, designation, _unit, _mgr, join in roster.people:
        if code in have:
            ids[code] = str(have[code])
            Employee.objects.filter(pk=have[code]).update(designation=designation, join_date=join, gender=gender)
            continue
        e = Employee.objects.create(company_id=company_id, emp_code=code, name_en=name, gender=gender, designation=designation,
                                    join_date=join)
        ids[code] = str(e.pk)
        EmployeeAssignment.objects.create(employee=e, valid=since, branch_id=branch_id,
                                          employment_type=EmployeeAssignment.EmploymentType.REGULAR)
        ShiftAssignment.objects.create(employee=e, shift_id=shift_id, valid=since)
    for code, _name, _g, _d, unit, mgr, _join in roster.people:
        EmployeeAssignment.objects.filter(employee_id=ids[code]).update(org_unit_id=units[unit], manager_id=ids[mgr] if mgr else None)
    return {'units': units, 'employees': ids}


def ensure_hr(company_id: str, roster: Roster, domain: str, *, password: str | None = None) -> tuple[AppUser, bool]:
    """The roster's HR officer's account (<hr_first>@<domain>), created with a random password (or `password`) when the
    employee has none. Returns (account, created)."""
    emp = Employee.objects.for_company(company_id).get(emp_code=roster.hr_code)
    found = AppUser.objects.filter(employee=emp).first()
    if found:
        return found, False
    user = create_user(company_id=company_id, employee_id=str(emp.pk), email=f'{roster.hr_first}@{domain}', name=emp.name_en,
                       password=password or secrets.token_urlsafe(18), roles=list(roster.hr_roles), must_change_password=False)
    return AppUser.objects.get(pk=user.id), True
