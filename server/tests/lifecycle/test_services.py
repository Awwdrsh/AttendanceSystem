"""Port of apps/api/test/lifecycle.test.ts at service level: onboarding, probation, resignation and exit, assets."""
from datetime import timedelta

import pytest

from hajiri.core import auth
from hajiri.core.clock import npt_today
from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.employees import service as emp
from hajiri.lifecycle import api
from hajiri.lifecycle.service import lifecycle_daily
from hajiri.workflow import services as wf
from tests.people.conftest import build_world
from tests.web.factories import PW, make_user


def _emp(id_):
    from hajiri.db.models import Employee
    return Employee.objects.get(id=id_)


def plus(n: int) -> str:
    return (npt_today() + timedelta(days=n)).isoformat()


@pytest.fixture
def world(db):
    return build_world(join_days_ago=200)


def _item(user, object_id):
    return next((i for i in wf.inbox_for(user) if i['objectId'] == str(object_id)), None)


def start_new_joiner(w):
    join = plus(5)
    new_id = emp.create_employee(w.hr, {'empCode': 'NEW1', 'nameEn': 'New Joiner', 'joinDate': join, 'managerId': w.ram_id,
                                        'branchId': w.branch_id})
    case = next(x for x in api.cases(w.hr, kind='onboarding') if x['employee_id'] == new_id)
    return new_id, case, join


# ---------------------------------------------------------------- onboarding checklists

def test_seeds_default_onboarding_and_exit_checklists(world):
    w = world
    api.ensure_defaults(w.hr)
    ts = api.templates(w.hr)
    assert sorted(t['kind'] for t in ts) == ['offboarding', 'onboarding']
    assert len(ts[0]['tasks']) > 3
    with pytest.raises(ForbiddenError, match='requires role hr or manager'):
        api.templates(w.sita)
    # Seeding is idempotent.
    api.ensure_defaults(w.hr)
    assert len(api.templates(w.hr)) == 2
    assert len(api.questions(w.hr)) == 6 and len(api.asset_types(w.hr)) == 8


def test_onboarding_starts_when_hr_adds_employee_with_due_dates_from_join(world):
    w = world
    new_id, case, join = start_new_joiner(w)
    assert case['anchor_date'].isoformat() == join
    d = api.case_detail(w.hr, case['id'])
    welcome = next(t for t in d['tasks'] if t['title'].startswith('Welcome meeting'))
    assert welcome['due_date'].isoformat() == plus(6) and welcome['assignee_type'] == 'manager'
    # The manager is told once with their own count.
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'lifecycle.onboarding.started' AND user_id = %s", [w.ram.id])
    assert n['title'] == 'Onboarding for New Joiner' and n['body'].endswith('tasks are waiting for you.')
    with pytest.raises(ConflictError, match='onboarding is already in progress for this employee'):
        api.start_onboarding(w.hr, {'employeeId': new_id})


def test_joiner_sees_own_tasks_and_only_ticks_their_own(world):
    w = world
    new_id, case, _ = start_new_joiner(w)
    joiner = make_user(w.company, roles=['employee'], employee=_emp(new_id))
    my = api.my_tasks(joiner)
    assert my and all(t['assignee_type'] == 'employee' for t in my)
    own = my[0]
    ok = api.update_task(joiner, own['id'], {'status': 'done', 'note': 'Uploaded'})
    assert ok['task']['status'] == 'done'
    d = api.case_detail(joiner, case['id'])
    hr_task = next(t for t in d['tasks'] if t['assignee_type'] == 'hr')
    assert hr_task['can_complete'] is False
    with pytest.raises(ForbiddenError, match='this task is for someone else'):
        api.update_task(joiner, hr_task['id'], {'status': 'done'})
    with pytest.raises(ForbiddenError, match='only HR can skip a task'):
        api.update_task(joiner, own['id'], {'status': 'skipped'})
    with pytest.raises(ForbiddenError, match='only HR can change due dates and owners'):
        api.update_task(joiner, own['id'], {'dueDate': npt_today().isoformat()})


