"""Port of apps/api/test/performance.test.ts (performance part), at service level."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.learning import services as lrn
from hajiri.performance import services as perf
from hajiri.performance.jobs import performance_reminders

from .world import outbox, plus_days, today

pytestmark = pytest.mark.django_db


def raises(exc, fn, *a, **kw):
    with pytest.raises(exc) as e:
        fn(*a, **kw)
    return e.value


# ---------------------------------------------------------------- scoring helpers

def test_key_result_progress_rising_falling_yes_no():
    kp = perf.key_result_progress
    assert kp({'measure': 'number', 'start_value': 0, 'target_value': 10, 'current_value': 5, 'weight': 1}) == 50
    assert kp({'measure': 'number', 'start_value': 20, 'target_value': 5, 'current_value': 8, 'weight': 1}) == 80
    assert kp({'measure': 'percent', 'start_value': 0, 'target_value': 100, 'current_value': 140, 'weight': 1}) == 100
    assert kp({'measure': 'boolean', 'start_value': 0, 'target_value': 1, 'current_value': 1, 'weight': 1}) == 100
    assert perf.goal_progress([{'measure': 'number', 'start_value': 0, 'target_value': 10, 'current_value': 10, 'weight': 3},
                               {'measure': 'number', 'start_value': 0, 'target_value': 10, 'current_value': 0, 'weight': 1}]) == 75
    assert perf.goal_progress([]) is None


def test_overall_score_blends_with_cycle_weights():
    items = [{'item_type': 'goal', 'weight': 2, 'rating': 5}, {'item_type': 'goal', 'weight': 1, 'rating': 2},
             {'item_type': 'competency', 'weight': 1, 'rating': 3}]
    assert perf.overall_score(items, 60, 40) == 3.6
    assert perf.overall_score([i for i in items if i['item_type'] == 'competency'], 60, 40) == 3
    assert perf.overall_score([], 60, 40) is None


def test_peer_aggregate_hides_below_minimum_and_never_names_reviewers():
    peers = [{'status': 'submitted', 'rating': 4, 'strengths': 'b', 'improvements': None},
             {'status': 'submitted', 'rating': 2, 'strengths': 'a', 'improvements': 'x'},
             {'status': 'approved', 'rating': None, 'strengths': None, 'improvements': None}]
    assert perf.peer_aggregate(peers, 3) == {'count': 2, 'shown': False}
    assert perf.peer_aggregate(peers, 2) == {'count': 2, 'shown': True, 'averageRating': 3, 'strengths': ['a', 'b'], 'improvements': ['x']}


def test_add_months_clamps_to_month_end():
    assert lrn.add_months('2026-01-31', 1) == '2026-02-28'
    assert lrn.add_months('2026-10-15', 12) == '2027-10-15'


# ---------------------------------------------------------------- goals and key results

def sita_goal(w) -> str:
    return perf.create_goal(w.sita, {'title': 'Ship the attendance app', 'weight': 60, 'keyResults': [
        {'title': 'Screens done', 'measure': 'number', 'startValue': 0, 'targetValue': 10, 'currentValue': 5},
        {'title': 'Released', 'measure': 'boolean'}]})


def test_goal_with_key_results_checkins_move_progress_and_tell_the_manager(w):
    gid = sita_goal(w)
    g = perf.goal_detail(w.sita, gid)
    assert float(g['goal']['progress']) == 25
    kr = next(k for k in g['goal']['key_results'] if k['title'] == 'Released')
    before = outbox('goal.checkin')
    perf.checkin(w.sita, gid, {'keyResultId': kr['id'], 'value': 1, 'confidence': 3, 'comment': 'Live'})
    g2 = perf.goal_detail(w.sita, gid)
    assert float(g2['goal']['progress']) == 75
    assert len(g2['checkins']) == 1
    assert outbox('goal.checkin') == before + 1
    # The manager is told.
    n = one("SELECT user_id, body FROM notification_outbox WHERE template = 'goal.checkin'")
    assert str(n['user_id']) == w.ram.id and '(75%): Live' in n['body']
    # Manual progress is refused when key results drive it.
    e = raises(BadRequestError, perf.update_goal, w.sita, gid, {'progress': 90})
    assert e.message == 'progress comes from the key results; update those instead'


def test_goal_input_is_validated(w):
    gid = sita_goal(w)
    raises(ValidationError, perf.create_goal, w.sita, {'title': ''})
    e = raises(BadRequestError, perf.create_goal, w.sita, {'title': 'x', 'startDate': '2026-10-10', 'dueDate': '2026-10-01'})
    assert e.message == 'the due date is before the start date'
    e = raises(ValidationError, perf.checkin, w.sita, gid, {})
    assert e.issues[0]['message'] == 'add a value, progress or a comment'


def test_managers_set_team_goals_colleagues_and_other_companies_cannot_see(w):
    gid = sita_goal(w)
    perf.create_goal(w.ram, {'employeeId': w.SITA, 'title': 'Mentor Hari', 'status': 'active'})
    assert outbox('goal.assigned') == 1
    raises(ForbiddenError, perf.create_goal, w.hari, {'employeeId': w.SITA, 'title': 'Nope'})
    raises(NotFoundError, perf.goal_detail, w.hari, gid)
    raises(ForbiddenError, perf.list_goals, w.hari, {'scope': 'employee', 'employeeId': w.SITA})
    raises(NotFoundError, perf.goal_detail, w.other_hr, gid)
    raises(NotFoundError, perf.create_goal, w.hr, {'employeeId': w.OTHER_EMP, 'title': 'x'})
    assert gid in [g['id'] for g in perf.list_goals(w.ram, {'scope': 'team'})]
    assert perf.list_goals(w.hari, {'scope': 'team'}) == []
    perf.update_goal(w.ram, gid, {'weight': 50})
    raises(NotFoundError, perf.update_goal, w.hari, gid, {'weight': 50})
    # A PATCH changes only what it sends.
    perf.update_goal(w.ram, gid, {'status': 'completed'})
    assert one('SELECT weight, status FROM perf_goal WHERE id = %s', [gid]) == {'weight': 50, 'status': 'completed'}


def test_company_goals_by_hr_visible_to_all_and_personal_goals_align(w):
    gid = sita_goal(w)
    e = raises(ForbiddenError, perf.create_goal, w.sita, {'scope': 'company', 'title': 'Grow 30%'})
    assert e.message == 'only HR sets company goals'
    cid = perf.create_goal(w.hr, {'scope': 'company', 'title': 'Grow 30%'})
    assert cid in [g['id'] for g in perf.list_goals(w.hari, {'scope': 'company'})]
    al = perf.create_goal(w.hari, {'title': 'Close 10 deals', 'parentId': cid})
    assert al in [x['id'] for x in perf.goal_detail(w.hr, cid)['children']]
    raises(NotFoundError, perf.create_goal, w.hari, {'title': 'x', 'parentId': gid})


def test_kpi_library_managed_by_hr_and_turns_into_goals(w):
    raises(ForbiddenError, perf.create_kpi, w.sita, {'title': 'x'})
    k = perf.create_kpi(w.hr, {'designation': 'Engineer', 'title': 'Bugs fixed per month', 'defaultTarget': 20})
    assert len(perf.list_kpis(w.sita, 'Engineer')) == 1
    g = perf.apply_kpi(w.sita, k, {'weight': 40})
    assert float(perf.goal_detail(w.sita, g)['goal']['key_results'][0]['target_value']) == 20
    raises(ForbiddenError, perf.apply_kpi, w.hari, k, {'employeeId': w.SITA})
    perf.delete_kpi(w.hr, k)
    assert perf.list_kpis(w.sita) == []


def test_key_results_can_be_added_changed_and_removed(w):
    gid = sita_goal(w)
    perf.add_key_result(w.sita, gid, {'title': 'Docs', 'measure': 'boolean'})
    krs = perf.goal_detail(w.sita, gid)['goal']['key_results']
    docs = next(k for k in krs if k['title'] == 'Docs')
    perf.update_key_result(w.sita, docs['id'], {'currentValue': 1})
    # 50 * 1 + 0 + 100 over 3
    assert float(perf.goal_detail(w.sita, gid)['goal']['progress']) == 50
    perf.delete_key_result(w.sita, docs['id'])
    assert float(perf.goal_detail(w.sita, gid)['goal']['progress']) == 25
    raises(NotFoundError, perf.delete_key_result, w.hari, krs[0]['id'])
    perf.cancel_goal(w.sita, gid)
    raises(BadRequestError, perf.checkin, w.sita, gid, {'comment': 'x'})
    assert one("SELECT action FROM audit_log WHERE table_name = 'perf_goal' AND row_id = %s", [gid])['action'] == 'cancel'


# ---------------------------------------------------------------- review cycles

class Cycle:
    """Runs the review-cycle steps of the TS suite in order; each test goes as far as it needs."""

    def __init__(self, w):
        self.w = w
        self.goal = sita_goal(w)

    def setup(self):
        w = self.w
        perf.create_competency(w.hr, {'kind': 'skill', 'name': 'Problem solving', 'indicators': [{'level': 3, 'text': 'Solves routine problems alone'}]})
        perf.create_competency(w.hr, {'kind': 'knowledge', 'name': 'Finance', 'designations': ['Accountant']})
        self.course = lrn.create_course(w.hr, {'code': 'LEAD', 'title': 'Leadership basics', 'cost': '5000.00'})
        self.cycle = perf.create_cycle(w.hr, {'name': 'FY 2083', 'periodStart': '2026-01-01', 'periodEnd': plus_days(200), 'peerEnabled': True,
                                              'peerMin': 2, 'selfDue': plus_days(3)})
        return self

    def launch(self):
        self.participants = perf.launch_cycle(self.w.hr, self.cycle)
        self.review = perf.list_reviews(self.w.sita)[0]['id']
        return self

    def self_review(self):
        rv = perf.review_detail(self.w.sita, self.review)
        perf.save_self(self.w.sita, self.review, {'items': [{'id': i['id'], 'rating': 4, 'comment': 'Did well'} for i in rv['items']],
                                                  'summary': 'Good year', 'submit': True})
        return self

    def peers(self):
        w = self.w
        perf.nominate_peers(w.sita, self.review, {'employeeIds': [w.HARI, w.GITA]})
        for p in perf.review_detail(w.ram, self.review)['peers']['nominations']:
            perf.decide_peer(w.ram, p['id'], {'approve': True})
        return self

    def items(self):
        rv = perf.review_detail(self.w.ram, self.review)
        return [{'id': i['id'], 'rating': 5 if i['item_type'] == 'goal' else 3, 'comment': 'Solid'} for i in rv['items']]

    def manager(self):
        w = self.w
        assert perf.advance_cycle(w.hr, self.cycle) == 'manager_review'
        perf.save_manager(w.ram, self.review, {'items': self.items(), 'summary': 'Strong delivery', 'submit': True,
                                                'recommendation': {'incrementPct': 10, 'promotion': True, 'trainingCourseIds': [self.course]}})
        assert perf.advance_cycle(w.hr, self.cycle) == 'peer_review'
        for u, rating in ((w.hari, 4), (w.gita, 5)):
            pr = perf.peer_requests(u)[0]
            perf.submit_peer(u, pr['id'], {'rating': rating, 'strengths': f'strength {rating}'})
        return self

    def release(self):
        w = self.w
        assert perf.advance_cycle(w.hr, self.cycle) == 'calibration'
        perf.calibrate(w.hr, self.review, {'finalRating': 4, 'reason': 'Bring in line with peers'})
        assert perf.advance_cycle(w.hr, self.cycle) == 'released'
        return self


def test_hr_defines_competencies_and_cycle_weights_add_up(w):
    raises(ForbiddenError, perf.create_competency, w.sita, {'kind': 'skill', 'name': 'x'})
    e = raises(BadRequestError, perf.create_cycle, w.hr, {'name': 'FY 2083', 'periodStart': '2026-07-17', 'periodEnd': '2027-07-16',
                                                          'goalsWeight': 70, 'competencyWeight': 40})
    assert e.message == 'goal and competency weights must add up to 100'
    c = Cycle(w).setup()
    raises(NotFoundError, perf.cycle_detail, w.sita, c.cycle)


def test_launch_creates_reviews_with_goals_and_role_competencies(w):
    c = Cycle(w).setup().launch()
    assert c.participants == 4
    raises(ConflictError, perf.launch_cycle, w.hr, c.cycle)
    e = raises(ConflictError, perf.update_cycle, w.hr, c.cycle, {'goalsWeight': 50, 'competencyWeight': 50})
    assert e.message == 'after launch only the name, deadlines and the peer minimum can change'
    # Deadlines still change after launch.
    perf.update_cycle(w.hr, c.cycle, {'name': 'FY 2083/84', 'selfDue': plus_days(4), 'peerMin': 2})
    rv = perf.review_detail(w.sita, c.review)
    assert rv['role'] == 'self' and rv['can']['editSelf'] is True
    types = [f"{i['item_type']}:{i['title']}" for i in rv['items']]
    assert 'goal:Ship the attendance app' in types and 'competency:Problem solving' in types and 'competency:Finance' not in types
    assert perf.review_detail(w.ram, c.review)['role'] == 'manager'
    raises(NotFoundError, perf.review_detail, w.hari, c.review)
    raises(NotFoundError, perf.review_detail, w.other_hr, c.review)
    assert outbox('review.self_open') == 4


def test_self_review_first_manager_waits_incomplete_refused(w):
    c = Cycle(w).setup().launch()
    rv = perf.review_detail(w.sita, c.review)
    raises(ConflictError, perf.save_manager, w.ram, c.review, {'summary': 'early'})
    raises(NotFoundError, perf.save_self, w.ram, c.review, {'summary': 'x'})
    e = raises(BadRequestError, perf.save_self, w.sita, c.review, {'items': [{'id': rv['items'][0]['id'], 'rating': 4}], 'submit': True})
    assert e.message == 'rate every goal and competency before submitting'
    c.self_review()
    raises(ConflictError, perf.save_self, w.sita, c.review, {'summary': 'again'})
    assert float(one('SELECT self_score FROM perf_review WHERE id = %s', [c.review])['self_score']) == 4


def test_peers_nominated_by_employee_approved_by_manager(w):
    c = Cycle(w).setup().launch().self_review()
    raises(BadRequestError, perf.nominate_peers, w.sita, c.review, {'employeeIds': [w.SITA]})
    raises(NotFoundError, perf.nominate_peers, w.sita, c.review, {'employeeIds': [w.OTHER_EMP]})
    assert perf.nominate_peers(w.sita, c.review, {'employeeIds': [w.HARI, w.GITA]}) == 2
    for p in perf.review_detail(w.ram, c.review)['peers']['nominations']:
        raises(ForbiddenError, perf.decide_peer, w.sita, p['id'], {'approve': True})
        perf.decide_peer(w.ram, p['id'], {'approve': True})
    reqs = perf.peer_requests(w.hari)
    assert len(reqs) == 1 and reqs[0]['employee_name'] == 'Sita Sharma'


def test_manager_review_with_recommendation_then_peers_respond(w):
    c = Cycle(w).setup().launch().self_review().peers()
    assert perf.advance_cycle(w.hr, c.cycle) == 'manager_review'
    assert perf.review_detail(w.ram, c.review)['can']['editManager'] is True
    items = c.items()
    raises(NotFoundError, perf.save_manager, w.hari, c.review, {'items': items})
    e = raises(BadRequestError, perf.save_manager, w.ram, c.review,
               {'items': items, 'recommendation': {'trainingCourseIds': ['00000000-0000-7000-8000-00000000ffff']}})
    assert e.message == 'a recommended course was not found'
    perf.save_manager(w.ram, c.review, {'items': items, 'summary': 'Strong delivery', 'submit': True,
                                        'recommendation': {'incrementPct': 10, 'promotion': True, 'trainingCourseIds': [c.course]}})
    assert float(one('SELECT manager_score FROM perf_review WHERE id = %s', [c.review])['manager_score']) == 4.2
    assert perf.advance_cycle(w.hr, c.cycle) == 'peer_review'
    for u, rating in ((w.hari, 4), (w.gita, 5)):
        pr = perf.peer_requests(u)[0]
        perf.submit_peer(u, pr['id'], {'rating': rating, 'strengths': f'strength {rating}'})
        raises(ConflictError, perf.submit_peer, u, pr['id'], {'rating': rating})
    # Before release the employee sees neither manager ratings nor peer feedback.
    s = perf.review_detail(w.sita, c.review)
    assert s['review']['final_rating'] is None
    assert all(i['manager_rating'] is None for i in s['items'])
    assert s['peers']['aggregate'] is None


def test_calibration_needs_reason_is_audited_release_creates_training(w):
    c = Cycle(w).setup().launch().self_review().peers()
    assert perf.advance_cycle(w.hr, c.cycle) == 'manager_review'
    perf.save_manager(w.ram, c.review, {'items': c.items(), 'summary': 'Strong delivery', 'submit': True,
                                        'recommendation': {'incrementPct': 10, 'promotion': True, 'trainingCourseIds': [c.course]}})
    assert perf.advance_cycle(w.hr, c.cycle) == 'peer_review'
    for u, rating in ((w.hari, 4), (w.gita, 5)):
        perf.submit_peer(u, perf.peer_requests(u)[0]['id'], {'rating': rating, 'strengths': f'strength {rating}'})
    raises(ConflictError, perf.calibrate, w.hr, c.review, {'finalRating': 4, 'reason': 'adjust'})
    assert perf.advance_cycle(w.hr, c.cycle) == 'calibration'
    assert perf.calibration(w.hr, c.cycle)['distribution']['overall'][3] == 1
    raises(ForbiddenError, perf.calibration, w.ram, c.cycle)
    raises(ValidationError, perf.calibrate, w.hr, c.review, {'finalRating': 4})
    raises(ForbiddenError, perf.calibrate, w.ram, c.review, {'finalRating': 4, 'reason': 'Bring in line'})
    perf.calibrate(w.hr, c.review, {'finalRating': 4, 'reason': 'Bring in line with peers'})
    assert len(rows("SELECT 1 FROM audit_log WHERE table_name = 'perf_review' AND action = 'calibrate' AND row_id = %s", [c.review])) == 1
    raises(ConflictError, perf.acknowledge, w.sita, c.review, {})
    assert perf.advance_cycle(w.hr, c.cycle) == 'released'
    s = perf.review_detail(w.sita, c.review)
    assert float(s['review']['final_rating']) == 4
    assert 'recommendation' not in s['review']
    agg = s['peers']['aggregate']
    assert agg['shown'] is True and agg['count'] == 2 and agg['averageRating'] == 4.5
    assert 'Hari' not in str(agg) and 'Gita' not in str(agg) and 'reviewer' not in str(agg)
    assert all(p['status'] != 'submitted' for p in s['peers']['nominations'])
    enr = rows('SELECT status, source FROM lrn_enrollment WHERE employee_id = %s AND course_id = %s', [w.SITA, c.course])
    assert enr[0] == {'status': 'enrolled', 'source': 'review'}
    perf.acknowledge(w.sita, c.review, {'comment': 'Thanks'})
    raises(ConflictError, perf.acknowledge, w.sita, c.review, {})


def test_print_summary_employee_copy_leaves_out_recommendation(w):
    c = Cycle(w).setup().launch().self_review().peers().manager().release()
    mine = perf.review_print_html(w.sita, c.review)
    assert 'Sita Sharma' in mine and 'Recommendation' not in mine
    assert 'Increment 10%' in perf.review_print_html(w.ram, c.review)
    raises(NotFoundError, perf.review_print_html, w.hari, c.review)


def test_draft_cycle_can_be_deleted_launched_cannot(w):
    c = Cycle(w).setup()
    other = perf.create_cycle(w.hr, {'name': 'Spare', 'periodStart': '2026-01-01', 'periodEnd': '2026-06-30'})
    perf.delete_cycle(w.hr, other)
    raises(NotFoundError, perf.cycle_detail, w.hr, other)
    c.launch()
    raises(ConflictError, perf.delete_cycle, w.hr, c.cycle)


# ---------------------------------------------------------------- journal, feedback, 1-on-1s

def test_journal_private_to_author_and_hr(w):
    c = Cycle(w).setup().launch()
    raises(ForbiddenError, perf.add_journal, w.hari, {'employeeId': w.SITA, 'noteDate': today(), 'note': 'x'})
    j = perf.add_journal(w.ram, {'employeeId': w.SITA, 'noteDate': today(), 'tag': 'positive', 'note': 'Handled the outage calmly'})
    raises(ForbiddenError, perf.journal, w.sita, {'employeeId': w.SITA})
    assert len(perf.journal(w.ram, {'employeeId': w.SITA})) == 1
    assert len(perf.journal(w.hr, {'employeeId': w.SITA})) == 1
    raises(NotFoundError, perf.journal, w.other_hr, {'employeeId': w.SITA})
    assert 'Handled the outage calmly' in [x['note'] for x in perf.review_detail(w.ram, c.review)['journal']]
    raises(NotFoundError, perf.delete_journal, w.hr, j)
    perf.delete_journal(w.ram, j)


def test_feedback_private_to_manager_hidden_from_subject(w):
    raises(BadRequestError, perf.give_feedback, w.sita, {'toEmployeeId': w.SITA, 'body': 'me'})
    perf.give_feedback(w.hari, {'toEmployeeId': w.SITA, 'body': 'Great code reviews'})
    perf.give_feedback(w.gita, {'toEmployeeId': w.SITA, 'body': 'Misses standups', 'privateToManager': True})
    assert [f['body'] for f in perf.feedback(w.sita, {'box': 'received'})] == ['Great code reviews']
    assert len(perf.feedback(w.ram, {'box': 'about', 'employeeId': w.SITA})) == 2
    raises(ForbiddenError, perf.feedback, w.hari, {'box': 'about', 'employeeId': w.SITA})
    # The private note went to the manager, not to Sita.
    n = rows("SELECT user_id, body FROM notification_outbox WHERE template = 'feedback.given' ORDER BY created_at, id")
    assert {str(x['user_id']) for x in n} == {w.sita.id, w.ram.id}


def test_feedback_requests_answered_or_declined_by_the_colleague(w):
    perf.request_feedback(w.sita, {'fromEmployeeIds': [w.GITA], 'prompt': 'How was the launch?'})
    raises(ForbiddenError, perf.request_feedback, w.hari, {'fromEmployeeIds': [w.GITA], 'aboutEmployeeId': w.SITA})
    req = perf.feedback(w.gita, {'box': 'requests'})[0]['id']
    raises(NotFoundError, perf.answer_feedback, w.hari, req, {'body': 'x'})
    perf.answer_feedback(w.gita, req, {'body': 'Smooth launch'})
    e = raises(ConflictError, perf.decline_feedback, w.gita, req)
    assert e.message == 'this request was already answered'
    mine = perf.feedback(w.sita, {'box': 'requests'})[0]
    assert mine['status'] == 'given' and mine['body'] == 'Smooth launch'


def test_one_on_ones_private_notes_and_recurring_roll_forward(w):
    at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')
    raises(ForbiddenError, perf.create_meeting, w.hari, {'withEmployeeId': w.SITA, 'scheduledAt': at})
    m = perf.create_meeting(w.ram, {'withEmployeeId': w.SITA, 'scheduledAt': at, 'cadence': 'weekly', 'agenda': ['Career plan']})
    raises(NotFoundError, perf.meeting_detail, w.hari, m)
    e = raises(ForbiddenError, perf.meeting_detail, w.hr, m)
    assert e.message == 'this account is not linked to an employee'
    perf.update_meeting(w.ram, m, {'privateNotes': 'Considering promotion'})
    perf.update_meeting(w.sita, m, {'privateNotes': 'Ask about training budget', 'sharedNotes': 'Agreed on Q2 focus'})
    s = perf.meeting_detail(w.sita, m)
    assert s['meeting']['private_notes'] == 'Ask about training budget'
    assert 'Considering promotion' not in str(s)
    raises(BadRequestError, perf.add_meeting_item, w.sita, m, {'kind': 'action', 'text': 'Draft plan', 'ownerEmployeeId': w.HARI})
    perf.add_meeting_item(w.sita, m, {'kind': 'action', 'text': 'Draft plan', 'ownerEmployeeId': w.SITA})
    nxt = perf.update_meeting(w.ram, m, {'status': 'done'})
    assert nxt
    assert 'Draft plan' in [a['text'] for a in perf.meeting_detail(w.ram, nxt)['openActions']]
    # Only the two people see it in their lists.
    assert {x['id'] for x in perf.meetings(w.sita)['meetings']} == {m, nxt}
    assert perf.meetings(w.hari)['meetings'] == []


# ---------------------------------------------------------------- phase reminders

def test_reminds_people_who_owe_a_self_review_once_per_step(w):
    c = perf.create_cycle(w.hr, {'name': 'Mid-year', 'periodStart': '2026-01-01', 'periodEnd': plus_days(100), 'selfDue': plus_days(1)})
    perf.launch_cycle(w.hr, c)
    assert performance_reminders()['reviews'] == 4
    assert performance_reminders()['reviews'] == 0


def test_reminds_both_sides_the_day_before_a_one_on_one(w):
    at = (datetime.now(timezone.utc) + timedelta(minutes=345)).replace(hour=6, minute=0) + timedelta(days=1)
    perf.create_meeting(w.ram, {'withEmployeeId': w.SITA, 'scheduledAt': at.replace(tzinfo=None).isoformat(timespec='seconds') + '+05:45'})
    assert performance_reminders()['oneOnOnes'] == 1
    assert outbox('one_on_one.reminder') == 2
    assert performance_reminders()['oneOnOnes'] == 0


_ = execute
