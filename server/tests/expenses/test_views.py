"""The expenses pages and HTMX fragments through django.test.Client (the TS test cases that went through the API)."""
from __future__ import annotations

from hajiri.core.db import one, rows
from hajiri.expenses import services as ex
from tests.web.factories import htmx, hx_triggers, set_features_off, toasts

from .helpers import approve_both, days_ago, days_ahead, today, upload


def line(cat, desc, date, amount='', km='', receipt=''):
    return {'expense_date': date, 'category_id': cat, 'description': desc, 'amount': amount, 'km': km, 'receipt_file_id': receipt}


def form(title, *lines, action='draft', **extra):
    data = {'title': title, 'action': action, **extra}
    for k in ('expense_date', 'category_id', 'description', 'amount', 'km', 'receipt_file_id'):
        data[k] = [ln[k] for ln in lines]
    return data


def test_pages_render_for_their_roles(policy_world):
    w = policy_world
    sita, ram, hr = w.client(w.sita), w.client(w.ram), w.client(w.hr)
    r = sita.get('/me/claims')
    assert r.status_code == 200 and b'No claims yet' in r.content and b'New claim' in r.content
    assert b'No trips yet' in sita.get('/me/claims/travel').content
    assert sita.get('/expenses').status_code in (302, 403)
    assert b'Nothing waiting for a decision' in ram.get('/expenses').content
    assert b'No trips waiting for a decision' in ram.get('/expenses/travel').content
    assert ram.get('/expenses/policies').status_code in (302, 403)
    p = hr.get('/expenses/policies')
    assert p.status_code == 200
    for text in (b'Claim rules', b'Expense categories', b'Fuel (own vehicle)', b'km \xc3\x97 NPR 20', b'Travel allowance (TADA)', b'Spending'):
        assert text in p.content
    assert b'To pay in the next pay run' in hr.get('/expenses').content


def test_feature_switch_hides_the_pages(policy_world):
    w = policy_world
    set_features_off(w.co, 'expenses')
    c = w.client(w.sita)
    assert c.get('/me/claims').status_code == 302
    assert c.get('/_/expenses/claims/new', **htmx()).status_code == 404


