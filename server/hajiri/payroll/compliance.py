"""
Payroll compliance extras for HR (port of payroll/compliance.ts; HR runs payroll alone, there is no bank sheet or
accounting export): opening balances, optimum CIT, held pay, tax and salary certificates, the annual tax summary and
the monthly statutory reports (tax deposit, retirement fund deposit, pay variance) plus the leave liability estimate.
Route functions take (user, query/body) and return the TS response bodies.
"""
from __future__ import annotations

import base64
import re
from decimal import localcontext
from pathlib import Path

from django.conf import settings
from django.db import transaction

from engines.bs_calendar import BS_MONTH_NAMES_EN, ad_to_bs, bs_month_bounds, fiscal_year, fiscal_year_of
from engines.payroll import amount_in_words_en, format_npr, optimum_cit
from engines.payroll.numeric import ENGINE_CONTEXT, D, js_str, to_dp, to_fixed

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_module
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Bool, Enum, Int, IsoDate, Money, Obj, Str, Uuid, parse
from hajiri.notifications.service import notify, user_id_for_employee

from ._db import fx, q, q1, x
from .compliance_service import effective_cit
from .tax_savings import current_fiscal_year_start, estimate_input, fy_label, fy_param

_AMOUNT = lambda **kw: Money(refine=(lambda v: not v.startswith('-'), 'must not be negative'), **kw)  # noqa: E731


def npr(v) -> str:
    with localcontext(ENGINE_CONTEXT):
        return format_npr(to_fixed(D(v if v is not None else 0), 2), 'latin')


