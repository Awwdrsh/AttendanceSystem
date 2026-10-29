"""
Groups of companies: hiring works in the ACTIVE company only.

Every page, fragment and action that takes an id answers 404 for a record of another company, for an outsider and
for the group admin while working in a sister company; the record's own company reaches it. Lists show only the
active company's jobs, and the people of a company (members of the group with access, group HR) can sit on panels
and hear about applications there.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from hajiri.db.models import HiringApplication, NotificationOutbox
from hajiri.hiring import services as hiring

from .world import future


@dataclass
class Beta:
    req: str
    job: str
    app: str
    interview: str
    offer: str


def _beta(world) -> Beta:
    """A hiring request, an open job, a candidate with an interview and a draft offer, all in Beta."""
    _, hr = world.session(world.admin_b)
    req = hiring.create_requisition(hr, {'title': 'Baker', 'justification': 'The new oven needs a second shift.'})
    job = hiring.create_job(hr, {'title': 'Baker', 'description': 'Bake the bread our shops sell every morning.'})
    hiring.publish_job(hr, job)
    from hajiri.files.store import check_file, save_file
    cv = save_file(company_id=str(world.beta.id), purpose='hiring_cv', filename='cv.pdf', uploaded_by=hr.id,
                   file=check_file('application/pdf', b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF'))
    app = hiring.add_candidate(hr, job, {'name': 'Hari Baker', 'email': 'hari@bake.test', 'source': 'referral', 'cvFileId': cv['id']})
    iv = hiring.schedule_interview(hr, app, {'date': future(3), 'time': '11:00', 'mode': 'phone', 'panel': [hr.id],
                                             'notifyCandidate': False})
    offer = hiring.create_offer(hr, app, {'designation': 'Baker', 'salary': '30000', 'joinDate': future(20)})
    return Beta(req, job, app, iv, offer)


# (name, method, url) with {job}, {app}, {req}, {interview}, {offer} filled from Beta's records.
H = '/api/v1/hiring'
CASES = [
    ('job', 'get', H + '/jobs/{job}'),
    ('job edit', 'patch', H + '/jobs/{job}'),
    ('job publish', 'post', H + '/jobs/{job}/publish'),
    ('job close', 'post', H + '/jobs/{job}/close'),
    ('job board', 'get', H + '/jobs/{job}/applications'),
    ('request cancel', 'post', H + '/requisitions/{req}/cancel'),
    ('candidate', 'get', H + '/applications/{app}'),
    ('candidate CV', 'get', H + '/applications/{app}/cv'),
    ('candidate export', 'get', H + '/applications/{app}/export'),
    ('candidate update', 'patch', H + '/applications/{app}'),
    ('candidate move', 'post', H + '/applications/{app}/move'),
    ('reopen', 'post', H + '/applications/{app}/reopen'),
    ('note', 'post', H + '/applications/{app}/notes'),
    ('interview update', 'patch', H + '/interviews/{interview}'),
    ('offer letter', 'get', H + '/offers/{offer}/letter'),
    ('offer respond', 'post', H + '/offers/{offer}/respond'),
    ('offer withdraw', 'post', H + '/offers/{offer}/withdraw'),
]

DATA = {'job edit': {'title': 'Head baker'}, 'candidate update': {'rating': 4}, 'candidate move': {'stage': 'Screening'},
        'note': {'body': 'Seen.'}, 'offer respond': {'response': 'accepted'}, 'interview update': {'status': 'completed'}}


def _call(client, method, url, data):
    if method == 'get':
        return client.get(url)
    return getattr(client, method)(url, data, content_type='application/json')


def test_services_refuse_ids_of_a_sister_company(world):
    b = _beta(world)
    _, admin_in_alpha = world.session(world.admin_a)
    for fn, args in ((hiring.get_job, (b.job,)), (hiring.board, (b.job,)), (hiring.application_detail, (b.app,)),
                     (hiring.get_requisition, (b.req,)), (hiring.offer_letter, (b.offer,)),
                     (hiring.update_interview, (b.interview, {'status': 'completed'}))):
        with pytest.raises(Exception) as e:
            fn(admin_in_alpha, *args)
        assert getattr(e.value, 'status', None) == 404, fn.__name__
    assert hiring.list_jobs(admin_in_alpha)['jobs'] == []
    assert hiring.list_requisitions(admin_in_alpha) == []
    assert hiring.list_interviews(admin_in_alpha, {'scope': 'all'}) == []
    assert hiring.reports(admin_in_alpha)['totals']['applications'] == 0
    # The same person, switched to Beta, works there.
    _, admin_in_beta = world.session(world.admin_a, world.beta)
    assert [j['title'] for j in hiring.list_jobs(admin_in_beta)['jobs']] == ['Baker']
    assert hiring.get_job(admin_in_beta, b.job)['job']['company_id'] == str(world.beta.id)


def test_people_of_the_company_sit_on_panels_and_own_candidates(world):
    """Members with access (dual is a manager in Beta) and group-role holders count; Alpha-only people do not."""
    b = _beta(world)
    _, hr = world.session(world.admin_b)
    iv = hiring.schedule_interview(hr, b.app, {'date': future(4), 'time': '10:00', 'mode': 'video',
                                               'panel': [world.dual.id, world.ghr.id], 'notifyCandidate': False})
    assert iv
    with pytest.raises(Exception) as e:
        hiring.schedule_interview(hr, b.app, {'date': future(4), 'time': '12:00', 'mode': 'video', 'panel': [world.solo.id]})
    assert e.value.message == 'every panel member must be an active user in your company'
    hiring.update_application(hr, b.app, {'ownerUserId': world.dual.id})
    with pytest.raises(Exception) as e:
        hiring.update_application(hr, b.app, {'ownerUserId': world.solo.id})
    assert e.value.message == 'owner must be an active user in your company'
    names = [u['name'] for u in hiring.lookups(hr)['users']]
    assert world.dual.name in names and world.ghr.name in names and world.solo.name not in names
    # The member's panel shows up when they work in Beta, not in their home company.
    _, dual_home = world.session(world.dual)
    _, dual_beta = world.session(world.dual, world.beta)
    assert hiring.list_interviews(dual_home, {'scope': 'mine'}) == []
    assert [i['id'] for i in hiring.list_interviews(dual_beta, {'scope': 'mine'})] == [iv]


def test_group_hr_hears_about_applications_in_every_company_of_the_group(world):
    from django.test import Client
    b = _beta(world)
    from hajiri.careers import services as careers
    careers.apply_limiter.reset()
    import base64
    pdf = 'data:application/pdf;base64,' + base64.b64encode(b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF').decode()
    r = Client().post(f'/api/v1/public/careers/companies/BETA/jobs/{b.job}/apply',
                      {'name': 'Sita Applicant', 'email': 'sita@app.test', 'phone': '9800000000', 'cvFilename': 'cv.pdf',
                       'cvDataUrl': pdf, 'consent': True}, content_type='application/json')
    assert r.status_code == 201, r.content
    told = set(NotificationOutbox.objects.filter(template='hiring.applied', company=world.beta).values_list('user_id', flat=True))
    assert {world.ghr.id, world.admin_b.id} <= {str(u) for u in told}
    assert str(world.solo.id) not in {str(u) for u in told}
    assert HiringApplication.objects.for_company(world.beta.id).count() == 2
    assert HiringApplication.objects.for_company(world.alpha.id).count() == 0


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_hiring_ids_of_another_company_are_not_found(world, case):
    name, method, url = case
    b = _beta(world)
    target = url.format(job=b.job, app=b.app, req=b.req, interview=b.interview, offer=b.offer)
    data = DATA.get(name, {})
    r = _call(world.signed_in(world.admin_c), method, target, data)
    assert r.status_code == 404, f'{name}: outsider got {r.status_code}'
    r = _call(world.signed_in(world.admin_a), method, target, data)
    assert r.status_code == 404, f'{name}: group admin working in Alpha got {r.status_code}'
    r = _call(world.signed_in(world.admin_b), method, target, data)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_hub_lists_only_the_active_company_and_follow_a_switch(world):
    _beta(world)
    c = world.signed_in(world.admin_a)
    jobs = c.get('/api/v1/hiring/jobs').json()
    assert jobs['jobs'] == [] and jobs['companyCode'] == 'ALPHA'
    assert b'Baker' not in c.get('/api/v1/hiring/requisitions').content
    assert b'Hari Baker' not in c.get('/api/v1/hiring/interviews').content
    c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type='application/json')
    jobs = c.get('/api/v1/hiring/jobs').json()
    assert [j['title'] for j in jobs['jobs']] and 'Baker' in jobs['jobs'][0]['title']
    assert jobs['companyCode'] == 'BETA'   # the hub links to Beta's own careers address
    board = c.get(f"/api/v1/hiring/jobs/{jobs['jobs'][0]['id']}/applications").content.decode()
    assert 'Hari Baker' in board


def test_group_hr_works_hiring_in_a_sister_company(world):
    b = _beta(world)
    c = world.signed_in(world.ghr, world.alpha)   # group_hr: HR in Alpha too
    assert c.get('/api/v1/hiring/jobs').status_code == 200
    assert c.get(f'/api/v1/hiring/applications/{b.app}').status_code == 404
    c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type='application/json')
    assert c.get(f'/api/v1/hiring/applications/{b.app}').status_code == 200

