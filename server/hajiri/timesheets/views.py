"""
Time tracking on the web (apps/web/src/pages/time): the employee's own week at /me/timesheet (MyTimesheetPage.tsx
with TimerCard, EntryModal and WeekDays), the review page at /attendance/timesheets (TimesheetsReviewPage.tsx: to
approve, this week, history, hours report and the review drawer), the catalogue at /attendance/projects
(ProjectsPage.tsx) and two fragments other pages embed: the Home project timer (HomeTimer.tsx) and the time tracking
settings card (TimeSettings.tsx).
"""
from __future__ import annotations

from datetime import date

from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.clock import now
from hajiri.core.errors import BadRequestError, HttpError
from hajiri.db.models import Employee
from hajiri.web.csv import csv_response
from hajiri.web.decorators import login_required, require_feature, require_module, require_role
from hajiri.web.format import add_days, fmt_date_short, minutes_to_hm
from hajiri.web.htmx import close_modal, htmx_target, refresh, toast, trigger
from hajiri.web.middleware import error_message
from hajiri.web.nav import area
from hajiri.workflow import service as wf

from . import services
from .forms import BRAND, ArchiveForm, DecisionForm, EntryForm, NameForm, NoteForm, ProjectForm, SettingsForm, TimerForm

WEEK_EVENT = 'timesheet-week'
TIMER_EVENT = 'timesheet-timer'
REVIEW_EVENT = 'timesheets-review'


# ---------------------------------------------------------------- helpers

def _today() -> str:
    return services.npt_today()


def _err(request, exc: HttpError, title: str) -> HttpResponse:
    """toast.err(e, title): a red toast with the TS title and no swap."""
    r = HttpResponse(error_message(exc), status=exc.status, content_type='text/plain; charset=utf-8')
    r.headers['HX-Reswap'] = 'none'
    return trigger(r, 'hj:toast', {'message': error_message(exc), 'tone': 'err', 'title': title})


def _iso_or(v: str | None, default: str) -> str:
    try:
        return date.fromisoformat((v or '')[:10]).isoformat() if v else default
    except ValueError:
        return default


def _week_label(ws: str) -> str:
    return f'{fmt_date_short(ws)} to {fmt_date_short(add_days(ws, 6))}'


def _work_options(user) -> dict:
    """useWorkOptions: the projects and activities this person may log to."""
    projects = services.list_projects(user)
    return {'projects': projects, 'activities': services.list_activities(user)}


def _project_label(p: dict) -> str:
    return f"{p['name']} ({p['code']}){f' · ' + p['client_name'] if p.get('client_name') else ''}"


def _elapsed(started_at) -> int:
    if not started_at:
        return 0
    return max(0, int((now() - started_at).total_seconds()))


# ================================================================ timer (TimerCard)

def _timer_ctx(request, compact: bool, prefill: dict | None = None) -> dict:
    user = request.hajiri_user
    policy = services.load_time_policy(company_id_of(user))
    t = services.timer_state(user)
    opts = _work_options(user)
    for p in opts['projects']:
        p['label'] = _project_label(p)
    running = t['running']
    # TimerCard keeps the project, activity and note of the timer it just stopped, ready for the next Start.
    pre = {'project_id': '', 'activity_id': '', 'note': ''}
    if prefill and not running:
        pid = prefill.get('project_id') or ''
        if pid and not any(p['id'] == pid for p in opts['projects']):
            pid = ''
        pre = {'project_id': pid, 'activity_id': prefill.get('activity_id') or '', 'note': prefill.get('note') or ''}
    start_activities = [a for a in opts['activities']
                        if not a['project_id'] or (pre['project_id'] and a['project_id'] == pre['project_id'])]
    return {'t': t, 'running': running, 'policy': policy, 'compact': compact, 'elapsed': _elapsed(running and running['started_at']),
            'pre': pre, 'start_activities': start_activities, **opts}


def _timer_response(request, message: str | None = None, status: int = 200, prefill: dict | None = None) -> HttpResponse:
    compact = (request.POST.get('compact') or request.GET.get('compact')) == '1'
    user = request.hajiri_user
    if not services.load_time_policy(company_id_of(user))['enabled']:
        return HttpResponse('')
    resp = render(request, 'timesheets/partials/timer.html', _timer_ctx(request, compact, prefill), status=status)
    if message:
        refresh(resp, WEEK_EVENT)
        toast(request, resp, message)
    return resp


