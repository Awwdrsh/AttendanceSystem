"""Port of apps/api/test/biometrics.test.ts: consent, templates, shredding, DSAR, transparency and retention."""
import json
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from tests.support.sql import execute, one, rows
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.devices import biometrics as bio
from tests.web.factories import htmx, sign_in, toasts

OTHER = '00000000-0000-7000-8000-00000000bbbb'


def insert_template(demo, employee_id, modality):
    aad = f'template:{employee_id}:{modality}:attendance'
    import base64
    ct = field_cipher().encrypt(base64.b64encode(b'template-bytes').decode(), aad)
    r = one("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (%s, '1', %s, 'zk_fp_v10', %s, 1, 'h', %s) RETURNING id""", [employee_id, modality, ct, demo['device_id']])
    return str(r['id'])


@pytest.fixture
def hr(login_as):
    return login_as(['hr'])


@pytest.fixture
def sita(login_as, demo):
    return login_as(['employee'], demo['sita'])


@pytest.fixture
def ram(login_as, demo):
    return login_as(['employee'], demo['ram'])


def client_for(login):
    c = Client()
    sign_in(c, login.email)
    return c


# ---------------------------------------------------------------- consent, templates, shredding

def test_employee_grants_consent_sees_metadata_only_and_revocation_shreds_that_modality(demo, sita):
    consent = bio.grant_consent(sita.user, {'employeeId': demo['sita'], 'modality': 'fingerprint', 'noticeVersion': 'v1'})
    insert_template(demo, demo['sita'], 'fingerprint')
    insert_template(demo, demo['sita'], 'face')
    listed = bio.templates(sita.user, {'employeeId': demo['sita']})
    assert len(listed) == 2
    assert 'ciphertext' not in json.dumps(listed, default=str)
    revoke = bio.revoke_consent(sita.user, consent['consent']['id'])
    assert revoke == {'ok': True, 'templatesShredded': 1}
    r = rows("""SELECT modality, length(ciphertext) AS len, shredded_at IS NOT NULL AS shredded FROM biometric_template
                 WHERE employee_id = %s ORDER BY modality""", [demo['sita']])
    assert [(x['modality'], x['shredded']) for x in r] == [('face', False), ('fingerprint', True)]
    assert r[1]['len'] == 0
    assert r[0]['len'] > 29
    assert [a['action'] for a in rows("SELECT action FROM audit_log WHERE action IN ('consent_granted', 'consent_revoked') ORDER BY id")] == \
        ['consent_granted', 'consent_revoked']


def test_employee_cannot_see_consent_for_or_shred_another_employee_hr_can(demo, sita, ram, hr):
    tid = insert_template(demo, demo['ram'], 'face')
    with pytest.raises(ForbiddenError):
        bio.templates(sita.user, {'employeeId': demo['ram']})
    with pytest.raises(ForbiddenError):
        bio.grant_consent(sita.user, {'employeeId': demo['ram'], 'modality': 'face', 'noticeVersion': 'v1'})
    with pytest.raises(ForbiddenError):
        bio.shred_template(sita.user, tid)
    with pytest.raises(ForbiddenError):
        bio.shred_template(ram.user, tid)
    assert bio.shred_template(hr.user, tid) == {'ok': True, 'shredded': 1}
    assert bio.shred_template(hr.user, tid)['shredded'] == 0


def test_dsar_export_contains_only_the_requesters_own_data_and_is_audited(demo, sita, ram):
    execute("""INSERT INTO punch_event (device_id, device_sn, vendor, device_user_pin, employee_id, punched_at, device_local_time, local_date, direction, verify_mode, source, idempotency_key)
               VALUES (%s, 'DEMO-01', 'zkteco', '2', %s, '2026-09-21T04:32:03Z', '2026-09-21 10:17:03', '2026-09-21', 'in', 'face', 'zkteco', 'dsar-k1')""",
            [demo['device_id'], demo['ram']])
    with pytest.raises(ForbiddenError):
        bio.dsar(sita.user, {'employeeId': demo['ram']})
    # Through the download, as the My data page does.
    assert client_for(sita).get(f"/_/devices/biometrics/dsar?employeeId={demo['ram']}").status_code == 403
    res = client_for(ram).get(f"/_/devices/biometrics/dsar?employeeId={demo['ram']}")
    assert res.status_code == 200
    assert res['Content-Disposition'] == f'attachment; filename="my-data-{demo["ram"]}.json"'
    own = json.loads(res.content)
    assert own['employee']['id'] == demo['ram']
    assert len(own['punches']) == 1
    assert demo['sita'] not in res.content.decode()
    assert 'pan_enc' not in own['employee']
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'dsar_export' AND row_id = %s", [demo['ram']])) == 1


def test_transparency_is_bilingual_and_available_to_any_authenticated_user(demo, sita):
    anon = Client().get('/_/devices/biometrics/transparency', **htmx())
    assert anon.status_code == 401
    t = bio.transparency()
    assert '30 days' in t['retention']['templates']['en']
    assert '३०' in t['retention']['templates']['ne']
    assert len(t['withdraw']['ne']) > 10
    page = client_for(sita).get('/_/devices/biometrics/transparency', **htmx())
    assert page.status_code == 200
    body = page.content.decode()
    assert '30 days' in body and '३०' in body


