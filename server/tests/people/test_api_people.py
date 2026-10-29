"""
HTTP-level port of apps/api/test/people.test.ts: the /api/v1/people routes the React app calls (structure, directory,
org chart, summary, card, avatar, profile sections, career events, change requests, history), with their status codes
and JSON shapes, plus company isolation (another company's ids are 404, its lists are empty).
"""
from datetime import datetime, timedelta, timezone

from hajiri.core.clock import npt_today
from hajiri.people.career import apply_due_career_events
from hajiri.workflow import services as wf
from tests.support.sql import one, rows

from .conftest import PDF, PNG, Api, add_days, upload

TODAY = lambda: npt_today().isoformat()  # noqa: E731


def apis(w):
    return {k: Api(getattr(w, k)) for k in ('hr', 'hr2', 'sita', 'ram', 'peer', 'foreign_hr')}


def build_structure(a):
    dept = a['hr'].post('/people/structure/units', {'kind': 'department', 'code': 'ENG', 'nameEn': 'Engineering', 'headEmployeeId': a['_w'].ram_id})
    assert dept.status_code == 201, dept.json()
    dept_id = dept.json()['unit']['id']
    assert dept.json()['unit'] == {'id': dept_id, 'code': 'ENG', 'name_en': 'Engineering', 'kind': 'department'}
    team = a['hr'].post('/people/structure/units', {'kind': 'team', 'code': 'ENG-WEB', 'nameEn': 'Web', 'parentId': dept_id})
    assert team.status_code == 201
    des = a['hr'].post('/people/structure/designations',
                       {'code': 'SE2', 'nameEn': 'Software Engineer II', 'grade': 'G5', 'jobLevel': 5, 'orgUnitId': dept_id})
    assert des.status_code == 201
    assert set(des.json()['designation']) == {'id', 'code', 'name_en', 'grade', 'job_level'}
    br = a['hr'].post('/people/structure/branches', {'code': 'PKR', 'nameEn': 'Pokhara', 'address': 'Lakeside'})
    assert br.status_code == 201
    assert set(br.json()['branch']) == {'id', 'code', 'name_en'}
    return {'dept': dept_id, 'team': team.json()['unit']['id'], 'designation': des.json()['designation']['id'],
            'branch2': br.json()['branch']['id']}


def setup(world):
    a = apis(world)
    a['_w'] = world
    return a, build_structure(a)


def promote_and_transfer(a, s):
    w = a['_w']
    ev = a['hr'].post(f'/people/{w.sita_id}/career', {'kind': 'promotion', 'effectiveDate': TODAY(),
                                                      'details': {'designationId': s['designation'], 'reason': 'Great year'},
                                                      'note': 'Annual review'})
    assert ev.status_code == 201, ev.json()
    date3 = add_days(npt_today(), -3)
    tr = a['hr'].post(f'/people/{w.sita_id}/career', {'kind': 'transfer', 'effectiveDate': date3,
                                                      'details': {'branchId': s['branch2'], 'orgUnitId': s['team']}})
    assert tr.status_code == 201
    return ev, date3


# ---------------------------------------------------------------- org structure

def test_hr_builds_structure_employees_cannot(world):
    a, _ = setup(world)
    for who in ('sita', 'ram'):
        r = a[who].post('/people/structure/units', {'kind': 'team', 'code': 'X', 'nameEn': 'X'})
        assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}


