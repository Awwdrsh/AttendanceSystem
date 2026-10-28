"""
People pages (apps/web/src/pages/people, components/people, employee/ProfilePage, employee/MyDataPage): the directory
and org chart for everyone, HR's org structure and change requests, the profile sections, career timeline and field
history used on /me/profile and the employee page. HTMX fragments live under /_/people/...
"""
from __future__ import annotations

import time

from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from django.db.models import BooleanField, ExpressionWrapper, Q

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError, ForbiddenError, HttpError, NotFoundError
from hajiri.db.models import BiometricTemplate, Employee, WorkflowInstance
from hajiri.web.decorators import login_required, require_role
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast
from hajiri.web.nav import url_of

from . import service as svc
from .career import CAREER_LABEL  # noqa: F401
from .forms import CareerForm, DirectoryForm, PolicyForm, RejectForm, SectionForm, StructureForm
from .profile import SECTION_LABEL, SENSITIVE_SECTIONS

ORDER = ['personal', 'contact', 'emergency', 'family', 'ids', 'bank', 'education', 'experience', 'skills']
SECRET = {'pan', 'bankAccount', 'citizenshipNo', 'passportNo', 'licenceNo'}
GENDER = [('male', 'Male'), ('female', 'Female'), ('other', 'Other')]
MARITAL = [('single', 'Single'), ('couple', 'Married')]
BLOOD = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-']
PROVINCES = ['Koshi', 'Madhesh', 'Bagmati', 'Gandaki', 'Lumbini', 'Karnali', 'Sudurpashchim']
RELATIONS = [('spouse', 'Spouse'), ('father', 'Father'), ('mother', 'Mother'), ('son', 'Son'), ('daughter', 'Daughter'),
             ('brother', 'Brother'), ('sister', 'Sister'), ('father_in_law', 'Father-in-law'), ('mother_in_law', 'Mother-in-law'),
             ('grandfather', 'Grandfather'), ('grandmother', 'Grandmother'), ('other', 'Other')]
LEVELS = [('see', 'SEE / SLC'), ('plus_two', '+2 / Intermediate'), ('diploma', 'Diploma'), ('bachelor', "Bachelor's"),
          ('master', "Master's"), ('mphil', 'MPhil'), ('phd', 'PhD'), ('other', 'Other')]
