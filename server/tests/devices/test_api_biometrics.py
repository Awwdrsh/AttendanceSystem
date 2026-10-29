"""The /api/v1/biometrics JSON endpoints (biometrics/routes.ts) as the React My data and employee pages call them: the
HTTP-level assertions of biometrics.test.ts and the biometrics case of endpoints.test.ts, plus response shapes."""
import json
from datetime import timedelta

import pytest

from hajiri.core.clock import npt_today
from tests.support.sql import execute, one, rows

from .jsonapi import Api
from .test_biometrics import insert_template

OTHER = '00000000-0000-7000-8000-00000000bbbb'
B = '/api/v1/biometrics'


@pytest.fixture
def hr(login_as):
    return Api(login_as(['hr']).token)


@pytest.fixture
def sita(login_as, demo):
    return Api(login_as(['employee'], demo['sita']).token)


@pytest.fixture
def ram(login_as, demo):
    return Api(login_as(['employee'], demo['ram']).token)


@pytest.fixture
def mgr(login_as):
    return Api(login_as(['manager']).token)


@pytest.fixture
def stranger(db):
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHER', 'Other') ON CONFLICT DO NOTHING", [OTHER])
    return str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Stranger') RETURNING id", [OTHER])['id'])


# ---------------------------------------------------------------- biometrics.test.ts

def test_employee_grants_consent_sees_metadata_only_and_revocation_shreds_that_modality(demo, sita):
    c = sita.post(f'{B}/consent', {'employeeId': demo['sita'], 'modality': 'fingerprint', 'noticeVersion': 'v1'})
    assert c.status_code == 201
    consent = c.json()['consent']
    assert set(consent) == {'id', 'grantedAt'} and consent['grantedAt'].endswith('Z')
    insert_template(demo, demo['sita'], 'fingerprint')
    insert_template(demo, demo['sita'], 'face')
    listed = sita.get(f'{B}/templates', {'employeeId': demo['sita']}).json()
    assert len(listed['templates']) == 2
    assert 'ciphertext' not in json.dumps(listed)
    assert set(listed['templates'][0]) == {'id', 'modality', 'vendor_format', 'device_user_pin', 'device_sn', 'key_version',
                                           'consent_id', 'created_at', 'shredded_at'}
    assert listed['templates'][0]['device_sn'] == 'DEMO-01'
    consents = sita.get(f'{B}/consent', {'employeeId': demo['sita']}).json()['consents']
    assert [x['id'] for x in consents] == [consent['id']]
    assert set(consents[0]) == {'id', 'purpose', 'modality', 'notice_version', 'granted_at', 'revoked_at'}
    assert consents[0]['purpose'] == 'attendance' and consents[0]['revoked_at'] is None
    r = sita.post(f"{B}/consent/{consent['id']}/revoke")
    assert r.status_code == 200 and r.json() == {'ok': True, 'templatesShredded': 1}
    t = rows('SELECT modality, length(ciphertext) AS len, shredded_at IS NOT NULL AS shredded FROM biometric_template '
             'WHERE employee_id = %s ORDER BY modality', [demo['sita']])
    assert [(x['modality'], x['shredded']) for x in t] == [('face', False), ('fingerprint', True)]
    assert t[0]['len'] > 29 and t[1]['len'] == 0
    audits = [a['action'] for a in rows("SELECT action FROM audit_log WHERE action IN ('consent_granted', 'consent_revoked') ORDER BY id")]
    assert audits == ['consent_granted', 'consent_revoked']


def test_an_employee_cannot_see_consent_for_or_shred_another_employee_hr_can(demo, hr, sita, ram):
    tid = insert_template(demo, demo['ram'], 'face')
    r = sita.get(f'{B}/templates', {'employeeId': demo['ram']})
    assert r.status_code == 403 and r.json() == {'error': 'not allowed to access this employee'}
    assert sita.post(f'{B}/consent', {'employeeId': demo['ram'], 'modality': 'face', 'noticeVersion': 'v1'}).status_code == 403
    assert sita.post(f'{B}/templates/{tid}/shred').status_code == 403
    assert ram.post(f'{B}/templates/{tid}/shred').status_code == 403
    assert hr.post(f'{B}/templates/{tid}/shred').json() == {'ok': True, 'shredded': 1}
    assert hr.post(f'{B}/templates/{tid}/shred').json()['shredded'] == 0