@require_GET
@login_required
@require_feature('time')
def timer(request):
    return _timer_response(request)


def _timer_action(request, fn, message: str, keep: bool = False) -> HttpResponse:
    try:
        out = fn()
    except HttpError as e:
        return _err(request, e, 'Timer')
    return _timer_response(request, message, prefill=out if keep else None)


@require_POST
@login_required
@require_feature('time')
def timer_start(request):
    body = TimerForm(request.POST).start()
    return _timer_action(request, lambda: services.start_timer(request.hajiri_user, body), 'Timer started')


@require_POST
@login_required
@require_feature('time')
def timer_stop(request):
    body = TimerForm(request.POST).stop()
    return _timer_action(request, lambda: services.stop_timer(request.hajiri_user, body), 'Time saved', keep=True)


@require_POST
@login_required
@require_feature('time')
def timer_resume(request):
    return _timer_action(request, lambda: services.resume_timer(request.hajiri_user), 'Timer resumed')


@require_GET
@login_required
@require_feature('time')
def home_timer(request):
    """HomeTimer.tsx: the compact timer on Home with "Open timesheet"; empty when the company does not track time."""
    user = request.hajiri_user
    if not services.load_time_policy(company_id_of(user))['enabled'] or not user.employee_id:
        return HttpResponse('')
    return render(request, 'timesheets/partials/home_timer.html', _timer_ctx(request, True))


@require_GET
@login_required
@require_feature('time')
def activity_options(request):
    """The activity choices for a project (activityData: company-wide ones plus the project's own)."""
    project_id = (request.GET.get('project_id') or '').strip() or None
    acts = services.list_activities(request.hajiri_user)
    acts = [a for a in acts if not a['project_id'] or a['project_id'] == project_id]
    return render(request, 'timesheets/partials/activity_options.html', {'activities': acts, 'selected': request.GET.get('selected') or ''})


# ================================================================ my week (MyTimesheetPage)

def _week_ctx(request, week: str) -> dict:
    user = request.hajiri_user
    this_week = services.week_start_of(_today())
    w = services.week(user, {'weekStart': week})
    ts = w['timesheet']
    locked = bool(ts and ts['status'] in ('submitted', 'approved'))
    hint = _week_label(w['weekStart'])
    if w['rounding']['step']:
        hint += f" · rounded {w['rounding']['mode']} to {w['rounding']['step']} min"
    today = _today()
    t = w['totals']
    stats = [{'label': 'Logged', 'value': minutes_to_hm(t['roundedMin'])},
             {'label': 'Billable', 'value': minutes_to_hm(t['billableMin'])},
             {'label': 'Attended', 'value': minutes_to_hm(t['attendanceMin']), 'hint': 'From your clock-ins'},
             {'label': 'Days to check', 'value': t['flaggedDays'], **({'tone': 'warn'} if t['flaggedDays'] > 0 else {})}]
    for d in w['days']:
        for e in d['entries']:
            e['delete_message'] = (f"{minutes_to_hm(e['duration_min'])} on {fmt_date_short(e['local_date'])}"
                                   f"{' for ' + e['project_name'] if e['project_name'] else ''} will be removed.")
    add_date = today if w['weekStart'] == this_week else w['weekStart']
    returned_title = f"Returned by {ts['decided_by_name'] or 'your manager'}" if ts else ''
    return {'w': w, 'ts': ts, 'locked': locked, 'stats': stats, 'returned_title': returned_title,
            'add_attrs': format_html('hx-get="{}?date={}" hx-target="#hj-modal-body" hx-swap="innerHTML"', reverse('timesheets:entry_new'), add_date), 'week': w['weekStart'], 'this_week': this_week, 'today': today,
            'prev_week': add_days(w['weekStart'], -7), 'next_week': add_days(w['weekStart'], 7),
            'has_time': w['totals']['trackedMin'] > 0, 'hint': hint,
            'add_date': add_date, 'editable': not locked}


