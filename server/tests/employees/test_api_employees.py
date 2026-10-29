"""
HTTP-level port of the employee master assertions of the TS tests (attendance, auth, fixes-payroll-auth, qa-foundation,
roles-flows, security-authz and work-from-home .test.ts): the /api/v1/employees routes the React app calls, their
status codes and JSON shapes ({employees}, {employee}, {id}, {pan, bankAccount}, {assignments}, {events}, import
results), and company isolation.
"""
import pytest
from django.test import Client

from tests.people.conftest import Api, build_world
from tests.support.sql import execute, one, rows
from tests.web.factories import make_employee, make_user


@pytest.fixture
def w(db):
    return build_world()


def test_anonymous_is_401_and_roles_are_enforced(w):
    assert Client().get('/api/v1/employees').status_code == 401
    hr, sita = Api(w.hr), Api(w.sita)
    assert hr.get('/employees').status_code == 200
    r = sita.get('/employees')
    assert r.status_code == 403 and r.json()['error'].startswith('requires role hr or manager')
    assert sita.get(f'/employees/{w.ram_id}').status_code == 403
    r = sita.get(f'/employees/{w.sita_id}')
    assert r.status_code == 200 and set(r.json()) == {'employee'} and r.json()['employee']['id'] == w.sita_id


def test_create_encrypts_pan_and_bank_duplicate_is_409_sensitive_is_audited(w):
    hr = Api(w.hr)
    shift = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'GEN', 'General', '09:00', '17:00') "
                    'RETURNING id', [w.company.id])['id'])
    body = {'empCode': 'E100', 'nameEn': 'Hari Lal', 'pan': '123456789', 'bankAccount': '00123456789012', 'regime': 'pf',
            'joinDate': '2026-09-01', 'shiftId': shift}
    c = hr.post('/employees', body)
    assert c.status_code == 201 and set(c.json()) == {'id'}
    new_id = c.json()['id']
    assert hr.post('/employees', body).status_code == 409
    raw = one('SELECT pan_enc, bank_account_last4 FROM employee WHERE id = %s', [new_id])
    assert '123456789' not in bytes(raw['pan_enc']).decode('latin1') and raw['bank_account_last4'] == '9012'
    sens = hr.get(f'/employees/{new_id}/sensitive')
    assert sens.json() == {'pan': '123456789', 'bankAccount': '00123456789012'}
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'read_sensitive' AND row_id = %s", [new_id])) == 1
    e = hr.get(f'/employees/{new_id}').json()['employee']
    assert e['shift_id'] == shift and e['has_pan'] is True and e['regime'] == 'pf' and 'pan_enc' not in e
    # Payroll admins read the list, not the sensitive fields, and cannot edit.
    pay = Api(make_user(w.company, roles=['payroll_admin']))
    assert pay.get('/employees').status_code == 200
    assert pay.get(f'/employees/{new_id}/sensitive').status_code == 403
    assert pay.patch(f'/employees/{new_id}', {'nameEn': 'X'}).status_code == 403


def test_csv_import_row_errors_and_success(w):
    hr = Api(w.hr)
    csv = 'emp_code,name_en,pan,join_date\nE200,Gita,223456789,2026-01-01\nE201,,12345,2026-13-40\n'
    dry = hr.client.post('/api/v1/employees/import?dryRun=true', csv, content_type='text/csv', **hr.headers)
    assert dry.status_code == 400 and [e['row'] for e in dry.json()['errors']] == [3, 3, 3]
    good = hr.client.post('/api/v1/employees/import', 'emp_code,name_en\nE300,Maya\n', content_type='text/csv', **hr.headers)
    assert good.status_code == 201 and good.json()['imported'] == 1
    # The React app sends {"csv": "..."} as JSON.
    js = hr.post('/employees/import?dryRun=true', {'csv': 'emp_code,name_en\nE301,Json\n'})
    assert js.status_code == 200 and js.json() == {'imported': 0, 'valid': 1, 'errors': []}
    bom = hr.client.post('/api/v1/employees/import', '﻿emp_code,name_en,name_ne\r\nC1,"Lama, Dorje",दोर्जे\r\n',
                         content_type='text/csv', **hr.headers)
    assert bom.status_code == 201 and bom.json()['imported'] == 1
    assert one("SELECT name_en, name_ne FROM employee WHERE emp_code = 'C1'") == {'name_en': 'Lama, Dorje', 'name_ne': 'दोर्जे'}
    r = hr.post('/employees/import', {})
    assert r.status_code == 400 and r.json()['error'] == 'send CSV as text/csv body or {"csv": "..."}'
    assert Api(w.sita).post('/employees/import', {'csv': 'emp_code,name_en\nX,Y\n'}).status_code == 403


def test_pan_already_recorded_is_refused_everywhere(w):
    hr = Api(w.hr)
    assert hr.patch(f'/employees/{w.sita_id}', {'pan': '123456789'}).status_code == 200
    assert hr.patch(f'/employees/{w.sita_id}', {'pan': '123456789'}).status_code == 200       # same person, same PAN
    upd = hr.patch(f'/employees/{w.ram_id}', {'pan': '123456789'})
    assert upd.status_code == 409 and 'E001' in upd.json()['error']
    assert hr.post('/employees', {'empCode': 'E900', 'nameEn': 'Copy Cat', 'pan': '123456789'}).status_code == 409
    imp = hr.client.post('/api/v1/employees/import', 'emp_code,name_en,pan\nE901,Dup One,987654321\nE902,Dup Two,987654321\nE903,Dup Three,123456789\n',
                         content_type='text/csv', **hr.headers)
    assert imp.status_code == 400
    msgs = ' '.join(e['message'] for e in imp.json()['errors'])
    assert 'duplicate PAN in file' in msgs and 'already recorded for E001' in msgs


