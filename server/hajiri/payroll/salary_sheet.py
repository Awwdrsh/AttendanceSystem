"""
Salary sheet for one pay run (port of payroll/salary-sheet.ts): one row per employee with every earning, deduction and
employer contribution in its own column, plus totals, as data, CSV for Excel and a printable A4 page. It is the HR
record of what each person is owed; nothing here talks to a bank or an accounting system. Also the SSF sheet
(exports.ts).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import localcontext
from functools import cmp_to_key

from engines.payroll import amount_in_words_en, format_npr
from engines.payroll.numeric import ENGINE_CONTEXT, D, js_str, to_fixed

from hajiri.core.bsdate import bs_label, bs_month_name
from hajiri.web.csv import csv_cell

from ._db import q, q1
from .compliance import esc

RUN_TYPE = {'regular': 'Regular', 'supplementary': 'Supplementary', 'arrears': 'Arrears', 'off_cycle': 'Off-cycle',
            'final_settlement': 'Final settlement', 'bonus': 'Bonus'}
TYPES = ('earning', 'deduction', 'employer_contribution')


def _iso_ms(d: datetime | None) -> str | None:
    if d is None:
        return None
    d = d.astimezone(timezone.utc)
    return d.strftime('%Y-%m-%dT%H:%M:%S.') + f'{d.microsecond // 1000:03d}Z'


def _locale_cmp(a: str, b: str) -> int:
    from engines.payroll.compute import _collation_key
    ka, kb = _collation_key(a), _collation_key(b)
    return (ka > kb) - (ka < kb)


def salary_sheet(company_id: str, run_id: str) -> dict | None:
    h = q1("""SELECT r.id, r.run_type, r.status, r.locked_at, r.approved_at, c.name_en AS company, p.bs_year, p.bs_month, p.start_ad::text, p.end_ad::text, p.days,
                     mk.name AS maker, ck.name AS checker
                FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id JOIN company c ON c.id = r.company_id
                LEFT JOIN app_user mk ON mk.id = r.maker_id LEFT JOIN app_user ck ON ck.id = r.checker_id
               WHERE r.id = $1 AND r.company_id = $2""", [str(run_id), str(company_id)])
    if not h:
        return None
    slips = q("""
        SELECT ps.id, ps.employee_id, e.emp_code, e.name_en, e.designation,
               (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> $2::date LIMIT 1) AS department,
               ps.paid_days::text, ps.lop_days::text, ps.ot_hours::text, ps.gross::text, ps.total_deductions::text, ps.net::text, ps.employer_statutory::text,
               e.bank_code, e.bank_branch, e.bank_account_last4,
               CASE WHEN EXISTS (SELECT 1 FROM payroll_hold h WHERE h.run_id = ps.run_id AND h.employee_id = ps.employee_id AND h.status <> 'cancelled')
                    THEN ps.flags || '["PayHeld"]'::jsonb ELSE ps.flags END AS flags
          FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 ORDER BY e.emp_code""", [str(run_id), h['end_ad']])
    lines = q("""SELECT l.payslip_id, l.component_code, l.type, l.name_en, l.amount::text, l.sort_order FROM payslip_line l JOIN payslip ps ON ps.id = l.payslip_id
                  WHERE ps.run_id = $1 AND l.type <> 'statistical' ORDER BY l.sort_order, l.component_code""", [str(run_id)])
    cols: dict[str, dict[str, dict]] = {t: {} for t in TYPES}
    byslip: dict[str, dict[str, dict[str, str]]] = {}
    rounding: dict[str, str] = {}
    with localcontext(ENGINE_CONTEXT):
        for ln in lines:
            sid = str(ln['payslip_id'])
            if ln['component_code'] == 'ROUNDING':
                rounding[sid] = to_fixed(-D(ln['amount']) if ln['type'] == 'deduction' else D(ln['amount']), 2)
                continue
            seen = cols[ln['type']].get(ln['component_code'])
            if not seen or ln['sort_order'] < seen['order']:
                cols[ln['type']][ln['component_code']] = {'name': ln['name_en'], 'order': ln['sort_order']}
            b = byslip.setdefault(sid, {t: {} for t in TYPES})
            b[ln['type']][ln['component_code']] = to_fixed(D(b[ln['type']].get(ln['component_code'], 0)) + D(ln['amount']), 2)

        def lst(t):
            items = list(cols[t].items())
            items.sort(key=cmp_to_key(lambda a, b: (a[1]['order'] - b[1]['order']) or _locale_cmp(a[0], b[0])))
            return [{'code': c, 'name': v['name']} for c, v in items]
        columns = {'earnings': lst('earning'), 'deductions': lst('deduction'), 'employer': lst('employer_contribution'), 'rounding': bool(rounding)}

        def sum_of(rec):
            t = D(0)
            for v in rec.values():
                t = t + D(v)
            return t
        out_rows = []
        for i, sl in enumerate(slips):
            sid = str(sl['id'])
            b = byslip.get(sid) or {t: {} for t in TYPES}
            employer_total = sum_of(b['employer_contribution']) if b['employer_contribution'] else D(sl['employer_statutory'])
            out_rows.append({
                'sn': i + 1, 'employeeId': str(sl['employee_id']), 'empCode': sl['emp_code'], 'name': sl['name_en'], 'designation': sl['designation'],
                'department': sl['department'], 'paidDays': js_str(D(sl['paid_days'])), 'lopDays': js_str(D(sl['lop_days'])),
                'otHours': js_str(D(sl['ot_hours'])), 'earnings': b['earning'], 'gross': to_fixed(D(sl['gross']), 2),
                'deductions': b['deduction'], 'totalDeductions': to_fixed(D(sl['total_deductions']), 2), 'rounding': rounding.get(sid, '0.00'),
                'net': to_fixed(D(sl['net']), 2), 'employer': b['employer_contribution'], 'employerTotal': to_fixed(employer_total, 2),
                'costToCompany': to_fixed(D(sl['gross']) + employer_total, 2),
                'bank': ', '.join(x for x in (sl['bank_code'], sl['bank_branch']) if x) or None,
                'bankAccount': f"****{sl['bank_account_last4']}" if sl['bank_account_last4'] else None,
                'flags': sl['flags'] if isinstance(sl['flags'], list) else [],
            })

        def total(pick):
            t = D(0)
            for r in out_rows:
                v = pick(r)
                t = t + D(v if v is not None else 0)
            return t

        def totals_by(cs, k):
            return {c['code']: to_fixed(total(lambda r, c=c: r[k].get(c['code'])), 2) for c in cs}
        totals = {
            'paidDays': js_str(total(lambda r: r['paidDays'])), 'lopDays': js_str(total(lambda r: r['lopDays'])),
            'otHours': js_str(total(lambda r: r['otHours'])),
            'earnings': totals_by(columns['earnings'], 'earnings'), 'gross': to_fixed(total(lambda r: r['gross']), 2),
            'deductions': totals_by(columns['deductions'], 'deductions'), 'totalDeductions': to_fixed(total(lambda r: r['totalDeductions']), 2),
            'rounding': to_fixed(total(lambda r: r['rounding']), 2), 'net': to_fixed(total(lambda r: r['net']), 2),
            'employer': totals_by(columns['employer'], 'employer'), 'employerTotal': to_fixed(total(lambda r: r['employerTotal']), 2),
            'costToCompany': to_fixed(total(lambda r: r['costToCompany']), 2),
        }
    return {
        'company': h['company'],
        'period': {'label': bs_month_name(h['bs_year'], h['bs_month']), 'bsYear': h['bs_year'], 'bsMonth': h['bs_month'], 'from': h['start_ad'],
                   'to': h['end_ad'], 'days': h['days']},
        'run': {'id': str(h['id']), 'type': h['run_type'], 'status': h['status'], 'final': h['locked_at'] is not None, 'preparedBy': h['maker'],
                'approvedBy': h['checker'], 'approvedAt': _iso_ms(h['approved_at']), 'lockedAt': _iso_ms(h['locked_at'])},
        'columns': columns, 'rows': out_rows, 'totals': totals,
    }


def sheet_filename(s: dict, ext: str = 'csv') -> str:
    return f"salary-sheet-{s['period']['bsYear']}-{s['period']['bsMonth']:02d}{'' if s['run']['final'] else '-draft'}.{ext}"


def salary_sheet_csv(s: dict) -> str:
    """CSV with a byte-order mark so Excel opens it as UTF-8; one header row, one row per employee, a totals row."""
    c = s['columns']
    earn, ded, emp = c['earnings'], c['deductions'], c['employer']
    rnd = c['rounding']
    header = ['S.N.', 'Employee code', 'Name', 'Designation', 'Department', 'Paid days', 'Unpaid days', 'OT hours',
              *[x['name'] for x in earn], 'Gross pay', *[x['name'] for x in ded], 'Total deductions', *(['Rounding'] if rnd else []), 'Net pay',
              *[f"{x['name']} (employer)" for x in emp], 'Employer contributions', 'Cost to company', 'Bank', 'Account']
    body = [[str(r['sn']), r['empCode'], r['name'], r['designation'] or '', r['department'] or '', r['paidDays'], r['lopDays'], r['otHours'],
             *[r['earnings'].get(x['code'], '0.00') for x in earn], r['gross'], *[r['deductions'].get(x['code'], '0.00') for x in ded],
             r['totalDeductions'], *([r['rounding']] if rnd else []), r['net'], *[r['employer'].get(x['code'], '0.00') for x in emp],
             r['employerTotal'], r['costToCompany'], r['bank'] or '', r['bankAccount'] or ''] for r in s['rows']]
    t = s['totals']
    foot = ['', '', f"Total ({len(s['rows'])})", '', '', t['paidDays'], t['lopDays'], t['otHours'], *[t['earnings'][x['code']] for x in earn], t['gross'],
            *[t['deductions'][x['code']] for x in ded], t['totalDeductions'], *([t['rounding']] if rnd else []), t['net'],
            *[t['employer'][x['code']] for x in emp], t['employerTotal'], t['costToCompany'], '', '']
    title = [f"{s['company']} salary sheet, {s['period']['label']} ({bs_label(s['period']['from'])} to {bs_label(s['period']['to'])})"
             f"{'' if s['run']['final'] else ', DRAFT'}"]
    return '﻿' + '\r\n'.join(','.join(csv_cell(v) for v in row) for row in [title, header, *body, foot]) + '\r\n'


SHEET_CSS = """@page{size:A4 landscape;margin:10mm}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:11px;color:#161c26;margin:24px;line-height:1.4}
h1{font-size:18px;margin:0;font-weight:600;letter-spacing:-.01em}.muted{color:#6b707b;font-size:10px}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:12px;border-bottom:2px solid #12773a;margin-bottom:14px}
.right{text-align:right}.right b{font-size:15px}
.draft{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:99px;background:#fff4d6;color:#8a5a00;font-size:10px;font-weight:600;letter-spacing:.04em}
.sum{display:flex;gap:28px;margin:0 0 14px}.sum div span{display:block;color:#6b707b;font-size:10px}.sum div b{font-size:14px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse}th{font-weight:600;color:#3b4250;font-size:10px;text-align:left;padding:6px 5px;border-bottom:1px solid #c9ccd2;background:#f4f6f5;vertical-align:bottom}
td{padding:5px;border-bottom:1px solid #eceef0;vertical-align:top}.amt{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}.c{text-align:center}
.strong{font-weight:600}.net{font-weight:700;color:#12773a}.sig{width:90px}tfoot td{border-top:2px solid #161c26;border-bottom:none;font-weight:600;padding-top:7px}
.words{margin-top:10px;font-size:11px}.signs{display:grid;grid-template-columns:repeat(3,1fr);gap:48px;margin-top:48px}
.signs div{border-top:1px solid #161c26;padding-top:6px}.signs span{display:block;color:#6b707b;font-size:10px}
.foot{margin-top:18px;font-size:9.5px;color:#6b707b}
@media print{body{margin:0}thead{display:table-header-group}tr{page-break-inside:avoid}}"""


def salary_sheet_html(s: dict) -> str:
    """Printable salary sheet (A4 landscape) with sign-off lines for the people who prepare, check and approve it."""
    def n(v):
        return format_npr(v if v is not None else '0', 'latin')
    earn, ded = s['columns']['earnings'], s['columns']['deductions']
    has_rnd = s['columns']['rounding']

    def rnd(v):
        if not has_rnd:
            return ''
        with localcontext(ENGINE_CONTEXT):
            d = D(v)
            txt = '' if d.is_zero() else f"{'+' if d > 0 else '−'}{n(to_fixed(abs(d), 2))}"
        return f'<td class="amt">{txt}</td>'

    def th(label, cls='amt'):
        return f'<th class="{cls}">{esc(label)}</th>'
    head = (f"<tr>{th('S.N.', 'c')}{th('Employee', '')}{th('Paid days')}{th('Unpaid')}{''.join(th(c['name']) for c in earn)}{th('Gross pay', 'amt strong')}"
            f"{''.join(th(c['name']) for c in ded)}{th('Total deductions', 'amt strong')}{th('Rounding') if has_rnd else ''}{th('Net pay', 'amt net')}"
            f"{th('Employer contributions')}{th('Bank', '')}{th('Signature', 'sig')}</tr>")
    body = ''
    for r in s['rows']:
        body += (f"<tr><td class=\"c\">{r['sn']}</td><td><b>{esc(r['name'])}</b><div class=\"muted\">{esc(r['empCode'])}"
                 f"{f' · {esc(r['designation'])}' if r['designation'] else ''}</div></td><td class=\"amt\">{r['paidDays']}</td><td class=\"amt\">{r['lopDays']}</td>"
                 + ''.join(f"<td class=\"amt\">{n(r['earnings'].get(c['code']))}</td>" for c in earn)
                 + f"<td class=\"amt strong\">{n(r['gross'])}</td>"
                 + ''.join(f"<td class=\"amt\">{n(r['deductions'].get(c['code']))}</td>" for c in ded)
                 + f"<td class=\"amt strong\">{n(r['totalDeductions'])}</td>{rnd(r['rounding'])}<td class=\"amt net\">{n(r['net'])}"
                 + ('<div class="muted">On hold</div>' if 'PayHeld' in r['flags'] else '')
                 + f"</td><td class=\"amt\">{n(r['employerTotal'])}</td><td>{esc(r['bank']) or '—'}"
                 + (f"<div class=\"muted\">{esc(r['bankAccount'])}</div>" if r['bankAccount'] else '') + '</td><td class="sig"></td></tr>')
    t = s['totals']
    foot = (f"<tr><td></td><td><b>Total ({len(s['rows'])})</b></td><td class=\"amt\">{t['paidDays']}</td><td class=\"amt\">{t['lopDays']}</td>"
            + ''.join(f"<td class=\"amt\">{n(t['earnings'].get(c['code']))}</td>" for c in earn)
            + f"<td class=\"amt strong\">{n(t['gross'])}</td>" + ''.join(f"<td class=\"amt\">{n(t['deductions'].get(c['code']))}</td>" for c in ded)
            + f"<td class=\"amt strong\">{n(t['totalDeductions'])}</td>{rnd(t['rounding'])}<td class=\"amt net\">{n(t['net'])}</td>"
            f"<td class=\"amt\">{n(t['employerTotal'])}</td><td></td><td></td></tr>")
    kind = RUN_TYPE.get(s['run']['type'], s['run']['type'])
    p = s['period']
    final = s['run']['final']
    if final:
        locked = datetime.strptime(s['run']['lockedAt'], '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) + timedelta(minutes=345)
        foot_note = f'Run locked {esc(bs_label(locked.date().isoformat()))}.'
    else:
        foot_note = 'Draft: figures can still change until the run is locked.'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Salary sheet {esc(p["label"])}</title>\n<style>\n{SHEET_CSS}\n</style></head><body>\n'
            f"<div class=\"hdr\"><div><h1>{esc(s['company'])}</h1><div class=\"muted\">Salary sheet · {esc(kind)} run</div></div>\n"
            f"<div class=\"right\"><div class=\"muted\">Pay period</div><b>{esc(p['label'])}</b>{'' if final else '<span class=\"draft\">DRAFT</span>'}"
            f"<div class=\"muted\">{esc(bs_label(p['from']))} to {esc(bs_label(p['to']))} · {p['days']} days</div></div></div>\n"
            f"<div class=\"sum\"><div><span>Employees</span><b>{len(s['rows'])}</b></div><div><span>Gross pay</span><b>NPR {n(t['gross'])}</b></div>"
            f"<div><span>Deductions</span><b>NPR {n(t['totalDeductions'])}</b></div><div><span>Net pay</span><b>NPR {n(t['net'])}</b></div>"
            f"<div><span>Employer contributions</span><b>NPR {n(t['employerTotal'])}</b></div><div><span>Cost to company</span><b>NPR {n(t['costToCompany'])}</b></div></div>\n"
            f"<table><thead>{head}</thead><tbody>{body or '<tr><td colspan=\"99\" class=\"muted\">No payslips in this run yet. Calculate the run first.</td></tr>'}</tbody><tfoot>{foot}</tfoot></table>\n"
            f"<div class=\"words\">Total net pay in words: <b>{esc(amount_in_words_en(t['net']))}</b></div>\n"
            f"<div class=\"signs\"><div>Prepared by<span>{esc(s['run']['preparedBy']) or '&nbsp;'}</span></div><div>Checked by<span>&nbsp;</span></div>"
            f"<div>Approved by<span>{esc(s['run']['approvedBy']) or '&nbsp;'}</span></div></div>\n"
            f"<div class=\"foot\">Generated by Hajiri. Amounts in NPR. {foot_note} Run {esc(s['run']['id'])}.</div>\n</body></html>")


def ssf_sheet_csv(run_id: str) -> str:
    """SSF monthly contribution sheet (plan §11.5, §11.10): per contributor basic, 11%, 20%, scheme allocation, 31% total."""
    r = q("""SELECT e.emp_code, e.name_en, e.ssf_id,
                    coalesce((SELECT l.amount FROM payslip_line l WHERE l.payslip_id = ps.id AND l.component_code = 'BASIC'), 0)::text AS basic,
                    ps.employee_statutory::text AS ee, ps.employer_statutory::text AS er, ps.inputs_snapshot->'result'->'ssfAllocation' AS alloc
               FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1 AND e.regime = 'ssf' ORDER BY e.emp_code""", [str(run_id)])
    rows = [['emp_code', 'ssf_id', 'name', 'basic', 'employee_11pct', 'employer_20pct', 'medical_1pct', 'accident_1_40pct', 'dependent_0_27pct',
             'old_age_28_33pct', 'total_31pct']]
    with localcontext(ENGINE_CONTEXT):
        for x in r:
            a = x['alloc'] or {}
            rows.append([x['emp_code'], x['ssf_id'] or '', x['name_en'], to_fixed(D(x['basic']), 2), to_fixed(D(x['ee']), 2), to_fixed(D(x['er']), 2),
                         a.get('medical', ''), a.get('accident', ''), a.get('dependent', ''), a.get('old_age', ''), to_fixed(D(x['ee']) + D(x['er']), 2)])
    return '\r\n'.join(','.join(csv_cell(v) for v in row) for row in rows) + '\r\n'
