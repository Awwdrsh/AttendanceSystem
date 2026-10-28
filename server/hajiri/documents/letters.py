"""
Letter engine (port of apps/api/src/modules/documents/letters.ts).

Letter templates: plain text with {{placeholders}}, blank lines between paragraphs, "- " bullet lines and **bold**.
Nothing else is interpreted; every value and every piece of template text is HTML-escaped, so a template or an
employee's name can never inject markup into a printed letter.
"""
from __future__ import annotations

import base64
import html as _html
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import BooleanField, ExpressionWrapper, F, Q
from django.utils.html import strip_tags

from engines.bs_calendar import ad_to_bs
from engines.payroll import amount_in_words_en, format_npr
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today as _npt_today
from hajiri.core.settings_store import get_setting
from hajiri.db.models import (
    Company, DocumentCategory, Employee, EmployeeAssignment, LetterSequence, LetterTemplate, SalaryAssignment,
    WorkflowDefinition,
)

PLACEHOLDERS: list[dict[str, str]] = [
    {'key': 'employee.name', 'label': 'Employee full name'},
    {'key': 'employee.first_name', 'label': 'Employee first name'},
    {'key': 'employee.code', 'label': 'Employee code'},
    {'key': 'employee.title', 'label': 'Mr. / Ms. (from gender)'},
    {'key': 'employee.email', 'label': 'Work email'},
    {'key': 'employee.phone', 'label': 'Phone'},
    {'key': 'designation', 'label': 'Designation'},
    {'key': 'grade', 'label': 'Grade'},
    {'key': 'department', 'label': 'Department'},
    {'key': 'branch', 'label': 'Branch'},
    {'key': 'manager.name', 'label': 'Line manager'},
    {'key': 'employment_type', 'label': 'Employment type'},
    {'key': 'join_date', 'label': 'Joining date (BS)'},
    {'key': 'exit_date', 'label': 'Last working date (BS)'},
    {'key': 'probation_end', 'label': 'Probation end date (BS)'},
    {'key': 'service_period', 'label': 'Length of service'},
    {'key': 'salary.gross', 'label': 'Monthly salary (NPR)'},
    {'key': 'salary.gross_words', 'label': 'Monthly salary in words'},
    {'key': 'salary.annual', 'label': 'Annual salary (NPR)'},
    {'key': 'today', 'label': "Today's date (BS)"},
    {'key': 'company.name', 'label': 'Company name'},
    {'key': 'company.address', 'label': 'Company address'},
    {'key': 'company.pan', 'label': 'Company PAN'},
    {'key': 'signatory.name', 'label': 'Signatory name'},
    {'key': 'signatory.title', 'label': 'Signatory title'},
    {'key': 'ref_no', 'label': 'Letter reference number'},
    {'key': 'purpose', 'label': 'Purpose (from the request)'},
    {'key': 'addressed_to', 'label': 'Addressed to (from the request)'},
]
KEYS = {p['key'] for p in PLACEHOLDERS}
LABEL = {p['key']: p['label'] for p in PLACEHOLDERS}
TOKEN = re.compile(r'\{\{\s*([a-z_.]+)\s*\}\}')
_STRAY = re.compile(r'\{\{[^}\n]{0,30}|[^{\n]{0,30}\}\}')


def npt_today() -> str:
    return _npt_today().isoformat()


def unknown_placeholders(*texts: str) -> list[str]:
    """Placeholders used in a template that are not on the whitelist (a typo would otherwise print blank)."""
    bad: dict[str, None] = {}
    for t in texts:
        for m in TOKEN.finditer(t):
            if m[1] not in KEYS:
                bad[m[1]] = None
        # Stray braces such as "{{name" or "{{ Name }}" are almost always mistakes.
        stripped = TOKEN.sub('', t)
        stray = _STRAY.search(stripped)
        if stray:
            bad[stray[0].strip()] = None
    return list(bad)


def esc(s: str) -> str:
    """HTML-escape text, quotes included (the standard library's html.escape)."""
    return _html.escape(s, quote=True)


def _inline(s: str) -> str:
    return re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', s)


