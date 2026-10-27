"""Expense claims, trips and tax-saving declarations (port of apps/api/src/db/demo-suite/money.ts)."""
from __future__ import annotations

import base64
import re

from hajiri.core.db import execute, one, rows
from hajiri.expenses import services as expenses
from hajiri.files.service import upload
from hajiri.payroll import tax_savings

from .context import DemoContext, already_seeded
from .kit import Actor, ApiError, add_days_iso, make_pdf

CLAIMS = [
    {'who': 1, 'title': 'Client visit to Nabil Bank', 'items': [(-6, 'LOCAL_TRAVEL', 'Taxi to Kamaladi and back', '850.00'),
                                                               (-6, 'MEALS', 'Lunch with the client team', '2400.00')], 'then': 'approve'},
    {'who': 2, 'title': 'Internet for work from home (Bhadra)', 'items': [(-20, 'MOBILE', 'WorldLink fibre, Bhadra', '1500.00')], 'then': 'approve'},
    {'who': 4, 'title': 'KU workshop supplies', 'items': [(-3, 'OFFICE', 'Printing handouts for 24 students', '1800.00'),
                                                         (-3, 'LOCAL_TRAVEL', 'Bus to Dhulikhel and back', '400.00')], 'then': 'submit'},
    {'who': 6, 'title': 'Datacenter night shift meals', 'items': [(-2, 'MEALS', 'Dinner during the rack cut-over', '950.00')], 'then': 'submit'},
    {'who': 7, 'title': 'Fuel for site visits', 'items': [(-9, 'FUEL', 'Bhaisepati datacenter, two round trips', '0', '36')], 'then': 'approve'},
    {'who': 8, 'title': 'Team lunch (not reimbursable)', 'items': [(-15, 'MEALS', 'Team lunch at Thamel', '6800.00')], 'then': 'reject'},
    {'who': 3, 'title': 'Conference ticket', 'items': [(-1, 'OTHER', 'Kathmandu Cloud Summit ticket', '3500.00')], 'then': 'draft'},
]


