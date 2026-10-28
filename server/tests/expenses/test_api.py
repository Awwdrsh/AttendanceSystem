"""The /api/v1/expenses JSON API (DRF) for Bearer-token clients: the TS routes' paths, statuses and answers."""
from __future__ import annotations

from django.test import Client

from hajiri.core import auth
from tests.web.factories import PW, bearer, set_features_off

from .helpers import days_ago, days_ahead, today


def _api(user):
    token = auth.login(user.email, PW).token
    c = Client()
    h = bearer(token)

    class Api:
        def get(self, url, **q):
            return c.get(url, q, **h)

        def send(self, method, url, body=None):
            return getattr(c, method)(url, body or {}, content_type='application/json', **h)
    return Api()


def test_an_employee_files_and_submits_a_claim_over_the_api(policy_world):
    w = policy_world
    api = _api(w.sita)
    pol = api.get('/api/v1/expenses/policy')
    assert pol.status_code == 200 and pol.json()['myGrade'] == 'G3'
    line = {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Bike', 'km': 10}
    chk = api.send('post', '/api/v1/expenses/claims/check', {'items': [line]})
    assert chk.status_code == 200 and chk.json()['total'] == '200.00'
    made = api.send('post', '/api/v1/expenses/claims', {'title': 'Client visit', 'items': [line]})
    assert made.status_code == 201 and made.json()['status'] == 'draft'
    cid = made.json()['id']
    assert api.send('post', f'/api/v1/expenses/claims/{cid}/submit').json() == {'id': cid, 'status': 'pending'}
    listed = api.get('/api/v1/expenses/claims', scope='own').json()['claims']
    assert [(c['id'], c['status'], c['totalClaimed'], c['items']) for c in listed] == [(cid, 'pending', '200.00', 1)]
    d = api.get(f'/api/v1/expenses/claims/{cid}').json()
    assert d['claim']['employee']['name'] == 'Sita Sharma' and d['items'][0]['km'] == '10.0'
    # The manager decides over the API too.
    ram = _api(w.ram)
    assert ram.send('post', f'/api/v1/expenses/claims/{cid}/review', {'decision': 'approve'}).json() == {'state': 'pending'}


def test_validation_and_refusals_have_the_ts_shape(policy_world):
    w = policy_world
    api = _api(w.sita)
    bad = api.send('post', '/api/v1/expenses/claims', {'title': '', 'items': []})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert {i['path'] for i in bad.json()['issues']} == {'title', 'items'}
    assert api.get('/api/v1/expenses/claims/not-a-uuid').status_code == 400
    hr_only = api.send('put', '/api/v1/expenses/policy/settings', {'mileageRatePerKm': '20', 'limitMode': 'warn', 'claimWindowDays': 60})
    assert hr_only.status_code == 403 and hr_only.json()['error'] == 'requires role hr'
    assert Client().get('/api/v1/expenses/policy').status_code == 401
    set_features_off(w.co, 'expenses')
    assert api.get('/api/v1/expenses/policy').status_code == 404


def test_travel_and_hr_reports_over_the_api(policy_world):
    w = policy_world
    api = _api(w.sita)
    q = api.send('post', '/api/v1/expenses/travel/quote', {'destinationCategory': 'city', 'fromDate': today(), 'toDate': days_ahead(1)})
    assert q.status_code == 200 and q.json()['quote']['daAmount'] == '4000.00'
    t = api.send('post', '/api/v1/expenses/travel', {'destination': 'Pokhara', 'destinationCategory': 'city', 'purpose': 'Client visit',
                                                     'fromDate': today(), 'toDate': days_ahead(1), 'mode': 'bus', 'advanceRequested': '3000'})
    assert t.status_code == 201 and t.json()['status'] == 'pending'
    assert [x['destination'] for x in api.get('/api/v1/expenses/travel').json()['trips']] == ['Pokhara']
    hr = _api(w.hr)
    spend = hr.get('/api/v1/expenses/reports/spend', **{'from': days_ago(30), 'to': today()})
    assert spend.status_code == 200 and spend.json()['columns'][0] == {'key': 'group', 'header': 'Category'}
    csv = hr.get('/api/v1/expenses/reports/pending-payment', format='csv')
    assert csv.status_code == 200 and csv['Content-Type'].startswith('text/csv')
    assert hr.get('/api/v1/expenses/reports/advances').json()['rows'] == []
    assert api.get('/api/v1/expenses/reports/advances').status_code == 403
