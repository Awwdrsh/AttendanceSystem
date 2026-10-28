"""Task boards in a group of companies: everything is scoped to the active company, ids of another company are not
found, and only people who can act in the company are put on its boards."""
import pytest

from hajiri.core.errors import HttpError
from hajiri.db.models import NotificationOutbox, Task, TaskBoard, TaskChecklistItem
from hajiri.tasks import services as tk
from tests.multicompany.conftest import world  # noqa: F401  (the group-of-companies fixture)
from tests.web.factories import bearer, htmx, make_employee, make_user

from .world import PDF, today


def _status(fn, *a, **kw) -> int:
    try:
        fn(*a, **kw)
    except HttpError as e:
        return e.status
    return 200


@pytest.fixture
def beta(world):
    """A Beta team board owned by Beta's admin, with a task, a checklist item and an attachment."""
    from hajiri.files import service as files
    _, admin_b = world.session(world.admin_b)
    board = tk.create_board(admin_b, {'name': 'Beta launch'})
    task = tk.create_task(admin_b, board['id'], {'title': 'Print menus', 'assigneeIds': [admin_b.id], 'dueDate': today()})
    item = tk.add_checklist(admin_b, task['id'], {'text': 'Proof'})
    file_id = files.upload(admin_b, {'purpose': 'task_attachment', 'filename': 'menu.pdf', 'dataUrl': PDF})['id']
    att = tk.add_attachment(admin_b, task['id'], {'fileId': file_id})
    return {'board': board['id'], 'task': task['id'], 'item': item['id'], 'att': att['id'], 'user': admin_b}


# (name, method, url, htmx?, data). Ids: {board}, {task}, {item}, {att}.
CASES = [
    ('board page', 'get', '/tasks/{board}', False, None),
    ('board columns', 'get', '/_/tasks/boards/{board}/columns', True, None),
    ('board report', 'get', '/_/tasks/boards/{board}/report', True, None),
    ('board settings', 'get', '/_/tasks/boards/{board}/settings', True, None),
    ('board archive', 'post', '/_/tasks/boards/{board}/archive', True, {}),
    ('quick add', 'post', '/_/tasks/boards/{board}/tasks', True, {'title': 'Sneaky'}),
    ('move', 'post', '/_/tasks/boards/{board}/move', True, {'task_id': '{task}', 'column_id': 'done'}),
    ('todo done', 'post', '/_/tasks/todo/{task}/done', True, {}),
    ('drawer', 'get', '/_/tasks/{task}', True, None),
    ('task link', 'get', '/_/tasks/{task}', False, None),
    ('task update', 'post', '/_/tasks/{task}/update', True, {'priority': 'high'}),
    ('task delete', 'post', '/_/tasks/{task}/delete', True, {}),
    ('checklist add', 'post', '/_/tasks/{task}/checklist', True, {'text': 'Sneaky'}),
    ('checklist tick', 'post', '/_/tasks/checklist/{item}', True, {'done': 'on'}),
    ('checklist delete', 'post', '/_/tasks/checklist/{item}/delete', True, {}),
    ('comment', 'post', '/_/tasks/{task}/comments', True, {'body': 'Sneaky'}),
    ('attach', 'post', '/_/tasks/{task}/attachments', True, {}),
    ('attachment file', 'get', '/_/tasks/attachments/{att}/file', False, None),
    ('attachment delete', 'post', '/_/tasks/attachments/{att}/delete', True, {}),
    ('api board', 'get', '/api/v1/tasks/boards/{board}', False, None),
    ('api board patch', 'patch', '/api/v1/tasks/boards/{board}', False, {'name': 'Renamed'}),
    ('api members', 'put', '/api/v1/tasks/boards/{board}/members', False, {'userIds': []}),
    ('api new task', 'post_json', '/api/v1/tasks/boards/{board}/tasks', False, {'title': 'Sneaky'}),
    ('api report', 'get', '/api/v1/tasks/reports/boards/{board}', False, None),
    ('api task', 'get', '/api/v1/tasks/{task}', False, None),
    ('api task patch', 'patch', '/api/v1/tasks/{task}', False, {'priority': 'high'}),
    ('api task delete', 'delete', '/api/v1/tasks/{task}', False, None),
    ('api checklist add', 'post_json', '/api/v1/tasks/{task}/checklist', False, {'text': 'Sneaky'}),
    ('api checklist patch', 'patch', '/api/v1/tasks/checklist/{item}', False, {'done': True}),
    ('api checklist delete', 'delete', '/api/v1/tasks/checklist/{item}', False, None),
    ('api comment', 'post_json', '/api/v1/tasks/{task}/comments', False, {'body': 'Sneaky'}),
    ('api attachment file', 'get', '/api/v1/tasks/attachments/{att}/file', False, None),
    ('api attachment delete', 'delete', '/api/v1/tasks/attachments/{att}', False, None),
]


