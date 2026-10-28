"""Devices page details checked in the completeness audit: BS validity labels, the replace confirmation tied to its
PIN, live tab counts, and the biometric page actions not covered elsewhere."""
import pytest
from django.test import Client

from tests.support.sql import one
from hajiri.core.errors import ValidationError
from hajiri.devices import biometrics as bio
from hajiri.devices import services as dev
from hajiri.devices.views import valid_label
from hajiri.web.format import fmt_date
from tests.web.factories import htmx, sign_in, toasts


@pytest.fixture
def hr(login_as):
    return login_as(['hr'])


def client_for(login):
    c = Client()
    sign_in(c, login.email)
    return c


def test_mapping_validity_is_shown_in_bikram_sambat():
    assert valid_label('[2026-09-01,)') == f"From {fmt_date('2026-09-01')}"
    assert valid_label('[2026-09-01,2026-10-01)') == f"{fmt_date('2026-09-01')} to {fmt_date('2026-09-30')}"
    assert 'Bhadra' in valid_label('[2026-09-01,)')
    assert valid_label('empty') == 'empty'


def test_drawer_mappings_show_bs_dates(demo, hr):
    dev.map_pin(hr.user, demo['device_id'], {'pin': '55', 'employeeId': demo['ram'], 'from': '2026-09-01'})
    body = client_for(hr).get(f"/_/devices/{demo['device_id']}/mappings", **htmx()).content.decode()
    assert f"From {fmt_date('2026-09-01')}" in body and '[2026-09-01,)' not in body


def test_replace_confirmation_does_not_carry_over_to_an_edited_pin(demo, hr):
    """DevicesPage.tsx drops `replace` when the PIN changes: the second PIN must be confirmed on its own."""
    c = client_for(hr)
    # PIN 1 is Sita's and PIN 2 is Ram's (demo seed). Ask for PIN 1 to Ram: conflict, confirmation shown.
    data = {'device_id': demo['device_id'], 'pin': '1', 'employee_id': demo['ram'], 'valid_from': '2026-09-05'}
    r = c.post('/_/devices/map', data, **htmx())
    assert r.status_code == 422 and b'name="replace_pin" value="1"' in r.content
    # The user edits the PIN to 2 (someone else's) and resubmits the same confirmation: asked again, nothing moved.
    edited = {**data, 'pin': '2', 'employee_id': demo['sita'], 'replace': '1', 'replace_pin': '1'}
    r = c.post('/_/devices/map', edited, **htmx())
    assert r.status_code == 422 and b'already mapped to Ram' in r.content
    holder = one("SELECT employee_id FROM device_user_map WHERE device_id = %s AND device_user_pin = '2' AND upper_inf(valid)",
                 [demo['device_id']])
    assert str(holder['employee_id']) == demo['ram']
    # Confirming for the PIN that was asked about goes through.
    r = c.post('/_/devices/map', {**data, 'replace': '1', 'replace_pin': '1'}, **htmx())
    assert r.status_code == 204


def test_tab_counts_reload_after_changes(demo, hr):
    body = client_for(hr).get('/settings/devices').content.decode()
    assert 'id="count-devices"' in body and 'hj:refresh-devices from:body' in body
    assert 'id="count-unmapped"' in body and 'hj:refresh-unmapped from:body' in body


def test_unmapped_punch_dates_are_bs(demo, hr):
    from tests.core.punches import insert_punch
    insert_punch(None, '2026-09-28', '10:00', '7')
    body = client_for(hr).get('/settings/devices').content.decode()
    assert fmt_date('2026-09-28') in body


def test_hr_shreds_one_template_and_runs_retention_from_the_page(demo, hr, login_as):
    from tests.devices.test_biometrics import insert_template
    tid = insert_template(demo, demo['sita'], 'face')
    c = client_for(hr)
    r = c.post(f'/_/devices/biometrics/templates/{tid}/shred', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '1 template shredded.'
    r = c.post(f'/_/devices/biometrics/templates/{tid}/shred', **htmx())
    assert toasts(r)[0]['message'] == '0 templates shredded.'
    r = c.post('/_/devices/biometrics/retention/run', **htmx())
    assert r.status_code == 204 and 'Retention run' in toasts(r)[0]['message']
    acts = one("SELECT array_agg(action ORDER BY id) AS a FROM audit_log WHERE action IN ('template_shred', 'retention_run')")['a']
    assert acts == ['template_shred', 'template_shred', 'retention_run']
    sita = login_as(['employee'], demo['sita'])
    assert client_for(sita).post(f'/_/devices/biometrics/templates/{tid}/shred', **htmx()).status_code == 403
    mgr = login_as(['manager'])
    assert client_for(mgr).get(f"/_/devices/biometrics/consents?employeeId={demo['sita']}", **htmx()).status_code == 403


def test_biometric_inputs_are_validated_like_the_ts_schemas(demo, hr):
    for body in ({'employeeId': demo['sita'], 'modality': 'iris', 'noticeVersion': 'v1'},
                 {'employeeId': demo['sita'], 'modality': 'face', 'noticeVersion': ''},
                 {'employeeId': 'not-a-uuid', 'modality': 'face', 'noticeVersion': 'v1'},
                 {'employeeId': demo['sita'], 'modality': 'face', 'noticeVersion': 'v1', 'purpose': ''}):
        with pytest.raises(ValidationError):
            bio.grant_consent(hr.user, body)
    with pytest.raises(ValidationError):
        bio.run_retention(hr.user, {'templateGraceDays': 400})
    assert bio.grant_consent(hr.user, {'employeeId': demo['sita'], 'modality': 'card', 'noticeVersion': 'v1'})['consent']['id']
    row = one('SELECT purpose, recorded_by FROM consent_record WHERE employee_id = %s', [demo['sita']])
    assert row['purpose'] == 'attendance' and str(row['recorded_by']) == hr.user_id
    c = client_for(hr)
    assert c.post('/_/devices/biometrics/consent', {'employee_id': demo['sita'], 'modality': 'iris', 'notice_version': 'v1'},
                  **htmx()).status_code == 400
    assert c.get('/_/devices/biometrics/templates?employeeId=nope', **htmx()).status_code == 400
