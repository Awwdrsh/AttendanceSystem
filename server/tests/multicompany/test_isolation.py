"""
Ids of another company behave as not found, in every app: pages, HTMX partials and JSON endpoints.

Each case makes one record in Beta and requests it as
- the admin of Gamma (a company outside the group): 404;
- the group admin while working in Alpha (they may see Beta, but are not working in it): 404;
- the admin of Beta (control): not 404, so the case really reaches the record.
"""
from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal

import pytest

from hajiri.db import models as m
from tests.web.factories import htmx, make_employee


PDF = b'%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n'


def _file(w):
    from hajiri.files.store import check_file, save_file
    row = save_file(company_id=str(w.beta.id), purpose='document', filename='contract.pdf',
                    file=check_file('application/pdf', PDF), uploaded_by=w.admin_b.id)
    return m.StoredFile.objects.get(pk=row['id'])


def employee(w):
    return make_employee(w.beta, name='Beta Person')


def leave_request(w):
    e = make_employee(w.beta)
    lt = m.LeaveType.objects.for_company(w.beta.id).first()
    return m.LeaveRequest.objects.create(employee=e, leave_type=lt, from_date=date(2026, 11, 2), to_date=date(2026, 11, 2),
                                         days_requested=Decimal('1'))


def leave_type(w):
    return m.LeaveType.objects.for_company(w.beta.id).first()


def payroll_run(w):
    period = m.PayrollPeriod.objects.create(company=w.beta, bs_year=2083, bs_month=7, start_ad=date(2026, 10, 18),
                                            end_ad=date(2026, 11, 16), days=30)
    return m.PayrollRun.objects.create(company=w.beta, period=period)


def document(w):
    cat = (m.DocumentCategory.objects.for_company(w.beta.id).first()
           or m.DocumentCategory.objects.create(company=w.beta, code='OTHER', name='Other'))
    return m.EmployeeDocument.objects.create(company=w.beta, employee_id=w.admin_b.employee_id, category=cat,
                                             title='Contract', file=_file(w))


def document_category(w):
    return (m.DocumentCategory.objects.for_company(w.beta.id).first()
            or m.DocumentCategory.objects.create(company=w.beta, code='OTHER', name='Other'))


def stored_file(w):
    return _file(w)


def hiring_job(w):
    return m.HiringJob.objects.create(company=w.beta, title='Accountant')


def hiring_application(w):
    job = hiring_job(w)
    cand = m.HiringCandidate.objects.create(company=w.beta, name='Ram Candidate', email='ram@cand.test')
    return m.HiringApplication.objects.create(company=w.beta, job=job, candidate=cand)


def expense_claim(w):
    return m.ExpenseClaim.objects.create(company=w.beta, employee_id=w.admin_b.employee_id, title='Taxi')


def helpdesk_ticket(w):
    from hajiri.core import auth
    from hajiri.helpdesk import services as hd
    _, admin_b = w.session(w.admin_b)
    t = hd.create_ticket(admin_b, {'categoryId': hd.list_categories(admin_b)[0]['id'], 'subject': 'Beta question',
                                   'body': 'Where is my letter?'})
    assert auth  # keeps the import used
    return m.HelpdeskTicket.objects.get(pk=t['id'])


def task_board(w):
    from hajiri.tasks.services import DEFAULT_COLUMNS
    return m.TaskBoard.objects.create(company=w.beta, kind='team', name='Beta launch', owner_user_id=w.admin_b.id,
                                      columns=DEFAULT_COLUMNS)


def approval_flow(w):
    return m.ApprovalFlow.objects.create(company=w.beta, object_type='leave_request', name='Beta leave flow',
                                         steps=[{'kind': 'manager'}])


def workflow_instance(w):
    lr = leave_request(w)
    return m.WorkflowInstance.objects.create(company=w.beta, object_type='leave_request', object_id=lr.id,
                                             steps=[{'kind': 'role', 'role': 'hr'}])


def shift(w):
    return m.Shift.objects.create(company=w.beta, code='BETA-DAY', name_en='Beta day', start_time=time(9), end_time=time(17))


def holiday(w):
    return m.Holiday.objects.create(company=w.beta, local_date=date(2026, 12, 25), name_en='Beta day off')


def device(w):
    return m.Device.objects.create(company=w.beta, serial_no='BETA-ZK-1', vendor='zkteco', protocol='adms')


