"""The React app writes with the cookie session and no CSRF token (frontend/src/api/client.js): the approvals writes
must not be refused when the request comes from the app's own origin."""
import json

import pytest
from django.test import Client

from hajiri.workflow import service

from .helpers import file_leave

pytestmark = pytest.mark.usefixtures('leave_handler')


def _cookie_client(login):
    c = Client(enforce_csrf_checks=True)
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.mark.parametrize('origin', [None, 'http://testserver'])
def test_cookie_session_writes_from_the_app(demo, login_as, origin):
    from tests.support.sql import execute
    execute('UPDATE employee_assignment SET manager_id = %s WHERE employee_id = %s', [demo['ram'], demo['sita']])
    ram = login_as(['manager'], demo['ram'])
    other = login_as(['employee'])
    hdr = {'HTTP_ORIGIN': origin} if origin else {}
    c = _cookie_client(ram)
    f = file_leave(demo['company_id'], demo['sita'], '2026-11-20')
    r = c.post(f"/api/v1/workflow/{f['instanceId']}/approve", '{}', content_type='application/json', **hdr)
    assert r.status_code == 200, r.content
    r = c.post('/api/v1/workflow/delegates', json.dumps({'delegateId': other.user_id, 'from': service.npt_today(),
                                                          'to': service.npt_today()}), content_type='application/json', **hdr)
    assert r.status_code == 201, r.content
    r = c.delete(f"/api/v1/workflow/delegates/{r.json()['id']}", **hdr)
    assert r.status_code == 200, r.content


def test_cookie_session_write_from_a_foreign_origin_is_refused(demo, login_as):
    hr = login_as(['hr'])
    r = _cookie_client(hr).post('/api/v1/workflow/groups', json.dumps({'name': 'x'}), content_type='application/json',
                                HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
