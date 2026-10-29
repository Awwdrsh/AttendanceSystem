"""
The /api/v1/expenses JSON API as the React app calls it (frontend/src/api/expenses.js): the session cookie, JSON
bodies, no CSRF token. Ports the HTTP-level assertions of the TS apps/api/test/expenses.test.ts (policy, claims,
travel, settlements, reports), plus the same-origin check for cookie requests and company isolation.
"""
from __future__ import annotations

import pytest
from django.test import Client

from hajiri.db.models import ExpensePayout, TravelSettlement
from tests.web.factories import set_features_off, sign_in

from .helpers import days_ago, days_ahead, today, upload

E = '/api/v1/expenses'


class Api:
    """A browser tab of the React app: the hajiri_session cookie on every call, JSON in and out."""

    def __init__(self, user, **headers):
        self.c = Client()
        sign_in(self.c, user)
        self.h = headers

    def get(self, path, **q):
        return self.c.get(E + path, q, **self.h)

    def post(self, path, body=None):
        return self.c.post(E + path, body if body is not None else {}, content_type='application/json', **self.h)

    def put(self, path, body):
        return self.c.put(E + path, body, content_type='application/json', **self.h)

    def patch(self, path, body):
        return self.c.patch(E + path, body, content_type='application/json', **self.h)


def _approve_both(w, path, body=None):
    """Manager then HR: the two-step chain for claims, trips and settlements."""
    m = Api(w.ram).post(path, {'decision': 'approve', **(body or {})})
    assert m.status_code == 200 and m.json()['state'] == 'pending', m.content
    h = Api(w.hr).post(path, {'decision': 'approve', **(body or {})})
    assert h.status_code == 200 and h.json()['state'] == 'approved', h.content


def _codes(r):
    return [i['code'] for i in r.json()['issues']]


# ---------------------------------------------------------------- auth, feature switch, origin

def test_signed_out_feature_off_and_cross_site_requests_are_refused(world):
    w = world
    out = Client().get(E + '/policy')
    assert out.status_code == 401 and out.json()['error']
    # The React app posts with the cookie and no CSRF token: same-origin requests pass, a foreign Origin does not.
    same = Api(w.hr, HTTP_ORIGIN='http://testserver')
    assert same.post('/categories', {'code': 'SAME_SITE', 'name': 'Same site', 'kind': 'amount'}).status_code == 201
    evil = Api(w.hr, HTTP_ORIGIN='https://evil.example')
    r = evil.post('/categories', {'code': 'CROSS_SITE', 'name': 'Cross site', 'kind': 'amount'})
    assert r.status_code == 403 and r.json()['error'] == 'cross-site request refused'
    assert evil.get('/policy').status_code == 200  # reads always pass
    set_features_off(w.co, 'expenses')
    assert Api(w.sita).get('/policy').status_code == 404


# ---------------------------------------------------------------- policy

def test_everyone_sees_the_policy_only_hr_changes_it(world):
    w = world
    sita, ram, hr = Api(w.sita), Api(w.ram), Api(w.hr)
    r = sita.get('/policy')
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {'settings', 'categories', 'limits', 'tadaRates', 'destinations', 'myGrade', 'grades'}
    assert {'LOCAL_TRAVEL', 'MEALS', 'FUEL', 'MOBILE', 'MEDICAL', 'OFFICE', 'OTHER'} <= {c['code'] for c in body['categories']}
    assert set(body['categories'][0]) == {'id', 'code', 'name', 'kind', 'receiptRequiredAbove', 'active', 'sortOrder'}
    assert body['myGrade'] == 'G3' and body['grades'] == []
    assert body['destinations'][0] == {'value': 'valley', 'label': 'Kathmandu valley'}
    assert 'G3' in hr.get('/policy').json()['grades']
    settings = {'mileageRatePerKm': '20', 'limitMode': 'warn', 'claimWindowDays': 60}
    for who in (sita, ram):
        refused = who.put('/policy/settings', settings)
        assert refused.status_code == 403 and refused.json()['error'] == 'requires role hr'
    s = hr.put('/policy/settings', {**settings, 'limitMode': 'block'})
    assert s.status_code == 200
    assert s.json() == {'settings': {'mileageRatePerKm': '20.00', 'limitMode': 'block', 'claimWindowDays': 60}}
    bad = hr.put('/policy/settings', {**settings, 'claimWindowDays': 3})
    assert bad.status_code == 400 and bad.json()['error'] == 'validation'
    assert [i['path'] for i in bad.json()['issues']] == ['claimWindowDays']


