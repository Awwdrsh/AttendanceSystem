"""
Pages and fragments checked against the TS web app after the port: the clock-in log labels (HomePage.tsx sourceLabel,
MyDataPage.tsx cap), the career form's manager change, the error toasts' own titles (toast.err(e, 'Not approved'),
'Not deleted', 'Photo not saved'), the family editor's nominee total and the org structure empty state.
"""
import base64

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from hajiri.core.clock import npt_today
from tests.support.sql import one
from hajiri.people import service as ppl
from hajiri.people.views import SOURCE_LABEL, cap
from tests.core.punches import insert_punch
from tests.web.factories import htmx, hx_triggers, sign_in, toasts

from .conftest import PDF


def client_for(user):
    c = Client()
    sign_in(c, user)
    return c


def test_clock_in_log_uses_the_ts_source_and_method_labels(world):
    w = world
    today = npt_today().isoformat()
    from tests.support.sql import execute
    from tests.core.punches import DEVICE
    execute("INSERT INTO device (id, company_id, serial_no, vendor, protocol, status) VALUES (%s, %s, 'DEMO-01', 'zkteco', 'adms', 'active')",
            [DEVICE, w.company.id])
    insert_punch(w.sita_id, today, '09:01', verify='fingerprint', source='device', key_prefix='a')
    insert_punch(w.sita_id, today, '18:02', verify='face_id', source='mobile', key_prefix='b')
    insert_punch(w.sita_id, today, '18:30', verify='card', source='zkteco', key_prefix='c')
    r = client_for(w.sita).get('/me/profile/privacy?tab=log')
    assert r.status_code == 200
    body = r.content.decode()
    assert 'Terminal' in body and 'Mobile' in body and 'Fingerprint' in body and 'Face id' in body
    assert 'zkteco' in body  # unknown sources are shown as stored, as sourceLabel does
    assert SOURCE_LABEL['manual'] == 'Added by HR' and SOURCE_LABEL['field'] == 'Field visit'
    assert cap('pin_code') == 'Pin code' and cap('faceID') == 'FaceID' and cap('') == ''


def test_manager_change_without_a_manager_is_refused(world):
    w = world
    h = client_for(w.hr)
    r = h.post(f'/_/people/{w.sita_id}/career/new', {'kind': 'manager_change', 'effectiveDate': npt_today().isoformat(), 'managerId': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'choose the new manager'
    r = h.post(f'/_/people/{w.sita_id}/career/new', {'kind': 'manager_change', 'effectiveDate': npt_today().isoformat(), 'managerId': w.peer_id},
               **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Recorded and applied.'
    a = one('SELECT manager_id FROM employee_assignment WHERE employee_id = %s AND valid @> %s::date', [w.sita_id, npt_today()])
    assert str(a['manager_id']) == w.peer_id


def test_error_toasts_carry_the_ts_titles(world):
    w = world
    # Maker-checker: the HR person who made a bank change cannot approve it ('Not approved').
    ppl.set_policy(w.hr, {'makerChecker': True})
    req = ppl.save_section(w.hr, w.sita_id, 'bank', {'bankCode': 'NABIL', 'bankAccount': '0011223344'})['request']
    r = client_for(w.hr).post(f"/_/people/requests/{req['id']}/approve", **htmx())
    assert r.status_code == 403
    t = toasts(r)[0]
    assert t['title'] == 'Not approved' and t['tone'] == 'err' and t['message']
    # The second HR person approves it.
    r = client_for(w.hr2).post(f"/_/people/requests/{req['id']}/approve", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Bank account updated for Sita Sharma.'
    # A unit people are assigned to cannot be deleted ('Not deleted').
    unit = ppl.create_unit(w.hr, {'kind': 'department', 'code': 'ENG', 'nameEn': 'Engineering'})
    from tests.support.sql import execute
    execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s', [unit['id'], w.sita_id])
    r = client_for(w.hr).post(f"/_/people/structure/unit/{unit['id']}/delete", **htmx())
    assert r.status_code == 409
    t = toasts(r)[0]
    assert t['title'] == 'Not deleted' and t['message'] == 'people are (or were) assigned to this unit; move them before deleting it'
    # Only images become a profile photo ('Photo not saved').
    pdf = base64.b64decode(PDF.split(',', 1)[1])
    r = client_for(w.sita).post('/_/people/me/avatar', {'file': SimpleUploadedFile('x.pdf', pdf, content_type='application/pdf')}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['title'] == 'Photo not saved'
    assert one('SELECT avatar_file_id FROM employee WHERE id = %s', [w.sita_id])['avatar_file_id'] is None


def test_family_editor_shows_the_nominee_total(world):
    w = world
    ppl.save_section(w.hr, w.sita_id, 'family', {'members': [
        {'name': 'Hari', 'relation': 'spouse', 'isNominee': True, 'nomineeShare': 60},
        {'name': 'Gita', 'relation': 'daughter', 'isNominee': True, 'nomineeShare': 40}]})
    r = client_for(w.sita).get(f'/_/people/{w.sita_id}/sections/family?mode=self', **htmx('hj-drawer-body'))
    body = r.content.decode()
    assert r.status_code == 200 and 'Nominee shares add up to' in body and 'they must total 100%.' in body
    assert '&quot;0&quot;: true' in body and '&quot;1&quot;: 40' in body
    # Other sections carry no nominee data.
    r = client_for(w.sita).get(f'/_/people/{w.sita_id}/sections/personal?mode=self', **htmx('hj-drawer-body'))
    assert 'Nominee shares add up to' not in r.content.decode()


def test_empty_org_structure_offers_to_add_a_department(world):
    w = world
    r = client_for(w.hr).get('/employees/structure')
    assert b'No departments yet' in r.content and b'Add a department' in r.content


def test_org_chart_highlight_does_not_replace_the_row_style(world):
    r = client_for(world.peer).get('/people/org-chart')
    body = r.content.decode()
    # A string :style would overwrite the indentation and x-show's display:none; the object form only sets background.
    assert ":style=\"{ background: hits().includes(" in body
    assert 'hj-hydro-soft' in body and hx_triggers(r) == {}