def test_structure_rejects_cycles_foreign_refs_and_deleting_used_units(world):
    a, s = setup(world)
    w = world
    assert a['hr'].patch(f"/people/structure/units/{s['dept']}", {'parentId': s['team']}).status_code == 400
    r = a['hr'].patch(f"/people/structure/units/{s['dept']}", {'parentId': s['dept']})
    assert r.status_code == 400 and r.json()['error'] == 'a unit cannot sit under itself or one of its own sub-units'
    r = a['foreign_hr'].post('/people/structure/units', {'kind': 'team', 'code': 'F1', 'nameEn': 'F', 'parentId': s['dept']})
    assert r.status_code == 400 and r.json()['error'] == 'parent unit does not exist in your company'
    assert a['hr'].delete(f"/people/structure/units/{s['dept']}").status_code == 409           # has a sub-unit
    assert a['hr'].delete(f'/people/structure/branches/{w.branch_id}').status_code == 409
    r = a['foreign_hr'].patch(f"/people/structure/units/{s['dept']}", {'nameEn': 'Hacked'})
    assert r.status_code == 404 and r.json() == {'error': 'unit not found'}
    assert a['foreign_hr'].delete(f"/people/structure/designations/{s['designation']}").status_code == 404
    assert a['foreign_hr'].patch(f"/people/structure/branches/{s['branch2']}", {'nameEn': 'X'}).status_code == 404
    # Validation in the TS shape; a malformed id is a validation error, not a crash.
    r = a['hr'].post('/people/structure/branches', {'code': '', 'nameEn': 'X'})
    assert r.status_code == 400 and r.json()['error'] == 'validation' and r.json()['issues'][0]['path'] == 'code'
    r = a['hr'].patch('/people/structure/units/not-a-uuid', {'nameEn': 'X'})
    assert r.status_code == 400
    # Duplicate codes say which kind clashed.
    r = a['hr'].post('/people/structure/branches', {'code': 'PKR', 'nameEn': 'Again'})
    assert r.status_code == 409 and r.json()['error'] == 'another branch already uses this code'


def test_structure_edit_and_delete(world):
    a, s = setup(world)
    r = a['hr'].patch(f"/people/structure/designations/{s['designation']}", {'nameEn': 'Senior Engineer'})
    assert r.status_code == 200 and r.json()['designation']['name_en'] == 'Senior Engineer'
    r = a['hr'].patch(f"/people/structure/branches/{s['branch2']}", {'phone': '061-555'})
    assert r.status_code == 200 and r.json()['branch']['code'] == 'PKR'
    assert a['hr'].delete(f"/people/structure/units/{s['team']}").json() == {'ok': True}
    assert a['hr'].delete(f"/people/structure/designations/{s['designation']}").json() == {'ok': True}
    assert a['hr'].delete(f"/people/structure/branches/{s['branch2']}").json() == {'ok': True}
    assert a['hr'].delete(f"/people/structure/branches/{s['branch2']}").status_code == 404


def test_structure_lists_headcounts_to_company_only(world):
    a, s = setup(world)
    r = a['sita'].get('/people/structure')
    assert r.status_code == 200 and set(r.json()) == {'branches', 'units', 'designations'}
    unit = next(u for u in r.json()['units'] if u['id'] == s['dept'])
    assert unit['name_en'] == 'Engineering' and isinstance(unit['head_name'], str) and unit['headcount'] == 0
    assert a['foreign_hr'].get('/people/structure').json()['units'] == []


def test_policy_is_hr_only(world):
    a = apis(world)
    assert a['hr'].get('/people/policy').json() == {'policy': {'makerChecker': False}}
    assert a['sita'].get('/people/policy').status_code == 403
    assert a['sita'].put('/people/policy', {'makerChecker': True}).status_code == 403
    r = a['hr'].put('/people/policy', {'makerChecker': True})
    assert r.status_code == 200 and r.json() == {'policy': {'makerChecker': True}}
    r = a['hr'].put('/people/policy', {'makerChecker': True, 'x': 1})          # zod .strict()
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert a['foreign_hr'].get('/people/policy').json() == {'policy': {'makerChecker': False}}


# ---------------------------------------------------------------- career events

def test_promotion_today_updates_designation_and_history(world):
    a, s = setup(world)
    w = world
    ev, _ = promote_and_transfer(a, s)
    assert ev.json()['event']['status'] == 'applied'
    e = one('SELECT designation, designation_id, grade FROM employee WHERE id = %s', [w.sita_id])
    assert e['designation'] == 'Software Engineer II' and str(e['designation_id']) == s['designation'] and e['grade'] == 'G5'
    hist = a['sita'].get(f'/people/{w.sita_id}/history')
    assert hist.status_code == 200
    assert {'Designation', 'Grade'} <= {h['field'] for h in hist.json()['history']}