def test_hr_sets_limits_and_tada_rates_duplicates_are_refused(world):
    w = world
    hr, sita = Api(w.hr), Api(w.sita)
    meals = w.cats['MEALS']
    dup = hr.put(f'/categories/{meals}/limits', {'limits': [{'grade': 'G3', 'perClaim': '1500'}, {'grade': 'G3', 'perClaim': '2000'}]})
    assert dup.status_code == 400 and dup.json()['error'] == 'each grade can appear only once'
    ok = hr.put(f'/categories/{meals}/limits', {'limits': [{'grade': 'G3', 'perClaim': '1500', 'perMonth': '3000'},
                                                          {'grade': '*', 'perClaim': '800'}]})
    assert ok.status_code == 200 and ok.json() == {'ok': True}
    limits = [x for x in hr.get('/policy').json()['limits'] if x['categoryId'] == meals]
    assert [(x['grade'], x['perClaim'], x['perMonth']) for x in limits] == [('*', '800.00', None), ('G3', '1500.00', '3000.00')]
    rates = [{'grade': '*', 'destination': 'valley', 'daRate': '1000', 'lodgingRate': '2500'},
             {'grade': '*', 'destination': 'city', 'daRate': '1500', 'lodgingRate': '3000'},
             {'grade': 'G3', 'destination': 'city', 'daRate': '2000', 'lodgingRate': '3500'},
             {'grade': '*', 'destination': 'international', 'daRate': '50', 'lodgingRate': '100'}]
    r = hr.put('/tada-rates', {'rates': rates})
    assert r.status_code == 200 and r.json() == {'ok': True}
    got = sita.get('/policy').json()['tadaRates']
    assert len(got) == 4 and got[0] == {'grade': '*', 'destination': 'city', 'daRate': '1500.00', 'lodgingRate': '3000.00'}
    twice = hr.put('/tada-rates', {'rates': [rates[0], rates[0]]})
    assert twice.status_code == 400 and twice.json()['error'] == 'each grade and destination can appear only once'
    assert sita.put('/tada-rates', {'rates': []}).status_code == 403
    assert hr.put('/categories/not-a-uuid/limits', {'limits': []}).status_code == 400


def test_hr_adds_a_category_another_company_cannot_touch_it(world):
    w = world
    hr = Api(w.hr)
    c = hr.post('/categories', {'code': 'TRAINING', 'name': 'Training fees', 'kind': 'amount', 'receiptRequiredAbove': '0'})
    assert c.status_code == 201 and set(c.json()) == {'id'}
    cid = c.json()['id']
    bad = hr.post('/categories', {'code': 'lower', 'name': '', 'kind': 'x'})
    assert bad.status_code == 400 and {i['path'] for i in bad.json()['issues']} == {'code', 'name', 'kind'}
    other = Api(w.other_hr).patch(f'/categories/{cid}', {'name': 'x'})
    assert other.status_code == 404 and other.json()['error'] == 'category not found'
    assert Api(w.other_hr).put(f'/categories/{cid}/limits', {'limits': []}).status_code == 404
    off = hr.patch(f'/categories/{cid}', {'active': False})
    assert off.status_code == 200 and off.json() == {'ok': True}
    cat = next(x for x in hr.get('/policy').json()['categories'] if x['id'] == cid)
    assert cat['active'] is False and cat['receiptRequiredAbove'] == '0.00'
    assert Api(w.sita).post('/categories', {'code': 'X_SITA', 'name': 'x', 'kind': 'amount'}).status_code == 403


# ---------------------------------------------------------------- claims

