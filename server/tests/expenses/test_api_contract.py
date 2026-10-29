"""
Field-by-field contract of the /api/v1/expenses responses the React pages read (frontend/src/pages/expenses/*.jsx,
approvalSummary.jsx, hr/ReportsPage.jsx), against the TS routes at 65a9079 (apps/api/src/modules/expenses/routes.ts):
every key the old SQL selected, with the old types (money and dates as strings, counts as numbers, timestamps as
JSON.stringify ISO strings, nulls where nothing is set), and the round trip of the edit form.
"""
from __future__ import annotations

import re

from tests.web.factories import make_user, sign_in

from .helpers import days_ago, today, upload
from .test_api_expenses import Api, _approve_both, _request_pokhara

ISO_INSTANT = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$')
ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MONEY = re.compile(r'^-?\d+\.\d{2}$')

CLAIM_KEYS = {'id', 'title', 'status', 'totalClaimed', 'totalApproved', 'paymentMethod', 'decisionNote', 'submittedAt',
              'decidedAt', 'paidAt', 'createdAt', 'employee'}
ITEM_KEYS = {'id', 'expenseDate', 'categoryId', 'categoryName', 'categoryKind', 'description', 'amount', 'km', 'receiptFileId',
             'receiptName', 'receiptMime', 'approvedAmount', 'adjustReason'}
TRIP_ROW_KEYS = {'id', 'destination', 'destinationCategory', 'purpose', 'fromDate', 'toDate', 'mode', 'days', 'daAmount',
                 'lodgingLimit', 'estimatedCost', 'advanceRequested', 'advanceApproved', 'advanceStatus', 'advanceMethod', 'status',
                 'createdAt', 'employeeId', 'empCode', 'employeeName', 'settlement'}
TRIP_KEYS = {'id', 'destination', 'destinationCategory', 'purpose', 'fromDate', 'toDate', 'mode', 'estimatedCost',
             'advanceRequested', 'advanceApproved', 'advanceStatus', 'advanceMethod', 'fxRate', 'days', 'nights', 'daRate',
             'lodgingRate', 'daAmount', 'lodgingLimit', 'status', 'decisionNote', 'decidedAt', 'createdAt', 'destinationLabel',
             'employee'}
SETTLEMENT_KEYS = {'id', 'status', 'actualFrom', 'actualTo', 'days', 'nights', 'daAmount', 'lodgingClaimed', 'lodgingAllowed',
                   'otherBills', 'entitlement', 'advance', 'net', 'note', 'decisionNote', 'createdAt', 'workflowInstanceId', 'bills'}
TRIP_PAYOUT_KEYS = {'id', 'sourceType', 'code', 'amount', 'status', 'paidAmount', 'paidAt', 'bsYear', 'bsMonth'}
QUOTE_KEYS = {'days', 'nights', 'currency', 'daRate', 'lodgingRate', 'fxRate', 'daRateNpr', 'lodgingRateNpr', 'daAmount',
              'lodgingLimit', 'grade'}


