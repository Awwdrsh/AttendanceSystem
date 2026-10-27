"""My learning and the Learning tab of the Performance hub, through django.test.Client."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.core.db import one, rows
from hajiri.files import service as files
from hajiri.learning import services as lrn
from hajiri.workflow import service as wf
from tests.web.factories import htmx, set_features_off, sign_in, toasts

pytestmark = pytest.mark.django_db
PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def client(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def test_hr_builds_a_course_with_sessions_and_an_assessment(w):
    h = client(w.hr)
    assert 'No courses yet' in h.get('/performance/learning').content.decode()
    r = h.post('/_/learning/courses/new', {'code': 'SAFE', 'title': 'Fire safety', 'delivery': 'online', 'source': 'internal', 'cost': '1000',
                                           'mandatory': 'on', 'validity_months': '12', 'pass_mark': '60', 'active': 'on'}, **htmx())
    assert toasts(r)[0]['message'] == 'Course saved.'
    cid = lrn.list_courses(w.hr)[0]['id']
    assert one('SELECT cost::text AS c, mandatory FROM lrn_course WHERE id = %s', [cid]) == {'c': '1000.00', 'mandatory': True}
    drawer = h.get(f'/_/learning/courses/{cid}/edit', **htmx()).content.decode()
    assert 'Sessions' in drawer and 'Assessment' in drawer and 'Enrol everyone it applies to' in drawer
    r = h.post(f'/_/learning/courses/{cid}/questions', {'count': '2', 'q0_prompt': 'Extinguisher for electrical fire?', 'q0_option': ['Water', 'CO2'],
                                                         'q0_correct': '1', 'q1_prompt': 'Assembly point?', 'q1_option': ['Car park', 'Roof'], 'q1_correct': '0'}, **htmx())
    assert toasts(r)[0]['message'] == 'Assessment saved.'
    assert [q['correct_index'] for q in rows('SELECT correct_index FROM lrn_question WHERE course_id = %s ORDER BY sort', [cid])] == [1, 0]
    day = (datetime.now(timezone.utc) + timedelta(minutes=345, days=7)).date().isoformat()
    r = h.post(f'/_/learning/courses/{cid}/sessions', {'date': day, 'start': '10:00', 'end': '13:00', 'venue': 'Room 2', 'capacity': '10'}, **htmx())
    assert toasts(r)[0]['message'] == 'Session added.' and 'Room 2' in r.content.decode()
    # A bad link is refused.
    r = h.post(f'/_/learning/courses/{cid}/edit', {'code': 'SAFE', 'title': 'Fire safety', 'url': 'ftp://x', 'active': 'on'}, **htmx())
    assert r.status_code == 400
    assert client(w.sita).post('/_/learning/courses/new', {'code': 'X', 'title': 'x'}, **htmx()).status_code == 403
    assert 'Fire safety' in h.get('/performance/learning').content.decode()


def _online(w):
    cid = lrn.create_course(w.hr, {'code': 'SAFE', 'title': 'Fire safety', 'delivery': 'online', 'mandatory': True, 'validityMonths': 12, 'cost': '1000'})
    lrn.save_questions(w.hr, cid, {'questions': [{'prompt': 'Q1', 'options': ['Water', 'CO2'], 'correctIndex': 1},
                                                 {'prompt': 'Q2', 'options': ['Car park', 'Roof'], 'correctIndex': 0}]})
    return cid


def test_employee_requests_a_course_takes_the_assessment_and_gets_a_certificate(w):
    cid = _online(w)
    s = client(w.sita)
    assert 'Request to join' in s.get('/me/growth/learning?view=catalogue').content.decode()
    assert 'Join Fire safety' in s.get(f'/_/learning/courses/{cid}/join', **htmx()).content.decode()
    r = s.post(f'/_/learning/courses/{cid}/join', {'reason': 'Required'}, **htmx())
    assert toasts(r)[0]['message'] == 'Sent to your manager for approval.'
    assert 'Awaiting approval' in s.get('/me/growth/learning').content.decode()
    eid = lrn.list_enrollments(w.sita)[0]['id']
    # The manager approves in the approvals inbox.
    inbox = client(w.ram).get('/approvals').content.decode()
    assert 'Fire safety' in inbox
    wf.decide(w.ram, wf.instance_for_object('training_nomination', eid)['id'], 'approve')
    form = s.get(f'/_/learning/enrollments/{eid}/assessment', **htmx()).content.decode()
    assert 'Q1' in form and 'correct' not in form.lower().replace('correctly', '')
    assert client(w.hari).get(f'/_/learning/enrollments/{eid}/assessment', **htmx()).status_code == 404
    r = s.post(f'/_/learning/enrollments/{eid}/assessment', {'q0': '0', 'q1': '1'}, **htmx())
    assert 'Not passed yet' in r.content.decode() and '4 attempts left' in r.content.decode()
    r = s.post(f'/_/learning/enrollments/{eid}/assessment', {'q0': '1', 'q1': '0'}, **htmx())
    assert 'Passed' in r.content.decode()
    page = s.get('/me/growth/learning').content.decode()
    assert 'Completed' in page and 'Valid until' in page and 'Rate course' in page
    cert = s.get(f'/_/learning/enrollments/{eid}/certificate')
    assert cert.status_code == 200 and 'Certificate of completion' in cert.content.decode()
    r = s.post(f'/_/learning/enrollments/{eid}/feedback', {'rating': '5'}, **htmx())
    assert toasts(r)[0]['message'] == 'Thanks for the feedback.'
    up = files.upload(w.sita, {'purpose': 'training_certificate', 'filename': 'cert.pdf', 'dataUrl': PDF})
    r = s.post(f'/_/learning/enrollments/{eid}/certificate/upload', {'file_id': up['id']}, **htmx())
    assert toasts(r)[0]['message'] == 'Certificate added.'
    assert client(w.ram).get(f'/_/learning/enrollments/{eid}/certificate-file').status_code == 200
    assert client(w.hari).get(f'/_/learning/enrollments/{eid}/certificate-file').status_code == 404


def test_manager_assigns_and_records_attendance(w):
    cid = lrn.create_course(w.hr, {'code': 'EXCEL', 'title': 'Excel for HR', 'delivery': 'classroom', 'cost': '2500'})
    m = client(w.ram)
    form = m.get(f'/_/learning/courses/{cid}/assign', **htmx()).content.decode()
    assert 'Hari Karki' in form and 'Foreign' not in form
    r = m.post(f'/_/learning/courses/{cid}/assign', {'employee_ids': [w.HARI]}, **htmx())
    assert toasts(r)[0]['message'] == '1 enrolled.'
    r = m.post(f'/_/learning/courses/{cid}/assign', {'employee_ids': [w.HARI]}, **htmx())
    assert toasts(r)[0]['message'] == '0 enrolled, 1 already enrolled.'
    page = m.get('/performance/learning?view=enrollments').content.decode()
    assert "Your team's training" in page and 'Attended' in page
    eid = lrn.list_enrollments(w.hari)[0]['id']
    assert client(w.gita).post(f'/_/learning/enrollments/{eid}/attendance', {'attended': '1'}, **htmx()).status_code == 404
    r = m.post(f'/_/learning/enrollments/{eid}/attendance', {'attended': '1'}, **htmx())
    assert toasts(r)[0]['message'] == 'Attendance recorded.'
    assert one('SELECT status FROM lrn_enrollment WHERE id = %s', [eid])['status'] == 'completed'
    # Managers do not get the HR tabs.
    assert m.get('/performance/learning?view=cost').status_code == 200
    assert 'Training spend' not in m.get('/performance/learning?view=cost').content.decode()


def test_hr_certificates_and_spend_tabs(w):
    cid = _online(w)
    lrn.assign(w.hr, {'courseId': cid, 'employeeIds': [w.SITA, w.HARI]})
    h = client(w.hr)
    page = h.get('/performance/learning?view=cost').content.decode()
    assert 'Training spend' in page and '2,000.00' in page
    assert 'Nothing expiring' in h.get('/performance/learning?view=expiring&days=30').content.decode()
    r = h.post(f'/_/learning/courses/{cid}/assign-mandatory', **htmx())
    assert toasts(r)[0]['message'] == '2 people enrolled.'


def test_learning_switched_off(w):
    set_features_off(w.co, 'learning')
    assert client(w.sita).get('/me/growth/learning').status_code == 302
    assert client(w.ram).get('/_/learning/enrollments/00000000-0000-7000-8000-000000000001/cancel', **htmx()).status_code in (404, 405)


def test_assign_uses_the_searchable_picker_and_rating_waits_for_a_choice(w):
    cid = lrn.create_course(w.hr, {'code': 'EXCEL', 'title': 'Excel for HR', 'delivery': 'online', 'cost': '0'})
    form = client(w.ram).get(f'/_/learning/courses/{cid}/assign', **htmx()).content.decode()
    assert 'data-multi-pick' in form and 'name="employee_ids"' in form and 'Gita Rai, Engineer' in form
    r = client(w.ram).post(f'/_/learning/courses/{cid}/assign', {'employee_ids': [w.HARI, w.GITA]}, **htmx())
    assert toasts(r)[0]['message'] == '2 enrolled.'
    eid = lrn.list_enrollments(w.hari)[0]['id']
    lrn.complete(w.hari, eid)
    form = client(w.hari).get(f'/_/learning/enrollments/{eid}/feedback', **htmx()).content.decode()
    assert ':disabled="!r" disabled' in form and 'Excellent' in form
    # With Learning off, the Growth hub's Learning tab opens Goals instead (Hub.tsx).
    set_features_off(w.co, 'learning')
    assert client(w.sita).get('/me/growth/learning')['Location'] == '/me/growth'
