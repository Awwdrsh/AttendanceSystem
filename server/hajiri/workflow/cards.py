"""
How the Approvals inbox writes each request (ApprovalsPage.tsx Summary, expenses/approvalSummary.tsx,
people/ProfileChangeSummary.tsx): a type badge, one readable line of what is asked and muted lines of context.

Other slices can add or override a type: register_card('my_type', fn) with fn(item) -> {'main': str | SafeString,
'extra': [str | SafeString | (text, tone)]}; `item` is an inbox_for() entry (its 'summary' comes from the summariser).
"""
from __future__ import annotations

from typing import Callable

from django.utils.html import format_html

from hajiri.web.format import bs_month_label, fmt_date_short, fmt_money, fmt_time, minutes_to_hm

TYPE_LABEL = {
    'leave_request': 'Leave', 'regularisation_request': 'Attendance correction', 'ot_request': 'Overtime',
    'payroll_run': 'Payroll run', 'timesheet': 'Timesheet', 'shift_swap': 'Shift swap', 'letter_request': 'Letter request',
    'probation_review': 'Probation', 'resignation': 'Resignation', 'job_requisition': 'Hiring request', 'job_offer': 'Job offer',
    'profile_change': 'Profile change', 'training_nomination': 'Training', 'tax_declaration': 'Tax savings',
    'expense_claim': 'Expense claim', 'travel_request': 'Travel', 'travel_settlement': 'Travel settlement',
}
# Mantine colours of the TS badges mapped onto the hj tones.
TYPE_TONE = {
    'leave_request': 'info', 'regularisation_request': 'warn', 'ot_request': 'accent', 'payroll_run': 'accent',
    'timesheet': 'ok', 'shift_swap': 'info', 'letter_request': 'info', 'probation_review': 'accent', 'resignation': 'bad',
    'job_requisition': 'accent', 'job_offer': 'ok', 'profile_change': 'info', 'training_nomination': 'ok',
    'tax_declaration': 'warn', 'expense_claim': 'ok', 'travel_request': 'info', 'travel_settlement': 'info',
}
REG_KIND = {'wfh': 'Working from home', 'missed_punch': 'Missed punch', 'on_duty': 'On duty elsewhere', 'half_day': 'Half day',
            'shift_change': 'Shift change'}

_cards: dict[str, Callable[[dict], dict]] = {}


def register_card(object_type: str, fn: Callable[[dict], dict]) -> None:
    _cards[object_type] = fn


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fmt_num(v) -> str:
    """String(Number) as JS prints it: 1 not 1.0."""
    f = _num(v)
    return str(int(f)) if f.is_integer() else str(f)


def date_range(f, t) -> str:
    fs, ts = str(f) if f is not None else '', str(t) if t is not None else ''
    return f'{fmt_date_short(fs)} to {fmt_date_short(ts)}' if ts and ts != 'None' and ts != fs else fmt_date_short(fs)


def _q(v) -> str:
    return f'“{v}”'


