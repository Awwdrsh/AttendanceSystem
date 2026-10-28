"""Port of apps/api/test/documents.test.ts at service level (the routes' rules, errors, audit and notifications)."""
from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import DatabaseError, transaction

from hajiri.core.clock import now as clock_now
from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError
from hajiri.documents import services as docs
from hajiri.documents.letters import npt_today, render_body, unknown_placeholders
from hajiri.workflow import service as wf

from .world import (add_days, build, employee_uploads, hr_verifies, outbox, signatory,
                    status, upload, welcome_template)

pytestmark = pytest.mark.django_db


# ================================================================ letter engine

def test_escapes_values_and_template_text_keeps_bold_and_lists():
    out = render_body('Dear **{{employee.name}}**,\n\n- one <b>\n- {{designation}}',
                      {'employee.name': '<script>x</script>', 'designation': 'a **b**'})
    assert '<b>&lt;script&gt;x&lt;/script&gt;</b>' in out['html']
    assert '<li>one &lt;b&gt;</li>' in out['html']
    assert 'a &#42;&#42;b&#42;&#42;' in out['html']
    assert '<script>' not in out['html']


def test_flags_unknown_placeholders_and_stray_braces():
    assert unknown_placeholders('Hi {{employee.name}}') == []
    assert unknown_placeholders('Hi {{employee.salary}}') == ['employee.salary']
    assert len(unknown_placeholders('Hi {{employee.name')) == 1


def test_reports_missing_values():
    assert render_body('{{designation}} {{grade}}', {'designation': 'X'})['missing'] == ['grade']


# ================================================================ employee documents

def test_seeds_categories_for_the_company_and_hides_other_companies():
    w = build()
    codes = [c['code'] for c in docs.list_categories(w.sita)]
    assert {'contract', 'citizenship', 'passport', 'other'} <= set(codes)
    foreign = docs.list_categories(w.foreign_hr)
    assert not set(c['id'] for c in foreign) & set(c['id'] for c in docs.list_categories(w.sita))


def test_employee_uploads_their_own_document_pending_hr_verification():
    w = build()
    employee_uploads(w)
    mine = docs.my_documents(w.sita)
    assert len(mine) == 1
    assert {k: mine[0][k] for k in ('status', 'title', 'version')} == {'status': 'pending', 'title': 'Citizenship', 'version': 1}
    assert outbox('document.uploaded') > 0


def test_refuses_categories_employees_may_not_upload_missing_expiry_and_other_employees():
    w = build()
    employee_uploads(w)
    assert status(docs.add_document, w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'contract'), 'title': 'Contract',
                                                         'fileId': upload(w.sita)}) == 403
    assert status(docs.add_document, w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'passport'), 'title': 'Passport',
                                                         'fileId': upload(w.sita)}) == 400
    assert status(docs.add_document, w.sita, w.peer_id, {'categoryId': w.category(w.sita, 'other'), 'title': 'X',
                                                         'fileId': upload(w.sita)}) == 403
    # A file uploaded by someone else cannot be attached.
    assert status(docs.add_document, w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'other'), 'title': 'X',
                                                         'fileId': upload(w.peer)}) == 403


def test_error_messages_match_the_ts_route():
    w = build()
    with pytest.raises(Exception) as e:
        docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'contract'), 'title': 'C', 'fileId': upload(w.sita)})
    assert e.value.message == 'only HR can add a employment contract'
    with pytest.raises(BadRequestError) as e:
        docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'passport'), 'title': 'P', 'fileId': upload(w.sita)})
    assert e.value.message == 'Passport needs an expiry date'
    with pytest.raises(Exception) as e:
        docs.add_document(w.sita, w.peer_id, {'categoryId': w.category(w.sita, 'other'), 'title': 'X', 'fileId': upload(w.sita)})
    assert e.value.message == 'you can only upload your own documents'


