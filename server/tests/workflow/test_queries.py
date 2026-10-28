"""
Query budgets of the list pages (the inbox, the request history and the chain check must not run a query per card or
per request type), the messages of the page buttons, and approvals waiting in a person's other companies.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import EmployeeAssignment
from hajiri.workflow import service
from tests.multicompany.conftest import world  # noqa: F401  (fixture)
from tests.web.factories import htmx, make_employee, toasts

from .helpers import file_leave
from .test_multicompany import _start, beta_request, letters  # noqa: F401  (fixtures)
from .test_views import client_for

pytestmark = pytest.mark.django_db


@pytest.fixture
def w(demo, login_as, leave_handler):
    EmployeeAssignment.objects.filter(employee_id=demo['sita']).update(manager_id=demo['ram'])
    return {'demo': demo, 'c': demo['company_id'], 'hr': login_as(['hr']), 'ram': login_as(['manager'], demo['ram'])}


def _queries(client, url) -> int:
    with CaptureQueriesContext(connection) as q:
        r = client.get(url)
    assert r.status_code == 200, r.content[:300]
    return len(q)


def _file(w, n, start):
    for i in range(n):
        file_leave(w['c'], w['demo']['sita'], (start + timedelta(days=i)).isoformat(), reason=f'day {i}')


@pytest.mark.parametrize('who', ['ram', 'hr'])
def test_inbox_queries_do_not_grow_with_the_number_of_cards(w, who):
    c = client_for(w[who])
    _file(w, 2, date(2026, 12, 1))
    few = _queries(c, '/approvals')
    _file(w, 6, date(2026, 12, 10))
    many = _queries(c, '/approvals')
    assert many <= few          # the first page view also stamps the session
    assert len(service.inbox_for(w[who].user)) == 8


def test_inbox_cards_read_in_one_go_match_the_single_summary(w):
    _file(w, 3, date(2026, 12, 1))
    items = service.inbox_for(w['ram'].user)
    for i in items:
        inst = service.get_instance(i['instanceId'])
        assert i['summary'] == service.summarise(inst)
    # the other two days of Sita's own requests are not "colleagues off"
    assert all(i['summary']['teamOverlap'] == [] and i['summary']['days'] == 1 for i in items)


def test_request_history_queries_do_not_grow_with_rows(w):
    c = client_for(w['hr'])
    _file(w, 2, date(2026, 12, 1))
    few = _queries(c, '/leave/history?tab=leave&status=pending')
    _file(w, 6, date(2026, 12, 10))
    assert _queries(c, "/leave/history?tab=leave&status=pending") <= few
    html = c.get('/leave/history?tab=leave&status=pending').content.decode()
    assert 'Sita Sharma' in html


def test_chain_check_reads_the_flows_once(w):
    c = client_for(w['hr'])
    with CaptureQueriesContext(connection) as q:
        r = c.get('/_/workflow/verify', **htmx('verify'))
    assert r.status_code == 200
    reads = [x['sql'] for x in q.captured_queries if 'FROM "approval_flow"' in x['sql']]
    assert len(reads) == 1


def test_page_buttons_keep_their_messages(w):
    f = file_leave(w['c'], w['demo']['sita'], '2026-12-20')
    c = client_for(w['ram'])
    url = f"/_/workflow/{f['instanceId']}/approve"
    for raw, msg in (('abc', 'Expected number, received NaN'), ('1.5', 'Expected integer, received float'),
                     ('300', 'Too big: expected number to be <=240')):
        r = c.post(url, {'approved_min': raw}, **htmx())
        assert r.status_code == 400 and toasts(r)[0]['message'] == msg
    r = c.post(url, {'on_behalf_of': 'nope'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Invalid UUID'
    r = c.post(url, {'reason': 'x' * 1001}, **htmx())
    assert toasts(r)[0]['message'] == 'reason: Too big: expected string to have <=1000 characters'
    r = c.post('/_/workflow/bulk-approve', {}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'Too small: expected array to have >=1 items'
    r = c.post('/_/workflow/bulk-approve', {'ids': ['nope']}, **htmx())
    assert toasts(r)[0]['message'] == '0: Invalid UUID'
    assert service.get_instance(f['instanceId'])['state'] == 'pending'


# ---------------------------------------------------------------- group of companies

def test_approvals_waiting_in_my_other_companies(world, beta_request):
    """Dipak works in Alpha (HR); his Beta record is the line manager of a waiting Beta request."""
    _t, dual_alpha = world.session(world.dual)
    assert service.inbox_for(dual_alpha) == []
    assert service.inbox_elsewhere(dual_alpha) == [{'companyId': str(world.beta.id), 'name': 'Beta Foods', 'count': 1}]
    # in Beta it is in the inbox itself, and nothing waits back in Alpha
    _t, dual_beta = world.session(world.dual, world.beta)
    assert len(service.inbox_for(dual_beta)) == 1 and service.inbox_elsewhere(dual_beta) == []
    # the approvals page in Alpha says so
    r = world.signed_in(world.dual).get('/approvals')
    assert r.status_code == 200 and r.context['elsewhere'][0]['count'] == 1
    # outside the group, or with no access to Beta, nothing is counted
    for person in (world.admin_c, world.solo):
        _t, u = world.session(person)
        assert service.inbox_elsewhere(u) == []


def test_other_company_counts_use_my_roles_there(world, letters):
    """An HR step in Beta: Dipak is HR at home in Alpha but only manager/employee in Beta, so it is not his to decide."""
    worker = make_employee(world.beta, name='Bina Worker')
    _start(world.beta, worker, [{'kind': 'role', 'role': 'hr'}])
    _t, dual_alpha = world.session(world.dual)
    assert service.inbox_elsewhere(dual_alpha) == []
    # group HR decides HR steps in every company of the group: counted for Beta while working in Alpha
    _t, ghr_alpha = world.session(world.ghr, world.alpha)
    assert service.inbox_elsewhere(ghr_alpha) == [{'companyId': str(world.beta.id), 'name': 'Beta Foods', 'count': 1}]
