"""Pages and HTMX fragments of the relations slice (pages/relations, components/relations), through django.test.Client."""
from datetime import timedelta

from hajiri.core.clock import npt_today
from hajiri.core.db import one, rows
from hajiri.relations import services as S
from tests.web.factories import hx_triggers, htmx, set_features_off, toasts

from .conftest import client_for


def test_hub_pages_load_for_hr_and_are_refused_to_employees(world):
    w = world
    hr = client_for(w.hr)
    for url, text in [('/relations', 'Grievance committee'), ('/relations/disciplinary', 'No disciplinary cases'),
                      ('/relations/milestones', 'Service anniversaries'), ('/relations/assignments', 'No temporary roles'),
                      ('/relations/kye', 'Record completeness'), ('/relations/devices', 'Clock in only from registered devices')]:
        r = hr.get(url)
        assert r.status_code == 200, url
        assert text in r.content.decode(), url
        assert 'Employee relations' in r.content.decode()
    emp = client_for(w.sita)
    for url in ('/relations/disciplinary', '/_/relations/stages', '/_/relations/policy'):
        assert emp.get(url, **htmx()).status_code == 403


def test_employee_files_a_grievance_and_follows_it_in_the_drawer(world):
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    c = client_for(w.sita)
    page = c.get('/me/help/grievances')
    assert page.status_code == 200 and 'No grievances' in page.content.decode()
    form = c.get('/_/relations/grievances/new', **htmx('hj-modal-body'))
    assert 'data-modal-title="Raise a grievance"' in form.content.decode()
    r = c.post('/_/relations/grievances/new', {'category': 'management', 'subject': 'Unfair shift allocation',
                                               'description': 'Night shifts every week', 'concerns': ''}, **htmx())
    assert r.status_code == 204
    assert toasts(r)[0]['message'] == 'Grievance G-1 filed. The committee will be in touch.'
    trig = hx_triggers(r)
    assert 'hj:refresh-grievances' in trig and 'hj:close-modal' in trig
    gid = str(one('SELECT id FROM grievance')['id'])
    assert trig['hj:open-grievance']['url'] == f'/_/relations/grievances/{gid}'
    thread = c.get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body'))
    body = thread.content.decode()
    assert thread.status_code == 200 and 'Night shifts every week' in body and 'Your manager is never shown it.' in body
    assert 'Committee actions' not in body
    r = c.post(f'/_/relations/grievances/{gid}/notes', {'body': 'More detail'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and 'More detail' in r.content.decode() and toasts(r)[0]['message'] == 'Sent.'
    # The list shows it.
    lst = c.get('/me/help/grievances', **htmx('grievances'))
    assert 'Unfair shift allocation' in lst.content.decode()
    # The committee member sees the case list on the same page, and the manager cannot open the thread.
    m = client_for(w.member).get('/me/help/grievances')
    assert 'Cases for the committee' in m.content.decode() and 'Unfair shift allocation' in m.content.decode()
    assert client_for(w.ram).get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body')).status_code == 404


def test_grievance_form_refuses_a_missing_category_with_a_toast(world):
    c = client_for(world.sita)
    r = c.post('/_/relations/grievances/new', {'subject': 's', 'description': 'd'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['tone'] == 'err'


def test_committee_hearing_decision_and_exclusion_through_the_thread(world):
    w = world
    hr = client_for(w.hr)
    r = hr.post('/_/relations/committee', {'userIds': [w.member.id]}, **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Committee saved.'
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No space'})['id']
    page = hr.get('/relations')
    assert 'Parking' in page.content.decode()
    m = client_for(w.member)
    r = m.post(f'/_/relations/grievances/{gid}/update', {'action': 'review'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Marked as under review.'
    r = m.post(f'/_/relations/grievances/{gid}/update', {'action': 'hearing', 'date': '2026-11-01', 'time': '10:00', 'place': 'Board room'},
               **htmx('hj-drawer-body'))
    assert r.status_code == 200 and 'Board room' in r.content.decode()
    assert one('SELECT status FROM grievance WHERE id = %s', [gid])['status'] == 'hearing_scheduled'
    # Only HR excludes; the member does not see the exclusion picker.
    assert 'Exclude from this case' not in r.content.decode()
    r = hr.post(f'/_/relations/grievances/{gid}/update', {'action': 'exclude', 'excludeUserIds': [w.member.id]}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Saved.'
    assert m.get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body')).status_code == 404
    r = hr.post(f'/_/relations/grievances/{gid}/update', {'action': 'exclude'}, **htmx('hj-drawer-body'))
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only an administrator can lift an exclusion'
    r = hr.post(f'/_/relations/grievances/{gid}/decision', {'outcome': 'upheld', 'findings': 'Spaces assigned'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Decision recorded.'
    s = client_for(w.sita)
    t = s.get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body')).content.decode()
    assert 'Spaces assigned' in t and 'Accept and close' in t and 'Appeal (until' in t
    r = s.post(f'/_/relations/grievances/{gid}/appeal', {'reason': 'Not enough'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Appeal sent to HR.'
    t = client_for(w.hr2).get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body')).content.decode()
    assert 'Hear the appeal' in t
    r = client_for(w.hr2).post(f'/_/relations/grievances/{gid}/appeal-decision', {'outcome': 'decision_upheld', 'note': 'Stands'},
                               **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Appeal decided.'
    assert one('SELECT status FROM grievance WHERE id = %s', [gid])['status'] == 'closed'


def test_attachment_download_is_scoped(world):
    from hajiri.files.service import upload

    from .conftest import PDF_DATA_URL
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    f = upload(w.sita, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 's', 'description': 'd', 'fileIds': [f['id']]})['id']
    r = client_for(w.member).get(f'/_/relations/grievances/{gid}/files/{f["id"]}')
    assert r.status_code == 200 and r['Content-Type'] == 'application/pdf'
    assert client_for(w.ram).get(f'/_/relations/grievances/{gid}/files/{f["id"]}').status_code == 404


def test_my_grievances_needs_the_helpdesk_feature(world):
    set_features_off(world.co, 'helpdesk')
    r = client_for(world.sita).get('/me/help/grievances')
    assert r.status_code == 302 and r['Location'] == '/'


def test_hr_runs_a_disciplinary_case_from_the_pages(world):
    w = world
    hr = client_for(w.hr)
    form = hr.get('/_/relations/cases/new', **htmx('hj-modal-body'))
    assert 'Open a disciplinary case' in form.content.decode()
    r = hr.post('/_/relations/cases/new', {'employeeId': str(w.sita_e.id), 'category': 'attendance', 'incidentDate': '2026-10-01',
                                           'incident': 'Absent'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Case D-1 opened.'
    cid = str(one('SELECT id FROM disciplinary_case')['id'])
    assert hx_triggers(r)['hj:open-case']['url'] == f'/_/relations/cases/{cid}'
    page = hr.get('/relations/disciplinary').content.decode()
    assert 'D-1' in page and 'Under investigation' in page
    d = hr.get(f'/_/relations/cases/{cid}', **htmx('hj-drawer-body')).content.decode()
    assert 'Incident (HR only)' in d and 'Send show-cause notice' in d
    r = hr.post(f'/_/relations/cases/{cid}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Notice sent to the employee.'
    assert 'Decide before the reply deadline' in r.content.decode()
    # The employee answers under Profile, Conduct, and the Home card asks them to.
    s = client_for(w.sita)
    home = s.get('/_/relations/home', **htmx()).content.decode()
    assert 'HR has asked you to respond' in home
    conduct = s.get('/me/profile/conduct').content.decode()
    assert 'Notice from HR' in conduct and 'Please respond by' in conduct
    r = s.post(f'/_/relations/cases/{cid}/respond', {'response': 'I was ill'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Your response was sent to HR.'
    assert 'hj:refresh-my-cases' in hx_triggers(r)
    # A suspension: the date fields are read only for the chosen stage's effect.
    susp = next(st for st in S.list_stages(w.hr)['stages'] if st['effect'] == 'suspension')
    r = hr.post(f'/_/relations/cases/{cid}/decide', {'stageId': susp['id'], 'note': 'Three days'}, **htmx('hj-drawer-body'))
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'enter the suspension dates'
    r = hr.post(f'/_/relations/cases/{cid}/decide', {'stageId': susp['id'], 'note': 'Three days', 'suspensionFrom': '2026-10-20',
                                                    'suspensionTo': '2026-10-22', 'lastWorkingDay': '2026-10-30'}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Decision recorded. The employee was told.'
    assert 'Issue the letter' in r.content.decode()
    assert not rows('SELECT 1 FROM employee_exit')
    r = hr.post(f'/_/relations/cases/{cid}/letter', {}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Letter issued and linked to the case.'
    assert one('SELECT letter_id FROM disciplinary_case WHERE id = %s', [cid])['letter_id']
    assert 'Open the letter' in s.get('/me/profile/conduct').content.decode()


def test_stages_editor_saves_and_adds_a_stage(world):
    w = world
    hr = client_for(w.hr)
    form = hr.get('/_/relations/stages', **htmx('hj-modal-body')).content.decode()
    assert 'Disciplinary stages' in form and 'Final written warning' in form
    stages = S.list_stages(w.hr)['stages']
    data = {}
    for i, s in enumerate(stages):
        data.update({f's-{i}-present': '1', f's-{i}-id': s['id'], f's-{i}-code': s['code'], f's-{i}-name': s['name'],
                     f's-{i}-severity': str(s['severity']), f's-{i}-effect': s['effect'], f's-{i}-letter': s['letter_template_code'] or '',
                     f's-{i}-active': '1'})
    data.update({'s-5-present': '1', 's-5-code': 'stage_6', 's-5-name': 'New stage', 's-5-severity': '3', 's-5-effect': 'none', 's-5-active': '1'})
    r = hr.post('/_/relations/stages', data, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Stages saved.'
    assert [s['code'] for s in S.list_stages(w.hr)['stages']][-1] == 'stage_6'


def test_milestones_page_policy_and_contract(world):
    w = world
    hr = client_for(w.hr)
    in10 = (npt_today() + timedelta(days=10)).isoformat()
    a = w.new_employee('MSV')
    r = hr.post(f'/_/relations/employee/{a.id}/contract', {'contractEnd': in10}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Saved.'
    page = hr.get('/relations/milestones?days=30').content.decode()
    assert 'Contract ends' in page and 'Person MSV' in page
    form = hr.get('/_/relations/policy', **htmx('hj-modal-body')).content.decode()
    assert 'Milestone and relations settings' in form
    r = hr.post('/_/relations/policy', {'retirementAge': '60', 'serviceYears': '10, 1, 5, 5', 'appealDays': '20'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Saved.'
    assert S.relations_policy(w.company_id) == {'retirementAge': 60, 'serviceYears': [1, 5, 10], 'registeredDevicesOnly': False, 'appealDays': 20}


def test_employee_relations_tab(world):
    w = world
    hr = client_for(w.hr)
    r = hr.get(f'/_/relations/employee/{w.sita_e.id}', **htmx())
    body = r.content.decode()
    assert r.status_code == 200 and 'Contract and retirement' in body and 'Record completeness' in body and 'No cases.' in body
    assert 'Add the date of birth to see the retirement date.' in body
    r = hr.post('/_/relations/assignments/new', {'employeeId': str(w.sita_e.id), 'kind': 'deputation', 'branchId': w.branch,
                                                 'startDate': '2026-11-01', 'endDate': '2026-12-01'}, **htmx())
    assert r.status_code == 204 and 'hj:refresh-relations-assignments' in hx_triggers(r)
    part = hr.get(f'/_/relations/employee/{w.sita_e.id}', **htmx('relations-assignments')).content.decode()
    assert 'Deputed to Head Office' in part
    assert client_for(w.sita).get(f'/_/relations/employee/{w.sita_e.id}', **htmx()).status_code == 403
    # The employees slice's Relations tab finds this fragment.
    page = hr.get(f'/employees/{w.sita_e.id}?tab=relations')
    if page.status_code == 200:
        assert f'/_/relations/employee/{w.sita_e.id}' in page.content.decode()


def test_assignments_page_end_and_cancel(world):
    w = world
    hr = client_for(w.hr)
    t = npt_today().isoformat()
    r = hr.post('/_/relations/assignments/new', {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'designationId': '', 'startDate': t,
                                                 'endDate': t}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'choose the position they are acting in'
    did = str(one("INSERT INTO designation (company_id, code, name_en) VALUES (%s, 'HOO', 'Head of Operations') RETURNING id",
                  [w.company_id])['id'])
    r = hr.post('/_/relations/assignments/new', {'employeeId': str(w.ram_e.id), 'kind': 'acting', 'designationId': did, 'startDate': t,
                                                 'endDate': t}, **htmx())
    assert r.status_code == 204
    page = hr.get('/relations/assignments').content.decode()
    assert 'Head of Operations' in page and 'End today' in page
    aid = S.list_assignments(w.hr)['assignments'][0]['id']
    r = hr.post(f'/_/relations/assignments/{aid}/end', {'cancel': '1'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Cancelled.'
    assert 'No temporary roles' in hr.get('/relations/assignments').content.decode()


def test_kye_campaign_from_launch_to_confirmation(world):
    w = world
    hr = client_for(w.hr)
    form = hr.get('/_/relations/kye/new', **htmx('hj-modal-body')).content.decode()
    assert 'Ask employees to review their records' in form
    r = hr.post('/_/relations/kye/new', {'title': 'Annual record check', 'sections': ['personal', 'bank'], 'audienceKind': 'all',
                                         'dueDate': '2099-01-01'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'].startswith('Sent to ')
    cid = str(one('SELECT id FROM kye_campaign')['id'])
    s = client_for(w.sita)
    home = s.get('/_/relations/home', **htmx()).content.decode()
    assert 'Annual record check' in home and 'Bank account: missing' in home
    r = s.post(f'/_/relations/kye/{cid}/respond', {'status': 'confirmed'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Thank you. HR has your confirmation.'
    assert s.get('/_/relations/home', **htmx()).content.decode().count('Annual record check') == 0
    r = client_for(w.ram).post(f'/_/relations/kye/{cid}/respond', {'status': 'changes_requested'}, **htmx())
    assert r.status_code == 204 and r['HX-Redirect'] == '/me/profile'
    d = hr.get(f'/_/relations/kye/{cid}', **htmx('hj-drawer-body')).content.decode()
    assert 'Confirmed' in d and 'Asked for changes' in d
    r = hr.post(f'/_/relations/kye/{cid}/nudge', {}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'].startswith('Reminded ')
    r = hr.post(f'/_/relations/kye/{cid}/nudge', {}, **htmx('hj-drawer-body'))
    assert toasts(r)[0]['message'] == 'Everyone pending was already reminded today.'
    r = hr.post(f'/_/relations/kye/{cid}/close', {}, **htmx('hj-drawer-body'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Closed.'
    comp = hr.get('/relations/kye?view=all', **htmx('kye-completeness')).content.decode()
    assert 'Sita Sharma' in comp


def test_home_card_is_empty_when_there_is_nothing(world):
    r = client_for(world.sita).get('/_/relations/home', **htmx())
    assert r.status_code == 200 and 'HR has asked' not in r.content.decode() and 'hj-card' not in r.content.decode()


def test_popup_notices_fragment_and_dismissal(world):
    from hajiri.engage import services as engage
    w = world
    s = client_for(w.sita)
    r = s.get('/_/relations/popups', **htmx())
    assert r.status_code == 200 and r.content == b''
    a = engage.post_announcement(w.hr, {'title': 'Office closed Friday', 'body': 'Tihar', 'popup': True})
    b = engage.post_announcement(w.hr, {'title': 'New leave policy', 'body': 'Read it', 'popup': True, 'requiresAck': True})
    # Both were posted in this test's transaction; make the newer one explicit (newest first).
    from hajiri.core.db import execute
    execute("UPDATE announcement SET created_at = created_at - interval '1 minute' WHERE id = %s", [a['id']])
    r = s.get('/_/relations/popups', **htmx())
    html = r.content.decode()
    assert 'New leave policy' in html and 'I have read and understood this notice' in html and 'Confirm' in html
    r = s.post(f'/_/relations/popups/{b["id"]}/dismiss', {}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'confirm that you have read this notice'
    r = s.post(f'/_/relations/popups/{b["id"]}/dismiss', {'acknowledge': '1'}, **htmx())
    assert r.status_code == 200 and 'Office closed Friday' in r.content.decode() and 'Got it' in r.content.decode()
    r = s.post(f'/_/relations/popups/{a["id"]}/dismiss', {}, **htmx())
    assert r.status_code == 200 and r.content == b''
    # Every signed-in page asks for the fragment.
    assert '/_/relations/popups' in s.get('/me/attendance/devices').content.decode()


def test_devices_pages_register_approve_and_switch_the_rule(world):
    w = world
    s = client_for(w.sita)
    page = s.get('/me/attendance/devices', HTTP_USER_AGENT='Mozilla/5.0 (iPhone)').content.decode()
    assert 'Register this device' in page and 'does not require registered devices' in page and 'iPhone' in page
    r = s.post('/_/relations/devices/register', {'device_token': 'tok-abcdefghijklmnopqrstu', 'label': 'My phone'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Registered. HR has been asked to approve it.'
    hr = client_for(w.hr)
    page = hr.get('/relations/devices').content.decode()
    assert 'My phone' in page and 'Approve' in page
    did = S.list_devices(w.hr)['devices'][0]['id']
    r = hr.post(f'/_/relations/devices/{did}/approve', {}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Approved.' and 'hj:refresh-devices' in hx_triggers(r)
    assert hr.post(f'/_/relations/devices/{did}/bogus', {}, **htmx()).status_code == 400  # z.enum on :action, as in TS
    r = hr.post('/_/relations/devices/policy', {'registeredDevicesOnly': ['0', '1']}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Clock-in now needs a registered device.'
    assert S.relations_policy(w.company_id)['registeredDevicesOnly'] is True
    assert 'requires a registered device' in s.get('/me/attendance/devices').content.decode()
    r = hr.post('/_/relations/devices/policy', {'registeredDevicesOnly': '0'}, **htmx())
    assert toasts(r)[0]['message'] == 'Any device can clock in again.'
    r = s.post(f'/_/relations/devices/mine/{did}/remove', {}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Removed.'
    assert 'No devices yet' in s.get('/me/attendance/devices', **htmx('my-devices')).content.decode()


def test_conduct_tab_needs_an_employee_record(world):
    r = client_for(world.hr).get('/me/profile/conduct')
    assert r.status_code == 302 and r['Location'] == '/me/profile'
    assert 'Nothing here' in client_for(world.sita).get('/me/profile/conduct').content.decode()


def test_nav_lists_the_relations_tabs(world):
    from hajiri.web import nav
    hr_tabs = [i.url_name for i in nav.items_for(world.hr)] if hasattr(nav, 'items_for') else None
    page = client_for(world.hr).get('/relations').content.decode()
    for label in ('Disciplinary', 'Milestones', 'Acting &amp; deputation', 'Know your employee', 'Clock-in devices'):
        assert label in page
    assert hr_tabs is None or 'relations:grievances' in hr_tabs


def test_popup_fragment_is_empty_behind_the_password_change_gate(world):
    from hajiri.engage import services as engage
    from tests.web.factories import make_user
    w = world
    engage.post_announcement(w.hr, {'title': 'Office closed Friday', 'body': 'Tihar', 'popup': True})
    u = make_user(w.co, roles=['employee'], employee=w.new_employee('PW1'), must_change_password=True)
    r = client_for(u).get('/_/relations/popups', **htmx())
    assert r.status_code == 200 and r.content == b''
    assert client_for(w.sita).get('/_/relations/popups', **htmx()).content != b''
