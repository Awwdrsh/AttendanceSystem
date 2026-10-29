"""
Contract review of /api/v1/relations against the React app (frontend/src/api/relations.js and pages/relations/*.jsx) and
the TS routes it was written for (65a9079 apps/api/src/modules/relations/routes.ts): the exact keys of every response
the pages read, timestamps written like JSON.stringify (UTC, milliseconds, Z), dates as 'YYYY-MM-DD', counts as numbers,
and the request bodies exactly as the React forms build them (nulls, offsets, empty bodies).
"""
from __future__ import annotations

import re
from datetime import timedelta

from hajiri.core.clock import npt_today

from .conftest import PDF_DATA_URL
from .test_api_relations import TOKEN, Api, _committee, _file, _open_case, _stages

TS = re.compile(r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$')
DAY = re.compile(r'^\d{4}-\d\d-\d\d$')

GRIEVANCE_KEYS = {
    'id', 'number', 'category', 'status', 'subject', 'description', 'concerns', 'employeeId', 'employeeName', 'empCode',
    'hearingAt', 'hearingPlace', 'outcome', 'findings', 'decidedAt', 'appealReason', 'appealedAt', 'appealOutcome',
    'appealNote', 'appealDecidedAt', 'createdAt', 'updatedAt', 'role', 'excludedUserIds',
}
CASE_KEYS = {
    'id', 'number', 'employeeId', 'employeeName', 'empCode', 'category', 'incidentDate', 'incident', 'status', 'showCause',
    'showCauseAt', 'responseDue', 'response', 'respondedAt', 'stageId', 'stageName', 'stageEffect', 'stageLetter',
    'decisionNote', 'suspensionFrom', 'suspensionTo', 'letterId', 'letterRef', 'exitId', 'decidedAt', 'createdAt', 'updatedAt',
}
ASSIGNMENT_KEYS = {
    'id', 'employee_id', 'employee_name', 'emp_code', 'kind', 'designation', 'designation_id', 'branch_id', 'branch_name',
    'org_unit_id', 'unit_name', 'start_date', 'end_date', 'note', 'status', 'created_at',
}
COMPLETENESS_KEYS = {'employeeId', 'empCode', 'name', 'missing', 'percent'}
MILESTONE_KEYS = {'kind', 'employeeId', 'empCode', 'name', 'date', 'detail'}
POLICY_KEYS = {'retirementAge', 'serviceYears', 'registeredDevicesOnly', 'appealDays'}


def _ts(v):
    assert isinstance(v, str) and TS.match(v), v


def test_grievance_shapes_and_the_react_hearing_body(world):
    from hajiri.files.service import upload
    w = world
    _committee(w)
    f = upload(w.sita, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    # MyGrievancesPage: concerns '' becomes null, no excludeUserIds.
    filed = Api(w.sita).post('/grievances', {'category': 'management', 'subject': 'Shifts', 'description': 'Night shifts',
                                             'concerns': None, 'fileIds': [f['id']]})
    assert filed.status_code == 201 and isinstance(filed.json()['number'], int)
    gid = filed.json()['id']

    mine = Api(w.sita).get('/grievances', scope='mine').json()['grievances']
    assert set(mine[0]) == GRIEVANCE_KEYS and mine[0]['role'] == 'complainant' and mine[0]['excludedUserIds'] == []
    assert mine[0]['concerns'] is None and mine[0]['empCode'] == 'E001' and mine[0]['employeeName'] == 'Sita Sharma'
    _ts(mine[0]['createdAt']), _ts(mine[0]['updatedAt'])
    # No scope reads as 'mine', like the zod default.
    assert [g['id'] for g in Api(w.sita).get('/grievances').json()['grievances']] == [gid]

    handle = Api(w.member).get('/grievances', scope='handle').json()['grievances']
    assert set(handle[0]) == GRIEVANCE_KEYS and handle[0]['role'] == 'committee' and handle[0]['description'] is None
    assert handle[0]['subject'] == 'Shifts' and handle[0]['excludedUserIds'] == []
    assert Api(w.hr).get('/grievances', scope='handle', status='decided').json() == {'grievances': []}

    # GrievanceThread: hearingAt with the Nepal offset, hearingPlace null.
    up = Api(w.member).patch(f'/grievances/{gid}', {'hearingAt': '2099-01-05T10:30:00+05:45', 'hearingPlace': None})
    assert up.status_code == 200 and up.json() == {'ok': True}
    d = Api(w.hr).get(f'/grievances/{gid}').json()
    assert set(d) == {'grievance', 'notes', 'files', 'canAppeal', 'appealUntil'}
    g = d['grievance']
    assert set(g) == GRIEVANCE_KEYS and g['role'] == 'hr' and g['status'] == 'hearing_scheduled'
    assert g['hearingAt'] == '2099-01-05T04:45:00.000Z' and g['hearingPlace'] is None
    assert d['files'] == [{'id': f['id'], 'filename': 'note.pdf', 'mime': 'application/pdf', 'bytes': d['files'][0]['bytes']}]
    assert isinstance(d['files'][0]['bytes'], int)
    assert d['canAppeal'] is False and d['appealUntil'] is None
    bad = Api(w.hr).patch(f'/grievances/{gid}', {'hearingAt': '5 January'})
    assert bad.status_code == 400 and bad.json()['issues'][0]['path'] == 'hearingAt'
    strict = Api(w.hr).patch(f'/grievances/{gid}', {'status': 'under_review', 'extra': 1})
    assert strict.status_code == 400 and strict.json()['error'] == 'validation'

    # addNote(id, body, shared): shared false keeps it internal.
    assert Api(w.member).post(f'/grievances/{gid}/notes', {'body': 'Check the roster', 'shared': False}).status_code == 201
    assert Api(w.sita).post(f'/grievances/{gid}/notes', {'body': 'Any news?', 'shared': False}).status_code == 201
    notes = Api(w.member).get(f'/grievances/{gid}').json()['notes']
    assert [set(n) for n in notes] == [{'id', 'authorName', 'mine', 'fromComplainant', 'shared', 'body', 'createdAt'}] * 2
    assert [(n['mine'], n['fromComplainant'], n['shared'], n['authorName']) for n in notes] == [
        (True, False, False, notes[0]['authorName']), (False, True, True, 'You')]
    _ts(notes[0]['createdAt'])
    assert [n['body'] for n in Api(w.sita).get(f'/grievances/{gid}').json()['notes']] == ['Any news?']

    # The attachment link the thread renders (grievanceFileUrl), inline and as a download.
    url = f"/grievances/{gid}/files/{f['id']}"
    inline = Api(w.sita).get(url)
    assert inline.status_code == 200 and inline['Content-Type'] == 'application/pdf'
    assert b''.join(inline.streaming_content if inline.streaming else [inline.content]).startswith(b'%PDF')
    dl = Api(w.hr).get(url, download='1')
    assert dl.status_code == 200 and 'attachment' in dl['Content-Disposition']
    assert Api(w.outsider).get(url).status_code == 404
    assert Api(w.other_hr).get(url).status_code == 404

    dec = Api(w.member).post(f'/grievances/{gid}/decision', {'outcome': 'partly_upheld', 'findings': 'Rotate the shifts'})
    assert dec.json() == {'ok': True}
    d = Api(w.sita).get(f'/grievances/{gid}').json()
    assert d['canAppeal'] is True
    _ts(d['appealUntil']), _ts(d['grievance']['decidedAt'])
    assert d['grievance']['findings'] == 'Rotate the shifts' and d['grievance']['outcome'] == 'partly_upheld'
    assert Api(w.sita).post(f'/grievances/{gid}/appeal', {'reason': 'Not enough'}).json() == {'ok': True}
    # GrievanceThread decideAppeal: newOutcome only when the decision changes.
    ad = Api(w.hr).post(f'/grievances/{gid}/appeal-decision', {'outcome': 'decision_changed', 'note': 'Changed', 'newOutcome': 'upheld'})
    assert ad.json() == {'ok': True}
    g = Api(w.sita).get(f'/grievances/{gid}').json()['grievance']
    assert (g['status'], g['outcome'], g['appealOutcome'], g['appealNote']) == ('closed', 'upheld', 'decision_changed', 'Changed')
    _ts(g['appealedAt']), _ts(g['appealDecidedAt'])


def test_committee_and_withdraw_with_no_body(world):
    w = world
    _committee(w)
    c = Api(w.hr).get('/grievances/committee').json()
    assert c['isMember'] is False and set(c['members'][0]) == {'user_id', 'name', 'email', 'added_at'}
    _ts(c['members'][0]['added_at'])
    gid = _file(w)['id']
    # relations.withdraw(id) and close(id) post with no body at all.
    assert Api(w.sita).post(f'/grievances/{gid}/withdraw').json() == {'ok': True}
    closed = Api(w.sita).post(f'/grievances/{gid}/close')
    assert closed.status_code == 409 and closed.json() == {'error': 'only a decided grievance can be closed'}
    assert Api(w.sita).get('/grievances/not-a-uuid').status_code == 400


def test_case_shapes_and_the_react_decide_and_stage_bodies(world):
    w = world
    stages = _stages(w)
    s0 = stages[0]
    assert isinstance(s0['severity'], int) and isinstance(s0['sort'], int) and s0['active'] is True
    # DisciplinaryPage StagesEditor: letterTemplateCode straight from letter_template_code (null for verbal warning), plus a new row.
    body = [{'id': s['id'], 'code': s['code'], 'name': s['name'], 'severity': s['severity'], 'effect': s['effect'],
             'letterTemplateCode': s['letter_template_code'], 'active': s['active']} for s in stages]
    body.append({'code': 'counselling', 'name': 'Counselling', 'severity': 1, 'effect': 'none', 'letterTemplateCode': None, 'active': True})
    assert Api(w.hr).put('/disciplinary/stages', {'stages': body}).json() == {'ok': True}
    assert [s['code'] for s in _stages(w)][-1] == 'counselling'

    cid = _open_case(w)
    k = Api(w.hr).get(f'/disciplinary/{cid}').json()['case']
    assert set(k) == CASE_KEYS and k['incidentDate'] == '2026-10-01' and isinstance(k['number'], int)
    assert k['showCause'] is None and k['showCauseAt'] is None and k['responseDue'] is None
    _ts(k['createdAt']), _ts(k['updatedAt'])
    due = (npt_today() + timedelta(days=5)).isoformat()
    assert Api(w.hr).post(f'/disciplinary/{cid}/show-cause', {'text': 'Explain', 'responseDue': due}).json() == {'ok': True}
    k = Api(w.sita).get('/disciplinary/mine').json()['cases'][0]
    assert set(k) == CASE_KEYS and k['responseDue'] == due and k['stageLetter'] is None and k['exitId'] is None
    _ts(k['showCauseAt'])
    # decideCase: stageId null, force false, no dates -> the deadline has not passed yet.
    early = Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': None, 'note': 'No action', 'force': False})
    assert early.status_code == 409 and early.json()['error'].startswith('the employee has until ')
    assert Api(w.sita).post(f'/disciplinary/{cid}/respond', {'response': 'Sorry'}).json() == {'ok': True}
    written = next(s for s in stages if s['code'] == 'written_warning')
    assert Api(w.hr).post(f'/disciplinary/{cid}/decide', {'stageId': written['id'], 'note': 'Warning', 'force': False}).json() == {'ok': True}
    hr_view = Api(w.hr).get('/disciplinary', employeeId=str(w.sita_e.id)).json()['cases'][0]
    assert set(hr_view) == CASE_KEYS
    assert (hr_view['stageName'], hr_view['stageEffect'], hr_view['stageLetter']) == ('Written warning', 'none', 'warning')
    _ts(hr_view['decidedAt']), _ts(hr_view['respondedAt'])
    own = Api(w.sita).get(f'/disciplinary/{cid}').json()['case']
    assert own['stageLetter'] is None and own['response'] == 'Sorry'
    # withdrawCase(id, reason) on a decided case.
    assert Api(w.hr).post(f'/disciplinary/{cid}/withdraw', {'reason': 'x'}).status_code == 409


def test_milestones_contract_and_completeness_shapes(world):
    w = world
    in10 = (npt_today() + timedelta(days=10)).isoformat()
    a = str(w.new_employee('MS2').id)
    assert Api(w.hr).put(f'/employees/{a}/contract', {'contractEnd': in10}).json() == {'ok': True}
    # MilestonesPage: relations.milestones(Number(days)).
    body = Api(w.hr).get('/milestones', days=30).json()
    assert set(body) == {'milestones', 'policy'} and set(body['policy']) == POLICY_KEYS
    m = next(x for x in body['milestones'] if x['employeeId'] == a)
    assert set(m) == MILESTONE_KEYS and m == {'kind': 'contract_end', 'employeeId': a, 'empCode': 'MS2', 'name': 'Person MS2',
                                              'date': in10, 'detail': 'Contract ends'}
    assert Api(w.hr).get('/milestones').status_code == 200
    sep = Api(w.hr).get('/separating').json()
    assert set(sep) == {'people'} and set(sep['people'][0]) == MILESTONE_KEYS
    c = Api(w.hr).get(f'/employees/{a}/contract').json()
    assert c == {'contractEnd': in10, 'retirementDate': None, 'retirementAge': 58}
    comp = Api(w.hr).get('/completeness').json()
    assert set(comp) == {'employees', 'sectionLabels'} and set(comp['employees'][0]) == COMPLETENESS_KEYS
    assert all(isinstance(e['percent'], int) for e in comp['employees'])
    # MilestonesPage savePolicy(p): the whole policy back.
    pol = Api(w.hr).get('/policy').json()['policy']
    assert Api(w.hr).put('/policy', {**pol, 'retirementAge': 60}).json() == {'policy': {**pol, 'retirementAge': 60}}


def test_assignment_shapes_and_the_react_form_bodies(world):
    from hajiri.db.models import Designation
    w = world
    t = npt_today()
    start, end = (t + timedelta(days=1)).isoformat(), (t + timedelta(days=20)).isoformat()
    des = Designation.objects.create(company=w.co, code='OPS', name_en='Operations Head')
    # AssignmentForm acting: designationId, note null.
    r = Api(w.hr).post('/assignments', {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'startDate': start, 'endDate': end,
                                        'note': None, 'designationId': str(des.id)})
    assert r.status_code == 201, r.content
    # AssignmentForm deputation: branchId set, orgUnitId null (an empty Mantine select).
    d = Api(w.hr).post('/assignments', {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'startDate': start, 'endDate': end,
                                        'note': 'Cover', 'branchId': w.branch, 'orgUnitId': None})
    assert d.status_code == 201, d.content
    listed = Api(w.hr).get('/assignments', status='active').json()['assignments']
    assert [set(x) for x in listed] == [ASSIGNMENT_KEYS] * 2
    acting = next(x for x in listed if x['kind'] == 'acting')
    assert (acting['designation'], acting['designation_id'], acting['start_date'], acting['end_date']) == (
        'Operations Head', str(des.id), start, end)
    _ts(acting['created_at'])
    dep = next(x for x in listed if x['kind'] == 'deputation')
    assert (dep['branch_id'], dep['branch_name'], dep['org_unit_id'], dep['unit_name'], dep['note']) == (
        w.branch, 'Head Office', None, None, 'Cover')
    # EmployeeRelationsSection: assignments({employeeId, status: 'all'}).
    assert len(Api(w.hr).get('/assignments', employeeId=str(w.sita_e.id), status='all').json()['assignments']) == 1
    # endAssignment(id, {}) ends today; the start is tomorrow, so it is refused.
    early = Api(w.hr).post(f"/assignments/{d.json()['id']}/end", {})
    assert early.status_code == 400 and early.json() == {'error': 'the end date is before the start date; cancel it instead'}
    assert Api(w.hr).post(f"/assignments/{d.json()['id']}/end", {'cancel': True}).json() == {'ok': True}


def test_kye_shapes_and_the_react_launch_body(world):
    w = world
    # KyePage LaunchCampaign: message null, audienceKind 'all', audienceId null.
    r = Api(w.hr).post('/kye/campaigns', {'title': 'Record check', 'message': None, 'sections': ['personal', 'bank'],
                                          'audienceKind': 'all', 'audienceId': None, 'dueDate': '2099-01-01'})
    assert r.status_code == 201 and set(r.json()) == {'id', 'employees'} and isinstance(r.json()['employees'], int)
    cid = r.json()['id']
    br = Api(w.hr).post('/kye/campaigns', {'title': 'HQ', 'message': '', 'sections': ['photo'], 'audienceKind': 'branch',
                                           'audienceId': w.branch, 'dueDate': '2099-01-01'})
    assert br.status_code == 201 and br.json()['employees'] >= 2
    camps = Api(w.hr).get('/kye/campaigns').json()['campaigns']
    assert set(camps[0]) == {'id', 'title', 'message', 'sections', 'audience_kind', 'audience_id', 'due_date', 'status',
                             'created_at', 'closed_at', 'total', 'confirmed', 'changes_requested'}
    c = next(x for x in camps if x['id'] == cid)
    assert (c['message'], c['sections'], c['audience_kind'], c['audience_id'], c['due_date'], c['closed_at']) == (
        None, ['personal', 'bank'], 'all', None, '2099-01-01', None)
    assert all(isinstance(c[k], int) for k in ('total', 'confirmed', 'changes_requested'))
    _ts(c['created_at'])
    # RelationsHomeCard respondKye(id, 'confirmed') sends note undefined; KyePage sends a note with changes.
    assert Api(w.sita).post(f'/kye/campaigns/{cid}/respond', {'status': 'changes_requested', 'note': 'New address'}).json() == {'ok': True}
    mine = Api(w.sita).get('/kye/mine').json()
    assert set(mine) == {'campaigns', 'completeness', 'sectionLabels'}
    assert set(mine['campaigns'][0]) == {'id', 'title', 'message', 'sections', 'due_date', 'status', 'responded_at'}
    assert set(mine['completeness']) == COMPLETENESS_KEYS
    detail = Api(w.hr).get(f'/kye/campaigns/{cid}').json()
    assert set(detail) == {'campaign', 'responses'}
    assert set(detail['campaign']) == {'id', 'title', 'message', 'sections', 'audience_kind', 'due_date', 'status'}
    row = next(x for x in detail['responses'] if x['employee_id'] == str(w.sita_e.id))
    assert set(row) == {'employee_id', 'name_en', 'emp_code', 'status', 'note', 'responded_at', 'last_nudged_at', 'completeness', 'missing'}
    assert (row['status'], row['note'], row['last_nudged_at']) == ('changes_requested', 'New address', None)
    _ts(row['responded_at'])
    # Pending people come first.
    assert detail['responses'][0]['status'] == 'pending'
    assert Api(w.hr).post(f'/kye/campaigns/{cid}/nudge').json()['nudged'] >= 1
    assert Api(w.hr).post(f'/kye/campaigns/{cid}/close').json() == {'ok': True}
    closed = next(x for x in Api(w.hr).get('/kye/campaigns').json()['campaigns'] if x['id'] == cid)
    assert closed['status'] == 'closed'
    _ts(closed['closed_at'])


def test_popup_dismiss_with_acknowledge_false_and_device_shapes(world):
    from hajiri.engage import services as engage
    w = world
    a = engage.post_announcement(w.hr, {'title': 'Closed', 'body': 'Tihar', 'popup': True})
    p = Api(w.sita).get('/popups').json()['popups'][0]
    _ts(p['createdAt'])
    assert p['requiresAck'] is False and p['authorName']
    # PopupNotices: dismissPopup(p.id, p.requiresAck) sends {acknowledge: false}.
    assert Api(w.sita).post(f"/popups/{a['id']}/dismiss", {'acknowledge': False}).json() == {'ok': True}

    reg = Api(w.sita, HTTP_USER_AGENT='UA').post('/devices/register', {'token': TOKEN, 'label': 'Phone'})
    assert reg.status_code == 201 and set(reg.json()) == {'id', 'status'}
    mine = Api(w.sita).get('/devices/mine').json()
    assert set(mine) == {'devices', 'required'}
    dv = mine['devices'][0]
    assert set(dv) == {'id', 'label', 'status', 'requested_at', 'decided_at', 'last_used_at'}
    _ts(dv['requested_at'])
    assert dv['decided_at'] is None and dv['last_used_at'] is None
    # RegisteredDevicesPage: devices(status) with 'all' by default.
    listed = Api(w.hr).get('/devices', status='all').json()['devices']
    assert set(listed[0]) == {'id', 'employee_id', 'employee_name', 'emp_code', 'label', 'user_agent', 'status', 'requested_at',
                              'decided_at', 'last_used_at'}
    assert Api(w.hr).post(f"/devices/{dv['id']}/reject").json() == {'ok': True}
    after = Api(w.sita).get('/devices/mine').json()['devices'][0]
    assert after['status'] == 'rejected'
    _ts(after['decided_at'])


def test_null_and_wrong_typed_bodies_are_validation_errors(world):
    w = world
    for user, path, body in ((w.sita, '/grievances', {}), (w.hr, '/disciplinary', {'employeeId': 'x'}),
                             (w.hr, '/assignments', {'kind': 'acting'}), (w.hr, '/kye/campaigns', {'title': ''})):
        r = Api(user).post(path, body)
        assert r.status_code == 400 and r.json()['error'] == 'validation' and r.json()['issues'], (path, r.content)
    # A signed-in account with no employee record cannot file a grievance (checked before the body, as in TS).
    r = Api(w.hr).post('/grievances', {})
    assert r.status_code == 400 and r.json() == {'error': 'Your account is not linked to an employee record.'}
