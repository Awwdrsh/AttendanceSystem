"""The time tracking JSON API (/api/v1/time, the TS routes) for Bearer-token clients."""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import TimeEntry, TimeProject
from hajiri.timesheets import services as ts
from tests.web.factories import PW, bearer, set_features_off

from .helpers import add_days, make_world

API = '/api/v1/time'


@pytest.fixture
def w(db):
    w = make_world()
    w.ids['client'] = ts.create_client(w.hr, {'name': 'Acme Hydro'})['id']
    w.ids['projA'] = ts.create_project(w.hr, {'code': 'HYDRO-1', 'name': 'Hydro survey', 'clientId': w.ids['client'],
                                              'billRate': 2000, 'budgetHours': 10})
    return w


def token(user) -> dict:
    return bearer(auth.login(user.email, PW).token)


def test_timer_and_entries(w):
    c, h = Client(), token(w.sita)
    r = c.post(f'{API}/timer/start', {'projectId': w.ids['projA'], 'note': 'Survey'}, content_type='application/json', **h)
    assert r.status_code == 201
    running = r.json()['running']
    assert (running['project_code'], running['note'], running['duration_min']) == ('HYDRO-1', 'Survey', None)
    assert c.get(f'{API}/timer', **h).json()['running']['id'] == running['id']
    r = c.post(f'{API}/timer/start', {}, content_type='application/json', **h)
    assert r.status_code == 409 and r.json() == {'error': 'A timer is already running. Stop it first.'}
    r = c.post(f'{API}/timer/stop', {'note': 'Done'}, content_type='application/json', **h)
    assert r.status_code == 200 and r.json()['entry']['note'] == 'Done' and r.json()['entry']['duration_min'] >= 1
    r = c.post(f'{API}/entries', {'date': w.MON, 'start': '09:00', 'end': '10:30', 'projectId': w.ids['projA']},
               content_type='application/json', **h)
    assert r.status_code == 201
    e = r.json()['entry']
    assert (e['duration_min'], e['billable'], e['local_date'], e['source']) == (90, True, w.MON, 'manual')
    r = c.patch(f"{API}/entries/{e['id']}", {'durationMin': 30}, content_type='application/json', **h)
    assert r.status_code == 200 and r.json()['entry']['duration_min'] == 30
    r = c.get(f'{API}/entries', {'from': w.WEEK, 'to': add_days(w.WEEK, 6)}, **h)
    assert [x['id'] for x in r.json()['entries']] == [e['id']]
    assert c.delete(f"{API}/entries/{e['id']}", **h).json() == {'ok': True}
    assert not TimeEntry.objects.filter(pk=e['id']).exists()


def test_validation_errors_use_the_ts_shape(w):
    c, h = Client(), token(w.sita)
    r = c.post(f'{API}/entries', {'date': '2026-13-40', 'durationMin': 0}, content_type='application/json', **h)
    assert r.status_code == 400
    body = r.json()
    assert body['error'] == 'validation'
    assert {i['path'] for i in body['issues']} == {'date', 'durationMin'}
    assert {'path': 'durationMin', 'message': 'Too small: expected number to be >=1'} in body['issues']
    r = c.get(f'{API}/entries', {'to': w.MON}, **h)
    assert r.status_code == 400 and r.json()['issues'] == [{'path': 'from', 'message': 'Required'}]
    r = c.patch(f'{API}/entries/not-a-uuid', {}, content_type='application/json', **h)
    assert r.status_code == 400 and r.json()['issues'][0]['message'] == 'Invalid UUID'


def test_week_submit_and_review(w):
    c = Client()
    sita, ram, hr = token(w.sita), token(w.ram), token(w.hr)
    ts.add_entry(w.sita, {'date': w.MON, 'durationMin': 90, 'projectId': w.ids['projA']})
    wk = c.get(f'{API}/week', {'weekStart': w.WEEK}, **sita).json()
    assert wk['weekStart'] == w.WEEK and wk['totals']['trackedMin'] == 90
    r = c.post(f'{API}/timesheets', {'weekStart': w.WEEK, 'note': 'Site week'}, content_type='application/json', **sita)
    assert r.status_code == 201
    tid = r.json()['id']
    listed = c.get(f'{API}/timesheets', **ram).json()['timesheets']
    assert [(t['id'], t['name_en'], t['diff_min']) for t in listed] == [(tid, 'Sita Sharma', 90)]
    assert c.get(f'{API}/timesheets', {'scope': 'all'}, **ram).status_code == 403
    d = c.get(f'{API}/timesheets/{tid}', **ram).json()
    assert d['employee']['name'] == 'Sita Sharma' and d['submission']['status'] == 'submitted'
    team = c.get(f'{API}/team-week', {'weekStart': w.WEEK}, **ram).json()
    assert [(p['name_en'], p['status']) for p in team['people']] == [('Sita Sharma', 'submitted')]
    assert c.get(f'{API}/timesheets/{tid}', **token(w.hari)).status_code == 403
    assert c.post(f'{API}/timesheets/{tid}/withdraw', **sita).json() == {'ok': True}
    assert c.get(f'{API}/team-week', **hr).status_code == 200