def test_employee_files_a_claim_through_the_drawer(policy_world):
    w = policy_world
    c = w.client(w.sita)
    r = c.get('/_/expenses/claims/new', **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'New expense claim' in r.content and b'Fuel (own vehicle)' in r.content
    # The live check: mileage at the company rate, a missing receipt blocks the submit button.
    chk = c.post('/_/expenses/claims/check', form('Client visit', line(w.cats['FUEL'], 'Bike', days_ago(2), km='12'),
                                                  line(w.cats['LOCAL_TRAVEL'], 'Taxi', days_ago(2), amount='900')), **htmx())
    assert chk.status_code == 200
    assert b'NPR 1,140' in chk.content and b'Line 2: attach a receipt for Local travel above Rs 500.' in chk.content
    # The receipt issue belongs to line 2: it is shown under that line (index 1), not in the claim-wide alert.
    assert b'disabled>Submit for approval' in chk.content and b'blocked = true' in chk.content
    assert b'&quot;item&quot;: 1' in chk.content and b'Fix before submitting' not in chk.content
    assert 'hj:chain-expense_claim' in chk.headers.get('HX-Trigger-After-Swap', '')
    # A claim-wide issue (the per-claim limit) is in the alert, titled by its level.
    over = c.post('/_/expenses/claims/check', form('Lunch', line(w.cats['MEALS'], 'Team lunch', days_ago(1), amount='1600',
                                                               receipt=upload(w.sita))), **htmx())
    assert b'Fix before submitting' in over.content and b'Meals: Rs 1,600 is over the limit of Rs 1,500 per claim.' in over.content
    # Incomplete lines: no figures yet and both buttons stay off.
    empty = c.post('/_/expenses/claims/check', form('x', line(w.cats['OFFICE'], '', days_ago(1), amount='')), **htmx())
    assert b'ready = false' in empty.content and b'disabled>Submit for approval' in empty.content
    # Submitting with the block is refused with the TS toast title.
    bad = c.post('/_/expenses/claims/new', form('Client visit', line(w.cats['LOCAL_TRAVEL'], 'Taxi', days_ago(2), amount='900'), action='submit'), **htmx())
    assert bad.status_code == 400 and toasts(bad)[0]['title'] == 'Not submitted'
    receipt = upload(w.sita)
    ok = c.post('/_/expenses/claims/new', form('Client visit', line(w.cats['LOCAL_TRAVEL'], 'Taxi', days_ago(2), amount='900', receipt=receipt),
                                               line(w.cats['FUEL'], 'Own bike', days_ago(2), km='10'), action='submit'), **htmx())
    assert ok.status_code == 200
    assert toasts(ok)[0] == {'message': 'Your manager has been asked to approve it.', 'tone': 'ok', 'title': 'Claim submitted'} or \
        toasts(ok)[0]['title'] == 'Claim submitted'
    assert ok.headers['HX-Retarget'] == '#hj-drawer-body' and 'hj:refresh-exp-claims' in hx_triggers(ok)
    assert b'Waiting for approval' in ok.content and b'Cancel claim' in ok.content
    claim = one("SELECT id::text, status, total_claimed::text FROM expense_claim WHERE title = 'Client visit'")
    assert claim['status'] == 'pending' and claim['total_claimed'] == '1100.00'
    page = c.get('/me/claims')
    assert b'Client visit' in page.content and b'Waiting for approval' in page.content
    assert c.get(f'/me/claims?open={claim["id"]}').content.count(claim['id'].encode()) >= 2


def test_draft_edit_and_submit(policy_world):
    w = policy_world
    c = w.client(w.sita)
    r = c.post('/_/expenses/claims/new', form('Pens', line(w.cats['OFFICE'], 'Pens', days_ago(1), amount='100')), **htmx())
    assert toasts(r)[0]['title'] == 'Draft saved'
    cid = one("SELECT id::text FROM expense_claim WHERE title = 'Pens'")['id']
    e = c.get(f'/_/expenses/claims/{cid}/edit', **htmx())
    assert e.status_code == 200 and b'Edit claim' in e.content and b'value="100.00"' in e.content
    r = c.post(f'/_/expenses/claims/{cid}/edit', form('Pens and paper', line(w.cats['FUEL'], 'Bike', days_ago(1), km='3'), claim_id=cid), **htmx())
    assert r.status_code == 200
    assert one('SELECT title, total_claimed::text FROM expense_claim WHERE id = %s', [cid]) == {'title': 'Pens and paper', 'total_claimed': '60.00'}
    assert w.client(w.ram).get(f'/_/expenses/claims/{cid}', **htmx()).status_code == 404
    s = c.post(f'/_/expenses/claims/{cid}/submit', **htmx())
    assert s.status_code == 200 and toasts(s)[0]['message'] == 'Claim submitted'
    assert c.post(f'/_/expenses/claims/{cid}/edit', form('x', line(w.cats['OFFICE'], 'x', days_ago(1), amount='1')), **htmx()).status_code == 409


def _submitted(w):
    d = ex.create_claim(w.sita, {'title': 'Client visit', 'submit': True, 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': upload(w.sita)},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Own bike', 'km': 10}]})
    return d['id']


