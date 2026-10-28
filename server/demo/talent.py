"""Performance, learning and hiring (port of apps/api/src/db/demo-suite/talent.ts)."""
from __future__ import annotations

import re

from hajiri.db.models import (
    HiringInterview, HiringJob, HiringOffer, HiringRequisition, LrnCourse, LrnEnrollment, PerfGoal, PerfJournal, PerfKeyResult,
    PerfReview, PerfReviewItem,
)
from hajiri.hiring import services as hiring
from hajiri.learning import services as learning
from hajiri.performance import conversations, goals, reviews
from hajiri.people import service as people
from hajiri.workflow import service as workflow

from .context import DemoContext, already_seeded, instance_of
from .kit import Actor, ApiError, add_days_iso, npt_instant, pick


def approve_as(ctx: DemoContext, who: Actor, type_: str, id_: str) -> None:
    inst = instance_of(ctx, type_, id_)
    if inst:
        ctx.kit.call(f'POST /workflow/{inst}/approve', workflow.decide, who.user, inst, 'approve')


def seed_performance(ctx: DemoContext) -> None:
    """Company and team goals with key results and check-ins, a KPI library, competencies, a review cycle in self-review, 1-on-1s, feedback and journal notes."""
    kit, hr, today = ctx.kit, ctx.hr, ctx.today
    if already_seeded(ctx, 'performance', PerfJournal.objects.all(), PerfGoal.objects.all()):
        return
    fy_end = add_days_iso(today, 270)

    def goal(who: Actor, b: dict) -> str:
        return str(kit.call('POST /performance/goals', goals.create_goal, who.user, b))
    c1 = goal(hr, {'scope': 'company', 'title': 'Grow GPU cloud revenue to NPR 12 crore this fiscal year', 'weight': 40, 'dueDate': fy_end,
                   'description': 'Win enterprise and research customers in Nepal and keep the ones we have.',
                   'keyResults': [{'title': 'Annual recurring revenue (NPR crore)', 'measure': 'number', 'startValue': 4, 'targetValue': 12, 'currentValue': 6.5, 'unit': 'crore'},
                                  {'title': 'Enterprise customers', 'measure': 'number', 'startValue': 3, 'targetValue': 10, 'currentValue': 5}]})
    goal(hr, {'scope': 'company', 'title': 'Keep the platform up 99.95% of the time', 'weight': 30, 'dueDate': fy_end,
              'keyResults': [{'title': 'Monthly uptime', 'measure': 'percent', 'startValue': 99.5, 'targetValue': 99.95, 'currentValue': 99.91},
                             {'title': 'Incidents closed within SLA', 'measure': 'percent', 'startValue': 70, 'targetValue': 95, 'currentValue': 88}]})
    goal(hr, {'scope': 'company', 'title': 'Make CoreValley a place people recommend', 'weight': 30, 'dueDate': fy_end,
              'keyResults': [{'title': 'eNPS', 'measure': 'number', 'startValue': 10, 'targetValue': 40, 'currentValue': 24},
                             {'title': 'Regretted attrition', 'measure': 'percent', 'startValue': 12, 'targetValue': 5, 'currentValue': 7}]})

    units = kit.call('GET /people/structure', people.structure, hr.user)['units']
    eng = next((u for u in units if u['code'] == 'PRD'), None)
    if eng:
        goal(hr, {'scope': 'department', 'orgUnitId': eng['id'], 'parentId': c1, 'title': 'Launch inference endpoints for enterprise customers', 'weight': 50,
                  'dueDate': add_days_iso(today, 120),
                  'keyResults': [{'title': 'Customers live on inference endpoints', 'measure': 'number', 'startValue': 0, 'targetValue': 4, 'currentValue': 1},
                                 {'title': 'p95 latency under 300 ms', 'measure': 'boolean', 'targetValue': 1, 'currentValue': 0}]})

    reports = [e for e in ctx.employees if e.manager_id][:8]
    titles = ['Cut GPU node provisioning time to under 10 minutes', 'Close three new enterprise deals', 'Automate the monthly billing run',
              'Raise test coverage of the console to 80%', 'Reduce support first-response time to 2 hours', 'Ship the mobile app beta',
              'Document every runbook for on-call', 'Publish two case studies']
    for i, e in enumerate(reports):
        mgr = ctx.manager_of(e)
        g = goal(mgr, {'scope': 'individual', 'employeeId': e.id, 'parentId': None if i % 2 else c1, 'title': pick(titles, i), 'weight': 100,
                       'dueDate': add_days_iso(today, 90 + i * 10),
                       'keyResults': [{'title': 'Progress', 'measure': 'percent', 'startValue': 0, 'targetValue': 100, 'currentValue': 10 + ((i * 17) % 70)}]})
        kr = PerfKeyResult.objects.for_company(ctx.company_id).filter(goal_id=g).values_list('id', flat=True).first()
        kit.call(f'POST /performance/goals/{g}/checkins', goals.checkin, ctx.as_(e).user, g,
                 {**({'keyResultId': str(kr), 'value': 20 + ((i * 17) % 70)} if kr else {'progress': 25}), 'confidence': 1 + (i % 3),
                  'comment': pick(['On track; the hard part is done.', 'Blocked on hardware delivery, catching up next week.', 'Good progress, two customers in pilot.'], i)})

    for body in ({'designation': 'Backend Engineer', 'title': 'Deployments without rollback', 'measure': 'percent', 'defaultTarget': 95, 'unit': '%'},
                 {'designation': 'Enterprise Sales', 'title': 'New annual contract value (NPR lakh)', 'measure': 'number', 'defaultTarget': 150, 'unit': 'lakh'},
                 {'designation': 'Customer Success', 'title': 'Customer satisfaction score', 'measure': 'percent', 'defaultTarget': 90, 'unit': '%'}):
        kit.call('POST /performance/kpis', goals.create_kpi, hr.user, body)

    def levels(a: str, b: str, c: str) -> list[dict]:
        return [{'level': 1, 'text': a}, {'level': 3, 'text': b}, {'level': 5, 'text': c}]
    prd_designations = list(dict.fromkeys(e.designation for e in ctx.employees if e.org_code == 'PRD' and e.designation))
    for body in (
        {'kind': 'skill', 'name': 'Problem solving', 'description': 'Breaks problems down and finds sound solutions.',
         'indicators': levels('Solves routine problems with help.', 'Solves most problems alone and explains the trade-offs.', 'Solves novel, cross-team problems and teaches others.')},
        {'kind': 'attitude', 'name': 'Ownership', 'description': 'Takes responsibility for outcomes, not just tasks.',
         'indicators': levels('Finishes assigned work.', 'Follows work through to the result and flags risks early.', 'Owns outcomes across teams and raises the bar.')},
        {'kind': 'skill', 'name': 'Communication', 'description': 'Writes and speaks clearly for the audience.',
         'indicators': levels('Shares updates when asked.', 'Writes clear updates and documents decisions.', 'Aligns stakeholders and handles difficult conversations well.')},
        {'kind': 'knowledge', 'name': 'Cloud and GPU infrastructure', 'description': 'Understands how our platform works end to end.', 'designations': prd_designations,
         'indicators': levels('Knows the main services.', 'Can change and operate the services safely.', 'Designs new parts of the platform.')},
    ):
        kit.call('POST /performance/competencies', reviews.create_competency, hr.user, body)

    cycle = str(kit.call('POST /performance/cycles', reviews.create_cycle, hr.user,
                         {'name': 'Mid-year review 2083', 'periodStart': add_days_iso(today, -180), 'periodEnd': today, 'goalsWeight': 60, 'competencyWeight': 40,
                          'peerEnabled': True, 'peerMin': 2, 'selfDue': add_days_iso(today, 10), 'managerDue': add_days_iso(today, 20),
                          'peerDue': add_days_iso(today, 20), 'calibrationDue': add_days_iso(today, 30)}))
    kit.call(f'POST /performance/cycles/{cycle}/launch', reviews.launch_cycle, hr.user, cycle)
    # A few people already submitted their self review.
    for i, r in enumerate(PerfReview.objects.for_company(ctx.company_id).filter(cycle_id=cycle).values('id', 'employee_id')[:4]):
        e = next((x for x in ctx.employees if x.id == str(r['employee_id'])), None)
        if not e or not e.user_id:
            continue
        items = PerfReviewItem.objects.for_company(ctx.company_id).filter(review_id=r['id']).values_list('id', flat=True)
        kit.call(f"PUT /performance/reviews/{r['id']}/self", reviews.save_self, ctx.as_(e).user, str(r['id']), {
            'items': [{'id': str(it), 'rating': 3 + ((i + k) % 3),
                       'comment': pick(['Delivered what I planned.', 'Took on more than expected.', 'Still learning here.'], i + k)} for k, it in enumerate(items)],
            'summary': 'A good half: shipped the main goal and helped the new joiners. I want to grow in system design next.', 'submit': i < 3,
        })

    # 1-on-1s, feedback and journal notes from managers.
    for i, e in enumerate(reports[:5]):
        mgr = ctx.manager_of(e)
        if not mgr.employee_id:
            continue
        kit.call('POST /performance/one-on-ones', conversations.create_meeting, mgr.user,
                 {'withEmployeeId': e.id, 'scheduledAt': npt_instant(add_days_iso(today, 1 + i), '15:00').isoformat(), 'cadence': 'biweekly' if i % 2 else 'weekly',
                  'agenda': ['How is the current goal going?', pick(['Workload and focus', 'Career growth', 'Feedback on the last release'], i)]})
        kit.call('POST /performance/journal', conversations.add_journal, mgr.user,
                 {'employeeId': e.id, 'noteDate': add_days_iso(today, -7 - i), 'tag': 'improvement' if i % 3 == 2 else 'positive',
                  'note': pick(['Handled the Nabil cut-over calmly at night and wrote a clear incident note.', 'Helped two new joiners set up their environment without being asked.',
                                'Missed the estimate on the billing task twice; talked about breaking work down.'], i)})
        peer = reports[(i + 1) % len(reports)]
        if peer.user_id and peer.id != e.id:
            kit.call('POST /performance/feedback', conversations.give_feedback, ctx.as_(peer).user,
                     {'toEmployeeId': e.id, 'body': pick(['Thanks for the quick review on the scheduler change, it saved me a day.', 'Your demo for the KU team was very clear.',
                                                          'Great job keeping the on-call channel calm last week.'], i)})


