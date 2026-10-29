"""
Field-by-field check of every /api/v1/documents response the React app reads (frontend/src/pages/documents/*.jsx,
relations/DisciplinaryPage.jsx) against the columns the TS routes selected (commit 65a9079,
apps/api/src/modules/documents/routes.ts): exact key sets, JSON types (ids as strings, dates as YYYY-MM-DD,
timestamps as JSON.stringify() ISO strings, sizes and counts as numbers, arrays of ids), file and letter headers, and
the bodies the React app sends (nulls for cleared fields, no reason on verify).
"""
from __future__ import annotations

import re

import pytest

from hajiri.documents.letters import npt_today

from .test_api_contract import Apps, a  # noqa: F401 (fixture)
from .world import add_days

pytestmark = pytest.mark.django_db

TS = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d\d-\d\d$')
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

# DOC_COLS of the TS route.
DOC_KEYS = {'id', 'company_id', 'employee_id', 'category_id', 'category_name', 'title', 'file_id', 'version',
            'replaces_id', 'is_current', 'status', 'reject_reason', 'expiry_date', 'visible_to_employee', 'notes',
            'uploaded_by', 'uploaded_by_name', 'verified_at', 'created_at', 'filename', 'mime', 'bytes'}


def _ts_or_none(v):
    assert v is None or TS.match(v), v


def _check_doc(d):
    for k in ('id', 'company_id', 'employee_id', 'category_id', 'file_id'):
        assert UUID.match(d[k]), (k, d[k])
    assert d['replaces_id'] is None or UUID.match(d['replaces_id'])
    assert d['uploaded_by'] is None or UUID.match(d['uploaded_by'])
    assert isinstance(d['version'], int) and isinstance(d['bytes'], int)
    assert isinstance(d['is_current'], bool) and isinstance(d['visible_to_employee'], bool)
    assert d['expiry_date'] is None or DAY.match(d['expiry_date'])
    assert TS.match(d['created_at'])
    _ts_or_none(d['verified_at'])


def _add(a: Apps, who, body: dict):
    r = who.post(f'/documents/employee/{a.w.sita_id}', body)
    assert r.status_code == 201, r.content
    return r.json()['id']


def test_document_rows_have_the_ts_columns_and_types(a):
    today = npt_today()
    # The React upload form's body: nulls for the blanks, replacesId null, no visibleToEmployee for employees.
    own = _add(a, a.sita, {'categoryId': a.category(a.sita, 'citizenship'), 'title': 'Citizenship',
                           'fileId': a.upload(a.sita), 'expiryDate': None, 'notes': None, 'replacesId': None})
    hr_doc = _add(a, a.hr, {'categoryId': a.category(a.hr, 'passport'), 'title': 'Passport', 'fileId': a.upload(a.hr),
                            'expiryDate': add_days(today, 10), 'visibleToEmployee': True, 'notes': 'scan',
                            'replacesId': None})
    mine = a.sita.get('/documents/mine').json()['documents']
    assert {d['id'] for d in mine} == {own, hr_doc}
    for d in mine:
        assert set(d) == DOC_KEYS
        _check_doc(d)
    by_id = {d['id']: d for d in mine}
    o, h = by_id[own], by_id[hr_doc]
    assert (o['status'], o['verified_at'], o['uploaded_by_name'], o['notes']) == ('pending', None, o['uploaded_by_name'], None)
    assert o['uploaded_by_name'] and o['uploaded_by'] == str(a.w.sita.id)
    assert (h['status'], h['expiry_date'], h['notes'], h['version'], h['replaces_id']) == (
        'verified', add_days(today, 10), 'scan', 1, None)
    assert TS.match(h['verified_at'])

    # HR's view with history: a replaced version stays, with replaces_id on the new one.
    new = _add(a, a.hr, {'categoryId': a.category(a.hr, 'passport'), 'title': 'Passport', 'fileId': a.upload(a.hr),
                         'expiryDate': add_days(today, 400), 'replacesId': hr_doc})
    rows = a.hr.get(f'/documents/employee/{a.w.sita_id}', {'history': 1}).json()['documents']
    assert {d['id'] for d in rows} == {own, hr_doc, new}
    for d in rows:
        assert set(d) == DOC_KEYS
    n = next(d for d in rows if d['id'] == new)
    assert (n['version'], n['replaces_id'], n['is_current']) == (2, hr_doc, True)
    assert next(d for d in rows if d['id'] == hr_doc)['is_current'] is False
    assert {d['id'] for d in a.hr.get(f'/documents/employee/{a.w.sita_id}').json()['documents']} == {own, new}

    # The edit form's body: expiryDate null and notes '' clear them.
    r = a.hr.patch(f'/documents/{new}', {'title': 'Passport', 'expiryDate': None, 'visibleToEmployee': False, 'notes': ''})
    assert r.status_code == 200 and r.json() == {'ok': True}
    d = next(x for x in a.hr.get(f'/documents/employee/{a.w.sita_id}').json()['documents'] if x['id'] == new)
    assert (d['expiry_date'], d['visible_to_employee'], d['notes']) == (None, False, '')
    # HR-only now: gone from the employee's list.
    assert new not in {x['id'] for x in a.sita.get('/documents/mine').json()['documents']}

    # verify(id, 'verify') sends no reason.
    r = a.hr.post(f'/documents/{own}/verify', {'decision': 'verify'})
    assert r.status_code == 200 and r.json() == {'ok': True}


