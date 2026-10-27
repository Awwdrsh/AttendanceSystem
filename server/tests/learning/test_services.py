"""Port of apps/api/test/performance.test.ts, 'learning' block, at service level."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.files import service as files
from hajiri.learning import services as lrn
from hajiri.performance.jobs import performance_reminders
from hajiri.workflow import service as wf
from tests.performance.world import plus_days

pytestmark = pytest.mark.django_db
PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def raises(exc, fn, *a, **kw):
    with pytest.raises(exc) as e:
        fn(*a, **kw)
    return e.value


def catalogue(w):
    online = lrn.create_course(w.hr, {'code': 'SAFE', 'title': 'Fire safety', 'delivery': 'online', 'mandatory': True, 'validityMonths': 12,
                                      'passMark': 60, 'cost': '1000'})
    lrn.save_questions(w.hr, online, {'questions': [
        {'prompt': 'Extinguisher for electrical fire?', 'options': ['Water', 'CO2'], 'correctIndex': 1},
        {'prompt': 'Assembly point?', 'options': ['Car park', 'Roof'], 'correctIndex': 0}]})
    classroom = lrn.create_course(w.hr, {'code': 'EXCEL', 'title': 'Excel for HR', 'delivery': 'classroom', 'cost': '2500'})
    return online, classroom


def passed_online(w, online):
    eid = lrn.self_enroll(w.sita, {'courseId': online, 'reason': 'Required'})
    inst = wf.instance_for_object('training_nomination', eid)
    wf.decide(w.ram, inst['id'], 'approve')
    lrn.attempt(w.sita, eid, {'answers': [1, 0]})
    return eid


def test_hr_builds_the_catalogue_employees_cannot(w):
    raises(ForbiddenError, lrn.create_course, w.sita, {'code': 'X', 'title': 'x'})
    online, _ = catalogue(w)
    e = raises(ValidationError, lrn.save_questions, w.hr, online, {'questions': [{'prompt': 'x', 'options': ['a', 'b'], 'correctIndex': 5}]})
    assert e.issues[0]['message'] == 'the correct answer must be one of the options'
    learner = lrn.course_detail(w.sita, online)
    assert learner['questions'] == [] and learner['questionCount'] == 2
    assert lrn.course_detail(w.hr, online)['questions'][0]['correct_index'] == 1


def test_self_nomination_goes_to_manager_assessment_completes_and_certifies(w):
    online, _ = catalogue(w)
    eid = lrn.self_enroll(w.sita, {'courseId': online, 'reason': 'Required'})
    e = raises(ConflictError, lrn.self_enroll, w.sita, {'courseId': online})
    assert e.message == 'you are already enrolled or waiting for approval for this course'
    item = next(i for i in wf.inbox_for(w.ram) if i['objectId'] == eid)
    assert item['summary']['course_title'] == 'Fire safety'
    wf.decide(w.ram, item['instanceId'], 'approve')
    a = lrn.assessment(w.sita, eid)
    assert len(a['questions']) == 2 and 'correct' not in str(a)
    raises(NotFoundError, lrn.assessment, w.hari, eid)
    e = raises(ConflictError, lrn.complete, w.sita, eid)
    assert e.message == 'pass the assessment to complete this course'
    e = raises(BadRequestError, lrn.attempt, w.sita, eid, {'answers': [1]})
    assert e.message == 'answer all 2 questions'
    fail = lrn.attempt(w.sita, eid, {'answers': [0, 1]})
    assert fail['score'] == 0 and fail['passed'] is False
    ok = lrn.attempt(w.sita, eid, {'answers': [1, 0]})
    assert ok['score'] == 100 and ok['passed'] is True
    row = one('SELECT status, certificate_issued_on, certificate_expires_on FROM lrn_enrollment WHERE id = %s', [eid])
    assert row['status'] == 'completed'
    assert row['certificate_expires_on'].isoformat() == lrn.add_months(row['certificate_issued_on'].isoformat(), 12)
    assert 'Certificate of completion' in lrn.certificate_html(w.sita, eid)
    lrn.course_feedback(w.sita, eid, {'rating': 5})
    assert [n['template'] for n in rows("SELECT template FROM notification_outbox WHERE template LIKE 'learning.%%' ORDER BY created_at, id")] == \
        ['learning.nomination_approved', 'learning.completed']


def test_rejected_nomination_and_cancel(w):
    online, classroom = catalogue(w)
    eid = lrn.self_enroll(w.sita, {'courseId': classroom})
    inst = wf.instance_for_object('training_nomination', eid)
    wf.decide(w.ram, inst['id'], 'reject', reason='budget')
    assert one('SELECT status FROM lrn_enrollment WHERE id = %s', [eid])['status'] == 'rejected'
    assert one("SELECT body FROM notification_outbox WHERE template = 'learning.nomination_rejected'")['body'] == 'Excel for HR (budget).'
    other = lrn.self_enroll(w.hari, {'courseId': online})
    lrn.cancel(w.hari, other)
    assert wf.instance_for_object('training_nomination', other)['state'] == 'cancelled'
    raises(ConflictError, lrn.cancel, w.hari, other)


def test_managers_assign_their_team_only_classroom_attendance_completes(w):
    _, classroom = catalogue(w)
    raises(ForbiddenError, lrn.assign, w.hari, {'courseId': classroom, 'employeeIds': [w.GITA]})
    raises(NotFoundError, lrn.assign, w.ram, {'courseId': classroom, 'employeeIds': [w.OTHER_EMP]})
    assert lrn.assign(w.ram, {'courseId': classroom, 'employeeIds': [w.HARI, w.HARI]}) == {'added': 1, 'skipped': 0}
    enr = next(e for e in lrn.list_enrollments(w.hari) if e['course_id'] == classroom)
    raises(ForbiddenError, lrn.complete, w.hari, enr['id'])
    raises(NotFoundError, lrn.record_attendance, w.gita, enr['id'], {'attended': True})
    lrn.record_attendance(w.ram, enr['id'], {'attended': True})
    team = lrn.list_enrollments(w.ram, {'scope': 'team'})
    assert next(e for e in team if e['id'] == enr['id'])['status'] == 'completed'
    raises(ForbiddenError, lrn.list_enrollments, w.ram, {'scope': 'all'})


def test_sessions_respect_capacity(w):
    _, classroom = catalogue(w)
    now = datetime.now(timezone.utc)
    iso = lambda d: d.isoformat().replace('+00:00', 'Z')  # noqa: E731
    sid = lrn.add_session(w.hr, classroom, {'startsAt': iso(now + timedelta(days=2)), 'endsAt': iso(now + timedelta(days=2, hours=1)),
                                            'capacity': 1, 'venue': 'Room 2'})
    lrn.self_enroll(w.gita, {'courseId': classroom, 'sessionId': sid})
    e = raises(ConflictError, lrn.self_enroll, w.sita, {'courseId': classroom, 'sessionId': sid})
    assert e.message == 'this session is full'
    e = raises(BadRequestError, lrn.add_session, w.hr, classroom, {'startsAt': iso(now), 'endsAt': iso(now)})
    assert e.message == 'the session ends before it starts'
    lrn.update_session(w.hr, sid, {'status': 'cancelled'})
    e = raises(ConflictError, lrn.self_enroll, w.hari, {'courseId': classroom, 'sessionId': sid})
    assert e.message == 'this session is not open'


def test_certificates_uploaded_by_learner_seen_by_managers_and_hr_only(w):
    online, _ = catalogue(w)
    eid = passed_online(w, online)
    up = files.upload(w.sita, {'purpose': 'training_certificate', 'filename': 'cert.pdf', 'dataUrl': PDF})
    raises(NotFoundError, lrn.attach_certificate, w.hari, eid, {'fileId': up['id']})
    raises(ForbiddenError, lrn.attach_certificate, w.sita, eid, {'fileId': up['id'], 'expiresOn': '2030-01-01'})
    lrn.attach_certificate(w.sita, eid, {'fileId': up['id']})
    assert lrn.certificate_file(w.ram, eid)['data'].startswith(b'%PDF')
    raises(NotFoundError, lrn.certificate_file, w.hari, eid)
    raises(NotFoundError, lrn.certificate_file, w.other_hr, eid)


def test_mandatory_assignment_cost_report_and_expiry_reminders(w):
    online, _ = catalogue(w)
    eid = passed_online(w, online)
    assert lrn.assign_mandatory(w.hr, online) == 3   # everyone except Sita, whose certificate is still valid
    cost = lrn.cost_report(w.hr, {'from': plus_days(-5), 'to': plus_days(5)})
    assert float(cost['total']) > 0
    raises(ForbiddenError, lrn.cost_report, w.ram, {'from': plus_days(-5), 'to': plus_days(5)})
    execute('UPDATE lrn_enrollment SET certificate_expires_on = %s WHERE id = %s', [plus_days(10), eid])
    assert eid in [e['id'] for e in lrn.expiring(w.hr)]
    assert performance_reminders()['learning']['expiring'] == 1
    assert performance_reminders()['learning']['expiring'] == 0


def test_session_reminder_the_day_before(w):
    _, classroom = catalogue(w)
    start = (datetime.now(timezone.utc) + timedelta(minutes=345)).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
    s = start.replace(tzinfo=None).isoformat() + '+05:45'
    e = start.replace(tzinfo=None, hour=10).isoformat() + '+05:45'
    sid = lrn.add_session(w.hr, classroom, {'startsAt': s, 'endsAt': e, 'venue': 'Room 2'})
    lrn.assign(w.hr, {'courseId': classroom, 'sessionId': sid, 'employeeIds': [w.HARI]})
    assert performance_reminders()['learning']['sessions'] == 1
    assert one("SELECT body FROM notification_outbox WHERE template = 'learning.session_tomorrow'")['body'] == 'Excel for HR at Room 2.'
    assert performance_reminders()['learning']['sessions'] == 0