@require_GET
@login_required
@require_feature('time')
def my_timesheet(request):
    user = request.hajiri_user
    policy = services.load_time_policy(company_id_of(user))
    if not policy['enabled']:
        return render(request, 'timesheets/mine.html', {'disabled': True})
    this_week = services.week_start_of(_today())
    week = services.week_start_of(_iso_or(request.GET.get('week'), this_week))
    return render(request, 'timesheets/mine.html', {'disabled': False, **_week_ctx(request, week), **_timer_ctx(request, False)})


@require_GET
@login_required
@require_feature('time')
def week_partial(request):
    this_week = services.week_start_of(_today())
    week = services.week_start_of(_iso_or(request.GET.get('week'), this_week))
    return render(request, 'timesheets/partials/week.html', _week_ctx(request, week))


# ---------------------------------------------------------------- entries (EntryModal)

def _entry_form(request, entry: dict | None, default_date: str) -> HttpResponse:
    from hajiri.web.format import fmt_time
    opts = _work_options(request.hajiri_user)
    for p in opts['projects']:
        p['label'] = _project_label(p)
    running = bool(entry and entry['duration_min'] is None)
    ctx = {
        'entry': entry, 'running': running, 'today': _today(), **opts,
        'mode': ('range' if entry['started_at'] else 'duration') if entry else 'range',
        'date': entry['local_date'] if entry else default_date,
        'start': (fmt_time(entry['started_at']) if entry and entry['started_at'] else '09:00'),
        'end': (fmt_time(entry['ended_at']) if entry and entry['ended_at'] else '10:00'),
        'duration': (entry['duration_min'] or 60) if entry else 60,
        'project_id': (entry or {}).get('project_id') or '', 'activity_id': (entry or {}).get('activity_id') or '',
        'note': (entry or {}).get('note') or '', 'billable': 'true' if entry and entry['billable'] else ('false' if entry else ''),
    }
    ctx['project_billable'] = next((p['billable'] for p in opts['projects'] if p['id'] == ctx['project_id']), False)
    ctx['activities_for'] = [a for a in opts['activities'] if not a['project_id'] or a['project_id'] == ctx['project_id']]
    return render(request, 'timesheets/partials/entry_form.html', ctx)


def _entry_saved(request, message: str) -> HttpResponse:
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, WEEK_EVENT)
    refresh(resp, TIMER_EVENT)
    return toast(request, resp, message)


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('time')
def entry_new(request):
    if request.method == 'GET':
        services.own_employee(request.hajiri_user)
        return _entry_form(request, None, _iso_or(request.GET.get('date'), _today()))
    services.add_entry(request.hajiri_user, EntryForm(request.POST).body(running=False))
    return _entry_saved(request, 'Time added')


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('time')
def entry_edit(request, id):
    entry = services.get_entry(request.hajiri_user, str(id))
    if request.method == 'GET':
        return _entry_form(request, entry, entry['local_date'])
    body = EntryForm(request.POST).body(running=entry['duration_min'] is None)
    services.update_entry(request.hajiri_user, str(id), body)
    return _entry_saved(request, 'Entry updated')


@require_POST
@login_required
@require_feature('time')
def entry_delete(request, id):
    services.delete_entry(request.hajiri_user, str(id))
    resp = HttpResponse(status=204)
    refresh(resp, WEEK_EVENT)
    refresh(resp, TIMER_EVENT)
    return toast(request, resp, 'Entry deleted')


# ---------------------------------------------------------------- submit and withdraw

@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('time')
def submit(request):
    user = request.hajiri_user
    src = request.POST if request.method == 'POST' else request.GET
    week = services.week_start_of(_iso_or(src.get('week'), _today()))
    if request.method == 'GET':
        w = services.week(user, {'weekStart': week})
        return render(request, 'timesheets/partials/submit_form.html', {'w': w, 'week': week,
                                                                       'label': _week_label(w['weekStart'])})
    note = NoteForm(request.POST).note_value()
    try:
        services.submit_timesheet(user, {'weekStart': week, **({'note': note} if note else {})})
    except HttpError as e:
        return _err(request, e, 'Not submitted')
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, WEEK_EVENT)
    return toast(request, resp, 'Timesheet sent for approval')