def test_mileage_receipts_limits_and_dates_are_checked(policy_world):
    w = policy_world
    sita = Api(w.sita)
    chk = sita.post('/claims/check', {'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Client visit, Lalitpur', 'km': 12},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900'}]})
    assert chk.status_code == 200
    assert chk.json()['amounts'] == ['240.00', '900.00'] and chk.json()['total'] == '1140.00'
    assert 'receipt_required' in _codes(chk)
    issue = next(i for i in chk.json()['issues'] if i['code'] == 'receipt_required')
    assert set(issue) == {'level', 'code', 'message', 'item'} and issue['level'] == 'block' and issue['item'] == 1
    blocked = sita.post('/claims', {'title': 'Client visit', 'submit': True, 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900'}]})
    assert blocked.status_code == 400 and blocked.json()['details']['code'] == 'claim_blocked'
    assert 'receipt_required' in [i['code'] for i in blocked.json()['details']['issues']]
    over = sita.post('/claims/check', {'items': [{'expenseDate': days_ago(1), 'categoryId': w.cats['MEALS'],
                                                  'description': 'Team lunch', 'amount': '1600', 'receiptFileId': None}]})
    assert 'per_claim_limit' in _codes(over)
    assert '1,500' in next(i for i in over.json()['issues'] if i['code'] == 'per_claim_limit')['message']
    fut = sita.post('/claims/check', {'items': [{'expenseDate': days_ahead(2), 'categoryId': w.cats['OFFICE'], 'description': 'Pens', 'amount': '100'}]})
    assert 'future_date' in _codes(fut)
    old = sita.post('/claims/check', {'items': [{'expenseDate': days_ago(80), 'categoryId': w.cats['OFFICE'], 'description': 'Pens', 'amount': '100'}]})
    assert 'too_old' in _codes(old)
    no_km = sita.post('/claims/check', {'items': [{'expenseDate': days_ago(1), 'categoryId': w.cats['FUEL'], 'description': 'Bike'}]})
    assert no_km.status_code == 400 and no_km.json()['error'] == 'Line 1: enter the distance in km.'
    empty = sita.post('/claims/check', {'items': []})
    assert empty.status_code == 400 and empty.json()['error'] == 'validation'


@pytest.fixture
def submitted(policy_world):
    """Sita's 'Client visit' claim (taxi 900 with a receipt, 10 km on her bike), saved as a draft and then submitted."""
    w = policy_world
    sita = Api(w.sita)
    receipt = upload(w.sita)
    d = sita.post('/claims', {'title': 'Client visit', 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': receipt},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Own bike', 'km': 10}]})
    assert d.status_code == 201, d.content
    assert d.json() == {'id': d.json()['id'], 'status': 'draft', 'total': '1100.00', 'issues': []}
    w.claim_id, w.receipt = d.json()['id'], receipt
    return w


def test_a_draft_is_private_editable_and_submits_into_the_chain(submitted):
    w = submitted
    sita, ram = Api(w.sita), Api(w.ram)
    cid = w.claim_id
    assert ram.get(f'/claims/{cid}').status_code == 404
    assert all(c['id'] != cid for c in ram.get('/claims').json()['claims'])
    # Another employee cannot reuse the receipt.
    reuse = ram.post('/claims', {'title': 'x', 'items': [{'expenseDate': days_ago(1), 'categoryId': w.cats['LOCAL_TRAVEL'],
                                                          'description': 'Taxi', 'amount': '900', 'receiptFileId': w.receipt}]})
    assert reuse.status_code in (400, 403)
    edited = sita.put(f'/claims/{cid}', {'title': 'Client visit', 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': w.receipt},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Own bike', 'km': 10}]})
    assert edited.status_code == 200 and edited.json()['status'] == 'draft' and edited.json()['total'] == '1100.00'
    assert ram.put(f'/claims/{cid}', {'title': 'x', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'x', 'amount': '1'}]}).status_code == 404
    sub = sita.post(f'/claims/{cid}/submit')
    assert sub.status_code == 200 and sub.json() == {'id': cid, 'status': 'pending'}
    again = sita.post(f'/claims/{cid}/submit')
    assert again.status_code == 409 and again.json()['error'] == 'claim is pending'
    late = sita.put(f'/claims/{cid}', {'title': 'x', 'items': [{'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'x', 'amount': '1'}]})
    assert late.status_code == 409 and late.json()['error'] == 'only a draft claim can be edited'
    inbox = Client()
    sign_in(inbox, w.ram)
    card = next(i for i in inbox.get('/api/v1/workflow/inbox').json()['items'] if i['objectId'] == cid)
    assert card['objectType'] == 'expense_claim'
    assert {k: card['summary'][k] for k in ('title', 'amount', 'items', 'receipts')} == \
        {'title': 'Client visit', 'amount': '1100.00', 'items': 2, 'receipts': 1}
    listed = sita.get('/claims', scope='own').json()['claims']
    row = next(c for c in listed if c['id'] == cid)
    assert set(row) == {'id', 'title', 'status', 'totalClaimed', 'totalApproved', 'submittedAt', 'decidedAt', 'paymentMethod', 'paidAt',
                        'createdAt', 'employeeId', 'empCode', 'employeeName', 'items', 'firstDate', 'lastDate', 'payoutStatus',
                        'payBsYear', 'payBsMonth'}
    assert (row['status'], row['items'], row['firstDate'], row['employeeName']) == ('pending', 2, days_ago(2), 'Sita Sharma')


def test_outsiders_cannot_see_or_decide_the_manager_sees_the_receipt(submitted):
    w = submitted
    cid = w.claim_id
    Api(w.sita).post(f'/claims/{cid}/submit')
    stranger, other, sita, ram = Api(w.stranger), Api(w.other_hr), Api(w.sita), Api(w.ram)
    assert stranger.get(f'/claims/{cid}').status_code == 404
    assert other.get(f'/claims/{cid}').status_code == 404
    assert sita.post(f'/claims/{cid}/review', {'decision': 'approve'}).status_code == 403
    assert stranger.post(f'/claims/{cid}/review', {'decision': 'approve'}).status_code == 403
    detail = ram.get(f'/claims/{cid}').json()
    assert set(detail) == {'claim', 'items', 'payouts', 'issues', 'canDecide'}
    assert detail['canDecide'] is True and sita.get(f'/claims/{cid}').json()['canDecide'] is False
    assert detail['claim']['employee'] == {'id': str(w.sita_emp.id), 'empCode': w.sita_emp.emp_code, 'name': 'Sita Sharma', 'grade': 'G3'}
    line = next(i for i in detail['items'] if i['receiptFileId'])
    assert line['receiptName'] == 'bill.pdf' and line['receiptMime'] == 'application/pdf' and line['categoryKind'] == 'amount'
    f = ram.get(f'/claims/{cid}/items/{line["id"]}/receipt')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    assert stranger.get(f'/claims/{cid}/items/{line["id"]}/receipt').status_code == 404
    bike = next(i for i in detail['items'] if not i['receiptFileId'])
    nofile = ram.get(f'/claims/{cid}/items/{bike["id"]}/receipt')
    assert nofile.status_code == 404 and nofile.json()['error'] == 'no receipt for this line'


def test_hr_approves_less_only_with_a_reason_the_payout_waits_for_payroll(submitted):
    w = submitted
    cid = w.claim_id
    sita, ram, hr = Api(w.sita), Api(w.ram), Api(w.hr)
    sita.post(f'/claims/{cid}/submit')
    assert ram.post(f'/claims/{cid}/review', {'decision': 'approve'}).json() == {'state': 'pending'}
    taxi = next(i for i in hr.get(f'/claims/{cid}').json()['items'] if i['description'] == 'Taxi')
    more = hr.post(f'/claims/{cid}/review', {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '1000'}]})
    assert more.status_code == 400 and more.json()['error'] == '"Taxi": the approved amount cannot be more than claimed.'
    why = hr.post(f'/claims/{cid}/review', {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '700'}]})
    assert why.status_code == 400 and why.json()['error'] == '"Taxi": give a reason for approving less.'
    ok = hr.post(f'/claims/{cid}/review', {'decision': 'approve', 'items': [
        {'id': taxi['id'], 'approvedAmount': '700', 'reason': 'Fare cap for this route'}]})
    assert ok.status_code == 200 and ok.json() == {'state': 'approved'}
    after = sita.get(f'/claims/{cid}').json()
    assert after['claim']['status'] == 'approved' and after['claim']['totalApproved'] == '900.00'
    assert [(p['code'], p['amount'], p['status']) for p in after['payouts']] == [('EXP_REIMB', '900.00', 'pending')]
    assert set(after['payouts'][0]) == {'id', 'code', 'amount', 'status', 'paidAmount', 'paidAt', 'bsYear', 'bsMonth'}
    taxi_after = next(i for i in after['items'] if i['description'] == 'Taxi')
    assert (taxi_after['approvedAmount'], taxi_after['adjustReason']) == ('700.00', 'Fare cap for this route')
    pending = hr.get('/reports/pending-payment')
    assert pending.status_code == 200 and len(pending.json()['rows']) == 1
    assert pending.json()['rows'][0]['kind'] == 'Pay' and pending.json()['rows'][0]['inRun'] == 'Next pay run'
    assert ram.get('/reports/pending-payment').status_code == 403
    # HR pays it in cash outside payroll; then nothing is waiting.
    assert ram.post(f'/claims/{cid}/pay-outside', {'note': 'cash'}).status_code == 403
    short = hr.post(f'/claims/{cid}/pay-outside', {'note': 'x'})
    assert short.status_code == 400 and short.json()['error'] == 'validation'
    paid = hr.post(f'/claims/{cid}/pay-outside', {'note': 'Paid from petty cash'})
    assert paid.status_code == 200 and paid.json() == {'id': cid, 'status': 'paid'}
    assert hr.get('/reports/pending-payment').json()['rows'] == []
    assert hr.post(f'/claims/{cid}/pay-outside', {'note': 'Paid twice'}).status_code == 409


