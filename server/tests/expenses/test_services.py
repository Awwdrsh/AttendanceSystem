"""Port of apps/api/test/expenses.test.ts at service level (the same assertions; HTTP statuses become the typed errors)."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from tests.support.sql import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, HttpError, NotFoundError
from hajiri.expenses import payroll as pay
from hajiri.expenses import services as ex
from hajiri.expenses.jobs import remind_travel_settlements
from hajiri.workflow import service as wf

from .helpers import approve_both, days_ago, days_ahead, today, upload


def status_of(fn, *a) -> int:
    try:
        fn(*a)
    except HttpError as e:
        return e.status
    return 200


def inbox_item(user, object_id):
    return next((i for i in wf.inbox_for(user) if i['objectId'] == object_id), None)


# ---------------------------------------------------------------- policy

def test_everyone_sees_categories_and_rates_only_hr_changes_them(world):
    r = ex.get_policy(world.sita)
    assert {'LOCAL_TRAVEL', 'MEALS', 'FUEL', 'MOBILE', 'MEDICAL', 'OFFICE', 'OTHER'} <= {c['code'] for c in r['categories']}
    assert len(r['tadaRates']) == 4
    assert r['myGrade'] == 'G3'
    body = {'mileageRatePerKm': '20', 'limitMode': 'warn', 'claimWindowDays': 60}
    assert status_of(ex.save_settings, world.sita, body) == 403
    assert status_of(ex.save_settings, world.ram, body) == 403
    s = ex.save_settings(world.hr, {'mileageRatePerKm': '20', 'limitMode': 'block', 'claimWindowDays': 60})
    assert s['settings']['mileageRatePerKm'] == '20.00'
    assert 'update' in [a['action'] for a in rows("SELECT action FROM audit_log WHERE table_name = 'company_setting'")]


def test_hr_sets_limits_per_grade_and_tada_rates_duplicates_refused(world):
    with pytest.raises(BadRequestError):
        ex.set_limits(world.hr, world.cats['MEALS'], {'limits': [{'grade': 'G3', 'perClaim': '1500'}, {'grade': 'G3', 'perClaim': '2000'}]})
    assert ex.set_limits(world.hr, world.cats['MEALS'], {'limits': [{'grade': 'G3', 'perClaim': '1500', 'perMonth': '3000'},
                                                                    {'grade': '*', 'perClaim': '800'}]}) == {'ok': True}
    assert ex.set_tada_rates(world.hr, {'rates': [
        {'grade': '*', 'destination': 'valley', 'daRate': '1000', 'lodgingRate': '2500'},
        {'grade': '*', 'destination': 'city', 'daRate': '1500', 'lodgingRate': '3000'},
        {'grade': 'G3', 'destination': 'city', 'daRate': '2000', 'lodgingRate': '3500'},
        {'grade': '*', 'destination': 'international', 'daRate': '50', 'lodgingRate': '100'},
    ]}) == {'ok': True}
    assert status_of(ex.set_tada_rates, world.sita, {'rates': []}) == 403
    with pytest.raises(BadRequestError, match='each grade and destination can appear only once'):
        ex.set_tada_rates(world.hr, {'rates': [{'grade': '*', 'destination': 'city', 'daRate': '1', 'lodgingRate': '1'}] * 2})


def test_hr_adds_a_category_another_company_cannot_touch_it(world):
    c = ex.create_category(world.hr, {'code': 'TRAINING', 'name': 'Training fees', 'kind': 'amount', 'receiptRequiredAbove': '0'})
    assert c['id']
    assert status_of(ex.update_category, world.other_hr, c['id'], {'name': 'x'}) == 404
    assert ex.update_category(world.hr, c['id'], {'active': False}) == {'ok': True}
    assert one('SELECT active FROM expense_category WHERE id = %s', [c['id']])['active'] is False


# ---------------------------------------------------------------- claims

def _draft_claim(w):
    receipt = upload(w.sita)
    d = ex.create_claim(w.sita, {'title': 'Client visit', 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': receipt},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Own bike', 'km': 10},
    ]})
    return d, receipt


def _submitted_claim(w):
    d, receipt = _draft_claim(w)
    ex.submit_claim(w.sita, d['id'])
    return d['id']


def _approved_claim(w):
    claim_id = _submitted_claim(w)
    assert ex.review_claim(w.ram, claim_id, {'decision': 'approve'})['state'] == 'pending'
    taxi = next(i for i in ex.claim_detail(w.hr, claim_id)['items'] if i['description'] == 'Taxi')
    ok = ex.review_claim(w.hr, claim_id, {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '700', 'reason': 'Fare cap for this route'}]})
    assert ok['state'] == 'approved'
    return claim_id


def test_mileage_is_km_times_rate_receipts_required_above_threshold(policy_world):
    w = policy_world
    chk = ex.check_items(w.sita, {'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Client visit, Lalitpur', 'km': 12},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900'},
    ]})
    assert chk['amounts'] == ['240.00', '900.00']
    assert 'receipt_required' in [i['code'] for i in chk['issues']]
    with pytest.raises(BadRequestError) as e:
        ex.create_claim(w.sita, {'title': 'Client visit', 'submit': True, 'items': [
            {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900'}]})
    assert e.value.details['code'] == 'claim_blocked'
    assert e.value.message == 'Line 1: attach a receipt for Local travel above Rs 500.'
    assert not rows('SELECT 1 FROM expense_claim')


def test_limits_per_claim_follow_grade_future_and_old_dates_refused(policy_world):
    w = policy_world
    over = ex.check_items(w.sita, {'items': [{'expenseDate': days_ago(1), 'categoryId': w.cats['MEALS'], 'description': 'Team lunch',
                                              'amount': '1600', 'receiptFileId': None}]})
    codes = [i['code'] for i in over['issues']]
    assert 'per_claim_limit' in codes
    assert re.search('1,500', next(i for i in over['issues'] if i['code'] == 'per_claim_limit')['message'])
    fut = ex.check_items(w.sita, {'items': [{'expenseDate': days_ahead(2), 'categoryId': w.cats['OFFICE'], 'description': 'Pens', 'amount': '100'}]})
    assert 'future_date' in [i['code'] for i in fut['issues']]
    old = ex.check_items(w.sita, {'items': [{'expenseDate': days_ago(80), 'categoryId': w.cats['OFFICE'], 'description': 'Pens', 'amount': '100'}]})
    assert 'too_old' in [i['code'] for i in old['issues']]
    assert next(i for i in old['issues'] if i['code'] == 'too_old')['message'] == 'Line 1: expenses older than 60 days cannot be claimed.'


def test_line_validation_messages(policy_world):
    w = policy_world
    with pytest.raises(BadRequestError, match='Line 1: enter the distance in km.'):
        ex.normalise_items(str(w.co.id), [{'expenseDate': days_ago(1), 'categoryId': w.cats['FUEL'], 'description': 'x'}])
    with pytest.raises(BadRequestError, match='Line 1: enter an amount above zero.'):
        ex.normalise_items(str(w.co.id), [{'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'x', 'amount': '0'}])
    with pytest.raises(BadRequestError, match='Line 1: choose a valid category.'):
        ex.normalise_items(str(w.co.id), [{'expenseDate': days_ago(1), 'categoryId': '00000000-0000-0000-0000-000000000000',
                                           'description': 'x', 'amount': '1'}])
    ex.update_category(w.hr, w.cats['OTHER'], {'active': False})
    with pytest.raises(BadRequestError, match='Line 1: Other is no longer available.'):
        ex.normalise_items(str(w.co.id), [{'expenseDate': days_ago(1), 'categoryId': w.cats['OTHER'], 'description': 'x', 'amount': '1'}])


def test_a_draft_is_private_editable_and_submits_into_manager_hr_chain(policy_world):
    w = policy_world
    d, receipt = _draft_claim(w)
    assert d['status'] == 'draft'
    claim_id = d['id']
    assert status_of(ex.claim_detail, w.ram, claim_id) == 404
    assert not [c for c in ex.list_claims(w.ram)['claims'] if c['id'] == claim_id]
    # Another employee cannot reuse the receipt.
    st = status_of(ex.create_claim, w.ram, {'title': 'x', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': receipt}]})
    assert st in (400, 403)
    assert ex.submit_claim(w.sita, claim_id) == {'id': claim_id, 'status': 'pending'}
    assert status_of(ex.update_claim, w.sita, claim_id, {'title': 'x', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'x', 'amount': '1'}]}) == 409
    card = inbox_item(w.ram, claim_id)
    assert card['objectType'] == 'expense_claim'
    assert {k: card['summary'][k] for k in ('title', 'amount', 'items', 'receipts')} == \
        {'title': 'Client visit', 'amount': '1100.00', 'items': 2, 'receipts': 1}
    assert one('SELECT attached_to FROM stored_file WHERE id = %s', [receipt])['attached_to'] == f'expense_claim:{claim_id}'
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'expense_claim.pending' AND channel = 'in_app'")
    assert n == {'title': 'Expense claim to approve', 'body': 'Sita Sharma: Client visit, Rs 1,100'}


def test_draft_edit_replaces_lines_and_can_submit(policy_world):
    w = policy_world
    d, _ = _draft_claim(w)
    r = ex.update_claim(w.sita, d['id'], {'title': 'Pens', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['FUEL'], 'description': 'Bike', 'km': 5}]})
    assert r['status'] == 'draft' and r['total'] == '100.00'
    assert one('SELECT title, total_claimed::text FROM expense_claim WHERE id = %s', [d['id']]) == {'title': 'Pens', 'total_claimed': '100.00'}
    assert status_of(ex.update_claim, w.ram, d['id'], {'title': 'x', 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['FUEL'], 'description': 'Bike', 'km': 5}]}) == 404
    r = ex.update_claim(w.sita, d['id'], {'title': 'Pens', 'submit': True, 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['FUEL'], 'description': 'Bike', 'km': 5}]})
    assert r['status'] == 'pending'
    assert status_of(ex.submit_claim, w.sita, d['id']) == 409


def test_outsiders_other_companies_and_employee_cannot_decide_receipt_shown_to_manager(policy_world):
    w = policy_world
    claim_id = _submitted_claim(w)
    assert status_of(ex.claim_detail, w.stranger, claim_id) == 404
    assert status_of(ex.claim_detail, w.other_hr, claim_id) == 404
    assert status_of(ex.review_claim, w.sita, claim_id, {'decision': 'approve'}) == 403
    assert status_of(ex.review_claim, w.stranger, claim_id, {'decision': 'approve'}) == 403
    detail = ex.claim_detail(w.ram, claim_id)
    assert detail['canDecide'] is True
    line = next(i for i in detail['items'] if i['receiptFileId'])
    f = ex.claim_receipt(w.ram, claim_id, line['id'])
    assert f['mime'] == 'application/pdf'
    assert 'view_receipt' in [a['action'] for a in rows('SELECT action FROM audit_log WHERE row_id = %s', [line['id']])]
    assert status_of(ex.claim_receipt, w.stranger, claim_id, line['id']) == 404


def test_hr_approves_less_only_with_reason_payout_waits_for_payroll(policy_world):
    w = policy_world
    claim_id = _submitted_claim(w)
    assert ex.review_claim(w.ram, claim_id, {'decision': 'approve'})['state'] == 'pending'
    taxi = next(i for i in ex.claim_detail(w.hr, claim_id)['items'] if i['description'] == 'Taxi')
    with pytest.raises(BadRequestError, match='the approved amount cannot be more than claimed'):
        ex.review_claim(w.hr, claim_id, {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '1000'}]})
    with pytest.raises(BadRequestError, match='"Taxi": give a reason for approving less.'):
        ex.review_claim(w.hr, claim_id, {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '700'}]})
    ok = ex.review_claim(w.hr, claim_id, {'decision': 'approve', 'items': [{'id': taxi['id'], 'approvedAmount': '700', 'reason': 'Fare cap for this route'}]})
    assert ok['state'] == 'approved'
    after = ex.claim_detail(w.sita, claim_id)
    assert after['claim']['status'] == 'approved'
    assert after['claim']['totalApproved'] == '900.00'
    assert [(p['code'], p['amount'], p['status']) for p in after['payouts']] == [('EXP_REIMB', '900.00', 'pending')]
    assert len(ex.report_pending_payment(w.hr)['rows']) == 1
    assert status_of(ex.report_pending_payment, w.ram) == 403
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'expense_claim.approved' AND channel = 'push'")
    assert n == {'title': 'Expense claim approved', 'body': '"Client visit" was approved for Rs 900. It will be paid with your next salary.'}


def test_nothing_paid_is_refused(policy_world):
    w = policy_world
    claim_id = _submitted_claim(w)
    ex.review_claim(w.ram, claim_id, {'decision': 'approve'})
    items = ex.claim_detail(w.hr, claim_id)['items']
    with pytest.raises(BadRequestError, match='Nothing would be paid; reject the claim instead.'):
        ex.review_claim(w.hr, claim_id, {'decision': 'approve', 'items': [{'id': i['id'], 'approvedAmount': '0', 'reason': 'no'} for i in items]})


def test_rejecting_needs_a_reason_cancelling_pending_withdraws(policy_world):
    w = policy_world
    c = ex.create_claim(w.sita, {'title': 'Stationery', 'submit': True, 'items': [
        {'expenseDate': days_ago(1), 'categoryId': w.cats['OFFICE'], 'description': 'Notebooks', 'amount': '300', 'receiptFileId': upload(w.sita)}]})
    assert c['status'] == 'pending'
    assert status_of(ex.review_claim, w.ram, c['id'], {'decision': 'reject'}) == 400
    assert ex.cancel_claim(w.sita, c['id'])['status'] == 'cancelled'
    assert inbox_item(w.ram, c['id']) is None
    assert status_of(ex.cancel_claim, w.sita, c['id']) == 409


def test_rejection_tells_the_employee(policy_world):
    w = policy_world
    claim_id = _submitted_claim(w)
    assert ex.review_claim(w.ram, claim_id, {'decision': 'reject', 'reason': 'Personal trip'})['state'] == 'rejected'
    assert one('SELECT status, decision_note FROM expense_claim WHERE id = %s', [claim_id]) == {'status': 'rejected', 'decision_note': 'Personal trip'}
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'expense_claim.rejected' AND channel = 'in_app'")
    assert n == {'title': 'Expense claim rejected', 'body': '"Client visit" was rejected: Personal trip.'}


def test_monthly_limit_counts_other_open_claims_in_same_bs_month(policy_world):
    w = policy_world
    day = days_ago(1)
    first = ex.create_claim(w.sita, {'title': 'Lunch 1', 'submit': True, 'items': [
        {'expenseDate': day, 'categoryId': w.cats['MEALS'], 'description': 'Lunch', 'amount': '1400', 'receiptFileId': upload(w.sita)}]})
    second = ex.create_claim(w.sita, {'title': 'Lunch 2', 'submit': True, 'items': [
        {'expenseDate': day, 'categoryId': w.cats['MEALS'], 'description': 'Lunch', 'amount': '1400', 'receiptFileId': upload(w.sita)}]})
    assert first['status'] == second['status'] == 'pending'
    with pytest.raises(BadRequestError, match='monthly limit'):
        ex.create_claim(w.sita, {'title': 'Lunch 3', 'submit': True, 'items': [
            {'expenseDate': day, 'categoryId': w.cats['MEALS'], 'description': 'Lunch', 'amount': '400', 'receiptFileId': upload(w.sita)}]})
    ex.cancel_claim(w.sita, first['id'])
    ex.cancel_claim(w.sita, second['id'])


def test_hr_sees_everyone_manager_team_employee_only_themself(policy_world):
    w = policy_world
    _submitted_claim(w)
    _draft_claim(w)
    assert status_of(ex.list_claims, w.ram, {'scope': 'all'}) == 403
    all_ = ex.list_claims(w.hr)['claims']
    assert len(all_) > 0
    assert all(c['status'] != 'draft' for c in all_)
    mine = ex.list_claims(w.sita)['claims']
    assert all(c['employeeId'] == str(w.sita_emp.id) for c in mine) and len(mine) == 2
    assert ex.list_claims(w.other_hr)['claims'] == []
    assert len(ex.list_claims(w.ram)['claims']) == 1
    assert status_of(ex.list_claims, w.sita, {'scope': 'team'}) == 403


# ---------------------------------------------------------------- travel

def _pokhara(w, advance='10000'):
    return ex.request_travel(w.sita, {'destination': 'Pokhara', 'destinationCategory': 'city', 'purpose': 'Site survey',
                                      'fromDate': days_ago(4), 'toDate': days_ahead(1), 'mode': 'bus', 'estimatedCost': '2000',
                                      'advanceRequested': advance})


def test_quotes_daily_allowance_by_grade_and_lodging_per_night(policy_world):
    w = policy_world
    q = ex.travel_quote(w.sita, {'destinationCategory': 'city', 'fromDate': days_ahead(3), 'toDate': days_ahead(5)})['quote']
    assert {k: q[k] for k in ('days', 'nights', 'daRate', 'daAmount', 'lodgingLimit', 'grade')} == \
        {'days': 3, 'nights': 2, 'daRate': '2000.00', 'daAmount': '6000.00', 'lodgingLimit': '7000.00', 'grade': 'G3'}
    with pytest.raises(BadRequestError, match='Enter the US dollar exchange rate'):
        ex.travel_quote(w.sita, {'destinationCategory': 'international', 'fromDate': days_ahead(3), 'toDate': days_ahead(4)})
    q2 = ex.travel_quote(w.sita, {'destinationCategory': 'international', 'fromDate': days_ahead(3), 'toDate': days_ahead(4), 'fxRate': '136.5'})['quote']
    assert {k: q2[k] for k in ('currency', 'daRateNpr', 'daAmount', 'lodgingLimit')} == \
        {'currency': 'USD', 'daRateNpr': '6825.00', 'daAmount': '13650.00', 'lodgingLimit': '13650.00'}
    assert q2['fxRate'] == '136.5000'
    with pytest.raises(BadRequestError, match='The return date must be on or after the start date.'):
        ex.travel_quote(w.sita, {'destinationCategory': 'city', 'fromDate': days_ahead(5), 'toDate': days_ahead(3)})
    with pytest.raises(BadRequestError, match='A single trip can be at most 90 days.'):
        ex.travel_quote(w.sita, {'destinationCategory': 'city', 'fromDate': days_ahead(1), 'toDate': days_ahead(95)})


def test_request_asks_advance_up_to_expected_cost_overlap_refused(policy_world):
    w = policy_world
    with pytest.raises(BadRequestError, match=r'The advance cannot be more than the expected cost of the trip \(Rs 31,500\)\.'):
        _pokhara(w, '99999')
    t = _pokhara(w)
    assert t['status'] == 'pending'
    with pytest.raises(ConflictError, match='You already have a trip on these dates.'):
        ex.request_travel(w.sita, {'destination': 'Butwal', 'destinationCategory': 'city', 'purpose': 'Other',
                                   'fromDate': days_ago(1), 'toDate': days_ahead(2), 'mode': 'bus'})
    s = inbox_item(w.ram, t['id'])['summary']
    assert (s['destination'], s['days'], s['advance']) == ('Pokhara', 6, '10000.00')
    with pytest.raises(BadRequestError, match='This trip has already ended. Ask HR to record it.'):
        ex.request_travel(w.sita, {'destination': 'Old', 'destinationCategory': 'city', 'purpose': 'Old trip',
                                   'fromDate': days_ago(9), 'toDate': days_ago(8), 'mode': 'bus'})


def _approved_trip(w):
    t = _pokhara(w)
    assert status_of(ex.review_trip, w.ram, t['id'], {'decision': 'approve', 'advanceApproved': '20000'}) == 400
    approve_both(ex.review_trip, t['id'], w, {'advanceApproved': '8000'})
    return t['id']


def test_approvers_may_lower_advance_approval_queues_it_for_payroll(policy_world):
    w = policy_world
    trip_id = _approved_trip(w)
    t = ex.trip_detail(w.sita, trip_id)
    assert (t['trip']['status'], t['trip']['advanceApproved'], t['trip']['advanceStatus']) == ('approved', '8000.00', 'pending')
    assert [(p['code'], p['amount'], p['status']) for p in t['payouts']] == [('TRAVEL_ADV', '8000.00', 'pending')]
    assert t['canSettle'] is True
    assert status_of(ex.trip_detail, w.stranger, trip_id) == 404
    assert len(ex.report_advances(w.hr)['rows']) == 1


def test_hr_pays_advance_in_cash_payroll_never_pays_it(policy_world):
    w = policy_world
    trip_id = _approved_trip(w)
    assert status_of(ex.pay_advance_outside, w.ram, trip_id, {'note': 'cash'}) == 403
    assert ex.pay_advance_outside(w.hr, trip_id, {'note': 'Paid from petty cash'}) == {'id': trip_id, 'advanceStatus': 'paid'}
    assert one("SELECT status FROM expense_payout WHERE source_type = 'travel_advance' AND source_id = %s", [trip_id])['status'] == 'cash'
    assert status_of(ex.cancel_trip, w.sita, trip_id) == 409


def _settled_pokhara(w):
    trip_id = _approved_trip(w)
    ex.pay_advance_outside(w.hr, trip_id, {'note': 'Paid from petty cash'})
    bill = upload(w.sita, 'travel_bill')
    s = ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': [
        {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200', 'fileId': bill},
        {'kind': 'lodging', 'billDate': days_ago(4), 'description': 'Hotel', 'amount': '400'}]})
    return trip_id, s


def test_settlement_da_lodging_bills_minus_advance_overspend_recovered(policy_world):
    w = policy_world
    trip_id = _approved_trip(w)
    ex.pay_advance_outside(w.hr, trip_id, {'note': 'Paid from petty cash'})
    with pytest.raises(BadRequestError, match='The return date cannot be in the future.'):
        ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ahead(1), 'bills': []})
    with pytest.raises(BadRequestError, match=r'Bill 1: attach the bill for amounts above Rs 500\.'):
        ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(3), 'bills': [
            {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200'}]})
    bill = upload(w.sita, 'travel_bill')
    s = ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': [
        {'kind': 'transport', 'billDate': days_ago(4), 'description': 'Bus', 'amount': '1200', 'fileId': bill},
        {'kind': 'lodging', 'billDate': days_ago(4), 'description': 'Hotel', 'amount': '400'}]})
    # One day: DA 2000, no nights so lodging allowed 0, transport 1200 -> 3200 - 8000 advance = -4800 to recover.
    assert {k: s[k] for k in ('daAmount', 'lodgingAllowed', 'entitlement', 'advance', 'net')} == \
        {'daAmount': '2000.00', 'lodgingAllowed': '0.00', 'entitlement': '3200.00', 'advance': '8000.00', 'net': '-4800.00'}
    with pytest.raises(ConflictError, match='a settlement is already waiting for approval'):
        ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': []})
    approve_both(ex.review_settlement, s['id'], w)
    t = ex.trip_detail(w.hr, trip_id)
    assert t['trip']['status'] == 'settled'
    assert ('EXP_RECOVERY', '4800.00', 'pending') in [(p['code'], p['amount'], p['status']) for p in t['payouts']]
    bill_row = next(b for b in t['settlements'][0]['bills'] if b['fileId'])
    assert ex.bill_file(w.ram, s['id'], bill_row['id'])['mime'] == 'application/pdf'
    assert status_of(ex.bill_file, w.other_hr, s['id'], bill_row['id']) == 404
    n = one("SELECT body FROM notification_outbox WHERE template = 'travel_settlement.approved' AND channel = 'in_app'")
    assert n['body'] == 'Rs 4,800 of unspent advance for Pokhara will be recovered from your next salary.'


def test_settlement_rules(policy_world):
    w = policy_world
    trip_id = _approved_trip(w)
    with pytest.raises(NotFoundError):
        ex.settle_trip(w.ram, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': []})
    with pytest.raises(BadRequestError, match='Bill 1: the date must be within the trip.'):
        ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(4), 'bills': [
            {'kind': 'other', 'billDate': days_ago(3), 'description': 'Snacks', 'amount': '100'}]})
    with pytest.raises(BadRequestError, match='The return date must be on or after the start date.'):
        ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(3), 'actualTo': days_ago(4), 'bills': []})


def test_returned_settlement_reopens_the_trip(policy_world):
    w = policy_world
    trip_id = _approved_trip(w)
    s = ex.settle_trip(w.sita, trip_id, {'actualFrom': days_ago(4), 'actualTo': days_ago(3), 'bills': [
        {'kind': 'lodging', 'billDate': days_ago(4), 'description': 'Hotel', 'amount': '5000', 'fileId': upload(w.sita, 'travel_bill')}]})
    # Two days 4000 + lodging capped at one night 3500 - 8000 advance.
    assert (s['lodgingAllowed'], s['entitlement'], s['net']) == ('3500.00', '7500.00', '-500.00')
    assert ex.review_settlement(w.ram, s['id'], {'decision': 'reject', 'reason': 'Hotel bill unreadable'})['state'] == 'rejected'
    assert ex.trip_detail(w.sita, trip_id)['trip']['status'] == 'approved'
    assert ex.trip_detail(w.sita, trip_id)['canSettle'] is True


def test_unpicked_advance_cancelled_at_settlement_full_entitlement_paid(policy_world):
    w = policy_world
    t = ex.request_travel(w.sita, {'destination': 'Hetauda', 'destinationCategory': 'city', 'purpose': 'Training', 'fromDate': days_ago(2),
                                   'toDate': today(), 'mode': 'office_vehicle', 'advanceRequested': '3000'})
    approve_both(ex.review_trip, t['id'], w)
    s = ex.settle_trip(w.sita, t['id'], {'actualFrom': days_ago(2), 'actualTo': days_ago(2), 'bills': []})
    assert s['net'] == '-1000.00'
    approve_both(ex.review_settlement, s['id'], w)
    p = rows("""SELECT component_code, status, amount::text FROM expense_payout
                 WHERE (source_type = 'travel_advance' AND source_id = %s) OR (source_type = 'travel_settlement' AND source_id = %s)
                 ORDER BY created_at""", [t['id'], s['id']])
    assert p == [{'component_code': 'TRAVEL_ADV', 'status': 'cancelled', 'amount': '3000.00'},
                 {'component_code': 'EXP_REIMB', 'status': 'pending', 'amount': '2000.00'}]
    assert one('SELECT advance::text, net::text FROM travel_settlement WHERE id = %s', [s['id']]) == {'advance': '0.00', 'net': '2000.00'}


def test_cancel_trip_withdraws_advance_and_approval(policy_world):
    w = policy_world
    t = _pokhara(w)
    assert status_of(ex.cancel_trip, w.ram, t['id']) == 404
    assert ex.cancel_trip(w.sita, t['id']) == {'id': t['id'], 'status': 'cancelled'}
    assert inbox_item(w.ram, t['id']) is None
    assert status_of(ex.cancel_trip, w.sita, t['id']) == 409
    t2 = _pokhara(w)
    approve_both(ex.review_trip, t2['id'], w)
    assert ex.cancel_trip(w.hr, t2['id'])['status'] == 'cancelled'
    assert one("SELECT status FROM expense_payout WHERE source_id = %s", [t2['id']])['status'] == 'cancelled'


def test_reminds_travellers_to_settle_three_days_after_the_trip(policy_world):
    w = policy_world
    t = ex.request_travel(w.sita, {'destination': 'Dhulikhel', 'destinationCategory': 'valley', 'purpose': 'Offsite',
                                   'fromDate': days_ahead(10), 'toDate': days_ahead(10), 'mode': 'bus'})
    approve_both(ex.review_trip, t['id'], w)
    later = datetime.fromisoformat(f'{days_ahead(13)}T06:00:00+00:00').astimezone(timezone.utc)
    assert remind_travel_settlements(later) >= 1
    n = one("SELECT title, body FROM notification_outbox WHERE template = 'travel_settlement.reminder' AND channel = 'in_app'")
    assert n == {'title': 'Settle your trip',
                 'body': 'Your trip to Dhulikhel has ended. Submit the settlement with your bills under Claims & travel.'}
    # 30 days on, HR hears only about trips with an advance.
    assert remind_travel_settlements(datetime.fromisoformat(f'{days_ahead(40)}T06:00:00+00:00')) == 0


def test_hr_hears_about_unsettled_advances_after_thirty_days(policy_world):
    w = policy_world
    t = _pokhara(w)
    approve_both(ex.review_trip, t['id'], w)
    n = remind_travel_settlements(datetime.fromisoformat(f'{days_ahead(31)}T06:00:00+00:00'))
    assert n == 2  # both HR users
    assert one("SELECT body FROM notification_outbox WHERE template = 'travel_settlement.overdue' LIMIT 1")['body'] == \
        'A trip to Pokhara ended 30 days ago and its advance has not been settled.'


# ---------------------------------------------------------------- payroll

def _run(w, run_type='regular', bs=None):
    from engines.bs_calendar import ad_to_bs, bs_month_bounds
    b = ad_to_bs(today())
    y, m = bs or (b.year, b.month)
    bounds = bs_month_bounds(y, m)
    period = one("""INSERT INTO payroll_period (company_id, bs_year, bs_month, start_ad, end_ad, days) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING RETURNING id::text""", [str(w.co.id), y, m, bounds['startAd'], bounds['endAd'], bounds['days']]) \
        or one('SELECT id::text FROM payroll_period WHERE company_id = %s AND bs_year = %s AND bs_month = %s', [str(w.co.id), y, m])
    return one('INSERT INTO payroll_run (company_id, period_id, run_type) VALUES (%s, %s, %s) RETURNING id::text',
               [str(w.co.id), period['id'], run_type])['id']


def _payslip(run_id, emp_id, lines):
    ps = one("""INSERT INTO payslip (run_id, employee_id, inputs_snapshot, bs_days_in_month, paid_days, gross, taxable, tds,
                                     total_deductions, net) VALUES (%s, %s, '{}', 30, 30, 0, 0, 0, 0, 0) RETURNING id""", [run_id, emp_id])
    for code, typ, amount in lines:
        execute('INSERT INTO payslip_line (payslip_id, component_code, type, name_en, amount) VALUES (%s, %s, %s, %s, %s)',
                [ps['id'], code, typ, code, amount])


def _all_pending(w):
    """The TS state before its payroll block: Client visit (900), Pokhara recovery (4800), Hetauda (2000)."""
    claim_id = _approved_claim(w)
    _settled = _settled_pokhara(w)
    approve_both(ex.review_settlement, _settled[1]['id'], w)
    t = ex.request_travel(w.sita, {'destination': 'Hetauda', 'destinationCategory': 'city', 'purpose': 'Training', 'fromDate': days_ago(2),
                                   'toDate': today(), 'mode': 'office_vehicle', 'advanceRequested': '3000'})
    approve_both(ex.review_trip, t['id'], w)
    s = ex.settle_trip(w.sita, t['id'], {'actualFrom': days_ago(2), 'actualTo': days_ago(2), 'bills': []})
    approve_both(ex.review_settlement, s['id'], w)
    return claim_id


def test_preflight_shows_what_will_be_paid_and_recovered(policy_world):
    w = policy_world
    _all_pending(w)
    run_id = _run(w)
    p = pay.payout_preview(str(w.co.id), run_id, 'regular', [str(w.sita_emp.id), str(w.ram_emp.id)])
    assert p['count'] == 3
    assert re.search(r'Rs 2,900.*to pay.*Rs 4,800.*to recover', pay.preview_message(p))
    assert pay.preview_message(p) == ('This run includes Rs 2,900 of approved expenses and travel to pay and Rs 4,800 of unspent '
                                      'travel advances to recover (not taxed).')
    assert p['employees'] == [f'{w.sita_emp.emp_code}: 3 items']
    assert pay.payout_preview(str(w.co.id), run_id, 'bonus', [str(w.sita_emp.id)])['count'] == 0


def test_a_run_pays_the_reimbursement_exactly_once_and_recovers_the_advance(policy_world):
    w = policy_world
    claim_id = _all_pending(w)
    run_id = _run(w)
    co, emp = str(w.co.id), str(w.sita_emp.id)
    first = pay.claim_payouts(co, run_id, 'regular', emp)
    # Recomputing releases and re-claims: the same payouts, never doubled.
    pay.release_run_payouts(run_id)
    again = pay.claim_payouts(co, run_id, 'regular', emp)
    assert sorted((x['code'], x['amount']) for x in again) == sorted((x['code'], x['amount']) for x in first)
    assert sorted((x['code'], x['amount']) for x in again) == [('EXP_RECOVERY', '4800.00'), ('EXP_REIMB', '2000.00'), ('EXP_REIMB', '900.00')]
    assert one('SELECT count(*)::int AS n FROM expense_payout WHERE payroll_run_id = %s', [run_id])['n'] == 3
    # Not taxed: the components added for the payouts are non-taxable.
    comps = pay.with_payout_components(co, [{'code': 'BASIC', 'type': 'earning', 'nameEn': 'Basic', 'taxable': True}], again)
    assert [(c['code'], c['taxable']) for c in comps] == [('BASIC', True), ('EXP_RECOVERY', False), ('EXP_REIMB', False)] or \
        [(c['code'], c['taxable']) for c in comps] == [('BASIC', True), ('EXP_REIMB', False), ('EXP_RECOVERY', False)]
    # Another run of the same company cannot take them while this run holds them.
    other_run = _run(w, 'supplementary')
    assert pay.claim_payouts(co, other_run, 'supplementary', emp) == []
    # Arrears and bonus runs never carry them.
    assert pay.claim_payouts(co, run_id, 'arrears', emp) == []
    # Cash payment is refused while a run being prepared holds the payout.
    with pytest.raises(ConflictError, match='already in a pay run being prepared'):
        ex.pay_claim_outside(w.hr, claim_id, {'note': 'cash'})


def test_company_renamed_payout_component(policy_world):
    w = policy_world
    execute("""INSERT INTO salary_component (company_id, code, type, name_en, taxable, active)
               VALUES (%s, 'EXP_REIMB', 'earning', 'Reimbursements', false, true)""", [str(w.co.id)])
    comps = pay.with_payout_components(str(w.co.id), [], [{'code': 'EXP_REIMB'}, {'code': 'EXP_REIMB'}, {'code': 'OTHER'}])
    assert [(c['code'], c['nameEn'], c['sortOrder']) for c in comps] == [('EXP_REIMB', 'Reimbursements', 96)]
    base = [{'code': 'EXP_REIMB', 'nameEn': 'Mine'}]
    assert pay.with_payout_components(str(w.co.id), base, [{'code': 'EXP_REIMB'}]) is base


def test_locking_marks_payouts_and_claims_paid_later_run_pays_nothing(policy_world):
    w = policy_world
    _all_pending(w)
    run_id = _run(w)
    co, emp = str(w.co.id), str(w.sita_emp.id)
    pay.claim_payouts(co, run_id, 'regular', emp)
    _payslip(run_id, emp, [('EXP_REIMB', 'earning', '2900'), ('EXP_RECOVERY', 'deduction', '4800')])
    pay.settle_run_payouts(run_id)
    assert rows('SELECT DISTINCT status FROM expense_payout WHERE payroll_run_id = %s', [run_id]) == [{'status': 'paid'}]
    assert one("SELECT status FROM expense_claim WHERE title = 'Client visit'")['status'] == 'paid'
    assert ex.report_pending_payment(w.hr)['rows'] == []
    supp = _run(w, 'supplementary')
    assert pay.claim_payouts(co, supp, 'supplementary', emp) == []


def test_recovery_larger_than_pay_is_capped_balance_stays_outstanding(policy_world):
    w = policy_world
    co, ram = str(w.co.id), str(w.ram_emp.id)
    execute("""INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note)
               VALUES (%s, %s, 'travel_settlement', uuidv7(), 'EXP_RECOVERY', 900000, 'test')""", [co, ram])
    run_id = _run(w)
    assert [x['amount'] for x in pay.claim_payouts(co, run_id, 'regular', ram)] == ['900000.00']
    # Deductions are capped at the pay left: only 25,000 could be recovered.
    _payslip(run_id, ram, [('EXP_RECOVERY', 'deduction', '25000')])
    pay.settle_run_payouts(run_id)
    r = rows("""SELECT status, amount::text, paid_amount::text, parent_payout_id::text, note FROM expense_payout
                 WHERE employee_id = %s AND component_code = 'EXP_RECOVERY' ORDER BY parent_payout_id IS NOT NULL, created_at""", [ram])
    assert len(r) == 2
    paid, rest = r
    assert paid['status'] == 'paid'
    assert 0 < float(paid['paid_amount']) < 900000
    assert rest['status'] == 'pending'
    assert float(rest['amount']) + float(paid['paid_amount']) == pytest.approx(900000, abs=0.01)
    assert rest['parent_payout_id'] is not None
    assert rest['note'] == 'test (balance)'


def test_spend_report_groups_approved_claims_and_settlements_hr_only(policy_world):
    w = policy_world
    _all_pending(w)
    r = ex.report_spend(w.hr, {'from': days_ago(30), 'to': today(), 'groupBy': 'category'})
    groups = {x['group']: x['amount'] for x in r['rows']}
    assert {k: groups.get(k) for k in ('Local travel', 'Fuel (own vehicle)', 'Travel')} == \
        {'Local travel': '700.00', 'Fuel (own vehicle)': '200.00', 'Travel': '5200.00'}
    assert r['columns'][0] == {'key': 'group', 'header': 'Category'}
    m = ex.report_spend(w.hr, {'from': days_ago(30), 'to': today(), 'groupBy': 'month'})
    assert m['filename'] == 'expense-spend-by-month' and sum(x['items'] for x in m['rows']) == 4
    assert status_of(ex.report_spend, w.sita, {'from': days_ago(30), 'to': today()}) == 403
    with pytest.raises(BadRequestError, match='the end date must be after the start date'):
        ex.report_spend(w.hr, {'from': today(), 'to': days_ago(3)})


def test_pay_claim_outside_marks_it_paid(policy_world):
    w = policy_world
    claim_id = _approved_claim(w)
    assert status_of(ex.pay_claim_outside, w.ram, claim_id, {'note': 'cash'}) == 403
    with pytest.raises(Exception):
        ex.pay_claim_outside(w.hr, claim_id, {'note': 'x'})
    assert ex.pay_claim_outside(w.hr, claim_id, {'note': 'Petty cash, voucher 112'}) == {'id': claim_id, 'status': 'paid'}
    c = one('SELECT status, payment_method, decision_note FROM expense_claim WHERE id = %s', [claim_id])
    assert c['status'] == 'paid' and c['payment_method'] == 'cash'
    assert c['decision_note'].endswith('Paid outside payroll: Petty cash, voucher 112')
    assert one("SELECT status FROM expense_payout WHERE source_id = %s", [claim_id])['status'] == 'cash'
    assert status_of(ex.pay_claim_outside, w.hr, claim_id, {'note': 'again'}) == 409


def test_seed_is_idempotent(world):
    from hajiri.expenses.seeds import seed_expense_defaults
    assert seed_expense_defaults(str(world.co.id)) == {'expenseCategories': 0}
    assert ex.expense_policy(str(world.co.id)) == {'mileageRatePerKm': '15.00', 'limitMode': 'block', 'claimWindowDays': 90}


def test_rs_and_money_formatting():
    assert ex.rs('1500') == 'Rs 1,500'
    assert ex.rs('1234567.5') == 'Rs 12,34,567.5'
    assert ex.rs('-4800') == 'Rs -4,800'
    assert ex.money('2.005') == '2.01'
    assert ForbiddenError  # imported for symmetry with the TS statuses
