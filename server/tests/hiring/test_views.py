"""The hiring hub pages and their HTMX fragments, driven through the Django test client."""
import pytest

from tests.support.sql import one, rows
from hajiri.hiring import services as hiring
from hajiri.workflow import service as workflow
from tests.web.factories import audit_actions, htmx, hx_triggers, set_features_off, toasts

from .world import World, future

pytestmark = pytest.mark.django_db


def _msg(r):
    return [t['message'] for t in toasts(r)]


def test_hub_pages_for_staff_and_not_for_employees():
    w = World()
    hr, ram, emp = w.client(w.hr), w.client(w.ram), w.client(w.emp)
    for url in ('/hiring', '/hiring/pipeline', '/hiring/interviews', '/hiring/requisitions'):
        assert hr.get(url).status_code == 200, url
        assert ram.get(url).status_code == 200, url
        assert emp.get(url, **htmx()).status_code == 403, url
    page = hr.get('/hiring').content.decode()
    assert 'Open a role, publish it to the careers page' in page and 'Average time to hire' in page and 'New job' in page
    assert 'Average time to hire' not in ram.get('/hiring').content.decode()   # reports and actions are HR's
    assert 'No jobs yet' in hr.get('/hiring/pipeline').content.decode()
    assert 'No interviews coming up' in hr.get('/hiring/interviews').content.decode()
    assert 'No hiring requests' in hr.get('/hiring/requisitions').content.decode()


def test_feature_off_sends_pages_home_and_fragments_404():
    w = World()
    set_features_off(w.co, 'hiring')
    hr = w.client(w.hr)
    r = hr.get('/hiring')
    assert r.status_code == 302 and r['Location'] == '/'
    assert hr.post('/_/hiring/requisitions/new', {'title': 'x'}, **htmx()).status_code == 404


