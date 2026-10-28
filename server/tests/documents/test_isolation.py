"""Documents & letters in a group of companies: every id of another company is not found (pages, HTMX actions and the
JSON API), lists show only the active company's records, and people who work in two companies see the right ones."""
from __future__ import annotations

import pytest

from hajiri.db.models import NotificationOutbox
from hajiri.documents import services as docs
from tests.multicompany.conftest import World, world  # noqa: F401  (the group-of-companies fixture)
from tests.web.factories import bearer, htmx, make_employee, make_user

from .world import upload

pytestmark = pytest.mark.django_db


def beta_records(w: World) -> dict:
    """One record of each kind in Beta, made by Beta's admin working there."""
    _, hr = w.session(w.admin_b)
    sita = make_employee(w.beta, name='Sita at Beta')
    sita_user = make_user(w.beta, roles=['employee'], employee=sita, email='sita@beta.test')
    _, sita_u = w.session(sita_user)
    cat = next(c['id'] for c in docs.list_categories(hr) if c['code'] == 'citizenship')
    doc = docs.add_document(sita_u, str(sita.id), {'categoryId': cat, 'title': 'Citizenship', 'fileId': upload(sita_u)})['id']
    tpl = next(t['id'] for t in docs.list_templates(hr) if t['code'] == 'to_whom_it_may_concern')
    req = docs.request_letter(sita_u, {'templateId': tpl, 'purpose': 'visa application'})['id']
    letter = docs.issue_letter(hr, {'templateId': tpl, 'employeeId': str(sita.id), 'allowMissing': True})['id']
    policy = docs.publish_policy(hr, {'title': 'Beta handbook', 'fileId': upload(hr, 'company_document')})['id']
    return {'doc': doc, 'category': cat, 'template': tpl, 'request': req, 'letter': letter, 'policy': policy,
            'employee': str(sita.id), 'sita': sita_user}


# (name, method, url, htmx?, data)
PAGES = [
    ('documents tab', 'get', '/_/documents/employee/{employee}', True, None),
    ('document edit form', 'get', '/_/documents/docs/{doc}/edit', True, None),
    ('document edit', 'post', '/_/documents/docs/{doc}/edit', True, {'title': 'X'}),
    ('document verify', 'post', '/_/documents/docs/{doc}/verify', True, None),
    ('document reject form', 'get', '/_/documents/docs/{doc}/reject?from=review', True, None),
    ('document reject', 'post', '/_/documents/docs/{doc}/reject', True, {'reason': 'Blurry'}),
    ('document delete', 'post', '/_/documents/docs/{doc}/delete', True, None),
    ('document file', 'get', '/_/documents/docs/{doc}/file', False, None),
    ('new version form', 'get', '/_/documents/docs/upload?mode=hr&replaces={doc}', True, None),
    ('category toggle', 'post', '/_/documents/categories/{category}/toggle', True, {'key': 'active'}),
    ('template edit form', 'get', '/_/documents/templates/{template}', True, None),
    ('template edit', 'post', '/_/documents/templates/{template}', True, {'name': 'X', 'subject': 'S', 'body': 'B'}),
    ('issue for a request', 'get', '/_/documents/letters/issue?request_id={request}', True, None),
    ('letter html', 'get', '/_/documents/letters/{letter}/html', False, None),
    ('void form', 'get', '/_/documents/letters/{letter}/void', True, None),
    ('void', 'post', '/_/documents/letters/{letter}/void', True, {'reason': 'wrong date'}),
    ('request cancel', 'post', '/_/documents/requests/{request}/cancel', True, None),
    ('policy acks', 'get', '/_/documents/policies/{policy}/acks', True, None),
    ('policy remind', 'post', '/_/documents/policies/{policy}/remind', True, None),
    ('policy archive', 'post', '/_/documents/policies/{policy}/archive', True, None),
    ('policy file', 'get', '/_/documents/policies/{policy}/file', False, None),
    ('policy acknowledge', 'post', '/_/documents/policies/{policy}/acknowledge', True, None),
]

