"""
Groups of companies: lifecycle pages treat checklists, tasks, templates, assets, exits and employees of another
company as not found; HR work in a sister company reaches the group's HR people.
"""
from datetime import timedelta

import pytest

from hajiri.core.clock import npt_today
from hajiri.db.models import NotificationOutbox
from hajiri.lifecycle import api
from tests.people.group import build_group
from tests.web.factories import htmx, make_employee


@pytest.fixture
def g(db):
    return build_group()


def plus(n):
    return (npt_today() + timedelta(days=n)).isoformat()


@pytest.fixture
def beta(g):
    b = g.session(g.admin_b)
    api.ensure_defaults(b)
    case_id = api.start_onboarding(b, {'employeeId': str(g.beta_emp.id), 'anchorDate': plus(3)})
    task_id = api.case_detail(b, case_id)['tasks'][0]['id']
    template_id = api.templates(b, 'onboarding')[0]['id']
    asset_id = api.create_asset(b, {'tag': 'LAP-1', 'name': 'Laptop'})
    leaver = make_employee(g.beta, code='B200', name='Leela Leaving')
    exit_id = api.create_exit(b, {'employeeId': str(leaver.id), 'kind': 'resignation', 'lastWorkingDay': plus(10)})
    return {'case': case_id, 'task': task_id, 'template': template_id, 'asset': asset_id, 'exit': exit_id,
            'employee': str(g.beta_emp.id)}


def _requests(x):
    c, t, tp, a, xi, e = x['case'], x['task'], x['template'], x['asset'], x['exit'], x['employee']
    return [
        ('get', f'/_/lifecycle/cases/{c}', None), ('post', f'/_/lifecycle/cases/{c}/tasks', {'title': 'More'}),
        ('post', f'/_/lifecycle/tasks/{t}', {'status': 'done'}), ('get', f'/_/lifecycle/tasks/{t}/comments', None),
        ('get', f'/_/lifecycle/probation/{e}/review', None), ('get', f'/_/lifecycle/probation/{e}/confirm', None),
        ('get', f'/_/lifecycle/templates/onboarding/{tp}', None),
        ('get', f'/_/lifecycle/assets/{a}/assign', None), ('get', f'/_/lifecycle/assets/{a}/return', None),
        ('post', f'/_/lifecycle/assets/{a}/assign', {'employee_id': e}),
        ('get', f'/_/lifecycle/exits/{xi}', None), ('get', f'/_/lifecycle/exits/{xi}/cancel', None),
        ('post', f'/_/lifecycle/exits/{xi}/update', {'lastWorkingDay': plus(12)}),
        ('get', f'/_/lifecycle/employees/{e}', None), ('post', f'/_/lifecycle/cases/{c}/cancel', {}),
    ]


def _call(c, method, url, data):
    return c.post(url, data or {}, **htmx()) if method == 'post' else c.get(url, **htmx())


def test_records_of_another_company_are_not_found(g, beta):
    for person in (g.admin_c, g.admin_a):
        c = g.client(person)
        for method, url, data in _requests(beta):
            assert _call(c, method, url, data).status_code == 404, (person.email, url)
    owner = g.client(g.admin_b)
    for method, url, data in _requests(beta):
        assert _call(owner, method, url, data).status_code != 404, url


def test_lists_show_the_active_company_only(g, beta):
    a = g.session(g.admin_a)
    assert api.cases(a) == [] and api.exits(a) == [] and api.assets(a, {}) == []
    assert all(t['company_id'] == str(g.alpha.id) for t in api.templates(a))
    in_beta = g.session(g.admin_a, g.beta)
    assert [c['id'] for c in api.cases(in_beta, kind='onboarding')] == [beta['case']]
    assert [x['id'] for x in api.exits(in_beta)] == [beta['exit']]


def test_group_hr_gets_hr_tasks_of_a_sister_company(g, beta):
    """Starting a checklist in Beta tells everyone with HR there: Beta's admin and the group's HR (group_hr)."""
    started = NotificationOutbox.objects.filter(company_id=g.beta.id, template='lifecycle.onboarding.started')
    assert started.filter(user_id=g.ghr.id).exists()
    # The group HR person, working in Alpha, sees Alpha's (empty) joiners, not Beta's.
    ghr_alpha = g.session(g.ghr, g.alpha)
    assert api.cases(ghr_alpha) == []
    ghr_beta = g.session(g.ghr, g.beta)
    assert [c['id'] for c in api.cases(ghr_beta, kind='onboarding')] == [beta['case']]


def test_member_manager_sees_their_beta_joiner(g, beta):
    """Dipak manages Bina in Beta through his membership: the manager task of her checklist is his there."""
    in_beta = g.session(g.dual, g.beta)
    mine = api.my_tasks(in_beta)
    assert mine and all(t['employee_id'] == str(g.beta_emp.id) for t in mine)
    assert api.my_tasks(g.session(g.dual)) == []


def test_asset_register_filtered_by_a_sister_company_employee_is_not_found(g, beta):
    """?employeeId= of another company's employee is 404 (not 403, not an empty list), for HR and for a manager."""
    from hajiri.core.errors import NotFoundError
    for person in (g.session(g.admin_a), g.session(g.dual)):
        with pytest.raises(NotFoundError):
            api.assets(person, {'employeeId': beta['employee']})
    # In Beta, Dipak manages Bina: her assets are his to see.
    assert api.assets(g.session(g.dual, g.beta), {'employeeId': beta['employee']}) == []