def test_serves_files_to_the_employee_and_hr_audited_never_to_managers_peers_or_other_companies():
    w = build()
    doc = employee_uploads(w)
    assert status(docs.document_file, w.sita, doc) == 200
    f = docs.document_file(w.hr, doc)
    assert f['mime'] == 'application/pdf'
    assert status(docs.document_file, w.ram, doc) == 404
    assert status(docs.document_file, w.peer, doc) == 404
    assert status(docs.document_file, w.foreign_hr, doc) == 404
    assert status(docs.employee_documents, w.ram, w.sita_id) == 403
    assert status(docs.employee_documents, w.foreign_hr, w.sita_id) == 404
    views = one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_document' AND row_id = %s", [doc])
    assert views['n'] == 1


def test_hr_verifies_or_rejects_with_a_reason_the_employee_cannot_verify():
    w = build()
    doc = employee_uploads(w)
    assert status(docs.verify_document, w.sita, doc, {'decision': 'verify'}) == 403
    assert status(docs.verify_document, w.hr, doc, {'decision': 'reject'}) == 400
    assert doc in [d['id'] for d in docs.queue(w.hr)['pending']]
    assert status(docs.verify_document, w.hr, doc, {'decision': 'verify'}) == 200
    assert status(docs.verify_document, w.hr, doc, {'decision': 'verify'}) == 409
    # Verified documents can no longer be deleted by the employee.
    assert status(docs.delete_document, w.sita, doc) == 403
    assert outbox('document.verified', employee_id=w.sita_id) == 1


def test_rejecting_tells_the_employee_why():
    w = build()
    doc = employee_uploads(w)
    docs.verify_document(w.hr, doc, {'decision': 'reject', 'reason': 'blurry scan'})
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'document.rejected'")
    assert n == {'title': 'Citizenship needs another look', 'body': 'HR could not accept your Citizenship: blurry scan. Upload it again.'}
    assert docs.my_documents(w.sita)[0]['reject_reason'] == 'blurry scan'


def test_hr_cannot_verify_their_own_documents():
    from tests.web.factories import make_employee, make_user
    w = build()
    me = make_employee(w.co, name='Gita HR')
    hr_self = make_user(w.co, roles=['hr'], employee=me)
    # An upload made before the person was given the HR role is still waiting.
    r = docs.add_document(hr_self, str(me.id), {'categoryId': w.category(hr_self, 'citizenship'), 'title': 'Citizenship',
                                                'fileId': upload(hr_self)})
    execute("UPDATE employee_document SET status = 'pending' WHERE id = %s", [r['id']])
    with pytest.raises(Exception) as e:
        docs.verify_document(hr_self, r['id'], {'decision': 'verify'})
    assert e.value.status == 403 and e.value.message == 'you cannot verify your own documents'


def test_keeps_versions_when_replaced_and_restores_the_previous_one_when_the_new_one_is_deleted():
    w = build()
    doc = employee_uploads(w)
    hr_verifies(w)
    r = docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'citizenship'), 'title': 'Citizenship',
                                              'fileId': upload(w.sita), 'replacesId': doc})
    mine = docs.my_documents(w.sita)
    assert len(mine) == 1
    assert mine[0]['version'] == 2
    hist = docs.employee_documents(w.hr, w.sita_id, {'history': '1'})
    assert [d['version'] for d in hist] == [2, 1]
    # Replacing an old version is refused.
    assert status(docs.add_document, w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'citizenship'), 'title': 'C',
                                                         'fileId': upload(w.sita), 'replacesId': doc}) == 409
    assert status(docs.delete_document, w.sita, r['id']) == 200
    after = docs.my_documents(w.sita)
    assert (after[0]['id'], after[0]['version']) == (doc, 1)
    wiped = one('SELECT f.data_enc FROM employee_document d JOIN stored_file f ON f.id = d.file_id WHERE d.id = %s', [r['id']])
    assert wiped['data_enc'] is None


def test_hr_only_documents_stay_hidden_from_the_employee():
    w = build()
    r = docs.add_document(w.hr, w.sita_id, {'categoryId': w.category(w.hr, 'contract'), 'title': 'Signed contract',
                                            'fileId': upload(w.hr), 'visibleToEmployee': False})
    assert r['id'] not in [d['id'] for d in docs.my_documents(w.sita)]
    assert status(docs.document_file, w.sita, r['id']) == 404
    # Nobody is told about an HR-only document.
    assert outbox('document.added') == 0


