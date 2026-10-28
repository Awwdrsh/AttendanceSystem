"""
Growth hub (/me/growth...) and Performance hub (/performance, /performance/reviews) pages, rebuilt from
apps/web/src/pages/performance/*. Lists live in a container with id "perf-main" that reloads itself (hx-select) after
any change; detail views (goal, review, 1-on-1) open in the shared drawer and re-render in place after an action.
"""
from __future__ import annotations

from functools import wraps

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.clock import NPT
from hajiri.core.collation import sorted_by_name
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.format import add_days, today_npt
from hajiri.web.htmx import close_modal, refresh, toast, trigger
from hajiri.web.middleware import _hub_redirect
from hajiri.web.nav import feature_on

from . import forms as f
from . import services as perf
from .common import fmt_number, num, round_half_up
from .reviews import PHASE_LABEL, next_phase

MAIN = 'perf-main'
DEFAULT_LABELS = ['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding']
NEXT_LABEL = {'self_review': 'Open manager reviews', 'manager_review': 'Next phase', 'peer_review': 'Start calibration',
              'calibration': 'Release results', 'released': 'Close cycle'}


def hub_tab(key):
    """
    Hub.tsx: a hub tab whose module is switched off sends the page to the first tab of the same hub that is still on
    (Growth with Performance off opens Learning); with nothing left, require_feature sends it home.
    """
    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            user = request.hajiri_user
            if (user and user.company_id and request.method == 'GET' and not request.headers.get('HX-Request')
                    and not feature_on(request, key)):
                landing = _hub_redirect(request)
                if landing:
                    return redirect(landing)
            return view(request, *args, **kwargs)
        return wrapped
    return deco


def feature(key):
    """login_required + the company feature switch."""
    def deco(view):
        return login_required(hub_tab(key)(require_feature(key)(view)))
    return deco


def staff(key):
    def deco(view):
        return require_role('manager', 'hr')(hub_tab(key)(require_feature(key)(view)))
    return deco


# ---------------------------------------------------------------- response helpers

def done(request, message: str, *, close: bool = False, extra: tuple[str, ...] = (), html: str | None = None, status: int = 200):
    resp = HttpResponse(html, status=status) if html is not None else HttpResponse(status=204)
    if close:
        close_modal(resp)
    refresh(resp, MAIN)
    for r in extra:
        refresh(resp, r)
    return toast(request, resp, message) if message else resp


def _pills(current: str, options: list[tuple[str, str]], param: str = 'view', extra: str = '') -> list[dict]:
    return [{'value': v, 'label': lbl, 'current': v == current, 'href': f'?{param}={v}{extra}'} for v, lbl in options]


def _team(user) -> list[dict]:
    hr = has_role(user, 'hr')
    return [c for c in perf.colleagues(user) if (c['id'] != user.employee_id if hr else c['manager_id'] == user.employee_id)]


# ================================================================ goals

def _goal_stats(goals: list[dict]) -> list[dict]:
    mine = [g for g in goals if g['status'] == 'active']
    if not mine:
        return []
    avg = round_half_up(sum(num(g['progress']) for g in mine) / len(mine))
    weight = sum(num(g['weight']) for g in mine)
    wstr = f'{weight:g}%'
    return [{'label': 'Active goals', 'value': len(mine)}, {'label': 'Average progress', 'value': f'{avg}%'},
            {'label': 'Total weight', 'value': wstr, **({'hint': 'Weights usually add up to 100%', 'tone': 'warn'} if 0 < weight != 100 else {})}]


@require_GET
@feature('performance')
def my_goals(request):
    user = request.hajiri_user
    view = request.GET.get('view') if request.GET.get('view') in ('mine', 'company') else 'mine'
    status = request.GET.get('status') if request.GET.get('status') in ('open', 'all') else 'open'
    goals = perf.list_goals(user, {'scope': view})
    shown = [g for g in goals if g['status'] in ('active', 'draft')] if status == 'open' else [g for g in goals if g['status'] != 'cancelled']
    return render(request, 'performance/my_goals.html', {
        'view': view, 'status': status, 'goals': shown, 'stats': _goal_stats(goals) if view == 'mine' else [],
        'views': _pills(view, [('mine', 'My goals'), ('company', 'Company goals')], extra=f'&status={status}'),
        'statuses': _pills(status, [('open', 'Open'), ('all', 'All')], 'status', f'&view={view}')})


