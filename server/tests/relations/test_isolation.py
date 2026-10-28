"""Groups of companies: every relations page, fragment and service works in the ACTIVE company only. A record of a
sister company is not found until the person switches to it, people of the group with access count as people of the
company (committee, HR notices), and a person who works in two companies files and follows grievances per company."""
from types import SimpleNamespace

import pytest
from django.test import Client

from hajiri.core import auth, companies
from hajiri.db.models import (
    Branch, Company, CompanyMembership, DisciplinaryCase, Grievance, GroupMember, NotificationOutbox,
)
from hajiri.relations import services as S
from tests.web.factories import PW, htmx, make_company, make_employee, make_user, sign_in, toasts

from .conftest import PDF_DATA_URL

NOWHERE = '00000000-0000-7000-8000-000000000000'


def session(person, company=None) -> auth.AuthUser:
    token = auth.login(person.email, PW).token
    user = auth.resolve_session(token)
    if company is not None and str(company.id) != user.company_id:
        user = auth.switch_company(user, token, str(company.id))
    return user


def client_in(person, company=None) -> Client:
    c = Client()
    sign_in(c, person)
    if company is not None:
        r = c.post('/_/web/company/switch', {'company_id': str(company.id)}, **htmx())
        assert r.status_code == 204, r.content
    return c


@pytest.fixture
def grp(db):
    """Alpha and Beta in one group (Alpha's admin started it, so is group admin), Gamma outside. Beta has a
    grievance, a disciplinary case, a KYE campaign, a device waiting for approval, a deputation and a pop-up notice.
    Dual is HR at home in Alpha and an employee of Beta by membership; Gopal is a Beta employee with group_hr."""
    from hajiri.engage import services as engage
    from hajiri.files.service import upload
    alpha = make_company('RALPHA', 'Alpha Traders')
    admin_a = make_user(alpha, roles=['admin'], employee=make_employee(alpha, name='Asha Admin'), email='admin@ralpha.test')
    beta = Company.objects.get(pk=companies.create_group_company(session(admin_a), {'code': 'RBETA', 'name_en': 'Beta Foods'})['id'])
    alpha.refresh_from_db()
    gamma = make_company('RGAMMA', 'Gamma Outside')
    admin_c = make_user(gamma, roles=['admin'], email='admin@rgamma.test')
    alpha_e = make_employee(alpha, name='Anita Alpha')
    alpha_emp = make_user(alpha, roles=['employee'], employee=alpha_e, email='anita@ralpha.test')
    hr_b = make_user(beta, roles=['hr'], name='Beta HR', email='hr@rbeta.test')
    hr_b2 = make_user(beta, roles=['hr'], name='Beta HR Two', email='hr2@rbeta.test')
    branch_b = Branch.objects.create(company=beta, code='BHQ', name_en='Beta Head Office')
    beta_e = make_employee(beta, name='Bina Beta', branch=str(branch_b.id))
    beta_emp = make_user(beta, roles=['employee'], employee=beta_e, email='bina@rbeta.test')
    dual = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Dipak Dual'), email='dual@ralpha.test')
    dual_beta = make_employee(beta, name='Dipak at Beta')
    CompanyMembership.objects.create(user_id=dual.id, company=beta, roles=['employee'], employee=dual_beta)
    ghr = make_user(beta, roles=['employee'], employee=make_employee(beta, name='Gopal Group HR'), email='ghr@rbeta.test')
    GroupMember.objects.create(user_id=ghr.id, group_id=alpha.group_id, roles=['group_hr'])

    f = upload(beta_emp, {'purpose': 'grievance_attachment', 'filename': 'note.pdf', 'dataUrl': PDF_DATA_URL})
    grievance = S.file_grievance(beta_emp, {'category': 'safety', 'subject': 'Beta wet floor', 'description': 'Kitchen floor',
                                            'fileIds': [f['id']]})['id']
    case = S.open_case(hr_b, {'employeeId': str(beta_e.id), 'category': 'conduct', 'incidentDate': '2026-10-01',
                              'incident': 'Beta incident'})['id']
    kye = S.launch_campaign(hr_b, {'title': 'Beta record check', 'sections': ['bank'], 'dueDate': '2099-01-01'})['id']
    device = S.register_device(beta_emp, {'token': 'beta-device-token-0123456789', 'label': 'Bina phone'})['id']
    assignment = S.add_assignment(hr_b, {'employeeId': str(beta_e.id), 'kind': 'deputation', 'branchId': str(branch_b.id),
                                         'startDate': '2099-01-01', 'endDate': '2099-02-01'})['id']
    notice = engage.post_announcement(hr_b, {'title': 'Beta closed Friday', 'body': 'Tihar', 'popup': True})['id']
    return SimpleNamespace(alpha=alpha, beta=beta, gamma=gamma, admin_a=admin_a, admin_c=admin_c, alpha_e=alpha_e,
                           alpha_emp=alpha_emp, hr_b=hr_b, hr_b2=hr_b2, beta_e=beta_e, beta_emp=beta_emp, dual=dual,
                           dual_beta=dual_beta, ghr=ghr, grievance=grievance, file=f['id'], case=case, kye=kye, device=device,
                           assignment=assignment, notice=notice)