def _fill(text: str, values: dict, missing: dict, mark: bool) -> str:
    """Fill placeholders into escaped text; values have their markdown characters neutralised so they stay literal."""
    # Bold markers are resolved on the template text first, with placeholders swapped for inert sentinels.
    tokens: list[str] = []

    def sentinel(m):
        tokens.append(m[1])
        return f'\u0000{len(tokens) - 1}\u0000'

    html = _inline(esc(TOKEN.sub(sentinel, text)))

    def value(m):
        key = tokens[int(m[1])]
        v = values.get(key)
        if v is None or v == '':
            missing[key] = None
            return f'<span class="missing">[{esc(key)}]</span>' if mark else ''
        return esc(str(v)).replace('*', '&#42;')

    return re.sub(r'\u0000(\d+)\u0000', value, html)


_BULLET = re.compile(r'^\s*[-•]\s+')


def render_body(body: str, values: dict, *, mark: bool = False) -> dict:
    """Render the body to HTML paragraphs and lists. `mark` highlights missing values (preview only).
    Returns {'html', 'missing'}."""
    missing: dict[str, None] = {}
    clean = re.sub(r'\r\n?', '\n', body).replace('\u0000', '')
    blocks = [b.strip() for b in re.split(r'\n\s*\n', clean)]
    blocks = [b for b in blocks if b]
    out_blocks = []
    for b in blocks:
        lines = b.split('\n')
        if any(_BULLET.search(ln) for ln in lines):
            # Leading text lines become a paragraph, bullet lines a list.
            out: list[str] = []
            lst: list[str] = []
            para: list[str] = []
            for ln in lines:
                if _BULLET.search(ln):
                    if para:
                        out.append(f"<p>{'<br>'.join(para)}</p>")
                        para = []
                    lst.append(f'<li>{_fill(_BULLET.sub("", ln, count=1), values, missing, mark)}</li>')
                else:
                    if lst:
                        out.append(f"<ul>{''.join(lst)}</ul>")
                        lst = []
                    para.append(_fill(ln, values, missing, mark))
            if para:
                out.append(f"<p>{'<br>'.join(para)}</p>")
            if lst:
                out.append(f"<ul>{''.join(lst)}</ul>")
            out_blocks.append('\n'.join(out))
        else:
            out_blocks.append(f"<p>{'<br>'.join(_fill(ln, values, missing, mark) for ln in lines)}</p>")
    return {'html': '\n'.join(out_blocks), 'missing': list(missing)}


def render_subject(subject: str, values: dict, missing: dict, mark: bool = False) -> str:
    return _fill(subject.replace('\u0000', ''), values, missing, mark)


def subject_text(subject_html: str) -> str:
    """The subject as plain text (the register, notifications): tags dropped, entities decoded."""
    return _html.unescape(strip_tags(subject_html))


# ---------------------------------------------------------------- letterhead

_default_logo: list = []


def brand_logo() -> str | None:
    if _default_logo:
        return _default_logo[0]
    try:
        svg = (Path(settings.BASE_DIR) / 'static' / 'brand' / 'cv-wordmark-carbon.svg').read_bytes()
        _default_logo.append(f'data:image/svg+xml;base64,{base64.b64encode(svg).decode()}')
    except OSError:
        _default_logo.append(None)
    return _default_logo[0]


_LOGO_OK = re.compile(r'^data:image/(png|jpeg|svg\+xml);base64,[A-Za-z0-9+/=]+$')

_STYLE = """@page{size:A4;margin:18mm 20mm}
body{font-family:Manrope,ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13.5px;color:#17191e;margin:32px auto;max-width:760px;line-height:1.65;padding:0 28px;background:#fff}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;padding-bottom:14px;border-bottom:2px solid #12773a}
.head img{height:34px;display:block}.co{text-align:right;font-size:12px;color:#4b505a}.co b{display:block;font-size:15px;color:#17191e}
.meta{display:flex;justify-content:space-between;margin:22px 0 18px;font-size:12.5px;color:#4b505a}.meta b{color:#17191e;font-weight:600}
h1{font-size:15px;text-align:center;text-decoration:underline;text-underline-offset:4px;margin:8px 0 18px;font-weight:700}
p{margin:0 0 12px;text-align:justify}ul{margin:0 0 12px 18px;padding:0}li{margin:2px 0}
.sign{margin-top:46px}.sign .line{width:200px;border-top:1px solid #17191e;margin-bottom:6px}.sign b{display:block}
.foot{margin-top:40px;font-size:10.5px;color:#8a8f99;border-top:1px solid #e6e7ea;padding-top:8px}
.missing{background:#fff1c2;color:#8a5a00;border-radius:3px;padding:0 3px;font-family:ui-monospace,monospace;font-size:11.5px}
.wm{position:fixed;top:38%;left:0;right:0;text-align:center;font-size:110px;font-weight:800;color:rgba(200,30,30,.10);transform:rotate(-24deg);pointer-events:none;letter-spacing:.1em}
@media print{body{margin:0 auto;padding:0}}"""


