"""
The /api/v1/relations JSON API as the React app calls it (frontend/src/api/relations.js): the session cookie, JSON
bodies, no CSRF token. Ports the HTTP-level assertions of the TS apps/api/test/relations.test.ts (grievances,
disciplinary action, milestones, acting and deputation, Know Your Employee, pop-ups, registered devices, summary),
plus the same-origin check for cookie requests and company isolation. Other modules' steps (file upload, letters,
announcements) run through their services so this file depends only on the relations endpoints.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from django.test import Client

from hajiri.core.clock import npt_today
from hajiri.relations import services as S
from tests.support.sql import execute, one, rows
from tests.web.factories import make_user, sign_in

from .conftest import PDF_DATA_URL

R = '/api/v1/relations'
TOKEN = 'dev-token-abcdefghijklmnopqrstuvwxyz'


class Api:
    """A browser tab of the React app: the hajiri_session cookie on every call, JSON in and out."""

    def __init__(self, user, **headers):
        self.c = Client()
        sign_in(self.c, user)
        self.h = headers

    def get(self, path, **q):
        return self.c.get(R + path, q, **self.h)

    def post(self, path, body=None):
        if body is None:   # React's post(path): no body at all
            return self.c.post(R + path, **self.h)
        return self.c.post(R + path, body, content_type='application/json', **self.h)

    def put(self, path, body):
        return self.c.put(R + path, body, content_type='application/json', **self.h)

    def patch(self, path, body):
        return self.c.patch(R + path, body, content_type='application/json', **self.h)

    def delete(self, path):
        return self.c.delete(R + path, **self.h)


def _committee(w):
    assert Api(w.hr).put('/grievances/committee', {'userIds': [w.member.id]}).status_code == 200


def _file(w, **kw):
    body = {'category': 'management', 'subject': 'Unfair shift allocation', 'description': 'My manager keeps giving me night shifts',
            'concerns': 'Ram', **kw}
    r = Api(w.sita).post('/grievances', body)
    assert r.status_code == 201, r.content
    return r.json()


# ================================================================ access

def test_signed_out_cross_site_and_wrong_role_are_refused(world):
    w = world
    out = Client().get(R + '/summary')
    assert out.status_code == 401 and out.json()['error']
    # The React app posts with the cookie and no CSRF token: same-origin passes, a foreign Origin does not.
    assert Api(w.hr, HTTP_ORIGIN='http://testserver').put('/grievances/committee', {'userIds': []}).status_code == 200
    foreign = Api(w.hr, HTTP_ORIGIN='https://evil.example').put('/grievances/committee', {'userIds': []})
    assert foreign.status_code == 403 and foreign.json() == {'error': 'cross-site request refused'}
    s = Api(w.sita).get('/summary')
    assert s.status_code == 403 and s.json() == {'error': 'requires role hr'}
    h = Api(w.hr).get('/summary')
    assert h.status_code == 200
    assert h.json() == {'openGrievances': 0, 'openCases': 0, 'pendingDevices': 0, 'openKye': 0, 'isHr': True}
    # Unrouted methods answer 404 like Express.
    assert Api(w.hr).delete('/summary').status_code == 404


def test_policy_is_hr_only_and_dedupes_service_years(world):
    w = world
    pol = Api(w.hr).get('/policy')
    assert pol.status_code == 200
    p = pol.json()['policy']
    assert {'appealDays', 'retirementAge', 'serviceYears', 'registeredDevicesOnly'} <= set(p)
    assert Api(w.sita).get('/policy').status_code == 403
    saved = Api(w.hr).put('/policy', {**p, 'serviceYears': [10, 1, 5, 5]})
    assert saved.status_code == 200 and saved.json()['policy']['serviceYears'] == [1, 5, 10]
    bad = Api(w.hr).put('/policy', {**p, 'appealDays': 'soon'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert [i['path'] for i in bad.json()['issues']] == ['appealDays']


# ================================================================ grievances

def test_hr_names_the_committee_only_hr_can_change_it(world):
    w = world
    assert Api(w.sita).put('/grievances/committee', {'userIds': [w.member.id]}).status_code == 403
    r = Api(w.hr).put('/grievances/committee', {'userIds': [w.member.id]})
    assert r.status_code == 200 and r.json() == {'ok': True, 'members': 1}
    m = Api(w.member).get('/grievances/committee').json()
    assert m['isMember'] is True and [x['user_id'] for x in m['members']] == [w.member.id]
    assert set(m['members'][0]) == {'user_id', 'name', 'email', 'added_at'}
    assert Api(w.outsider).get('/grievances/committee').json() == {'members': [], 'isMember': False}
    # Another company's user is not a person of this company.
    other = Api(w.hr).put('/grievances/committee', {'userIds': [w.other_hr.id]})
    assert other.status_code == 400 and other.json() == {'error': 'every member must be an active user in your company'}


def test_an_employee_files_a_confidential_grievance_and_only_handlers_see_it(world):
    from hajiri.files.service import upload
    w = world
    _committee(w)
    f = upload(w.sita, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    r = _file(w, fileIds=[f['id']])
    assert r['number'] == 1 and set(r) == {'id', 'number'}
    gid = r['id']
    raw = one('SELECT description_enc FROM grievance WHERE id = %s', [gid])
    assert 'night shifts' not in bytes(raw['description_enc']).decode('latin1')
    n = rows("SELECT user_id, title, body FROM notification_outbox WHERE template = 'relations.grievance.new'")
    assert w.member.id in [str(x['user_id']) for x in n]
    assert all('night' not in x['body'] and 'Sita' not in x['title'] for x in n)

    # The line manager, other employees and other companies never see it.
    assert Api(w.ram).get(f'/grievances/{gid}').status_code == 404
    handle = Api(w.ram).get('/grievances', scope='handle')
    assert handle.status_code == 403 and handle.json() == {'error': 'only HR and the grievance committee handle grievances'}
    assert Api(w.outsider).get(f'/grievances/{gid}').status_code == 404
    assert Api(w.other_hr).get(f'/grievances/{gid}').status_code == 404
    assert Api(w.hr).get('/grievances/not-a-uuid').status_code == 400
    # The committee and HR do (audited).
    m = Api(w.member).get(f'/grievances/{gid}')
    assert m.status_code == 200
    body = m.json()
    assert set(body) == {'grievance', 'notes', 'files', 'canAppeal', 'appealUntil'}
    assert 'night shifts' in body['grievance']['description'] and body['grievance']['role'] == 'committee'
    assert body['grievance']['employeeName'] == 'Sita Sharma' and body['grievance']['empCode'] == 'E001'
    lst = Api(w.member).get('/grievances', scope='handle').json()['grievances']
    assert lst[0]['description'] is None and lst[0]['subject'] == 'Unfair shift allocation'
    assert rows("SELECT 1 FROM audit_log WHERE table_name = 'grievance' AND action = 'view' AND row_id = %s", [gid])
    mine = Api(w.sita).get('/grievances').json()['grievances']
    assert mine[0]['subject'] == 'Unfair shift allocation' and mine[0]['role'] == 'complainant' and mine[0]['excludedUserIds'] == []

    # Attachments are served only to people who can see the case.
    files = Api(w.sita).get(f'/grievances/{gid}').json()['files']
    assert files[0]['filename'] == 'note.pdf'
    got = Api(w.member).get(f"/grievances/{gid}/files/{files[0]['id']}")
    assert got.status_code == 200 and got['Content-Type'] == 'application/pdf' and got.content.startswith(b'%PDF')
    assert Api(w.member).get(f"/grievances/{gid}/files/{files[0]['id']}", download='1')['Content-Disposition'].startswith('attachment')
    assert Api(w.ram).get(f"/grievances/{gid}/files/{files[0]['id']}").status_code == 404
    assert Api(w.other_hr).get(f"/grievances/{gid}/files/{files[0]['id']}").status_code == 404


def test_internal_notes_stay_with_the_committee_shared_notes_reach_the_employee(world):
    w = world
    _committee(w)
    gid = _file(w)['id']
    a = Api(w.member).post(f'/grievances/{gid}/notes', {'body': 'Check the roster history'})
    assert a.status_code == 201 and set(a.json()) == {'id'}
    assert Api(w.member).post(f'/grievances/{gid}/notes', {'body': 'We will meet you on Sunday', 'shared': True}).status_code == 201
    seen = [n['body'] for n in Api(w.sita).get(f'/grievances/{gid}').json()['notes']]
    assert seen == ['We will meet you on Sunday']
    assert Api(w.sita).post(f'/grievances/{gid}/notes', {'body': 'Thank you'}).status_code == 201
    notes = Api(w.member).get(f'/grievances/{gid}').json()['notes']
    assert [(n['body'], n['shared'], n['fromComplainant']) for n in notes] == [
        ('Check the roster history', False, False), ('We will meet you on Sunday', True, False), ('Thank you', True, True)]
    assert Api(w.ram).post(f'/grievances/{gid}/notes', {'body': 'x'}).status_code == 404
    empty = Api(w.member).post(f'/grievances/{gid}/notes', {'body': ''})
    assert empty.status_code == 400 and empty.json()['issues'][0]['path'] == 'body'


def test_exclusions_only_grow_and_nobody_becomes_the_only_handler(world):
    w = world
    _committee(w)
    gid = _file(w)['id']
    assert Api(w.member).patch(f'/grievances/{gid}', {'excludeUserIds': [w.hr.id]}).status_code == 403
    assert Api(w.hr).patch(f'/grievances/{gid}', {'excludeUserIds': [w.member.id]}).json() == {'ok': True}
    assert Api(w.member).get(f'/grievances/{gid}').status_code == 404
    assert Api(w.hr).get(f'/grievances/{gid}').json()['grievance']['excludedUserIds'] == [w.member.id]
    lift = Api(w.hr).patch(f'/grievances/{gid}', {'excludeUserIds': []})
    assert lift.status_code == 403 and lift.json() == {'error': 'only an administrator can lift an exclusion'}
    admin = make_user(w.co, roles=['admin'])
    assert Api(admin).patch(f'/grievances/{gid}', {'excludeUserIds': []}).status_code == 200
    # Excluding every other handler is refused.
    handlers = [x for x in S.grievance_handlers(w.company_id, w.sita.id, []) if x != w.hr.id]
    assert handlers
    only = Api(w.hr).patch(f'/grievances/{gid}', {'excludeUserIds': handlers})
    assert only.status_code == 400
    assert only.json() == {'error': 'at least one other HR or committee member must be able to handle this case'}
    # Unknown keys are refused (zod .strict()).
    assert Api(w.hr).patch(f'/grievances/{gid}', {'subject': 'x'}).status_code == 400


def test_hearing_decision_appeal_once_and_hr_not_the_decider_hears_it(world):
    w = world
    _committee(w)
    gid = _file(w)['id']
    assert Api(w.sita).patch(f'/grievances/{gid}', {'status': 'under_review'}).status_code == 403
    no_time = Api(w.member).patch(f'/grievances/{gid}', {'status': 'hearing_scheduled'})
    assert no_time.status_code == 400 and no_time.json() == {'error': 'set the hearing date and time'}
    assert Api(w.member).patch(f'/grievances/{gid}', {'hearingAt': '2026-11-01T10:00:00+05:45', 'hearingPlace': 'Board room'}).status_code == 200
    g = Api(w.sita).get(f'/grievances/{gid}').json()['grievance']
    assert g['status'] == 'hearing_scheduled' and g['hearingPlace'] == 'Board room' and g['hearingAt'] == '2026-11-01T04:15:00.000Z'
    assert Api(w.sita).post(f'/grievances/{gid}/decision', {'outcome': 'upheld', 'findings': 'x'}).status_code == 403
    assert Api(w.hr).post(f'/grievances/{gid}/decision', {'outcome': 'partly_upheld', 'findings': 'Shifts will rotate fairly from next month'}).status_code == 200
    d = Api(w.sita).get(f'/grievances/{gid}').json()
    assert d['canAppeal'] is True and d['appealUntil'] and d['grievance']['findings'] == 'Shifts will rotate fairly from next month'
    assert Api(w.member).post(f'/grievances/{gid}/appeal', {'reason': 'x'}).status_code == 403
    assert Api(w.sita).post(f'/grievances/{gid}/appeal', {'reason': 'Rotation is not enough'}).status_code == 200
    again = Api(w.sita).post(f'/grievances/{gid}/appeal', {'reason': 'again'})
    assert again.status_code == 409 and again.json() == {'error': 'you can appeal once a decision has been made'}
    decider = Api(w.hr).post(f'/grievances/{gid}/appeal-decision', {'outcome': 'decision_upheld', 'note': 'n'})
    assert decider.status_code == 403 and decider.json() == {'error': 'someone other than the person who decided must hear the appeal'}
    committee = Api(w.member).post(f'/grievances/{gid}/appeal-decision', {'outcome': 'decision_upheld', 'note': 'n'})
    assert committee.status_code == 403 and committee.json() == {'error': 'requires role hr'}
    no_new = Api(w.hr2).post(f'/grievances/{gid}/appeal-decision', {'outcome': 'decision_changed', 'note': 'n'})
    assert no_new.status_code == 400 and no_new.json() == {'error': 'choose the new outcome'}
    r = Api(w.hr2).post(f'/grievances/{gid}/appeal-decision', {'outcome': 'decision_changed', 'newOutcome': 'upheld', 'note': 'Upheld in full'})
    assert r.status_code == 200 and r.json() == {'ok': True}
    g = Api(w.sita).get(f'/grievances/{gid}').json()['grievance']
    assert {k: g[k] for k in ('status', 'outcome', 'appealOutcome', 'appealNote')} == {
        'status': 'closed', 'outcome': 'upheld', 'appealOutcome': 'decision_changed', 'appealNote': 'Upheld in full'}
    assert Api(w.member).post(f'/grievances/{gid}/notes', {'body': 'late'}).status_code == 409


def test_withdrawal_close_and_the_daily_job(world):
    w = world
    _committee(w)
    a = Api(w.sita).post('/grievances', {'category': 'other', 'subject': 'Parking', 'description': 'No parking space'}).json()['id']
    assert Api(w.member).post(f'/grievances/{a}/withdraw').status_code == 403
    assert Api(w.sita).post(f'/grievances/{a}/withdraw').json() == {'ok': True}
    assert Api(w.sita).post(f'/grievances/{a}/withdraw').status_code == 409
    b = Api(w.sita).post('/grievances', {'category': 'safety', 'subject': 'Wet floor', 'description': 'Kitchen floor is always wet'}).json()['id']
    assert Api(w.member).post(f'/grievances/{b}/decision', {'outcome': 'upheld', 'findings': 'Mats ordered'}).status_code == 200
    assert Api(w.member).post(f'/grievances/{b}/close').status_code == 403
    out = S.relations_daily(datetime.now(timezone.utc) + timedelta(days=20))
    assert out['grievancesClosed'] >= 1
    assert Api(w.sita).get(f'/grievances/{b}').json()['grievance']['status'] == 'closed'
    c = Api(w.sita).post('/grievances', {'category': 'other', 'subject': 'Lockers', 'description': 'No lockers'}).json()['id']
    assert Api(w.sita).post(f'/grievances/{c}/close').status_code == 409
    assert Api(w.hr).post(f'/grievances/{c}/decision', {'outcome': 'not_upheld', 'findings': 'There are lockers'}).status_code == 200
    assert Api(w.sita).post(f'/grievances/{c}/close').json() == {'ok': True}
    assert [x['status'] for x in Api(w.hr).get('/grievances', scope='handle', status='closed').json()['grievances']] == ['closed', 'closed']


def test_grievances_validate_input(world):
    w = world
    bad = Api(w.sita).post('/grievances', {'category': 'gossip', 'subject': 's', 'description': 'd'})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation' and bad.json()['issues'][0]['path'] == 'category'
    unlinked = Api(w.hr).post('/grievances', {'category': 'other', 'subject': 's', 'description': 'd'})
    assert unlinked.status_code == 400 and unlinked.json() == {'error': 'Your account is not linked to an employee record.'}
    scope = Api(w.sita).get('/grievances', scope='everyone')
    assert scope.status_code == 400 and scope.json()['issues'][0]['path'] == 'scope'


# ================================================================ disciplinary action

def _stages(w):
    r = Api(w.hr).get('/disciplinary/stages')
    assert r.status_code == 200
    return r.json()['stages']


def _open_case(w, employee_id=None, **kw):
    body = {'employeeId': str(employee_id or w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01',
            'incident': 'Absent without notice for three days', **kw}
    r = Api(w.hr).post('/disciplinary', body)
    assert r.status_code == 201, r.content
    return r.json()['id']


def test_default_stages_are_seeded_hr_edits_them(world):
    w = world
    stages = _stages(w)
    assert [s['code'] for s in stages] == ['verbal_warning', 'written_warning', 'final_warning', 'suspension', 'termination']
    assert set(stages[0]) == {'id', 'code', 'name', 'severity', 'effect', 'letter_template_code', 'sort', 'active'}
    assert Api(w.sita).put('/disciplinary/stages', {'stages': []}).status_code == 403
    edited = [{'id': s['id'], 'code': s['code'], 'name': s['name'], 'severity': s['severity'], 'effect': s['effect'],
               'letterTemplateCode': s['letter_template_code'], 'active': s['code'] != 'verbal_warning'} for s in stages]
    assert Api(w.hr).put('/disciplinary/stages', {'stages': edited}).json() == {'ok': True}
    assert [s['active'] for s in _stages(w)] == [False, True, True, True, True]
    # Another company's stage id is not found.
    other = Api(w.other_hr).put('/disciplinary/stages', {'stages': edited[:1]})
    assert other.status_code == 404 and other.json() == {'error': 'stage not found'}


def test_hr_opens_a_case_the_employee_does_not_see_it_until_asked(world):
    w = world
    assert Api(w.ram).post('/disciplinary', {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01',
                                             'incident': 'x'}).status_code == 403
    cid = _open_case(w)
    assert Api(w.sita).get('/disciplinary/mine').json() == {'cases': []}
    assert Api(w.sita).get(f'/disciplinary/{cid}').status_code == 404
    assert Api(w.ram).get(f'/disciplinary/{cid}').status_code == 404
    assert Api(w.other_hr).get(f'/disciplinary/{cid}').status_code == 404
    assert Api(w.ram).get('/disciplinary').status_code == 403
    k = Api(w.hr).get(f'/disciplinary/{cid}').json()['case']
    assert k['incident'] == 'Absent without notice for three days' and k['status'] == 'open' and k['employeeName'] == 'Sita Sharma'
    listed = Api(w.hr).get('/disciplinary', employeeId=str(w.sita_e.id)).json()['cases']
    assert [x['id'] for x in listed] == [cid]
    assert Api(w.other_hr).get('/disciplinary').json() == {'cases': []}
    future = Api(w.hr).post('/disciplinary', {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2099-01-01', 'incident': 'x'})
    assert future.status_code == 400 and future.json() == {'error': 'the incident date is in the future'}
    foreign = Api(w.other_hr).post('/disciplinary', {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01', 'incident': 'x'})
    assert foreign.status_code == 404 and foreign.json() == {'error': 'employee not found'}


def test_show_cause_response_and_no_early_decision_without_confirmation(world):
    w = world
    stages = _stages(w)
    cid = _open_case(w)
    past = Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Explain the absence', 'responseDue': '2020-01-01'})
    assert past.status_code == 400 and past.json() == {'error': 'the response deadline has already passed'}
    assert Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Explain your absence on 1 to 3 Kartik', 'responseDue': '2099-01-01'}).status_code == 200
    assert Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Again', 'responseDue': '2099-01-01'}).status_code == 409
    early = Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': stages[0]['id'], 'note': 'n'})
    assert early.status_code == 409
    mine = Api(w.sita).get('/disciplinary/mine').json()['cases']
    assert {k: mine[0][k] for k in ('status', 'showCause', 'stageLetter', 'exitId')} == {
        'status': 'show_cause', 'showCause': 'Explain your absence on 1 to 3 Kartik', 'stageLetter': None, 'exitId': None}
    assert Api(w.ram).post(f'/disciplinary/{cid}/respond', {'response': 'x'}).status_code == 404
    assert Api(w.sita).post(f'/disciplinary/{cid}/respond', {'response': 'My mother was in hospital'}).status_code == 200
    again = Api(w.sita).post(f'/disciplinary/{cid}/respond', {'response': 'again'})
    assert again.status_code == 409 and again.json() == {'error': 'you have already responded'}
    assert Api(w.sita).get(f'/disciplinary/{cid}').json()['case']['response'] == 'My mother was in hospital'


def test_decide_early_with_force_no_action_and_withdrawal(world):
    w = world
    cid = _open_case(w)
    Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'})
    r = Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': None, 'note': 'Admitted in the hearing', 'force': True})
    assert r.status_code == 200 and r.json() == {'ok': True}
    k = Api(w.sita).get(f'/disciplinary/{cid}').json()['case']
    assert k['status'] == 'decided' and k['stageName'] is None and k['decisionNote'] == 'Admitted in the hearing'
    assert Api(w.hr).post(f'/disciplinary/{cid}/withdraw', {'reason': 'late'}).status_code == 409

    k2 = _open_case(w, w.ram_e.id, category='conduct', incidentDate='2026-10-02', incident='Shouting')
    Api(w.hr).post(f'/disciplinary/{k2}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'})
    execute("UPDATE disciplinary_case SET response_due = '2026-01-01' WHERE id = %s", [k2])
    assert S.relations_daily()['overdueResponses'] == 1
    assert S.relations_daily()['overdueResponses'] == 0
    assert Api(w.ram).post(f'/disciplinary/{k2}/withdraw', {'reason': 'x'}).status_code == 403
    assert Api(w.hr).post(f'/disciplinary/{k2}/withdraw', {'reason': 'Misunderstanding'}).status_code == 200
    assert Api(w.hr).get('/disciplinary', employeeId=str(w.ram_e.id)).json()['cases'][0]['status'] == 'withdrawn'


def test_suspension_with_dates_and_a_warning_letter_linked(world):
    from hajiri.documents import services as docs
    w = world
    stages = _stages(w)
    cid = _open_case(w)
    Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'})
    Api(w.sita).post(f'/disciplinary/{cid}/respond', {'response': 'Sorry'})
    susp = next(s for s in stages if s['effect'] == 'suspension')
    no_dates = Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': susp['id'], 'note': 'Three days'})
    assert no_dates.status_code == 400 and no_dates.json() == {'error': 'enter the suspension dates'}
    assert Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': susp['id'], 'note': 'Three days without pay',
                                                          'suspensionFrom': '2026-10-20', 'suspensionTo': '2026-10-22'}).status_code == 200
    ev = one("SELECT status, details FROM career_event WHERE employee_id = %s AND kind = 'suspension'", [w.sita_e.id])
    assert ev['status'] == 'applied' and ev['details']['endDate'] == '2026-10-22'
    tpl = next(t for t in docs.list_templates(w.hr) if t['code'] == 'warning')
    letter = docs.issue_letter(w.hr, {'templateId': str(tpl['id']), 'employeeId': str(w.sita_e.id), 'purpose': 'unauthorised absence',
                                      'allowMissing': True})
    assert Api(w.hr).post(f'/disciplinary/{cid}/letter', {'letterId': letter['id']}).json() == {'ok': True}
    k = Api(w.sita).get(f'/disciplinary/{cid}').json()['case']
    assert {x: k[x] for x in ('status', 'stageName', 'suspensionFrom', 'letterRef')} == {
        'status': 'decided', 'stageName': 'Suspension', 'suspensionFrom': '2026-10-20', 'letterRef': letter['refNo']}
    other = docs.issue_letter(w.hr, {'templateId': str(tpl['id']), 'employeeId': str(w.ram_e.id), 'allowMissing': True})
    wrong = Api(w.hr).post(f'/disciplinary/{cid}/letter', {'letterId': other['id']})
    assert wrong.status_code == 400 and wrong.json() == {'error': 'that letter was not issued to this employee'}
    assert Api(w.other_hr).post(f'/disciplinary/{cid}/letter', {'letterId': letter['id']}).status_code == 404


def test_a_termination_stage_records_the_exit(world):
    w = world
    stages = _stages(w)
    emp = w.new_employee('TRM1')
    k = _open_case(w, emp.id, category='misconduct', incidentDate='2026-09-01', incident='Theft')
    term = next(s for s in stages if s['effect'] == 'termination')
    no_day = Api(w.hr).post(f'/disciplinary/{k}/decide', {'stageId': term['id'], 'note': 'Gross misconduct'})
    assert no_day.status_code == 400 and no_day.json() == {'error': 'enter the last working day'}
    assert Api(w.hr).post(f'/disciplinary/{k}/decide', {'stageId': term['id'], 'note': 'Gross misconduct', 'lastWorkingDay': '2026-10-30'}).status_code == 200
    ex = one('SELECT kind, voluntary, status FROM employee_exit WHERE employee_id = %s', [emp.id])
    assert (ex['kind'], ex['voluntary'], ex['status']) == ('termination', False, 'accepted')
    assert Api(w.hr).get(f'/disciplinary/{k}').json()['case']['exitId']


# ================================================================ milestones, acting and deputation

def test_milestones_contract_end_and_retirement(world):
    w = world
    in10 = (npt_today() + timedelta(days=10)).isoformat()
    joined = f'{int(in10[:4]) - 5}{in10[4:]}'
    dob = f'{int(in10[:4]) - 58}{in10[4:]}'
    a = str(w.new_employee('MS1', join_date=date.fromisoformat(joined), dob=date.fromisoformat(dob)).id)
    assert Api(w.sita).put(f'/employees/{a}/contract', {'contractEnd': in10}).status_code == 403
    assert Api(w.hr).put(f'/employees/{a}/contract', {'contractEnd': in10}).json() == {'ok': True}
    body = Api(w.hr).get('/milestones', days='30').json()
    assert 'policy' in body
    m = [x for x in body['milestones'] if x['employeeId'] == a]
    assert sorted(x['kind'] for x in m) == ['contract_end', 'retirement', 'service']
    assert next(x for x in m if x['kind'] == 'service')['detail'] == '5 years of service'
    assert any(x['employeeId'] == a for x in Api(w.hr).get('/separating').json()['people'])
    assert Api(w.ram).get('/milestones').status_code == 403
    assert Api(w.hr).get('/milestones', days='999').status_code == 400
    c = Api(w.hr).get(f'/employees/{a}/contract').json()
    assert c == {'contractEnd': in10, 'retirementDate': f'{int(dob[:4]) + 58}{dob[4:]}', 'retirementAge': 58}
    before = Api(w.hr).put(f'/employees/{a}/contract', {'contractEnd': '2000-01-01'})
    assert before.status_code == 400 and before.json() == {'error': 'the contract cannot end before the join date'}
    assert Api(w.other_hr).get(f'/employees/{a}/contract').status_code == 404
    assert Api(w.other_hr).put(f'/employees/{a}/contract', {'contractEnd': None}).status_code == 404
    assert Api(w.hr).put(f'/employees/{a}/contract', {'contractEnd': None}).status_code == 200


def test_acting_assignments_and_deputation(world):
    w = world
    t = npt_today().isoformat()
    ram = str(w.ram_e.id)
    no_pos = Api(w.hr).post('/assignments', {'employeeId': ram, 'kind': 'acting', 'startDate': t, 'endDate': t})
    assert no_pos.status_code == 400 and no_pos.json() == {'error': 'choose the position they are acting in'}
    r = Api(w.hr).post('/assignments', {'employeeId': ram, 'kind': 'acting', 'designation': 'Head of Operations', 'startDate': t, 'endDate': t})
    assert r.status_code == 201 and set(r.json()) == {'id'}
    assert Api(w.hr).post('/assignments', {'employeeId': ram, 'kind': 'acting', 'designation': 'CEO', 'startDate': t, 'endDate': t}).status_code == 409
    listed = Api(w.hr).get('/assignments').json()['assignments']
    assert listed[0]['designation'] == 'Head of Operations' and listed[0]['employee_name'] == 'Ram Thapa'
    assert Api(w.other_hr).get('/assignments').json() == {'assignments': []}
    out = S.relations_daily(datetime.now(timezone.utc) + timedelta(days=2))
    assert out['assignmentsEnded'] >= 1
    assert len(Api(w.ram).get('/assignments', employeeId=ram, status='ended').json()['assignments']) == 1
    assert Api(w.ram).get('/assignments', employeeId=str(w.sita_e.id)).status_code == 403

    sita = str(w.sita_e.id)
    no_dest = Api(w.hr).post('/assignments', {'employeeId': sita, 'kind': 'deputation', 'startDate': '2026-11-01', 'endDate': '2026-12-01'})
    assert no_dest.status_code == 400 and no_dest.json() == {'error': 'choose where they are deputed to'}
    d = Api(w.hr).post('/assignments', {'employeeId': sita, 'kind': 'deputation', 'branchId': w.branch, 'startDate': '2026-11-01', 'endDate': '2026-12-01'})
    assert d.status_code == 201
    did = d.json()['id']
    assert Api(w.ram).post(f'/assignments/{did}/end', {'cancel': True}).status_code == 403
    assert Api(w.other_hr).post(f'/assignments/{did}/end', {'cancel': True}).status_code == 404
    assert Api(w.hr).post(f'/assignments/{did}/end', {'cancel': True}).json() == {'ok': True}
    ended = Api(w.hr).post(f'/assignments/{did}/end', {})
    assert ended.status_code == 409 and ended.json() == {'error': 'this assignment is cancelled'}
    # Another company's branch and employees do not exist here.
    assert Api(w.other_hr).post('/assignments', {'employeeId': sita, 'kind': 'deputation', 'branchId': w.branch,
                                                 'startDate': '2026-11-01', 'endDate': '2026-12-01'}).status_code == 404


# ================================================================ Know Your Employee

def test_kye_campaign_confirm_progress_nudge_close(world):
    w = world
    assert Api(w.sita).post('/kye/campaigns', {'title': 'x', 'sections': ['personal'], 'dueDate': '2099-01-01'}).status_code == 403
    r = Api(w.hr).post('/kye/campaigns', {'title': 'Annual record check', 'sections': ['personal', 'contact', 'family'], 'dueDate': '2099-01-01'})
    assert r.status_code == 201 and r.json()['employees'] >= 3
    cid = r.json()['id']
    mine = Api(w.sita).get('/kye/mine').json()
    assert {k: mine['campaigns'][0][k] for k in ('id', 'status', 'due_date')} == {'id': cid, 'status': 'pending', 'due_date': '2099-01-01'}
    assert 'bank' in mine['completeness']['missing'] and mine['sectionLabels']['bank']
    assert Api(w.hr).get('/kye/mine').json() == {'campaigns': [], 'completeness': None}
    assert Api(w.sita).post(f'/kye/campaigns/{cid}/respond', {'status': 'confirmed'}).json() == {'ok': True}
    detail = Api(w.hr).get(f'/kye/campaigns/{cid}').json()
    assert detail['campaign']['title'] == 'Annual record check'
    row = next(x for x in detail['responses'] if x['employee_id'] == str(w.sita_e.id))
    assert row['status'] == 'confirmed' and 'completeness' in row and 'missing' in row
    assert Api(w.other_hr).get(f'/kye/campaigns/{cid}').status_code == 404
    n1 = Api(w.hr).post(f'/kye/campaigns/{cid}/nudge').json()['nudged']
    n2 = Api(w.hr).post(f'/kye/campaigns/{cid}/nudge').json()['nudged']
    assert n1 > 0 and n2 == 0
    camp = Api(w.hr).get('/kye/campaigns').json()['campaigns'][0]
    assert camp['confirmed'] == 1 and camp['total'] >= 3
    assert Api(w.other_hr).post(f'/kye/campaigns/{cid}/close').status_code == 404
    assert Api(w.hr).post(f'/kye/campaigns/{cid}/close').json() == {'ok': True}
    assert Api(w.hr).post(f'/kye/campaigns/{cid}/close').status_code == 404
    assert Api(w.hr).post(f'/kye/campaigns/{cid}/nudge').status_code == 409
    late = Api(w.ram).post(f'/kye/campaigns/{cid}/respond', {'status': 'confirmed'})
    assert late.status_code == 404 and late.json() == {'error': 'nothing to confirm for this campaign'}
    past = Api(w.hr).post('/kye/campaigns', {'title': 'x', 'sections': ['personal'], 'dueDate': '2020-01-01'})
    assert past.status_code == 400 and past.json() == {'error': 'the due date has already passed'}


def test_record_completeness_report_is_hr_only(world):
    w = world
    r = Api(w.hr).get('/completeness')
    assert r.status_code == 200 and r.json()['sectionLabels']
    assert next(x for x in r.json()['employees'] if x['employeeId'] == str(w.sita_e.id))['percent'] < 100
    one_emp = Api(w.hr).get('/completeness', employeeId=str(w.sita_e.id)).json()['employees']
    assert [x['employeeId'] for x in one_emp] == [str(w.sita_e.id)]
    assert Api(w.other_hr).get('/completeness', employeeId=str(w.sita_e.id)).json()['employees'] == []
    assert Api(w.sita).get('/completeness').status_code == 403


# ================================================================ sign-in pop-ups

def test_popups_each_person_sees_once_or_until_they_acknowledge(world):
    from hajiri.engage import services as engage
    w = world
    a = engage.post_announcement(w.hr, {'title': 'Office closed Friday', 'body': 'Tihar', 'popup': True})
    b = engage.post_announcement(w.hr, {'title': 'New leave policy', 'body': 'Read it', 'popup': True, 'requiresAck': True})
    p1 = Api(w.sita).get('/popups').json()['popups']
    assert {a['id'], b['id']} <= {x['id'] for x in p1}
    assert set(p1[0]) == {'id', 'title', 'body', 'requiresAck', 'createdAt', 'authorName'}
    assert Api(w.sita).post(f"/popups/{a['id']}/dismiss").json() == {'ok': True}
    need = Api(w.sita).post(f"/popups/{b['id']}/dismiss")
    assert need.status_code == 400 and need.json() == {'error': 'confirm that you have read this notice'}
    assert Api(w.sita).post(f"/popups/{b['id']}/dismiss", {'acknowledge': True}).status_code == 200
    assert Api(w.sita).get('/popups').json() == {'popups': []}
    assert len(Api(w.ram).get('/popups').json()['popups']) == 2
    assert engage.announcement_acks(w.hr, b['id'])['acked'] == 1
    assert Api(w.other_hr).post(f"/popups/{a['id']}/dismiss").status_code == 404
    assert Api(w.other_hr).get('/popups').json() == {'popups': []}


# ================================================================ registered clock-in devices

def test_devices_register_approve_revoke(world):
    w = world
    pol = Api(w.hr).get('/policy').json()['policy']
    assert Api(w.hr).put('/policy', {**pol, 'registeredDevicesOnly': True}).status_code == 200
    reg = Api(w.sita, HTTP_USER_AGENT='Mozilla/5.0 (iPhone)').post('/devices/register', {'token': TOKEN, 'label': 'My phone'})
    assert reg.status_code == 201 and reg.json()['status'] == 'pending'
    did = reg.json()['id']
    again = Api(w.sita).post('/devices/register', {'token': TOKEN, 'label': 'Again'})
    assert again.status_code == 409 and again.json() == {'error': 'this device is already registered'}
    short = Api(w.sita).post('/devices/register', {'token': 'short', 'label': 'x'})
    assert short.status_code == 400 and short.json()['issues'][0]['path'] == 'token'
    assert Api(w.hr).post('/devices/register', {'token': TOKEN + 'x', 'label': 'x'}).status_code == 400
    assert Api(w.sita).post(f'/devices/{did}/approve').status_code == 403
    assert Api(w.other_hr).post(f'/devices/{did}/approve').status_code == 409
    listed = Api(w.hr).get('/devices', status='pending').json()['devices']
    assert [(x['id'], x['label'], x['user_agent'], x['employee_name']) for x in listed] == [(did, 'My phone', 'Mozilla/5.0 (iPhone)', 'Sita Sharma')]
    assert Api(w.other_hr).get('/devices').json() == {'devices': []}
    assert Api(w.hr).get('/summary').json()['pendingDevices'] == 1
    assert Api(w.hr).post(f'/devices/{did}/approve').json() == {'ok': True}
    assert Api(w.hr).post(f'/devices/{did}/approve').status_code == 409
    bad = Api(w.hr).post(f'/devices/{did}/explode')
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    mine = Api(w.sita).get('/devices/mine').json()
    assert mine['required'] is True and [(d['id'], d['status']) for d in mine['devices']] == [(did, 'approved')]
    assert Api(w.hr).post(f'/devices/{did}/revoke').json() == {'ok': True}
    assert Api(w.sita).get('/devices/mine').json()['devices'] == []
    Api(w.hr).put('/policy', {**pol, 'registeredDevicesOnly': False})


def test_device_limit_own_approval_and_removal(world):
    w = world
    ids = [Api(w.sita).post('/devices/register', {'token': f'{TOKEN}{i}', 'label': f'Phone {i}'}).json()['id'] for i in range(3)]
    full = Api(w.sita).post('/devices/register', {'token': f'{TOKEN}9', 'label': 'Fourth'})
    assert full.status_code == 409 and full.json() == {'error': 'you already have three devices; remove one first'}
    assert Api(w.ram).delete(f'/devices/mine/{ids[0]}').status_code == 404
    assert Api(w.sita).delete(f'/devices/mine/{ids[0]}').json() == {'ok': True}
    assert Api(w.sita).delete(f'/devices/mine/{ids[0]}').status_code == 404
    assert len(Api(w.sita).get('/devices/mine').json()['devices']) == 2
    # An HR person who is also an employee cannot approve their own device.
    hr_emp = w.new_employee('HRE1')
    hr_e = make_user(w.co, roles=['hr'], employee=hr_emp)
    own = Api(hr_e).post('/devices/register', {'token': f'{TOKEN}hr', 'label': 'HR phone'}).json()['id']
    refused = Api(hr_e).post(f'/devices/{own}/approve')
    assert refused.status_code == 403 and refused.json() == {'error': 'someone else must approve your own clock-in device'}
    assert Api(w.hr).post(f'/devices/{own}/approve').status_code == 200
    unlinked = Api(w.hr).get('/devices/mine').json()
    assert unlinked == {'devices': [], 'required': False}


# ================================================================ summary

def test_summary_counts_what_is_open(world):
    w = world
    _committee(w)
    _file(w)
    _open_case(w)
    Api(w.hr).post('/kye/campaigns', {'title': 'Check', 'sections': ['personal'], 'dueDate': '2099-01-01'})
    s = Api(w.hr).get('/summary').json()
    assert s == {'openGrievances': 1, 'openCases': 1, 'pendingDevices': 0, 'openKye': 1, 'isHr': True}
    assert Api(w.other_hr).get('/summary').json() == {'openGrievances': 0, 'openCases': 0, 'pendingDevices': 0, 'openKye': 0, 'isHr': True}
    assert Api(w.sita).get('/summary').status_code == 403