def _fill(value, ids):
    if isinstance(value, str):
        return value.format(**ids)
    if isinstance(value, dict):
        return {k: _fill(v, ids) for k, v in value.items()}
    return value


def _call(client, method, url, hx, data):
    headers = htmx() if hx else {}
    if method == 'post':
        return client.post(url, data or {}, **headers)
    if method in ('post_json', 'patch', 'put'):
        verb = 'post' if method == 'post_json' else method
        return getattr(client, verb)(url, data or {}, content_type='application/json', **headers)
    return getattr(client, method)(url, **headers)


@pytest.mark.parametrize('case', CASES, ids=[c[0] for c in CASES])
def test_ids_of_another_company_are_not_found(world, beta, case):
    name, method, url, hx, data = case
    target, body = _fill(url, beta), _fill(data, beta)
    for who in (world.admin_c, world.admin_a):     # outside the group; group admin working in Alpha
        r = _call(world.signed_in(who), method, target, hx, body)
        assert r.status_code == 404, f'{name}: {who.email} got {r.status_code}'
    r = _call(world.signed_in(world.admin_b), method, target, hx, body)
    assert r.status_code != 404 and r.status_code < 500, f'{name}: Beta admin got {r.status_code} (control)'


def test_nothing_changed_by_the_refused_requests(world, beta):
    for name, method, url, hx, data in CASES:
        for who in (world.admin_c, world.admin_a):
            _call(world.signed_in(who), method, _fill(url, beta), hx, _fill(data, beta))
    t = Task.objects.get(pk=beta['task'])
    assert (t.priority, t.column_id, t.archived_at) == ('normal', 'todo', None)
    assert TaskBoard.objects.get(pk=beta['board']).name == 'Beta launch'
    assert list(TaskChecklistItem.objects.filter(task_id=beta['task']).values_list('text', 'done')) == [('Proof', False)]
    assert not Task.objects.filter(title='Sneaky').exists()


def test_after_switching_the_board_opens(world, beta):
    c = world.signed_in(world.admin_a)
    assert c.get(f"/tasks/{beta['board']}").status_code == 404
    c.post('/_/web/company/switch', {'company_id': str(world.beta.id)}, **htmx())
    page = c.get(f"/tasks/{beta['board']}")
    assert page.status_code == 200 and b'Print menus' in page.content
    # Admin there through the group role: HR access, not the owner's.
    assert b'Board settings' not in page.content


def test_board_lists_show_only_the_active_company(world, beta):
    _, admin_a = world.session(world.admin_a)
    alpha_board = tk.create_board(admin_a, {'name': 'Alpha stocktake'})['id']
    _, in_beta = world.session(world.admin_a, world.beta)
    ids = [b['id'] for b in tk.list_boards(in_beta, all_=True)['boards']]
    assert beta['board'] in ids and alpha_board not in ids
    ids = [b['id'] for b in tk.list_boards(admin_a, all_=True)['boards']]
    assert alpha_board in ids and beta['board'] not in ids
    token, _ = world.session(world.admin_a)
    from django.test import Client
    r = Client().get('/api/v1/tasks/boards?all=1', **bearer(token))
    assert r.status_code == 200 and beta['board'] not in [b['id'] for b in r.json()['boards']]


