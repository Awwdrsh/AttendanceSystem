"""Port of apps/api/test/people.test.ts at service level: structure, directory, org chart, profile, career, change requests."""
from datetime import datetime, timedelta, timezone

import pytest

from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.employees import service as emp
from hajiri.people import service as ppl
from hajiri.people.career import apply_due_career_events
from hajiri.workflow import services as wf

from .conftest import PDF, PNG, add_days, upload

TODAY = lambda: npt_today().isoformat()  # noqa: E731


def make_structure(w):
    dept = ppl.create_unit(w.hr, {'kind': 'department', 'code': 'ENG', 'nameEn': 'Engineering', 'headEmployeeId': w.ram_id})
    team = ppl.create_unit(w.hr, {'kind': 'team', 'code': 'ENG-WEB', 'nameEn': 'Web', 'parentId': dept['id']})
    des = ppl.create_designation(w.hr, {'code': 'SE2', 'nameEn': 'Software Engineer II', 'grade': 'G5', 'jobLevel': 5, 'orgUnitId': dept['id']})
    br = ppl.create_branch(w.hr, {'code': 'PKR', 'nameEn': 'Pokhara', 'address': 'Lakeside'})
    return {'dept': dept['id'], 'team': team['id'], 'designation': des['id'], 'branch2': br['id']}


def promote_and_transfer(w, s):
    ev = ppl.record_career(w.hr, w.sita_id, {'kind': 'promotion', 'effectiveDate': TODAY(),
                                             'details': {'designationId': s['designation'], 'reason': 'Great year'}, 'note': 'Annual review'})
    date3 = add_days(npt_today(), -3)
    ppl.record_career(w.hr, w.sita_id, {'kind': 'transfer', 'effectiveDate': date3, 'details': {'branchId': s['branch2'], 'orgUnitId': s['team']}})
    return ev, date3


# ---------------------------------------------------------------- org structure

def test_hr_builds_structure_employees_cannot(world):
    w = world
    s = make_structure(w)
    assert s['dept'] and s['team'] and s['designation'] and s['branch2']
    with pytest.raises(ForbiddenError):
        ppl.create_unit(w.sita, {'kind': 'team', 'code': 'X', 'nameEn': 'X'})
    with pytest.raises(ForbiddenError):
        ppl.create_unit(w.ram, {'kind': 'team', 'code': 'X', 'nameEn': 'X'})


def test_rejects_cycles_foreign_refs_and_deleting_used_units(world):
    w = world
    s = make_structure(w)
    with pytest.raises(BadRequestError):
        ppl.update_unit(w.hr, s['dept'], {'parentId': s['team']})
    with pytest.raises(BadRequestError, match='a unit cannot sit under itself'):
        ppl.update_unit(w.hr, s['dept'], {'parentId': s['dept']})
    with pytest.raises(BadRequestError, match='parent unit does not exist in your company'):
        ppl.create_unit(w.foreign_hr, {'kind': 'team', 'code': 'F1', 'nameEn': 'F', 'parentId': s['dept']})
    with pytest.raises(ConflictError, match='sub-units'):
        ppl.delete_unit(w.hr, s['dept'])
    with pytest.raises(ConflictError, match='people are \\(or were\\) assigned to this branch'):
        ppl.delete_branch(w.hr, w.branch_id)
    with pytest.raises(NotFoundError, match='unit not found'):
        ppl.update_unit(w.foreign_hr, s['dept'], {'nameEn': 'Hacked'})


def test_duplicate_codes_say_which_kind_clashed(world):
    w = world
    make_structure(w)
    with pytest.raises(ConflictError, match='another branch already uses this code'):
        ppl.create_branch(w.hr, {'code': 'PKR', 'nameEn': 'Again'})
    with pytest.raises(ConflictError, match='another department or team already uses this code'):
        ppl.create_unit(w.hr, {'kind': 'team', 'code': 'ENG', 'nameEn': 'Again'})
    with pytest.raises(ConflictError, match='another designation already uses this code'):
        ppl.create_designation(w.hr, {'code': 'SE2', 'nameEn': 'Again'})


def test_structure_lists_headcounts_to_company_only(world):
    w = world
    s = make_structure(w)
    st = ppl.structure(w.sita)
    unit = next(u for u in st['units'] if u['id'] == s['dept'])
    assert unit['name_en'] == 'Engineering' and isinstance(unit['head_name'], str) and unit['headcount'] == 0
    assert ppl.structure(w.foreign_hr)['units'] == []