def test_queue_rows_add_employee_and_days_left(a):
    today = npt_today()
    pend = _add(a, a.sita, {'categoryId': a.category(a.sita, 'citizenship'), 'title': 'Citizenship',
                            'fileId': a.upload(a.sita)})
    exp = _add(a, a.hr, {'categoryId': a.category(a.hr, 'passport'), 'title': 'Passport', 'fileId': a.upload(a.hr),
                         'expiryDate': add_days(today, 5)})
    q = a.hr.get('/documents/queue', {'days': 30}).json()
    assert set(q) == {'pending', 'expiring'}
    p = next(x for x in q['pending'] if x['id'] == pend)
    assert set(p) == DOC_KEYS | {'emp_code', 'name_en'}
    _check_doc(p)
    e = next(x for x in q['expiring'] if x['id'] == exp)
    assert set(e) == DOC_KEYS | {'emp_code', 'name_en', 'days_left'}
    _check_doc(e)
    assert e['days_left'] == 5 and isinstance(e['days_left'], int)
    assert (e['emp_code'], e['name_en']) == (p['emp_code'], p['name_en'])
    # The page's default call has no days: 30.
    assert exp in {x['id'] for x in a.hr.get('/documents/queue').json()['expiring']}


def test_document_file_headers(a):
    doc = _add(a, a.sita, {'categoryId': a.category(a.sita, 'citizenship'), 'title': 'Citizenship',
                           'fileId': a.upload(a.sita)})
    inline = a.sita.get(f'/documents/{doc}/file')
    assert inline.status_code == 200 and inline['Content-Type'].startswith('application/pdf')
    assert inline.content.startswith(b'%PDF')
    assert not inline.get('Content-Disposition', '').startswith('attachment')
    dl = a.hr.get(f'/documents/{doc}/file', {'download': '1'})
    assert dl.status_code == 200 and dl['Content-Disposition'].startswith('attachment')
    assert a.sita.get('/documents/not-a-uuid/file').status_code == 400


