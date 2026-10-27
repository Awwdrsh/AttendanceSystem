"""
Rules of apps/api/src/modules/hiring/routes.ts that the shared story does not reach: job edits, publishing checks,
requisition cancelling, interview changes, scorecard and offer edge cases, rejecting with open offers.
"""
import pytest

from hajiri.core.db import one, rows
from hajiri.core.errors import HttpError
from hajiri.hiring import services as hiring
from hajiri.workflow import service as workflow

from .world import World, future, status

pytestmark = pytest.mark.django_db


def _err(fn, *args) -> tuple[int, str]:
    try:
        fn(*args)
    except HttpError as e:
        return e.status, e.message
    raise AssertionError('expected an error')


def _job(w: World, **extra) -> str:
    jid = hiring.create_job(w.hr, {'title': 'Designer', 'description': 'Design the screens our customers use every day.',
                                   'orgUnitId': w.unit, 'hiringManagerId': str(w.ram_emp.id), **extra})
    hiring.publish_job(w.hr, jid)
    return jid


def _candidate(w: World, jid: str, email='asha@example.com') -> str:
    return hiring.add_candidate(w.hr, jid, {'name': 'Asha Lama', 'email': email, 'source': 'referral'})


def test_job_update_changes_only_sent_fields_and_guards_stages_and_request():
    w = World()
    jid = _job(w, experienceMin=2, experienceMax=5, location='Pokhara')
    hiring.update_job(w.hr, jid, {'title': 'Product designer'})
    j = hiring.get_job(w.hr, jid)['job']
    assert (j['title'], j['location'], j['work_mode'], float(j['experience_min'])) == ('Product designer', 'Pokhara', 'onsite', 2.0)
    assert _err(hiring.update_job, w.hr, jid, {'experienceMax': 1}) == (400, 'the most experience cannot be below the least')
    assert _err(hiring.update_job, w.hr, jid, {'requisitionId': None}) == (400, 'the hiring request of a job cannot be changed')
    _candidate(w, jid)
    hiring.move_application(w.hr, str(one('SELECT id FROM hiring_application')['id']), {'stage': 'Screening'})
    assert _err(hiring.update_job, w.hr, jid, {'stages': ['Applied', 'Interview', 'Hired']}) == \
        (409, 'candidates are in: Screening. Move them before removing the stage.')
    hiring.update_job(w.hr, jid, {'stages': ['Applied', 'Screening', 'Final round', 'Hired']})
    assert hiring.get_job(w.hr, jid)['job']['stages'][2] == 'Final round'
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE table_name = 'hiring_job' AND action = 'update'")['n'] == 2
    assert status(hiring.update_job, w.ram, jid, {'title': 'Nope'}) == 403


def test_job_validation_messages():
    w = World()
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'salaryMin': '50000', 'salaryMax': '40000'}) == \
        (400, 'the highest salary cannot be below the lowest')
    qs = [{'id': 'q1', 'kind': 'yes_no', 'label': 'Can you drive?'}, {'id': 'q1', 'kind': 'text', 'label': 'Anything else?'}]
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'questions': qs}) == (400, 'each question needs a different id')
    yn = [{'id': 'q1', 'kind': 'yes_no', 'label': 'Can you drive?', 'preferred': {'min': 1}}]
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'questions': yn}) == \
        (400, '"Can you drive?": a yes or no rule needs a yes or no answer')
    num = [{'id': 'q1', 'kind': 'number', 'label': 'Years driving', 'knockout': {'equals': 'yes'}}]
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'questions': num}) == (400, '"Years driving": a number rule needs a minimum')
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'orgUnitId': '00000000-0000-7000-8000-00000000ffff'}) == \
        (400, 'department does not exist in your company')
    req = hiring.create_requisition(w.ram, {'title': 'Ops lead', 'justification': 'We are opening a new branch soon.'})
    assert _err(hiring.create_job, w.hr, {'title': 'Ops', 'requisitionId': req}) == \
        (400, 'only an approved hiring request can be turned into a job')