TEAM_VIEWS = [('team', 'Team goals'), ('company', 'Company & department'), ('journal', 'Journal'), ('feedback', 'Feedback')]


@require_GET
@staff('performance')
def team_goals(request):
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    options = TEAM_VIEWS + ([('kpis', 'KPI library')] if hr else [])
    view = request.GET.get('view') if request.GET.get('view') in dict(options) else 'team'
    team = _team(user)
    person = request.GET.get('person') or None
    if person and person not in {t['id'] for t in team}:
        person = None
    ctx = {'view': view, 'views': _pills(view, options), 'hr': hr, 'team': team, 'person': person}
    if view == 'team':
        goals = perf.list_goals(user, {'scope': 'all' if hr else 'team'})
        shown = [g for g in goals if g['status'] != 'cancelled' and (not person or g['employee_id'] == person)]
        groups: dict[str, list] = {}
        for g in shown:
            groups.setdefault(g['employee_name'] or '', []).append(g)
        ctx['groups'] = [{'name': n, 'goals': gs, 'avg': round_half_up(sum(num(g['progress']) for g in gs) / len(gs))}
                         for n, gs in sorted_by_name(groups.items(), key=lambda x: x[0])]
        without = [t['name_en'] for t in team if not any(g['employee_id'] == t['id'] and g['status'] == 'active' for g in goals)]
        ctx['without'] = (', '.join(without[:8]) + (f' and {len(without) - 8} more' if len(without) > 8 else '')) if without and not person else ''
        ctx['any_goals'] = bool(shown)
    elif view == 'company':
        ctx['goals'] = perf.list_goals(user, {'scope': 'company'})
    elif view == 'kpis':
        ctx['kpis'] = perf.list_kpis(user)
    elif view == 'journal' and person:
        ctx['entries'] = perf.journal(user, {'employeeId': person})
        ctx['today'] = today_npt()
    elif view == 'feedback' and person:
        ctx['feedback'] = perf.feedback(user, {'box': 'about', 'employeeId': person})
    return render(request, 'performance/team_goals.html', ctx)


@require_http_methods(['GET', 'POST'])
@feature('performance')
def goal_new(request):
    user = request.hajiri_user
    mode = request.GET.get('for') or request.POST.get('for') or 'me'   # me | team | company
    if request.method == 'GET':
        employees = _team(user) if mode == 'team' else []
        return render(request, 'performance/partials/goal_form.html', {
            'mode': mode, 'employees': employees, 'default_employee': request.GET.get('employee') or '',
            'parents': perf.list_goals(user, {'scope': 'company'}),
            'departments': perf.departments(user) if mode == 'company' else []})
    perf.create_goal(user, f.GoalForm(request.POST).body())
    return done(request, 'Goal saved.', close=True)


def _goal_drawer(request, goal_id, status=200):
    d = perf.goal_detail(request.hajiri_user, goal_id)
    return render(request, 'performance/partials/goal_detail.html', {'d': d, 'g': d['goal']}, status=status)


@require_GET
@feature('performance')
def goal_detail(request, id):
    return _goal_drawer(request, str(id))


@require_POST
@feature('performance')
def goal_checkin(request, id):
    goal = perf.goal_detail(request.hajiri_user, str(id))['goal']
    perf.checkin(request.hajiri_user, str(id), f.CheckinForm(request.POST).body(goal))
    resp = _goal_drawer(request, str(id))
    return done(request, 'Check-in saved.', html=resp.content)


@require_POST
@feature('performance')
def goal_status(request, id):
    status = f.GoalStatusForm(request.POST).cleaned()['status']
    perf.update_goal(request.hajiri_user, str(id), {'status': status})
    resp = _goal_drawer(request, str(id))
    return done(request, 'Marked complete.' if status == 'completed' else 'Reopened.', html=resp.content)