def test_hr_upload_is_verified_and_tells_the_employee():
    w = build()
    r = docs.add_document(w.hr, w.sita_id, {'categoryId': w.category(w.hr, 'academic'), 'title': 'Degree', 'fileId': upload(w.hr)})
    d = one('SELECT status, verified_by FROM employee_document WHERE id = %s', [r['id']])
    assert d['status'] == 'verified' and str(d['verified_by']) == w.hr.id
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'document.added'")
    assert n == {'title': 'New document in your profile', 'body': 'HR added Degree to your documents.'}


def test_sends_expiry_alerts_once_per_threshold_to_the_employee_and_a_summary_to_hr():
    w = build()
    today = npt_today()
    r = docs.add_document(w.hr, w.sita_id, {'categoryId': w.category(w.hr, 'passport'), 'title': 'Passport',
                                            'fileId': upload(w.hr), 'expiryDate': add_days(today, 20)})
    first = docs.documents_daily_job()
    assert first['expiryAlerts'] == 1
    assert docs.documents_daily_job()['expiryAlerts'] == 0
    # Twelve days on, it is within 7 days: one more alert.
    assert docs.documents_daily_job(clock_now() + timedelta(days=14))['expiryAlerts'] == 1
    assert outbox('document.expiring', 'in_app') == 2
    assert r['id'] in [d['id'] for d in docs.queue(w.hr, {'days': 30})['expiring']]
    assert status(docs.queue, w.ram) == 403
    summary = rows("SELECT title FROM notification_outbox WHERE template = 'document.expiring.summary' ORDER BY created_at")
    assert summary and summary[0]['title'] == '1 employee document expiring'


def test_a_new_expiry_date_restarts_its_alerts():
    w = build()
    r = docs.add_document(w.hr, w.sita_id, {'categoryId': w.category(w.hr, 'passport'), 'title': 'Passport',
                                            'fileId': upload(w.hr), 'expiryDate': add_days(npt_today(), 20)})
    assert docs.documents_daily_job()['expiryAlerts'] == 1
    docs.update_document(w.hr, r['id'], {'expiryDate': add_days(npt_today(), 25)})
    assert one('SELECT count(*)::int AS n FROM document_expiry_alert WHERE document_id = %s', [r['id']])['n'] == 0
    assert docs.documents_daily_job()['expiryAlerts'] == 1


# ================================================================ letters

def test_rejects_templates_with_unknown_placeholders_and_saves_valid_ones():
    w = build()
    with pytest.raises(BadRequestError) as e:
        docs.create_template(w.hr, {'code': 'bad_one', 'name': 'Bad', 'subject': 'S', 'body': 'Hello {{employee.nmae}}'})
    assert e.value.status == 400
    assert e.value.details['unknown'] == ['employee.nmae']
    assert e.value.message == 'Unknown placeholder: employee.nmae'
    assert welcome_template(w)
    assert status(docs.create_template, w.sita, {'code': 'x_y', 'name': 'X', 'subject': 'S', 'body': 'B'}) == 403


def test_previews_with_missing_values_marked_and_a_draft_watermark():
    w = build()
    r = docs.preview_letter(w.hr, {'employeeId': w.sita_id, 'templateId': w.template('appointment')})
    assert 'Sita' in r['html']
    assert 'DRAFT' in r['html']
    assert {'department', 'salary.gross'} <= set(r['missing'])
    assert status(docs.preview_letter, w.foreign_hr, {'employeeId': w.sita_id, 'subject': 'S', 'body': 'B'}) == 404