def test_transfer_splits_assignment_at_its_date(world):
    a, s = setup(world)
    w = world
    _, date3 = promote_and_transfer(a, s)
    rs = rows("SELECT lower(valid)::text AS lo, upper(valid)::text AS hi, branch_id, org_unit_id, manager_id FROM employee_assignment "
              'WHERE employee_id = %s ORDER BY lower(valid)', [w.sita_id])
    last = rs[-1]
    assert (last['lo'], last['hi'], str(last['branch_id']), str(last['org_unit_id']), str(last['manager_id'])) == \
        (date3, None, s['branch2'], s['team'], w.ram_id)
    assert rs[-2]['hi'] == date3


def test_future_event_is_scheduled_then_applied(world):
    a, s = setup(world)
    w = world
    promote_and_transfer(a, s)
    ev = a['hr'].post(f'/people/{w.sita_id}/career', {'kind': 'grade_change', 'effectiveDate': add_days(npt_today(), 5),
                                                      'details': {'grade': 'G6'}})
    assert ev.status_code == 201 and ev.json()['event']['status'] == 'scheduled'
    assert one('SELECT grade FROM employee WHERE id = %s', [w.sita_id])['grade'] == 'G5'
    assert apply_due_career_events(datetime.now(timezone.utc)) == 0
    assert apply_due_career_events(datetime.now(timezone.utc) + timedelta(days=6)) == 1
    assert one('SELECT grade FROM employee WHERE id = %s', [w.sita_id])['grade'] == 'G6'


def test_scheduled_separation_cancel_and_validation(world):
    a = apis(world)
    w = world
    lwd = add_days(npt_today(), 20)
    assert a['hr'].post(f'/people/{w.peer_id}/career', {'kind': 'separation', 'effectiveDate': lwd, 'details': {}}).status_code == 400
    ev = a['hr'].post(f'/people/{w.peer_id}/career', {'kind': 'separation', 'effectiveDate': lwd,
                                                      'details': {'separationType': 'resignation', 'reason': 'Studies abroad'}})
    assert ev.status_code == 201 and ev.json()['event']['status'] == 'scheduled'
    e = one('SELECT exit_date::text AS exit_date, status FROM employee WHERE id = %s', [w.peer_id])
    assert e == {'exit_date': lwd, 'status': 'active'}
    dup = a['hr'].post(f'/people/{w.peer_id}/career', {'kind': 'separation', 'effectiveDate': lwd, 'details': {'separationType': 'resignation'}})
    assert dup.status_code == 409
    cancel = a['hr'].post(f"/people/{w.peer_id}/career/{ev.json()['event']['id']}/cancel")
    assert cancel.status_code == 200 and cancel.json() == {'ok': True}
    assert one('SELECT exit_date FROM employee WHERE id = %s', [w.peer_id])['exit_date'] is None
    assert a['hr'].post(f'/people/{w.peer_id}/career', {'kind': 'manager_change', 'effectiveDate': TODAY(),
                                                        'details': {'managerId': w.peer_id}}).status_code == 400
    assert a['hr'].post(f'/people/{w.peer_id}/career', {'kind': 'grade_change', 'effectiveDate': '2020-01-01',
                                                        'details': {'grade': 'X'}}).status_code == 400
    assert a['foreign_hr'].post(f"/people/{w.peer_id}/career/{ev.json()['event']['id']}/cancel").status_code == 404