def test_category_and_template_shapes(a):
    cats = a.hr.get('/documents/categories').json()['categories']
    for c in cats:
        assert set(c) == {'id', 'code', 'name', 'employee_can_upload', 'expiry_required', 'visible_to_employee',
                          'sort_order', 'active'}
        assert isinstance(c['sort_order'], int) and isinstance(c['active'], bool) and UUID.match(c['id'])
    # The React "add document type" body.
    r = a.hr.post('/documents/categories', {'name': 'Licence', 'code': 'licence', 'employeeCanUpload': True,
                                            'expiryRequired': False, 'visibleToEmployee': True})
    assert r.status_code == 201
    c = next(x for x in a.hr.get('/documents/categories').json()['categories'] if x['id'] == r.json()['id'])
    assert (c['sort_order'], c['active']) == (50, True)

    tpls = a.hr.get('/documents/templates').json()['templates']
    assert tpls
    for t in tpls:
        assert set(t) == {'id', 'code', 'name', 'subject', 'body', 'requestable', 'visible_to_employee', 'is_builtin',
                          'active', 'updated_at'}
        assert TS.match(t['updated_at']) and isinstance(t['is_builtin'], bool)
    # The template editor's save bodies (create, then update with active).
    body = {'name': 'Welcome', 'subject': 'Welcome {{employee.first_name}}', 'body': 'Hello\n\n{{company.name}}',
            'requestable': False, 'visibleToEmployee': True}
    r = a.hr.post('/documents/templates', {'code': 'welcome', **body})
    assert r.status_code == 201
    tid = r.json()['id']
    assert a.hr.patch(f'/documents/templates/{tid}', {**body, 'active': False}).json() == {'ok': True}
    t = next(x for x in a.hr.get('/documents/templates').json()['templates'] if x['id'] == tid)
    assert (t['active'], t['is_builtin'], t['code']) == (False, False, 'welcome')
    # Inactive templates sort last for HR.
    assert a.hr.get('/documents/templates').json()['templates'][-1]['active'] is False


def test_placeholders_settings_and_preview_shapes(a):
    ph = a.hr.get('/documents/placeholders').json()['placeholders']
    assert all(set(p) == {'key', 'label'} for p in ph)
    assert len(ph) == 28 and ph[0] == {'key': 'employee.name', 'label': 'Employee full name'}
    assert a.sita.get('/documents/placeholders').json() == {'error': 'requires role hr'}
    s = a.hr.get('/documents/letter-settings').json()['settings']
    assert set(s) == {'signatoryName', 'signatoryTitle', 'refPrefix'} and isinstance(s['refPrefix'], str)
    # The settings form sends null for blank names.
    body = {'signatoryName': None, 'signatoryTitle': None, 'refPrefix': 'HR'}
    assert a.hr.put('/documents/letter-settings', body).json() == {'settings': body}
    # Template editor preview (subject/body, no templateId) and the issue drawer's (templateId, nulls).
    r = a.hr.post('/documents/templates/preview', {'employeeId': a.w.sita_id, 'subject': 'Hi {{employee.name}} ',
                                                   'body': 'Body {{nope}}', 'purpose': 'the stated purpose',
                                                   'addressedTo': 'The Recipient'})
    assert r.status_code == 200
    out = r.json()
    assert set(out) == {'html', 'missing', 'unknown'} and out['unknown'] == ['nope'] and isinstance(out['missing'], list)
    r = a.hr.post('/documents/templates/preview', {'employeeId': a.w.sita_id, 'templateId': a.template('confirmation'),
                                                   'purpose': None, 'addressedTo': None})
    assert r.status_code == 200 and 'DRAFT' in r.json()['html']