def test_designation_rename_renames_holders_and_delete_needs_none(world):
    w = world
    s = make_structure(w)
    promote_and_transfer(w, s)
    ppl.update_designation(w.hr, s['designation'], {'nameEn': 'Senior Engineer'})
    assert one('SELECT designation FROM employee WHERE id = %s', [w.sita_id])['designation'] == 'Senior Engineer'
    with pytest.raises(ConflictError, match='mark it inactive instead'):
        ppl.delete_designation(w.hr, s['designation'])
    spare = ppl.create_designation(w.hr, {'code': 'SPARE', 'nameEn': 'Spare'})
    with pytest.raises(NotFoundError, match='designation not found'):
        ppl.delete_designation(w.foreign_hr, spare['id'])
    ppl.delete_designation(w.hr, spare['id'])


# ---------------------------------------------------------------- career events

def test_promotion_today_updates_designation_and_grade_and_records_history(world):
    w = world
    s = make_structure(w)
    ev = ppl.record_career(w.hr, w.sita_id, {'kind': 'promotion', 'effectiveDate': TODAY(),
                                             'details': {'designationId': s['designation'], 'reason': 'Great year'}, 'note': 'Annual review'})
    assert ev['status'] == 'applied'
    e = one('SELECT designation, designation_id, grade FROM employee WHERE id = %s', [w.sita_id])
    assert e['designation'] == 'Software Engineer II' and str(e['designation_id']) == s['designation'] and e['grade'] == 'G5'
    fields = [h['field'] for h in ppl.history(w.sita, w.sita_id)]
    assert 'Designation' in fields and 'Grade' in fields
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'people.career_event' AND user_id = %s", [w.sita.id])
    assert n['title'] == 'Promoted' and n['body'] == 'Your designation is now Software Engineer II, grade G5.'


def test_transfer_splits_assignment_at_its_date(world):
    w = world
    s = make_structure(w)
    d = add_days(npt_today(), -3)
    ppl.record_career(w.hr, w.sita_id, {'kind': 'transfer', 'effectiveDate': d, 'details': {'branchId': s['branch2'], 'orgUnitId': s['team']}})
    rs = rows("""SELECT lower(valid)::text AS lo, upper(valid)::text AS hi, branch_id::text, org_unit_id::text, manager_id::text
                   FROM employee_assignment WHERE employee_id = %s ORDER BY lower(valid)""", [w.sita_id])
    assert rs[-1] == {'lo': d, 'hi': None, 'branch_id': s['branch2'], 'org_unit_id': s['team'], 'manager_id': w.ram_id}
    assert rs[-2]['hi'] == d


def test_future_event_is_scheduled_then_applied_by_daily_job(world):
    w = world
    make_structure(w)
    ppl.update_designation  # noqa: B018
    ev = ppl.record_career(w.hr, w.sita_id, {'kind': 'grade_change', 'effectiveDate': add_days(npt_today(), 5), 'details': {'grade': 'G6'}})
    assert ev['status'] == 'scheduled'
    assert one('SELECT grade FROM employee WHERE id = %s', [w.sita_id])['grade'] is None
    now = datetime.now(timezone.utc)
    assert apply_due_career_events(now) == 0
    assert apply_due_career_events(now + timedelta(days=6)) == 1
    assert one('SELECT grade FROM employee WHERE id = %s', [w.sita_id])['grade'] == 'G6'