@require_POST
@login_required
@require_feature('time')
def withdraw(request, id):
    services.withdraw_timesheet(request.hajiri_user, str(id))
    resp = HttpResponse(status=204)
    refresh(resp, WEEK_EVENT)
    return toast(request, resp, 'Timesheet withdrawn. You can edit the week again.')


# ================================================================ review (TimesheetsReviewPage)

HISTORY_PAGE_SIZE = 50
REVIEW_VIEWS = (('approve', 'To approve'), ('week', 'This week'), ('history', 'History'), ('report', 'Hours report'))
STATUS_FILTER = (('submitted', 'Waiting for approval'), ('approved', 'Approved'), ('rejected', 'Returned'), ('cancelled', 'Withdrawn'))
GROUP_LABELS = (('project', 'Project'), ('client', 'Client'), ('employee', 'Person'), ('activity', 'Activity'), ('day', 'Day'))


def _with_week_end(items: list[dict]) -> list[dict]:
    for t in items:
        t['week_end'] = add_days(t['week_start'], 6)
    return items


def _review_ctx(request, view: str) -> dict:
    user = request.hajiri_user
    g = request.GET
    ctx: dict = {'view': view, 'views': REVIEW_VIEWS, 'is_hr': has_role(user, 'hr')}
    if view == 'approve':
        ctx['timesheets'] = _with_week_end(services.list_timesheets(user, {'status': 'submitted'}))
    elif view == 'history':
        status = g.get('status') if g.get('status') in services.STATUSES else ''
        qs = services.timesheets_queryset(user, {'status': status} if status else {})
        page = Paginator(services.timesheet_values(qs), HISTORY_PAGE_SIZE).get_page(g.get('page'))
        ctx.update({'timesheets': _with_week_end(services.timesheet_rows(page)), 'page': page, 'status': status,
                    'statuses': STATUS_FILTER})
    elif view == 'week':
        this_week = services.week_start_of(_today())
        week = services.week_start_of(_iso_or(g.get('week'), this_week))
        tw = services.team_week(user, {'weekStart': week})
        people = tw['people']
        for p in people:
            p['diff_min'] = p['tracked_min'] - p['attendance_min']
        submitted = sum(1 for p in people if p['status'] in ('submitted', 'approved'))
        missing = len(people) - submitted
        from hajiri.web.format import minutes_to_hm
        ctx.update({'tw': tw, 'people': people, 'week': week, 'this_week': this_week, 'label': _week_label(week),
                    'prev_week': add_days(week, -7), 'next_week': add_days(week, 7),
                    'stats': [{'label': 'People', 'value': len(people)}, {'label': 'Submitted', 'value': submitted},
                              {'label': 'Not submitted', 'value': missing, **({'tone': 'warn'} if missing > 0 else {})},
                              {'label': 'Logged', 'value': minutes_to_hm(sum(p['tracked_min'] for p in people))}]})
    else:
        today = _today()
        frm = _iso_or(g.get('from'), add_days(today, -29))
        to = _iso_or(g.get('to'), today)
        group_by = g.get('group_by') if g.get('group_by') in services.GROUP_BY else 'project'
        r = services.report_summary(user, {'from': frm, 'to': to, 'groupBy': group_by})
        from hajiri.web.format import fmt_money
        from .templatetags.timesheet_tags import fixed2
        ctx.update({'report': r, 'from': frm, 'to': to, 'group_by': group_by, 'groups': GROUP_LABELS, 'today': today,
                    'label_header': 'Person' if group_by == 'employee' else group_by[0].upper() + group_by[1:],
                    'stats': [{'label': 'Hours', 'value': fixed2(r['totals']['hours'])},
                              {'label': 'Billable hours', 'value': fixed2(r['totals']['billableHours'])},
                              {'label': 'Billable amount', 'value': fmt_money(r['totals']['billableAmount'])}]})
    return ctx


@require_GET
@require_role('manager', *area('attendance'))
@require_feature('time')
def review(request):
    view = request.GET.get('view') if request.GET.get('view') in dict(REVIEW_VIEWS) else 'approve'
    ctx = _review_ctx(request, view)
    if htmx_target(request) == 'ts-review':
        return render(request, 'timesheets/partials/review_body.html', ctx)
    return render(request, 'timesheets/review.html', ctx)