def test_dsar_export_contains_only_the_requesters_own_data_and_is_audited(demo, sita, ram):
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
               VALUES (%s, 'DEMO-01', 'zkteco', '2', %s, '2026-09-21T04:32:03Z', '2026-09-21 10:17:03', '2026-09-21', 'in', 'face', 'zkteco', 'dsar-k1')""",
            [demo['device_id'], demo['ram']])
    assert sita.get(f'{B}/dsar', {'employeeId': demo['ram']}).status_code == 403
    own = ram.get(f'{B}/dsar', {'employeeId': demo['ram']})
    assert own.status_code == 200
    body = own.json()
    assert body['employee']['id'] == demo['ram']
    assert len(body['punches']) == 1 and body['punches'][0]['punched_at'] == '2026-09-21T04:32:03.000Z'
    assert demo['sita'] not in own.content.decode()
    assert 'pan_enc' not in body['employee'] and body['employee']['has_pan'] in (True, False)
    assert set(body) == {'generatedAt', 'employee', 'consents', 'templates', 'attendance', 'punches', 'leaveLedger',
                         'requests', 'notifications', 'retention'}
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'dsar_export' AND row_id = %s", [demo['ram']])['n'] == 1


def test_transparency_is_bilingual_and_available_to_any_signed_in_user(demo, sita):
    assert Api(None).get(f'{B}/transparency').status_code == 401
    t = sita.get(f'{B}/transparency')
    assert t.status_code == 200
    body = t.json()
    assert '30 days' in body['retention']['templates']['en'] and '३०' in body['retention']['templates']['ne']
    assert len(body['withdraw']['ne']) > 10
    assert set(body) == {'stored', 'notStored', 'retention', 'access', 'withdraw', 'legalBasis'}


def test_retention_run_shreds_templates_of_employees_exited_more_than_30_days_ago(demo, hr, sita):
    execute("UPDATE employee SET exit_date = %s, status = 'exited' WHERE id = %s", [npt_today() - timedelta(days=45), demo['ram']])
    tid = insert_template(demo, demo['ram'], 'palm')
    r = hr.post(f'{B}/retention/run', {})
    assert r.status_code == 200 and r.json() == {'employees': 1, 'templatesShredded': 1, 'photosDeleted': 0}
    assert one('SELECT shredded_at FROM biometric_template WHERE id = %s', [tid])['shredded_at'] is not None
    assert sita.post(f'{B}/retention/run', {}).status_code == 403
    # An empty body is the TS `req.body ?? {}`; the grace period is validated.
    assert hr.post_raw(f'{B}/retention/run', '', 'application/json').status_code == 200
    bad = hr.post(f'{B}/retention/run', {'templateGraceDays': 400})
    assert bad.status_code == 400 and bad.json()['issues'][0]['path'] == 'templateGraceDays'


# ---------------------------------------------------------------- biometrics.test.ts: authorisation edges

def test_a_manager_cannot_reach_others_biometrics_and_cross_company_ids_are_404_for_hr(demo, hr, mgr, stranger):
    assert mgr.post(f'{B}/consent', {'employeeId': demo['sita'], 'modality': 'face', 'noticeVersion': 'v1'}).status_code == 403
    for path in ('templates', 'dsar', 'consent'):
        assert mgr.get(f'{B}/{path}', {'employeeId': demo['sita']}).status_code == 403, path
    for path in ('templates', 'consent', 'dsar'):
        r = hr.get(f'{B}/{path}', {'employeeId': stranger})
        assert r.status_code == 404 and r.json() == {'error': 'employee not found'}, path
    assert hr.post(f'{B}/employees/{stranger}/shred-all').status_code == 404
    assert hr.post(f'{B}/consent', {'employeeId': stranger, 'modality': 'face', 'noticeVersion': 'v1'}).status_code == 404
    foreign_consent = one("INSERT INTO consent_record (employee_id, purpose, modality, notice_version) VALUES (%s, 'attendance', 'face', 'v1') RETURNING id",
                          [stranger])['id']
    assert hr.post(f'{B}/consent/{foreign_consent}/revoke').json() == {'error': 'consent not found'}
    foreign_tpl = one("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash)
                         VALUES (%s, '1', 'face', 'zk_fp_v10', '\\x01'::bytea, 1, 'h') RETURNING id""", [stranger])['id']
    assert hr.post(f'{B}/templates/{foreign_tpl}/shred').status_code == 404
    assert one('SELECT shredded_at FROM biometric_template WHERE id = %s', [foreign_tpl])['shredded_at'] is None