def test_retention_run_shreds_templates_of_employees_exited_more_than_30_days_ago(demo, hr, sita):
    execute("UPDATE employee SET exit_date = %s, status = 'exited' WHERE id = %s", [npt_today() - timedelta(days=45), demo['ram']])
    tid = insert_template(demo, demo['ram'], 'palm')
    run = bio.run_retention(hr.user, {})
    assert run['employees'] == 1 and run['templatesShredded'] == 1
    assert one('SELECT shredded_at FROM biometric_template WHERE id = %s', [tid])['shredded_at'] is not None
    with pytest.raises(ForbiddenError):
        bio.run_retention(sita.user, {})
    assert client_for(sita).post('/_/devices/biometrics/retention/run', **htmx()).status_code == 403


# ---------------------------------------------------------------- authorisation edges

def test_manager_cannot_touch_biometrics_of_others_and_cross_company_ids_are_404_for_hr(demo, login_as, hr):
    mgr = login_as(['manager']).user
    with pytest.raises(ForbiddenError):
        bio.grant_consent(mgr, {'employeeId': demo['sita'], 'modality': 'face', 'noticeVersion': 'v1'})
    for fn in (bio.templates, bio.dsar, bio.consents):
        with pytest.raises(ForbiddenError):
            fn(mgr, {'employeeId': demo['sita']})
    execute("INSERT INTO company (id, code, name_en) VALUES (%s, 'OTHER', 'Other')", [OTHER])
    emp = str(one("INSERT INTO employee (company_id, emp_code, name_en) VALUES (%s, 'X1', 'Stranger') RETURNING id", [OTHER])['id'])
    for fn in (bio.templates, bio.consents, bio.dsar):
        with pytest.raises(NotFoundError):
            fn(hr.user, {'employeeId': emp})
    with pytest.raises(NotFoundError):
        bio.shred_all(hr.user, emp)
    c = client_for(hr)
    for path in ('/_/devices/biometrics/templates', '/_/devices/biometrics/consents', '/_/devices/biometrics/dsar'):
        assert c.get(f'{path}?employeeId={emp}', **htmx()).status_code == 404
    assert c.post(f'/_/devices/biometrics/employees/{emp}/shred-all', **htmx()).status_code == 404


def test_retention_run_is_idempotent_and_only_touches_employees_exited_past_the_grace_period(demo, hr):
    today = npt_today()
    exited = str(one("""INSERT INTO employee (company_id, emp_code, name_en, status, exit_date) VALUES (%s, 'EX1', 'Left', 'exited', %s) RETURNING id""",
                     [demo['company_id'], today - timedelta(days=45)])['id'])
    recent = str(one("""INSERT INTO employee (company_id, emp_code, name_en, status, exit_date) VALUES (%s, 'EX2', 'Just left', 'exited', %s) RETURNING id""",
                     [demo['company_id'], today - timedelta(days=5)])['id'])
    insert_template(demo, exited, 'face')
    insert_template(demo, recent, 'face')
    first = bio.run_retention(hr.user, {})
    assert first['employees'] == 1 and first['templatesShredded'] == 1
    second = bio.run_retention(hr.user, {})
    assert second['employees'] == 0 and second['templatesShredded'] == 0
    r = rows('SELECT shredded_at IS NOT NULL AS shredded FROM biometric_template WHERE employee_id = ANY(%s::uuid[])', [[exited, recent]])
    assert sorted(x['shredded'] for x in r) == [False, True]


# ---------------------------------------------------------------- the page actions

def test_consent_actions_through_the_page_endpoints(demo, sita, hr):
    c = client_for(sita)
    r = c.post('/_/devices/biometrics/consent', {'employee_id': demo['sita'], 'modality': 'face', 'notice_version': '2026-10'}, **htmx())
    assert r.status_code == 201 and toasts(r)[0]['message'] == 'Consent recorded.'
    frag = c.get(f"/_/devices/biometrics/consents?employeeId={demo['sita']}", **htmx())
    assert frag.status_code == 200 and b'Withdraw' in frag.content
    cid = one('SELECT id FROM consent_record WHERE employee_id = %s', [demo['sita']])['id']
    insert_template(demo, demo['sita'], 'face')
    r = c.post(f'/_/devices/biometrics/consent/{cid}/revoke', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Consent withdrawn. 1 template(s) deleted.'
    insert_template(demo, demo['sita'], 'palm')
    assert c.post(f"/_/devices/biometrics/employees/{demo['sita']}/shred-all", **htmx()).status_code == 403
    r = client_for(hr).post(f"/_/devices/biometrics/employees/{demo['sita']}/shred-all", **htmx())
    assert toasts(r)[0]['message'] == '1 template shredded.'
    t = client_for(hr).get(f"/_/devices/biometrics/templates?employeeId={demo['sita']}", **htmx())
    assert t.status_code == 200 and b'Shredded' in t.content and b'ciphertext' not in t.content


def test_unmapped_templates_and_stats_are_hr_only(demo, hr, sita):
    execute("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (NULL, '99', 'face', 'zk_fp_v10', '\\x01'::bytea, 1, 'h', %s)""", [demo['device_id']])
    assert [t['device_user_pin'] for t in bio.unmapped_templates(hr.user)] == ['99']
    assert bio.stats(hr.user) == [{'modality': 'face', 'active': 1, 'shredded': 0, 'unmapped': 1}]
    with pytest.raises(ForbiddenError):
        bio.unmapped_templates(sita.user)
    with pytest.raises(ForbiddenError):
        bio.stats(sita.user)