def test_claim_detail_has_every_ts_field_and_the_edit_form_round_trips(policy_world):
    w = policy_world
    sita = Api(w.sita)
    receipt = upload(w.sita)
    d = sita.post('/claims', {'title': 'Client visit', 'items': [
        {'expenseDate': days_ago(2), 'categoryId': w.cats['LOCAL_TRAVEL'], 'description': 'Taxi', 'amount': '900', 'receiptFileId': receipt},
        {'expenseDate': days_ago(2), 'categoryId': w.cats['FUEL'], 'description': 'Own bike', 'km': 12.5, 'receiptFileId': None}]})
    assert d.status_code == 201, d.content
    cid = d.json()['id']
    body = sita.get(f'/claims/{cid}').json()
    c = body['claim']
    assert set(c) == CLAIM_KEYS
    assert (c['status'], c['totalClaimed'], c['totalApproved'], c['paymentMethod'], c['decisionNote']) == \
        ('draft', '1150.00', None, None, None)
    assert c['submittedAt'] is None and c['decidedAt'] is None and c['paidAt'] is None
    assert ISO_INSTANT.match(c['createdAt'])
    assert body['payouts'] == [] and body['issues'] == [] and body['canDecide'] is False
    taxi, bike = body['items']
    assert set(taxi) == ITEM_KEYS and set(bike) == ITEM_KEYS
    assert (taxi['expenseDate'], taxi['categoryName'], taxi['categoryKind'], taxi['amount'], taxi['km']) == \
        (days_ago(2), 'Local travel', 'amount', '900.00', None)
    assert (taxi['receiptFileId'], taxi['receiptName'], taxi['approvedAmount'], taxi['adjustReason']) == \
        (receipt, 'bill.pdf', None, None)
    assert bike['categoryKind'] == 'mileage' and bike['amount'] == '250.00' and float(bike['km']) == 12.5
    assert (bike['receiptFileId'], bike['receiptName'], bike['receiptMime']) == (None, None, None)

    # ClaimForm rebuilds the lines from the detail (amount for amount lines, Number(km) for mileage) and PUTs them back.
    lines = []
    for i in body['items']:
        line = {'expenseDate': i['expenseDate'], 'categoryId': i['categoryId'], 'description': i['description'],
                'receiptFileId': i['receiptFileId']}
        line.update({'km': float(i['km'])} if i['categoryKind'] == 'mileage' else {'amount': i['amount']})
        lines.append(line)
    chk = sita.post('/claims/check', {'items': lines, 'claimId': cid})
    assert chk.status_code == 200 and chk.json() == {'amounts': ['900.00', '250.00'], 'total': '1150.00', 'issues': []}
    put = sita.put(f'/claims/{cid}', {'title': 'Client visit', 'items': lines, 'submit': True})
    assert put.status_code == 200 and put.json() == {'id': cid, 'status': 'pending', 'total': '1150.00', 'issues': []}

    after = Api(w.ram).get(f'/claims/{cid}').json()
    assert ISO_INSTANT.match(after['claim']['submittedAt']) and after['canDecide'] is True
    row = Api(w.ram).get('/claims', scope='team', status='pending').json()['claims'][0]
    assert row['id'] == cid and isinstance(row['items'], int) and row['totalApproved'] is None
    assert (row['payoutStatus'], row['payBsYear'], row['payBsMonth'], row['paymentMethod']) == (None, None, None, None)
    assert ISO_INSTANT.match(row['submittedAt']) and ISO_INSTANT.match(row['createdAt']) and row['decidedAt'] is None
    assert (row['firstDate'], row['lastDate']) == (days_ago(2), days_ago(2))

    _approve_both(w, f'/claims/{cid}/review')
    row = Api(w.sita).get('/claims', scope='own').json()['claims'][0]
    assert (row['status'], row['totalApproved'], row['paymentMethod'], row['payoutStatus']) == ('approved', '1150.00', 'payroll', 'pending')
    assert ISO_INSTANT.match(row['decidedAt'])
    p = Api(w.sita).get(f'/claims/{cid}').json()['payouts'][0]
    assert (p['code'], p['amount'], p['status'], p['paidAmount'], p['paidAt'], p['bsYear'], p['bsMonth']) == \
        ('EXP_REIMB', '1150.00', 'pending', None, None, None, None)
    Api(w.hr).post(f'/claims/{cid}/pay-outside', {'note': 'Petty cash'})
    paid = Api(w.sita).get(f'/claims/{cid}').json()
    assert paid['claim']['status'] == 'paid' and paid['claim']['paymentMethod'] == 'cash'
    assert paid['claim']['decisionNote'] == 'Paid outside payroll: Petty cash' and ISO_INSTANT.match(paid['claim']['paidAt'])
    assert paid['payouts'][0]['status'] == 'cash' and paid['payouts'][0]['paidAmount'] == '1150.00'
    assert ISO_INSTANT.match(paid['payouts'][0]['paidAt'])