def esc(s) -> str:
    return re.sub(r'[&<>"]', lambda m: {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[m.group(0)], '' if s is None else str(s))


def fy_month(bs_month: int) -> int:
    """Fiscal-year month index (Shrawan = 1) of a BS month."""
    return ((bs_month - 4 + 12) % 12) + 1


def _today() -> str:
    return npt_today().isoformat()


def assert_employee(company_id: str, employee_id: str) -> dict:
    r = q1('SELECT id, emp_code, name_en FROM employee WHERE id = $1 AND company_id = $2', [employee_id, company_id])
    if not r:
        raise NotFoundError('employee not found')
    return r


def _decrypt_pan(enc, employee_id: str) -> str | None:
    if enc is None:
        return None
    return field_cipher().decrypt(bytes(enc), f'employee:{employee_id}:pan')


# ---------------------------------------------------------------- annual tax summary (certificates and report)

def sst_share(tds, tax: dict | None):
    """Share of a month's TDS that is social security tax (the 1% band), from that payslip's own tax computation."""
    with localcontext(ENGINE_CONTEXT):
        if not tax or not tax.get('bands') or not tax.get('annualTax') or D(tax['annualTax']).is_zero():
            return D(0)
        first = tax['bands'][0] if tax['bands'] else None
        if not first or first.get('waived') or first.get('rate') != '0.01':
            return D(0)
        return to_dp(D(tds) * D(first['tax']) / D(tax['annualTax']), 2)


_MONTH_KEYS = ('gross', 'taxable', 'tds', 'sst', 'ssfEe', 'ssfEr', 'pfEe', 'pfEr', 'gratuity', 'cit')
_LINE_KEY = {'SSF_EE': 'ssfEe', 'SSF_ER': 'ssfEr', 'PF_EE': 'pfEe', 'PF_ER': 'pfEr', 'GRATUITY': 'gratuity', 'CIT': 'cit'}


def annual_summaries(company_id: str, fiscal_year_start: int, employee_ids: list[str] | None, *, reveal_ids: bool = False,
                     published_only: bool = False) -> list[dict]:
    fy = fiscal_year(fiscal_year_start)
    emps = q("""
        SELECT e.id, e.emp_code, e.name_en, e.designation, e.join_date::text, e.exit_date::text, e.pan_enc, e.ssf_id, e.pf_id, e.cit_id,
               (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id ORDER BY lower(a.valid) DESC LIMIT 1) AS department
          FROM employee e
         WHERE e.company_id = $1 AND ($2::uuid[] IS NULL OR e.id = ANY($2::uuid[]))
           AND (EXISTS (SELECT 1 FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
                         WHERE ps.employee_id = e.id AND r.locked_at IS NOT NULL AND p.start_ad BETWEEN $3::date AND $4::date)
                OR EXISTS (SELECT 1 FROM payroll_opening_balance o WHERE o.employee_id = e.id AND o.fiscal_year_start = $5)
                OR $2::uuid[] IS NOT NULL)
         ORDER BY e.emp_code""", [str(company_id), employee_ids, fy.start_ad, fy.end_ad, fiscal_year_start])
    if not emps:
        return []
    ids = [str(e['id']) for e in emps]
    slips = q("""
        SELECT ps.id, ps.employee_id, p.bs_year, p.bs_month, ps.gross::text, ps.taxable::text, ps.tds::text, ps.inputs_snapshot->'result'->'tax' AS tax, p.start_ad::text
          FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
         WHERE ps.employee_id = ANY($1::uuid[]) AND r.company_id = $2 AND r.locked_at IS NOT NULL AND p.start_ad BETWEEN $3::date AND $4::date
           AND (NOT $5::boolean OR ps.published_at IS NOT NULL)
         ORDER BY p.start_ad, r.locked_at""", [ids, str(company_id), fy.start_ad, fy.end_ad, published_only])
    lines = q("""SELECT l.payslip_id, l.component_code, l.type, l.name_en, l.amount::text, l.sort_order FROM payslip_line l
                  WHERE l.payslip_id = ANY($1::uuid[]) AND l.type IN ('earning', 'deduction', 'employer_contribution')""",
              [[str(sl['id']) for sl in slips]])
    taxable_code = {c['code']: c['taxable'] for c in q('SELECT code, taxable FROM salary_component WHERE company_id = $1', [str(company_id)])}
    byslip: dict[str, list[dict]] = {}
    for ln in lines:
        byslip.setdefault(str(ln['payslip_id']), []).append(ln)
    opening = {str(o['employee_id']): o for o in q("""
        SELECT employee_id, gross::text, taxable::text, tds_paid::text, retirement_employee::text, retirement_employer::text, source
          FROM payroll_opening_balance WHERE employee_id = ANY($1::uuid[]) AND fiscal_year_start = $2""", [ids, fiscal_year_start])}
    complete_from = bs_month_bounds(fiscal_year_start + 1, 3)['startAd']
    out = []
    with localcontext(ENGINE_CONTEXT):
        for e in emps:
            eid = str(e['id'])
            mine = [sl for sl in slips if str(sl['employee_id']) == eid]
            months: dict[str, dict] = {}
            heads: dict[str, dict] = {}
            for sl in mine:
                key = f"{sl['bs_year']}-{sl['bs_month']}"
                m = months.get(key) or {'bsYear': sl['bs_year'], 'bsMonth': sl['bs_month'], **{k: '0.00' for k in _MONTH_KEYS}}

                def add(k, v, m=m):
                    m[k] = to_fixed(D(m[k]) + D(v), 2)
                add('gross', sl['gross'])
                add('taxable', sl['taxable'])
                add('tds', sl['tds'])
                add('sst', sst_share(sl['tds'], sl['tax']))
                for ln in byslip.get(str(sl['id']), []):
                    k = _LINE_KEY.get(ln['component_code'])
                    if k:
                        add(k, ln['amount'])
                    if ln['type'] == 'earning' and ln['component_code'] != 'ROUNDING':
                        h = heads.get(ln['component_code']) or {'code': ln['component_code'], 'name': ln['name_en'], 'amount': D(0),
                                                                'taxable': taxable_code.get(ln['component_code'], True), 'order': ln['sort_order']}
                        h['amount'] = h['amount'] + D(ln['amount'])
                        heads[ln['component_code']] = h
                months[key] = m
            month_list = list(months.values())

            def total(k):
                t = D(0)
                for m in month_list:
                    t = t + D(m[k])
                return t
            ob = opening.get(eid)
            last = mine[-1] if mine else None
            last_tax = (last or {}).get('tax')
            complete = bool(last) and last['start_ad'] >= complete_from
            tax = None
            if last_tax:
                g = lambda k, d='0': str(last_tax.get(k) if last_tax.get(k) is not None else d)  # noqa: E731
                tax = {'complete': complete, 'asOfMonth': f"{BS_MONTH_NAMES_EN[last['bs_month'] - 1]} {last['bs_year']}" if last else None,
                       'projectedAnnualTaxable': g('projectedAnnualTaxable'), 'retirementDeduction': g('retirementDeduction'),
                       'insuranceDeduction': g('insuranceDeduction'), 'remoteAreaDeduction': g('remoteAreaDeduction'),
                       'disabilityDeduction': g('disabilityDeduction'), 'donationDeduction': g('donationDeduction', '0.00'),
                       'taxableAfterDeductions': g('taxableAfterDeductions'), 'grossTax': g('grossTax'), 'femaleRebate': g('femaleRebate'),
                       'medicalCredit': g('medicalCredit'), 'annualTax': g('annualTax')}
            obv = lambda k: D(ob[k]) if ob else D(0)  # noqa: E731
            tds_total = total('tds') + obv('tds_paid')
            pan = None
            if e['pan_enc'] is not None:
                full = _decrypt_pan(e['pan_enc'], eid)
                pan = full if reveal_ids else f'*****{full[-4:]}'
            out.append({
                'employee': {'id': eid, 'code': e['emp_code'], 'name': e['name_en'], 'designation': e['designation'], 'department': e['department'],
                             'joinDate': e['join_date'], 'exitDate': e['exit_date'], 'pan': pan, 'ssfId': e['ssf_id'], 'pfId': e['pf_id'],
                             'citId': e['cit_id']},
                'fiscalYearStart': fiscal_year_start, 'months': month_list,
                'incomeHeads': [{'code': h['code'], 'name': h['name'], 'amount': to_fixed(h['amount'], 2), 'taxable': h['taxable']}
                                for h in sorted(heads.values(), key=lambda h: h['order'])],
                'opening': ({'gross': ob['gross'], 'taxable': ob['taxable'], 'tds': ob['tds_paid'], 'retirementEmployee': ob['retirement_employee'],
                             'retirementEmployer': ob['retirement_employer'], 'source': ob['source']} if ob else None),
                'totals': {
                    'gross': to_fixed(total('gross') + obv('gross'), 2), 'taxable': to_fixed(total('taxable') + obv('taxable'), 2),
                    'tds': to_fixed(tds_total, 2), 'sst': to_fixed(total('sst'), 2),
                    'retirementEmployee': to_fixed(total('ssfEe') + total('pfEe') + total('cit') + obv('retirement_employee'), 2),
                    'retirementEmployer': to_fixed(total('ssfEr') + total('pfEr') + total('gratuity') + obv('retirement_employer'), 2),
                    'cit': to_fixed(total('cit'), 2),
                },
                'tax': tax, 'balance': to_fixed(D(tax['annualTax']) - tds_total, 2) if tax else None,
            })
    return out


# ---------------------------------------------------------------- certificate pages

_brand: str | None = None
_brand_read = False


def brand_logo() -> str | None:
    global _brand, _brand_read
    if not _brand_read:
        _brand_read = True
        try:
            path = Path(settings.BASE_DIR) / 'static' / 'brand' / 'cv-wordmark-carbon.svg'
            _brand = 'data:image/svg+xml;base64,' + base64.b64encode(path.read_bytes()).decode()
        except (OSError, AttributeError):
            _brand = None
    return _brand


def company_profile(company_id: str) -> dict:
    c = q1("""SELECT c.name_en, s.value FROM company c LEFT JOIN company_setting s ON s.company_id = c.id AND s.key = 'company.profile'
               WHERE c.id = $1""", [str(company_id)]) or {}
    v = c.get('value') or {}
    logo = v.get('logoDataUrl')
    if not (isinstance(logo, str) and re.fullmatch(r'data:image/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+', logo)):
        logo = brand_logo()
    return {'name': v.get('nameEn') or c.get('name_en') or 'Company', 'address': v.get('address'), 'pan': v.get('pan'), 'logo': logo}


PAGE_CSS = ('body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:12.5px;color:#17191e;margin:24px auto;max-width:820px;line-height:1.5;padding:0 24px}\n'
            '.head{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid #12773a;padding-bottom:12px;margin-bottom:16px}.head img{height:34px}.co{text-align:right;color:#4b5060}.co b{color:#17191e;font-size:15px}\n'
            'h1{font-size:18px;margin:6px 0 2px;letter-spacing:-.01em}.sub{color:#6b707b}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:8px 20px;margin:14px 0}.meta span{display:block;color:#6b707b;font-size:11px}\n'
            'table{width:100%;border-collapse:collapse;margin:8px 0 14px}th{text-align:left;font-weight:600;color:#4b5060;font-size:11.5px;border-bottom:1px solid #d9dbe0;padding:5px 4px}\n'
            'td{border-bottom:1px solid #eeeff2;padding:5px 4px}.n{text-align:right;font-variant-numeric:tabular-nums}tr.t td{font-weight:600;border-top:1px solid #9aa0aa}\n'
            'h2{font-size:13.5px;margin:18px 0 4px}.note{font-size:11px;color:#6b707b}.sign{display:grid;grid-template-columns:1fr 1fr;gap:40px;margin-top:48px}.sign div{border-top:1px solid #9aa0aa;padding-top:4px;color:#4b5060}\n'
            '.draft{background:#fff4e5;color:#8a4b00;padding:8px 12px;border-radius:8px;margin-bottom:10px}@media print{body{margin:8mm auto}}')


def page_head(co: dict, title: str) -> str:
    logo = f'<img src="{esc(co["logo"])}" alt="">' if co.get('logo') else '<span></span>'
    extra = (f"<br>{esc(co['address'])}" if co.get('address') else '') + (f"<br>PAN {esc(co['pan'])}" if co.get('pan') else '')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(title)}</title><style>{PAGE_CSS}</style></head><body>\n'
            f'<div class="head">{logo}<div class="co"><b>{esc(co["name"])}</b>{extra}</div></div>')


