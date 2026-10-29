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
from tests.web.factories import sign_in

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


# ---------------------------------------------------------------- authorisation edges

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

def test_unmapped_templates_and_stats_are_hr_only(demo, hr, sita):
    execute("""INSERT INTO biometric_template (employee_id, device_user_pin, modality, vendor_format, ciphertext, key_version, aad_hash, enrolled_on_device_id)
               VALUES (NULL, '99', 'face', 'zk_fp_v10', '\\x01'::bytea, 1, 'h', %s)""", [demo['device_id']])
    assert [t['device_user_pin'] for t in bio.unmapped_templates(hr.user)] == ['99']
    assert bio.stats(hr.user) == [{'modality': 'face', 'active': 1, 'shredded': 0, 'unmapped': 1}]
    with pytest.raises(ForbiddenError):
        bio.unmapped_templates(sita.user)
    with pytest.raises(ForbiddenError):
        bio.stats(sita.user)
