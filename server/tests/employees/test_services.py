"""The employee master (apps/api/src/modules/employees/routes.ts) at service level."""
import pytest

from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.employees import service as emp
from tests.people.conftest import build_world
from tests.web.factories import make_user


@pytest.fixture
def world(db):
    return build_world()


def test_list_scoping_hr_read_all_manager_and_self(world):
    w = world
    assert {e['id'] for e in emp.list_employees(w.hr)} >= {w.sita_id, w.ram_id, w.peer_id}
    payroll = make_user(w.company, roles=['payroll_admin'])
    assert len(emp.list_employees(payroll)) == 3
    mine = {e['id'] for e in emp.list_employees(w.ram)}
    assert mine == {w.ram_id, w.sita_id}
    with pytest.raises(ForbiddenError, match='requires role hr or manager or payroll_admin or attendance_admin'):
        emp.list_employees(w.sita)
    assert emp.list_employees(w.foreign_hr) == []
    assert [e['id'] for e in emp.list_employees(w.hr, {'search': 'sita'})] == [w.sita_id]
    assert emp.list_employees(w.hr, {'search': 'si%a'}) == []
    assert emp.list_employees(w.hr, {'status': 'exited'}) == []


def test_pan_lookup_is_hr_only_and_audited(world):
    w = world
    emp.update_employee(w.hr, w.sita_id, {'pan': '123456789'})
    assert [e['id'] for e in emp.list_employees(w.hr, {'pan': '123456789'})] == [w.sita_id]
    assert rows("SELECT after FROM audit_log WHERE action = 'pan_lookup'")[0]['after'] == {'matches': 1}
    with pytest.raises(ForbiddenError, match='PAN lookup requires HR'):
        emp.list_employees(w.ram, {'pan': '123456789'})


def test_get_employee_scoping(world):
    w = world
    assert emp.get_employee(w.ram, w.sita_id)['name_en'] == 'Sita Sharma'
    with pytest.raises(ForbiddenError, match='not allowed to access this employee'):
        emp.get_employee(w.sita, w.peer_id)
    with pytest.raises(NotFoundError):
        emp.get_employee(w.foreign_hr, w.sita_id)
    payroll = make_user(w.company, roles=['payroll_admin'])
    assert emp.get_employee(payroll, w.peer_id)['emp_code'] == 'E077'


def test_create_validates_refs_pan_and_encrypts(world):
    w = world
    with pytest.raises(BadRequestError, match='branch does not exist in your company'):
        emp.create_employee(w.hr, {'empCode': 'X1', 'nameEn': 'X', 'branchId': str(one('SELECT gen_random_uuid() AS id')['id'])})
    with pytest.raises(BadRequestError, match='probation end cannot be before join date'):
        emp.create_employee(w.hr, {'empCode': 'X1', 'nameEn': 'X', 'joinDate': '2026-01-10', 'probationEnd': '2026-01-01'})
    with pytest.raises(ValidationError):
        emp.create_employee(w.hr, {'empCode': 'X1', 'nameEn': 'X', 'pan': '12'})
    new_id = emp.create_employee(w.hr, {'empCode': 'X1', 'nameEn': 'Xavier', 'pan': '987654321', 'bankAccount': '00112233445566'})
    row = one('SELECT pan_enc, bank_account_last4, regime, ot_eligible, weekly_off_days FROM employee WHERE id = %s', [new_id])
    assert b'987654321' not in bytes(row['pan_enc']) and row['bank_account_last4'] == '5566'
    assert row['regime'] == 'ssf' and row['ot_eligible'] is True and list(row['weekly_off_days']) == [6]
    assert emp.sensitive(w.hr, new_id) == {'pan': '987654321', 'bankAccount': '00112233445566'}
    assert 'read_sensitive' in [r['action'] for r in rows('SELECT action FROM audit_log WHERE row_id = %s', [new_id])]
    a = rows("SELECT after FROM audit_log WHERE action = 'create' AND table_name = 'employee' AND row_id = %s", [new_id])[0]['after']
    assert a['pan'] == '***' and a['bankAccount'] == '***'
    with pytest.raises(ConflictError, match='this PAN is already recorded for employee X1'):
        emp.create_employee(w.hr, {'empCode': 'X2', 'nameEn': 'Y', 'pan': '987654321'})
    with pytest.raises(ForbiddenError):
        emp.sensitive(w.ram, new_id)
    with pytest.raises(ForbiddenError):
        emp.create_employee(w.ram, {'empCode': 'X3', 'nameEn': 'Z'})


