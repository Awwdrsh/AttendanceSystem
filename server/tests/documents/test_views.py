"""Pages and HTMX actions of the documents app through django.test.Client (MyDocumentsPage, DocumentsAdminPage,
EmployeeDocumentsTab and their drawers)."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core.db import one
from hajiri.documents import services as docs
from hajiri.documents.letters import npt_today
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts

from .world import add_days, build, employee_uploads, outbox, upload, welcome_template

pytestmark = pytest.mark.django_db


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


# ================================================================ pages

def test_my_documents_page_shows_policies_documents_and_letters():
    w = build()
    employee_uploads(w)
    docs.publish_policy(w.hr, {'title': 'Employee handbook', 'fileId': upload(w.hr, 'company_document')})
    docs.issue_letter(w.hr, {'templateId': w.template('confirmation'), 'employeeId': w.sita_id})
    r = client_for(w.sita).get('/me/profile/documents')
    assert r.status_code == 200
    html = r.content.decode()
    for text in ('Company policies', '1 waiting for you to read and acknowledge', 'Employee handbook', 'I have read this',
                 'My documents', 'Citizenship', 'Awaiting HR check', 'Letters from HR', 'Request a letter',
                 'Confirmation of employment'):
        assert text in html, text


def test_my_documents_empty_states():
    w = build()
    html = client_for(w.peer).get('/me/profile/documents').content.decode()
    assert 'No documents yet' in html
    assert 'Upload your citizenship, PAN card and academic certificates so HR has them on file.' in html
    assert 'Letters HR issues to you, such as a salary certificate, appear here.' in html
    assert 'Company policies' not in html


def test_feature_off_hides_documents():
    w = build()
    set_features_off(w.co, 'documents')
    c = client_for(w.sita)
    r = c.get('/me/profile/documents')
    assert r.status_code == 302 and r['Location'] == '/'
    assert c.get('/_/documents/docs', **htmx()).status_code == 404


def test_admin_page_sections_for_hr_only():
    w = build()
    employee_uploads(w)
    c = client_for(w.hr)
    r = c.get('/employees/documents')
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Documents to verify' in html and 'Citizenship' in html and 'Sita Sharma' in html
    assert 'Expiring in 30 days' in html and 'No open letter requests' in html
    for section, text in (('letters', 'No letters yet'), ('templates', 'Letterhead and signature'),
                          ('policies', 'No policies published'), ('categories', 'Employment contract')):
        r = c.get(f'/employees/documents?section={section}')
        assert r.status_code == 200 and text in r.content.decode(), section
    assert 'Appointment letter' in c.get('/employees/documents?section=templates').content.decode()
    assert client_for(w.sita).get('/employees/documents', **htmx()).status_code == 403


def test_employee_tab_for_hr():
    w = build()
    employee_uploads(w)
    c = client_for(w.hr)
    r = c.get(f'/_/documents/employee/{w.sita_id}', **htmx())
    assert r.status_code == 200
    html = r.content.decode()
    assert 'Show earlier versions' in html and 'Verify' in html and 'Issue a letter' in html
    assert client_for(w.ram).get(f'/_/documents/employee/{w.sita_id}', **htmx()).status_code == 403
    assert client_for(w.foreign_hr).get(f'/_/documents/employee/{w.sita_id}', **htmx()).status_code == 404


# ================================================================ employee documents

def test_employee_uploads_through_the_drawer():
    w = build()
    c = client_for(w.sita)
    form = c.get('/_/documents/docs/upload?mode=self', **htmx('hj-drawer-body'))
    assert form.status_code == 200
    html = form.content.decode()
    assert 'Upload for HR check' in html and 'Employment contract' not in html and 'Citizenship certificate' in html
    # The expiry label says whether the chosen type needs one, as the TS DateField label did.
    assert "'Expiry date (required)' : 'Expiry date (optional)'" in html and 'for="doc-expiry"' in html
    # Removing the file disables the save button again.
    assert "closest('.hj-file button')) file = ''" in html
    r = c.post('/_/documents/docs/upload', {'mode': 'self', 'category_id': w.category(w.sita, 'citizenship'), 'title': '',
                                            'file_id': upload(w.sita), 'expiry_date': '', 'notes': ''}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == 'Uploaded. HR will check it shortly.'
    assert 'hj:refresh-docs-grid' in hx_triggers(r)
    mine = docs.my_documents(w.sita)
    assert mine[0]['title'] == 'Citizenship certificate' and mine[0]['status'] == 'pending'
    # An employee cannot switch to HR mode to upload for someone else.
    r = c.post('/_/documents/docs/upload', {'mode': 'hr', 'employee_id': w.peer_id, 'category_id': w.category(w.sita, 'other'),
                                            'file_id': upload(w.sita)}, **htmx())
    assert r.status_code == 204
    assert docs.my_documents(w.peer) == []


def test_upload_errors_show_as_toasts():
    w = build()
    c = client_for(w.sita)
    r = c.post('/_/documents/docs/upload', {'mode': 'self', 'category_id': w.category(w.sita, 'passport'), 'title': 'Passport',
                                            'file_id': upload(w.sita)}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Passport needs an expiry date'


def test_hr_uploads_for_an_employee_hidden_from_them():
    w = build()
    c = client_for(w.hr)
    form = c.get(f'/_/documents/docs/upload?mode=hr&employee_id={w.sita_id}', **htmx('hj-drawer-body')).content.decode()
    assert 'Employee can see this document' in form and 'Employment contract' in form
    r = c.post('/_/documents/docs/upload', {'mode': 'hr', 'employee_id': w.sita_id, 'category_id': w.category(w.hr, 'contract'),
                                            'title': 'Signed contract', 'file_id': upload(w.hr)}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Document saved.'
    d = docs.employee_documents(w.hr, w.sita_id)[0]
    assert d['visible_to_employee'] is False and d['status'] == 'verified'
    assert docs.my_documents(w.sita) == []


def test_new_version_form_keeps_the_type():
    w = build()
    doc = employee_uploads(w)
    html = client_for(w.sita).get(f'/_/documents/docs/upload?mode=self&replaces={doc}', **htmx('hj-drawer-body')).content.decode()
    assert 'New version of Citizenship' in html and f'name="replaces_id" value="{doc}"' in html


def test_documents_grid_and_history_switch():
    w = build()
    doc = employee_uploads(w)
    docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'citizenship'), 'title': 'Citizenship',
                                          'fileId': upload(w.sita), 'replacesId': doc})
    c = client_for(w.hr)
    now = c.get(f'/_/documents/docs?mode=hr&employee_id={w.sita_id}', **htmx()).content.decode()
    assert 'Earlier version' not in now and 'version 2' in now
    hist = c.get(f'/_/documents/docs?mode=hr&employee_id={w.sita_id}&history=1', **htmx()).content.decode()
    assert 'Earlier version' in hist
    # The employee's own grid never shows HR controls.
    mine = client_for(w.sita).get(f'/_/documents/docs?mode=hr&employee_id={w.peer_id}', **htmx()).content.decode()
    assert 'Show earlier versions' not in mine and 'Citizenship' in mine


def test_open_and_download_files():
    w = build()
    doc = employee_uploads(w)
    r = client_for(w.sita).get(f'/_/documents/docs/{doc}/file')
    assert r.status_code == 200 and r['Content-Type'] == 'application/pdf' and r['Content-Disposition'].startswith('inline')
    r = client_for(w.hr).get(f'/_/documents/docs/{doc}/file?download=1')
    assert r.status_code == 200 and r['Content-Disposition'].startswith('attachment')
    assert client_for(w.peer).get(f'/_/documents/docs/{doc}/file').status_code == 404
    assert client_for(w.ram).get(f'/_/documents/docs/{doc}/file').status_code == 404
    # The TS API address still opens it.
    assert client_for(w.sita).get(f'/api/v1/documents/{doc}/file').status_code == 200


def test_hr_verifies_and_sends_back_from_the_review_list():
    w = build()
    doc = employee_uploads(w)
    c = client_for(w.hr)
    r = c.post(f'/_/documents/docs/{doc}/verify', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Citizenship verified.'
    assert 'hj:refresh-doc-review' in hx_triggers(r)
    assert client_for(w.sita).post(f'/_/documents/docs/{doc}/verify', **htmx()).status_code == 403
    doc2 = docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'pan_card'), 'title': 'PAN card',
                                                 'fileId': upload(w.sita)})['id']
    form = c.get(f'/_/documents/docs/{doc2}/reject?from=review', **htmx('hj-drawer-body')).content.decode()
    assert 'Send back PAN card' in form and 'Sita Sharma will be asked to upload it again with your reason.' in form
    r = c.post(f'/_/documents/docs/{doc2}/reject', {'from': 'review', 'reason': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'give a reason so the employee knows what to fix'
    r = c.post(f'/_/documents/docs/{doc2}/reject', {'from': 'review', 'reason': 'Blurry'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Sent back to the employee.'
    doc3 = docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'academic'), 'title': 'Degree',
                                                 'fileId': upload(w.sita)})['id']
    r = c.post(f'/_/documents/docs/{doc3}/reject', {'reason': 'Wrong file'}, **htmx())
    assert toasts(r)[0]['message'] == 'The employee has been asked to upload it again.'


def test_hr_edits_document_details():
    w = build()
    doc = employee_uploads(w)
    c = client_for(w.hr)
    assert 'Edit document' in c.get(f'/_/documents/docs/{doc}/edit', **htmx('hj-drawer-body')).content.decode()
    exp = add_days(npt_today(), 100)
    r = c.post(f'/_/documents/docs/{doc}/edit', {'title': 'Citizenship (both sides)', 'expiry_date': exp, 'notes': ''}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Document updated.'
    d = one('SELECT title, expiry_date::text AS e, visible_to_employee, notes FROM employee_document WHERE id = %s', [doc])
    assert d == {'title': 'Citizenship (both sides)', 'e': exp, 'visible_to_employee': False, 'notes': None}
    assert client_for(w.sita).post(f'/_/documents/docs/{doc}/edit', {'title': 'X'}, **htmx()).status_code == 403


def test_employee_removes_own_pending_upload_only():
    w = build()
    doc = employee_uploads(w)
    assert client_for(w.peer).post(f'/_/documents/docs/{doc}/delete', **htmx()).status_code == 404
    r = client_for(w.sita).post(f'/_/documents/docs/{doc}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Document removed.'
    assert docs.my_documents(w.sita) == []


# ================================================================ categories, settings, templates

def test_hr_adds_and_toggles_document_types():
    w = build()
    c = client_for(w.hr)
    assert 'New document type' in c.get('/_/documents/categories/new', **htmx('hj-drawer-body')).content.decode()
    r = c.post('/_/documents/categories/new', {'name': 'Work permit', 'code': 'work_permit', 'expiry_required': '1',
                                               'employee_can_upload': '1', 'visible_to_employee': '1'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Document type added.'
    cat = next(x for x in docs.list_categories(w.hr) if x['code'] == 'work_permit')
    assert cat['expiry_required'] is True
    r = c.post(f"/_/documents/categories/{cat['id']}/toggle", {'key': 'active'}, **htmx())
    assert r.status_code == 204 and 'hj:refresh-doc-categories' in hx_triggers(r)
    assert next(x for x in docs.list_categories(w.hr) if x['code'] == 'work_permit')['active'] is False
    r = c.post(f"/_/documents/categories/{cat['id']}/toggle", {'key': 'active', 'value': '1'}, **htmx())
    assert next(x for x in docs.list_categories(w.hr) if x['code'] == 'work_permit')['active'] is True
    assert 'Work permit' in c.get('/_/documents/categories', **htmx()).content.decode()


def test_letter_settings_save():
    w = build()
    c = client_for(w.hr)
    r = c.post('/_/documents/letter-settings', {'signatory_name': 'Gita HR', 'signatory_title': 'HR Manager', 'ref_prefix': 'CV/HR'},
               **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Letter settings saved.'
    assert docs.get_letter_settings(w.hr) == {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager', 'refPrefix': 'CV/HR'}
    r = c.post('/_/documents/letter-settings', {'ref_prefix': 'no spaces'}, **htmx())
    assert r.status_code == 400


def test_hr_creates_and_edits_templates_with_a_live_preview():
    w = build()
    c = client_for(w.hr)
    form = c.get('/_/documents/templates/new', **htmx('hj-drawer-body')).content.decode()
    assert 'New letter template' in form and '{{employee.name}}' in form and 'Insert a detail' in form
    r = c.post('/_/documents/templates/new', {'code': 'bad_one', 'name': 'Bad', 'subject': 'S', 'body': 'Hello {{employee.nmae}}'},
               **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Unknown placeholder: employee.nmae'
    r = c.post('/_/documents/templates/new', {'code': 'welcome', 'name': 'Welcome note', 'subject': 'Welcome {{employee.first_name}}',
                                              'body': 'Welcome to {{company.name}}.', 'visible_to_employee': '1'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Template saved.'
    tid = w.template('welcome')
    edit = c.get(f'/_/documents/templates/{tid}', **htmx('hj-drawer-body')).content.decode()
    assert 'Edit Welcome note' in edit and 'Active' in edit
    r = c.post(f'/_/documents/templates/{tid}', {'name': 'Welcome', 'subject': 'Welcome', 'body': 'Hi {{employee.name}}',
                                                 'requestable': '1', 'visible_to_employee': '1', 'active': '1'}, **htmx())
    assert r.status_code == 204
    assert 'welcome' in {t['code'] for t in docs.list_templates(w.sita)}
    p = c.post('/_/documents/templates/preview', {'preview_employee_id': w.sita_id, 'subject': 'Hi {{employee.first_name}}',
                                                  'body': 'Dear {{grade}} {{bogus.key}}'}, **htmx())
    html = p.content.decode()
    assert p.status_code == 200 and 'Unknown placeholders' in html and 'bogus.key' in html
    assert 'Missing for this employee: grade' in html and 'srcdoc=' in html
    empty = c.post('/_/documents/templates/preview', {'subject': 'S', 'body': 'B'}, **htmx()).content.decode()
    assert 'Choose an employee to see the letter exactly as it will print.' in empty
    assert client_for(w.sita).post('/_/documents/templates/new', {'code': 'x_y', 'name': 'X', 'subject': 'S', 'body': 'B'},
                                   **htmx()).status_code == 403


# ================================================================ letters

def test_issue_drawer_preview_and_issue():
    w = build()
    c = client_for(w.hr)
    form = c.get('/_/documents/letters/issue', **htmx('hj-drawer-body')).content.decode()
    assert 'Issue a letter' in form and 'Choose a template' in form
    p = c.post('/_/documents/letters/preview', {'employee_id': w.sita_id, 'template_id': w.template('promotion')}, **htmx())
    html = p.content.decode()
    assert 'Some details are missing' in html and 'grade, department, monthly salary (npr)' in html
    assert 'Issue anyway with these left blank' in html and 'disabled' in html.split('Issue letter')[0].rsplit('<button', 1)[1]
    blank = c.post('/_/documents/letters/preview', {'employee_id': w.sita_id}, **htmx()).content.decode()
    assert 'Choose an employee and a letter to see a preview.' in blank
    r = c.post('/_/documents/letters/issue', {'employee_id': w.sita_id, 'template_id': w.template('promotion'),
                                              'visible_to_employee': '1'}, **htmx())
    assert r.status_code == 400
    t = toasts(r)[0]
    assert t['title'] == 'Some details are missing' and t['message'].startswith('Fill in these details first: ')
    r = c.post('/_/documents/letters/issue', {'employee_id': w.sita_id, 'template_id': w.template('promotion'),
                                              'visible_to_employee': '1', 'allow_missing': '1'}, **htmx())
    assert r.status_code == 200
    ref = one('SELECT id, ref_no FROM issued_letter WHERE employee_id = %s', [w.sita_id])
    assert toasts(r)[0]['message'] == f"Letter {ref['ref_no']} issued."
    assert 'Open letter' in r.content.decode() and 'hj:refresh-doc-letters' in hx_triggers(r)
    # The letter opens in a new tab straight away (IssueLetterDrawer's window.open).
    assert f"window.open('/_/documents/letters/{ref['id']}/html', '_blank', 'noopener')" in r.content.decode()


def test_letter_html_opens_with_its_own_policy_and_void_hides_it():
    w = build()
    letter = docs.issue_letter(w.hr, {'templateId': w.template('confirmation'), 'employeeId': w.sita_id})
    r = client_for(w.sita).get(f"/_/documents/letters/{letter['id']}/html")
    assert r.status_code == 200 and letter['refNo'] in r.content.decode()
    assert r['Cache-Control'] == 'private, no-store' and "default-src 'none'" in r['Content-Security-Policy']
    assert client_for(w.sita).get(f"/api/v1/documents/letters/{letter['id']}/html").status_code == 200
    assert client_for(w.peer).get(f"/_/documents/letters/{letter['id']}/html").status_code == 404
    c = client_for(w.hr)
    assert f"Void {letter['refNo']}" in c.get(f"/_/documents/letters/{letter['id']}/void", **htmx('hj-drawer-body')).content.decode()
    r = c.post(f"/_/documents/letters/{letter['id']}/void", {'reason': 'no'}, **htmx())
    assert r.status_code == 400
    r = c.post(f"/_/documents/letters/{letter['id']}/void", {'reason': 'wrong date'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == f"{letter['refNo']} voided."
    assert client_for(w.sita).get(f"/_/documents/letters/{letter['id']}/html").status_code == 404
    assert 'VOID' in c.get(f"/_/documents/letters/{letter['id']}/html").content.decode()
    table = c.get('/_/documents/letters?scope=hr&show_employee=1', **htmx()).content.decode()
    assert 'Void' in table and 'Sita Sharma' in table


def test_letters_register_search():
    w = build()
    welcome_template(w)
    docs.issue_letter(w.hr, {'templateId': w.template('confirmation'), 'employeeId': w.sita_id})
    docs.issue_letter(w.hr, {'templateId': w.template('welcome'), 'employeeId': w.peer_id})
    c = client_for(w.hr)
    html = c.get('/_/documents/letters?scope=hr&show_employee=1&q=Peer', **htmx()).content.decode()
    assert 'Peer Person' in html and 'Sita Sharma' not in html
    # An employee asking for HR scope only sees their own letters.
    mine = client_for(w.peer).get('/_/documents/letters?scope=hr&show_employee=1', **htmx()).content.decode()
    assert 'Welcome Peer' in mine and 'Confirmation of employment' not in mine


def test_letter_request_from_profile_to_issue():
    w = build()
    c = client_for(w.sita)
    form = c.get('/_/documents/requests/new', **htmx('hj-drawer-body')).content.decode()
    assert 'Salary certificate' in form and 'Appointment letter' not in form
    tpl = w.template('salary_certificate')
    r = c.post('/_/documents/requests/new', {'template_id': tpl, 'purpose': 'bank loan', 'addressed_to': ''}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Request sent to HR.'
    assert 'hj:refresh-my-requests' in hx_triggers(r)
    r = c.post('/_/documents/requests/new', {'template_id': tpl, 'purpose': 'again'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'you already have an open request for a salary certificate'
    mine = c.get('/_/documents/requests', **htmx()).content.decode()
    assert 'Waiting for HR' in mine and 'bank loan' in mine
    req = docs.list_requests(w.sita)[0]
    hr = client_for(w.hr)
    review = hr.get('/employees/documents').content.decode()
    assert 'Prepare letter' in review and 'Sita Sharma (E002)' in review
    form = hr.get(f"/_/documents/letters/issue?request_id={req['id']}", **htmx('hj-drawer-body')).content.decode()
    assert 'Issue salary certificate for Sita Sharma' in form and 'bank loan' in form
    r = hr.post('/_/documents/letters/issue', {'request_id': req['id'], 'purpose': 'bank loan', 'addressed_to': 'ABC Bank',
                                               'visible_to_employee': '1', 'allow_missing': '1'}, **htmx())
    assert r.status_code == 200
    assert docs.list_requests(w.sita)[0]['status'] == 'issued'
    # Issued requests leave the open list (MyDocumentsPage shows pending, approved and declined ones); the letter is listed.
    assert 'Waiting for HR' not in c.get('/_/documents/requests', **htmx()).content.decode()
    assert 'Salary certificate' in c.get('/_/documents/letters', **htmx()).content.decode()


def test_employee_cancels_a_request():
    w = build()
    r = docs.request_letter(w.sita, {'templateId': w.template('experience'), 'purpose': 'new job'})
    assert client_for(w.peer).post(f"/_/documents/requests/{r['id']}/cancel", **htmx()).status_code == 404
    resp = client_for(w.sita).post(f"/_/documents/requests/{r['id']}/cancel", **htmx())
    assert resp.status_code == 204 and toasts(resp)[0]['message'] == 'Request cancelled.'
    assert client_for(w.sita).post(f"/_/documents/requests/{r['id']}/cancel", **htmx()).status_code == 409


# ================================================================ policies

def test_hr_publishes_a_policy_and_tracks_acknowledgements():
    w = build()
    c = client_for(w.hr)
    form = c.get('/_/documents/policies/new', **htmx('hj-drawer-body')).content.decode()
    assert 'Publish a policy' in form and 'Kathmandu' in form
    assert "closest('.hj-file button')) file = ''" in form
    r = c.post('/_/documents/policies/new', {'title': 'Employee handbook', 'description': '', 'file_id': upload(w.hr, 'company_document'),
                                             'audience': 'all', 'requires_ack': '1'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Published. Everyone it applies to has been notified.'
    assert outbox('policy.published', 'in_app') == 3
    pid = docs.list_policies(w.hr)[0]['id']
    lst = c.get('/_/documents/policies', **htmx()).content.decode()
    assert 'Employee handbook' in lst and '0 of 4' in lst and 'Everyone' in lst
    s = client_for(w.sita)
    assert s.get(f'/_/documents/policies/{pid}/file').status_code == 200
    r = s.post(f'/_/documents/policies/{pid}/acknowledge', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Thanks, Employee handbook acknowledged.'
    assert 'Acknowledged' in s.get('/_/documents/policies/mine', **htmx()).content.decode()
    acks = c.get(f'/_/documents/policies/{pid}/acks', **htmx('hj-drawer-body')).content.decode()
    assert '1 of 4 have acknowledged.' in acks and 'Remind the rest' in acks
    r = c.post(f'/_/documents/policies/{pid}/remind', **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Reminder sent to 2 people.'
    assert 'reminded' in r.content.decode()
    r = c.post(f'/_/documents/policies/{pid}/archive', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Archived.'
    assert 'Employee handbook' not in c.get('/_/documents/policies', **htmx()).content.decode()
    assert 'Archived' in c.get('/_/documents/policies?archived=1', **htmx()).content.decode()
    assert client_for(w.sita).get(f'/_/documents/policies/{pid}/acks', **htmx()).status_code == 403


def test_branch_policy_needs_a_branch():
    w = build()
    c = client_for(w.hr)
    r = c.post('/_/documents/policies/new', {'title': 'Rules', 'file_id': upload(w.hr, 'company_document'), 'audience': 'branch'},
               **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'choose at least one branch'
    r = c.post('/_/documents/policies/new', {'title': 'Rules', 'file_id': upload(w.hr, 'company_document'), 'audience': 'branch',
                                             'audience_ids': [w.branch], 'requires_ack': '1'}, **htmx())
    assert r.status_code == 204
    assert 'Kathmandu' in c.get('/_/documents/policies', **htmx()).content.decode()


def test_policy_outside_audience_is_not_found_for_employees():
    w = build()
    b2 = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'PKR', 'Pokhara') RETURNING id", [w.co.id])['id'])
    pid = docs.publish_policy(w.hr, {'title': 'Pokhara rules', 'fileId': upload(w.hr, 'company_document'), 'audience': 'branch',
                                     'audienceIds': [b2]})['id']
    s = client_for(w.sita)
    assert s.get(f'/_/documents/policies/{pid}/file').status_code == 404
    assert s.post(f'/_/documents/policies/{pid}/acknowledge', **htmx()).status_code == 404
    assert s.get(f'/api/v1/documents/policies/{pid}/file').status_code == 404
    assert client_for(w.hr).get(f'/api/v1/documents/policies/{pid}/file').status_code == 200


def test_expiring_documents_show_in_review():
    w = build()
    docs.add_document(w.hr, w.sita_id, {'categoryId': w.category(w.hr, 'passport'), 'title': 'Passport', 'fileId': upload(w.hr),
                                        'expiryDate': add_days(npt_today(), 5)})
    html = client_for(w.hr).get('/_/documents/review', **htmx()).content.decode()
    assert 'Expiring documents' in html and '(5 days)' in html
