"""Feature switches over JSON (TS /api/v1/settings/features)."""
import json

from django.test import Client

from tests.core.fixtures import bearer


def _put(token, body):
    return Client().put('/api/v1/settings/features', json.dumps(body), content_type='application/json', **bearer(token))


def test_everyone_reads_and_hr_changes_the_switches(demo, login_as):
    emp = login_as(['employee'], demo['sita'])
    hr = login_as(['hr'])
    r = Client().get('/api/v1/settings/features', **bearer(emp.token))
    assert r.status_code == 200 and r.json()['enabled']['tasks'] is True
    assert _put(emp.token, {'enabled': {'tasks': False}}).status_code == 403
    bad = _put(hr.token, {'enabled': {'payroll': False}})
    assert bad.status_code == 400 and bad.json()['issues'] == [{'path': 'enabled.payroll', 'message': 'Invalid key in record'}]
    ok = _put(hr.token, {'enabled': {'tasks': False}})
    assert ok.status_code == 200 and ok.json()['enabled']['tasks'] is False
    assert Client().get('/api/v1/settings/features', **bearer(emp.token)).json()['enabled']['tasks'] is False