def test_update_assignment_is_effective_dated_and_exit_sets_status(world):
    w = world
    with pytest.raises(BadRequestError, match='an employee cannot be their own manager'):
        emp.update_employee(w.hr, w.peer_id, {'managerId': w.peer_id})
    before = rows('SELECT valid::text FROM employee_assignment WHERE employee_id = %s', [w.peer_id])
    assert len(before) == 1
    out = emp.update_employee(w.hr, w.peer_id, {'managerId': w.ram_id, 'employmentType': 'casual'})
    assert out['manager_id'] == w.ram_id and out['employment_type'] == 'casual'
    hist = emp.assignments(w.hr, w.peer_id)
    assert len(hist) == 2 and hist[0]['valid_to'] == npt_today() and hist[1]['valid_from'] == npt_today()
    # A second change the same day replaces today's period instead of adding another.
    emp.update_employee(w.hr, w.peer_id, {'employmentType': 'regular'})
    assert len(emp.assignments(w.hr, w.peer_id)) == 2
    with pytest.raises(BadRequestError, match='exit date cannot be before join date'):
        emp.update_employee(w.hr, w.peer_id, {'exitDate': '2024-01-01'})
    assert emp.update_employee(w.hr, w.peer_id, {'exitDate': '2026-12-31'})['status'] == 'exited'
    assert emp.update_employee(w.hr, w.peer_id, {'exitDate': None})['status'] == 'active'
    fields = [h['field'] for h in rows('SELECT field FROM employee_field_history WHERE employee_id = %s', [w.peer_id])]
    assert 'Manager' in fields and 'Employment type' in fields and 'Exit date' in fields
    with pytest.raises(NotFoundError):
        emp.update_employee(w.foreign_hr, w.peer_id, {'phone': '1'})


def test_update_pan_history_masks_it(world):
    w = world
    emp.update_employee(w.hr, w.sita_id, {'pan': '111222333'})
    h = one("SELECT old_value, new_value FROM employee_field_history WHERE employee_id = %s AND field = 'PAN'", [w.sita_id])
    assert h == {'old_value': None, 'new_value': '•••• 2333'}
    assert len(rows("SELECT 1 FROM employee_field_history WHERE employee_id = %s AND field = 'PAN'", [w.sita_id])) == 1


def test_import_csv_dry_run_errors_and_import(world):
    w = world
    with pytest.raises(BadRequestError, match='send CSV as text/csv body'):
        emp.import_csv(w.hr, '')
    with pytest.raises(BadRequestError, match='CSV needs a header row and at least one data row'):
        emp.import_csv(w.hr, 'emp_code,name_en\n')
    with pytest.raises(BadRequestError, match='missing column name_en'):
        emp.import_csv(w.hr, 'emp_code\nE1\n')
    bad = 'emp_code,name_en,email,pan\nE10,Hari,not-an-email,\nE11,Gita,,123456789\nE11,Gita again,,123456789\n'
    status, body = emp.import_csv(w.hr, bad, dry_run=True)
    assert status == 400 and body['imported'] == 0
    msgs = [e['message'] for e in body['errors']]
    assert any(m.startswith('email:') for m in msgs)
    assert 'duplicate emp_code in file: E11' in msgs and 'duplicate PAN in file: *****6789' in msgs
    ok = '﻿emp_code,name_en,join_date,designation,work_from_home\nE010,"Lal, Hari",2026-01-01,Accountant,yes\nE011,Sita B,,,\n'
    status, body = emp.import_csv(w.hr, ok, dry_run=True)
    assert (status, body) == (200, {'imported': 0, 'valid': 2, 'errors': []})
    status, body = emp.import_csv(w.hr, ok)
    assert status == 201 and body['imported'] == 2
    r = one("SELECT name_en, work_from_home FROM employee WHERE emp_code = 'E010' AND company_id = %s", [w.company.id])
    assert r == {'name_en': 'Lal, Hari', 'work_from_home': True}
    assert one("SELECT count(*)::int AS n FROM career_event c JOIN employee e ON e.id = c.employee_id WHERE e.emp_code IN ('E010', 'E011')")['n'] == 2
    status, body = emp.import_csv(w.hr, ok)
    assert status == 201 and body['imported'] == 0 and body['errors'][0]['message'] == 'emp_code E010 already exists; skipped'


def test_parse_csv_quotes_and_crlf():
    assert emp.parse_csv('a,b\r\n"x ""y""",z\n\n') == [['a', 'b'], ['x "y"', 'z']]


def test_audit_trail_for_self_and_hr(world):
    w = world
    emp.update_employee(w.hr, w.sita_id, {'phone': '9800'})
    assert emp.audit_events(w.sita, w.sita_id)[0]['action'] == 'update'
    with pytest.raises(ForbiddenError):
        emp.audit_events(w.peer, w.sita_id)
