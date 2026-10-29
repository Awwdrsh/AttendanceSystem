"""
Expense claims and travel (port of apps/api/src/modules/expenses: service.ts and routes.ts).

Claims: an employee files lines (amounts, or km x the company mileage rate) with receipts; limits by grade, receipt
thresholds and the claim window are checked before submitting; the claim goes through the manager -> HR chain and an
approved claim becomes an expense_payout row that the next pay run pays (hajiri.expenses.payroll).

Travel: a trip is requested with its daily allowance (TADA by grade and destination) and an optional advance, which
payroll pays once the trip is approved; afterwards the traveller settles it with bills, and the difference between
the entitlement and the advance is paid or recovered through payroll.

The route-level functions take the signed-in AuthUser and a body in the TS camelCase shape (validated with the
serializers in serializers.py, the zod rules of the TS routes) and return what the TS route answered, so the pages,
the JSON API and the tests share one contract. Every query is scoped to the user's active company; an id of another
company is not found.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Max, Min, OuterRef, Q, StringAgg, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce, Now

from engines.bs_calendar import ad_to_bs, bs_month_bounds
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.bsdate import bs_label, bs_range
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import get_or_404
from hajiri.core.settings_store import get_setting, put_setting
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (Employee, EmployeeAssignment, ExpenseCategory, ExpenseClaim, ExpenseItem, ExpenseLimit,
                              ExpensePayout, TadaRate, TravelBill, TravelRequest, TravelSettlement)
from hajiri.files.store import attach_file, read_file
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.web.format import group_digits
from hajiri.workflow.service import (DecisionContext, act, can_act, cancel_workflow, instance_in, register_handler,
                                     register_summariser, start_workflow)

from . import filters
from . import serializers as s
from .serializers import BILL_KINDS, CLAIM_STATUSES, DESTINATIONS, GROUP_BY, MODES, SCOPES, TRIP_STATUSES  # noqa: F401

Claim = ExpenseClaim.Status
Trip = TravelRequest.Status
Advance = TravelRequest.AdvanceStatus
Settlement = TravelSettlement.Status
Payout = ExpensePayout.Status
Source = ExpensePayout.SourceType
Code = ExpensePayout.ComponentCode

# ---------------------------------------------------------------- constants

DESTINATION_LABEL = {'valley': 'Kathmandu valley', 'city': 'Other city', 'rural': 'Rural area', 'international': 'International'}
MODE_LABEL = {'bus': 'Bus', 'air': 'Air', 'office_vehicle': 'Office vehicle', 'own_vehicle': 'Own vehicle', 'other': 'Other'}
BILL_KIND_LABEL = {'transport': 'Transport', 'lodging': 'Lodging', 'other': 'Other'}
# Travel bills above this need the bill attached.
BILL_RECEIPT_ABOVE = 500
# Claims, travel and HR payment states that count towards a monthly limit.
COUNTED = (Claim.PENDING, Claim.APPROVED, Claim.PAID)


# ---------------------------------------------------------------- numbers

def D(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, float):
        return Decimal(repr(v))
    return Decimal(str(v))


def _q2(d: Decimal) -> Decimal:
    return D(d).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def money(d) -> str:
    """Two decimals, half up: '1100.00'."""
    return f'{_q2(D(d)):.2f}'


def rs(d) -> str:
    """'Rs 1,500' / 'Rs 1,500.5' (en-IN grouping, up to 2 decimals), as the TS rs()."""
    s_ = money(d)
    neg = s_.startswith('-')
    i, _, f = s_.lstrip('-').partition('.')
    f = f.rstrip('0')
    return f"Rs {'-' if neg else ''}{group_digits(i)}{'.' + f if f else ''}"


def _days_between(a, b) -> int:
    return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days


def npt_today() -> str:
    return _npt_today().isoformat()


def _s(v) -> str | None:
    """A column as the text PostgreSQL's ::text gave (numeric '900.00', date 'YYYY-MM-DD', uuid)."""
    return None if v is None else str(v)


# ---------------------------------------------------------------- policy

def expense_policy(company_id: str) -> dict:
    v = get_setting(company_id, 'expense.policy', {}) or {}
    if not isinstance(v, dict):
        v = {}
    raw = v.get('mileageRatePerKm')
    rate = str(raw) if isinstance(raw, str) or (isinstance(raw, (int, float)) and not isinstance(raw, bool)) else '15'
    try:
        d = D(rate)
        mileage = money(d) if d.is_finite() and d >= 0 else '15.00'
    except (InvalidOperation, ValueError):
        mileage = '15.00'
    days = v.get('claimWindowDays')
    return {
        'mileageRatePerKm': mileage,
        'limitMode': 'warn' if v.get('limitMode') == 'warn' else 'block',
        'claimWindowDays': days if isinstance(days, (int, float)) and not isinstance(days, bool) else 90,
    }


def employee_of(company_id: str, employee_id) -> dict:
    r = (Employee.objects.for_company(company_id).filter(pk=employee_id)
         .values('id', 'name_en', 'emp_code', 'grade', 'status').first())
    if not r:
        raise NotFoundError('employee not found')
    return {**r, 'id': str(r['id'])}


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def _self_employee(user: AuthUser) -> str:
    if not user.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    return user.employee_id


# ---------------------------------------------------------------- claim checks

def _categories(company_id: str) -> dict[str, dict]:
    cats = ExpenseCategory.objects.for_company(company_id).values('id', 'code', 'name', 'kind', 'receipt_required_above',
                                                                  'active')
    return {str(c['id']): {**c, 'id': str(c['id']), 'receipt_required_above': _s(c['receipt_required_above'])} for c in cats}


def normalise_items(company_id: str, items: list[dict]) -> list[dict]:
    """Server-side amounts: mileage lines are km x the company rate; amount lines must carry an amount."""
    policy = expense_policy(company_id)
    by_id = _categories(company_id)
    out = []
    for i, it in enumerate(items):
        category = by_id.get(str(it.get('categoryId')))
        if not category:
            raise BadRequestError(f'Line {i + 1}: choose a valid category.')
        if not category['active']:
            raise BadRequestError(f"Line {i + 1}: {category['name']} is no longer available.")
        if category['kind'] == ExpenseCategory.Kind.MILEAGE:
            km = it.get('km')
            if not km or km <= 0:
                raise BadRequestError(f'Line {i + 1}: enter the distance in km.')
            out.append({**it, 'category': category, 'amount': money(D(km) * D(policy['mileageRatePerKm']))})
            continue
        amt = it.get('amount')
        if not amt or not D(amt) > 0:
            raise BadRequestError(f'Line {i + 1}: enter an amount above zero.')
        out.append({**it, 'km': None, 'category': category, 'amount': money(amt)})
    return out


def _month_total_elsewhere(company_id: str, employee_id, category_id, start, end, exclude_claim_id) -> Decimal:
    """The employee's other open or paid claims for this category in these dates (approved amounts where decided)."""
    qs = ExpenseItem.objects.for_company(company_id).filter(
        claim__employee_id=employee_id, category_id=category_id, claim__status__in=COUNTED, expense_date__range=(start, end))
    if exclude_claim_id:
        qs = qs.exclude(claim_id=exclude_claim_id)
    return qs.aggregate(t=Coalesce(Sum(Coalesce('approved_amount', 'amount')), Value(Decimal(0))))['t']


