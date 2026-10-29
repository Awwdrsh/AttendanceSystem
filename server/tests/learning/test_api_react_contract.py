"""
The React learning pages' own requests against /api/v1/learning, field by field
(frontend/src/pages/performance/{LearningAdminPage,MyLearningPage}.jsx): the bodies they send (nulls for empty inputs,
cost as a fixed-2 string, +05:45 instants) and the JSON types they read, compared with what the TS routes at 65a9079
answered (numeric columns as strings, counts and smallint columns as numbers, dates as YYYY-MM-DD).
"""
from __future__ import annotations

import re

import pytest

from hajiri.files import service as files
from hajiri.workflow import service as wf
from tests.performance.world import build, plus_days, today

from .test_api_learning import PDF, LBrowser

ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ISO_INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')
NUMERIC = re.compile(r'^-?\d+(\.\d+)?$')


@pytest.fixture
def w(db):
    w = build()
    for k in ('hr', 'sita', 'ram', 'hari', 'gita', 'other_hr'):
        setattr(w, f'b_{k}', LBrowser(getattr(w, k)))
    return w


def numeric(v) -> bool:
    return isinstance(v, str) and bool(NUMERIC.match(v))


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def course_body(**kw):
    """CourseDrawer.save: every field, empty ones as null, cost Number(..).toFixed(2)."""
    b = {'code': 'SAFE', 'title': 'Fire safety', 'description': None, 'category': None, 'delivery': 'classroom', 'source': 'internal',
         'provider': None, 'durationHours': None, 'url': None, 'cost': '1500.00', 'mandatory': False, 'mandatoryFor': [],
         'validityMonths': None, 'passMark': None, 'active': True}
    b.update(kw)
    return b


def test_catalogue_and_course_drawer(w):
    hr, sita = w.b_hr, w.b_sita
    r = hr.post('/courses', course_body(durationHours=3.5, url='https://example.com/x', provider='Red Cross', validityMonths=12))
    assert r.status_code == 201, r.json()
    cid = r.json()['id']
    row = next(c for c in hr.get('/courses').json()['courses'] if c['id'] == cid)
    # `c.*` plus the counts: the drawer is filled from this row and posts it back.
    assert {'id', 'company_id', 'code', 'title', 'description', 'category', 'delivery', 'source', 'provider', 'duration_hours', 'url',
            'cost', 'mandatory', 'mandatory_for', 'validity_months', 'pass_mark', 'active', 'created_at', 'questions', 'completed',
            'enrolled', 'next_session'} <= set(row)
    assert numeric(row['cost']) and row['cost'] == '1500.00' and numeric(row['duration_hours'])
    assert is_int(row['validity_months']) and row['pass_mark'] is None and row['mandatory_for'] == []
    for f in ('questions', 'completed', 'enrolled'):
        assert row[f] == 0 and is_int(row[f])
    assert row['next_session'] is None
    # Re-saving from the row, as CourseDrawer does (durationHours Number(), cost toFixed(2), passMark || null).
    f = row
    again = course_body(code=f['code'], title=f['title'], durationHours=float(f['duration_hours']), url=f['url'], provider=f['provider'],
                        cost=f'{float(f["cost"]):.2f}', mandatory=True, mandatoryFor=['Engineer'], validityMonths=f['validity_months'],
                        passMark=f['pass_mark'], active=f['active'])
    assert hr.patch(f'/courses/{cid}', again).json() == {'ok': True}
    # url '' from an emptied input means none.
    assert hr.patch(f'/courses/{cid}', {'url': ''}).json() == {'ok': True}

    # Sessions: +05:45 instants, capacity null or a number.
    day = plus_days(7)
    s = hr.post(f'/courses/{cid}/sessions', {'startsAt': f'{day}T10:00:00+05:45', 'endsAt': f'{day}T13:00:00+05:45', 'venue': None,
                                             'trainer': None, 'capacity': 2})
    assert s.status_code == 201, s.json()
    d = hr.get(f'/courses/{cid}').json()
    assert set(d) == {'course', 'sessions', 'questions', 'questionCount'}
    sess = d['sessions'][0]
    assert {'id', 'company_id', 'course_id', 'starts_at', 'ends_at', 'venue', 'trainer', 'capacity', 'status', 'created_at', 'taken'} <= set(sess)
    assert is_int(sess['capacity']) and is_int(sess['taken']) and sess['taken'] == 0 and ISO_INSTANT.match(sess['starts_at'])
    assert sess['starts_at'] == f'{day}T04:15:00.000Z'
    nxt = next(c for c in hr.get('/courses').json()['courses'] if c['id'] == cid)['next_session']
    assert nxt == sess['starts_at']

    # QuestionsEditor: {prompt, options, correctIndex} from the HR view's correct_index.
    assert hr.put(f'/courses/{cid}/questions', {'questions': [{'prompt': 'Exit?', 'options': ['Lift', 'Stairs'], 'correctIndex': 1}]}).json() == {'ok': True}
    d = hr.get(f'/courses/{cid}').json()
    assert d['questions'] == [{'id': d['questions'][0]['id'], 'prompt': 'Exit?', 'options': ['Lift', 'Stairs'], 'correct_index': 1}]
    assert d['course']['pass_mark'] == 60 and d['questionCount'] == 1
    # Employees see no questions, only the count.
    sd = sita.get(f'/courses/{cid}').json()
    assert sd['questions'] == [] and sd['questionCount'] == 1
    # Session status buttons.
    assert hr.patch(f'/sessions/{sess["id"]}', {'status': 'cancelled'}).json() == {'ok': True}


