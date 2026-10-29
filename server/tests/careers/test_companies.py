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
from tests.web.factories import set_features_off

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


def test_public_api_needs_no_csrf_token_even_with_a_session(world, jobs):
    """Other sites post applications from their visitors' browsers; a stray session cookie must not demand a token."""
    c = Client(enforce_csrf_checks=True)
    from tests.web.factories import sign_in
    sign_in(c, world.admin_b)
    r = c.post(f'{API}/jobs/{jobs["beta"]}/apply', _body(email='csrf@example.com'), content_type='application/json')
    assert r.status_code == 201
