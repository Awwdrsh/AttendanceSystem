"""
Contract review of /api/v1/hiring and /api/v1/public/careers against the React pages (frontend/src/pages/hiring/*,
api/hiring.js): the exact bodies the pages send, through the cookie session they use (no CSRF token, the browser's own
Origin on writes), the exact keys of every row as the TS routes' SELECTs returned them (routes.ts at 65a9079), and the
value formats the pages read (numerics as the strings pg returned, timestamps as JSON.stringify ISO strings, calendar
dates as YYYY-MM-DD, ids as strings, nulls).
"""
from __future__ import annotations

import re

import pytest
from django.test import Client

from hajiri.careers import services as careers
from hajiri.workflow import service as workflow
from tests.web.factories import sign_in

from .world import PDF, PUB, QUESTIONS, World, future

pytestmark = pytest.mark.django_db

H = '/api/v1/hiring'
ISO_MS = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# The SELECT lists of routes.ts, key for key.
REQUISITION_KEYS = {
    'id', 'company_id', 'raised_by', 'raised_by_employee_id', 'title', 'designation', 'org_unit_id', 'branch_id', 'headcount',
    'filled', 'salary_min', 'salary_max', 'kind', 'replacing_employee_id', 'justification', 'status', 'decided_at',
    'decision_reason', 'created_at', 'department', 'branch', 'raised_by_name', 'replacing_name', 'jobs'}
JOB_KEYS = {
    'id', 'company_id', 'requisition_id', 'title', 'description', 'requirements', 'category', 'location', 'work_mode',
    'employment_type', 'experience_min', 'experience_max', 'salary_min', 'salary_max', 'show_salary', 'openings', 'deadline',
    'org_unit_id', 'branch_id', 'hiring_manager_id', 'stages', 'questions', 'status', 'published_at', 'closed_at', 'created_by',
    'created_at', 'updated_at', 'department', 'hiring_manager_name'}
JOB_LIST_KEYS = JOB_KEYS | {'applications', 'active', 'new_this_week', 'hired'}
BOARD_KEYS = {
    'id', 'stage', 'status', 'source', 'score', 'knocked_out', 'rating', 'tags', 'experience_years', 'current_employer',
    'created_at', 'stage_changed_at', 'rejected_reason', 'owner_user_id', 'owner_name', 'candidate_id', 'name', 'email', 'phone',
    'has_cv', 'other_applications', 'next_interview'}
APPLICATION_KEYS = {
    'id', 'job_id', 'candidate_id', 'stage', 'status', 'source', 'cover_note', 'answers', 'experience_years', 'current_employer',
    'score', 'score_detail', 'knocked_out', 'rating', 'tags', 'owner_user_id', 'rejected_reason', 'closed_at', 'hired_employee_id',
    'consent_at', 'stage_changed_at', 'created_at', 'has_cv', 'name', 'email', 'phone', 'purged_at', 'owner_name'}
EVENT_KEYS = {'id', 'kind', 'body', 'data', 'created_at', 'actor'}
DETAIL_INTERVIEW_KEYS = {'id', 'title', 'starts_at', 'duration_min', 'mode', 'location', 'panel', 'status', 'panel_names'}
SCORECARD_KEYS = {'id', 'interview_id', 'reviewer_id', 'ratings', 'recommendation', 'notes', 'submitted_at', 'reviewer'}
OFFER_KEYS = {'id', 'designation', 'salary', 'join_date', 'expires_on', 'notes', 'status', 'created_at', 'decided_at', 'responded_at'}
OTHER_APP_KEYS = {'id', 'stage', 'status', 'created_at', 'title'}
INTERVIEW_LIST_KEYS = {
    'id', 'application_id', 'title', 'starts_at', 'duration_min', 'mode', 'location', 'status', 'panel', 'panel_names', 'candidate',
    'job', 'job_id', 'on_panel', 'my_scorecard', 'scorecards'}
PUBLIC_JOB_KEYS = {
    'id', 'title', 'description', 'requirements', 'category', 'location', 'work_mode', 'employment_type', 'experience_min',
    'experience_max', 'salary_min', 'salary_max', 'openings', 'deadline', 'published_at', 'department'}