def check_claim(company_id: str, employee_id: str, items: list[dict], exclude_claim_id: str | None) -> list[dict]:
    """
    Limits by grade (grade row, else the '*' row), receipts above the category threshold, the claim window and the
    monthly total across the employee's other open or paid claims in the same BS month.
    """
    policy = expense_policy(company_id)
    emp = employee_of(company_id, employee_id)
    issues: list[dict] = []
    today = npt_today()
    for i, it in enumerate(items):
        d = str(it['expenseDate'])
        if d > today:
            issues.append({'level': 'block', 'code': 'future_date', 'message': f'Line {i + 1}: the date is in the future.', 'item': i})
        elif _days_between(d, today) > policy['claimWindowDays']:
            issues.append({'level': 'block', 'code': 'too_old',
                           'message': f"Line {i + 1}: expenses older than {policy['claimWindowDays']} days cannot be claimed.", 'item': i})
        thr = it['category']['receipt_required_above']
        if thr is not None and D(it['amount']) > D(thr) and not it.get('receiptFileId'):
            what = f" for {it['category']['name']} above {rs(thr)}" if D(thr) > 0 else f" for {it['category']['name']}"
            issues.append({'level': 'block', 'code': 'receipt_required', 'message': f'Line {i + 1}: attach a receipt{what}.', 'item': i})
    cat_ids = list(dict.fromkeys(it['category']['id'] for it in items))
    limits = [{**x, 'category_id': str(x['category_id'])} for x in ExpenseLimit.objects.for_company(company_id).filter(
        category_id__in=cat_ids, grade__in=(emp['grade'] or '*', '*')).values('category_id', 'grade', 'per_claim', 'per_month')]

    def limit_for(cat_id):
        return (next((x for x in limits if x['category_id'] == cat_id and x['grade'] == emp['grade']), None)
                or next((x for x in limits if x['category_id'] == cat_id and x['grade'] == '*'), None))

    level = policy['limitMode']
    for cat_id in cat_ids:
        lim = limit_for(cat_id)
        if not lim:
            continue
        mine = [x for x in items if x['category']['id'] == cat_id]
        name = mine[0]['category']['name']
        total = sum((D(x['amount']) for x in mine), Decimal(0))
        if lim['per_claim'] is not None and total > lim['per_claim']:
            issues.append({'level': level, 'code': 'per_claim_limit',
                           'message': f"{name}: {rs(total)} is over the limit of {rs(lim['per_claim'])} per claim."})
        if lim['per_month'] is not None:
            months: dict[tuple[int, int], Decimal] = {}
            for x in mine:
                b = ad_to_bs(str(x['expenseDate']))
                months[(b.year, b.month)] = months.get((b.year, b.month), Decimal(0)) + D(x['amount'])
            for (y, m), amt in months.items():
                bounds = bs_month_bounds(y, m)
                month_total = amt + _month_total_elsewhere(company_id, employee_id, cat_id, bounds['startAd'],
                                                           bounds['endAd'], exclude_claim_id)
                if month_total > lim['per_month']:
                    label = bs_label(bounds['startAd']).split(' ', 1)[1]
                    issues.append({'level': level, 'code': 'per_month_limit',
                                   'message': f"{name}: {rs(month_total)} in {label} is over the monthly limit of {rs(lim['per_month'])}."})
    return issues


# ---------------------------------------------------------------- travel allowance

def quote_tada(company_id: str, employee_id: str, dest: str, from_: str, to: str, fx_rate: str | None) -> dict:
    """Daily allowance for every calendar day of the trip and lodging for every night, at the grade's rate (else the default)."""
    if to < from_:
        raise BadRequestError('The return date must be on or after the start date.')
    emp = employee_of(company_id, employee_id)
    rate = (TadaRate.objects.for_company(company_id).filter(destination=dest, grade__in=(emp['grade'] or '*', '*'))
            .order_by(Case(When(grade='*', then=1), default=0, output_field=IntegerField()))
            .values('grade', 'da_rate', 'lodging_rate').first()) or {'grade': '*', 'da_rate': Decimal(0), 'lodging_rate': Decimal(0)}
    international = dest == TravelRequest.DestinationCategory.INTERNATIONAL
    if international and (not fx_rate or not D(fx_rate) > 0):
        raise BadRequestError('Enter the US dollar exchange rate for an international trip.')
    fx = D(fx_rate) if international else Decimal(1)
    days = _days_between(from_, to) + 1
    if days > 90:
        raise BadRequestError('A single trip can be at most 90 days.')
    nights = days - 1
    da_npr = D(rate['da_rate']) * fx
    lodging_npr = D(rate['lodging_rate']) * fx
    return {
        'days': days, 'nights': nights, 'currency': 'USD' if international else 'NPR', 'daRate': money(rate['da_rate']),
        'lodgingRate': money(rate['lodging_rate']),
        'fxRate': f"{D(fx_rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP):.4f}" if international else None,
        'daRateNpr': money(da_npr), 'lodgingRateNpr': money(lodging_npr), 'daAmount': money(da_npr * days),
        'lodgingLimit': money(lodging_npr * nights), 'grade': rate['grade'],
    }


# ---------------------------------------------------------------- payouts

def create_payout(*, company_id: str, employee_id: str, source_type: str, source_id: str, code: str, amount: Decimal, note: str) -> None:
    """One payout per source and component; a second call for the same source does nothing (the partial unique index
    on (source_type, source_id, component_code) for root payouts)."""
    if not amount > 0:
        return
    ExpensePayout.objects.bulk_create([ExpensePayout(
        company_id=company_id, employee_id=employee_id, source_type=source_type, source_id=source_id, component_code=code,
        amount=money(amount), note=note[:200])], ignore_conflicts=True)


def _tell(company_id, employee_id, template: str, title: str, body: str, payload: dict) -> None:
    user_id = user_id_for_employee(str(employee_id))
    if not user_id:
        return
    notify(company_id=str(company_id), user_id=user_id, employee_id=str(employee_id), template=template, title=title,
           body=body, payload=payload, channels=('push',))


def _why(reason: str | None) -> str:
    return f': {reason}' if reason else ''


# ---------------------------------------------------------------- workflow handlers
# Called by the workflow engine with the object id of an instance it already loaded for the deciding user's company.

def apply_claim_decision(claim_id: str, state: str, ctx: DecisionContext) -> None:
    claim_id = str(claim_id)
    c = ExpenseClaim.objects.select_for_update().filter(pk=claim_id).first()
    if not c:
        raise NotFoundError('claim not found')
    if c.status != Claim.PENDING:
        raise ConflictError(f'claim already {c.status}')
    claims = ExpenseClaim.objects.filter(pk=claim_id)
    if state == 'rejected':
        claims.update(status=Claim.REJECTED, decided_at=Now(), decision_note=ctx.reason, updated_at=Now())
        _tell(c.company_id, c.employee_id, 'expense_claim.rejected', 'Expense claim rejected',
              f'"{c.title}" was rejected{_why(ctx.reason)}.', {'claimId': claim_id})
        return
    lines = ExpenseItem.objects.filter(claim_id=claim_id)
    lines.update(approved_amount=Coalesce('approved_amount', 'amount'))
    total = lines.aggregate(t=Coalesce(Sum('approved_amount'), Value(Decimal(0))))['t']
    claims.update(status=Claim.APPROVED if total > 0 else Claim.REJECTED, total_approved=money(total), decided_at=Now(),
                  decision_note=ctx.reason, payment_method=ExpenseClaim.PaymentMethod.PAYROLL, updated_at=Now())
    if not total > 0:
        _tell(c.company_id, c.employee_id, 'expense_claim.rejected', 'Expense claim not paid',
              f'"{c.title}" was approved for nothing{_why(ctx.reason)}.', {'claimId': claim_id})
        return
    create_payout(company_id=c.company_id, employee_id=c.employee_id, source_type=Source.EXPENSE_CLAIM, source_id=claim_id,
                  code=Code.EXP_REIMB, amount=total, note=f'Claim: {c.title}')
    _tell(c.company_id, c.employee_id, 'expense_claim.approved', 'Expense claim approved',
          f'"{c.title}" was approved for {rs(total)}. It will be paid with your next salary.', {'claimId': claim_id})


def apply_travel_decision(travel_id: str, state: str, ctx: DecisionContext) -> None:
    travel_id = str(travel_id)
    t = TravelRequest.objects.select_for_update().filter(pk=travel_id).first()
    if not t:
        raise NotFoundError('travel request not found')
    if t.status != Trip.PENDING:
        raise ConflictError(f'travel request already {t.status}')
    when = bs_range(t.from_date, t.to_date)
    trips = TravelRequest.objects.filter(pk=travel_id)
    if state == 'rejected':
        trips.update(status=Trip.REJECTED, decided_at=Now(), decision_note=ctx.reason)
        _tell(t.company_id, t.employee_id, 'travel_request.rejected', 'Travel request rejected',
              f'Your trip to {t.destination} ({when}) was rejected{_why(ctx.reason)}.', {'travelId': travel_id})
        return
    advance = D(t.advance_approved if t.advance_approved is not None else t.advance_requested)
    trips.update(status=Trip.APPROVED, decided_at=Now(), decision_note=ctx.reason, advance_approved=money(advance),
                 advance_status=Advance.PENDING if advance > 0 else Advance.NONE,
                 advance_method=TravelRequest.AdvanceMethod.PAYROLL if advance > 0 else None)
    create_payout(company_id=t.company_id, employee_id=t.employee_id, source_type=Source.TRAVEL_ADVANCE, source_id=travel_id,
                  code=Code.TRAVEL_ADV, amount=advance, note=f'Travel advance: {t.destination}')
    with_adv = f' with an advance of {rs(advance)}' if advance > 0 else ''
    _tell(t.company_id, t.employee_id, 'travel_request.approved', 'Travel request approved',
          f'Your trip to {t.destination} ({when}) was approved{with_adv}.', {'travelId': travel_id})