def gets(w):
    return [f'/_/relations/grievances/{w.grievance}', f'/_/relations/grievances/{w.grievance}/files/{w.file}',
            f'/_/relations/cases/{w.case}', f'/_/relations/kye/{w.kye}', f'/_/relations/employee/{w.beta_e.id}']


def posts(w):
    return [(f'/_/relations/grievances/{w.grievance}/notes', {'body': 'x'}),
            (f'/_/relations/grievances/{w.grievance}/update', {'action': 'review'}),
            (f'/_/relations/grievances/{w.grievance}/decision', {'outcome': 'upheld', 'findings': 'x'}),
            (f'/_/relations/grievances/{w.grievance}/close', {}),
            (f'/_/relations/cases/{w.case}/show-cause', {'text': 'Explain', 'responseDue': '2099-01-01'}),
            (f'/_/relations/cases/{w.case}/decide', {'note': 'x', 'force': '1'}),
            (f'/_/relations/cases/{w.case}/withdraw', {'reason': 'x'}),
            (f'/_/relations/kye/{w.kye}/nudge', {}), (f'/_/relations/kye/{w.kye}/close', {}),
            (f'/_/relations/assignments/{w.assignment}/end', {'cancel': '1'}),
            (f'/_/relations/employee/{w.beta_e.id}/contract', {'contractEnd': '2099-01-01'})]


@pytest.mark.parametrize('who', ['admin_a', 'admin_c'])
def test_a_sister_or_outside_company_record_is_not_found(grp, who):
    """The group admin working in Alpha (admin in Beta too) and Gamma's admin both get 404 for Beta's records."""
    w = grp
    c = client_in(getattr(w, who))
    for url in gets(w):
        assert c.get(url, **htmx('hj-drawer-body')).status_code == 404, url
    for url, data in posts(w):
        assert c.post(url, data, **htmx('hj-drawer-body')).status_code == 404, url
    # A device of another company answers exactly like a device that does not exist (the TS 409).
    foreign = c.post(f'/_/relations/devices/{w.device}/approve', {}, **htmx())
    missing = c.post(f'/_/relations/devices/{NOWHERE}/approve', {}, **htmx())
    assert foreign.status_code == missing.status_code == 409
    assert toasts(foreign)[0]['message'] == toasts(missing)[0]['message']
    # Nothing changed in Beta.
    assert Grievance.objects.get(pk=w.grievance).status == Grievance.Status.SUBMITTED
    assert DisciplinaryCase.objects.get(pk=w.case).status == DisciplinaryCase.Status.OPEN