def test_scheduled_separation_sets_exit_date_can_be_cancelled_and_validates(world):
    w = world
    lwd = add_days(npt_today(), 20)
    with pytest.raises(BadRequestError, match='choose the type of separation'):
        ppl.record_career(w.hr, w.peer_id, {'kind': 'separation', 'effectiveDate': lwd, 'details': {}})
    ev = ppl.record_career(w.hr, w.peer_id, {'kind': 'separation', 'effectiveDate': lwd,
                                             'details': {'separationType': 'resignation', 'reason': 'Studies abroad'}})
    assert ev['status'] == 'scheduled'
    e = one('SELECT exit_date, status FROM employee WHERE id = %s', [w.peer_id])
    assert e['exit_date'].isoformat() == lwd and e['status'] == 'active'
    with pytest.raises(ConflictError, match='a separation is already scheduled'):
        ppl.record_career(w.hr, w.peer_id, {'kind': 'separation', 'effectiveDate': lwd, 'details': {'separationType': 'resignation'}})
    ppl.cancel_career(w.hr, w.peer_id, ev['id'])
    assert one('SELECT exit_date FROM employee WHERE id = %s', [w.peer_id])['exit_date'] is None
    with pytest.raises(BadRequestError, match='an employee cannot be their own manager'):
        ppl.record_career(w.hr, w.peer_id, {'kind': 'manager_change', 'effectiveDate': TODAY(), 'details': {'managerId': w.peer_id}})
    with pytest.raises(BadRequestError, match='the effective date is before the employee joined'):
        ppl.record_career(w.hr, w.peer_id, {'kind': 'grade_change', 'effectiveDate': '2020-01-01', 'details': {'grade': 'X'}})
    with pytest.raises(ConflictError, match='only a scheduled event can be cancelled'):
        ppl.cancel_career(w.hr, w.peer_id, ev['id'])


def test_timeline_self_and_manager_read_peers_cannot_only_hr_records(world):
    w = world
    s = make_structure(w)
    promote_and_transfer(w, s)
    own = ppl.career(w.sita, w.sita_id)
    promo = next(e for e in own if e['kind'] == 'promotion')
    assert promo['label'] == 'Promoted' and 'Software Engineer II' in promo['summary'] and promo['reason'] == 'Great year'
    mgr = ppl.career(w.ram, w.sita_id)
    assert next(e for e in mgr if e['kind'] == 'promotion')['reason'] is None
    with pytest.raises(ForbiddenError, match='not allowed to see this career history'):
        ppl.career(w.peer, w.sita_id)
    with pytest.raises(ForbiddenError):
        ppl.record_career(w.ram, w.sita_id, {'kind': 'note', 'effectiveDate': TODAY()})
    with pytest.raises(NotFoundError):
        ppl.career(w.foreign_hr, w.sita_id)


def test_managers_do_not_see_salary_revisions_or_separation_types(world):
    w = world
    ppl.record_career(w.hr, w.sita_id, {'kind': 'salary_revision', 'effectiveDate': TODAY(), 'details': {'revisionPercent': 10}})
    full = ppl.career(w.hr, w.sita_id)
    assert next(e for e in full if e['kind'] == 'salary_revision')['summary'] == '+10%'
    assert all(e['kind'] != 'salary_revision' for e in ppl.career(w.ram, w.sita_id))


# ---------------------------------------------------------------- profile sections

def test_hr_edits_sections_ids_encrypted_and_masked_unless_revealed(world):
    w = world
    put = ppl.save_section(w.hr, w.sita_id, 'ids', {'citizenshipNo': '27-01-75-12345', 'citizenshipDistrict': 'Kathmandu', 'passportNo': 'PA1234567'})
    assert put['pending'] is False
    assert next(c for c in put['changes'] if c['label'] == 'Citizenship number')['to'] == '•••• 2345'
    raw = bytes(one('SELECT citizenship_no_enc FROM employee WHERE id = %s', [w.sita_id])['citizenship_no_enc'])
    assert b'12345' not in raw
    assert ppl.profile(w.sita, w.sita_id)['profile']['ids']['citizenshipNo'] == '•••• 2345'
    assert ppl.profile(w.hr, w.sita_id, reveal=True)['profile']['ids']['passportNo'] == 'PA1234567'
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'read_sensitive' AND table_name = 'employee_profile' AND row_id = %s", [w.sita_id])) == 1