def test_line_manager_sees_case_and_ticks_manager_tasks_strangers_cannot(world):
    w = world
    new_id, case, _ = start_new_joiner(w)
    assert any(c['id'] == case['id'] for c in api.cases(w.ram, kind='onboarding'))
    mt = next(t for t in api.my_tasks(w.ram) if t['assignee_type'] == 'manager' and t['employee_id'] == new_id)
    api.update_task(w.ram, mt['id'], {'status': 'done'})
    with pytest.raises(NotFoundError, match='checklist not found'):
        api.case_detail(w.sita, case['id'])
    with pytest.raises(NotFoundError):
        api.case_detail(w.foreign_hr, case['id'])
    assert api.cases(w.foreign_hr) == []


def test_comments_notify_and_checklist_closes_when_everything_done(world):
    w = world
    new_id, case, _ = start_new_joiner(w)
    joiner = make_user(w.company, roles=['employee'], employee=_emp(new_id))
    d = api.case_detail(w.hr, case['id'])
    hr_task = next(t for t in d['tasks'] if t['assignee_type'] == 'hr' and t['status'] == 'open')
    api.add_comment(joiner, hr_task['id'], {'body': 'When will my account be ready?'})
    assert len(api.comments(w.hr, hr_task['id'])) == 1
    assert one("SELECT title FROM notification_outbox WHERE template = 'lifecycle.task.comment' AND user_id = %s", [w.hr.id])['title'] == \
        f'Comment on "{hr_task["title"]}"'
    last = {'caseCompleted': False}
    for t in d['tasks']:
        if t['status'] != 'open':
            continue
        last = api.update_task(w.hr, t['id'], {'status': 'done'})
    assert last['caseCompleted'] is True
    assert api.case_detail(w.hr, case['id'])['case']['status'] == 'completed'
    # Re-opening a task re-opens the checklist.
    api.update_task(w.hr, d['tasks'][0]['id'], {'status': 'open'})
    assert api.case_detail(w.hr, case['id'])['case']['status'] == 'active'


def test_hr_edits_templates_named_person_needs_that_person(world):
    w = world
    api.ensure_defaults(w.hr)
    with pytest.raises(BadRequestError):
        api.save_template(w.hr, None, {'kind': 'onboarding', 'name': 'Engineering', 'tasks': [{'title': 'Repo access', 'assigneeType': 'user',
                                                                                               'dueOffsetDays': 0}]})
    tid = api.save_template(w.hr, None, {'kind': 'onboarding', 'name': 'Engineering', 'tasks': [
        {'title': 'Repo access', 'assigneeType': 'user', 'assigneeUserId': w.ram.id, 'dueOffsetDays': 0, 'link': '/me/profile'}]})
    with pytest.raises(BadRequestError):
        api.save_template(w.hr, tid, {'kind': 'onboarding', 'name': 'Engineering', 'tasks': [
            {'title': 'x', 'assigneeType': 'hr', 'link': 'https://evil.example'}]})
    with pytest.raises(ConflictError, match='a checklist with this name already exists'):
        api.save_template(w.hr, None, {'kind': 'onboarding', 'name': 'Engineering', 'tasks': []})
    with pytest.raises(BadRequestError, match='the chosen person is not an active user in your company'):
        api.save_template(w.hr, tid, {'kind': 'onboarding', 'name': 'Engineering', 'tasks': [
            {'title': 'x', 'assigneeType': 'user', 'assigneeUserId': w.foreign_hr.id}]})
    t = api.get_template(w.hr, tid)
    assert t['tasks'][0]['link'] == '/me/profile' and t['tasks'][0]['assignee_user_id'] == w.ram.id


