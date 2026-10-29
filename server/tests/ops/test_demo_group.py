"""The demo group seeder (demo.group, `manage.py seed_demo_group`): two companies in one group, a person working in
both, a group executive, both companies filled by the demo suite, and every record kept to its own company."""
import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from demo.group import DEFAULT_SISTER, seed_demo_group
from demo.management.commands.seed_demo_group import NEED_TARGET, Command
from demo.roster import MAIN, SISTER
from demo.suite import DEMO_SUITE_SECTIONS
from hajiri.core import auth
from hajiri.core.companies import group_overview
from hajiri.core.users import reset_password
from hajiri.db.models import (
    AppUser, AuditLog, Company, CompanyMembership, DocumentCategory, Employee, GroupMember, HelpdeskTicket, LeaveType, OrgUnit,
    Session,
)

from .test_demo_suite import LIVE, _this_database_url, live_env  # noqa: F401 (fixture)

PW = 'correct-horse-battery-staple'
SISTER_CODE = DEFAULT_SISTER[0]


def quiet(_msg: str) -> None:
    pass


def sign_in(email: str, company_id: str | None = None) -> tuple[Client, auth.AuthUser]:
    reset_password(email, PW)
    token = auth.login(email, PW).token
    user = auth.resolve_session(token)
    if company_id and user.company_id != company_id:
        user = auth.switch_company(user, token, company_id)
    c = Client()
    c.cookies[auth.SESSION_COOKIE] = token
    return c, user


def test_sets_up_a_group_of_two_companies(demo):
    out = seed_demo_group(suite=False, log=quiet)
    main = Company.objects.get(code='DEMO')
    sister = Company.objects.get(code=SISTER_CODE)
    assert main.group_id and sister.group_id == main.group_id
    assert out.group_id == str(main.group_id)
    assert [c['code'] for c in out.companies] == ['DEMO', SISTER_CODE]
    assert sister.name_en == DEFAULT_SISTER[1]
    # Each company has its own departments and people; the fixture's Sita and Ram kept their records.
    for company, roster in ((main, MAIN), (sister, SISTER)):
        people = Employee.objects.for_company(company.pk).filter(status=Employee.Status.ACTIVE)
        assert sorted(people.values_list('emp_code', flat=True)) == sorted(p[0] for p in roster.people)
        assert OrgUnit.objects.for_company(company.pk).filter(kind=OrgUnit.Kind.DEPARTMENT).count() == 4
    assert str(Employee.objects.get(emp_code='E001', company=main).pk) == demo['sita']
    # The sister company got the standard defaults (it was added through the group page's service).
    assert DocumentCategory.objects.for_company(sister.pk).exists() and LeaveType.objects.for_company(sister.pk).exists()

    laxmi = AppUser.objects.get(email='laxmi@corevalley.test')
    sabina = AppUser.objects.get(email=f'sabina@{SISTER_CODE.lower()}.corevalley.test')
    assert str(sabina.company_id) == str(sister.pk) and 'hr' in sabina.roles
    # The main HR officer added the sister company: group admin, and admin of the sister company by membership.
    assert list(GroupMember.objects.get(user=laxmi).roles) == ['group_admin']
    assert 'admin' in CompanyMembership.objects.get(user=laxmi, company=sister).roles
    # Rajesh works in both: one sign-in, the sister company's own employee record there.
    rajesh = AppUser.objects.get(employee__company=main, employee__emp_code='E009')
    ms = CompanyMembership.objects.get(user=rajesh, company=sister)
    assert ms.employee.emp_code == 'S008' and ms.employee.company_id == sister.pk
    # The group executive sees the group, has no employee record.
    bhim = AppUser.objects.get(email='bhim@corevalley.test')
    assert bhim.employee_id is None and list(GroupMember.objects.get(user=bhim).roles) == ['group_executive']
    # Every step went through the audited services.
    assert AuditLog.objects.filter(table_name='company_group', action='create').count() == 1
    assert AuditLog.objects.filter(table_name='company_membership', action='create').count() == 1
    assert AuditLog.objects.filter(table_name='group_member', action='create').count() == 2
    # The seeder's own sessions are revoked.
    assert not Session.objects.filter(user_agent='demo-suite seeder', revoked_at__isnull=True).exists()

    # A second run finds everything there and writes nothing.
    before = AuditLog.objects.count()
    again = seed_demo_group(suite=False, log=quiet)
    assert again.created == [] and again.group_id == out.group_id
    assert AuditLog.objects.count() == before