def test_validates_list_sections_nominee_shares_total_100(world):
    w = world
    with pytest.raises(ValidationError) as e:
        ppl.save_section(w.hr, w.sita_id, 'family', {'members': [
            {'name': 'Hari', 'relation': 'father', 'isNominee': True, 'nomineeShare': 60},
            {'name': 'Gita', 'relation': 'mother', 'isNominee': True, 'nomineeShare': 30}]})
    assert e.value.issues[0]['message'] == 'nominee shares must add up to 100%'
    ok = ppl.save_section(w.hr, w.sita_id, 'family', {'members': [
        {'name': 'Hari', 'relation': 'father', 'isNominee': True, 'nomineeShare': 60},
        {'name': 'Gita', 'relation': 'mother', 'isNominee': True, 'nomineeShare': 40}]})
    assert ok['pending'] is False and ok['changes'][0]['to'] == 'Hari (father, nominee 60%); Gita (mother, nominee 40%)'
    # Saving the same list again changes nothing.
    assert ppl.save_section(w.hr, w.sita_id, 'family', {'members': [
        {'name': 'Hari', 'relation': 'father', 'isNominee': True, 'nomineeShare': 60},
        {'name': 'Gita', 'relation': 'mother', 'isNominee': True, 'nomineeShare': 40}]})['changes'] == []
    with pytest.raises(NotFoundError, match='unknown profile section salary'):
        ppl.save_section(w.hr, w.sita_id, 'salary', {})
    with pytest.raises(BadRequestError):
        ppl.save_section(w.hr, w.sita_id, 'personal', {'status': 'exited'})


def test_only_employee_and_hr_read_a_profile(world):
    w = world
    with pytest.raises(ForbiddenError, match='only the employee and HR can see this'):
        ppl.profile(w.ram, w.sita_id)
    with pytest.raises(ForbiddenError):
        ppl.profile(w.peer, w.sita_id)
    with pytest.raises(NotFoundError):
        ppl.profile(w.foreign_hr, w.sita_id)
    with pytest.raises(ForbiddenError):
        ppl.save_section(w.sita, w.sita_id, 'personal', {'bloodGroup': 'O+'})


# ---------------------------------------------------------------- profile change requests

def _item(user, object_id):
    return next((i for i in wf.inbox_for(user) if i['objectId'] == str(object_id)), None)


def test_change_request_with_proof_approved_from_inbox_and_applied(world):
    w = world
    proof = upload(w.sita, 'profile_change', PDF, 'citizenship.pdf')
    r = ppl.request_change(w.sita, {'section': 'contact', 'value': {'personalPhone': '+977 9800000001',
                                    'permanentAddress': {'district': 'Lalitpur', 'municipality': 'Lalitpur Metropolitan', 'ward': 5}},
                                    'reason': 'Moved', 'proofFileId': proof})
    req_id = r['id']
    assert r['sectionLabel'] == 'Contact and address'
    with pytest.raises(ConflictError, match='a change to contact and address is already waiting for approval'):
        ppl.request_change(w.sita, {'section': 'contact', 'value': {'personalPhone': '+977 9800000002'}})
    item = _item(w.hr, req_id)
    assert item['summary']['sectionLabel'] == 'Contact and address' and item['summary']['hasProof'] is True
    labels = [c['label'] for c in item['summary']['changes']]
    assert 'Personal phone' in labels and 'Permanent address' in labels
    # The employee's manager is not an approver of profile changes.
    assert _item(w.ram, req_id) is None
    assert ppl.request_proof(w.hr, req_id)['mime'] == 'application/pdf'
    with pytest.raises(NotFoundError):
        ppl.request_proof(w.peer, req_id)
    assert wf.decide(w.hr, item['instanceId'], 'approve') == 'approved'
    contact = ppl.profile(w.sita, w.sita_id)['profile']['contact']
    assert contact['personalPhone'] == '+977 9800000001'
    assert contact['permanentAddress']['district'] == 'Lalitpur' and contact['permanentAddress']['ward'] == 5
    assert next(x for x in ppl.my_requests(w.sita) if x['id'] == req_id)['status'] == 'approved'
    hist = ppl.history(w.hr, w.sita_id)
    assert any(h['field'] == 'Personal phone' and h['source'] == 'change_request' for h in hist)
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'profile_change.approved' AND user_id = %s", [w.sita.id])
    assert n['title'] == 'Profile change approved' and n['body'] == 'Contact and address updated.'
    # The proof is audited when HR opened it.
    assert rows("SELECT 1 FROM audit_log WHERE action = 'view_file' AND row_id = %s", [req_id])


