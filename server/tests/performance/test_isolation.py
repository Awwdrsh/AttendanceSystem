"""
Performance and learning across a group of companies: ids of another company behave as not found, lists show only
the active company, and a person who works in two companies sees the records of the company they are working in.

The `world` fixture (tests/multicompany/conftest.py): Alpha and Beta in one group, Gamma outside it; admin_a is group
admin (working in Alpha unless switched), admin_b runs Beta, admin_c runs Gamma, dual is HR of Alpha and a manager in
Beta by membership.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hajiri.learning import services as lrn
from hajiri.performance import services as perf
from tests.multicompany.conftest import world  # noqa: F401  (the fixture)
from tests.web.factories import make_employee

pytestmark = pytest.mark.django_db


class Beta:
    """One of each performance and learning record in Beta, made by Beta's admin through the services."""

    def __init__(self, w):
        self.w = w
        _, b = w.session(w.admin_b)
        self.b = b
        report = make_employee(w.beta, name='Beta Report', manager=_employee(w.admin_b))
        self.report = str(report.id)
        self.goal = perf.create_goal(b, {'title': 'Beta goal', 'keyResults': [{'title': 'Beta KR', 'targetValue': 10}]})
        self.company_goal = perf.create_goal(b, {'scope': 'company', 'title': 'Beta company goal'})
        self.kpi = perf.create_kpi(b, {'title': 'Beta KPI'})
        self.competency = perf.create_competency(b, {'kind': 'skill', 'name': 'Beta competency'})
        self.cycle = perf.create_cycle(b, {'name': 'Beta cycle', 'periodStart': '2026-01-01', 'periodEnd': '2027-01-01', 'peerEnabled': True})
        self.draft = perf.create_cycle(b, {'name': 'Beta draft', 'periodStart': '2026-01-01', 'periodEnd': '2027-01-01'})
        perf.launch_cycle(b, self.cycle)
        self.review = next(r['id'] for r in perf.cycle_detail(b, self.cycle)['reviews'] if r['employee_id'] == self.report)
        perf.nominate_peers(b, self.review, {'employeeIds': [b.employee_id]})
        self.peer = perf.review_detail(b, self.review)['peers']['nominations'][0]['id']
        at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat().replace('+00:00', 'Z')
        self.meeting = perf.create_meeting(b, {'withEmployeeId': self.report, 'scheduledAt': at, 'agenda': ['Plan']})
        self.item = perf.meeting_detail(b, self.meeting)['items'][0]['id']
        self.journal = perf.add_journal(b, {'employeeId': self.report, 'noteDate': '2026-10-01', 'note': 'Beta note'})
        from hajiri.db.models import PerfFeedback
        PerfFeedback.objects.create(company=w.beta, giver_employee_id=b.employee_id, subject_employee_id=self.report,
                                    status=PerfFeedback.Status.REQUESTED, prompt='How is the report doing?')
        self.course = lrn.create_course(b, {'code': 'BETA', 'title': 'Beta course', 'delivery': 'online'})
        now = datetime.now(timezone.utc)
        self.session = lrn.add_session(b, self.course, {'startsAt': (now + timedelta(days=3)).isoformat(),
                                                        'endsAt': (now + timedelta(days=3, hours=1)).isoformat()})
        lrn.assign(b, {'courseId': self.course, 'employeeIds': [b.employee_id]})
        self.enrollment = lrn.list_enrollments(b)[0]['id']

    def feedback_request(self) -> str:
        return perf.feedback(self.b, {'box': 'requests'})[0]['id']


def _employee(user):
    from hajiri.db.models import Employee
    return Employee.objects.get(pk=user.employee_id)


@pytest.fixture
def beta(world):  # noqa: F811
    return Beta(world)


