"""A company shaped like the one the demo suite was written for: four departments, line managers, an HR user who has
set a password, and fourteen active employees (the DEMO data set's Sita and Ram plus twelve more)."""
import pytest

from tests.core.fixtures import *  # noqa: F401,F403

from hajiri.core import auth
from hajiri.core.db import execute, one

PEOPLE = [
    # code, name, gender, designation, unit, manager, join date
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
]
UNITS = [('PRD', 'Engineering'), ('SAL', 'Sales & growth'), ('FIN', 'Finance'), ('OPS', 'Operations')]
HR_EMAIL = 'laxmi@corevalley.test'
HR_PASSWORD = 'correct-horse-battery-staple'


@pytest.fixture
def suite_company(demo):
    """Company DEMO filled out for the demo suite. Returns {'company_id', 'employees': {code: id}, 'hr_user_id'}."""
    cid = demo['company_id']
    units = {code: str(one("""INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', %s, %s) RETURNING id""",
                           [cid, code, name])['id']) for code, name in UNITS}
    ids: dict[str, str] = {'E001': demo['sita'], 'E002': demo['ram']}
    for code, name, gender, designation, unit, _mgr, join in PEOPLE:
        if code in ids:
            execute('UPDATE employee SET designation = %s, join_date = %s, gender = %s WHERE id = %s', [designation, join, gender, ids[code]])
            continue
        ids[code] = str(one("""INSERT INTO employee (company_id, emp_code, name_en, gender, designation, join_date)
                               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""", [cid, code, name, gender, designation, join])['id'])
        execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, employment_type) VALUES (%s, daterange('2024-01-01', NULL), %s, 'regular')""",
                [ids[code], demo['branch_id']])
        execute("""INSERT INTO shift_assignment (employee_id, shift_id, valid) VALUES (%s, %s, daterange('2024-01-01', NULL))""",
                [ids[code], demo['shift_id']])
    for code, _name, _g, _d, unit, mgr, _join in PEOPLE:
        execute('UPDATE employee_assignment SET org_unit_id = %s, manager_id = %s WHERE employee_id = %s',
                [units[unit], ids[mgr] if mgr else None, ids[code]])
    hr = auth.create_user(company_id=cid, employee_id=ids['E012'], email=HR_EMAIL, name='Laxmi Poudel', password=HR_PASSWORD,
                          roles=['employee', 'hr', 'admin'], must_change_password=False)
    return {'company_id': cid, 'employees': ids, 'units': units, 'hr_user_id': hr.id}