def test_timeline_visibility(world):
    a, s = setup(world)
    w = world
    promote_and_transfer(a, s)
    own = a['sita'].get(f'/people/{w.sita_id}/career')
    assert own.status_code == 200
    promo = next(e for e in own.json()['events'] if e['kind'] == 'promotion')
    assert promo['label'] == 'Promoted' and 'Software Engineer II' in promo['summary'] and promo['reason'] == 'Great year'
    assert set(promo) == {'id', 'kind', 'label', 'effectiveDate', 'status', 'summary', 'note', 'reason', 'createdBy', 'createdAt'}
    mgr = a['ram'].get(f'/people/{w.sita_id}/career')
    assert mgr.status_code == 200
    assert next(e for e in mgr.json()['events'] if e['kind'] == 'promotion')['reason'] is None
    assert a['peer'].get(f'/people/{w.sita_id}/career').status_code == 403
    assert a['ram'].post(f'/people/{w.sita_id}/career', {'kind': 'note', 'effectiveDate': TODAY()}).status_code == 403
    assert a['foreign_hr'].get(f'/people/{w.sita_id}/career').status_code == 404


# ---------------------------------------------------------------- profile sections

def test_hr_edits_sections_ids_encrypted_and_masked(world):
    a = apis(world)
    w = world
    put = a['hr'].put(f'/people/{w.sita_id}/profile/ids', {'citizenshipNo': '27-01-75-12345', 'citizenshipDistrict': 'Kathmandu',
                                                            'passportNo': 'PA1234567'})
    assert put.status_code == 200 and put.json()['pending'] is False
    assert next(c for c in put.json()['changes'] if c['label'] == 'Citizenship number')['to'] == '•••• 2345'
    raw = bytes(one('SELECT citizenship_no_enc FROM employee WHERE id = %s', [w.sita_id])['citizenship_no_enc'])
    assert '12345' not in raw.decode('latin1')
    masked = a['sita'].get(f'/people/{w.sita_id}/profile')
    assert set(masked.json()) == {'profile', 'pending', 'secretFields'}
    assert masked.json()['profile']['ids']['citizenshipNo'] == '•••• 2345'
    revealed = a['hr'].get(f'/people/{w.sita_id}/profile', reveal='1')
    assert revealed.json()['profile']['ids']['passportNo'] == 'PA1234567'
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'read_sensitive' AND table_name = 'employee_profile' AND row_id = %s",
                    [w.sita_id])) == 1


def test_section_validation(world):
    a = apis(world)
    w = world
    members = [{'name': 'Hari', 'relation': 'father', 'isNominee': True, 'nomineeShare': 60},
               {'name': 'Gita', 'relation': 'mother', 'isNominee': True, 'nomineeShare': 30}]
    assert a['hr'].put(f'/people/{w.sita_id}/profile/family', {'members': members}).status_code == 400
    members[1]['nomineeShare'] = 40
    assert a['hr'].put(f'/people/{w.sita_id}/profile/family', {'members': members}).status_code == 200
    assert a['hr'].put(f'/people/{w.sita_id}/profile/salary', {}).status_code == 404
    assert a['hr'].put(f'/people/{w.sita_id}/profile/personal', {'status': 'exited'}).status_code == 400


def test_only_employee_and_hr_read_a_profile(world):
    a = apis(world)
    w = world
    assert a['ram'].get(f'/people/{w.sita_id}/profile').status_code == 403
    assert a['peer'].get(f'/people/{w.sita_id}/profile').status_code == 403
    assert a['foreign_hr'].get(f'/people/{w.sita_id}/profile').status_code == 404
    assert a['sita'].put(f'/people/{w.sita_id}/profile/personal', {'bloodGroup': 'O+'}).status_code == 403
    assert a['foreign_hr'].put(f'/people/{w.sita_id}/profile/personal', {'bloodGroup': 'O+'}).status_code == 404
    assert a['foreign_hr'].get(f'/people/{w.sita_id}/history').status_code == 404
    assert a['hr'].get('/people/not-a-uuid/profile').status_code == 400


# ---------------------------------------------------------------- profile change requests

def _item(user, object_id):
    return next((i for i in wf.inbox_for(user) if i['objectId'] == str(object_id)), None)