def render_letter_html(*, company: dict, ref_no: str, date_bs: str, subject_html: str, body_html: str,
                       signatory: dict, draft: bool = False, voided: str | None = None) -> str:
    """A4 printable letter with the company letterhead, reference and date, subject, body and signature block."""
    logo_url = company.get('logoDataUrl')
    logo = logo_url if logo_url and _LOGO_OK.match(logo_url) else brand_logo()
    wm = 'VOID' if voided else 'DRAFT' if draft else None
    name = esc(company['name'])
    co = (f"<b>{name}</b>{esc(company['address']) if company.get('address') else ''}"
          f"{'<br>PAN ' + esc(company['pan']) if company.get('pan') else ''}")
    head_logo = f'<img src="{esc(logo)}" alt="">' if logo else '<span></span>'
    wm_html = f'<div class="wm">{wm}</div>' if wm else ''
    void_note = f' This letter was voided: {esc(voided)}.' if voided else ''
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{esc(ref_no)}</title>\n'
            f'<style>\n{_STYLE}\n</style></head><body>\n'
            f'{wm_html}\n'
            f'<div class="head">{head_logo}<div class="co">{co}</div></div>\n'
            f'<div class="meta"><div>Ref: <b>{esc(ref_no)}</b></div><div>Date: <b>{esc(date_bs)}</b></div></div>\n'
            f'<h1>{subject_html}</h1>\n'
            f'{body_html}\n'
            f"<div class=\"sign\"><div class=\"line\"></div><b>{esc(signatory.get('name') or '')}</b>"
            f"{esc(signatory.get('title') or '')}<br>{name}</div>\n"
            f'<div class="foot">Reference {esc(ref_no)}. Issued from Hajiri; the reference can be verified with {name} HR.'
            f'{void_note}</div>\n'
            f'</body></html>')


# ---------------------------------------------------------------- values for an employee

DEFAULT_LETTER_SETTINGS = {'signatoryName': None, 'signatoryTitle': 'Human Resources', 'refPrefix': 'HR'}


SETTINGS_KEY = 'documents.letters'


def letter_settings(company_id: str) -> dict:
    v = get_setting(company_id, SETTINGS_KEY)
    return {**DEFAULT_LETTER_SETTINGS, **(v if isinstance(v, dict) else {})}


def company_head(company_id: str) -> dict:
    """The letterhead: the company profile setting, falling back to the company's name."""
    name = Company.objects.for_company(company_id).values_list('name_en', flat=True).first()
    p = get_setting(company_id, 'company.profile')
    p = p if isinstance(p, dict) else {}
    return {'name': p.get('nameEn') or name or 'Company', 'address': p.get('address'), 'pan': p.get('pan'),
            'logoDataUrl': p.get('logoDataUrl')}


TYPE_LABEL = {'regular': 'regular', 'work_based': 'work-based', 'time_based': 'time-based', 'casual': 'casual',
              'part_time': 'part-time'}


def service_period(from_: str | None, to: str) -> str | None:
    if not from_:
        return None
    a = date.fromisoformat(from_)
    b = date.fromisoformat(to)
    if b < a:
        return None
    months = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day < a.day:
        months -= 1
    y, m = months // 12, months % 12
    parts = [p for p in (f"{y} year{'' if y == 1 else 's'}" if y else '', f"{m} month{'' if m == 1 else 's'}" if m else '') if p]
    return ' and '.join(parts) if parts else 'less than a month'


def bs_year_of(ad: str) -> int:
    try:
        return ad_to_bs(ad).year
    except Exception:  # noqa: BLE001 - outside the calendar table
        return int(ad[:4]) + 57