def apply_settlement_decision(settlement_id: str, state: str, ctx: DecisionContext) -> None:
    settlement_id = str(settlement_id)
    s_ = (TravelSettlement.objects.select_for_update(of=('self',)).select_related('travel_request')
          .filter(pk=settlement_id).first())
    if not s_:
        raise NotFoundError('settlement not found')
    if s_.status != Settlement.PENDING:
        raise ConflictError(f'settlement already {s_.status}')
    destination = s_.travel_request.destination
    trip_id = s_.travel_request_id
    rows_ = TravelSettlement.objects.filter(pk=settlement_id)
    if state == 'rejected':
        rows_.update(status=Settlement.REJECTED, decided_at=Now(), decision_note=ctx.reason)
        TravelRequest.objects.filter(pk=trip_id, status=Trip.SETTLING).update(status=Trip.APPROVED)
        _tell(s_.company_id, s_.employee_id, 'travel_settlement.rejected', 'Travel settlement returned',
              f'Your settlement for {destination} was rejected{_why(ctx.reason)}. You can correct and submit it again.',
              {'settlementId': settlement_id})
        return
    # An advance that payroll has not picked up yet is never paid: it is cancelled and the whole entitlement is settled instead.
    advance = s_.advance
    cancelled = ExpensePayout.objects.for_company(s_.company_id).filter(
        source_type=Source.TRAVEL_ADVANCE, source_id=trip_id, status=Payout.PENDING, payroll_run__isnull=True,
    ).update(status=Payout.CANCELLED)
    if cancelled:
        advance = Decimal(0)
        TravelRequest.objects.filter(pk=trip_id).update(advance_status=Advance.NONE)
    net = s_.entitlement - advance
    rows_.update(status=Settlement.APPROVED, decided_at=Now(), decision_note=ctx.reason, advance=money(advance), net=money(net))
    TravelRequest.objects.filter(pk=trip_id).update(status=Trip.SETTLED)
    if net > 0:
        create_payout(company_id=s_.company_id, employee_id=s_.employee_id, source_type=Source.TRAVEL_SETTLEMENT,
                      source_id=settlement_id, code=Code.EXP_REIMB, amount=net, note=f'Travel settlement: {destination}')
    if net < 0:
        create_payout(company_id=s_.company_id, employee_id=s_.employee_id, source_type=Source.TRAVEL_SETTLEMENT,
                      source_id=settlement_id, code=Code.EXP_RECOVERY, amount=-net, note=f'Unspent travel advance: {destination}')
    if net > 0:
        body = f'{rs(net)} for your trip to {destination} will be paid with your next salary.'
    elif net < 0:
        body = f'{rs(-net)} of unspent advance for {destination} will be recovered from your next salary.'
    else:
        body = f'Your trip to {destination} is settled with nothing more to pay or recover.'
    _tell(s_.company_id, s_.employee_id, 'travel_settlement.approved', 'Travel settlement approved', body,
          {'settlementId': settlement_id})


def _summary_claim(inst: dict) -> dict:
    c = (ExpenseClaim.objects.for_company(inst['company_id']).filter(pk=inst['object_id'])
         .annotate(n_items=Count('expense_items'),
                   n_receipts=Count('expense_items', filter=Q(expense_items__receipt_file__isnull=False)),
                   category_names=StringAgg('expense_items__category__name', Value(', '), distinct=True,
                                            order_by='expense_items__category__name'))
         .values('title', 'total_claimed', 'n_items', 'n_receipts', 'category_names').first())
    return {'title': c['title'], 'amount': _s(c['total_claimed']), 'items': c['n_items'], 'receipts': c['n_receipts'],
            'categories': c['category_names']} if c else {}


def _summary_travel(inst: dict) -> dict:
    t = TravelRequest.objects.for_company(inst['company_id']).filter(pk=inst['object_id']).first()
    return {'destination': t.destination, 'destinationCategory': DESTINATION_LABEL.get(t.destination_category),
            'purpose': t.purpose, 'from': _s(t.from_date), 'to': _s(t.to_date), 'days': t.days,
            'dailyAllowance': _s(t.da_amount), 'estimatedCost': _s(t.estimated_cost), 'advance': _s(t.advance_requested),
            'mode': t.mode} if t else {}


def _summary_settlement(inst: dict) -> dict:
    s_ = (TravelSettlement.objects.for_company(inst['company_id']).filter(pk=inst['object_id'])
          .annotate(destination=F('travel_request__destination'), n_bills=Count('travel_bills')).first())
    return {'destination': s_.destination, 'from': _s(s_.actual_from), 'to': _s(s_.actual_to),
            'entitlement': _s(s_.entitlement), 'advance': _s(s_.advance), 'net': _s(s_.net), 'bills': s_.n_bills} if s_ else {}


def register() -> None:
    """Decision handlers and inbox summaries (called from ExpensesConfig.ready and at import)."""
    register_handler('expense_claim', apply_claim_decision)
    register_handler('travel_request', apply_travel_decision)
    register_handler('travel_settlement', apply_settlement_decision)
    register_summariser('expense_claim', _summary_claim)
    register_summariser('travel_request', _summary_travel)
    register_summariser('travel_settlement', _summary_settlement)


register()


# ================================================================ route-level functions

def _assert_can_see(u: AuthUser, employee_id) -> None:
    """The employee, their manager and HR may read a claim or trip; everyone else gets a 404."""
    if has_role(u, 'hr') or u.employee_id == str(employee_id):
        return
    if has_role(u, 'manager') and u.employee_id and is_manager_of(u.employee_id, str(employee_id)):
        return
    raise NotFoundError('not found')


def _can_act_on(user: AuthUser, instance_id) -> bool:
    inst = instance_in(user.company_id, instance_id) if instance_id else None
    return can_act(user, inst) if inst else False


def _pay_outside(company_id: str, source_type: str, source_id: str) -> None:
    """Settle a source's pending payout outside payroll; refused while a pay run being prepared holds it."""
    waiting = ExpensePayout.objects.for_company(company_id).filter(source_type=source_type, source_id=source_id,
                                                                   status=Payout.PENDING)
    held = list(waiting.select_for_update().values_list('payroll_run_id', flat=True))
    if not held:
        raise ConflictError('nothing is waiting to be paid')
    if any(held):
        raise ConflictError('This payment is already in a pay run being prepared. Pay it in that run instead.')
    waiting.update(status=Payout.CASH, paid_amount=F('amount'), paid_at=Now())
    from .payroll import mark_sources_paid
    mark_sources_paid([{'source_type': source_type, 'source_id': source_id}])


def _claim(company_id: str, id: str, *, lock: bool = False) -> ExpenseClaim:
    qs = ExpenseClaim.objects.select_for_update() if lock else ExpenseClaim.objects.all()
    return get_or_404(qs, company_id, 'claim not found', pk=id)


def _trip(company_id: str, id: str, *, lock: bool = False) -> TravelRequest:
    qs = TravelRequest.objects.select_for_update() if lock else TravelRequest.objects.all()
    return get_or_404(qs, company_id, 'travel request not found', pk=id)


def _payout_rows(qs) -> list[dict]:
    """Payouts with the pay period of the run holding them (none while waiting for a run)."""
    out = []
    for p in qs.order_by('created_at', 'id').values('id', 'source_type', 'component_code', 'amount', 'status', 'paid_amount',
                                                    'paid_at', bs_year=F('payroll_run__period__bs_year'),
                                                    bs_month=F('payroll_run__period__bs_month')):
        out.append({'id': str(p['id']), 'sourceType': p['source_type'], 'code': p['component_code'], 'amount': _s(p['amount']),
                    'status': p['status'], 'paidAmount': _s(p['paid_amount']), 'paidAt': p['paid_at'],
                    'bsYear': p['bs_year'], 'bsMonth': p['bs_month']})
    return out


# ---------------------------------------------------------------- policy routes