def test_change_request_with_proof_approved_and_applied(world):
    a = apis(world)
    w = world
    proof = upload(w.sita, 'profile_change', PDF, 'citizenship.pdf')
    r = a['sita'].post('/people/me/change-requests', {
        'section': 'contact', 'value': {'personalPhone': '+977 9800000001',
                                        'permanentAddress': {'district': 'Lalitpur', 'municipality': 'Lalitpur Metropolitan', 'ward': 5}},
        'reason': 'Moved', 'proofFileId': proof})
    assert r.status_code == 201, r.json()
    req_id = r.json()['request']['id']
    assert r.json()['request']['sectionLabel'] == 'Contact and address'
    dup = a['sita'].post('/people/me/change-requests', {'section': 'contact', 'value': {'personalPhone': '+977 9800000002'}})
    assert dup.status_code == 409
    item = _item(w.hr, req_id)
    assert item['summary']['sectionLabel'] == 'Contact and address' and item['summary']['hasProof'] is True
    assert _item(w.ram, req_id) is None
    proof_r = a['hr'].get(f'/people/change-requests/{req_id}/proof')
    assert proof_r.status_code == 200 and proof_r['Content-Type'].startswith('application/pdf')
    assert a['peer'].get(f'/people/change-requests/{req_id}/proof').status_code == 404
    assert a['foreign_hr'].get(f'/people/change-requests/{req_id}/proof').status_code == 404
    # HR's list and one request (with the diff while pending).
    lst = a['hr'].get('/people/change-requests')
    assert lst.status_code == 200 and [x['id'] for x in lst.json()['requests']] == [req_id]
    assert lst.json()['requests'][0]['employee_name'] == 'Sita Sharma'
    assert a['sita'].get('/people/change-requests').status_code == 403
    one_r = a['hr'].get(f'/people/change-requests/{req_id}')
    assert one_r.status_code == 200 and set(one_r.json()) == {'request', 'changes'}
    assert 'payload_enc' not in one_r.json()['request']
    assert {'Personal phone', 'Permanent address'} <= {c['label'] for c in one_r.json()['changes']}
    assert a['sita'].get(f'/people/change-requests/{req_id}').status_code == 200
    assert a['peer'].get(f'/people/change-requests/{req_id}').status_code == 404
    assert a['foreign_hr'].get(f'/people/change-requests/{req_id}').status_code == 404
    assert a['foreign_hr'].get('/people/change-requests').json() == {'requests': []}
    assert wf.decide(w.hr, item['instanceId'], 'approve') == 'approved'
    p = a['sita'].get(f'/people/{w.sita_id}/profile').json()
    assert p['profile']['contact']['personalPhone'] == '+977 9800000001'
    assert p['profile']['contact']['permanentAddress']['district'] == 'Lalitpur'
    mine = a['sita'].get('/people/me/change-requests')
    assert next(x for x in mine.json()['requests'] if x['id'] == req_id)['status'] == 'approved'
    hist = a['hr'].get(f'/people/{w.sita_id}/history').json()['history']
    assert any(h['field'] == 'Personal phone' and h['source'] == 'change_request' for h in hist)


def test_no_op_request_refused_and_cancel(world):
    a = apis(world)
    w = world
    r = a['sita'].post('/people/me/change-requests', {'section': 'skills', 'value': {'skills': ['TypeScript'], 'languages': ['English', 'Nepali']}})
    assert r.status_code == 201
    rid = r.json()['request']['id']
    assert a['peer'].post(f'/people/me/change-requests/{rid}/cancel').status_code == 404
    assert a['foreign_hr'].post(f'/people/me/change-requests/{rid}/cancel').status_code == 404
    c = a['sita'].post(f'/people/me/change-requests/{rid}/cancel')
    assert c.status_code == 200 and c.json() == {'ok': True}
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [rid])['state'] == 'cancelled'
    assert a['sita'].post(f'/people/me/change-requests/{rid}/cancel').status_code == 409
    # Nothing to change: refused.
    a['hr'].put(f'/people/{w.sita_id}/profile/contact', {'personalPhone': '+977 9800000001'})
    same = a['sita'].post('/people/me/change-requests', {'section': 'contact', 'value': {'personalPhone': '+977 9800000001'}})
    assert same.status_code == 409
    # Someone without an employee record has no profile to change and no requests.
    assert a['hr'].post('/people/me/change-requests', {'section': 'skills', 'value': {'skills': ['x']}}).status_code == 403
    assert a['hr'].get('/people/me/change-requests').json() == {'requests': []}