def test_rejecting_needs_a_reason_cancelling_withdraws_a_pending_claim(policy_world):
    w = policy_world
    sita, ram = Api(w.sita), Api(w.ram)
    c = sita.post('/claims', {'title': 'Stationery', 'submit': True, 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'Notebooks', 'amount': '300', 'receiptFileId': upload(w.sita)}]})
    assert c.status_code == 201 and c.json()['status'] == 'pending'
    cid = c.json()['id']
    assert ram.post(f'/claims/{cid}/review', {'decision': 'reject'}).status_code == 400
    assert ram.post(f'/claims/{cid}/cancel').status_code == 404
    cancel = sita.post(f'/claims/{cid}/cancel')
    assert cancel.status_code == 200 and cancel.json() == {'id': cid, 'status': 'cancelled'}
    inbox = Client()
    sign_in(inbox, w.ram)
    assert all(i['objectId'] != cid for i in inbox.get('/api/v1/workflow/inbox').json()['items'])
    again = sita.post(f'/claims/{cid}/cancel')
    assert again.status_code == 409 and again.json()['error'] == 'a cancelled claim cannot be cancelled'
    assert ram.post(f'/claims/{cid}/review', {'decision': 'approve'}).status_code == 409


def test_the_monthly_limit_counts_other_open_claims(policy_world):
    w = policy_world
    sita = Api(w.sita)
    day = days_ago(1)

    def lunch(title, amount):
        return sita.post('/claims', {'title': title, 'submit': True, 'items': [
            {'expenseDate': day, 'categoryId': w.cats['MEALS'], 'description': 'Lunch', 'amount': amount, 'receiptFileId': upload(w.sita)}]})
    assert lunch('Lunch 1', '1400').status_code == 201
    assert lunch('Lunch 2', '1400').status_code == 201
    third = lunch('Lunch 3', '400')
    assert third.status_code == 400 and 'monthly limit' in third.json()['error']


