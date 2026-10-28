"""
Documents & letters pages and their HTMX actions:

- /me/profile/documents (MyDocumentsPage.tsx): company policies to read and acknowledge, my documents, letters from HR
  and letter requests.
- /employees/documents (DocumentsAdminPage.tsx, HR): to review, letters register, templates, policies, document types.
- /_/documents/employee/<id> (EmployeeDocumentsTab.tsx): the Documents tab of an employee's page, loaded over HTMX.

Rules live in services.py; forms.py reads the posts into the TS request bodies; views answer fragments, toasts and
refresh events. The JSON API for Bearer-token clients (the TS routes under /api/v1/documents) is in api.py.
"""
from __future__ import annotations

from datetime import date

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.errors import BadRequestError, HttpError
from hajiri.files.store import stored_file_response
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.format import fmt_date, today_npt
from hajiri.web.htmx import close_modal, refresh, toast, trigger

from . import forms, services
from .letters import LABEL, TOKEN

FEATURE = 'documents'

DOC_STATUS = {'pending': ('Awaiting HR check', 'warn'), 'verified': ('Verified', 'ok'), 'rejected': ('Not accepted', 'bad')}
REQ_STATUS = {'pending': ('Waiting for HR', 'warn'), 'approved': ('Being prepared', 'info'), 'issued': ('Issued', 'ok'),
              'rejected': ('Declined', 'bad'), 'cancelled': ('Cancelled', 'idle')}
SECTIONS = (('review', 'To review'), ('letters', 'Letters'), ('templates', 'Templates'), ('policies', 'Policies'),
            ('categories', 'Document types'))
AUDIENCES = (('all', 'Everyone'), ('branch', 'Selected branches'), ('department', 'Selected departments'))
NEW_TEMPLATE_BODY = 'Dear {{employee.title}} {{employee.name}},\n\n'
SHOWN_REQUESTS = ('pending', 'approved', 'rejected')


# ---------------------------------------------------------------- helpers

def _days_until(ad: str) -> int:
    return (date.fromisoformat(ad) - date.fromisoformat(today_npt())).days


def file_size(n: int) -> str:
    if n < 1024:
        return f'{n} B'
    if n < 1024 * 1024:
        return f'{round(n / 1024)} KB'
    return f'{n / 1024 / 1024:.1f} MB'


def expiry_info(date_iso: str | None) -> dict:
    """ExpiryText: "Expires 3 Magh 2083", orange within 30 days and red once expired."""
    if not date_iso:
        return {'text': 'No expiry', 'tone': 'muted', 'warn': False}
    d = _days_until(date_iso)
    tone = 'bad' if d < 0 else 'warn' if d <= 30 else 'muted'
    if d < 0:
        text = f'Expired {fmt_date(date_iso)}'
    elif d == 0:
        text = 'Expires today'
    else:
        text = f"Expires {fmt_date(date_iso)}{f' ({d} days)' if d <= 30 else ''}"
    return {'text': text, 'tone': tone, 'warn': d <= 30}


def _doc(d: dict) -> dict:
    label, tone = DOC_STATUS[d['status']]
    sub = ' · '.join(x for x in (d['category_name'] if d['category_name'] != d['title'] else None,
                                 f"version {d['version']}" if d['version'] > 1 else None) if x) or 'Document'
    return {**d, 'status_label': label, 'status_tone': tone, 'subline': sub, 'expiry': expiry_info(d['expiry_date']),
            'size': file_size(d['bytes']), 'is_pdf': d['mime'] == 'application/pdf'}


def _req(r: dict) -> dict:
    label, tone = REQ_STATUS[r['status']]
    return {**r, 'status_label': label, 'status_tone': tone, 'open': r['status'] in ('pending', 'approved'),
            'template_name_lower': r['template_name'].lower()}


def _done(request, message: str, *refresh_ids: str, close: bool = True, status: int = 204) -> HttpResponse:
    resp = HttpResponse(status=status)
    if close:
        close_modal(resp)
    for rid in refresh_ids:
        refresh(resp, rid)
    return toast(request, resp, message)