def test_people_work_where_the_group_says(demo):
    seed_demo_group(suite=False, log=quiet)
    main = Company.objects.get(code='DEMO')
    sister = Company.objects.get(code=SISTER_CODE)
    rajesh_email = AppUser.objects.get(employee__company=main, employee__emp_code='E009').email

    _, rajesh = sign_in(rajesh_email)
    assert rajesh.company_ids == (str(main.pk), str(sister.pk))
    assert rajesh.company_id == str(main.pk)
    in_sister = rajesh.company(sister.pk)
    assert in_sister.employee_id == str(Employee.objects.get(company=sister, emp_code='S008').pk)

    _, laxmi = sign_in('laxmi@corevalley.test')
    assert laxmi.has_group_role('group_admin') and set(laxmi.company_ids) == {str(main.pk), str(sister.pk)}

    _, bhim = sign_in('bhim@corevalley.test')
    overview = group_overview(bhim)
    assert {c['id'] for c in overview['companies']} == {str(main.pk), str(sister.pk)}
    assert sum(c['headcount'] for c in overview['companies']) == len(MAIN.people) + len(SISTER.people)

    # The sister's HR officer works in the sister company only.
    _, sabina = sign_in(f'sabina@{SISTER_CODE.lower()}.corevalley.test')
    assert sabina.company_ids == (str(sister.pk),)


def test_fills_both_companies_and_keeps_records_apart(demo):
    out = seed_demo_group(log=quiet)
    assert out.failed == [], '\n'.join(f"{f['company']} {f['section']}: {f['error']}" for f in out.failed)
    assert set(out.suites) == {'DEMO', SISTER_CODE}
    for r in out.suites.values():
        assert r.done == DEMO_SUITE_SECTIONS
    main = Company.objects.get(code='DEMO')
    sister = Company.objects.get(code=SISTER_CODE)
    for c in (main, sister):
        assert HelpdeskTicket.objects.for_company(c.pk).count() == 5
    # Rajesh's sister-company employee record took part in the sister's demo (as a line manager there).
    rajesh = AppUser.objects.get(employee__company=main, employee__emp_code='E009')
    assert 'manager' in CompanyMembership.objects.get(user=rajesh, company=sister).roles

    # A ticket of the sister company is not found from the main company, even for the group admin, until they switch.
    theirs = HelpdeskTicket.objects.for_company(sister.pk).first()
    ours = HelpdeskTicket.objects.for_company(main.pk).first()
    client, _ = sign_in('laxmi@corevalley.test')
    assert client.get(f'/api/v1/helpdesk/tickets/{theirs.pk}').status_code == 404
    assert client.get(f'/api/v1/helpdesk/tickets/{ours.pk}').status_code == 200
    sister_hr, _ = sign_in(f'sabina@{SISTER_CODE.lower()}.corevalley.test')
    assert sister_hr.get(f'/api/v1/helpdesk/tickets/{theirs.pk}').status_code == 200
    assert sister_hr.get(f'/api/v1/helpdesk/tickets/{ours.pk}').status_code == 404
    switched, _ = sign_in('laxmi@corevalley.test', str(sister.pk))
    assert switched.get(f'/api/v1/helpdesk/tickets/{theirs.pk}').status_code == 200

    # A second run adds nothing to either company.
    before = AuditLog.objects.count()
    again = seed_demo_group(log=quiet)
    assert again.created == [] and all(r.done == [] and r.skipped == DEMO_SUITE_SECTIONS for r in again.suites.values())
    assert AuditLog.objects.count() == before


def test_staffs_only_a_company_that_needs_people(suite_company):
    """A main company that already has its people and HR keeps them; only the sister company is staffed."""
    before = Employee.objects.for_company(suite_company['company_id']).count()
    out = seed_demo_group(suite=False, log=quiet)
    assert Employee.objects.for_company(suite_company['company_id']).count() == before
    assert not any(line.startswith('DEMO:') for line in out.created)
    assert any(line.startswith(f'{SISTER_CODE}: 12 people') for line in out.created)