def test_retention_run_is_idempotent_and_only_touches_employees_exited_past_the_grace_period(demo, hr):
    today = npt_today()
    exited = str(one("INSERT INTO employee (company_id, emp_code, name_en, status, exit_date) VALUES (%s, 'EX1', 'Left', 'exited', %s) RETURNING id",
                     [demo['company_id'], today - timedelta(days=45)])['id'])
    recent = str(one("INSERT INTO employee (company_id, emp_code, name_en, status, exit_date) VALUES (%s, 'EX2', 'Just left', 'exited', %s) RETURNING id",
                     [demo['company_id'], today - timedelta(days=5)])['id'])
    insert_template(demo, exited, 'face')
    insert_template(demo, recent, 'face')
    assert hr.post(f'{B}/retention/run', {}).json() == {'employees': 1, 'templatesShredded': 1, 'photosDeleted': 0}
    assert hr.post(f'{B}/retention/run', {}).json() == {'employees': 0, 'templatesShredded': 0, 'photosDeleted': 0}
    r = rows('SELECT employee_id, shredded_at IS NOT NULL AS shredded FROM biometric_template WHERE employee_id IN (%s, %s)', [exited, recent])
    assert {str(x['employee_id']): x['shredded'] for x in r} == {exited: True, recent: False}


def test_shred_all_revokes_consents_and_shreds_every_template(demo, hr, sita):
    sita.post(f'{B}/consent', {'employeeId': demo['sita'], 'modality': 'face', 'noticeVersion': 'v1'})
    insert_template(demo, demo['sita'], 'face')
    insert_template(demo, demo['sita'], 'palm')
    assert sita.post(f"{B}/employees/{demo['sita']}/shred-all").status_code == 403
    r = hr.post(f"{B}/employees/{demo['sita']}/shred-all")
    assert r.status_code == 200 and r.json() == {'ok': True, 'shredded': 2}
    assert all(c['revoked_at'] for c in sita.get(f'{B}/consent', {'employeeId': demo['sita']}).json()['consents'])
    assert hr.post(f'{B}/employees/nope/shred-all').json()['issues'][0]['message'] == 'Invalid UUID'


def test_validation_issues(demo, sita):
    r = sita.post(f'{B}/consent', {'employeeId': demo['sita'], 'modality': 'iris'})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert {i['path'] for i in r.json()['issues']} == {'modality', 'noticeVersion'}
    r = sita.get(f'{B}/templates')
    assert r.status_code == 400 and r.json()['issues'] == [{'path': 'employeeId', 'message': 'Required'}]
    assert sita.get(f'{B}/dsar', {'employeeId': 'nope'}).status_code == 400
    assert sita.post(f'{B}/consent/nope/revoke').status_code == 400


# ---------------------------------------------------------------- endpoints.test.ts: biometrics

def test_unmapped_templates_and_stats_are_hr_only(demo, hr, sita):
    execute("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (NULL, '99', 'fingerprint', 'zk_fp_v10', '\\x00'::bytea, 1, 'h', %s)""", [demo['device_id']])
    u = hr.get(f'{B}/templates/unmapped')
    assert u.status_code == 200
    t = u.json()['templates']
    assert len(t) == 1 and (t[0]['modality'], t[0]['device_user_pin'], t[0]['device_sn']) == ('fingerprint', '99', 'DEMO-01')
    assert 'ciphertext' not in t[0]
    s = hr.get(f'{B}/stats')
    assert s.status_code == 200
    assert s.json()['byModality'] == [{'modality': 'fingerprint', 'active': 1, 'shredded': 0, 'unmapped': 1}]
    assert sita.get(f'{B}/templates/unmapped').status_code == 403
    assert sita.get(f'{B}/stats').status_code == 403