def test_board_members_are_people_of_the_active_company(world, beta):
    _, admin_b = world.session(world.admin_b)
    # Sita works only in Alpha: she cannot be put on a Beta board.
    with pytest.raises(HttpError) as e:
        tk.set_members(admin_b, beta['board'], {'userIds': [world.solo.id]})
    assert e.value.status == 400 and e.value.message == 'a chosen person does not exist or cannot sign in'
    # Dipak (a member of Beta) and Gopal (group HR) can act in Beta.
    out = tk.set_members(admin_b, beta['board'], {'userIds': [world.dual.id, world.ghr.id]})
    by_id = {m['user_id']: m for m in out['members']}
    assert set(by_id) == {world.dual.id, world.ghr.id}
    # A member's employee record on the board is the one they have in Beta, not their home one.
    assert by_id[world.dual.id]['employee_id'] == str(world.dual_beta_employee.id)
    people = [p['user_id'] for p in tk.people(admin_b)['people']]
    assert world.dual.id in people and world.solo.id not in people


def test_a_manager_by_membership_manages_the_reports_they_have_there(world):
    # Dipak is HR in Alpha and a manager in Beta; Bina reports to his Beta employee record.
    bina_e = make_employee(world.beta, name='Bina Beta', manager=world.dual_beta_employee)
    bina = make_user(world.beta, roles=['employee'], employee=bina_e, email='bina@beta.test')
    _, dual_beta = world.session(world.dual, world.beta)
    assert dual_beta.employee_id == str(world.dual_beta_employee.id)
    assert [p['user_id'] for p in tk.people(dual_beta)['people']] == [bina.id]
    board = tk.create_board(dual_beta, {'name': 'Kitchen', 'addMyReports': True})
    assert [m['user_id'] for m in tk.get_board(dual_beta, board['id'])['members']] == [bina.id]
    assert TaskBoard.objects.get(pk=board['id']).company_id == world.beta.id
    # Admin of Beta is not his report there.
    with pytest.raises(HttpError) as e:
        tk.set_members(dual_beta, board['id'], {'userIds': [bina.id, world.admin_b.id]})
    assert e.value.status == 403


def test_each_company_has_its_own_personal_board(world):
    _, dual_alpha = world.session(world.dual)
    mine = tk.personal_board(dual_alpha)
    task = tk.create_task(dual_alpha, mine['id'], {'title': 'Renew licence', 'dueDate': today()})
    _, dual_beta = world.session(world.dual, world.beta)
    theirs = tk.personal_board(dual_beta)
    assert theirs['id'] != mine['id'] and theirs['company_id'] == str(world.beta.id)
    assert [b['id'] for b in tk.list_boards(dual_beta)['boards'] if b['kind'] == 'personal'] == [theirs['id']]
    assert tk.my_todo(dual_beta)['tasks'] == []
    assert _status(tk.get_task, dual_beta, task['id']) == 404
    assert _status(tk.get_board, dual_beta, mine['id']) == 404
    page = world.signed_in(world.dual, world.beta).get('/tasks')
    assert page.status_code == 200 and b'Renew licence' not in page.content
    # Back in Alpha it is there as before, and still one board there.
    assert tk.personal_board(dual_alpha)['id'] == mine['id']
    assert task['id'] in [t['id'] for t in tk.my_todo(dual_alpha)['tasks']]


def test_reminders_are_filed_under_each_tasks_company(world, beta):
    _, admin_a = world.session(world.admin_a)
    tk.create_task(admin_a, tk.personal_board(admin_a)['id'], {'title': 'Alpha rent', 'dueDate': today()})
    assert tk.task_reminders() == {'dueToday': 2, 'overdue': 0}
    sent = {str(u): c for u, c in NotificationOutbox.objects.filter(template='tasks.due_today')
            .values_list('user_id', 'company_id')}
    assert sent == {world.admin_a.id: world.alpha.id, world.admin_b.id: world.beta.id}


def test_a_file_uploaded_in_another_company_cannot_be_attached(world, beta):
    from hajiri.files import service as files
    from hajiri.db.models import TaskAttachment
    _, admin_a = world.session(world.admin_a)
    alpha_file = files.upload(admin_a, {'purpose': 'task_attachment', 'filename': 'a.pdf', 'dataUrl': PDF})['id']
    _, a_in_beta = world.session(world.admin_a, world.beta)
    with pytest.raises(HttpError) as e:
        tk.add_attachment(a_in_beta, beta['task'], {'fileId': alpha_file})
    assert e.value.status == 400 and e.value.message == 'The attached file was not found. Upload it again.'
    assert TaskAttachment.objects.filter(task_id=beta['task']).count() == 1   # Beta's own, nothing added