def test_cancel_onboarding_and_add_task(world):
    w = world
    new_id, case, _ = start_new_joiner(w)
    t = api.add_task(w.hr, case['id'], {'title': 'Order business cards', 'assigneeType': 'manager'})
    assert t['title'] == 'Order business cards'
    with pytest.raises(BadRequestError, match='choose the person for this task'):
        api.add_task(w.hr, case['id'], {'title': 'x', 'assigneeType': 'user'})
    api.cancel_case(w.hr, case['id'])
    with pytest.raises(ConflictError, match='checklist is cancelled'):
        api.cancel_case(w.hr, case['id'])
    with pytest.raises(ConflictError, match='this checklist was cancelled'):
        api.update_task(w.hr, t['id'], {'status': 'done'})


# ---------------------------------------------------------------- probation

def test_probation_list_reminders_review_and_confirmation(world):
    w = world
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(10), w.sita_id])
    assert w.sita_id in [e['id'] for e in api.probation(w.ram)]
    with pytest.raises(ForbiddenError):
        api.probation(w.sita)

    # Reminds the manager once per threshold.
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(7), w.sita_id])
    a = lifecycle_daily()
    b = lifecycle_daily()
    assert a['probationReminders'] == 1 and b['probationReminders'] == 0
    n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'lifecycle.probation.ending' AND channel = 'in_app'",
            [w.ram.id])
    assert n['n'] == 1

    # Validates the review and stops people reviewing themselves.
    with pytest.raises(ForbiddenError, match='you cannot review your own probation'):
        api.submit_review(w.ram, {'employeeId': w.ram_id, 'rating': 5, 'recommendation': 'confirm', 'comments': 'I am great'})
    with pytest.raises(BadRequestError, match='the new end date must be after the current probation end'):
        api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 3, 'recommendation': 'extend', 'extendTo': npt_today().isoformat(),
                                  'comments': 'Needs more time'})
    with pytest.raises(ForbiddenError):
        api.submit_review(w.sita, {'employeeId': w.sita_id, 'rating': 5, 'recommendation': 'confirm', 'comments': 'ok ok'})

    # Manager recommends confirmation, HR approves, the employee is confirmed.
    rid = api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm', 'comments': 'Reliable and quick to learn.'})
    with pytest.raises(ConflictError, match='a probation review is already waiting for approval'):
        api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm', 'comments': 'Again'})
    item = _item(w.hr, rid)
    assert item['summary']['headline'].startswith('Probation review: confirm')
    with pytest.raises(ForbiddenError):
        wf.decide(w.ram, item['instanceId'], 'approve')
    assert wf.decide(w.hr, item['instanceId'], 'approve') == 'approved'
    assert one('SELECT confirmed_on FROM employee WHERE id = %s', [w.sita_id])['confirmed_on'].isoformat() == plus(7)
    s = api.employee_summary(w.hr, w.sita_id)
    assert len(s['probation']['confirmations']) == 1
    assert rows("SELECT kind FROM career_event WHERE employee_id = %s AND kind = 'confirmation'", [w.sita_id])
    with pytest.raises(ConflictError, match='already confirmed on'):
        api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm', 'comments': 'Again'})
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'lifecycle.probation_review.approved' AND user_id = %s", [w.ram.id])
    assert n['title'] == 'Probation review for Sita Sharma' and n['body'].startswith('Your review was approved: confirmed from ')


def test_probation_extend_and_terminate_outcomes(world):
    w = world
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(10), w.sita_id])
    rid = api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 3, 'recommendation': 'extend', 'extendTo': plus(60), 'comments': 'More time'})
    wf.decide(w.hr, _item(w.hr, rid)['instanceId'], 'approve')
    assert one('SELECT probation_end FROM employee WHERE id = %s', [w.sita_id])['probation_end'].isoformat() == plus(60)
    rid = api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 1, 'recommendation': 'terminate', 'lastWorkingDay': plus(15),
                                    'comments': 'Not a fit'})
    wf.decide(w.hr, _item(w.hr, rid)['instanceId'], 'reject', reason='Give it time')
    n = one("SELECT body FROM notification_outbox WHERE template = 'lifecycle.probation_review.rejected' AND user_id = %s", [w.ram.id])
    assert n['body'] == 'Your review was not approved: Give it time.'
    rid = api.submit_review(w.ram, {'employeeId': w.sita_id, 'rating': 1, 'recommendation': 'terminate', 'lastWorkingDay': plus(15),
                                    'comments': 'Not a fit'})
    wf.decide(w.hr, _item(w.hr, rid)['instanceId'], 'approve')
    x = one("SELECT kind, status, accepted_lwd, reason_category FROM employee_exit WHERE employee_id = %s", [w.sita_id])
    assert x['kind'] == 'termination' and x['status'] == 'accepted' and x['accepted_lwd'].isoformat() == plus(15)
    assert x['reason_category'] == 'Probation not successful'


