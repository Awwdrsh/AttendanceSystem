"""
Expense claims and travel (port of apps/api/src/modules/expenses: service.ts and routes.ts).

Claims: an employee files lines (amounts, or km x the company mileage rate) with receipts; limits by grade, receipt
thresholds and the claim window are checked before submitting; the claim goes through the manager -> HR chain and an
approved claim becomes an expense_payout row that the next pay run pays (hajiri.expenses.payroll).

Travel: a trip is requested with its daily allowance (TADA by grade and destination) and an optional advance, which
payroll pays once the trip is approved; afterwards the traveller settles it with bills, and the difference between
the entitlement and the advance is paid or recovered through payroll.

The route-level functions take the signed-in AuthUser and a body in the TS camelCase shape (validated with the same
rules as the zod schemas) and return what the TS route answered, so the views and the tests share one contract.
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.db import transaction

from engines.bs_calendar import ad_to_bs, bs_month_bounds
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.bsdate import bs_label, bs_range
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid
from hajiri.files.store import attach_file, read_file
from hajiri.notifications.service import notify, user_id_for_employee
from hajiri.web.format import group_digits
from hajiri.workflow.service import (DecisionContext, act, can_act, cancel_workflow, get_instance, register_handler,
                                     register_summariser, start_workflow)

# ---------------------------------------------------------------- constants

DESTINATIONS = ('valley', 'city', 'rural', 'international')
DESTINATION_LABEL = {'valley': 'Kathmandu valley', 'city': 'Other city', 'rural': 'Rural area', 'international': 'International'}
MODES = ('bus', 'air', 'office_vehicle', 'own_vehicle', 'other')
MODE_LABEL = {'bus': 'Bus', 'air': 'Air', 'office_vehicle': 'Office vehicle', 'own_vehicle': 'Own vehicle', 'other': 'Other'}
BILL_KINDS = ('transport', 'lodging', 'other')
BILL_KIND_LABEL = {'transport': 'Transport', 'lodging': 'Lodging', 'other': 'Other'}
CLAIM_STATUSES = ('draft', 'pending', 'approved', 'rejected', 'cancelled', 'paid')
TRIP_STATUSES = ('pending', 'approved', 'rejected', 'cancelled', 'settling', 'settled')
SCOPES = ('own', 'team', 'all')
# Travel bills above this need the bill attached.
BILL_RECEIPT_ABOVE = 500

_AMOUNT_RE = r'^\d{1,10}(\.\d{1,2})?$'
_FX_RE = r'^\d{1,6}(\.\d{1,4})?$'


def _amount(**kw) -> Str:
    return Str(regex=_AMOUNT_RE, regex_message='amount with up to 2 decimals', **kw)


def _fx(**kw) -> Str:
    return Str(regex=_FX_RE, regex_message='exchange rate with up to 4 decimals', **kw)


ITEM_SCHEMA = Obj({
    'expenseDate': IsoDate(), 'categoryId': Uuid(), 'description': Str(trim=True, min=1, max=300),
    'amount': _amount(optional=True), 'km': Num(min=0, max=5000, optional=True,
                                                refine=(lambda v: v > 0, 'Too small: expected number to be >0')),
    'receiptFileId': Uuid(nullish=True),
})
CLAIM_SCHEMA = Obj({'title': Str(trim=True, min=1, max=120), 'items': Arr(ITEM_SCHEMA, min=1, max=50), 'submit': Bool(optional=True)})


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
    s = money(d)
    neg = s.startswith('-')
    i, _, f = s.lstrip('-').partition('.')
    f = f.rstrip('0')
    return f"Rs {'-' if neg else ''}{group_digits(i)}{'.' + f if f else ''}"


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days


def npt_today() -> str:
    return _npt_today().isoformat()


def _s(v) -> str | None:
    return None if v is None else str(v)


# ---------------------------------------------------------------- policy

def expense_policy(company_id: str) -> dict:
    r = one("SELECT value FROM company_setting WHERE company_id = %s AND key = 'expense.policy'", [company_id])
    v = (r or {}).get('value') or {}
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


def employee_of(company_id: str, employee_id: str) -> dict:
    r = one('SELECT id::text, name_en, emp_code, grade, status FROM employee WHERE id = %s AND company_id = %s',
            [str(employee_id), company_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def _self_employee(user: AuthUser) -> str:
    if not user.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    return user.employee_id


# ---------------------------------------------------------------- claim checks

def _categories(company_id: str) -> dict[str, dict]:
    cats = rows("""SELECT id::text, code, name, kind, receipt_required_above::text, active FROM expense_category
                    WHERE company_id = %s""", [company_id])
    return {c['id']: c for c in cats}


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
        if category['kind'] == 'mileage':
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
    limits = rows("""SELECT category_id::text, grade, per_claim::text, per_month::text FROM expense_limit
                      WHERE company_id = %s AND category_id = ANY(%s::uuid[]) AND grade IN (%s, '*')""",
                  [company_id, cat_ids, emp['grade'] or '*'])

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
        if lim['per_claim'] is not None and total > D(lim['per_claim']):
            issues.append({'level': level, 'code': 'per_claim_limit',
                           'message': f"{name}: {rs(total)} is over the limit of {rs(lim['per_claim'])} per claim."})
        if lim['per_month'] is not None:
            months: dict[tuple[int, int], Decimal] = {}
            for x in mine:
                b = ad_to_bs(str(x['expenseDate']))
                months[(b.year, b.month)] = months.get((b.year, b.month), Decimal(0)) + D(x['amount'])
            for (y, m), amt in months.items():
                bounds = bs_month_bounds(y, m)
                other = one("""SELECT coalesce(sum(coalesce(i.approved_amount, i.amount)), 0)::text AS total
                                 FROM expense_item i JOIN expense_claim c ON c.id = i.claim_id
                                WHERE c.employee_id = %s AND i.category_id = %s AND c.status IN ('pending', 'approved', 'paid')
                                  AND i.expense_date BETWEEN %s AND %s AND (%s::uuid IS NULL OR c.id <> %s::uuid)""",
                            [employee_id, cat_id, bounds['startAd'], bounds['endAd'], exclude_claim_id, exclude_claim_id])
                month_total = amt + D(other['total'])
                if month_total > D(lim['per_month']):
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
    rate = one("""SELECT grade, da_rate::text, lodging_rate::text FROM tada_rate WHERE company_id = %s AND destination = %s
                   AND grade IN (%s, '*') ORDER BY (grade = '*') LIMIT 1""", [company_id, dest, emp['grade'] or '*']) \
        or {'grade': '*', 'da_rate': '0', 'lodging_rate': '0'}
    international = dest == 'international'
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
    if not amount > 0:
        return
    execute("""INSERT INTO expense_payout (company_id, employee_id, source_type, source_id, component_code, amount, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (source_type, source_id, component_code) WHERE parent_payout_id IS NULL DO NOTHING""",
            [str(company_id), str(employee_id), source_type, str(source_id), code, money(amount), note[:200]])


def _tell(company_id, employee_id, template: str, title: str, body: str, payload: dict) -> None:
    user_id = user_id_for_employee(str(employee_id))
    if not user_id:
        return
    notify(company_id=str(company_id), user_id=user_id, employee_id=str(employee_id), template=template, title=title,
           body=body, payload=payload, channels=('push',))


def _why(reason: str | None) -> str:
    return f': {reason}' if reason else ''


# ---------------------------------------------------------------- workflow handlers

def apply_claim_decision(claim_id: str, state: str, ctx: DecisionContext) -> None:
    c = one('SELECT company_id::text, employee_id::text, title, status FROM expense_claim WHERE id = %s FOR UPDATE', [claim_id])
    if not c:
        raise NotFoundError('claim not found')
    if c['status'] != 'pending':
        raise ConflictError(f"claim already {c['status']}")
    if state == 'rejected':
        execute("""UPDATE expense_claim SET status = 'rejected', decided_at = now(), decision_note = %s, updated_at = now()
                    WHERE id = %s""", [ctx.reason, claim_id])
        _tell(c['company_id'], c['employee_id'], 'expense_claim.rejected', 'Expense claim rejected',
              f"\"{c['title']}\" was rejected{_why(ctx.reason)}.", {'claimId': claim_id})
        return
    execute('UPDATE expense_item SET approved_amount = coalesce(approved_amount, amount) WHERE claim_id = %s', [claim_id])
    total = D(one('SELECT coalesce(sum(approved_amount), 0)::text AS total FROM expense_item WHERE claim_id = %s', [claim_id])['total'])
    execute("""UPDATE expense_claim SET status = %s, total_approved = %s, decided_at = now(), decision_note = %s,
                      payment_method = 'payroll', updated_at = now() WHERE id = %s""",
            ['approved' if total > 0 else 'rejected', money(total), ctx.reason, claim_id])
    if not total > 0:
        _tell(c['company_id'], c['employee_id'], 'expense_claim.rejected', 'Expense claim not paid',
              f"\"{c['title']}\" was approved for nothing{_why(ctx.reason)}.", {'claimId': claim_id})
        return
    create_payout(company_id=c['company_id'], employee_id=c['employee_id'], source_type='expense_claim', source_id=claim_id,
                  code='EXP_REIMB', amount=total, note=f"Claim: {c['title']}")
    _tell(c['company_id'], c['employee_id'], 'expense_claim.approved', 'Expense claim approved',
          f"\"{c['title']}\" was approved for {rs(total)}. It will be paid with your next salary.", {'claimId': claim_id})


def apply_travel_decision(travel_id: str, state: str, ctx: DecisionContext) -> None:
    t = one("""SELECT company_id::text, employee_id::text, destination, status, from_date::text, to_date::text,
                      advance_requested::text, advance_approved::text FROM travel_request WHERE id = %s FOR UPDATE""", [travel_id])
    if not t:
        raise NotFoundError('travel request not found')
    if t['status'] != 'pending':
        raise ConflictError(f"travel request already {t['status']}")
    when = bs_range(t['from_date'], t['to_date'])
    if state == 'rejected':
        execute("UPDATE travel_request SET status = 'rejected', decided_at = now(), decision_note = %s WHERE id = %s", [ctx.reason, travel_id])
        _tell(t['company_id'], t['employee_id'], 'travel_request.rejected', 'Travel request rejected',
              f"Your trip to {t['destination']} ({when}) was rejected{_why(ctx.reason)}.", {'travelId': travel_id})
        return
    advance = D(t['advance_approved'] if t['advance_approved'] is not None else t['advance_requested'])
    execute("""UPDATE travel_request SET status = 'approved', decided_at = now(), decision_note = %s, advance_approved = %s,
                      advance_status = CASE WHEN %s::numeric > 0 THEN 'pending' ELSE 'none' END,
                      advance_method = CASE WHEN %s::numeric > 0 THEN 'payroll' END WHERE id = %s""",
            [ctx.reason, money(advance), money(advance), money(advance), travel_id])
    create_payout(company_id=t['company_id'], employee_id=t['employee_id'], source_type='travel_advance', source_id=travel_id,
                  code='TRAVEL_ADV', amount=advance, note=f"Travel advance: {t['destination']}")
    with_adv = f' with an advance of {rs(advance)}' if advance > 0 else ''
    _tell(t['company_id'], t['employee_id'], 'travel_request.approved', 'Travel request approved',
          f"Your trip to {t['destination']} ({when}) was approved{with_adv}.", {'travelId': travel_id})


def apply_settlement_decision(settlement_id: str, state: str, ctx: DecisionContext) -> None:
    s = one("""SELECT s.company_id::text, s.employee_id::text, s.status, s.travel_request_id::text, s.entitlement::text,
                      s.advance::text, s.net::text, t.destination
                 FROM travel_settlement s JOIN travel_request t ON t.id = s.travel_request_id WHERE s.id = %s FOR UPDATE OF s""",
            [settlement_id])
    if not s:
        raise NotFoundError('settlement not found')
    if s['status'] != 'pending':
        raise ConflictError(f"settlement already {s['status']}")
    if state == 'rejected':
        execute("UPDATE travel_settlement SET status = 'rejected', decided_at = now(), decision_note = %s WHERE id = %s",
                [ctx.reason, settlement_id])
        execute("UPDATE travel_request SET status = 'approved' WHERE id = %s AND status = 'settling'", [s['travel_request_id']])
        _tell(s['company_id'], s['employee_id'], 'travel_settlement.rejected', 'Travel settlement returned',
              f"Your settlement for {s['destination']} was rejected{_why(ctx.reason)}. You can correct and submit it again.",
              {'settlementId': settlement_id})
        return
    # An advance that payroll has not picked up yet is never paid: it is cancelled and the whole entitlement is settled instead.
    advance = D(s['advance'])
    adv = rows("""UPDATE expense_payout SET status = 'cancelled' WHERE source_type = 'travel_advance' AND source_id = %s
                   AND status = 'pending' AND payroll_run_id IS NULL RETURNING id""", [s['travel_request_id']])
    if adv:
        advance = Decimal(0)
        execute("UPDATE travel_request SET advance_status = 'none' WHERE id = %s", [s['travel_request_id']])
    net = D(s['entitlement']) - advance
    execute("""UPDATE travel_settlement SET status = 'approved', decided_at = now(), decision_note = %s, advance = %s, net = %s
                WHERE id = %s""", [ctx.reason, money(advance), money(net), settlement_id])
    execute("UPDATE travel_request SET status = 'settled' WHERE id = %s", [s['travel_request_id']])
    if net > 0:
        create_payout(company_id=s['company_id'], employee_id=s['employee_id'], source_type='travel_settlement', source_id=settlement_id,
                      code='EXP_REIMB', amount=net, note=f"Travel settlement: {s['destination']}")
    if net < 0:
        create_payout(company_id=s['company_id'], employee_id=s['employee_id'], source_type='travel_settlement', source_id=settlement_id,
                      code='EXP_RECOVERY', amount=-net, note=f"Unspent travel advance: {s['destination']}")
    if net > 0:
        body = f"{rs(net)} for your trip to {s['destination']} will be paid with your next salary."
    elif net < 0:
        body = f"{rs(-net)} of unspent advance for {s['destination']} will be recovered from your next salary."
    else:
        body = f"Your trip to {s['destination']} is settled with nothing more to pay or recover."
    _tell(s['company_id'], s['employee_id'], 'travel_settlement.approved', 'Travel settlement approved', body,
          {'settlementId': settlement_id})


def _summary_claim(inst: dict) -> dict:
    c = one("""SELECT c.title, c.total_claimed::text, (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id) AS items,
                      (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id AND i.receipt_file_id IS NOT NULL) AS receipts,
                      (SELECT string_agg(DISTINCT ec.name, ', ') FROM expense_item i JOIN expense_category ec ON ec.id = i.category_id
                        WHERE i.claim_id = c.id) AS categories
                 FROM expense_claim c WHERE c.id = %s""", [inst['object_id']])
    return {'title': c['title'], 'amount': c['total_claimed'], 'items': c['items'], 'receipts': c['receipts'],
            'categories': c['categories']} if c else {}


def _summary_travel(inst: dict) -> dict:
    t = one("""SELECT destination, destination_category, purpose, from_date::text, to_date::text, days, da_amount::text,
                      estimated_cost::text, advance_requested::text, mode FROM travel_request WHERE id = %s""", [inst['object_id']])
    return {'destination': t['destination'], 'destinationCategory': DESTINATION_LABEL.get(t['destination_category']),
            'purpose': t['purpose'], 'from': t['from_date'], 'to': t['to_date'], 'days': t['days'],
            'dailyAllowance': t['da_amount'], 'estimatedCost': t['estimated_cost'], 'advance': t['advance_requested'],
            'mode': t['mode']} if t else {}


def _summary_settlement(inst: dict) -> dict:
    s = one("""SELECT t.destination, s.actual_from::text, s.actual_to::text, s.entitlement::text, s.advance::text, s.net::text,
                      (SELECT count(*)::int FROM travel_bill b WHERE b.settlement_id = s.id) AS bills
                 FROM travel_settlement s JOIN travel_request t ON t.id = s.travel_request_id WHERE s.id = %s""", [inst['object_id']])
    return {'destination': s['destination'], 'from': s['actual_from'], 'to': s['actual_to'], 'entitlement': s['entitlement'],
            'advance': s['advance'], 'net': s['net'], 'bills': s['bills']} if s else {}


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

def _default_scope(u: AuthUser) -> str:
    return 'all' if has_role(u, 'hr') else 'team' if has_role(u, 'manager') else 'own'


def _scope_filter(u: AuthUser, scope: str, alias: str) -> tuple[str, list]:
    """Own records, a manager's reports (and own), or the whole company for HR."""
    if scope == 'all':
        if not has_role(u, 'hr'):
            raise ForbiddenError('only HR can see everyone')
        return 'TRUE', []
    if scope == 'team':
        if not has_role(u, 'manager', 'hr') or not u.employee_id:
            raise ForbiddenError('only managers can see their team')
        return f'({alias}.employee_id = %s OR hj_manages(%s::uuid, {alias}.employee_id, (now() AT TIME ZONE \'Asia/Kathmandu\')::date))', [u.employee_id, u.employee_id]
    if not u.employee_id:
        raise BadRequestError('your account is not linked to an employee')
    return f'{alias}.employee_id = %s', [u.employee_id]


def _assert_can_see(u: AuthUser, employee_id: str) -> None:
    """The employee, their manager and HR may read a claim or trip; everyone else gets a 404."""
    if has_role(u, 'hr') or u.employee_id == str(employee_id):
        return
    if has_role(u, 'manager') and u.employee_id and is_manager_of(u.employee_id, str(employee_id)):
        return
    raise NotFoundError('not found')


def _can_act_on(user: AuthUser, instance_id) -> bool:
    inst = get_instance(str(instance_id)) if instance_id else None
    return can_act(user, inst) if inst else False


def _pay_outside(source_type: str, source_id: str) -> None:
    """Settle a source's pending payout outside payroll; refused while a pay run being prepared holds it."""
    p = rows("""SELECT id, payroll_run_id FROM expense_payout WHERE source_type = %s AND source_id = %s AND status = 'pending'
                 FOR UPDATE""", [source_type, source_id])
    if not p:
        raise ConflictError('nothing is waiting to be paid')
    if any(x['payroll_run_id'] for x in p):
        raise ConflictError('This payment is already in a pay run being prepared. Pay it in that run instead.')
    execute("""UPDATE expense_payout SET status = 'cash', paid_amount = amount, paid_at = now()
                WHERE source_type = %s AND source_id = %s AND status = 'pending'""", [source_type, source_id])
    from .payroll import mark_sources_paid
    mark_sources_paid([{'source_type': source_type, 'source_id': source_id}])


def _claim_row(company_id: str, id: str) -> dict:
    r = one("""SELECT id::text, employee_id::text, title, status, total_claimed::text, total_approved::text,
                      workflow_instance_id::text, payment_method FROM expense_claim WHERE id = %s AND company_id = %s""", [id, company_id])
    if not r:
        raise NotFoundError('claim not found')
    return r


# ---------------------------------------------------------------- policy routes

def get_policy(user: AuthUser) -> dict:
    company_id = company_id_of(user)
    settings = expense_policy(company_id)
    cats = rows("""SELECT id::text, code, name, kind, receipt_required_above::text AS "receiptRequiredAbove", active,
                          sort_order AS "sortOrder" FROM expense_category WHERE company_id = %s ORDER BY sort_order, name""", [company_id])
    limits = rows("""SELECT id::text, category_id::text AS "categoryId", grade, per_claim::text AS "perClaim",
                            per_month::text AS "perMonth" FROM expense_limit WHERE company_id = %s ORDER BY grade""", [company_id])
    rates = rows("""SELECT grade, destination, da_rate::text AS "daRate", lodging_rate::text AS "lodgingRate" FROM tada_rate
                     WHERE company_id = %s ORDER BY (grade = '*') DESC, grade, destination""", [company_id])
    grade = employee_of(company_id, user.employee_id)['grade'] if user.employee_id else None
    grades = [g['grade'] for g in rows('SELECT DISTINCT grade FROM employee WHERE company_id = %s AND grade IS NOT NULL ORDER BY grade',
                                       [company_id])] if has_role(user, 'hr') else []
    return {'settings': settings, 'categories': cats, 'limits': limits, 'tadaRates': rates,
            'destinations': [{'value': d, 'label': DESTINATION_LABEL[d]} for d in DESTINATIONS], 'myGrade': grade, 'grades': grades}


SETTINGS_SCHEMA = Obj({'mileageRatePerKm': _amount(), 'limitMode': Enum(('block', 'warn')), 'claimWindowDays': Int(min=7, max=365)})


def save_settings(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse(SETTINGS_SCHEMA, body)
    company_id = company_id_of(user)
    import json
    with transaction.atomic():
        before = expense_policy(company_id)
        execute("""INSERT INTO company_setting (company_id, key, value) VALUES (%s, 'expense.policy', %s)
                   ON CONFLICT (company_id, key) DO UPDATE SET value = EXCLUDED.value""", [company_id, json.dumps(b)])
        audit('update', 'company_setting', actor_id=user.id, row_id=None, before={'key': 'expense.policy', **before}, after=b)
    return {'settings': expense_policy(company_id)}


def _category_fields(partial: bool) -> dict:
    o = {'optional': True} if partial else {}
    f = {
        'name': Str(trim=True, min=1, max=80, **o), 'kind': Enum(('amount', 'mileage'), **o),
        'receiptRequiredAbove': _amount(nullish=True), 'active': Bool(optional=True), 'sortOrder': Int(min=0, max=9999, optional=True),
    }
    if not partial:
        f['code'] = Str(regex=r'^[A-Z][A-Z0-9_]{1,29}$', regex_message='capital letters, digits and _')
    return f


def create_category(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse(Obj(_category_fields(False)), body)
    with transaction.atomic():
        r = one("""INSERT INTO expense_category (company_id, code, name, kind, receipt_required_above, active, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                [company_id_of(user), b['code'], b['name'], b['kind'], b.get('receiptRequiredAbove'), b.get('active', True),
                 b.get('sortOrder', 100)])
        audit('create', 'expense_category', actor_id=user.id, row_id=r['id'], after=b)
    return {'id': r['id']}


def update_category(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = parse(Obj(_category_fields(True)), body)
    with transaction.atomic():
        r = one("""UPDATE expense_category SET name = coalesce(%s, name), kind = coalesce(%s, kind),
                          receipt_required_above = CASE WHEN %s::boolean THEN %s::numeric ELSE receipt_required_above END,
                          active = coalesce(%s, active), sort_order = coalesce(%s, sort_order)
                    WHERE id = %s AND company_id = %s RETURNING id""",
                [b.get('name'), b.get('kind'), 'receiptRequiredAbove' in b, b.get('receiptRequiredAbove'), b.get('active'),
                 b.get('sortOrder'), id, company_id_of(user)])
        if not r:
            raise NotFoundError('category not found')
        audit('update', 'expense_category', actor_id=user.id, row_id=id, after=b)
    return {'ok': True}


LIMITS_SCHEMA = Obj({'limits': Arr(Obj({'grade': Str(trim=True, min=1, max=40), 'perClaim': _amount(nullish=True),
                                        'perMonth': _amount(nullish=True)}), max=50)})


def set_limits(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = parse(LIMITS_SCHEMA, body)
    company_id = company_id_of(user)
    grades = [x['grade'] for x in b['limits']]
    if len(set(grades)) != len(grades):
        raise BadRequestError('each grade can appear only once')
    with transaction.atomic():
        if not one('SELECT 1 AS x FROM expense_category WHERE id = %s AND company_id = %s', [id, company_id]):
            raise NotFoundError('category not found')
        execute('DELETE FROM expense_limit WHERE category_id = %s', [id])
        for x in b['limits']:
            if not x.get('perClaim') and not x.get('perMonth'):
                continue
            execute('INSERT INTO expense_limit (company_id, category_id, grade, per_claim, per_month) VALUES (%s, %s, %s, %s, %s)',
                    [company_id, id, x['grade'], x.get('perClaim'), x.get('perMonth')])
        audit('update', 'expense_limit', actor_id=user.id, row_id=id, after=b['limits'])
    return {'ok': True}


TADA_SCHEMA = Obj({'rates': Arr(Obj({'grade': Str(trim=True, min=1, max=40), 'destination': Enum(DESTINATIONS),
                                     'daRate': _amount(), 'lodgingRate': _amount()}), max=200)})


def set_tada_rates(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse(TADA_SCHEMA, body)
    keys = [f"{x['grade']}|{x['destination']}" for x in b['rates']]
    if len(set(keys)) != len(keys):
        raise BadRequestError('each grade and destination can appear only once')
    company_id = company_id_of(user)
    with transaction.atomic():
        execute('DELETE FROM tada_rate WHERE company_id = %s', [company_id])
        for x in b['rates']:
            execute('INSERT INTO tada_rate (company_id, grade, destination, da_rate, lodging_rate) VALUES (%s, %s, %s, %s, %s)',
                    [company_id, x['grade'], x['destination'], x['daRate'], x['lodgingRate']])
        audit('update', 'tada_rate', actor_id=user.id, row_id=None, after=b['rates'])
    return {'ok': True}


# ---------------------------------------------------------------- claims

def check_items(user: AuthUser, body: dict) -> dict:
    b = parse(Obj({'items': Arr(ITEM_SCHEMA, min=1, max=50), 'claimId': Uuid(optional=True)}), body)
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
    execute('DELETE FROM expense_item WHERE claim_id = %s', [claim_id])
    for i, it in enumerate(items):
        if it.get('receiptFileId'):
            attach_file(company_id=company_id, file_id=it['receiptFileId'], user_id=user_id, purpose='expense_receipt',
                        attached_to=f'expense_claim:{claim_id}')
        execute("""INSERT INTO expense_item (claim_id, expense_date, category_id, description, amount, km, receipt_file_id, sort_order)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                [claim_id, it['expenseDate'], it['categoryId'], it['description'], it['amount'], it.get('km'),
                 it.get('receiptFileId'), i])
    total = sum((D(it['amount']) for it in items), Decimal(0))
    execute('UPDATE expense_claim SET total_claimed = %s, updated_at = now() WHERE id = %s', [money(total), claim_id])
    return issues, total


def _submit(company_id: str, claim_id: str, employee_id: str, title: str, total: str) -> None:
    emp = employee_of(company_id, employee_id)
    inst = start_workflow(company_id=company_id, object_type='expense_claim', object_id=claim_id, employee_id=employee_id,
                          amount=float(total), title='Expense claim to approve', body=f"{emp['name_en']}: {title}, {rs(total)}")
    execute("""UPDATE expense_claim SET status = 'pending', submitted_at = now(), workflow_instance_id = %s, updated_at = now()
                WHERE id = %s""", [inst['id'], claim_id])


def create_claim(user: AuthUser, body: dict) -> dict:
    b = parse(CLAIM_SCHEMA, body)
    company_id = company_id_of(user)
    employee_id = _self_employee(user)
    emp = employee_of(company_id, employee_id)
    if emp['status'] != 'active':
        raise ForbiddenError('only active employees can claim expenses')
    submit = bool(b.get('submit'))
    with transaction.atomic():
        id = one('INSERT INTO expense_claim (company_id, employee_id, title, created_by) VALUES (%s, %s, %s, %s) RETURNING id::text',
                 [company_id, employee_id, b['title'], user.id])['id']
        issues, total = _write_items(company_id, user.id, employee_id, id, b['items'], submit)
        if submit:
            _submit(company_id, id, employee_id, b['title'], money(total))
        audit('create', 'expense_claim', actor_id=user.id, row_id=id, after={'title': b['title'], 'total': money(total), 'submitted': submit})
    return {'id': id, 'status': 'pending' if submit else 'draft', 'total': money(total), 'issues': issues}


def update_claim(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = parse(CLAIM_SCHEMA, body)
    company_id = company_id_of(user)
    submit = bool(b.get('submit'))
    with transaction.atomic():
        row = one('SELECT employee_id::text, status FROM expense_claim WHERE id = %s AND company_id = %s FOR UPDATE', [id, company_id])
        if not row or row['employee_id'] != user.employee_id:
            raise NotFoundError('claim not found')
        if row['status'] != 'draft':
            raise ConflictError('only a draft claim can be edited')
        execute('UPDATE expense_claim SET title = %s WHERE id = %s', [b['title'], id])
        issues, total = _write_items(company_id, user.id, row['employee_id'], id, b['items'], submit)
        if submit:
            _submit(company_id, id, row['employee_id'], b['title'], money(total))
    return {'id': id, 'status': 'pending' if submit else 'draft', 'total': money(total), 'issues': issues}


def _stored_items(claim_id: str) -> list[dict]:
    its = rows("""SELECT expense_date::text, category_id::text, description, amount::text, km::text, receipt_file_id::text
                    FROM expense_item WHERE claim_id = %s ORDER BY sort_order""", [claim_id])
    return [{'expenseDate': i['expense_date'], 'categoryId': i['category_id'], 'description': i['description'], 'amount': i['amount'],
             'km': float(i['km']) if i['km'] else None, 'receiptFileId': i['receipt_file_id']} for i in its]


def submit_claim(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    with transaction.atomic():
        row = one('SELECT employee_id::text, status, title FROM expense_claim WHERE id = %s AND company_id = %s FOR UPDATE', [id, company_id])
        if not row or row['employee_id'] != user.employee_id:
            raise NotFoundError('claim not found')
        if row['status'] != 'draft':
            raise ConflictError(f"claim is {row['status']}")
        raw = _stored_items(id)
        if not raw:
            raise BadRequestError('add at least one expense')
        _, total = _write_items(company_id, user.id, row['employee_id'], id, raw, True)
        _submit(company_id, id, row['employee_id'], row['title'], money(total))
    return {'id': id, 'status': 'pending'}


def cancel_claim(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    with transaction.atomic():
        c = _claim_row(company_id_of(user), id)
        if c['employee_id'] != user.employee_id:
            raise NotFoundError('claim not found')
        if c['status'] not in ('draft', 'pending'):
            raise ConflictError(f"a {c['status']} claim cannot be cancelled")
        execute("UPDATE expense_claim SET status = 'cancelled', updated_at = now() WHERE id = %s", [id])
        cancel_workflow('expense_claim', id)
    return {'id': id, 'status': 'cancelled'}


def list_claims(user: AuthUser, query: dict | None = None) -> dict:
    q = parse(Obj({'scope': Enum(SCOPES, optional=True), 'status': Enum(CLAIM_STATUSES, optional=True),
                   'employeeId': Uuid(optional=True)}), {k: v for k, v in (query or {}).items() if v not in (None, '')})
    scope = q.get('scope') or _default_scope(user)
    sql, params = _scope_filter(user, scope, 'c')
    params = [company_id_of(user), *params]
    extra = ''
    if q.get('status'):
        params.append(q['status'])
        extra += ' AND c.status = %s'
    if q.get('employeeId'):
        params.append(q['employeeId'])
        extra += ' AND c.employee_id = %s'
    # Drafts are private to the employee.
    if user.employee_id:
        params.append(user.employee_id)
        extra += " AND (c.status <> 'draft' OR c.employee_id = %s)"
    else:
        extra += " AND c.status <> 'draft'"
    claims = rows(f"""
        SELECT c.id::text, c.title, c.status, c.total_claimed::text AS "totalClaimed", c.total_approved::text AS "totalApproved",
               c.submitted_at AS "submittedAt", c.decided_at AS "decidedAt", c.payment_method AS "paymentMethod", c.paid_at AS "paidAt",
               c.created_at AS "createdAt", e.id::text AS "employeeId", e.emp_code AS "empCode", e.name_en AS "employeeName",
               (SELECT count(*)::int FROM expense_item i WHERE i.claim_id = c.id) AS items,
               (SELECT min(i.expense_date)::text FROM expense_item i WHERE i.claim_id = c.id) AS "firstDate",
               (SELECT max(i.expense_date)::text FROM expense_item i WHERE i.claim_id = c.id) AS "lastDate",
               p.status AS "payoutStatus", pp.bs_year AS "payBsYear", pp.bs_month AS "payBsMonth"
          FROM expense_claim c JOIN employee e ON e.id = c.employee_id
          LEFT JOIN expense_payout p ON p.source_type = 'expense_claim' AND p.source_id = c.id AND p.parent_payout_id IS NULL
          LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
         WHERE c.company_id = %s AND {sql}{extra}
         ORDER BY c.created_at DESC LIMIT 500""", params)
    return {'claims': claims}


def _emp_card(emp: dict) -> dict:
    return {'id': emp['id'], 'empCode': emp['emp_code'], 'name': emp['name_en'], 'grade': emp['grade']}


def claim_detail(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    c = _claim_row(company_id, id)
    _assert_can_see(user, c['employee_id'])
    if c['status'] == 'draft' and c['employee_id'] != user.employee_id:
        raise NotFoundError('claim not found')
    emp = employee_of(company_id, c['employee_id'])
    items = rows("""
        SELECT i.id::text, i.expense_date::text AS "expenseDate", i.category_id::text AS "categoryId", ec.name AS "categoryName",
               ec.kind AS "categoryKind", i.description, i.amount::text AS amount, i.km::text AS km,
               i.receipt_file_id::text AS "receiptFileId", f.filename AS "receiptName", f.mime AS "receiptMime",
               i.approved_amount::text AS "approvedAmount", i.adjust_reason AS "adjustReason"
          FROM expense_item i JOIN expense_category ec ON ec.id = i.category_id
          LEFT JOIN stored_file f ON f.id = i.receipt_file_id AND f.deleted_at IS NULL
         WHERE i.claim_id = %s ORDER BY i.sort_order""", [id])
    full = one("""SELECT decision_note AS "decisionNote", submitted_at AS "submittedAt", decided_at AS "decidedAt", paid_at AS "paidAt",
                         created_at AS "createdAt" FROM expense_claim WHERE id = %s""", [id])
    payouts = rows("""
        SELECT p.id::text, p.component_code AS code, p.amount::text, p.status, p.paid_amount::text AS "paidAmount", p.paid_at AS "paidAt",
               pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
          FROM expense_payout p LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
         WHERE p.source_type = 'expense_claim' AND p.source_id = %s ORDER BY p.created_at""", [id])
    # Approvers see the same limit and receipt checks the employee saw when submitting.
    issues: list[dict] = []
    if c['status'] in ('draft', 'pending') and items:
        try:
            with transaction.atomic():
                norm = normalise_items(company_id, [{'expenseDate': i['expenseDate'], 'categoryId': i['categoryId'],
                                                     'description': i['description'], 'amount': i['amount'],
                                                     'km': float(i['km']) if i['km'] else None, 'receiptFileId': i['receiptFileId']}
                                                    for i in items])
                issues = check_claim(company_id, c['employee_id'], norm, id)
        except Exception:  # noqa: BLE001 - the TS route swallowed any error here
            issues = []
    can_decide = _can_act_on(user, c['workflow_instance_id']) if c['status'] == 'pending' and c['workflow_instance_id'] else False
    return {
        'claim': {'id': c['id'], 'title': c['title'], 'status': c['status'], 'totalClaimed': c['total_claimed'],
                  'totalApproved': c['total_approved'], 'paymentMethod': c['payment_method'], **(full or {}), 'employee': _emp_card(emp)},
        'items': items, 'payouts': payouts, 'issues': issues, 'canDecide': can_decide,
    }


def claim_receipt(user: AuthUser, id: str, item_id: str) -> dict:
    """The receipt file of one line; an approver's view is audited."""
    id, item_id = parse_uuid(str(id)), parse_uuid(str(item_id))
    company_id = company_id_of(user)
    c = _claim_row(company_id, id)
    _assert_can_see(user, c['employee_id'])
    i = one('SELECT receipt_file_id::text FROM expense_item WHERE id = %s AND claim_id = %s', [item_id, id])
    file_id = i and i['receipt_file_id']
    if not file_id:
        raise NotFoundError('no receipt for this line')
    if c['employee_id'] != user.employee_id:
        audit('view_receipt', 'expense_item', actor_id=user.id, row_id=item_id)
    return read_file(company_id, file_id)


REVIEW_SCHEMA = Obj({'decision': Enum(('approve', 'reject')), 'reason': Str(trim=True, max=1000, optional=True),
                     'items': Arr(Obj({'id': Uuid(), 'approvedAmount': _amount(), 'reason': Str(trim=True, max=300, optional=True)}),
                                  max=50, optional=True)})


def review_claim(user: AuthUser, id: str, body: dict) -> dict:
    """Approve (optionally reducing line amounts, with a reason) or reject, as the current approver."""
    id = parse_uuid(str(id))
    b = parse(REVIEW_SCHEMA, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        row = one('SELECT status, workflow_instance_id::text FROM expense_claim WHERE id = %s AND company_id = %s FOR UPDATE', [id, company_id])
        if not row:
            raise NotFoundError('claim not found')
        if row['status'] != 'pending' or not row['workflow_instance_id']:
            raise ConflictError(f"claim is {row['status']}")
        if b['decision'] == 'approve' and b.get('items'):
            lines = {x['id']: x for x in rows('SELECT id::text, amount::text, description FROM expense_item WHERE claim_id = %s', [id])}
            for it in b['items']:
                line = lines.get(it['id'])
                if not line:
                    raise BadRequestError('a line does not belong to this claim')
                if D(it['approvedAmount']) > D(line['amount']):
                    raise BadRequestError(f"\"{line['description']}\": the approved amount cannot be more than claimed.")
                reduced = D(it['approvedAmount']) < D(line['amount'])
                if reduced and not it.get('reason'):
                    raise BadRequestError(f"\"{line['description']}\": give a reason for approving less.")
                execute('UPDATE expense_item SET approved_amount = %s, adjust_reason = %s WHERE id = %s',
                        [money(it['approvedAmount']), it.get('reason') if reduced else None, it['id']])
            t = one('SELECT coalesce(sum(coalesce(approved_amount, amount)), 0)::text AS total FROM expense_item WHERE claim_id = %s', [id])
            if not D(t['total']) > 0:
                raise BadRequestError('Nothing would be paid; reject the claim instead.')
        state = act(instance_id=row['workflow_instance_id'], user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


NOTE_SCHEMA = Obj({'note': Str(trim=True, min=3, max=300)})


def pay_claim_outside(user: AuthUser, id: str, body: dict) -> dict:
    """HR paid an approved claim in cash or by transfer outside payroll; payroll will not pay it again."""
    _require_hr(user)
    id = parse_uuid(str(id))
    b = parse(NOTE_SCHEMA, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        c = one('SELECT status FROM expense_claim WHERE id = %s AND company_id = %s FOR UPDATE', [id, company_id])
        if not c:
            raise NotFoundError('claim not found')
        if c['status'] != 'approved':
            raise ConflictError(f"claim is {c['status']}")
        execute("UPDATE expense_claim SET payment_method = 'cash' WHERE id = %s", [id])
        _pay_outside('expense_claim', id)
        execute("""UPDATE expense_claim SET decision_note = concat_ws(' · ', decision_note, %s::text), updated_at = now()
                    WHERE id = %s""", [f"Paid outside payroll: {b['note']}", id])
        audit('pay_outside_payroll', 'expense_claim', actor_id=user.id, row_id=id, after={'note': b['note']})
    return {'id': id, 'status': 'paid'}


# ---------------------------------------------------------------- travel

TRAVEL_SCHEMA = Obj({
    'destination': Str(trim=True, min=1, max=120), 'destinationCategory': Enum(DESTINATIONS), 'purpose': Str(trim=True, min=3, max=1000),
    'fromDate': IsoDate(), 'toDate': IsoDate(), 'mode': Enum(MODES),
    'estimatedCost': _amount(optional=True), 'advanceRequested': _amount(optional=True), 'fxRate': _fx(nullish=True),
})
QUOTE_SCHEMA = Obj({'destinationCategory': Enum(DESTINATIONS), 'fromDate': IsoDate(), 'toDate': IsoDate(), 'fxRate': _fx(nullish=True)})


def travel_quote(user: AuthUser, body: dict) -> dict:
    b = parse(QUOTE_SCHEMA, body)
    return {'quote': quote_tada(company_id_of(user), _self_employee(user), b['destinationCategory'], b['fromDate'], b['toDate'],
                                b.get('fxRate'))}


def request_travel(user: AuthUser, body: dict) -> dict:
    b = parse(TRAVEL_SCHEMA, body)
    company_id = company_id_of(user)
    employee_id = _self_employee(user)
    emp = employee_of(company_id, employee_id)
    if emp['status'] != 'active':
        raise ForbiddenError('only active employees can request travel')
    if b['toDate'] < npt_today():
        raise BadRequestError('This trip has already ended. Ask HR to record it.')
    q = quote_tada(company_id, employee_id, b['destinationCategory'], b['fromDate'], b['toDate'], b.get('fxRate'))
    if one("""SELECT 1 AS x FROM travel_request WHERE employee_id = %s AND status IN ('pending', 'approved', 'settling')
               AND from_date <= %s AND to_date >= %s""", [employee_id, b['toDate'], b['fromDate']]):
        raise ConflictError('You already have a trip on these dates.')
    advance = D(b.get('advanceRequested') or '0')
    ceiling = D(q['daAmount']) + D(q['lodgingLimit']) + D(b.get('estimatedCost') or '0')
    if advance > ceiling:
        raise BadRequestError(f'The advance cannot be more than the expected cost of the trip ({rs(ceiling)}).')
    with transaction.atomic():
        id = one("""INSERT INTO travel_request (company_id, employee_id, destination, destination_category, purpose, from_date, to_date,
                                                mode, estimated_cost, advance_requested, fx_rate, days, nights, da_rate, lodging_rate,
                                                da_amount, lodging_limit, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                 [company_id, employee_id, b['destination'], b['destinationCategory'], b['purpose'], b['fromDate'], b['toDate'], b['mode'],
                  b.get('estimatedCost') or '0', money(advance), q['fxRate'], q['days'], q['nights'], q['daRate'], q['lodgingRate'],
                  q['daAmount'], q['lodgingLimit'], user.id])['id']
        adv = f', advance {rs(advance)}' if advance > 0 else ''
        inst = start_workflow(company_id=company_id, object_type='travel_request', object_id=id, employee_id=employee_id,
                              amount=float(ceiling), title='Travel request to approve',
                              body=f"{emp['name_en']}: {b['destination']}, {bs_range(b['fromDate'], b['toDate'])}{adv}")
        execute('UPDATE travel_request SET workflow_instance_id = %s WHERE id = %s', [inst['id'], id])
        audit('create', 'travel_request', actor_id=user.id, row_id=id, after={**b, 'quote': q})
    return {'id': id, 'status': 'pending', 'quote': q}


def list_trips(user: AuthUser, query: dict | None = None) -> dict:
    q = parse(Obj({'scope': Enum(SCOPES, optional=True), 'status': Enum(TRIP_STATUSES, optional=True)}),
              {k: v for k, v in (query or {}).items() if v not in (None, '')})
    sql, params = _scope_filter(user, q.get('scope') or _default_scope(user), 't')
    params = [company_id_of(user), *params]
    extra = ''
    if q.get('status'):
        params.append(q['status'])
        extra = ' AND t.status = %s'
    trips = rows(f"""
        SELECT t.id::text, t.destination, t.destination_category AS "destinationCategory", t.purpose, t.from_date::text AS "fromDate",
               t.to_date::text AS "toDate", t.mode, t.days, t.da_amount::text AS "daAmount", t.lodging_limit::text AS "lodgingLimit",
               t.estimated_cost::text AS "estimatedCost", t.advance_requested::text AS "advanceRequested",
               t.advance_approved::text AS "advanceApproved", t.advance_status AS "advanceStatus", t.advance_method AS "advanceMethod",
               t.status, t.created_at AS "createdAt", e.id::text AS "employeeId", e.emp_code AS "empCode", e.name_en AS "employeeName",
               (SELECT jsonb_build_object('id', s.id, 'status', s.status, 'net', s.net::text) FROM travel_settlement s
                 WHERE s.travel_request_id = t.id ORDER BY s.created_at DESC LIMIT 1) AS settlement
          FROM travel_request t JOIN employee e ON e.id = t.employee_id
         WHERE t.company_id = %s AND {sql}{extra} ORDER BY t.from_date DESC LIMIT 500""", params)
    return {'trips': trips}


def _travel_row(company_id: str, id: str, lock: bool = False) -> dict:
    r = one(f"""SELECT id::text, employee_id::text, status, destination, from_date::text, to_date::text, advance_approved::text,
                       advance_status, da_rate::text, lodging_rate::text, fx_rate::text, workflow_instance_id::text,
                       advance_requested::text
                  FROM travel_request WHERE id = %s AND company_id = %s{' FOR UPDATE' if lock else ''}""", [id, company_id])
    if not r:
        raise NotFoundError('travel request not found')
    return r


def trip_detail(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    t = _travel_row(company_id, id)
    _assert_can_see(user, t['employee_id'])
    emp = employee_of(company_id, t['employee_id'])
    trip = one("""
        SELECT id::text, destination, destination_category AS "destinationCategory", purpose, from_date::text AS "fromDate",
               to_date::text AS "toDate", mode, estimated_cost::text AS "estimatedCost", advance_requested::text AS "advanceRequested",
               advance_approved::text AS "advanceApproved", advance_status AS "advanceStatus", advance_method AS "advanceMethod",
               fx_rate::text AS "fxRate", days, nights, da_rate::text AS "daRate", lodging_rate::text AS "lodgingRate",
               da_amount::text AS "daAmount", lodging_limit::text AS "lodgingLimit", status, decision_note AS "decisionNote",
               decided_at AS "decidedAt", created_at AS "createdAt"
          FROM travel_request WHERE id = %s""", [id])
    settlements = rows("""
        SELECT s.id::text, s.status, s.actual_from::text AS "actualFrom", s.actual_to::text AS "actualTo", s.days, s.nights,
               s.da_amount::text AS "daAmount", s.lodging_claimed::text AS "lodgingClaimed", s.lodging_allowed::text AS "lodgingAllowed",
               s.other_bills::text AS "otherBills", s.entitlement::text, s.advance::text, s.net::text, s.note,
               s.decision_note AS "decisionNote", s.created_at AS "createdAt", s.workflow_instance_id::text AS "workflowInstanceId",
               coalesce((SELECT jsonb_agg(jsonb_build_object('id', b.id, 'kind', b.kind, 'billDate', b.bill_date::text,
                                 'description', b.description, 'amount', b.amount::text, 'fileId', b.file_id, 'fileName', f.filename)
                                 ORDER BY b.bill_date)
                           FROM travel_bill b LEFT JOIN stored_file f ON f.id = b.file_id WHERE b.settlement_id = s.id), '[]') AS bills
          FROM travel_settlement s WHERE s.travel_request_id = %s ORDER BY s.created_at DESC""", [id])
    payouts = rows("""
        SELECT p.id::text, p.source_type AS "sourceType", p.component_code AS code, p.amount::text, p.status,
               p.paid_amount::text AS "paidAmount", p.paid_at AS "paidAt", pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
          FROM expense_payout p LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id LEFT JOIN payroll_period pp ON pp.id = pr.period_id
         WHERE (p.source_type = 'travel_advance' AND p.source_id = %s)
            OR (p.source_type = 'travel_settlement' AND p.source_id IN (SELECT id FROM travel_settlement WHERE travel_request_id = %s))
         ORDER BY p.created_at""", [id, id])
    pending = next((s for s in settlements if s['status'] == 'pending'), None)
    return {
        'trip': {**trip, 'destinationLabel': DESTINATION_LABEL.get(trip['destinationCategory']), 'employee': _emp_card(emp)},
        'settlements': settlements, 'payouts': payouts,
        'canDecide': _can_act_on(user, t['workflow_instance_id']) if t['status'] == 'pending' and t['workflow_instance_id'] else False,
        'canDecideSettlement': _can_act_on(user, pending['workflowInstanceId']) if pending and pending['workflowInstanceId'] else False,
        'canSettle': t['employee_id'] == user.employee_id and t['status'] == 'approved' and t['from_date'] <= npt_today(),
    }


def cancel_trip(user: AuthUser, id: str) -> dict:
    id = parse_uuid(str(id))
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _travel_row(company_id, id, True)
        if t['employee_id'] != user.employee_id and not has_role(user, 'hr'):
            raise NotFoundError('travel request not found')
        if t['status'] not in ('pending', 'approved'):
            raise ConflictError(f"a {t['status']} trip cannot be cancelled")
        if t['advance_status'] == 'paid':
            raise ConflictError('The advance has been paid; submit a settlement instead so it can be recovered.')
        if one("""SELECT 1 AS x FROM expense_payout WHERE source_type = 'travel_advance' AND source_id = %s AND status = 'pending'
                   AND payroll_run_id IS NOT NULL""", [id]):
            raise ConflictError('The advance is already in a pay run being prepared; submit a settlement instead.')
        execute("UPDATE expense_payout SET status = 'cancelled' WHERE source_type = 'travel_advance' AND source_id = %s AND status = 'pending'", [id])
        execute("UPDATE travel_request SET status = 'cancelled', advance_status = 'none' WHERE id = %s", [id])
        cancel_workflow('travel_request', id)
        audit('cancel', 'travel_request', actor_id=user.id, row_id=id)
    return {'id': id, 'status': 'cancelled'}


def review_trip(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = parse(Obj({'decision': Enum(('approve', 'reject')), 'reason': Str(trim=True, max=1000, optional=True),
                   'advanceApproved': _amount(optional=True)}), body)
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _travel_row(company_id, id, True)
        if t['status'] != 'pending' or not t['workflow_instance_id']:
            raise ConflictError(f"travel request is {t['status']}")
        if b['decision'] == 'approve' and 'advanceApproved' in b:
            if D(b['advanceApproved']) > D(t['advance_requested']):
                raise BadRequestError('The approved advance cannot be more than requested.')
            execute('UPDATE travel_request SET advance_approved = %s WHERE id = %s', [money(b['advanceApproved']), id])
        state = act(instance_id=t['workflow_instance_id'], user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


def pay_advance_outside(user: AuthUser, id: str, body: dict) -> dict:
    _require_hr(user)
    id = parse_uuid(str(id))
    b = parse(NOTE_SCHEMA, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _travel_row(company_id, id, True)
        if t['advance_status'] != 'pending':
            raise ConflictError('there is no advance waiting to be paid')
        _pay_outside('travel_advance', id)
        execute("UPDATE travel_request SET advance_status = 'paid', advance_method = 'cash' WHERE id = %s", [id])
        audit('pay_outside_payroll', 'travel_request', actor_id=user.id, row_id=id, after={'note': b['note']})
    return {'id': id, 'advanceStatus': 'paid'}


BILL_SCHEMA = Obj({'kind': Enum(BILL_KINDS), 'billDate': IsoDate(), 'description': Str(trim=True, min=1, max=300), 'amount': _amount(),
                   'fileId': Uuid(nullish=True)})
SETTLE_SCHEMA = Obj({'actualFrom': IsoDate(), 'actualTo': IsoDate(), 'bills': Arr(BILL_SCHEMA, max=60),
                     'note': Str(trim=True, max=1000, optional=True)})


def settlement_figures(trip: dict, actual_from: str, actual_to: str, bills: list[dict]) -> dict:
    """Daily allowance for each day travelled, lodging up to the nightly limit, transport and other bills as spent, less the advance.
    `trip` has da_rate, lodging_rate, fx_rate, advance_status, advance_approved."""
    days = _days_between(actual_from, actual_to) + 1
    nights = days - 1
    fx = D(trip['fx_rate'] if trip.get('fx_rate') is not None else 1)
    da = D(trip['da_rate']) * fx * days
    lodging_claimed = sum((D(x['amount']) for x in bills if x['kind'] == 'lodging'), Decimal(0))
    lodging_allowed = min(lodging_claimed, D(trip['lodging_rate']) * fx * max(nights, 0))
    other = sum((D(x['amount']) for x in bills if x['kind'] != 'lodging'), Decimal(0))
    entitlement = da + lodging_allowed + other
    advance = D(trip.get('advance_approved') or 0) if trip['advance_status'] in ('pending', 'paid') else Decimal(0)
    return {'days': days, 'nights': nights, 'da': da, 'lodgingClaimed': lodging_claimed, 'lodgingAllowed': lodging_allowed,
            'other': other, 'entitlement': entitlement, 'advance': advance, 'net': entitlement - advance}


def settle_trip(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = parse(SETTLE_SCHEMA, body)
    company_id = company_id_of(user)
    with transaction.atomic():
        t = _travel_row(company_id, id, True)
        if t['employee_id'] != user.employee_id:
            raise NotFoundError('travel request not found')
        if t['status'] != 'approved':
            raise ConflictError('a settlement is already waiting for approval' if t['status'] == 'settling'
                                else f"a {t['status']} trip cannot be settled")
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
        sid = one("""INSERT INTO travel_settlement (company_id, travel_request_id, employee_id, actual_from, actual_to, days, nights,
                                                    da_amount, lodging_claimed, lodging_allowed, other_bills, entitlement, advance, net,
                                                    note, created_by)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id::text""",
                  [company_id, id, t['employee_id'], b['actualFrom'], b['actualTo'], f['days'], f['nights'], money(f['da']),
                   money(f['lodgingClaimed']), money(f['lodgingAllowed']), money(f['other']), money(f['entitlement']),
                   money(f['advance']), money(f['net']), b.get('note') or None, user.id])['id']
        for x in b['bills']:
            if x.get('fileId'):
                attach_file(company_id=company_id, file_id=x['fileId'], user_id=user.id, purpose=['travel_bill', 'expense_receipt'],
                            attached_to=f'travel_settlement:{sid}')
            execute('INSERT INTO travel_bill (settlement_id, kind, bill_date, description, amount, file_id) VALUES (%s, %s, %s, %s, %s, %s)',
                    [sid, x['kind'], x['billDate'], x['description'], money(x['amount']), x.get('fileId')])
        emp = employee_of(company_id, t['employee_id'])
        net = f['net']
        inst = start_workflow(company_id=company_id, object_type='travel_settlement', object_id=sid, employee_id=t['employee_id'],
                              amount=float(f['entitlement']), title='Travel settlement to approve',
                              body=f"{emp['name_en']}: {t['destination']}, " + (f'pay {rs(net)}' if net >= 0 else f'recover {rs(-net)}'))
        execute('UPDATE travel_settlement SET workflow_instance_id = %s WHERE id = %s', [inst['id'], sid])
        execute("UPDATE travel_request SET status = 'settling' WHERE id = %s", [id])
        audit('create', 'travel_settlement', actor_id=user.id, row_id=sid,
              after={'entitlement': money(f['entitlement']), 'advance': money(f['advance']), 'net': money(net)})
    return {'id': sid, 'daAmount': money(f['da']), 'lodgingAllowed': money(f['lodgingAllowed']), 'otherBills': money(f['other']),
            'entitlement': money(f['entitlement']), 'advance': money(f['advance']), 'net': money(net)}


def review_settlement(user: AuthUser, id: str, body: dict) -> dict:
    id = parse_uuid(str(id))
    b = parse(Obj({'decision': Enum(('approve', 'reject')), 'reason': Str(trim=True, max=1000, optional=True)}), body)
    company_id = company_id_of(user)
    with transaction.atomic():
        row = one('SELECT status, workflow_instance_id::text FROM travel_settlement WHERE id = %s AND company_id = %s FOR UPDATE',
                  [id, company_id])
        if not row:
            raise NotFoundError('settlement not found')
        if row['status'] != 'pending' or not row['workflow_instance_id']:
            raise ConflictError(f"settlement is {row['status']}")
        state = act(instance_id=row['workflow_instance_id'], user=user, decision=b['decision'], reason=b.get('reason'))
    return {'state': state}


def bill_file(user: AuthUser, id: str, bill_id: str) -> dict:
    id, bill_id = parse_uuid(str(id)), parse_uuid(str(bill_id))
    company_id = company_id_of(user)
    row = one("""SELECT s.employee_id::text, b.file_id::text FROM travel_settlement s JOIN travel_bill b ON b.settlement_id = s.id
                  WHERE s.id = %s AND s.company_id = %s AND b.id = %s""", [id, company_id, bill_id])
    if not row:
        raise NotFoundError('bill not found')
    _assert_can_see(user, row['employee_id'])
    if not row['file_id']:
        raise NotFoundError('no file for this bill')
    if row['employee_id'] != user.employee_id:
        audit('view_receipt', 'travel_bill', actor_id=user.id, row_id=bill_id)
    return read_file(company_id, row['file_id'])


# ---------------------------------------------------------------- reports (HR)

GROUP_BY = ('category', 'department', 'month', 'employee')
GROUP_LABEL = {'category': 'Category', 'department': 'Department', 'month': 'Month', 'employee': 'Employee'}


def report_spend(user: AuthUser, query: dict) -> dict:
    """Approved claims and settled trips grouped by category, department, BS month or employee."""
    _require_hr(user)
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate(), 'groupBy': Enum(GROUP_BY, default='category')}),
              {k: v for k, v in (query or {}).items() if k in ('from', 'to', 'groupBy') and v not in (None, '')})
    if q['to'] < q['from']:
        raise BadRequestError('the end date must be after the start date')
    company_id = company_id_of(user)
    lines = rows("""
        SELECT i.expense_date::text, ec.name AS category, ou.name_en AS department, e.emp_code, e.name_en,
               coalesce(i.approved_amount, i.amount)::text AS amount
          FROM expense_item i JOIN expense_claim c ON c.id = i.claim_id JOIN expense_category ec ON ec.id = i.category_id
          JOIN employee e ON e.id = c.employee_id
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> i.expense_date LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
         WHERE c.company_id = %s AND c.status IN ('approved', 'paid') AND i.expense_date BETWEEN %s AND %s
        UNION ALL
        SELECT s.actual_to::text, 'Travel', ou.name_en, e.emp_code, e.name_en, s.entitlement::text
          FROM travel_settlement s JOIN employee e ON e.id = s.employee_id
          LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> s.actual_to LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
         WHERE s.company_id = %s AND s.status = 'approved' AND s.actual_to BETWEEN %s AND %s""",
                 [company_id, q['from'], q['to'], company_id, q['from'], q['to']])
    gb = q['groupBy']
    groups: dict[str, dict] = {}
    for x in lines:
        if gb == 'category':
            key = x['category']
        elif gb == 'department':
            key = x['department'] or 'No department'
        elif gb == 'employee':
            key = f"{x['emp_code']} {x['name_en']}"
        else:
            key = bs_label(x['expense_date']).split(' ', 1)[-1]
        g = groups.setdefault(key, {'key': key, 'amount': Decimal(0), 'count': 0, 'sort': x['expense_date']})
        g['amount'] += D(x['amount'])
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
    out = rows("""SELECT t.id::text, e.emp_code AS "empCode", e.name_en AS "employeeName", t.destination, t.from_date::text AS "fromDate",
                         t.to_date::text AS "toDate", t.advance_approved::text AS advance, t.advance_status AS "advanceStatus", t.status
                    FROM travel_request t JOIN employee e ON e.id = t.employee_id
                   WHERE t.company_id = %s AND t.advance_status IN ('pending', 'paid') AND t.status IN ('approved', 'settling')
                   ORDER BY t.to_date""", [company_id_of(user)])
    for x in out:
        x['trip'] = bs_range(x['fromDate'], x['toDate'])
    return {'filename': 'outstanding-travel-advances', 'columns': [
        {'key': 'empCode', 'header': 'Code'}, {'key': 'employeeName', 'header': 'Employee'}, {'key': 'destination', 'header': 'Destination'},
        {'key': 'trip', 'header': 'Trip'}, {'key': 'advance', 'header': 'Advance (NPR)'}, {'key': 'advanceStatus', 'header': 'Advance'},
        {'key': 'status', 'header': 'Trip status'}], 'rows': out}


def report_pending_payment(user: AuthUser) -> dict:
    _require_hr(user)
    out = rows("""SELECT p.id::text, e.emp_code AS "empCode", e.name_en AS "employeeName", p.source_type AS "sourceType",
                         p.source_id::text AS "sourceId", p.component_code AS code, p.amount::text, p.note, p.created_at AS "createdAt",
                         pp.bs_year AS "bsYear", pp.bs_month AS "bsMonth"
                    FROM expense_payout p JOIN employee e ON e.id = p.employee_id LEFT JOIN payroll_run pr ON pr.id = p.payroll_run_id
                    LEFT JOIN payroll_period pp ON pp.id = pr.period_id
                   WHERE p.company_id = %s AND p.status = 'pending' ORDER BY e.emp_code, p.created_at""", [company_id_of(user)])
    for x in out:
        x['kind'] = 'Recover' if x['code'] == 'EXP_RECOVERY' else 'Pay'
        x['inRun'] = f"{x['bsYear']}-{x['bsMonth']:02d}" if x['bsYear'] else 'Next pay run'
    return {'filename': 'expenses-pending-payment', 'columns': [
        {'key': 'empCode', 'header': 'Code'}, {'key': 'employeeName', 'header': 'Employee'}, {'key': 'note', 'header': 'For'},
        {'key': 'kind', 'header': 'Pay or recover'}, {'key': 'amount', 'header': 'Amount (NPR)'}, {'key': 'inRun', 'header': 'Pay run'}],
        'rows': out}