def test_refuses_to_issue_with_missing_values_unless_allowed_then_numbers_letters_per_bs_year():
    w = build()
    welcome = welcome_template(w)
    tpl = w.template('confirmation')
    with pytest.raises(BadRequestError) as e:
        docs.issue_letter(w.hr, {'templateId': w.template('promotion'), 'employeeId': w.sita_id})
    assert e.value.details['code'] == 'missing_values'
    signatory(w)
    one_ = docs.issue_letter(w.hr, {'templateId': tpl, 'employeeId': w.sita_id})
    assert re.fullmatch(r'CV/HR/20\d\d/0001', one_['refNo'])
    two = docs.issue_letter(w.hr, {'templateId': welcome, 'employeeId': w.sita_id})
    assert two['refNo'].endswith('/0002')
    html = docs.letter_html(w.sita, one_['id'])
    assert one_['refNo'] in html
    assert 'Gita HR' in html
    assert 'DRAFT' not in html
    assert status(docs.letter_html, w.peer, one_['id']) == 404
    assert status(docs.letter_html, w.ram, one_['id']) == 404
    assert status(docs.letter_html, w.foreign_hr, one_['id']) == 404
    # Issued letters cannot be edited in the database either.
    with pytest.raises(DatabaseError, match='cannot be changed'):
        with transaction.atomic():
            execute("UPDATE issued_letter SET html = 'x' WHERE id = %s", [one_['id']])
    # Voiding hides it from the employee and marks it for HR.
    assert status(docs.void_letter, w.hr, one_['id'], {'reason': 'wrong date'}) == 200
    assert status(docs.letter_html, w.sita, one_['id']) == 404
    assert 'VOID' in docs.letter_html(w.hr, one_['id'])
    assert [l['id'] for l in docs.list_letters(w.sita)] == [two['id']]
    assert status(docs.list_letters, w.sita, {'employeeId': w.peer_id}) == 403


def test_hr_reading_a_letter_is_audited_and_voiding_twice_is_refused():
    w = build()
    r = docs.issue_letter(w.hr, {'templateId': w.template('confirmation'), 'employeeId': w.sita_id})
    docs.letter_html(w.sita, r['id'])
    docs.letter_html(w.hr, r['id'])
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'view_letter' AND row_id = %s", [r['id']])['n'] == 1
    docs.void_letter(w.hr, r['id'], {'reason': 'wrong date'})
    assert status(docs.void_letter, w.hr, r['id'], {'reason': 'again'}) == 404
    assert status(docs.void_letter, w.hr2, r['id'], {'reason': 'no'}) == 400


def test_runs_a_letter_request_through_hr_inbox_issue_completes_it_and_notifies():
    w = build()
    reqs = docs.list_templates(w.sita)
    assert {'salary_certificate', 'experience'} <= {t['code'] for t in reqs}
    assert 'body' not in reqs[0]
    to_whom = next(t['id'] for t in reqs if t['code'] == 'to_whom_it_may_concern')
    assert status(docs.request_letter, w.sita, {'templateId': w.template('appointment'), 'purpose': 'bank loan'}) == 400
    r = docs.request_letter(w.sita, {'templateId': to_whom, 'purpose': 'visa application'})
    assert status(docs.request_letter, w.sita, {'templateId': to_whom, 'purpose': 'again'}) == 409
    item = next(i for i in wf.inbox_for(w.hr) if i['objectId'] == r['id'])
    assert {'letter': item['summary']['letter'], 'purpose': item['summary']['purpose']} == {
        'letter': 'To whom it may concern', 'purpose': 'visa application'}
    assert not [i for i in wf.inbox_for(w.ram) if i['objectId'] == r['id']]
    issued = docs.issue_letter(w.hr, {'templateId': to_whom, 'employeeId': w.sita_id, 'requestId': r['id'], 'allowMissing': True})
    mine = next(x for x in docs.list_requests(w.sita) if x['id'] == r['id'])
    assert (mine['status'], mine['issued_letter_id']) == ('issued', issued['id'])
    assert 'visa application' in docs.letter_html(w.sita, issued['id'])
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [r['id']])['state'] == 'approved'
    assert outbox('letter.issued', employee_id=w.sita_id) > 0


def test_a_rejected_request_notifies_the_employee_cancel_works_only_for_open_requests():
    w = build()
    tpl = w.template('salary_certificate')
    r = docs.request_letter(w.sita, {'templateId': tpl, 'purpose': 'bank loan', 'addressedTo': 'The Manager, ABC Bank'})
    inst = one('SELECT id FROM workflow_instance WHERE object_id = %s', [r['id']])['id']
    assert wf.decide(w.hr, str(inst), 'reject', reason='no salary set yet') == 'rejected'
    assert next(x for x in docs.list_requests(w.sita) if x['id'] == r['id'])['status'] == 'rejected'
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'letter_request.rejected' AND channel = 'in_app'")
    assert n == {'title': 'Salary certificate request declined',
                 'body': 'HR declined your request for a salary certificate: no salary set yet'}
    assert status(docs.cancel_request, w.sita, r['id']) == 409
    r2 = docs.request_letter(w.sita, {'templateId': tpl, 'purpose': 'bank loan'})
    assert status(docs.cancel_request, w.peer, r2['id']) == 404
    assert status(docs.cancel_request, w.sita, r2['id']) == 200
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [r2['id']])['state'] == 'cancelled'