def test_hr_confirms_directly(world):
    w = world
    with pytest.raises(BadRequestError, match='confirmation cannot be before the join date'):
        api.confirm(w.hr, {'employeeId': w.sita_id, 'confirmedOn': '2000-01-01'})
    api.confirm(w.hr, {'employeeId': w.sita_id, 'confirmedOn': npt_today().isoformat()})
    with pytest.raises(ConflictError, match='already confirmed on'):
        api.confirm(w.hr, {'employeeId': w.sita_id, 'confirmedOn': npt_today().isoformat()})


# ---------------------------------------------------------------- resignation and exit

def test_resignation_exit_interview_assets_and_completion(world):
    w = world
    with pytest.raises(BadRequestError, match='the last working day cannot be in the past'):
        api.resign(w.sita, {'proposedLastWorkingDay': plus(-1)})
    r = api.resign(w.sita, {'proposedLastWorkingDay': plus(10), 'reasonCategory': 'Further studies', 'reason': 'Masters abroad'})
    assert r['noticeDays'] == 30 and r['shortfallDays'] == 20
    exit_id = r['id']
    with pytest.raises(ConflictError, match='you already have a resignation in progress'):
        api.resign(w.sita, {'proposedLastWorkingDay': plus(30)})
    assert api.my_exits(w.sita)['exits'][0]['status'] == 'pending'

    # Manager then HR approve; the exit date is set and clearance starts.
    mi = _item(w.ram, exit_id)
    assert 'short by 20 days' in mi['summary']['detail']
    assert wf.decide(w.ram, mi['instanceId'], 'approve') == 'pending'
    hi = _item(w.hr, exit_id)
    assert wf.decide(w.hr, hi['instanceId'], 'approve') == 'approved'
    x = api.exit_detail(w.hr, exit_id)
    assert x['exit']['status'] == 'accepted' and x['exit']['accepted_lwd'].isoformat() == plus(10)
    assert x['exit']['tasks_total'] > 3 and len(x['settlement']['steps']) > 0
    e = one('SELECT exit_date, status FROM employee WHERE id = %s', [w.sita_id])
    assert e['exit_date'].isoformat() == plus(10) and e['status'] == 'active'
    with pytest.raises(ConflictError, match='your resignation was already accepted; ask HR to cancel it'):
        api.withdraw_exit(w.sita, exit_id)
    assert api.exit_detail(w.sita, exit_id)['settlement'] is None
    with pytest.raises(NotFoundError):
        api.exit_detail(w.foreign_hr, exit_id)

    # The exit interview is confidential to HR and recorded once.
    qs = api.questions(w.sita)
    answers = [{'questionId': q['id'], 'answer': 4 if q['kind'] == 'rating' else q['options'][0] if q['kind'] == 'choice' else 'Good team'} for q in qs]
    with pytest.raises(BadRequestError, match='needs one of the listed options'):
        api.record_interview(w.sita, exit_id, {'answers': [{'questionId': qs[0]['id'], 'answer': 'Not an option'}]})
    with pytest.raises(ForbiddenError, match='only the leaver or HR can record the exit interview'):
        api.record_interview(w.ram, exit_id, {'answers': answers})
    api.record_interview(w.sita, exit_id, {'answers': answers})
    with pytest.raises(ConflictError, match='the exit interview has already been recorded'):
        api.record_interview(w.sita, exit_id, {'answers': answers})
    with pytest.raises(ForbiddenError):
        api.interview(w.sita, exit_id)
    with pytest.raises(ForbiddenError):
        api.interview(w.ram, exit_id)
    assert api.interview(w.hr, exit_id)['answers'][3]['answer'] == 'Good team'
    assert b'Good team' not in bytes(one('SELECT answers_enc FROM exit_interview WHERE exit_id = %s', [exit_id])['answers_enc'])
    # Recording the interview ticks the "Exit interview" clearance task.
    assert one("""SELECT t.status FROM lifecycle_task t JOIN lifecycle_case c ON c.id = t.case_id
                   WHERE c.exit_id = %s AND t.title = 'Exit interview'""", [exit_id])['status'] == 'done'

    # Assets are assigned and block the exit until returned.
    types = api.asset_types(w.hr)
    asset_id = api.create_asset(w.hr, {'tag': 'LT-001', 'name': 'ThinkPad T14', 'typeId': types[0]['id'], 'serial': 'PF123', 'cost': '150000.00'})
    with pytest.raises(ConflictError, match='an asset with this tag already exists'):
        api.create_asset(w.hr, {'tag': 'LT-001', 'name': 'Dup'})
    api.assign_asset(w.hr, asset_id, {'employeeId': w.sita_id})
    with pytest.raises(ConflictError, match='this asset is already with someone'):
        api.assign_asset(w.hr, asset_id, {'employeeId': w.ram_id})
    with pytest.raises(NotFoundError):
        api.assign_asset(w.foreign_hr, asset_id, {'employeeId': w.sita_id})
    assert len(api.my_assets(w.sita)) == 1
    assert len(api.assets(w.ram, {'employeeId': w.sita_id})) == 1
    with pytest.raises(ForbiddenError, match='choose one of your team members'):
        api.assets(w.ram)
    with pytest.raises(ForbiddenError):
        api.create_asset(w.sita, {'tag': 'X', 'name': 'X'})

    # Completion waits for the last working day, the assets and the clearance.
    with pytest.raises(ConflictError, match='complete the exit after it'):
        api.complete(w.hr, exit_id, {})
    lwd = plus(-1)
    api.update_exit(w.hr, exit_id, {'lastWorkingDay': lwd})
    with pytest.raises(ConflictError, match='asset'):
        api.complete(w.hr, exit_id, {'skipOpenTasks': True})
    api.return_asset(w.hr, asset_id, {'condition': 'good'})
    assert lifecycle_daily()['exitsFlagged'] == 1
    with pytest.raises(ConflictError, match='clearance'):
        api.complete(w.hr, exit_id, {})
    api.complete(w.hr, exit_id, {'skipOpenTasks': True})
    e = one('SELECT exit_date, status FROM employee WHERE id = %s', [w.sita_id])
    assert e['exit_date'].isoformat() == lwd and e['status'] == 'exited'
    assert rows("SELECT 1 FROM career_event WHERE employee_id = %s AND kind = 'separation' AND status = 'applied'", [w.sita_id])
    # The leaver can no longer sign in after the last working day.
    with pytest.raises(Exception):
        auth.login(w.sita.email, PW)


