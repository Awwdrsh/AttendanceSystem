"""Printable payslip in English (port of payroll/payslip-html.ts). Employer contributions are shown apart from deductions."""
from __future__ import annotations

from engines.bs_calendar import BS_MONTH_NAMES_EN
from engines.payroll import amount_in_words_en, format_npr
from engines.payroll.numeric import D

from hajiri.core.bsdate import bs_label

from .compliance import esc

CSS = """body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px;color:#17191e;margin:32px auto;max-width:860px;line-height:1.55;padding:0 24px}
h1{font-size:20px;margin:0;font-weight:600;letter-spacing:-.01em}.sub{color:#6b707b}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:16px;border-bottom:1px solid #e6e7ea}
.period{text-align:right}.period b{font-size:16px;font-weight:600}
.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:12px 24px;margin:20px 0}.meta div span{color:#6b707b;display:block;font-size:11.5px}
.cols{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}table.blk{width:100%;border-collapse:collapse}
table.blk th{text-align:left;font-weight:500;color:#6b707b;font-size:12px;padding:6px 0;border-bottom:1px solid #e6e7ea}
table.blk td{padding:6px 0;border-bottom:1px solid #f0f0f2}.amt{text-align:right;font-variant-numeric:tabular-nums}.muted{color:#9aa0aa}
.totals{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-top:8px;font-weight:600}
.net{margin-top:24px;padding:18px 20px;border:1px solid #e6e7ea;border-radius:12px;display:flex;justify-content:space-between;align-items:center}
.net .label{color:#6b707b}.net b{font-size:24px;font-weight:600;letter-spacing:-.02em;color:#12773a;font-variant-numeric:tabular-nums}.words{font-size:12px;color:#6b707b;margin-top:2px}
.held{margin-top:20px;padding:10px 14px;border-radius:10px;background:#fff4e5;color:#8a4b00;font-weight:500}
.foot{margin-top:24px;font-size:11px;color:#6b707b;border-top:1px solid #e6e7ea;padding-top:10px}.explain{margin-top:16px;font-size:12px}
@media print{body{margin:10mm auto}.net{border-color:#bbb}}"""


def render_payslip_html(v: dict) -> str:
    def n(x):
        return format_npr(x, 'latin')
    period = f"{BS_MONTH_NAMES_EN[v['period']['bsMonth'] - 1]} {v['period']['bsYear']}"

    def block(title, type_):
        rows = [ln for ln in v['lines'] if ln['type'] == type_]
        body = ''.join(f"<tr><td>{esc(ln['nameEn'])}</td><td class=\"amt\">{n(ln['amount'])}</td></tr>" for ln in rows)
        return (f'<table class="blk"><thead><tr><th>{title}</th><th class="amt">NPR</th></tr></thead><tbody>{body}'
                f"{'' if rows else '<tr><td colspan=\"2\" class=\"muted\">None</td></tr>'}</tbody></table>")
    e = v['employee']
    held = ('<div class="held">Payment on hold: this net pay has not been paid yet. HR will release it in a later month.</div>'
            if v.get('held') else '')
    explain = ''.join(f"<li><b>{esc(x.get('code'))}</b>: {esc(x.get('text'))}</li>" for x in v['explain'])
    negative = ' Net pay is negative.' if D(v['net']).is_signed() and not D(v['net']).is_zero() else ''
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>Payslip {esc(e['code'])} {esc(period)}</title>\n<style>\n{CSS}\n</style></head><body>\n"
            f"<div class=\"hdr\"><div><h1>{esc(v['company']['nameEn'])}</h1><div class=\"sub\">Payslip</div></div>\n"
            f"<div class=\"period\"><div class=\"sub\">Pay period</div><b>{esc(period)}</b><div class=\"muted\">{esc(bs_label(v['period']['startAd']))} to {esc(bs_label(v['period']['endAd']))}</div></div></div>\n"
            '<div class="meta">\n'
            f"<div><span>Employee</span>{esc(e['nameEn'])} ({esc(e['code'])})</div>\n"
            f"<div><span>Designation</span>{esc(e['designation']) or '—'}</div><div><span>Department</span>{esc(e['department']) or '—'}</div>\n"
            f"<div><span>PAN</span>{esc(e['panMasked']) or '—'}</div><div><span>SSF</span>{esc(e['ssfMasked']) or '—'}</div><div><span>Bank account</span>{esc(e['bankMasked']) or '—'}</div>\n"
            f"<div><span>Paid days</span>{v['paidDays']}</div><div><span>Unpaid days</span>{v['lopDays']}</div><div><span>Overtime hours</span>{v['otHours']}</div>\n"
            '</div>\n'
            f"<div class=\"cols\">{block('Earnings', 'earning')}{block('Deductions', 'deduction')}{block('Employer contributions', 'employer_contribution')}</div>\n"
            f"<div class=\"totals\"><div class=\"amt\">Gross {n(v['gross'])}</div><div class=\"amt\">Total deductions {n(v['totalDeductions'])}</div><div class=\"amt\">{n(v['employerStatutory'])}</div></div>\n"
            f"{held}\n"
            f"<div class=\"net\"><div><div class=\"label\">Net pay</div><div class=\"words\">{esc(amount_in_words_en(v['net']))}</div></div><b>NPR {n(v['net'])}</b></div>\n"
            f"<div class=\"meta\"><div><span>Year-to-date gross</span>{n(v['ytd']['gross'])}</div><div><span>Year-to-date tax (TDS)</span>{n(v['ytd']['tds'])}</div><div><span>Year-to-date SSF/PF</span>{n(v['ytd']['statutory'])}</div></div>\n"
            f"<details class=\"explain\"><summary>How was this amount calculated?</summary><ul>{explain}</ul></details>\n"
            f"<div class=\"foot\">Computer-generated payslip. Verification code {esc(v['payslipId'])}. Figures in NPR.{negative} Contact HR if anything on this payslip looks wrong.</div>\n"
            '</body></html>')