def tax_certificate_html(co: dict, s: dict, issued_on: str) -> str:
    e = s['employee']
    fy = fiscal_year(s['fiscalYearStart'])
    with localcontext(ENGINE_CONTEXT):
        month_rows = ''.join(
            f"<tr><td>{esc(BS_MONTH_NAMES_EN[m['bsMonth'] - 1])} {m['bsYear']}</td><td class=\"n\">{npr(m['gross'])}</td><td class=\"n\">{npr(m['taxable'])}</td>"
            f"<td class=\"n\">{npr(D(m['ssfEe']) + D(m['pfEe']))}</td><td class=\"n\">{npr(m['cit'])}</td><td class=\"n\">{npr(m['sst'])}</td>"
            f"<td class=\"n\">{npr(D(m['tds']) - D(m['sst']))}</td><td class=\"n\">{npr(m['tds'])}</td></tr>" for m in s['months'])
        t = s['tax']

        def row(label, v, strong=False):
            return f"<tr{' class=\"t\"' if strong else ''}><td>{esc(label)}</td><td class=\"n\">{npr(v if v is not None else '0')}</td></tr>"
        service = ('from ' + bs_label(e['joinDate'])) if e['joinDate'] and e['joinDate'] > fy.start_ad else 'whole year'
        service = esc(service) + (f", until {esc(bs_label(e['exitDate']))}" if e['exitDate'] and e['exitDate'] <= fy.end_ad else '')
        op = s['opening']
        opening_row = ''
        if op:
            src = 'previous employer' if op['source'] == 'previous_employer' else 'earlier payroll'
            opening_row = (f"<tr><td>Before Hajiri ({esc(src)})</td><td class=\"n\">{npr(op['gross'])}</td><td class=\"n\">{npr(op['taxable'])}</td>"
                           f"<td class=\"n\" colspan=\"2\">{npr(op['retirementEmployee'])}</td><td class=\"n\" colspan=\"2\"></td><td class=\"n\">{npr(op['tds'])}</td></tr>")
        tt = s['totals']
        heads = ''.join(f"<tr><td>{esc(h['name'])}</td><td>{'Yes' if h['taxable'] else 'No'}</td><td class=\"n\">{npr(h['amount'])}</td></tr>"
                        for h in s['incomeHeads']) or '<tr><td colspan="3" class="sub">None</td></tr>'
        comp = ''
        if t:
            bal = D(s['balance'] or 0)
            comp = ('<h2>Tax computation</h2><table><tbody>\n'
                    + row('Assessable remuneration for the year', t['projectedAnnualTaxable']) + '\n'
                    + row('Less retirement contributions (SSF, PF, CIT; Sec 63)', t['retirementDeduction']) + '\n'
                    + row('Less insurance premiums', t['insuranceDeduction']) + '\n'
                    + (row('Less remote area allowance', t['remoteAreaDeduction']) if D(t['remoteAreaDeduction']) > 0 else '') + '\n'
                    + (row('Less disability allowance', t['disabilityDeduction']) if D(t['disabilityDeduction']) > 0 else '') + '\n'
                    + (row('Less approved donations (Sec 12)', t['donationDeduction']) if D(t['donationDeduction']) > 0 else '') + '\n'
                    + row('Taxable income', t['taxableAfterDeductions'], True) + '\n'
                    + row('Tax on taxable income', t['grossTax']) + '\n'
                    + (row('Less female rebate', t['femaleRebate']) if D(t['femaleRebate']) > 0 else '') + '\n'
                    + (row('Less medical tax credit (Sec 51)', t['medicalCredit']) if D(t['medicalCredit']) > 0 else '') + '\n'
                    + row('Tax liability for the year', t['annualTax'], True) + '\n'
                    + row('Tax deducted and deposited', tt['tds']) + '\n'
                    + row('Tax deducted in excess' if bal.is_signed() and not bal.is_zero() else 'Balance still to be deducted', to_fixed(abs(bal), 2), True)
                    + '\n</tbody></table>')
        draft = (f'<div class="draft">The fiscal year is not finished: the tax figures are the projection as of {esc(t["asOfMonth"])} and will change with later payslips.</div>'
                 if t and not t['complete'] else '')
        fyl = fy_label(s['fiscalYearStart'])
        return (page_head(co, f"Tax certificate {e['code']} FY {fyl}") + '\n' + draft + '\n'
                f"<h1>Tax deduction certificate</h1><div class=\"sub\">Remuneration and tax deducted at source, fiscal year {fyl} (Shrawan {s['fiscalYearStart']} to Ashadh {s['fiscalYearStart'] + 1})</div>\n"
                f"<div class=\"meta\"><div><span>Employee</span>{esc(e['name'])} ({esc(e['code'])})</div><div><span>Designation</span>{esc(e['designation']) or '—'}</div><div><span>Department</span>{esc(e['department']) or '—'}</div>\n"
                f"<div><span>Employee PAN</span>{esc(e['pan']) or '—'}</div><div><span>SSF number</span>{esc(e['ssfId']) or '—'}</div><div><span>Service in the year</span>{service}</div></div>\n"
                '<h2>Monthly remuneration and tax deposited</h2>\n'
                '<table><thead><tr><th>Month</th><th class="n">Gross</th><th class="n">Taxable</th><th class="n">SSF/PF (employee)</th><th class="n">CIT</th><th class="n">SST</th><th class="n">Income tax</th><th class="n">Total TDS</th></tr></thead><tbody>\n'
                + (month_rows or '<tr><td colspan="8" class="sub">No locked payslips in this fiscal year.</td></tr>') + '\n'
                + opening_row + '\n'
                f"<tr class=\"t\"><td>Total</td><td class=\"n\">{npr(tt['gross'])}</td><td class=\"n\">{npr(tt['taxable'])}</td><td class=\"n\" colspan=\"2\">{npr(tt['retirementEmployee'])}</td><td class=\"n\">{npr(tt['sst'])}</td><td class=\"n\">{npr(D(tt['tds']) - D(tt['sst']))}</td><td class=\"n\">{npr(tt['tds'])}</td></tr>\n"
                '</tbody></table>\n'
                '<h2>Income by head</h2><table><thead><tr><th>Head</th><th>Taxable</th><th class="n">Amount</th></tr></thead><tbody>\n'
                + heads + '</tbody></table>\n' + comp + '\n'
                f"<p class=\"note\">Issued on {esc(issued_on)}. Amounts in NPR. Computer-generated from the locked payroll records of {esc(co['name'])}. Figures depend on the tax rules configured in Hajiri; confirm them with your accountant.</p>\n"
                '<div class="sign"><div>Prepared by</div><div>Authorised signatory</div></div></body></html>')


def salary_certificate_html(co: dict, s: dict, current: dict | None, issued_on: str, purpose: str | None) -> str:
    e = s['employee']
    today = _today()
    left = bool(e['exitDate'] and e['exitDate'] < today)
    cur = ''
    if current:
        cur = (f"<h2>Current monthly salary (as of {esc(current['asOf'])})</h2><table><tbody>\n"
               + ''.join(f"<tr><td>{esc(ln['name'])}</td><td class=\"n\">{npr(ln['amount'])}</td></tr>" for ln in current['lines']) + '\n'
               f"<tr class=\"t\"><td>Gross monthly salary</td><td class=\"n\">{npr(current['gross'])}</td></tr>\n"
               + ''.join(f"<tr><td>Less {esc(ln['name'])}</td><td class=\"n\">{npr(ln['amount'])}</td></tr>" for ln in current['deductions']) + '\n'
               f"<tr class=\"t\"><td>Net monthly salary (last payslip)</td><td class=\"n\">{npr(current['net'])}</td></tr></tbody></table>\n"
               f"<p class=\"note\">{esc(amount_in_words_en(current['gross']))} gross per month.</p>")
    t = s['totals']
    return (page_head(co, f"Salary certificate {e['code']}") + '\n'
            f"<h1>Salary certificate</h1><div class=\"sub\">Issued on {esc(issued_on)}{f' · for {esc(purpose)}' if purpose else ''}</div>\n"
            f"<p style=\"margin-top:14px\">This is to certify that <b>{esc(e['name'])}</b> (employee code {esc(e['code'])}) {'was' if left else 'is'} employed with {esc(co['name'])}"
            + (f" as <b>{esc(e['designation'])}</b>" if e['designation'] else '')
            + (f" in {esc(e['department'])}" if e['department'] else '')
            + f" since {esc(bs_label(e['joinDate']))}" + (f" until {esc(bs_label(e['exitDate']))}" if left else '') + '.</p>\n'
            + cur + '\n'
            f"<h2>Fiscal year {fy_label(s['fiscalYearStart'])} so far</h2><table><tbody>\n"
            f"<tr><td>Gross remuneration</td><td class=\"n\">{npr(t['gross'])}</td></tr><tr><td>Retirement contributions (employee)</td><td class=\"n\">{npr(t['retirementEmployee'])}</td></tr>\n"
            f"<tr><td>Tax deducted at source</td><td class=\"n\">{npr(t['tds'])}</td></tr></tbody></table>\n"
            "<p class=\"note\">Amounts in NPR, from the locked payroll records. This certificate is issued at the employee's request without any liability on the part of the company.</p>\n"
            '<div class="sign"><div>Prepared by</div><div>Authorised signatory</div></div></body></html>')


