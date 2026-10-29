"""Sign-in accounts for the browser tests, on a database filled by `manage.py seed_demo_group`.

    DATABASE_URL=postgres://localhost:5433/hajiri_e2e .venv/bin/python manage.py shell < tests/e2e/seed_users.py > users.json

Never run it against the live database: it resets passwords. Per company (DEMO and its sister DEMOSYS) it gives one
account to every company role (admin, hr, payroll_admin, attendance_admin, recruiter, executive; a manager with
direct reports and one of their reports), plus a group admin, a group HR and a group executive, and the demo person
who works in both companies. Every account gets the same password. The JSON on stdout (after the E2EJSON marker
line) is what tests/e2e/test_browser.py reads from HAJIRI_E2E_USERS.
"""
import datetime
import json

from hajiri.core.auth import create_user
from hajiri.core.users import reset_password
from hajiri.db.models import AppUser, Company, CompanyMembership, Employee, EmployeeAssignment, GroupMember

PW = 'E2e-Hajiri-Pass-2026!'
ROLES = ['admin', 'hr', 'payroll_admin', 'attendance_admin', 'recruiter', 'executive']
TWO_COMPANY = 'rajesh@corevalley.test'  # demo.group SHARED: one sign-in, an employee record in each company


def ensure(company, email, name, roles, employee_id=None):
    if AppUser.objects.filter(email=email).exists():
        reset_password(email, PW)
    else:
        create_user(company_id=str(company.pk), email=email, name=name, password=PW, roles=roles,
                    employee_id=employee_id, must_change_password=False)
    return email


def account_for(company, emp, roles):
    """The sign-in of an employee record (their own account or their membership), or a new one."""
    u = AppUser.objects.filter(employee=emp).first()
    if u:
        AppUser.objects.filter(pk=u.pk).update(roles=sorted(set(u.roles) | set(roles)), must_change_password=False)
        reset_password(u.email, PW)
        return u.email
    mem = CompanyMembership.objects.filter(employee=emp).select_related('user').first()
    if mem:
        CompanyMembership.objects.filter(user=mem.user, company=company).update(roles=sorted(set(mem.roles) | set(roles)))
        reset_password(mem.user.email, PW)
        return mem.user.email
    return ensure(company, f'e2e-{emp.emp_code.lower()}@{company.code.lower()}.test', emp.name_en, roles, str(emp.pk))


out = {'password': PW, 'companies': {}, 'group': {}}
today = datetime.date.today()
for code in ('DEMO', 'DEMOSYS'):
    c = Company.objects.get(code=code)
    d = {'id': str(c.pk), 'code': code}
    for r in ROLES:
        d[r] = ensure(c, f'e2e-{r.replace("_", "-")}@{code.lower()}.test', f'E2E {r} {code}', [r])
    mgr_ids = set(EmployeeAssignment.objects.filter(employee__company=c, manager__isnull=False, valid__contains=today)
                  .values_list('manager_id', flat=True))
    mgr = Employee.objects.filter(pk__in=mgr_ids, status='active').order_by('emp_code').first()
    emp = (Employee.objects.filter(company=c, status='active', employee_assignments__manager=mgr)
           .exclude(pk__in=mgr_ids).order_by('emp_code').first())
    d['manager'] = account_for(c, mgr, ['employee', 'manager'])
    d['employee'] = account_for(c, emp, ['employee'])
    d['manager_employee_id'], d['employee_id'] = str(mgr.pk), str(emp.pk)
    out['companies'][code] = d

demo = Company.objects.get(code='DEMO')
for gr, home in (('group_admin', 'admin'), ('group_hr', 'hr'), ('group_executive', 'executive')):
    email = ensure(demo, f'e2e-{gr.replace("_", "-")}@demo.test', f'E2E {gr}', [home])
    GroupMember.objects.update_or_create(user=AppUser.objects.get(email=email), group_id=demo.group_id,
                                         defaults={'roles': [gr]})
    out['group'][gr] = email
reset_password(TWO_COMPANY, PW)
out['group']['two_company'] = TWO_COMPANY
print('E2EJSON')
print(json.dumps(out))
