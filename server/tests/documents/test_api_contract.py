"""
HTTP-level port of apps/api/test/documents.test.ts (commit 65a9079) against /api/v1/documents, called the way the React
app does (frontend/src/api/documents.js): the session cookie with fetch(), JSON bodies, no CSRF token, the browser's
Origin header on writes. Status codes, JSON field names and error bodies are the TS ones; every check that crosses a
company boundary answers as not found.
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.core.clock import now as clock_now
from hajiri.documents import services as docs
from hajiri.documents.letters import npt_today
from tests.support.sql import execute, one
from tests.web.factories import PW

from .world import PDF, add_days, build, outbox

pytestmark = pytest.mark.django_db

ORIGIN = 'http://testserver'


class Browser:
    """The React app's fetch(): cookie session, JSON in and out, Origin on state-changing requests."""

    def __init__(self, user, origin: str | None = ORIGIN):
        self.c = Client(enforce_csrf_checks=True)
        self.c.cookies[auth.SESSION_COOKIE] = auth.login(user.email, PW).token
        self.h = {'HTTP_ORIGIN': origin} if origin else {}

    def get(self, path, query=None):
        return self.c.get(f'/api/v1{path}', query or {})

    def _send(self, method, path, body):
        data = json.dumps(body) if body is not None else ''
        return getattr(self.c, method)(f'/api/v1{path}', data, content_type='application/json', **self.h)

    def post(self, path, body=None):
        return self._send('post', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)

    def delete(self, path):
        return self._send('delete', path, None)


class Apps:
    """One signed-in browser per person of the TS test's beforeAll."""

    def __init__(self, w):
        self.w = w
        self.hr, self.sita, self.ram = Browser(w.hr), Browser(w.sita), Browser(w.ram)
        self.peer, self.foreign_hr = Browser(w.peer), Browser(w.foreign_hr)

    def upload(self, who: Browser, purpose: str = 'employee_document') -> str:
        r = who.post('/files', {'purpose': purpose, 'filename': 'doc.pdf', 'dataUrl': PDF})
        assert r.status_code == 201, r.content
        return r.json()['file']['id']

    def category(self, who: Browser, code: str) -> str:
        return next(c['id'] for c in who.get('/documents/categories').json()['categories'] if c['code'] == code)

    def template(self, code: str) -> str:
        return next(t['id'] for t in self.hr.get('/documents/templates').json()['templates'] if t['code'] == code)


@pytest.fixture
def a():
    w = build()
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [w.ram_id, w.sita_id])
    return Apps(w)


def _sita_uploads(a: Apps) -> str:
    r = a.sita.post(f'/documents/employee/{a.w.sita_id}',
                    {'categoryId': a.category(a.sita, 'citizenship'), 'title': 'Citizenship', 'fileId': a.upload(a.sita)})
    assert r.status_code == 201
    return r.json()['id']


# ================================================================ employee documents

def test_seeds_categories_for_the_company_and_hides_other_companies(a):
    r = a.sita.get('/documents/categories')
    assert r.status_code == 200
    cats = r.json()['categories']
    assert {'contract', 'citizenship', 'passport', 'other'} <= {c['code'] for c in cats}
    assert set(cats[0]) == {'id', 'code', 'name', 'employee_can_upload', 'expiry_required', 'visible_to_employee',
                            'sort_order', 'active'}
    foreign = {c['id'] for c in a.foreign_hr.get('/documents/categories').json()['categories']}
    assert not foreign & {c['id'] for c in cats}
    # Another company's category cannot be changed.
    r = a.foreign_hr.patch(f"/documents/categories/{cats[0]['id']}", {'name': 'Mine now'})
    assert r.status_code == 404 and r.json() == {'error': 'category not found'}


