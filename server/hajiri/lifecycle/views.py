"""
Lifecycle pages (apps/web/src/pages/lifecycle): Onboarding (joiners, probation, checklists, assets, settings) and Exits
(exits, clearance checklists, interview questions), the checklist and exit drawers, the employee page section, and
the fragments Home and the help desk embed (my checklist, leaving the company). Fragments under /_/lifecycle/...
"""
from __future__ import annotations

from datetime import timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.clock import npt_today
from hajiri.core.errors import BadRequestError
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.format import fmt_date
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast

from . import api
from .api import ASSIGNEE_LABEL, EXIT_KIND_LABEL, RESIGN_REASONS

EXIT_STATUS = {'pending': ('Awaiting approval', 'warn'), 'accepted': ('Serving notice', 'info'), 'completed': ('Left', 'idle'),
               'rejected': ('Not accepted', 'idle'), 'withdrawn': ('Withdrawn', 'idle'), 'cancelled': ('Cancelled', 'idle')}
CASE_STATUS = {'active': ('In progress', 'info'), 'completed': ('Complete', 'ok'), 'cancelled': ('Cancelled', 'idle')}
CONDITIONS = [('new', 'New'), ('good', 'Good'), ('fair', 'Fair'), ('poor', 'Poor'), ('damaged', 'Damaged'), ('lost', 'Lost')]
ASSET_STATUS = {'available': ('Available', 'ok'), 'assigned': ('With someone', 'info'), 'retired': ('Retired', 'idle')}
REC = {'confirm': 'Confirm', 'extend': 'Extend', 'terminate': 'End employment'}
RATING = ['', 'Poor', 'Below expectations', 'Meets expectations', 'Exceeds expectations', 'Outstanding']


def _ok(request, message: str, *refreshes: str, close: bool = True, title: str | None = None) -> HttpResponse:
    resp = HttpResponse(status=204)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message, title=title)


def _then_open(request, resp: HttpResponse, message: str, *refreshes: str) -> HttpResponse:
    """Close the form's modal and open the new checklist or exit in the drawer (the TS pages' setOpenId)."""
    close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    resp['HX-Retarget'] = '#hj-drawer-body'
    resp['HX-Reswap'] = 'innerHTML'
    return toast(request, resp, message)


def _b(v):
    return None if v is None or (isinstance(v, str) and not v.strip()) else (v.strip() if isinstance(v, str) else v)


def _seed(request) -> None:
    api.ensure_defaults(request.hajiri_user)


def due_tone(t: dict) -> tuple[str, str] | None:
    """CaseDrawer.tsx dueTone."""
    if t['status'] != 'open' or not t.get('due_date'):
        return None
    today = npt_today()
    if t['due_date'] < today:
        return ('bad', f"Overdue, was due {fmt_date(t['due_date'])}")
    if t['due_date'] == today:
        return ('warn', 'Due today')
    return ('muted', f"Due {fmt_date(t['due_date'])}")


def _task_view(t: dict, can_skip: bool = False) -> dict:
    owner = (t.get('assignee_name') or 'Named person') if t['assignee_type'] == 'user' else ASSIGNEE_LABEL.get(t['assignee_type'], t['assignee_type'])
    return {**t, 'owner': f"{t['department']}, {owner}" if t.get('department') else owner, 'due': due_tone(t), 'can_skip': can_skip}


def _progress(done: int, total: int, overdue: int = 0) -> dict:
    return {'done': done, 'total': total, 'overdue': overdue, 'pct': round(done / total * 100) if total else 0}


# ================================================================ onboarding page

ONB_TABS = [('joiners', 'Joiners'), ('probation', 'Probation'), ('templates', 'Checklists'), ('assets', 'Assets'), ('settings', 'Settings')]
EXIT_TABS = [('exits', 'Exits'), ('templates', 'Clearance checklists'), ('questions', 'Interview questions')]