def _m(v) -> str:
    n = _num(v)
    return fmt_money(str(v if v is not None else 0), decimals=0 if n % 1 == 0 else 2)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def card(item: dict) -> dict:
    """{'label', 'tone', 'main', 'extra': [(text_or_html, tone|None)]} for one inbox item."""
    t = item['objectType']
    s = item.get('summary') or {}
    extra: list = []
    main = None
    if t in _cards:
        r = _cards[t](item)
        main, extra = r.get('main'), list(r.get('extra') or [])
    elif t == 'leave_request':
        days = _num(s.get('days'))
        name = s.get('leaveTypeName') or s.get('leaveType')
        if s.get('fromTime'):
            main = format_html('{}, {}, {}–{} <span class="hj-muted">({} h, {} of a day)</span>', name,
                               date_range(s.get('from'), s.get('to')), s['fromTime'], s.get('toTime'),
                               _fmt_num(s.get('hours')), _fmt_num(s.get('days')))
        else:
            main = format_html('{}, {}{} <span class="hj-muted">({} {})</span>', name, date_range(s.get('from'), s.get('to')),
                               ' (half day)' if s.get('halfDay') else '', _fmt_num(s.get('days')), 'day' if days == 1 else 'days')
        extra.append(f"Balance {_num(s.get('balanceNow')):.1f} to {_num(s.get('balanceAfter')):.1f}")
        if s.get('teamOverlap'):
            extra.append(('Also off: ' + ', '.join(s['teamOverlap']), 'warn'))
        if s.get('reason'):
            extra.append(_q(s['reason']))
    elif t == 'regularisation_request':
        kind = REG_KIND.get(str(s.get('kind')), str(s.get('kind') or '').replace('_', ' '))
        punch = ''
        if s.get('punch_time'):
            d = 'clock-in' if str(s.get('punch_direction') or '').lower() == 'in' else 'clock-out'
            punch = f", {d} at {fmt_time(s['punch_time'])}"
        main = f"{_cap(kind)} on {fmt_date_short(s.get('local_date'))}{punch}"
        if s.get('reason'):
            extra.append(_q(s['reason']))
    elif t == 'ot_request':
        main = f"{minutes_to_hm(s.get('requested_min') or 0)} overtime on {fmt_date_short(s.get('local_date'))}"
        if s.get('reason'):
            extra.append(_q(s['reason']))
    elif t == 'payroll_run':
        try:
            period = bs_month_label(int(s.get('bs_year')), int(s.get('bs_month')))
        except (TypeError, ValueError):
            period = ''
        main = f"{_cap(str(s.get('run_type') or ''))} run for {period}"
        extra += [f"{s.get('payslips')} payslips", f"Net {fmt_money(str(s.get('total_net') or 0))}"]
    elif t == 'timesheet':
        main = f"{minutes_to_hm(s.get('totalMin') or 0)} logged, {date_range(s.get('weekStart'), s.get('weekEnd'))}"
        extra.append(f"Attended {minutes_to_hm(s.get('attendanceMin') or 0)}")
        if s.get('projects'):
            extra.append(' · '.join(f"{p['name']} {minutes_to_hm(p['min'])}" for p in s['projects']))
        if s.get('note'):
            extra.append(_q(s['note']))
    elif t == 'shift_swap':
        main = format_html('Swap shifts with {} on {} <span class="hj-muted">({} ⇄ {})</span>', s.get('colleague_name'),
                           fmt_date_short(s.get('local_date')), s.get('requester_shift_code') or 'Off',
                           s.get('colleague_shift_code') or 'Off')
        if s.get('reason'):
            extra.append(_q(s['reason']))
        if s.get('colleague_note'):
            extra.append(f"{s.get('colleague_name')}: {_q(s['colleague_note'])}")
    elif t == 'letter_request':
        main = f"{s.get('letter') or 'Letter'} requested"
        if s.get('purpose'):
            extra.append(f"For {s['purpose']}")
        if s.get('addressed_to'):
            extra.append(f"To {s['addressed_to']}")
    elif t == 'job_requisition':
        main = f"Hire {s.get('headcount') or 1} × {s.get('title') or ''}" + (f" for {s['department']}" if s.get('department') else '')
        if s.get('salary_min') or s.get('salary_max'):
            extra.append('Budget NPR ' + ' – '.join(fmt_money(str(x), currency=False, decimals=0)
                                                    for x in (s.get('salary_min'), s.get('salary_max')) if x))
        if s.get('justification'):
            extra.append(_q(s['justification']))
    elif t == 'job_offer':
        main = f"Offer to {s.get('candidate') or ''} as {s.get('designation') or ''}"
        extra += [f"NPR {fmt_money(str(s.get('salary') or 0), currency=False)} a month", f"Joins {fmt_date_short(s.get('join_date'))}"]
    elif t == 'profile_change':
        main = f"{s.get('sectionLabel') or 'Profile'}{' (HR edit, second approval)' if s.get('byHr') else ''}" \
               f"{', proof attached' if s.get('hasProof') else ''}"
        changes = s.get('changes') or []
        for c in changes[:4]:
            extra.append(f"{c.get('label')}: {c.get('from') if c.get('from') is not None else '(empty)'} → "
                         f"{c.get('to') if c.get('to') is not None else '(cleared)'}")
        if len(changes) > 4:
            extra.append(f'and {len(changes) - 4} more')
        if s.get('reason'):
            extra.append(_q(s['reason']))
    elif t == 'training_nomination':
        main = f"Join {s.get('course_title')}" + (f" on {fmt_date_short(str(s['starts_at'])[:10])}" if s.get('starts_at') else '')
        if _num(s.get('cost')) > 0:
            extra.append(f"Cost {fmt_money(str(s['cost']))}")
        if s.get('reason'):
            extra.append(_q(s['reason']))
    elif t == 'tax_declaration':
        fy = int(_num(s.get('fiscalYearStart')))
        parts = [('Life insurance', s.get('lifePremium')), ('Health insurance', s.get('healthPremium')),
                 ('CIT a month', s.get('citMonthly')), ('Other retirement', s.get('otherRetirementAnnual')),
                 ('Medical', s.get('medicalCost')), ('Donations', s.get('donation'))]
        declared = ', '.join(f'{k} {fmt_money(str(v))}' for k, v in parts if _num(v) > 0) or 'nothing declared'
        main = f'Tax savings for FY {fy}/{str(fy + 1)[-2:]}: {declared}'
        extra.append(f"{int(_num(s.get('proofs')))} proof file(s)")
        if s.get('note'):
            extra.append(_q(s['note']))
        extra.append('Check the receipts under Payroll, Tax declarations')
    elif t == 'expense_claim':
        n = int(_num(s.get('items')))
        main = format_html('{} <span class="hj-muted">({})</span>', s.get('title'), _m(s.get('amount')))
        extra += [f"{s.get('items')} {'item' if n == 1 else 'items'}" + (f": {s['categories']}" if s.get('categories') else ''),
                  f"{s.get('receipts')} with receipts",
                  format_html('<a href="/expenses?open={}">Review lines</a>', item['objectId'])]
    elif t == 'travel_request':
        d = _num(s.get('days'))
        main = format_html('Trip to {}, {} <span class="hj-muted">({} {})</span>', s.get('destination'),
                           date_range(s.get('from'), s.get('to')), _fmt_num(s.get('days')), 'day' if d == 1 else 'days')
        extra.append(f"Allowance {_m(s.get('dailyAllowance'))}")
        if _num(s.get('advance')) > 0:
            extra.append((f"Advance {_m(s.get('advance'))}", 'warn'))
        if s.get('purpose'):
            extra.append(_q(s['purpose']))
        extra.append(format_html('<a href="/expenses/travel?open={}">Details</a>', item['objectId']))
    elif t == 'travel_settlement':
        net = _num(s.get('net'))
        main = f"Settle trip to {s.get('destination')}: " + (f'pay {_m(net)}' if net >= 0 else f'recover {_m(-net)}')
        b = int(_num(s.get('bills')))
        extra += [f"Entitled {_m(s.get('entitlement'))}, advance {_m(s.get('advance'))}", f"{s.get('bills')} {'bill' if b == 1 else 'bills'}"]
    else:
        import json
        main = format_html('<span class="hj-muted">{}</span>', json.dumps(s, default=str))
    if item.get('routing'):
        extra.append(('. '.join(item['routing']), 'warn'))
    out_extra = [x if isinstance(x, tuple) else (x, None) for x in extra]
    wfh = t == 'regularisation_request' and s.get('kind') == 'wfh'
    return {'label': 'Work from home' if wfh else TYPE_LABEL.get(t, t), 'tone': 'info' if wfh else TYPE_TONE.get(t, 'idle'),
            'main': main, 'extra': out_extra}