def announcement(w):
    return m.Announcement.objects.create(company=w.beta, author_id=w.admin_b.id, title='Beta news', body='Hello Beta')


def perf_goal(w):
    return m.PerfGoal.objects.create(company=w.beta, scope='company', title='Grow Beta')


def perf_kpi(w):
    return m.PerfKpi.objects.create(company=w.beta, title='Orders a day')


def course(w):
    return m.LrnCourse.objects.create(company=w.beta, code='SAFE', title='Food safety')


def lifecycle_case(w):
    return m.LifecycleCase.objects.create(company=w.beta, employee=make_employee(w.beta), kind='onboarding',
                                          anchor_date=date.today() + timedelta(days=7))


def time_project(w):
    return m.TimeProject.objects.create(company=w.beta, code='BETA', name='Beta project')


def location(w):
    return m.Location.objects.create(company=w.beta, name='Beta warehouse', lat=Decimal('27.7'), lng=Decimal('85.3'))


def beta_user(w):
    class Ref:
        id = w.admin_b.id
    return Ref


def letter(w):
    return m.IssuedLetter.objects.create(company=w.beta, employee_id=w.admin_b.employee_id, template_code='EXP',
                                         ref_no='BETA/1', subject='Experience letter', html='<p>Letter</p>')


def admin_b_employee(w):
    return m.Employee.objects.get(pk=w.admin_b.employee_id)


# (name, method, url with {id}, maker, htmx?[, POST data])
CASES = [
    ('document file api', 'get', '/api/v1/documents/{id}/file', document, False),
    ('letter html api', 'get', '/api/v1/documents/letters/{id}/html', letter, False),
    ('helpdesk ticket api', 'get', '/api/v1/helpdesk/tickets/{id}', helpdesk_ticket, False),
    ('workflow api', 'get', '/api/v1/workflow/{id}', workflow_instance, False),
    ('leave type api', 'patch', '/api/v1/leave/types/{id}', leave_type, False),
    ('leave cancel api', 'post_json', '/api/v1/leave/requests/{id}/cancel', leave_request, False),
]


def _call(client, method, url, hx, data=None):
    headers = htmx() if hx else {}
    if method == 'post':
        return client.post(url, data or {}, **headers)
    if method == 'post_json':
        return client.post(url, data or {}, content_type='application/json', **headers)
    if method == 'patch':
        return client.patch(url, {'nameEn': 'Renamed'}, content_type='application/json', **headers)
    return getattr(client, method)(url, **headers)


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_ids_of_another_company_are_not_found(world, case):
    name, method, url, maker, hx, *rest = case
    obj = maker(world)
    target = url.format(id=obj.id)
    data = {k: v.format(emp=world.admin_b.employee_id) for k, v in (rest[0] if rest else {}).items()}
    outsider = world.signed_in(world.admin_c)
    r = _call(outsider, method, target, hx, data)
    assert r.status_code == 404, f'{name}: outsider got {r.status_code}'
    group_admin_in_alpha = world.signed_in(world.admin_a)
    r = _call(group_admin_in_alpha, method, target, hx, data)
    assert r.status_code == 404, f'{name}: group admin working in Alpha got {r.status_code}'
    owner = world.signed_in(world.admin_b)
    r = _call(owner, method, target, hx, data)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_after_switching_the_record_is_found(world):
    ticket = helpdesk_ticket(world)
    c = world.signed_in(world.admin_a)
    assert c.get(f'/api/v1/helpdesk/tickets/{ticket.id}').status_code == 404
    c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type='application/json')
    assert c.get(f'/api/v1/helpdesk/tickets/{ticket.id}').status_code == 200


def test_lists_show_only_the_active_company(world):
    helpdesk_ticket(world)
    m.Employee.objects.filter(company=world.beta).update(name_en='Zed Betaperson')
    c = world.signed_in(world.admin_a)
    assert len(c.get('/api/v1/helpdesk/tickets').json()['tickets']) == 0
    page = c.get('/api/v1/employees')
    assert page.status_code == 200 and b'Zed Betaperson' not in page.content
    c.post('/api/v1/me/companies/switch', {'companyId': str(world.beta.id)}, content_type='application/json')
    assert len(c.get('/api/v1/helpdesk/tickets').json()['tickets']) == 1
    assert b'Zed Betaperson' in c.get('/api/v1/employees').content
