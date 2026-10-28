"""
Hiring hub pages (/hiring, /hiring/pipeline, /hiring/interviews, /hiring/requisitions; HiringHub in pages/hubs.tsx)
and their HTMX fragments under /_/hiring/... The JSON API of the TS module was used only by the web app, so it became
these pages. Posts are read by the forms in hajiri.hiring.forms into the camelCase bodies the TS routes took, and
the services validate them with their serializers. Everything works in the active company: an id of another
company is not found, even for a GET that only draws a form.

Forms in a drawer (job, hiring request) close everything on success. Forms in the shared modal opened from the
candidate drawer (interview, offer, hire, reject, scorecard) close only the modal: their root element listens for
`hj:hiring-done` and the drawer reloads on `hj:refresh-candidate`.
"""
from __future__ import annotations

from collections import OrderedDict

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils.html import format_html
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.api import ApiEncoder
from hajiri.core.auth import MODULE_ROLES, has_role
from hajiri.files.store import stored_file_response
from hajiri.web.decorators import login_required, require_feature, require_module, require_role
from hajiri.web.format import add_days, fmt_date_long, fmt_date_short, today_npt
from hajiri.web.htmx import close_modal, htmx_target, is_htmx, refresh, toast, trigger
from hajiri.web.tables import Column, Table, table_response

from . import forms
from . import labels as L
from . import services

STAFF = ('manager', *MODULE_ROLES['hiring'])


def _done(request, message: str | None, *refreshes: str, tone: str = 'ok', close: str = 'modal') -> HttpResponse:
    """204 with a toast; close='all' closes drawer and modal, 'modal' only the action modal above the drawer."""
    resp = HttpResponse(status=204)
    if close == 'all':
        close_modal(resp)
    elif close == 'modal':
        trigger(resp, 'hj:hiring-done', True)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message, tone) if message else resp


def _base(request) -> str:
    return request.build_absolute_uri('/').rstrip('/')


# ================================================================ jobs

def _careers(request, job_id=None, code=None) -> str:
    return services.careers_url(_base(request), job_id, code)


def _job_cell(r):
    sub = ' · '.join(x for x in (r.get('department'), r.get('location'), L.WORK_MODE.get(r['work_mode']),
                                L.EMPLOYMENT.get(r['employment_type'])) if x)
    return format_html('<div class="hj-strong hj-sm">{}</div><div class="hj-small hj-muted">{}</div>', r['title'], sub)


def _job_status(r):
    text, tone = L.label(L.JOB_STATUS, r['status'])
    return format_html('<span class="hj-badge hj-tone-{}">{}</span>', tone, text)


def _job_counts(r):
    extra = f", {r['new_this_week']} new this week" if r.get('new_this_week') else ''
    return format_html('<div class="hj-sm num">{} active</div><div class="hj-small hj-muted">{} total{}</div>',
                       r.get('active') or 0, r.get('applications') or 0, extra)


def _deadline(today):
    def f(r):
        if not r['deadline']:
            return 'No deadline'
        late = r['deadline'].isoformat() < today and r['status'] == 'open'
        return format_html('<span class="{}">{}</span>', 'hj-bad-text' if late else '', fmt_date_short(r['deadline']))
    return f


def _reports_ctx(user) -> dict:
    d = services.reports(user)
    t = d['timeToHire']
    stats = [{'label': 'Applications', 'value': d['totals']['applications']}, {'label': 'In progress', 'value': d['totals']['active']},
             {'label': 'Hired', 'value': d['totals']['hired']},
             {'label': 'Average time to hire', 'value': '—' if t['averageDays'] is None else f"{t['averageDays']} days",
              'hint': None if t['medianDays'] is None else f"Median {t['medianDays']} days"}]
    funnel = []
    for f in d['funnel'][:5]:
        total = f['total']
        stages = []
        for st in f['stages']:
            pct = services._half_up(100 * st['reached'] / total) if total else 0
            stages.append({'stage': st['stage'], 'reached': st['reached'], 'pct': pct,
                           'label': f"{st['reached']} ({pct}%)" if total else str(st['reached'])})
        funnel.append({'title': f['title'], 'stages': stages})
    sources = [{'label': L.SOURCE_LABEL.get(s['source'], s['source']), **s} for s in d['sources']]
    return {'stats': stats, 'funnel': funnel, 'sources': sources, 'has_funnel': bool(d['funnel'])}