def test_requisition_form_cancel_and_create_job_from_it():
    w = World()
    ram, hr = w.client(w.ram), w.client(w.hr)
    assert 'Why is this needed?' in ram.get('/_/hiring/requisitions/new', **htmx()).content.decode()
    r = ram.post('/_/hiring/requisitions/new', {'kind': 'new', 'title': 'Backend engineer', 'headcount': '2', 'orgUnitId': w.unit,
                                                'salaryMin': '80000', 'salaryMax': '120000',
                                                'justification': 'New payments project starting next quarter.'}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Request sent to HR for approval.']
    assert 'hj:refresh-requisitions' in hx_triggers(r)
    req = one('SELECT id, headcount, salary_min FROM hiring_requisition')
    assert req['headcount'] == 2
    bad = ram.post('/_/hiring/requisitions/new', {'kind': 'replacement', 'title': 'Backend engineer',
                                                  'justification': 'Someone left the team last week.'}, **htmx())
    assert bad.status_code == 400 and _msg(bad) == ['choose the employee being replaced']
    page = ram.get('/hiring/requisitions').content.decode()
    assert '2 × Backend engineer' in page and 'Waiting for HR' in page and 'NPR 80,000 – 1,20,000' in page
    item = next(i for i in workflow.inbox_for(w.hr) if i['objectId'] == str(req['id']))
    workflow.decide(w.hr, item['instanceId'], 'approve')
    page = hr.get('/hiring/requisitions').content.decode()
    assert 'Create job' in page and '0 of 2 hired' in page
    form = hr.get(f"/_/hiring/jobs/new?requisition={req['id']}", **htmx()).content.decode()
    assert 'value="Backend engineer"' in form and 'name="requisitionId"' in form
    r = hr.post(f"/_/hiring/jobs/new?requisition={req['id']}", {
        'title': 'Backend engineer', 'workMode': 'hybrid', 'employmentType': 'full_time', 'openings': '2', 'description': 'Build things.',
        'stages_json': '["Applied","Screening","Interview","Offer","Hired"]', 'questions_json': '[]', 'orgUnitId': w.unit}, **htmx())
    assert r.status_code == 204
    assert _msg(r) == ['Job created as a draft. Publish it when it is ready.', 'Find the new job under Jobs and publish it when ready.']
    assert str(one('SELECT requisition_id FROM hiring_job')['requisition_id']) == str(req['id'])
    # The raiser cancels; anyone else in the company does not see it to cancel.
    other_req = hiring.create_requisition(w.ram, {'title': 'Tester', 'justification': 'Quality needs a hand now.'})
    assert w.client(w.mgr2).post(f'/_/hiring/requisitions/{other_req}/cancel', **htmx()).status_code == 404
    r = ram.post(f'/_/hiring/requisitions/{other_req}/cancel', **htmx())
    assert r.status_code == 204 and _msg(r) == ['Request cancelled.']
    assert 'cancel' in audit_actions(other_req)
    assert ram.post(f'/_/hiring/requisitions/{other_req}/cancel', **htmx()).status_code == 409


def test_job_form_questions_publish_close_and_policy():
    w = World()
    hr = w.client(w.hr)
    assert 'Screening questions' in hr.get('/_/hiring/jobs/new', **htmx()).content.decode()
    qs = ('[{"id":"q1","kind":"yes_no","label":"Work permit for Nepal?","required":true,"rule":"knockout","equals":"yes","min":1},'
          '{"id":"q2","kind":"number","label":"Years with SQL","required":false,"rule":"preferred","equals":"yes","min":"3"},'
          '{"id":"q3","kind":"text","label":"Portfolio link","required":false,"rule":"none"}]')
    r = hr.post('/_/hiring/jobs/new', {'title': 'Data analyst', 'workMode': 'remote', 'employmentType': 'contract', 'openings': '1',
                                       'experienceMin': '1', 'experienceMax': '4', 'salaryMin': '50,000', 'salaryMax': '', 'showSalary': '1',
                                       'deadline': future(10), 'description': 'Short', 'requirements': '',
                                       'stages_json': '["Applied","Hired"]', 'questions_json': qs}, **htmx())
    assert r.status_code == 204, r.content
    job = one('SELECT * FROM hiring_job')
    assert job['questions'] == [{'id': 'q1', 'kind': 'yes_no', 'label': 'Work permit for Nepal?', 'required': True, 'knockout': {'equals': 'yes'}},
                                {'id': 'q2', 'kind': 'number', 'label': 'Years with SQL', 'required': False, 'preferred': {'min': 3}},
                                {'id': 'q3', 'kind': 'text', 'label': 'Portfolio link', 'required': False}]
    assert job['stages'] == ['Applied', 'Hired'] and str(job['salary_min']) == '50000.00' and job['show_salary'] is True
    jid = str(job['id'])
    r = hr.post(f'/_/hiring/jobs/{jid}/publish', **htmx())
    assert r.status_code == 400 and _msg(r) == ['add a job description (at least 20 characters) before publishing']
    edit = hr.get(f'/_/hiring/jobs/{jid}/edit', **htmx()).content.decode()
    assert 'Edit Data analyst' in edit and 'Save job' in edit
    r = hr.post(f'/_/hiring/jobs/{jid}/edit', {'title': 'Data analyst', 'workMode': 'remote', 'employmentType': 'contract', 'openings': '1',
                                               'description': 'Turn our attendance data into decisions for clients.',
                                               'stages_json': '["Applied","Hired"]', 'questions_json': qs}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Job saved.']
    r = hr.post(f'/_/hiring/jobs/{jid}/publish', **htmx())
    assert _msg(r) == ['Published. The job is on the careers page.'] and 'hj:refresh-jobs' in hx_triggers(r)
    page = hr.get('/hiring?status=open').content.decode()
    assert 'Data analyst' in page and f'/careers/demo/{jid}' in page and 'View on careers page' in page   # the company's own careers address
    assert 'Data analyst' not in hr.get('/hiring?status=draft').content.decode()
    r = hr.post(f'/_/hiring/jobs/{jid}/close', **htmx())
    assert _msg(r) == ['Closed. No new applications will be accepted.']
    assert w.client(w.ram).post(f'/_/hiring/jobs/{jid}/close', **htmx()).status_code == 403
    assert 'Public careers page' in hr.get('/_/hiring/policy', **htmx()).content.decode()
    r = hr.post('/_/hiring/policy', {'retentionDays': '120'}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Hiring settings saved.']
    assert hiring.hiring_policy(str(w.co.id)) == {'retentionDays': 120, 'careersEnabled': False}


def test_pipeline_board_drawer_and_actions():
    w = World.run('step_knockout')
    hr, ram = w.client(w.hr), w.client(w.ram)
    page = hr.get(f'/hiring/pipeline?job={w.job_id}').content.decode()
    assert 'Anita Gurung' in page and 'Bikash Rai' in page and '2 in progress · 2 applications in total' in page
    assert 'Add candidate' in page and 'Add candidate' not in ram.get('/hiring/pipeline').content.decode()
    r = ram.post(f'/_/hiring/applications/{w.app_id}/move', {'stage': 'Screening', 'name': 'Anita Gurung'}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Anita Gurung moved to Screening.']
    assert {'hj:refresh-board', 'hj:refresh-candidate'} <= set(hx_triggers(r))
    board = ram.get(f'/hiring/pipeline?job={w.job_id}', **htmx('board')).content.decode()
    assert '<html' not in board and 'Anita Gurung' in board
    drawer = ram.get(f'/_/hiring/applications/{w.app_id}', **htmx('hj-drawer-body')).content.decode()
    assert 'data-modal-title="Anita Gurung"' in drawer and 'Screening score 100 of 100' in drawer and 'Schedule interview' in drawer
    assert 'Make an offer' not in drawer   # HR's
    assert 'Make an offer' in hr.get(f'/_/hiring/applications/{w.app_id}', **htmx()).content.decode()
    assert w.client(w.mgr2).get(f'/_/hiring/applications/{w.app_id}', **htmx()).status_code == 404
    r = ram.post(f'/_/hiring/applications/{w.app_id}/update', {'rating': '4'}, **htmx())
    assert r.status_code == 204 and one('SELECT rating FROM hiring_application WHERE id = %s', [w.app_id])['rating'] == 4
    ram.post(f'/_/hiring/applications/{w.app_id}/update', {'tags': 'strong, payments, strong'}, **htmx())
    assert one('SELECT tags FROM hiring_application WHERE id = %s', [w.app_id])['tags'] == ['strong', 'payments']
    r = hr.post(f'/_/hiring/applications/{w.app_id}/notes', {'body': 'Great phone screen.'}, **htmx())
    assert _msg(r) == ['Note added.']
    assert 'Great phone screen.' in hr.get(f'/_/hiring/applications/{w.app_id}?tab=activity', **htmx()).content.decode()
    # Not moving forward with the knocked-out candidate.
    bik = str(one("SELECT a.id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id WHERE c.name = 'Bikash Rai'")['id'])
    assert 'Not moving forward with Bikash Rai' in ram.get(f'/_/hiring/applications/{bik}/reject', **htmx()).content.decode()
    r = ram.post(f'/_/hiring/applications/{bik}/reject', {'reason': 'No work permit', 'notifyCandidate': '1'}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Bikash Rai was told by e-mail.'] and 'hj:hiring-done' in hx_triggers(r)
    assert 'Reopen' in hr.get(f'/_/hiring/applications/{bik}', **htmx()).content.decode()
    r = hr.post(f'/_/hiring/applications/{bik}/reopen', **htmx())
    assert _msg(r) == ['Application reopened.']
    r = ram.post(f'/_/hiring/applications/{bik}/reject', {'reason': 'No permit'}, **htmx())
    assert _msg(r) == ['Application closed.']


def test_add_candidate_with_uploaded_cv():
    w = World.run('step_job')
    hr = w.client(w.hr)
    assert 'Add a candidate' in hr.get(f'/_/hiring/jobs/{w.job_id}/candidates/new', **htmx()).content.decode()
    from hajiri.files.store import parse_upload, save_file
    from .world import PDF
    f = save_file(company_id=str(w.co.id), purpose='hiring_cv', filename='cv.pdf', file=parse_upload(PDF), uploaded_by=w.hr.id)
    # Required screening questions mean the candidate applies on the careers page.
    r = hr.post(f'/_/hiring/jobs/{w.job_id}/candidates/new', {'name': 'Hari Bista', 'email': 'hari@example.com', 'source': 'referral'}, **htmx())
    assert r.status_code == 400 and _msg(r) == ['Please answer every required question.']
    hiring.update_job(w.hr, w.job_id, {'questions': []})
    r = hr.post(f'/_/hiring/jobs/{w.job_id}/candidates/new', {'name': 'Hari Bista', 'email': 'hari@example.com', 'source': 'referral',
                                                             'experienceYears': '3', 'cvFileId': f['id']}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Candidate added.'] and 'hj:open-candidate' in hx_triggers(r)
    a = one("SELECT a.id, a.source, a.cv_file_id, a.owner_user_id, a.score FROM hiring_application a")
    assert (a['source'], str(a['cv_file_id']), str(a['owner_user_id']), a['score']) == ('referral', f['id'], w.hr.id, 100)
    assert one('SELECT attached_to FROM stored_file WHERE id = %s', [f['id']])['attached_to'] == f"hiring_application:{a['id']}"
    ev = one("SELECT body FROM hiring_event WHERE kind = 'applied'")['body']
    assert ev == 'Added by HR One (referral)'
    r = hr.post(f'/_/hiring/jobs/{w.job_id}/candidates/new', {'name': 'Hari Bista', 'email': 'HARI@example.com'}, **htmx())
    assert r.status_code == 409 and _msg(r) == ['this candidate has already applied for this job']


def test_interviews_scorecards_offer_letter_and_hire_through_pages():
    w = World.run('step_move')
    hr, ram, mgr2 = w.client(w.hr), w.client(w.ram), w.client(w.mgr2)
    form = ram.get(f'/_/hiring/applications/{w.app_id}/interviews/new', **htmx()).content.decode()
    assert 'Schedule an interview' in form and 'Second Manager' in form
    r = ram.post(f'/_/hiring/applications/{w.app_id}/interviews/new', {
        'title': 'Technical', 'date': future(3), 'time': '11:00', 'durationMin': '60', 'mode': 'phone',
        'panel': [w.ram.id, w.mgr2.id]}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Interview scheduled. The panel has been told.']
    assert not rows("SELECT 1 FROM notification_outbox WHERE template = 'hiring.interview_invite'")
    iv = str(one('SELECT id FROM hiring_interview')['id'])
    page = mgr2.get('/hiring/interviews').content.decode()
    assert 'Anita Gurung' in page and 'Add scorecard' in page and 'Technical · 60 min · Phone' in page
    assert 'Anita Gurung' not in w.client(w.hr).get('/hiring/interviews').content.decode()   # not on the panel
    assert 'Anita Gurung' in hr.get('/hiring/interviews?scope=all').content.decode()
    assert 'Scorecard: Anita Gurung' in mgr2.get(f'/_/hiring/interviews/{iv}/scorecard', **htmx()).content.decode()
    r = mgr2.post(f'/_/hiring/interviews/{iv}/scorecard', {'rating_0': '4', 'rating_1': '0', 'rating_2': '5', 'recommendation': 'strong_yes',
                                                          'notes': 'Clear thinker.'}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Scorecard saved.']
    sc = one('SELECT ratings, recommendation FROM hiring_scorecard')
    assert sc['ratings'] == {'Skills': 4, 'Communication': 5} and sc['recommendation'] == 'strong_yes'
    assert 'Update scorecard' in mgr2.get('/hiring/interviews').content.decode()
    panel = mgr2.get(f'/_/hiring/applications/{w.app_id}', **htmx()).content.decode()
    assert "You are on this candidate's interview panel" in panel and 'Activity' not in panel
    assert hr.get(f'/_/hiring/interviews/{iv}/scorecard', **htmx()).status_code == 404
    r = ram.post(f'/_/hiring/interviews/{iv}/update', {'status': 'completed'}, **htmx())
    assert _msg(r) == ['Marked as done.']
    # Offer, approval by the other HR user, letter, acceptance, hire.
    assert 'Make an offer' in hr.get(f'/_/hiring/applications/{w.app_id}/offers/new', **htmx()).content.decode()
    r = hr.post(f'/_/hiring/applications/{w.app_id}/offers/new', {'designation': 'Software Engineer', 'salary': '100000',
                                                                  'joinDate': future(20), 'expiresOn': future(7)}, **htmx())
    assert _msg(r) == ['Offer drafted. Another HR user needs to approve it before it goes to the candidate.']
    offer = str(one('SELECT id FROM hiring_offer')['id'])
    letter = hr.get(f'/_/hiring/offers/{offer}/letter')
    assert letter.status_code == 200 and letter['Content-Type'].startswith('text/html') and 'DRAFT' in letter.content.decode()
    assert 'Waiting for approval' in hr.get(f'/_/hiring/applications/{w.app_id}?tab=offers', **htmx()).content.decode()
    # The drafter cannot approve their own offer.
    inst = workflow.instance_for_object('job_offer', offer)
    assert hr.post(f"/_/workflow/{inst['id']}/approve", **htmx()).status_code in (403, 404)
    workflow.decide(w.hr2, inst['id'], 'approve')
    r = hr.post(f'/_/hiring/offers/{offer}/respond', {'response': 'accepted'}, **htmx())
    assert _msg(r) == ['Marked as accepted. You can hire the candidate now.']
    form = hr.get(f'/_/hiring/applications/{w.app_id}/hire', **htmx()).content.decode()
    assert 'Hire and create the employee' in form and 'General (GEN)' in form
    assert w.client(w.ram).get(f'/_/hiring/applications/{w.app_id}/hire', **htmx()).status_code == 403
    r = hr.post(f'/_/hiring/applications/{w.app_id}/hire', {'empCode': 'e950', 'gender': 'female', 'shiftId': w.shift_id}, **htmx())
    assert r.status_code == 204 and _msg(r) == ['Employee record created.']
    e = one('SELECT emp_code, gender, designation FROM employee WHERE emp_code = %s', ['E950'])
    assert (e['gender'], e['designation']) == ('female', 'Software Engineer')
    assert 'Withdraw' not in hr.get(f'/_/hiring/applications/{w.app_id}?tab=offers', **htmx()).content.decode()


def test_withdraw_offer_and_export_page():
    w = World.run('step_panel')
    hr = w.client(w.hr)
    offer = hiring.create_offer(w.hr, w.app_id, {'designation': 'Engineer', 'salary': '90000', 'joinDate': future(20)})
    r = hr.post(f'/_/hiring/offers/{offer}/withdraw', **htmx())
    assert r.status_code == 204 and _msg(r) == ['Offer withdrawn.']
    assert one('SELECT status FROM hiring_offer WHERE id = %s', [offer])['status'] == 'withdrawn'
    assert workflow.instance_for_object('job_offer', offer)['state'] == 'cancelled'
    assert hr.post(f'/_/hiring/offers/{offer}/withdraw', **htmx()).status_code == 409
    ex = hr.get(f'/_/hiring/applications/{w.app_id}/export')
    assert ex.status_code == 200 and ex.json()['candidate']['name'] == 'Anita Gurung'
    assert 'export' in audit_actions()
    assert w.client(w.ram).get(f'/_/hiring/applications/{w.app_id}/export', **htmx()).status_code == 403


def test_other_company_cannot_reach_records():
    w = World.run('step_interview')
    other = w.client(w.other_hr)
    assert other.get(f'/_/hiring/applications/{w.app_id}', **htmx()).status_code == 404
    assert other.get(f'/_/hiring/applications/{w.app_id}/cv').status_code == 404
    assert other.post(f'/_/hiring/jobs/{w.job_id}/close', **htmx()).status_code == 404
    assert other.get(f'/hiring/pipeline?job={w.job_id}', **htmx('board')).status_code == 404
    assert other.get(f'/_/hiring/interviews/{w.interview_id}/scorecard', **htmx()).status_code == 404


def test_requisition_form_needs_the_replaced_employee_before_sending():
    w = World()
    page = w.client(w.ram).get('/_/hiring/requisitions/new', **htmx()).content.decode()
    assert "(kind === 'replacement' &amp;&amp; !replacing)" in page or "(kind === 'replacement' && !replacing)" in page
    assert "replacingEmployeeId" in page