def test_manager_then_hr_review_with_a_lowered_line_and_receipt(policy_world):
    w = policy_world
    cid = _submitted(w)
    ram, hr = w.client(w.ram), w.client(w.hr)
    assert w.client(w.stranger).get(f'/_/expenses/claims/{cid}', **htmx()).status_code == 404
    assert w.client(w.other_hr).get(f'/_/expenses/claims/{cid}', **htmx()).status_code == 404
    d = ram.get(f'/_/expenses/claims/{cid}', **htmx())
    assert b'Your decision' in d.content and b'Sita Sharma' in d.content
    assert b'Client visit' in ram.get('/expenses').content
    items = ex.claim_detail(w.ram, cid)['items']
    taxi = next(i for i in items if i['description'] == 'Taxi')
    f = ram.get(f'/_/expenses/claims/{cid}/items/{taxi["id"]}/receipt')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    assert w.client(w.stranger).get(f'/_/expenses/claims/{cid}/items/{taxi["id"]}/receipt').status_code == 404
    assert w.client(w.sita).post(f'/_/expenses/claims/{cid}/review', {'decision': 'approve'}, **htmx()).status_code == 403
    rj = ram.post(f'/_/expenses/claims/{cid}/review', {'decision': 'reject', 'reason': ''}, **htmx())
    assert rj.status_code == 400 and toasts(rj)[0]['message'] == 'a reason is required to reject'
    m = ram.post(f'/_/expenses/claims/{cid}/review', {'decision': 'approve', f'adj-{taxi["id"]}': ''}, **htmx())
    assert m.status_code == 200 and toasts(m)[0]['message'] == 'Claim approved'
    less = hr.post(f'/_/expenses/claims/{cid}/review', {'decision': 'approve', f'adj-{taxi["id"]}': '700'}, **htmx())
    assert less.status_code == 400 and toasts(less)[0]['message'] == '"Taxi": give a reason for approving less.'
    ok = hr.post(f'/_/expenses/claims/{cid}/review', {'decision': 'approve', f'adj-{taxi["id"]}': '700',
                                                       f'adjreason-{taxi["id"]}': 'Fare cap for this route'}, **htmx())
    assert ok.status_code == 200 and b'With the next salary' in ok.content and b'Fare cap for this route' in ok.content
    assert b'Paid another way?' in ok.content
    assert one('SELECT total_approved::text FROM expense_claim WHERE id = %s', [cid])['total_approved'] == '900.00'
    page = hr.get('/expenses?status=approved')
    assert b'Client visit' in page.content and b'NPR 900' in page.content
    csv = hr.get('/_/expenses/reports/pending-payment.csv')
    assert csv.status_code == 200 and 'text/csv' in csv['Content-Type'] and 'expenses-pending-payment.csv' in csv['Content-Disposition']
    assert ram.get('/_/expenses/reports/pending-payment.csv').status_code == 403
    assert ram.post(f'/_/expenses/claims/{cid}/pay-outside', {'note': 'cash'}, **htmx()).status_code == 403
    paid = hr.post(f'/_/expenses/claims/{cid}/pay-outside', {'note': 'Petty cash, voucher 112'}, **htmx())
    assert paid.status_code == 200 and toasts(paid)[0]['message'] == 'Marked as paid; payroll will not pay it'
    assert b'Paid outside payroll' in paid.content


def test_cancel_claim_from_drawer(policy_world):
    w = policy_world
    cid = _submitted(w)
    r = w.client(w.sita).post(f'/_/expenses/claims/{cid}/cancel', **htmx())
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Claim cancelled' and b'Cancelled' in r.content
    assert rows("SELECT state FROM workflow_instance WHERE object_id = %s", [cid]) == [{'state': 'cancelled'}]


def travel_form(**kw):
    return {'destination': 'Pokhara', 'destination_category': 'city', 'purpose': 'Site survey', 'from_date': days_ago(4),
            'to_date': days_ahead(1), 'mode': 'bus', 'estimated_cost': '2000', 'advance_requested': '10000', **kw}