def test_hr_sees_everyone_a_manager_the_team_an_employee_themself(submitted):
    w = submitted
    Api(w.sita).post(f'/claims/{w.claim_id}/submit')
    Api(w.hr).post('/claims', {'title': 'HR draft', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'Pens', 'amount': '100'}]})
    ram = Api(w.ram)
    r = ram.get('/claims', scope='all')
    assert r.status_code == 403 and r.json()['error'] == 'only HR can see everyone'
    assert Api(w.sita).get('/claims', scope='team').status_code == 403
    assert [c['id'] for c in ram.get('/claims', scope='team').json()['claims']] == [w.claim_id]
    hr_all = Api(w.hr).get('/claims', scope='all').json()['claims']
    assert hr_all and all(c['status'] != 'draft' or c['employeeName'] == 'Hari HR' for c in hr_all)
    mine = Api(w.sita).get('/claims').json()['claims']
    assert mine and all(c['employeeId'] == str(w.sita_emp.id) for c in mine)
    assert Api(w.sita).get('/claims', status='approved').json()['claims'] == []
    assert Api(w.sita).get('/claims', status='nonsense').status_code == 400
    assert Api(w.other_hr).get('/claims', scope='all').json()['claims'] == []
    assert Api(w.sita).get('/claims/not-a-uuid').status_code == 400


# ---------------------------------------------------------------- travel

def test_quotes_daily_allowance_by_grade_and_lodging_per_night(policy_world):
    w = policy_world
    sita = Api(w.sita)
    q = sita.post('/travel/quote', {'destinationCategory': 'city', 'fromDate': days_ahead(3), 'toDate': days_ahead(5)})
    assert q.status_code == 200
    quote = q.json()['quote']
    assert {k: quote[k] for k in ('days', 'nights', 'daRate', 'daAmount', 'lodgingLimit', 'grade', 'currency', 'fxRate')} == \
        {'days': 3, 'nights': 2, 'daRate': '2000.00', 'daAmount': '6000.00', 'lodgingLimit': '7000.00', 'grade': 'G3',
         'currency': 'NPR', 'fxRate': None}
    intl = sita.post('/travel/quote', {'destinationCategory': 'international', 'fromDate': days_ahead(3), 'toDate': days_ahead(4)})
    assert intl.status_code == 400 and intl.json()['error'] == 'Enter the US dollar exchange rate for an international trip.'
    intl2 = sita.post('/travel/quote', {'destinationCategory': 'international', 'fromDate': days_ahead(3), 'toDate': days_ahead(4),
                                        'fxRate': '136.5'}).json()['quote']
    assert {k: intl2[k] for k in ('currency', 'daRateNpr', 'daAmount', 'lodgingLimit', 'fxRate')} == \
        {'currency': 'USD', 'daRateNpr': '6825.00', 'daAmount': '13650.00', 'lodgingLimit': '13650.00', 'fxRate': '136.5000'}
    back = sita.post('/travel/quote', {'destinationCategory': 'city', 'fromDate': days_ahead(5), 'toDate': days_ahead(3)})
    assert back.status_code == 400 and back.json()['error'] == 'The return date must be on or after the start date.'


def _request_pokhara(w, **extra):
    return Api(w.sita).post('/travel', {'destination': 'Pokhara', 'destinationCategory': 'city', 'purpose': 'Site survey',
                                        'fromDate': days_ago(4), 'toDate': days_ahead(1), 'mode': 'bus', 'estimatedCost': '2000',
                                        'advanceRequested': '10000', **extra})


