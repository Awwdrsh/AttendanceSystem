"""A demo GROUP of companies: the main demo company and a sister company in one group, each filled by the demo suite.

What it sets up, through the same services the group pages use (create_group_company, grant_access,
grant_group_role), so every step is checked and audited as if an admin had clicked through it:

- the main company (DEMO is created with `seed_demo` when missing) staffed with the demo roster when it has fewer
  than eight active people, and its HR officer's account;
- a sister company in the same group, added by that HR officer (who becomes the group admin and an admin of the
  sister company), with its own departments, people and HR officer;
- one person who works in both companies: the finance manager has an employee record in each and one sign-in,
  switching between them with the company switcher;
- a group executive who sees every company of the group on the Group overview, read-only;
- then the demo suite for each company (unless suite=False).

Running it again adds only what is missing. Payroll runs are never touched.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable

from django.db import transaction

from hajiri.core import companies
from hajiri.core.audit import audit
from hajiri.core.auth import create_user
from hajiri.db.models import AppUser, Company, CompanyGroup, CompanyMembership, Employee, GroupMember

from .context import build_context, demo_hr
from .kit import Kit
from .roster import MAIN, SHARED, SISTER, Roster, ensure_hr, staff_company
from .suite import SuiteResult, seed_demo_suite

MIN_PEOPLE = 8
DEFAULT_SISTER = ('DEMOSYS', 'Demo Systems Pvt. Ltd.')
EXECUTIVE_NAME = 'Bhim Bahadur Gurung'


@dataclass
class GroupResult:
    group_id: str
    group_name: str
    companies: list[dict]                               # [{'id', 'code', 'name'}], main first
    created: list[str] = field(default_factory=list)    # what this run added, in words
    suites: dict[str, SuiteResult] = field(default_factory=dict)

    @property
    def failed(self) -> list[dict]:
        return [{'company': code, **f} for code, r in self.suites.items() for f in r.failed]


def _active_people(company_id: str) -> int:
    return Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE).count()


def _main_company(code: str, log) -> Company:
    c = Company.objects.filter(code=code).first()
    if c is None and code == 'DEMO':
        from hajiri.company_settings.demo import seed_demo
        seed_demo()
        log('created company DEMO (the development seed)')
        c = Company.objects.get(code=code)
    if c is None:
        raise RuntimeError(f'company {code} not found')
    return c


def _staff(company: Company, roster: Roster, domain: str, created: list[str], log) -> AppUser:
    """Staff a company with a roster when it has fewer than MIN_PEOPLE active people; returns its HR account."""
    cid = str(company.pk)
    if _active_people(cid) < MIN_PEOPLE:
        staff_company(cid, roster)
        created.append(f'{company.code}: {len(roster.people)} people in {len(roster.units)} departments')
    hr = demo_hr(cid)
    if hr is None:
        hr, made = ensure_hr(cid, roster, domain)
        if not made:
            # The roster's HR officer already has an account, but not one the seeder can work as.
            raise RuntimeError(f'{company.code}: {hr.email} ({roster.hr_code}) is not an active HR or admin account '
                               'with a password set; fix that account or add an HR user, then run again')
        created.append(f'{company.code}: HR account {hr.email}')
        log(f'{company.code}: HR account {hr.email} (random password; set one with manage.py reset_password)')
    return hr


def _domain(email: str) -> str:
    parts = email.split('@')
    return parts[1] if len(parts) > 1 else 'example.com'


def seed_demo_group(*, company: str = 'DEMO', sister: str = DEFAULT_SISTER[0], sister_name: str = DEFAULT_SISTER[1],
                    group_name: str | None = None, suite: bool = True, only: list[str] | None = None,
                    log: Callable[[str], None] = print) -> GroupResult:
    """Set up the demo group (see the module docstring) and fill both companies with the demo suite."""
    sister = sister.strip().upper()
    if sister == company.strip().upper():
        raise RuntimeError('the sister company needs a code of its own')
    kit = Kit()
    kit.start()
    created: list[str] = []
    try:
        with transaction.atomic():
            main = _main_company(company, log)
            main_id = str(main.pk)
            hr_main = _staff(main, MAIN, 'corevalley.test', created, log)
            domain = _domain(hr_main.email)

            # The sister company, added by the main company's HR officer from the group page's "Add a company".
            sis = Company.objects.filter(code__iexact=sister).first()
            if sis is not None and (not main.group_id or sis.group_id != main.group_id):
                raise RuntimeError(f'company {sis.code} already exists outside the {main.code} group')
            if sis is None:
                actor = kit.actor(str(hr_main.pk), main_id)
                kit.call('POST /group/companies', companies.create_group_company, actor.user,
                         {'code': sister, 'name_en': sister_name})
                sis = Company.objects.get(code=sister)
                main.refresh_from_db(fields=['group'])
                created.append(f'{sis.code}: added to the group by {hr_main.email}')
            group = CompanyGroup.objects.get(pk=main.group_id)
            if group_name and group.name_en != group_name:
                audit('update', 'company_group', actor_id=str(hr_main.pk), row_id=str(group.pk),
                      before={'name': group.name_en}, after={'name': group_name})
                CompanyGroup.objects.filter(pk=group.pk).update(name_en=group_name)
                group.name_en = group_name
            sis_id = str(sis.pk)
            _staff(sis, SISTER, f'{sis.code.lower()}.{domain}', created, log)

            # Employee accounts in the main company (the shared person needs one to be given access).
            build_context(kit, main.code, log)
            _share_person(kit, hr_main, main_id, sis_id, created)
            _group_executive(kit, hr_main, main_id, str(main.group_id), domain, created, log)

        result = GroupResult(group_id=str(group.pk), group_name=group.name_en, created=created,
                             companies=[{'id': main_id, 'code': main.code, 'name': main.name_en},
                                        {'id': sis_id, 'code': sis.code, 'name': sis.name_en}])
    finally:
        kit.stop()
    for line in created:
        log(f'  + {line}')
    if suite:
        for c in result.companies:
            log(f"{c['code']} ({c['name']}):")
            result.suites[c['code']] = seed_demo_suite(company=c['code'], only=only, log=log)
    return result


def _share_person(kit: Kit, hr_main: AppUser, main_id: str, sis_id: str, created: list[str]) -> None:
    """The main company's finance manager also works in the sister company, with the sister's employee record."""
    main_code, sister_code = SHARED
    person = AppUser.objects.filter(employee__company_id=main_id, employee__emp_code=main_code).first()
    there = Employee.objects.for_company(sis_id).filter(emp_code=sister_code).first()
    if person is None or there is None or CompanyMembership.objects.for_company(sis_id).filter(user=person).exists():
        return
    if CompanyMembership.objects.filter(employee=there).exists() or AppUser.objects.filter(employee=there).exists():
        return
    admin_there = kit.actor(str(hr_main.pk), sis_id)
    kit.call('POST /group/access', companies.grant_access, admin_there.user,
             {'email': person.email, 'roles': [AppUser.Role.EMPLOYEE], 'employee_id': str(there.pk)})
    created.append(f'{person.email}: works in both companies (one sign-in, an employee record in each)')


def _group_executive(kit: Kit, hr_main: AppUser, main_id: str, group_id: str, domain: str, created: list[str],
                     log) -> None:
    """A group executive: an account of the main company without an employee record, who sees every company of the
    group on the Group overview."""
    email = f'bhim@{domain}'
    exec_user = AppUser.objects.filter(email=email).first()
    if exec_user is None:
        exec_user = AppUser.objects.get(pk=create_user(
            company_id=main_id, email=email, name=EXECUTIVE_NAME, password=secrets.token_urlsafe(18),
            roles=[AppUser.Role.EXECUTIVE], must_change_password=False, actor_id=str(hr_main.pk)).id)
        log(f'group executive {email} (random password; set one with manage.py reset_password)')
    if GroupMember.objects.filter(user=exec_user, group_id=group_id).exists():
        return
    admin = kit.actor(str(hr_main.pk), main_id)
    kit.call('POST /group/roles', companies.grant_group_role, admin.user,
             {'email': email, 'roles': [GroupMember.Role.GROUP_EXECUTIVE]})
    created.append(f'{email}: group executive')
