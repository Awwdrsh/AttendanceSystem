"""
Expense claims and travel pages (apps/web/src/pages/expenses): My claims (/me/claims), My travel (/me/claims/travel),
the review pages for managers and HR (/expenses, /expenses/travel) and HR's policies (/expenses/policies), with their
HTMX fragments under /_/expenses/... The claim and trip drawers open in the shared drawer (#hj-drawer-body).
"""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.errors import ConflictError, ForbiddenError, HttpError, NotFoundError
from hajiri.files.store import stored_file_response
from hajiri.web.csv import csv_response
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.format import add_days, bs_month_label, fmt_date, fmt_date_short, fmt_money, today_npt
from hajiri.web.htmx import close_modal, refresh, toast, trigger

from . import services as ex
from .forms import (CategoryForm, ClaimForm, ClaimLinesForm, ClaimReviewForm, DecisionForm, NoteForm, PolicySettingsForm,
                    ReviewFilterForm, SettleForm, SpendFilterForm, TadaForm, TravelForm, TripReviewForm)

FEATURE = 'expenses'

CLAIM_STATUS = {'draft': ('Draft', 'idle'), 'pending': ('Waiting for approval', 'warn'), 'approved': ('Approved', 'accent'),
                'rejected': ('Rejected', 'bad'), 'cancelled': ('Cancelled', 'idle'), 'paid': ('Paid', 'ok')}
TRIP_STATUS = {'pending': ('Waiting for approval', 'warn'), 'approved': ('Approved', 'accent'), 'rejected': ('Rejected', 'bad'),
               'cancelled': ('Cancelled', 'idle'), 'settling': ('Settlement submitted', 'info'), 'settled': ('Settled', 'ok')}