API = [
    ('employee documents', 'get', '/api/v1/documents/employee/{employee}', None),
    ('document file', 'get', '/api/v1/documents/{doc}/file', None),
    ('document patch', 'patch', '/api/v1/documents/{doc}', {'title': 'X'}),
    ('document delete', 'delete', '/api/v1/documents/{doc}', None),
    ('document verify', 'post', '/api/v1/documents/{doc}/verify', {'decision': 'verify'}),
    ('category patch', 'patch', '/api/v1/documents/categories/{category}', {'name': 'X'}),
    ('template patch', 'patch', '/api/v1/documents/templates/{template}', {'name': 'X'}),
    ('template preview', 'post', '/api/v1/documents/templates/preview', {'employeeId': '{employee}', 'templateId': '{template}'}),
    ('issue', 'post', '/api/v1/documents/letters', {'templateId': '{template}', 'employeeId': '{employee}', 'allowMissing': True}),
    ('letter html', 'get', '/api/v1/documents/letters/{letter}/html', None),
    ('void', 'post', '/api/v1/documents/letters/{letter}/void', {'reason': 'wrong date'}),
    ('request cancel', 'post', '/api/v1/documents/letter-requests/{request}/cancel', None),
    ('policy patch', 'patch', '/api/v1/documents/policies/{policy}', {'archived': True}),
    ('policy file', 'get', '/api/v1/documents/policies/{policy}/file', None),
    ('policy acks', 'get', '/api/v1/documents/policies/{policy}/acks', None),
    ('policy remind', 'post', '/api/v1/documents/policies/{policy}/remind', None),
]


def _page(client, method, url, hx, data):
    headers = htmx() if hx else {}
    return client.post(url, data or {}, **headers) if method == 'post' else client.get(url, **headers)


def _api(client, token, method, url, data):
    if method == 'get':
        return client.get(url, **bearer(token))
    return getattr(client, method)(url, data or {}, content_type='application/json', **bearer(token))


def _fill(v, ids):
    if isinstance(v, str):
        return v.format(**ids)
    if isinstance(v, dict):
        return {k: _fill(x, ids) for k, x in v.items()}
    return v


@pytest.mark.parametrize('case', PAGES, ids=[c[0] for c in PAGES])
def test_pages_answer_not_found_for_another_companys_ids(world, case):
    name, method, url, hx, data = case
    ids = beta_records(world)
    target, body = _fill(url, ids), _fill(data, ids)
    for person in (world.admin_c, world.admin_a, world.dual):
        r = _page(world.signed_in(person), method, target, hx, body)
        assert r.status_code == 404, f'{name}: {person.email} got {r.status_code}'
    # Beta's own admin is not refused as "not found" (the control).
    r = _page(world.signed_in(world.admin_b), method, target, hx, body)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code}'


@pytest.mark.parametrize('case', API, ids=[c[0] for c in API])
def test_api_answers_not_found_for_another_companys_ids(world, case):
    from django.test import Client
    name, method, url, data = case
    ids = beta_records(world)
    target, body = _fill(url, ids), _fill(data, ids)
    for person in (world.admin_c, world.admin_a):
        token, _ = world.session(person)
        r = _api(Client(), token, method, target, body)
        assert r.status_code == 404, f'{name}: {person.email} got {r.status_code}'
    token, _ = world.session(world.admin_b)
    r = _api(Client(), token, method, target, body)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} {r.content[:200]}'


def test_group_admin_finds_beta_records_after_switching(world):
    ids = beta_records(world)
    c = world.signed_in(world.admin_a, world.beta)
    assert c.get(f"/_/documents/docs/{ids['doc']}/edit", **htmx()).status_code == 200
    assert c.get(f"/_/documents/letters/{ids['letter']}/html").status_code == 200
    token, user = world.session(world.admin_a, world.beta)
    assert ids['doc'] in [d['id'] for d in docs.queue(user)['pending']]