@require_GET
@require_role(*STAFF)
@require_feature('hiring')
def jobs(request):
    user = request.hajiri_user
    status = request.GET.get('status') if request.GET.get('status') in ('open', 'draft', 'closed') else 'all'
    data = services.list_jobs(user, None if status == 'all' else status)
    hr = has_role(user, 'hr')
    code = data['companyCode']
    for j in data['jobs']:
        j['careers_url'] = _careers(request, j['id'], code)
        j['hr'] = hr
    cols = [Column('title', 'Job', render=_job_cell), Column('status', 'Status', render=_job_status),
            Column('applications', 'Candidates', align='right', render=_job_counts),
            Column('deadline', 'Apply by', render=_deadline(today_npt())),
            Column('hiring_manager_name', 'Hiring manager', render=lambda r: r['hiring_manager_name'] or '—'),
            Column('id', '', template='hiring/partials/job_actions.html', csv=False, align='right')]
    table = Table(request, data['jobs'], cols, id='jobs', page_size=None, row_url=lambda r: f"/hiring/pipeline?job={r['id']}",
                  empty_title='No jobs here',
                  empty_hint='Create a job, or turn an approved hiring request into one.' if hr
                  else 'Jobs for your department appear here once HR creates them.',
                  extra_params={'status': status} if status != 'all' else {})
    ctx = {'table': table, 'hr': hr, 'status': status, 'careers_url': _careers(request, None, code),
           'pills': [('all', 'All'), ('open', 'Open'), ('draft', 'Drafts'), ('closed', 'Closed')],
           'reports': _reports_ctx(user) if hr else None}
    return table_response(request, table, 'hiring/jobs.html', ctx)


def _lookups(user) -> dict:
    lk = services.lookups(user)
    for k in ('users', 'orgUnits', 'branches', 'shifts'):
        for r in lk[k]:
            r['id'] = str(r['id'])
    return lk


def _job_form_ctx(request, job=None, requisition=None) -> dict:
    user = request.hajiri_user
    if job:
        n = lambda v: '' if v is None else L._n(v)  # noqa: E731
        f = {'title': job['title'], 'category': job['category'] or '', 'location': job['location'] or '', 'workMode': job['work_mode'],
             'employmentType': job['employment_type'], 'openings': job['openings'], 'experienceMin': n(job['experience_min']),
             'experienceMax': n(job['experience_max']), 'salaryMin': n(job['salary_min']), 'salaryMax': n(job['salary_max']),
             'showSalary': job['show_salary'], 'deadline': job['deadline'].isoformat() if job['deadline'] else None,
             'orgUnitId': job['org_unit_id'], 'branchId': job['branch_id'], 'hiringManagerId': job['hiring_manager_id'],
             'description': job['description'], 'requirements': job['requirements'], 'stages': job['stages'],
             'questions': job['questions'], 'requisitionId': job['requisition_id']}
    else:
        r = requisition
        f = {'title': r['title'] if r else '', 'category': '', 'location': '', 'workMode': 'onsite', 'employmentType': 'full_time',
             'openings': (r['headcount'] - r['filled']) if r else 1, 'experienceMin': '', 'experienceMax': '',
             'salaryMin': L._n(r['salary_min']) if r and r['salary_min'] else '', 'salaryMax': L._n(r['salary_max']) if r and r['salary_max'] else '',
             'showSalary': False, 'deadline': None, 'orgUnitId': _sid(r and r['org_unit_id']), 'branchId': _sid(r and r['branch_id']),
             'hiringManagerId': None, 'description': '', 'requirements': '', 'stages': list(services.DEFAULT_STAGES), 'questions': [],
             'requisitionId': str(r['id']) if r else None}
    return {'job': job, 'f': f, 'lookups': _lookups(user), 'today': today_npt(),
            'alpine': {'stages': f['stages'], 'questions': [_editor_q(q) for q in f['questions']], 'title': f['title'], 'newStage': ''},
            'work_modes': [('onsite', 'On site'), ('hybrid', 'Hybrid'), ('remote', 'Remote')],
            'employment_types': list(L.EMPLOYMENT.items())}


def _editor_q(q: dict) -> dict:
    rule = 'knockout' if q.get('knockout') else 'preferred' if q.get('preferred') else 'none'
    rr = q.get('knockout') or q.get('preferred') or {}
    return {'id': q['id'], 'kind': q['kind'], 'label': q['label'], 'required': q.get('required', True), 'rule': rule,
            'equals': rr.get('equals') or 'yes', 'min': rr.get('min') if rr.get('min') is not None else 1}


def _sid(v):
    return None if v is None else str(v)


