"""
Help desk pages (HelpdeskPage.tsx for HR at /helpdesk, MyTicketsPage.tsx at /me/help) and their HTMX actions
(TicketThread.tsx). Rules live in services.py; views turn form posts into the TS request bodies.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import has_role
from hajiri.core.clock import now
from hajiri.core.scoping import is_uuid
from hajiri.files.store import stored_file_response
from hajiri.web.decorators import login_required, require_feature, require_role
from hajiri.web.htmx import close_modal, htmx_target, hx_redirect, is_htmx, refresh, toast
from hajiri.web.nav import url_of

from . import services
from .forms import CategoryForm, MessageForm, QueueFilterForm, RateForm, SettingsForm, TicketForm, TicketUpdateForm
from .services import ACTIVE, PRIORITY_LABEL, STATUS_LABEL, STATUSES

PRIORITY_TONE = {'low': 'idle', 'normal': 'info', 'high': 'warn', 'urgent': 'bad'}
STATUS_TONE = {'open': 'info', 'in_progress': 'accent', 'waiting': 'warn', 'resolved': 'ok', 'closed': 'idle'}
QUEUE_STATUSES = (('active', 'Open'), ('resolved', 'Resolved'), ('closed', 'Closed'))
# The page the lifecycle app may provide for the "Leaving the company" block on /me/help (MyTicketsPage Leaving).
LEAVING_URL_NAME = 'lifecycle:my_leaving'


def _open_ticket(request) -> str | None:
    """?ticket=<id> opens that ticket in the drawer; anything that is not an id is ignored."""
    v = (request.GET.get('ticket') or '').strip()
    return v if v and is_uuid(v) else None


def _decorate(t: dict) -> dict:
    """Labels and tones for TicketBadges, plus the overdue flag the queue highlights."""
    t['status_label'] = STATUS_LABEL[t['status']]
    t['status_tone'] = STATUS_TONE[t['status']]
    t['priority_label'] = PRIORITY_LABEL[t['priority']]
    t['priority_tone'] = PRIORITY_TONE[t['priority']]
    t['is_overdue'] = t['status'] in ACTIVE and t['sla_due_at'] < now()
    t['is_done'] = t['status'] in ('resolved', 'closed')
    return t


# ---------------------------------------------------------------- employee: /me/help

@require_GET
@login_required
@require_feature('helpdesk')
def mine(request):
    user = request.hajiri_user
    tickets = [_decorate(t) for t in services.my_tickets(user)]
    ctx = {'tickets': tickets, 'open_ticket': _open_ticket(request),
           'leaving_url': url_of(LEAVING_URL_NAME) if user.employee_id else None,
           'grievance_url': url_of('relations:my_grievances') or '/me/help/grievances'}
    if htmx_target(request) == 'hd-mine':
        return render(request, 'helpdesk/partials/my_list.html', ctx)
    return render(request, 'helpdesk/mine.html', ctx)


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('helpdesk')
def new_ticket(request):
    user = request.hajiri_user
    if request.method == 'POST':
        r = services.create_ticket(user, TicketForm(request.POST).ticket())
        resp = hx_redirect(request, f"{reverse('helpdesk:mine')}?ticket={r['id']}")
        return toast(request, resp, f"Ticket #{r['number']} raised. We will get back to you.")
    cats = [c for c in services.list_categories(user) if c['active']]
    cat_map = {c['id']: {'description': c['description'], 'confidential': c['confidential']} for c in cats}
    return render(request, 'helpdesk/partials/new_ticket.html',
                  {'categories': cats, 'cat_map': cat_map, 'priorities': PRIORITY_LABEL.items()})


# ---------------------------------------------------------------- HR: /helpdesk

@require_GET
@require_role('hr')
@require_feature('helpdesk')
def queue(request):
    user = request.hajiri_user
    tab = 'setup' if request.GET.get('tab') == 'setup' else 'queue'
    target = htmx_target(request)
    if tab == 'setup':
        ctx = {'tab': tab, 'categories': services.list_categories(user), 'policy': services.get_settings(user)['policy'],
               'priorities': PRIORITY_LABEL.items()}
        if target == 'hd-topics':
            return render(request, 'helpdesk/partials/topics.html', ctx)
        return render(request, 'helpdesk/queue.html', ctx)
    filters = QueueFilterForm(request.GET).filters()
    tickets = [_decorate(t) for t in services.queue(user, filters)]
    ctx = {'tab': tab, 'tickets': tickets, 'filters': filters, 'statuses': QUEUE_STATUSES,
           'empty_title': 'No open tickets' if filters['status'] == 'active' else 'Nothing here'}
    if target == 'hd-queue':
        return render(request, 'helpdesk/partials/queue_list.html', ctx)
    s = services.stats(user)['stats']
    ctx.update({
        'stats': [
            {'label': 'Open', 'value': s['open'], 'hint': f"{s['unassigned']} unassigned"},
            {'label': 'Past due time', 'value': s['overdue'], 'tone': 'bad' if s['overdue'] else 'ok'},
            {'label': 'Average time to resolve', 'value': f"{s['avg_resolution_hours']} h" if s['avg_resolution_hours'] else '—',
             'hint': 'Last 90 days'},
            {'label': 'Satisfaction', 'value': f"{s['avg_rating']} / 5" if s['avg_rating'] else '—', 'hint': f"{s['ratings']} ratings"},
        ],
        'categories': services.list_categories(user), 'agents': services.list_agents(user),
        'open_ticket': _open_ticket(request),
    })
    if target == 'hd-stats':
        return render(request, 'helpdesk/partials/stats.html', ctx)
    return render(request, 'helpdesk/queue.html', ctx)


@require_POST
@require_role('hr')
@require_feature('helpdesk')
def save_settings(request):
    services.put_settings(request.hajiri_user, SettingsForm(request.POST).policy())
    return toast(request, HttpResponse(status=204), 'Settings saved.')


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('helpdesk')
def category_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        services.create_category(user, CategoryForm(request.POST).category())
        return _topic_saved(request)
    return render(request, 'helpdesk/partials/category_form.html',
                  {'category': {'name': '', 'active': True, 'confidential': False, 'sort': 0}, 'agents': services.list_agents(user)})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
@require_feature('helpdesk')
def category_edit(request, category_id):
    user = request.hajiri_user
    if request.method == 'POST':
        services.update_category(user, category_id, CategoryForm(request.POST).category())
        return _topic_saved(request)
    return render(request, 'helpdesk/partials/category_form.html',
                  {'category': services.get_category(user, category_id), 'agents': services.list_agents(user)})


def _topic_saved(request):
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, 'hd-topics')
    return toast(request, resp, 'Topic saved.')


# ---------------------------------------------------------------- one ticket (the drawer)

def _thread(request, ticket_id, status: int = 200):
    user = request.hajiri_user
    d = services.ticket_detail(user, ticket_id)
    t = _decorate(d['ticket'])
    names = {f['id']: f['filename'] for f in d['files']}
    for m in d['messages']:
        m['files'] = [{'id': f, 'filename': names.get(f, 'Attachment')} for f in m['file_ids']]
    agents = None
    if d['role'] == 'agent' and has_role(user, 'hr'):
        agents = [a for a in services.list_agents(user) if (not t['confidential'] or a['is_hr']) and a['id'] != t['requester_user_id']]
    ctx = {'t': t, 'role': d['role'], 'agent': d['role'] == 'agent', 'messages': d['messages'], 'can_reopen': d['canReopen'],
           'agents': agents, 'closed': t['status'] == 'closed',
           'status_options': [(k, 'Waiting on requester' if k == 'waiting' else STATUS_LABEL[k]) for k in STATUSES],
           'priority_options': PRIORITY_LABEL.items(), 'stars': range(1, 6)}
    return render(request, 'helpdesk/partials/thread.html', ctx, status=status)


def _changed(request, ticket_id, message: str):
    resp = _thread(request, ticket_id)
    for target in ('hd-queue', 'hd-stats', 'hd-mine'):
        refresh(resp, target)
    return toast(request, resp, message)


@require_GET
@login_required
@require_feature('helpdesk')
def ticket(request, ticket_id):
    if not is_htmx(request):
        # A link to a ticket (from a notification) opens its page with the drawer showing it.
        services.ticket_detail(request.hajiri_user, ticket_id)
        page = 'helpdesk:queue' if has_role(request.hajiri_user, 'hr') else 'helpdesk:mine'
        return redirect(f'{reverse(page)}?ticket={ticket_id}')
    return _thread(request, ticket_id)


@require_POST
@login_required
@require_feature('helpdesk')
def reply(request, ticket_id):
    message = MessageForm(request.POST).message()
    services.add_message(request.hajiri_user, ticket_id, message)
    return _changed(request, ticket_id, 'Note added.' if message['internal'] else 'Message sent.')


@require_POST
@login_required
@require_feature('helpdesk')
def update(request, ticket_id):
    body = TicketUpdateForm(request.POST).patch()
    services.update_ticket(request.hajiri_user, ticket_id, body)
    if 'assigneeUserId' in body:
        msg = 'Assigned.' if body['assigneeUserId'] else 'Unassigned.'
    elif 'priority' in body and 'status' not in body:
        msg = 'Priority changed.'
    else:
        msg = 'Status changed.'
    return _changed(request, ticket_id, msg)


@require_POST
@login_required
@require_feature('helpdesk')
def reopen(request, ticket_id):
    services.reopen_ticket(request.hajiri_user, ticket_id)
    return _changed(request, ticket_id, 'Ticket reopened.')


@require_POST
@login_required
@require_feature('helpdesk')
def close(request, ticket_id):
    services.close_ticket(request.hajiri_user, ticket_id)
    return _changed(request, ticket_id, 'Ticket closed.')


@require_POST
@login_required
@require_feature('helpdesk')
def rate(request, ticket_id):
    services.rate_ticket(request.hajiri_user, ticket_id, RateForm(request.POST).rating_body())
    return _changed(request, ticket_id, 'Thanks for the feedback.')


@require_GET
@login_required
@require_feature('helpdesk')
def ticket_file(request, ticket_id, file_id):
    f = services.ticket_file(request.hajiri_user, ticket_id, file_id)
    return stored_file_response(f, download=request.GET.get('download') == '1')