LANGUAGES = ['Nepali', 'English', 'Hindi', 'Maithili', 'Newari', 'Bhojpuri', 'Tamang']
SOURCE = {'hr_edit': 'HR edit', 'change_request': 'Approved request', 'career_event': 'Career change', 'import': 'Import', 'system': 'System'}
REQ_STATUS = {'pending': ('Waiting', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Not approved', 'bad'), 'cancelled': ('Cancelled', 'idle')}
MY_REQ_STATUS = {'pending': ('Waiting for HR', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Not approved', 'bad'), 'cancelled': ('Cancelled', 'idle')}
CAREER_KINDS_FORM = [
    ('promotion', 'Promotion', 'New designation and/or grade.'),
    ('confirmation', 'Confirmation', 'Probation completed; employment confirmed.'),
    ('transfer', 'Transfer', 'Move to another branch or department.'),
    ('designation_change', 'Designation change', 'Change of title without a promotion.'),
    ('grade_change', 'Grade change', 'Change of grade only.'),
    ('manager_change', 'Manager change', 'Report to someone else.'),
    ('employment_type_change', 'Employment type change', 'E.g. from contract to regular.'),
    ('salary_revision', 'Salary revision note', 'A record for the timeline. Update the amounts under Payroll, Employee salaries.'),
    ('separation', 'Separation', 'The person leaves on the last working day. Sign-in stops the day after.'),
    ('rehire', 'Rejoined', 'Bring back someone who had left.'),
    ('note', 'Note', 'Anything else worth keeping on the timeline.'),
]
EMPLOYMENT = [('regular', 'Regular'), ('work_based', 'Work-based'), ('time_based', 'Time-based'), ('casual', 'Casual'), ('part_time', 'Part-time')]
SEPARATION = [('resignation', 'Resignation'), ('termination', 'Termination'), ('retirement', 'Retirement'), ('contract_end', 'End of contract'),
              ('death', 'Death'), ('other', 'Other')]
KIND_ICON = {'hire': 'user-plus', 'rehire': 'user-plus', 'promotion': 'trending-up', 'confirmation': 'circle-check', 'transfer': 'arrows-exchange',
             'deputation': 'arrows-exchange', 'acting': 'briefcase', 'separation': 'door-exit', 'note': 'note'}


def _ok(request, message: str, *refreshes: str, close: bool = True, tone: str = 'ok', title: str | None = None) -> HttpResponse:
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message, tone=tone, title=title)


def failed(request, exc: HttpError, title: str) -> HttpResponse:
    """toast.err(e, title) of the TS page: the error with the page's own title (the middleware says 'Not saved')."""
    from hajiri.web.middleware import error_message
    if exc.status == 401 or not is_htmx(request):
        raise exc
    msg = error_message(exc)
    resp = HttpResponse(msg, status=exc.status, content_type='text/plain; charset=utf-8')
    resp.headers['HX-Reswap'] = 'none'
    return toast(request, resp, msg, tone='err', title=title)


def square_photo(upload):
    """A profile photo the page script did not shrink becomes a 320 px square JPEG (Pillow); anything Pillow cannot
    read goes to the file store as sent, which checks the type itself."""
    from django.core.files.uploadedfile import SimpleUploadedFile

    from hajiri.core.images import image_size, square_jpeg
    if upload is None:
        return None
    data = upload.read()
    upload.seek(0)
    try:
        if image_size(data) == (320, 320) and (upload.content_type or '') == 'image/jpeg':
            return upload
        small = square_jpeg(data)
    except BadRequestError:
        return upload
    name = (upload.name or 'photo').rsplit('.', 1)[0] + '.jpg'
    return SimpleUploadedFile(name, small, content_type='image/jpeg')


def _label(opts, v) -> str:
    return next((lbl for val, lbl in opts if val == v), v) if v else '—'


# ================================================================ directory and org chart

@require_GET
@login_required
def directory(request):
    user = request.hajiri_user
    q = DirectoryForm(request.GET).query()
    people = svc.directory(user, {k: v for k, v in q.items() if v})
    filtered = any(q.values())
    ctx = {'people': people, 'filtered': filtered, 'q': q}
    if is_htmx(request) and request.headers.get('HX-Target') == 'people-grid':
        return render(request, 'people/partials/directory_grid.html', ctx)
    st = svc.structure(user)
    s = svc.summary(user)
    ctx.update({'units': st['units'], 'branches': st['branches'], 'stats': [
        {'label': 'People', 'value': s['active']}, {'label': 'Departments and teams', 'value': len(st['units'])},
        {'label': 'Branches', 'value': len(st['branches'])}, {'label': 'Joined in the last 30 days', 'value': s['joinedLast30Days']}]})
    return render(request, 'people/directory.html', ctx)


def _tree_rows(tree: dict) -> list[dict]:
    """Depth-first rows for the org chart; Alpine shows a row when every ancestor is expanded."""
    out = []

    def walk(node, depth, ancestors):
        kids = tree['children'].get(node['id'], [])
        out.append({**node, 'depth': depth, 'indent': depth * 22, 'ancestors': ancestors, 'has_kids': bool(kids),
                    'size': tree['size'].get(node['id'], 0)})
        for k in kids:
            walk(k, depth + 1, ancestors + [node['id']])
    for r in tree['roots']:
        walk(r, 0, [])
    return out


@require_GET
@login_required
def org_chart(request):
    nodes = svc.org_chart(request.hajiri_user)
    tree = svc.build_tree(nodes)
    rows_ = _tree_rows(tree)
    # Start with the top level open (roots that have a team).
    defaults = [r['id'] for r in tree['roots'] if r['id'] in tree['children']]
    return render(request, 'people/org_chart.html', {
        'rows': rows_, 'empty': not tree['roots'], 'defaults': defaults, 'all_ids': list(tree['children'].keys()),
        'search_index': {r['id']: f"{r['name_en']} {r.get('designation') or ''}".lower() for r in rows_},
        'parents': tree['parent'],
    })


@require_GET
@login_required
def card(request, id):
    data = svc.card(request.hajiri_user, str(id))
    return render(request, 'people/partials/card.html', {**data, 'is_hr': has_role(request.hajiri_user, 'hr')})


@require_GET
@login_required
def avatar(request, id):
    from hajiri.files.store import stored_file_response
    f = svc.avatar_file(request.hajiri_user, str(id))
    resp = stored_file_response(f)
    resp['Cache-Control'] = 'private, max-age=300'
    return resp


def _avatar_change(request, employee_id: str, self_: bool) -> HttpResponse:
    from hajiri.files.service import upload_file
    user = request.hajiri_user
    if request.POST.get('remove'):
        svc.set_my_avatar(user, None) if self_ else svc.set_employee_avatar(user, employee_id, None)
        msg = 'Photo removed.'
    else:
        if self_ and not user.employee_id:
            raise ForbiddenError('only employees have a profile photo')
        if not self_ and not has_role(user, 'hr'):
            raise ForbiddenError('requires role hr')
        try:
            f = upload_file(user, 'avatar', square_photo(request.FILES.get('file')))
            svc.set_my_avatar(user, {'fileId': f['id']}) if self_ else svc.set_employee_avatar(user, employee_id, {'fileId': f['id']})
        except HttpError as exc:
            return failed(request, exc, 'Photo not saved')
        msg = 'Photo updated.'
    e = (Employee.objects.for_company(company_id_of(user)).filter(pk=employee_id)
         .annotate(has_avatar=ExpressionWrapper(Q(avatar_file__isnull=False), output_field=BooleanField()))
         .values('id', 'name_en', 'has_avatar').get())
    resp = render(request, 'people/partials/avatar_editor.html', {'p': {'id': str(e['id']), 'name_en': e['name_en'], 'has_avatar': e['has_avatar']},
                                                                    'own': self_, 'v': str(int(time.time()))})
    return toast(request, resp, msg)


@require_GET
@require_role('hr')
def avatar_editor(request, id):
    """EmployeeProfilePanel.tsx: HR's photo editor for one employee."""
    e = svc.employee_in(company_id_of(request.hajiri_user), str(id))
    p = {'id': e['id'], 'name_en': e['name_en'], 'has_avatar': bool(e['avatar_file_id'])}
    return render(request, 'people/partials/avatar_editor.html', {'p': p, 'own': False})


@require_POST
@login_required
def my_avatar(request):
    user = request.hajiri_user
    if not user.employee_id:
        raise ForbiddenError('only employees have a profile photo')
    return _avatar_change(request, user.employee_id, True)


@require_POST
@require_role('hr')
def employee_avatar(request, id):
    svc.employee_in(company_id_of(request.hajiri_user), str(id))
    return _avatar_change(request, str(id), False)


# ================================================================ profile sections (self and HR)

def address_text(a) -> str:
    if not a:
        return '—'
    bits = [a.get('street'), f"Ward {a['ward']}" if a.get('ward') else None, a.get('municipality'), a.get('district'),
            f"{a['province']} Province" if a.get('province') else None]
    return ', '.join(str(b) for b in bits if b) or '—'


def _sections_ctx(request, employee_id: str, mode: str, reveal: bool = False) -> dict:
    data = svc.profile(request.hajiri_user, employee_id, reveal=reveal)
    p = data['profile']
    pending = {x['section'] for x in data['pending']}
    views = []
    for s in ORDER:
        views.append({'key': s, 'label': SECTION_LABEL[s], 'pending': s in pending, 'data': p[s]})
    return {'employee_id': employee_id, 'mode': mode, 'reveal': reveal, 'sections': views, 'p': p,
            'perm_addr': address_text(p['contact']['permanentAddress']), 'temp_addr': address_text(p['contact']['temporaryAddress']),
            'gender': _label(GENDER, p['personal']['gender']), 'marital': _label(MARITAL, p['personal']['maritalStatus']),
            'relations': dict(RELATIONS), 'levels': dict(LEVELS)}


@require_GET
@login_required
def sections(request, id):
    mode = 'hr' if request.GET.get('mode') == 'hr' else 'self'
    if mode == 'hr' and not has_role(request.hajiri_user, 'hr'):
        raise ForbiddenError('requires role hr')
    ctx = _sections_ctx(request, str(id), mode, reveal=request.GET.get('reveal') == '1' and mode == 'hr')
    return render(request, 'people/partials/sections.html', ctx)


@require_http_methods(['GET', 'POST'])
@login_required
def section_edit(request, id, section):
    user = request.hajiri_user
    id = str(id)
    mode = 'hr' if (request.GET.get('mode') or request.POST.get('mode')) == 'hr' else 'self'
    if section not in SECTION_LABEL:
        raise NotFoundError(f'unknown profile section {section}')
    if mode == 'hr' and not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    if mode == 'self' and user.employee_id != id:
        raise ForbiddenError('only employees have a profile to change')
    if request.method == 'POST':
        form = SectionForm(section, request.POST)
        if mode == 'hr':
            value = form.value()
            r = svc.save_section(user, id, section, value)
            if r['pending']:
                return _ok(request, 'Saved for approval. A different HR person must approve this change before it applies.', 'profile-sections',
                           tone='info', title='Waiting for a second approval')
            n = len(r['changes'])
            return _ok(request, f"{n} {'change' if n == 1 else 'changes'} saved." if n else 'Nothing changed.', 'profile-sections', 'field-history')
        svc.request_change(user, form.change_request())
        return _ok(request, 'HR will review your change. You will be notified when it is decided.', 'profile-sections', 'my-requests',
                   title='Change requested')
    data = svc.profile(user, id)
    p = data['profile']
    value = dict(p[section])
    masked = {k: value.get(k) for k in SECRET if k in value}
    for k in list(value):
        if k in SECRET:
            value[k] = ''
    rows_ = {'emergency': value.get('contacts'), 'family': value.get('members'), 'education': value.get('items'),
             'experience': value.get('items')}.get(section)
    title = f"{'Edit' if mode == 'hr' else 'Request a change:'} {SECTION_LABEL[section].lower()}"
    return render(request, 'people/partials/section_form.html', {
        'employee_id': id, 'section': section, 'mode': mode, 'v': value, 'masked': masked, 'rows': rows_ or [], 'title': title,
        'gender': GENDER, 'marital': MARITAL, 'blood': BLOOD, 'provinces': PROVINCES, 'relations': RELATIONS, 'levels': LEVELS,
        'languages': LANGUAGES, 'perm': p['contact']['permanentAddress'] or {}, 'temp': p['contact']['temporaryAddress'] or {},
        'skills_text': ', '.join(p['skills']['skills']), 'languages_text': ', '.join(p['skills']['languages']),
        'slots': _slots(section, rows_ or []), 'shown': len(rows_ or []),
        # Family: which rows are nominees and their shares, for the live "Nominee shares add up to" line.
        'nom': {str(i): bool(r.get('isNominee')) for i, r in enumerate(rows_ or [])} if section == 'family' else {},
        'share': {str(i): r.get('nomineeShare') or '' for i, r in enumerate(rows_ or [])} if section == 'family' else {},
        'add_label': {'emergency': 'Add a contact', 'family': 'Add a family member', 'education': 'Add a qualification',
                      'experience': 'Add a previous job'}.get(section),
    })


MAX_ROWS = {'emergency': 5, 'family': 20, 'education': 15, 'experience': 20}
BLANK_ROW = {'family': {'relation': 'spouse'}, 'education': {'level': 'bachelor'}}


def _slots(section: str, existing: list[dict]) -> list[dict]:
    """Every row the list editor can hold: the stored ones, then blanks Alpine reveals with "Add"."""
    n = MAX_ROWS.get(section, 0)
    prefix = {'emergency': 'c', 'family': 'm', 'education': 'e', 'experience': 'x'}.get(section, 'r')
    return [{'i': i, 'p': f'{prefix}-{i}', 'r': existing[i] if i < len(existing) else dict(BLANK_ROW.get(section, {}))} for i in range(n)]


# ================================================================ history and career

@require_GET
@login_required
def history(request, id):
    rows_ = svc.history(request.hajiri_user, str(id))
    for r in rows_:
        r['how'] = SOURCE.get(r['source'], r['source'])
    return render(request, 'people/partials/history.html', {'history': rows_, 'employee_id': str(id)})


@require_GET
@login_required
def career(request, id):
    user = request.hajiri_user
    events = svc.career(user, str(id))
    can_edit = has_role(user, 'hr') and request.GET.get('edit') == '1'
    for e in events:
        e['icon'] = KIND_ICON.get(e['kind'], 'briefcase')
    return render(request, 'people/partials/career.html', {'events': events, 'employee_id': str(id), 'can_edit': can_edit})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def career_new(request, id):
    user = request.hajiri_user
    id = str(id)
    svc.employee_in(company_id_of(user), id)        # another company's id is not found, even for the empty form
    if request.method == 'POST':
        body = CareerForm(request.POST).body()
        ev = svc.record_career(user, id, body)
        from hajiri.web.format import fmt_date
        msg = (f"Scheduled for {fmt_date(body['effectiveDate'])}. It applies automatically on that day." if ev['status'] == 'scheduled'
               else 'Recorded and applied.')
        return _ok(request, msg, 'career', 'field-history', 'employee-overview')
    kind = request.GET.get('kind') or 'promotion'
    st = svc.structure(user)
    designations = [(d['id'], f"{d['name_en']}{' (' + d['grade'] + ')' if d['grade'] else ''}") for d in st['designations'] if d['active']]
    return render(request, 'people/partials/career_form.html', {
        'employee_id': id, 'kind': kind, 'kinds': CAREER_KINDS_FORM, 'hint': next((h for k, _, h in CAREER_KINDS_FORM if k == kind), ''),
        'designations': designations, 'branches': st['branches'], 'units': st['units'], 'employment': EMPLOYMENT, 'separation': SEPARATION,
        'today': npt_today().isoformat(),
    })


@require_POST
@require_role('hr')
def career_cancel(request, id, event_id):
    svc.cancel_career(request.hajiri_user, str(id), str(event_id))
    return _ok(request, 'Scheduled change cancelled.', 'career', 'employee-overview', close=False)


# ================================================================ my profile (/me/profile) and my data

ROLE = {'admin': 'Admin', 'hr': 'HR', 'manager': 'Manager', 'employee': 'Employee'}
REGIME_LONG = {'ssf': 'Social Security Fund (SSF)', 'pf': 'Provident Fund (PF)', 'none': 'None'}


@require_GET
@login_required
def my_profile(request):
    user = request.hajiri_user
    e = None
    person = None
    if user.employee_id:
        e = (Employee.objects.for_company(company_id_of(user)).filter(pk=user.employee_id)
             .values('emp_code', 'designation', 'join_date', 'regime', 'bank_code', 'bank_account_last4').first())
        person = svc.card(user, user.employee_id)['person']
    return render(request, 'people/my_profile.html', {
        'e': e, 'person': person, 'roles': ', '.join(ROLE.get(r, r) for r in user.roles) or '—',
        'regime': REGIME_LONG.get(e['regime'], e['regime']) if e else None,
        'bank': (f"{e['bank_code']}, ending {e['bank_account_last4'] or '—'}" if e and e['bank_code'] else '—'),
        'has_requests': bool(user.employee_id and svc.my_requests(user)),
    })


@require_GET
@login_required
def my_requests(request):
    reqs = svc.my_requests(request.hajiri_user)[:10]
    for r in reqs:
        r['badge'] = MY_REQ_STATUS.get(r['status'], (r['status'], 'idle'))
    return render(request, 'people/partials/my_requests.html', {'requests': reqs})


@require_POST
@login_required
def request_cancel(request, id):
    svc.cancel_my_request(request.hajiri_user, str(id))
    return _ok(request, 'Request cancelled.', 'my-requests', 'profile-sections', close=False)


# HomePage.tsx sourceLabel: anything else is shown as stored.
SOURCE_LABEL = {'web': 'Web', 'mobile': 'Mobile', 'device': 'Terminal', 'manual': 'Added by HR', 'regularisation': 'Correction',
                'kiosk': 'Kiosk', 'field': 'Field visit'}


def cap(s: str | None) -> str:
    """MyDataPage.tsx cap(): first letter upper case, underscores to spaces, the rest as it is."""
    s = s or ''
    return s[:1].upper() + s[1:].replace('_', ' ') if s else s


@require_GET
@login_required
def my_data(request):
    """MyDataPage.tsx: what is stored and who sees it, biometric consent, the clock-in log, and the data download."""
    user = request.hajiri_user
    tab = request.GET.get('tab') or 'privacy'
    punches = []
    if user.employee_id and tab == 'log':
        from datetime import timedelta
        today = npt_today()
        try:
            from hajiri.attendance.api import punches as att_punches
            punches = att_punches(user, {'employeeId': user.employee_id, 'from': (today - timedelta(days=14)).isoformat(), 'to': today.isoformat()})
        except ImportError:
            punches = []
        for p in punches:
            p['source_label'] = SOURCE_LABEL.get(p['source'], p['source'])
            p['entry'] = 'Clock-in' if p['direction'] == 'in' else 'Clock-out' if p['direction'] == 'out' else 'Unpaired'
            p['method'] = cap(p['verify_mode'])
    templates = 0
    if user.employee_id:
        templates = BiometricTemplate.objects.filter(employee_id=user.employee_id, shredded_at__isnull=True).count()
    return render(request, 'people/my_data.html', {
        'tab': tab, 'punches': punches, 'active_templates': templates,
        'transparency_url': url_of('devices:transparency'), 'consents_url': url_of('devices:consents'), 'dsar_url': url_of('devices:dsar'),
    })


@require_GET
def legacy_my_data(request):
    return HttpResponseRedirect(reverse('people:privacy'))


# ================================================================ change requests (HR)

def _request_ctx(r: dict, user, changes=None, revealed=False) -> dict:
    r['badge'] = REQ_STATUS.get(r['status'], (r['status'], 'idle'))
    return {'r': r, 'changes': changes if changes is not None else (r['summary'].get('changes') or []), 'revealed': revealed,
            'own': r['requested_by'] == user.id, 'sensitive': r['section'] in SENSITIVE_SECTIONS}


@require_GET
@require_role('hr')
def change_requests(request):
    user = request.hajiri_user
    status = request.GET.get('status') or 'pending'
    if status not in ('pending', 'approved', 'rejected', 'all'):
        status = 'pending'
    items = [_request_ctx(r, user) for r in svc.change_requests(user, status)]
    ctx = {'items': items, 'status': status, 'pills': [('pending', 'Waiting'), ('approved', 'Approved'), ('rejected', 'Declined'), ('all', 'All')]}
    if is_htmx(request) and request.headers.get('HX-Target') == 'change-requests':
        return render(request, 'people/partials/requests_list.html', ctx)
    return render(request, 'people/requests.html', ctx)


@require_GET
@require_role('hr')
def request_reveal(request, id):
    user = request.hajiri_user
    d = svc.request_detail(user, str(id), reveal=True)
    full = next((r for r in svc.change_requests(user, 'all') if r['id'] == str(id)), None)
    if not full:
        raise NotFoundError('change request not found')
    return render(request, 'people/partials/request_card.html', {'it': _request_ctx(full, user, d['changes'], True)})


@require_GET
@login_required
def request_proof(request, id):
    from hajiri.files.store import stored_file_response
    return stored_file_response(svc.request_proof(request.hajiri_user, str(id)), download=request.GET.get('download') == '1')


def _instance_of(user, request_id: str) -> str:
    from .change_requests import PROFILE_CHANGE
    wid = (WorkflowInstance.objects.for_company(company_id_of(user)).filter(object_type=PROFILE_CHANGE, object_id=request_id)
           .values_list('id', flat=True).first())
    if not wid:
        raise NotFoundError('change request not found')
    return str(wid)


@require_POST
@require_role('hr')
def request_approve(request, id):
    from hajiri.workflow.services import decide
    user = request.hajiri_user
    r = svc.load_request(user, str(id))
    try:
        decide(user, _instance_of(user, str(id)), 'approve')
    except HttpError as exc:
        return failed(request, exc, 'Not approved')
    name = Employee.objects.filter(pk=r['employee_id']).values_list('name_en', flat=True).first()
    return _ok(request, f"{SECTION_LABEL[r['section']]} updated for {name}.", 'change-requests', close=False)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def request_reject(request, id):
    from hajiri.workflow.services import decide
    user = request.hajiri_user
    svc.load_request(user, str(id))
    if request.method == 'POST':
        reason = RejectForm(request.POST).reason_text()
        decide(user, _instance_of(user, str(id)), 'reject', reason=reason)
        return _ok(request, 'Request declined. The employee has been told why.', 'change-requests')
    return render(request, 'people/partials/request_reject.html', {'id': str(id)})


# ================================================================ org structure (HR)

def _unit_tree(units: list[dict]) -> list[dict]:
    ids = {u['id'] for u in units}

    def kids(parent):
        return [u for u in units if (u['parent_id'] == parent if parent else (not u['parent_id'] or u['parent_id'] not in ids))]

    def total(u) -> int:
        return u['headcount'] + sum(total(k) for k in kids(u['id']))
    out = []

    def walk(u, depth):
        out.append({**u, 'depth': depth, 'indent': depth * 24, 'total': total(u)})
        for k in kids(u['id']):
            walk(k, depth + 1)
    for u in kids(None):
        walk(u, 0)
    return out


@require_GET
@require_role('hr')
def structure(request):
    user = request.hajiri_user
    st = svc.structure(user)
    unit_name = {u['id']: u['name_en'] for u in st['units']}
    for d in st['designations']:
        d['unit_name'] = unit_name.get(d['org_unit_id'], 'Any')
    ctx = {'tree': _unit_tree(st['units']), 'designations': st['designations'], 'branches': st['branches'],
           'policy': svc.get_policy(user)}
    if is_htmx(request) and request.headers.get('HX-Target') == 'org-structure':
        return render(request, 'people/partials/structure_body.html', ctx)
    return render(request, 'people/structure.html', ctx)


_KINDS = {'unit', 'branch', 'designation'}


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def structure_edit(request, kind, id=None):
    user = request.hajiri_user
    if kind not in _KINDS:
        raise NotFoundError('not found')
    if request.method == 'POST':
        body = StructureForm(kind, request.POST).body()
        fn = {('unit', False): svc.create_unit, ('branch', False): svc.create_branch, ('designation', False): svc.create_designation}
        if id:
            {'unit': svc.update_unit, 'branch': svc.update_branch, 'designation': svc.update_designation}[kind](user, str(id), body)
        else:
            fn[(kind, False)](user, body)
        return _ok(request, 'Saved.', 'org-structure')
    st = svc.structure(user)
    item = None
    if id:
        coll = {'unit': st['units'], 'branch': st['branches'], 'designation': st['designations']}[kind]
        item = next((x for x in coll if x['id'] == str(id)), None)
        if not item:
            raise NotFoundError({'unit': 'unit not found', 'branch': 'branch not found', 'designation': 'designation not found'}[kind])
    parent = None
    if kind == 'unit' and not item and request.GET.get('parent'):
        parent = next((u for u in st['units'] if u['id'] == request.GET['parent']), None)
    unit_kind = (item or {}).get('kind') or ('team' if parent else 'department')
    noun = ('team' if unit_kind == 'team' else 'department') if kind == 'unit' else kind
    return render(request, 'people/partials/structure_form.html', {
        'kind': kind, 'item': item, 'parent': parent, 'unit_kind': unit_kind, 'title': f"{'Edit' if item else 'New'} {noun}",
        'units': [u for u in st['units'] if not item or u['id'] != item['id']], 'all_units': st['units'], 'branches': st['branches'],
        'action': request.path,
    })


@require_POST
@require_role('hr')
def structure_delete(request, kind, id):
    user = request.hajiri_user
    if kind not in _KINDS:
        raise NotFoundError('not found')
    st = svc.structure(user)
    coll = {'unit': st['units'], 'branch': st['branches'], 'designation': st['designations']}[kind]
    name = next((x['name_en'] for x in coll if x['id'] == str(id)), '')
    try:
        {'unit': svc.delete_unit, 'branch': svc.delete_branch, 'designation': svc.delete_designation}[kind](user, str(id))
    except HttpError as exc:
        return failed(request, exc, 'Not deleted')
    return _ok(request, f'{name} deleted.', 'org-structure', close=False)


@require_POST
@require_role('hr')
def policy(request):
    on = svc.set_policy(request.hajiri_user, PolicyForm(request.POST).body())['makerChecker']
    return _ok(request, 'Second approval turned on.' if on else 'Second approval turned off.', close=False)