@require_http_methods(['GET', 'POST'])
@require_module('hiring')
@require_feature('hiring')
def job_new(request):
    user = request.hajiri_user
    req_id = request.GET.get('requisition') or request.POST.get('requisitionId') or None
    requisition = services.get_requisition(user, req_id) if req_id else None
    if request.method == 'POST':
        body = forms.JobForm(request.POST).to_body()
        body['requisitionId'] = req_id
        services.create_job(user, body)
        resp = _done(request, 'Job created as a draft. Publish it when it is ready.', 'jobs', 'requisitions', close='all')
        if requisition:
            toast(request, resp, 'Find the new job under Jobs and publish it when ready.', 'info')
        return resp
    return render(request, 'hiring/partials/job_form.html', _job_form_ctx(request, requisition=requisition))


@require_http_methods(['GET', 'POST'])
@require_module('hiring')
@require_feature('hiring')
def job_edit(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        services.update_job(user, str(id), forms.JobForm(request.POST).to_body())
        return _done(request, 'Job saved.', 'jobs', close='all')
    job = services.get_job(user, str(id))['job']
    return render(request, 'hiring/partials/job_form.html', _job_form_ctx(request, job=job))


@require_POST
@require_module('hiring')
@require_feature('hiring')
def job_publish(request, id):
    services.publish_job(request.hajiri_user, str(id))
    return _done(request, 'Published. The job is on the careers page.', 'jobs', close=None)


@require_POST
@require_module('hiring')
@require_feature('hiring')
def job_close(request, id):
    services.close_job(request.hajiri_user, str(id))
    return _done(request, 'Closed. No new applications will be accepted.', 'jobs', close=None)


@require_http_methods(['GET', 'POST'])
@require_role(*STAFF)
@require_feature('hiring')
def policy(request):
    user = request.hajiri_user
    if request.method == 'POST':
        services.set_policy(user, forms.PolicyForm(request.POST).to_body())
        return _done(request, 'Hiring settings saved.', close='all')
    return render(request, 'hiring/partials/policy.html', {'p': services.policy(user)})


# ================================================================ requisitions

def _req_role(r):
    bits = [r['department'], r['branch'], f"Replacing {r['replacing_name'] or ''}" if r['kind'] == 'replacement' else 'New position']
    return format_html('<div class="hj-sm hj-strong">{} × {}</div><div class="hj-small hj-muted">{}</div>', r['headcount'], r['title'],
                       ' · '.join(b for b in bits if b))


def _req_status(r):
    text, tone = L.label(L.REQ_STATUS, r['status'])
    extra = ''
    if r['status'] in ('approved', 'filled'):
        extra = format_html('<div class="hj-small hj-muted">{} of {} hired · {} job{}</div>', r['filled'], r['headcount'], r['jobs'],
                            '' if r['jobs'] == 1 else 's')
    reason = format_html('<div class="hj-small hj-muted">{}</div>', r['decision_reason']) if r['decision_reason'] else ''
    return format_html('<span class="hj-badge hj-tone-{}">{}</span>{}{}', tone, text, extra, reason)


@require_GET
@require_role(*STAFF)
@require_feature('hiring')
def requisitions(request):
    user = request.hajiri_user
    reqs = services.list_requisitions(user)
    hr = has_role(user, 'hr')
    for r in reqs:
        r['id'] = str(r['id'])
        r['can_job'] = hr and r['status'] == 'approved'
        r['can_cancel'] = (hr or str(r['raised_by']) == user.id) and r['status'] in ('pending', 'approved')
    cols = [Column('title', 'Role', render=_req_role),
            Column('budget', 'Budget', render=lambda r: L.salary_text(r['salary_min'], r['salary_max'], '') or '—'),
            Column('raised_by_name', 'Raised by', render=lambda r: format_html(
                '<div class="hj-sm">{}</div><div class="hj-small hj-muted">{}</div>', r['raised_by_name'] or '—',
                fmt_date_short(r['created_at']))),
            Column('status', 'Status', render=_req_status),
            Column('id', '', template='hiring/partials/req_actions.html', csv=False, align='right')]
    table = Table(request, reqs, cols, id='requisitions', page_size=None, empty_title='No hiring requests',
                  empty_hint='Raise a request when your team needs another person.')
    return table_response(request, table, 'hiring/requisitions.html', {'table': table})


@require_http_methods(['GET', 'POST'])
@require_role(*STAFF)
@require_feature('hiring')
def requisition_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        services.create_requisition(user, forms.RequisitionForm(request.POST).to_body())
        return _done(request, 'Request sent to HR for approval.', 'requisitions', close='all')
    return render(request, 'hiring/partials/requisition_form.html', {'lookups': _lookups(user)})


@require_POST
@require_role(*STAFF)
@require_feature('hiring')
def requisition_cancel(request, id):
    services.cancel_requisition(request.hajiri_user, str(id))
    return _done(request, 'Request cancelled.', 'requisitions', close=None)


# ================================================================ pipeline

def _card(c: dict, stages: list[str]) -> dict:
    nxt = stages[stages.index(c['stage']) + 1] if c['stage'] in stages and stages.index(c['stage']) + 1 < len(stages) else None
    return {**c, 'score_badge': L.score_badge(c['score'], c['knocked_out']),
            'exp': 'Experience not given' if c['experience_years'] is None else f"{L._n(c['experience_years'])} yrs",
            'moves': [s for s in stages[:-1] if s != c['stage']],
            'next': nxt if c['status'] == 'active' and nxt and nxt != stages[-1] else None,
            'when': L.fmt_when(c['next_interview']) if c['next_interview'] else None}


def _board_ctx(request, job_id: str | None) -> dict:
    user = request.hajiri_user
    include = 'all' if request.GET.get('include') in ('all', '1', 'on') else 'active'
    data = services.list_jobs(user)
    js = data['jobs']
    if not job_id:
        job_id = next((j['id'] for j in js if j['status'] == 'open'), js[0]['id'] if js else None)
    ctx = {'jobs': js, 'job_id': job_id, 'include': include, 'hr': has_role(user, 'hr'), 'columns': []}
    if job_id:
        b = services.board(user, job_id, include)
        stages = b['stages']
        ctx['columns'] = [{'stage': s, 'cards': [_card(c, stages) for c in b['applications'] if c['stage'] == s]} for s in stages]
        job = next((j for j in js if j['id'] == str(job_id)), None)
        ctx['summary'] = f"{job.get('active') or 0} in progress · {job.get('applications') or 0} applications in total" if job else ''
    return ctx


@require_GET
@require_role(*STAFF)
@require_feature('hiring')
def pipeline(request):
    ctx = _board_ctx(request, request.GET.get('job') or None)
    ctx['open_candidate'] = request.GET.get('candidate') or None
    if is_htmx(request) and htmx_target(request) == 'board':
        return render(request, 'hiring/partials/board.html', ctx)
    return render(request, 'hiring/pipeline.html', ctx)


@require_POST
@require_role(*STAFF)
@require_feature('hiring')
def move(request, id):
    stage = request.POST.get('stage', '')
    services.move_application(request.hajiri_user, str(id), {'stage': stage})   # the stage name, checked against the job
    name = request.POST.get('name')
    return _done(request, f'{name} moved to {stage}.' if name else f'Moved to {stage}.', 'board', 'candidate', close=None)


@require_http_methods(['GET', 'POST'])
@require_module('hiring')
@require_feature('hiring')
def candidate_new(request, id):
    user = request.hajiri_user
    job = services.load_job(user, str(id))
    if request.method == 'POST':
        app_id = services.add_candidate(user, str(id), forms.CandidateForm(request.POST).to_body())
        resp = _done(request, 'Candidate added.', 'board', close='all')
        return trigger(resp, 'hj:open-candidate', {'url': f'/_/hiring/applications/{app_id}'})
    return render(request, 'hiring/partials/candidate_form.html', {'job': job})


# ================================================================ candidate drawer

def _detail_ctx(request, app_id: str) -> dict:
    user = request.hajiri_user
    d = services.application_detail(user, app_id)
    a, job = d['application'], d['job']
    full = d['access'] == 'full'
    active = a['status'] == 'active'
    offers = d['offers']
    accepted = next((o for o in offers if o['status'] == 'accepted'), None)
    open_offer = next((o for o in offers if o['status'] in services.OPEN_OFFER), None)
    last = job['stages'][-1] if job['stages'] else None
    for i in d['interviews']:
        cards = [s for s in d['scorecards'] if s['interview_id'] == i['id']]
        for s in cards:
            s['rec'] = L.label(L.RECOMMENDATION, s['recommendation'])
            s['ratings_text'] = ' · '.join(f'{k} {v}/5' for k, v in (s['ratings'] or {}).items())
        i['cards'] = cards
        i['mine'] = next((s for s in cards if s['reviewer_id'] == user.id), None)
        i['on_panel'] = user.id in i['panel']
        i['when'] = L.fmt_when(i['starts_at'])
        i['mode_label'] = L.MODE_LABEL.get(i['mode'], i['mode'])
        i['tone'] = 'idle' if i['status'] == 'cancelled' else 'ok' if i['status'] == 'completed' else 'info'
    for o in offers:
        o['badge'] = L.label(L.OFFER_STATUS, o['status'])
    for o in d['otherApplications']:
        o['badge'] = L.label(L.APP_STATUS, o['status'])
    for e in d['events']:
        e['label'] = L.EVENT_LABEL.get(e['kind'], e['kind'])
        e['when'] = L.fmt_when(e['created_at'])
    answers = []
    for q in job['questions']:
        v = a['answers'].get(q['id'])
        answers.append({'label': q['label'], 'text': '—' if v is None else {'yes': 'Yes', 'no': 'No'}.get(str(v), L._n(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v))})
    return {
        'd': d, 'a': a, 'job': job, 'full': full, 'active': active, 'hr': has_role(user, 'hr'), 'accepted': accepted, 'open_offer': open_offer,
        'status_badge': L.label(L.APP_STATUS, a['status']), 'score_badge': L.score_badge(a['score'], a['knocked_out']),
        'source': L.SOURCE_LABEL.get(a['source'], a['source']), 'stage_options': [s for s in job['stages'] if s != last or s == a['stage']],
        'answers': answers, 'tab': request.GET.get('tab') or 'profile',
        'experience': 'Not given' if a['experience_years'] is None else f"{L._n(a['experience_years'])} years",
        'stars': range(1, 6), 'tags_text': ', '.join(a['tags']),
    }


@require_GET
@login_required
@require_feature('hiring')
def candidate(request, id):
    return render(request, 'hiring/partials/candidate.html', _detail_ctx(request, str(id)))


@require_GET
@login_required
@require_feature('hiring')
def cv(request, id):
    return stored_file_response(services.cv_file(request.hajiri_user, str(id)), download=request.GET.get('download') == '1')


@require_POST
@require_role(*STAFF)
@require_feature('hiring')
def update(request, id):
    services.update_application(request.hajiri_user, str(id), forms.ApplicationForm(request.POST).to_body())
    return _done(request, None, 'candidate', 'board', close=None)


@require_http_methods(['GET', 'POST'])
@require_role(*STAFF)
@require_feature('hiring')
def reject(request, id):
    user = request.hajiri_user
    app, _, _ = services.load_app(user, str(id))
    if request.method == 'POST':
        emailed = services.reject_application(user, str(id), forms.RejectForm(request.POST).to_body())
        return _done(request, f"{app['name']} was told by e-mail." if emailed else 'Application closed.', 'candidate', 'board')
    return render(request, 'hiring/partials/reject.html', {'app': app})


@require_POST
@require_module('hiring')
@require_feature('hiring')
def reopen(request, id):
    services.reopen_application(request.hajiri_user, str(id))
    return _done(request, 'Application reopened.', 'candidate', 'board', close=None)


@require_POST
@require_role(*STAFF)
@require_feature('hiring')
def note(request, id):
    services.add_note(request.hajiri_user, str(id), forms.NoteForm(request.POST).to_body())
    return _done(request, 'Note added.', 'candidate', close=None)


@require_GET
@require_module('hiring')
@require_feature('hiring')
def export(request, id):
    data = services.export_candidate(request.hajiri_user, str(id))
    cand_id = data.pop('_candidate_id')
    res = JsonResponse(data, encoder=ApiEncoder, json_dumps_params={'ensure_ascii': False})
    res['Content-Disposition'] = f'attachment; filename="candidate-{cand_id}.json"'
    return res


# ================================================================ interviews

@require_http_methods(['GET', 'POST'])
@require_role(*STAFF)
@require_feature('hiring')
def interview_new(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        body = forms.InterviewForm(request.POST).to_body()
        notify_candidate = body['notifyCandidate']
        services.schedule_interview(user, str(id), body)
        return _done(request, 'Interview scheduled. The panel and the candidate have been told.' if notify_candidate
                     else 'Interview scheduled. The panel has been told.', 'candidate', 'board', 'interviews')
    app, _, _ = services.load_app(user, str(id))
    return render(request, 'hiring/partials/interview_form.html', {
        'app': app, 'users': _lookups(user)['users'], 'me': user.id, 'date': add_days(today_npt(), 2), 'today': today_npt()})


@require_POST
@require_role(*STAFF)
@require_feature('hiring')
def interview_update(request, id):
    body = forms.InterviewStatusForm(request.POST).to_body()
    status = body.get('status')
    services.update_interview(request.hajiri_user, str(id), body)
    return _done(request, 'Interview cancelled.' if status == 'cancelled' else 'Marked as done.', 'candidate', 'interviews', close=None)


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('hiring')
def scorecard(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        services.submit_scorecard(user, str(id), forms.ScorecardForm(request.POST).to_body())
        return _done(request, 'Scorecard saved.', 'candidate', 'interviews')
    i = services.interview_for_scorecard(user, str(id))
    # A fresh scorecard each time it is opened, as ScorecardModal does; saving replaces the earlier one.
    return render(request, 'hiring/partials/scorecard.html', {
        'i': i, 'criteria': [{'n': n, 'label': c, 'value': 0} for n, c in enumerate(L.DEFAULT_CRITERIA)],
        'criteria_values': [0] * len(L.DEFAULT_CRITERIA), 'rec': '', 'notes': '', 'stars': range(1, 6),
        'recs': [(k, L.RECOMMENDATION[k][0]) for k in reversed(services.RECOMMENDATIONS)]})


@require_GET
@require_role(*STAFF)
@require_feature('hiring')
def interviews(request):
    user = request.hajiri_user
    scope = 'all' if request.GET.get('scope') == 'all' else 'mine'
    when = 'past' if request.GET.get('when') == 'past' else 'upcoming'
    today = today_npt()
    rng = {'from': today} if when == 'upcoming' else {'from': add_days(today, -90), 'to': add_days(today, -1)}
    items = services.list_interviews(user, {'scope': scope, **rng})
    if when == 'past':
        items.reverse()
    days: OrderedDict[str, list] = OrderedDict()
    for i in items:
        i['time'] = i['starts_at']
        i['mode_label'] = L.MODE_LABEL.get(i['mode'], i['mode'])
        days.setdefault(L.npt_date_of(i['starts_at']), []).append(i)
    groups = [{'label': f'Today, {fmt_date_long(d)}' if d == today else fmt_date_long(d), 'items': v} for d, v in days.items()]
    ctx = {'groups': groups, 'scope': scope, 'when': when,
           'empty_title': 'No interviews coming up' if when == 'upcoming' else 'No interviews in the last 90 days'}
    if is_htmx(request) and htmx_target(request) == 'interviews':
        return render(request, 'hiring/partials/interviews.html', ctx)
    return render(request, 'hiring/interviews.html', ctx)


# ================================================================ offers and hire

@require_http_methods(['GET', 'POST'])
@require_module('hiring')
@require_feature('hiring')
def offer_new(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        services.create_offer(user, str(id), forms.OfferForm(request.POST).to_body())
        return _done(request, 'Offer drafted. Another HR user needs to approve it before it goes to the candidate.', 'candidate', 'board')
    app, job, _ = services.load_app(user, str(id))
    today = today_npt()
    return render(request, 'hiring/partials/offer_form.html', {
        'app': app, 'designation': job['title'], 'today': today, 'join': add_days(today, 30), 'expires': add_days(today, 7)})


@require_GET
@require_role(*STAFF)
@require_feature('hiring')
def offer_letter(request, id):
    return HttpResponse(services.offer_letter(request.hajiri_user, str(id)), content_type='text/html; charset=utf-8')


@require_POST
@require_module('hiring')
@require_feature('hiring')
def offer_respond(request, id):
    body = forms.RespondForm(request.POST).to_body()
    response = body['response']
    services.respond_offer(request.hajiri_user, str(id), body)
    return _done(request, 'Marked as accepted. You can hire the candidate now.' if response == 'accepted' else 'Marked as declined.',
                 'candidate', close=None)


@require_POST
@require_module('hiring')
@require_feature('hiring')
def offer_withdraw(request, id):
    services.withdraw_offer(request.hajiri_user, str(id))
    return _done(request, 'Offer withdrawn.', 'candidate', 'board', close=None)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('hiring')
def hire(request, id):
    user = request.hajiri_user
    if request.method == 'POST':
        services.hire(user, str(id), forms.HireForm(request.POST).to_body())
        return _done(request, 'Employee record created.', 'candidate', 'board')
    d = services.application_detail(user, str(id))
    accepted = next((o for o in d['offers'] if o['status'] == 'accepted'), None)
    return render(request, 'hiring/partials/hire_form.html', {
        'app': d['application'], 'join_date': accepted and accepted['join_date'], 'shifts': _lookups(user)['shifts']})

