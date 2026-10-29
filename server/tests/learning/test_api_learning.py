"""
The /api/v1/learning endpoints as the React app calls them (frontend/src/api/performance.js `learning`): the session
cookie and JSON bodies, never a CSRF token. A port of the HTTP-level assertions of the 'learning' block of
apps/api/test/performance.test.ts (65a9079), plus the response shapes the pages read, the feature switch and company
isolation (including a group of companies).
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest

from hajiri.files import service as files
from hajiri.learning import services as lrn
from hajiri.performance.jobs import performance_reminders
from hajiri.workflow import service as wf
from tests.multicompany.conftest import world  # noqa: F401  (the fixture)
from tests.performance.test_api_performance import Browser
from tests.performance.test_isolation import Beta
from tests.performance.world import build, plus_days
from tests.support.sql import execute, one
from tests.web.factories import set_features_off

L = '/api/v1/learning'
PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()


def _url(path: str) -> str:
    return path if path.startswith('/api') else f'{L}{path}'


class LBrowser(Browser):
    """The same React client, with paths under /api/v1/learning."""

    def get(self, path, query=None):
        return super().get(_url(path), query)

    def _send(self, method, path, body):
        return super()._send(method, _url(path), body)

    def delete(self, path):
        return super().delete(_url(path))


@pytest.fixture
def w(db):
    w = build()
    for k in ('hr', 'sita', 'ram', 'hari', 'gita', 'other_hr'):
        setattr(w, f'b_{k}', LBrowser(getattr(w, k)))
    return w


def ids(rows) -> list[str]:
    return [r['id'] for r in rows]


def _catalogue(w):
    hr = w.b_hr
    c = hr.post('/courses', {'code': 'SAFE', 'title': 'Fire safety', 'delivery': 'online', 'mandatory': True, 'validityMonths': 12,
                             'passMark': 60, 'cost': '1000'})
    assert c.status_code == 201 and set(c.json()) == {'id'}
    online = c.json()['id']
    r = hr.put(f'/courses/{online}/questions', {'questions': [
        {'prompt': 'Extinguisher for electrical fire?', 'options': ['Water', 'CO2'], 'correctIndex': 1},
        {'prompt': 'Assembly point?', 'options': ['Car park', 'Roof'], 'correctIndex': 0}]})
    assert r.status_code == 200 and r.json() == {'ok': True}
    classroom = hr.post('/courses', {'code': 'EXCEL', 'title': 'Excel for HR', 'delivery': 'classroom', 'cost': '2500'}).json()['id']
    return online, classroom


def _passed(w, online) -> str:
    eid = w.b_sita.post('/enrollments/self', {'courseId': online, 'reason': 'Required'}).json()['id']
    wf.decide(w.ram, wf.instance_for_object('training_nomination', eid)['id'], 'approve')
    w.b_sita.post(f'/enrollments/{eid}/attempts', {'answers': [1, 0]})
    return eid


def test_hr_builds_the_catalogue_employees_cannot(w):
    sita, hr, other = w.b_sita, w.b_hr, w.b_other_hr
    r = sita.post('/courses', {'code': 'X', 'title': 'x'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    online, classroom = _catalogue(w)
    r = hr.put(f'/courses/{online}/questions', {'questions': [{'prompt': 'x', 'options': ['a', 'b'], 'correctIndex': 5}]})
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'the correct answer must be one of the options'
    learner = sita.get(f'/courses/{online}').json()
    assert set(learner) == {'course', 'sessions', 'questions', 'questionCount'}
    assert learner['questions'] == [] and learner['questionCount'] == 2
    assert hr.get(f'/courses/{online}').json()['questions'][0]['correct_index'] == 1
    assert learner['course']['code'] == 'SAFE' and learner['course']['cost'] == '1000.00'
    listing = sita.get('/courses').json()['courses']
    assert ids(listing) == [online, classroom]            # mandatory first
    assert {'questions', 'completed', 'enrolled', 'next_session', 'code', 'title', 'mandatory'} <= set(listing[0])
    # HR edits; another company cannot see or change the course.
    assert sita.patch(f'/courses/{classroom}', {'title': 'x'}).status_code == 403
    assert hr.patch(f'/courses/{classroom}', {'active': False}).json() == {'ok': True}
    assert classroom not in ids(sita.get('/courses').json()['courses'])
    assert sita.get(f'/courses/{classroom}').status_code == 404
    assert classroom in ids(hr.get('/courses').json()['courses'])
    for r in (other.get(f'/courses/{online}'), other.patch(f'/courses/{online}', {'title': 'x'}),
              other.put(f'/courses/{online}/questions', {'questions': []}),
              other.post(f'/courses/{online}/sessions', {'startsAt': '2026-12-01T09:00:00+05:45', 'endsAt': '2026-12-01T10:00:00+05:45'}),
              other.post(f'/courses/{online}/assign-mandatory')):
        assert r.status_code == 404, r.content
    assert online not in ids(other.get('/courses').json()['courses'])
    assert sita.get('/courses/nope').status_code == 400


def test_self_nomination_assessment_and_certificate(w):
    sita, ram, hari = w.b_sita, w.b_ram, w.b_hari
    online, _ = _catalogue(w)
    n = sita.post('/enrollments/self', {'courseId': online, 'reason': 'Required'})
    assert n.status_code == 201
    eid = n.json()['id']
    r = sita.post('/enrollments/self', {'courseId': online})
    assert r.status_code == 409 and r.json() == {'error': 'you are already enrolled or waiting for approval for this course'}
    item = next(i for i in wf.inbox_for(w.ram) if i['objectId'] == eid)
    assert item['summary']['course_title'] == 'Fire safety'
    wf.decide(w.ram, item['instanceId'], 'approve')
    a = sita.get(f'/enrollments/{eid}/assessment')
    assert a.status_code == 200
    assert set(a.json()) == {'title', 'passMark', 'questions', 'attempts', 'attemptsLeft'}
    assert len(a.json()['questions']) == 2 and 'correct' not in a.content.decode()
    assert hari.get(f'/enrollments/{eid}/assessment').status_code == 404
    assert ram.get(f'/enrollments/{eid}/assessment').status_code == 404      # the learner's only
    r = sita.post(f'/enrollments/{eid}/complete')
    assert r.status_code == 409 and r.json() == {'error': 'pass the assessment to complete this course'}
    r = sita.post(f'/enrollments/{eid}/attempts', {'answers': [1]})
    assert r.status_code == 400 and r.json() == {'error': 'answer all 2 questions'}
    fail = sita.post(f'/enrollments/{eid}/attempts', {'answers': [0, 1]})
    assert fail.status_code == 201 and fail.json()['score'] == 0 and fail.json()['passed'] is False
    ok = sita.post(f'/enrollments/{eid}/attempts', {'answers': [1, 0]})
    assert ok.json() == {'score': 100, 'passed': True, 'correct': 2, 'total': 2, 'attemptsLeft': 3}
    row = one('SELECT status, certificate_issued_on, certificate_expires_on FROM lrn_enrollment WHERE id = %s', [eid])
    assert row['status'] == 'completed'
    assert row['certificate_expires_on'].isoformat() == lrn.add_months(row['certificate_issued_on'].isoformat(), 12)
    cert = sita.get(f'/enrollments/{eid}/certificate')
    assert cert.status_code == 200 and cert['Content-Type'].startswith('text/html')
    assert 'Certificate of completion' in cert.content.decode()
    assert ram.get(f'/enrollments/{eid}/certificate').status_code == 200
    assert hari.get(f'/enrollments/{eid}/certificate').status_code == 404
    assert w.b_other_hr.get(f'/enrollments/{eid}/certificate').status_code == 404
    assert sita.post(f'/enrollments/{eid}/feedback', {'rating': 5}).json() == {'ok': True}
    assert sita.post(f'/enrollments/{eid}/feedback', {'rating': 6}).status_code == 400
    assert hari.post(f'/enrollments/{eid}/feedback', {'rating': 5}).status_code == 404
    mine = sita.get('/enrollments').json()['enrollments']
    assert mine[0]['id'] == eid and mine[0]['status'] == 'completed' and mine[0]['feedback_rating'] == 5
    assert {'code', 'title', 'delivery', 'mandatory', 'pass_mark', 'name_en', 'emp_code', 'questions', 'attempts', 'cost'} <= set(mine[0])


def test_managers_assign_their_team_only_attendance_completes(w):
    hari, ram, gita, hr = w.b_hari, w.b_ram, w.b_gita, w.b_hr
    _, classroom = _catalogue(w)
    r = hari.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.GITA]})
    assert r.status_code == 403
    assert ram.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.OTHER_EMP]}).status_code == 404
    a = ram.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.HARI, w.HARI]})
    assert a.status_code == 201 and a.json() == {'added': 1, 'skipped': 0}
    enr = next(e for e in hari.get('/enrollments').json()['enrollments'] if e['course_id'] == classroom)
    r = hari.post(f'/enrollments/{enr["id"]}/complete')
    assert r.status_code == 403 and r.json() == {'error': 'classroom attendance is recorded by the trainer or HR'}
    assert gita.post(f'/enrollments/{enr["id"]}/attendance', {'attended': True}).status_code == 404
    assert hari.post(f'/enrollments/{enr["id"]}/attendance', {'attended': True}).status_code == 403
    assert ram.post(f'/enrollments/{enr["id"]}/attendance', {'attended': True}).json() == {'ok': True}
    team = ram.get('/enrollments', {'scope': 'team'}).json()['enrollments']
    assert next(e for e in team if e['id'] == enr['id'])['status'] == 'completed'
    assert ram.get('/enrollments', {'scope': 'all'}).status_code == 403
    assert enr['id'] in ids(hr.get('/enrollments', {'scope': 'all', 'courseId': classroom}).json()['enrollments'])
    assert hr.get('/enrollments', {'scope': 'all', 'status': 'cancelled'}).json() == {'enrollments': []}
    # Cancelling an open enrolment (the learner, a manager or HR; not another colleague).
    again = ram.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.GITA]})
    assert again.json() == {'added': 1, 'skipped': 0}
    gid = next(e['id'] for e in gita.get('/enrollments').json()['enrollments'] if e['course_id'] == classroom)
    assert hari.post(f'/enrollments/{gid}/cancel').status_code == 404
    assert w.b_other_hr.post(f'/enrollments/{gid}/cancel').status_code == 404
    assert gita.post(f'/enrollments/{gid}/cancel').json() == {'ok': True}
    r = gita.post(f'/enrollments/{gid}/cancel')
    assert r.status_code == 409 and r.json() == {'error': 'this enrolment is cancelled'}


def test_sessions_respect_capacity(w):
    hr, gita, sita, other = w.b_hr, w.b_gita, w.b_sita, w.b_other_hr
    _, classroom = _catalogue(w)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    body = {'startsAt': start.isoformat(), 'endsAt': (start + timedelta(hours=1)).isoformat(), 'capacity': 1, 'venue': 'Room 2'}
    assert w.b_sita.post(f'/courses/{classroom}/sessions', body).status_code == 403
    s = hr.post(f'/courses/{classroom}/sessions', body)
    assert s.status_code == 201
    sid = s.json()['id']
    assert gita.post('/enrollments/self', {'courseId': classroom, 'sessionId': sid}).status_code == 201
    r = sita.post('/enrollments/self', {'courseId': classroom, 'sessionId': sid})
    assert r.status_code == 409 and r.json() == {'error': 'this session is full'}
    sessions = hr.get(f'/courses/{classroom}').json()['sessions']
    assert sessions[0]['id'] == sid and sessions[0]['taken'] == 1 and sessions[0]['venue'] == 'Room 2'
    assert sita.patch(f'/sessions/{sid}', {'capacity': 2}).status_code == 403
    assert other.patch(f'/sessions/{sid}', {'capacity': 2}).status_code == 404
    assert hr.patch(f'/sessions/{sid}', {'capacity': 2}).json() == {'ok': True}
    assert sita.post('/enrollments/self', {'courseId': classroom, 'sessionId': sid}).status_code == 201
    r = hr.post(f'/courses/{classroom}/sessions', {**body, 'endsAt': start.isoformat()})
    assert r.status_code == 400 and r.json() == {'error': 'the session ends before it starts'}


def test_certificates_uploaded_by_learner_seen_by_managers_and_hr_only(w):
    sita, ram, hari, other = w.b_sita, w.b_ram, w.b_hari, w.b_other_hr
    online, _ = _catalogue(w)
    eid = _passed(w, online)
    up = files.upload(w.sita, {'purpose': 'training_certificate', 'filename': 'cert.pdf', 'dataUrl': PDF})
    assert hari.post(f'/enrollments/{eid}/certificate', {'fileId': up['id']}).status_code == 404
    r = sita.post(f'/enrollments/{eid}/certificate', {'fileId': up['id'], 'expiresOn': plus_days(400)})
    assert r.status_code == 403 and r.json() == {'error': 'only HR can set the issue and expiry dates of a certificate'}
    assert sita.post(f'/enrollments/{eid}/certificate', {'fileId': up['id']}).json() == {'ok': True}
    f = ram.get(f'/enrollments/{eid}/certificate-file')
    assert f.status_code == 200 and f['Content-Type'] == 'application/pdf' and f.content.startswith(b'%PDF')
    assert f['Content-Disposition'].startswith('inline')
    assert ram.get(f'/enrollments/{eid}/certificate-file', {'download': '1'})['Content-Disposition'].startswith('attachment')
    assert hari.get(f'/enrollments/{eid}/certificate-file').status_code == 404
    assert other.get(f'/enrollments/{eid}/certificate-file').status_code == 404


def test_mandatory_assignment_cost_report_and_expiry(w):
    hr, ram = w.b_hr, w.b_ram
    online, classroom = _catalogue(w)
    eid = _passed(w, online)
    r = hr.post(f'/courses/{classroom}/assign-mandatory')
    assert r.status_code == 400 and r.json() == {'error': 'mark the course mandatory first'}
    assert ram.post(f'/courses/{online}/assign-mandatory').status_code == 403
    m = hr.post(f'/courses/{online}/assign-mandatory')
    assert m.status_code == 200 and m.json() == {'ok': True, 'added': 3}   # everyone except Sita, whose certificate is valid
    cost = hr.get('/reports/cost', {'from': plus_days(-5), 'to': plus_days(5)})
    assert set(cost.json()) == {'byCourse', 'byDepartment', 'total'} and float(cost.json()['total']) > 0
    assert set(cost.json()['byCourse'][0]) == {'id', 'code', 'title', 'seats', 'completed', 'cost', 'rating'}
    assert ram.get('/reports/cost', {'from': plus_days(-5), 'to': plus_days(5)}).status_code == 403
    assert hr.get('/reports/cost', {'from': plus_days(-5)}).status_code == 400
    assert w.b_other_hr.get('/reports/cost', {'from': plus_days(-5), 'to': plus_days(5)}).json()['total'] == '0.00'
    execute('UPDATE lrn_enrollment SET certificate_expires_on = %s WHERE id = %s', [plus_days(10), eid])
    assert eid in ids(hr.get('/expiring').json()['enrollments'])
    assert eid not in ids(hr.get('/expiring', {'days': 5}).json()['enrollments'])
    assert hr.get('/expiring', {'days': 999}).status_code == 400
    assert ram.get('/expiring').status_code == 403
    assert w.b_other_hr.get('/expiring').json() == {'enrollments': []}
    assert performance_reminders()['learning']['expiring'] == 1
    assert performance_reminders()['learning']['expiring'] == 0


def test_feature_switch(w):
    set_features_off(w.co, 'learning')
    r = w.b_hr.post('/courses', {'code': 'X', 'title': 'x'})
    assert r.status_code == 404 and r.json()['error'] == 'feature off'
    assert w.b_sita.get('/courses').status_code == 404
    assert w.b_other_hr.get('/courses').status_code == 200
    # Performance stays on.
    assert w.b_sita.get('/api/v1/performance/goals').status_code == 200


# ---------------------------------------------------------------- groups of companies

@pytest.fixture
def beta(world):  # noqa: F811
    return Beta(world)


def test_group_company_ids_are_not_found(world, beta):  # noqa: F811
    b = beta
    start = '2026-12-01T09:00:00+05:45'
    cases = [
        ('get', f'/courses/{b.course}', None), ('patch', f'/courses/{b.course}', {'title': 'x'}),
        ('put', f'/courses/{b.course}/questions', {'questions': []}),
        ('post', f'/courses/{b.course}/sessions', {'startsAt': start, 'endsAt': '2026-12-01T10:00:00+05:45'}),
        ('post', f'/courses/{b.course}/assign-mandatory', None), ('patch', f'/sessions/{b.session}', {'capacity': 3}),
        ('post', '/enrollments/self', {'courseId': b.course}),
        ('post', '/enrollments/assign', {'courseId': b.course, 'employeeIds': [b.report]}),
        ('post', f'/enrollments/{b.enrollment}/cancel', None), ('post', f'/enrollments/{b.enrollment}/attendance', {'attended': True}),
        ('post', f'/enrollments/{b.enrollment}/complete', None), ('get', f'/enrollments/{b.enrollment}/assessment', None),
        ('post', f'/enrollments/{b.enrollment}/attempts', {'answers': []}),
        ('post', f'/enrollments/{b.enrollment}/feedback', {'rating': 4}),
        ('post', f'/enrollments/{b.enrollment}/certificate', {'fileId': b.enrollment}),
        ('get', f'/enrollments/{b.enrollment}/certificate', None), ('get', f'/enrollments/{b.enrollment}/certificate-file', None),
    ]
    token_a, _ = world.session(world.admin_a)
    token_c, _ = world.session(world.admin_c)
    for who, token in (('group admin in Alpha', token_a), ('outsider', token_c)):
        br = LBrowser(None, token=token)
        for method, path, body in cases:
            r = br.get(path, body) if method == 'get' else getattr(br, method)(path, body)
            assert r.status_code == 404, (who, method, path, r.status_code, r.content[:200])
        assert b.course not in ids(br.get('/courses').json()['courses'])
        assert b.enrollment not in ids(br.get('/enrollments', {'scope': 'all'}).json()['enrollments'])
    token_b, _ = world.session(world.admin_b)
    owner = LBrowser(None, token=token_b)
    assert owner.get(f'/courses/{b.course}').status_code == 200
    assert b.enrollment in ids(owner.get('/enrollments', {'scope': 'all'}).json()['enrollments'])