@pytest.fixture
def trip(policy_world):
    w = policy_world
    t = _request_pokhara(w)
    assert t.status_code == 201, t.content
    w.trip_id = t.json()['id']
    return w


def test_a_request_asks_for_an_advance_up_to_the_expected_cost_overlaps_are_refused(policy_world):
    w = policy_world
    big = _request_pokhara(w, advanceRequested='99999')
    assert big.status_code == 400 and big.json()['error'].startswith('The advance cannot be more than the expected cost of the trip')
    t = _request_pokhara(w)
    assert t.status_code == 201
    body = t.json()
    assert set(body) == {'id', 'status', 'quote'} and body['status'] == 'pending' and body['quote']['days'] == 6
    dup = Api(w.sita).post('/travel', {'destination': 'Butwal', 'destinationCategory': 'city', 'purpose': 'Other',
                                       'fromDate': days_ago(1), 'toDate': days_ahead(2), 'mode': 'bus'})
    assert dup.status_code == 409 and dup.json()['error'] == 'You already have a trip on these dates.'
    ended = Api(w.sita).post('/travel', {'destination': 'Butwal', 'destinationCategory': 'city', 'purpose': 'Other',
                                         'fromDate': days_ago(20), 'toDate': days_ago(19), 'mode': 'bus'})
    assert ended.status_code == 400 and ended.json()['error'] == 'This trip has already ended. Ask HR to record it.'
    bad = Api(w.sita).post('/travel', {'destination': '', 'destinationCategory': 'moon', 'purpose': 'x', 'fromDate': 'soon',
                                       'toDate': today(), 'mode': 'rocket'})
    assert bad.status_code == 400 and {i['path'] for i in bad.json()['issues']} >= {'destination', 'destinationCategory', 'purpose',
                                                                                     'fromDate', 'mode'}
    inbox = Client()
    sign_in(inbox, w.ram)
    card = next(i for i in inbox.get('/api/v1/workflow/inbox').json()['items'] if i['objectId'] == body['id'])
    assert (card['summary']['destination'], card['summary']['days'], card['summary']['advance']) == ('Pokhara', 6, '10000.00')
    listed = Api(w.sita).get('/travel').json()['trips']
    assert [(x['destination'], x['status'], x['settlement']) for x in listed] == [('Pokhara', 'pending', None)]
    assert Api(w.sita).get('/travel', status='settled').json()['trips'] == []


def test_approvers_may_lower_the_advance_approval_queues_it(trip):
    w = trip
    tid = w.trip_id
    too_much = Api(w.ram).post(f'/travel/{tid}/review', {'decision': 'approve', 'advanceApproved': '20000'})
    assert too_much.status_code == 400 and too_much.json()['error'] == 'The approved advance cannot be more than requested.'
    assert Api(w.ram).get(f'/travel/{tid}').json()['canDecide'] is True
    _approve_both(w, f'/travel/{tid}/review', {'advanceApproved': '8000'})
    t = Api(w.sita).get(f'/travel/{tid}').json()
    assert set(t) == {'trip', 'settlements', 'payouts', 'canDecide', 'canDecideSettlement', 'canSettle'}
    assert (t['trip']['status'], t['trip']['advanceApproved'], t['trip']['advanceStatus']) == ('approved', '8000.00', 'pending')
    assert t['trip']['destinationLabel'] == 'Other city' and t['trip']['employee']['grade'] == 'G3'
    assert [(p['sourceType'], p['code'], p['amount'], p['status']) for p in t['payouts']] == \
        [('travel_advance', 'TRAVEL_ADV', '8000.00', 'pending')]
    assert t['canSettle'] is True and t['canDecide'] is False
    assert Api(w.stranger).get(f'/travel/{tid}').status_code == 404
    assert Api(w.other_hr).get(f'/travel/{tid}').status_code == 404
    adv = Api(w.hr).get('/reports/advances')
    assert adv.status_code == 200 and len(adv.json()['rows']) == 1
    assert adv.json()['columns'][0] == {'key': 'empCode', 'header': 'Code'}
    assert adv.json()['rows'][0]['advance'] == '8000.00'
    assert Api(w.sita).get('/reports/advances').status_code == 403


