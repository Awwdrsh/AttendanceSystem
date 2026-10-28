"""Port of apps/api/test/relations.test.ts against the service layer (one function per TS route).

The TS file runs its cases in order against one database; here each test builds the state it needs, so the steps of
one TS `describe` that depend on each other run inside one test."""
from datetime import date, datetime, timedelta, timezone

import pytest

from hajiri.core.clock import npt_today
from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from hajiri.relations import services as S
from tests.web.factories import make_user

from .conftest import PDF_DATA_URL


def status_of(fn, *a, **kw) -> int:
    try:
        fn(*a, **kw)
    except (BadRequestError, ForbiddenError, NotFoundError, ConflictError) as e:
        return e.status
    return 200


def file_one(w, **kw):
    body = {'category': 'management', 'subject': 'Unfair shift allocation', 'description': 'My manager keeps giving me night shifts',
            'concerns': 'Ram', **kw}
    return S.file_grievance(w.sita, body)


def with_committee(w):
    S.set_committee(w.hr, {'userIds': [w.member.id]})


# ================================================================ grievances

def test_hr_names_the_committee_only_hr_can_change_it(world):
    w = world
    assert status_of(S.set_committee, w.sita, {'userIds': [w.member.id]}) == 403
    assert S.set_committee(w.hr, {'userIds': [w.member.id]}) == {'ok': True, 'members': 1}
    assert S.committee(w.member)['isMember'] is True
    assert S.committee(w.outsider)['members'] == []
    # Every member must be an active user of the company.
    with pytest.raises(BadRequestError, match='every member must be an active user in your company'):
        S.set_committee(w.hr, {'userIds': [w.other_hr.id]})