def test_trip_list_detail_settlement_and_reports_have_every_ts_field(policy_world):
    w = policy_world
    q = Api(w.sita).post('/travel/quote', {'destinationCategory': 'city', 'fromDate': days_ago(4), 'toDate': today()}).json()['quote']
    assert set(q) == QUOTE_KEYS and isinstance(q['days'], int) and isinstance(q['nights'], int)
    assert (q['daRateNpr'], q['lodgingRateNpr']) == ('2000.00', '3500.00')
    t = _request_pokhara(w)
    assert t.status_code == 201
    tid = t.json()['id']
    assert set(t.json()['quote']) == QUOTE_KEYS

    row = Api(w.sita).get('/travel', scope='own').json()['trips'][0]
    assert set(row) == TRIP_ROW_KEYS
    assert (row['destinationCategory'], row['mode'], row['days'], row['estimatedCost'], row['advanceRequested']) == \
        ('city', 'bus', 6, '2000.00', '10000.00')
    assert (row['advanceApproved'], row['advanceStatus'], row['advanceMethod'], row['settlement']) == (None, 'none', None, None)
    assert ISO_DATE.match(row['fromDate']) and ISO_INSTANT.match(row['createdAt']) and row['employeeName'] == 'Sita Sharma'

    detail = Api(w.ram).get(f'/travel/{tid}').json()
    trip = detail['trip']
    assert set(trip) == TRIP_KEYS
    assert (trip['fxRate'], trip['nights'], trip['daRate'], trip['lodgingRate'], trip['decisionNote'], trip['decidedAt']) == \
        (None, 5, '2000.00', '3500.00', None, None)
    assert trip['employee'] == {'id': str(w.sita_emp.id), 'empCode': w.sita_emp.emp_code, 'name': 'Sita Sharma', 'grade': 'G3'}
    assert (detail['settlements'], detail['payouts'], detail['canDecide'], detail['canDecideSettlement'], detail['canSettle']) == \
        ([], [], True, False, False)

    _approve_both(w, f'/travel/{tid}/review', {'advanceApproved': '8000', 'reason': 'Bus fare only'})
    trip = Api(w.sita).get(f'/travel/{tid}').json()['trip']
    assert (trip['advanceMethod'], trip['decisionNote']) == ('payroll', 'Bus fare only') and ISO_INSTANT.match(trip['decidedAt'])
    adv = Api(w.hr).get('/reports/advances').json()['rows'][0]
    assert set(adv) == {'id', 'empCode', 'employeeName', 'destination', 'fromDate', 'toDate', 'advance', 'advanceStatus', 'status', 'trip'}
    assert (adv['id'], adv['advance'], adv['advanceStatus'], adv['status']) == (tid, '8000.00', 'pending', 'approved')
    pend = Api(w.hr).get('/reports/pending-payment').json()['rows'][0]
    assert set(pend) == {'id', 'empCode', 'employeeName', 'sourceType', 'sourceId', 'code', 'amount', 'note', 'createdAt', 'bsYear',
                         'bsMonth', 'kind', 'inRun'}
    assert (pend['sourceType'], pend['sourceId'], pend['code'], pend['amount'], pend['note'], pend['kind'], pend['inRun']) == \
        ('travel_advance', tid, 'TRAVEL_ADV', '8000.00', 'Travel advance: Pokhara', 'Pay', 'Next pay run')
    assert pend['bsYear'] is None and ISO_INSTANT.match(pend['createdAt'])

    bill = upload(w.sita, 'travel_bill')
    s = Api(w.sita).post(f'/travel/{tid}/settlement', {'actualFrom': days_ago(4), 'actualTo': days_ago(3), 'note': 'Back early', 'bills': [
        {'kind': 'lodging', 'billDate': days_ago(4), 'description': 'Hotel', 'amount': '4000', 'fileId': bill},
        {'kind': 'other', 'billDate': days_ago(3), 'description': 'Tea', 'amount': '50', 'fileId': None}]})
    assert s.status_code == 201, s.content
    # Two days of DA 2000, one night of lodging capped at 3500, tea 50: 7550 less the 8000 advance.
    assert s.json()['entitlement'] == '7550.00' and s.json()['net'] == '-450.00'
    d = Api(w.ram).get(f'/travel/{tid}').json()
    assert d['canDecideSettlement'] is True and d['trip']['status'] == 'settling'
    st = d['settlements'][0]
    assert set(st) == SETTLEMENT_KEYS
    assert (st['status'], st['days'], st['nights'], st['lodgingClaimed'], st['lodgingAllowed'], st['otherBills'], st['note'],
            st['decisionNote']) == ('pending', 2, 1, '4000.00', '3500.00', '50.00', 'Back early', None)
    assert ISO_INSTANT.match(st['createdAt']) and st['workflowInstanceId']
    assert [(b['kind'], b['billDate'], b['amount'], b['fileName']) for b in st['bills']] == \
        [('lodging', days_ago(4), '4000.00', 'bill.pdf'), ('other', days_ago(3), '50.00', None)]
    assert all(set(p) == TRIP_PAYOUT_KEYS for p in d['payouts'])
    assert [(p['sourceType'], p['code']) for p in d['payouts']] == [('travel_advance', 'TRAVEL_ADV')]
    listed = Api(w.sita).get('/travel').json()['trips'][0]['settlement']
    assert listed == {'id': s.json()['id'], 'status': 'pending', 'net': '-450.00'}