P = '/api/v1/performance/'
L = '/api/v1/learning/'
GETS = [
    ('goal', P + 'goals/{goal}'),
    ('company goal', P + 'goals/{company_goal}'),
    ('cycle', P + 'cycles/{draft}'),
    ('calibration', P + 'cycles/{cycle}/calibration'),
    ('review', P + 'reviews/{review}'),
    ('review print', P + 'reviews/{review}/print'),
    ('1-on-1', P + 'one-on-ones/{meeting}'),
    ('journal of a Beta employee', P + 'journal?employeeId={report}'),
    ('course', L + 'courses/{course}'),
    ('assessment', L + 'enrollments/{enrollment}/assessment'),
]

# (name, method, url, body)
POSTS = [
    ('goal check-in', 'post', P + 'goals/{goal}/checkins', {'comment': 'x'}),
    ('goal edit', 'patch', P + 'goals/{goal}', {'title': 'x'}),
    ('goal key result', 'post', P + 'goals/{goal}/key-results', {'title': 'x', 'targetValue': 5}),
    ('kpi edit', 'patch', P + 'kpis/{kpi}', {'title': 'x'}),
    ('kpi apply', 'post', P + 'kpis/{kpi}/apply', {'employeeIds': ['{report}'], 'weight': 10}),
    ('kpi delete', 'delete', P + 'kpis/{kpi}', {}),
    ('course questions', 'put', L + 'courses/{course}/questions', {'questions': []}),
    ('competency edit', 'patch', P + 'competencies/{competency}', {'name': 'x'}),
    ('cycle edit', 'patch', P + 'cycles/{draft}', {'name': 'x'}),
    ('cycle delete', 'delete', P + 'cycles/{draft}', {}),
    ('cycle launch', 'post', P + 'cycles/{draft}/launch', {}),
    ('cycle advance', 'post', P + 'cycles/{cycle}/advance', {}),
    ('review manager', 'put', P + 'reviews/{review}/manager', {'summary': 'x'}),
    ('review calibrate', 'post', P + 'reviews/{review}/calibrate', {'finalRating': 4, 'reason': 'Because of x'}),
    ('review peers', 'post', P + 'reviews/{review}/peers', {'employeeIds': ['{report}']}),
    ('peer decide', 'post', P + 'peers/{peer}/decision', {'approve': True}),
    ('peer remove', 'delete', P + 'peers/{peer}', {}),
    ('1-on-1 update', 'patch', P + 'one-on-ones/{meeting}', {'sharedNotes': 'x'}),
    ('1-on-1 item', 'post', P + 'one-on-ones/{meeting}/items', {'kind': 'agenda', 'text': 'x'}),
    ('1-on-1 item toggle', 'patch', P + 'one-on-one-items/{item}', {'done': True}),
    ('1-on-1 item delete', 'delete', P + 'one-on-one-items/{item}', {}),
    ('journal delete', 'delete', P + 'journal/{journal}', {}),
    ('journal add', 'post', P + 'journal', {'employeeId': '{report}', 'noteDate': '2026-10-01', 'note': 'x'}),
    ('course edit', 'patch', L + 'courses/{course}', {'title': 'x'}),
    ('session add', 'post', L + 'courses/{course}/sessions', {'startsAt': '2026-12-01T10:00:00+05:45', 'endsAt': '2026-12-01T12:00:00+05:45'}),
    ('session status', 'patch', L + 'sessions/{session}', {'status': 'cancelled'}),
    ('assign', 'post', L + 'enrollments/assign', {'courseId': '{course}', 'employeeIds': ['{report}']}),
    ('mandatory', 'post', L + 'courses/{course}/assign-mandatory', {}),
    ('enrolment cancel', 'post', L + 'enrollments/{enrollment}/cancel', {}),
    ('enrolment attendance', 'post', L + 'enrollments/{enrollment}/attendance', {'attended': True}),
    ('enrolment complete', 'post', L + 'enrollments/{enrollment}/complete', {}),
]


def _fill(value, beta: Beta):
    ids = {k: v for k, v in vars(beta).items() if isinstance(v, str)}
    if isinstance(value, list):
        return [_fill(v, beta) for v in value]
    return value.format(**ids)