def test_issue_for_a_request_refuses_another_employee_and_closed_requests():
    w = build()
    tpl = w.template('to_whom_it_may_concern')
    r = docs.request_letter(w.sita, {'templateId': tpl, 'purpose': 'visa application'})
    with pytest.raises(BadRequestError) as e:
        docs.issue_letter(w.hr, {'templateId': tpl, 'employeeId': w.peer_id, 'requestId': r['id'], 'allowMissing': True})
    assert e.value.message == 'the request belongs to another employee'
    docs.cancel_request(w.sita, r['id'])
    assert status(docs.issue_letter, w.hr, {'templateId': tpl, 'employeeId': w.sita_id, 'requestId': r['id'],
                                            'allowMissing': True}) == 409


def test_letter_settings_default_and_validation():
    w = build()
    assert docs.get_letter_settings(w.hr) == {'signatoryName': None, 'signatoryTitle': 'Human Resources', 'refPrefix': 'HR'}
    assert status(docs.save_letter_settings, w.hr, {'signatoryName': None, 'signatoryTitle': None, 'refPrefix': 'bad prefix!'}) == 400
    assert status(docs.get_letter_settings, w.sita) == 403
    signatory(w)
    assert docs.get_letter_settings(w.hr)['refPrefix'] == 'CV/HR'


def test_template_update_checks_placeholders_and_company():
    w = build()
    tid = w.template('appointment')
    assert status(docs.update_template, w.hr, tid, {'body': 'Hi {{nope}}'}) == 400
    assert status(docs.update_template, w.foreign_hr, tid, {'name': 'X'}) == 404
    docs.update_template(w.hr, tid, {'requestable': True})
    assert 'appointment' in {t['code'] for t in docs.list_templates(w.sita)}


# ================================================================ company policies

def _handbook(w) -> str:
    return docs.publish_policy(w.hr, {'title': 'Employee handbook', 'description': 'Read it',
                                      'fileId': upload(w.hr, 'company_document')})['id']


def test_publishes_to_an_audience_notifies_and_needs_a_company_document_upload():
    w = build()
    assert status(docs.publish_policy, w.hr, {'title': 'Handbook', 'fileId': upload(w.hr, 'expense_receipt')}) == 400
    assert _handbook(w)
    assert status(docs.publish_policy, w.sita, {'title': 'X', 'fileId': upload(w.sita, 'company_document')}) == 403
    assert outbox('policy.published', 'in_app') >= 3


def test_branch_audiences_only_reach_that_branch():
    w = build()
    policy = _handbook(w)
    b2 = str(one("INSERT INTO branch (company_id, code, name_en) VALUES (%s, 'PKR', 'Pokhara') RETURNING id", [w.co.id])['id'])
    r = docs.publish_policy(w.hr, {'title': 'Pokhara rules', 'fileId': upload(w.hr, 'company_document'), 'audience': 'branch',
                                   'audienceIds': [b2]})
    assert [p['id'] for p in docs.list_policies(w.sita)] == [policy]
    assert status(docs.policy_file, w.sita, r['id']) == 404
    assert status(docs.acknowledge_policy, w.sita, r['id']) == 404
    assert status(docs.publish_policy, w.hr, {'title': 'x', 'fileId': upload(w.hr, 'company_document'), 'audience': 'branch',
                                              'audienceIds': ['00000000-0000-7000-8000-00000000ffff']}) == 400
    assert status(docs.publish_policy, w.hr, {'title': 'x', 'fileId': upload(w.hr, 'company_document'), 'audience': 'branch',
                                              'audienceIds': []}) == 400