def test_hr_records_exit_and_daily_job_completes_it(world):
    w = world
    new_id = emp.create_employee(w.hr, {'empCode': 'TMP1', 'nameEn': 'Contract Worker', 'joinDate': plus(-100)})
    with pytest.raises(ForbiddenError):
        api.create_exit(w.ram, {'employeeId': new_id, 'kind': 'contract_end', 'lastWorkingDay': plus(-2)})
    xid = api.create_exit(w.hr, {'employeeId': new_id, 'kind': 'contract_end', 'lastWorkingDay': plus(-2), 'reasonCategory': 'Contract ended'})
    detail = api.exit_detail(w.hr, xid)
    case = api.case_detail(w.hr, detail['exit']['case_id'])
    for t in case['tasks']:
        api.update_task(w.hr2, t['id'], {'status': 'done'})
    assert one('SELECT status FROM lifecycle_case WHERE id = %s', [detail['exit']['case_id']])['status'] == 'active'
    assert lifecycle_daily()['exitsCompleted'] == 1
    assert one('SELECT status FROM employee WHERE id = %s', [new_id])['status'] == 'exited'
    with pytest.raises(ForbiddenError, match='another HR user must record your own exit'):
        hr_emp = emp.create_employee(w.hr, {'empCode': 'HR9', 'nameEn': 'HR Person', 'joinDate': plus(-100)})
        hr_user = make_user(w.company, roles=['hr'], employee=_emp(hr_emp))
        api.create_exit(hr_user, {'employeeId': hr_emp, 'kind': 'resignation', 'lastWorkingDay': plus(5)})


