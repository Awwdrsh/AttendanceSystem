"""
HTTP-level port of apps/api/test/hiring.test.ts: every /api/v1/hiring route the React app calls (src/api/hiring.js)
and the public careers API, with the TS status codes and JSON shapes, plus company isolation (another company's ids
are 404 for an outsider and for the group admin working in a sister company; lists stay in the active company).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.careers import services as careers
from hajiri.hiring import services as hiring
from tests.support.sql import one, rows
from tests.web.factories import set_features_off, sign_in

from .test_isolation import _beta
from .world import PUB, QUESTIONS, World, future

pytestmark = pytest.mark.django_db

H = '/api/v1/hiring'


class Api:
    """Bearer requests as one person (no cookie, so no CSRF), or anonymous with `Api(None)`."""

    def __init__(self, user=None):
        self.c = Client()
        self.h = {}
        if user is not None:
            self.h = {'HTTP_AUTHORIZATION': f'Bearer {sign_in(Client(), user)}'}

    def get(self, path, query=None):
        return self.c.get(path, query or {}, **self.h)

    def _send(self, method, path, body):
        return getattr(self.c, method)(path, json.dumps(body if body is not None else {}), content_type='application/json', **self.h)

    def post(self, path, body=None):
        return self._send('post', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)


@pytest.fixture(autouse=True)
def _reset_limiter():
    careers.apply_limiter.reset()
    yield
    careers.apply_limiter.reset()


def test_hiring_story_over_http():
    """The whole TS story, one `it` after another, through the JSON API only."""
    w = World()
    hr, hr2, ram, mgr2, emp, other_hr = (Api(u) for u in (w.hr, w.hr2, w.ram, w.mgr2, w.emp, w.other_hr))

    # ---- a manager raises a hiring request and HR approves it
    assert emp.post(f'{H}/requisitions', {'title': 'Engineer', 'justification': 'We need more people.'}).status_code == 403
    short = ram.post(f'{H}/requisitions', {'title': 'Engineer', 'justification': 'short'})
    assert short.status_code == 400 and short.json()['error'] == 'validation'
    r = ram.post(f'{H}/requisitions', {'title': 'Backend engineer', 'designation': 'Software Engineer', 'orgUnitId': w.unit,
                                       'headcount': 1, 'salaryMin': '80000', 'salaryMax': '120000',
                                       'justification': 'New payments project starting next quarter.'})
    assert r.status_code == 201, r.json()
    req_id = r.json()['id']
    mine = ram.get(f'{H}/requisitions').json()['requisitions']
    assert len(mine) == 1
    assert mine[0]['id'] == req_id and mine[0]['department'] == 'Engineering' and mine[0]['jobs'] == 0
    assert mine[0]['status'] == 'pending' and mine[0]['salary_min'] == '80000.00'
    assert mgr2.get(f'{H}/requisitions').json() == {'requisitions': []}
    inbox = hr.get('/api/v1/workflow/inbox')
    assert inbox.status_code == 200
    item = next(i for i in inbox.json()['items'] if i['objectId'] == req_id)
    assert item['summary']['title'] == 'Backend engineer' and item['summary']['headcount'] == 1
    assert hr.post(f"/api/v1/workflow/{item['instanceId']}/approve", {}).status_code == 200
    assert one('SELECT status FROM hiring_requisition WHERE id = %s', [req_id])['status'] == 'approved'

    # ---- HR creates a job from the request; it stays private until published
    assert ram.post(f'{H}/jobs', {'title': 'X'}).status_code == 403
    bad_q = hr.post(f'{H}/jobs', {'title': 'Backend engineer',
                                  'questions': [{'id': 'q1', 'kind': 'text', 'label': 'Anything?', 'knockout': {'equals': 'yes'}}]})
    assert bad_q.status_code == 400 and bad_q.json() == {'error': '"Anything?": text answers cannot be scored'}
    assert hr.post(f'{H}/jobs', {'title': 'Backend engineer', 'stages': ['Screening', 'Hired']}).status_code == 400
    j = hr.post(f'{H}/jobs', {
        'requisitionId': req_id, 'title': 'Backend engineer', 'description': 'Build and run the payments services that power our clients.',
        'requirements': 'TypeScript, PostgreSQL', 'category': 'Engineering', 'location': 'Kathmandu', 'workMode': 'hybrid',
        'experienceMin': 2, 'experienceMax': 5, 'salaryMin': '80000', 'salaryMax': '120000', 'showSalary': False,
        'deadline': future(30), 'orgUnitId': w.unit, 'hiringManagerId': str(w.ram_emp.id), 'questions': QUESTIONS})
    assert j.status_code == 201, j.json()
    job_id = w.job_id = j.json()['id']
    assert w.public.get(f'{PUB}/jobs/{job_id}').status_code == 404
    pub = hr.post(f'{H}/jobs/{job_id}/publish')
    assert pub.status_code == 200 and pub.json() == {'ok': True}
    assert hr.post(f'{H}/jobs/{job_id}/publish').json() == {'ok': True}   # already open: nothing to do
    got = hr.get(f'{H}/jobs/{job_id}')
    assert got.status_code == 200
    assert got.json()['stageCounts'] == {}
    gj = got.json()['job']
    assert gj['status'] == 'open' and gj['department'] == 'Engineering' and gj['hiring_manager_name'] == 'Ram Thapa'
    assert gj['stages'] == ['Applied', 'Screening', 'Interview', 'Offer', 'Hired'] and gj['requisition_id'] == req_id
    assert (gj['experience_min'], gj['experience_max'], gj['salary_min']) == ('2.0', '5.0', '80000.00')   # numerics as strings
    listing = hr.get(f'{H}/jobs').json()
    assert listing['companyCode'] == 'demo' and [x['id'] for x in listing['jobs']] == [job_id]
    assert hr.get(f'{H}/jobs', {'status': 'draft'}).json()['jobs'] == []
    assert hr.get(f'{H}/jobs', {'status': 'nope'}).status_code == 400
    bad_id = hr.get(f'{H}/jobs/not-a-uuid')
    assert bad_id.status_code == 400 and bad_id.json() == {'error': 'validation', 'issues': [{'path': '', 'message': 'Invalid UUID'}]}

    # ---- the public careers page lists open jobs without private fields (the TS public test, over HTTP)
    assert w.public.get(f'{PUB}/jobs').status_code == 404
    lst = w.public.get(f'{PUB}/jobs?company=demo').json()
    assert lst['company']['name'] == 'Demo Company' and len(lst['jobs']) == 1
    assert lst['jobs'][0]['salary_min'] is None and 'company_id' not in lst['jobs'][0]
    one_job = w.public.get(f'{PUB}/jobs/{job_id}').json()
    assert one_job['job']['questions'][0] == {'id': 'q1', 'kind': 'yes_no', 'label': QUESTIONS[0]['label'], 'required': True}

    # ---- applications from the careers page
    assert w.apply().status_code == 201
    careers.apply_limiter.reset()
    assert w.apply(name='Bikash Rai', email='bikash@example.com', experienceYears=1, answers={'q1': 'no', 'q2': 0}).status_code == 201
    app_id = str(one("""SELECT a.id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                         WHERE c.email = 'anita@example.com'""")['id'])
    other_app = str(one("""SELECT a.id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                           WHERE c.email = 'bikash@example.com'""")['id'])

    # ---- scopes the pipeline: HR and the department manager yes; others no
    board = hr.get(f'{H}/jobs/{job_id}/applications')
    assert board.status_code == 200
    b = board.json()
    assert set(b) == {'applications', 'stages'}
    assert b['applications'][0]['name'] == 'Anita Gurung' and b['applications'][1]['knocked_out'] is True
    assert b['applications'][0]['has_cv'] is True and b['applications'][0]['other_applications'] == 0
    assert b['stages'] == ['Applied', 'Screening', 'Interview', 'Offer', 'Hired']
    assert ram.get(f'{H}/jobs/{job_id}/applications').status_code == 200
    assert hr.get(f'{H}/jobs/{job_id}/applications', {'include': 'bogus'}).status_code == 400
    assert len(ram.get(f'{H}/jobs').json()['jobs']) == 1
    assert len(mgr2.get(f'{H}/jobs').json()['jobs']) == 0
    assert mgr2.get(f'{H}/jobs/{job_id}/applications').status_code == 404
    assert mgr2.get(f'{H}/applications/{app_id}').status_code == 404
    denied = emp.get(f'{H}/jobs')
    assert denied.status_code == 403 and denied.json() == {'error': 'requires role manager or hr or recruiter'}
    look = ram.get(f'{H}/lookups').json()
    assert set(look) == {'users', 'orgUnits', 'branches', 'shifts'}
    assert [u['name_en'] for u in look['orgUnits']] == ['Engineering']
    assert all('email' not in u for u in look['users'])
    assert emp.get(f'{H}/lookups').status_code == 403
    assert other_hr.get(f'{H}/applications/{app_id}').status_code == 404
    assert other_hr.get(f'{H}/jobs/{job_id}').status_code == 404
    assert other_hr.get(f'{H}/jobs/{job_id}').json() == {'error': 'job not found'}
    cv = ram.get(f'{H}/applications/{app_id}/cv')
    assert cv.status_code == 200 and cv['Content-Type'] == 'application/pdf'
    assert 'sandbox' in cv['Content-Security-Policy'] and cv['Content-Disposition'].startswith('inline')
    assert ram.get(f'{H}/applications/{app_id}/cv', {'download': '1'})['Content-Disposition'].startswith('attachment')

    # ---- moves, rates, tags and notes an application with a timeline
    assert ram.post(f'{H}/applications/{app_id}/move', {'stage': 'Nowhere'}).status_code == 400
    assert ram.post(f'{H}/applications/{app_id}/move', {'stage': 'Hired'}).status_code == 400
    assert ram.post(f'{H}/applications/{app_id}/move', {'stage': 'Screening'}).json() == {'ok': True}
    assert ram.patch(f'{H}/applications/{app_id}', {'rating': 4, 'tags': ['strong', 'payments', 'strong']}).json() == {'ok': True}
    assert ram.patch(f'{H}/applications/{app_id}', {'rating': 9}).status_code == 400
    assert ram.patch(f'{H}/applications/{app_id}', {'ownerUserId': w.other_hr.id}).status_code == 400
    note = hr.post(f'{H}/applications/{app_id}/notes', {'body': 'Great phone screen.'})
    assert note.status_code == 201 and note.json() == {'ok': True}
    d = hr.get(f'{H}/applications/{app_id}').json()
    assert set(d) == {'application', 'access', 'otherApplications', 'events', 'interviews', 'scorecards', 'offers', 'job'}
    a = d['application']
    assert (a['stage'], a['rating'], a['tags']) == ('Screening', 4, ['strong', 'payments'])
    assert 'cv_file_id' not in a and a['has_cv'] is True and d['access'] == 'full'
    assert {'applied', 'stage', 'rating', 'note', 'email'} <= {e['kind'] for e in d['events']}
    assert len(a['score_detail']) > 0
    assert d['job'] == {'id': job_id, 'title': 'Backend engineer', 'stages': b['stages'], 'questions': QUESTIONS, 'status': 'open',
                        'requisitionId': req_id}

    # ---- schedules an interview: panel notified, candidate invited, stage moves on
    past = ram.post(f'{H}/applications/{app_id}/interviews', {'date': '2020-01-01', 'time': '10:00', 'mode': 'video', 'panel': [w.ram.id]})
    assert past.status_code == 400 and past.json() == {'error': 'the interview time is in the past'}
    foreign = ram.post(f'{H}/applications/{app_id}/interviews',
                       {'date': future(3), 'time': '11:00', 'mode': 'video', 'panel': ['00000000-0000-7000-8000-00000000ffff']})
    assert foreign.status_code == 400
    iv = ram.post(f'{H}/applications/{app_id}/interviews', {'date': future(3), 'time': '11:00', 'mode': 'video',
                                                             'location': 'https://meet.example.com/x', 'panel': [w.ram.id, w.mgr2.id]})
    assert iv.status_code == 201, iv.json()
    interview_id = iv.json()['id']
    assert one('SELECT stage FROM hiring_application WHERE id = %s', [app_id])['stage'] == 'Interview'
    assert '11:00' in one("SELECT body FROM notification_outbox WHERE template = 'hiring.interview_invite'")['body']
    mine_iv = mgr2.get(f'{H}/interviews', {'scope': 'mine'}).json()['interviews']
    assert len(mine_iv) == 1
    assert mine_iv[0]['id'] == interview_id and mine_iv[0]['candidate'] == 'Anita Gurung' and mine_iv[0]['on_panel'] is True
    assert mine_iv[0]['my_scorecard'] is False and mine_iv[0]['scorecards'] == 0 and mine_iv[0]['job_id'] == job_id
    assert emp.get(f'{H}/interviews', {'scope': 'all'}).status_code == 403
    assert emp.get(f'{H}/interviews').json() == {'interviews': []}
    assert len(hr.get(f'{H}/interviews', {'scope': 'all', 'from': future(3), 'to': future(3)}).json()['interviews']) == 1
    assert hr.get(f'{H}/interviews', {'scope': 'all', 'from': future(4)}).json()['interviews'] == []

    # ---- panel members get a read-only view and submit their own scorecard
    view = mgr2.get(f'{H}/applications/{app_id}')
    assert view.status_code == 200 and view.json()['access'] == 'panel' and view.json()['events'] == []
    assert mgr2.get(f'{H}/applications/{app_id}/cv').status_code == 200
    assert mgr2.post(f'{H}/applications/{app_id}/move', {'stage': 'Offer'}).status_code == 404
    assert hr.post(f'{H}/interviews/{interview_id}/scorecard', {'ratings': {'Skills': 4}, 'recommendation': 'yes'}).status_code == 404
    assert mgr2.post(f'{H}/interviews/{interview_id}/scorecard', {'ratings': {'Skills': 9}, 'recommendation': 'yes'}).status_code == 400
    sc = mgr2.post(f'{H}/interviews/{interview_id}/scorecard',
                   {'ratings': {'Skills': 4, 'Communication': 5}, 'recommendation': 'strong_yes', 'notes': 'Clear thinker.'})
    assert sc.status_code == 201 and sc.json() == {'ok': True}
    assert ram.post(f'{H}/interviews/{interview_id}/scorecard', {'ratings': {'Skills': 3}, 'recommendation': 'yes'}).status_code == 201
    assert len(mgr2.get(f'{H}/applications/{app_id}').json()['scorecards']) == 1
    assert len(hr.get(f'{H}/applications/{app_id}').json()['scorecards']) == 2
    up = ram.patch(f'{H}/interviews/{interview_id}', {'location': 'Room 2'})
    assert up.status_code == 200 and up.json() == {'ok': True}
    assert one('SELECT location FROM hiring_interview WHERE id = %s', [interview_id])['location'] == 'Room 2'
    assert ram.patch(f'{H}/interviews/{interview_id}', {'status': 'nope'}).status_code == 400
    assert other_hr.patch(f'{H}/interviews/{interview_id}', {'location': 'x'}).status_code == 404

    # ---- an offer needs approval by another HR user, then the candidate accepts
    offer = {'designation': 'Software Engineer', 'salary': '100000', 'joinDate': future(20)}
    assert ram.post(f'{H}/applications/{app_id}/offers', offer).status_code == 403
    assert hr.post(f'{H}/applications/{app_id}/offers', {**offer, 'joinDate': '2020-01-01'}).status_code == 400
    o = hr.post(f'{H}/applications/{app_id}/offers', {**offer, 'expiresOn': future(7), 'notes': 'Includes a laptop.'})
    assert o.status_code == 201, o.json()
    offer_id = o.json()['id']
    assert hr.post(f'{H}/applications/{app_id}/offers', {'designation': 'Engineer', 'salary': '1', 'joinDate': future(20)}).status_code == 409
    draft = hr.get(f'{H}/offers/{offer_id}/letter')
    assert draft.status_code == 200 and draft['Content-Type'].startswith('text/html') and 'DRAFT' in draft.content.decode()
    early = hr.post(f'{H}/offers/{offer_id}/respond', {'response': 'accepted'})
    assert early.status_code == 409 and early.json() == {'error': 'the offer must be approved before the candidate can respond'}
    assert hr.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E900'}).status_code == 409
    item = next(i for i in hr2.get('/api/v1/workflow/inbox').json()['items'] if i['objectId'] == offer_id)
    assert (item['summary']['candidate'], item['summary']['designation']) == ('Anita Gurung', 'Software Engineer')
    assert hr2.post(f"/api/v1/workflow/{item['instanceId']}/approve", {}).status_code == 200
    letter = hr.get(f'{H}/offers/{offer_id}/letter').content.decode()
    assert 'Anita Gurung' in letter and 'class="draft"' not in letter and '1,00,000.00' in letter
    assert other_hr.get(f'{H}/offers/{offer_id}/letter').status_code == 404
    assert emp.get(f'{H}/offers/{offer_id}/letter').status_code == 403
    assert hr.post(f'{H}/offers/{offer_id}/respond', {'response': 'maybe'}).status_code == 400
    assert hr.post(f'{H}/offers/{offer_id}/respond', {'response': 'accepted'}).json() == {'ok': True}
    offers = hr.get(f'{H}/applications/{app_id}').json()['offers']
    assert offers[0]['id'] == offer_id and offers[0]['status'] == 'accepted' and offers[0]['salary'] == '100000.00'

    # ---- hires the candidate as an employee and fills the request
    assert ram.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E900'}).status_code == 403
    assert hr.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E001'}).status_code == 409   # code in use
    h = hr.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E900', 'shiftId': w.shift_id})
    assert h.status_code == 201, h.json()
    emp_id = h.json()['employeeId']
    e = one('SELECT name_en, email, designation, join_date FROM employee WHERE id = %s', [emp_id])
    assert (e['name_en'], e['email'], e['designation'], e['join_date'].isoformat()) == \
        ('Anita Gurung', 'anita@example.com', 'Software Engineer', future(20))
    r_ = one('SELECT status, filled FROM hiring_requisition WHERE id = %s', [req_id])
    assert (r_['status'], r_['filled']) == ('filled', 1)
    assert hr.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E901'}).status_code == 409
    assert hr.post(f'{H}/offers/{offer_id}/withdraw').status_code == 409   # already hired

    # ---- rejects with a courteous e-mail and reports the funnel, time to hire and sources
    rj = ram.post(f'{H}/applications/{other_app}/reject', {'reason': 'No work permit'})
    assert rj.status_code == 200 and rj.json() == {'ok': True, 'emailed': True}
    assert ram.post(f'{H}/applications/{other_app}/reject', {'reason': 'Again'}).status_code == 409
    rep = hr.get(f'{H}/reports')
    assert rep.status_code == 200
    rb = rep.json()
    assert set(rb) == {'totals', 'funnel', 'sources', 'timeToHire'}
    assert rb['totals'] == {'applications': 2, 'active': 0, 'hired': 1, 'rejected': 1}
    assert [s['reached'] for s in rb['funnel'][0]['stages']] == [2, 1, 1, 1, 1]
    assert rb['funnel'][0]['jobId'] == job_id and rb['funnel'][0]['total'] == 2 and rb['funnel'][0]['rejected'] == 1
    assert rb['timeToHire']['hires'] == 1
    assert rb['sources'] == [{'source': 'careers', 'applications': 2, 'hired': 1}]
    assert ram.get(f'{H}/reports').status_code == 403
    assert hr.get(f'{H}/reports', {'from': 'yesterday'}).status_code == 400

    # ---- exports a candidate for HR, purges after retention, reopen refused
    ex = hr.get(f'{H}/applications/{other_app}/export')
    assert ex.status_code == 200 and ex.json()['candidate']['email'] == 'bikash@example.com'
    assert set(ex.json()) == {'exportedAt', 'candidate', 'applications', 'events', 'interviews', 'offers'}
    assert ex['Content-Disposition'].startswith('attachment; filename="candidate-')
    assert ram.get(f'{H}/applications/{other_app}/export').status_code == 403
    assert hiring.purge_rejected_candidates(datetime.now(timezone.utc) + timedelta(days=200)) == 1
    reopen = hr.post(f'{H}/applications/{other_app}/reopen')
    assert reopen.status_code == 409 and reopen.json() == {'error': "this candidate's data has been removed"}

    # ---- HR can switch the careers page off and set retention
    assert hr.get(f'{H}/policy').json() == {'policy': {'retentionDays': 180, 'careersEnabled': True}}
    assert hr.put(f'{H}/policy', {'retentionDays': 5, 'careersEnabled': True}).status_code == 400
    assert ram.put(f'{H}/policy', {'retentionDays': 90, 'careersEnabled': False}).status_code == 403
    pol = hr.put(f'{H}/policy', {'retentionDays': 90, 'careersEnabled': False})
    assert pol.status_code == 200 and pol.json() == {'policy': {'retentionDays': 90, 'careersEnabled': False}}
    assert ram.get(f'{H}/policy').json()['policy']['retentionDays'] == 90
    assert emp.get(f'{H}/policy').status_code == 403
    assert w.public.get(f'{PUB}/jobs?company=DEMO').status_code == 404
    assert w.public.get(f'{PUB}/jobs/{job_id}').status_code == 404
    hr.put(f'{H}/policy', {'retentionDays': 90, 'careersEnabled': True})
    assert hr.post(f'{H}/jobs/{job_id}/close').json() == {'ok': True}
    assert one('SELECT status FROM hiring_job WHERE id = %s', [job_id])['status'] == 'closed'
    assert w.apply(email='after@example.com').status_code == 404


def test_requisition_cancel_job_patch_and_manual_candidate():
    w = World()
    hr, ram, mgr2 = Api(w.hr), Api(w.ram), Api(w.mgr2)
    rid = ram.post(f'{H}/requisitions', {'title': 'Tester', 'justification': 'We ship too many bugs today.'}).json()['id']
    assert mgr2.post(f'{H}/requisitions/{rid}/cancel').status_code == 404   # not theirs
    c = ram.post(f'{H}/requisitions/{rid}/cancel')
    assert c.status_code == 200 and c.json() == {'ok': True}
    again = ram.post(f'{H}/requisitions/{rid}/cancel')
    assert again.status_code == 409 and again.json() == {'error': 'this request is already cancelled'}
    unapproved = ram.post(f'{H}/requisitions', {'title': 'Tester', 'justification': 'We ship too many bugs today.'}).json()['id']
    j = hr.post(f'{H}/jobs', {'requisitionId': unapproved, 'title': 'Tester'})
    assert j.status_code == 400 and j.json() == {'error': 'only an approved hiring request can be turned into a job'}

    job = hr.post(f'{H}/jobs', {'title': 'Tester', 'description': 'short'}).json()['id']
    nope = hr.post(f'{H}/jobs/{job}/publish')
    assert nope.status_code == 400 and nope.json() == {'error': 'add a job description (at least 20 characters) before publishing'}
    assert ram.patch(f'{H}/jobs/{job}', {'title': 'QA'}).status_code == 403
    p = hr.patch(f'{H}/jobs/{job}', {'title': 'QA engineer', 'description': 'Find the bugs before our customers ever do.'})
    assert p.status_code == 200 and p.json() == {'ok': True}
    row = one('SELECT title, work_mode, openings FROM hiring_job WHERE id = %s', [job])
    assert (row['title'], row['work_mode'], row['openings']) == ('QA engineer', 'onsite', 1)   # defaults not re-applied
    assert hr.patch(f'{H}/jobs/{job}', {'requisitionId': None}).json() == {'error': 'the hiring request of a job cannot be changed'}
    assert hr.patch(f'{H}/jobs/{job}', {'experienceMin': 5, 'experienceMax': 2}).status_code == 400

    add = hr.post(f'{H}/jobs/{job}/applications', {'name': 'Mina Lama', 'email': 'mina@example.com', 'source': 'referral'})
    assert add.status_code == 201, add.json()
    aid = add.json()['id']
    dup = hr.post(f'{H}/jobs/{job}/applications', {'name': 'Mina Lama', 'email': 'MINA@example.com'})
    assert dup.status_code == 409 and dup.json() == {'error': 'this candidate has already applied for this job'}
    assert hr.post(f'{H}/jobs/{job}/applications', {'name': 'X', 'email': 'bad'}).status_code == 400
    assert ram.post(f'{H}/jobs/{job}/applications', {'name': 'Mina Two', 'email': 'm2@example.com'}).status_code == 403
    d = hr.get(f'{H}/applications/{aid}').json()['application']
    assert d['source'] == 'referral' and d['owner_user_id'] == w.hr.id and d['has_cv'] is False
    assert hr.get(f'{H}/applications/{aid}/cv').json() == {'error': 'no CV on file'}
    assert hr.post(f'{H}/applications/{aid}/reject', {'reason': 'Not now', 'notifyCandidate': False}).json() == {'ok': True, 'emailed': False}
    assert hr.post(f'{H}/applications/{aid}/reopen').json() == {'ok': True}
    assert hr.post(f'{H}/applications/{aid}/reopen').status_code == 409
    assert hr.get(f'{H}/jobs/{job}/applications', {'include': 'all'}).json()['applications'][0]['id'] == aid

    offer = hr.post(f'{H}/applications/{aid}/offers', {'designation': 'QA', 'salary': '50000', 'joinDate': future(10)}).json()['id']
    wd = hr.post(f'{H}/offers/{offer}/withdraw')
    assert wd.status_code == 200 and wd.json() == {'ok': True}
    assert hr.post(f'{H}/offers/{offer}/withdraw').json() == {'error': 'this offer is withdrawn'}
    assert ram.post(f'{H}/offers/{offer}/withdraw').status_code == 403


def test_bad_ids_auth_and_feature_switch():
    w = World()
    hr = Api(w.hr)
    bad = hr.get(f'{H}/applications/not-a-uuid')
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert hr.post(f'{H}/offers/00000000-0000-7000-8000-00000000ffff/respond', {'response': 'accepted'}).json() == {'error': 'offer not found'}
    assert Api(None).get(f'{H}/jobs').status_code == 401
    # The React app's cookie session works too.
    c = Client()
    sign_in(c, w.hr)
    assert c.get(f'{H}/jobs').status_code == 200
    set_features_off(w.co, 'hiring')
    off = hr.get(f'{H}/jobs')
    assert off.status_code == 404 and off.json()['error'] == 'feature off'


# ---------------------------------------------------------------- groups of companies

def test_company_isolation(world):
    """Beta's hiring records are 404 for an outsider and for the group admin working in Alpha; Beta's admin reaches them."""
    beta = _beta(world)
    owner, outsider = Api(world.admin_b), Api(world.admin_c)
    in_alpha = Api(world.admin_a)   # group admin, active company Alpha
    paths = [
        ('get', f'{H}/jobs/{beta.job}'), ('get', f'{H}/jobs/{beta.job}/applications'), ('patch', f'{H}/jobs/{beta.job}'),
        ('post', f'{H}/jobs/{beta.job}/publish'), ('post', f'{H}/jobs/{beta.job}/applications'),
        ('get', f'{H}/applications/{beta.app}'), ('get', f'{H}/applications/{beta.app}/cv'),
        ('get', f'{H}/applications/{beta.app}/export'), ('patch', f'{H}/applications/{beta.app}'),
        ('post', f'{H}/applications/{beta.app}/notes'), ('post', f'{H}/applications/{beta.app}/move'),
        ('patch', f'{H}/interviews/{beta.interview}'), ('get', f'{H}/offers/{beta.offer}/letter'),
        ('post', f'{H}/offers/{beta.offer}/withdraw'), ('post', f'{H}/requisitions/{beta.req}/cancel'),
    ]
    bodies = {'notes': {'body': 'hello'}, 'move': {'stage': 'Screening'}, 'applications': {'name': 'Eve Spy', 'email': 'eve@x.test'}}
    for method, path in paths:
        body = next((v for k, v in bodies.items() if path.endswith(k)), {})
        for who in (outsider, in_alpha):
            r = who.get(path) if method == 'get' else getattr(who, method)(path, body)
            assert r.status_code == 404, (path, r.status_code, r.content)
    assert owner.get(f'{H}/jobs/{beta.job}').status_code == 200
    assert owner.get(f'{H}/applications/{beta.app}').status_code == 200
    assert owner.get(f'{H}/offers/{beta.offer}/letter').status_code == 200
    # Lists stay in the active company.
    assert in_alpha.get(f'{H}/jobs').json()['jobs'] == []
    assert in_alpha.get(f'{H}/requisitions').json()['requisitions'] == []
    assert in_alpha.get(f'{H}/interviews', {'scope': 'all'}).json()['interviews'] == []
    assert in_alpha.get(f'{H}/reports').json()['totals']['applications'] == 0
    assert [j['id'] for j in owner.get(f'{H}/jobs').json()['jobs']] == [beta.job]
    # Ids of another company in a body are refused.
    alpha_unit = str(one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'A1', 'Alpha dept') RETURNING id",
                         [world.alpha.id])['id'])
    r = owner.post(f'{H}/jobs', {'title': 'Driver', 'orgUnitId': alpha_unit})
    assert r.status_code == 400 and r.json() == {'error': 'department does not exist in your company'}
    assert rows('SELECT 1 FROM hiring_job WHERE title = %s', ['Driver']) == []