def test_refuses_a_sister_outside_the_group(demo):
    from hajiri.company_settings.seed import create_company
    create_company('OTHER', 'Someone else')
    with pytest.raises(RuntimeError, match='company OTHER already exists outside the DEMO group'):
        seed_demo_group(sister='OTHER', suite=False, log=quiet)
    # Nothing was left behind.
    assert not Company.objects.get(code='DEMO').group_id
    with pytest.raises(RuntimeError, match='needs a code of its own'):
        seed_demo_group(sister='demo', suite=False, log=quiet)
    with pytest.raises(RuntimeError, match='company NOPE not found'):
        seed_demo_group(company='NOPE', suite=False, log=quiet)


def test_seeder_sessions_stay_in_companies_the_user_may_open(demo):
    """The seeder works as a person only in a company that person can switch to: the sister company's HR officer has
    no access to the main company, so no session is opened there for her."""
    from demo.kit import Kit
    seed_demo_group(suite=False, log=quiet)
    main, sister = Company.objects.get(code='DEMO'), Company.objects.get(code=SISTER_CODE)
    sabina = AppUser.objects.get(company=sister, roles__contains=['hr'])
    kit = Kit()
    kit.start()
    try:
        assert kit.actor(str(sabina.pk), str(sister.pk)).user.company_id == str(sister.pk)
        with pytest.raises(RuntimeError, match='has no access to company'):
            kit.actor(str(sabina.pk), str(main.pk))
    finally:
        kit.stop()
    assert not Session.objects.filter(user=sabina, revoked_at__isnull=True).exists()


def test_refuses_an_hr_officer_account_it_cannot_work_as(demo):
    """When the roster's HR officer already has an account the seeder cannot use, it says so and adds nothing."""
    from demo.roster import staff_company
    cid = demo['company_id']
    ids = staff_company(cid, MAIN)['employees']
    auth.create_user(company_id=cid, employee_id=ids[MAIN.hr_code], email='laxmi@corevalley.test', name='Laxmi Poudel',
                     password=PW, roles=['employee'], must_change_password=False)
    with pytest.raises(RuntimeError, match=r'laxmi@corevalley\.test \(E012\) is not an active HR or admin account'):
        seed_demo_group(suite=False, log=quiet)
    assert not Company.objects.filter(code=SISTER_CODE).exists()
    assert not Company.objects.get(code='DEMO').group_id


def test_creates_demo_when_missing(db):
    out = seed_demo_group(suite=False, group_name='CoreValley Group', log=quiet)
    assert out.group_name == 'CoreValley Group'
    assert Company.objects.filter(code__in=['DEMO', SISTER_CODE], group_id=out.group_id).count() == 2


# ------------------------------------------------------------------ the command


def run(**opts) -> str:
    out = io.StringIO()
    call_command(Command(), stdout=out, **opts)
    return out.getvalue()


def test_command_needs_an_explicit_database_and_refuses_the_live_one(live_env, db):  # noqa: F811
    with pytest.raises(CommandError) as e:
        run()
    assert str(e.value) == NEED_TARGET
    with pytest.raises(CommandError) as e:
        run(database=LIVE)
    assert str(e.value) == NEED_TARGET
    with pytest.raises(CommandError) as e:
        run(database=LIVE, yes=True)
    assert 'refusing to add demo data to the live database' in str(e.value)
    assert not Company.objects.filter(code=SISTER_CODE).exists()


def test_command_sets_up_the_group(live_env, demo):  # noqa: F811
    text = run(database=_this_database_url(), yes=True, only='structure,helpdesk')
    assert '  ✓ helpdesk' in text
    assert f'group Demo Company group: Demo Company (DEMO), {DEFAULT_SISTER[1]} ({SISTER_CODE})' in text
    assert HelpdeskTicket.objects.for_company(Company.objects.get(code=SISTER_CODE).pk).count() == 5
    text = run(database=_this_database_url(), yes=True, no_suite=True)
    assert text.strip().endswith('0 new setup steps')


def test_command_reports_a_refused_step(live_env, demo):  # noqa: F811
    from hajiri.company_settings.seed import create_company
    create_company('OTHER', 'Someone else')
    with pytest.raises(CommandError, match='already exists outside the DEMO group'):
        run(database=_this_database_url(), yes=True, sister='OTHER')


def test_python_m_demo_group_needs_a_target(live_env, capsys):  # noqa: F811
    from demo.__main__ import main
    with pytest.raises(SystemExit) as e:
        main(['group', '--yes'])
    assert e.value.code == 1
    assert NEED_TARGET in capsys.readouterr().err
