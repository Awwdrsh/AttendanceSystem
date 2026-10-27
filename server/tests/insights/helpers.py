"""The engage.test.ts set-up the insights cases run on: two departments, Sita and Ram in Engineering, five more people."""
from datetime import date, timedelta

from django.test import Client

from hajiri.core import auth
from hajiri.core.clock import npt_today
from hajiri.core.db import execute, one
from tests.core.fixtures import PW


def add_days(ad: str, n: int) -> str:
    return (date.fromisoformat(ad) + timedelta(days=n)).isoformat()


def employee(demo, code, name, unit, manager_id=None, **cols) -> str:
    names = ''.join(f', {c}' for c in cols)
    marks = ''.join(', %s' for _ in cols)
    e = one(f'INSERT INTO employee (company_id, emp_code, name_en{names}) VALUES (%s, %s, %s{marks}) RETURNING id',
            [demo['company_id'], code, name, *cols.values()])
    execute("""INSERT INTO employee_assignment (employee_id, valid, branch_id, org_unit_id, manager_id, employment_type)
               VALUES (%s, daterange('2020-01-01', NULL), %s, %s, %s, 'regular')""", [e['id'], demo['branch_id'], unit, manager_id])
    return str(e['id'])


def client_for(login) -> Client:
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = login.token
    return c


def engage_world(demo, login_as) -> dict:
    today = npt_today().isoformat()
    dept_a = str(one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'ENG', 'Engineering') RETURNING id", [demo['company_id']])['id'])
    dept_b = str(one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'OPS', 'Operations') RETURNING id", [demo['company_id']])['id'])
    hr = login_as(['hr'])
    execute('UPDATE employee_assignment SET org_unit_id = %s, manager_id = %s WHERE employee_id = %s', [dept_a, demo['ram'], demo['sita']])
    execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s', [dept_a, demo['ram']])
    sita = login_as(['employee'], demo['sita'])
    ram = login_as(['employee', 'manager'], demo['ram'])
    extra = [employee(demo, f'E10{i}', f'Person {i}', dept_a if i < 3 else dept_b, demo['ram'] if i == 0 else None) for i in range(5)]
    # engage.test.ts celebrations: Person 4 joined three days ago.
    execute('UPDATE employee SET join_date = %s WHERE id = %s', [add_days(today, -3), extra[4]])
    c = str(one("INSERT INTO company (code, name_en) VALUES ('OTHERENG', 'Other Co') RETURNING id")['id'])
    auth.create_user(company_id=c, email='hr@other.engage', name='Other HR', password=PW, roles=['hr'])
    r = auth.login('hr@other.engage', PW)
    from tests.core.fixtures import Login
    foreign = Login(r.token, '', auth.resolve_session(r.token), 'hr@other.engage')
    return {'today': today, 'dept_a': dept_a, 'dept_b': dept_b, 'hr': hr, 'sita': sita, 'ram': ram, 'extra': extra, 'foreign': foreign}