def test_the_owner_company_and_the_group_admin_after_switching_can_open_them(grp):
    w = grp
    for c in (client_in(w.hr_b), client_in(w.admin_a, w.beta)):
        for url in gets(w):
            assert c.get(url, **htmx('hj-drawer-body')).status_code == 200, url
    r = client_in(w.admin_a, w.beta).post(f'/_/relations/devices/{w.device}/approve', {}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Approved.'


def test_hub_lists_show_only_the_active_company(grp):
    w = grp
    alpha = client_in(w.admin_a)
    pages = {'/relations': 'Beta wet floor', '/relations/disciplinary': 'Bina Beta', '/relations/kye': 'Beta record check',
             '/relations/devices?status=all': 'Bina phone', '/relations/assignments?status=all': 'Bina Beta'}
    for url, text in pages.items():
        r = alpha.get(url)
        assert r.status_code == 200 and text not in r.content.decode(), url
    beta = client_in(w.admin_a, w.beta)
    for url, text in pages.items():
        assert text in beta.get(url).content.decode(), url
    # The services agree.
    a = session(w.admin_a)
    assert S.list_cases(a)['cases'] == [] and S.list_campaigns(a)['campaigns'] == [] and S.list_devices(a)['devices'] == []
    assert S.summary(a) == {'openGrievances': 0, 'openCases': 0, 'pendingDevices': 0, 'openKye': 0, 'isHr': True}
    assert all(m['employeeId'] != str(w.beta_e.id) for m in S.completeness(str(w.alpha.id)))


def test_pop_up_notices_belong_to_their_company(grp):
    w = grp
    dual_alpha = client_in(w.dual)
    assert dual_alpha.get('/_/relations/popups', **htmx()).content == b''
    assert dual_alpha.post(f'/_/relations/popups/{w.notice}/dismiss', {}, **htmx()).status_code == 404
    dual_beta = client_in(w.dual, w.beta)
    assert 'Beta closed Friday' in dual_beta.get('/_/relations/popups', **htmx()).content.decode()
    assert dual_beta.post(f'/_/relations/popups/{w.notice}/dismiss', {}, **htmx()).status_code == 200


def test_a_person_in_two_companies_files_with_the_record_of_the_active_company(grp):
    w = grp
    in_beta = session(w.dual, w.beta)
    gid = S.file_grievance(in_beta, {'category': 'other', 'subject': 'Dual at Beta', 'description': 'd'})['id']
    g = Grievance.objects.get(pk=gid)
    assert str(g.company_id) == str(w.beta.id) and str(g.employee_id) == str(w.dual_beta.id)
    assert [x['subject'] for x in S.list_grievances(in_beta)['grievances']] == ['Dual at Beta']
    # Back home in Alpha, the same person (HR there) sees neither it in their own list nor in Alpha's queue.
    at_home = session(w.dual)
    assert S.list_grievances(at_home)['grievances'] == []
    assert all(x['id'] != gid for x in S.list_grievances(at_home, {'scope': 'handle'})['grievances'])
    with pytest.raises(Exception) as e:
        S.grievance_detail(at_home, gid)
    assert getattr(e.value, 'status', None) == 404


def test_people_of_the_group_count_as_people_of_the_company(grp):
    w = grp
    hr_alpha = session(w.admin_a)
    # Only people who can act in Alpha can sit on its committee: Gopal (group HR) can, a Beta-only account cannot.
    people = {u['id'] for u in S.company_users(hr_alpha)}
    assert w.ghr.id in people and w.hr_b.id not in people
    with pytest.raises(Exception, match='every member must be an active user in your company'):
        S.set_committee(hr_alpha, {'userIds': [w.hr_b.id]})
    assert S.set_committee(hr_alpha, {'userIds': [w.ghr.id]}) == {'ok': True, 'members': 1}
    # A grievance filed in Alpha reaches Alpha's HR including group HR, never Beta's HR.
    gid = S.file_grievance(w.alpha_emp, {'category': 'other', 'subject': 's', 'description': 'd'})['id']
    got = {str(u) for u in NotificationOutbox.objects.filter(template='relations.grievance.new',
                                                             payload__grievanceId=gid).values_list('user_id', flat=True)}
    assert w.ghr.id in got and w.admin_a.id in got and w.dual.id in got
    assert w.hr_b.id not in got and w.alpha_emp.id not in got
    # Group HR handles it while working in Alpha, but not while working in Beta.
    assert S.grievance_detail(session(w.ghr, w.alpha), gid)['grievance']['role'] == 'hr'
    with pytest.raises(Exception) as e:
        S.grievance_detail(session(w.ghr, w.beta), gid)
    assert getattr(e.value, 'status', None) == 404


def test_a_case_or_assignment_cannot_name_an_employee_of_another_company(grp):
    w = grp
    a = session(w.admin_a)
    with pytest.raises(Exception, match='employee not found'):
        S.open_case(a, {'employeeId': str(w.beta_e.id), 'category': 'conduct', 'incidentDate': '2026-10-01', 'incident': 'x'})
    with pytest.raises(Exception, match='employee not found'):
        S.add_assignment(a, {'employeeId': str(w.beta_e.id), 'kind': 'acting', 'designation': 'Lead', 'startDate': '2099-01-01',
                             'endDate': '2099-01-02'})
    # A Beta branch is not a destination for an Alpha deputation.
    branch_b = str(Branch.objects.get(company=w.beta).id)
    with pytest.raises(Exception, match='branch does not exist in your company'):
        S.add_assignment(a, {'employeeId': str(w.alpha_e.id), 'kind': 'deputation', 'branchId': branch_b,
                             'startDate': '2099-01-01', 'endDate': '2099-01-02'})
    with pytest.raises(Exception, match='choose the branch'):
        S.launch_campaign(a, {'title': 'x', 'sections': ['bank'], 'audienceKind': 'branch', 'audienceId': branch_b,
                              'dueDate': '2099-01-01'})


def test_a_beta_device_token_does_not_clock_in_at_alpha(grp):
    w = grp
    S.decide_device(w.hr_b, w.device, 'approve')
    S.save_policy(w.hr_b, {**S.relations_policy(str(w.beta.id)), 'registeredDevicesOnly': True})
    S.assert_registered_device(str(w.beta.id), str(w.beta_e.id), 'beta-device-token-0123456789')
    S.save_policy(session(w.admin_a), {**S.relations_policy(str(w.alpha.id)), 'registeredDevicesOnly': True})
    with pytest.raises(Exception) as e:
        S.assert_registered_device(str(w.alpha.id), str(w.beta_e.id), 'beta-device-token-0123456789')
    assert getattr(e.value, 'status', None) == 403