def test_travel_request_quote_review_and_settlement(policy_world):
    w = policy_world
    sita, ram, hr = w.client(w.sita), w.client(w.ram), w.client(w.hr)
    f = sita.get('/_/expenses/travel/new', **htmx())
    assert f.status_code == 200 and b'Request travel' in f.content and b'Kathmandu valley' in f.content
    q = sita.get('/_/expenses/travel/quote', {'destination_category': 'city', 'from_date': days_ahead(3), 'to_date': days_ahead(5)}, **htmx())
    assert q.status_code == 200 and b'NPR 6,000' in q.content and b'NPR 7,000' in q.content and b'3 (2 nights)' in q.content
    assert b'grade G3' in q.content and b'value="13000.00"' in q.content, q.content.decode()
    intl = sita.get('/_/expenses/travel/quote', {'destination_category': 'international', 'from_date': days_ahead(3), 'to_date': days_ahead(4),
                                                 'fx_rate': '136.5'}, **htmx())
    assert b'NPR 13,650' in intl.content
    big = sita.post('/_/expenses/travel/new', travel_form(advance_requested='99999'), **htmx())
    assert big.status_code == 400 and toasts(big)[0]['title'] == 'Not requested'
    ok = sita.post('/_/expenses/travel/new', travel_form(), **htmx())
    assert ok.status_code == 200 and toasts(ok)[0]['title'] == 'Travel requested' and b'Trip to Pokhara' in ok.content
    tid = one("SELECT id::text FROM travel_request WHERE destination = 'Pokhara'")['id']
    dup = sita.post('/_/expenses/travel/new', travel_form(destination='Butwal', from_date=days_ago(1), to_date=days_ahead(2)), **htmx())
    assert dup.status_code == 409
    assert b'Pokhara' in ram.get('/expenses/travel').content
    assert w.client(w.stranger).get(f'/_/expenses/travel/{tid}', **htmx()).status_code == 404
    d = ram.get(f'/_/expenses/travel/{tid}', **htmx())
    assert b'Advance to approve (NPR)' in d.content
    assert ram.post(f'/_/expenses/travel/{tid}/review', {'decision': 'approve', 'advance_approved': '20000'}, **htmx()).status_code == 400
    assert toasts(ram.post(f'/_/expenses/travel/{tid}/review', {'decision': 'approve', 'advance_approved': '8000'}, **htmx()))[0]['message'] == 'Trip approved'
    r = hr.post(f'/_/expenses/travel/{tid}/review', {'decision': 'approve', 'advance_approved': '8000'}, **htmx())
    assert r.status_code == 200 and b'NPR 8,000 (asked NPR 10,000)' in r.content
    assert b'Advances not yet settled' in hr.get('/expenses/travel').content
    assert 'outstanding-travel-advances.csv' in hr.get('/_/expenses/reports/advances.csv')['Content-Disposition']
    assert ram.post(f'/_/expenses/travel/{tid}/advance-outside', {'note': 'cash'}, **htmx()).status_code == 403
    cash = hr.post(f'/_/expenses/travel/{tid}/advance-outside', {'note': 'Paid from petty cash'}, **htmx())
    assert toasts(cash)[0]['message'] == 'Advance marked as paid' and b'Paid outside payroll' in cash.content
    assert sita.post(f'/_/expenses/travel/{tid}/cancel', **htmx()).status_code == 409
    mine = sita.get(f'/_/expenses/travel/{tid}', **htmx())
    assert b'Settle this trip' in mine.content
    assert sita.get('/_/expenses/travel/bill-row', {'date': days_ago(4)}, **htmx()).status_code == 200
    bill = upload(w.sita, 'travel_bill')
    data = {'actual_from': days_ago(4), 'actual_to': days_ago(4), 'bill_kind': ['transport', 'lodging'], 'bill_date': [days_ago(4)] * 2,
            'bill_description': ['Bus', 'Hotel'], 'bill_amount': ['1200', '400'], 'bill_file_id': [bill, '']}
    pv = sita.post(f'/_/expenses/travel/{tid}/settlement/preview', data, **htmx())
    assert pv.status_code == 200 and b'You return' in pv.content and b'NPR 4,800' in pv.content and b'Allowance (1 day)' in pv.content
    no_file = sita.post(f'/_/expenses/travel/{tid}/settlement', {**data, 'bill_file_id': ['', '']}, **htmx())
    assert no_file.status_code == 400 and toasts(no_file)[0]['title'] == 'Not submitted'
    s = sita.post(f'/_/expenses/travel/{tid}/settlement', data, **htmx())
    assert s.status_code == 200 and toasts(s)[0] == {'message': 'Sent for approval.', 'tone': 'ok', 'title': 'Settlement submitted'}
    assert b'To recover from the employee' in s.content
    sid = one('SELECT id::text FROM travel_settlement WHERE travel_request_id = %s', [tid])['id']
    assert toasts(ram.post(f'/_/expenses/settlements/{sid}/review', {'decision': 'approve'}, **htmx()))[0]['message'] == 'Settlement approved'
    done = hr.post(f'/_/expenses/settlements/{sid}/review', {'decision': 'approve'}, **htmx())
    assert done.status_code == 200 and b'Settled' in done.content and b'To be recovered from the next salary' in done.content
    b = one('SELECT id::text FROM travel_bill WHERE settlement_id = %s AND file_id IS NOT NULL', [sid])['id']
    assert ram.get(f'/_/expenses/settlements/{sid}/bills/{b}/file').status_code == 200
    assert w.client(w.other_hr).get(f'/_/expenses/settlements/{sid}/bills/{b}/file').status_code == 404
    assert b'Pokhara' in sita.get('/me/claims/travel').content


