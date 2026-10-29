"""
The shared story of apps/api/test/hiring.test.ts: one company with HR, a department manager, a second manager, an
employee and a second company. Each TS `it` builds on the ones before it, so World.run(n) replays the first n steps
(with their assertions) inside the current test's transaction.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

from django.test import Client

from hajiri.careers import services as careers
from tests.support.sql import one, rows
from hajiri.core.errors import HttpError
from hajiri.hiring import services as hiring
from hajiri.workflow import service as workflow
from tests.web.factories import make_company, make_employee, make_user, sign_in

PDF = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()
PUB = '/api/v1/public/careers'

QUESTIONS = [
    {'id': 'q1', 'kind': 'yes_no', 'label': 'Do you hold a valid work permit for Nepal?', 'required': True, 'knockout': {'equals': 'yes'}},
    {'id': 'q2', 'kind': 'number', 'label': 'Years of TypeScript experience', 'required': True, 'preferred': {'min': 2}},
    {'id': 'q3', 'kind': 'text', 'label': 'Link to your portfolio', 'required': False},
]


def future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days, minutes=345)).date().isoformat()


def status(fn, *args, ok: int = 200, **kw) -> int:
    """Run a service call the way the TS route answered it: `ok` on success, else the error's status."""
    try:
        fn(*args, **kw)
    except HttpError as e:
        return e.status
    return ok