def seed_learning(ctx: DemoContext) -> None:
    """A course catalogue with a mandatory safety course, sessions, enrolments (requested, attended, completed) and certificates."""
    kit, hr, today = ctx.kit, ctx.hr, ctx.today
    if already_seeded(ctx, 'learning', LrnEnrollment.objects.filter(course__code='K8S-201'), LrnCourse.objects.all()):
        return

    def course(b: dict) -> str:
        return str(kit.call('POST /learning/courses', learning.create_course, hr.user, b))
    fire = course({'code': 'FIRE-01', 'title': 'Fire safety and evacuation', 'category': 'Health and safety', 'delivery': 'classroom', 'durationHours': 2,
                   'mandatory': True, 'validityMonths': 12, 'passMark': 70,
                   'description': 'Fire extinguisher use, evacuation routes and assembly points for the Baluwatar office.'})
    kit.call(f'PUT /learning/courses/{fire}/questions', learning.save_questions, hr.user, fire, {'questions': [
        {'prompt': 'Where is the assembly point for the Baluwatar office?', 'options': ['Car park in front of the building', 'Rooftop', 'Basement'], 'correctIndex': 0},
        {'prompt': 'Which extinguisher do you use on an electrical fire?', 'options': ['Water', 'CO2', 'Foam'], 'correctIndex': 1},
        {'prompt': 'What do you do first when the alarm sounds?', 'options': ['Finish your email', 'Leave by the nearest exit', 'Take the lift'], 'correctIndex': 1},
    ]})
    k8s = course({'code': 'K8S-201', 'title': 'Kubernetes for GPU workloads', 'category': 'Technical', 'delivery': 'blended', 'durationHours': 12, 'cost': '15000.00',
                  'source': 'internal'})
    comm = course({'code': 'COMM-101', 'title': 'Clear writing for customers', 'category': 'Communication', 'delivery': 'online', 'durationHours': 4,
                   'source': 'external', 'provider': 'Coursera', 'url': 'https://www.coursera.org', 'cost': '4500.00'})
    course({'code': 'POSH-01', 'title': 'Respect at work (anti-harassment)', 'category': 'Legal and compliance', 'delivery': 'online', 'durationHours': 1,
            'mandatory': True, 'validityMonths': 24})

    def session(course_id: str, b: dict) -> str:
        return str(kit.call(f'POST /learning/courses/{course_id}/sessions', learning.add_session, hr.user, course_id, b))
    fire_session = session(fire, {'startsAt': npt_instant(add_days_iso(today, 1), '14:00').isoformat(), 'endsAt': npt_instant(add_days_iso(today, 1), '16:00').isoformat(),
                                  'venue': 'Meeting room 2', 'trainer': 'Nepal Fire Safety Services', 'capacity': 30})
    k8s_session = session(k8s, {'startsAt': npt_instant(add_days_iso(today, 9), '10:00').isoformat(), 'endsAt': npt_instant(add_days_iso(today, 10), '17:00').isoformat(),
                                'venue': 'Training room', 'trainer': 'Ram Thapa', 'capacity': 12})

    everyone = [e.id for e in ctx.employees]
    kit.call('POST /learning/enrollments/assign', learning.assign, hr.user, {'courseId': fire, 'sessionId': fire_session, 'employeeIds': everyone[:12]})
    # The session took place three weeks ago (sessions are scheduled ahead, so it is moved back once people are enrolled).
    kit.call(f'PATCH /learning/sessions/{fire_session}', learning.update_session, hr.user, fire_session,
             {'startsAt': npt_instant(add_days_iso(today, -20), '14:00').isoformat(), 'endsAt': npt_instant(add_days_iso(today, -20), '16:00').isoformat(), 'status': 'completed'})
    enrollments = LrnEnrollment.objects.for_company(ctx.company_id)
    for i, en in enumerate(enrollments.filter(course_id=fire).order_by('employee_id').values('id', 'employee_id')):
        if i > 8:
            break
        eid = str(en['id'])
        kit.call(f'POST /learning/enrollments/{eid}/attendance', learning.record_attendance, hr.user, eid, {'attended': True})
        kit.call(f'POST /learning/enrollments/{eid}/complete', learning.complete, hr.user, eid)
        if i % 3 == 0:
            f = kit.upload_pdf(hr, 'training_certificate', 'fire-safety-certificate.pdf', 'Certificate of completion',
                                 ['Fire safety and evacuation', f"Issued to employee {str(en['employee_id'])[:8]}", 'Nepal Fire Safety Services'])
            kit.call(f'POST /learning/enrollments/{eid}/certificate', learning.attach_certificate, hr.user, eid,
                     {'fileId': f, 'issuedOn': add_days_iso(today, -20), 'expiresOn': add_days_iso(today, 20 if i == 0 else 345)})
    engineers = [e for e in ctx.employees if e.org_code == 'PRD' or re.search(r'engineer', e.designation or '', re.I)]
    for i, e in enumerate(engineers[:4]):
        if not e.user_id:
            continue
        kit.call('POST /learning/enrollments/self', learning.self_enroll, ctx.as_(e).user,
                 {'courseId': k8s, 'sessionId': k8s_session,
                  'reason': pick(['I run GPU jobs every day and want to schedule them better.', 'Needed for the inference endpoints goal.'], i)})
    for en in enrollments.filter(course_id=k8s).values('id', 'employee_id')[:2]:
        e = next(x for x in ctx.employees if x.id == str(en['employee_id']))
        try:
            approve_as(ctx, ctx.manager_of(e), 'training_nomination', str(en['id']))
        except ApiError:
            pass
    sales = [e for e in ctx.employees if re.search(r'sales|success|growth', e.designation or '', re.I)]
    if sales:
        kit.call('POST /learning/enrollments/assign', learning.assign, hr.user, {'courseId': comm, 'employeeIds': [e.id for e in sales]})


