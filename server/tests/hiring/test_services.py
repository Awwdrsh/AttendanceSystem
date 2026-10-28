"""Port of apps/api/test/hiring.test.ts ('hiring and applicant tracking'). Each test replays the story up to its step."""
import pytest

from tests.support.sql import rows
from hajiri.core.errors import BadRequestError
from hajiri.hiring import services as hiring
from hajiri.hiring.services import clean_answers, score_application

from .world import QUESTIONS, World

pytestmark = pytest.mark.django_db


def test_scores_transparently():
    """experience and preferred answers earn points, a failed must-have is flagged"""
    job = {'experience_min': '2', 'experience_max': '5', 'questions': QUESTIONS}
    good = score_application(job, 3, {'q1': 'yes', 'q2': 4})
    assert (good['score'], good['knockedOut']) == (100, False)
    weak = score_application(job, 1, {'q1': 'no', 'q2': 1})
    assert weak['knockedOut'] is True
    assert weak['score'] == 33   # 20 of 40 for experience, 0 of 20 for the preferred answer
    assert 'Does not meet the must-have' in [d['note'] for d in weak['detail']]


def test_score_notes_and_edges():
    job = {'experience_min': None, 'experience_max': '5', 'questions': []}
    assert score_application(job, 7, {})['detail'][0] == {'label': 'Experience', 'points': 30, 'max': 40,
                                                           'note': '7 years, more than the 5 years in the range'}
    assert score_application(job, None, {})['detail'][0]['note'] == 'Experience not given'
    assert score_application({'experience_min': None, 'experience_max': None, 'questions': []}, 3, {})['score'] is None
    half = score_application({'experience_min': '2', 'experience_max': None, 'questions': []}, 1.5, {})
    assert half['detail'][0]['note'] == '1.5 years, below the 2 years asked for' and half['detail'][0]['points'] == 30


def test_clean_answers_rules():
    assert clean_answers(QUESTIONS, {'q1': True, 'q2': '4', 'q3': '  hi  ', 'zz': 'dropped'}) == {'q1': 'yes', 'q2': 4, 'q3': 'hi'}
    for bad, msg in (({'q2': 1}, 'Please answer every required question.'),
                     ({'q1': 'maybe', 'q2': 1}, 'Answer the yes or no questions with yes or no.'),
                     ({'q1': 'yes', 'q2': 'lots'}, 'Number answers must be a number.')):
        with pytest.raises(BadRequestError) as e:
            clean_answers(QUESTIONS, bad)
        assert e.value.message == msg


def test_manager_raises_request_and_hr_approves():
    World.run('step_requisition')


def test_hr_creates_job_private_until_published():
    World.run('step_job')


def test_public_careers_lists_open_jobs_without_private_fields():
    World.run('step_public_list')


def test_public_application_encrypted_cv_emails_and_tells_hr():
    World.run('step_apply')


def test_repeat_bot_and_bad_input_store_nothing():
    World.run('step_repeat')


def test_rate_limits_applications():
    World.run('step_rate_limit')


def test_flags_candidate_failing_must_have():
    World.run('step_knockout')


def test_scopes_the_pipeline():
    World.run('step_scope')


def test_moves_rates_tags_notes_with_timeline():
    World.run('step_move')


def test_schedules_interview():
    World.run('step_interview')


def test_panel_read_only_and_own_scorecard():
    World.run('step_panel')


def test_offer_needs_second_hr_then_accepted():
    World.run('step_offer')


def test_hires_candidate_as_employee():
    World.run('step_hire')


def test_rejects_and_reports():
    World.run('step_reject_and_report')


def test_exports_and_purges():
    World.run('step_export_and_purge')


def test_policy_switches_careers_off():
    World.run('step_policy')


def test_registered_with_workflow_and_worker():
    from hajiri.worker import registry
    from hajiri.workflow.service import handler_for
    registry.autodiscover()
    assert 'candidatesPurged' in {j.name for j in registry.daily_jobs()}
    assert handler_for('job_requisition') is not None and handler_for('job_offer') is not None


def test_requester_cannot_approve_own_request():
    from hajiri.core.errors import ForbiddenError
    from hajiri.workflow.service import DecisionContext, handler_for
    w = World()
    req = hiring.create_requisition(w.hr, {'title': 'Designer', 'justification': 'Our brand work needs a person.'})
    with pytest.raises(ForbiddenError) as e:
        handler_for('job_requisition')(req, 'approved', DecisionContext(w.hr.id, None))
    assert e.value.message == 'you raised this hiring request; someone else must approve it'


def test_seeder_adds_default_policy_once():
    from hajiri.hiring.seeds import seed_hiring
    w = World()
    # The row is counted under seed_defaults' 'settings' total (seed-defaults.ts), so the seeder reports nothing itself.
    assert seed_hiring(str(w.co.id)) == {}
    assert seed_hiring(str(w.co.id)) == {}
    assert rows("SELECT 1 FROM company_setting WHERE company_id = %s AND key = 'hiring.policy'", [str(w.co.id)]) != []
    assert hiring.hiring_policy(str(w.co.id)) == {'retentionDays': 180, 'careersEnabled': True}