def get_policy(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    settings = expense_policy(company_id)
    cats = [{'id': str(c.id), 'code': c.code, 'name': c.name, 'kind': c.kind, 'receiptRequiredAbove': _s(c.receipt_required_above),
             'active': c.active, 'sortOrder': c.sort_order}
            for c in ExpenseCategory.objects.for_company(company_id).order_by('sort_order', 'name')]
    limits = [{'id': str(x.id), 'categoryId': str(x.category_id), 'grade': x.grade, 'perClaim': _s(x.per_claim),
               'perMonth': _s(x.per_month)} for x in ExpenseLimit.objects.for_company(company_id).order_by('grade')]
    rates = [{'grade': r.grade, 'destination': r.destination, 'daRate': _s(r.da_rate), 'lodgingRate': _s(r.lodging_rate)}
             for r in TadaRate.objects.for_company(company_id).order_by(
                 Case(When(grade='*', then=0), default=1, output_field=IntegerField()), 'grade', 'destination')]
    grade = employee_of(company_id, user.employee_id)['grade'] if user.employee_id else None
    grades = list(Employee.objects.for_company(company_id).filter(grade__isnull=False).order_by('grade')
                  .values_list('grade', flat=True).distinct()) if has_role(user, 'hr') else []
    return {'settings': settings, 'categories': cats, 'limits': limits, 'tadaRates': rates,
            'destinations': [{'value': d, 'label': DESTINATION_LABEL[d]} for d in DESTINATIONS], 'myGrade': grade, 'grades': grades}


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = validate(s.SettingsIn, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        before = expense_policy(company_id)
        put_setting(company_id, 'expense.policy', b, updated_by=user.id)
        audit('update', 'company_setting', actor_id=user.id, row_id=None, before={'key': 'expense.policy', **before}, after=b)
    return {'settings': expense_policy(company_id)}


def create_category(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = validate(s.CategoryIn, body)
    with transaction.atomic():
        c = ExpenseCategory.objects.create(company_id=company_id_of(user), code=b['code'], name=b['name'], kind=b['kind'],
                                           receipt_required_above=b.get('receiptRequiredAbove'), active=b.get('active', True),
                                           sort_order=b.get('sortOrder', 100))
        audit('create', 'expense_category', actor_id=user.id, row_id=str(c.id), after=b)
    return {'id': str(c.id)}


_CATEGORY_COLUMNS = {'name': 'name', 'kind': 'kind', 'receiptRequiredAbove': 'receipt_required_above', 'active': 'active',
                     'sortOrder': 'sort_order'}


def update_category(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = validate(s.CategoryPatchIn, body)
    with transaction.atomic():
        found = ExpenseCategory.objects.for_company(company_id_of(user)).filter(pk=id)
        changes = {_CATEGORY_COLUMNS[k]: v for k, v in b.items() if k in _CATEGORY_COLUMNS}
        if not (found.update(**changes) if changes else found.exists()):
            raise NotFoundError('category not found')
        audit('update', 'expense_category', actor_id=user.id, row_id=id, after=b)
    return {'ok': True}


def set_limits(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = validate(s.LimitsIn, body)
    company_id = company_id_of(user)
    grades = [x['grade'] for x in b['limits']]
    if len(set(grades)) != len(grades):
        raise BadRequestError('each grade can appear only once')
    with transaction.atomic():
        get_or_404(ExpenseCategory, company_id, 'category not found', pk=id)
        ExpenseLimit.objects.for_company(company_id).filter(category_id=id).delete()
        ExpenseLimit.objects.bulk_create([
            ExpenseLimit(company_id=company_id, category_id=id, grade=x['grade'], per_claim=x.get('perClaim'),
                         per_month=x.get('perMonth'))
            for x in b['limits'] if x.get('perClaim') or x.get('perMonth')])
        audit('update', 'expense_limit', actor_id=user.id, row_id=id, after=b['limits'])
    return {'ok': True}


def set_tada_rates(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = validate(s.TadaRatesIn, body)
    keys = [f"{x['grade']}|{x['destination']}" for x in b['rates']]
    if len(set(keys)) != len(keys):
        raise BadRequestError('each grade and destination can appear only once')
    company_id = company_id_of(user)
    with transaction.atomic():
        TadaRate.objects.for_company(company_id).delete()
        TadaRate.objects.bulk_create([
            TadaRate(company_id=company_id, grade=x['grade'], destination=x['destination'], da_rate=x['daRate'],
                     lodging_rate=x['lodgingRate']) for x in b['rates']])
        audit('update', 'tada_rate', actor_id=user.id, row_id=None, after=b['rates'])
    return {'ok': True}


# ---------------------------------------------------------------- claims

def check_items(user: AuthUser, body: dict) -> dict:
    b = plain(validate(s.CheckIn, body))
    company_id = company_id_of(user)
    items = normalise_items(company_id, b['items'])
    issues = check_claim(company_id, _self_employee(user), items, b.get('claimId'))
    return {'amounts': [i['amount'] for i in items], 'total': money(sum((D(i['amount']) for i in items), Decimal(0))), 'issues': issues}


def _write_items(company_id: str, user_id: str, employee_id: str, claim_id: str, raw: list[dict], submitting: bool) -> tuple[list, Decimal]:
    """Replace the claim's lines, attaching each receipt to the claim. Submitting refuses blocking issues."""
    items = normalise_items(company_id, raw)
    issues = check_claim(company_id, employee_id, items, claim_id)
    blocks = [i for i in issues if i['level'] == 'block']
    if submitting and blocks:
        raise BadRequestError(' '.join(x['message'] for x in blocks), {'code': 'claim_blocked', 'issues': issues})
    ExpenseItem.objects.filter(claim_id=claim_id).delete()
    lines = []
    for i, it in enumerate(items):
        if it.get('receiptFileId'):
            attach_file(company_id=company_id, file_id=it['receiptFileId'], user_id=user_id, purpose='expense_receipt',
                        attached_to=f'expense_claim:{claim_id}')
        lines.append(ExpenseItem(claim_id=claim_id, expense_date=it['expenseDate'], category_id=it['categoryId'],
                                 description=it['description'], amount=it['amount'],
                                 km=D(it['km']) if it.get('km') is not None else None,
                                 receipt_file_id=it.get('receiptFileId'), sort_order=i))
    ExpenseItem.objects.bulk_create(lines)
    total = sum((D(it['amount']) for it in items), Decimal(0))
    ExpenseClaim.objects.filter(pk=claim_id).update(total_claimed=money(total), updated_at=Now())
    return issues, total


def _submit(company_id: str, claim_id: str, employee_id: str, title: str, total: str) -> None:
    emp = employee_of(company_id, employee_id)
    inst = start_workflow(company_id=company_id, object_type='expense_claim', object_id=claim_id, employee_id=employee_id,
                          amount=float(total), title='Expense claim to approve', body=f"{emp['name_en']}: {title}, {rs(total)}")
    ExpenseClaim.objects.filter(pk=claim_id).update(status=Claim.PENDING, submitted_at=Now(), workflow_instance_id=inst['id'],
                                                    updated_at=Now())


def create_claim(user: AuthUser, body: dict) -> dict:
    b = plain(validate(s.ClaimIn, body))
    company_id = company_id_of(user)
    employee_id = _self_employee(user)
    emp = employee_of(company_id, employee_id)
    if emp['status'] != Employee.Status.ACTIVE:
        raise ForbiddenError('only active employees can claim expenses')
    submit = bool(b.get('submit'))
    with transaction.atomic():
        id = str(ExpenseClaim.objects.create(company_id=company_id, employee_id=employee_id, title=b['title'],
                                             created_by_id=user.id).id)
        issues, total = _write_items(company_id, user.id, employee_id, id, b['items'], submit)
        if submit:
            _submit(company_id, id, employee_id, b['title'], money(total))
        audit('create', 'expense_claim', actor_id=user.id, row_id=id, after={'title': b['title'], 'total': money(total), 'submitted': submit})
    return {'id': id, 'status': Claim.PENDING if submit else Claim.DRAFT, 'total': money(total), 'issues': issues}


def _own_draft(user: AuthUser, company_id: str, id: str) -> ExpenseClaim:
    """The caller's own claim, locked; anyone else's is not found."""
    c = ExpenseClaim.objects.for_company(company_id).select_for_update().filter(pk=id).first()
    if not c or str(c.employee_id) != user.employee_id:
        raise NotFoundError('claim not found')
    return c


def update_claim(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = plain(validate(s.ClaimIn, body))
    company_id = company_id_of(user)
    submit = bool(b.get('submit'))
    with transaction.atomic():
        c = _own_draft(user, company_id, id)
        if c.status != Claim.DRAFT:
            raise ConflictError('only a draft claim can be edited')
        ExpenseClaim.objects.filter(pk=id).update(title=b['title'])
        issues, total = _write_items(company_id, user.id, str(c.employee_id), id, b['items'], submit)
        if submit:
            _submit(company_id, id, str(c.employee_id), b['title'], money(total))
    return {'id': id, 'status': Claim.PENDING if submit else Claim.DRAFT, 'total': money(total), 'issues': issues}


def _stored_items(claim_id: str) -> list[dict]:
    return [{'expenseDate': _s(i.expense_date), 'categoryId': str(i.category_id), 'description': i.description,
             'amount': _s(i.amount), 'km': float(i.km) if i.km else None, 'receiptFileId': _s(i.receipt_file_id)}
            for i in ExpenseItem.objects.filter(claim_id=claim_id).order_by('sort_order')]


def submit_claim(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    with transaction.atomic():
        c = _own_draft(user, company_id, id)
        if c.status != Claim.DRAFT:
            raise ConflictError(f'claim is {c.status}')
        raw = _stored_items(id)
        if not raw:
            raise BadRequestError('add at least one expense')
        _, total = _write_items(company_id, user.id, str(c.employee_id), id, raw, True)
        _submit(company_id, id, str(c.employee_id), c.title, money(total))
    return {'id': id, 'status': Claim.PENDING}


def cancel_claim(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    with transaction.atomic():
        c = _claim(company_id_of(user), id)
        if str(c.employee_id) != user.employee_id:
            raise NotFoundError('claim not found')
        if c.status not in (Claim.DRAFT, Claim.PENDING):
            raise ConflictError(f'a {c.status} claim cannot be cancelled')
        ExpenseClaim.objects.filter(pk=id).update(status=Claim.CANCELLED, updated_at=Now())
        cancel_workflow('expense_claim', id)
    return {'id': id, 'status': Claim.CANCELLED}


def _query(query: dict | None) -> dict:
    """Query-string filters: empty values are left out (an empty select means 'any')."""
    return {k: v for k, v in (query or {}).items() if v not in (None, '')}


def claims_queryset(user: AuthUser, query: dict | None = None):
    """The claims the user may list (scope own / team / all, drafts only their own), newest first."""
    q = plain(validate(s.ClaimsQueryIn, _query(query)))
    return filters.apply(filters.ClaimFilter, user, ExpenseClaim.objects.for_company(company_id_of(user)), q) \
        .order_by('-created_at', '-id')


def _claim_list_rows(qs, limit: int | None = 500) -> list[dict]:
    payout = ExpensePayout.objects.filter(source_type=Source.EXPENSE_CLAIM, source_id=OuterRef('pk'), parent_payout__isnull=True)
    rows_ = qs.annotate(
        n_items=Count('expense_items'), first_date=Min('expense_items__expense_date'), last_date=Max('expense_items__expense_date'),
        payout_status=Subquery(payout.values('status')[:1]),
        pay_bs_year=Subquery(payout.values('payroll_run__period__bs_year')[:1]),
        pay_bs_month=Subquery(payout.values('payroll_run__period__bs_month')[:1]),
    ).values('id', 'title', 'status', 'total_claimed', 'total_approved', 'submitted_at', 'decided_at', 'payment_method', 'paid_at',
             'created_at', 'employee_id', 'n_items', 'first_date', 'last_date', 'payout_status', 'pay_bs_year', 'pay_bs_month',
             emp_code=F('employee__emp_code'), employee_name=F('employee__name_en'))
    if limit is not None:
        rows_ = rows_[:limit]
    return [{'id': str(c['id']), 'title': c['title'], 'status': c['status'], 'totalClaimed': _s(c['total_claimed']),
             'totalApproved': _s(c['total_approved']), 'submittedAt': c['submitted_at'], 'decidedAt': c['decided_at'],
             'paymentMethod': c['payment_method'], 'paidAt': c['paid_at'], 'createdAt': c['created_at'],
             'employeeId': str(c['employee_id']), 'empCode': c['emp_code'], 'employeeName': c['employee_name'],
             'items': c['n_items'], 'firstDate': _s(c['first_date']), 'lastDate': _s(c['last_date']),
             'payoutStatus': c['payout_status'], 'payBsYear': c['pay_bs_year'], 'payBsMonth': c['pay_bs_month']}
            for c in rows_]


def list_claims(user: AuthUser, query: dict | None = None) -> dict:
    return {'claims': _claim_list_rows(claims_queryset(user, query))}


def _emp_card(emp: dict) -> dict:
    return {'id': emp['id'], 'empCode': emp['emp_code'], 'name': emp['name_en'], 'grade': emp['grade']}


def _claim_items(claim_id: str) -> list[dict]:
    lines = (ExpenseItem.objects.filter(claim_id=claim_id).order_by('sort_order')
             .values('id', 'expense_date', 'category_id', 'description', 'amount', 'km', 'receipt_file_id', 'approved_amount',
                     'adjust_reason', category_name=F('category__name'), category_kind=F('category__kind'),
                     receipt_name=Case(When(receipt_file__deleted_at__isnull=True, then=F('receipt_file__filename'))),
                     receipt_mime=Case(When(receipt_file__deleted_at__isnull=True, then=F('receipt_file__mime')))))
    return [{'id': str(i['id']), 'expenseDate': _s(i['expense_date']), 'categoryId': str(i['category_id']),
             'categoryName': i['category_name'], 'categoryKind': i['category_kind'], 'description': i['description'],
             'amount': _s(i['amount']), 'km': _s(i['km']), 'receiptFileId': _s(i['receipt_file_id']),
             'receiptName': i['receipt_name'], 'receiptMime': i['receipt_mime'], 'approvedAmount': _s(i['approved_amount']),
             'adjustReason': i['adjust_reason']} for i in lines]


def claim_detail(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    c = _claim(company_id, id)
    _assert_can_see(user, c.employee_id)
    if c.status == Claim.DRAFT and str(c.employee_id) != user.employee_id:
        raise NotFoundError('claim not found')
    emp = employee_of(company_id, c.employee_id)
    items = _claim_items(id)
    payouts = [{k: v for k, v in p.items() if k != 'sourceType'} for p in _payout_rows(
        ExpensePayout.objects.for_company(company_id).filter(source_type=Source.EXPENSE_CLAIM, source_id=id))]
    # Approvers see the same limit and receipt checks the employee saw when submitting.
    issues: list[dict] = []
    if c.status in (Claim.DRAFT, Claim.PENDING) and items:
        try:
            with transaction.atomic():
                norm = normalise_items(company_id, [{'expenseDate': i['expenseDate'], 'categoryId': i['categoryId'],
                                                     'description': i['description'], 'amount': i['amount'],
                                                     'km': float(i['km']) if i['km'] else None, 'receiptFileId': i['receiptFileId']}
                                                    for i in items])
                issues = check_claim(company_id, str(c.employee_id), norm, id)
        except Exception:  # noqa: BLE001 - the TS route swallowed any error here
            issues = []
    can_decide = _can_act_on(user, c.workflow_instance_id) if c.status == Claim.PENDING and c.workflow_instance_id else False
    return {
        'claim': {'id': str(c.id), 'title': c.title, 'status': c.status, 'totalClaimed': _s(c.total_claimed),
                  'totalApproved': _s(c.total_approved), 'paymentMethod': c.payment_method, 'decisionNote': c.decision_note,
                  'submittedAt': c.submitted_at, 'decidedAt': c.decided_at, 'paidAt': c.paid_at, 'createdAt': c.created_at,
                  'employee': _emp_card(emp)},
        'items': items, 'payouts': payouts, 'issues': issues, 'canDecide': can_decide,
    }


def claim_receipt(user: AuthUser, id: str, item_id: str) -> dict:
    """The receipt file of one line; an approver's view is audited."""
    id, item_id = parse_uuid(str(id)), parse_uuid(str(item_id))
    company_id = company_id_of(user)
    c = _claim(company_id, id)
    _assert_can_see(user, c.employee_id)
    file_id = ExpenseItem.objects.filter(pk=item_id, claim_id=id).values_list('receipt_file_id', flat=True).first()
    if not file_id:
        raise NotFoundError('no receipt for this line')
    if str(c.employee_id) != user.employee_id:
        audit('view_receipt', 'expense_item', actor_id=user.id, row_id=item_id)
    return read_file(company_id, str(file_id))


def review_claim(user: AuthUser, id: str, body: dict) -> dict:
    """Approve (optionally reducing line amounts, with a reason) or reject, as the current approver."""
    id = parse_uuid(str(id))
    b = plain(validate(s.ClaimReviewIn, body))
    company_id = company_id_of(user)
    with transaction.atomic():
        c = _claim(company_id, id, lock=True)
        if c.status != Claim.PENDING or not c.workflow_instance_id:
            raise ConflictError(f'claim is {c.status}')
        if b['decision'] == 'approve' and b.get('items'):
            lines = {str(x.id): x for x in ExpenseItem.objects.filter(claim_id=id)}
            changed = []
            for it in b['items']:
                line = lines.get(it['id'])
                if not line:
                    raise BadRequestError('a line does not belong to this claim')
                if D(it['approvedAmount']) > line.amount:
                    raise BadRequestError(f'"{line.description}": the approved amount cannot be more than claimed.')
                reduced = D(it['approvedAmount']) < line.amount
                if reduced and not it.get('reason'):
                    raise BadRequestError(f'"{line.description}": give a reason for approving less.')
                line.approved_amount = money(it['approvedAmount'])
                line.adjust_reason = it.get('reason') if reduced else None
                changed.append(line)
            ExpenseItem.objects.bulk_update(changed, ['approved_amount', 'adjust_reason'])
            total = ExpenseItem.objects.filter(claim_id=id).aggregate(
                t=Coalesce(Sum(Coalesce('approved_amount', 'amount')), Value(Decimal(0))))['t']
            if not total > 0:
                raise BadRequestError('Nothing would be paid; reject the claim instead.')
        state = act(instance_id=str(c.workflow_instance_id), user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


def pay_claim_outside(user: AuthUser, id: str, body: dict) -> dict:
    """HR paid an approved claim in cash or by transfer outside payroll; payroll will not pay it again."""
    _require_hr(user)
    id = parse_uuid(str(id))
    b = validate(s.NoteIn, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        c = _claim(company_id, id, lock=True)
        if c.status != Claim.APPROVED:
            raise ConflictError(f'claim is {c.status}')
        ExpenseClaim.objects.filter(pk=id).update(payment_method=ExpenseClaim.PaymentMethod.CASH)
        _pay_outside(company_id, Source.EXPENSE_CLAIM, id)
        # concat_ws(' · ', decision_note, note): the note is appended to any decision note.
        note = f"Paid outside payroll: {b['note']}"
        ExpenseClaim.objects.filter(pk=id).update(
            decision_note=f'{c.decision_note} · {note}' if c.decision_note is not None else note, updated_at=Now())
        audit('pay_outside_payroll', 'expense_claim', actor_id=user.id, row_id=id, after={'note': b['note']})
    return {'id': id, 'status': Claim.PAID}


# ---------------------------------------------------------------- travel

def travel_quote(user: AuthUser, body: dict) -> dict:
    b = validate(s.QuoteIn, body)
    return {'quote': quote_tada(company_id_of(user), _self_employee(user), b['destinationCategory'], b['fromDate'], b['toDate'],
                                b.get('fxRate'))}


def request_travel(user: AuthUser, body: dict) -> dict:
    b = validate(s.TravelIn, body)
    company_id = company_id_of(user)
    employee_id = _self_employee(user)
    emp = employee_of(company_id, employee_id)
    if emp['status'] != Employee.Status.ACTIVE:
        raise ForbiddenError('only active employees can request travel')
    if b['toDate'] < npt_today():
        raise BadRequestError('This trip has already ended. Ask HR to record it.')
    q = quote_tada(company_id, employee_id, b['destinationCategory'], b['fromDate'], b['toDate'], b.get('fxRate'))
    if TravelRequest.objects.for_company(company_id).filter(
            employee_id=employee_id, status__in=(Trip.PENDING, Trip.APPROVED, Trip.SETTLING),
            from_date__lte=b['toDate'], to_date__gte=b['fromDate']).exists():
        raise ConflictError('You already have a trip on these dates.')
    advance = D(b.get('advanceRequested') or '0')
    ceiling = D(q['daAmount']) + D(q['lodgingLimit']) + D(b.get('estimatedCost') or '0')
    if advance > ceiling:
        raise BadRequestError(f'The advance cannot be more than the expected cost of the trip ({rs(ceiling)}).')
    with transaction.atomic():
        t = TravelRequest.objects.create(
            company_id=company_id, employee_id=employee_id, destination=b['destination'],
            destination_category=b['destinationCategory'], purpose=b['purpose'], from_date=b['fromDate'], to_date=b['toDate'],
            mode=b['mode'], estimated_cost=b.get('estimatedCost') or '0', advance_requested=money(advance), fx_rate=q['fxRate'],
            days=q['days'], nights=q['nights'], da_rate=q['daRate'], lodging_rate=q['lodgingRate'], da_amount=q['daAmount'],
            lodging_limit=q['lodgingLimit'], created_by_id=user.id)
        id = str(t.id)
        adv = f', advance {rs(advance)}' if advance > 0 else ''
        inst = start_workflow(company_id=company_id, object_type='travel_request', object_id=id, employee_id=employee_id,
                              amount=float(ceiling), title='Travel request to approve',
                              body=f"{emp['name_en']}: {b['destination']}, {bs_range(b['fromDate'], b['toDate'])}{adv}")
        TravelRequest.objects.filter(pk=id).update(workflow_instance_id=inst['id'])
        audit('create', 'travel_request', actor_id=user.id, row_id=id, after={**b, 'quote': q})
    return {'id': id, 'status': Trip.PENDING, 'quote': q}


def trips_queryset(user: AuthUser, query: dict | None = None):
    q = validate(s.TripsQueryIn, _query(query))
    return filters.apply(filters.TripFilter, user, TravelRequest.objects.for_company(company_id_of(user)), q) \
        .order_by('-from_date', '-id')


def _trip_list_rows(qs, limit: int | None = 500) -> list[dict]:
    latest = TravelSettlement.objects.filter(travel_request=OuterRef('pk')).order_by('-created_at', '-id')
    qs = qs.annotate(s_id=Subquery(latest.values('id')[:1]), s_status=Subquery(latest.values('status')[:1]),
                     s_net=Subquery(latest.values('net')[:1]), emp_code=F('employee__emp_code'),
                     employee_name=F('employee__name_en'))
    out = []
    for t in (qs[:limit] if limit is not None else qs):
        out.append({'id': str(t.id), 'destination': t.destination, 'destinationCategory': t.destination_category,
                    'purpose': t.purpose, 'fromDate': _s(t.from_date), 'toDate': _s(t.to_date), 'mode': t.mode, 'days': t.days,
                    'daAmount': _s(t.da_amount), 'lodgingLimit': _s(t.lodging_limit), 'estimatedCost': _s(t.estimated_cost),
                    'advanceRequested': _s(t.advance_requested), 'advanceApproved': _s(t.advance_approved),
                    'advanceStatus': t.advance_status, 'advanceMethod': t.advance_method, 'status': t.status,
                    'createdAt': t.created_at, 'employeeId': str(t.employee_id), 'empCode': t.emp_code,
                    'employeeName': t.employee_name,
                    'settlement': {'id': str(t.s_id), 'status': t.s_status, 'net': _s(t.s_net)} if t.s_id else None})
    return out


def list_trips(user: AuthUser, query: dict | None = None) -> dict:
    return {'trips': _trip_list_rows(trips_queryset(user, query))}


def _settlement_rows(trip_id: str) -> list[dict]:
    settlements = list(TravelSettlement.objects.filter(travel_request_id=trip_id).order_by('-created_at', '-id'))
    bills: dict[str, list] = {}
    for b in (TravelBill.objects.filter(settlement__travel_request_id=trip_id).order_by('bill_date', 'id')
              .values('id', 'settlement_id', 'kind', 'bill_date', 'description', 'amount', 'file_id', file_name=F('file__filename'))):
        bills.setdefault(str(b['settlement_id']), []).append({
            'id': str(b['id']), 'kind': b['kind'], 'billDate': _s(b['bill_date']), 'description': b['description'],
            'amount': _s(b['amount']), 'fileId': _s(b['file_id']), 'fileName': b['file_name']})
    return [{'id': str(x.id), 'status': x.status, 'actualFrom': _s(x.actual_from), 'actualTo': _s(x.actual_to), 'days': x.days,
             'nights': x.nights, 'daAmount': _s(x.da_amount), 'lodgingClaimed': _s(x.lodging_claimed),
             'lodgingAllowed': _s(x.lodging_allowed), 'otherBills': _s(x.other_bills), 'entitlement': _s(x.entitlement),
             'advance': _s(x.advance), 'net': _s(x.net), 'note': x.note, 'decisionNote': x.decision_note,
             'createdAt': x.created_at, 'workflowInstanceId': _s(x.workflow_instance_id), 'bills': bills.get(str(x.id), [])}
            for x in settlements]


def trip_detail(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    t = _trip(company_id, id)
    _assert_can_see(user, t.employee_id)
    emp = employee_of(company_id, t.employee_id)
    settlements = _settlement_rows(id)
    payouts = _payout_rows(ExpensePayout.objects.for_company(company_id).filter(
        Q(source_type=Source.TRAVEL_ADVANCE, source_id=id)
        | Q(source_type=Source.TRAVEL_SETTLEMENT,
            source_id__in=TravelSettlement.objects.filter(travel_request_id=id).values('id'))))
    pending = next((x for x in settlements if x['status'] == Settlement.PENDING), None)
    trip = {'id': str(t.id), 'destination': t.destination, 'destinationCategory': t.destination_category, 'purpose': t.purpose,
            'fromDate': _s(t.from_date), 'toDate': _s(t.to_date), 'mode': t.mode, 'estimatedCost': _s(t.estimated_cost),
            'advanceRequested': _s(t.advance_requested), 'advanceApproved': _s(t.advance_approved),
            'advanceStatus': t.advance_status, 'advanceMethod': t.advance_method, 'fxRate': _s(t.fx_rate), 'days': t.days,
            'nights': t.nights, 'daRate': _s(t.da_rate), 'lodgingRate': _s(t.lodging_rate), 'daAmount': _s(t.da_amount),
            'lodgingLimit': _s(t.lodging_limit), 'status': t.status, 'decisionNote': t.decision_note,
            'decidedAt': t.decided_at, 'createdAt': t.created_at}
    return {
        'trip': {**trip, 'destinationLabel': DESTINATION_LABEL.get(t.destination_category), 'employee': _emp_card(emp)},
        'settlements': settlements, 'payouts': payouts,
        'canDecide': _can_act_on(user, t.workflow_instance_id) if t.status == Trip.PENDING and t.workflow_instance_id else False,
        'canDecideSettlement': _can_act_on(user, pending['workflowInstanceId']) if pending and pending['workflowInstanceId'] else False,
        'canSettle': str(t.employee_id) == user.employee_id and t.status == Trip.APPROVED and _s(t.from_date) <= npt_today(),
    }


def cancel_trip(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _trip(company_id, id, lock=True)
        if str(t.employee_id) != user.employee_id and not has_role(user, 'hr'):
            raise NotFoundError('travel request not found')
        if t.status not in (Trip.PENDING, Trip.APPROVED):
            raise ConflictError(f'a {t.status} trip cannot be cancelled')
        if t.advance_status == Advance.PAID:
            raise ConflictError('The advance has been paid; submit a settlement instead so it can be recovered.')
        advance = ExpensePayout.objects.for_company(company_id).filter(source_type=Source.TRAVEL_ADVANCE, source_id=id,
                                                                       status=Payout.PENDING)
        if advance.filter(payroll_run__isnull=False).exists():
            raise ConflictError('The advance is already in a pay run being prepared; submit a settlement instead.')
        advance.update(status=Payout.CANCELLED)
        TravelRequest.objects.filter(pk=id).update(status=Trip.CANCELLED, advance_status=Advance.NONE)
        cancel_workflow('travel_request', id)
        audit('cancel', 'travel_request', actor_id=user.id, row_id=id)
    return {'id': id, 'status': Trip.CANCELLED}


def review_trip(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = validate(s.TripReviewIn, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _trip(company_id, id, lock=True)
        if t.status != Trip.PENDING or not t.workflow_instance_id:
            raise ConflictError(f'travel request is {t.status}')
        if b['decision'] == 'approve' and 'advanceApproved' in b:
            if D(b['advanceApproved']) > t.advance_requested:
                raise BadRequestError('The approved advance cannot be more than requested.')
            TravelRequest.objects.filter(pk=id).update(advance_approved=money(b['advanceApproved']))
        state = act(instance_id=str(t.workflow_instance_id), user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


def pay_advance_outside(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = validate(s.NoteIn, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _trip(company_id, id, lock=True)
        if t.advance_status != Advance.PENDING:
            raise ConflictError('there is no advance waiting to be paid')
        _pay_outside(company_id, Source.TRAVEL_ADVANCE, id)
        TravelRequest.objects.filter(pk=id).update(advance_status=Advance.PAID, advance_method=TravelRequest.AdvanceMethod.CASH)
        audit('pay_outside_payroll', 'travel_request', actor_id=user.id, row_id=id, after={'note': b['note']})
    return {'id': id, 'advanceStatus': Advance.PAID}


def settlement_figures(trip, actual_from: str, actual_to: str, bills: list[dict]) -> dict:
    """Daily allowance for each day travelled, lodging up to the nightly limit, transport and other bills as spent, less the
    advance. `trip` is a TravelRequest (or a dict with da_rate, lodging_rate, fx_rate, advance_status, advance_approved)."""
    get = trip.get if isinstance(trip, dict) else (lambda k, d=None: getattr(trip, k, d))
    days = _days_between(actual_from, actual_to) + 1
    nights = days - 1
    fx = D(get('fx_rate') if get('fx_rate') is not None else 1)
    da = D(get('da_rate')) * fx * days
    lodging_claimed = sum((D(x['amount']) for x in bills if x['kind'] == TravelBill.Kind.LODGING), Decimal(0))
    lodging_allowed = min(lodging_claimed, D(get('lodging_rate')) * fx * max(nights, 0))
    other = sum((D(x['amount']) for x in bills if x['kind'] != TravelBill.Kind.LODGING), Decimal(0))
    entitlement = da + lodging_allowed + other
    advance = D(get('advance_approved') or 0) if get('advance_status') in (Advance.PENDING, Advance.PAID) else Decimal(0)
    return {'days': days, 'nights': nights, 'da': da, 'lodgingClaimed': lodging_claimed, 'lodgingAllowed': lodging_allowed,
            'other': other, 'entitlement': entitlement, 'advance': advance, 'net': entitlement - advance}


def own_trip(user: AuthUser, id: str) -> TravelRequest:
    """The caller's own trip (the settlement form's figures); anyone else's is not found."""
    t = TravelRequest.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(str(id))).first()
    if not t or str(t.employee_id) != user.employee_id:
        raise NotFoundError('travel request not found')
    return t


def settle_trip(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = plain(validate(s.SettleIn, body))
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _trip(company_id, id, lock=True)
        if str(t.employee_id) != user.employee_id:
            raise NotFoundError('travel request not found')
        if t.status != Trip.APPROVED:
            raise ConflictError('a settlement is already waiting for approval' if t.status == Trip.SETTLING
                                else f'a {t.status} trip cannot be settled')
        today = npt_today()
        if b['actualFrom'] > today:
            raise BadRequestError('Settle the trip once it has started.')
        if b['actualTo'] < b['actualFrom']:
            raise BadRequestError('The return date must be on or after the start date.')
        if b['actualTo'] > today:
            raise BadRequestError('The return date cannot be in the future.')
        for i, x in enumerate(b['bills']):
            if x['billDate'] < b['actualFrom'] or x['billDate'] > b['actualTo']:
                raise BadRequestError(f'Bill {i + 1}: the date must be within the trip.')
            if D(x['amount']) > BILL_RECEIPT_ABOVE and not x.get('fileId'):
                raise BadRequestError(f'Bill {i + 1}: attach the bill for amounts above {rs(BILL_RECEIPT_ABOVE)}.')
        if _days_between(b['actualFrom'], b['actualTo']) + 1 > 90:
            raise BadRequestError('A single trip can be at most 90 days.')
        f = settlement_figures(t, b['actualFrom'], b['actualTo'], b['bills'])
        st = TravelSettlement.objects.create(
            company_id=company_id, travel_request_id=id, employee_id=t.employee_id, actual_from=b['actualFrom'],
            actual_to=b['actualTo'], days=f['days'], nights=f['nights'], da_amount=money(f['da']),
            lodging_claimed=money(f['lodgingClaimed']), lodging_allowed=money(f['lodgingAllowed']), other_bills=money(f['other']),
            entitlement=money(f['entitlement']), advance=money(f['advance']), net=money(f['net']), note=b.get('note') or None,
            created_by_id=user.id)
        sid = str(st.id)
        for x in b['bills']:
            if x.get('fileId'):
                attach_file(company_id=company_id, file_id=x['fileId'], user_id=user.id, purpose=['travel_bill', 'expense_receipt'],
                            attached_to=f'travel_settlement:{sid}')
        TravelBill.objects.bulk_create([
            TravelBill(settlement_id=sid, kind=x['kind'], bill_date=x['billDate'], description=x['description'],
                       amount=money(x['amount']), file_id=x.get('fileId')) for x in b['bills']])
        emp = employee_of(company_id, t.employee_id)
        net = f['net']
        inst = start_workflow(company_id=company_id, object_type='travel_settlement', object_id=sid, employee_id=str(t.employee_id),
                              amount=float(f['entitlement']), title='Travel settlement to approve',
                              body=f"{emp['name_en']}: {t.destination}, " + (f'pay {rs(net)}' if net >= 0 else f'recover {rs(-net)}'))
        TravelSettlement.objects.filter(pk=sid).update(workflow_instance_id=inst['id'])
        TravelRequest.objects.filter(pk=id).update(status=Trip.SETTLING)
        audit('create', 'travel_settlement', actor_id=user.id, row_id=sid,
              after={'entitlement': money(f['entitlement']), 'advance': money(f['advance']), 'net': money(net)})
    return {'id': sid, 'daAmount': money(f['da']), 'lodgingAllowed': money(f['lodgingAllowed']), 'otherBills': money(f['other']),
            'entitlement': money(f['entitlement']), 'advance': money(f['advance']), 'net': money(net)}


def settlement_trip_id(user: AuthUser, id: str) -> str:
    """The trip a settlement of the active company belongs to (the drawer to redraw after a decision)."""
    trip_id = (TravelSettlement.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(str(id)))
               .values_list('travel_request_id', flat=True).first())
    if not trip_id:
        raise NotFoundError('settlement not found')
    return str(trip_id)


def review_settlement(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = validate(s.SettlementReviewIn, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        st = get_or_404(TravelSettlement.objects.select_for_update(), company_id, 'settlement not found', pk=id)
        if st.status != Settlement.PENDING or not st.workflow_instance_id:
            raise ConflictError(f'settlement is {st.status}')
        state = act(instance_id=str(st.workflow_instance_id), user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


def bill_file(user: AuthUser, id: str, bill_id: str) -> dict:
    id, bill_id = parse_uuid(str(id)), parse_uuid(str(bill_id))
    company_id = company_id_of(user)
    row = (TravelBill.objects.for_company(company_id).filter(pk=bill_id, settlement_id=id)
           .values('file_id', employee_id=F('settlement__employee_id')).first())
    if not row:
        raise NotFoundError('bill not found')
    _assert_can_see(user, row['employee_id'])
    if not row['file_id']:
        raise NotFoundError('no file for this bill')
    if str(row['employee_id']) != user.employee_id:
        audit('view_receipt', 'travel_bill', actor_id=user.id, row_id=bill_id)
    return read_file(company_id, str(row['file_id']))


# ---------------------------------------------------------------- reports (HR)

GROUP_LABEL = {'category': 'Category', 'department': 'Department', 'month': 'Month', 'employee': 'Employee'}


def _department_on(employee_ref: str, date_ref: str) -> Subquery:
    """The department of the employee's assignment on that date (employee_assignment.valid @> date)."""
    return Subquery(EmployeeAssignment.objects.filter(employee_id=OuterRef(employee_ref), valid__contains=OuterRef(date_ref))
                    .values('org_unit__name_en')[:1])


def _spend_lines(company_id: str, from_: str, to: str) -> list[dict]:
    """Approved or paid claim lines (approved amounts) and approved travel settlements (the entitlement, as 'Travel')."""
    items = (ExpenseItem.objects.filter(claim__company_id=company_id, claim__status__in=(Claim.APPROVED, Claim.PAID),
                                        expense_date__range=(from_, to))
             .values('expense_date', category_name=F('category__name'), department=_department_on('claim__employee_id', 'expense_date'),
                     emp_code=F('claim__employee__emp_code'), name_en=F('claim__employee__name_en'),
                     spent=Coalesce('approved_amount', 'amount')))
    trips = (TravelSettlement.objects.for_company(company_id).filter(status=Settlement.APPROVED, actual_to__range=(from_, to))
             .values(expense_date=F('actual_to'), category_name=Value('Travel'),
                     department=_department_on('employee_id', 'actual_to'), emp_code=F('employee__emp_code'),
                     name_en=F('employee__name_en'), spent=F('entitlement')))
    return [{**x, 'expense_date': _s(x['expense_date'])} for x in (*items, *trips)]


def report_spend(user: AuthUser, query: dict) -> dict:
    """Approved claims and settled trips grouped by category, department, BS month or employee."""
    _require_hr(user)
    q = validate(s.SpendQueryIn, {k: v for k, v in (query or {}).items() if k in ('from', 'to', 'groupBy', 'format') and v not in (None, '')})
    if q['to'] < q['from']:
        raise BadRequestError('the end date must be after the start date')
    gb = q['groupBy']
    groups: dict[str, dict] = {}
    for x in _spend_lines(company_id_of(user), q['from'], q['to']):
        if gb == 'category':
            key = x['category_name']
        elif gb == 'department':
            key = x['department'] or 'No department'
        elif gb == 'employee':
            key = f"{x['emp_code']} {x['name_en']}"
        else:
            key = bs_label(x['expense_date']).split(' ', 1)[-1]
        g = groups.setdefault(key, {'key': key, 'amount': Decimal(0), 'count': 0, 'sort': x['expense_date']})
        g['amount'] += x['spent']
        g['count'] += 1
        if x['expense_date'] < g['sort']:
            g['sort'] = x['expense_date']
    vals = list(groups.values())
    ordered = sorted(vals, key=lambda g: g['sort']) if gb == 'month' else sorted(vals, key=lambda g: -g['amount'])
    return {'filename': f'expense-spend-by-{gb}',
            'columns': [{'key': 'group', 'header': GROUP_LABEL[gb]}, {'key': 'items', 'header': 'Items'}, {'key': 'amount', 'header': 'Amount (NPR)'}],
            'rows': [{'group': g['key'], 'items': g['count'], 'amount': money(g['amount'])} for g in ordered]}


def report_advances(user: AuthUser) -> dict:
    _require_hr(user)
    out = [{'id': str(t.id), 'empCode': t.employee.emp_code, 'employeeName': t.employee.name_en, 'destination': t.destination,
            'fromDate': _s(t.from_date), 'toDate': _s(t.to_date), 'advance': _s(t.advance_approved),
            'advanceStatus': t.advance_status, 'status': t.status, 'trip': bs_range(t.from_date, t.to_date)}
           for t in TravelRequest.objects.for_company(company_id_of(user)).select_related('employee').filter(
               advance_status__in=(Advance.PENDING, Advance.PAID), status__in=(Trip.APPROVED, Trip.SETTLING)).order_by('to_date', 'id')]
    return {'filename': 'outstanding-travel-advances', 'columns': [
        {'key': 'empCode', 'header': 'Code'}, {'key': 'employeeName', 'header': 'Employee'}, {'key': 'destination', 'header': 'Destination'},
        {'key': 'trip', 'header': 'Trip'}, {'key': 'advance', 'header': 'Advance (NPR)'}, {'key': 'advanceStatus', 'header': 'Advance'},
        {'key': 'status', 'header': 'Trip status'}], 'rows': out}


def report_pending_payment(user: AuthUser) -> dict:
    _require_hr(user)
    out = []
    for p in (ExpensePayout.objects.for_company(company_id_of(user)).filter(status=Payout.PENDING)
              .order_by('employee__emp_code', 'created_at', 'id')
              .values('id', 'source_type', 'source_id', 'component_code', 'amount', 'note', 'created_at',
                      emp_code=F('employee__emp_code'), employee_name=F('employee__name_en'),
                      bs_year=F('payroll_run__period__bs_year'), bs_month=F('payroll_run__period__bs_month'))):
        out.append({'id': str(p['id']), 'empCode': p['emp_code'], 'employeeName': p['employee_name'], 'sourceType': p['source_type'],
                    'sourceId': str(p['source_id']), 'code': p['component_code'], 'amount': _s(p['amount']), 'note': p['note'],
                    'createdAt': p['created_at'], 'bsYear': p['bs_year'], 'bsMonth': p['bs_month'],
                    'kind': 'Recover' if p['component_code'] == Code.EXP_RECOVERY else 'Pay',
                    'inRun': f"{p['bs_year']}-{p['bs_month']:02d}" if p['bs_year'] else 'Next pay run'})
    return {'filename': 'expenses-pending-payment', 'columns': [
        {'key': 'empCode', 'header': 'Code'}, {'key': 'employeeName', 'header': 'Employee'}, {'key': 'note', 'header': 'For'},
        {'key': 'kind', 'header': 'Pay or recover'}, {'key': 'amount', 'header': 'Amount (NPR)'}, {'key': 'inRun', 'header': 'Pay run'}],
        'rows': out}
