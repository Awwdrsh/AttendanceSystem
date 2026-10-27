"""
Tasks pages (TasksPage.tsx at /tasks and /tasks/<board>, TaskDrawer.tsx, MyTodoCard.tsx on Home) and their HTMX
actions. Rules live in services.py; views turn form posts into the TS request bodies.
"""
from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.errors import NotFoundError
from hajiri.core.schema import is_uuid
from hajiri.files.store import stored_file_response
from hajiri.web.decorators import login_required, require_feature
from hajiri.web.htmx import close_modal, hx_redirect, is_htmx, refresh, toast

from . import services
from .services import PRIORITY_LABEL, PRIORITY_TONE

BOARD_TARGET = 'tk-board'
REPORT_TARGET = 'tk-report'
TODO_TARGET = 'tk-todo'


def _checked(request, name: str) -> bool:
    return request.POST.get(name) in ('on', '1', 'true')


def _initials(name: str) -> str:
    return ''.join(w[0] for w in (name or '').split() if w)[:2].upper()


def _decorate(t: dict, today: str) -> dict:
    """TaskCard: overdue flag, priority badge, assignee initials."""
    t['overdue'] = not t.get('completed_at') and bool(t.get('due_date')) and t['due_date'] < today
    t['priority_label'] = PRIORITY_LABEL.get(t['priority'], t['priority'])
    t['priority_tone'] = PRIORITY_TONE.get(t['priority'], 'idle')
    for a in t.get('assignees') or []:
        a['initials'] = _initials(a['name'])
    return t


def _picker(user, current_id: str | None) -> dict:
    data = services.list_boards(user)
    boards = data['boards']
    return {'boards': boards, 'can_create': data['canCreateTeamBoard'],
            'current_id': current_id or next((b['id'] for b in boards if b['kind'] == 'personal'), None)
            or (boards[0]['id'] if boards else None)}


def _board_ctx(user, board_id: str) -> dict:
    d = services.get_board(user, board_id)
    today = services.today_iso()
    by_col: dict[str, list] = {c['id']: [] for c in d['board']['columns']}
    for t in d['tasks']:
        if t['column_id'] in by_col:
            by_col[t['column_id']].append(_decorate(t, today))
    n = len(d['members'])
    if d['board']['kind'] == 'personal':
        who = 'Only you can see this board.'
    else:
        who = f"Owned by {d['ownerName'] or 'someone'}; {n} other {'person' if n == 1 else 'people'} on it."
    return {**d, 'columns': [{**c, 'tasks': by_col[c['id']]} for c in d['board']['columns']], 'who': who,
            'is_owner': d['access'] == 'owner'}


def _board_fragment(request, board_id: str, status: int = 200):
    user = request.hajiri_user
    ctx = {**_board_ctx(user, board_id), 'picker': _picker(user, str(board_id)), 'oob': True}
    return render(request, 'tasks/partials/board.html', ctx, status=status)


# ---------------------------------------------------------------- the page

@require_GET
@login_required
@require_feature('tasks')
def page(request, board_id=None):
    user = request.hajiri_user
    picker = _picker(user, str(board_id) if board_id else None)
    view = 'report' if request.GET.get('view') == 'report' else 'board'
    ctx = {'picker': picker, 'view': view, 'b': None}
    if picker['current_id']:
        ctx.update(_board_ctx(user, picker['current_id']))
        ctx['b'] = True
        if view == 'report':
            ctx['report'] = _report_ctx(user, picker['current_id'])
    task = (request.GET.get('task') or '').strip()
    ctx['open_task'] = task if task and is_uuid(task) else None
    return render(request, 'tasks/page.html', ctx)


@require_GET
@login_required
@require_feature('tasks')
def board_columns(request, board_id):
    return _board_fragment(request, board_id)


def _report_ctx(user, board_id) -> dict:
    r = services.board_report(user, board_id)
    today = services.today_iso()
    tot = r['totals']
    r['stats'] = [
        {'label': 'Open', 'value': tot['open']},
        {'label': 'Overdue', 'value': tot['overdue'], **({'tone': 'bad'} if tot['overdue'] else {})},
        {'label': 'Done this week', 'value': tot['done_this_week']},
        {'label': 'Unassigned', 'value': tot['unassigned']},
    ]
    r['overdueTasks'] = [_decorate(t, today) for t in r['overdueTasks']]
    for t in r['overdueTasks']:
        t['who'] = ', '.join(a['name'] for a in t['assignees']) or '—'
    return r


@require_GET
@login_required
@require_feature('tasks')
def board_report(request, board_id):
    return render(request, 'tasks/partials/report.html', {'report': _report_ctx(request.hajiri_user, board_id)})