def test_publish_needs_description_and_live_deadline_and_close_is_audited():
    w = World()
    jid = hiring.create_job(w.hr, {'title': 'Driver', 'description': 'Short'})
    assert _err(hiring.publish_job, w.hr, jid) == (400, 'add a job description (at least 20 characters) before publishing')
    hiring.update_job(w.hr, jid, {'description': 'Drive the delivery van around the valley.'})
    one("UPDATE hiring_job SET deadline = '2020-01-01' WHERE id = %s RETURNING id", [jid])
    assert _err(hiring.publish_job, w.hr, jid) == (400, 'the application deadline has passed; set a new deadline first')
    hiring.update_job(w.hr, jid, {'deadline': future(10)})
    hiring.publish_job(w.hr, jid)
    hiring.publish_job(w.hr, jid)   # already open: no-op
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'publish' AND row_id = %s", [jid])['n'] == 1
    hiring.close_job(w.hr, jid)
    j = one('SELECT status, closed_at FROM hiring_job WHERE id = %s', [jid])
    assert j['status'] == 'closed' and j['closed_at'] is not None
    hiring.publish_job(w.hr, jid)
    assert one('SELECT status, closed_at FROM hiring_job WHERE id = %s', [jid]) == {'status': 'open', 'closed_at': None}


def test_requisition_cancel_rules():
    w = World()
    req = hiring.create_requisition(w.ram, {'title': 'Ops lead', 'justification': 'We are opening a new branch soon.'})
    assert workflow.instance_for_object('job_requisition', req)['state'] == 'pending'
    assert _err(hiring.cancel_requisition, w.mgr2, req) == (404, 'request not found')
    assert _err(hiring.cancel_requisition, w.other_hr, req) == (404, 'request not found')
    hiring.cancel_requisition(w.ram, req)
    assert one('SELECT status FROM hiring_requisition WHERE id = %s', [req])['status'] == 'cancelled'
    assert workflow.instance_for_object('job_requisition', req)['state'] == 'cancelled'
    assert _err(hiring.cancel_requisition, w.hr, req) == (409, 'this request is already cancelled')
    assert _err(hiring.get_requisition, w.mgr2, req) == (404, 'request not found')


def test_requisition_rejected_notifies_with_reason_and_replacement_needs_employee():
    w = World()
    assert _err(hiring.create_requisition, w.ram, {'title': 'Ops lead', 'kind': 'replacement',
                                                    'justification': 'Our lead has resigned this month.'}) == \
        (400, 'choose the employee being replaced')
    req = hiring.create_requisition(w.ram, {'title': 'Ops lead', 'kind': 'replacement', 'replacingEmployeeId': str(w.sita_emp.id),
                                            'justification': 'Our lead has resigned this month.'})
    r = next(x for x in hiring.list_requisitions(w.hr) if str(x['id']) == req)
    assert r['replacing_name'] == 'Sita Sharma'
    inst = workflow.instance_for_object('job_requisition', req)
    workflow.decide(w.hr, inst['id'], 'reject', reason='No budget this year')
    q = one('SELECT status, decision_reason FROM hiring_requisition WHERE id = %s', [req])
    assert (q['status'], q['decision_reason']) == ('rejected', 'No budget this year')
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'job_requisition.rejected' AND user_id = %s", [w.ram.id])
    assert n['title'] == 'Hiring request rejected'
    assert n['body'] == 'Your request to hire "Ops lead" was rejected: No budget this year.'