@require_GET
@require_role('manager', *area('attendance'))
@require_feature('time')
def report_csv(request):
    """Summary CSV and All entries CSV (downloadSummary / downloadEntries)."""
    g = request.GET
    q = {'from': g.get('from') or '', 'to': g.get('to') or '', 'groupBy': g.get('group_by') or 'project', 'format': 'csv'}
    r = services.report_entries(request.hajiri_user, q) if g.get('kind') == 'entries' else services.report_summary(request.hajiri_user, q)
    return csv_response(r['filename'], r['columns'], r['rows'])


@require_GET
@login_required
@require_feature('time')
def detail(request, id):
    """ReviewDrawer: one submitted week with its days, and approve or return while it waits."""
    d = services.timesheet_detail(request.hajiri_user, str(id))
    sub = d['submission']
    can_decide = False
    if sub and sub['status'] == 'submitted' and sub['workflow_instance_id']:
        inst = wf.get_instance(sub['workflow_instance_id'])
        can_decide = bool(inst and inst['state'] == 'pending' and wf.can_act(request.hajiri_user, inst))
    from hajiri.web.format import minutes_to_hm
    t = d['totals']
    stats = [{'label': 'Logged', 'value': minutes_to_hm(t['roundedMin'])}, {'label': 'Attended', 'value': minutes_to_hm(t['attendanceMin'])},
             {'label': 'Billable', 'value': minutes_to_hm(t['billableMin'])},
             {'label': 'Days to check', 'value': t['flaggedDays'], **({'tone': 'warn'} if t['flaggedDays'] else {})}]
    return render(request, 'timesheets/partials/detail.html', {
        'd': d, 'sub': sub, 'stats': stats, 'can_decide': can_decide, 'today': _today(),
        'title': f"{d['employee']['name']} · {_week_label(d['weekStart'])}"})


@require_POST
@login_required
@require_feature('time')
def decide(request, id):
    user = request.hajiri_user
    d = services.timesheet_detail(user, str(id))
    sub = d['submission']
    if not sub or not sub['workflow_instance_id']:
        raise BadRequestError('timesheet is not waiting for approval')
    kind, reason = DecisionForm(request.POST).decision_value()
    if kind == 'reject' and not reason:
        return toast(request, HttpResponse(status=204), 'Say what needs fixing so they can correct it.', tone='info')
    if len(reason) > 1000:
        raise BadRequestError('reason: Too big: expected string to have <=1000 characters')
    wf.decide(user, sub['workflow_instance_id'], 'approve' if kind == 'approve' else 'reject', reason=reason or None)
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, REVIEW_EVENT)
    return toast(request, resp, 'Timesheet approved' if kind == 'approve' else 'Timesheet returned')


# ================================================================ projects, clients, activities (ProjectsPage)

PROJECT_VIEWS = (('projects', 'Projects'), ('clients', 'Clients'), ('activities', 'Activities'))
SWATCHES = (BRAND, '#0b7285', '#1971c2', '#6741d9', '#c2255c', '#e8590c', '#f08c00', '#495057')


def _projects_ctx(request, view: str) -> dict:
    user = request.hajiri_user
    ctx: dict = {'view': view, 'views': PROJECT_VIEWS}
    if view == 'projects':
        projects = services.list_projects(user, True)
        for p in projects:
            if p['budget_hours']:
                pct = services.round_half_up((p['tracked_min'] / 60 / float(p['budget_hours'])) * 100)
                p['budget_pct'] = pct
                p['budget_bar'] = min(pct, 100)
                p['budget_tone'] = 'bad' if pct >= 100 else 'warn' if pct >= 80 else 'ok'
                p['budget_num'] = services.whole(float(p['budget_hours']))
        ctx['projects'] = projects
    elif view == 'clients':
        ctx['clients'] = services.list_clients(user, True)
    else:
        ctx['activities'] = services.list_activities(user, {'all': '1'})
        ctx['active_projects'] = [p for p in services.list_projects(user, True) if p['status'] == 'active']
    return ctx