@pytest.mark.parametrize('name,method,url,data', POSTS, ids=[p[0] for p in POSTS])
def test_changes_to_another_company_are_not_found(world, beta, name, method, url, data):  # noqa: F811
    url = _fill(url, beta)
    data = {k: _fill(v, beta) if isinstance(v, (str, list)) else v for k, v in data.items()}
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        r = getattr(client, method)(url, data, content_type='application/json')
        assert r.status_code == 404, (url, r.status_code, r.content[:200])


def test_services_refuse_ids_of_another_company(world, beta):  # noqa: F811
    from hajiri.core.errors import NotFoundError
    _, alpha_admin = world.session(world.admin_a)
    for fn, args in ((perf.goal_detail, (beta.goal,)), (perf.review_detail, (beta.review,)), (perf.cycle_detail, (beta.cycle,)),
                     (perf.get_competency, (beta.competency,)), (perf.get_kpi, (beta.kpi,)), (lrn.course_detail, (beta.course,)),
                     (lrn.cancel, (beta.enrollment,)), (perf.journal, ({'employeeId': beta.report},)),
                     (perf.create_goal, ({'employeeId': beta.report, 'title': 'x'},)),
                     (lrn.assign, ({'courseId': beta.course, 'employeeIds': [beta.report]},))):
        with pytest.raises(NotFoundError):
            fn(alpha_admin, *args)


@pytest.mark.parametrize('name,url', GETS, ids=[g[0] for g in GETS])
def test_pages_of_another_company_are_not_found(world, beta, name, url):  # noqa: F811
    url = _fill(url, beta)
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        r = client.get(url)
        assert r.status_code == 404, (url, r.status_code, r.content[:200])
    r = world.signed_in(world.admin_b).get(url)
    assert r.status_code == 200, (url, r.status_code, r.content[:200])


def test_feedback_requests_of_another_company_are_not_found(world, beta):  # noqa: F811
    fid = beta.feedback_request()
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        assert client.put(f'{P}feedback/{fid}', {'body': 'x'}, content_type='application/json').status_code == 404
        assert client.post(f'{P}feedback/{fid}/decline', {}, content_type='application/json').status_code == 404
    # Beta's admin, who was asked, sees the request in their own list.
    asked = world.signed_in(world.admin_b).get(f'{P}feedback?box=requests').json()
    assert fid in str(asked)


def test_lists_show_only_the_active_company(world, beta):  # noqa: F811
    a = world.signed_in(world.admin_a)
    for url, text in ((P + 'goals?scope=company', 'Beta company goal'), (P + 'kpis', 'Beta KPI'),
                      (P + 'cycles', 'Beta cycle'), (P + 'competencies', 'Beta competency'),
                      (L + 'courses', 'Beta course'), (L + 'enrollments?scope=all', 'Beta course')):
        r = a.get(url)
        assert r.status_code == 200, (url, r.content[:200])
        assert text not in r.content.decode(), url
    # The same lists in Beta do show them.
    b = world.signed_in(world.admin_a, world.beta)
    assert 'Beta company goal' in b.get(P + 'goals?scope=company').content.decode()
    assert 'Beta course' in b.get(L + 'courses').content.decode()


def test_a_person_in_two_companies_sees_the_goals_of_the_company_they_work_in(world):  # noqa: F811
    _, in_beta = world.session(world.dual, world.beta)
    assert in_beta.employee_id == str(world.dual_beta_employee.id)
    gid = perf.create_goal(in_beta, {'title': 'Dual at Beta'})
    assert [g['id'] for g in perf.list_goals(in_beta, {'scope': 'mine'})] == [gid]
    _, in_alpha = world.session(world.dual)
    assert perf.list_goals(in_alpha, {'scope': 'mine'}) == []
    assert 'Dual at Beta' not in world.signed_in(world.dual).get(P + 'goals?scope=mine').content.decode()
    assert 'Dual at Beta' in world.signed_in(world.dual, world.beta).get(P + 'goals?scope=mine').content.decode()