def test_interview_update_reschedules_in_nepal_time_and_checks_panel():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    iid = hiring.schedule_interview(w.hr, app, {'date': future(3), 'time': '11:00', 'mode': 'phone', 'panel': [w.ram.id],
                                                'notifyCandidate': False})
    assert not rows("SELECT 1 FROM notification_outbox WHERE template = 'hiring.interview_invite'")
    hiring.update_interview(w.hr, iid, {'time': '15:30'})
    i = one("SELECT to_char(starts_at AT TIME ZONE 'Asia/Kathmandu', 'YYYY-MM-DD HH24:MI') AS at FROM hiring_interview WHERE id = %s", [iid])
    assert i['at'] == f'{future(3)} 15:30'
    hiring.update_interview(w.hr, iid, {'date': future(4)})
    i = one("SELECT to_char(starts_at AT TIME ZONE 'Asia/Kathmandu', 'YYYY-MM-DD HH24:MI') AS at FROM hiring_interview WHERE id = %s", [iid])
    assert i['at'] == f'{future(4)} 15:30'
    assert _err(hiring.update_interview, w.hr, iid, {'panel': [w.other_hr.id]}) == \
        (400, 'every panel member must be an active user in your company')
    hiring.update_interview(w.hr, iid, {'location': 'Room 2', 'panel': [w.ram.id, w.mgr2.id, w.ram.id]})
    i = one('SELECT location, panel FROM hiring_interview WHERE id = %s', [iid])
    assert i['location'] == 'Room 2' and [str(p) for p in i['panel']] == [w.ram.id, w.mgr2.id]
    hiring.update_interview(w.hr, iid, {})   # nothing sent: nothing logged
    bodies = [e['body'] for e in rows("SELECT body FROM hiring_event WHERE kind = 'interview' ORDER BY created_at, id")]
    assert bodies.count('Interview updated') == 3
    hiring.update_interview(w.hr, iid, {'status': 'cancelled'})
    assert 'Interview cancelled' in [e['body'] for e in rows("SELECT body FROM hiring_event WHERE kind = 'interview'")]
    assert _err(hiring.submit_scorecard, w.ram, iid, {'ratings': {'Skills': 3}, 'recommendation': 'no'}) == \
        (409, 'this interview was cancelled')
    assert _err(hiring.update_interview, w.other_hr, iid, {'status': 'completed'}) == (404, 'interview not found')
    assert status(hiring.update_interview, w.emp, iid, {'status': 'completed'}) == 403


def test_interviews_listing_scopes_and_window():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    hiring.schedule_interview(w.hr, app, {'date': future(3), 'time': '11:00', 'mode': 'video', 'panel': [w.mgr2.id]})
    assert len(hiring.list_interviews(w.ram, {'scope': 'all'})) == 1     # department manager sees the job's interviews
    assert len(hiring.list_interviews(w.ram, {'scope': 'mine'})) == 0
    assert len(hiring.list_interviews(w.mgr2, {'scope': 'mine'})) == 1   # panel member
    assert len(hiring.list_interviews(w.hr, {'scope': 'all', 'from': future(4)})) == 0
    assert len(hiring.list_interviews(w.hr, {'scope': 'all', 'from': future(3), 'to': future(3)})) == 1
    assert len(hiring.list_interviews(w.other_hr, {'scope': 'all'})) == 0
    assert len(hiring.list_interviews(w.emp, {'scope': 'mine'})) == 0
    assert _err(hiring.list_interviews, w.emp, {'scope': 'all'}) == (403, 'only managers and HR can see all interviews')


def test_reject_withdraws_open_offer_and_reopen_needs_hr():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    offer = hiring.create_offer(w.hr, app, {'designation': 'Designer', 'salary': '50000', 'joinDate': future(20)})
    assert one('SELECT stage FROM hiring_application WHERE id = %s', [app])['stage'] == 'Offer'
    assert _err(hiring.create_offer, w.hr, app, {'designation': 'Designer', 'salary': '0', 'joinDate': future(20)})[0] == 400
    assert _err(hiring.create_offer, w.hr, app, {'designation': 'Designer', 'salary': '1', 'joinDate': future(20), 'expiresOn': future(21)}) == \
        (400, 'the offer should expire on or before the joining date')
    assert hiring.reject_application(w.ram, app, {'reason': 'Went another way', 'notifyCandidate': False}) is False
    assert one('SELECT status FROM hiring_offer WHERE id = %s', [offer])['status'] == 'withdrawn'
    assert workflow.instance_for_object('job_offer', offer)['state'] == 'cancelled'
    assert not rows("SELECT 1 FROM notification_outbox WHERE template = 'hiring.rejected'")
    assert _err(hiring.withdraw_offer, w.hr, offer) == (409, 'this offer is withdrawn')
    assert _err(hiring.move_application, w.hr, app, {'stage': 'Screening'}) == (409, 'this application is rejected')
    assert status(hiring.reopen_application, w.ram, app) == 403
    hiring.reopen_application(w.hr, app)
    a = one('SELECT status, rejected_reason, closed_at FROM hiring_application WHERE id = %s', [app])
    assert (a['status'], a['rejected_reason'], a['closed_at']) == ('active', None, None)
    assert _err(hiring.reopen_application, w.hr, app) == (409, 'this application is active')