def test_rejected_bank_change_not_applied(world):
    a = apis(world)
    w = world
    r = a['sita'].post('/people/me/change-requests', {'section': 'bank', 'value': {'bankCode': 'NABIL', 'bankAccount': '0123456789012'}})
    assert r.status_code == 201
    assert '0123456789012' not in str(r.json()['request']['summary'])
    rid = r.json()['request']['id']
    inst = str(one('SELECT id FROM workflow_instance WHERE object_id = %s', [rid])['id'])
    assert wf.decide(w.hr, inst, 'reject', reason='Account name does not match') == 'rejected'
    assert one('SELECT bank_code FROM employee WHERE id = %s', [w.sita_id])['bank_code'] != 'NABIL'


def test_maker_checker_bank_edit_waits_for_second_hr(world):
    a = apis(world)
    w = world
    assert a['hr'].put('/people/policy', {'makerChecker': True}).status_code == 200
    edit = a['hr'].put(f'/people/{w.ram_id}/profile/bank', {'bankCode': 'NIC', 'bankAccount': '9988776655'})
    assert edit.status_code == 202 and edit.json()['pending'] is True
    via_employees = a['hr'].patch(f'/employees/{w.ram_id}', {'bankAccount': '1111222233'})
    assert via_employees.status_code == 409
    inst = str(one('SELECT id FROM workflow_instance WHERE object_id = %s', [edit.json()['request']['id']])['id'])
    assert wf.decide(w.hr2, inst, 'approve') == 'approved'
    e = one('SELECT bank_code, bank_account_last4 FROM employee WHERE id = %s', [w.ram_id])
    assert e == {'bank_code': 'NIC', 'bank_account_last4': '6655'}
    direct = a['hr'].put(f'/people/{w.ram_id}/profile/personal', {'bloodGroup': 'B+'})
    assert direct.status_code == 200 and direct.json()['pending'] is False


# ---------------------------------------------------------------- directory, org chart, avatar and summary

def test_directory_without_personal_data_search_and_filters(world):
    a, s = setup(world)
    w = world
    promote_and_transfer(a, s)
    a['hr'].put(f'/people/{w.sita_id}/profile/contact', {'personalPhone': '+977 9800000001'})
    d = a['peer'].get('/people/directory')
    assert d.status_code == 200 and set(d.json()) == {'people'}
    me = next(p for p in d.json()['people'] if p['id'] == w.sita_id)
    assert me['designation'] == 'Software Engineer II' and me['unit_name'] == 'Web' and isinstance(me['manager_name'], str)
    assert '9800000001' not in d.content.decode()
    assert a['peer'].get('/people/directory', search='software%').json()['people'] == []        # % is literal
    assert [p['id'] for p in a['peer'].get('/people/directory', orgUnitId=s['team']).json()['people']] == [w.sita_id]
    assert a['foreign_hr'].get('/people/directory').json()['people'] == []
    assert a['peer'].get('/people/directory', orgUnitId='nope').status_code == 400


def test_org_chart_and_card(world):
    a = apis(world)
    w = world
    c = a['sita'].get('/people/org-chart')
    assert c.status_code == 200
    assert next(n for n in c.json()['nodes'] if n['id'] == w.sita_id)['manager_id'] == w.ram_id
    card = a['sita'].get(f'/people/{w.ram_id}/card')
    assert card.status_code == 200 and set(card.json()) == {'person', 'reports'}
    assert w.sita_id in [r['id'] for r in card.json()['reports']]
    assert a['foreign_hr'].get(f'/people/{w.ram_id}/card').status_code == 404
    r = a['sita'].get('/people/not-a-uuid/card')
    assert r.status_code == 400 and r.json()['error'] == 'validation'
    assert a['foreign_hr'].get('/people/org-chart').json() == {'nodes': []}