CANDIDATES = [
    ('Aashish Pandey', 'aashish.pandey@example.com', 6, 'Leapfrog Technology', 'linkedin'),
    ('Pooja Shrestha', 'pooja.shrestha@example.com', 5, 'F1Soft International', 'careers_page'),
    ('Nabin Gautam', 'nabin.gautam@example.com', 8, 'Fusemachines', 'referral'),
    ('Srijana Rai', 'srijana.rai@example.com', 4, 'Cloud Factory', 'careers_page'),
    ('Ujjwal Karki', 'ujjwal.karki@example.com', 2, 'Freelance', 'job_board'),
    ('Riya Thapa', 'riya.thapa@example.com', 7, 'Daraz Nepal', 'linkedin'),
]


def seed_hiring(ctx: DemoContext) -> None:
    """A hiring request, two open jobs on the careers page, candidates across the pipeline, interviews with scorecards, and an offer."""
    kit, hr, today = ctx.kit, ctx.hr, ctx.today
    if already_seeded(ctx, 'hiring', HiringOffer.objects.all(), HiringRequisition.objects.all()):
        return
    units = kit.call('GET /people/structure', people.structure, hr.user)['units']
    eng = next((u['id'] for u in units if u['code'] == 'PRD'), None)
    sal = next((u['id'] for u in units if u['code'] == 'SAL'), None)
    eng_manager = next((e for e in ctx.employees if any(x.manager_id == e.id for x in ctx.employees) and e.org_code == 'PRD'), None) or ctx.employees[1]
    mgr_actor = ctx.as_(eng_manager) if eng_manager.user_id else hr

    req = str(kit.call('POST /hiring/requisitions', hiring.create_requisition, mgr_actor.user,
                       {'title': 'Senior Backend Engineer', 'designation': 'Backend Engineer', 'orgUnitId': eng, 'headcount': 2, 'salaryMin': '180000',
                        'salaryMax': '260000', 'kind': 'new',
                        'justification': 'Two enterprise customers go live on inference endpoints next quarter; the team needs more backend capacity.'}))
    approve_as(ctx, hr, 'job_requisition', req)
    kit.call('POST /hiring/requisitions', hiring.create_requisition, hr.user,
             {'title': 'Sales Development Representative', 'orgUnitId': sal, 'headcount': 1, 'kind': 'new',
              'justification': 'Outbound pipeline for banks and hospitals outside the valley.'})

    try:
        job = str(kit.call('POST /hiring/jobs', hiring.create_job, hr.user, {
            'requisitionId': req, 'title': 'Senior Backend Engineer', 'category': 'Engineering', 'location': 'Kathmandu', 'workMode': 'hybrid',
            'employmentType': 'full_time', 'experienceMin': 4, 'experienceMax': 10, 'salaryMin': '180000', 'salaryMax': '260000', 'showSalary': True,
            'openings': 2, 'deadline': add_days_iso(today, 25), 'orgUnitId': eng, 'hiringManagerId': eng_manager.id,
            'description': "Build the APIs and schedulers behind Nepal's first GPU cloud.\n\nYou will design services that place customer workloads on GPUs, meter usage and bill it accurately.",
            'requirements': '- 4+ years building backend services in Go, TypeScript or Python\n- Solid PostgreSQL and API design\n- Comfortable with Linux and containers\n- Kubernetes experience is a plus',
            'questions': [
                {'id': 'q1', 'kind': 'number', 'label': 'Years of backend experience', 'required': True, 'knockout': {'op': 'gte', 'value': 3}},
                {'id': 'q2', 'kind': 'yes_no', 'label': 'Can you work from our Kathmandu office three days a week?', 'required': True,
                 'knockout': {'op': 'eq', 'value': True}},
                {'id': 'q3', 'kind': 'text', 'label': 'Link to your GitHub or portfolio', 'required': False},
            ],
        }))
    except ApiError:
        job = str(kit.call('POST /hiring/jobs', hiring.create_job, hr.user, {
            'requisitionId': req, 'title': 'Senior Backend Engineer', 'category': 'Engineering', 'location': 'Kathmandu', 'workMode': 'hybrid',
            'experienceMin': 4, 'experienceMax': 10, 'salaryMin': '180000', 'salaryMax': '260000', 'showSalary': True, 'openings': 2,
            'deadline': add_days_iso(today, 25), 'orgUnitId': eng, 'description': "Build the APIs and schedulers behind Nepal's first GPU cloud.",
            'requirements': '4+ years building backend services.'}))
    kit.call(f'POST /hiring/jobs/{job}/publish', hiring.publish_job, hr.user, job)
    job2 = str(kit.call('POST /hiring/jobs', hiring.create_job, hr.user, {
        'title': 'Customer Success Associate', 'category': 'Customer success', 'location': 'Kathmandu', 'workMode': 'onsite', 'employmentType': 'full_time',
        'experienceMin': 1, 'experienceMax': 3, 'deadline': add_days_iso(today, 18), 'orgUnitId': sal,
        'description': 'Help research labs and enterprises get the most from CoreValley.', 'requirements': '- Clear written English\n- 1+ year in a customer-facing role'}))
    kit.call(f'POST /hiring/jobs/{job2}/publish', hiring.publish_job, hr.user, job2)

    def cv(name: str) -> str:
        return kit.upload_pdf(hr, 'hiring_cv', f"{re.sub(r'\s+', '-', name)}-CV.pdf", f'{name} - Curriculum Vitae',
                                ['Backend engineer', 'Experience: Go, PostgreSQL, Kubernetes', 'Education: BE Computer, Pulchowk Campus'])
    apps: list[str] = []
    for name, email, years, employer, source in CANDIDATES:
        route = f'POST /hiring/jobs/{job}/applications'
        try:
            a = kit.call(route, hiring.add_candidate, hr.user, job, {
                'name': name, 'email': email, 'phone': f'98{str(10000000 + len(name) * 1234567)[:8]}', 'source': source, 'cvFileId': cv(name),
                'experienceYears': years, 'currentEmployer': employer,
                'answers': {'q1': years, 'q2': years != 2, 'q3': f"https://github.com/{name.split(' ')[0].lower()}"}})
        except ApiError as e:
            if 'source' not in str(e):
                raise
            a = kit.call(route, hiring.add_candidate, hr.user, job, {'name': name, 'email': email, 'cvFileId': cv(name), 'experienceYears': years,
                                                                     'currentEmployer': employer, 'answers': {'q1': years, 'q2': True}})
        apps.append(str(a))
    stages = HiringJob.objects.for_company(ctx.company_id).values_list('stages', flat=True).get(pk=job)

    def stage(pattern: str) -> str:
        return next((s for s in stages if re.search(pattern, s, re.I)), stages[1])

    def move(app: str, pattern: str) -> None:
        kit.call(f'POST /hiring/applications/{app}/move', hiring.move_application, hr.user, app, {'stage': stage(pattern)})
    move(apps[0], 'screen')
    move(apps[0], 'interview')
    move(apps[1], 'screen')
    move(apps[2], 'screen')
    move(apps[2], 'interview')
    move(apps[2], 'offer')
    kit.call(f'POST /hiring/applications/{apps[4]}/reject', hiring.reject_application, hr.user, apps[4],
             {'reason': 'Less than three years of backend experience.', 'notifyCandidate': True})

    panel = [hr.user_id, *([eng_manager.user_id] if eng_manager.user_id else [])]
    kit.call(f'POST /hiring/applications/{apps[0]}/interviews', hiring.schedule_interview, hr.user, apps[0],
             {'title': 'Technical interview', 'date': add_days_iso(today, 2), 'time': '11:00', 'durationMin': 60, 'mode': 'video', 'location': 'Google Meet',
              'panel': panel})
    iv = str(kit.call(f'POST /hiring/applications/{apps[2]}/interviews', hiring.schedule_interview, hr.user, apps[2],
                      {'title': 'System design', 'date': add_days_iso(today, 1), 'time': '14:00', 'durationMin': 60, 'mode': 'in_person',
                       'location': 'Meeting room 1', 'panel': panel}))
    # It happened last week (interviews can only be booked ahead, so move it back before the panel scores it).
    HiringInterview.objects.for_company(ctx.company_id).filter(pk=iv).update(starts_at=npt_instant(add_days_iso(today, -5), '14:00'))
    kit.call(f'POST /hiring/interviews/{iv}/scorecard', hiring.submit_scorecard, hr.user, iv,
             {'ratings': {'System design': 5, 'Coding': 4, 'Communication': 4, 'Culture add': 5}, 'recommendation': 'strong_yes',
              'notes': 'Designed the GPU scheduler cleanly and asked sharp questions about billing.'})
    if eng_manager.user_id:
        kit.call(f'POST /hiring/interviews/{iv}/scorecard', hiring.submit_scorecard, mgr_actor.user, iv,
                 {'ratings': {'System design': 4, 'Coding': 5, 'Communication': 4}, 'recommendation': 'yes',
                  'notes': 'Strong coder; would pair well with the platform team.'})
    kit.call(f'POST /hiring/applications/{apps[2]}/offers', hiring.create_offer, hr.user, apps[2],
             {'designation': 'Senior Backend Engineer', 'salary': '235000', 'joinDate': add_days_iso(today, 30), 'expiresOn': add_days_iso(today, 10),
              'notes': 'Includes laptop and hybrid work.'})