@require_GET
@require_role('hr')
@require_feature('lifecycle')
def onboarding(request):
    _seed(request)
    user = request.hajiri_user
    tab = request.GET.get('tab') or 'joiners'
    if tab not in dict(ONB_TABS):
        tab = 'joiners'
    ctx: dict = {'tab': tab, 'tabs': ONB_TABS}
    if tab == 'joiners':
        ctx.update(_joiners_ctx(request))
        if is_htmx(request) and request.headers.get('HX-Target') == 'joiners':
            return render(request, 'lifecycle/partials/joiners.html', ctx)
    elif tab == 'probation':
        ctx.update(_probation_ctx(request))
        if is_htmx(request) and request.headers.get('HX-Target') == 'probation':
            return render(request, 'lifecycle/partials/probation.html', ctx)
    elif tab == 'templates':
        ctx.update(_templates_ctx(user, 'onboarding'))
        if is_htmx(request) and request.headers.get('HX-Target') == 'templates':
            return render(request, 'lifecycle/partials/templates.html', ctx)
    elif tab == 'assets':
        ctx.update(_assets_ctx(request))
        if is_htmx(request) and request.headers.get('HX-Target') == 'assets':
            return render(request, 'lifecycle/partials/assets.html', ctx)
    else:
        ctx['policy'] = api.get_settings(user)
        ctx['reminders'] = ', '.join(str(x) for x in ctx['policy']['probationReminderDays'])
    return render(request, 'lifecycle/onboarding.html', ctx)


def _joiners_ctx(request) -> dict:
    user = request.hajiri_user
    show = 'completed' if request.GET.get('show') == 'completed' else 'active'
    cs = api.cases(user, kind='onboarding', status=show)
    today = npt_today()
    soon = today + timedelta(days=30)
    for c in cs:
        c['progress'] = _progress(c['done'], c['total'], c['overdue'])
    stats = [{'label': 'Onboarding now', 'value': len(cs)},
             {'label': 'Joining in the next 30 days', 'value': sum(1 for c in cs if today <= c['anchor_date'] <= soon)},
             {'label': 'Overdue tasks', 'value': sum(c['overdue'] for c in cs), 'tone': 'warn' if any(c['overdue'] for c in cs) else 'ok'}]
    return {'cases': cs, 'show': show, 'stats': stats}


def _probation_ctx(request) -> dict:
    within = request.GET.get('within') or '60'
    if within not in ('30', '60', '180'):
        within = '60'
    emps = api.probation(request.hajiri_user, within)
    for e in emps:
        d = e['days_left']
        e['left'] = f'{-d} days ago' if d < 0 else 'Today' if d == 0 else f'In {d} days'
        e['left_tone'] = 'bad' if d < 0 else 'warn' if d <= 7 else 'muted'
        lr = e['last_review']
        if lr:
            e['review'] = (('Waiting for HR', 'warn') if lr['status'] == 'pending' else
                           (('Extended' if lr['recommendation'] == 'extend' else 'Approved'), 'ok') if lr['status'] == 'approved' else ('Not approved', 'idle'))
    return {'employees': emps, 'within': within}


def _templates_ctx(user, kind: str) -> dict:
    ts = api.templates(user, kind)
    for t in ts:
        t['scope'] = ', '.join(x for x in (t.get('org_unit_name'), t.get('branch_name')) if x) or 'Everyone'
    return {'templates': ts, 'kind': kind}


def _assets_ctx(request) -> dict:
    status = request.GET.get('status') or 'all'
    search = (request.GET.get('search') or '').strip()
    q = {}
    if status in ('available', 'assigned', 'retired'):
        q['status'] = status
    if search:
        q['search'] = search[:80]
    items = api.assets(request.hajiri_user, q)
    for a in items:
        a['badge'] = ASSET_STATUS.get(a['status'], (a['status'], 'idle'))
    return {'assets': items, 'status': status, 'search': search}


# ---------------------------------------------------------------- joiners

@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def case_new(request):
    user = request.hajiri_user
    _seed(request)
    if request.method == 'POST':
        body = {'employeeId': request.POST.get('employee_id') or '', 'templateId': _b(request.POST.get('templateId'))}
        if _b(request.POST.get('anchorDate')):
            body['anchorDate'] = request.POST['anchorDate']
        case_id = api.start_onboarding(user, body)
        return _then_open(request, _case_response(request, case_id), 'Onboarding started.', 'joiners', 'lifecycle-section')
    templates = [t for t in api.templates(user, 'onboarding') if t['active']]
    return render(request, 'lifecycle/partials/case_new.html', {'templates': templates, 'employee_id': request.GET.get('employee_id') or ''})


@require_GET
@login_required
@require_feature('lifecycle')
def case_detail(request, id):
    return _case_response(request, id)