def _err_toast(message: str, title: str, status: int = 400) -> HttpResponse:
    r = HttpResponse(message, status=status, content_type='text/plain; charset=utf-8')
    r.headers['HX-Reswap'] = 'none'
    return trigger(r, 'hj:toast', {'message': message, 'tone': 'err', 'title': title})


def _data(request):
    return request.POST if request.method == 'POST' else request.GET


# ---------------------------------------------------------------- documents grid (EmployeeDocuments.tsx)

def _grid_ctx(request, mode: str, employee_id: str | None, history: bool) -> dict:
    user = request.hajiri_user
    hr = mode == 'hr' and has_role(user, 'hr')
    if hr and employee_id:
        docs = services.employee_documents(user, employee_id, {'history': '1' if history else '0'})
    else:
        hr, employee_id = False, user.employee_id
        docs = services.my_documents(user)
    q = f"mode={'hr' if hr else 'self'}" + (f'&employee_id={employee_id}' if hr else '')
    return {'docs': [_doc(d) for d in docs], 'hr': hr, 'employee_id': employee_id, 'history': history, 'grid_query': q}


@require_GET
@login_required
@require_feature(FEATURE)
def docs_grid(request):
    f = forms.GridQueryForm(request.GET)
    d = f.cleaned()
    ctx = _grid_ctx(request, d['mode'] or 'self', d['employee_id'], d['history'])
    return render(request, 'documents/partials/docs_grid.html', ctx)


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature(FEATURE)
def upload(request):
    user = request.hajiri_user
    f = forms.UploadForm(_data(request))
    hr = f.hr_mode(user)
    employee_id = (f.cleaned_data['employee_id'] if hr else None) or user.employee_id
    if request.method == 'POST':
        cats = {c['id']: c for c in services.list_categories(user)}
        cat = cats.get(f.cleaned_data['category_id'])
        body = f.document(hr=hr, category_name=cat['name'] if cat else None)
        if not employee_id:
            raise BadRequestError('only employees can upload documents')
        services.add_document(user, employee_id, body)
        return _done(request, 'Document saved.' if hr else 'Uploaded. HR will check it shortly.', 'docs-grid', 'doc-review')
    rid = f.cleaned_data['replaces']
    replaces = services.document_for(user, rid) if rid else None
    cats = [c for c in services.list_categories(user) if c['active'] and (hr or c['employee_can_upload'])]
    cat_map = {c['id']: {'name': c['name'], 'expiry': c['expiry_required']} for c in cats}
    return render(request, 'documents/partials/upload_form.html', {
        'hr': hr, 'employee_id': employee_id, 'categories': cats, 'cat_map': cat_map, 'replaces': replaces,
        'mode': 'hr' if hr else 'self'})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def doc_edit(request, doc_id):
    user = request.hajiri_user
    d = services.hr_document(user, str(doc_id))
    if request.method == 'POST':
        services.update_document(user, str(doc_id), forms.DocumentEditForm(request.POST).patch())
        return _done(request, 'Document updated.', 'docs-grid', 'doc-review')
    return render(request, 'documents/partials/doc_edit.html', {'d': d})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def verify(request, doc_id):
    user = request.hajiri_user
    d = services.hr_document(user, str(doc_id))
    services.verify_document(user, str(doc_id), {'decision': 'verify'})
    return _done(request, f"{d['title']} verified.", 'docs-grid', 'doc-review', close=False)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def reject(request, doc_id):
    user = request.hajiri_user
    f = forms.RejectForm(_data(request))
    from_review = f.from_review()
    d = services.hr_document(user, str(doc_id))
    if request.method == 'POST':
        services.verify_document(user, str(doc_id), f.body())
        msg = 'Sent back to the employee.' if from_review else 'The employee has been asked to upload it again.'
        return _done(request, msg, 'docs-grid', 'doc-review')
    return render(request, 'documents/partials/reject_form.html',
                  {'d': d, 'from_review': from_review, 'name': d['employee_name'] if from_review else None})


@require_POST
@login_required
@require_feature(FEATURE)
def doc_delete(request, doc_id):
    services.delete_document(request.hajiri_user, str(doc_id))
    return _done(request, 'Document removed.', 'docs-grid', 'doc-review', close=False)


