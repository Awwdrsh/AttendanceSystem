"""
Salary sheet for one pay run (port of payroll/salary-sheet.ts): one row per employee with every earning, deduction and
employer contribution in its own column, plus totals, as data, CSV for Excel and a printable A4 page. It is the HR
record of what each person is owed; nothing here talks to a bank or an accounting system. Also the SSF sheet
(exports.ts).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from decimal import localcontext

from django.db.models import Exists, F, OuterRef
from django.utils.html import escape

from engines.payroll import amount_in_words_en, format_npr
from engines.payroll.numeric import ENGINE_CONTEXT, D, js_str, to_fixed

from hajiri.core.bsdate import bs_label, bs_month_name
from hajiri.core.collation import collate_key
from hajiri.db.models import Employee, PayrollHold, PayrollRun, Payslip, PayslipLine
from hajiri.web.csv import cell_text

from .numbers import iso, iso_ms
from .queries import department_on


def esc(s) -> str:
    """HTML-escaped text ('' for None)."""
    return escape('' if s is None else str(s))


def csv_text(rows, *, bom: bool = False) -> str:
    """RFC 4180 rows with CRLF line ends (the csv module), formula-protected cells; a BOM so Excel reads UTF-8."""
    buf = io.StringIO()
    if bom:
        buf.write('\ufeff')
    w = csv.writer(buf, lineterminator='\r\n')
    for row in rows:
        w.writerow([cell_text(v) for v in row])
    return buf.getvalue()

RUN_TYPE = {'regular': 'Regular', 'supplementary': 'Supplementary', 'arrears': 'Arrears', 'off_cycle': 'Off-cycle',
            'final_settlement': 'Final settlement', 'bonus': 'Bonus'}
TYPES = ('earning', 'deduction', 'employer_contribution')


def salary_sheet(company_id: str, run_id: str) -> dict | None:
    h = (PayrollRun.objects.for_company(company_id).filter(pk=run_id)
         .values('id', 'run_type', 'status', 'locked_at', 'approved_at', 'period__bs_year', 'period__bs_month', 'period__start_ad',
                 'period__end_ad', 'period__days', company_name=F('company__name_en'), maker_name=F('maker__name'),
                 checker_name=F('checker__name'))
         .first())
    if not h:
        return None
    end_ad = h['period__end_ad']
    held = PayrollHold.objects.filter(run_id=OuterRef('run_id'), employee_id=OuterRef('employee_id')).exclude(
        status=PayrollHold.Status.CANCELLED)
    slips = (Payslip.objects.filter(run_id=run_id).order_by('employee__emp_code')
             .annotate(department=department_on(end_ad, 'employee_id'), held=Exists(held))
             .values('id', 'employee_id', 'paid_days', 'lop_days', 'ot_hours', 'gross', 'total_deductions', 'net', 'employer_statutory',
                     'flags', 'department', 'held', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                     designation=F('employee__designation'), bank_code=F('employee__bank_code'),
                     bank_branch=F('employee__bank_branch'), bank_account_last4=F('employee__bank_account_last4')))
    lines = (PayslipLine.objects.filter(payslip__run_id=run_id).exclude(type=PayslipLine.Type.STATISTICAL)
             .order_by('sort_order', 'component_code').values('payslip_id', 'component_code', 'type', 'name_en', 'amount', 'sort_order'))
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
            items.sort(key=lambda kv: (kv[1]['order'], collate_key(kv[0])))
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
                'flags': [*(sl['flags'] if isinstance(sl['flags'], list) else []), *(['PayHeld'] if sl['held'] else [])],
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
        'company': h['company_name'],
        'period': {'label': bs_month_name(h['period__bs_year'], h['period__bs_month']), 'bsYear': h['period__bs_year'],
                   'bsMonth': h['period__bs_month'], 'from': iso(h['period__start_ad']), 'to': iso(end_ad), 'days': h['period__days']},
        'run': {'id': str(h['id']), 'type': h['run_type'], 'status': h['status'], 'final': h['locked_at'] is not None,
                'preparedBy': h['maker_name'], 'approvedBy': h['checker_name'], 'approvedAt': iso_ms(h['approved_at']),
                'lockedAt': iso_ms(h['locked_at'])},
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
    return csv_text([title, header, *body, foot], bom=True)


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
    basic = PayslipLine.objects.filter(payslip_id=OuterRef('pk'), component_code='BASIC')
    from .queries import sum_of
    r = (Payslip.objects.filter(run_id=run_id, employee__regime=Employee.Regime.SSF).order_by('employee__emp_code')
         .annotate(basic=sum_of(basic, 'amount', group='payslip_id'))
         .values('basic', 'employee_statutory', 'employer_statutory', emp_code=F('employee__emp_code'), name_en=F('employee__name_en'),
                 ssf_id=F('employee__ssf_id'), alloc=F('inputs_snapshot__result__ssfAllocation')))
    rows = [['emp_code', 'ssf_id', 'name', 'basic', 'employee_11pct', 'employer_20pct', 'medical_1pct', 'accident_1_40pct', 'dependent_0_27pct',
             'old_age_28_33pct', 'total_31pct']]
    with localcontext(ENGINE_CONTEXT):
        for x in r:
            a = x['alloc'] or {}
            ee, er = D(x['employee_statutory']), D(x['employer_statutory'])
            rows.append([x['emp_code'], x['ssf_id'] or '', x['name_en'], to_fixed(D(x['basic']), 2), to_fixed(ee, 2), to_fixed(er, 2),
                         a.get('medical', ''), a.get('accident', ''), a.get('dependent', ''), a.get('old_age', ''), to_fixed(ee + er, 2)])
    return csv_text(rows)