def _case_response(request, id):
    user = request.hajiri_user
    d = api.case_detail(user, str(id))
    hr = has_role(user, 'hr')
    active = d['case']['status'] == 'active'
    tasks = [_task_view(t, hr and active) for t in d['tasks']]
    done = sum(1 for t in tasks if t['status'] != 'open')
    return render(request, 'lifecycle/partials/case.html', {
        'c': d['case'], 'emp': d['employee'], 'tasks': tasks, 'progress': _progress(done, len(tasks)), 'hr': hr, 'active': active,
        'badge': CASE_STATUS.get(d['case']['status'], (d['case']['status'], 'idle')),
        'assignees': [(k, ASSIGNEE_LABEL[k]) for k in ('hr', 'manager', 'employee')],
    })


@require_POST
@require_role('hr')
@require_feature('lifecycle')
def case_cancel(request, id):
    api.cancel_case(request.hajiri_user, str(id))
    return _ok(request, 'Checklist cancelled.', 'joiners', 'lifecycle-section', 'case-' + str(id), close=False)


@require_POST
@require_role('hr')
@require_feature('lifecycle')
def task_add(request, id):
    body = {'title': (request.POST.get('title') or '').strip(), 'assigneeType': request.POST.get('assigneeType') or 'hr',
            'dueDate': _b(request.POST.get('dueDate'))}
    api.add_task(request.hajiri_user, str(id), body)
    return _ok(request, 'Task added.', 'case-' + str(id), 'joiners', 'exits', close=False)