@require_POST
@feature('performance')
def goal_cancel(request, id):
    perf.cancel_goal(request.hajiri_user, str(id))
    return done(request, 'Goal cancelled.', close=True)


@require_http_methods(['GET', 'POST'])
@feature('performance')
def kpi_apply(request):
    user = request.hajiri_user
    if request.method == 'GET':
        kpis = perf.list_kpis(user)
        return render(request, 'performance/partials/kpi_apply.html', {
            'kpis': kpis, 'employee': request.GET.get('employee') or '',
            'kpi_info': {str(k['id']): {'d': k['description'] or '', 't': fmt_number(k['default_target'])} for k in kpis}})
    form = f.KpiApplyForm(request.POST)
    perf.apply_kpi(user, form.kpi(), form.body())
    return done(request, 'Goal added.', close=True)


@require_http_methods(['GET', 'POST'])
@staff('performance')
def kpi_edit(request, id=None):
    user = request.hajiri_user
    if request.method == 'GET':
        kpi = perf.get_kpi(user, str(id)) if id else None
        return render(request, 'performance/partials/kpi_form.html', {'kpi': kpi or {'measure': 'number', 'default_target': 100, 'active': True}})
    body = f.KpiForm(request.POST).body()
    if id:
        perf.update_kpi(user, str(id), body)
    else:
        perf.create_kpi(user, body)
    return done(request, 'KPI saved.', close=True)


# ================================================================ journal and feedback

@require_POST
@staff('performance')
def journal_add(request):
    perf.add_journal(request.hajiri_user, f.JournalForm(request.POST).body())
    return done(request, 'Note saved.')


@require_POST
@feature('performance')
def journal_delete(request, id):
    perf.delete_journal(request.hajiri_user, str(id))
    return done(request, '')


BOXES = [('received', 'Received'), ('given', 'Given'), ('requests', 'Requests')]


@require_GET
@feature('performance')
def feedback_page(request):
    user = request.hajiri_user
    box = request.GET.get('box') if request.GET.get('box') in dict(BOXES) else 'received'
    rows_ = perf.feedback(user, {'box': box})
    for f in rows_:
        f['to_answer'] = box == 'requests' and f['status'] == 'requested' and f['giver_employee_id'] == user.employee_id
    return render(request, 'performance/feedback.html', {'box': box, 'boxes': _pills(box, BOXES, 'box'), 'rows': rows_, 'me': user.employee_id})


@require_http_methods(['GET', 'POST'])
@feature('performance')
def feedback_give(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'performance/partials/feedback_give.html', {
            'colleagues': [c for c in perf.colleagues(user) if c['id'] != user.employee_id], 'request_': None})
    perf.give_feedback(user, f.FeedbackForm(request.POST).give())
    return done(request, 'Feedback sent.', close=True)


@require_http_methods(['GET', 'POST'])
@feature('performance')
def feedback_answer(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'performance/partials/feedback_give.html', {'request_': perf.get_feedback_request(user, str(id))})
    perf.answer_feedback(user, str(id), f.FeedbackForm(request.POST).answer())
    return done(request, 'Feedback sent.', close=True)


@require_POST
@feature('performance')
def feedback_decline(request, id):
    perf.decline_feedback(request.hajiri_user, str(id))
    return done(request, 'Declined.')


@require_http_methods(['GET', 'POST'])
@feature('performance')
def feedback_ask(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'performance/partials/feedback_ask.html',
                      {'colleagues': [c for c in perf.colleagues(user) if c['id'] != user.employee_id]})
    perf.request_feedback(user, f.FeedbackAskForm(request.POST).body())
    resp = done(request, 'Request sent.', close=True)
    resp.headers['HX-Redirect'] = reverse('performance:feedback') + '?box=requests'
    return resp


# ================================================================ 1-on-1s