def test_enrolment_flow_as_my_learning_reads_it(w):
    hr, sita, ram = w.b_hr, w.b_sita, w.b_ram
    online = hr.post('/courses', course_body(code='ON', title='Security basics', delivery='online', validityMonths=12,
                                             cost='800.00')).json()['id']
    hr.put(f'/courses/{online}/questions', {'questions': [{'prompt': 'Phishing?', 'options': ['Click', 'Report'], 'correctIndex': 1},
                                                          {'prompt': 'Password?', 'options': ['Long', 'Short'], 'correctIndex': 0}]})
    # EnrollModal: sessionId and reason only when set.
    eid = sita.post('/enrollments/self', {'courseId': online, 'reason': 'Want it'}).json()['id']
    e = sita.get('/enrollments', {'scope': 'mine'}).json()['enrollments'][0]
    assert {'id', 'course_id', 'code', 'title', 'delivery', 'mandatory', 'pass_mark', 'validity_months', 'session_id', 'starts_at',
            'ends_at', 'venue', 'employee_id', 'name_en', 'emp_code', 'source', 'status', 'attended', 'completed_at', 'score', 'passed',
            'feedback_rating', 'feedback_comment', 'certificate_file_id', 'certificate_issued_on', 'certificate_expires_on', 'cost',
            'reason', 'created_at', 'questions', 'attempts'} == set(e)
    assert e['status'] == 'pending_approval' and e['source'] == 'self' and numeric(e['cost'])
    assert e['questions'] == 2 and is_int(e['questions']) and e['attempts'] == 0 and e['score'] is None and e['pass_mark'] == 60
    wf.decide(w.ram, wf.instance_for_object('training_nomination', eid)['id'], 'approve')

    # QuizModal.
    a = sita.get(f'/enrollments/{eid}/assessment').json()
    assert set(a) == {'title', 'passMark', 'questions', 'attempts', 'attemptsLeft'}
    assert a['passMark'] == 60 and a['attemptsLeft'] == 5 and [set(q) for q in a['questions']] == [{'id', 'prompt', 'options'}] * 2
    bad = sita.post(f'/enrollments/{eid}/attempts', {'answers': [0, 0]})
    assert bad.status_code == 201 and bad.json() == {'score': 50, 'passed': False, 'correct': 1, 'total': 2, 'attemptsLeft': 4}
    a = sita.get(f'/enrollments/{eid}/assessment').json()
    assert numeric(a['attempts'][0]['score']) and a['attempts'][0]['passed'] is False and ISO_INSTANT.match(a['attempts'][0]['created_at'])
    good = sita.post(f'/enrollments/{eid}/attempts', {'answers': [1, 0]}).json()
    assert good == {'score': 100, 'passed': True, 'correct': 2, 'total': 2, 'attemptsLeft': 3}
    e = sita.get('/enrollments').json()['enrollments'][0]
    assert e['status'] == 'completed' and numeric(e['score']) and e['attempts'] == 2
    assert ISO_DAY.match(e['certificate_issued_on']) and e['certificate_issued_on'] == today() and ISO_DAY.match(e['certificate_expires_on'])

    # RateModal: comment undefined when empty.
    assert sita.post(f'/enrollments/{eid}/feedback', {'rating': 4}).json() == {'ok': True}
    assert sita.get('/enrollments').json()['enrollments'][0]['feedback_rating'] == 4
    # CertificateModal: the uploaded file's id only.
    f = files.upload(w.sita, {'purpose': 'training_certificate', 'filename': 'cert.pdf', 'dataUrl': PDF})
    assert sita.post(f'/enrollments/{eid}/certificate', {'fileId': f['id']}).json() == {'ok': True}
    assert sita.get('/enrollments').json()['enrollments'][0]['certificate_file_id'] == f['id']
    assert sita.get(f'/enrollments/{eid}/certificate-file').status_code == 200
    cert = sita.get(f'/enrollments/{eid}/certificate')
    assert cert.status_code == 200 and cert['Content-Type'].startswith('text/html')

    # Manager's team view (LearningAdminPage enrolments tab) and assignment.
    assert [x['id'] for x in ram.get('/enrollments', {'scope': 'team'}).json()['enrollments']] == [eid]
    classroom = hr.post('/courses', course_body(code='CL', title='Excel')).json()['id']
    out = ram.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.SITA, w.HARI]})
    assert out.status_code == 201 and out.json() == {'added': 2, 'skipped': 0}
    again = ram.post('/enrollments/assign', {'courseId': classroom, 'employeeIds': [w.SITA]}).json()
    assert again == {'added': 0, 'skipped': 1}
    rows = ram.get('/enrollments', {'scope': 'team', 'courseId': classroom, 'status': 'enrolled'}).json()['enrollments']
    assert len(rows) == 2 and all(r['source'] == 'assigned' for r in rows)
    hari_row = next(r for r in rows if r['employee_id'] == w.HARI)
    assert ram.post(f'/enrollments/{hari_row["id"]}/attendance', {'attended': False}).json() == {'ok': True}
    sita_row = next(r for r in rows if r['employee_id'] == w.SITA)
    assert ram.post(f'/enrollments/{sita_row["id"]}/attendance', {'attended': True}).json() == {'ok': True}
    assert ram.get('/enrollments', {'scope': 'team', 'courseId': classroom, 'status': 'completed'}).json()['enrollments'][0]['attended'] is True
    # HR's "all" scope, mandatory assignment.
    assert len(hr.get('/enrollments', {'scope': 'all'}).json()['enrollments']) == 3
    hr.patch(f'/courses/{online}', {'mandatory': True})
    m = hr.post(f'/courses/{online}/assign-mandatory')
    assert m.status_code == 200 and m.json()['ok'] is True and is_int(m.json()['added'])