def last_payslip(employee_id: str, published_only: bool = False) -> dict | None:
    """The latest locked regular payslip (for the salary certificate's current salary)."""
    ps = q1("""SELECT ps.id, ps.gross::text, ps.net::text, p.bs_year, p.bs_month FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id
                 JOIN payroll_period p ON p.id = r.period_id
                WHERE ps.employee_id = $1 AND r.locked_at IS NOT NULL AND r.run_type = 'regular' AND (NOT $2::boolean OR ps.published_at IS NOT NULL)
                ORDER BY p.start_ad DESC LIMIT 1""", [employee_id, published_only])
    if not ps:
        return None
    lines = q("""SELECT name_en, amount::text, type FROM payslip_line WHERE payslip_id = $1 AND type IN ('earning', 'deduction')
                  AND component_code <> 'ROUNDING' ORDER BY sort_order""", [ps['id']])
    return {'lines': [{'name': ln['name_en'], 'amount': ln['amount'], 'type': ln['type']} for ln in lines], 'gross': ps['gross'], 'net': ps['net'],
            'label': f"{BS_MONTH_NAMES_EN[ps['bs_month'] - 1]} {ps['bs_year']}"}


# ---------------------------------------------------------------- opening balances

def opening_schema() -> Obj:
    return Obj({
        'employeeId': Uuid(), 'fiscalYearStart': fy_param(),
        'gross': _AMOUNT(default='0'), 'taxable': _AMOUNT(default='0'), 'tdsPaid': _AMOUNT(default='0'),
        'retirementEmployee': _AMOUNT(default='0'), 'retirementEmployer': _AMOUNT(default='0'),
        'festivalPaid': Bool(default=False), 'months': Int(min=0, max=12, default=0),
        'source': Enum(['previous_employer', 'go_live', 'other'], default='previous_employer'),
        'note': Str(trim=True, max=500, nullish=True),
    })


_OPENING_REFINE = 'taxable income cannot be more than gross plus the employer contribution'


def parse_opening(body: dict) -> dict:
    from hajiri.core.errors import ValidationError
    b = parse(opening_schema(), body)
    with localcontext(ENGINE_CONTEXT):
        if not D(b['taxable']) <= D(b['gross']) + D(b['retirementEmployer']):
            raise ValidationError([{'path': 'taxable', 'message': _OPENING_REFINE}])
    return b


def save_opening(company_id: str, actor_id: str, b: dict) -> None:
    x("""INSERT INTO payroll_opening_balance (company_id, employee_id, fiscal_year_start, gross, taxable, tds_paid, retirement_employee, retirement_employer,
                                             festival_paid, months, source, note, updated_by)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
         ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET gross = EXCLUDED.gross, taxable = EXCLUDED.taxable, tds_paid = EXCLUDED.tds_paid,
           retirement_employee = EXCLUDED.retirement_employee, retirement_employer = EXCLUDED.retirement_employer, festival_paid = EXCLUDED.festival_paid,
           months = EXCLUDED.months, source = EXCLUDED.source, note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = now()""",
      [str(company_id), b['employeeId'], b['fiscalYearStart'], b['gross'], b['taxable'], b['tdsPaid'], b['retirementEmployee'],
       b['retirementEmployer'], b['festivalPaid'], b['months'], b['source'], b.get('note'), actor_id])
    audit('upsert', 'payroll_opening_balance', actor_id=actor_id, row_id=b['employeeId'],
          after={'fiscalYearStart': b['fiscalYearStart'], 'gross': b['gross'], 'taxable': b['taxable'], 'tdsPaid': b['tdsPaid']})


def _require_hr(user: AuthUser) -> None:
    if not has_module(user, 'payroll'):
        from hajiri.core.auth import MODULE_ROLES
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES['payroll'])}")


def list_openings(user: AuthUser, query: dict) -> dict:
    _require_hr(user)
    qy = parse(Obj({'fiscalYearStart': fy_param(optional=True)}), query)
    fy = qy.get('fiscalYearStart') or current_fiscal_year_start()
    r = q("""SELECT o.id, o.employee_id, e.emp_code, e.name_en, o.fiscal_year_start, o.gross::text, o.taxable::text, o.tds_paid::text,
                    o.retirement_employee::text, o.retirement_employer::text, o.festival_paid, o.months, o.source, o.note, o.updated_at,
                    u.name AS updated_by_name
               FROM payroll_opening_balance o JOIN employee e ON e.id = o.employee_id LEFT JOIN app_user u ON u.id = o.updated_by
              WHERE o.company_id = $1 AND o.fiscal_year_start = $2 ORDER BY e.emp_code""", [company_id_of(user), fy])
    for o in r:
        o['id'], o['employee_id'] = str(o['id']), str(o['employee_id'])
    return {'fiscalYearStart': fy, 'balances': r}


@transaction.atomic
def put_opening(user: AuthUser, body: dict) -> dict:
    _require_hr(user)
    b = parse_opening(body)
    company_id = company_id_of(user)
    assert_employee(company_id, b['employeeId'])
    save_opening(company_id, user.id, b)
    return {'ok': True}


@transaction.atomic
def delete_opening(user: AuthUser, id_: str) -> dict:
    _require_hr(user)
    oid = parse(Uuid(), id_)
    d = q1('DELETE FROM payroll_opening_balance WHERE id = $1 AND company_id = $2 RETURNING employee_id, fiscal_year_start', [oid, company_id_of(user)])
    if not d:
        raise NotFoundError('opening balance not found')
    audit('delete', 'payroll_opening_balance', actor_id=user.id, row_id=oid,
          before={'employee_id': str(d['employee_id']), 'fiscal_year_start': d['fiscal_year_start']})
    return {'ok': True}


def import_openings(user: AuthUser, text: str | None) -> dict:
    """CSV: emp_code, fiscal_year_start, gross, taxable, tds_paid, ... All rows or none. Raises BadRequestError with
    details {errors: [{row, message}]} when any row is wrong."""
    from hajiri.employees.service import parse_csv
    from hajiri.core.errors import ValidationError
    _require_hr(user)
    if not text:
        raise BadRequestError('send CSV as text/csv body or {"csv": "..."}')
    rws = parse_csv(text.lstrip('﻿'))
    if len(rws) < 2:
        raise BadRequestError('CSV needs a header row and at least one data row')
    if len(rws) > 5001:
        raise BadRequestError('CSV has more than 5,000 data rows; split the file')
    header = [h.strip().lower() for h in rws[0]]
    for h in ('emp_code', 'fiscal_year_start'):
        if h not in header:
            raise BadRequestError(f'missing column {h}')
    company_id = company_id_of(user)
    by_code = {c['emp_code'].lower(): str(c['id']) for c in q('SELECT id, emp_code FROM employee WHERE company_id = $1', [company_id])}
    errors: list[dict] = []
    valid: list[dict] = []
    for i in range(1, len(rws)):
        rec = {h: (rws[i][j].strip() if j < len(rws[i]) else '') for j, h in enumerate(header)}
        if all(not v for v in rec.values()):
            continue
        employee_id = by_code.get(str(rec.get('emp_code', '')).lower())
        if not employee_id:
            errors.append({'row': i + 1, 'message': f"no employee with code {rec.get('emp_code', '')}"})
            continue

        def num(k):
            return str(rec[k]).replace(',', '') if rec.get(k) else '0'
        months_raw = rec.get('months')
        months: object = 0
        if months_raw:
            try:
                fm = float(months_raw)
                months = int(fm) if fm.is_integer() else fm
            except ValueError:
                months = float('nan')
        candidate = {'employeeId': employee_id, 'fiscalYearStart': rec.get('fiscal_year_start'), 'gross': num('gross'), 'taxable': num('taxable'),
                     'tdsPaid': num('tds_paid'), 'retirementEmployee': num('retirement_employee'), 'retirementEmployer': num('retirement_employer'),
                     'festivalPaid': str(rec.get('festival_paid') or '').lower() in ('true', '1', 'yes'), 'months': months}
        if rec.get('source'):
            candidate['source'] = rec['source']
        if rec.get('note'):
            candidate['note'] = rec['note']
        try:
            valid.append(parse_opening(candidate))
        except ValidationError as e:
            for iss in e.issues:
                errors.append({'row': i + 1, 'message': f"{iss.get('path') or 'row'}: {iss.get('message')}"})
    if errors:
        raise BadRequestError('some rows have errors; nothing was imported', {'errors': errors})
    with transaction.atomic():
        for b in valid:
            save_opening(company_id, user.id, b)
    return {'imported': len(valid)}


