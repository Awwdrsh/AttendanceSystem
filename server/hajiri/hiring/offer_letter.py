"""Printable A4 offer letter in the CoreValley style (port of apps/api/src/modules/hiring/offer-letter.ts)."""
from __future__ import annotations

import re

from engines.payroll.words import amount_in_words_en, format_npr
from hajiri.core.bsdate import bs_label

_ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}


def _esc(s) -> str:
    return re.sub(r'[&<>"\']', lambda m: _ESC[m.group(0)], '' if s is None else str(s))


TYPE = {'full_time': 'full-time', 'part_time': 'part-time', 'contract': 'contract', 'internship': 'internship', 'temporary': 'temporary'}

_STYLE = """@page{size:A4;margin:18mm}
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13.5px;color:#17191e;margin:32px auto;max-width:760px;line-height:1.65;padding:0 24px}
.hdr{display:flex;justify-content:space-between;align-items:flex-end;padding-bottom:14px;border-bottom:2px solid #12773a}
h1{font-size:20px;margin:0;font-weight:700;letter-spacing:-.01em}.muted{color:#6b707b}.ref{text-align:right;font-size:12px}
h2{font-size:16px;margin:28px 0 12px}table{border-collapse:collapse;margin:12px 0 18px;width:100%}
td{padding:7px 0;border-bottom:1px solid #eef0f2;vertical-align:top}td:first-child{color:#6b707b;width:38%}
.sign{display:grid;grid-template-columns:1fr 1fr;gap:48px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;font-size:12px;color:#6b707b}
.draft{position:fixed;top:40%;left:0;right:0;text-align:center;font-size:96px;color:rgba(200,30,30,.08);transform:rotate(-24deg);font-weight:800;pointer-events:none}
@media print{body{margin:0 auto}}"""


def render_offer_letter(*, company: str, candidate: str, designation: str, department: str | None, location: str | None,
                        employment_type: str, salary, join_date, expires_on, issued_on: str, notes: str | None, reference: str,
                        draft: bool) -> str:
    """Marked DRAFT until the offer is approved."""
    paras = ''.join(f"<p>{_esc(p).replace(chr(10), '<br>')}</p>"
                    for p in (x.strip() for x in re.split(r'\n{2,}', notes or '')) if p)
    dept = f' in {_esc(department)}' if department else ''
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Offer letter: {_esc(candidate)}</title>\n'
        f'<style>\n{_STYLE}\n</style></head><body>\n'
        f"{'<div class=\"draft\">DRAFT</div>' if draft else ''}\n"
        f'<div class="hdr"><div><h1>{_esc(company)}</h1><div class="muted">Offer of employment</div></div>\n'
        f'<div class="ref"><div>Ref. {_esc(reference)}</div><div>{_esc(bs_label(issued_on))}</div></div></div>\n'
        f'<p style="margin-top:24px">Dear {_esc(candidate)},</p>\n'
        f'<p>We are pleased to offer you the {_esc(TYPE.get(employment_type, "full-time"))} position of <b>{_esc(designation)}</b>{dept} '
        f'at {_esc(company)}. The main terms of this offer are below.</p>\n'
        '<table>\n'
        f'<tr><td>Position</td><td>{_esc(designation)}</td></tr>\n'
        f"{f'<tr><td>Department</td><td>{_esc(department)}</td></tr>' if department else ''}\n"
        f"{f'<tr><td>Work location</td><td>{_esc(location)}</td></tr>' if location else ''}\n"
        f'<tr><td>Monthly gross salary</td><td>NPR {_esc(format_npr(str(salary), "latin"))}<div class="muted" style="font-size:12px">'
        f'{_esc(amount_in_words_en(str(salary)))}</div></td></tr>\n'
        f'<tr><td>Joining date</td><td>{_esc(bs_label(join_date))}</td></tr>\n'
        f"{f'<tr><td>Please accept by</td><td>{_esc(bs_label(expires_on))}</td></tr>' if expires_on else ''}\n"
        '</table>\n'
        f'{paras}\n'
        "<p>Your employment will be governed by the Labour Act 2074, the company's policies and the appointment letter you will "
        'receive on joining. Salary is subject to tax and Social Security Fund deductions as required by law.</p>\n'
        '<p>Please sign and return a copy of this letter to confirm that you accept the offer. We look forward to welcoming you.</p>\n'
        f'<div class="sign"><div>For {_esc(company)}<br>Authorised signatory</div><div>Accepted by {_esc(candidate)}<br>Signature and date</div></div>\n'
        '</body></html>')