def test_offer_rejected_by_approver_and_drafter_cannot_approve():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    offer = hiring.create_offer(w.hr, app, {'designation': 'Designer', 'salary': '50000', 'joinDate': future(20)})
    from hajiri.core.errors import ForbiddenError
    from hajiri.workflow.service import DecisionContext, handler_for
    with pytest.raises(ForbiddenError) as e:
        handler_for('job_offer')(offer, 'approved', DecisionContext(w.hr.id, None))
    assert e.value.message == 'you drafted this offer; someone else must approve it'
    inst = workflow.instance_for_object('job_offer', offer)
    workflow.decide(w.hr2, inst['id'], 'reject', reason='Too high')
    assert one('SELECT status FROM hiring_offer WHERE id = %s', [offer])['status'] == 'rejected'
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'job_offer.rejected' AND user_id = %s", [w.hr.id])
    assert (n['title'], n['body']) == ('Offer rejected', 'The offer for Asha Lama was rejected: Too high.')
    assert 'Offer rejected: Too high' in [x['body'] for x in rows("SELECT body FROM hiring_event WHERE kind = 'offer'")]
    assert _err(hiring.respond_offer, w.hr, offer, {'response': 'accepted'}) == (409, 'this offer is rejected')
    # A new offer can be drafted once the earlier one is closed.
    assert hiring.create_offer(w.hr, app, {'designation': 'Designer', 'salary': '45000', 'joinDate': future(20)})


def test_hire_checks_code_probation_and_refs():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    offer = hiring.create_offer(w.hr, app, {'designation': 'Designer', 'salary': '50000', 'joinDate': future(20)})
    workflow.decide(w.hr2, workflow.instance_for_object('job_offer', offer)['id'], 'approve')
    hiring.respond_offer(w.hr, offer, {'response': 'accepted'})
    assert status(hiring.hire, w.ram, app, {'empCode': 'E77'}) == 403
    assert _err(hiring.hire, w.hr, app, {'empCode': 'E77', 'probationEnd': '2020-01-01'}) == \
        (400, 'probation end cannot be before join date')
    assert _err(hiring.hire, w.hr, app, {'empCode': 'E77', 'shiftId': '00000000-0000-7000-8000-00000000ffff'}) == \
        (400, 'shift does not exist in your company')
    emp = hiring.hire(w.hr, app, {'empCode': 'E77', 'gender': 'female', 'joinDate': future(25), 'designation': 'Senior designer'})
    e = one('SELECT gender, designation, join_date, weekly_off_days, regime FROM employee WHERE id = %s', [emp])
    assert (e['gender'], e['designation'], e['join_date'].isoformat(), list(e['weekly_off_days']), e['regime']) == \
        ('female', 'Senior designer', future(25), [6], 'ssf')
    a = one("SELECT lower(valid) AS start, employment_type FROM employee_assignment WHERE employee_id = %s", [emp])
    assert (a['start'].isoformat(), a['employment_type']) == (future(25), 'regular')
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE action = 'hire' AND row_id = %s", [emp])['n'] == 1
    assert _err(hiring.withdraw_offer, w.hr, offer) == (409, 'the candidate has already been hired')
    assert _err(hiring.submit_scorecard, w.hr, '00000000-0000-7000-8000-00000000ffff',
                {'ratings': {'Skills': 3}, 'recommendation': 'no'}) == (404, 'interview not found')


def test_application_owner_and_rating_clear():
    w = World()
    jid = _job(w)
    app = _candidate(w, jid)
    assert _err(hiring.update_application, w.hr, app, {'ownerUserId': w.other_hr.id}) == \
        (400, 'owner must be an active user in your company')
    hiring.update_application(w.hr, app, {'ownerUserId': w.ram.id, 'rating': 3})
    hiring.update_application(w.hr, app, {'rating': None})
    d = hiring.application_detail(w.hr, app)['application']
    assert d['owner_name'] == w.ram.name and d['rating'] is None
    bodies = [x['body'] for x in rows("SELECT body FROM hiring_event WHERE kind = 'rating' ORDER BY created_at, id")]
    assert bodies == ['Rated 3 of 5', 'Rating cleared']
    assert _err(hiring.add_candidate, w.hr, jid, {'name': 'Asha Lama', 'email': 'ASHA@example.com'}) == \
        (409, 'this candidate has already applied for this job')