# ---------------------------------------------------------------- optimum CIT

def optimum_for(company_id: str, employee_id: str, fy: int) -> dict:
    built = estimate_input(company_id, employee_id, fy)
    if 'reason' in built:
        return {'available': False, 'reason': built['reason']}
    return {'available': True, **optimum_cit(built['input'])}


def optimum_me(user: AuthUser) -> dict:
    if not user.employee_id:
        raise BadRequestError('this account is not linked to an employee')
    return optimum_for(company_id_of(user), user.employee_id, current_fiscal_year_start())


def optimum_all(user: AuthUser) -> dict:
    _require_hr(user)
    company_id = company_id_of(user)
    fy = current_fiscal_year_start()
    emps = q("""SELECT e.id, e.emp_code, e.name_en FROM employee e WHERE e.company_id = $1 AND e.status = 'active' AND (e.exit_date IS NULL OR e.exit_date >= $2::date)
                  AND EXISTS (SELECT 1 FROM salary_assignment a WHERE a.employee_id = e.id) ORDER BY e.emp_code LIMIT 2000""", [company_id, _today()])
    out = [{'employeeId': str(e['id']), 'empCode': e['emp_code'], 'name': e['name_en'], **optimum_for(company_id, str(e['id']), fy)} for e in emps]
    return {'fiscalYearStart': fy, 'employees': out}


def apply_cit(user: AuthUser, body: dict) -> dict:
    """HR applies a monthly CIT amount from a month of the current fiscal year; earlier months keep the amount they had."""
    _require_hr(user)
    b = parse(Obj({'employeeId': Uuid(), 'monthlyCit': _AMOUNT(), 'fromBsMonth': Int(min=1, max=12, optional=True)}), body)
    company_id = company_id_of(user)
    emp = assert_employee(company_id, b['employeeId'])
    fy = current_fiscal_year_start()
    today = ad_to_bs(_today())
    from_index = fy_month(b.get('fromBsMonth') or today.month)
    if from_index < fy_month(today.month):
        raise BadRequestError('choose this month or a later month of the fiscal year')
    with transaction.atomic():
        cur = q1("""SELECT cit_monthly::text, cit_from_month, cit_prev_monthly::text FROM tax_declaration
                     WHERE employee_id = $1 AND fiscal_year_start = $2 AND status = 'verified' FOR UPDATE""", [b['employeeId'], fy])
        # The amount in force just before the new start month.
        before = effective_cit(cur, max(1, from_index - 1)) if cur else '0.00'
        x("""INSERT INTO tax_declaration (employee_id, fiscal_year_start, cit_monthly, cit_from_month, cit_prev_monthly, status, verified_by, verified_at)
             VALUES ($1, $2, $3, $4, $5, 'verified', $6, now())
             ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET cit_monthly = EXCLUDED.cit_monthly, cit_from_month = EXCLUDED.cit_from_month,
               cit_prev_monthly = EXCLUDED.cit_prev_monthly, status = 'verified', verified_by = EXCLUDED.verified_by, verified_at = now(), updated_at = now()""",
          [b['employeeId'], fy, b['monthlyCit'], None if from_index == 1 else from_index, before, user.id])
        audit('apply_cit', 'tax_declaration', actor_id=user.id, row_id=b['employeeId'],
              after={'fiscalYearStart': fy, 'monthlyCit': b['monthlyCit'], 'fromMonthIndex': from_index, 'before': before})
        month_name = BS_MONTH_NAMES_EN[(from_index + 2) % 12]
        notify(company_id=company_id, user_id=user_id_for_employee(b['employeeId']), employee_id=b['employeeId'], template='payroll.cit_applied',
               title='Your CIT contribution changed',
               body=f"HR set your Citizen Investment Trust contribution to NPR {npr(b['monthlyCit'])} a month from {month_name}. It lowers your income tax this year.",
               payload={'fiscalYearStart': fy}, channels=('push',))
    return {'ok': True, 'employee': emp['emp_code'], 'fiscalYearStart': fy, 'fromMonthIndex': from_index}


# ---------------------------------------------------------------- held pay

HOLD_STATUS = ('held', 'releasing', 'released', 'cancelled', 'open')


def list_holds(user: AuthUser, query: dict) -> dict:
    _require_hr(user)
    qy = parse(Obj({'status': Enum(HOLD_STATUS, default='open')}), query)
    r = q("""
        SELECT h.id, h.employee_id, e.emp_code, e.name_en, h.run_id, r.run_type, r.status AS run_status, p.bs_year, p.bs_month, h.amount::text, h.reason,
               h.status, h.release_mode, h.release_run_id, rp.bs_year AS release_bs_year, rp.bs_month AS release_bs_month, h.release_note, h.held_at,
               hb.name AS held_by_name, h.released_at
          FROM payroll_hold h JOIN employee e ON e.id = h.employee_id JOIN payroll_run r ON r.id = h.run_id JOIN payroll_period p ON p.id = r.period_id
          LEFT JOIN payroll_run rr ON rr.id = h.release_run_id LEFT JOIN payroll_period rp ON rp.id = rr.period_id LEFT JOIN app_user hb ON hb.id = h.held_by
         WHERE h.company_id = $1 AND ($2 = 'open' AND h.status IN ('held', 'releasing') OR h.status = $2)
         ORDER BY h.held_at DESC LIMIT 500""", [company_id_of(user), qy['status']])
    for h in r:
        for k in ('id', 'employee_id', 'run_id', 'release_run_id'):
            if h[k] is not None:
                h[k] = str(h[k])
    return {'holds': r}


def hold_pay(user: AuthUser, run_id: str, body: dict) -> dict:
    from .service import get_run
    _require_hr(user)
    rid = parse(Uuid(), run_id)
    b = parse(Obj({'employeeId': Uuid(), 'reason': Str(trim=True, min=3, max=500)}), body)
    company_id = company_id_of(user)
    with transaction.atomic():
        run = get_run(company_id, rid)
        if run['status'] not in ('computed', 'reviewed', 'approved'):
            raise ConflictError('this run is locked and paid; pay can only be held before the run is locked' if run['status'] == 'locked'
                                else "calculate the run before holding anyone's pay")
        if b['employeeId'] == user.employee_id:
            raise ForbiddenError('someone else must hold or release your own pay')
        ps = q1('SELECT net::text FROM payslip WHERE run_id = $1 AND employee_id = $2', [rid, b['employeeId']])
        if not ps:
            raise NotFoundError('this employee has no payslip in the run')
        if not D(ps['net']) > 0:
            raise ConflictError('there is no net pay to hold')
        existing = q1('SELECT id, status FROM payroll_hold WHERE run_id = $1 AND employee_id = $2', [rid, b['employeeId']])
        if existing and existing['status'] != 'cancelled':
            raise ConflictError('pay is already held for this employee in this run')
        row = q1("""INSERT INTO payroll_hold (company_id, employee_id, run_id, amount, reason, held_by) VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (run_id, employee_id) DO UPDATE SET amount = EXCLUDED.amount, reason = EXCLUDED.reason, held_by = EXCLUDED.held_by, held_at = now(),
                      status = 'held', release_mode = NULL, release_run_id = NULL, release_note = NULL, released_by = NULL, released_at = NULL
                    RETURNING id""", [company_id, b['employeeId'], rid, ps['net'], b['reason'], user.id])
        audit('hold', 'payroll_hold', actor_id=user.id, row_id=str(row['id']),
              after={'runId': rid, 'employeeId': b['employeeId'], 'amount': ps['net'], 'reason': b['reason']})
    return {'id': str(row['id']), 'amount': ps['net']}