def test_letter_and_request_row_types(a):
    tpl = a.template('confirmation')
    r = a.hr.post('/documents/letters', {'templateId': tpl, 'employeeId': a.w.sita_id, 'visibleToEmployee': True,
                                         'purpose': None, 'addressedTo': None, 'allowMissing': True})
    assert r.status_code == 201
    lid = r.json()['id']
    rows = a.sita.get('/documents/letters').json()['letters']
    row = next(x for x in rows if x['id'] == lid)
    assert set(row) == {'id', 'employee_id', 'emp_code', 'name_en', 'template_code', 'ref_no', 'subject',
                        'visible_to_employee', 'request_id', 'issued_at', 'voided_at', 'void_reason', 'issued_by_name'}
    assert TS.match(row['issued_at']) and row['voided_at'] is None and row['request_id'] is None
    assert row['ref_no'] == r.json()['refNo'] and row['template_code'] == 'confirmation'
    a.hr.post(f'/documents/letters/{lid}/void', {'reason': 'typo in date'})
    hr_row = next(x for x in a.hr.get('/documents/letters', {'employeeId': a.w.sita_id}).json()['letters'] if x['id'] == lid)
    assert TS.match(hr_row['voided_at']) and hr_row['void_reason'] == 'typo in date'
    # HR's register search (?q=) and limit.
    assert a.hr.get('/documents/letters', {'q': 'zzz-no-match'}).json() == {'letters': []}
    assert len(a.hr.get('/documents/letters', {'limit': 1}).json()['letters']) == 1

    sal = next(t['id'] for t in a.sita.get('/documents/templates').json()['templates'] if t['code'] == 'salary_certificate')
    rid = a.sita.post('/documents/letter-requests', {'templateId': sal, 'purpose': 'bank loan', 'addressedTo': None}).json()['id']
    req = next(x for x in a.sita.get('/documents/letter-requests').json()['requests'] if x['id'] == rid)
    assert TS.match(req['created_at']) and req['decided_at'] is None and req['addressed_to'] is None
    assert (req['template_id'], req['status'], req['template_code']) == (sal, 'pending', 'salary_certificate')
    for status in ('pending', 'open'):
        assert [x['id'] for x in a.hr.get('/documents/letter-requests', {'status': status}).json()['requests']] == [rid]
    assert a.hr.get('/documents/letter-requests', {'status': 'issued'}).json() == {'requests': []}
    assert a.sita.post(f'/documents/letter-requests/{rid}/cancel').json() == {'ok': True}
    req = next(x for x in a.sita.get('/documents/letter-requests').json()['requests'] if x['id'] == rid)
    assert req['status'] == 'cancelled' and TS.match(req['decided_at'])


def test_hr_policy_rows_have_the_ts_columns_and_types(a):
    r = a.hr.post('/documents/policies', {'title': 'Leave policy', 'description': None, 'fileId': a.upload(a.hr, 'company_document'),
                                          'audience': 'all', 'audienceIds': [], 'requiresAck': True})
    assert r.status_code == 201
    pid = r.json()['id']
    rows = a.hr.get('/documents/policies').json()['policies']
    p = next(x for x in rows if x['id'] == pid)
    assert set(p) == {'id', 'title', 'description', 'audience', 'audience_ids', 'requires_ack', 'published_at',
                      'archived_at', 'filename', 'acks', 'audience_size', 'my_ack_at', 'in_audience'}
    assert (p['description'], p['audience'], p['audience_ids'], p['archived_at'], p['acks']) == (None, 'all', [], None, 0)
    assert TS.match(p['published_at']) and isinstance(p['audience_size'], int) and p['filename'] == 'doc.pdf'
    # A branch audience keeps its ids as strings.
    r = a.hr.post('/documents/policies', {'title': 'Branch rules', 'description': 'x', 'fileId': a.upload(a.hr, 'company_document'),
                                          'audience': 'branch', 'audienceIds': [a.w.branch], 'requiresAck': False})
    assert r.status_code == 201, r.content
    b = next(x for x in a.hr.get('/documents/policies').json()['policies'] if x['id'] == r.json()['id'])
    assert b['audience_ids'] == [a.w.branch] and b['requires_ack'] is False
    # The archive action, then ?archived=1.
    assert a.hr.patch(f'/documents/policies/{pid}', {'archived': True}).json() == {'ok': True}
    arch = next(x for x in a.hr.get('/documents/policies', {'archived': 1}).json()['policies'] if x['id'] == pid)
    assert TS.match(arch['archived_at'])
    acks = a.hr.get(f'/documents/policies/{pid}/acks').json()['employees']
    for e in acks:
        assert e['acknowledged_at'] is None
        _ts_or_none(e['last_reminded_at'])
    pol = a.hr.get(f'/documents/policies/{pid}/file')
    assert pol.status_code == 200 and pol['Content-Type'].startswith('application/pdf')