def test_change_nothing_refused_and_employee_cancels_pending(world):
    w = world
    ppl.save_section(w.hr, w.sita_id, 'contact', {'personalPhone': '+977 9800000001'})
    with pytest.raises(ConflictError, match='nothing would change'):
        ppl.request_change(w.sita, {'section': 'contact', 'value': {'personalPhone': '+977 9800000001'}})
    r = ppl.request_change(w.sita, {'section': 'skills', 'value': {'skills': ['TypeScript'], 'languages': ['English', 'Nepali']}})
    with pytest.raises(NotFoundError):
        ppl.cancel_my_request(w.peer, r['id'])
    ppl.cancel_my_request(w.sita, r['id'])
    assert one('SELECT state FROM workflow_instance WHERE object_id = %s', [r['id']])['state'] == 'cancelled'
    with pytest.raises(ConflictError, match='this request is already cancelled'):
        ppl.cancel_my_request(w.sita, r['id'])


def test_rejected_bank_change_not_applied_and_employee_told_why(world):
    w = world
    r = ppl.request_change(w.sita, {'section': 'bank', 'value': {'bankCode': 'NABIL', 'bankAccount': '0123456789012'}})
    import json
    assert '0123456789012' not in json.dumps(r['summary'])
    stored = bytes(one('SELECT payload_enc FROM profile_change_request WHERE id = %s', [r['id']])['payload_enc'])
    assert b'0123456789012' not in stored
    inst = one('SELECT id FROM workflow_instance WHERE object_id = %s', [r['id']])['id']
    assert wf.decide(w.hr, inst, 'reject', reason='Account name does not match') == 'rejected'
    assert one('SELECT bank_code FROM employee WHERE id = %s', [w.sita_id])['bank_code'] != 'NABIL'
    n = one("SELECT body FROM notification_outbox WHERE template = 'profile_change.rejected' ORDER BY created_at DESC LIMIT 1")
    assert 'Account name does not match' in n['body']


def test_request_detail_reveals_numbers_for_hr_only(world):
    w = world
    r = ppl.request_change(w.sita, {'section': 'bank', 'value': {'bankAccount': '0123456789012'}})
    masked = ppl.request_detail(w.hr, r['id'])
    assert masked['changes'][0]['to'] == '•••• 9012'
    full = ppl.request_detail(w.hr, r['id'], reveal=True)
    assert full['changes'][0]['to'] == '0123456789012'
    assert 'payload_enc' not in full['request']
    assert ppl.request_detail(w.sita, r['id'], reveal=True)['changes'][0]['to'] == '•••• 9012'
    with pytest.raises(NotFoundError):
        ppl.request_detail(w.peer, r['id'])


def test_maker_checker_hr_bank_edits_wait_for_a_different_hr(world):
    w = world
    assert ppl.set_policy(w.hr, {'makerChecker': True}) == {'makerChecker': True}
    edit = ppl.save_section(w.hr, w.ram_id, 'bank', {'bankCode': 'NIC', 'bankAccount': '9988776655'})
    assert edit['pending'] is True
    with pytest.raises(ConflictError, match='PAN and bank account changes need a second HR approval'):
        emp.update_employee(w.hr, w.ram_id, {'bankAccount': '1111222233'})
    inst = one('SELECT id FROM workflow_instance WHERE object_id = %s', [edit['request']['id']])['id']
    with pytest.raises(ForbiddenError, match='a different HR person must approve this change'):
        wf.decide(w.hr, inst, 'approve')
    assert one('SELECT bank_code FROM employee WHERE id = %s', [w.ram_id])['bank_code'] != 'NIC'
    assert wf.decide(w.hr2, inst, 'approve') == 'approved'
    e = one('SELECT bank_code, bank_account_last4 FROM employee WHERE id = %s', [w.ram_id])
    assert e == {'bank_code': 'NIC', 'bank_account_last4': '6655'}
    # Non-sensitive sections still apply at once.
    assert ppl.save_section(w.hr, w.ram_id, 'personal', {'bloodGroup': 'B+'})['pending'] is False
    ppl.set_policy(w.hr, {'makerChecker': False})
    assert 'update' in [r['action'] for r in rows("SELECT action FROM audit_log WHERE table_name = 'company_setting'")]


# ---------------------------------------------------------------- directory, org chart, avatar and summary

def test_directory_without_personal_data_search_and_filters(world):
    w = world
    s = make_structure(w)
    promote_and_transfer(w, s)
    ppl.save_section(w.hr, w.sita_id, 'contact', {'personalPhone': '+977 9800000001'})
    d = ppl.directory(w.peer)
    p = next(x for x in d if x['id'] == w.sita_id)
    assert p['designation'] == 'Software Engineer II' and p['unit_name'] == 'Web' and isinstance(p['manager_name'], str)
    assert '9800000001' not in repr(d)
    assert ppl.directory(w.peer, {'search': 'software%'}) == []  # % is literal
    assert [x['id'] for x in ppl.directory(w.peer, {'orgUnitId': s['team']})] == [w.sita_id]
    assert ppl.directory(w.foreign_hr) == []