def test_lists_show_only_the_active_companys_records(world):
    ids = beta_records(world)
    _, in_alpha = world.session(world.admin_a)
    assert ids['doc'] not in [d['id'] for d in docs.queue(in_alpha)['pending']]
    assert ids['letter'] not in [x['id'] for x in docs.list_letters(in_alpha)]
    assert ids['request'] not in [x['id'] for x in docs.list_requests(in_alpha)]
    assert ids['policy'] not in [x['id'] for x in docs.list_policies(in_alpha)]
    assert ids['template'] not in [x['id'] for x in docs.list_templates(in_alpha)]
    assert ids['category'] not in [x['id'] for x in docs.list_categories(in_alpha)]
    html = world.signed_in(world.admin_a).get('/employees/documents?section=letters').content.decode()
    assert 'Sita at Beta' not in html
    _, in_beta = world.session(world.admin_a, world.beta)
    assert ids['letter'] in [x['id'] for x in docs.list_letters(in_beta)]
    # Through the JSON API as well.
    from django.test import Client
    token, _ = world.session(world.admin_a)
    r = Client().get('/api/v1/documents/letters', **bearer(token))
    assert r.status_code == 200 and ids['letter'] not in [x['id'] for x in r.json()['letters']]


def test_a_person_in_two_companies_sees_the_documents_of_the_active_one(world):
    """Dipak is HR at Alpha (home) and an employee at Beta: his own documents follow the active company."""
    _, at_alpha = world.session(world.dual)
    _, at_beta = world.session(world.dual, world.beta)
    beta_eid = str(world.dual_beta_employee.id)
    assert at_beta.employee_id == beta_eid
    cat = next(c['id'] for c in docs.list_categories(at_beta) if c['code'] == 'pan_card')
    doc = docs.add_document(at_beta, beta_eid, {'categoryId': cat, 'title': 'PAN card', 'fileId': upload(at_beta)})['id']
    assert [d['id'] for d in docs.my_documents(at_beta)] == [doc]
    assert docs.my_documents(at_alpha) == []
    # At Beta he is an employee there: no HR queue, and his upload waits for Beta's HR.
    with pytest.raises(Exception) as e:
        docs.queue(at_beta)
    assert e.value.status == 403
    pending = docs.queue(world.session(world.admin_b)[1])['pending']
    assert doc in [d['id'] for d in pending] and pending[0]['status'] == 'pending'
    # Uploading for an Alpha employee while working in Beta is not possible.
    alpha_emp = make_employee(world.alpha, name='Alpha person')
    assert _status(docs.employee_documents, at_beta, str(alpha_emp.id)) == 403


def test_group_hr_works_as_hr_in_each_company_and_gets_its_notices(world):
    ids = beta_records(world)
    _, ghr_beta = world.session(world.ghr)                      # home company Beta
    assert ids['doc'] in [d['id'] for d in docs.queue(ghr_beta)['pending']]
    # Group HR is told about uploads to verify in Beta.
    assert NotificationOutbox.objects.filter(template='document.uploaded', user_id=world.ghr.id,
                                             company_id=world.beta.id).exists()
    # And acts as HR in Alpha after switching, with Alpha's records only.
    _, ghr_alpha = world.session(world.ghr, world.alpha)
    assert ids['doc'] not in [d['id'] for d in docs.queue(ghr_alpha)['pending']]
    assert _status(docs.verify_document, ghr_alpha, ids['doc'], {'decision': 'verify'}) == 404
    assert _status(docs.verify_document, ghr_beta, ids['doc'], {'decision': 'verify'}) == 200