def test_avatar_self_service_and_hr(world):
    a = apis(world)
    w = world
    file_id = upload(w.sita, 'avatar', PNG, 'me.png')
    r = a['sita'].post('/people/me/avatar', {'fileId': file_id})
    assert r.status_code == 200 and r.json() == {'ok': True}
    img = a['peer'].get(f'/people/{w.sita_id}/avatar')
    assert img.status_code == 200 and 'image/png' in img['Content-Type']
    assert a['foreign_hr'].get(f'/people/{w.sita_id}/avatar').status_code == 404
    # Someone else's upload cannot be attached as your photo.
    theirs = upload(w.peer, 'avatar', PNG, 'p.png')
    assert a['sita'].post('/people/me/avatar', {'fileId': theirs}).status_code == 403
    replaced = upload(w.sita, 'avatar', PNG, 'me2.png')
    assert a['sita'].post('/people/me/avatar', {'fileId': replaced}).status_code == 200
    assert one('SELECT deleted_at FROM stored_file WHERE id = %s', [file_id])['deleted_at'] is not None
    assert a['sita'].post('/people/me/avatar', {}).status_code == 400
    # Removing; HR sets and removes other people's photos; employees cannot.
    assert a['sita'].delete('/people/me/avatar').json() == {'ok': True}
    assert a['peer'].get(f'/people/{w.sita_id}/avatar').status_code == 404
    hr_file = upload(w.hr, 'avatar', PNG, 'x.png')
    assert a['peer'].post(f'/people/{w.sita_id}/avatar', {'fileId': hr_file}).status_code == 403
    assert a['hr'].post(f'/people/{w.sita_id}/avatar', {'fileId': hr_file}).status_code == 200
    assert a['hr'].delete(f'/people/{w.sita_id}/avatar').status_code == 200
    assert a['foreign_hr'].delete(f'/people/{w.sita_id}/avatar').status_code == 404
    assert a['hr'].post('/people/me/avatar', {'fileId': hr_file}).status_code == 403   # no employee record


def test_summary_counts_the_company(world):
    a, s = setup(world)
    promote_and_transfer(a, s)
    r = a['sita'].get('/people/summary')
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {'active', 'joinedLast30Days', 'onProbation', 'leaving', 'byUnit', 'byBranch'}
    assert body['active'] >= 3
    assert next(u for u in body['byUnit'] if u['id'] == s['team'])['count'] == 1
    assert a['foreign_hr'].get('/people/summary').json()['active'] == 0


# ---------------------------------------------------------------- employees module integration

def test_creating_an_employee_records_hire_and_links_designation(world):
    a, s = setup(world)
    w = world
    c = a['hr'].post('/employees', {'empCode': 'E500', 'nameEn': 'New Joiner', 'joinDate': TODAY(), 'designation': 'software engineer ii',
                                    'branchId': w.branch_id})
    assert c.status_code == 201
    new_id = c.json()['id']
    assert str(one('SELECT designation_id FROM employee WHERE id = %s', [new_id])['designation_id']) == s['designation']
    assert rows('SELECT kind, status FROM career_event WHERE employee_id = %s', [new_id]) == [{'kind': 'hire', 'status': 'applied'}]


def test_hr_edits_through_employee_form_are_in_history(world):
    a = apis(world)
    w = world
    p = a['hr'].patch(f'/employees/{w.peer_id}', {'phone': '9801111111', 'managerId': w.ram_id})
    assert p.status_code == 200
    h = a['hr'].get(f'/people/{w.peer_id}/history').json()['history']
    assert {'Phone', 'Manager'} <= {x['field'] for x in h}
    assert next(x for x in h if x['field'] == 'Manager')['new_value'] == 'Ram Bahadur'
    assert a['ram'].get(f'/people/{w.peer_id}/history').status_code == 403