def test_hr_adds_and_updates_categories_employees_cannot(a):
    r = a.hr.post('/documents/categories', {'code': 'work_permit', 'name': 'Work permit', 'expiryRequired': True})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    cid = r.json()['id']
    assert a.hr.patch(f'/documents/categories/{cid}', {'active': False}).json() == {'ok': True}
    assert cid in {c['id'] for c in a.hr.get('/documents/categories').json()['categories']}
    assert cid not in {c['id'] for c in a.sita.get('/documents/categories').json()['categories']}
    r = a.sita.post('/documents/categories', {'code': 'x_y', 'name': 'X'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    r = a.hr.patch('/documents/categories/not-a-uuid', {'name': 'X'})
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'Invalid UUID'


def test_lets_an_employee_upload_their_own_document_pending_hr_verification(a):
    doc = _sita_uploads(a)
    mine = a.sita.get('/documents/mine').json()['documents']
    assert len(mine) == 1
    d = mine[0]
    assert (d['id'], d['status'], d['title'], d['version']) == (doc, 'pending', 'Citizenship', 1)
    assert {'category_name', 'filename', 'mime', 'bytes', 'uploaded_by_name', 'expiry_date', 'is_current',
            'visible_to_employee', 'reject_reason', 'notes', 'created_at'} <= set(d)
    assert d['mime'] == 'application/pdf' and isinstance(d['bytes'], int) and d['expiry_date'] is None
    assert re.match(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$', d['created_at'])
    assert outbox('document.uploaded') > 0


def test_refuses_categories_employees_may_not_upload_missing_expiry_dates_and_other_employees(a):
    sita = a.w.sita_id
    r = a.sita.post(f'/documents/employee/{sita}',
                    {'categoryId': a.category(a.sita, 'contract'), 'title': 'Contract', 'fileId': a.upload(a.sita)})
    assert r.status_code == 403
    r = a.sita.post(f'/documents/employee/{sita}',
                    {'categoryId': a.category(a.sita, 'passport'), 'title': 'Passport', 'fileId': a.upload(a.sita)})
    assert r.status_code == 400 and r.json()['error'].endswith('needs an expiry date')
    r = a.sita.post(f'/documents/employee/{a.w.peer_id}',
                    {'categoryId': a.category(a.sita, 'other'), 'title': 'X', 'fileId': a.upload(a.sita)})
    assert r.status_code == 403 and r.json() == {'error': 'you can only upload your own documents'}
    # A file uploaded by someone else cannot be attached.
    r = a.sita.post(f'/documents/employee/{sita}',
                    {'categoryId': a.category(a.sita, 'other'), 'title': 'X', 'fileId': a.upload(a.peer)})
    assert r.status_code == 403
    # The TS validation shape.
    r = a.sita.post(f'/documents/employee/{sita}', {'categoryId': 'nope', 'title': '', 'fileId': a.upload(a.sita)})
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert {'categoryId', 'title'} <= {i['path'] for i in r.json()['issues']}


def test_serves_files_to_the_employee_and_hr_audited_never_to_managers_peers_or_other_companies(a):
    doc = _sita_uploads(a)
    assert a.sita.get(f'/documents/{doc}/file').status_code == 200
    h = a.hr.get(f'/documents/{doc}/file')
    assert h.status_code == 200 and 'application/pdf' in h['Content-Type']
    assert a.hr.get(f'/documents/{doc}/file', {'download': '1'})['Content-Disposition'].startswith('attachment')
    for who in (a.ram, a.peer, a.foreign_hr):
        r = who.get(f'/documents/{doc}/file')
        assert r.status_code == 404 and r.json() == {'error': 'document not found'}
    r = a.ram.get(f'/documents/employee/{a.w.sita_id}')
    assert r.status_code == 403 and r.json() == {'error': 'you can only see your own documents'}
    r = a.foreign_hr.get(f'/documents/employee/{a.w.sita_id}')
    assert r.status_code == 404 and r.json() == {'error': 'employee not found'}
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_document' AND row_id = %s", [doc])['n'] == 2
    assert a.hr.get('/documents/not-a-uuid/file').status_code == 400


def test_hr_verifies_or_rejects_with_a_reason_the_employee_cannot_verify(a):
    doc = _sita_uploads(a)
    r = a.sita.post(f'/documents/{doc}/verify', {'decision': 'verify'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    r = a.hr.post(f'/documents/{doc}/verify', {'decision': 'reject'})
    assert r.status_code == 400 and r.json() == {'error': 'give a reason so the employee knows what to fix'}
    q = a.hr.get('/documents/queue').json()
    pending = next(d for d in q['pending'] if d['id'] == doc)
    assert (pending['emp_code'], pending['name_en']) == ('E002', 'Sita Sharma')
    assert a.hr.post(f'/documents/{doc}/verify', {'decision': 'verify'}).json() == {'ok': True}
    r = a.hr.post(f'/documents/{doc}/verify', {'decision': 'verify'})
    assert r.status_code == 409 and r.json() == {'error': 'this document is not waiting for verification'}
    # Verified documents can no longer be deleted by the employee.
    r = a.sita.delete(f'/documents/{doc}')
    assert r.status_code == 403 and r.json() == {'error': 'only HR can delete a verified document'}
    # Another company's HR does not see it at all.
    assert a.foreign_hr.post(f'/documents/{doc}/verify', {'decision': 'verify'}).status_code == 404
    assert a.foreign_hr.patch(f'/documents/{doc}', {'title': 'X'}).status_code == 404
    assert a.foreign_hr.delete(f'/documents/{doc}').status_code == 404


def test_keeps_versions_when_a_document_is_replaced_and_restores_the_previous_one_when_deleted(a):
    doc = _sita_uploads(a)
    docs.verify_document(a.w.hr, doc, {'decision': 'verify'})
    cat = a.category(a.sita, 'citizenship')
    r = a.sita.post(f'/documents/employee/{a.w.sita_id}',
                    {'categoryId': cat, 'title': 'Citizenship', 'fileId': a.upload(a.sita), 'replacesId': doc})
    assert r.status_code == 201
    new = r.json()['id']
    mine = a.sita.get('/documents/mine').json()['documents']
    assert len(mine) == 1 and mine[0]['version'] == 2
    hist = a.hr.get(f'/documents/employee/{a.w.sita_id}', {'history': '1'}).json()['documents']
    assert [d['version'] for d in hist] == [2, 1]
    # The React app sends history=1 only when asked; without it HR sees the current version.
    assert [d['version'] for d in a.hr.get(f'/documents/employee/{a.w.sita_id}').json()['documents']] == [2]
    # Replacing an old version is refused.
    r = a.sita.post(f'/documents/employee/{a.w.sita_id}',
                    {'categoryId': cat, 'title': 'C', 'fileId': a.upload(a.sita), 'replacesId': doc})
    assert r.status_code == 409 and r.json() == {'error': 'only the latest version can be replaced'}
    assert a.sita.delete(f'/documents/{new}').json() == {'ok': True}
    after = a.sita.get('/documents/mine').json()['documents']
    assert (after[0]['id'], after[0]['version']) == (doc, 1)
    wiped = one('SELECT f.data_enc FROM employee_document d JOIN stored_file f ON f.id = d.file_id WHERE d.id = %s', [new])
    assert wiped['data_enc'] is None


def test_hr_only_documents_stay_hidden_from_the_employee(a):
    r = a.hr.post(f'/documents/employee/{a.w.sita_id}',
                  {'categoryId': a.category(a.hr, 'contract'), 'title': 'Signed contract', 'fileId': a.upload(a.hr),
                   'visibleToEmployee': False})
    assert r.status_code == 201
    hidden = r.json()['id']
    assert hidden not in [d['id'] for d in a.sita.get('/documents/mine').json()['documents']]
    assert a.sita.get(f'/documents/{hidden}/file').status_code == 404
    hr_view = a.hr.get(f'/documents/employee/{a.w.sita_id}').json()['documents']
    row = next(d for d in hr_view if d['id'] == hidden)
    assert (row['status'], row['visible_to_employee']) == ('verified', False)


def test_hr_edits_a_document(a):
    doc = _sita_uploads(a)
    r = a.hr.patch(f'/documents/{doc}', {'title': 'Citizenship (both sides)', 'expiryDate': '2030-01-01', 'notes': 'ok'})
    assert r.json() == {'ok': True}
    d = a.hr.get(f'/documents/employee/{a.w.sita_id}').json()['documents'][0]
    assert (d['title'], d['expiry_date'], d['notes']) == ('Citizenship (both sides)', '2030-01-01', 'ok')
    assert a.sita.patch(f'/documents/{doc}', {'title': 'X'}).json() == {'error': 'requires role hr'}
    r = a.hr.patch(f'/documents/{doc}', {'expiryDate': '2030-13-01'})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'expiryDate'


def test_sends_expiry_alerts_once_per_threshold_and_lists_them_in_the_queue(a):
    today = npt_today()
    r = a.hr.post(f'/documents/employee/{a.w.sita_id}',
                  {'categoryId': a.category(a.hr, 'passport'), 'title': 'Passport', 'fileId': a.upload(a.hr),
                   'expiryDate': add_days(today, 20)})
    assert r.status_code == 201
    pid = r.json()['id']
    assert docs.documents_daily_job()['expiryAlerts'] == 1
    assert docs.documents_daily_job()['expiryAlerts'] == 0
    assert docs.documents_daily_job(clock_now() + timedelta(days=14))['expiryAlerts'] == 1
    assert outbox('document.expiring', 'in_app') == 2
    exp = a.hr.get('/documents/queue', {'days': '30'}).json()['expiring']
    row = next(d for d in exp if d['id'] == pid)
    assert row['days_left'] == 20 and row['expiry_date'] == add_days(today, 20)
    assert pid not in [d['id'] for d in a.hr.get('/documents/queue', {'days': '10'}).json()['expiring']]
    r = a.ram.get('/documents/queue')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    r = a.hr.get('/documents/queue', {'days': '999'})
    assert r.status_code == 400 and r.json()['issues'][0]['path'] == 'days'
    assert a.foreign_hr.get('/documents/queue').json() == {'pending': [], 'expiring': []}


# ================================================================ letters

def test_rejects_templates_with_unknown_placeholders_and_saves_valid_ones(a):
    bad = a.hr.post('/documents/templates', {'code': 'bad_one', 'name': 'Bad', 'subject': 'S',
                                             'body': 'Hello {{employee.nmae}}'})
    assert bad.status_code == 400
    assert bad.json() == {'error': 'Unknown placeholder: employee.nmae', 'details': {'unknown': ['employee.nmae']}}
    ok = a.hr.post('/documents/templates', {'code': 'welcome', 'name': 'Welcome note',
                                            'subject': 'Welcome {{employee.first_name}}', 'body': 'Welcome to {{company.name}}.'})
    assert ok.status_code == 201
    r = a.sita.post('/documents/templates', {'code': 'x_y', 'name': 'X', 'subject': 'S', 'body': 'B'})
    assert r.status_code == 403
    hr_tpl = next(t for t in a.hr.get('/documents/templates').json()['templates'] if t['id'] == ok.json()['id'])
    assert set(hr_tpl) == {'id', 'code', 'name', 'subject', 'body', 'requestable', 'visible_to_employee', 'is_builtin',
                           'active', 'updated_at'}
    assert a.hr.patch(f"/documents/templates/{ok.json()['id']}", {'requestable': True}).json() == {'ok': True}
    r = a.hr.patch(f"/documents/templates/{ok.json()['id']}", {'body': '{{nope}}'})
    assert r.status_code == 400 and r.json()['details'] == {'unknown': ['nope']}
    r = a.foreign_hr.patch(f"/documents/templates/{ok.json()['id']}", {'name': 'X'})
    assert r.status_code == 404 and r.json() == {'error': 'template not found'}


def test_previews_with_missing_values_marked_and_a_draft_watermark(a):
    r = a.hr.post('/documents/templates/preview', {'employeeId': a.w.sita_id, 'templateId': a.template('appointment')})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {'html', 'missing', 'unknown'}
    assert 'Sita' in body['html'] and 'DRAFT' in body['html']
    assert {'department', 'salary.gross'} <= set(body['missing']) and 'ref_no' not in body['missing']
    r = a.foreign_hr.post('/documents/templates/preview', {'employeeId': a.w.sita_id, 'subject': 'S', 'body': 'B'})
    assert r.status_code == 404 and r.json() == {'error': 'employee not found'}
    r = a.hr.post('/documents/templates/preview', {'employeeId': a.w.sita_id})
    assert r.status_code == 400 and r.json() == {'error': 'subject and body are required'}
    assert a.sita.post('/documents/templates/preview', {'employeeId': a.w.sita_id, 'subject': 'S', 'body': 'B'}).status_code == 403


def test_placeholders_and_letter_settings(a):
    assert {'key': 'employee.name', 'label': 'Employee full name'} in a.hr.get('/documents/placeholders').json()['placeholders']
    assert a.sita.get('/documents/placeholders').status_code == 403
    assert set(a.hr.get('/documents/letter-settings').json()['settings']) == {'signatoryName', 'signatoryTitle', 'refPrefix'}
    body = {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager', 'refPrefix': 'CV/HR'}
    assert a.hr.put('/documents/letter-settings', body).json() == {'settings': body}
    assert a.hr.get('/documents/letter-settings').json() == {'settings': body}
    # Another company keeps its own settings.
    assert a.foreign_hr.get('/documents/letter-settings').json()['settings']['refPrefix'] != 'CV/HR'
    r = a.hr.put('/documents/letter-settings', {**body, 'refPrefix': 'a b'})
    assert r.status_code == 400 and r.json()['issues'][0] == {'path': 'refPrefix', 'message': 'letters, digits, / _ - only'}
    assert a.sita.put('/documents/letter-settings', body).status_code == 403


def test_refuses_to_issue_with_missing_values_unless_allowed_then_numbers_letters_per_bs_year(a):
    tpl = a.template('confirmation')
    missing = a.hr.post('/documents/letters', {'templateId': a.template('promotion'), 'employeeId': a.w.sita_id})
    assert missing.status_code == 400
    assert missing.json()['details']['code'] == 'missing_values' and missing.json()['details']['missing']
    assert missing.json()['error'].startswith('Fill in these details first: ')
    a.hr.put('/documents/letter-settings', {'signatoryName': 'Gita HR', 'signatoryTitle': 'HR Manager', 'refPrefix': 'CV/HR'})
    one_ = a.hr.post('/documents/letters', {'templateId': tpl, 'employeeId': a.w.sita_id})
    assert one_.status_code == 201 and set(one_.json()) == {'id', 'refNo'}
    assert re.match(r'^CV/HR/20\d\d/0001$', one_.json()['refNo'])
    welcome = docs.create_template(a.w.hr, {'code': 'welcome', 'name': 'Welcome note',
                                            'subject': 'Welcome {{employee.first_name}}', 'body': 'Welcome to {{company.name}}.'})
    two = a.hr.post('/documents/letters', {'templateId': welcome['id'], 'employeeId': a.w.sita_id})
    assert two.json()['refNo'].endswith('/0002')
    lid = one_.json()['id']
    html = a.sita.get(f'/documents/letters/{lid}/html')
    assert html.status_code == 200 and html['Content-Type'].startswith('text/html')
    assert html['Cache-Control'] == 'private, no-store'
    text = html.content.decode()
    assert one_.json()['refNo'] in text and 'Gita HR' in text and 'DRAFT' not in text
    for who in (a.peer, a.ram, a.foreign_hr):
        r = who.get(f'/documents/letters/{lid}/html')
        assert r.status_code == 404 and r.json() == {'error': 'letter not found'}
    # Voiding hides it from the employee and marks it for HR.
    assert a.foreign_hr.post(f'/documents/letters/{lid}/void', {'reason': 'wrong date'}).status_code == 404
    assert a.hr.post(f'/documents/letters/{lid}/void', {'reason': 'wrong date'}).json() == {'ok': True}
    r = a.hr.post(f'/documents/letters/{lid}/void', {'reason': 'wrong date'})
    assert r.status_code == 404 and r.json() == {'error': 'letter not found or already voided'}
    assert a.sita.get(f'/documents/letters/{lid}/html').status_code == 404
    assert 'VOID' in a.hr.get(f'/documents/letters/{lid}/html').content.decode()
    mine = a.sita.get('/documents/letters').json()['letters']
    assert [x['id'] for x in mine] == [two.json()['id']]
    r = a.sita.get('/documents/letters', {'employeeId': a.w.peer_id})
    assert r.status_code == 403 and r.json() == {'error': 'you can only see your own letters'}
    hr_list = a.hr.get('/documents/letters', {'employeeId': a.w.sita_id}).json()['letters']
    assert {x['id'] for x in hr_list} == {lid, two.json()['id']}
    assert {'id', 'employee_id', 'emp_code', 'name_en', 'template_code', 'ref_no', 'subject', 'visible_to_employee',
            'request_id', 'issued_at', 'voided_at', 'void_reason', 'issued_by_name'} == set(hr_list[0])
    assert a.hr.get('/documents/letters', {'q': 'CV/HR/'}).json()['letters']
    assert a.foreign_hr.get('/documents/letters').json() == {'letters': []}
    r = a.foreign_hr.post('/documents/letters', {'templateId': tpl, 'employeeId': a.w.sita_id})
    assert r.status_code == 404 and r.json() == {'error': 'template not found'}


def test_runs_a_letter_request_through_hr_issue_completes_it_and_notifies(a):
    tpls = a.sita.get('/documents/templates').json()['templates']
    assert {'salary_certificate', 'experience'} <= {t['code'] for t in tpls}
    assert set(tpls[0]) == {'id', 'code', 'name', 'subject'}
    to_whom = next(t['id'] for t in tpls if t['code'] == 'to_whom_it_may_concern')
    r = a.sita.post('/documents/letter-requests', {'templateId': a.template('appointment'), 'purpose': 'bank loan'})
    assert r.status_code == 400 and r.json() == {'error': 'this letter cannot be requested'}
    r = a.sita.post('/documents/letter-requests', {'templateId': to_whom, 'purpose': 'visa application'})
    assert r.status_code == 201
    rid = r.json()['id']
    dup = a.sita.post('/documents/letter-requests', {'templateId': to_whom, 'purpose': 'again'})
    assert dup.status_code == 409 and dup.json()['error'].startswith('you already have an open request for a ')
    inbox = a.hr.get('/workflow/inbox').json()['items']
    item = next(i for i in inbox if i['objectId'] == rid)
    assert {k: item['summary'][k] for k in ('letter', 'purpose')} == {'letter': 'To whom it may concern',
                                                                      'purpose': 'visa application'}
    assert not [i for i in a.ram.get('/workflow/inbox').json()['items'] if i['objectId'] == rid]
    open_ = a.hr.get('/documents/letter-requests', {'status': 'open'}).json()['requests']
    assert [x['id'] for x in open_] == [rid]
    assert a.foreign_hr.get('/documents/letter-requests').json() == {'requests': []}
    r = a.foreign_hr.post('/documents/letters', {'templateId': to_whom, 'employeeId': a.w.sita_id, 'requestId': rid})
    assert r.status_code == 404
    issued = a.hr.post('/documents/letters', {'templateId': to_whom, 'employeeId': a.w.sita_id, 'requestId': rid,
                                              'allowMissing': True})
    assert issued.status_code == 201
    mine = next(x for x in a.sita.get('/documents/letter-requests').json()['requests'] if x['id'] == rid)
    assert (mine['status'], mine['issued_letter_id']) == ('issued', issued.json()['id'])
    assert {'id', 'employee_id', 'emp_code', 'name_en', 'template_id', 'template_name', 'template_code', 'purpose',
            'addressed_to', 'status', 'decision_reason', 'issued_letter_id', 'created_at', 'decided_at'} == set(mine)
    assert 'visa application' in a.sita.get(f"/documents/letters/{issued.json()['id']}/html").content.decode()
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [rid])['state'] == 'approved'
    assert outbox('letter.issued', employee_id=a.w.sita_id) > 0


def test_a_rejected_request_notifies_the_employee_cancel_works_only_for_open_requests(a):
    tpl = a.template('salary_certificate')
    r = a.sita.post('/documents/letter-requests', {'templateId': tpl, 'purpose': 'bank loan',
                                                   'addressedTo': 'The Manager, ABC Bank'})
    rid = r.json()['id']
    wf = one('SELECT id FROM workflow_instance WHERE object_id = %s', [rid])['id']
    # The workflow module's own endpoint (a Bearer call keeps this test independent of how that slice authenticates).
    rej = Client().post(f'/api/v1/workflow/{wf}/reject', json.dumps({'reason': 'no salary set yet'}),
                        content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {auth.login(a.w.hr.email, PW).token}')
    assert rej.status_code in (200, 201)
    row = next(x for x in a.sita.get('/documents/letter-requests').json()['requests'] if x['id'] == rid)
    assert (row['status'], row['decision_reason']) == ('rejected', 'no salary set yet')
    assert outbox('letter_request.rejected') > 0
    r = a.sita.post(f'/documents/letter-requests/{rid}/cancel')
    assert r.status_code == 409 and r.json() == {'error': 'request already rejected'}
    r2 = a.sita.post('/documents/letter-requests', {'templateId': tpl, 'purpose': 'bank loan'}).json()['id']
    r = a.peer.post(f'/documents/letter-requests/{r2}/cancel')
    assert r.status_code == 404 and r.json() == {'error': 'request not found'}
    assert a.foreign_hr.post(f'/documents/letter-requests/{r2}/cancel').status_code == 404
    assert a.sita.post(f'/documents/letter-requests/{r2}/cancel').json() == {'ok': True}
    assert a.hr.get('/documents/letter-requests', {'status': 'bogus'}).status_code == 400


# ================================================================ company policies

def _handbook(a: Apps) -> str:
    r = a.hr.post('/documents/policies', {'title': 'Employee handbook', 'description': 'Read it',
                                          'fileId': a.upload(a.hr, 'company_document')})
    assert r.status_code == 201
    return r.json()['id']


def test_publishes_to_an_audience_notifies_and_needs_a_company_document_upload(a):
    bad = a.hr.post('/documents/policies', {'title': 'Handbook', 'fileId': a.upload(a.hr, 'expense_receipt')})
    assert bad.status_code == 400
    _handbook(a)
    r = a.sita.post('/documents/policies', {'title': 'X', 'fileId': a.upload(a.sita, 'company_document')})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    assert outbox('policy.published', 'in_app') >= 3


def test_branch_audiences_only_reach_that_branch(a):
    handbook = _handbook(a)
    b2 = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'PKR', 'Pokhara') RETURNING id", [a.w.co.id])['id'])
    r = a.hr.post('/documents/policies', {'title': 'Pokhara rules', 'fileId': a.upload(a.hr, 'company_document'),
                                          'audience': 'branch', 'audienceIds': [b2]})
    assert r.status_code == 201
    pkr = r.json()['id']
    assert [p['id'] for p in a.sita.get('/documents/policies').json()['policies']] == [handbook]
    r = a.sita.get(f'/documents/policies/{pkr}/file')
    assert r.status_code == 404 and r.json() == {'error': 'policy not found'}
    assert a.sita.post(f'/documents/policies/{pkr}/acknowledge').status_code == 404
    r = a.hr.post('/documents/policies', {'title': 'x', 'fileId': a.upload(a.hr, 'company_document'), 'audience': 'branch',
                                          'audienceIds': ['00000000-0000-7000-8000-00000000ffff']})
    assert r.status_code == 400 and r.json() == {'error': 'unknown branch'}
    r = a.hr.post('/documents/policies', {'title': 'x', 'fileId': a.upload(a.hr, 'company_document'), 'audience': 'branch'})
    assert r.status_code == 400 and r.json() == {'error': 'choose at least one branch'}
    # Another company's branch is unknown here.
    other_branch = str(one("INSERT INTO branch (company_id, code, name_en) SELECT company_id, 'OTH', 'Other' FROM app_user "
                           "WHERE email = 'hr@other.docs' RETURNING id")['id'])
    r = a.hr.post('/documents/policies', {'title': 'x', 'fileId': a.upload(a.hr, 'company_document'), 'audience': 'branch',
                                          'audienceIds': [other_branch]})
    assert r.status_code == 400 and r.json() == {'error': 'unknown branch'}


def test_employees_read_and_acknowledge_hr_sees_who_has_and_reminds_the_rest(a):
    pid = _handbook(a)
    mine = a.sita.get('/documents/policies').json()['policies']
    assert set(mine[0]) == {'id', 'title', 'description', 'requires_ack', 'published_at', 'filename', 'my_ack_at', 'in_audience'}
    assert (mine[0]['id'], mine[0]['my_ack_at'], mine[0]['in_audience']) == (pid, None, True)
    f = a.sita.get(f'/documents/policies/{pid}/file')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    assert a.sita.post(f'/documents/policies/{pid}/acknowledge').json() == {'ok': True}
    assert a.sita.post(f'/documents/policies/{pid}/acknowledge').json() == {'ok': True}
    assert a.sita.get('/documents/policies').json()['policies'][0]['my_ack_at'] is not None
    acks = a.hr.get(f'/documents/policies/{pid}/acks').json()['employees']
    sita = next(e for e in acks if e['employee_id'] == a.w.sita_id)
    assert sita['acknowledged_at'] is not None
    assert set(sita) == {'employee_id', 'emp_code', 'name_en', 'acknowledged_at', 'last_reminded_at'}
    r = a.sita.get(f'/documents/policies/{pid}/acks')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    p = next(x for x in a.hr.get('/documents/policies').json()['policies'] if x['id'] == pid)
    assert p['acks'] == 1 and p['audience_size'] >= 3
    assert {'audience', 'audience_ids', 'archived_at', 'filename', 'my_ack_at', 'in_audience'} <= set(p)
    assert a.hr.post(f'/documents/policies/{pid}/remind').json() == {'sent': 2}
    for path in (f'/documents/policies/{pid}/file', f'/documents/policies/{pid}/acks'):
        assert a.foreign_hr.get(path).status_code == 404
    assert a.foreign_hr.post(f'/documents/policies/{pid}/remind').status_code == 404
    assert a.foreign_hr.patch(f'/documents/policies/{pid}', {'archived': True}).status_code == 404
    assert a.foreign_hr.get('/documents/policies').json() == {'policies': []}


def test_nudges_weekly_and_archiving_stops_them_and_hides_the_policy(a):
    pid = _handbook(a)
    a.sita.post(f'/documents/policies/{pid}/acknowledge')
    a.hr.post(f'/documents/policies/{pid}/remind')
    later = clock_now() + timedelta(days=9)
    assert docs.documents_daily_job(later)['policyNudges'] == 2
    assert docs.documents_daily_job(later)['policyNudges'] == 0
    a.ram.post(f'/documents/policies/{pid}/acknowledge')
    assert docs.documents_daily_job(later + timedelta(days=8))['policyNudges'] == 1
    assert a.hr.patch(f'/documents/policies/{pid}', {'archived': True}).json() == {'ok': True}
    assert docs.documents_daily_job(later + timedelta(days=30))['policyNudges'] == 0
    assert a.peer.get('/documents/policies').json() == {'policies': []}
    r = a.hr.post(f'/documents/policies/{pid}/remind')
    assert r.status_code == 409 and r.json() == {'error': 'this policy is archived'}
    assert a.hr.get('/documents/policies').json() == {'policies': []}
    archived = a.hr.get('/documents/policies', {'archived': '1'}).json()['policies']
    assert archived[0]['id'] == pid and archived[0]['archived_at'] and archived[0]['in_audience'] is False


# ================================================================ the React app's session

def test_cookie_session_without_csrf_token_and_the_origin_check(a):
    # The React app never sends a CSRF token: its same-origin writes pass.
    r = a.hr.post('/documents/categories', {'code': 'x_y', 'name': 'X'})
    assert r.status_code == 201
    # No Origin and no Referer (e.g. a non-browser client) passes too, as the TS sameOriginGuard let it.
    assert Browser(a.w.hr, origin=None).post('/documents/categories', {'code': 'x_z', 'name': 'Y'}).status_code == 201
    # A cross-site write carried by the cookie is refused.
    r = Browser(a.w.hr, origin='https://evil.example').post('/documents/categories', {'code': 'x_w', 'name': 'Z'})
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    # Reads are never origin-checked (letter and file links open in a new tab).
    assert Browser(a.w.sita, origin='https://evil.example').get('/documents/mine').status_code == 200
    assert Client().get('/api/v1/documents/mine').status_code == 401


def test_feature_off_answers_before_the_role_check(a):
    from tests.web.factories import set_features_off
    set_features_off(a.w.co, 'documents')
    for who, path in ((a.sita, '/documents/mine'), (a.sita, '/documents/queue'), (a.hr, '/documents/queue')):
        r = who.get(path)
        assert r.status_code == 404 and r.json() == {'error': 'feature off', 'feature': 'documents'}
    # The other company still has the module.
    assert a.foreign_hr.get('/documents/queue').status_code == 200