def release_hold(user: AuthUser, hold_id: str, body: dict) -> dict:
    """Release held pay: through the next pay run as a non-taxable line, or record that it was paid outside payroll."""
    from .service import get_run
    _require_hr(user)
    hid = parse(Uuid(), hold_id)
    b = parse(Obj({'mode': Enum(['payroll', 'outside']), 'note': Str(trim=True, max=500, optional=True)}), body)
    company_id = company_id_of(user)
    with transaction.atomic():
        hold = q1('SELECT status, run_id, employee_id, amount::text FROM payroll_hold WHERE id = $1 AND company_id = $2', [hid, company_id])
        if not hold:
            raise NotFoundError('hold not found')
        if hold['status'] != 'held':
            raise ConflictError(f"this pay is already {hold['status']}")
        if str(hold['employee_id']) == user.employee_id:
            raise ForbiddenError('someone else must hold or release your own pay')
        run = get_run(company_id, str(hold['run_id']))
        if not run['locked_at']:
            raise ConflictError('lock the pay run first, or cancel the hold to pay normally in this run')
        if b['mode'] == 'outside':
            x("""UPDATE payroll_hold SET status = 'released', release_mode = 'outside', release_note = $2, released_by = $3, released_at = now()
                  WHERE id = $1""", [hid, b.get('note'), user.id])
        else:
            x("UPDATE payroll_hold SET status = 'releasing', release_mode = 'payroll', release_note = $2, released_by = $3 WHERE id = $1",
              [hid, b.get('note'), user.id])
        audit('release_hold', 'payroll_hold', actor_id=user.id, row_id=hid, after={'mode': b['mode'], 'amount': hold['amount']})
        emp = str(hold['employee_id'])
        notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='payroll.hold_released',
               title='Held pay released',
               body=(f"NPR {npr(hold['amount'])} that was on hold will be paid with your next salary." if b['mode'] == 'payroll'
                     else f"NPR {npr(hold['amount'])} that was on hold has been paid."),
               payload={'holdId': hid}, channels=('push',))
    return {'ok': True}


def cancel_hold(user: AuthUser, hold_id: str) -> dict:
    """Undo a hold (made by mistake) while its run is not locked: the employee is paid normally in that run."""
    from .service import get_run
    _require_hr(user)
    hid = parse(Uuid(), hold_id)
    company_id = company_id_of(user)
    with transaction.atomic():
        h = q1('SELECT status, run_id FROM payroll_hold WHERE id = $1 AND company_id = $2', [hid, company_id])
        if not h:
            raise NotFoundError('hold not found')
        if h['status'] != 'held':
            raise ConflictError(f"this pay is already {h['status']}")
        run = get_run(company_id, str(h['run_id']))
        if run['locked_at']:
            raise ConflictError('the run is locked: release the pay instead')
        x("UPDATE payroll_hold SET status = 'cancelled' WHERE id = $1", [hid])
        audit('cancel_hold', 'payroll_hold', actor_id=user.id, row_id=hid)
    return {'ok': True}


# ---------------------------------------------------------------- certificates and annual summary

def certificate(user: AuthUser, employee: str, query: dict) -> str:
    """The certificate page HTML (tax or salary). People outside payroll only see pay HR has published."""
    qy = parse(Obj({'fiscalYearStart': fy_param(optional=True), 'kind': Enum(['tax', 'salary'], default='tax'),
                    'purpose': Str(trim=True, max=120, optional=True)}), query)
    company_id = company_id_of(user)
    employee_id = user.employee_id if employee == 'me' else parse(Uuid(), employee)
    if not employee_id:
        raise BadRequestError('this account is not linked to an employee')
    own = employee_id == user.employee_id
    if not own and not has_module(user, 'payroll'):
        raise NotFoundError('employee not found')
    assert_employee(company_id, employee_id)
    fy = qy.get('fiscalYearStart') or current_fiscal_year_start()
    published_only = not has_module(user, 'payroll')
    found = annual_summaries(company_id, fy, [employee_id], reveal_ids=True, published_only=published_only)
    if not found:
        raise NotFoundError('employee not found')
    s = found[0]
    co = company_profile(company_id)
    issued_on = bs_label(_today())
    if not own:
        audit('certificate', 'employee', actor_id=user.id, row_id=employee_id, after={'kind': qy['kind'], 'fiscalYearStart': fy})
    if qy['kind'] == 'tax':
        return tax_certificate_html(co, s, issued_on)
    last = last_payslip(employee_id, published_only)
    current = ({'lines': [{'name': ln['name'], 'amount': ln['amount']} for ln in last['lines'] if ln['type'] == 'earning'], 'gross': last['gross'],
                'deductions': [{'name': ln['name'], 'amount': ln['amount']} for ln in last['lines'] if ln['type'] == 'deduction'],
                'net': last['net'], 'asOf': last['label']} if last else None)
    return salary_certificate_html(co, s, current, issued_on, qy.get('purpose'))


ANNUAL_COLUMNS = [
    {'key': 'empCode', 'header': 'Employee code'}, {'key': 'name', 'header': 'Name'}, {'key': 'pan', 'header': 'PAN'}, {'key': 'gross', 'header': 'Gross'},
    {'key': 'taxable', 'header': 'Taxable pay'}, {'key': 'retirementEmployee', 'header': 'Retirement (employee)'},
    {'key': 'retirementEmployer', 'header': 'Retirement (employer)'}, {'key': 'taxableAfterDeductions', 'header': 'Taxable income'},
    {'key': 'annualTax', 'header': 'Tax liability'}, {'key': 'sst', 'header': 'SST deducted'}, {'key': 'tds', 'header': 'TDS deducted'},
    {'key': 'balance', 'header': 'Balance'}, {'key': 'complete', 'header': 'Year complete'}, {'key': 'opening', 'header': 'Opening balance'},
]
FORMAT = lambda: Enum(['json', 'csv'], default='json')  # noqa: E731


def annual_tax(user: AuthUser, query: dict) -> dict:
    """{'format', 'filename', 'columns', 'rows'}: JSON answers {columns, rows}, CSV a download."""
    _require_hr(user)
    qy = parse(Obj({'fiscalYearStart': fy_param(optional=True), 'format': FORMAT()}), query)
    fy = qy.get('fiscalYearStart') or current_fiscal_year_start()
    lst = annual_summaries(company_id_of(user), fy, None, reveal_ids=qy['format'] == 'csv')
    if qy['format'] == 'csv':
        audit('export', 'payroll_annual_tax', actor_id=user.id, after={'fiscalYearStart': fy, 'rows': len(lst)})
    out = [{'employeeId': s['employee']['id'], 'empCode': s['employee']['code'], 'name': s['employee']['name'], 'pan': s['employee']['pan'],
            'gross': s['totals']['gross'], 'taxable': s['totals']['taxable'], 'retirementEmployee': s['totals']['retirementEmployee'],
            'retirementEmployer': s['totals']['retirementEmployer'], 'taxableAfterDeductions': (s['tax'] or {}).get('taxableAfterDeductions', ''),
            'annualTax': (s['tax'] or {}).get('annualTax', ''), 'sst': s['totals']['sst'], 'tds': s['totals']['tds'],
            'balance': s['balance'] if s['balance'] is not None else '', 'complete': 'yes' if (s['tax'] or {}).get('complete') else 'no',
            'opening': 'yes' if s['opening'] else 'no'} for s in lst]
    return {'format': qy['format'], 'filename': f'annual-tax-{fy}', 'columns': ANNUAL_COLUMNS, 'rows': out}