def test_employee_withdraws_pending_resignation_hr_cancels_accepted_exit(world):
    w = world
    r = api.resign(w.ram, {'proposedLastWorkingDay': plus(40)})
    api.withdraw_exit(w.ram, r['id'])
    assert one("SELECT state FROM workflow_instance WHERE object_type = 'resignation' AND object_id = %s", [r['id']])['state'] == 'cancelled'
    xid = api.create_exit(w.hr, {'employeeId': w.ram_id, 'kind': 'termination', 'lastWorkingDay': plus(20)})
    with pytest.raises(BadRequestError):
        api.cancel_exit(w.hr, xid, {'reason': 'no'})
    api.cancel_exit(w.hr, xid, {'reason': 'Decision reversed'})
    assert one('SELECT exit_date FROM employee WHERE id = %s', [w.ram_id])['exit_date'] is None
    assert one('SELECT status FROM lifecycle_case WHERE exit_id = %s', [xid])['status'] == 'cancelled'
    with pytest.raises(ConflictError, match='exit is cancelled'):
        api.cancel_exit(w.hr, xid, {'reason': 'Again please'})


def test_rejected_resignation_tells_the_employee(world):
    w = world
    r = api.resign(w.sita, {'proposedLastWorkingDay': plus(40)})
    wf.decide(w.ram, _item(w.ram, r['id'])['instanceId'], 'reject', reason='Please stay')
    assert one('SELECT status FROM employee_exit WHERE id = %s', [r['id']])['status'] == 'rejected'
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'lifecycle.exit.rejected' AND user_id = %s", [w.sita.id])
    assert n['title'] == 'Resignation not accepted' and n['body'] == 'Please stay'


def test_settings_policy(world):
    w = world
    assert api.get_settings(w.sita) == {'noticeDays': 30, 'autoStartOnboarding': True, 'probationReminderDays': [14, 7], 'selfExitInterview': True}
    with pytest.raises(ForbiddenError):
        api.save_settings(w.sita, {'noticeDays': 10})
    p = api.save_settings(w.hr, {'noticeDays': 15, 'autoStartOnboarding': False, 'probationReminderDays': [10], 'selfExitInterview': False})
    assert api.get_settings(w.sita) == p
    # Auto-start off: adding an employee starts no checklist.
    new_id = emp.create_employee(w.hr, {'empCode': 'NOAUTO', 'nameEn': 'No Auto'})
    assert not rows('SELECT 1 FROM lifecycle_case WHERE employee_id = %s', [new_id])
    api.ensure_defaults(w.sita)
    r = api.resign(w.sita, {'proposedLastWorkingDay': plus(10)})
    assert r['noticeDays'] == 15 and r['shortfallDays'] == 5
    with pytest.raises(ForbiddenError, match='HR will hold your exit interview in person'):
        api.record_interview(w.sita, r['id'], {'answers': [{'questionId': api.questions(w.sita)[0]['id'], 'answer': 'Other'}]})