def test_org_chart_links_active_managers_and_card_lists_reports(world):
    w = world
    nodes = ppl.org_chart(w.sita)
    assert next(n for n in nodes if n['id'] == w.sita_id)['manager_id'] == w.ram_id
    c = ppl.card(w.sita, w.ram_id)
    assert w.sita_id in [r['id'] for r in c['reports']]
    with pytest.raises(NotFoundError, match='person not found'):
        ppl.card(w.foreign_hr, w.ram_id)
    tree = ppl.build_tree(nodes)
    assert w.ram_id in [r['id'] for r in tree['roots']] and tree['size'][w.ram_id] == 1


def test_build_tree_breaks_loops():
    a = {'id': 'a', 'name_en': 'A', 'manager_id': 'b'}
    b = {'id': 'b', 'name_en': 'B', 'manager_id': 'a'}
    tree = ppl.build_tree([a, b])
    assert len(tree['roots']) >= 1 and sum(tree['size'].values()) <= 2


def test_employee_sets_own_photo_colleagues_see_it_images_only(world):
    w = world
    file_id = upload(w.sita, 'avatar', PNG, 'me.png')
    ppl.set_my_avatar(w.sita, {'fileId': file_id})
    img = ppl.avatar_file(w.peer, w.sita_id)
    assert img['mime'] == 'image/png'
    with pytest.raises(NotFoundError):
        ppl.avatar_file(w.foreign_hr, w.sita_id)
    with pytest.raises(BadRequestError):
        upload(w.sita, 'avatar', PDF, 'x.pdf')
    # Someone else's upload cannot be attached as your photo.
    theirs = upload(w.peer, 'avatar', PNG, 'p.png')
    with pytest.raises(ForbiddenError):
        ppl.set_my_avatar(w.sita, {'fileId': theirs})
    replaced = upload(w.sita, 'avatar', PNG, 'me2.png')
    ppl.set_my_avatar(w.sita, {'fileId': replaced})
    assert one('SELECT deleted_at FROM stored_file WHERE id = %s', [file_id])['deleted_at'] is not None
    ppl.set_my_avatar(w.sita, None)
    with pytest.raises(NotFoundError, match='no profile photo'):
        ppl.avatar_file(w.peer, w.sita_id)
    with pytest.raises(ForbiddenError, match='only employees have a profile photo'):
        ppl.set_my_avatar(w.hr, None)


def test_summary_counts_the_company(world):
    w = world
    s = make_structure(w)
    promote_and_transfer(w, s)
    sm = ppl.summary(w.sita)
    assert sm['active'] >= 3
    assert next(u for u in sm['byUnit'] if u['id'] == s['team'])['count'] == 1


# ---------------------------------------------------------------- employees module integration

def test_creating_employee_records_hire_and_links_designation(world):
    w = world
    s = make_structure(w)
    new_id = emp.create_employee(w.hr, {'empCode': 'E500', 'nameEn': 'New Joiner', 'joinDate': TODAY(),
                                        'designation': 'software engineer ii', 'branchId': w.branch_id})
    assert str(one('SELECT designation_id FROM employee WHERE id = %s', [new_id])['designation_id']) == s['designation']
    assert rows('SELECT kind, status FROM career_event WHERE employee_id = %s', [new_id]) == [{'kind': 'hire', 'status': 'applied'}]


def test_hr_edits_through_employee_form_go_to_field_history(world):
    w = world
    emp.update_employee(w.hr, w.peer_id, {'phone': '9801111111', 'managerId': w.ram_id})
    h = ppl.history(w.hr, w.peer_id)
    fields = [x['field'] for x in h]
    assert 'Phone' in fields and 'Manager' in fields
    assert next(x for x in h if x['field'] == 'Manager')['new_value'] == one('SELECT name_en FROM employee WHERE id = %s', [w.ram_id])['name_en']
    with pytest.raises(ForbiddenError):
        ppl.history(w.ram, w.peer_id)