# ---------------------------------------------------------------- boards

@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('tasks')
def board_new(request):
    user = request.hajiri_user
    if request.method == 'POST':
        board = services.create_board(user, {'name': (request.POST.get('name') or '').strip(),
                                             'addMyReports': _checked(request, 'add_my_reports')})
        return hx_redirect(request, reverse('tasks:board_detail', args=[board['id']]))
    return render(request, 'tasks/partials/board_new.html', {})


def _columns_from_post(request) -> list[dict]:
    ids, names = request.POST.getlist('col_id'), request.POST.getlist('col_name')
    done = set(request.POST.getlist('col_done'))
    return [{'id': i, 'name': n, 'done': i in done} for i, n in zip(ids, names)]


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('tasks')
def board_settings(request, board_id):
    user = request.hajiri_user
    d = services.get_board(user, board_id)
    services.assert_owner(d['access'])
    board = d['board']
    if request.method == 'POST':
        body: dict = {'name': (request.POST.get('name') or '').strip(), 'columns': _columns_from_post(request)}
        move_to = request.POST.get('move_to') or ''
        if move_to:
            body['moveTo'] = move_to
        services.update_board(user, board['id'], body)
        if board['kind'] == 'team':
            ids = [x for x in request.POST.getlist('member_ids') if x.strip()]
            services.set_members(user, board['id'], {'userIds': ids})
        resp = hx_redirect(request, reverse('tasks:board_detail', args=[board['id']]))
        return toast(request, resp, 'Board saved.')
    options = []
    if board['kind'] == 'team':
        people = services.people(user)['people']
        options = [{'user_id': p['user_id'], 'name': p['name']} for p in people]
        known = {p['user_id'] for p in people}
        options += [{'user_id': m['user_id'], 'name': m['name']} for m in d['members'] if m['user_id'] not in known]
    return render(request, 'tasks/partials/board_settings.html',
                  {'board': board, 'options': options, 'member_ids': {m['user_id'] for m in d['members']}})


@require_POST
@login_required
@require_feature('tasks')
def board_archive(request, board_id):
    services.update_board(request.hajiri_user, board_id, {'archived': True})
    return toast(request, hx_redirect(request, reverse('tasks:board')), 'Board archived.')


# ---------------------------------------------------------------- tasks on the board

@require_POST
@login_required
@require_feature('tasks')
def task_new(request, board_id):
    title = (request.POST.get('title') or '').strip()
    if not title:
        return _board_fragment(request, board_id)
    column = request.POST.get('column_id') or ''
    services.create_task(request.hajiri_user, board_id, {'title': title, **({'columnId': column} if column else {})})
    return _board_fragment(request, board_id)


@require_POST
@login_required
@require_feature('tasks')
def task_move(request, board_id):
    """Drag and drop: move a card to another column."""
    user = request.hajiri_user
    task_id, column_id = request.POST.get('task_id') or '', request.POST.get('column_id') or ''
    task, board, _ = services.task_for(user, task_id)
    if board['id'] != str(board_id):
        raise NotFoundError('task not found')
    if task['column_id'] != column_id:
        services.update_task(user, task['id'], {'columnId': column_id})
    return _board_fragment(request, board_id)


# ---------------------------------------------------------------- my to-do (Home)

def _todo_ctx(user) -> dict:
    d = services.my_todo(user)
    tasks = [_decorate(t, d['today']) for t in d['tasks']]
    return {'tasks': tasks[:8], 'more': max(0, len(tasks) - 8), 'today': d['today']}


@require_GET
@login_required
def todo(request):
    from hajiri.web.nav import feature_on
    if not feature_on(request, 'tasks'):
        return HttpResponse('')
    ctx = _todo_ctx(request.hajiri_user)
    if not ctx['tasks']:
        return HttpResponse('')
    return render(request, 'tasks/partials/todo.html', ctx)


@require_POST
@login_required
@require_feature('tasks')
def todo_done(request, task_id):
    user = request.hajiri_user
    t = services.get_task(user, task_id)
    done_col = next((c for c in t['board']['columns'] if c['done']), None)
    if done_col:
        services.update_task(user, task_id, {'columnId': done_col['id']})
    ctx = _todo_ctx(user)
    resp = render(request, 'tasks/partials/todo.html', ctx) if ctx['tasks'] else HttpResponse('')
    return toast(request, resp, 'Done.')


# ---------------------------------------------------------------- one task (the drawer)

