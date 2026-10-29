"""
HTTP-level port of apps/api/test/lifecycle.test.ts: the /api/v1/lifecycle routes the React app calls (settings,
templates, onboarding cases and tasks, my tasks, probation, resignation and exits, the exit interview, assets), their
status codes and JSON shapes, and company isolation (another company's ids are 404, its lists empty).
"""
from datetime import timedelta

import pytest

from hajiri.core.clock import npt_today
from hajiri.lifecycle.service import lifecycle_daily
from hajiri.workflow import services as wf
from tests.people.conftest import Api, build_world
from tests.support.sql import execute, one
from tests.web.factories import make_user


def plus(n: int) -> str:
    return (npt_today() + timedelta(days=n)).isoformat()


def _emp(id_):
    from hajiri.db.models import Employee
    return Employee.objects.get(pk=id_)


def _item(user, object_id):
    return next((i for i in wf.inbox_for(user) if i['objectId'] == str(object_id)), None)


@pytest.fixture
def w(db):
    return build_world(join_days_ago=200)


@pytest.fixture
def a(w):
    return {k: Api(getattr(w, k)) for k in ('hr', 'hr2', 'sita', 'ram', 'foreign_hr')}


def start_new_joiner(w, a):
    join = plus(5)
    c = a['hr'].post('/employees', {'empCode': 'NEW1', 'nameEn': 'New Joiner', 'joinDate': join, 'managerId': w.ram_id,
                                    'branchId': w.branch_id})
    assert c.status_code == 201
    new_id = c.json()['id']
    cases = a['hr'].get('/lifecycle/cases', kind='onboarding')
    assert cases.status_code == 200
    case = next(x for x in cases.json()['cases'] if x['employee_id'] == new_id)
    return new_id, case, join


# ---------------------------------------------------------------- settings and templates

def test_settings(w, a):
    r = a['sita'].get('/lifecycle/settings')
    assert r.status_code == 200 and set(r.json()) == {'policy'} and r.json()['policy']['noticeDays'] == 30
    assert a['sita'].put('/lifecycle/settings', {'noticeDays': 10}).status_code == 403
    r = a['hr'].put('/lifecycle/settings', {**a['hr'].get('/lifecycle/settings').json()['policy'], 'noticeDays': 45})
    assert r.status_code == 200 and r.json()['policy']['noticeDays'] == 45
    assert a['foreign_hr'].get('/lifecycle/settings').json()['policy']['noticeDays'] == 30


def test_seeds_default_checklists(w, a):
    r = a['hr'].get('/lifecycle/templates')
    assert r.status_code == 200
    ts = r.json()['templates']
    assert sorted(t['kind'] for t in ts) == ['offboarding', 'onboarding'] and len(ts[0]['tasks']) > 3
    assert a['sita'].get('/lifecycle/templates').status_code == 403
    assert [t['kind'] for t in a['hr'].get('/lifecycle/templates', kind='onboarding').json()['templates']] == ['onboarding']
    assert a['hr'].get('/lifecycle/templates', kind='bogus').status_code == 400


def test_hr_edits_templates_named_person_needs_that_person(w, a):
    bad = a['hr'].post('/lifecycle/templates', {'kind': 'onboarding', 'name': 'Engineering',
                                                'tasks': [{'title': 'Repo access', 'assigneeType': 'user', 'dueOffsetDays': 0}]})
    assert bad.status_code == 400
    ok = a['hr'].post('/lifecycle/templates', {'kind': 'onboarding', 'name': 'Engineering', 'tasks': [
        {'title': 'Repo access', 'assigneeType': 'user', 'assigneeUserId': w.ram.id, 'dueOffsetDays': 0, 'link': '/me/profile'}]})
    assert ok.status_code == 201 and set(ok.json()) == {'id'}
    tid = ok.json()['id']
    bad_link = a['hr'].put(f'/lifecycle/templates/{tid}', {'kind': 'onboarding', 'name': 'Engineering',
                                                           'tasks': [{'title': 'x', 'assigneeType': 'hr', 'link': 'https://evil.example'}]})
    assert bad_link.status_code == 400
    put = a['hr'].put(f'/lifecycle/templates/{tid}', {'kind': 'onboarding', 'name': 'Engineering 2', 'tasks': []})
    assert put.status_code == 200 and put.json() == {'id': tid}
    dup = a['hr'].post('/lifecycle/templates', {'kind': 'onboarding', 'name': 'Engineering 2', 'tasks': []})
    assert dup.status_code == 409
    assert a['foreign_hr'].put(f'/lifecycle/templates/{tid}', {'kind': 'onboarding', 'name': 'X', 'tasks': []}).status_code == 404
    assert a['ram'].post('/lifecycle/templates', {'kind': 'onboarding', 'name': 'M', 'tasks': []}).status_code == 403


