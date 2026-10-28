"""
One careers site per company. Companies of a group share one Hajiri, so the address names the company:
/careers/<code> and /careers/<code>/<job id> for people, /api/v1/public/careers/companies/<code>/jobs... for other
sites. Under a company's address only that company's open jobs exist; the TS addresses (?company=CODE, a bare job
id) keep working.
"""
import base64

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from hajiri.careers import services as careers
from hajiri.db.models import HiringApplication
from hajiri.hiring import services as hiring
from tests.web.factories import htmx, set_features_off

pytestmark = pytest.mark.django_db

PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF'
PDF_URL = 'data:application/pdf;base64,' + base64.b64encode(PDF).decode()
API = '/api/v1/public/careers'


@pytest.fixture(autouse=True)
def _reset_limiter():
    careers.apply_limiter.reset()
    yield
    careers.apply_limiter.reset()


def _job(world, person, company, title):
    _, user = world.session(person, company)
    jid = hiring.create_job(user, {'title': title, 'description': f'{title}: a role described in enough words.'})
    hiring.publish_job(user, jid)
    return jid


@pytest.fixture
def jobs(world):
    return {'alpha': _job(world, world.admin_a, None, 'Alpha cashier'), 'beta': _job(world, world.admin_b, None, 'Beta baker')}


def _body(**extra):
    return {'name': 'Gita Rai', 'email': 'gita@example.com', 'phone': '9800000001', 'cvFilename': 'cv.pdf', 'cvDataUrl': PDF_URL,
            'consent': True, **extra}


def test_each_company_has_its_own_careers_page(world, jobs):
    c = Client()
    alpha = c.get('/careers/ALPHA').content.decode()
    assert 'Work with Alpha Traders' in alpha and 'Alpha cashier' in alpha and 'Beta baker' not in alpha
    assert f'href="/careers/ALPHA/{jobs["alpha"]}"' in alpha
    assert 'action="/careers/ALPHA"' in alpha
    beta = c.get('/careers/beta').content.decode()   # any case
    assert 'Beta baker' in beta and 'Alpha cashier' not in beta
    # Filters stay on the company's address.
    frag = c.get('/careers/BETA?q=baker', **htmx('careers-jobs')).content.decode()
    assert '<html' not in frag and 'Beta baker' in frag
    # Several companies: the bare /careers names none.
    r = c.get('/careers')
    assert r.status_code == 404 and 'Careers page not available' in r.content.decode()
    assert c.get('/careers/NOPE').status_code == 404
    # The TS link form still works and now points at the company's address.
    legacy = c.get('/careers?company=BETA').content.decode()
    assert 'Beta baker' in legacy and f'/careers/BETA/{jobs["beta"]}' in legacy


def test_a_job_is_found_only_under_its_own_company(world, jobs):
    c = Client()
    assert 'Alpha cashier' in c.get(f'/careers/ALPHA/{jobs["alpha"]}').content.decode()
    r = c.get(f'/careers/ALPHA/{jobs["beta"]}')
    assert r.status_code == 404 and 'This job is no longer accepting applications' in r.content.decode()
    assert c.get(f'/careers/GAMMA/{jobs["alpha"]}').status_code == 404
    # A bare job id names its company.
    page = c.get(f'/careers/{jobs["beta"]}').content.decode()
    assert 'Beta baker' in page and 'Careers at Beta Foods' in page
    assert c.get(f'/careers/{jobs["beta"]}?company=ALPHA').status_code == 404


def test_public_api_per_company(world, jobs):
    c = Client()
    r = c.get(f'{API}/companies/ALPHA/jobs')
    assert r.status_code == 200 and r['Cache-Control'] == 'no-store'
    assert r.json()['company'] == {'name': 'Alpha Traders', 'code': 'ALPHA'}
    assert [j['title'] for j in r.json()['jobs']] == ['Alpha cashier']
    assert [j['title'] for j in c.get(f'{API}/jobs?company=BETA').json()['jobs']] == ['Beta baker']
    assert c.get(f'{API}/jobs').status_code == 404
    assert c.get(f'{API}/companies/ALPHA/jobs/{jobs["alpha"]}').json()['job']['title'] == 'Alpha cashier'
    r = c.get(f'{API}/companies/ALPHA/jobs/{jobs["beta"]}')
    assert r.status_code == 404 and r.json() == {'error': 'This job is no longer accepting applications.'}
    # Applying under the wrong company stores nothing.
    r = c.post(f'{API}/companies/ALPHA/jobs/{jobs["beta"]}/apply', _body(), content_type='application/json')
    assert r.status_code == 404 and not HiringApplication.objects.exists()
    r = c.post(f'{API}/companies/BETA/jobs/{jobs["beta"]}/apply', _body(), content_type='application/json')
    assert r.status_code == 201 and r.json() == {'ok': True, 'message': careers.OK_MESSAGE}
    app = HiringApplication.objects.get()
    assert str(app.company_id) == str(world.beta.id) and app.candidate.company_id == world.beta.id
    # The same address applying to the sister company is a new candidate there (candidates belong to one company).
    c.post(f'{API}/companies/ALPHA/jobs/{jobs["alpha"]}/apply', _body(), content_type='application/json')
    assert HiringApplication.objects.for_company(world.alpha.id).count() == 1
    assert HiringApplication.objects.for_company(world.beta.id).count() == 1


def test_switching_careers_off_in_one_company_leaves_the_other(world, jobs):
    set_features_off(world.alpha, 'hiring')
    c = Client()
    assert c.get('/careers/ALPHA').status_code == 404
    assert c.get(f'{API}/jobs/{jobs["alpha"]}').status_code == 404
    assert 'Beta baker' in c.get('/careers/BETA').content.decode()


def test_the_page_form_applies_to_the_company_in_the_address(world, jobs):
    c = Client()
    form = c.get(f'/careers/BETA/{jobs["beta"]}').content.decode()
    assert f'/_/careers/{jobs["beta"]}/apply?company=BETA' in form
    data = {'name': 'Anita Gurung', 'email': 'anita@example.com', 'phone': '+977 9800000000', 'consent': '1'}
    r = c.post(f'/_/careers/{jobs["beta"]}/apply?company=ALPHA', {**data, 'cv': SimpleUploadedFile('cv.pdf', PDF, 'application/pdf')},
               **htmx('apply'))
    assert r.status_code == 422 and careers.GONE in r.content.decode()
    r = c.post(f'/_/careers/{jobs["beta"]}/apply?company=BETA', {**data, 'cv': SimpleUploadedFile('cv.pdf', PDF, 'application/pdf')},
               **htmx('apply'))
    assert r.status_code == 200 and careers.OK_MESSAGE in r.content.decode()
    assert HiringApplication.objects.for_company(world.beta.id).count() == 1


def test_public_api_needs_no_csrf_token_even_with_a_session(world, jobs):
    """Other sites post applications from their visitors' browsers; a stray session cookie must not demand a token."""
    c = Client(enforce_csrf_checks=True)
    from tests.web.factories import sign_in
    sign_in(c, world.admin_b)
    r = c.post(f'{API}/jobs/{jobs["beta"]}/apply', _body(email='csrf@example.com'), content_type='application/json')
    assert r.status_code == 201


def test_hiring_hub_links_to_the_company_careers_address(world, jobs):
    c = world.signed_in(world.admin_b)
    page = c.get('/hiring').content.decode()
    assert 'href="http://testserver/careers/BETA"' in page
    assert f'/careers/BETA/{jobs["beta"]}' in page