def _drawer(request, task_id, status: int = 200):
    user = request.hajiri_user
    d = services.get_task(user, task_id)
    t = _decorate(d['task'], services.today_iso())
    options = [{'user_id': m['user_id'], 'name': m['name']} for m in d['members']]
    if d['board']['kind'] == 'team' and not any(o['user_id'] == str(user.id) for o in options):
        options.insert(0, {'user_id': str(user.id), 'name': user.name})
    for a in t['assignees']:
        if not any(o['user_id'] == a['userId'] for o in options):
            options.append({'user_id': a['userId'], 'name': a['name']})
    assigned = {a['userId'] for a in t['assignees']}
    done_n = sum(1 for c in d['checklist'] if c['done'])
    ctx = {**d, 't': t, 'options': options, 'assigned': assigned, 'priorities': PRIORITY_LABEL.items(),
           'checklist_label': f"Checklist ({done_n}/{len(d['checklist'])})" if d['checklist'] else 'Checklist',
           'can_remove': services.can_remove_task(user, t, d['access'])}
    return render(request, 'tasks/partials/drawer.html', ctx, status=status)


def _changed(request, task_id, message: str | None = None):
    resp = _drawer(request, task_id)
    for target in (BOARD_TARGET, REPORT_TARGET):
        refresh(resp, target)
    return toast(request, resp, message) if message else resp


@require_GET
@login_required
@require_feature('tasks')
def task(request, task_id):
    if not is_htmx(request):
        # A link to a task opens its board with the drawer showing it.
        _, board, _ = services.task_for(request.hajiri_user, task_id)
        return redirect(f"{reverse('tasks:board_detail', args=[board['id']])}?task={task_id}")
    return _drawer(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def task_update(request, task_id):
    """One field at a time, as the drawer saves: title, description, column, priority, due date, labels, assignees."""
    p = request.POST
    user = request.hajiri_user
    body: dict = {}
    if 'title' in p:
        title = p['title'].strip()
        if not title:
            return _drawer(request, task_id)   # the drawer only saves a non-empty title
        body['title'] = title
    if 'description' in p:
        body['description'] = p['description'] or None
    if 'column_id' in p:
        body['columnId'] = p['column_id']
    if 'priority' in p:
        body['priority'] = p['priority']
    if 'due_date' in p:
        body['dueDate'] = p['due_date'] or None
    if 'labels_sent' in p:
        labels = [x for x in p.getlist('labels') if x.strip()]
        new = (p.get('label_new') or '').strip()
        if new and new not in labels and len(labels) < 8:
            labels.append(new[:24])
        drop = p.get('remove_label')
        if drop is not None:
            labels = [x for x in labels if x != drop]
        body['labels'] = [x[:24] for x in labels]
    if 'assignees_sent' in p:
        body['assigneeIds'] = [x for x in p.getlist('assignee_ids') if x.strip()]
    if body:
        services.update_task(user, task_id, body)
    return _changed(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def task_delete(request, task_id):
    services.delete_task(request.hajiri_user, task_id)
    resp = HttpResponse(status=204)
    close_modal(resp)
    for target in (BOARD_TARGET, REPORT_TARGET):
        refresh(resp, target)
    return toast(request, resp, 'Task removed.')


@require_POST
@login_required
@require_feature('tasks')
def checklist_add(request, task_id):
    text = (request.POST.get('text') or '').strip()
    if text:
        services.add_checklist(request.hajiri_user, task_id, {'text': text})
    return _changed(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def checklist_update(request, item_id):
    user = request.hajiri_user
    task_id = services.checklist_task_id(item_id)
    services.update_checklist(user, item_id, {'done': _checked(request, 'done')})
    return _changed(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def checklist_delete(request, item_id):
    user = request.hajiri_user
    task_id = services.checklist_task_id(item_id)
    services.delete_checklist(user, item_id)
    return _changed(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def comment_add(request, task_id):
    body = (request.POST.get('body') or '').strip()
    if body:
        services.add_comment(request.hajiri_user, task_id, {'body': body})
    return _changed(request, task_id)


@require_POST
@login_required
@require_feature('tasks')
def attachment_add(request, task_id):
    file_id = (request.POST.get('file_id') or '').strip()
    if file_id:
        services.add_attachment(request.hajiri_user, task_id, {'fileId': file_id})
    return _changed(request, task_id)


@require_GET
@login_required
@require_feature('tasks')
def attachment_file(request, att_id):
    f = services.attachment_file(request.hajiri_user, att_id)
    return stored_file_response(f, download=request.GET.get('download') == '1')


@require_POST
@login_required
@require_feature('tasks')
def attachment_delete(request, att_id):
    task_id = services.attachment_task_id(att_id)
    services.delete_attachment(request.hajiri_user, att_id)
    return _changed(request, task_id)