# ---------------------------------------------------------------- onboarding

def test_onboarding_starts_with_due_dates_from_join_and_second_is_refused(w, a):
    new_id, case, join = start_new_joiner(w, a)
    assert case['anchor_date'] == join
    d = a['hr'].get(f"/lifecycle/cases/{case['id']}")
    assert d.status_code == 200 and set(d.json()) == {'case', 'employee', 'tasks'}
    welcome = next(t for t in d.json()['tasks'] if t['title'].startswith('Welcome meeting'))
    assert welcome['due_date'] == plus(6) and welcome['assignee_type'] == 'manager'
    r = a['hr'].post('/lifecycle/cases', {'employeeId': new_id})
    assert r.status_code == 409
    assert a['ram'].post('/lifecycle/cases', {'employeeId': new_id}).status_code == 403
    assert a['foreign_hr'].post('/lifecycle/cases', {'employeeId': new_id}).status_code == 404


def test_joiner_sees_own_tasks_and_only_ticks_their_own(w, a):
    new_id, case, _ = start_new_joiner(w, a)
    joiner = Api(make_user(w.company, roles=['employee'], employee=_emp(new_id)))
    my = joiner.get('/lifecycle/my-tasks')
    assert my.status_code == 200
    tasks = my.json()['tasks']
    assert tasks and all(t['assignee_type'] == 'employee' for t in tasks)
    own = tasks[0]
    ok = joiner.patch(f"/lifecycle/tasks/{own['id']}", {'status': 'done', 'note': 'Uploaded'})
    assert ok.status_code == 200 and ok.json()['task']['status'] == 'done' and ok.json()['caseCompleted'] is False
    d = joiner.get(f"/lifecycle/cases/{case['id']}").json()
    hr_task = next(t for t in d['tasks'] if t['assignee_type'] == 'hr')
    assert hr_task['can_complete'] is False
    r = joiner.patch(f"/lifecycle/tasks/{hr_task['id']}", {'status': 'done'})
    assert r.status_code == 403 and r.json() == {'error': 'this task is for someone else'}
    assert joiner.patch(f"/lifecycle/tasks/{own['id']}", {'status': 'skipped'}).status_code == 403
    assert joiner.patch(f"/lifecycle/tasks/{own['id']}", {'dueDate': plus(0)}).status_code == 403


def test_line_manager_sees_case_strangers_and_other_companies_cannot(w, a):
    new_id, case, _ = start_new_joiner(w, a)
    assert any(c['id'] == case['id'] for c in a['ram'].get('/lifecycle/cases', kind='onboarding').json()['cases'])
    mt = next(t for t in a['ram'].get('/lifecycle/my-tasks').json()['tasks']
              if t['assignee_type'] == 'manager' and t['employee_id'] == new_id)
    assert a['ram'].patch(f"/lifecycle/tasks/{mt['id']}", {'status': 'done'}).status_code == 200
    assert a['sita'].get(f"/lifecycle/cases/{case['id']}").status_code == 404
    assert a['foreign_hr'].get(f"/lifecycle/cases/{case['id']}").status_code == 404
    assert a['foreign_hr'].get('/lifecycle/cases').json() == {'cases': []}
    assert a['foreign_hr'].patch(f"/lifecycle/tasks/{mt['id']}", {'status': 'open'}).status_code == 404
    assert a['foreign_hr'].get(f"/lifecycle/tasks/{mt['id']}/comments").status_code == 404
    assert a['foreign_hr'].post(f"/lifecycle/cases/{case['id']}/cancel").status_code == 404
    assert a['hr'].get('/lifecycle/cases/not-a-uuid').status_code == 400


