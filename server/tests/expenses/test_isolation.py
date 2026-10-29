"""
Expenses in a group of companies: every page, fragment and JSON endpoint reads the ACTIVE company only. A claim,
trip, settlement, bill or category of the sister company is 'not found' (404) for HR of the other company and for a
person who may switch to the sister company but is still working in the first; after switching it opens.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from django.test import Client

from hajiri.core import auth
from hajiri.db.models import CompanyMembership, ExpensePayout, TravelBill, TravelRequest
from hajiri.expenses import services as ex
from hajiri.expenses.seeds import seed_expense_defaults
from tests.web.factories import PW, bearer, make_company, make_employee, make_user, sign_in

from .helpers import approve_both, days_ago, days_ahead, today, upload


@dataclass
class Group:
    alpha: object
    beta: object
    alpha_hr: object      # HR of Alpha only
    dual: object          # HR at home in Alpha, HR in Beta by membership (own employee record there)
    beta_hr: object
    beta_boss: object
    beta_emp: object      # an employee of Beta who files claims and trips
    claim: str
    trip: str
    settlement: str
    bill: str
    category: str
    item: str

    def client(self, person, company=None) -> Client:
        c = Client()
        sign_in(c, person)
        if company is not None:
            r = c.post('/api/v1/me/companies/switch', {'companyId': str(company.id)}, content_type='application/json')
            assert r.status_code == 200, r.content
        return c

    def session(self, person, company=None):
        token = auth.login(person.email, PW).token
        user = auth.resolve_session(token)
        if company is not None and str(company.id) != user.company_id:
            user = auth.switch_company(user, token, str(company.id))
        return token, user


@pytest.fixture
def group(db) -> Group:
    from hajiri.db.models import Company, CompanyGroup
    grp = CompanyGroup.objects.create(code='GRP', name_en='The Group')
    alpha, beta = make_company('ALPHA', 'Alpha Traders'), make_company('BETA', 'Beta Foods')
    Company.objects.filter(pk__in=[alpha.id, beta.id]).update(group=grp)
    for co in (alpha, beta):
        seed_expense_defaults(str(co.id))
    alpha_hr = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Asha HR'), email='hr@alpha.test')
    dual = make_user(alpha, roles=['hr'], employee=make_employee(alpha, name='Dipak Dual'), email='dual@alpha.test')
    CompanyMembership.objects.create(user_id=dual.id, company=beta, roles=['hr'], employee=make_employee(beta, name='Dipak at Beta'))
    boss_emp = make_employee(beta, name='Bimal Boss')
    beta_boss = make_user(beta, roles=['manager'], employee=boss_emp, email='boss@beta.test')
    beta_hr = make_user(beta, roles=['hr'], employee=make_employee(beta, name='Bina HR'), email='hr@beta.test')
    beta_emp = make_user(beta, roles=['employee'], employee=make_employee(beta, name='Bishnu Staff', manager=boss_emp),
                         email='staff@beta.test')
    cats = {c['code']: c['id'] for c in ex.get_policy(beta_emp)['categories']}
    claim = ex.create_claim(beta_emp, {'title': 'Beta taxi', 'submit': True, 'items': [
        {'expenseDate': days_ago(1), 'categoryId': cats['MEALS'], 'description': 'Lunch', 'amount': '300'}]})['id']
    item = str(ex.claim_detail(beta_emp, claim)['items'][0]['id'])
    trip = ex.request_travel(beta_emp, {'destination': 'Pokhara', 'destinationCategory': 'city', 'purpose': 'Client visit',
                                        'fromDate': today(), 'toDate': days_ahead(1), 'mode': 'bus', 'advanceRequested': '1000'})['id']
    w = Group(alpha, beta, alpha_hr, dual, beta_hr, beta_boss, beta_emp, claim, trip, '', '', cats['MEALS'], item)
    approve_trip = lambda u, i, b: ex.review_trip(u, i, b)  # noqa: E731
    w.ram, w.hr = beta_boss, beta_hr  # approve_both reads .ram (manager) and .hr
    approve_both(approve_trip, trip, w)
    bill_file = upload(beta_emp, 'travel_bill')
    w.settlement = ex.settle_trip(beta_emp, trip, {'actualFrom': today(), 'actualTo': today(), 'bills': [
        {'kind': 'transport', 'billDate': today(), 'description': 'Bus', 'amount': '800', 'fileId': bill_file}]})['id']
    w.bill = str(TravelBill.objects.get(settlement_id=w.settlement).id)
    return w


def _api_urls(w: Group) -> list[tuple[str, str, dict]]:
    c, t, s_, b, cat, item = w.claim, w.trip, w.settlement, w.bill, w.category, w.item
    return [
        ('get', f'/api/v1/expenses/claims/{c}', {}),
        ('put', f'/api/v1/expenses/claims/{c}', {'title': 'x', 'items': [
            {'expenseDate': days_ago(1), 'categoryId': cat, 'description': 'x', 'amount': '1'}]}),
        ('post', f'/api/v1/expenses/claims/{c}/submit', {}),
        ('post', f'/api/v1/expenses/claims/{c}/cancel', {}),
        ('post', f'/api/v1/expenses/claims/{c}/review', {'decision': 'approve'}),
        ('post', f'/api/v1/expenses/claims/{c}/pay-outside', {'note': 'paid in cash'}),
        ('get', f'/api/v1/expenses/claims/{c}/items/{item}/receipt', {}),
        ('get', f'/api/v1/expenses/travel/{t}', {}),
        ('post', f'/api/v1/expenses/travel/{t}/cancel', {}),
        ('post', f'/api/v1/expenses/travel/{t}/review', {'decision': 'approve'}),
        ('post', f'/api/v1/expenses/travel/{t}/advance-outside', {'note': 'paid in cash'}),
        ('post', f'/api/v1/expenses/travel/{t}/settlement', {'actualFrom': today(), 'actualTo': today(), 'bills': []}),
        ('post', f'/api/v1/expenses/settlements/{s_}/review', {'decision': 'approve'}),
        ('get', f'/api/v1/expenses/settlements/{s_}/bills/{b}/file', {}),
        ('patch', f'/api/v1/expenses/categories/{cat}', {'name': 'Renamed'}),
        ('put', f'/api/v1/expenses/categories/{cat}/limits', {'limits': []}),
    ]


def _call(client: Client, method: str, url: str, data: dict, headers: dict | None = None):
    headers = headers or {}
    if method == 'get':
        return client.get(url, **headers)
    return getattr(client, method)(url, data, content_type='application/json', **headers)


def test_a_beta_category_cannot_be_used_on_an_alpha_claim(group):
    w = group
    _, a_user = w.session(w.alpha_hr)
    from hajiri.core.errors import BadRequestError
    with pytest.raises(BadRequestError, match='Line 1: choose a valid category.'):
        ex.check_items(a_user, {'items': [{'expenseDate': days_ago(1), 'categoryId': w.category, 'description': 'x', 'amount': '1'}]})


def test_claims_follow_the_employee_record_of_the_active_company(group):
    w = group
    _, in_beta = w.session(w.dual, w.beta)
    cats = {c['code']: c['id'] for c in ex.get_policy(in_beta)['categories']}
    d = ex.create_claim(in_beta, {'title': 'Filed in Beta', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': cats['OFFICE'], 'description': 'Pens', 'amount': '120'}]})
    detail = ex.claim_detail(in_beta, d['id'])
    assert detail['claim']['employee']['name'] == 'Dipak at Beta'
    # Back in Alpha the Beta draft is someone else's company: not found.
    _, in_alpha = w.session(w.dual)
    from hajiri.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        ex.claim_detail(in_alpha, d['id'])
    assert ex.list_claims(in_alpha, {'scope': 'own'})['claims'] == []


def test_payroll_claims_only_the_run_company_payouts(group):
    w = group
    from hajiri.expenses import payroll as pay
    approve_both(ex.review_claim, w.claim, w)
    emp = w.beta_emp.employee_id
    assert ExpensePayout.objects.filter(employee_id=emp, status='pending').exists()
    # Asked for in Alpha's name, Beta's payouts are not there to claim or preview.
    assert pay.payout_preview(str(w.alpha.id), '00000000-0000-0000-0000-000000000000', 'regular', [emp])['count'] == 0


def test_a_receipt_uploaded_in_beta_cannot_be_attached_in_alpha(group):
    w = group
    _, in_beta = w.session(w.dual, w.beta)
    _, in_alpha = w.session(w.dual)
    file_id = upload(in_beta)
    cats = {c['code']: c['id'] for c in ex.get_policy(in_alpha)['categories']}
    from hajiri.core.errors import BadRequestError
    with pytest.raises(BadRequestError, match='The attached file was not found'):
        ex.create_claim(in_alpha, {'title': 'Cross receipt', 'items': [
            {'expenseDate': days_ago(1), 'categoryId': cats['OFFICE'], 'description': 'Pens', 'amount': '120',
             'receiptFileId': file_id}]})


@pytest.mark.parametrize('who', ['alpha_hr', 'dual'])
def test_beta_records_are_not_found_from_alpha(group, who):
    w = group
    c = w.client(getattr(w, who))           # the cookie session, as the React app sends it
    for method, url, data in _api_urls(w):
        r = _call(c, method, url, data)
        assert r.status_code == 404, (who, method, url, r.status_code, r.content[:200])
    token, _ = w.session(getattr(w, who))
    api = Client()
    for method, url, data in _api_urls(w):
        r = _call(api, method, url, data, bearer(token))
        assert r.status_code == 404, (who, method, url, r.status_code, r.content[:200])
    # Nothing changed in Beta.
    assert ex.claim_detail(w.beta_hr, w.claim)['claim']['status'] == 'pending'
    assert TravelRequest.objects.get(pk=w.trip).status == 'settling'


def test_the_owner_company_opens_them(group):
    w = group
    for person, company in ((w.beta_hr, None), (w.dual, w.beta)):
        c = w.client(person, company)
        assert c.get(f'/api/v1/expenses/claims/{w.claim}').status_code == 200
        assert c.get(f'/api/v1/expenses/travel/{w.trip}').status_code == 200
        assert c.get(f'/api/v1/expenses/settlements/{w.settlement}/bills/{w.bill}/file').status_code == 200
        assert w.category in {x['id'] for x in c.get('/api/v1/expenses/policy').json()['categories']}


def test_lists_and_reports_show_the_active_company_only(group):
    w = group
    alpha = w.client(w.alpha_hr)
    assert b'Beta taxi' not in alpha.get('/api/v1/expenses/claims?scope=all').content
    assert b'Pokhara' not in alpha.get('/api/v1/expenses/travel?scope=all').content
    assert b'Pokhara' not in alpha.get('/api/v1/expenses/reports/advances?format=csv').content
    _, a_user = w.session(w.alpha_hr)
    assert ex.list_claims(a_user, {'scope': 'all'})['claims'] == []
    assert ex.list_trips(a_user, {'scope': 'all'})['trips'] == []
    assert ex.report_advances(a_user)['rows'] == []
    assert ex.report_pending_payment(a_user)['rows'] == []
    # The same person, switched to Beta, sees Beta's claims, trips and advances.
    _, in_beta = w.session(w.dual, w.beta)
    assert [c['title'] for c in ex.list_claims(in_beta, {'scope': 'all'})['claims']] == ['Beta taxi']
    assert [t['destination'] for t in ex.list_trips(in_beta, {'scope': 'all'})['trips']] == ['Pokhara']
    assert [r['destination'] for r in ex.report_advances(in_beta)['rows']] == ['Pokhara']
    assert b'Beta taxi' in w.client(w.dual, w.beta).get('/api/v1/expenses/claims?scope=all').content
    # Policies are per company: Alpha's categories are not Beta's.
    alpha_cats = {c['id'] for c in ex.get_policy(a_user)['categories']}
    assert w.category not in alpha_cats and len(alpha_cats) == 7