def test_hr_without_an_employee_record_reads_policy_and_lists(policy_world):
    w = policy_world
    admin = make_user(w.co, roles=['hr'], email='hr-only@demo.local', name='HR desk')
    hr = Api(admin)
    p = hr.get('/policy').json()
    assert p['myGrade'] is None and 'G3' in p['grades']
    assert hr.get('/claims').json() == {'claims': []}
    assert hr.get('/travel').json() == {'trips': []}
    mine = hr.get('/claims', scope='own')
    assert mine.status_code == 400 and mine.json()['error'] == 'your account is not linked to an employee'
    q = hr.post('/travel/quote', {'destinationCategory': 'city', 'fromDate': today(), 'toDate': today()})
    assert q.status_code == 400 and q.json()['error'] == 'your account is not linked to an employee'


def test_bad_list_filters_and_report_formats_answer_like_the_ts_routes(policy_world):
    w = policy_world
    hr = Api(w.hr)
    bad = hr.get('/claims', status='lost')
    assert bad.status_code == 400 and bad.json()['error'] == 'validation' and bad.json()['issues'][0]['path'] == 'status'
    assert hr.get('/travel', scope='everyone').status_code == 400
    assert Api(w.sita).get('/claims', scope='all').json()['error'] == 'only HR can see everyone'
    assert Api(w.sita).get('/travel', scope='team').json()['error'] == 'only managers can see their team'
    fmt = hr.get('/reports/advances', format='xml')
    assert fmt.status_code == 400 and fmt.json()['error'] == 'validation'
    assert hr.get('/reports/pending-payment', format='pdf').status_code == 400
    assert hr.get('/reports/spend', **{'from': days_ago(3), 'to': today(), 'format': 'xls'}).status_code == 400
    assert hr.get('/reports/spend', **{'from': days_ago(3), 'to': today(), 'groupBy': 'team'}).status_code == 400
    bad_id = hr.get('/claims/not-a-uuid')
    assert bad_id.status_code == 400 and bad_id.json()['error'] == 'validation'
    assert [i['message'] for i in bad_id.json()['issues']] == ['Invalid UUID']