def test_comments_and_checklist_closes_when_everything_done(w, a):
    new_id, case, _ = start_new_joiner(w, a)
    joiner = Api(make_user(w.company, roles=['employee'], employee=_emp(new_id)))
    d = a['hr'].get(f"/lifecycle/cases/{case['id']}").json()
    hr_task = next(t for t in d['tasks'] if t['assignee_type'] == 'hr' and t['status'] == 'open')
    c = joiner.post(f"/lifecycle/tasks/{hr_task['id']}/comments", {'body': 'When will my account be ready?'})
    assert c.status_code == 201 and c.json()['comment']['body'] == 'When will my account be ready?'
    lst = a['hr'].get(f"/lifecycle/tasks/{hr_task['id']}/comments")
    assert len(lst.json()['comments']) == 1 and lst.json()['comments'][0]['author_name']
    assert joiner.post(f"/lifecycle/tasks/{hr_task['id']}/comments", {'body': ' '}).status_code == 400
    last = {'caseCompleted': False}
    for t in d['tasks']:
        if t['status'] != 'open':
            continue
        r = a['hr'].patch(f"/lifecycle/tasks/{t['id']}", {'status': 'done'})
        assert r.status_code == 200
        last = r.json()
    assert last['caseCompleted'] is True
    assert a['hr'].get(f"/lifecycle/cases/{case['id']}").json()['case']['status'] == 'completed'


def test_hr_adds_a_task_and_cancels_a_case(w, a):
    new_id, case, _ = start_new_joiner(w, a)
    r = a['hr'].post(f"/lifecycle/cases/{case['id']}/tasks", {'title': 'Laptop', 'assigneeType': 'hr'})
    assert r.status_code == 201 and r.json()['task']['title'] == 'Laptop'
    assert a['hr'].post(f"/lifecycle/cases/{case['id']}/tasks", {'title': 'X', 'assigneeType': 'user'}).status_code == 400
    assert a['ram'].post(f"/lifecycle/cases/{case['id']}/tasks", {'title': 'X', 'assigneeType': 'hr'}).status_code == 403
    c = a['hr'].post(f"/lifecycle/cases/{case['id']}/cancel")
    assert c.status_code == 200 and c.json() == {'ok': True}
    assert a['hr'].post(f"/lifecycle/cases/{case['id']}/cancel").status_code == 409


# ---------------------------------------------------------------- probation