def _month_query(query: dict) -> dict:
    return parse(Obj({'bsYear': Int(min=2070, max=2200, coerce=True), 'bsMonth': Int(min=1, max=12, coerce=True), 'format': FORMAT()}), query)


TAX_DEPOSIT_COLUMNS = [
    {'key': 'empCode', 'header': 'Employee code'}, {'key': 'name', 'header': 'Name'}, {'key': 'pan', 'header': 'PAN'},
    {'key': 'taxable', 'header': 'Taxable pay'}, {'key': 'sst', 'header': 'Social security tax (1%)'}, {'key': 'incomeTax', 'header': 'Income tax'},
    {'key': 'tds', 'header': 'Total TDS'},
]


def tax_deposit(user: AuthUser, query: dict) -> dict:
    """TDS and SST withheld per employee in a BS month (all locked runs of the period), to deposit with the tax office."""
    _require_hr(user)
    qy = _month_query(query)
    company_id = company_id_of(user)
    r = q("""SELECT ps.employee_id, e.emp_code, e.name_en, e.pan_enc, ps.taxable::text, ps.tds::text, ps.inputs_snapshot->'result'->'tax' AS tax
               FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
              WHERE r.company_id = $1 AND p.bs_year = $2 AND p.bs_month = $3 AND r.locked_at IS NOT NULL ORDER BY e.emp_code, r.locked_at""",
          [company_id, qy['bsYear'], qy['bsMonth']])
    by: dict[str, dict] = {}
    with localcontext(ENGINE_CONTEXT):
        for row in r:
            eid = str(row['employee_id'])
            cur = by.get(eid) or {'empCode': row['emp_code'], 'name': row['name_en'], 'pan': _decrypt_pan(row['pan_enc'], eid),
                                  'taxable': D(0), 'sst': D(0), 'tds': D(0)}
            cur['taxable'] = cur['taxable'] + D(row['taxable'])
            cur['tds'] = cur['tds'] + D(row['tds'])
            cur['sst'] = cur['sst'] + sst_share(row['tds'], row['tax'])
            by[eid] = cur
        out = [{'empCode': v['empCode'], 'name': v['name'],
                'pan': v['pan'] if qy['format'] == 'csv' else (f"*****{v['pan'][-4:]}" if v['pan'] else None),
                'taxable': to_fixed(v['taxable'], 2), 'sst': to_fixed(v['sst'], 2), 'incomeTax': to_fixed(v['tds'] - v['sst'], 2),
                'tds': to_fixed(v['tds'], 2)} for v in by.values()]
    if qy['format'] == 'csv':
        audit('export', 'payroll_tax_deposit', actor_id=user.id, after={'bsYear': qy['bsYear'], 'bsMonth': qy['bsMonth']})
    return {'format': qy['format'], 'filename': f"tax-deposit-{qy['bsYear']}-{qy['bsMonth']:02d}", 'columns': TAX_DEPOSIT_COLUMNS, 'rows': out}


RETIREMENT_COLUMNS = [
    {'key': 'empCode', 'header': 'Employee code'}, {'key': 'name', 'header': 'Name'}, {'key': 'ssfId', 'header': 'SSF number'},
    {'key': 'SSF_EE', 'header': 'SSF employee (11%)'}, {'key': 'SSF_ER', 'header': 'SSF employer (20%)'}, {'key': 'ssfTotal', 'header': 'SSF total'},
    {'key': 'pfId', 'header': 'PF number'}, {'key': 'PF_EE', 'header': 'PF employee'}, {'key': 'PF_ER', 'header': 'PF employer'},
    {'key': 'GRATUITY', 'header': 'Gratuity'}, {'key': 'pfTotal', 'header': 'PF + gratuity total'}, {'key': 'citId', 'header': 'CIT number'},
    {'key': 'CIT', 'header': 'CIT'},
]


def retirement_deposit(user: AuthUser, query: dict) -> dict:
    """SSF, PF, gratuity and CIT per employee in a BS month, to deposit with each fund."""
    _require_hr(user)
    qy = _month_query(query)
    r = q("""SELECT e.emp_code, e.name_en, e.ssf_id, e.pf_id, e.cit_id, l.component_code AS code, sum(l.amount)::text AS amount
               FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
               JOIN payslip_line l ON l.payslip_id = ps.id AND l.component_code IN ('SSF_EE', 'SSF_ER', 'PF_EE', 'PF_ER', 'GRATUITY', 'CIT')
              WHERE r.company_id = $1 AND p.bs_year = $2 AND p.bs_month = $3 AND r.locked_at IS NOT NULL
              GROUP BY e.emp_code, e.name_en, e.ssf_id, e.pf_id, e.cit_id, l.component_code ORDER BY e.emp_code""",
          [company_id_of(user), qy['bsYear'], qy['bsMonth']])
    by: dict[str, dict] = {}
    for row in r:
        cur = by.get(row['emp_code']) or {'empCode': row['emp_code'], 'name': row['name_en'], 'ssfId': row['ssf_id'] or '', 'pfId': row['pf_id'] or '',
                                          'citId': row['cit_id'] or '', 'SSF_EE': '0.00', 'SSF_ER': '0.00', 'PF_EE': '0.00', 'PF_ER': '0.00',
                                          'GRATUITY': '0.00', 'CIT': '0.00'}
        cur[row['code']] = fx(D(cur.get(row['code']) or 0) + D(row['amount']))
        by[row['emp_code']] = cur
    out = [{**v, 'ssfTotal': fx(D(v['SSF_EE']) + D(v['SSF_ER'])), 'pfTotal': fx(D(v['PF_EE']) + D(v['PF_ER']) + D(v['GRATUITY']))} for v in by.values()]
    return {'format': qy['format'], 'filename': f"retirement-deposit-{qy['bsYear']}-{qy['bsMonth']:02d}", 'columns': RETIREMENT_COLUMNS, 'rows': out}


VARIANCE_COLUMNS = [
    {'key': 'empCode', 'header': 'Employee code'}, {'key': 'name', 'header': 'Name'}, {'key': 'previousNet', 'header': 'Previous net'},
    {'key': 'net', 'header': 'Net'}, {'key': 'difference', 'header': 'Difference'}, {'key': 'percent', 'header': 'Change %'},
    {'key': 'reasons', 'header': 'Reasons'},
]


