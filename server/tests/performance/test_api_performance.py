"""
The /api/v1/performance endpoints as the React app calls them (frontend/src/api/performance.js): the session cookie
and JSON bodies, never a CSRF token. A port of the HTTP-level assertions of apps/api/test/performance.test.ts
(65a9079; the learning block is in tests/learning/test_api_learning.py), plus the response shapes the pages read, the
error bodies, the feature switch, the Origin check and company isolation (including a group of companies).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.learning import services as lrn
from tests.performance.test_isolation import Beta
from tests.multicompany.conftest import world  # noqa: F401  (the fixture)
from tests.support.sql import one
from tests.web.factories import PW, set_features_off

from .world import build, outbox, plus_days, today

P = '/api/v1/performance'
L = '/api/v1/learning'
NOWHERE = '00000000-0000-7000-8000-00000000ffff'


class Browser:
    """The React client: the hajiri_session cookie and JSON bodies (client.js), never a CSRF token."""

    def __init__(self, user, origin: str | None = None, token: str | None = None):
        self.c = Client()
        self.c.cookies[auth.SESSION_COOKIE] = token or auth.login(user.email, PW).token
        self.h = {'HTTP_ORIGIN': origin} if origin else {}

    def get(self, path, query=None):
        return self.c.get(path if path.startswith('/api') else f'{P}{path}', query or {}, **self.h)

    def _send(self, method, path, body):
        return getattr(self.c, method)(path if path.startswith('/api') else f'{P}{path}',
                                       json.dumps(body if body is not None else {}), content_type='application/json', **self.h)

    def post(self, path, body=None):
        return self._send('post', path, body)

    def put(self, path, body=None):
        return self._send('put', path, body)

    def patch(self, path, body=None):
        return self._send('patch', path, body)

    def delete(self, path):
        return self.c.delete(path if path.startswith('/api') else f'{P}{path}', **self.h)


@pytest.fixture
def w(db):
    w = build()
    for k in ('hr', 'sita', 'ram', 'hari', 'gita', 'other_hr'):
        setattr(w, f'b_{k}', Browser(getattr(w, k)))
    return w


def tomorrow_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace('+00:00', 'Z')


def ids(rows) -> list[str]:
    return [r['id'] for r in rows]


# ---------------------------------------------------------------- goals and key results

def _sita_goal(w) -> str:
    r = w.b_sita.post('/goals', {'title': 'Ship the attendance app', 'weight': 60, 'keyResults': [
        {'title': 'Screens done', 'measure': 'number', 'startValue': 0, 'targetValue': 10, 'currentValue': 5},
        {'title': 'Released', 'measure': 'boolean'}]})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    return r.json()['id']


def test_goals_key_results_and_checkins(w):
    sita, ram, hari, hr, other = w.b_sita, w.b_ram, w.b_hari, w.b_hr, w.b_other_hr
    goal = _sita_goal(w)
    g = sita.get(f'/goals/{goal}')
    assert g.status_code == 200
    body = g.json()
    assert set(body) == {'goal', 'checkins', 'children', 'canEdit'} and body['canEdit'] is True
    assert float(body['goal']['progress']) == 25
    assert {'id', 'scope', 'org_unit_id', 'org_unit_name', 'employee_id', 'employee_name', 'emp_code', 'parent_id', 'parent_title',
            'title', 'description', 'weight', 'start_date', 'due_date', 'status', 'progress', 'created_at', 'updated_at',
            'key_results', 'last_checkin'} <= set(body['goal'])
    kr = next(k for k in body['goal']['key_results'] if k['title'] == 'Released')
    assert set(kr) == {'id', 'title', 'measure', 'start_value', 'target_value', 'current_value', 'unit', 'weight'}

    # A check-in moves the progress and tells the manager.
    before = outbox('goal.checkin')
    r = sita.post(f'/goals/{goal}/checkins', {'keyResultId': kr['id'], 'value': 1, 'confidence': 3, 'comment': 'Live'})
    assert r.status_code == 201 and r.json() == {'ok': True}
    g2 = sita.get(f'/goals/{goal}').json()
    assert float(g2['goal']['progress']) == 75 and len(g2['checkins']) == 1
    assert g2['goal']['last_checkin']['comment'] == 'Live'
    assert {'key_result_title', 'author_name', 'progress', 'confidence'} <= set(g2['checkins'][0])
    assert outbox('goal.checkin') == before + 1
    # Manual progress is refused when key results drive it.
    r = sita.patch(f'/goals/{goal}', {'progress': 90})
    assert r.status_code == 400 and r.json() == {'error': 'progress comes from the key results; update those instead'}

    # validates input
    r = sita.post('/goals', {'title': ''})
    assert r.status_code == 400 and r.json()['error'] == 'validation' and r.json()['issues'][0]['path'] == 'title'
    assert sita.post('/goals', {'title': 'x', 'startDate': '2026-10-10', 'dueDate': '2026-10-01'}).status_code == 400
    assert sita.post(f'/goals/{goal}/checkins', {}).status_code == 400
    assert sita.get('/goals/not-a-uuid').status_code == 400

    # Managers set goals for their team; colleagues and other companies cannot see them.
    assert ram.post('/goals', {'employeeId': w.SITA, 'title': 'Mentor Hari', 'status': 'active'}).status_code == 201
    r = hari.post('/goals', {'employeeId': w.SITA, 'title': 'Nope'})
    assert r.status_code == 403 and r.json() == {'error': 'you can set goals only for yourself and your team'}
    assert hari.get(f'/goals/{goal}').status_code == 404
    assert hari.get('/goals', {'scope': 'employee', 'employeeId': w.SITA}).status_code == 403
    assert other.get(f'/goals/{goal}').status_code == 404
    assert hr.post('/goals', {'employeeId': w.OTHER_EMP, 'title': 'x'}).status_code == 404
    assert goal in ids(ram.get('/goals', {'scope': 'team'}).json()['goals'])
    assert hari.get('/goals', {'scope': 'team'}).json() == {'goals': []}
    assert goal in ids(sita.get('/goals').json()['goals'])          # scope defaults to mine
    assert hari.get('/goals', {'scope': 'all'}).status_code == 403
    assert ram.patch(f'/goals/{goal}', {'weight': 50}).json() == {'ok': True}
    assert hari.patch(f'/goals/{goal}', {'weight': 50}).status_code == 404

    # Key results: add, change, delete (progress follows).
    r = sita.post(f'/goals/{goal}/key-results', {'title': 'Docs', 'targetValue': 4})
    assert r.status_code == 201 and r.json() == {'ok': True}
    docs = next(k for k in sita.get(f'/goals/{goal}').json()['goal']['key_results'] if k['title'] == 'Docs')
    assert sita.patch(f'/key-results/{docs["id"]}', {'currentValue': 4}).json() == {'ok': True}
    assert hari.patch(f'/key-results/{docs["id"]}', {'currentValue': 0}).status_code == 404
    assert other.delete(f'/key-results/{docs["id"]}').status_code == 404
    assert sita.delete(f'/key-results/{docs["id"]}').json() == {'ok': True}
    assert sita.delete(f'/key-results/{docs["id"]}').status_code == 404

    # Company goals are set by HR and visible to everyone; personal goals align to them.
    r = sita.post('/goals', {'scope': 'company', 'title': 'Grow 30%'})
    assert r.status_code == 403 and r.json() == {'error': 'only HR sets company goals'}
    c = hr.post('/goals', {'scope': 'company', 'title': 'Grow 30%'})
    assert c.status_code == 201
    assert c.json()['id'] in ids(hari.get('/goals', {'scope': 'company'}).json()['goals'])
    al = hari.post('/goals', {'title': 'Close 10 deals', 'parentId': c.json()['id']})
    assert al.status_code == 201
    assert al.json()['id'] in ids(hr.get(f'/goals/{c.json()["id"]}').json()['children'])
    assert hari.post('/goals', {'title': 'x', 'parentId': goal}).status_code == 404
    assert hari.get(f'/goals/{c.json()["id"]}').json()['canEdit'] is False
    assert hari.patch(f'/goals/{c.json()["id"]}', {'title': 'Mine now'}).status_code == 403

    # Cancel (DELETE) is a soft cancel; a cancelled goal takes no check-ins.
    assert sita.delete(f'/goals/{al.json()["id"]}').status_code == 404       # not hers
    assert hari.delete(f'/goals/{al.json()["id"]}').json() == {'ok': True}
    r = hari.post(f'/goals/{al.json()["id"]}/checkins', {'comment': 'late'})
    assert r.status_code == 400 and r.json() == {'error': 'this goal was cancelled'}

    # Pickers.
    deps = hr.get('/departments')
    assert deps.status_code == 200 and deps.json() == {'departments': []}
    col = sita.get('/colleagues', {'q': 'Hari'}).json()['colleagues']
    assert [x['name_en'] for x in col] == ['Hari Karki']
    assert set(col[0]) == {'id', 'emp_code', 'name_en', 'designation', 'department', 'manager_id'}
    assert w.OTHER_EMP not in ids(hr.get('/colleagues').json()['colleagues'])


def test_kpi_library(w):
    sita, hari, hr, other = w.b_sita, w.b_hari, w.b_hr, w.b_other_hr
    r = sita.post('/kpis', {'title': 'x'})
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    k = hr.post('/kpis', {'designation': 'Engineer', 'title': 'Bugs fixed per month', 'defaultTarget': 20})
    assert k.status_code == 201
    kid = k.json()['id']
    lib = sita.get('/kpis', {'designation': 'Engineer'}).json()['kpis']
    assert len(lib) == 1
    assert set(lib[0]) == {'id', 'designation', 'title', 'description', 'measure', 'default_target', 'unit', 'active'}
    g = sita.post(f'/kpis/{kid}/apply', {'weight': 40})
    assert g.status_code == 201
    goal = sita.get(f'/goals/{g.json()["id"]}').json()
    assert float(goal['goal']['key_results'][0]['target_value']) == 20
    assert hari.post(f'/kpis/{kid}/apply', {'employeeId': w.SITA}).status_code == 403
    assert other.post(f'/kpis/{kid}/apply', {}).status_code == 404
    # HR edits and retires KPIs; another company's HR cannot.
    assert sita.patch(f'/kpis/{kid}', {'title': 'x'}).status_code == 403
    assert hr.patch(f'/kpis/{kid}', {'unit': 'bugs', 'designation': ''}).json() == {'ok': True}
    assert other.patch(f'/kpis/{kid}', {'title': 'x'}).status_code == 404
    assert other.delete(f'/kpis/{kid}').status_code == 404
    assert hr.delete(f'/kpis/{kid}').json() == {'ok': True}
    assert sita.get('/kpis').json() == {'kpis': []}
    assert [x['active'] for x in hr.get('/kpis').json()['kpis']] == [False]
    assert sita.post(f'/kpis/{kid}/apply', {}).status_code == 404


# ---------------------------------------------------------------- review cycles

def test_review_cycle_end_to_end(w):
    sita, ram, hari, gita, hr, other = w.b_sita, w.b_ram, w.b_hari, w.b_gita, w.b_hr, w.b_other_hr
    _sita_goal(w)

    # HR defines competencies and a cycle; weights must add up to 100.
    assert sita.post('/competencies', {'kind': 'skill', 'name': 'x'}).status_code == 403
    c = hr.post('/competencies', {'kind': 'skill', 'name': 'Problem solving', 'indicators': [{'level': 3, 'text': 'Solves routine problems alone'}]})
    assert c.status_code == 201
    hr.post('/competencies', {'kind': 'knowledge', 'name': 'Finance', 'designations': ['Accountant']})
    comps = sita.get('/competencies', {'designation': 'Engineer'}).json()['competencies']
    assert [x['name'] for x in comps] == ['Problem solving']
    assert set(comps[0]) == {'id', 'kind', 'name', 'description', 'indicators', 'designations', 'grades', 'active'}
    assert comps[0]['indicators'] == [{'level': 3, 'text': 'Solves routine problems alone'}]
    assert sita.get('/competencies', {'all': '2'}).status_code == 400
    assert sita.patch(f'/competencies/{c.json()["id"]}', {'active': False}).status_code == 403
    assert other.patch(f'/competencies/{c.json()["id"]}', {'active': False}).status_code == 404
    assert hr.patch(f'/competencies/{c.json()["id"]}', {'description': 'Thinks it through'}).json() == {'ok': True}

    bad = hr.post('/cycles', {'name': 'FY 2083', 'periodStart': '2026-07-17', 'periodEnd': '2027-07-16', 'goalsWeight': 70,
                              'competencyWeight': 40})
    assert bad.status_code == 400
    course = lrn.create_course(w.hr, {'code': 'LEAD', 'title': 'Leadership basics', 'cost': '5000.00'})
    assert sita.post('/cycles', {'name': 'x', 'periodStart': '2026-01-01', 'periodEnd': plus_days(200)}).status_code == 403
    ok = hr.post('/cycles', {'name': 'FY 2083', 'periodStart': '2026-01-01', 'periodEnd': plus_days(200), 'peerEnabled': True,
                             'peerMin': 2, 'selfDue': plus_days(3)})
    assert ok.status_code == 201
    cycle = ok.json()['id']
    assert sita.get(f'/cycles/{cycle}').status_code == 404
    assert other.get(f'/cycles/{cycle}').status_code == 404
    assert [x['id'] for x in hr.get('/cycles').json()['cycles']] == [cycle]
    assert sita.get('/cycles').json() == {'cycles': []}            # a draft is HR's only

    # Launch creates a review per employee with their goals and the competencies of their role.
    assert ram.post(f'/cycles/{cycle}/launch').status_code == 403
    assert other.post(f'/cycles/{cycle}/launch').status_code == 404
    launch = hr.post(f'/cycles/{cycle}/launch')
    assert launch.status_code == 200 and launch.json() == {'ok': True, 'participants': 4}
    r = hr.post(f'/cycles/{cycle}/launch')
    assert r.status_code == 409 and r.json() == {'error': 'this cycle was already launched'}
    assert hr.patch(f'/cycles/{cycle}', {'goalsWeight': 50, 'competencyWeight': 50}).status_code == 409
    assert hr.delete(f'/cycles/{cycle}').status_code == 409
    detail = ram.get(f'/cycles/{cycle}').json()
    assert set(detail) == {'cycle', 'reviews'} and len(detail['reviews']) == 3       # Ram's reports
    mine = sita.get('/reviews').json()['reviews']
    review = mine[0]['id']
    assert mine[0]['final_rating'] is None and mine[0]['manager_score'] is None and mine[0]['cycle_name'] == 'FY 2083'
    rv = sita.get(f'/reviews/{review}').json()
    assert set(rv) == {'role', 'cycle', 'review', 'items', 'peers', 'journal', 'can'}
    assert rv['role'] == 'self' and rv['can']['editSelf'] is True
    types = [f"{i['item_type']}:{i['title']}" for i in rv['items']]
    assert 'goal:Ship the attendance app' in types and 'competency:Problem solving' in types
    assert 'competency:Finance' not in types
    assert ram.get(f'/reviews/{review}').json()['role'] == 'manager'
    assert hari.get(f'/reviews/{review}').status_code == 404
    assert other.get(f'/reviews/{review}').status_code == 404
    assert review in ids(ram.get('/reviews', {'scope': 'team'}).json()['reviews'])

    # Self review first: the manager waits, incomplete ratings are refused.
    assert ram.put(f'/reviews/{review}/manager', {'summary': 'early'}).status_code == 409
    assert ram.put(f'/reviews/{review}/self', {'summary': 'x'}).status_code == 404
    partial = sita.put(f'/reviews/{review}/self', {'items': [{'id': rv['items'][0]['id'], 'rating': 4}], 'submit': True})
    assert partial.status_code == 400 and partial.json() == {'error': 'rate every goal and competency before submitting'}
    every = [{'id': i['id'], 'rating': 4, 'comment': 'Did well'} for i in rv['items']]
    assert sita.put(f'/reviews/{review}/self', {'items': every, 'summary': 'Good year', 'submit': True}).json() == {'ok': True}
    assert sita.put(f'/reviews/{review}/self', {'summary': 'again'}).status_code == 409
    assert float(one('SELECT self_score FROM perf_review WHERE id = %s', [review])['self_score']) == 4

    # Peers are nominated by the employee and approved by the manager.
    assert sita.post(f'/reviews/{review}/peers', {'employeeIds': [w.SITA]}).status_code == 400
    assert sita.post(f'/reviews/{review}/peers', {'employeeIds': [w.OTHER_EMP]}).status_code == 404
    n = sita.post(f'/reviews/{review}/peers', {'employeeIds': [w.HARI, w.GITA]})
    assert n.status_code == 201 and n.json() == {'ok': True, 'added': 2}
    for p in ram.get(f'/reviews/{review}').json()['peers']['nominations']:
        r = sita.post(f'/peers/{p["id"]}/decision', {'approve': True})
        assert r.status_code == 403 and r.json() == {'error': 'the manager approves peer reviewers'}
        assert ram.post(f'/peers/{p["id"]}/decision', {'approve': True}).json() == {'ok': True}
        assert other.post(f'/peers/{p["id"]}/decision', {'approve': True}).status_code == 404
    reqs = hari.get('/peer-requests').json()['requests']
    assert len(reqs) == 1 and reqs[0]['employee_name'] == 'Sita Sharma'

    # Manager review with a recommendation, then peers respond.
    assert hr.post(f'/cycles/{cycle}/advance').json() == {'ok': True, 'status': 'manager_review'}
    rv = ram.get(f'/reviews/{review}').json()
    assert rv['can']['editManager'] is True
    items = [{'id': i['id'], 'rating': 5 if i['item_type'] == 'goal' else 3, 'comment': 'Solid'} for i in rv['items']]
    assert hari.put(f'/reviews/{review}/manager', {'items': items}).status_code == 404
    bad = ram.put(f'/reviews/{review}/manager', {'items': items, 'recommendation': {'trainingCourseIds': [NOWHERE]}})
    assert bad.status_code == 400
    assert ram.put(f'/reviews/{review}/manager', {
        'items': items, 'summary': 'Strong delivery', 'submit': True,
        'recommendation': {'incrementPct': 10, 'promotion': True, 'trainingCourseIds': [course]}}).json() == {'ok': True}
    assert float(one('SELECT manager_score FROM perf_review WHERE id = %s', [review])['manager_score']) == 4.2
    assert hr.post(f'/cycles/{cycle}/advance').json()['status'] == 'peer_review'
    for b, rating in ((hari, 4), (gita, 5)):
        pr = b.get('/peer-requests').json()['requests'][0]
        assert b.put(f'/peers/{pr["id"]}', {'rating': rating, 'strengths': f'strength {rating}'}).json() == {'ok': True}
        assert b.put(f'/peers/{pr["id"]}', {'rating': rating}).status_code == 409
    # Before release the employee sees neither manager ratings nor peer feedback.
    self_view = sita.get(f'/reviews/{review}').json()
    assert self_view['review']['final_rating'] is None
    assert all(i['manager_rating'] is None for i in self_view['items'])
    assert self_view['peers']['aggregate'] is None

    # Calibration needs a reason and is audited; release shows results and creates training.
    assert hr.post(f'/reviews/{review}/calibrate', {'finalRating': 4, 'reason': 'adjust'}).status_code == 409
    assert hr.post(f'/cycles/{cycle}/advance').json()['status'] == 'calibration'
    cal = hr.get(f'/cycles/{cycle}/calibration')
    assert set(cal.json()) == {'cycle', 'reviews', 'distribution'}
    assert cal.json()['distribution']['overall'][3] == 1
    assert set(cal.json()['distribution']) == {'overall', 'byDepartment'}
    assert ram.get(f'/cycles/{cycle}/calibration').status_code == 403
    assert other.get(f'/cycles/{cycle}/calibration').status_code == 404
    assert hr.post(f'/reviews/{review}/calibrate', {'finalRating': 4}).status_code == 400
    assert ram.post(f'/reviews/{review}/calibrate', {'finalRating': 4, 'reason': 'Bring in line'}).status_code == 403
    assert hr.post(f'/reviews/{review}/calibrate', {'finalRating': 4, 'reason': 'Bring in line with peers'}).json() == {'ok': True}
    assert one("SELECT count(*)::int AS n FROM audit_log WHERE table_name = 'perf_review' AND action = 'calibrate' AND row_id = %s",
               [review])['n'] == 1
    assert sita.post(f'/reviews/{review}/acknowledge', {}).status_code == 409
    assert hr.post(f'/cycles/{cycle}/advance').json()['status'] == 'released'
    self_view = sita.get(f'/reviews/{review}').json()
    assert float(self_view['review']['final_rating']) == 4
    assert 'recommendation' not in self_view['review']
    agg = self_view['peers']['aggregate']
    assert agg['shown'] is True and agg['count'] == 2 and agg['averageRating'] == 4.5
    assert 'Hari' not in json.dumps(agg) and 'Gita' not in json.dumps(agg) and 'reviewer' not in json.dumps(agg)
    assert all(p['status'] != 'submitted' for p in self_view['peers']['nominations'])
    enr = one('SELECT status, source FROM lrn_enrollment WHERE employee_id = %s AND course_id = %s', [w.SITA, course])
    assert enr == {'status': 'enrolled', 'source': 'review'}
    assert sita.post(f'/reviews/{review}/acknowledge', {'comment': 'Thanks'}).json() == {'ok': True}
    assert sita.post(f'/reviews/{review}/acknowledge', {}).status_code == 409

    # Prints a summary; the employee copy leaves out the recommendation.
    mine = sita.get(f'/reviews/{review}/print')
    assert mine.status_code == 200 and mine['Content-Type'].startswith('text/html')
    assert mine['Cache-Control'] == 'private, no-store'
    text = mine.content.decode()
    assert 'Sita Sharma' in text and 'Recommendation' not in text
    assert 'Increment 10%' in ram.get(f'/reviews/{review}/print').content.decode()
    assert hari.get(f'/reviews/{review}/print').status_code == 404
    assert other.get(f'/reviews/{review}/print').status_code == 404

    # The manager's journal shows in the manager's view of the review.
    j = ram.post('/journal', {'employeeId': w.SITA, 'noteDate': today(), 'tag': 'positive', 'note': 'Handled the outage calmly'})
    assert j.status_code == 201
    assert 'Handled the outage calmly' in [x['note'] for x in ram.get(f'/reviews/{review}').json()['journal']]


def test_cycle_edit_delete_and_peer_removal(w):
    hr, sita, ram, other = w.b_hr, w.b_sita, w.b_ram, w.b_other_hr
    cycle = hr.post('/cycles', {'name': 'Draft', 'periodStart': '2026-01-01', 'periodEnd': plus_days(50)}).json()['id']
    assert sita.patch(f'/cycles/{cycle}', {'name': 'x'}).status_code == 403
    assert other.patch(f'/cycles/{cycle}', {'name': 'x'}).status_code == 404
    assert hr.patch(f'/cycles/{cycle}', {'name': 'Mid-year', 'peerEnabled': True}).json() == {'ok': True}
    got = hr.get(f'/cycles/{cycle}').json()
    assert got['cycle']['name'] == 'Mid-year' and got['reviews'] == []
    assert other.delete(f'/cycles/{cycle}').status_code == 404
    assert sita.delete(f'/cycles/{cycle}').status_code == 403
    assert hr.delete(f'/cycles/{cycle}').json() == {'ok': True}
    assert hr.get(f'/cycles/{cycle}').status_code == 404

    cycle = hr.post('/cycles', {'name': 'H2', 'periodStart': '2026-01-01', 'periodEnd': plus_days(50), 'peerEnabled': True}).json()['id']
    hr.post(f'/cycles/{cycle}/launch')
    review = sita.get('/reviews').json()['reviews'][0]['id']
    sita.post(f'/reviews/{review}/peers', {'employeeIds': [w.HARI]})
    peer = sita.get(f'/reviews/{review}').json()['peers']['nominations'][0]['id']
    assert other.delete(f'/peers/{peer}').status_code == 404
    assert sita.delete(f'/peers/{peer}').json() == {'ok': True}
    sita.post(f'/reviews/{review}/peers', {'employeeIds': [w.HARI]})
    peer = sita.get(f'/reviews/{review}').json()['peers']['nominations'][0]['id']
    ram.post(f'/peers/{peer}/decision', {'approve': True})
    r = sita.delete(f'/peers/{peer}')
    assert r.status_code == 403 and r.json() == {'error': 'ask your manager to remove an approved reviewer'}
    assert ram.delete(f'/peers/{peer}').json() == {'ok': True}


# ---------------------------------------------------------------- journal, feedback and 1-on-1s

def test_journal_is_private_to_its_author_and_hr(w):
    sita, ram, hari, hr, other = w.b_sita, w.b_ram, w.b_hari, w.b_hr, w.b_other_hr
    r = hari.post('/journal', {'employeeId': w.SITA, 'noteDate': today(), 'note': 'x'})
    assert r.status_code == 403 and r.json() == {'error': "only the employee's managers and HR keep a journal"}
    j = ram.post('/journal', {'employeeId': w.SITA, 'noteDate': today(), 'tag': 'positive', 'note': 'Handled the outage calmly'})
    assert j.status_code == 201 and set(j.json()) == {'id'}
    assert sita.get('/journal', {'employeeId': w.SITA}).status_code == 403
    entries = ram.get('/journal', {'employeeId': w.SITA}).json()['entries']
    assert len(entries) == 1
    assert set(entries[0]) == {'id', 'note_date', 'tag', 'note', 'created_at', 'author_id', 'author_name'}
    assert len(hr.get('/journal', {'employeeId': w.SITA}).json()['entries']) == 1
    assert other.get('/journal', {'employeeId': w.SITA}).status_code == 404
    assert ram.get('/journal').status_code == 400                       # employeeId is required
    r = hr.delete(f'/journal/{j.json()["id"]}')
    assert r.status_code == 404 and r.json() == {'error': 'note not found'}
    assert ram.delete(f'/journal/{j.json()["id"]}').json() == {'ok': True}


def test_feedback_private_notes_and_requests(w):
    sita, ram, hari, gita, other = w.b_sita, w.b_ram, w.b_hari, w.b_gita, w.b_other_hr
    r = sita.post('/feedback', {'toEmployeeId': w.SITA, 'body': 'me'})
    assert r.status_code == 400 and r.json() == {'error': 'you cannot give feedback to yourself'}
    assert hari.post('/feedback', {'toEmployeeId': w.SITA, 'body': 'Great code reviews'}).status_code == 201
    r = gita.post('/feedback', {'toEmployeeId': w.SITA, 'body': 'Misses standups', 'privateToManager': True})
    assert r.status_code == 201 and r.json() == {'ok': True}
    recv = sita.get('/feedback', {'box': 'received'}).json()['feedback']
    assert [f['body'] for f in recv] == ['Great code reviews']
    assert {'id', 'status', 'prompt', 'body', 'private_to_manager', 'created_at', 'given_at', 'giver_employee_id', 'giver_name',
            'subject_employee_id', 'subject_name', 'requester_employee_id', 'requester_name'} == set(recv[0])
    assert len(ram.get('/feedback', {'box': 'about', 'employeeId': w.SITA}).json()['feedback']) == 2
    assert hari.get('/feedback', {'box': 'about', 'employeeId': w.SITA}).status_code == 403
    assert ram.get('/feedback', {'box': 'about'}).status_code == 400
    assert hari.post('/feedback', {'toEmployeeId': w.OTHER_EMP, 'body': 'x'}).status_code == 404

    # Requests are answered or declined by the colleague asked.
    r = sita.post('/feedback/requests', {'fromEmployeeIds': [w.GITA], 'prompt': 'How was the launch?'})
    assert r.status_code == 201 and r.json() == {'ok': True}
    assert hari.post('/feedback/requests', {'fromEmployeeIds': [w.GITA], 'aboutEmployeeId': w.SITA}).status_code == 403
    inbox = gita.get('/feedback', {'box': 'requests'}).json()['feedback']
    req = inbox[0]['id']
    assert hari.put(f'/feedback/{req}', {'body': 'x'}).status_code == 404
    assert other.post(f'/feedback/{req}/decline').status_code == 403     # no employee record (the TS me())
    assert gita.put(f'/feedback/{req}', {'body': 'Smooth launch'}).json() == {'ok': True}
    r = gita.post(f'/feedback/{req}/decline')
    assert r.status_code == 409 and r.json() == {'error': 'this request was already answered'}
    mine = sita.get('/feedback', {'box': 'requests'}).json()['feedback']
    assert mine[0]['status'] == 'given' and mine[0]['body'] == 'Smooth launch'
    # Declining an open request.
    sita.post('/feedback/requests', {'fromEmployeeIds': [w.HARI]})
    req = hari.get('/feedback', {'box': 'requests'}).json()['feedback'][0]['id']
    assert hari.post(f'/feedback/{req}/decline').json() == {'ok': True}


def test_one_on_ones(w):
    sita, ram, hari, hr, other = w.b_sita, w.b_ram, w.b_hari, w.b_hr, w.b_other_hr
    r = hari.post('/one-on-ones', {'withEmployeeId': w.SITA, 'scheduledAt': tomorrow_iso()})
    assert r.status_code == 403 and r.json() == {'error': '1-on-1s are between a person and their direct manager'}
    m = ram.post('/one-on-ones', {'withEmployeeId': w.SITA, 'scheduledAt': tomorrow_iso(), 'cadence': 'weekly', 'agenda': ['Career plan']})
    assert m.status_code == 201
    mid = m.json()['id']
    assert hari.get(f'/one-on-ones/{mid}').status_code == 404
    r = hr.get(f'/one-on-ones/{mid}')
    assert r.status_code == 403 and r.json() == {'error': 'this account is not linked to an employee'}
    assert other.get(f'/one-on-ones/{mid}').status_code == 403      # no employee record either
    assert ram.patch(f'/one-on-ones/{mid}', {'privateNotes': 'Considering promotion'}).json() == {'ok': True, 'nextId': None}
    assert sita.patch(f'/one-on-ones/{mid}', {'privateNotes': 'Ask about training budget', 'sharedNotes': 'Agreed on Q2 focus'}).status_code == 200
    as_sita = sita.get(f'/one-on-ones/{mid}')
    body = as_sita.json()
    assert set(body) == {'meeting', 'items', 'openActions'}
    assert body['meeting']['private_notes'] == 'Ask about training budget' and body['meeting']['side'] == 'employee'
    assert body['meeting']['shared_notes'] == 'Agreed on Q2 focus'
    assert 'Considering promotion' not in as_sita.content.decode()
    assert [i['text'] for i in body['items']] == ['Career plan']
    r = sita.post(f'/one-on-ones/{mid}/items', {'kind': 'action', 'text': 'Draft plan', 'ownerEmployeeId': w.HARI})
    assert r.status_code == 400
    item = sita.post(f'/one-on-ones/{mid}/items', {'kind': 'action', 'text': 'Draft plan', 'ownerEmployeeId': w.SITA})
    assert item.status_code == 201 and set(item.json()) == {'id'}
    listing = ram.get('/one-on-ones').json()
    assert set(listing) == {'meetings', 'reports', 'manager'}
    assert mid in ids(listing['meetings']) and listing['manager'] is None
    assert w.SITA in ids(listing['reports'])
    assert sita.get('/one-on-ones', {'with': w.RAM}).json()['manager']['id'] == w.RAM
    done = ram.patch(f'/one-on-ones/{mid}', {'status': 'done'})
    assert done.status_code == 200 and done.json()['nextId']
    nxt = ram.get(f'/one-on-ones/{done.json()["nextId"]}').json()
    assert 'Draft plan' in [a['text'] for a in nxt['openActions']]
    # Items: toggle and delete by either of the two, nobody else.
    iid = item.json()['id']
    assert hari.patch(f'/one-on-one-items/{iid}', {'done': True}).status_code == 404
    assert ram.patch(f'/one-on-one-items/{iid}', {'done': True}).json() == {'ok': True}
    assert ram.get(f'/one-on-ones/{done.json()["nextId"]}').json()['openActions'] == []
    assert hari.delete(f'/one-on-one-items/{iid}').status_code == 404
    assert sita.delete(f'/one-on-one-items/{iid}').json() == {'ok': True}
    assert sita.delete(f'/one-on-one-items/{iid}').status_code == 404


# ---------------------------------------------------------------- the session, the feature switch, the Origin check

def test_auth_feature_and_origin(w):
    assert Client().get(f'{P}/goals').status_code == 401
    assert 'error' in Client().get(f'{P}/goals').json()
    # Bearer tokens work too (no Origin check).
    token = auth.login(w.sita.email, PW).token
    r = Client().get(f'{P}/goals', HTTP_AUTHORIZATION=f'Bearer {token}')
    assert r.status_code == 200 and r.json() == {'goals': []}
    # The cookie is honoured only from this site's own origin for writes.
    evil = Browser(w.sita, origin='https://evil.example')
    r = evil.post('/goals', {'title': 'Sneaky'})
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    assert evil.get('/goals').status_code == 200
    own = Browser(w.sita, origin='http://testserver')
    assert own.post('/goals', {'title': 'Mine'}).status_code == 201
    # Unrouted methods answer 404 like Express.
    assert w.b_sita.put('/goals', {}).status_code == 404
    # The feature switch comes before the role check.
    set_features_off(w.co, 'performance')
    r = w.b_hr.post('/kpis', {'title': 'x'})
    assert r.status_code == 404 and r.json()['error'] == 'feature off'
    assert w.b_sita.post('/kpis', {'title': 'x'}).status_code == 404
    assert w.b_sita.get('/goals').status_code == 404
    assert w.b_other_hr.get('/kpis').status_code == 200            # the other company still has it


# ---------------------------------------------------------------- groups of companies

@pytest.fixture
def beta(world):  # noqa: F811
    return Beta(world)


def test_group_company_ids_are_not_found(world, beta):  # noqa: F811
    """Beta's records through Alpha's session (the group admin working in Alpha, an outsider), then Beta's own admin."""
    b = beta
    meeting_item = b.item
    cases = [
        ('get', f'/goals/{b.goal}', None), ('patch', f'/goals/{b.goal}', {'title': 'x'}), ('delete', f'/goals/{b.goal}', None),
        ('get', f'/goals/{b.company_goal}', None), ('post', f'/goals/{b.goal}/key-results', {'title': 'x'}),
        ('post', f'/goals/{b.goal}/checkins', {'comment': 'x'}),
        ('patch', f'/kpis/{b.kpi}', {'title': 'x'}), ('delete', f'/kpis/{b.kpi}', None), ('post', f'/kpis/{b.kpi}/apply', {}),
        ('patch', f'/competencies/{b.competency}', {'name': 'x'}),
        ('get', f'/cycles/{b.cycle}', None), ('patch', f'/cycles/{b.draft}', {'name': 'x'}), ('delete', f'/cycles/{b.draft}', None),
        ('post', f'/cycles/{b.draft}/launch', None), ('post', f'/cycles/{b.cycle}/advance', None),
        ('get', f'/cycles/{b.cycle}/calibration', None),
        ('get', f'/reviews/{b.review}', None), ('put', f'/reviews/{b.review}/self', {'summary': 'x'}),
        ('put', f'/reviews/{b.review}/manager', {'summary': 'x'}),
        ('post', f'/reviews/{b.review}/calibrate', {'finalRating': 4, 'reason': 'Because of x'}),
        ('post', f'/reviews/{b.review}/acknowledge', {}), ('post', f'/reviews/{b.review}/peers', {'employeeIds': [b.report]}),
        ('get', f'/reviews/{b.review}/print', None),
        ('post', f'/peers/{b.peer}/decision', {'approve': True}), ('put', f'/peers/{b.peer}', {'rating': 3}),
        ('delete', f'/peers/{b.peer}', None),
        ('get', f'/one-on-ones/{b.meeting}', None), ('patch', f'/one-on-ones/{b.meeting}', {'sharedNotes': 'x'}),
        ('post', f'/one-on-ones/{b.meeting}/items', {'kind': 'agenda', 'text': 'x'}),
        ('patch', f'/one-on-one-items/{meeting_item}', {'done': True}), ('delete', f'/one-on-one-items/{meeting_item}', None),
        ('delete', f'/journal/{b.journal}', None), ('get', '/journal', {'employeeId': b.report}),
        ('put', f'/feedback/{b.feedback_request()}', {'body': 'x'}), ('post', f'/feedback/{b.feedback_request()}/decline', None),
        ('post', '/journal', {'employeeId': b.report, 'noteDate': '2026-10-01', 'note': 'x'}),
        ('post', '/feedback', {'toEmployeeId': b.report, 'body': 'x'}),
        ('post', '/goals', {'employeeId': b.report, 'title': 'x'}),
    ]
    token_a, _ = world.session(world.admin_a)
    token_c, _ = world.session(world.admin_c)
    for who, token in (('group admin in Alpha', token_a), ('outsider', token_c)):
        br = Browser(None, token=token)
        for method, path, body in cases:
            r = br.get(path, body) if method == 'get' else br.delete(path) if method == 'delete' else getattr(br, method)(path, body)
            assert r.status_code == 404, (who, method, path, r.status_code, r.content[:200])
        # Lists show only the active company.
        assert b.goal not in ids(br.get('/goals', {'scope': 'all'}).json()['goals'])
        assert b.company_goal not in ids(br.get('/goals', {'scope': 'company'}).json()['goals'])
        assert b.kpi not in ids(br.get('/kpis').json()['kpis'])
        assert b.competency not in ids(br.get('/competencies', {'all': '1'}).json()['competencies'])
        assert b.cycle not in ids(br.get('/cycles').json()['cycles'])
        assert b.report not in ids(br.get('/colleagues').json()['colleagues'])
    # The same admin, switched to Beta, reaches them; so does Beta's own admin.
    token_ab, _ = world.session(world.admin_a, world.beta)
    assert Browser(None, token=token_ab).get(f'/goals/{b.goal}').status_code == 200
    token_b, _ = world.session(world.admin_b)
    owner = Browser(None, token=token_b)
    assert owner.get(f'/goals/{b.goal}').status_code == 200
    assert owner.get(f'/reviews/{b.review}').status_code == 200
    assert owner.get(f'/one-on-ones/{b.meeting}').status_code == 200
    assert b.cycle in ids(owner.get('/cycles').json()['cycles'])