def test_hr_pays_the_advance_in_cash_then_the_trip_cannot_be_cancelled(trip):
    w = trip
    tid = w.trip_id
    _approve_both(w, f'/travel/{tid}/review', {'advanceApproved': '8000'})
    assert Api(w.ram).post(f'/travel/{tid}/advance-outside', {'note': 'cash'}).status_code == 403
    r = Api(w.hr).post(f'/travel/{tid}/advance-outside', {'note': 'Paid from petty cash'})
    assert r.status_code == 200 and r.json() == {'id': tid, 'advanceStatus': 'paid'}
    assert ExpensePayout.objects.get(source_type='travel_advance', source_id=tid).status == 'cash'
    again = Api(w.hr).post(f'/travel/{tid}/advance-outside', {'note': 'Paid twice'})
    assert again.status_code == 409 and again.json()['error'] == 'there is no advance waiting to be paid'
    cancel = Api(w.sita).post(f'/travel/{tid}/cancel')
    assert cancel.status_code == 409 and 'submit a settlement instead' in cancel.json()['error']


def test_a_pending_trip_is_cancelled_by_its_traveller(trip):
    w = trip
    tid = w.trip_id
    assert Api(w.ram).post(f'/travel/{tid}/cancel').status_code == 404
    r = Api(w.sita).post(f'/travel/{tid}/cancel')
    assert r.status_code == 200 and r.json() == {'id': tid, 'status': 'cancelled'}
    again = Api(w.sita).post(f'/travel/{tid}/cancel')
    assert again.status_code == 409 and again.json()['error'] == 'a cancelled trip cannot be cancelled'
    assert Api(w.ram).post(f'/travel/{tid}/review', {'decision': 'approve'}).status_code == 409


def test_settlement_recovers_an_unspent_paid_advance(trip):
    w = trip
    tid = w.trip_id
    _approve_both(w, f'/travel/{tid}/review', {'advanceApproved': '8000'})
    Api(w.hr).post(f'/travel/{tid}/advance-outside', {'note': 'Paid from petty cash'})
    sita = Api(w.sita)
    future = sita.post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ahead(1), 'bills': []})
    assert future.status_code == 400 and future.json()['error'] == 'The return date cannot be in the future.'
    no_file = sita.post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(3), 'bills': [
        {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200'}]})
    assert no_file.status_code == 400 and no_file.json()['error'] == 'Bill 1: attach the bill for amounts above Rs 500.'
    outside = sita.post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(3), 'bills': [
        {'kind': 'other', 'billDate': days_ago(10), 'description': 'Tea', 'amount': '50'}]})
    assert outside.status_code == 400 and outside.json()['error'] == 'Bill 1: the date must be within the trip.'
    assert Api(w.ram).post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': []}).status_code == 404
    bill = upload(w.sita, 'travel_bill')
    s = sita.post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': [
        {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200', 'fileId': bill},
        {'kind': 'lodging', 'billDate': days_ago(4), 'description': 'Hotel', 'amount': '400'}]})
    assert s.status_code == 201, s.content
    # One day: DA 2000, no nights so lodging allowed 0, transport 1200: 3200 - 8000 advance = -4800 to recover.
    assert s.json() == {'id': s.json()['id'], 'daAmount': '2000.00', 'lodgingAllowed': '0.00', 'otherBills': '1200.00',
                        'entitlement': '3200.00', 'advance': '8000.00', 'net': '-4800.00'}
    sid = s.json()['id']
    twice = sita.post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': []})
    assert twice.status_code == 409 and twice.json()['error'] == 'a settlement is already waiting for approval'
    assert Api(w.ram).get(f'/travel/{tid}').json()['canDecideSettlement'] is True
    assert Api(w.sita).post(f'/settlements/{sid}/review', {'decision': 'approve'}).status_code == 403
    _approve_both(w, f'/settlements/{sid}/review')
    again = Api(w.hr).post(f'/settlements/{sid}/review', {'decision': 'approve'})
    assert again.status_code == 409 and again.json()['error'] == 'settlement is approved'
    t = Api(w.hr).get(f'/travel/{tid}').json()
    assert t['trip']['status'] == 'settled'
    assert ('EXP_RECOVERY', '4800.00', 'pending') in [(p['code'], p['amount'], p['status']) for p in t['payouts']]
    st = t['settlements'][0]
    assert (st['status'], st['entitlement'], st['net'], len(st['bills'])) == ('approved', '3200.00', '-4800.00', 2)
    bill_row = next(b for b in st['bills'] if b['fileId'])
    assert set(bill_row) == {'id', 'kind', 'billDate', 'description', 'amount', 'fileId', 'fileName'}
    assert Api(w.ram).get(f'/settlements/{sid}/bills/{bill_row["id"]}/file').status_code == 200
    assert Api(w.other_hr).get(f'/settlements/{sid}/bills/{bill_row["id"]}/file').status_code == 404
    assert Api(w.stranger).get(f'/settlements/{sid}/bills/{bill_row["id"]}/file').status_code == 404
    hotel = next(b for b in st['bills'] if not b['fileId'])
    nofile = Api(w.hr).get(f'/settlements/{sid}/bills/{hotel["id"]}/file')
    assert nofile.status_code == 404 and nofile.json()['error'] == 'no file for this bill'
    listed = Api(w.sita).get('/travel').json()['trips'][0]
    assert listed['settlement'] == {'id': sid, 'status': 'approved', 'net': '-4800.00'}