def pay_variance(user: AuthUser, query: dict) -> dict:
    """Per employee: this regular run's net against the previous locked regular run, with the reasons it changed."""
    from .service import get_run
    _require_hr(user)
    qy = parse(Obj({'runId': Uuid(), 'format': FORMAT()}), query)
    company_id = company_id_of(user)
    run = get_run(company_id, qy['runId'])
    if run['run_type'] != 'regular':
        raise BadRequestError('pay variance compares regular monthly runs')
    prev_run = q1("""SELECT r.id, p.bs_year, p.bs_month FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id
                      WHERE r.company_id = $1 AND r.run_type = 'regular' AND r.locked_at IS NOT NULL AND r.id <> $2
                        AND (p.bs_year, p.bs_month) < (SELECT bs_year, bs_month FROM payroll_period WHERE id = $3)
                      ORDER BY p.bs_year DESC, p.bs_month DESC LIMIT 1""", [company_id, run['id'], run['period_id']])

    def load(rid):
        return q("""SELECT ps.employee_id, e.emp_code, e.name_en, ps.net::text, ps.gross::text, ps.tds::text, ps.lop_days::text, ps.ot_hours::text,
                           ps.inputs_snapshot->'input'->'baseAmounts'->>'BASIC' AS basic,
                           jsonb_array_length(coalesce(ps.inputs_snapshot->'input'->'inputs', '[]'::jsonb)) AS inputs, ps.flags
                      FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1""", [str(rid)])
    cur = load(run['id'])
    prev = {str(sl['employee_id']): sl for sl in load(prev_run['id'])} if prev_run else {}
    seen: set[str] = set()
    out = []
    with localcontext(ENGINE_CONTEXT):
        for c in cur:
            eid = str(c['employee_id'])
            seen.add(eid)
            p = prev.get(eid)
            reasons: list[str] = []
            flags = c['flags'] if isinstance(c['flags'], list) else []
            if not p:
                reasons.append('New in payroll')
            else:
                if c['basic'] != p['basic'] or 'SalaryRevised' in flags:
                    reasons.append('Salary revision')
                if D(c['lop_days']) != D(p['lop_days']):
                    reasons.append(f"Unpaid days {js_str(D(p['lop_days']))} → {js_str(D(c['lop_days']))}")
                if D(c['ot_hours']) != D(p['ot_hours']):
                    reasons.append(f"Overtime {js_str(D(p['ot_hours']))} h → {js_str(D(c['ot_hours']))} h")
                if c['inputs'] > 0 or p['inputs'] > 0:
                    reasons.append('One-time pay or deductions')
                if D(c['tds']) != D(p['tds']):
                    reasons.append('Tax changed')
                if 'LastMonth' in flags:
                    reasons.append('Leaving this month')
            diff = D(c['net']) - D(p['net']) if p else D(c['net'])
            out.append({'empCode': c['emp_code'], 'name': c['name_en'], 'previousNet': p['net'] if p else '', 'net': c['net'],
                        'difference': to_fixed(diff, 2),
                        'percent': js_str(to_dp(diff / D(p['net']) * D(100), 1)) if p and D(p['net']) > 0 else '',
                        'reasons': '; '.join(reasons) or 'No change'})
        for eid, p in prev.items():
            if eid not in seen:
                out.append({'empCode': p['emp_code'], 'name': p['name_en'], 'previousNet': p['net'], 'net': '0.00', 'difference': to_fixed(-D(p['net']), 2),
                            'percent': '-100', 'reasons': 'Not in this run (left or not paid)'})
        out.sort(key=lambda r_: abs(D(r_['difference'])), reverse=True)
    previous = {'runId': str(prev_run['id']), 'bsYear': prev_run['bs_year'], 'bsMonth': prev_run['bs_month']} if prev_run else None
    return {'format': qy['format'], 'filename': f"pay-variance-{run['id']}", 'columns': VARIANCE_COLUMNS, 'rows': out, 'previous': previous}


LIABILITY_COLUMNS = [
    {'key': 'empCode', 'header': 'Employee code'}, {'key': 'name', 'header': 'Name'}, {'key': 'department', 'header': 'Department'},
    {'key': 'leaveType', 'header': 'Leave type'}, {'key': 'balance', 'header': 'Balance (days)'}, {'key': 'dailyRate', 'header': 'Daily rate'},
    {'key': 'amount', 'header': 'Amount'}, {'key': 'note', 'header': 'Note'},
]


def leave_liability(user: AuthUser, query: dict) -> dict:
    """What the encashable leave balances are worth as of a date: balance × last basic ÷ the encashment divisor."""
    _require_hr(user)
    qy = parse(Obj({'asOf': IsoDate(optional=True), 'format': FORMAT()}), query)
    company_id = company_id_of(user)
    as_of = qy.get('asOf') or _today()
    pol = q1("SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payroll.policy'", [company_id])
    bs = ad_to_bs(as_of)
    divisor = 30 if ((pol or {}).get('value') or {}).get('encashmentDivisor') == 'fixed_30' else bs_month_bounds(bs.year, bs.month)['days']
    r = q("""
        SELECT e.id AS employee_id, e.emp_code, e.name_en,
               (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> $2::date LIMIT 1) AS department,
               lt.code AS leave_code, lt.name_en AS leave_name, sum(ll.delta_days)::text AS balance,
               (SELECT sa.base_amounts->>'BASIC' FROM salary_assignment sa WHERE sa.employee_id = e.id AND sa.valid @> $2::date LIMIT 1) AS basic
          FROM employee e JOIN leave_ledger ll ON ll.employee_id = e.id AND ll.effective_date <= $2::date
          JOIN leave_type lt ON lt.id = ll.leave_type_id AND lt.encashable
         WHERE e.company_id = $1 AND e.status = 'active' AND (e.exit_date IS NULL OR e.exit_date >= $2::date)
         GROUP BY e.id, e.emp_code, e.name_en, lt.code, lt.name_en, lt.sort_order HAVING sum(ll.delta_days) > 0 ORDER BY e.emp_code, lt.sort_order""",
          [company_id, as_of])
    out = []
    with localcontext(ENGINE_CONTEXT):
        for row in r:
            rate = D(row['basic']) / D(divisor) if row['basic'] else None
            out.append({'empCode': row['emp_code'], 'name': row['name_en'], 'department': row['department'] or 'No department',
                        'leaveType': row['leave_name'], 'balance': to_fixed(D(row['balance']), 2),
                        'dailyRate': to_fixed(to_dp(rate, 2), 2) if rate is not None else '',
                        'amount': to_fixed(to_dp(rate * D(row['balance']), 2), 2) if rate is not None else '',
                        'note': '' if row['basic'] else 'no salary on this date'})
        total = D(0)
        by_dep: dict[str, object] = {}
        for o in out:
            amt = D(o['amount'] or 0)
            total = total + amt
            by_dep[o['department']] = by_dep.get(o['department'], D(0)) + amt
        by_department = sorted(({'department': k, 'amount': to_fixed(v, 2)} for k, v in by_dep.items()), key=lambda d: D(d['amount']), reverse=True)
        total_s = to_fixed(total, 2)
    return {'format': qy['format'], 'filename': f'leave-liability-{as_of}', 'columns': LIABILITY_COLUMNS, 'rows': out,
            'asOf': as_of, 'divisor': divisor, 'total': total_s, 'byDepartment': by_department}


def compliance_years(user: AuthUser) -> dict:
    """Fiscal years with data, for the pickers."""
    _require_hr(user)
    r = q("""SELECT DISTINCT CASE WHEN p.bs_month >= 4 THEN p.bs_year ELSE p.bs_year - 1 END AS fy FROM payroll_period p
              WHERE p.company_id = $1 ORDER BY fy DESC""", [company_id_of(user)])
    cur = current_fiscal_year_start()
    years = sorted({cur, *[int(x_['fy']) for x_ in r]}, reverse=True)
    t = ad_to_bs(_today())
    return {'current': cur, 'years': years, 'currentMonth': {'year': t.year, 'month': t.month, 'day': t.day},
            'fiscalYearOf': fiscal_year_of(t).start_year}


__all__ = ['annual_summaries', 'tax_certificate_html', 'salary_certificate_html', 'company_profile', 'list_openings', 'put_opening',
           'delete_opening', 'import_openings', 'optimum_me', 'optimum_all', 'apply_cit', 'list_holds', 'hold_pay', 'release_hold',
           'cancel_hold', 'certificate', 'annual_tax', 'tax_deposit', 'retirement_deposit', 'pay_variance', 'leave_liability',
           'compliance_years']