@require_GET
@feature('performance')
def one_on_ones(request):
    user = request.hajiri_user
    show = request.GET.get('show') if request.GET.get('show') in ('upcoming', 'past') else 'upcoming'
    d = perf.meetings(user)
    ms = [m for m in d['meetings'] if (m['status'] == 'scheduled') == (show == 'upcoming')]
    people = ([{**d['manager'], 'rel': 'Your manager'}] if d['manager'] else []) + [{**r, 'rel': 'Reports to you'} for r in d['reports']]
    return render(request, 'performance/one_on_ones.html', {
        'show': show, 'shows': _pills(show, [('upcoming', 'Upcoming'), ('past', 'Past')], 'show'), 'meetings': ms, 'people': people})


@require_http_methods(['GET', 'POST'])
@feature('performance')
def meeting_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        d = perf.meetings(user)
        people = ([{**d['manager'], 'rel': 'Your manager'}] if d['manager'] else []) + [{**r, 'rel': 'Reports to you'} for r in d['reports']]
        return render(request, 'performance/partials/meeting_form.html', {'people': people, 'date': add_days(today_npt(), 1)})
    mid = perf.create_meeting(user, f.MeetingForm(request.POST).body())
    resp = _meeting_drawer(request, mid)
    resp = done(request, '1-on-1 scheduled.', html=resp.content)
    resp.headers['HX-Retarget'] = '#hj-drawer-body'
    resp.headers['HX-Reswap'] = 'innerHTML'
    trigger(resp, 'hj:close-modal', True)
    return resp


def _meeting_drawer(request, mid, status=200):
    d = perf.meeting_detail(request.hajiri_user, mid)
    m = d['meeting']
    local = m['scheduled_at'].astimezone(NPT)
    return render(request, 'performance/partials/meeting_detail.html', {
        'd': d, 'm': m, 'other': m['employee_name'] if m['side'] == 'manager' else m['manager_name'],
        'agenda': [i for i in d['items'] if i['kind'] == 'agenda'], 'actions': [i for i in d['items'] if i['kind'] == 'action'],
        'date': local.date().isoformat(), 'time': local.strftime('%H:%M')}, status=status)


@require_GET
@feature('performance')
def meeting_detail(request, id):
    return _meeting_drawer(request, str(id))


@require_POST
@feature('performance')
def meeting_update(request, id):
    user = request.hajiri_user
    form = f.MeetingUpdateForm(request.POST)
    action = form.action_name()
    mid = str(id)
    if action == 'cancel':
        perf.update_meeting(user, mid, {'status': 'cancelled'})
        msg = 'Cancelled.'
    elif action == 'reschedule':
        perf.update_meeting(user, mid, {'scheduledAt': form.scheduled_at()})
        msg = 'Rescheduled.'
    elif action == 'done':
        nxt = perf.update_meeting(user, mid, {'status': 'done', **form.notes()})
        if nxt:
            return done(request, 'Done. The next one is scheduled.', html=_meeting_drawer(request, nxt).content)
        msg = 'Marked done.'
    else:
        perf.update_meeting(user, mid, form.notes())
        msg = 'Notes saved.'
    return done(request, msg, html=_meeting_drawer(request, mid).content)


@require_POST
@feature('performance')
def meeting_item_add(request, id):
    perf.add_meeting_item(request.hajiri_user, str(id), f.MeetingItemForm(request.POST).body())
    return done(request, '', html=_meeting_drawer(request, str(id)).content)


@require_POST
@feature('performance')
def meeting_item_toggle(request, id):
    m = perf.update_meeting_item(request.hajiri_user, str(id), {'done': f.DoneForm(request.POST).cleaned()['done']})
    return done(request, '', html=_meeting_drawer(request, m['id']).content)


@require_POST
@feature('performance')
def meeting_item_delete(request, id):
    m = perf.delete_meeting_item(request.hajiri_user, str(id))
    return done(request, '', html=_meeting_drawer(request, m['id']).content)


# ================================================================ reviews