def test_policy_audiences_and_letter_numbers_are_per_company(world):
    ids = beta_records(world)
    _, hr_alpha = world.session(world.admin_a)
    a = docs.publish_policy(hr_alpha, {'title': 'Alpha handbook', 'fileId': upload(hr_alpha, 'company_document')})['id']
    # Nobody in Beta is in an Alpha audience.
    beta_ids = {str(x) for x in world.beta.employees.values_list('id', flat=True)}
    assert not beta_ids & {e['employee_id'] for e in docs.policy_acks(hr_alpha, a)}
    _, sita = world.session(ids['sita'])
    assert [p['id'] for p in docs.list_policies(sita)] == [ids['policy']]
    # A branch of Beta is unknown in Alpha.
    _, hr_beta = world.session(world.admin_b)
    from hajiri.db.models import Branch
    beta_branch = Branch.objects.create(company=world.beta, code='BKT', name_en='Bhaktapur')
    assert _status(docs.publish_policy, hr_alpha, {'title': 'X', 'fileId': upload(hr_alpha, 'company_document'),
                                                   'audience': 'branch', 'audienceIds': [str(beta_branch.id)]}) == 400
    # Reference numbers count per company: Alpha's first letter is 0001 although Beta issued one already.
    alpha_emp = make_employee(world.alpha, name='Ram Alpha')
    tpl = next(t['id'] for t in docs.list_templates(hr_alpha) if t['code'] == 'to_whom_it_may_concern')
    r = docs.issue_letter(hr_alpha, {'templateId': tpl, 'employeeId': str(alpha_emp.id), 'allowMissing': True})
    assert r['refNo'].endswith('/0001')
    # A Beta template cannot be used for an Alpha letter, nor an Alpha employee for a Beta one.
    assert _status(docs.issue_letter, hr_alpha, {'templateId': ids['template'], 'employeeId': str(alpha_emp.id)}) == 404
    assert _status(docs.issue_letter, hr_beta, {'templateId': ids['template'], 'employeeId': str(alpha_emp.id),
                                                'allowMissing': True}) == 404


def _status(fn, *a, **kw) -> int:
    from hajiri.core.errors import HttpError
    try:
        fn(*a, **kw)
    except HttpError as e:
        return e.status
    return 200



def test_ids_of_another_company_inside_a_body_are_refused(world):
    """Ids posted in a body (category, document to replace, letter request, template, policy audience, employee)
    are re-read in the active company: Beta's ids do nothing in Alpha."""
    from hajiri.db.models import Branch, EmployeeDocument, LetterRequest, OrgUnit

    ids = beta_records(world)
    _, hr = world.session(world.admin_a)                  # group admin working in Alpha
    alpha_emp = make_employee(world.alpha, name='Ravi at Alpha')
    alpha_cat = next(c['id'] for c in docs.list_categories(hr) if c['code'] == 'citizenship')
    alpha_tpl = next(t['id'] for t in docs.list_templates(hr) if t['code'] == 'to_whom_it_may_concern')

    # a Beta category, or a Beta document as the one to replace
    assert _status(docs.add_document, hr, str(alpha_emp.id), {'categoryId': ids['category'], 'title': 'X',
                                                             'fileId': upload(hr)}) == 400
    assert _status(docs.add_document, hr, str(alpha_emp.id), {'categoryId': alpha_cat, 'title': 'X', 'fileId': upload(hr),
                                                             'replacesId': ids['doc']}) == 404
    assert EmployeeDocument.objects.get(pk=ids['doc']).is_current
    # Beta's employee for an Alpha document
    assert _status(docs.add_document, hr, ids['employee'], {'categoryId': alpha_cat, 'title': 'X',
                                                           'fileId': upload(hr)}) == 404
    # a Beta letter request attached to an Alpha letter
    assert _status(docs.issue_letter, hr, {'templateId': alpha_tpl, 'employeeId': str(alpha_emp.id),
                                           'requestId': ids['request'], 'allowMissing': True}) == 404
    assert LetterRequest.objects.get(pk=ids['request']).status == LetterRequest.Status.PENDING
    # a Beta template for an Alpha letter request (by an Alpha employee)
    _, solo = world.session(world.solo)
    assert _status(docs.request_letter, solo, {'templateId': ids['template'], 'purpose': 'visa application'}) == 400
    # a policy addressed to Beta's branch or department
    beta_branch = str(Branch.objects.create(company_id=world.beta.id, code='BB', name_en='Beta branch').id)
    beta_unit = str(OrgUnit.objects.create(company_id=world.beta.id, code='BU', name_en='Beta unit',
                                           kind=OrgUnit.Kind.DEPARTMENT).id)
    for audience, target in (('branch', beta_branch), ('department', beta_unit)):
        assert _status(docs.publish_policy, hr, {'title': 'P', 'fileId': upload(hr, 'company_document'),
                                                 'audience': audience, 'audienceIds': [target]}) == 400
    # Beta's employee in a preview or in the letters register filter
    assert _status(docs.preview_letter, hr, {'employeeId': ids['employee'], 'templateId': alpha_tpl}) == 404
    assert docs.list_letters(hr, {'employeeId': ids['employee']}) == []
