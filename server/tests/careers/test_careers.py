"""The public careers site: JSON for other sites (careers/routes.ts) and the /careers pages (CareersPage.tsx)."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from hajiri.careers import services as careers
from tests.support.sql import one, rows
from hajiri.hiring import services as hiring
from tests.hiring.world import QUESTIONS, World, future
from tests.web.factories import make_company, make_user, set_features_off

pytestmark = pytest.mark.django_db

PDF_BYTES = b'%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF'


@pytest.fixture(autouse=True)
def _reset_limiter():
    careers.apply_limiter.reset()
    yield
    careers.apply_limiter.reset()


def _open_job(**extra):
    co = make_company('solo', 'Solo Traders')
    hr = make_user(co, roles=['hr'])
    jid = hiring.create_job(hr, {'title': 'Accountant', 'description': 'Keep the books of a busy trading house in order.',
                                 'requirements': 'You need:\n- CA inter\n- Tally', 'workMode': 'onsite', 'salaryMin': '40000',
                                 'salaryMax': '60000', **extra})
    hiring.publish_job(hr, jid)
    return co, hr, jid


def test_rate_limiter_window():
    lim = careers.RateLimiter(2, 60)
    assert lim.allow('a', 0) and lim.allow('a', 1) and not lim.allow('a', 2)
    assert lim.allow('b', 2)
    assert lim.allow('a', 61.5)


def test_single_company_install_needs_no_code_and_hides_salary_unless_shown():
    co, hr, jid = _open_job()
    c = Client()
    r = c.get('/api/v1/public/careers/jobs')
    assert r.status_code == 200 and r['Cache-Control'] == 'no-store'
    job = r.json()['jobs'][0]
    assert job['salary_min'] is None and job['department'] is None and r.json()['company'] == {'name': 'Solo Traders', 'code': 'solo'}
    hiring.update_job(hr, jid, {'showSalary': True})
    assert c.get('/api/v1/public/careers/jobs').json()['jobs'][0]['salary_min'] == '40000.00'
    assert len(c.get('/api/v1/public/careers/jobs?q=books').json()['jobs']) == 1
    assert len(c.get('/api/v1/public/careers/jobs?q=%25').json()['jobs']) == 0   # % is a literal, not a wildcard
    assert c.get('/api/v1/public/careers/jobs?experience=abc').status_code == 400
    set_features_off(co, 'hiring')
    assert c.get('/api/v1/public/careers/jobs').status_code == 404
    assert c.get(f'/api/v1/public/careers/jobs/{jid}').json() == {'error': 'This job is no longer accepting applications.'}


def test_deadline_passed_closes_the_job_to_applicants():
    co, hr, jid = _open_job()
    from tests.support.sql import execute
    execute("UPDATE hiring_job SET deadline = (now() AT TIME ZONE 'Asia/Kathmandu')::date - 5 WHERE id = %s", [jid])
    assert Client().get(f'/api/v1/public/careers/jobs/{jid}').status_code == 404
    assert Client().get('/api/v1/public/careers/jobs').json()['jobs'] == []


def test_api_apply_creates_candidate_events_and_watchers_once():
    w = World.run('step_apply')
    ev = rows('SELECT kind, body FROM hiring_event WHERE application_id = %s ORDER BY created_at', [w.app_id])
    assert [e['kind'] for e in ev] == ['applied', 'email']
    assert ev[0]['body'] == 'Applied through the careers page (careers)'
    n = one("SELECT payload FROM notification_outbox WHERE template = 'hiring.applied' AND user_id = %s", [w.ram.id])
    assert n['payload'] == {'applicationId': w.app_id, 'jobId': w.job_id}
    mail = one("SELECT title, body, user_id FROM notification_outbox WHERE template = 'hiring.received'")
    assert mail['title'] == 'We received your application for Backend engineer' and mail['user_id'] is None
    assert mail['body'].startswith('Dear Anita Gurung,\n\nThank you for applying for the Backend engineer role at Demo Company.')


def test_answer_keys_longer_than_twenty_characters_are_refused():
    """z.record(z.string().max(20), ...) in careers/routes.ts applyBody."""
    co, hr, jid = _open_job()
    pdf = 'data:application/pdf;base64,' + __import__('base64').b64encode(PDF_BYTES).decode()
    body = {'name': 'Gita Rai', 'email': 'gita@example.com', 'phone': '9800000001', 'cvFilename': 'cv.pdf', 'cvDataUrl': pdf,
            'consent': True, 'answers': {'q' + 'x' * 20: 'yes'}}
    r = Client().post(f'/api/v1/public/careers/jobs/{jid}/apply', body, content_type='application/json')
    assert r.status_code == 400
    assert not rows('SELECT 1 FROM hiring_application WHERE job_id = %s', [jid])
    body['answers'] = {'q' + 'x' * 19: 'yes'}   # unknown but short: dropped
    assert Client().post(f'/api/v1/public/careers/jobs/{jid}/apply', body, content_type='application/json').status_code == 201
