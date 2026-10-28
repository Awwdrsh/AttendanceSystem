"""People pages through django.test.Client: directory, org chart, card, profile, sections, career, requests, structure."""
from django.test import Client

from hajiri.core.clock import npt_today
from tests.support.sql import one
from hajiri.people import service as ppl
from tests.web.factories import htmx, hx_triggers, sign_in, toasts

from .conftest import PNG, upload


def client_for(user):
    c = Client()
    sign_in(c, user)
    return c


def test_directory_and_org_chart_pages_for_everyone(world):
    w = world
    c = client_for(w.peer)
    r = c.get('/people')
    assert r.status_code == 200 and b'Sita Sharma' in r.content and b'Joined in the last 30 days' in r.content
    r = c.get('/people?search=nobody-here', **htmx('people-grid'))
    assert r.status_code == 200 and b'Nobody matches that search' in r.content
    r = c.get('/people/org-chart')
    assert r.status_code == 200 and b'Ram Bahadur' in r.content and b'1 report' in r.content
    r = c.get(f'/_/people/{w.ram_id}/card', **htmx())
    assert r.status_code == 200 and b'Direct reports (1)' in r.content and b'Open employee record' not in r.content
    assert client_for(w.hr).get(f'/_/people/{w.ram_id}/card', **htmx()).content.count(b'Open employee record') == 1
    assert client_for(w.foreign_hr).get(f'/_/people/{w.ram_id}/card', **htmx()).status_code == 404