def test_probation_list_review_and_confirmation(w, a):
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(10), w.sita_id])
    r = a['ram'].get('/lifecycle/probation')
    assert r.status_code == 200 and w.sita_id in [e['id'] for e in r.json()['employees']]
    assert a['sita'].get('/lifecycle/probation').status_code == 403
    assert a['ram'].get('/lifecycle/probation', within='0').status_code == 400
    assert a['foreign_hr'].get('/lifecycle/probation').json() == {'employees': []}
    execute('UPDATE employee SET probation_end = %s WHERE id = %s', [plus(7), w.sita_id])
    me = a['ram'].post('/lifecycle/probation/reviews', {'employeeId': w.ram_id, 'rating': 5, 'recommendation': 'confirm', 'comments': 'I am great'})
    assert me.status_code == 403
    ext = a['ram'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 3, 'recommendation': 'extend', 'extendTo': plus(0),
                                                         'comments': 'Needs more time'})
    assert ext.status_code == 400
    assert a['sita'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 5, 'recommendation': 'confirm',
                                                           'comments': 'ok ok'}).status_code == 403
    assert a['foreign_hr'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 5, 'recommendation': 'confirm',
                                                                 'comments': 'ok ok'}).status_code == 404
    r = a['ram'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm',
                                                       'comments': 'Reliable and quick to learn.'})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    dup = a['ram'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm', 'comments': 'Again'})
    assert dup.status_code == 409
    item = _item(w.hr, r.json()['id'])
    assert item['summary']['headline'].startswith('Probation review: confirm')
    assert wf.decide(w.hr, item['instanceId'], 'approve') == 'approved'
    assert one('SELECT confirmed_on::text AS d FROM employee WHERE id = %s', [w.sita_id])['d'] == plus(7)
    s = a['hr'].get(f'/lifecycle/employees/{w.sita_id}/summary')
    assert s.status_code == 200 and set(s.json()) == {'employee', 'cases', 'probation', 'exits', 'assets'}
    assert len(s.json()['probation']['confirmations']) == 1
    assert a['foreign_hr'].get(f'/lifecycle/employees/{w.sita_id}/summary').status_code == 404
    again = a['ram'].post('/lifecycle/probation/reviews', {'employeeId': w.sita_id, 'rating': 4, 'recommendation': 'confirm', 'comments': 'Again'})
    assert again.status_code == 409


def test_hr_confirms_directly(w, a):
    assert a['ram'].post('/lifecycle/probation/confirm', {'employeeId': w.sita_id, 'confirmedOn': plus(0)}).status_code == 403
    r = a['hr'].post('/lifecycle/probation/confirm', {'employeeId': w.sita_id, 'confirmedOn': plus(0)})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    assert a['hr'].post('/lifecycle/probation/confirm', {'employeeId': w.sita_id, 'confirmedOn': plus(0)}).status_code == 409
    assert a['foreign_hr'].post('/lifecycle/probation/confirm', {'employeeId': w.ram_id, 'confirmedOn': plus(0)}).status_code == 404


# ---------------------------------------------------------------- resignation and exit

def test_resignation_to_completed_exit(w, a):
    past = a['sita'].post('/lifecycle/exits/resign', {'proposedLastWorkingDay': plus(-1)})
    assert past.status_code == 400
    r = a['sita'].post('/lifecycle/exits/resign', {'proposedLastWorkingDay': plus(10), 'reasonCategory': 'Further studies', 'reason': 'Masters abroad'})
    assert r.status_code == 201 and r.json()['noticeDays'] == 30 and r.json()['shortfallDays'] == 20
    exit_id = r.json()['id']
    assert a['sita'].post('/lifecycle/exits/resign', {'proposedLastWorkingDay': plus(30)}).status_code == 409
    mine = a['sita'].get('/lifecycle/exits/mine')
    assert set(mine.json()) == {'exits', 'policy'} and mine.json()['exits'][0]['status'] == 'pending'

    # Manager then HR approve: the exit date is set and clearance starts.
    mi = _item(w.ram, exit_id)
    assert 'short by 20 days' in mi['summary']['detail']
    wf.decide(w.ram, mi['instanceId'], 'approve')
    wf.decide(w.hr, _item(w.hr, exit_id)['instanceId'], 'approve')
    x = a['hr'].get(f'/lifecycle/exits/{exit_id}')
    assert x.status_code == 200 and set(x.json()) == {'exit', 'blockers', 'settlement'}
    assert x.json()['exit']['status'] == 'accepted' and x.json()['exit']['accepted_lwd'] == plus(10)
    assert x.json()['exit']['tasks_total'] > 3 and len(x.json()['settlement']['steps']) > 0
    assert one('SELECT exit_date::text AS d, status FROM employee WHERE id = %s', [w.sita_id]) == {'d': plus(10), 'status': 'active'}
    assert a['sita'].post(f'/lifecycle/exits/{exit_id}/withdraw').status_code == 409
    assert a['sita'].get(f'/lifecycle/exits/{exit_id}').json()['settlement'] is None
    assert a['foreign_hr'].get(f'/lifecycle/exits/{exit_id}').status_code == 404
    assert a['foreign_hr'].get('/lifecycle/exits').json() == {'exits': []}
    assert exit_id in [e['id'] for e in a['hr'].get('/lifecycle/exits', status='open').json()['exits']]
    assert a['sita'].get('/lifecycle/exits').status_code == 403

    # The exit interview is confidential to HR and recorded once.
    qs = a['sita'].get('/lifecycle/exit-questions').json()['questions']
    answers = [{'questionId': q['id'], 'answer': 4 if q['kind'] == 'rating' else q['options'][0] if q['kind'] == 'choice' else 'Good team'}
               for q in qs]
    assert a['sita'].post(f'/lifecycle/exits/{exit_id}/interview', {'answers': [{'questionId': qs[0]['id'], 'answer': 'Not an option'}]}).status_code == 400
    assert a['ram'].post(f'/lifecycle/exits/{exit_id}/interview', {'answers': answers}).status_code == 403
    rec = a['sita'].post(f'/lifecycle/exits/{exit_id}/interview', {'answers': answers})
    assert rec.status_code == 201 and rec.json() == {'ok': True}
    assert a['sita'].post(f'/lifecycle/exits/{exit_id}/interview', {'answers': answers}).status_code == 409
    assert a['sita'].get(f'/lifecycle/exits/{exit_id}/interview').status_code == 403
    assert a['ram'].get(f'/lifecycle/exits/{exit_id}/interview').status_code == 403
    read = a['hr'].get(f'/lifecycle/exits/{exit_id}/interview')
    assert read.json()['interview']['answers'][3]['answer'] == 'Good team'
    raw = bytes(one('SELECT answers_enc FROM exit_interview WHERE exit_id = %s', [exit_id])['answers_enc'])
    assert 'Good team' not in raw.decode('latin1')

    # Assets are assigned and block the exit until returned.
    types = a['hr'].get('/lifecycle/asset-types').json()['types']
    asset = a['hr'].post('/lifecycle/assets', {'tag': 'LT-001', 'name': 'ThinkPad T14', 'typeId': types[0]['id'], 'serial': 'PF123', 'cost': '150000.00'})
    assert asset.status_code == 201
    asset_id = asset.json()['id']
    assert a['hr'].post('/lifecycle/assets', {'tag': 'LT-001', 'name': 'Dup'}).status_code == 409
    assert a['hr'].post(f'/lifecycle/assets/{asset_id}/assign', {'employeeId': w.sita_id}).json() == {'ok': True}
    assert a['hr'].post(f'/lifecycle/assets/{asset_id}/assign', {'employeeId': w.ram_id}).status_code == 409
    assert a['foreign_hr'].post(f'/lifecycle/assets/{asset_id}/assign', {'employeeId': w.sita_id}).status_code == 404
    assert len(a['sita'].get('/lifecycle/assets/mine').json()['assets']) == 1
    assert len(a['ram'].get('/lifecycle/assets', employeeId=w.sita_id).json()['assets']) == 1
    assert a['ram'].get('/lifecycle/assets').status_code == 403
    assert a['sita'].post('/lifecycle/assets', {'tag': 'X', 'name': 'X'}).status_code == 403
    assert [h['employee_id'] for h in a['hr'].get(f'/lifecycle/assets/{asset_id}/history').json()['history']] == [w.sita_id]
    assert a['foreign_hr'].get(f'/lifecycle/assets/{asset_id}/history').json() == {'history': []}

    # Completion waits for the last working day, the assets and the clearance.
    sita_session = a['sita']
    assert a['hr'].post(f'/lifecycle/exits/{exit_id}/complete', {}).status_code == 409
    lwd = plus(-1)
    assert a['hr'].patch(f'/lifecycle/exits/{exit_id}', {'lastWorkingDay': lwd}).json() == {'ok': True}
    blocked = a['hr'].post(f'/lifecycle/exits/{exit_id}/complete', {'skipOpenTasks': True})
    assert blocked.status_code == 409 and 'asset' in blocked.json()['error']
    assert a['hr'].post(f'/lifecycle/assets/{asset_id}/return', {'condition': 'good'}).json() == {'ok': True}
    assert lifecycle_daily()['exitsFlagged'] == 1
    tasks_block = a['hr'].post(f'/lifecycle/exits/{exit_id}/complete', {})
    assert tasks_block.status_code == 409 and 'clearance' in tasks_block.json()['error']
    assert a['hr'].post(f'/lifecycle/exits/{exit_id}/complete', {'skipOpenTasks': True}).json() == {'ok': True}
    assert one('SELECT exit_date::text AS d, status FROM employee WHERE id = %s', [w.sita_id]) == {'d': lwd, 'status': 'exited'}
    # The leaver's session no longer works after the last working day.
    assert sita_session.get('/lifecycle/my-tasks').status_code == 401


def test_hr_records_an_exit_and_the_daily_job_completes_it(w, a):
    c = a['hr'].post('/employees', {'empCode': 'TMP1', 'nameEn': 'Contract Worker', 'joinDate': plus(-100)})
    eid = c.json()['id']
    assert a['ram'].post('/lifecycle/exits', {'employeeId': eid, 'kind': 'contract_end', 'lastWorkingDay': plus(-2)}).status_code == 403
    assert a['foreign_hr'].post('/lifecycle/exits', {'employeeId': eid, 'kind': 'contract_end', 'lastWorkingDay': plus(-2)}).status_code == 404
    x = a['hr'].post('/lifecycle/exits', {'employeeId': eid, 'kind': 'contract_end', 'lastWorkingDay': plus(-2), 'reasonCategory': 'Contract ended'})
    assert x.status_code == 201
    detail = a['hr'].get(f"/lifecycle/exits/{x.json()['id']}").json()
    case_id = detail['exit']['case_id']
    for t in a['hr'].get(f'/lifecycle/cases/{case_id}').json()['tasks']:
        a['hr2'].patch(f"/lifecycle/tasks/{t['id']}", {'status': 'done'})
    assert one('SELECT status FROM lifecycle_case WHERE id = %s', [case_id])['status'] == 'active'
    assert lifecycle_daily()['exitsCompleted'] == 1
    assert one('SELECT status FROM employee WHERE id = %s', [eid])['status'] == 'exited'


def test_withdraw_pending_resignation_and_cancel_accepted_exit(w, a):
    r = a['ram'].post('/lifecycle/exits/resign', {'proposedLastWorkingDay': plus(40)})
    assert r.status_code == 201
    assert a['sita'].post(f"/lifecycle/exits/{r.json()['id']}/withdraw").status_code == 404
    assert a['ram'].post(f"/lifecycle/exits/{r.json()['id']}/withdraw").json() == {'ok': True}
    assert one("SELECT state FROM workflow_instance WHERE object_type = 'resignation' AND object_id = %s", [r.json()['id']])['state'] == 'cancelled'
    x = a['hr'].post('/lifecycle/exits', {'employeeId': w.ram_id, 'kind': 'termination', 'lastWorkingDay': plus(20)})
    assert x.status_code == 201
    assert a['hr'].post(f"/lifecycle/exits/{x.json()['id']}/cancel", {'reason': 'no'}).status_code == 400
    assert a['foreign_hr'].post(f"/lifecycle/exits/{x.json()['id']}/cancel", {'reason': 'Decision reversed'}).status_code == 404
    assert a['hr'].post(f"/lifecycle/exits/{x.json()['id']}/cancel", {'reason': 'Decision reversed'}).json() == {'ok': True}
    assert one('SELECT exit_date FROM employee WHERE id = %s', [w.ram_id])['exit_date'] is None
    assert one('SELECT status FROM lifecycle_case WHERE exit_id = %s', [x.json()['id']])['status'] == 'cancelled'


# ---------------------------------------------------------------- exit questions and the asset register

def test_exit_questions_hr_only_edit(w, a):
    qs = a['hr'].get('/lifecycle/exit-questions').json()['questions']
    assert len(qs) == 6
    assert a['sita'].put('/lifecycle/exit-questions', {'questions': []}).status_code == 403
    keep = [{'id': q['id'], 'question': q['question'], 'kind': q['kind'], 'options': q['options']} for q in qs[:2]]
    r = a['hr'].put('/lifecycle/exit-questions', {'questions': [*keep, {'question': 'Anything else?', 'kind': 'text'}]})
    assert r.status_code == 200 and r.json() == {'ok': True}
    assert len(a['sita'].get('/lifecycle/exit-questions').json()['questions']) == 3
    assert len(a['hr'].get('/lifecycle/exit-questions').json()['questions']) == 7     # switched off, never deleted
    bad = a['hr'].put('/lifecycle/exit-questions', {'questions': [{'question': 'Pick', 'kind': 'choice', 'options': ['one']}]})
    assert bad.status_code == 400
    assert a['foreign_hr'].put('/lifecycle/exit-questions', {'questions': keep}).status_code == 400   # another company's ids


def test_asset_types_and_updates(w, a):
    t = a['hr'].post('/lifecycle/asset-types', {'name': 'Drone'})
    assert t.status_code == 201 and t.json()['type']['name'] == 'Drone'
    assert a['hr'].post('/lifecycle/asset-types', {'name': 'Drone'}).status_code == 409
    assert a['sita'].post('/lifecycle/asset-types', {'name': 'X'}).status_code == 403
    assert 'Drone' not in [x['name'] for x in a['foreign_hr'].get('/lifecycle/asset-types').json()['types']]
    aid = a['hr'].post('/lifecycle/assets', {'tag': 'D-1', 'name': 'Mavic', 'typeId': t.json()['type']['id']}).json()['id']
    assert a['foreign_hr'].post('/lifecycle/assets', {'tag': 'D-2', 'name': 'X', 'typeId': t.json()['type']['id']}).status_code == 400
    assert a['hr'].patch(f'/lifecycle/assets/{aid}', {'notes': 'Spare battery', 'status': 'retired'}).json() == {'ok': True}
    assert a['foreign_hr'].patch(f'/lifecycle/assets/{aid}', {'notes': 'x'}).status_code == 404
    assets = a['hr'].get('/lifecycle/assets', status='retired').json()['assets']
    assert [x['tag'] for x in assets] == ['D-1'] and assets[0]['notes'] == 'Spare battery'
    assert a['hr'].post(f'/lifecycle/assets/{aid}/assign', {'employeeId': w.sita_id}).status_code == 409
    assert a['hr'].post(f'/lifecycle/assets/{aid}/return', {'condition': 'good'}).status_code == 409
    assert a['foreign_hr'].get('/lifecycle/assets').json() == {'assets': []}
    assert a['hr'].get('/lifecycle/assets', search='mav').json()['assets'][0]['id'] == aid
