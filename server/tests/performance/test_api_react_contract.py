"""
The React pages' own requests against /api/v1/performance, field by field: the exact bodies the pages send
(frontend/src/pages/performance/*.jsx: nulls for empty inputs, spread recommendation objects, deadline-only patches)
and the JSON types they read back, compared with what the TS routes at 65a9079 answered (numeric columns as strings,
counts and integer columns as numbers, key results built by json_agg as JSON numbers, dates as YYYY-MM-DD,
instants as ISO strings).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import pytest

from .test_api_performance import Browser
from .world import build, plus_days, today

ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')
ISO_INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$')
NUMERIC = re.compile(r'^-?\d+(\.\d+)?$')


@pytest.fixture
def w(db):
    w = build()
    for k in ('hr', 'sita', 'ram', 'hari', 'gita', 'other_hr'):
        setattr(w, f'b_{k}', Browser(getattr(w, k)))
    return w


def numeric(v) -> bool:
    """A numeric column as node-pg returned it: a string of digits."""
    return isinstance(v, str) and bool(NUMERIC.match(v))


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def npt_instant(day: str, hhmm: str) -> str:
    return f'{day}T{hhmm}:00+05:45'


# ---------------------------------------------------------------- goals (parts.jsx GoalDrawer / GoalDetail, MyGoalsPage)

def test_goal_drawer_body_and_goal_card_fields(w):
    sita, ram, hr = w.b_sita, w.b_ram, w.b_hr
    # HR's company goal (allowCompany) with the drawer's nulls.
    company = hr.post('/goals', {'scope': 'company', 'title': 'Grow revenue', 'description': None, 'weight': 0, 'dueDate': None,
                                 'parentId': None, 'keyResults': []})
    assert company.status_code == 201, company.json()
    dept = hr.get('/departments').json()
    assert set(dept) == {'departments'}
    # GoalDrawer as Sita: no employeeId, KRs with unit null, a decimal start.
    r = sita.post('/goals', {'scope': 'individual', 'title': 'Close faster', 'description': None, 'weight': 40, 'dueDate': plus_days(30),
                             'parentId': company.json()['id'],
                             'keyResults': [{'title': 'Close in days', 'measure': 'number', 'startValue': 10, 'targetValue': 3, 'unit': 'days'},
                                            {'title': 'Accuracy', 'measure': 'percent', 'startValue': 0, 'targetValue': 100, 'unit': None},
                                            {'title': 'Budget', 'measure': 'currency', 'startValue': 0, 'targetValue': 2500.5, 'unit': None},
                                            {'title': 'Live', 'measure': 'boolean', 'startValue': 0, 'targetValue': 100, 'unit': None}]})
    assert r.status_code == 201, r.json()
    gid = r.json()['id']

    goals = sita.get('/goals', {'scope': 'mine'}).json()['goals']
    g = next(x for x in goals if x['id'] == gid)
    # Top-level numeric columns: strings (NUMERIC parser); dates YYYY-MM-DD; instants ISO.
    assert numeric(g['weight']) and numeric(g['progress'])
    assert ISO_DAY.match(g['due_date']) and g['start_date'] is None
    assert ISO_INSTANT.match(g['created_at']) and ISO_INSTANT.match(g['updated_at'])
    assert g['parent_title'] == 'Grow revenue' and g['employee_name'] == 'Sita Sharma' and g['org_unit_name'] is None
    assert g['last_checkin'] is None
    # Key results came from json_agg: numbers, in insertion order, the boolean one normalised to 0..1.
    assert [k['title'] for k in g['key_results']] == ['Close in days', 'Accuracy', 'Budget', 'Live']
    for k in g['key_results']:
        for f in ('start_value', 'target_value', 'current_value', 'weight'):
            assert is_num(k[f]), (f, k[f])
    budget = g['key_results'][2]
    assert budget['target_value'] == 2500.5 and budget['unit'] is None
    live = g['key_results'][3]
    assert (live['start_value'], live['target_value'], live['current_value']) == (0, 1, 0)

    # GoalDetail check-in: value on a KR, confidence as a number, the comment.
    kr = g['key_results'][0]['id']
    c = sita.post(f'/goals/{gid}/checkins', {'keyResultId': kr, 'value': 5, 'confidence': 3, 'comment': 'Better'})
    assert c.status_code == 201 and c.json() == {'ok': True}
    # Without key results: progress (the NumberInput gives a number).
    plain = sita.post('/goals', {'title': 'Read books', 'description': None, 'weight': 0, 'dueDate': None, 'parentId': None,
                                 'keyResults': []}).json()['id']
    assert sita.post(f'/goals/{plain}/checkins', {'progress': 35, 'confidence': 2}).status_code == 201
    d = sita.get(f'/goals/{gid}').json()
    assert d['canEdit'] is True and d['children'] == []
    lc = d['goal']['last_checkin']
    assert set(lc) == {'created_at', 'confidence', 'comment'} and lc['confidence'] == 3 and ISO_INSTANT.match(lc['created_at'])
    ch = d['checkins'][0]
    assert set(ch) == {'id', 'key_result_id', 'key_result_title', 'value', 'progress', 'confidence', 'comment', 'created_at', 'author_name'}
    assert numeric(ch['value']) and numeric(ch['progress']) and ch['confidence'] == 3 and ch['key_result_title'] == 'Close in days'
    assert ch['author_name'] == w.sita.name
    # The company goal lists the individual one as a child (with numeric progress as a string).
    cd = hr.get(f'/goals/{company.json()["id"]}').json()
    assert [x['id'] for x in cd['children']] == [gid] and numeric(cd['children'][0]['progress'])

    # Mark complete / reopen / cancel, as GoalDetail does.
    assert sita.patch(f'/goals/{gid}', {'status': 'completed'}).json() == {'ok': True}
    assert sita.get(f'/goals/{gid}').json()['goal']['weight'] in ('40', '40.00')     # a status change keeps the weight
    assert sita.patch(f'/goals/{gid}', {'status': 'active'}).json() == {'ok': True}
    assert sita.delete(f'/goals/{plain}').json() == {'ok': True}
    # TeamGoalsPage: the manager's team view and HR's 'all'.
    assert gid in [x['id'] for x in ram.get('/goals', {'scope': 'team'}).json()['goals']]
    assert gid in [x['id'] for x in hr.get('/goals', {'scope': 'all'}).json()['goals']]
    # Company goals for the "Supports" picker.
    assert [x['id'] for x in sita.get('/goals', {'scope': 'company'}).json()['goals']] == [company.json()['id']]


def test_colleagues_and_kpis_as_the_pages_read_them(w):
    sita, hr = w.b_sita, w.b_hr
    cols = sita.get('/colleagues').json()['colleagues']
    sita_row = next(c for c in cols if c['id'] == w.SITA)
    assert set(sita_row) == {'id', 'emp_code', 'name_en', 'designation', 'department', 'manager_id'}
    assert sita_row['manager_id'] == w.RAM           # TeamGoalsPage compares it with user.employeeId

    # KpiLibrary save body (designation and unit null when empty, defaultTarget a number).
    k = hr.post('/kpis', {'designation': None, 'title': 'Tickets in SLA', 'description': None, 'measure': 'percent',
                          'defaultTarget': 95, 'unit': None, 'active': True})
    assert k.status_code == 201, k.json()
    kid = k.json()['id']
    row = next(x for x in hr.get('/kpis').json()['kpis'] if x['id'] == kid)
    assert set(row) == {'id', 'designation', 'title', 'description', 'measure', 'default_target', 'unit', 'active'}
    assert numeric(row['default_target']) and row['active'] is True and row['designation'] is None
    # Editing re-sends the whole body.
    assert hr.patch(f'/kpis/{kid}', {'designation': 'Engineer', 'title': 'Tickets in SLA', 'description': None, 'measure': 'percent',
                                     'defaultTarget': 90, 'unit': None, 'active': True}).json() == {'ok': True}
    # KpiLibraryModal: target omitted, weight 0, dueDate null.
    a = sita.post(f'/kpis/{kid}/apply', {'weight': 0, 'dueDate': None})
    assert a.status_code == 201 and set(a.json()) == {'id'}
    kr = sita.get(f'/goals/{a.json()["id"]}').json()['goal']['key_results'][0]
    assert kr['target_value'] == 90 and kr['measure'] == 'percent'


# ---------------------------------------------------------------- review cycles (ReviewCyclesPage, ReviewForm, MyReviewsPage)

LABELS = ['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding']


def test_review_cycle_as_the_pages_drive_it(w):
    hr, sita, ram, hari = w.b_hr, w.b_sita, w.b_ram, w.b_hari
    # Competencies editor body.
    c = hr.post('/competencies', {'kind': 'skill', 'name': 'Communication', 'description': None,
                                  'indicators': [{'level': 1, 'text': 'Basic'}, {'level': 5, 'text': 'Role model'}],
                                  'designations': [], 'grades': [], 'active': True})
    assert c.status_code == 201, c.json()
    comps = hr.get('/competencies', {'all': '1'}).json()['competencies']
    assert comps[0]['indicators'] == [{'level': 1, 'text': 'Basic'}, {'level': 5, 'text': 'Role model'}]
    assert comps[0]['designations'] == [] and comps[0]['grades'] == []
    goal = sita.post('/goals', {'title': 'Ship v2', 'weight': 50, 'keyResults': []}).json()['id']

    # CycleDrawer (new, draft): every field, deadlines null, filter {}.
    body = {'name': 'FY 2083', 'selfDue': plus_days(10), 'managerDue': None, 'peerDue': None, 'calibrationDue': None, 'peerMin': 1,
            'periodStart': '2026-01-01', 'periodEnd': plus_days(60), 'goalsWeight': 60, 'competencyWeight': 40, 'peerEnabled': True,
            'ratingLabels': LABELS, 'filter': {}}
    r = hr.post('/cycles', body)
    assert r.status_code == 201, r.json()
    cid = r.json()['id']
    row = hr.get('/cycles').json()['cycles'][0]
    for f in ('participants', 'self_done', 'manager_done', 'peer_min'):
        assert is_num(row[f]), f
    assert numeric(row['goals_weight']) and numeric(row['competency_weight'])
    assert row['rating_labels'] == LABELS and row['filter'] == {} and row['peer_enabled'] is True
    assert ISO_DAY.match(row['period_start']) and ISO_DAY.match(row['self_due']) and row['manager_due'] is None
    # Re-saving the draft from the drawer (same full body) works.
    assert hr.patch(f'/cycles/{cid}', body).json() == {'ok': True}

    launch = hr.post(f'/cycles/{cid}/launch').json()
    assert launch['ok'] is True and is_num(launch['participants'])
    # After launch the drawer sends only the deadlines.
    assert hr.patch(f'/cycles/{cid}', {'name': 'FY 2083 review', 'selfDue': plus_days(12), 'managerDue': plus_days(20), 'peerDue': None,
                                       'calibrationDue': None, 'peerMin': 1}).json() == {'ok': True}

    # MyReviewsPage list.
    mine = sita.get('/reviews', {'scope': 'mine'}).json()['reviews']
    assert len(mine) == 1
    m = mine[0]
    assert m['rating_labels'] == LABELS and m['cycle_status'] == 'self_review' and ISO_DAY.match(m['period_start'])
    assert m['final_rating'] is None and m['manager_score'] is None
    rid = m['id']

    # ReviewForm, self side: the body built from the draft (comment null when empty, summary null).
    d = sita.get(f'/reviews/{rid}').json()
    assert d['role'] == 'self' and d['can']['editSelf'] is True and d['can']['nominatePeers'] is True
    assert d['cycle']['rating_labels'] == LABELS and is_num(d['cycle']['peer_min'])
    for i in d['items']:
        assert set(i) == {'id', 'item_type', 'ref_id', 'title', 'detail', 'weight', 'self_rating', 'self_comment', 'manager_rating',
                          'manager_comment'}
        assert numeric(i['weight'])
    assert {i['item_type'] for i in d['items']} == {'goal', 'competency'}
    assert any(i['ref_id'] == goal for i in d['items'])
    draft = {'items': [{'id': i['id'], 'rating': None, 'comment': None} for i in d['items']], 'summary': None, 'submit': False}
    assert sita.put(f'/reviews/{rid}/self', draft).json() == {'ok': True}
    # Nominate a peer (ColleaguePicker ids).
    p = sita.post(f'/reviews/{rid}/peers', {'employeeIds': [w.HARI]})
    assert p.status_code == 201 and p.json() == {'ok': True, 'added': 1}
    submit = {'items': [{'id': i['id'], 'rating': 4, 'comment': 'Good' if n == 0 else None} for n, i in enumerate(d['items'])],
              'summary': 'A good year', 'submit': True}
    assert sita.put(f'/reviews/{rid}/self', submit).json() == {'ok': True}
    d = sita.get(f'/reviews/{rid}').json()
    assert numeric(d['review']['self_score']) and ISO_INSTANT.match(d['review']['self_submitted_at'])
    assert d['peers']['nominations'][0]['status'] == 'nominated' and d['peers']['aggregate'] is None

    # Manager side: approve the peer, the review body with the recommendation spread from the stored one.
    md = ram.get(f'/reviews/{rid}').json()
    assert md['role'] == 'manager' and md['can']['editManager'] is True
    assert md['review']['recommendation'] == {} and md['review']['self_summary'] == 'A good year'
    nom = md['peers']['nominations'][0]
    assert set(nom) == {'id', 'reviewer_employee_id', 'reviewer_name', 'status', 'rating', 'strengths', 'improvements', 'submitted_at'}
    assert ram.post(f'/peers/{nom["id"]}/decision', {'approve': True}).json() == {'ok': True}
    assert md['peers']['aggregate'] == {'count': 0, 'shown': False}
    # Ram's journal for the period.
    assert ram.post('/journal', {'employeeId': w.SITA, 'noteDate': today(), 'tag': 'positive', 'note': 'Led the launch'}).status_code == 201
    j = ram.get(f'/reviews/{rid}').json()['journal']
    assert [set(x) for x in j] == [{'id', 'note_date', 'tag', 'note', 'author_name'}] and ISO_DAY.match(j[0]['note_date'])

    # Hari answers the peer request (MyReviewsPage: strengths/improvements null when empty).
    reqs = hari.get('/peer-requests').json()['requests']
    assert len(reqs) == 1 and reqs[0]['status'] == 'approved' and reqs[0]['rating_labels'] == LABELS
    assert set(reqs[0]) == {'id', 'status', 'rating', 'strengths', 'improvements', 'submitted_at', 'employee_name', 'designation',
                            'cycle_name', 'cycle_status', 'peer_due', 'rating_labels'}
    assert hari.put(f'/peers/{reqs[0]["id"]}', {'rating': 5, 'strengths': 'Calm', 'improvements': None}).json() == {'ok': True}

    rec = {'promotion': False, 'pip': False, 'trainingCourseIds': [], **md['review']['recommendation']}
    mbody = {'items': [{'id': i['id'], 'rating': 4, 'comment': None} for i in md['items']], 'summary': None, 'submit': False,
             'recommendation': {**rec, 'incrementPct': 10, 'notes': ''}}
    assert ram.put(f'/reviews/{rid}/manager', mbody).json() == {'ok': True}
    stored = ram.get(f'/reviews/{rid}').json()['review']['recommendation']
    assert stored == {'incrementPct': 10, 'promotion': False, 'pip': False, 'trainingCourseIds': [], 'notes': ''}
    # The next save spreads the stored recommendation back in (incrementPct cleared to null by the NumberInput).
    again = {**mbody, 'submit': True, 'recommendation': {'promotion': False, 'pip': False, 'trainingCourseIds': [], **stored,
                                                          'incrementPct': None}}
    assert ram.put(f'/reviews/{rid}/manager', again).json() == {'ok': True}
    md = ram.get(f'/reviews/{rid}').json()
    assert numeric(md['review']['manager_score']) and md['can']['editManager'] is False
    team = ram.get('/reviews', {'scope': 'team'}).json()['reviews']
    assert numeric(next(x for x in team if x['id'] == rid)['manager_score'])

    # Cycle detail as the HR table reads it.
    cd = hr.get(f'/cycles/{cid}').json()
    srow = next(x for x in cd['reviews'] if x['id'] == rid)
    for f in ('peers_done', 'peers_total', 'peers_pending'):
        assert is_num(srow[f]), f
    assert srow['peers_done'] == 1 and srow['manager_name'] == 'Ram Thapa' and numeric(srow['manager_score'])
    assert cd['cycle']['created_by'] == w.hr.id and 'created_by_id' not in cd['cycle']

    # Advance to calibration (peer review is on), calibrate as the form does, release, acknowledge.
    assert hr.post(f'/cycles/{cid}/advance').json() == {'ok': True, 'status': 'manager_review'}
    assert hr.post(f'/cycles/{cid}/advance').json() == {'ok': True, 'status': 'peer_review'}
    assert hr.post(f'/cycles/{cid}/advance').json() == {'ok': True, 'status': 'calibration'}
    cal = hr.get(f'/cycles/{cid}/calibration').json()
    assert all(is_num(n) for n in cal['distribution']['overall'])
    assert set(cal['reviews'][0]) == {'id', 'employee_id', 'name_en', 'emp_code', 'department', 'self_score', 'manager_score',
                                      'final_rating', 'calibration_reason', 'recommendation'}
    hd = hr.get(f'/reviews/{rid}').json()
    assert hd['role'] == 'hr' and hd['can']['calibrate'] is True
    assert hr.post(f'/reviews/{rid}/calibrate', {'finalRating': 4.25, 'reason': 'Calibrated across teams'}).json() == {'ok': True}
    assert hr.post(f'/cycles/{cid}/advance').json() == {'ok': True, 'status': 'released'}
    d = sita.get(f'/reviews/{rid}').json()
    assert d['review']['final_rating'] == '4.25' and d['can']['acknowledge'] is True
    agg = d['peers']['aggregate']
    assert agg == {'count': 1, 'shown': True, 'averageRating': 5, 'strengths': ['Calm'], 'improvements': []}
    assert sita.post(f'/reviews/{rid}/acknowledge', {}).json() == {'ok': True}     # comment undefined
    d = sita.get(f'/reviews/{rid}').json()
    assert ISO_INSTANT.match(d['review']['acknowledged_at']) and d['review']['employee_comment'] is None
    assert sita.get('/reviews').json()['reviews'][0]['final_rating'] == '4.25'
    pr = sita.get(f'/reviews/{rid}/print')
    assert pr.status_code == 200 and pr['Content-Type'].startswith('text/html')


# ---------------------------------------------------------------- journal, feedback, 1-on-1s

def test_journal_feedback_and_meetings_as_the_pages_read_them(w):
    sita, ram, hari = w.b_sita, w.b_ram, w.b_hari
    assert ram.post('/journal', {'employeeId': w.SITA, 'noteDate': today(), 'tag': 'improvement', 'note': 'Late twice'}).status_code == 201
    e = ram.get('/journal', {'employeeId': w.SITA}).json()['entries'][0]
    assert set(e) == {'id', 'note_date', 'tag', 'note', 'created_at', 'author_id', 'author_name'} and e['author_id'] == w.ram.id

    # FeedbackPage: give, request (prompt omitted when blank), answer, decline.
    assert hari.post('/feedback', {'toEmployeeId': w.SITA, 'body': 'Nice demo', 'privateToManager': False}).status_code == 201
    assert sita.post('/feedback/requests', {'fromEmployeeIds': [w.HARI, w.GITA]}).status_code == 201
    got = sita.get('/feedback', {'box': 'received'}).json()['feedback']
    assert set(got[0]) == {'id', 'status', 'prompt', 'body', 'private_to_manager', 'created_at', 'given_at', 'giver_employee_id',
                           'giver_name', 'subject_employee_id', 'subject_name', 'requester_employee_id', 'requester_name'}
    asked = hari.get('/feedback', {'box': 'requests'}).json()['feedback']
    req = next(f for f in asked if f['status'] == 'requested' and f['giver_employee_id'] == w.HARI)
    assert req['requester_name'] == 'Sita Sharma'
    assert hari.put(f'/feedback/{req["id"]}', {'body': 'Speak up more', 'privateToManager': True}).json() == {'ok': True}
    gita_req = next(f for f in w.b_gita.get('/feedback', {'box': 'requests'}).json()['feedback'] if f['status'] == 'requested')
    assert w.b_gita.post(f'/feedback/{gita_req["id"]}/decline').json() == {'ok': True}
    mine = sita.get('/feedback', {'box': 'requests'}).json()['feedback']
    assert next(f for f in mine if f['giver_employee_id'] == w.HARI)['body'] is None       # private to the manager
    about = ram.get('/feedback', {'box': 'about', 'employeeId': w.SITA}).json()['feedback']
    assert {f['body'] for f in about} == {'Nice demo', 'Speak up more'}

    # OneOnOnesPage.
    lst = sita.get('/one-on-ones').json()
    assert set(lst) == {'meetings', 'reports', 'manager'} and lst['manager'] == {'id': w.RAM, 'name_en': 'Ram Thapa', 'designation': 'Engineer'}
    assert lst['reports'] == []
    day = plus_days(3)
    r = ram.post('/one-on-ones', {'withEmployeeId': w.SITA, 'scheduledAt': npt_instant(day, '10:30'), 'cadence': 'weekly',
                                  'agenda': ['Roadmap', 'Leave plan']})
    assert r.status_code == 201
    mid = r.json()['id']
    row = ram.get('/one-on-ones').json()['meetings'][0]
    assert row['side'] == 'manager' and row['agenda_items'] == 2 and row['open_actions'] == 0
    assert ISO_INSTANT.match(row['scheduled_at'])
    # splitNpt reads the stored instant back as the Nepal date and time that were sent.
    at = datetime.fromisoformat(row['scheduled_at'].replace('Z', '+00:00')) + timedelta(minutes=345)
    assert at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M') == f'{day} 10:30'
    assert ram.post(f'/one-on-ones/{mid}/items', {'kind': 'agenda', 'text': 'Training', 'ownerEmployeeId': None}).status_code == 201
    act = ram.post(f'/one-on-ones/{mid}/items', {'kind': 'action', 'text': 'Book course', 'ownerEmployeeId': w.SITA}).json()['id']
    d = sita.get(f'/one-on-ones/{mid}').json()
    m = d['meeting']
    assert m['side'] == 'employee' and m['private_notes'] is None and m['shared_notes'] is None
    assert 'manager_private_notes' not in m and 'employee_private_notes' not in m
    assert {'manager_employee_id', 'manager_name', 'employee_id', 'employee_name', 'cadence', 'status', 'scheduled_at'} <= set(m)
    a = next(i for i in d['items'] if i['id'] == act)
    assert set(a) == {'id', 'kind', 'text', 'owner_employee_id', 'owner_name', 'done', 'created_at', 'added_by'}
    assert a['owner_name'] == 'Sita Sharma' and a['done'] is False
    assert ram.patch(f'/one-on-ones/{mid}', {'sharedNotes': 'Agreed', 'privateNotes': None}).json() == {'ok': True, 'nextId': None}
    done = ram.patch(f'/one-on-ones/{mid}', {'status': 'done', 'sharedNotes': 'Agreed', 'privateNotes': 'Watch workload'}).json()
    assert done['ok'] is True and isinstance(done['nextId'], str)
    nxt = sita.get(f'/one-on-ones/{done["nextId"]}').json()
    assert [x['id'] for x in nxt['openActions']] == [act] and ISO_INSTANT.match(nxt['openActions'][0]['scheduled_at'])
    assert ram.get(f'/one-on-ones/{mid}').json()['meeting']['private_notes'] == 'Watch workload'
    assert sita.get(f'/one-on-ones/{mid}').json()['meeting']['private_notes'] is None
    assert sita.patch(f'/one-on-one-items/{act}', {'done': True}).json() == {'ok': True}
    assert ram.patch(f'/one-on-ones/{done["nextId"]}', {'scheduledAt': npt_instant(plus_days(11), '09:00')}).json()['ok'] is True
    assert ram.delete(f'/one-on-one-items/{act}').json() == {'ok': True}