class Browser:
    """The React app (api/client.js): the session cookie, JSON bodies (`{}` when a call passes none), no CSRF token, and
    the browser's own Origin on writes."""

    def __init__(self, user=None, origin='http://testserver'):
        self.c = Client(enforce_csrf_checks=True)
        if user is not None:
            sign_in(self.c, user)
        self.h = {'HTTP_ORIGIN': origin}

    def get(self, path, q=None):
        return self.c.get(path, {k: v for k, v in (q or {}).items() if v not in (None, '')})

    def post(self, path, body=None):
        return self.c.post(path, body if body is not None else {}, content_type='application/json', **self.h)

    def put(self, path, body):
        return self.c.put(path, body, content_type='application/json', **self.h)

    def patch(self, path, body):
        return self.c.patch(path, body, content_type='application/json', **self.h)


@pytest.fixture(autouse=True)
def _reset_limiter():
    careers.apply_limiter.reset()
    yield
    careers.apply_limiter.reset()


def _ok(r, code=200):
    assert r.status_code == code, (r.status_code, r.content[:500])
    return r.json()


def _approve(user, object_id):
    item = next(i for i in workflow.inbox_for(user) if i['objectId'] == object_id)
    assert workflow.decide(user, item['instanceId'], 'approve') == 'approved'


def _from_job(j):
    """JobForm.jsx fromJob() then save(): the PATCH body the edit drawer sends for a listing row."""
    n = (lambda v: None if v is None else float(v))
    m = (lambda v: None if v is None else str(int(float(v))))
    return {
        'title': j['title'], 'category': j['category'] or None, 'location': j['location'] or None, 'workMode': j['work_mode'],
        'employmentType': j['employment_type'], 'openings': j['openings'], 'experienceMin': n(j['experience_min']),
        'experienceMax': n(j['experience_max']), 'salaryMin': m(j['salary_min']), 'salaryMax': m(j['salary_max']),
        'showSalary': j['show_salary'], 'deadline': j['deadline'], 'orgUnitId': j['org_unit_id'], 'branchId': j['branch_id'],
        'hiringManagerId': j['hiring_manager_id'], 'description': j['description'], 'requirements': j['requirements'],
        'stages': j['stages'], 'questions': j['questions']}