def test_trip_reject_and_cancel(policy_world):
    w = policy_world
    sita = w.client(w.sita)
    sita.post('/_/expenses/travel/new', travel_form(), **htmx())
    tid = one("SELECT id::text FROM travel_request WHERE destination = 'Pokhara'")['id']
    r = sita.post(f'/_/expenses/travel/{tid}/cancel', **htmx())
    assert toasts(r)[0]['message'] == 'Trip cancelled'
    sita.post('/_/expenses/travel/new', travel_form(destination='Butwal'), **htmx())
    t2 = one("SELECT id::text FROM travel_request WHERE destination = 'Butwal'")['id']
    rj = w.client(w.ram).post(f'/_/expenses/travel/{t2}/review', {'decision': 'reject', 'reason': 'No budget'}, **htmx())
    assert toasts(rj)[0]['message'] == 'Trip rejected' and b'No budget' in rj.content


def test_hr_edits_policies(policy_world):
    w = policy_world
    hr, sita = w.client(w.hr), w.client(w.sita)
    r = hr.post('/_/expenses/policy/settings', {'mileage_rate_per_km': '25', 'limit_mode': 'warn', 'claim_window_days': '30'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Expense rules saved'
    assert ex.expense_policy(str(w.co.id)) == {'mileageRatePerKm': '25.00', 'limitMode': 'warn', 'claimWindowDays': 30}
    bad = hr.post('/_/expenses/policy/settings', {'mileage_rate_per_km': '25', 'limit_mode': 'warn', 'claim_window_days': '3'}, **htmx())
    assert bad.status_code == 400
    assert sita.post('/_/expenses/policy/settings', {'mileage_rate_per_km': '25', 'limit_mode': 'warn', 'claim_window_days': '30'}, **htmx()).status_code == 403
    n = hr.get('/_/expenses/categories/new', **htmx())
    assert b'New category' in n.content and b'Every grade (*)' not in n.content
    lr = hr.get('/_/expenses/categories/limit-row', {'taken': ['*']}, **htmx())
    assert b'value="G3" selected' in lr.content
    c = hr.post('/_/expenses/categories/new', {'code': 'training', 'name': 'Training fees', 'kind': 'amount', 'receipt_required_above': '0',
                                              'limit_grade': ['G3'], 'limit_per_claim': ['5000'], 'limit_per_month': ['']}, **htmx())
    assert c.status_code == 204 and toasts(c)[0]['message'] == 'Category saved' and 'hj:refresh-exp-categories' in hx_triggers(c)
    cat = one("SELECT id::text, receipt_required_above::text FROM expense_category WHERE code = 'TRAINING' AND company_id = %s", [str(w.co.id)])
    assert cat['receipt_required_above'] == '0.00'
    assert rows('SELECT grade, per_claim::text FROM expense_limit WHERE category_id = %s', [cat['id']]) == [{'grade': 'G3', 'per_claim': '5000.00'}]
    e = hr.get(f'/_/expenses/categories/{cat["id"]}/edit', **htmx())
    assert b'Training fees' in e.content and b'Employees can choose this category' in e.content
    dup = hr.post(f'/_/expenses/categories/{cat["id"]}/edit', {'name': 'Training fees', 'kind': 'amount', 'limit_grade': ['G3', 'G3'],
                                                              'limit_per_claim': ['1', '2'], 'limit_per_month': ['', '']}, **htmx())
    assert dup.status_code == 400 and toasts(dup)[0]['message'] == 'each grade can appear only once'
    hide = hr.post(f'/_/expenses/categories/{cat["id"]}/edit', {'name': 'Training', 'kind': 'amount', 'receipt_required_above': ''}, **htmx())
    assert hide.status_code == 204
    assert one('SELECT name, active, receipt_required_above FROM expense_category WHERE id = %s', [cat['id']]) == \
        {'name': 'Training', 'active': False, 'receipt_required_above': None}
    assert b'Hidden' in hr.get('/_/expenses/categories', **htmx()).content
    assert w.client(w.other_hr).get(f'/_/expenses/categories/{cat["id"]}/edit', **htmx()).status_code == 404
    t = hr.post('/_/expenses/tada', {'grade': ['*', 'G3'], 'da|*|valley': '1100', 'lodging|*|valley': '2600', 'da|G3|city': '2100'}, **htmx())
    assert t.status_code == 204 and toasts(t)[0]['message'] == 'Travel rates saved'
    assert rows("SELECT grade, destination, da_rate::text, lodging_rate::text FROM tada_rate WHERE company_id = %s ORDER BY grade",
                [str(w.co.id)]) == [{'grade': '*', 'destination': 'valley', 'da_rate': '1100.00', 'lodging_rate': '2600.00'},
                                    {'grade': 'G3', 'destination': 'city', 'da_rate': '2100.00', 'lodging_rate': '0.00'}]
    assert b'name="da|G3|valley"' in hr.get('/_/expenses/tada/grade-row', {'grade': 'G3'}, **htmx()).content


def test_spend_report_page_and_csv(policy_world):
    w = policy_world
    cid = _submitted(w)
    approve_both(ex.review_claim, cid, w)
    hr = w.client(w.hr)
    r = hr.get('/_/expenses/reports/spend', {'from': days_ago(30), 'to': today(), 'group_by': 'category'}, **htmx())
    assert r.status_code == 200 and b'Local travel' in r.content and b'NPR 900' in r.content and b'82%' in r.content
    csv = hr.get('/_/expenses/reports/spend', {'from': days_ago(30), 'to': today(), 'group_by': 'month', 'format': 'csv'})
    assert 'text/csv' in csv['Content-Type'] and 'expense-spend-by-month.csv' in csv['Content-Disposition']
    assert w.client(w.sita).get('/_/expenses/reports/spend', {'from': days_ago(30), 'to': today()}, **htmx()).status_code == 403


def test_inbox_card_links_to_the_review_pages(policy_world):
    w = policy_world
    cid = _submitted(w)
    page = w.client(w.ram).get('/approvals')
    assert page.status_code == 200 and f'/expenses?open={cid}'.encode() in page.content


def test_claim_form_lines_and_drawer_amounts_follow_the_ts_page(policy_world):
    """ClaimForm: numbered lines, the receipt hint by category, the km rate; ClaimDrawer amounts without paisa when whole."""
    w = policy_world
    c = w.client(w.sita)
    f = c.get('/_/expenses/claims/new', **htmx('hj-drawer-body')).content
    assert b'Expense 1' in f and b'Receipt (optional)' in f and b'NPR 20.00 per km' in f
    assert b'data-receipt="Receipt needed above NPR 500"' in f and b'data-receipt="Receipt needed"' in f
    # Mileage categories never ask for a receipt.
    assert f'value="{w.cats["FUEL"]}" data-kind="mileage" data-receipt="Receipt (optional)"'.encode() in f
    assert b'Up to 2 decimals' in f and b':disabled="!ready || !title.trim()"' in f
    cid = _submitted(w)
    d = w.client(w.ram).get(f'/_/expenses/claims/{cid}', **htmx()).content
    assert b'NPR 1,100' in d and b'NPR 1,100.00' not in d and b'fmt(total())' in d
    t = w.client(w.sita).post('/_/expenses/travel/new', travel_form(), **htmx()).content
    assert b'NPR 12,000' in t and b'NPR 12,000.00' not in t