def _next_step(r: dict) -> tuple[str, str]:
    if r['released_at']:
        return ('Acknowledged', 'idle') if r['acknowledged_at'] else ('Results ready', 'ok')
    if not r['self_submitted_at'] and r['cycle_status'] in ('self_review', 'manager_review'):
        return ('Self review to do', 'info')
    return ('With your manager', 'idle')


@require_GET
@feature('performance')
def my_reviews(request):
    user = request.hajiri_user
    reviews = perf.list_reviews(user, 'mine')
    for r in reviews:
        r['step'], r['step_tone'] = _next_step(r)
    pending = [p for p in perf.peer_requests(user) if p['status'] == 'approved']
    return render(request, 'performance/my_reviews.html', {'reviews': reviews, 'pending': pending})


def _review_drawer(request, review_id, status=200):
    user = request.hajiri_user
    d = perf.review_detail(user, review_id)
    side = 'self' if d['role'] == 'self' else 'manager'
    editable = d['can']['editSelf'] if side == 'self' else d['can']['editManager']
    released = bool(d['review']['released_at'])
    labels = d['cycle']['rating_labels'] or DEFAULT_LABELS
    for i in d['items']:
        i['mine'] = i['self_rating'] if side == 'self' else i['manager_rating']
        i['mine_comment'] = (i['self_comment'] if side == 'self' else i['manager_comment']) or ''
    unrated = sum(1 for i in d['items'] if not i['mine'])
    rec = d['review'].get('recommendation') or {}
    rv = d['review']
    final = rv.get('final_rating') if rv.get('final_rating') is not None else rv.get('manager_score')
    from hajiri.learning import services as lrn
    courses = lrn.list_courses(user) if side == 'manager' else []
    colleagues = [c for c in perf.colleagues(user) if c['id'] != rv['employee_id']] if d['can']['nominatePeers'] else []
    return render(request, 'performance/partials/review_detail.html', {
        'd': d, 'rv': rv, 'cycle': d['cycle'], 'side': side, 'editable': editable, 'released': released, 'labels': labels,
        'levels': [(n, labels[n - 1] if n - 1 < len(labels) else '') for n in range(1, 6)],
        'goals': [i for i in d['items'] if i['item_type'] == 'goal'], 'competencies': [i for i in d['items'] if i['item_type'] == 'competency'],
        'unrated': unrated, 'rec': rec, 'rec_courses': set(rec.get('trainingCourseIds') or []), 'courses': courses,
        'colleagues': colleagues, 'can_print': d['role'] != 'self' or released,
        'deadline': d['cycle']['self_due'] if side == 'self' else d['cycle']['manager_due'],
        'final_value': f'{num(final):g}' if final is not None else '', 'summary': (rv.get('self_summary') if side == 'self' else rv.get('manager_summary')) or ''},
        status=status)


@require_GET
@feature('performance')
def review_detail(request, id):
    return _review_drawer(request, str(id))


@require_POST
@feature('performance')
def review_save(request, id):
    user = request.hajiri_user
    d = perf.review_detail(user, str(id))
    form = f.ReviewForm(request.POST, d['items'])
    submit = form.submitting()
    if d['role'] == 'self':
        perf.save_self(user, str(id), form.body(manager=False))
    else:
        perf.save_manager(user, str(id), form.body(manager=True))
    return done(request, 'Review submitted.' if submit else 'Draft saved.', html=_review_drawer(request, str(id)).content)


@require_POST
@feature('performance')
def review_calibrate(request, id):
    perf.calibrate(request.hajiri_user, str(id), f.CalibrateForm(request.POST).body())
    return done(request, 'Final rating set.', html=_review_drawer(request, str(id)).content)


@require_POST
@feature('performance')
def review_acknowledge(request, id):
    perf.acknowledge(request.hajiri_user, str(id), f.AcknowledgeForm(request.POST).body())
    return done(request, 'Thanks, acknowledged.', html=_review_drawer(request, str(id)).content)