def test_my_profile_sections_and_change_request_flow(world):
    w = world
    c = client_for(w.sita)
    r = c.get('/me/profile')
    assert r.status_code == 200 and b'Your account and employment details' in r.content and b'Change password' in r.content
    r = c.get(f'/_/people/{w.sita_id}/sections?mode=self', **htmx())
    assert r.status_code == 200 and b'Request a change' in r.content and b'Contact and address' in r.content
    r = c.get(f'/_/people/{w.sita_id}/sections/contact?mode=self', **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'Send to HR' in r.content
    r = c.post(f'/_/people/{w.sita_id}/sections/contact', {'mode': 'self', 'personalPhone': '+977 9800000001', 'perm_district': 'Lalitpur',
                                                           'perm_ward': '5', 'reason': 'Moved'}, **htmx())
    assert r.status_code == 204, r.content
    assert toasts(r)[0]['message'] == 'HR will review your change. You will be notified when it is decided.'
    assert 'hj:refresh-profile-sections' in hx_triggers(r)
    pending = one("SELECT id FROM profile_change_request WHERE employee_id = %s AND status = 'pending'", [w.sita_id])
    r = c.get('/_/people/me/requests', **htmx())
    assert b'Waiting for HR' in r.content and b'Personal phone' in r.content
    # HR approves it from the change requests page.
    h = client_for(w.hr)
    r = h.get('/employees/requests')
    assert r.status_code == 200 and b'Approve and apply' in r.content and b'Sita Sharma' in r.content
    r = h.post(f"/_/people/requests/{pending['id']}/approve", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Contact and address updated for Sita Sharma.'
    assert ppl.profile(w.sita, w.sita_id)['profile']['contact']['permanentAddress'] == {'district': 'Lalitpur', 'ward': 5}
    # Another employee cannot edit or read this profile.
    p = client_for(w.peer)
    assert p.get(f'/_/people/{w.sita_id}/sections?mode=self', **htmx()).status_code == 403
    assert p.post(f'/_/people/{w.sita_id}/sections/contact', {'mode': 'self'}, **htmx()).status_code == 403
    assert p.get('/employees/requests').status_code in (302, 403)


def test_hr_edits_sections_reveal_and_reject(world):
    w = world
    h = client_for(w.hr)
    r = h.post(f'/_/people/{w.sita_id}/sections/ids', {'mode': 'hr', 'pan': '123456789', 'citizenshipNo': '27-01-75-12345'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == '2 changes saved.'
    r = h.get(f'/_/people/{w.sita_id}/sections?mode=hr', **htmx())
    assert '•••• 6789'.encode() in r.content and b'123456789' not in r.content
    r = h.get(f'/_/people/{w.sita_id}/sections?mode=hr&reveal=1', **htmx())
    assert b'123456789' in r.content
    r = h.post(f'/_/people/{w.sita_id}/sections/family', {'mode': 'hr', 'm-0-name': 'Hari', 'm-0-relation': 'father', 'm-0-isNominee': 'on',
                                                          'm-0-nomineeShare': '60'}, **htmx())
    assert r.status_code == 400 and 'nominee shares must add up to 100%' in toasts(r)[0]['message']
    # A bank change request is declined with a reason.
    req = ppl.request_change(w.sita, {'section': 'bank', 'value': {'bankAccount': '0123456789012'}})
    r = h.get(f"/_/people/requests/{req['id']}/reveal", **htmx())
    assert b'0123456789012' in r.content
    r = h.post(f"/_/people/requests/{req['id']}/reject", {'reason': 'Account name does not match'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Request declined. The employee has been told why.'
    assert one('SELECT status FROM profile_change_request WHERE id = %s', [req['id']])['status'] == 'rejected'


def test_career_timeline_record_and_cancel(world):
    w = world
    h = client_for(w.hr)
    r = h.get(f'/_/people/{w.sita_id}/career/new?kind=separation', **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'Last working day' in r.content and b'Type of separation' in r.content
    from datetime import timedelta
    lwd = (npt_today() + timedelta(days=20)).isoformat()
    r = h.post(f'/_/people/{w.sita_id}/career/new', {'kind': 'separation', 'effectiveDate': lwd, 'separationType': 'resignation'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'].startswith('Scheduled for ')
    r = h.get(f'/_/people/{w.sita_id}/career?edit=1', **htmx())
    assert b'Left the company' in r.content and b'Cancel this change' in r.content
    ev = one("SELECT id FROM career_event WHERE employee_id = %s AND kind = 'separation'", [w.sita_id])
    r = h.post(f"/_/people/{w.sita_id}/career/{ev['id']}/cancel", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Scheduled change cancelled.'
    assert client_for(w.ram).post(f'/_/people/{w.sita_id}/career/new', {'kind': 'note', 'effectiveDate': lwd}, **htmx()).status_code == 403
    assert client_for(w.ram).get(f'/_/people/{w.sita_id}/career', **htmx()).status_code == 200
    assert client_for(w.peer).get(f'/_/people/{w.sita_id}/career', **htmx()).status_code == 403


def test_history_fragment_is_self_or_hr(world):
    w = world
    ppl.save_section(w.hr, w.sita_id, 'personal', {'bloodGroup': 'O+'})
    assert b'Blood group' in client_for(w.sita).get(f'/_/people/{w.sita_id}/history', **htmx()).content
    assert client_for(w.ram).get(f'/_/people/{w.sita_id}/history', **htmx()).status_code == 403


def test_avatar_upload_view_and_image(world):
    w = world
    from django.core.files.uploadedfile import SimpleUploadedFile
    import base64
    data = base64.b64decode(PNG.split(',', 1)[1])
    c = client_for(w.sita)
    r = c.post('/_/people/me/avatar', {'file': SimpleUploadedFile('me.png', data, content_type='image/png')}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Photo updated.'
    # AvatarEditor.tsx shrinks the photo to a 320 px square JPEG in the browser before uploading: the input is
    # processed by app.js (data-square-jpeg) and only then fires the HTMX request (hx-trigger="squared").
    assert b'data-square-jpeg="320"' in r.content and b'hx-trigger="squared"' in r.content
    assert b'hx-trigger="change"' not in r.content
    img =client_for(w.peer).get(f'/_/people/{w.sita_id}/avatar')
    assert img.status_code == 200 and img['Content-Type'].startswith('image/png')
    r = c.post('/_/people/me/avatar', {'remove': '1'}, **htmx())
    assert toasts(r)[0]['message'] == 'Photo removed.'
    assert client_for(w.peer).get(f'/_/people/{w.sita_id}/avatar').status_code == 404
    upload  # noqa: B018


def test_org_structure_page_create_edit_delete_and_policy(world):
    w = world
    h = client_for(w.hr)
    r = h.get('/employees/structure')
    assert r.status_code == 200 and b'No departments yet' in r.content and b'Second HR approval for bank and ID changes' in r.content
    r = h.post('/_/people/structure/unit/new', {'kind': 'department', 'code': 'eng', 'nameEn': 'Engineering'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Saved.' and 'hj:refresh-org-structure' in hx_triggers(r)
    unit = one("SELECT id, code FROM org_unit WHERE company_id = %s", [w.company.id])
    assert unit['code'] == 'ENG'
    r = h.post('/_/people/structure/unit/new', {'kind': 'team', 'code': 'ENG', 'nameEn': 'Again'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'another department or team already uses this code'
    r = h.get(f"/_/people/structure/unit/new?parent={unit['id']}", **htmx('hj-drawer-body'))
    assert b'New team' in r.content
    r = h.post(f"/_/people/structure/unit/{unit['id']}/edit", {'kind': 'department', 'code': 'ENG', 'nameEn': 'Engineering & IT'}, **htmx())
    assert r.status_code == 204
    r = h.post(f"/_/people/structure/unit/{unit['id']}/delete", **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Engineering & IT deleted.'
    r = h.post('/_/people/policy', {'makerChecker': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Second approval turned on.' and ppl.people_policy(str(w.company.id)) == {'makerChecker': True}
    r = h.post('/_/people/policy', {}, **htmx())
    assert toasts(r)[0]['message'] == 'Second approval turned off.'
    assert client_for(w.sita).post('/_/people/structure/unit/new', {'kind': 'team', 'code': 'X', 'nameEn': 'X'}, **htmx()).status_code == 403


def test_my_data_page_and_legacy_redirect(world):
    w = world
    c = client_for(w.sita)
    r = c.get('/me/profile/privacy')
    assert r.status_code == 200 and b'My data and privacy' in r.content
    assert c.get('/me/profile/privacy?tab=log').status_code == 200
    assert b'Nothing recorded in the last 14 days' in c.get('/me/profile/privacy?tab=log').content
    r = c.get('/me/data')
    assert r.status_code == 302 and r['Location'] == '/me/profile/privacy'