def seed_expenses(ctx: DemoContext) -> None:
    """Expense claims and trips in every state: draft, waiting for the manager, approved for payroll, a trip with an advance and its settlement."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'expenses', "SELECT 1 FROM travel_request WHERE company_id = %s AND destination = 'Biratnagar'",
                      'SELECT 1 FROM expense_claim WHERE company_id = %s LIMIT 1'):
        return
    policy = kit.call('GET /expenses/policy', expenses.get_policy, hr.user)

    def cat(code: str) -> str:
        return str(next(c for c in policy['categories'] if c['code'] == code)['id'])
    # Travel allowance rates by grade (the * row applies to every grade without its own).
    try:
        kit.call('PUT /expenses/tada-rates', expenses.set_tada_rates, hr.user, {'rates': [
            {'grade': '*', 'destination': 'valley', 'daRate': '800.00', 'lodgingRate': '0.00'},
            {'grade': '*', 'destination': 'city', 'daRate': '1500.00', 'lodgingRate': '3500.00'},
            {'grade': '*', 'destination': 'rural', 'daRate': '1200.00', 'lodgingRate': '2500.00'},
            {'grade': '*', 'destination': 'international', 'daRate': '60.00', 'lodgingRate': '150.00'},
            {'grade': 'G7', 'destination': 'city', 'daRate': '2200.00', 'lodgingRate': '5000.00'},
        ]})
    except ApiError:
        pass

    def receipt(who: Actor, what: str, amount: str) -> str:
        pdf = make_pdf(f'Receipt: {what}', [f'Amount: NPR {amount}', 'Paid in cash', 'Thank you'])
        filename = re.sub(r'\W+', '-', what.lower()) + '.pdf'
        return str(kit.call('POST /files', upload, who.user, {'purpose': 'expense_receipt', 'filename': filename,
                                                             'dataUrl': f'data:application/pdf;base64,{base64.b64encode(pdf).decode()}'})['id'])

    people = [e for e in ctx.employees if e.manager_id]
    for c in CLAIMS:
        e = people[c['who'] % len(people)]
        me = ctx.as_(e)
        items = []
        for it in c['items']:
            back, code, description, amount = it[:4]
            km = it[4] if len(it) > 4 else None
            day = add_days_iso(ctx.today, back)
            if km:
                items.append({'expenseDate': day, 'categoryId': cat(code), 'description': description, 'km': int(km)})
            else:
                items.append({'expenseDate': day, 'categoryId': cat(code), 'description': description, 'amount': amount,
                              'receiptFileId': receipt(me, description, amount) if float(amount) >= 500 else None})
        r = kit.call('POST /expenses/claims', expenses.create_claim, me.user, {'title': c['title'], 'items': items, 'submit': c['then'] != 'draft'})
        cid = str(r['id'])
        if c['then'] in ('approve', 'reject'):
            mgr = ctx.manager_of(e)
            kit.call(f'POST /expenses/claims/{cid}/review', expenses.review_claim, mgr.user, cid,
                     {'decision': 'approve'} if c['then'] == 'approve'
                     else {'decision': 'reject', 'reason': 'Team lunches are covered by the team budget, not personal claims.'})
            if c['then'] == 'approve':
                st = one('SELECT status FROM expense_claim WHERE id = %s', [cid])
                if st and st['status'] in ('submitted', 'pending'):
                    kit.call(f'POST /expenses/claims/{cid}/review', expenses.review_claim, hr.user, cid, {'decision': 'approve'})

    # A trip to Pokhara approved with an advance and settled, and a trip to Biratnagar waiting for approval.
    t1e = people[1 % len(people)]
    t1 = ctx.as_(t1e)
    trip = kit.call('POST /expenses/travel', expenses.request_travel, t1.user,
                    {'destination': 'Pokhara', 'destinationCategory': 'city', 'purpose': 'Install the edge GPU node at Gandaki College and train their IT team.',
                     'fromDate': add_days_iso(ctx.today, 1), 'toDate': add_days_iso(ctx.today, 3), 'mode': 'bus', 'estimatedCost': '12000.00',
                     'advanceRequested': '8000.00'})
    tid = str(trip['id'])
    mgr1 = ctx.manager_of(t1e)
    kit.call(f'POST /expenses/travel/{tid}/review', expenses.review_trip, mgr1.user, tid, {'decision': 'approve'})
    ts = one('SELECT status FROM travel_request WHERE id = %s', [tid])
    if not ts or ts['status'] != 'approved':
        kit.call(f'POST /expenses/travel/{tid}/review', expenses.review_trip, hr.user, tid, {'decision': 'approve', 'advanceApproved': '8000.00'})
    # The demo needs a finished trip: requests can only be raised for the future, so move this one back two weeks.
    execute("UPDATE travel_request SET from_date = %s, to_date = %s, created_at = now() - interval '16 days' WHERE id = %s",
            [add_days_iso(ctx.today, -12), add_days_iso(ctx.today, -10), tid])
    bus = receipt(t1, 'Bus tickets Kathmandu-Pokhara', '2400.00')
    hotel = receipt(t1, 'Hotel Lakeside, two nights', '6000.00')
    kit.call(f'POST /expenses/travel/{tid}/settlement', expenses.settle_trip, t1.user, tid,
             {'actualFrom': add_days_iso(ctx.today, -12), 'actualTo': add_days_iso(ctx.today, -10), 'note': 'Trip went as planned.',
              'bills': [{'kind': 'transport', 'billDate': add_days_iso(ctx.today, -12), 'description': 'Bus tickets both ways', 'amount': '2400.00', 'fileId': bus},
                        {'kind': 'lodging', 'billDate': add_days_iso(ctx.today, -10), 'description': 'Hotel, two nights', 'amount': '6000.00', 'fileId': hotel}]})

    t2e = people[5 % len(people)]
    kit.call('POST /expenses/travel', expenses.request_travel, ctx.as_(t2e).user,
             {'destination': 'Biratnagar', 'destinationCategory': 'city', 'purpose': 'Meet two hospitals interested in on-premise GPU servers.',
              'fromDate': add_days_iso(ctx.today, 8), 'toDate': add_days_iso(ctx.today, 10), 'mode': 'air', 'estimatedCost': '22000.00',
              'advanceRequested': '10000.00'})


def seed_tax_savings(ctx: DemoContext) -> None:
    """Two employees declared their insurance and CIT for this fiscal year; HR has not checked them yet."""
    kit = ctx.kit
    if rows('SELECT 1 FROM tax_declaration_request WHERE company_id = %s LIMIT 1', [ctx.company_id]):
        return
    fy = tax_savings.current_fiscal_year_start()
    for k, e in enumerate(ctx.employees[3:5]):
        me = ctx.as_(e)
        proof = kit.upload_pdf(me, 'tax_proof', f'insurance-receipt-{e.code}.pdf', 'Life insurance premium receipt',
                               [f'Policy holder: {e.name}', f"Premium paid: NPR {'25,000' if k else '40,000'}", 'Nepal Life Insurance'])
        kit.call('POST /payroll/tax-savings', tax_savings.submit_savings, me.user,
                 {'fiscalYearStart': fy, 'lifePremium': '25000' if k else '40000', 'healthPremium': '0' if k else '15000',
                  'citMonthly': '3000' if k else '0', 'note': 'Receipt attached.', 'proofFileIds': [proof]})