SETTLEMENT_STATUS = {'pending': ('Waiting for approval', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Returned', 'bad'),
                     'cancelled': ('Cancelled', 'idle')}
CLAIM_FILTERS = [('pending', 'Waiting'), ('approved', 'Approved'), ('paid', 'Paid'), ('all', 'All')]
TRIP_FILTERS = [('pending', 'To approve'), ('approved', 'Approved'), ('settling', 'Settlements'), ('settled', 'Settled'), ('all', 'All')]


# ---------------------------------------------------------------- display helpers (shared.tsx)

def money(v) -> str:
    """NPR without paisa for whole amounts (shared.tsx money)."""
    if v is None or v == '':
        return fmt_money(None)
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return str(v)
    return fmt_money(d, decimals=0 if d == d.to_integral_value() else 2)


def span(f, t) -> str:
    return fmt_date(f) if str(f) == str(t) else f'{fmt_date_short(f)} to {fmt_date(t)}'


def payout_label(p: dict) -> str:
    """Where the money stands: "In the Ashwin 2083 pay run", "Paid outside payroll", "With the next salary"."""
    verb = 'Recovered' if p.get('code') == 'EXP_RECOVERY' else 'Paid'
    st = p.get('status')
    if st == 'cash':
        return f'{verb} outside payroll'
    if st == 'cancelled':
        return 'Cancelled'
    y, m = p.get('bsYear'), p.get('bsMonth')
    if st == 'paid':
        return f'{verb} in {bs_month_label(y, m or 1)} salary' if y else verb
    if y:
        return f'In the {bs_month_label(y, m or 1)} pay run'
    return 'To be recovered from the next salary' if p.get('code') == 'EXP_RECOVERY' else 'With the next salary'


def _n(v) -> Decimal:
    try:
        return Decimal(str(v)) if v not in (None, '') else Decimal(0)
    except InvalidOperation:
        return Decimal(0)


def _plural(n, one, many) -> str:
    return f'{n} {one if n == 1 else many}'


def _claim_display(c: dict) -> dict:
    label, tone = CLAIM_STATUS.get(c['status'], (c['status'], 'idle'))
    when = span(c['firstDate'], c['lastDate']) if c.get('firstDate') and c.get('lastDate') else ''
    if c.get('payoutStatus'):
        payment = payout_label({'status': c['payoutStatus'], 'bsYear': c.get('payBsYear'), 'bsMonth': c.get('payBsMonth'), 'code': 'EXP_REIMB'})
    else:
        payment = 'Paid outside payroll' if c.get('paymentMethod') == 'cash' else '—'
    return {**c, 'status_label': label, 'status_tone': tone, 'sub': f"{when} · {_plural(c['items'], 'item', 'items')}",
            'amount_text': money(c['totalApproved'] if c.get('totalApproved') is not None else c['totalClaimed']),
            'of_text': money(c['totalClaimed']) if c.get('totalApproved') and c['totalApproved'] != c['totalClaimed'] else '',
            'payment': payment}


def _trip_display(t: dict, today: str) -> dict:
    label, tone = TRIP_STATUS.get(t['status'], (t['status'], 'idle'))
    s = t.get('settlement') or None
    return {**t, 'status_label': label, 'status_tone': tone,
            'sub': f"{ex.DESTINATION_LABEL.get(t['destinationCategory'], '')} · {span(t['fromDate'], t['toDate'])}",
            'allowance': money(t['daAmount']),
            'advance': money(t['advanceApproved'] or t['advanceRequested']) if _n(t['advanceRequested']) > 0 else '—',
            'returned': bool(s and t['status'] != 'settled' and s.get('status') == 'rejected'),
            'to_settle': t['status'] == 'approved' and t['fromDate'] <= today}


def _issues_ctx(issues: list[dict]) -> dict:
    blocks = [i for i in issues if i['level'] == 'block']
    return {'issues': issues, 'blocked': bool(blocks), 'issues_title': 'Fix before submitting' if blocks else 'Over policy',
            'issues_tone': 'bad' if blocks else 'warn'}


def _err(request, e: HttpError, title: str) -> HttpResponse:
    """The failed action's toast with the title the TS page used (toast.err(e, title))."""
    from hajiri.web.middleware import error_message
    r = HttpResponse(error_message(e), status=e.status, content_type='text/plain; charset=utf-8')
    r.headers['HX-Reswap'] = 'none'
    return trigger(r, 'hj:toast', {'message': error_message(e), 'tone': 'err', 'title': title})


def _into_drawer(resp: HttpResponse) -> HttpResponse:
    resp.headers['HX-Retarget'] = '#hj-drawer-body'
    resp.headers['HX-Reswap'] = 'innerHTML'
    return resp


# ================================================================ claims

def _claims_page_ctx(request, own: bool) -> dict:
    user = request.hajiri_user
    if own:
        claims = [_claim_display(c) for c in ex.list_claims(user, {'scope': 'own'})['claims']]
        def total(f, key):
            return sum((_n(c[key]) for c in claims if f(c)), Decimal(0))
        waiting = [c for c in claims if c['status'] == 'pending']
        stats = [
            {'label': 'Waiting for approval', 'value': len(waiting),
             'hint': money(total(lambda c: c['status'] == 'pending', 'totalClaimed')) if waiting else None},
            {'label': 'Approved, to be paid', 'value': money(total(lambda c: c['status'] == 'approved', 'totalApproved'))},
            {'label': 'Paid so far', 'value': money(total(lambda c: c['status'] == 'paid', 'totalApproved'))},
        ]
        return {'claims': claims, 'stats': stats, 'own': True}
    status = ReviewFilterForm(request.GET).status_for(CLAIM_FILTERS)
    hr = has_role(user, 'hr')
    q = {'scope': 'all' if hr else 'team', **({} if status == 'all' else {'status': status})}
    claims = [_claim_display(c) for c in ex.list_claims(user, q)['claims']]
    ctx = {'claims': claims, 'own': False, 'status': status, 'filters': CLAIM_FILTERS, 'hr': hr}
    if hr:
        pay = ex.report_pending_payment(user)['rows']
        ctx['stats'] = [
            {'label': 'To pay in the next pay run', 'value': money(sum((_n(r['amount']) for r in pay if r['kind'] == 'Pay'), Decimal(0))),
             'hint': 'Approved claims and travel settlements'},
            {'label': 'To recover', 'value': money(sum((_n(r['amount']) for r in pay if r['kind'] == 'Recover'), Decimal(0))),
             'hint': 'Unspent travel advances'},
            {'label': 'Items', 'value': len(pay)},
        ]
    return ctx


@require_GET
@login_required
@require_feature(FEATURE)
def my_claims(request):
    ctx = _claims_page_ctx(request, True)
    ctx['open'] = request.GET.get('open') or ''
    return render(request, 'expenses/my_claims.html', ctx)


@require_GET
@require_role('manager', 'hr')
@require_feature(FEATURE)
def claims_review(request):
    ctx = _claims_page_ctx(request, False)
    ctx['open'] = request.GET.get('open') or ''
    return render(request, 'expenses/claims_review.html', ctx)


@require_GET
@login_required
@require_feature(FEATURE)
def claims_list(request):
    own = request.GET.get('own') == '1'
    if not own and not has_role(request.hajiri_user, 'manager', 'hr'):
        raise ForbiddenError('requires role manager or hr')
    return render(request, 'expenses/partials/claims_block.html', _claims_page_ctx(request, own))


# ---------------------------------------------------------------- claim form

def _receipt_hint(c: dict) -> str:
    """The text above a line's receipt upload (ClaimForm): needed above the threshold, always, or optional."""
    thr = c.get('receiptRequiredAbove')
    if thr is None or c.get('kind') != 'amount':
        return 'Receipt (optional)'
    return f'Receipt needed above {money(thr)}' if _n(thr) > 0 else 'Receipt needed'


def _line_ctx(policy: dict, n: int, item: dict | None = None) -> dict:
    item = item or {}
    cats = [c for c in policy['categories'] if c['active'] or c['id'] == item.get('categoryId')]
    cat = next((c for c in policy['categories'] if c['id'] == item.get('categoryId')), None)
    today = today_npt()
    return {'n': n, 'no': n + 1, 'item': item, 'cats': [{**c, 'receipt_hint': _receipt_hint(c)} for c in cats],
            'kind': cat['kind'] if cat else 'amount', 'receipt_hint': _receipt_hint(cat) if cat else 'Receipt (optional)',
            'rate': fmt_money(policy['settings']['mileageRatePerKm']), 'rate_num': policy['settings']['mileageRatePerKm'],
            'min_date': add_days(today, -int(policy['settings']['claimWindowDays'])), 'max_date': today}


def _claim_form_ctx(request, policy: dict, claim: dict | None) -> dict:
    if claim:
        items = [{'expenseDate': i['expenseDate'], 'categoryId': i['categoryId'], 'description': i['description'],
                  'amount': '' if i['categoryKind'] == 'mileage' else i['amount'], 'km': i['km'] or '',
                  'receiptFileId': i['receiptFileId'] or '', 'receiptName': i['receiptName'] or 'Receipt'} for i in claim['items']]
    else:
        items = [{'expenseDate': today_npt()}]
    return {'policy': policy, 'claim': claim and claim['claim'], 'lines': [_line_ctx(policy, n, it) for n, it in enumerate(items)],
            'user': request.hajiri_user}


def _save_claim(request, claim_id: str | None):
    user = request.hajiri_user
    form = ClaimForm(request.POST)
    submit = form.submitting
    body = form.claim(ex.get_policy(user)['categories'])
    try:
        r = ex.update_claim(user, claim_id, body) if claim_id else ex.create_claim(user, body)
    except HttpError as e:
        return _err(request, e, 'Not submitted' if submit else 'Not saved')
    resp = _into_drawer(render(request, 'expenses/partials/claim_detail.html', _claim_detail_ctx(request, r['id'])))
    refresh(resp, 'exp-claims')
    if submit:
        return toast(request, resp, 'Your manager has been asked to approve it.', title='Claim submitted')
    return toast(request, resp, 'Saved as a draft. Submit it when it is complete.', title='Draft saved')


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature(FEATURE)
def claim_new(request):
    if request.method == 'POST':
        return _save_claim(request, None)
    return render(request, 'expenses/partials/claim_form.html', _claim_form_ctx(request, ex.get_policy(request.hajiri_user), None))


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature(FEATURE)
def claim_edit(request, id):
    if request.method == 'POST':
        return _save_claim(request, str(id))
    d = ex.claim_detail(request.hajiri_user, str(id))
    if d['claim']['employee']['id'] != request.hajiri_user.employee_id or d['claim']['status'] != 'draft':
        raise ConflictError('only a draft claim can be edited')
    return render(request, 'expenses/partials/claim_form.html', _claim_form_ctx(request, ex.get_policy(request.hajiri_user), d))


@require_GET
@login_required
@require_feature(FEATURE)
def claim_line(request):
    try:
        n = max(0, min(49, int(request.GET.get('n') or 0)))
    except ValueError:
        n = 0
    return render(request, 'expenses/partials/claim_line.html', _line_ctx(ex.get_policy(request.hajiri_user), n, {'expenseDate': today_npt()}))


@require_POST
@login_required
@require_feature(FEATURE)
def claim_check(request):
    """Live total and policy checks while the claim is typed (ClaimForm's debounced check)."""
    user = request.hajiri_user
    body = ClaimLinesForm(request.POST).check_body(ex.get_policy(user)['categories'])
    try:
        r = ex.check_items(user, body)
    except HttpError:
        r = None   # incomplete lines: no figures yet, as the TS form showed '—'
    issues = r['issues'] if r else []
    # Line issues show under their line (ClaimForm lineIssues); the alert holds the claim-wide ones (limits).
    claim_issues = [i for i in issues if i.get('item') is None]
    ctx = {'check': r, 'total_text': money(r['total']) if r else '—', 'total': r['total'] if r else '',
           **_issues_ctx(claim_issues), 'blocked': any(i['level'] == 'block' for i in issues),
           'line_issues_json': json.dumps([{'item': i['item'], 'level': i['level'], 'message': i['message']}
                                           for i in issues if i.get('item') is not None])}
    resp = render(request, 'expenses/partials/claim_check.html', ctx)
    return trigger(resp, 'hj:chain-expense_claim', True, header='HX-Trigger-After-Swap')


# ---------------------------------------------------------------- claim drawer

def _claim_detail_ctx(request, id: str) -> dict:
    user = request.hajiri_user
    d = ex.claim_detail(user, id)
    c = d['claim']
    mine = c['employee']['id'] == user.employee_id
    label, tone = CLAIM_STATUS.get(c['status'], (c['status'], 'idle'))
    items = [{**i, 'amount_text': money(i['amount']), 'approved_text': money(i['approvedAmount']), 'date_text': fmt_date_short(i['expenseDate'])}
             for i in d['items']]
    payouts_text = ' · '.join(f"{money(p['amount'])}: {payout_label(p)}" for p in d['payouts'])
    return {'d': d, 'c': c, 'items': items, 'mine': mine, 'status_label': label, 'status_tone': tone,
            'payouts_text': payouts_text, 'payouts_pending': any(p['status'] == 'pending' for p in d['payouts']),
            'can_pay_outside': has_role(user, 'hr') and c['status'] == 'approved'
                               and any(p['status'] == 'pending' and not p['bsYear'] for p in d['payouts']),
            'claimed_total': c['totalClaimed'], **_issues_ctx(d['issues'])}


@require_GET
@login_required
@require_feature(FEATURE)
def claim_detail(request, id):
    return render(request, 'expenses/partials/claim_detail.html', _claim_detail_ctx(request, str(id)))


def _claim_action(request, id, fn, message: str, title: str | None = None):
    fn()
    resp = _into_drawer(render(request, 'expenses/partials/claim_detail.html', _claim_detail_ctx(request, str(id))))
    refresh(resp, 'exp-claims')
    return toast(request, resp, message, title=title)


@require_POST
@login_required
@require_feature(FEATURE)
def claim_submit(request, id):
    return _claim_action(request, id, lambda: ex.submit_claim(request.hajiri_user, str(id)), 'Claim submitted')


@require_POST
@login_required
@require_feature(FEATURE)
def claim_cancel(request, id):
    return _claim_action(request, id, lambda: ex.cancel_claim(request.hajiri_user, str(id)), 'Claim cancelled')


@require_POST
@login_required
@require_feature(FEATURE)
def claim_review(request, id):
    body = ClaimReviewForm(request.POST).review()
    return _claim_action(request, id, lambda: ex.review_claim(request.hajiri_user, str(id), body),
                         'Claim approved' if body['decision'] == 'approve' else 'Claim rejected')


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def claim_pay_outside(request, id):
    body = NoteForm(request.POST).note_body()
    return _claim_action(request, id, lambda: ex.pay_claim_outside(request.hajiri_user, str(id), body),
                         'Marked as paid; payroll will not pay it')


@require_GET
@login_required
@require_feature(FEATURE)
def claim_receipt(request, id, item_id):
    f = ex.claim_receipt(request.hajiri_user, str(id), str(item_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')


# ================================================================ travel

def _trips_page_ctx(request, own: bool) -> dict:
    user = request.hajiri_user
    today = today_npt()
    if own:
        trips = [_trip_display(t, today) for t in ex.list_trips(user, {'scope': 'own'})['trips']]
        to_settle = [t for t in trips if t['to_settle']]
        upcoming = [t for t in trips if t['status'] in ('approved', 'pending') and t['fromDate'] > today]
        out = sum((_n(t['advanceApproved']) for t in trips if t['advanceStatus'] != 'none' and t['status'] in ('approved', 'settling')),
                  Decimal(0))
        stats = [{'label': 'Upcoming trips', 'value': len(upcoming)},
                 {'label': 'To settle', 'value': len(to_settle),
                  **({'tone': 'warn', 'hint': 'Open the trip and add your bills'} if to_settle else {})},
                 {'label': 'Advance not yet settled', 'value': money(out)}]
        return {'trips': trips, 'stats': stats, 'own': True}
    status = ReviewFilterForm(request.GET).status_for(TRIP_FILTERS)
    hr = has_role(user, 'hr')
    q = {'scope': 'all' if hr else 'team', **({} if status == 'all' else {'status': status})}
    trips = [_trip_display(t, today) for t in ex.list_trips(user, q)['trips']]
    ctx = {'trips': trips, 'own': False, 'status': status, 'filters': TRIP_FILTERS, 'hr': hr}
    if hr:
        adv = ex.report_advances(user)['rows']
        ctx['stats'] = [{'label': 'Advances not yet settled', 'value': money(sum((_n(r['advance']) for r in adv), Decimal(0)))},
                        {'label': 'Trips with an open advance', 'value': len(adv)}]
    return ctx


@require_GET
@login_required
@require_feature(FEATURE)
def my_travel(request):
    ctx = _trips_page_ctx(request, True)
    ctx['open'] = request.GET.get('open') or ''
    return render(request, 'expenses/my_travel.html', ctx)


@require_GET
@require_role('manager', 'hr')
@require_feature(FEATURE)
def travel_review(request):
    ctx = _trips_page_ctx(request, False)
    ctx['open'] = request.GET.get('open') or ''
    return render(request, 'expenses/travel_review.html', ctx)


@require_GET
@login_required
@require_feature(FEATURE)
def trips_list(request):
    own = request.GET.get('own') == '1'
    if not own and not has_role(request.hajiri_user, 'manager', 'hr'):
        raise ForbiddenError('requires role manager or hr')
    return render(request, 'expenses/partials/trips_block.html', _trips_page_ctx(request, own))


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature(FEATURE)
def travel_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        try:
            r = ex.request_travel(user, TravelForm(request.POST).travel())
        except HttpError as e:
            return _err(request, e, 'Not requested')
        resp = _into_drawer(render(request, 'expenses/partials/trip_detail.html', _trip_detail_ctx(request, r['id'])))
        refresh(resp, 'exp-trips')
        return toast(request, resp, 'Your manager has been asked to approve the trip.', title='Travel requested')
    today = today_npt()
    return render(request, 'expenses/partials/travel_form.html', {
        'policy': ex.get_policy(user), 'from_date': add_days(today, 1), 'to_date': add_days(today, 2), 'today': today,
        'modes': list(ex.MODE_LABEL.items()), 'user': user})


@require_GET
@login_required
@require_feature(FEATURE)
def travel_quote(request):
    """The allowance figures while the trip is typed (TravelForm's debounced quote)."""
    form = TravelForm(request.GET)
    ctx: dict = {'quote': None, 'error': None, 'chain_amount': _n(form.travel().get('estimatedCost'))}
    body = form.quote()
    if body:
        try:
            q = ex.travel_quote(request.hajiri_user, body)['quote']
            per_day = (f"USD {q['daRate']} × {q['fxRate']}" if q['currency'] == 'USD' else money(q['daRate'])) + ' per day' + \
                (f", grade {q['grade']}" if q['grade'] != '*' else '')
            nights = f" ({_plural(q['nights'], 'night', 'nights')})" if q['nights'] else ''
            ctx.update(quote=q, per_day=per_day, days_text=f"{q['days']}{nights}",
                       da_text=money(q['daAmount']), lodging_text=money(q['lodgingLimit']), lodging_tip=f"{money(q['lodgingRateNpr'])} per night",
                       both_text=money(_n(q['daAmount']) + _n(q['lodgingLimit'])))
            ctx['chain_amount'] += _n(q['daAmount']) + _n(q['lodgingLimit'])
        except HttpError as e:
            ctx['error'] = e.message
    resp = render(request, 'expenses/partials/travel_quote.html', ctx)
    return trigger(resp, 'hj:chain-travel_request', True, header='HX-Trigger-After-Swap')


# ---------------------------------------------------------------- trip drawer

def _trip_detail_ctx(request, id: str) -> dict:
    user = request.hajiri_user
    d = ex.trip_detail(user, id)
    t = d['trip']
    today = today_npt()
    label, tone = TRIP_STATUS.get(t['status'], (t['status'], 'idle'))
    mine = t['employee']['id'] == user.employee_id
    adv_payouts = [p for p in d['payouts'] if p['sourceType'] == 'travel_advance']
    if _n(t['advanceRequested']) > 0:
        adv_text = money(t['advanceApproved'] or t['advanceRequested'])
        if t['advanceApproved'] and t['advanceApproved'] != t['advanceRequested']:
            adv_text += f" (asked {money(t['advanceRequested'])})"
    else:
        adv_text = 'None requested'
    adv_where = ' · '.join(payout_label(p) for p in adv_payouts) or \
        ('Paid once approved' if t['status'] == 'pending' and _n(t['advanceRequested']) > 0 else '')
    settlements = []
    for s in d['settlements']:
        sl, st = SETTLEMENT_STATUS.get(s['status'], (s['status'], 'idle'))
        net = _n(s['net'])
        settlements.append({**s, 'status_label': sl, 'status_tone': st, 'when': span(s['actualFrom'], s['actualTo']),
                            'allowance_label': f"Allowance ({_plural(s['days'], 'day', 'days')})",
                            'net_label': 'To pay the employee' if net > 0 else 'To recover from the employee' if net < 0 else 'Nothing to pay or recover',
                            'net_text': money(abs(net)) if net != 0 else '',
                            'lodging_tip': f"Claimed {money(s['lodgingClaimed'])}" if _n(s['lodgingClaimed']) > _n(s['lodgingAllowed']) else None,
                            'bills': [{**b, 'date_text': fmt_date_short(b['billDate']), 'kind_label': ex.BILL_KIND_LABEL.get(b['kind'], b['kind']),
                                       'amount_text': money(b['amount'])} for b in s['bills']],
                            'can_decide': d['canDecideSettlement'] and s['status'] == 'pending'})
    settle_payouts = ' · '.join(f"{money(p['amount'])}: {payout_label(p)}" for p in d['payouts'] if p['sourceType'] == 'travel_settlement')
    ctx = {'d': d, 't': t, 'mine': mine, 'status_label': label, 'status_tone': tone, 'when': span(t['fromDate'], t['toDate']),
           'mode_label': ex.MODE_LABEL.get(t['mode'], t['mode']), 'allowance_label': f"Allowance ({_plural(t['days'], 'day', 'days')})",
           'allowance_tip': f"USD {t['daRate']} per day at {t['fxRate']}" if t['fxRate'] else f"{money(t['daRate'])} per day",
           'adv_text': adv_text, 'adv_where': adv_where, 'settlements': settlements, 'settle_payouts': settle_payouts,
           'advance_asked': _n(t['advanceRequested']) > 0, 'advance_requested_text': money(t['advanceRequested']),
           'can_pay_outside': has_role(user, 'hr') and t['advanceStatus'] == 'pending'
                              and not any(p['status'] == 'pending' and p['bsYear'] for p in adv_payouts),
           'can_cancel': (mine or has_role(user, 'hr')) and t['status'] in ('pending', 'approved') and t['advanceStatus'] != 'paid',
           'money': money}
    if d['canSettle']:
        last = t['toDate'] if t['toDate'] < today else today
        ctx['settle'] = {'from': t['fromDate'], 'to': last, 'today': today}
    return ctx


@require_GET
@login_required
@require_feature(FEATURE)
def trip_detail(request, id):
    return render(request, 'expenses/partials/trip_detail.html', _trip_detail_ctx(request, str(id)))


def _trip_action(request, id, fn, message: str, title: str | None = None, err_title: str | None = None):
    try:
        fn()
    except HttpError as e:
        if err_title:
            return _err(request, e, err_title)
        raise
    resp = _into_drawer(render(request, 'expenses/partials/trip_detail.html', _trip_detail_ctx(request, str(id))))
    refresh(resp, 'exp-trips')
    return toast(request, resp, message, title=title)


@require_POST
@login_required
@require_feature(FEATURE)
def trip_cancel(request, id):
    return _trip_action(request, id, lambda: ex.cancel_trip(request.hajiri_user, str(id)), 'Trip cancelled')


@require_POST
@login_required
@require_feature(FEATURE)
def trip_review(request, id):
    body = TripReviewForm(request.POST).review()
    return _trip_action(request, id, lambda: ex.review_trip(request.hajiri_user, str(id), body),
                        'Trip approved' if body['decision'] == 'approve' else 'Trip rejected')


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def trip_advance_outside(request, id):
    body = NoteForm(request.POST).note_body()
    return _trip_action(request, id, lambda: ex.pay_advance_outside(request.hajiri_user, str(id), body), 'Advance marked as paid')


@require_POST
@login_required
@require_feature(FEATURE)
def trip_settle(request, id):
    body = SettleForm(request.POST).settlement()
    return _trip_action(request, id, lambda: ex.settle_trip(request.hajiri_user, str(id), body), 'Sent for approval.',
                        title='Settlement submitted', err_title='Not submitted')


@require_POST
@login_required
@require_feature(FEATURE)
def settle_preview(request, id):
    """SettlementForm's running figures: allowance for the days, lodging up to the limit, the advance and the balance."""
    t = ex.own_trip(request.hajiri_user, str(id))
    form = SettleForm(request.POST)
    d = form.values()
    f_, to = d['actual_from'], d['actual_to']
    bills = form.preview_bills()
    # No valid dates yet: zero days (the TS form showed the allowance for 0 days).
    fig = ex.settlement_figures(t, f_, to, bills) if f_ and to and to >= f_ else ex.settlement_figures(t, '2000-01-02', '2000-01-01', bills)
    return render(request, 'expenses/partials/settle_preview.html', {
        'f': fig, 'allowance_label': f"Allowance ({_plural(fig['days'], 'day', 'days')})", 'da_text': money(fig['da']),
        'lodging_text': money(fig['lodgingAllowed']), 'advance_text': money(fig['advance']),
        'lodging_tip': f"Claimed {money(fig['lodgingClaimed'])}; capped at the nightly limit" if fig['lodgingClaimed'] > fig['lodgingAllowed'] else None,
        'net_label': 'You receive' if fig['net'] >= 0 else 'You return', 'net_text': money(abs(fig['net']))})


@require_GET
@login_required
@require_feature(FEATURE)
def bill_row(request):
    return render(request, 'expenses/partials/bill_row.html', {'kinds': list(ex.BILL_KIND_LABEL.items()), 'date': request.GET.get('date') or '',
                                                               'min': request.GET.get('min') or '', 'max': request.GET.get('max') or ''})


@require_POST
@login_required
@require_feature(FEATURE)
def settlement_review(request, id):
    body = DecisionForm(request.POST).decision_body()
    trip_id = ex.settlement_trip_id(request.hajiri_user, str(id))
    return _trip_action(request, trip_id, lambda: ex.review_settlement(request.hajiri_user, str(id), body),
                        'Settlement approved' if body['decision'] == 'approve' else 'Settlement returned')


@require_GET
@login_required
@require_feature(FEATURE)
def bill_file(request, id, bill_id):
    f = ex.bill_file(request.hajiri_user, str(id), str(bill_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')


# ================================================================ policies (HR)

def _categories_ctx(policy: dict) -> dict:
    cats = []
    for c in policy['categories']:
        ls = [x for x in policy['limits'] if x['categoryId'] == c['id']]
        if ls:
            limits = ' · '.join(f"{'All' if x['grade'] == '*' else x['grade']}: " + ', '.join(
                v for v in (f"{money(x['perClaim'])}/claim" if x['perClaim'] else '', f"{money(x['perMonth'])}/month" if x['perMonth'] else '') if v)
                for x in ls)
        else:
            limits = 'No limit'
        rra = c['receiptRequiredAbove']
        cats.append({**c, 'kind_text': f"km × {money(policy['settings']['mileageRatePerKm'])}" if c['kind'] == 'mileage' else 'Amount',
                     'receipt_text': 'Not needed' if rra is None else f'Above {money(rra)}' if _n(rra) > 0 else 'Always', 'limits_text': limits})
    return {'categories': cats}


def _tada_ctx(policy: dict) -> dict:
    grades = list(dict.fromkeys(['*', *[r['grade'] for r in policy['tadaRates']]]))
    rates = {f"{r['grade']}|{r['destination']}": r for r in policy['tadaRates']}
    rows_ = [{'grade': g, 'label': 'Every grade' if g == '*' else g,
              'cells': [{'dest': d, 'da': (rates.get(f'{g}|{d}') or {}).get('daRate', ''), 'lodging': (rates.get(f'{g}|{d}') or {}).get('lodgingRate', '')}
                        for d in ex.DESTINATIONS]} for g in grades]
    return {'tada_rows': rows_, 'dest_heads': [ex.DESTINATION_LABEL[d] + (' (USD)' if d == 'international' else '') for d in ex.DESTINATIONS],
            'addable': [g for g in policy['grades'] if g not in grades]}


def _spend_ctx(request) -> dict:
    today = today_npt()
    q = SpendFilterForm(request.GET).query(today, add_days(today, -90))
    f, t, group_by = q['from'], q['to'], q['groupBy']
    r = ex.report_spend(request.hajiri_user, q)
    total = sum((_n(x['amount']) for x in r['rows']), Decimal(0))
    rows_ = [{**x, 'amount_text': money(x['amount']),
              'share': f"{int((_n(x['amount']) / total * 100).quantize(Decimal(1), rounding='ROUND_HALF_UP'))}%" if total else '—'}
             for x in r['rows']]
    return {'spend_rows': rows_, 'spend_total': money(total) if rows_ else '', 'from': f, 'to': t, 'today': today, 'group_by': group_by,
            'group_header': group_by[:1].upper() + group_by[1:],
            'groups': [('category', 'Category'), ('department', 'Department'), ('month', 'Month'), ('employee', 'Employee')]}


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def policies(request):
    policy = ex.get_policy(request.hajiri_user)
    return render(request, 'expenses/policies.html', {'policy': policy, 's': policy['settings'], **_categories_ctx(policy),
                                                      **_tada_ctx(policy), **_spend_ctx(request)})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def policy_settings(request):
    ex.save_settings(request.hajiri_user, PolicySettingsForm(request.POST).settings())
    resp = HttpResponse(status=204)
    refresh(resp, 'exp-categories')
    return toast(request, resp, 'Expense rules saved')


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def categories_partial(request):
    return render(request, 'expenses/partials/categories.html', _categories_ctx(ex.get_policy(request.hajiri_user)))


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def category_form(request, id=None):
    user = request.hajiri_user
    policy = ex.get_policy(user)
    cat = next((c for c in policy['categories'] if c['id'] == str(id)), None) if id else None
    if id and not cat:
        raise NotFoundError('category not found')
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        body = form.category(new=not cat)
        with transaction.atomic():
            if cat:
                ex.update_category(user, cat['id'], body)
                cid = cat['id']
            else:
                cid = ex.create_category(user, body)['id']
            ex.set_limits(user, cid, form.limits())
        resp = HttpResponse(status=204)
        close_modal(resp)
        refresh(resp, 'exp-categories')
        return toast(request, resp, 'Category saved')
    limits = [x for x in policy['limits'] if cat and x['categoryId'] == cat['id']]
    grades = list(dict.fromkeys(['*', *policy['grades'], *[x['grade'] for x in limits]]))
    return render(request, 'expenses/partials/category_form.html', {'cat': cat, 'limits': limits, 'grades': grades})


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def limit_row(request):
    policy = ex.get_policy(request.hajiri_user)
    taken = request.GET.getlist('taken')
    grades = list(dict.fromkeys(['*', *policy['grades'], *taken]))
    pick = next((g for g in grades if g not in taken), '*')
    return render(request, 'expenses/partials/limit_row.html', {'l': {'grade': pick, 'perClaim': '', 'perMonth': ''}, 'grades': grades})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def tada_rates(request):
    ex.set_tada_rates(request.hajiri_user, TadaForm(request.POST).rates())
    return toast(request, HttpResponse(status=204), 'Travel rates saved')


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def tada_grade_row(request):
    g = (request.GET.get('grade') or '').strip()[:40]
    if not g:
        return HttpResponse('')
    return render(request, 'expenses/partials/tada_row.html',
                  {'r': {'grade': g, 'label': g, 'cells': [{'dest': d, 'da': '', 'lodging': ''} for d in ex.DESTINATIONS]}})


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def report_spend(request):
    if request.GET.get('format') == 'csv':
        today = today_npt()
        r = ex.report_spend(request.hajiri_user, SpendFilterForm(request.GET).query(today, add_days(today, -90), strict=True))
        return csv_response(r['filename'], r['columns'], r['rows'])
    return render(request, 'expenses/partials/spend.html', _spend_ctx(request))


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def report_advances_csv(request):
    r = ex.report_advances(request.hajiri_user)
    return csv_response(r['filename'], r['columns'], r['rows'])


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def report_pending_csv(request):
    r = ex.report_pending_payment(request.hajiri_user)
    return csv_response(r['filename'], r['columns'], r['rows'])