@require_POST
@login_required
@require_feature('lifecycle')
def task_update(request, id):
    user = request.hajiri_user
    status = request.POST.get('status')
    if status not in ('open', 'done', 'skipped'):
        raise BadRequestError('status: Invalid option: expected one of "open"|"done"|"skipped"')
    r = api.update_task(user, str(id), {'status': status})
    t = api.load_task(user, str(id))
    resp = HttpResponse(status=204)
    for x in ('case-' + t['case_id'], 'joiners', 'exits', 'lifecycle-section', 'my-checklist'):
        refresh(resp, x)
    return toast(request, resp, 'Checklist complete.') if r['caseCompleted'] else resp


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('lifecycle')
def comments(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        api.add_comment(user, str(id), {'body': request.POST.get('body') or ''})
    return render(request, 'lifecycle/partials/comments.html', {'task_id': str(id), 'comments': api.comments(user, str(id))})


# ---------------------------------------------------------------- probation

@require_http_methods(['GET', 'POST'])
@require_role('hr', 'manager')
@require_feature('lifecycle')
def review(request, employee_id):
    user = request.hajiri_user
    if request.method == 'POST':
        rec = request.POST.get('recommendation') or 'confirm'
        try:
            rating = int(request.POST.get('rating') or 4)
        except ValueError:
            rating = request.POST.get('rating')
        body = {'employeeId': str(employee_id), 'rating': rating, 'recommendation': rec, 'comments': (request.POST.get('comments') or '').strip(),
                'extendTo': _b(request.POST.get('extendTo')) if rec == 'extend' else None,
                'lastWorkingDay': _b(request.POST.get('lastWorkingDay')) if rec == 'terminate' else None}
        api.submit_review(user, body)
        return _ok(request, 'Review sent to HR for approval.', 'probation', 'lifecycle-section')
    e = api.employee_in(user.company_id, str(employee_id))
    today = npt_today()
    base = e['probation_end'] if e['probation_end'] and e['probation_end'] > today else today
    return render(request, 'lifecycle/partials/review.html', {'e': e, 'extend_default': base + timedelta(days=90), 'lwd_default': base,
                                                              'min_extend': base + timedelta(days=1), 'ratings': list(enumerate(RATING))[1:]})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def confirm(request, employee_id):
    user = request.hajiri_user
    e = api.employee_in(user.company_id, str(employee_id))
    if request.method == 'POST':
        api.confirm(user, {'employeeId': str(employee_id), 'confirmedOn': request.POST.get('confirmedOn') or ''})
        return _ok(request, f"{e['name_en']} is confirmed.", 'probation', 'lifecycle-section')
    return render(request, 'lifecycle/partials/confirm.html', {'e': e})


# ---------------------------------------------------------------- checklists (templates)

@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def template_edit(request, kind, id=None):
    user = request.hajiri_user
    _seed(request)
    if kind not in ('onboarding', 'offboarding'):
        raise BadRequestError('kind: Invalid option: expected one of "onboarding"|"offboarding"')
    if request.method == 'POST':
        p = request.POST
        idx = sorted({int(k.split('-')[1]) for k in p if k.startswith('t-') and k.split('-')[1].isdigit()})

        def position(i: int):
            # The editor's move up / move down posts each task's place as t-<i>-sort; without it, slot order.
            v = p.get(f't-{i}-sort') or ''
            return (int(v), i) if v.lstrip('-').isdigit() else (i, i)
        idx.sort(key=position)
        tasks = []
        for i in idx:
            title = (p.get(f't-{i}-title') or '').strip()
            if not title:
                continue
            at = p.get(f't-{i}-assigneeType') or 'hr'
            try:
                off = int(p.get(f't-{i}-dueOffsetDays') or 0)
            except ValueError:
                off = 0
            tasks.append({'title': title, 'description': _b(p.get(f't-{i}-description')), 'department': _b(p.get(f't-{i}-department')),
                          'assigneeType': at, 'assigneeUserId': _b(p.get(f't-{i}-assigneeUserId')) if at == 'user' else None,
                          'dueOffsetDays': off, 'link': _b(p.get(f't-{i}-link'))})
        body = {'kind': kind, 'name': (p.get('name') or '').strip(), 'description': p.get('description') or '',
                'isDefault': p.get('isDefault') in ('on', 'true', '1'), 'active': p.get('active') in ('on', 'true', '1'), 'tasks': tasks}
        api.save_template(user, str(id) if id else None, body)
        return _ok(request, 'Checklist saved.', 'templates')
    t = api.get_template(user, str(id)) if id else {'name': '', 'description': '', 'is_default': False, 'active': True,
                                                    'tasks': [{'title': '', 'assignee_type': 'hr', 'due_offset_days': 0}]}
    from hajiri.core.db import rows
    users = [(str(u['id']), u['name']) for u in rows("SELECT id, name FROM app_user WHERE company_id = %s AND status = 'active' ORDER BY name",
                                                     [user.company_id])]
    tasks = list(t['tasks'])
    slots = [{'i': i, 't': tasks[i] if i < len(tasks) else {'title': '', 'assignee_type': 'hr', 'due_offset_days': 0}} for i in range(60)]
    return render(request, 'lifecycle/partials/template_form.html', {
        't': t, 'kind': kind, 'id': str(id) if id else None, 'slots': slots, 'shown': max(1, len(tasks)), 'users': users,
        'assignees': list(ASSIGNEE_LABEL.items()), 'anchor': 'joining' if kind == 'onboarding' else 'the last day',
        'zero_label': 'On the first day' if kind == 'onboarding' else 'On the last day',
    })


# ---------------------------------------------------------------- assets

@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def asset_new(request):
    user = request.hajiri_user
    _seed(request)
    if request.method == 'POST':
        p = request.POST
        cost = (p.get('cost') or '').strip()
        if cost:
            try:
                cost = f'{float(cost):.2f}'
            except ValueError:
                pass
        body = {'tag': (p.get('tag') or '').strip(), 'name': (p.get('name') or '').strip(), 'typeId': _b(p.get('typeId')),
                'serial': _b(p.get('serial')), 'condition': p.get('condition') or 'good', 'purchaseDate': _b(p.get('purchaseDate')),
                'cost': cost or None}
        api.create_asset(user, body)
        return _ok(request, 'Asset added.', 'assets')
    return render(request, 'lifecycle/partials/asset_new.html', {'types': api.asset_types(user), 'conditions': [c for c in CONDITIONS if c[0] != 'lost']})


def _asset_row(user, id_: str) -> dict:
    a = next((x for x in api.assets(user, {}) if x['id'] == str(id_)), None)
    if not a:
        from hajiri.core.errors import NotFoundError
        raise NotFoundError('asset not found')
    return a


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def asset_assign(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        body = {'employeeId': request.POST.get('employee_id') or '', 'notes': _b(request.POST.get('notes'))}
        if _b(request.POST.get('assignedOn')):
            body['assignedOn'] = request.POST['assignedOn']
        api.assign_asset(user, str(id), body)
        return _ok(request, 'Asset handed over.', 'assets', 'lifecycle-section')
    return render(request, 'lifecycle/partials/asset_assign.html', {'a': _asset_row(user, str(id)), 'today': npt_today()})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def asset_return(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        body = {'condition': request.POST.get('condition') or '', 'notes': _b(request.POST.get('notes'))}
        if _b(request.POST.get('returnedOn')):
            body['returnedOn'] = request.POST['returnedOn']
        api.return_asset(user, str(id), body)
        return _ok(request, 'Return recorded.', 'assets', 'lifecycle-section', 'exits')
    return render(request, 'lifecycle/partials/asset_return.html', {'a': _asset_row(user, str(id)), 'today': npt_today(), 'conditions': CONDITIONS})


# ---------------------------------------------------------------- settings

@require_POST
@require_role('hr')
@require_feature('lifecycle')
def settings_save(request):
    p = request.POST
    days = []
    for x in (p.get('probationReminderDays') or '').replace(' ', '').split(','):
        if x.isdigit() and 0 < int(x) <= 90:
            days.append(int(x))
    try:
        notice = int(p.get('noticeDays') or 0)
    except ValueError:
        notice = 0
    api.save_settings(request.hajiri_user, {'autoStartOnboarding': p.get('autoStartOnboarding') in ('on', 'true', '1'), 'noticeDays': notice,
                                            'probationReminderDays': days[:4], 'selfExitInterview': p.get('selfExitInterview') in ('on', 'true', '1')})
    return _ok(request, 'Settings saved.', close=False)


# ================================================================ exits page

@require_GET
@require_role('hr')
@require_feature('lifecycle')
def exits(request):
    _seed(request)
    user = request.hajiri_user
    tab = request.GET.get('tab') or 'exits'
    if tab not in dict(EXIT_TABS):
        tab = 'exits'
    ctx: dict = {'tab': tab, 'tabs': EXIT_TABS}
    if tab == 'exits':
        ctx.update(_exits_ctx(request))
        if is_htmx(request) and request.headers.get('HX-Target') == 'exits':
            return render(request, 'lifecycle/partials/exits.html', ctx)
    elif tab == 'templates':
        ctx.update(_templates_ctx(user, 'offboarding'))
        if is_htmx(request) and request.headers.get('HX-Target') == 'templates':
            return render(request, 'lifecycle/partials/templates.html', ctx)
    else:
        qs = api.questions(user)
        ctx['questions'] = qs
        ctx['slots'] = [{'i': i, 'q': qs[i] if i < len(qs) else {'question': '', 'kind': 'text', 'options': [], 'active': True}} for i in range(30)]
        ctx['shown'] = len(qs)
    return render(request, 'lifecycle/exits.html', ctx)


def _exits_ctx(request) -> dict:
    status = request.GET.get('status') or 'open'
    if status not in ('open', 'completed', 'all'):
        status = 'open'
    xs = api.exits(request.hajiri_user, None if status == 'all' else status)
    today = npt_today()
    for x in xs:
        x['badge'] = EXIT_STATUS.get(x['status'], (x['status'], 'idle'))
        x['kind_label'] = EXIT_KIND_LABEL.get(x['kind'], x['kind'])
        x['progress'] = _progress(x['tasks_done'], x['tasks_total']) if x['tasks_total'] else None
    stats = [{'label': 'Waiting for approval', 'value': sum(1 for x in xs if x['status'] == 'pending')},
             {'label': 'Serving notice', 'value': sum(1 for x in xs if x['status'] == 'accepted')},
             {'label': 'Clearance overdue', 'value': sum(1 for x in xs if x['status'] == 'accepted' and x['lwd'] < today),
              'tone': 'warn' if any(x['status'] == 'accepted' and x['lwd'] < today for x in xs) else 'ok'}]
    return {'exits': xs, 'status': status, 'stats': stats}


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def exit_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        p = request.POST
        body = {'employeeId': p.get('employee_id') or '', 'kind': p.get('kind') or 'resignation', 'lastWorkingDay': p.get('lastWorkingDay') or '',
                'voluntary': p.get('voluntary') in ('on', 'true', '1'), 'reasonCategory': _b(p.get('reasonCategory')), 'reason': _b(p.get('reason'))}
        xid = api.create_exit(user, body)
        return _then_open(request, _exit_response(request, xid), 'Exit recorded and clearance started.', 'exits', 'lifecycle-section')
    return render(request, 'lifecycle/partials/exit_new.html', {
        'kinds': list(EXIT_KIND_LABEL.items()), 'reasons': RESIGN_REASONS + ['Performance', 'Misconduct', 'Redundancy', 'Contract ended']})


@require_GET
@login_required
@require_feature('lifecycle')
def exit_detail(request, id):
    return _exit_response(request, id)


def _exit_response(request, id):
    user = request.hajiri_user
    d = api.exit_detail(user, str(id))
    x = d['exit']
    hr = has_role(user, 'hr')
    case = None
    tasks = []
    if x['case_id']:
        case = api.case_detail(user, x['case_id'])
        tasks = [_task_view(t, case['case']['status'] == 'active' and hr) for t in case['tasks']]
    interview = api.interview(user, x['id']) if hr and x['interview_done'] else None
    b = d['blockers']
    today = npt_today()
    s = d['settlement']
    if s and s.get('period'):
        s['period_label'] = f"{_month(s['period']['bsMonth'])} {s['period']['bsYear']}"
    return render(request, 'lifecycle/partials/exit.html', {
        'x': x, 'b': b, 's': s, 'tasks': tasks, 'case': case, 'interview': interview, 'hr': hr,
        'open': x['status'] in ('pending', 'accepted'), 'past': x['lwd'] <= today,
        'badge': EXIT_STATUS.get(x['status'], (x['status'], 'idle')), 'kind_label': EXIT_KIND_LABEL.get(x['kind'], x['kind']),
        'clearance_hint': (f"{b['openTasks']} open, {b['assetsHeld']} {'asset' if b['assetsHeld'] == 1 else 'assets'} not returned"
                           if b['openTasks'] or b['assetsHeld'] else 'Everything is cleared.'),
        'questions': [q for q in api.questions(user) if q['active']] if not interview else [],
    })


def _month(m: int) -> str:
    from .service import BS_MONTHS
    return BS_MONTHS[m - 1] if 1 <= m <= 12 else str(m)


@require_POST
@require_role('hr')
@require_feature('lifecycle')
def exit_update(request, id):
    api.update_exit(request.hajiri_user, str(id), {'lastWorkingDay': request.POST.get('lastWorkingDay') or ''})
    return _ok(request, 'Last working day changed.', 'exits', 'exit-' + str(id), close=False)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('lifecycle')
def exit_cancel(request, id):
    if request.method == 'POST':
        api.cancel_exit(request.hajiri_user, str(id), {'reason': (request.POST.get('reason') or '').strip()})
        return _ok(request, 'Exit cancelled.', 'exits', 'exit-' + str(id), 'lifecycle-section')
    api.load_exit(request.hajiri_user, str(id))
    return render(request, 'lifecycle/partials/exit_cancel.html', {'id': str(id)})


@require_POST
@require_role('hr')
@require_feature('lifecycle')
def exit_complete(request, id):
    api.complete(request.hajiri_user, str(id), {'skipOpenTasks': request.POST.get('skipOpenTasks') in ('on', 'true', '1')})
    return _ok(request, 'Exit completed.', 'exits', 'exit-' + str(id), 'lifecycle-section', close=False)


@require_POST
@login_required
@require_feature('lifecycle')
def exit_withdraw(request, id):
    api.withdraw_exit(request.hajiri_user, str(id))
    return _ok(request, 'Resignation withdrawn.', 'my-leaving', close=False)


def interview_answers(post, questions: list[dict]) -> list[dict]:
    out = []
    for q in questions:
        v = (post.get(f"q-{q['id']}") or '').strip()
        if not v:
            continue
        out.append({'questionId': q['id'], 'answer': int(v) if q['kind'] == 'rating' and v.isdigit() else v})
    return out


@require_POST
@login_required
@require_feature('lifecycle')
def interview(request, id):
    user = request.hajiri_user
    answers = interview_answers(request.POST, api.questions(user))
    api.record_interview(user, str(id), {'answers': answers})
    return _ok(request, 'Thank you. Your answers were saved.', 'exits', 'exit-' + str(id), 'my-leaving', close=False)


@require_POST
@require_role('hr')
@require_feature('lifecycle')
def questions_save(request):
    p = request.POST
    idx = sorted({int(k.split('-')[1]) for k in p if k.startswith('q-') and k.split('-')[1].isdigit()})
    qs = []
    for i in idx:
        text = (p.get(f'q-{i}-question') or '').strip()
        if not text:
            continue
        q = {'question': text, 'kind': p.get(f'q-{i}-kind') or 'text',
             'options': [o.strip() for o in (p.get(f'q-{i}-options') or '').split(',') if o.strip()],
             'active': p.get(f'q-{i}-active') in ('on', 'true', '1')}
        if _b(p.get(f'q-{i}-id')):
            q['id'] = p[f'q-{i}-id']
        qs.append(q)
    api.save_questions(request.hajiri_user, {'questions': qs})
    return _ok(request, 'Questions saved.', close=False)


# ================================================================ employee page section, home card, help desk block

@require_GET
@login_required
@require_feature('lifecycle')
def employee_section(request, id):
    user = request.hajiri_user
    _seed(request)
    d = api.employee_summary(user, str(id))
    for c in d['cases']:
        c['progress'] = _progress(c['done'], c['total'])
        c['badge'] = (('In progress', 'info') if c['status'] == 'active' else (c['status'], 'ok' if c['status'] == 'completed' else 'idle'))
    for r in d['probation']['reviews']:
        r['rec'] = REC.get(r['recommendation'], r['recommendation'])
        r['tone'] = 'ok' if r['status'] == 'approved' else 'warn' if r['status'] == 'pending' else 'idle'
    for x in d['exits']:
        x['kind_label'] = EXIT_KIND_LABEL.get(x['kind'], x['kind'])
        x['lwd'] = x['accepted_lwd'] or x['proposed_lwd']
    hr = has_role(user, 'hr')
    return render(request, 'lifecycle/partials/employee_section.html', {
        **d, 'id': str(id), 'hr': hr, 'onboarding': next((c for c in d['cases'] if c['kind'] == 'onboarding'), None),
        'pending_review': any(r['status'] == 'pending' for r in d['probation']['reviews']),
    })


@require_GET
@login_required
@require_feature('lifecycle')
def my_checklist(request):
    """MyChecklistCard.tsx: the Home card. Empty (200) when there is nothing to do."""
    user = request.hajiri_user
    tasks = [_task_view({**t, 'can_complete': True}) for t in api.my_tasks(user)]
    if not tasks:
        return HttpResponse('<div id="my-checklist" hx-get="%s" hx-trigger="hj:refresh-my-checklist from:body" hx-swap="outerHTML"></div>'
                            % request.path)
    own = [t for t in tasks if t['own']]
    others = [t for t in tasks if not t['own']]
    groups: dict[str, list] = {}
    for t in others:
        groups.setdefault(t['employee_id'], []).append(t)
    group_list = [{'name': g[0]['employee_name'], 'kind': g[0]['kind'], 'tasks': g[:5], 'more': max(0, len(g) - 5)} for g in list(groups.values())[:6]]
    return render(request, 'lifecycle/partials/my_checklist.html', {'own': own, 'groups': group_list,
                                                                    'leaving': bool(own) and own[0]['kind'] == 'offboarding'})


@require_GET
@login_required
@require_feature('lifecycle')
def my_leaving(request):
    """The "Leaving the company" block of MyTicketsPage.tsx (Leaving): resign, follow notice and clearance, exit interview."""
    user = request.hajiri_user
    _seed(request)
    mine = api.my_exits(user)
    policy = mine['policy']
    current = next((x for x in mine['exits'] if x['status'] in ('pending', 'accepted')), None)
    if current:
        current['badge'] = EXIT_STATUS.get(current['status'], (current['status'], 'idle'))
    today = npt_today()
    return render(request, 'lifecycle/partials/my_leaving.html', {
        'assets': api.my_assets(user), 'current': current, 'policy': policy, 'reasons': RESIGN_REASONS, 'today': today,
        'default_lwd': today + timedelta(days=policy['noticeDays']), 'form': request.GET.get('form') == '1',
        'questions': [q for q in api.questions(user) if q['active']] if current and current['status'] == 'accepted' else [],
    })


@require_POST
@login_required
@require_feature('lifecycle')
def resign(request):
    p = request.POST
    api.resign(request.hajiri_user, {'proposedLastWorkingDay': p.get('proposedLastWorkingDay') or '', 'reasonCategory': _b(p.get('reasonCategory')),
                                     'reason': _b(p.get('reason'))})
    return _ok(request, 'Your resignation was sent to your manager.', 'my-leaving', close=False)
