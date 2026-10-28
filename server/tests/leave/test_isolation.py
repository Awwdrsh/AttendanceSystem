"""
Leave in a group of companies: ids of another company behave as not found on every leave page, fragment and JSON
endpoint; lists show only the active company; people who work in two companies use the employee record and roles of
the company they are working in. Uses the group world of tests/multicompany (Alpha and Beta in a group, Gamma outside).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from hajiri.db import models as m
from hajiri.leave import services
from tests.multicompany.conftest import world  # noqa: F401  (the group world replaces the leave world here)
from tests.web.factories import htmx, make_employee


def _type(company, code='CASUAL', **kw):
    t = m.LeaveType.objects.for_company(company.id).filter(code=code).first()
    if t:
        return t
    return m.LeaveType.objects.create(company=company, code=code, name_en=kw.pop('name_en', f'{code.title()} leave'), **kw)


def _request(company, employee=None, **kw):
    e = employee or make_employee(company)
    return m.LeaveRequest.objects.create(employee=e, leave_type=_type(company), from_date=kw.pop('from_date', date(2026, 11, 2)),
                                         to_date=kw.pop('to_date', date(2026, 11, 2)), days_requested=Decimal('1'), **kw)


def _beta_employee(w):
    return m.Employee.objects.get(pk=w.admin_b.employee_id)


# (name, method, url, makes {id} and {emp}, htmx?, data)
CASES = [
    ('request detail', 'get', '/_/leave/requests/{id}', 'request', True, None),
    ('request cancel', 'post', '/_/leave/requests/{id}/cancel', 'request', True, {}),
    ('type edit form', 'get', '/_/leave/types/{id}/edit', 'type', True, None),
    ('type edit post', 'post', '/_/leave/types/{id}/edit', 'type', True, {'nameEn': 'Renamed'}),
    ('type move', 'post', '/_/leave/types/{id}/move', 'type', True, {'dir': 'up'}),
    ('apply form', 'get', '/_/leave/apply?employee_id={emp}', 'type', True, None),
    ('apply post', 'post', '/_/leave/apply', 'type', True,
     {'employee_id': '{emp}', 'leave_type_id': '{id}', 'from': '2026-11-02', 'to': '2026-11-02'}),
    ('balances panel', 'get', '/leave/balances?employee_id={emp}', 'type', False, None),
    ('adjust post', 'post', '/_/leave/adjust', 'type', True,
     {'employee_id': '{emp}', 'leave_type_id': '{id}', 'delta_days': '2', 'reason': 'opening', 'kind': 'adjustment'}),
    ('type api', 'patch', '/api/v1/leave/types/{id}', 'type', False, {'nameEn': 'Renamed'}),
    ('cancel api', 'post_json', '/api/v1/leave/requests/{id}/cancel', 'request', False, {}),
    ('balances api', 'get', '/api/v1/leave/balances?employeeId={emp}', 'type', False, None),
    ('ledger api', 'get', '/api/v1/leave/ledger?employeeId={emp}', 'type', False, None),
    ('requests api for an employee', 'get', '/api/v1/leave/requests?employeeId={emp}', 'type', False, None),
    ('preview api', 'get', '/api/v1/leave/preview?employeeId={emp}&leaveTypeId={id}&from=2026-11-02&to=2026-11-02', 'type', False,
     None),
    ('request api', 'post_json', '/api/v1/leave/requests', 'type', False,
     {'employeeId': '{emp}', 'leaveTypeId': '{id}', 'from': '2026-11-02', 'to': '2026-11-02'}),
    ('adjust api', 'post_json', '/api/v1/leave/adjust', 'type', False,
     {'employeeId': '{emp}', 'leaveTypeId': '{id}', 'deltaDays': 2, 'reason': 'opening'}),
]


def _fill(v, ids):
    if isinstance(v, str):
        return v.format(**ids)
    if isinstance(v, dict):
        return {k: _fill(x, ids) for k, x in v.items()}
    return v


def _call(client, method, url, hx, data):
    headers = htmx() if hx else {}
    if method == 'post':
        return client.post(url, data or {}, **headers)
    if method == 'post_json':
        return client.post(url, data or {}, content_type='application/json', **headers)
    if method == 'patch':
        return client.patch(url, data or {}, content_type='application/json', **headers)
    return client.get(url, **headers)


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_leave_ids_of_another_company_are_not_found(world, case):  # noqa: F811
    name, method, url, makes, hx, data = case
    emp = _beta_employee(world)
    obj = _request(world.beta, emp) if makes == 'request' else _type(world.beta)
    ids = {'id': obj.id, 'emp': emp.id}
    target, body = _fill(url, ids), _fill(data, ids)
    for who, client in (('outsider', world.signed_in(world.admin_c)), ('group admin in Alpha', world.signed_in(world.admin_a)),
                        ('group HR in Alpha', world.signed_in(world.ghr, world.alpha))):
        r = _call(client, method, target, hx, body)
        assert r.status_code == 404, f'{name}: {who} got {r.status_code} {r.content[:200]!r}'
    r = _call(world.signed_in(world.admin_b), method, target, hx, body)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control) {r.content[:300]!r}'


def test_the_preview_of_another_company_shows_nothing(world):  # noqa: F811
    t, emp = _type(world.beta), _beta_employee(world)
    q = {'employee_id': str(emp.id), 'leave_type_id': str(t.id), 'from': '2026-11-02', 'to': '2026-11-02'}
    assert world.signed_in(world.admin_a).get('/_/leave/preview', q, **htmx()).content == b''
    own = world.signed_in(world.admin_b).get('/_/leave/preview', q, **htmx())
    assert b'will be deducted' in own.content


def test_reordering_with_a_type_of_another_company_is_refused(world):  # noqa: F811
    mine, theirs = _type(world.alpha), _type(world.beta)
    c = world.signed_in(world.admin_a)
    r = c.post('/api/v1/leave/types/order', {'ids': [str(mine.id), str(theirs.id)]}, content_type='application/json')
    assert r.status_code == 400 and r.json()['error'] == 'a leave type in the list does not exist'
    m.LeaveType.objects.filter(pk=theirs.pk, sort_order=theirs.sort_order).get()


def test_clubbing_with_a_type_of_another_company_is_refused(world):  # noqa: F811
    theirs = _type(world.beta)
    c = world.signed_in(world.admin_a)
    r = c.post('/api/v1/leave/types', {'code': 'XCLUB', 'nameEn': 'Clubbed', 'clubWithId': str(theirs.id)},
               content_type='application/json')
    assert r.status_code == 400 and r.json()['error'] == 'the leave type to club with does not exist'
    assert not m.LeaveType.objects.filter(code='XCLUB').exists()
    mine = _type(world.alpha)
    r = c.patch(f'/api/v1/leave/types/{mine.id}', {'clubWithId': str(theirs.id)}, content_type='application/json')
    assert r.status_code == 400 and r.json()['error'] == 'the leave type to club with does not exist'
    assert m.LeaveType.objects.get(pk=mine.pk).club_with_id is None


def test_lists_show_only_the_active_company(world):  # noqa: F811
    _type(world.alpha)
    beta_request = _request(world.beta, _beta_employee(world), status=m.LeaveRequest.Status.APPROVED)
    c = world.signed_in(world.admin_a)
    assert c.get('/api/v1/leave/requests').json()['requests'] == []
    types = c.get('/api/v1/leave/types').json()['types']
    assert types and {t['company_id'] for t in types} == {str(world.alpha.id)}
    cal = c.get('/api/v1/leave/team-calendar', {'from': '2026-11-01', 'to': '2026-11-30'}).json()['days']
    assert cal == []
    page = c.get('/leave', {'y': 2083, 'm': 7})
    assert page.status_code == 200 and b'Bimal' not in page.content
    # After switching to Beta the same person sees Beta's request.
    c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx())
    listed = c.get('/api/v1/leave/requests').json()['requests']
    assert [r['id'] for r in listed] == [str(beta_request.id)]
    cal = c.get('/api/v1/leave/team-calendar', {'from': '2026-11-01', 'to': '2026-11-30'}).json()['days']
    assert cal and cal[0]['off'][0]['employeeId'] == str(beta_request.employee_id)


def test_two_company_person_uses_the_active_company_employee_record(world):  # noqa: F811
    """Dipak is HR at Alpha (home) and an employee at Beta: Time off in Beta shows the Beta record's requests, and a
    request filed there is for the Beta employee."""
    beta_type = _type(world.beta, 'LWP', name_en='Leave without pay', paid=False)
    alpha_emp = m.Employee.objects.get(pk=world.dual.employee_id)
    _request(world.alpha, alpha_emp)
    c = world.signed_in(world.dual, world.beta)
    assert c.get('/api/v1/leave/requests', {'scope': 'own'}).json()['requests'] == []
    r = c.post('/api/v1/leave/requests', {'leaveTypeId': str(beta_type.id), 'from': '2026-11-03', 'to': '2026-11-03'},
               content_type='application/json')
    assert r.status_code == 201, r.json()
    lr = m.LeaveRequest.objects.get(pk=r.json()['id'])
    assert lr.employee_id == world.dual_beta_employee.id
    mine = c.get('/api/v1/leave/requests', {'scope': 'own'}).json()['requests']
    assert [x['id'] for x in mine] == [r.json()['id']]
    # The decision notice reaches the person through their Beta membership.
    assert services.user_id_for_employee(str(world.dual_beta_employee.id)) == str(world.dual.id)


def test_group_hr_works_on_leave_in_every_company_of_the_group(world):  # noqa: F811
    t, emp = _type(world.beta), _beta_employee(world)
    alpha_type, alpha_emp = _type(world.alpha), m.Employee.objects.get(pk=world.solo.employee_id)
    c = world.signed_in(world.ghr, world.alpha)
    r = c.post('/api/v1/leave/adjust', {'employeeId': str(alpha_emp.id), 'leaveTypeId': str(alpha_type.id), 'deltaDays': 3,
                                        'reason': 'opening'}, content_type='application/json')
    assert r.status_code == 201 and r.json()['balance'] == 3
    c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx())
    r = c.post('/api/v1/leave/adjust', {'employeeId': str(emp.id), 'leaveTypeId': str(t.id), 'deltaDays': 2, 'reason': 'opening'},
               content_type='application/json')
    assert r.status_code == 201 and r.json()['balance'] == 2
    # Group executive: read-only executive, not the attendance area, so no adjustments.
    ex = world.signed_in(world.gexec, world.beta)
    r = ex.post('/api/v1/leave/adjust', {'employeeId': str(emp.id), 'leaveTypeId': str(t.id), 'deltaDays': 2, 'reason': 'opening'},
                content_type='application/json')
    assert r.status_code == 403