class World:
    def __init__(self):
        self.co = make_company('demo', 'Demo Company')
        self.unit = one("INSERT INTO org_unit (company_id, kind, code, name_en) VALUES (%s, 'department', 'ENG', 'Engineering') RETURNING id",
                        [self.co.id])['id']
        self.unit = str(self.unit)
        self.ram_emp = make_employee(self.co, code='E002', name='Ram Thapa', org_unit=self.unit)
        self.sita_emp = make_employee(self.co, code='E001', name='Sita Sharma')
        self.shift_id = str(one("INSERT INTO shift (company_id, code, name_en, start_time, end_time) VALUES (%s, 'GEN', 'General', '10:00', '18:00') RETURNING id",
                                [self.co.id])['id'])
        self.hr = make_user(self.co, roles=['hr'], name='HR One')
        self.hr2 = make_user(self.co, roles=['hr'], name='HR Two')
        self.ram = make_user(self.co, roles=['manager'], employee=self.ram_emp)
        self.mgr2 = make_user(self.co, roles=['manager'], name='Second Manager')
        self.emp = make_user(self.co, roles=['employee'], employee=self.sita_emp)
        self.other = make_company('OTHERCO', 'Other Co')
        self.other_hr = make_user(self.other, roles=['hr'], email='hr@otherco.local', name='Other HR')
        self.public = Client()
        self.hired: list[str] = []
        careers.apply_limiter.reset()

    def client(self, user) -> Client:
        c = Client()
        sign_in(c, user)
        return c

    def apply(self, **body):
        b = {'name': 'Anita Gurung', 'email': 'Anita@example.com', 'phone': '+977 9800000000', 'experienceYears': 3, 'coverNote': 'I love payments.',
             'answers': {'q1': 'yes', 'q2': 4, 'q3': 'https://example.com'}, 'cvFilename': 'anita-cv.pdf', 'cvDataUrl': PDF, 'consent': True, **body}
        return self.public.post(f'{PUB}/jobs/{self.job_id}/apply', b, content_type='application/json')

    # ------------------------------------------------------------ steps (one per TS `it`, in order)

    def step_requisition(self):
        """a manager raises a hiring request and HR approves it"""
        assert status(hiring.create_requisition, self.emp, {'title': 'Engineer', 'justification': 'We need more people.'}) == 403
        assert status(hiring.create_requisition, self.ram, {'title': 'Engineer', 'justification': 'short'}) == 400
        self.req_id = hiring.create_requisition(self.ram, {
            'title': 'Backend engineer', 'designation': 'Software Engineer', 'orgUnitId': self.unit, 'headcount': 1, 'salaryMin': '80000',
            'salaryMax': '120000', 'justification': 'New payments project starting next quarter.'})
        assert len(hiring.list_requisitions(self.ram)) == 1
        assert len(hiring.list_requisitions(self.mgr2)) == 0
        item = next(i for i in workflow.inbox_for(self.hr) if i['objectId'] == self.req_id)
        s = item['summary']
        assert (s['title'], s['department'], s['headcount']) == ('Backend engineer', 'Engineering', 1)
        assert workflow.decide(self.hr, item['instanceId'], 'approve') == 'approved'
        assert one('SELECT status FROM hiring_requisition WHERE id = %s', [self.req_id])['status'] == 'approved'
        assert len(rows("SELECT 1 FROM notification_outbox WHERE user_id = %s AND template = 'job_requisition.approved'", [self.ram.id])) == 1

    def step_job(self):
        """HR creates a job from the request; it stays private until published"""
        assert status(hiring.create_job, self.ram, {'title': 'X'}) == 403
        bad_q = {'title': 'Backend engineer', 'questions': [{'id': 'q1', 'kind': 'text', 'label': 'Anything?', 'knockout': {'equals': 'yes'}}]}
        assert status(hiring.create_job, self.hr, bad_q) == 400
        assert status(hiring.create_job, self.hr, {'title': 'Backend engineer', 'stages': ['Screening', 'Hired']}) == 400
        self.job_id = hiring.create_job(self.hr, {
            'requisitionId': self.req_id, 'title': 'Backend engineer', 'description': 'Build and run the payments services that power our clients.',
            'requirements': 'TypeScript, PostgreSQL', 'category': 'Engineering', 'location': 'Kathmandu', 'workMode': 'hybrid', 'experienceMin': 2,
            'experienceMax': 5, 'salaryMin': '80000', 'salaryMax': '120000', 'showSalary': False, 'deadline': future(30), 'orgUnitId': self.unit,
            'hiringManagerId': str(self.ram_emp.id), 'questions': QUESTIONS})
        assert self.public.get(f'{PUB}/jobs/{self.job_id}').status_code == 404
        assert status(hiring.publish_job, self.hr, self.job_id) == 200

    def step_public_list(self):
        """the public careers page lists open jobs without private fields"""
        assert self.public.get(f'{PUB}/jobs').status_code == 404   # two companies: a code is needed
        r = self.public.get(f'{PUB}/jobs?company=demo')
        assert r.status_code == 200
        body = r.json()
        assert body['company']['name'] == 'Demo Company'
        assert len(body['jobs']) == 1
        assert body['jobs'][0]['salary_min'] is None
        assert 'company_id' not in body['jobs'][0]
        assert len(self.public.get(f'{PUB}/jobs?company=demo&workMode=remote').json()['jobs']) == 0
        assert len(self.public.get(f'{PUB}/jobs?company=OTHERCO').json()['jobs']) == 0
        one_ = self.public.get(f'{PUB}/jobs/{self.job_id}')
        assert one_.json()['job']['questions'][0] == {'id': 'q1', 'kind': 'yes_no', 'label': QUESTIONS[0]['label'], 'required': True}
        assert 'knockout' not in one_.content.decode()
        assert self.public.get(f'{PUB}/jobs/not-a-uuid').status_code == 404

    def step_apply(self):
        """accepts a public application with an encrypted CV, e-mails the candidate and tells HR"""
        r = self.apply()
        assert r.status_code == 201
        a = rows('SELECT id, score, knocked_out, cv_file_id FROM hiring_application WHERE job_id = %s', [self.job_id])
        assert len(a) == 1
        self.app_id = str(a[0]['id'])
        assert (a[0]['score'], a[0]['knocked_out']) == (100, False)
        f = one('SELECT data_enc, attached_to FROM stored_file WHERE id = %s', [a[0]['cv_file_id']])
        assert f['attached_to'] == f'hiring_application:{self.app_id}'
        assert b'%PDF' not in bytes(f['data_enc'])
        mail = one("SELECT to_email, title FROM notification_outbox WHERE template = 'hiring.received'")
        assert mail['to_email'] == 'anita@example.com'
        n = one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'hiring.applied'")['n']
        assert n >= 3   # both HR users and the hiring manager

    def step_repeat(self):
        """a repeat application, a bot and bad input store nothing and reveal nothing"""
        careers.apply_limiter.reset()
        assert self.apply(name='Someone Else').status_code == 201
        assert self.apply(email='bot@example.com', website='http://spam').status_code == 201
        assert one('SELECT count(*)::int AS n FROM hiring_application WHERE job_id = %s', [self.job_id])['n'] == 1
        assert self.apply(email='x@example.com', consent=False).status_code == 400
        assert self.apply(email='y@example.com', answers={'q2': 3}).status_code == 400
        html = 'data:application/pdf;base64,' + base64.b64encode(b'<script>x</script>').decode()
        assert self.apply(email='z@example.com', cvDataUrl=html).status_code == 400
        careers.apply_limiter.reset()
        assert self.apply(email='w@example.com', cvDataUrl='data:image/png;base64,iVBORw0KGgo=').status_code == 400
        assert self.apply(email='v@example.com', extra='field').status_code == 400

    def step_rate_limit(self):
        """rate-limits applications from one connection"""
        careers.apply_limiter.reset()
        for i in range(5):
            self.apply(email=f'bad{i}@example.com', consent=False)
        assert self.apply(email='late@example.com').status_code == 429

    def step_knockout(self):
        """flags a candidate who fails a must-have"""
        careers.apply_limiter.reset()
        r = self.apply(name='Bikash Rai', email='bikash@example.com', experienceYears=1, answers={'q1': 'no', 'q2': 0})
        assert r.status_code == 201
        row = one("""SELECT a.knocked_out, a.score FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                      WHERE c.email = 'bikash@example.com'""")
        assert (row['knocked_out'], row['score']) == (True, 33)

    def step_scope(self):
        """scopes the pipeline: HR and the department manager yes; others no"""
        board = hiring.board(self.hr, self.job_id)
        assert board['applications'][0]['name'] == 'Anita Gurung'   # best score first, knocked-out last
        assert board['applications'][1]['knocked_out'] is True
        assert status(hiring.board, self.ram, self.job_id) == 200
        assert len(hiring.list_jobs(self.ram)['jobs']) == 1
        assert len(hiring.list_jobs(self.mgr2)['jobs']) == 0
        assert status(hiring.board, self.mgr2, self.job_id) == 404
        assert status(hiring.application_detail, self.mgr2, self.app_id) == 404
        assert status(hiring.list_jobs, self.emp) == 403
        look = hiring.lookups(self.ram)
        assert [u['name_en'] for u in look['orgUnits']] == ['Engineering']
        assert all('email' not in u for u in look['users'])
        assert status(hiring.lookups, self.emp) == 403
        assert status(hiring.application_detail, self.other_hr, self.app_id) == 404
        assert status(hiring.get_job, self.other_hr, self.job_id) == 404
        cv = self.client(self.ram).get(f'/api/v1/hiring/applications/{self.app_id}/cv')
        assert cv.status_code == 200
        assert 'sandbox' in cv['Content-Security-Policy']

    def step_move(self):
        """moves, rates, tags and notes an application with a timeline"""
        assert status(hiring.move_application, self.ram, self.app_id, {'stage': 'Nowhere'}) == 400
        assert status(hiring.move_application, self.ram, self.app_id, {'stage': 'Hired'}) == 400
        assert status(hiring.move_application, self.ram, self.app_id, {'stage': 'Screening'}) == 200
        assert status(hiring.update_application, self.ram, self.app_id, {'rating': 4, 'tags': ['strong', 'payments', 'strong']}) == 200
        assert status(hiring.add_note, self.hr, self.app_id, {'body': 'Great phone screen.'}, ok=201) == 201
        d = hiring.application_detail(self.hr, self.app_id)
        a = d['application']
        assert (a['stage'], a['rating'], a['tags']) == ('Screening', 4, ['strong', 'payments'])
        assert 'cv_file_id' not in a
        kinds = {e['kind'] for e in d['events']}
        assert {'applied', 'stage', 'rating', 'note', 'email'} <= kinds
        assert len(a['score_detail']) > 0

    def step_interview(self):
        """schedules an interview: panel notified, candidate invited, stage moves on"""
        past = {'date': '2020-01-01', 'time': '10:00', 'mode': 'video', 'panel': [self.ram.id]}
        assert status(hiring.schedule_interview, self.ram, self.app_id, past) == 400
        foreign = {'date': future(3), 'time': '11:00', 'mode': 'video', 'panel': ['00000000-0000-7000-8000-00000000ffff']}
        assert status(hiring.schedule_interview, self.ram, self.app_id, foreign) == 400
        self.interview_id = hiring.schedule_interview(self.ram, self.app_id, {
            'date': future(3), 'time': '11:00', 'mode': 'video', 'location': 'https://meet.example.com/x', 'panel': [self.ram.id, self.mgr2.id]})
        assert one('SELECT stage FROM hiring_application WHERE id = %s', [self.app_id])['stage'] == 'Interview'
        assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE template = 'hiring.interview' AND channel = 'in_app'")['n'] == 2
        invite = one("SELECT body FROM notification_outbox WHERE template = 'hiring.interview_invite'")
        assert '11:00' in invite['body']
        assert len(hiring.list_interviews(self.mgr2, {'scope': 'mine'})) == 1
        assert status(hiring.list_interviews, self.emp, {'scope': 'all'}) == 403

    def step_panel(self):
        """panel members get a read-only view and submit their own scorecard"""
        view = hiring.application_detail(self.mgr2, self.app_id)
        assert view['access'] == 'panel'
        assert view['events'] == []
        assert self.client(self.mgr2).get(f'/api/v1/hiring/applications/{self.app_id}/cv').status_code == 200
        assert status(hiring.move_application, self.mgr2, self.app_id, {'stage': 'Offer'}) == 404
        assert status(hiring.submit_scorecard, self.hr, self.interview_id, {'ratings': {'Skills': 4}, 'recommendation': 'yes'}) == 404
        assert status(hiring.submit_scorecard, self.mgr2, self.interview_id, {'ratings': {'Skills': 9}, 'recommendation': 'yes'}) == 400
        assert status(hiring.submit_scorecard, self.mgr2, self.interview_id,
                      {'ratings': {'Skills': 4, 'Communication': 5}, 'recommendation': 'strong_yes', 'notes': 'Clear thinker.'}, ok=201) == 201
        assert status(hiring.submit_scorecard, self.ram, self.interview_id, {'ratings': {'Skills': 3}, 'recommendation': 'yes'}, ok=201) == 201
        assert len(hiring.application_detail(self.mgr2, self.app_id)['scorecards']) == 1
        assert len(hiring.application_detail(self.hr, self.app_id)['scorecards']) == 2

    def step_offer(self):
        """an offer needs approval by another HR user, then the candidate accepts"""
        offer = {'designation': 'Software Engineer', 'salary': '100000', 'joinDate': future(20)}
        assert status(hiring.create_offer, self.ram, self.app_id, offer) == 403
        assert status(hiring.create_offer, self.hr, self.app_id, {**offer, 'joinDate': '2020-01-01'}) == 400
        self.offer_id = hiring.create_offer(self.hr, self.app_id, {**offer, 'expiresOn': future(7), 'notes': 'Includes a laptop.'})
        assert status(hiring.create_offer, self.hr, self.app_id, {'designation': 'Engineer', 'salary': '1', 'joinDate': future(20)}) == 409
        assert 'DRAFT' in hiring.offer_letter(self.hr, self.offer_id)
        assert status(hiring.respond_offer, self.hr, self.offer_id, {'response': 'accepted'}) == 409
        assert status(hiring.hire, self.hr, self.app_id, {'empCode': 'E900'}) == 409
        item = next(i for i in workflow.inbox_for(self.hr2) if i['objectId'] == self.offer_id)
        assert (item['summary']['candidate'], item['summary']['designation']) == ('Anita Gurung', 'Software Engineer')
        assert workflow.decide(self.hr2, item['instanceId'], 'approve') == 'approved'
        letter = hiring.offer_letter(self.hr, self.offer_id)
        assert 'Anita Gurung' in letter
        assert 'class="draft"' not in letter
        assert '1,00,000.00' in letter
        assert status(hiring.respond_offer, self.hr, self.offer_id, {'response': 'accepted'}) == 200

    def step_hire(self):
        """hires the candidate as an employee, fills the request and runs the onboarding hook"""
        from hajiri.integrations import hooks
        fn = hooks.on_candidate_hired(lambda e: self.hired.append(e.employee_id))
        try:
            assert status(hiring.hire, self.hr, self.app_id, {'empCode': 'E001'}) == 409
            emp_id = hiring.hire(self.hr, self.app_id, {'empCode': 'E900', 'shiftId': self.shift_id})
        finally:
            hooks.off_candidate_hired(fn)
        e = one('SELECT name_en, email, designation, join_date FROM employee WHERE id = %s', [emp_id])
        assert (e['name_en'], e['email'], e['designation'], e['join_date'].isoformat()) == \
            ('Anita Gurung', 'anita@example.com', 'Software Engineer', future(20))
        a = one('SELECT org_unit_id, manager_id FROM employee_assignment WHERE employee_id = %s', [emp_id])
        assert (str(a['org_unit_id']), str(a['manager_id'])) == (self.unit, str(self.ram_emp.id))
        r = one('SELECT status, filled FROM hiring_requisition WHERE id = %s', [self.req_id])
        assert (r['status'], r['filled']) == ('filled', 1)
        h = one('SELECT status, stage, cv_file_id IS NOT NULL AS cv FROM hiring_application WHERE id = %s', [self.app_id])
        assert (h['status'], h['stage'], h['cv']) == ('hired', 'Hired', True)
        assert self.hired == [emp_id]
        assert one('SELECT 1 AS x FROM shift_assignment WHERE employee_id = %s AND shift_id = %s', [emp_id, self.shift_id])
        assert status(hiring.hire, self.hr, self.app_id, {'empCode': 'E901'}) == 409
        self.employee_id = emp_id

    def _bikash(self) -> dict:
        return one("""SELECT a.id, a.cv_file_id FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                       WHERE c.email = 'bikash@example.com'""")

    def step_reject_and_report(self):
        """rejects with a courteous e-mail and reports the funnel, time to hire and sources"""
        other = str(self._bikash()['id'])
        assert hiring.reject_application(self.ram, other, {'reason': 'No work permit'}) is True
        assert status(hiring.reject_application, self.ram, other, {'reason': 'Again'}) == 409
        rep = hiring.reports(self.hr)
        t = rep['totals']
        assert (t['applications'], t['hired'], t['rejected']) == (2, 1, 1)
        assert [s['reached'] for s in rep['funnel'][0]['stages']] == [2, 1, 1, 1, 1]
        assert rep['timeToHire']['hires'] == 1
        assert rep['sources'] == [{'source': 'careers', 'applications': 2, 'hired': 1}]
        assert status(hiring.reports, self.ram) == 403

    def step_export_and_purge(self):
        """exports a candidate for HR and purges rejected candidates after the retention period"""
        other = self._bikash()
        oid = str(other['id'])
        ex = self.client(self.hr).get(f'/api/v1/hiring/applications/{oid}/export')
        assert ex.status_code == 200
        assert ex.json()['candidate']['email'] == 'bikash@example.com'
        assert 'attachment' in ex['Content-Disposition']
        assert status(hiring.export_candidate, self.ram, oid) == 403
        now = datetime.now(timezone.utc)
        assert hiring.purge_rejected_candidates(now) == 0
        assert hiring.purge_rejected_candidates(now + timedelta(days=200)) == 1   # the hired candidate is kept
        c = one("""SELECT c.name, c.email, a.cv_file_id, a.cover_note FROM hiring_application a JOIN hiring_candidate c ON c.id = a.candidate_id
                    WHERE a.id = %s""", [oid])
        assert (c['name'], c['email'], c['cv_file_id'], c['cover_note']) == ('Removed candidate', None, None, None)
        assert one('SELECT data_enc FROM stored_file WHERE id = %s', [other['cv_file_id']])['data_enc'] is None
        assert one("SELECT to_email FROM notification_outbox WHERE template = 'hiring.rejected'")['to_email'] is None
        assert status(hiring.reopen_application, self.hr, oid) == 409

    def step_policy(self):
        """HR can switch the careers page off and set retention"""
        assert status(hiring.set_policy, self.hr, {'retentionDays': 5, 'careersEnabled': True}) == 400
        assert status(hiring.set_policy, self.ram, {'retentionDays': 90, 'careersEnabled': False}) == 403
        assert status(hiring.set_policy, self.hr, {'retentionDays': 90, 'careersEnabled': False}) == 200
        assert self.public.get(f'{PUB}/jobs?company=DEMO').status_code == 404
        assert self.public.get(f'{PUB}/jobs/{self.job_id}').status_code == 404
        hiring.set_policy(self.hr, {'retentionDays': 90, 'careersEnabled': True})
        assert status(hiring.close_job, self.hr, self.job_id) == 200
        careers.apply_limiter.reset()
        assert self.apply(email='after@example.com').status_code == 404

    STEPS = ('step_requisition', 'step_job', 'step_public_list', 'step_apply', 'step_repeat', 'step_rate_limit', 'step_knockout',
             'step_scope', 'step_move', 'step_interview', 'step_panel', 'step_offer', 'step_hire', 'step_reject_and_report',
             'step_export_and_purge', 'step_policy')

    @classmethod
    def run(cls, upto: str) -> 'World':
        w = cls()
        for name in cls.STEPS:
            careers.apply_limiter.reset()
            getattr(w, name)()
            if name == upto:
                return w
        raise AssertionError(f'unknown step {upto}')