def test_react_pages_end_to_end():
    w = World()
    hr, hr2, ram, mgr2, emp = (Browser(u) for u in (w.hr, w.hr2, w.ram, w.mgr2, w.emp))

    # ---- RequisitionsPage: RequestForm.save()
    rid = _ok(ram.post(f'{H}/requisitions', {
        'title': 'Backend engineer', 'designation': None, 'orgUnitId': w.unit, 'branchId': None, 'headcount': 2,
        'salaryMin': '80000', 'salaryMax': None, 'kind': 'new', 'replacingEmployeeId': None,
        'justification': 'New payments project starting next quarter.'}), 201)['id']
    _approve(w.hr, rid)
    reqs = _ok(hr.get(f'{H}/requisitions'))['requisitions']
    r = reqs[0]
    assert set(r) == REQUISITION_KEYS
    assert (r['id'], r['raised_by'], r['status'], r['headcount'], r['filled'], r['jobs']) == (rid, w.ram.id, 'approved', 2, 0, 0)
    assert (r['salary_min'], r['salary_max'], r['department'], r['branch'], r['raised_by_name']) == \
        ('80000.00', None, 'Engineering', None, 'Ram Thapa')
    assert ISO_MS.match(r['created_at']) and ISO_MS.match(r['decided_at'])

    # ---- JobForm (from the requisition): createJob
    job_body = {
        'title': 'Backend engineer', 'category': 'Engineering', 'location': 'Kathmandu', 'workMode': 'hybrid',
        'employmentType': 'full_time', 'openings': r['headcount'] - r['filled'], 'experienceMin': 2, 'experienceMax': 5.5,
        'salaryMin': str(int(float(r['salary_min']))), 'salaryMax': None, 'showSalary': True, 'deadline': future(30),
        'orgUnitId': r['org_unit_id'], 'branchId': r['branch_id'], 'hiringManagerId': str(w.ram_emp.id),
        'description': 'Build and run the payments services that power our clients.', 'requirements': 'PostgreSQL',
        'stages': ['Applied', 'Screening', 'Interview', 'Offer', 'Hired'], 'questions': QUESTIONS, 'requisitionId': rid}
    job_id = _ok(hr.post(f'{H}/jobs', job_body), 201)['id']
    assert _ok(hr.post(f'{H}/jobs/{job_id}/publish')) == {'ok': True}

    # ---- JobsPage: the table and the edit drawer
    listing = _ok(hr.get(f'{H}/jobs'))
    assert set(listing) == {'jobs', 'companyCode'} and listing['companyCode'] == 'demo'
    j = listing['jobs'][0]
    assert set(j) == JOB_LIST_KEYS
    assert (j['experience_min'], j['experience_max'], j['salary_min'], j['salary_max']) == ('2.0', '5.5', '80000.00', None)
    assert DAY.match(j['deadline']) and ISO_MS.match(j['published_at']) and j['closed_at'] is None
    assert (j['applications'], j['active'], j['new_this_week'], j['hired']) == (0, 0, 0, 0)
    assert (j['requisition_id'], j['hiring_manager_id'], j['created_by']) == (rid, str(w.ram_emp.id), w.hr.id)
    assert j['questions'] == QUESTIONS and j['show_salary'] is True and j['openings'] == 2
    edit = _from_job(j)
    edit['title'] = 'Senior backend engineer'
    assert _ok(hr.patch(f'{H}/jobs/{job_id}', edit)) == {'ok': True}
    detail = _ok(hr.get(f'{H}/jobs/{job_id}'))
    assert set(detail) == {'job', 'stageCounts'} and set(detail['job']) == JOB_KEYS
    assert detail['job']['title'] == 'Senior backend engineer' and detail['job']['experience_max'] == '5.5'
    assert hr.get(f'{H}/jobs', {'status': None}).status_code == 200   # jobs(undefined): no status parameter

    # ---- CareersPage: list, job, apply (no session; a visitor's stray cookie needs no CSRF token either)
    visitor = Browser(w.emp)
    pub = _ok(visitor.get(f'{PUB}/jobs', {'company': 'demo', 'q': None, 'workMode': None, 'category': None}))
    assert set(pub) == {'company', 'jobs', 'categories'} and pub['company'] == {'name': 'Demo Company', 'code': 'demo'}
    assert set(pub['jobs'][0]) == PUBLIC_JOB_KEYS and pub['jobs'][0]['salary_min'] == '80000.00'
    assert pub['categories'] == ['Engineering']
    one_job = _ok(visitor.get(f'{PUB}/jobs/{job_id}'))
    assert set(one_job['job']) == PUBLIC_JOB_KEYS | {'questions'}
    assert one_job['job']['questions'][1] == {'id': 'q2', 'kind': 'number', 'label': QUESTIONS[1]['label'], 'required': True}
    apply_body = {'name': 'Anita Gurung', 'email': 'anita@example.com', 'phone': '+977 9800000000', 'experienceYears': 3.5,
                  'currentEmployer': None, 'coverNote': None, 'answers': {'q1': 'yes', 'q2': 4}, 'cvFilename': 'cv.pdf',
                  'cvDataUrl': PDF, 'consent': True}
    applied = _ok(visitor.post(f'{PUB}/jobs/{job_id}/apply', apply_body), 201)
    assert applied == {'ok': True, 'message': 'Thank you. Your application has been received.'}

    # ---- PipelinePage: the board and AddCandidate
    board = _ok(hr.get(f'{H}/jobs/{job_id}/applications', {'include': 'active'}))
    assert set(board) == {'applications', 'stages'}
    c = board['applications'][0]
    assert set(c) == BOARD_KEYS
    assert (c['experience_years'], c['score'], c['knocked_out'], c['has_cv'], c['tags'], c['rating']) == ('3.5', 100, False, True, [], None)
    assert ISO_MS.match(c['created_at']) and ISO_MS.match(c['stage_changed_at']) and c['next_interview'] is None
    app_id = c['id']
    job2 = _ok(hr.post(f'{H}/jobs', {**job_body, 'requisitionId': None, 'title': 'Data engineer', 'questions': []}), 201)['id']
    added = _ok(hr.post(f'{H}/jobs/{job2}/applications', {
        'name': 'Anita Gurung', 'email': 'ANITA@example.com', 'phone': None, 'source': 'referral', 'experienceYears': None,
        'currentEmployer': None, 'coverNote': None, 'cvFileId': None}), 201)
    second = added['id']

    # ---- CandidateDrawer: move, rate, tags, note
    assert _ok(ram.post(f'{H}/applications/{app_id}/move', {'stage': 'Screening'})) == {'ok': True}
    assert _ok(ram.patch(f'{H}/applications/{app_id}', {'rating': 4})) == {'ok': True}
    assert _ok(ram.patch(f'{H}/applications/{app_id}', {'rating': None})) == {'ok': True}   # Rating cleared (v || null)
    assert _ok(ram.patch(f'{H}/applications/{app_id}', {'tags': ['payments']})) == {'ok': True}
    assert _ok(hr.post(f'{H}/applications/{app_id}/notes', {'body': 'Strong.'}), 201) == {'ok': True}

    # ---- InterviewModal, then the InterviewsPage list and the ScorecardModal
    iv = _ok(ram.post(f'{H}/applications/{app_id}/interviews', {
        'title': 'Interview', 'date': future(2), 'time': '11:00', 'durationMin': 45, 'mode': 'in_person', 'location': None,
        'panel': [w.ram.id, w.mgr2.id], 'notifyCandidate': True}), 201)['id']
    mine = _ok(mgr2.get(f'{H}/interviews', {'scope': 'mine', 'from': future(0), 'to': future(7)}))['interviews']
    assert len(mine) == 1 and set(mine[0]) == INTERVIEW_LIST_KEYS
    m = mine[0]
    assert (m['id'], m['application_id'], m['job_id'], m['candidate'], m['job']) == (iv, app_id, job_id, 'Anita Gurung', 'Senior backend engineer')
    assert (m['on_panel'], m['my_scorecard'], m['scorecards'], m['duration_min']) == (True, False, 0, 45)
    assert m['panel'] == [w.ram.id, w.mgr2.id] and m['panel_names'] == ['Ram Thapa', 'Second Manager'] and ISO_MS.match(m['starts_at'])
    assert _ok(mgr2.post(f'{H}/interviews/{iv}/scorecard', {'ratings': {'Skills': 4, 'Culture fit': 5}, 'recommendation': 'yes',
                                                             'notes': None}), 201) == {'ok': True}
    assert _ok(mgr2.get(f'{H}/interviews'))['interviews'][0]['my_scorecard'] is True
    assert _ok(ram.patch(f'{H}/interviews/{iv}', {'status': 'completed'})) == {'ok': True}

    # ---- OfferModal, approval by the second HR user, letter, accept, HireModal
    offer_id = _ok(hr.post(f'{H}/applications/{app_id}/offers', {
        'designation': 'Software Engineer', 'salary': '100000', 'joinDate': future(30), 'expiresOn': future(7), 'notes': None}), 201)['id']
    _approve(w.hr2, offer_id)
    letter = hr.get(f'{H}/offers/{offer_id}/letter')
    assert letter.status_code == 200 and letter['Content-Type'] == 'text/html; charset=utf-8'
    assert _ok(hr.post(f'{H}/offers/{offer_id}/respond', {'response': 'accepted'})) == {'ok': True}

    d = _ok(hr.get(f'{H}/applications/{app_id}'))
    assert set(d) == {'application', 'access', 'otherApplications', 'events', 'interviews', 'scorecards', 'offers', 'job'}
    a = d['application']
    assert set(a) == APPLICATION_KEYS
    assert (a['rating'], a['tags'], a['experience_years'], a['stage'], a['answers']) == (None, ['payments'], '3.5', 'Offer', {'q1': 'yes', 'q2': 4})
    assert ISO_MS.match(a['consent_at']) and a['purged_at'] is None and a['closed_at'] is None
    assert all(set(x) == {'label', 'points', 'max', 'note'} for x in a['score_detail'])
    assert d['access'] == 'full'
    assert [set(o) for o in d['otherApplications']] == [OTHER_APP_KEYS]
    assert (d['otherApplications'][0]['id'], d['otherApplications'][0]['title']) == (second, 'Data engineer')
    assert d['events'] and all(set(e) == EVENT_KEYS for e in d['events']) and all(ISO_MS.match(e['created_at']) for e in d['events'])
    assert {e['actor'] for e in d['events']} == {'Ram Thapa', 'HR One', 'HR Two', 'Second Manager', None}
    assert [set(i) for i in d['interviews']] == [DETAIL_INTERVIEW_KEYS] and d['interviews'][0]['status'] == 'completed'
    assert [set(s) for s in d['scorecards']] == [SCORECARD_KEYS]
    s = d['scorecards'][0]
    assert (s['reviewer_id'], s['reviewer'], s['ratings'], s['notes']) == (w.mgr2.id, 'Second Manager', {'Skills': 4, 'Culture fit': 5}, None)
    assert [set(o) for o in d['offers']] == [OFFER_KEYS]
    o = d['offers'][0]
    assert (o['id'], o['salary'], o['status'], o['join_date'], o['expires_on'], o['notes']) == \
        (offer_id, '100000.00', 'accepted', future(30), future(7), None)
    assert ISO_MS.match(o['created_at']) and ISO_MS.match(o['decided_at']) and ISO_MS.match(o['responded_at'])
    assert d['job'] == {'id': job_id, 'title': 'Senior backend engineer', 'stages': job_body['stages'], 'questions': QUESTIONS,
                        'status': 'open', 'requisitionId': rid}
    panel_view = _ok(mgr2.get(f'{H}/applications/{app_id}'))
    assert panel_view['access'] == 'panel' and panel_view['offers'] == [] and panel_view['otherApplications'] == []

    hired = _ok(hr.post(f'{H}/applications/{app_id}/hire', {'empCode': 'E900', 'probationEnd': None}), 201)
    assert set(hired) == {'employeeId'}

    # ---- RejectModal and reopen on the second application
    assert _ok(hr.post(f'{H}/applications/{second}/reject', {'reason': 'Not a fit', 'notifyCandidate': False})) == {'ok': True, 'emailed': False}
    assert _ok(hr.post(f'{H}/applications/{second}/reopen')) == {'ok': True}

    # ---- JobsPage: reports and the careers policy drawer; shared.jsx lookups
    rep = _ok(hr.get(f'{H}/reports'))
    assert rep['totals'] == {'applications': 2, 'active': 1, 'hired': 1, 'rejected': 0}
    assert isinstance(rep['timeToHire']['averageDays'], (int, float)) and rep['timeToHire']['hires'] == 1
    assert {x['source'] for x in rep['sources']} == {'careers', 'referral'}
    pol = _ok(hr.get(f'{H}/policy'))['policy']
    assert _ok(hr.put(f'{H}/policy', {**pol, 'retentionDays': 120})) == {'policy': {'retentionDays': 120, 'careersEnabled': True}}
    look = _ok(ram.get(f'{H}/lookups'))
    assert set(look) == {'users', 'orgUnits', 'branches', 'shifts'}
    assert all(set(u) == {'id', 'name', 'roles'} and isinstance(u['id'], str) for u in look['users'])
    assert [set(u) for u in look['orgUnits']] == [{'id', 'name_en', 'kind'}]
    assert [set(x) for x in look['shifts']] == [{'id', 'code', 'name_en'}] and isinstance(look['shifts'][0]['id'], str)

    # ---- the export the drawer opens in a tab
    ex = hr.get(f'{H}/applications/{app_id}/export')
    assert ex.status_code == 200 and ex['Content-Type'] == 'application/json; charset=utf-8'

    # ---- RequisitionsPage cancel and job close (bodiless posts)
    rid2 = _ok(ram.post(f'{H}/requisitions', {'title': 'Tester', 'justification': 'We ship too many bugs today.'}), 201)['id']
    assert _ok(ram.post(f'{H}/requisitions/{rid2}/cancel')) == {'ok': True}
    assert _ok(hr.post(f'{H}/jobs/{job2}/close')) == {'ok': True}
    assert emp.get(f'{H}/jobs').status_code == 403


def test_cookie_writes_from_another_site_are_refused():
    """The TS sameOriginGuard: the session cookie only works for writes from the app's own origin; Bearer needs no Origin."""
    w = World()
    evil = Browser(w.hr, origin='https://evil.example')
    r = evil.post(f'{H}/requisitions', {'title': 'Engineer', 'justification': 'We need more people.'})
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert evil.put(f'{H}/policy', {'retentionDays': 90, 'careersEnabled': False}).status_code == 403
    assert evil.get(f'{H}/jobs').status_code == 200   # reads pass
    bearer = Client(enforce_csrf_checks=True)
    token = sign_in(Client(), w.hr)
    ok = bearer.post(f'{H}/requisitions', {'title': 'Engineer', 'justification': 'We need more people.'},
                     content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert ok.status_code == 201