def test_an_employee_files_a_confidential_grievance_text_encrypted_notice_reveals_nothing(world):
    from hajiri.files.service import upload
    w = world
    with_committee(w)
    f = upload(w.sita, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    r = file_one(w, fileIds=[f['id']])
    assert r['number'] == 1
    raw = one('SELECT subject_enc, description_enc FROM grievance WHERE id = %s', [r['id']])
    assert 'night shifts' not in bytes(raw['description_enc']).decode('latin1')
    n = rows("SELECT user_id, title, body FROM notification_outbox WHERE template = 'relations.grievance.new'")
    assert w.member.id in [str(x['user_id']) for x in n]
    assert all('night' not in x['body'] and 'Sita' not in x['title'] for x in n)
    # The attachment now belongs to the grievance.
    assert one('SELECT attached_to FROM stored_file WHERE id = %s', [f['id']])['attached_to'] == f"grievance:{r['id']}"
    # A second grievance takes the next number.
    assert S.file_grievance(w.sita, {'category': 'other', 'subject': 's', 'description': 'd'})['number'] == 2


def test_manager_others_and_other_companies_never_see_it_committee_and_hr_do_audited(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    assert status_of(S.grievance_detail, w.ram, gid) == 404
    assert status_of(S.list_grievances, w.ram, {'scope': 'handle'}) == 403
    assert status_of(S.grievance_detail, w.outsider, gid) == 404
    assert status_of(S.grievance_detail, w.other_hr, gid) == 404
    m = S.grievance_detail(w.member, gid)
    assert 'night shifts' in m['grievance']['description']
    assert m['grievance']['role'] == 'committee'
    lst = S.list_grievances(w.member, {'scope': 'handle'})
    assert lst['grievances'][0]['description'] is None
    assert rows("SELECT 1 FROM audit_log WHERE table_name = 'grievance' AND action = 'view' AND row_id = %s", [gid])
    assert S.list_grievances(w.sita)['grievances'][0]['subject'] == 'Unfair shift allocation'
    # The complainant's own look is not audited as a view.
    before = len(rows("SELECT 1 FROM audit_log WHERE action = 'view' AND row_id = %s", [gid]))
    S.grievance_detail(w.sita, gid)
    assert len(rows("SELECT 1 FROM audit_log WHERE action = 'view' AND row_id = %s", [gid])) == before
    # HR sees it with the hr role; the complainant never sees who was excluded.
    assert S.grievance_detail(w.hr, gid)['grievance']['role'] == 'hr'
    assert S.grievance_detail(w.sita, gid)['grievance']['excludedUserIds'] == []


def test_attachments_are_served_only_to_people_who_can_see_the_case(world):
    from hajiri.files.service import upload
    w = world
    with_committee(w)
    f = upload(w.sita, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    gid = file_one(w, fileIds=[f['id']])['id']
    files = S.grievance_detail(w.sita, gid)['files']
    got = S.grievance_file(w.member, gid, files[0]['id'])
    assert got['data'].startswith(b'%PDF')
    assert status_of(S.grievance_file, w.ram, gid, files[0]['id']) == 404
    assert 'view_file' in [r['action'] for r in rows('SELECT action FROM audit_log WHERE row_id = %s', [gid])]


def test_internal_notes_stay_with_the_committee_shared_notes_reach_the_employee(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    assert 'id' in S.add_note(w.member, gid, {'body': 'Check the roster history'})
    assert 'id' in S.add_note(w.member, gid, {'body': 'We will meet you on Sunday', 'shared': True})
    seen = [n['body'] for n in S.grievance_detail(w.sita, gid)['notes']]
    assert seen == ['We will meet you on Sunday']
    assert 'id' in S.add_note(w.sita, gid, {'body': 'Thank you'})
    # The complainant's message is always shared and reaches the handlers; the committee sees every note.
    assert len(S.grievance_detail(w.member, gid)['notes']) == 3
    assert rows("SELECT 1 FROM notification_outbox WHERE template = 'relations.grievance.note' AND user_id = %s", [w.member.id])
    assert rows("SELECT 1 FROM notification_outbox WHERE template = 'relations.grievance.note' AND user_id = %s", [w.sita.id])


def test_hr_can_exclude_a_committee_member_with_a_conflict_of_interest(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    assert status_of(S.update_grievance, w.member, gid, {'excludeUserIds': [w.hr.id]}) == 403
    assert S.update_grievance(w.hr, gid, {'excludeUserIds': [w.member.id]}) == {'ok': True}
    assert status_of(S.grievance_detail, w.member, gid) == 404
    # Exclusions only grow: HR cannot lift one (an administrator can).
    assert status_of(S.update_grievance, w.hr, gid, {'excludeUserIds': []}) == 403
    admin = make_user(w.co, roles=['admin'])
    assert S.update_grievance(admin, gid, {'excludeUserIds': []}) == {'ok': True}
    assert S.grievance_detail(w.member, gid)['grievance']['role'] == 'committee'
    # Nobody excludes themselves.
    with pytest.raises(BadRequestError, match='you cannot exclude yourself; ask another HR person'):
        S.update_grievance(w.hr, gid, {'excludeUserIds': [w.hr.id]})


def test_nobody_can_make_themselves_the_only_handler_of_a_grievance(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    handlers = [str(r['id']) for r in rows(
        """SELECT id FROM app_user WHERE company_id = %s AND id <> %s AND id <> %s
              AND ('hr' = ANY(roles) OR 'admin' = ANY(roles) OR id IN (SELECT user_id FROM grievance_committee_member))""",
        [w.company_id, w.hr.id, w.sita.id])]
    assert handlers
    with pytest.raises(BadRequestError, match='at least one other HR or committee member must be able to handle this case'):
        S.update_grievance(w.hr, gid, {'excludeUserIds': handlers})


def test_hearing_decision_appeal_once_and_hr_not_the_decider_hears_it(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    assert status_of(S.update_grievance, w.sita, gid, {'status': 'under_review'}) == 403
    assert S.update_grievance(w.member, gid, {'hearingAt': '2026-11-01T10:00:00+05:45', 'hearingPlace': 'Board room'}) == {'ok': True}
    g = S.grievance_detail(w.sita, gid)['grievance']
    assert g['status'] == 'hearing_scheduled' and g['hearingPlace'] == 'Board room'
    n = one("SELECT body, title FROM notification_outbox WHERE template = 'relations.grievance.hearing' AND channel = 'in_app'")
    assert n['body'] == 'The committee will hear your grievance on 15 Kartik 2083 at Board room.'
    assert one("SELECT 1 AS x FROM notification_outbox WHERE template = 'relations.grievance.hearing' AND channel = 'push'")
    assert status_of(S.decide_grievance, w.sita, gid, {'outcome': 'upheld', 'findings': 'x'}) == 403
    assert S.decide_grievance(w.hr, gid, {'outcome': 'partly_upheld', 'findings': 'Shifts will rotate fairly from next month'}) == {'ok': True}
    assert status_of(S.appeal_grievance, w.member, gid, {'reason': 'x'}) == 403
    assert S.grievance_detail(w.sita, gid)['canAppeal'] is True
    assert S.appeal_grievance(w.sita, gid, {'reason': 'Rotation is not enough'}) == {'ok': True}
    assert status_of(S.appeal_grievance, w.sita, gid, {'reason': 'again'}) == 409
    assert status_of(S.decide_appeal, w.hr, gid, {'outcome': 'decision_upheld', 'note': 'n'}) == 403
    assert status_of(S.decide_appeal, w.member, gid, {'outcome': 'decision_upheld', 'note': 'n'}) == 403
    with pytest.raises(BadRequestError, match='choose the new outcome'):
        S.decide_appeal(w.hr2, gid, {'outcome': 'decision_changed', 'note': 'x'})
    assert S.decide_appeal(w.hr2, gid, {'outcome': 'decision_changed', 'newOutcome': 'upheld', 'note': 'Upheld in full'}) == {'ok': True}
    g = S.grievance_detail(w.sita, gid)['grievance']
    assert {k: g[k] for k in ('status', 'outcome', 'appealOutcome', 'appealNote')} == {
        'status': 'closed', 'outcome': 'upheld', 'appealOutcome': 'decision_changed', 'appealNote': 'Upheld in full'}
    assert [r['action'] for r in rows("SELECT action FROM audit_log WHERE table_name = 'grievance' AND row_id = %s AND action <> 'view' ORDER BY id",
                                      [gid])] == ['create', 'update', 'decide', 'appeal', 'appeal_decide']


def test_review_status_and_hearing_needs_a_time(world):
    w = world
    gid = file_one(w)['id']
    with pytest.raises(BadRequestError, match='set the hearing date and time'):
        S.update_grievance(w.hr, gid, {'status': 'hearing_scheduled'})
    S.update_grievance(w.hr, gid, {'status': 'under_review'})
    assert S.grievance_detail(w.sita, gid)['grievance']['status'] == 'under_review'
    assert one("SELECT title FROM notification_outbox WHERE template = 'relations.grievance.review'")['title'] == \
        'Your grievance G-1 is under review'
    with pytest.raises(ValidationError):
        S.update_grievance(w.hr, gid, {'note': 'not a field'})


def test_withdrawal_before_a_decision_and_the_daily_job_closes_after_the_appeal_window(world):
    w = world
    with_committee(w)
    a = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No parking space'})['id']
    assert S.withdraw_grievance(w.sita, a) == {'ok': True}
    assert status_of(S.withdraw_grievance, w.sita, a) == 409
    b = S.file_grievance(w.sita, {'category': 'safety', 'subject': 'Wet floor', 'description': 'Kitchen floor is always wet'})['id']
    S.decide_grievance(w.member, b, {'outcome': 'upheld', 'findings': 'Mats ordered'})
    out = S.relations_daily(datetime.now(timezone.utc) + timedelta(days=20))
    assert out['grievancesClosed'] >= 1
    assert S.grievance_detail(w.sita, b)['grievance']['status'] == 'closed'


def test_committee_cannot_close_and_appeal_window_passes(world):
    w = world
    with_committee(w)
    gid = file_one(w)['id']
    S.decide_grievance(w.member, gid, {'outcome': 'not_upheld', 'findings': 'No evidence'})
    assert status_of(S.close_grievance, w.member, gid) == 403
    execute("UPDATE grievance SET decided_at = now() - interval '16 days' WHERE id = %s", [gid])
    with pytest.raises(ConflictError, match='the 15-day appeal window has passed'):
        S.appeal_grievance(w.sita, gid, {'reason': 'late'})
    assert S.close_grievance(w.sita, gid) == {'ok': True}
    with pytest.raises(ConflictError, match='this grievance is closed'):
        S.add_note(w.sita, gid, {'body': 'hello'})


def test_grievances_validate_input(world):
    w = world
    with pytest.raises(ValidationError):
        S.file_grievance(w.sita, {'category': 'gossip', 'subject': 's', 'description': 'd'})
    with pytest.raises(BadRequestError, match='Your account is not linked to an employee record.'):
        S.file_grievance(w.hr, {'category': 'other', 'subject': 's', 'description': 'd'})


def test_people_the_complainant_names_never_get_the_arrival_notice(world):
    w = world
    with_committee(w)
    S.file_grievance(w.sita, {'category': 'management', 'subject': 's', 'description': 'd', 'excludeUserIds': [w.member.id]})
    got = {str(r['user_id']) for r in rows("SELECT user_id FROM notification_outbox WHERE template = 'relations.grievance.new'")}
    assert w.member.id not in got and w.hr.id in got and w.sita.id not in got


# ================================================================ disciplinary action

def stage_by(w, effect):
    return next(s for s in S.list_stages(w.hr)['stages'] if s['effect'] == effect)


def open_sita_case(w):
    return S.open_case(w.hr, {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01',
                              'incident': 'Absent without notice for three days'})


def test_default_stages_are_seeded_hr_edits_them(world):
    w = world
    stages = S.list_stages(w.hr)['stages']
    assert [s['code'] for s in stages] == ['verbal_warning', 'written_warning', 'final_warning', 'suspension', 'termination']
    assert status_of(S.save_stages, w.sita, {'stages': []}) == 403
    edited = [{'id': s['id'], 'code': s['code'], 'name': s['name'], 'severity': s['severity'], 'effect': s['effect'],
               'letterTemplateCode': s['letter_template_code'], 'active': s['code'] != 'verbal_warning'} for s in stages]
    edited.append({'code': 'counselling', 'name': 'Counselling', 'severity': 1, 'effect': 'none'})
    assert S.save_stages(w.hr, {'stages': edited}) == {'ok': True}
    after = S.list_stages(w.hr)['stages']
    assert [s['code'] for s in after][-1] == 'counselling'
    assert next(s for s in after if s['code'] == 'verbal_warning')['active'] is False


def test_hr_opens_a_case_the_employee_does_not_see_it_until_asked(world):
    w = world
    assert status_of(S.open_case, w.ram, {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01',
                                          'incident': 'x'}) == 403
    r = open_sita_case(w)
    assert r['number'] == 1
    cid = r['id']
    assert S.my_cases(w.sita)['cases'] == []
    assert status_of(S.case_detail, w.sita, cid) == 404
    assert status_of(S.case_detail, w.ram, cid) == 404
    assert status_of(S.case_detail, w.other_hr, cid) == 404
    # HR's look is audited.
    S.case_detail(w.hr, cid)
    assert 'view' in [r['action'] for r in rows('SELECT action FROM audit_log WHERE row_id = %s', [cid])]


def test_open_case_rules(world):
    w = world
    future = (npt_today() + timedelta(days=2)).isoformat()
    with pytest.raises(BadRequestError, match='the incident date is in the future'):
        S.open_case(w.hr, {'employeeId': str(w.sita_e.id), 'category': 'conduct', 'incidentDate': future, 'incident': 'x'})
    hr_e = w.new_employee('HR9')
    hr_self = make_user(w.co, roles=['hr'], employee=hr_e)
    with pytest.raises(ForbiddenError, match='you cannot open a case about yourself'):
        S.open_case(hr_self, {'employeeId': str(hr_e.id), 'category': 'conduct', 'incidentDate': '2026-10-01', 'incident': 'x'})
    with pytest.raises(NotFoundError, match='employee not found'):
        S.open_case(w.other_hr, {'employeeId': str(w.sita_e.id), 'category': 'conduct', 'incidentDate': '2026-10-01', 'incident': 'x'})


def test_show_cause_response_and_no_early_decision_without_confirmation(world):
    w = world
    cid = open_sita_case(w)['id']
    verbal = S.list_stages(w.hr)['stages'][0]
    assert status_of(S.show_cause, w.hr, cid, {'text': 'Explain the absence', 'responseDue': '2020-01-01'}) == 400
    assert S.show_cause(w.hr, cid, {'text': 'Explain your absence on 1 to 3 Kartik', 'responseDue': '2099-01-01'}) == {'ok': True}
    assert status_of(S.show_cause, w.hr, cid, {'text': 'again', 'responseDue': '2099-01-01'}) == 409
    assert status_of(S.decide_case, w.hr, cid, {'stageId': verbal['id'], 'note': 'n'}) == 409
    mine = S.my_cases(w.sita)['cases']
    assert mine[0]['status'] == 'show_cause' and mine[0]['showCause'] == 'Explain your absence on 1 to 3 Kartik'
    assert mine[0]['stageLetter'] is None and mine[0]['exitId'] is None
    assert status_of(S.respond_case, w.ram, cid, {'response': 'x'}) == 404
    assert S.respond_case(w.sita, cid, {'response': 'My mother was in hospital'}) == {'ok': True}
    assert status_of(S.respond_case, w.sita, cid, {'response': 'again'}) == 409
    # HR got the response notice; the employee got a push for the notice.
    assert one("SELECT 1 AS x FROM notification_outbox WHERE template = 'relations.disciplinary.responded' AND user_id = %s", [w.hr.id])
    assert one("SELECT 1 AS x FROM notification_outbox WHERE template = 'relations.disciplinary.show_cause' AND channel = 'push'")


def test_decide_early_with_force(world):
    w = world
    cid = open_sita_case(w)['id']
    S.show_cause(w.hr, cid, {'text': 'Explain', 'responseDue': '2099-01-01'})
    with pytest.raises(ConflictError, match='the employee has until .* to respond; decide after that, or confirm you want to decide now'):
        S.decide_case(w.hr, cid, {'stageId': None, 'note': 'n'})
    assert S.decide_case(w.hr, cid, {'stageId': None, 'note': 'Admitted in the hearing', 'force': True}) == {'ok': True}
    k = S.case_detail(w.sita, cid)['case']
    assert k['status'] == 'decided' and k['stageName'] is None
    assert one("SELECT body FROM notification_outbox WHERE template = 'relations.disciplinary.decided' AND channel = 'in_app'")['body'] == \
        'HR closed the case with no action. See Profile, Conduct.'


def test_suspension_with_dates_on_the_career_timeline_and_a_warning_letter_linked(world):
    from hajiri.documents import services as docs
    from hajiri.people import service as people
    w = world
    cid = open_sita_case(w)['id']
    S.show_cause(w.hr, cid, {'text': 'Explain', 'responseDue': '2099-01-01'})
    S.respond_case(w.sita, cid, {'response': 'Sorry'})
    susp = stage_by(w, 'suspension')
    assert status_of(S.decide_case, w.hr, cid, {'stageId': susp['id'], 'note': 'Three days'}) == 400
    with pytest.raises(BadRequestError, match='the suspension ends before it starts'):
        S.decide_case(w.hr, cid, {'stageId': susp['id'], 'note': 'x', 'suspensionFrom': '2026-10-22', 'suspensionTo': '2026-10-20'})
    assert S.decide_case(w.hr, cid, {'stageId': susp['id'], 'note': 'Three days without pay', 'suspensionFrom': '2026-10-20',
                                     'suspensionTo': '2026-10-22'}) == {'ok': True}
    ev = one("SELECT kind, status, details FROM career_event WHERE employee_id = %s AND kind = 'suspension'", [w.sita_e.id])
    assert ev['status'] == 'applied' and ev['details']['endDate'] == '2026-10-22'
    # Managers do not see suspensions on the career timeline; HR does.
    as_manager = [e['kind'] for e in people.career(w.ram, str(w.sita_e.id))]
    assert 'suspension' not in as_manager
    as_hr = [e['kind'] for e in people.career(w.hr, str(w.sita_e.id))]
    assert 'suspension' in as_hr
    tpl = next(t for t in docs.list_templates(w.hr) if t['code'] == 'warning')
    letter = docs.issue_letter(w.hr, {'templateId': str(tpl['id']), 'employeeId': str(w.sita_e.id), 'purpose': 'unauthorised absence',
                                      'allowMissing': True})
    assert S.link_letter(w.hr, cid, {'letterId': letter['id']}) == {'ok': True}
    k = S.case_detail(w.sita, cid)['case']
    assert {x: k[x] for x in ('status', 'stageName', 'suspensionFrom', 'letterRef')} == {
        'status': 'decided', 'stageName': 'Suspension', 'suspensionFrom': '2026-10-20', 'letterRef': letter['refNo']}
    # A letter issued to someone else cannot be linked.
    other_letter = docs.issue_letter(w.hr, {'templateId': str(tpl['id']), 'employeeId': str(w.ram_e.id), 'allowMissing': True})
    with pytest.raises(BadRequestError, match='that letter was not issued to this employee'):
        S.link_letter(w.hr, cid, {'letterId': other_letter['id']})


def test_a_termination_stage_records_the_exit_through_the_lifecycle_module(world):
    w = world
    emp = w.new_employee('TRM1')
    k = S.open_case(w.hr, {'employeeId': str(emp.id), 'category': 'misconduct', 'incidentDate': '2026-09-01', 'incident': 'Theft'})['id']
    term = stage_by(w, 'termination')
    assert status_of(S.decide_case, w.hr, k, {'stageId': term['id'], 'note': 'Gross misconduct'}) == 400
    assert S.decide_case(w.hr, k, {'stageId': term['id'], 'note': 'Gross misconduct', 'lastWorkingDay': '2026-10-30'}) == {'ok': True}
    ex = one('SELECT kind, voluntary, status FROM employee_exit WHERE employee_id = %s', [emp.id])
    assert ex == {'kind': 'termination', 'voluntary': False, 'status': 'accepted'}
    assert S.case_detail(w.hr, k)['case']['exitId']


def test_withdrawal_and_the_overdue_reminder(world):
    w = world
    k = S.open_case(w.hr, {'employeeId': str(w.ram_e.id), 'category': 'conduct', 'incidentDate': '2026-10-02', 'incident': 'Shouting'})['id']
    S.show_cause(w.hr, k, {'text': 'Explain', 'responseDue': '2099-01-01'})
    execute("UPDATE disciplinary_case SET response_due = '2026-01-01' WHERE id = %s", [k])
    assert S.relations_daily()['overdueResponses'] == 1
    assert S.relations_daily()['overdueResponses'] == 0
    assert S.withdraw_case(w.hr, k, {'reason': 'Misunderstanding'}) == {'ok': True}
    assert S.list_cases(w.hr, {'employeeId': str(w.ram_e.id)})['cases'][0]['status'] == 'withdrawn'
    assert one("SELECT 1 AS x FROM notification_outbox WHERE template = 'relations.disciplinary.withdrawn'")
    assert status_of(S.withdraw_case, w.hr, k, {'reason': 'again'}) == 409


# ================================================================ milestones, acting and deputation

def test_service_anniversaries_contract_ends_retirements_and_hr_sets_contract_end(world):
    w = world
    in10 = (npt_today() + timedelta(days=10)).isoformat()
    joined = f'{int(in10[:4]) - 5}{in10[4:]}'
    dob = f'{int(in10[:4]) - 58}{in10[4:]}'
    a = w.new_employee('MS1', join_date=date.fromisoformat(joined), dob=date.fromisoformat(dob))
    assert status_of(S.set_contract, w.sita, str(a.id), {'contractEnd': in10}) == 403
    assert S.set_contract(w.hr, str(a.id), {'contractEnd': in10}) == {'ok': True}
    m = [x for x in S.list_milestones(w.hr, {'days': '30'})['milestones'] if x['employeeId'] == str(a.id)]
    assert sorted(x['kind'] for x in m) == ['contract_end', 'retirement', 'service']
    assert next(x for x in m if x['kind'] == 'service')['detail'] == '5 years of service'
    assert any(x['employeeId'] == str(a.id) for x in S.separating(w.hr)['people'])
    assert status_of(S.list_milestones, w.ram, {}) == 403
    c = S.get_contract(w.hr, str(a.id))
    assert c == {'contractEnd': in10, 'retirementDate': dob.replace(dob[:4], str(int(dob[:4]) + 58), 1), 'retirementAge': 58}
    with pytest.raises(BadRequestError, match='the contract cannot end before the join date'):
        S.set_contract(w.hr, str(a.id), {'contractEnd': '2000-01-01'})


def test_milestone_reminders_go_once_to_hr_and_the_manager(world):
    w = world
    today = npt_today().isoformat()
    a = w.new_employee('MS2', manager=w.ram_e, join_date=date.fromisoformat(f'{int(today[:4]) - 1}{today[4:]}'))
    first = S.relations_daily()
    assert first['milestoneReminders'] >= 1
    n = rows("SELECT user_id, title FROM notification_outbox WHERE template = 'relations.milestone.service'")
    assert {str(x['user_id']) for x in n} >= {w.hr.id, w.ram.id}
    assert n[0]['title'] == f'{a.name_en}: 1 year of service'
    assert S.relations_daily()['milestoneReminders'] == 0


def test_acting_assignments_show_on_the_directory_and_end_by_themselves(world):
    from hajiri.people import service as people
    w = world
    t = npt_today().isoformat()
    assert status_of(S.add_assignment, w.hr, {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'startDate': t, 'endDate': t}) == 400
    r = S.add_assignment(w.hr, {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'designation': 'Head of Operations', 'startDate': t, 'endDate': t})
    assert r['id']
    assert status_of(S.add_assignment, w.hr, {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'designation': 'CEO', 'startDate': t,
                                              'endDate': t}) == 409
    d = next(p for p in people.directory(w.sita) if str(p['id']) == str(w.ram_e.id))
    assert d['temp_assignment']['kind'] == 'acting' and d['temp_assignment']['label'] == 'Head of Operations'
    assert len(rows("SELECT kind FROM career_event WHERE employee_id = %s AND kind = 'acting'", [w.ram_e.id])) == 1
    assert one('SELECT designation FROM employee WHERE id = %s', [w.ram_e.id])['designation'] != 'Head of Operations'
    out = S.relations_daily(datetime.now(timezone.utc) + timedelta(days=2))
    assert out['assignmentsEnded'] >= 1
    assert len(S.list_assignments(w.ram, {'employeeId': str(w.ram_e.id), 'status': 'ended'})['assignments']) == 1
    assert status_of(S.list_assignments, w.ram, {'employeeId': str(w.sita_e.id)}) == 403
    # Without naming themselves, a non-HR user cannot list everyone.
    assert status_of(S.list_assignments, w.ram, {}) == 403


def test_deputation_needs_a_destination_hr_can_end_it_early(world):
    w = world
    assert status_of(S.add_assignment, w.hr, {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'startDate': '2026-11-01',
                                              'endDate': '2026-12-01'}) == 400
    r = S.add_assignment(w.hr, {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'branchId': w.branch, 'startDate': '2026-11-01',
                                'endDate': '2026-12-01'})
    assert S.end_assignment(w.hr, r['id'], {'cancel': True}) == {'ok': True}
    assert status_of(S.end_assignment, w.hr, r['id'], {}) == 409
    # Ending one before its start is refused.
    r2 = S.add_assignment(w.hr, {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'branchId': w.branch, 'startDate': '2099-11-01',
                                 'endDate': '2099-12-01'})
    with pytest.raises(BadRequestError, match='the end date is before the start date; cancel it instead'):
        S.end_assignment(w.hr, r2['id'], {})
    with pytest.raises(BadRequestError, match='the end date is before the start date'):
        S.add_assignment(w.hr, {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'branchId': w.branch, 'startDate': '2026-11-02',
                                'endDate': '2026-11-01'})


# ================================================================ Know Your Employee

def test_hr_launches_a_campaign_employees_confirm_hr_sees_progress_and_nudges(world):
    w = world
    assert status_of(S.launch_campaign, w.sita, {'title': 'x', 'sections': ['personal'], 'dueDate': '2099-01-01'}) == 403
    r = S.launch_campaign(w.hr, {'title': 'Annual record check', 'sections': ['personal', 'contact', 'family'], 'dueDate': '2099-01-01'})
    assert r['employees'] >= 3
    cid = r['id']
    mine = S.my_kye(w.sita)
    assert mine['campaigns'][0]['id'] == cid and mine['campaigns'][0]['status'] == 'pending'
    assert 'bank' in mine['completeness']['missing']
    assert S.respond_kye(w.sita, cid, {'status': 'confirmed'}) == {'ok': True}
    detail = S.campaign_detail(w.hr, cid)
    assert next(x for x in detail['responses'] if x['employee_id'] == str(w.sita_e.id))['status'] == 'confirmed'
    n1 = S.nudge_campaign(w.hr, cid)['nudged']
    n2 = S.nudge_campaign(w.hr, cid)['nudged']
    assert n1 > 0 and n2 == 0
    assert S.list_campaigns(w.hr)['campaigns'][0]['confirmed'] == 1
    assert S.close_campaign(w.hr, cid) == {'ok': True}
    assert status_of(S.respond_kye, w.ram, cid, {'status': 'confirmed'}) == 404
    assert status_of(S.nudge_campaign, w.hr, cid) == 409
    assert status_of(S.close_campaign, w.hr, cid) == 404


def test_kye_audience_and_due_date_rules_and_the_reminder(world):
    w = world
    with pytest.raises(BadRequestError, match='the due date has already passed'):
        S.launch_campaign(w.hr, {'title': 'x', 'sections': ['personal'], 'dueDate': '2020-01-01'})
    with pytest.raises(BadRequestError, match='choose the branch'):
        S.launch_campaign(w.hr, {'title': 'x', 'sections': ['personal'], 'audienceKind': 'branch', 'dueDate': '2099-01-01'})
    soon = (npt_today() + timedelta(days=2)).isoformat()
    r = S.launch_campaign(w.hr, {'title': 'Branch check', 'sections': ['bank'], 'audienceKind': 'branch', 'audienceId': w.branch,
                                 'dueDate': soon})
    assert r['employees'] >= 4
    out = S.relations_daily()
    assert out['kyeReminders'] >= 1
    assert S.relations_daily()['kyeReminders'] == 0


def test_record_completeness_report_is_hr_only(world):
    w = world
    r = S.completeness_report(w.hr)
    assert next(x for x in r['employees'] if x['employeeId'] == str(w.sita_e.id))['percent'] < 100
    assert status_of(S.completeness_report, w.sita) == 403


# ================================================================ sign-in pop-up notices

def test_popups_each_person_sees_once_or_until_they_acknowledge(world):
    from hajiri.engage import services as engage
    w = world
    a = engage.post_announcement(w.hr, {'title': 'Office closed Friday', 'body': 'Tihar', 'popup': True})
    b = engage.post_announcement(w.hr, {'title': 'New leave policy', 'body': 'Read it', 'popup': True, 'requiresAck': True})
    p1 = [x['id'] for x in S.popups(w.sita)['popups']]
    assert a['id'] in p1 and b['id'] in p1
    assert S.dismiss_popup(w.sita, a['id']) == {'ok': True}
    assert status_of(S.dismiss_popup, w.sita, b['id']) == 400
    assert S.dismiss_popup(w.sita, b['id'], {'acknowledge': True}) == {'ok': True}
    assert S.popups(w.sita)['popups'] == []
    assert len(S.popups(w.ram)['popups']) == 2
    assert engage.announcement_acks(w.hr, b['id'])['acked'] == 1
    # A manager cannot make pop-ups.
    m = engage.post_announcement(w.ram, {'title': 'Team lunch', 'body': 'Friday', 'audienceKind': 'reports', 'popup': True})
    assert one('SELECT popup FROM announcement WHERE id = %s', [m['id']])['popup'] is False
    # The author does not get their own pop-up; another company's notice is not found.
    assert S.popups(w.hr)['popups'] == []
    assert status_of(S.dismiss_popup, w.other_hr, a['id']) == 404


# ================================================================ registered clock-in devices

TOKEN = 'dev-token-abcdefghijklmnopqrstuvwxyz'


def punch_status(user, token=None) -> int:
    """What the punch route's registered-device check answers (punch calls assert_registered_device first)."""
    try:
        S.assert_registered_device(user.company_id, user.employee_id, token)
    except ForbiddenError as e:
        return e.status
    return 201


def test_off_by_default_punches_work_without_a_device(world):
    assert punch_status(world.sita) != 403


def test_when_required_only_approved_devices_may_punch(world):
    w = world
    pol = S.get_policy(w.hr)['policy']
    assert S.save_policy(w.hr, {**pol, 'registeredDevicesOnly': True})['policy']['registeredDevicesOnly'] is True
    with pytest.raises(ForbiddenError) as blocked:
        S.assert_registered_device(w.sita.company_id, w.sita.employee_id, None)
    assert blocked.value.status == 403 and blocked.value.details == {'code': 'device_not_registered'}
    assert blocked.value.message == 'Clock in from a registered device. Register this phone under My attendance and ask HR to approve it.'
    reg = S.register_device(w.sita, {'token': TOKEN, 'label': 'My phone'}, 'Mozilla/5.0 (iPhone)')
    assert reg['status'] == 'pending'
    assert status_of(S.register_device, w.sita, {'token': TOKEN, 'label': 'Again'}) == 409
    assert punch_status(w.sita, TOKEN) == 403
    assert status_of(S.decide_device, w.sita, reg['id'], 'approve') == 403
    assert S.decide_device(w.hr, reg['id'], 'approve') == {'ok': True}
    assert punch_status(w.sita, TOKEN) != 403
    assert one('SELECT last_used_at FROM registered_device WHERE id = %s', [reg['id']])['last_used_at'] is not None
    # Another employee cannot use Sita's device token.
    assert punch_status(w.ram, TOKEN) == 403
    mine = S.my_devices(w.sita)
    assert mine['required'] is True
    assert S.decide_device(w.hr, reg['id'], 'revoke') == {'ok': True}
    assert punch_status(w.sita, TOKEN) == 403
    S.save_policy(w.hr, {**pol, 'registeredDevicesOnly': False})
    assert punch_status(w.sita) != 403


def test_device_limits_own_approval_and_removal(world):
    w = world
    for i in range(3):
        S.register_device(w.sita, {'token': f'{TOKEN}{i}', 'label': f'Phone {i}'})
    with pytest.raises(ConflictError, match='you already have three devices; remove one first'):
        S.register_device(w.sita, {'token': f'{TOKEN}x', 'label': 'Fourth'})
    d = S.my_devices(w.sita)['devices'][0]
    assert S.remove_my_device(w.sita, d['id']) == {'ok': True}
    assert status_of(S.remove_my_device, w.sita, d['id']) == 404
    assert status_of(S.remove_my_device, w.ram, S.my_devices(w.sita)['devices'][0]['id']) == 404
    # HR approving their own device needs someone else; HR registering does not notify themselves.
    hr_e = w.new_employee('HRD')
    hr_self = make_user(w.co, roles=['hr'], employee=hr_e)
    mine = S.register_device(hr_self, {'token': f'{TOKEN}hr', 'label': 'HR phone'})
    with pytest.raises(ForbiddenError, match='someone else must approve your own clock-in device'):
        S.decide_device(hr_self, mine['id'], 'approve')
    assert not rows("SELECT 1 FROM notification_outbox WHERE template = 'relations.device.new' AND user_id = %s", [hr_self.id])
    assert S.decide_device(w.hr, mine['id'], 'reject') == {'ok': True}
    with pytest.raises(ConflictError, match='this device cannot be changed that way'):
        S.decide_device(w.hr, mine['id'], 'approve')
    assert one("SELECT title FROM notification_outbox WHERE template = 'relations.device.decided' AND user_id = %s", [hr_self.id])['title'] == \
        'Device not approved'
    with pytest.raises(ValidationError):
        S.register_device(w.sita, {'token': 'short', 'label': 'x'})


def test_summary_is_hr_only(world):
    w = world
    s = S.summary(w.hr)
    assert s == {'openGrievances': 0, 'openCases': 0, 'pendingDevices': 0, 'openKye': 0, 'isHr': True}
    assert status_of(S.summary, w.sita) == 403


def test_policy_is_hr_only_and_dedupes_service_years(world):
    w = world
    assert status_of(S.get_policy, w.sita) == 403
    p = S.save_policy(w.hr, {'retirementAge': 60, 'serviceYears': [10, 5, 5, 1], 'registeredDevicesOnly': False, 'appealDays': 30})
    assert p['policy']['serviceYears'] == [1, 5, 10]
    assert S.get_policy(w.hr)['policy'] == {'retirementAge': 60, 'serviceYears': [1, 5, 10], 'registeredDevicesOnly': False, 'appealDays': 30}
    with pytest.raises(ValidationError):
        S.save_policy(w.hr, {'retirementAge': 20})