def _money(d: Decimal) -> str:
    return str(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def _covers(day: date) -> ExpressionWrapper:
    """True for a range row that contains `day` (ORDER BY (valid @> day) DESC puts it first)."""
    return ExpressionWrapper(Q(valid__contains=day), output_field=BooleanField())


def current_assignment(employee_id: str, today: date) -> dict:
    """The assignment in force today, else the latest one (open-ended first); {} when there is none."""
    return (EmployeeAssignment.objects.filter(employee_id=employee_id)
            .order_by(_covers(today).desc(), F('valid__endswith').desc(nulls_first=True))
            .values('employment_type', 'probation_end', dept=F('org_unit__name_en'), branch_name=F('branch__name_en'),
                    manager_name=F('manager__name_en'))
            .first()) or {}


def monthly_gross(employee_id: str, today: date) -> str | None:
    """The sum of the base amounts of the salary in force today (else the latest one), as '12345.00'."""
    amounts = (SalaryAssignment.objects.filter(employee_id=employee_id)
               .order_by(_covers(today).desc(), F('valid__startswith').desc())
               .values_list('base_amounts', flat=True).first())
    if amounts is None:
        return None
    t = Decimal(0)
    for v in (amounts or {}).values():
        try:
            t += Decimal(str(v))
        except (InvalidOperation, ValueError):
            pass  # skip non-numeric
    return _money(t) if t.is_finite() and t > 0 else None


def employee_letter_values(company_id: str, employee_id: str, *, ref_no: str | None = None, purpose: str | None = None,
                           addressed_to: str | None = None) -> dict | None:
    """Every placeholder value for an employee (company-scoped; None if the employee is not in the company)."""
    today_d = _npt_today()
    today = today_d.isoformat()
    e = (Employee.objects.for_company(company_id).filter(pk=employee_id)
         .values('name_en', 'emp_code', 'gender', 'email', 'phone', 'designation', 'grade', 'join_date', 'exit_date',
                 'probation_end').first())
    if not e:
        return None
    a = current_assignment(employee_id, today_d)
    gross = monthly_gross(employee_id, today_d)
    head = company_head(company_id)
    s = letter_settings(company_id)
    name = e['name_en']
    join, exit_ = _iso(e['join_date']), _iso(e['exit_date'])
    probation = _iso(a.get('probation_end') or e['probation_end'])
    employment_type = a.get('employment_type')
    end = exit_ if exit_ and exit_ < today else today
    return {
        'employee.name': name,
        'employee.first_name': re.split(r'\s+', name)[0],
        'employee.code': e['emp_code'],
        'employee.title': 'Mr.' if e['gender'] == 'male' else 'Ms.' if e['gender'] == 'female' else None,
        'employee.email': e['email'],
        'employee.phone': e['phone'],
        'designation': e['designation'],
        'grade': e['grade'],
        'department': a.get('dept'),
        'branch': a.get('branch_name'),
        'manager.name': a.get('manager_name'),
        'employment_type': TYPE_LABEL.get(employment_type, employment_type) if employment_type else None,
        'join_date': bs_label(join) if join else None,
        'exit_date': bs_label(exit_) if exit_ else None,
        'probation_end': bs_label(probation) if probation else None,
        'service_period': service_period(join, end),
        'salary.gross': f'NPR {format_npr(gross, "latin")}' if gross else None,
        'salary.gross_words': amount_in_words_en(gross) if gross else None,
        'salary.annual': f'NPR {format_npr(_money(Decimal(gross) * 12), "latin")}' if gross else None,
        'today': bs_label(today),
        'company.name': head['name'],
        'company.address': head['address'],
        'company.pan': head['pan'],
        'signatory.name': s.get('signatoryName'),
        'signatory.title': s.get('signatoryTitle'),
        'ref_no': ref_no,
        'purpose': purpose,
        'addressed_to': addressed_to,
    }


# ---------------------------------------------------------------- built-in templates

BUILTIN_TEMPLATES: list[dict] = [
    {'code': 'appointment', 'name': 'Appointment letter', 'requestable': False, 'subject': 'Letter of appointment', 'body':
     """Dear {{employee.title}} {{employee.name}},

We are pleased to appoint you as **{{designation}}** in the {{department}} department of {{company.name}}, effective from {{join_date}}.

The main terms of your appointment are:
- Employment type: {{employment_type}}
- Place of work: {{branch}}
- Reporting to: {{manager.name}}
- Monthly salary: {{salary.gross}} ({{salary.gross_words}})
- Probation period ending on {{probation_end}}

Your employment is governed by the Labour Act 2074, the Labour Rules 2075 and the company's policies as amended from time to time.

Please sign and return a copy of this letter as your acceptance. We look forward to working with you."""},
    {'code': 'confirmation', 'name': 'Confirmation letter', 'requestable': False, 'subject': 'Confirmation of employment', 'body':
     """Dear {{employee.title}} {{employee.name}},

Following a review of your performance during the probation period, we are pleased to confirm your employment as **{{designation}}** with {{company.name}}.

All other terms and conditions of your employment remain unchanged.

Congratulations, and thank you for your contribution so far."""},
    {'code': 'promotion', 'name': 'Promotion letter', 'requestable': False, 'subject': 'Promotion', 'body':
     """Dear {{employee.title}} {{employee.name}},

In recognition of your performance and contribution, we are pleased to promote you to the position of **{{designation}}** (grade {{grade}}) in the {{department}} department.

Your revised monthly salary will be {{salary.gross}}. All other terms of your employment remain unchanged.

Congratulations on this well-deserved promotion."""},
    {'code': 'transfer', 'name': 'Transfer letter', 'requestable': False, 'subject': 'Transfer', 'body':
     """Dear {{employee.title}} {{employee.name}},

You are hereby transferred to the {{department}} department at our {{branch}} office as **{{designation}}**, reporting to {{manager.name}}.

Please complete the handover of your current responsibilities and report to your new place of work as advised by HR. All other terms of your employment remain unchanged."""},
    {'code': 'salary_increment', 'name': 'Salary increment letter', 'requestable': False, 'subject': 'Revision of salary', 'body':
     """Dear {{employee.title}} {{employee.name}},

We are pleased to inform you that your monthly salary has been revised to **{{salary.gross}}** ({{salary.gross_words}}).

This revision reflects your contribution to {{company.name}}. All other terms of your employment remain unchanged."""},
    {'code': 'experience', 'name': 'Experience letter', 'requestable': True, 'subject': 'Experience certificate', 'body':
     """To whom it may concern,

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) worked with {{company.name}} as **{{designation}}** in the {{department}} department from {{join_date}} to {{exit_date}}, a period of {{service_period}}.

During this period we found {{employee.first_name}} sincere, hardworking and professional. We wish {{employee.first_name}} every success in the future."""},
    {'code': 'relieving', 'name': 'Relieving letter', 'requestable': False, 'subject': 'Relieving letter', 'body':
     """Dear {{employee.title}} {{employee.name}},

This is to confirm that you have been relieved of your duties as **{{designation}}** at {{company.name}} with effect from the close of business on {{exit_date}}, after completing the handover of your responsibilities.

We thank you for your service and wish you the very best."""},
    {'code': 'salary_certificate', 'name': 'Salary certificate', 'requestable': True, 'subject': 'Salary certificate', 'body':
     """To {{addressed_to}},

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) has been working with {{company.name}} as **{{designation}}** since {{join_date}}.

{{employee.first_name}}'s current monthly salary is {{salary.gross}} ({{salary.gross_words}}), an annual salary of {{salary.annual}}.

This certificate is issued on request for the purpose of {{purpose}}."""},
    {'code': 'to_whom_it_may_concern', 'name': 'To whom it may concern', 'requestable': True, 'subject': 'To whom it may concern', 'body':
     """To whom it may concern,

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) is a {{employment_type}} employee of {{company.name}}, working as **{{designation}}** in the {{department}} department since {{join_date}}.

This letter is issued on request for the purpose of {{purpose}}."""},
    {'code': 'noc', 'name': 'No objection certificate', 'requestable': True, 'subject': 'No objection certificate', 'body':
     """To {{addressed_to}},

This is to certify that {{employee.title}} {{employee.name}} (employee code {{employee.code}}) is working with {{company.name}} as **{{designation}}** since {{join_date}}.

The company has no objection to {{employee.first_name}}'s request for the purpose of {{purpose}}."""},
    {'code': 'warning', 'name': 'Warning letter', 'requestable': False, 'subject': 'Warning', 'body':
     """Dear {{employee.title}} {{employee.name}},

This letter is a formal warning regarding {{purpose}}.

Such conduct is not consistent with the company's code of conduct. You are advised to correct this immediately. Any repetition may lead to further disciplinary action under the Labour Act 2074 and the company's policies.

Please acknowledge receipt of this letter."""},
]

DEFAULT_CATEGORIES: list[dict] = [
    {'code': 'contract', 'name': 'Employment contract', 'employeeCanUpload': False, 'expiryRequired': False, 'visible': True},
    {'code': 'citizenship', 'name': 'Citizenship certificate', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'pan_card', 'name': 'PAN card', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'academic', 'name': 'Academic certificate', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'experience', 'name': 'Experience letter', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'medical', 'name': 'Medical certificate', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'photo', 'name': 'Photograph', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
    {'code': 'passport', 'name': 'Passport', 'employeeCanUpload': True, 'expiryRequired': True, 'visible': True},
    {'code': 'driving_licence', 'name': 'Driving licence', 'employeeCanUpload': True, 'expiryRequired': True, 'visible': True},
    {'code': 'other', 'name': 'Other', 'employeeCanUpload': True, 'expiryRequired': False, 'visible': True},
]


def seed_document_defaults(company_id: str) -> dict:
    """Idempotent per-company defaults: document categories, built-in letter templates and the letter-request workflow.
    Codes the company already has are left alone (HR may have renamed or switched them off). Returns how many rows
    were added."""
    have = set(DocumentCategory.objects.for_company(company_id).values_list('code', flat=True))
    cats = [DocumentCategory(company_id=company_id, code=c['code'], name=c['name'],
                             employee_can_upload=c['employeeCanUpload'], expiry_required=c['expiryRequired'],
                             visible_to_employee=c['visible'], sort_order=i)
            for i, c in enumerate(DEFAULT_CATEGORIES) if c['code'] not in have]
    DocumentCategory.objects.bulk_create(cats, ignore_conflicts=True)
    have = set(LetterTemplate.objects.for_company(company_id).values_list('code', flat=True))
    tpls = [LetterTemplate(company_id=company_id, code=t['code'], name=t['name'], subject=t['subject'], body=t['body'],
                           requestable=t['requestable'], is_builtin=True)
            for t in BUILTIN_TEMPLATES if t['code'] not in have]
    LetterTemplate.objects.bulk_create(tpls, ignore_conflicts=True)
    WorkflowDefinition.objects.get_or_create(company_id=company_id, object_type='letter_request',
                                             defaults={'steps': [{'role': 'hr'}]})
    return {'categories': len(cats), 'templates': len(tpls)}


def next_ref_no(company_id: str, prefix: str, today: str | None = None) -> str:
    """Next reference number for the company and BS year, e.g. HR/2083/0001. Call inside the issuing transaction: the
    increment keeps the counter row locked until it commits, so two letters never share a number."""
    year = bs_year_of(today or npt_today())
    with transaction.atomic():
        LetterSequence.objects.bulk_create([LetterSequence(company_id=company_id, bs_year=year, last_no=0)],
                                           ignore_conflicts=True)
        seq = LetterSequence.objects.for_company(company_id).filter(bs_year=year)
        seq.update(last_no=F('last_no') + 1)
        last_no = seq.values_list('last_no', flat=True).get()
    clean = re.sub(r'/+$', '', re.sub(r'[^A-Za-z0-9/_-]', '', prefix)) or 'HR'
    return f'{clean}/{year}/{str(last_no).zfill(4)}'


__all__ = ['PLACEHOLDERS', 'KEYS', 'LABEL', 'unknown_placeholders', 'esc', 'render_body', 'render_subject', 'subject_text',
           'render_letter_html', 'letter_settings', 'company_head', 'service_period', 'bs_year_of',
           'employee_letter_values', 'BUILTIN_TEMPLATES', 'DEFAULT_CATEGORIES', 'seed_document_defaults', 'next_ref_no',
           'DEFAULT_LETTER_SETTINGS']