@require_GET
@login_required
@require_feature(FEATURE)
def doc_file(request, doc_id):
    f = services.document_file(request.hajiri_user, str(doc_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')


# ---------------------------------------------------------------- letters (Letters.tsx)

def _letters_ctx(request, *, hr: bool, employee_id: str | None, show_employee: bool, term: str = '') -> dict:
    query = {}
    if employee_id:
        query['employeeId'] = employee_id
    if term:
        query['q'] = term
    letters = services.list_letters(request.hajiri_user, query)
    qs = f"scope={'hr' if hr else 'self'}" + (f'&employee_id={employee_id}' if employee_id else '') + \
         ('&show_employee=1' if show_employee else '')
    return {'letters': letters, 'hr': hr, 'show_employee': show_employee, 'letters_query': qs, 'term': term,
            'empty_hint': ('Issue an appointment, experience or salary letter from a template.' if hr
                           else 'Letters HR issues to you, such as a salary certificate, appear here.')}


@require_GET
@login_required
@require_feature(FEATURE)
def letters(request):
    f = forms.LettersQueryForm(request.GET)
    d = f.cleaned()
    hr = d['scope'] == 'hr' and has_role(request.hajiri_user, 'hr')
    ctx = _letters_ctx(request, hr=hr, employee_id=d['employee_id'] if hr else None,
                       show_employee=hr and d['show_employee'], term=f.term())
    return render(request, 'documents/partials/letters_table.html', ctx)


def _template_info(templates: list[dict]) -> dict:
    out = {}
    for t in templates:
        text = (t.get('body') or '') + t['subject']
        keys = {m[1] for m in TOKEN.finditer(text)} if t.get('body') else set()
        out[t['id']] = {'visible': t.get('visible_to_employee') is not False, 'purpose': 'purpose' in keys,
                        'addressee': 'addressed_to' in keys}
    return out


def _request_of(user, f: forms.IssueForm) -> dict | None:
    rid = f.cleaned()['request_id']
    return services.get_request(user, rid) if rid else None


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def issue(request):
    """IssueLetterDrawer: pick the employee (unless fixed), the template and request details, preview, issue."""
    user = request.hajiri_user
    f = forms.IssueForm(_data(request))
    req = _request_of(user, f)
    if request.method == 'POST':
        try:
            r = services.issue_letter(user, f.issue(req))
        except BadRequestError as e:
            if isinstance(e.details, dict) and e.details.get('code') == 'missing_values':
                return _err_toast(e.message, 'Some details are missing')
            raise
        resp = render(request, 'documents/partials/issued.html', {'letter': r})
        for rid in ('doc-letters', 'doc-review', 'my-requests'):
            refresh(resp, rid)
        return toast(request, resp, f"Letter {r['refNo']} issued.")
    templates = [t for t in services.list_templates(user) if t.get('active') is not False]
    fixed_employee = req['employee_id'] if req else f.ids(None)[0]
    return render(request, 'documents/partials/issue_form.html', {
        'templates': templates, 'tpl_info': _template_info(templates), 'req': req, 'fixed_employee': fixed_employee,
        'employee_fixed': bool(req or fixed_employee)})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def issue_preview(request):
    """The live preview in the issue drawer, with missing details highlighted and the Issue button."""
    user = request.hajiri_user
    f = forms.IssueForm(request.POST)
    req = _request_of(user, f)
    emp, tpl = f.ids(req)
    d = f.cleaned()
    ctx = {'ready': bool(emp and tpl), 'allow_missing': d['allow_missing'], 'preview': None, 'missing': []}
    if emp and tpl:
        try:
            out = services.preview_letter(user, {'employeeId': emp, 'templateId': tpl, 'purpose': d['purpose'],
                                                 'addressedTo': d['addressed_to']})
        except HttpError as e:
            return _err_toast(e.message, 'Preview failed', status=e.status)
        ctx['preview'] = out['html']
        ctx['missing'] = out['missing']
        ctx['missing_labels'] = ', '.join(LABEL.get(k, k).lower() if k in LABEL else k for k in out['missing'])
    ctx['can_issue'] = ctx['ready'] and (not ctx['missing'] or ctx['allow_missing'])
    return render(request, 'documents/partials/issue_preview.html', ctx)


def letter_response(html: str) -> HttpResponse:
    """A printable letter: its own strict policy (inline styles and data: images only) and never cached."""
    r = HttpResponse(html, content_type='text/html; charset=utf-8')
    r['Cache-Control'] = 'private, no-store'
    r['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'self' data:"
    return r


@require_GET
@login_required
@require_feature(FEATURE)
def letter_html(request, letter_id):
    return letter_response(services.letter_html(request.hajiri_user, str(letter_id)))


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def void(request, letter_id):
    user = request.hajiri_user
    letter = services.letter_ref(user, str(letter_id))
    if request.method == 'POST':
        services.void_letter(user, str(letter_id), {'reason': forms.VoidForm(request.POST).cleaned()['reason']})
        return _done(request, f"{letter['ref_no']} voided.", 'doc-letters')
    return render(request, 'documents/partials/void_form.html', {'letter': letter})


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature(FEATURE)
def request_new(request):
    """RequestLetterDrawer: an employee asks HR for a letter such as a salary certificate."""
    user = request.hajiri_user
    if request.method == 'POST':
        services.request_letter(user, forms.LetterRequestForm(request.POST).letter_request())
        return _done(request, 'Request sent to HR.', 'my-requests')
    templates = services.list_templates(user)
    if has_role(user, 'hr'):
        templates = [t for t in templates if t.get('active') and t.get('requestable')]
    return render(request, 'documents/partials/request_form.html', {'templates': templates})


@require_POST
@login_required
@require_feature(FEATURE)
def request_cancel(request, request_id):
    services.cancel_request(request.hajiri_user, str(request_id))
    return _done(request, 'Request cancelled.', 'my-requests', 'doc-review', close=False)


def _my_requests_ctx(request) -> dict:
    reqs = [_req(r) for r in services.list_requests(request.hajiri_user)]
    return {'requests': [r for r in reqs if r['status'] in SHOWN_REQUESTS][:6]}


@require_GET
@login_required
@require_feature(FEATURE)
def my_requests(request):
    return render(request, 'documents/partials/my_requests.html', _my_requests_ctx(request))


# ---------------------------------------------------------------- policies

def _my_policies_ctx(request) -> dict:
    ps = services.list_policies(request.hajiri_user)
    ps = sorted(ps, key=lambda p: int(bool(p['my_ack_at']) or not p['requires_ack']))
    pending = sum(1 for p in ps if p['requires_ack'] and not p['my_ack_at'])
    return {'policies': ps, 'pending': pending}


@require_GET
@login_required
@require_feature(FEATURE)
def my_policies(request):
    return render(request, 'documents/partials/my_policies.html', _my_policies_ctx(request))


@require_POST
@login_required
@require_feature(FEATURE)
def acknowledge(request, policy_id):
    user = request.hajiri_user
    p = services.get_policy(user, str(policy_id))
    services.acknowledge_policy(user, str(policy_id))
    return _done(request, f"Thanks, {p['title']} acknowledged.", 'my-policies', close=False)


@require_GET
@login_required
@require_feature(FEATURE)
def policy_file(request, policy_id):
    f = services.policy_file(request.hajiri_user, str(policy_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')


def _audience_label(p: dict, branch_names: dict, unit_names: dict) -> str:
    if not p.get('audience') or p['audience'] == 'all':
        return 'Everyone'
    names_of = branch_names if p['audience'] == 'branch' else unit_names
    names = [names_of.get(i, '') for i in (p.get('audience_ids') or [])]
    names = [n for n in names if n]
    return ', '.join(names) or ('Selected branches' if p['audience'] == 'branch' else 'Selected departments')


def _policies_ctx(request, archived: bool) -> dict:
    user = request.hajiri_user
    b = {x['id']: x['name_en'] for x in services.branches(user)}
    u = {x['id']: x['name_en'] for x in services.org_units(user)}
    ps = []
    for p in services.list_policies(user, {'archived': '1' if archived else '0'}):
        size = p.get('audience_size') or 0
        ps.append({**p, 'audience_label': _audience_label(p, b, u),
                   'pct': round((p.get('acks') or 0) / size * 100) if size else 0})
    return {'policies': ps, 'archived': archived}


def _archived(request) -> bool:
    return forms.ArchivedForm(request.GET).cleaned()['archived']


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def policies(request):
    return render(request, 'documents/partials/policies.html', _policies_ctx(request, _archived(request)))


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def policy_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        services.publish_policy(user, forms.PolicyForm(request.POST).policy())
        return _done(request, 'Published. Everyone it applies to has been notified.', 'doc-policies')
    return render(request, 'documents/partials/policy_form.html', {
        'audiences': AUDIENCES, 'branches': services.branches(user), 'units': services.org_units(user)})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def policy_archive(request, policy_id):
    services.update_policy(request.hajiri_user, str(policy_id), {'archived': True})
    return _done(request, 'Archived.', 'doc-policies', close=False)


def _acks_page(request, policy_id) -> HttpResponse:
    user = request.hajiri_user
    p = services.get_policy(user, str(policy_id))
    emps = services.policy_acks(user, str(policy_id))
    return render(request, 'documents/partials/policy_acks.html', {
        'p': p, 'employees': emps, 'acked': sum(1 for e in emps if e['acknowledged_at'])})


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def policy_acks(request, policy_id):
    return _acks_page(request, policy_id)


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def policy_remind(request, policy_id):
    n = services.remind_policy(request.hajiri_user, str(policy_id))['sent']
    msg = f"Reminder sent to {n} {'person' if n == 1 else 'people'}." if n else 'Everyone has acknowledged it.'
    return toast(request, _acks_page(request, policy_id), msg)


# ---------------------------------------------------------------- categories

def _categories(user) -> list[dict]:
    return [{**c, 'toggles': [('employee_can_upload', 'Employees can upload', c['employee_can_upload']),
                              ('expiry_required', 'Expiry required', c['expiry_required']),
                              ('visible_to_employee', 'Visible to employee', c['visible_to_employee']),
                              ('active', 'Active', c['active'])]}
            for c in services.list_categories(user)]


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def categories(request):
    return render(request, 'documents/partials/categories.html', {'categories': _categories(request.hajiri_user)})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def category_new(request):
    if request.method == 'POST':
        services.create_category(request.hajiri_user, forms.CategoryForm(request.POST).category())
        return _done(request, 'Document type added.', 'doc-categories')
    return render(request, 'documents/partials/category_form.html')


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def category_toggle(request, category_id):
    services.update_category(request.hajiri_user, str(category_id), forms.CategoryToggleForm(request.POST).patch())
    return refresh(HttpResponse(status=204), 'doc-categories')


# ---------------------------------------------------------------- templates and letter settings

@require_GET
@require_role('hr')
@require_feature(FEATURE)
def templates(request):
    user = request.hajiri_user
    return render(request, 'documents/partials/templates.html', {
        'templates': services.list_templates(user), 'settings': services.get_letter_settings(user)})


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def letter_settings(request):
    services.save_letter_settings(request.hajiri_user, forms.LetterSettingsForm(request.POST).settings())
    return _done(request, 'Letter settings saved.', 'doc-templates', close=False)


def _template_form(request, t: dict | None):
    form = ({'id': t['id'], 'code': t['code'], 'name': t['name'], 'subject': t['subject'], 'body': t.get('body') or '',
             'requestable': bool(t.get('requestable')), 'visible': t.get('visible_to_employee') is not False,
             'active': t.get('active') is not False}
            if t else {'id': None, 'code': '', 'name': '', 'subject': '', 'body': NEW_TEMPLATE_BODY, 'requestable': False,
                       'visible': True, 'active': True})
    return render(request, 'documents/partials/template_form.html', {
        'form': form, 'placeholders': services.PLACEHOLDERS})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def template_new(request):
    if request.method == 'POST':
        services.create_template(request.hajiri_user, forms.TemplateForm(request.POST).template())
        return _done(request, 'Template saved.', 'doc-templates')
    return _template_form(request, None)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature(FEATURE)
def template_edit(request, template_id):
    user = request.hajiri_user
    t = services.get_template(user, str(template_id))
    if request.method == 'POST':
        services.update_template(user, str(template_id), forms.TemplateForm(request.POST).patch())
        return _done(request, 'Template saved.', 'doc-templates')
    return _template_form(request, t)


@require_POST
@require_role('hr')
@require_feature(FEATURE)
def template_preview(request):
    """TemplateEditor's live preview: the letter for the chosen employee, with unknown and missing placeholders."""
    d = forms.TemplatePreviewForm(request.POST).cleaned()
    emp, subject, body = d['preview_employee_id'], d['subject'], d['body']
    preview = None
    if emp and subject.strip() and body.strip():
        try:
            preview = services.preview_letter(request.hajiri_user, {
                'employeeId': emp, 'subject': subject, 'body': body, 'purpose': 'the stated purpose',
                'addressedTo': 'The Recipient'})
        except HttpError:
            preview = None
    return render(request, 'documents/partials/template_preview.html', {'preview': preview})


# ---------------------------------------------------------------- HR review (DocumentsAdminPage ReviewSection)

def _review_ctx(request) -> dict:
    user = request.hajiri_user
    q = services.queue(user, {'days': 30})
    reqs = [_req(r) for r in services.list_requests(user, {'status': 'open'})]
    pending = [_doc(d) for d in q['pending']]
    expiring = [_doc(d) for d in q['expiring']]
    expired = sum(1 for d in expiring if (d.get('days_left') or 0) < 0)
    stats = [
        {'label': 'Documents to verify', 'value': len(pending), **({'tone': 'warn'} if pending else {})},
        {'label': 'Letter requests', 'value': len(reqs), **({'tone': 'warn'} if reqs else {})},
        {'label': 'Expiring in 30 days', 'value': len(expiring) - expired},
        {'label': 'Expired', 'value': expired, **({'tone': 'bad'} if expired else {})},
    ]
    return {'stats': stats, 'pending': pending, 'expiring': expiring, 'requests': reqs}


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def review(request):
    return render(request, 'documents/partials/review.html', _review_ctx(request))


# ---------------------------------------------------------------- pages

@require_GET
@require_role('hr')
@require_feature(FEATURE)
def admin(request):
    """Employees → Documents & letters (DocumentsAdminPage.tsx)."""
    section = request.GET.get('section') or 'review'
    if section not in dict(SECTIONS):
        section = 'review'
    ctx = {'section': section, 'sections': SECTIONS}
    if section == 'review':
        ctx.update(_review_ctx(request))
    elif section == 'letters':
        term = forms.LettersQueryForm(request.GET).term()
        ctx.update(_letters_ctx(request, hr=True, employee_id=None, show_employee=True, term=term))
    elif section == 'templates':
        ctx.update({'templates': services.list_templates(request.hajiri_user),
                    'settings': services.get_letter_settings(request.hajiri_user)})
    elif section == 'policies':
        ctx.update(_policies_ctx(request, _archived(request)))
    else:
        ctx['categories'] = _categories(request.hajiri_user)
    return render(request, 'documents/admin.html', ctx)


@require_GET
@login_required
@require_feature(FEATURE)
def mine(request):
    """Profile → Documents & letters (MyDocumentsPage.tsx)."""
    ctx = {**_my_policies_ctx(request), **_my_requests_ctx(request), **_grid_ctx(request, 'self', None, False),
           **_letters_ctx(request, hr=False, employee_id=None, show_employee=False)}
    return render(request, 'documents/mine.html', ctx)


@require_GET
@require_role('hr')
@require_feature(FEATURE)
def employee_tab(request, employee_id):
    """The Documents tab of an employee's page (EmployeeDocumentsTab.tsx), loaded with hx-get into the tab panel."""
    eid = str(employee_id)
    history = forms.GridQueryForm(request.GET).cleaned()['history']
    ctx = {**_grid_ctx(request, 'hr', eid, history),
           **_letters_ctx(request, hr=True, employee_id=eid, show_employee=False)}
    return render(request, 'documents/partials/employee_tab.html', ctx)