def test_an_advance_payroll_has_not_picked_up_is_cancelled_at_settlement(policy_world):
    w = policy_world
    t = Api(w.sita).post('/travel', {'destination': 'Hetauda', 'destinationCategory': 'city', 'purpose': 'Training',
                                     'fromDate': days_ago(2), 'toDate': today(), 'mode': 'office_vehicle', 'advanceRequested': '3000'})
    assert t.status_code == 201
    tid = t.json()['id']
    _approve_both(w, f'/travel/{tid}/review')
    s = Api(w.sita).post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(2), 'actualTo': days_ago(2), 'bills': []})
    assert s.status_code == 201 and s.json()['net'] == '-1000.00'
    sid = s.json()['id']
    _approve_both(w, f'/settlements/{sid}/review')
    payouts = Api(w.hr).get(f'/travel/{tid}').json()['payouts']
    assert [(p['code'], p['status'], p['amount']) for p in payouts] == [('TRAVEL_ADV', 'cancelled', '3000.00'),
                                                                       ('EXP_REIMB', 'pending', '2000.00')]
    st = TravelSettlement.objects.get(pk=sid)
    assert (str(st.advance), str(st.net)) == ('0.00', '2000.00')


# ---------------------------------------------------------------- reports

def test_spend_report_groups_approved_claims_and_settlements_csv_is_hr_only(submitted):
    w = submitted
    cid = w.claim_id
    Api(w.sita).post(f'/claims/{cid}/submit')
    Api(w.ram).post(f'/claims/{cid}/review', {'decision': 'approve'})
    taxi = next(i for i in Api(w.hr).get(f'/claims/{cid}').json()['items'] if i['description'] == 'Taxi')
    Api(w.hr).post(f'/claims/{cid}/review', {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '700', 'reason': 'Fare cap'}]})
    t = _request_pokhara(w, advanceRequested='0')
    tid = t.json()['id']
    _approve_both(w, f'/travel/{tid}/review')
    bill = upload(w.sita, 'travel_bill')
    s = Api(w.sita).post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': [
        {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200', 'fileId': bill}]})
    _approve_both(w, f'/settlements/{s.json()["id"]}/review')
    hr = Api(w.hr)
    r = hr.get('/reports/spend', **{'from': days_ago(30), 'to': today(), 'groupBy': 'category'})
    assert r.status_code == 200
    assert r.json()['columns'] == [{'key': 'group', 'header': 'Category'}, {'key': 'items', 'header': 'Items'},
                                   {'key': 'amount', 'header': 'Amount (NPR)'}]
    groups = {x['group']: x['amount'] for x in r.json()['rows']}
    assert groups == {'Local travel': '700.00', 'Fuel (own vehicle)': '200.00', 'Travel': '3200.00'}
    by_emp = hr.get('/reports/spend', **{'from': days_ago(30), 'to': today(), 'groupBy': 'employee'}).json()
    assert by_emp['columns'][0]['header'] == 'Employee' and [x['amount'] for x in by_emp['rows']] == ['4100.00']
    csv = hr.get('/reports/spend', **{'from': days_ago(30), 'to': today(), 'groupBy': 'month', 'format': 'csv'})
    assert csv.status_code == 200 and 'text/csv' in csv['Content-Type']
    assert 'expense-spend-by-month' in csv['Content-Disposition']
    assert Api(w.sita).get('/reports/spend', **{'from': days_ago(30), 'to': today()}).status_code == 403
    backwards = hr.get('/reports/spend', **{'from': today(), 'to': days_ago(30)})
    assert backwards.status_code == 400 and backwards.json()['error'] == 'the end date must be after the start date'
    assert hr.get('/reports/spend').status_code == 400
    assert hr.get('/reports/advances', format='csv')['Content-Type'].startswith('text/csv')
    pending = hr.get('/reports/pending-payment').json()
    assert [c['key'] for c in pending['columns']] == ['empCode', 'employeeName', 'note', 'kind', 'amount', 'inRun']
    assert sorted(x['amount'] for x in pending['rows']) == ['3200.00', '900.00']
    # Another company's HR sees none of it.
    assert Api(w.other_hr).get('/reports/spend', **{'from': days_ago(30), 'to': today()}).json()['rows'] == []
    assert Api(w.other_hr).get('/reports/pending-payment').json()['rows'] == []
    assert Api(w.other_hr).get('/travel', scope='all').json()['trips'] == []