@require_POST
@feature('performance')
def review_peers(request, id):
    user = request.hajiri_user
    rv_role = perf.review_detail(user, str(id))['role']
    perf.nominate_peers(user, str(id), f.PeersForm(request.POST).body())
    return done(request, 'Sent to your manager for approval.' if rv_role == 'self' else 'Peer reviewers added.',
                html=_review_drawer(request, str(id)).content)


@require_POST
@feature('performance')
def peer_decide(request, id):
    approve = f.DecideForm(request.POST).approved()
    rv = perf.decide_peer(request.hajiri_user, str(id), {'approve': approve})
    return done(request, 'Approved.' if approve else 'Declined.', html=_review_drawer(request, rv['id']).content)


@require_POST
@feature('performance')
def peer_remove(request, id):
    rv = perf.remove_peer(request.hajiri_user, str(id))
    return done(request, 'Removed.', html=_review_drawer(request, rv['id']).content)


@require_http_methods(['GET', 'POST'])
@feature('performance')
def peer_respond(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        req = next((r for r in perf.peer_requests(user) if r['id'] == str(id) and r['status'] == 'approved'), None)
        if not req:
            raise NotFoundError('peer review not found')
        labels = req['rating_labels'] or DEFAULT_LABELS
        return render(request, 'performance/partials/peer_form.html', {'req': req, 'levels': [(n, labels[n - 1]) for n in range(1, 6)]})
    perf.submit_peer(user, str(id), f.PeerResponseForm(request.POST).body())
    return done(request, 'Thanks, feedback sent.', close=True)


@require_GET
@feature('performance')
def review_print(request, id):
    resp = HttpResponse(perf.review_print_html(request.hajiri_user, str(id)), content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'private, no-store'
    return resp


# ================================================================ review cycles (HR runs them; managers review their team)

@require_GET
@staff('performance')
def cycles(request):
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    if request.GET.get('cycle'):
        return _cycle_page(request, request.GET['cycle'])
    view = 'competencies' if hr and request.GET.get('view') == 'competencies' else 'cycles'
    ctx = {'hr': hr, 'view': view, 'views': _pills(view, [('cycles', 'Review cycles'), ('competencies', 'Competencies')]) if hr else []}
    if view == 'cycles':
        ctx['cycles'] = perf.list_cycles(user)
    else:
        ctx['competencies'] = perf.list_competencies(user, all_=True)
    return render(request, 'performance/cycles.html', ctx)


def _cycle_page(request, cycle_id):
    user = request.hajiri_user
    hr = has_role(user, 'hr')
    d = perf.cycle_detail(user, cycle_id)
    c = d['cycle']
    flt = request.GET.get('filter') if request.GET.get('filter') in ('all', 'self', 'manager') else 'all'
    rows_ = d['reviews']
    shown = [r for r in rows_ if (not r['self_submitted_at'] if flt == 'self' else not r['manager_submitted_at'] if flt == 'manager' else True)]
    self_done = sum(1 for r in rows_ if r['self_submitted_at'])
    mgr_done = sum(1 for r in rows_ if r['manager_submitted_at'])
    stats = [{'label': 'People', 'value': len(rows_)}, {'label': 'Self reviews done', 'value': f'{self_done} / {len(rows_)}'},
             {'label': 'Manager reviews done', 'value': f'{mgr_done} / {len(rows_)}',
              **({'tone': 'warn'} if mgr_done < len(rows_) and c['status'] in ('calibration', 'released') else {})},
             {'label': 'Acknowledged', 'value': sum(1 for r in rows_ if r['acknowledged_at'])}]
    action = None
    if hr:
        if c['status'] == 'draft':
            action = {'url': reverse('performance:cycle_launch', args=[c['id']]), 'label': 'Launch cycle', 'title': 'Launch this cycle?',
                      'confirm': 'Launch', 'message': 'Everyone in scope is notified and the setup is locked.'}
        elif c['status'] in NEXT_LABEL:
            label = ('Start calibration' if not c['peer_enabled'] else 'Open peer review') if c['status'] == 'manager_review' else NEXT_LABEL[c['status']]
            action = {'url': reverse('performance:cycle_advance', args=[c['id']]), 'label': label, 'title': f'Move to {PHASE_LABEL[next_phase(c)]}?',
                      'confirm': 'Continue',
                      'message': ('Employees will see their final ratings and manager comments. Training needs become enrolments.'
                                  if c['status'] == 'calibration' else 'Everyone involved in the next phase is notified.')}
    dist = None
    if hr and c['status'] == 'calibration':
        cal = perf.calibration(user, c['id'])['distribution']
        total = sum(cal['overall']) or 1
        dist = {'overall': [{'n': n, 'level': i + 1, 'pct': n / total * 100, 'tone': ('bad', 'warn', 'idle', 'info', 'ok')[i]} for i, n in enumerate(cal['overall'])],
                'departments': [{'name': k, 'counts': v, 'avg': (sum(n * (i + 1) for i, n in enumerate(v)) / sum(v)) if sum(v) else 0}
                                for k, v in cal['byDepartment'].items()]}
    return render(request, 'performance/cycle_detail.html', {
        'c': c, 'hr': hr, 'rows': shown, 'all_rows': rows_, 'stats': stats, 'action': action, 'dist': dist, 'labels': c['rating_labels'] or DEFAULT_LABELS,
        'filters': _pills(flt, [('all', 'Everyone'), ('self', 'Self review pending'), ('manager', 'Manager review pending')], 'filter', f"&cycle={c['id']}")})


@require_http_methods(['GET', 'POST'])
@staff('performance')
def cycle_edit(request, id=None):
    user = request.hajiri_user
    existing = perf.load_cycle(user.company_id, str(id)) if id else None
    if existing and not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')
    draft = not existing or existing['status'] == 'draft'
    if request.method == 'GET':
        if not has_role(user, 'hr'):
            raise ForbiddenError('requires role hr')
        c = existing or {'name': '', 'goals_weight': 60, 'competency_weight': 40, 'peer_enabled': False, 'peer_min': 3, 'filter': {},
                         'rating_labels': DEFAULT_LABELS}
        return render(request, 'performance/partials/cycle_form.html', {
            'c': c, 'existing': existing, 'draft': draft, 'locked': not draft, 'departments': perf.departments(user), 'today': today_npt(),
            'org_units': set((c.get('filter') or {}).get('orgUnitIds') or []), 'labels': list(enumerate(c['rating_labels'] or DEFAULT_LABELS, 1))})
    body = f.CycleForm(request.POST).body(draft)
    if existing:
        perf.update_cycle(user, existing['id'], body)
    else:
        perf.create_cycle(user, body)
    return done(request, 'Cycle saved.', close=True)


@require_POST
@staff('performance')
def cycle_delete(request, id):
    perf.delete_cycle(request.hajiri_user, str(id))
    resp = done(request, 'Deleted.', close=True)
    return resp


@require_POST
@staff('performance')
def cycle_launch(request, id):
    n = perf.launch_cycle(request.hajiri_user, str(id))
    return done(request, f'Launched for {n} people.')


@require_POST
@staff('performance')
def cycle_advance(request, id):
    to = perf.advance_cycle(request.hajiri_user, str(id))
    return done(request, f'Moved to {PHASE_LABEL[to]}.')


@require_http_methods(['GET', 'POST'])
@staff('performance')
def competency_edit(request, id=None):
    user = request.hajiri_user
    if request.method == 'GET':
        if id:
            c = perf.get_competency(user, str(id))
        else:
            if not has_role(user, 'hr'):
                raise ForbiddenError('requires role hr')
            c = {'kind': 'skill', 'indicators': [], 'designations': [], 'grades': [], 'active': True}
        ind = {i['level']: i['text'] for i in c['indicators']}
        return render(request, 'performance/partials/competency_form.html', {'c': c, 'levels': [(n, ind.get(n, '')) for n in range(1, 6)]})
    body = f.CompetencyForm(request.POST).body()
    if id:
        perf.update_competency(user, str(id), body)
    else:
        perf.create_competency(user, body)
    return done(request, 'Competency saved.', close=True)

