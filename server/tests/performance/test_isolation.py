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
from tests.web.factories import htmx, make_employee

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


GETS = [
    ('goal drawer', '/_/performance/goals/{goal}'),
    ('company goal drawer', '/_/performance/goals/{company_goal}'),
    ('kpi edit', '/_/performance/kpis/{kpi}/edit'),
    ('competency edit', '/_/performance/competencies/{competency}/edit'),
    ('cycle edit', '/_/performance/cycles/{draft}/edit'),
    ('cycle page', '/performance/reviews?cycle={cycle}'),
    ('review drawer', '/_/performance/reviews/{review}'),
    ('review print', '/_/performance/reviews/{review}/print'),
    ('1-on-1 drawer', '/_/performance/one-on-ones/{meeting}'),
    ('course edit', '/_/learning/courses/{course}/edit'),
    ('course assign form', '/_/learning/courses/{course}/assign'),
    ('course join form', '/_/learning/courses/{course}/join'),
    ('assessment', '/_/learning/enrollments/{enrollment}/assessment'),
    ('course rating form', '/_/learning/enrollments/{enrollment}/feedback'),
    ('certificate upload form', '/_/learning/enrollments/{enrollment}/certificate/upload'),
    ('certificate', '/_/learning/enrollments/{enrollment}/certificate'),
]

POSTS = [
    ('goal check-in', '/_/performance/goals/{goal}/checkin', {'comment': 'x'}),
    ('goal status', '/_/performance/goals/{goal}/status', {'status': 'completed'}),
    ('goal cancel', '/_/performance/goals/{goal}/cancel', {}),
    ('kpi edit', '/_/performance/kpis/{kpi}/edit', {'title': 'x', 'active': 'on'}),
    ('kpi apply', '/_/performance/kpis/apply', {'kpi_id': '{kpi}', 'weight': '10'}),
    ('competency edit', '/_/performance/competencies/{competency}/edit', {'kind': 'skill', 'name': 'x'}),
    ('cycle edit', '/_/performance/cycles/{draft}/edit', {'name': 'x'}),
    ('cycle delete', '/_/performance/cycles/{draft}/delete', {}),
    ('cycle launch', '/_/performance/cycles/{draft}/launch', {}),
    ('cycle advance', '/_/performance/cycles/{cycle}/advance', {}),
    ('review save', '/_/performance/reviews/{review}/save', {'summary': 'x'}),
    ('review calibrate', '/_/performance/reviews/{review}/calibrate', {'final_rating': '4', 'reason': 'Because of x'}),
    ('review peers', '/_/performance/reviews/{review}/peers', {'employee_ids': ['{report}']}),
    ('peer decide', '/_/performance/peers/{peer}/decide', {'approve': '1'}),
    ('peer remove', '/_/performance/peers/{peer}/remove', {}),
    ('1-on-1 update', '/_/performance/one-on-ones/{meeting}/update', {'action': 'notes', 'shared_notes': 'x'}),
    ('1-on-1 item', '/_/performance/one-on-ones/{meeting}/items', {'kind': 'agenda', 'text': 'x'}),
    ('1-on-1 item toggle', '/_/performance/one-on-one-items/{item}/toggle', {'done': 'on'}),
    ('1-on-1 item delete', '/_/performance/one-on-one-items/{item}/delete', {}),
    ('journal delete', '/_/performance/journal/{journal}/delete', {}),
    ('journal add', '/_/performance/journal', {'employee_id': '{report}', 'note_date': '2026-10-01', 'note': 'x'}),
    ('feedback give', '/_/performance/feedback/give', {'to_employee_id': '{report}', 'body': 'x'}),
    ('course edit', '/_/learning/courses/{course}/edit', {'code': 'X', 'title': 'x'}),
    ('course questions', '/_/learning/courses/{course}/questions', {'count': '0'}),
    ('session add', '/_/learning/courses/{course}/sessions', {'date': '2026-12-01'}),
    ('session status', '/_/learning/sessions/{session}/status', {'status': 'cancelled'}),
    ('assign', '/_/learning/courses/{course}/assign', {'employee_ids': ['{report}']}),
    ('mandatory', '/_/learning/courses/{course}/assign-mandatory', {}),
    ('enrolment cancel', '/_/learning/enrollments/{enrollment}/cancel', {}),
    ('enrolment attendance', '/_/learning/enrollments/{enrollment}/attendance', {'attended': '1'}),
    ('enrolment complete', '/_/learning/enrollments/{enrollment}/complete', {}),
]


def _fill(value, beta: Beta):
    ids = {k: v for k, v in vars(beta).items() if isinstance(v, str)}
    if isinstance(value, list):
        return [_fill(v, beta) for v in value]
    return value.format(**ids)


@pytest.mark.parametrize('name,url', GETS, ids=[g[0] for g in GETS])
def test_pages_of_another_company_are_not_found(world, beta, name, url):  # noqa: F811
    url = _fill(url, beta)
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        assert client.get(url, **htmx()).status_code == 404, url
    assert world.signed_in(world.admin_b).get(url, **htmx()).status_code != 404, url


@pytest.mark.parametrize('name,url,data', POSTS, ids=[p[0] for p in POSTS])
def test_changes_to_another_company_are_not_found(world, beta, name, url, data):  # noqa: F811
    url = _fill(url, beta)
    data = {k: _fill(v, beta) for k, v in data.items()}
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        assert client.post(url, data, **htmx()).status_code == 404, url


def test_feedback_requests_of_another_company_are_not_found(world, beta):  # noqa: F811
    fid = beta.feedback_request()
    for client in (world.signed_in(world.admin_c), world.signed_in(world.admin_a)):
        assert client.get(f'/_/performance/feedback/{fid}/answer', **htmx()).status_code == 404
        assert client.post(f'/_/performance/feedback/{fid}/decline', **htmx()).status_code == 404
    assert world.signed_in(world.admin_b).get(f'/_/performance/feedback/{fid}/answer', **htmx()).status_code == 200


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


def test_lists_show_only_the_active_company(world, beta):  # noqa: F811
    a = world.signed_in(world.admin_a)
    for url, text in (('/performance?view=company', 'Beta company goal'), ('/performance?view=kpis', 'Beta KPI'),
                      ('/performance/reviews', 'Beta cycle'), ('/performance/reviews?view=competencies', 'Beta competency'),
                      ('/performance/learning', 'Beta course'), ('/performance/learning?view=enrollments', 'Beta course'),
                      ('/me/growth?view=company', 'Beta company goal')):
        r = a.get(url)
        assert r.status_code == 200, url
        assert text not in r.content.decode(), url
    # The same pages in Beta do show them.
    b = world.signed_in(world.admin_a, world.beta)
    assert 'Beta company goal' in b.get('/performance?view=company').content.decode()
    assert 'Beta course' in b.get('/performance/learning').content.decode()


def test_a_person_in_two_companies_sees_the_goals_of_the_company_they_work_in(world):  # noqa: F811
    _, in_beta = world.session(world.dual, world.beta)
    assert in_beta.employee_id == str(world.dual_beta_employee.id)
    gid = perf.create_goal(in_beta, {'title': 'Dual at Beta'})
    assert [g['id'] for g in perf.list_goals(in_beta, {'scope': 'mine'})] == [gid]
    _, in_alpha = world.session(world.dual)
    assert perf.list_goals(in_alpha, {'scope': 'mine'}) == []
    assert 'Dual at Beta' not in world.signed_in(world.dual).get('/me/growth').content.decode()
    assert 'Dual at Beta' in world.signed_in(world.dual, world.beta).get('/me/growth').content.decode()