def test_search_is_literal_and_pan_lookup_is_audited(w):
    hr = Api(w.hr)
    assert len(hr.get('/employees').json()['employees']) > 1
    for s in ('%', '_', '\\'):
        assert hr.get('/employees', search=s).json()['employees'] == []
    assert len(hr.get('/employees', search='sita').json()['employees']) == 1
    assert hr.post('/employees', {'empCode': 'P1', 'nameEn': 'Pan Person', 'pan': '987654321'}).status_code == 201
    assert [e['emp_code'] for e in hr.get('/employees', pan='987654321').json()['employees']] == ['P1']
    assert hr.get('/employees', pan='111111111').json()['employees'] == []
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'pan_lookup'")['n'] == 2
    r = Api(w.ram).get('/employees', pan='987654321')
    assert r.status_code == 403 and r.json() == {'error': 'PAN lookup requires HR'}
    assert hr.get('/employees', status='bogus').status_code == 400


def test_references_to_other_company_and_self_manager_rejected(w):
    hr = Api(w.hr)
    shift = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'X', 'x', '09:00', '17:00') RETURNING id",
                    [w.other.id])['id'])
    c = hr.post('/employees', {'empCode': 'T1', 'nameEn': 'T', 'shiftId': shift})
    assert c.status_code == 400 and 'shift does not exist' in c.json()['error']
    p = hr.patch(f'/employees/{w.sita_id}', {'managerId': w.sita_id})
    assert p.status_code == 400 and 'own manager' in p.json()['error']


def test_manager_sees_reports_only_and_other_tenants_are_not_found(w):
    mgr = Api(w.ram)
    assert mgr.get(f'/employees/{w.sita_id}').status_code == 200
    assert mgr.get(f'/employees/{w.sita_id}/audit').status_code == 200
    assert set(mgr.get(f'/employees/{w.sita_id}/audit').json()) == {'events'}
    assert mgr.get(f'/employees/{w.peer_id}').status_code == 403
    assert sorted(e['id'] for e in mgr.get('/employees').json()['employees']) == sorted([w.sita_id, w.ram_id])
    ghost = make_employee(w.other, code='O1', name='Outsider')
    assert mgr.get(f'/employees/{ghost.id}').status_code == 404
    assert Api(w.foreign_hr).get(f'/employees/{w.sita_id}').status_code == 404
    assert Api(w.foreign_hr).get(f'/employees/{w.sita_id}/assignments').status_code == 404
    assert Api(w.foreign_hr).get(f'/employees/{w.sita_id}/audit').status_code == 404
    assert Api(w.foreign_hr).get(f'/employees/{w.sita_id}/sensitive').status_code == 404
    assert Api(w.foreign_hr).patch(f'/employees/{w.sita_id}', {'grade': 'X'}).status_code == 404
    assert w.sita_id not in {e['id'] for e in Api(w.foreign_hr).get('/employees').json()['employees']}
    r = Api(w.hr).get('/employees/not-a-uuid')
    assert r.status_code == 400 and r.json()['error'] == 'validation'


def test_patch_changes_only_fields_sent_and_exit_dates(w):
    hr = Api(w.hr)
    execute("UPDATE employee SET ot_eligible = false, name_ne = 'पुरानो' WHERE id = %s", [w.ram_id])
    r = hr.patch(f'/employees/{w.ram_id}', {'nameNe': 'राम'})
    assert r.status_code == 200
    e = r.json()['employee']
    assert (e['name_ne'], e['ot_eligible'], e['weekly_off_days'], e['regime']) == ('राम', False, [6], 'ssf')
    assert hr.patch(f'/employees/{w.ram_id}', {'joinDate': '2026-03-01', 'exitDate': '2026-02-01'}).status_code == 400
    hr.patch(f'/employees/{w.ram_id}', {'joinDate': '2026-01-01'})
    assert hr.patch(f'/employees/{w.ram_id}', {'exitDate': '2025-12-31'}).status_code == 400
    assert hr.patch(f'/employees/{w.ram_id}', {'exitDate': '2026-12-31'}).json()['employee']['status'] == 'exited'
    assert hr.patch(f'/employees/{w.ram_id}', {'exitDate': None}).json()['employee']['status'] == 'active'
    # Work from home (work-from-home.test.ts).
    assert hr.patch(f'/employees/{w.ram_id}', {'workFromHome': True}).status_code == 200
    assert hr.get(f'/employees/{w.ram_id}').json()['employee']['work_from_home'] is True


def test_assignment_history(w):
    hr = Api(w.hr)
    hr.patch(f'/employees/{w.peer_id}', {'managerId': w.ram_id})
    r = hr.get(f'/employees/{w.peer_id}/assignments')
    assert r.status_code == 200 and set(r.json()) == {'assignments'}
    a = r.json()['assignments']
    assert a[-1]['manager_id'] == w.ram_id and a[-1]['valid'].endswith(',)')
    assert Api(w.sita).get(f'/employees/{w.peer_id}/assignments').status_code == 403