def test_reports_as_the_admin_page_reads_them(w):
    hr, sita = w.b_hr, w.b_sita
    cid = hr.post('/courses', course_body(code='ON', title='Security', delivery='online', validityMonths=1, cost='1200.50')).json()['id']
    out = hr.post('/enrollments/assign', {'courseId': cid, 'employeeIds': [w.SITA, w.HARI]}).json()
    assert out == {'added': 2, 'skipped': 0}
    eid = next(e['id'] for e in sita.get('/enrollments').json()['enrollments'])
    assert sita.post(f'/enrollments/{eid}/complete').json() == {'ok': True}
    assert sita.post(f'/enrollments/{eid}/feedback', {'rating': 5, 'comment': 'Good'}).json() == {'ok': True}

    # Expiring certificates (days as a number in the query string).
    ex = hr.get('/expiring', {'days': 60}).json()
    assert set(ex) == {'enrollments'} and [x['id'] for x in ex['enrollments']] == [eid]
    assert ISO_DAY.match(ex['enrollments'][0]['certificate_expires_on'])

    # Cost report: seats/completed numbers, cost/total strings with two decimals, rating a string or null.
    rep = hr.get('/reports/cost', {'from': plus_days(-1), 'to': plus_days(1)}).json()
    assert set(rep) == {'byCourse', 'byDepartment', 'total'}
    c = rep['byCourse'][0]
    assert set(c) == {'id', 'code', 'title', 'seats', 'completed', 'cost', 'rating'}
    assert c == {'id': cid, 'code': 'ON', 'title': 'Security', 'seats': 2, 'completed': 1, 'cost': '2401.00', 'rating': '5.00'}
    assert rep['byDepartment'] == [{'department': 'No department', 'seats': 2, 'cost': '2401.00'}]
    assert rep['total'] == '2401.00'
    other = hr.post('/courses', course_body(code='X', title='Unrated', cost='0.00')).json()['id']
    hr.post('/enrollments/assign', {'courseId': other, 'employeeIds': [w.GITA]})
    rep = hr.get('/reports/cost', {'from': plus_days(-1), 'to': plus_days(1)}).json()
    assert next(x for x in rep['byCourse'] if x['id'] == other)['rating'] is None
    assert hr.get('/reports/cost', {'from': plus_days(1), 'to': plus_days(-1)}).status_code == 400
    assert sita.get('/reports/cost', {'from': plus_days(-1), 'to': plus_days(1)}).status_code == 403