def test_catalogue_settings_and_roles(w):
    c, hr, sita = Client(), token(w.hr), token(w.sita)
    r = c.post(f'{API}/clients', {'name': 'Nepal Power'}, content_type='application/json', **sita)
    assert r.status_code == 403 and r.json()['error'].startswith('requires role ')
    r = c.post(f'{API}/clients', {'name': 'Nepal Power'}, content_type='application/json', **hr)
    assert r.status_code == 201 and r.json()['client']['name'] == 'Nepal Power'
    cid = r.json()['client']['id']
    r = c.patch(f'{API}/clients/{cid}', {'archived': True}, content_type='application/json', **hr)
    assert r.json()['client']['archived'] is True
    assert [x['name'] for x in c.get(f'{API}/clients', {'all': '1'}, **hr).json()['clients']] == ['Acme Hydro', 'Nepal Power']
    r = c.post(f'{API}/projects', {'code': 'p2', 'name': 'Second'}, content_type='application/json', **hr)
    assert r.status_code == 201
    pid = r.json()['id']
    assert TimeProject.objects.get(pk=pid).code == 'P2'
    assert c.patch(f'{API}/projects/{pid}', {'status': 'archived'}, content_type='application/json', **hr).json() == {'ok': True}
    assert [p['code'] for p in c.get(f'{API}/projects', **sita).json()['projects']] == ['HYDRO-1']
    r = c.post(f'{API}/activities', {'name': 'Design', 'projectId': w.ids['projA']}, content_type='application/json', **hr)
    assert r.status_code == 201
    aid = r.json()['activity']['id']
    acts = c.get(f'{API}/activities', {'projectId': w.ids['projA']}, **sita).json()['activities']
    assert [(a['name'], a['project_name']) for a in acts] == [('Design', 'Hydro survey')]
    assert c.patch(f'{API}/activities/{aid}', {'archived': True}, content_type='application/json', **hr).json()['activity']['archived']
    assert c.get(f'{API}/settings', **sita).json()['policy']['roundingStep'] == 0
    r = c.put(f'{API}/settings', {'roundingStep': 15, 'roundingMode': 'up'}, content_type='application/json', **hr)
    assert r.status_code == 200 and r.json()['policy']['roundingStep'] == 15
    assert c.put(f'{API}/settings', {'roundingStep': 7}, content_type='application/json', **hr).status_code == 400
    assert c.put(f'{API}/settings', {}, content_type='application/json', **sita).status_code == 403


def test_reports_json_and_csv(w):
    c, hr = Client(), token(w.hr)
    ts.add_entry(w.sita, {'date': w.MON, 'start': '09:00', 'end': '10:30', 'projectId': w.ids['projA']})
    q = {'from': w.WEEK, 'to': add_days(w.WEEK, 6)}
    s = c.get(f'{API}/reports/summary', q, **hr).json()
    assert set(s) == {'columns', 'rows', 'totals', 'rounding'}
    assert s['rows'][0]['code'] == 'HYDRO-1' and s['totals'] == {'hours': 1.5, 'billableHours': 1.5, 'billableAmount': 3000}
    r = c.get(f'{API}/reports/summary', {**q, 'format': 'csv'}, **hr)
    assert r['Content-Type'].startswith('text/csv') and 'Budget used %' in r.content.decode('utf-8-sig')
    e = c.get(f'{API}/reports/entries', q, **hr).json()
    assert set(e) == {'columns', 'rows'} and e['rows'][0]['start'] == '09:00' and e['rows'][0]['end'] == '10:30'
    r = c.get(f'{API}/reports/entries', {**q, 'format': 'csv'}, **hr)
    assert r['Content-Disposition'] == f'attachment; filename="time-entries-{w.WEEK}-to-{add_days(w.WEEK, 6)}.csv"'


def test_feature_off_and_signed_out(w):
    c = Client()
    assert c.get(f'{API}/timer').status_code == 401
    set_features_off(w.company, 'time')
    r = c.get(f'{API}/timer', **token(w.sita))
    assert r.status_code == 404 and r.json()['error'] == 'feature off'


def test_cookie_posts_need_the_csrf_token(w):
    from tests.web.factories import sign_in
    c = Client(enforce_csrf_checks=True)
    sign_in(c, w.sita)
    assert c.post(f'{API}/timer/start', {}, content_type='application/json').status_code == 403
    assert c.get(f'{API}/timer').status_code == 200