def test_department_audiences_include_sub_units():
    w = build()
    parent = str(one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'ENG', 'Engineering') RETURNING id", [w.co.id])['id'])
    child = str(one("INSERT INTO org_unit (company_id, kind, code, name_en, parent_id) VALUES (%s, 'team', 'WEB', 'Web', %s) RETURNING id",
                    [w.co.id, parent])['id'])
    execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s', [child, w.sita_id])
    r = docs.publish_policy(w.hr, {'title': 'Engineering rules', 'fileId': upload(w.hr, 'company_document'),
                                   'audience': 'department', 'audienceIds': [parent]})
    assert [p['id'] for p in docs.list_policies(w.sita)] == [r['id']]
    assert docs.list_policies(w.peer) == []


def test_employees_read_and_acknowledge_hr_sees_who_has_and_reminds_the_rest():
    w = build()
    policy = _handbook(w)
    assert status(docs.policy_file, w.sita, policy) == 200
    assert status(docs.acknowledge_policy, w.sita, policy) == 200
    assert status(docs.acknowledge_policy, w.sita, policy) == 200
    acks = docs.policy_acks(w.hr, policy)
    sita_row = next(e for e in acks if e['employee_id'] == w.sita_id)
    assert sita_row['acknowledged_at'] is not None
    assert status(docs.policy_acks, w.sita, policy) == 403
    p = next(x for x in docs.list_policies(w.hr) if x['id'] == policy)
    assert p['acks'] == 1
    assert p['audience_size'] >= 3
    assert docs.remind_policy(w.hr, policy)['sent'] == 2
    assert status(docs.policy_file, w.foreign_hr, policy) == 404


def test_nudges_weekly_from_the_daily_job_and_stops_once_acknowledged_or_archived():
    w = build()
    policy = _handbook(w)
    docs.acknowledge_policy(w.sita, policy)
    docs.remind_policy(w.hr, policy)
    later = clock_now() + timedelta(days=9)
    assert docs.documents_daily_job(later)['policyNudges'] == 2
    assert docs.documents_daily_job(later)['policyNudges'] == 0
    docs.acknowledge_policy(w.ram, policy)
    assert docs.documents_daily_job(later + timedelta(days=8))['policyNudges'] == 1
    assert status(docs.update_policy, w.hr, policy, {'archived': True}) == 200
    assert docs.documents_daily_job(later + timedelta(days=30))['policyNudges'] == 0
    assert docs.list_policies(w.peer) == []
    assert status(docs.remind_policy, w.hr, policy) == 409


# ================================================================ categories and seeding

def test_hr_adds_and_updates_categories():
    w = build()
    r = docs.create_category(w.hr, {'code': 'work_permit', 'name': 'Work permit', 'expiryRequired': True})
    assert status(docs.create_category, w.sita, {'code': 'x_y', 'name': 'X'}) == 403
    assert status(docs.create_category, w.hr, {'code': 'Bad Code', 'name': 'X'}) == 400
    docs.update_category(w.hr, r['id'], {'active': False})
    assert r['id'] not in [c['id'] for c in docs.list_categories(w.sita)]
    assert r['id'] in [c['id'] for c in docs.list_categories(w.hr)]
    assert status(docs.update_category, w.foreign_hr, r['id'], {'name': 'X'}) == 404
    # An inactive category cannot take uploads.
    assert status(docs.add_document, w.hr, w.sita_id, {'categoryId': r['id'], 'title': 'P', 'fileId': upload(w.hr),
                                                       'expiryDate': add_days(npt_today(), 300)}) == 400


def test_seeding_is_idempotent_and_adds_the_letter_request_flow():
    from hajiri.documents.letters import seed_document_defaults
    w = build()
    first = seed_document_defaults(str(w.co.id))
    again = seed_document_defaults(str(w.co.id))
    assert again == {'categories': 0, 'templates': 0}
    assert first['templates'] in (0, 11)
    assert one("SELECT steps FROM workflow_definition WHERE company_id = %s AND object_type = 'letter_request'", [w.co.id])['steps'] == [{'role': 'hr'}]


def test_seeder_and_daily_job_are_registered():
    from hajiri.worker import registry
    assert 'documents' in {j.name for j in registry.daily_jobs()}
    assert 'documents' in {s.name for s in registry.seeders()}
    assert wf.handler_for('letter_request') is docs.apply_letter_request_decision