@require_GET
@require_module('attendance')
@require_feature('time')
def projects(request):
    view = request.GET.get('view') if request.GET.get('view') in dict(PROJECT_VIEWS) else 'projects'
    ctx = _projects_ctx(request, view)
    if htmx_target(request) == 'ts-catalogue':
        return render(request, 'timesheets/partials/catalogue.html', ctx)
    return render(request, 'timesheets/projects.html', ctx)


def _project_form(request, project: dict | None) -> HttpResponse:
    user = request.hajiri_user
    clients = [c for c in services.list_clients(user, True) if not c['archived']]
    people = [{**e, 'id': str(e['id'])} for e in Employee.objects.for_company(company_id_of(user))
              .filter(status=Employee.Status.ACTIVE).order_by('name_en').values('id', 'name_en', 'emp_code')]
    p = project or {'code': '', 'name': '', 'client_id': None, 'color': BRAND, 'billable': True, 'bill_rate': None,
                    'budget_hours': None, 'all_members': True, 'members': [], 'status': 'active'}
    return render(request, 'timesheets/partials/project_form.html', {
        'project': project, 'p': p, 'clients': clients, 'people': people, 'swatches': SWATCHES,
        'member_ids': [m['id'] for m in p.get('members') or []],
        'bill_rate': '' if p['bill_rate'] is None else services.whole(float(p['bill_rate'])),
        'budget_hours': '' if p['budget_hours'] is None else services.whole(float(p['budget_hours']))})


def _catalogue_saved(request, message: str) -> HttpResponse:
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, 'timesheets-catalogue')
    return toast(request, resp, message)


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
@require_feature('time')
def project_new(request):
    if request.method == 'GET':
        return _project_form(request, None)
    services.create_project(request.hajiri_user, ProjectForm(request.POST).body(editing=False))
    return _catalogue_saved(request, 'Project created')


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
@require_feature('time')
def project_edit(request, id):
    project = services.get_project(request.hajiri_user, str(id))
    if request.method == 'GET':
        return _project_form(request, project)
    services.update_project(request.hajiri_user, str(id), ProjectForm(request.POST).body(editing=True))
    return _catalogue_saved(request, 'Project saved')


@require_POST
@require_module('attendance')
@require_feature('time')
def client_new(request):
    services.create_client(request.hajiri_user, NameForm(request.POST).client())
    return _catalogue_saved(request, 'Client added')


@require_POST
@require_module('attendance')
@require_feature('time')
def client_archive(request, id):
    services.update_client(request.hajiri_user, str(id), ArchiveForm(request.POST).body())
    return _refresh_only('timesheets-catalogue')


@require_POST
@require_module('attendance')
@require_feature('time')
def activity_new(request):
    services.create_activity(request.hajiri_user, NameForm(request.POST).activity())
    return _catalogue_saved(request, 'Activity added')


@require_POST
@require_module('attendance')
@require_feature('time')
def activity_archive(request, id):
    services.update_activity(request.hajiri_user, str(id), ArchiveForm(request.POST).body())
    return _refresh_only('timesheets-catalogue')


def _refresh_only(target: str) -> HttpResponse:
    """The TS archive/restore toggles reload the list without a toast."""
    return refresh(HttpResponse(status=204), target)


# ================================================================ settings card (TimeSettings.tsx)

ROUNDING_STEPS = (('0', 'None'), ('5', '5 min'), ('6', '6 min'), ('10', '10 min'), ('15', '15 min'))
ROUNDING_MODES = (('nearest', 'Nearest'), ('up', 'Up'), ('down', 'Down'))


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
@require_feature('time')
def settings_card(request):
    """The Time tracking tab of Settings: GET the card, POST saves it (PUT /time/settings: the attendance area)."""
    user = request.hajiri_user
    if request.method == 'POST':
        policy = services.save_settings(user, SettingsForm(request.POST).policy())['policy']
        resp = render(request, 'timesheets/partials/settings_card.html', {'p': policy, 'steps': ROUNDING_STEPS, 'modes': ROUNDING_MODES})
        return toast(request, resp, 'Time tracking settings saved')
    policy = services.get_settings(user)['policy']
    return render(request, 'timesheets/partials/settings_card.html', {'p': policy, 'steps': ROUNDING_STEPS, 'modes': ROUNDING_MODES})

