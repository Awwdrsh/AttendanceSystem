"""The tasks pages (TasksPage, TaskDrawer, MyTodoCard) through django.test.Client, replaying tasks.test.ts."""
import pytest
from django.test import Client

from hajiri.db.models import NotificationOutbox, Task, TaskAssignee, TaskAttachment, TaskBoard, TaskChecklistItem
from hajiri.tasks import services as tk
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts

from .world import build, plus, team_board, today, upload


@pytest.fixture
def w(db):
    return build()


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def test_tasks_page_shows_the_personal_board(w):
    c = client_for(w.sita)
    r = c.get('/tasks')
    assert r.status_code == 200
    html = r.content.decode()
    for text in ('Your own to-dos, and the boards you share with your team.', 'Only you can see this board.',
                 'To do', 'In progress', 'Done', 'My tasks (just you)', 'Drop tasks here'):
        assert text in html
    assert 'Team board' not in html            # employees cannot set up team boards
    assert 'Board settings' in html            # the owner of the personal board may rename it
    assert TaskBoard.objects.filter(owner_user_id=w.sita.id).count() == 1


def test_feature_off_sends_the_page_home_and_fragments_404(w):
    set_features_off(w.co, 'tasks')
    c = client_for(w.sita)
    assert c.get('/tasks')['Location'] == '/'
    board_id = tk.personal_board(w.sita)['id']
    assert c.post(f'/_/tasks/boards/{board_id}/tasks', {'title': 'x'}, **htmx()).status_code == 404
    assert c.get('/_/tasks/todo').content == b''


def test_someone_elses_personal_board_is_not_found(w):
    board_id = tk.personal_board(w.sita)['id']
    assert client_for(w.hr).get(f'/tasks/{board_id}').status_code == 404
    assert client_for(w.ram).get(f'/_/tasks/boards/{board_id}/columns', **htmx()).status_code == 404


def test_quick_add_move_by_drag_and_my_todo(w):
    c = client_for(w.sita)
    board_id = tk.personal_board(w.sita)['id']
    r = c.post(f'/_/tasks/boards/{board_id}/tasks', {'title': 'Renew passport', 'column_id': 'todo'}, **htmx('tk-board'))
    assert r.status_code == 200 and 'Renew passport' in r.content.decode()
    assert 'id="tk-picker"' in r.content.decode() and '1 open' in r.content.decode()
    task_id = str(Task.objects.get(title='Renew passport').id)
    bad = c.post(f'/_/tasks/boards/{board_id}/tasks', {'title': 'x', 'column_id': 'nope'}, **htmx())
    assert bad.status_code == 400 and toasts(bad)[0]['message'] == 'that column does not exist on this board'
    todo = c.get('/_/tasks/todo', **htmx())
    assert 'My to-do' in todo.content.decode() and 'Renew passport' in todo.content.decode()
    r = c.post(f'/_/tasks/boards/{board_id}/move', {'task_id': task_id, 'column_id': 'done'}, **htmx('tk-board'))
    assert r.status_code == 200
    assert Task.objects.get(pk=task_id).completed_at is not None
    assert c.get('/_/tasks/todo', **htmx()).content == b''
    c.post(f'/_/tasks/boards/{board_id}/move', {'task_id': task_id, 'column_id': 'todo'}, **htmx('tk-board'))
    assert Task.objects.get(pk=task_id).completed_at is None


def test_todo_card_ticks_off_a_task(w):
    board_id = tk.personal_board(w.sita)['id']
    task = tk.create_task(w.sita, board_id, {'title': 'Pay rent', 'dueDate': plus(-2), 'priority': 'urgent'})
    c = client_for(w.sita)
    html = c.get('/_/tasks/todo', **htmx()).content.decode()
    assert 'Overdue, ' in html and 'Urgent' in html and 'Due this week, and anything overdue' in html
    r = c.post(f"/_/tasks/todo/{task['id']}/done", **htmx('tk-todo'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Done.'
    assert Task.objects.get(pk=task['id']).column_id == 'done'


def test_employee_cannot_set_up_a_team_board_manager_can(w):
    r = client_for(w.sita).post('/_/tasks/boards/new', {'name': 'Mine'}, **htmx())
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only managers and HR can set up team boards'
    c = client_for(w.ram)
    assert 'Add the people who report to me' in c.get('/_/tasks/boards/new', **htmx()).content.decode()
    r = c.post('/_/tasks/boards/new', {'name': 'Ops', 'add_my_reports': 'on'}, **htmx())
    board = {'id': str(TaskBoard.objects.get(name='Ops').id)}
    assert r.status_code == 204 and r['HX-Redirect'] == f"/tasks/{board['id']}"
    page = c.get(f"/tasks/{board['id']}").content.decode()
    assert 'Owned by Ram Bahadur; 1 other person on it.' in page


def test_drawer_edits_fields_and_notifies_new_assignees(w):
    team_board(w)
    c = client_for(w.ram)
    r = c.get(f'/_/tasks/{w.task_id}', **htmx('hj-drawer-body'))
    html = r.content.decode()
    assert r.status_code == 200 and 'data-modal-title="Ops"' in html and 'Count stock' in html and 'Remove task' in html
    # Not an HTMX request: the board opens with the drawer showing the task.
    assert c.get(f'/_/tasks/{w.task_id}')['Location'] == f'/tasks/{w.board_id}?task={w.task_id}'
    url = f'/_/tasks/{w.task_id}/update'
    r = c.post(url, {'priority': 'high'}, **htmx('tk-drawer'))
    assert r.status_code == 200 and 'hj:refresh-tk-board' in hx_triggers(r)
    c.post(url, {'title': '  Count all stock '}, **htmx('tk-drawer'))
    c.post(url, {'labels_sent': '1', 'labels': [], 'label_new': 'stock'}, **htmx('tk-drawer'))
    c.post(url, {'labels_sent': '1', 'labels': ['stock'], 'label_new': 'urgent-ish'}, **htmx('tk-drawer'))
    c.post(url, {'labels_sent': '1', 'labels': ['stock', 'urgent-ish'], 'remove_label': 'stock'}, **htmx('tk-drawer'))
    c.post(url, {'description': 'All shelves'}, **htmx('tk-drawer'))
    c.post(url, {'due_date': ''}, **htmx('tk-drawer'))
    t = Task.objects.filter(pk=w.task_id).values('title', 'priority', 'labels', 'description', 'due_date').get()
    assert t == {'title': 'Count all stock', 'priority': 'high', 'labels': ['urgent-ish'], 'description': 'All shelves', 'due_date': None}
    # Assign Ram himself too; Ram assigned himself, so nobody new is told.
    c.post(url, {'assignees_sent': '1', 'assignee_ids': [w.sita.id, w.ram.id]}, **htmx('tk-drawer'))
    assert TaskAssignee.objects.filter(task_id=w.task_id).count() == 2
    assert not NotificationOutbox.objects.filter(user_id=w.ram.id, template='tasks.assigned').exists()
    bad = c.post(url, {'assignees_sent': '1', 'assignee_ids': [w.other.id]}, **htmx('tk-drawer'))
    assert bad.status_code == 400 and toasts(bad)[0]['message'] == 'assign tasks only to people on this board'
    # Unassign everyone.
    c.post(url, {'assignees_sent': '1'}, **htmx('tk-drawer'))
    assert not TaskAssignee.objects.filter(task_id=w.task_id).exists()


def test_checklist_comments_and_attachments_in_the_drawer(w):
    team_board(w)
    c = client_for(w.sita)
    r = c.post(f'/_/tasks/{w.task_id}/checklist', {'text': 'Shelf A'}, **htmx('tk-drawer'))
    assert r.status_code == 200 and 'Shelf A' in r.content.decode() and 'Checklist (0/1)' in r.content.decode()
    item = str(TaskChecklistItem.objects.get(task_id=w.task_id).id)
    r = c.post(f'/_/tasks/checklist/{item}', {'done': 'on'}, **htmx('tk-drawer'))
    assert 'Checklist (1/1)' in r.content.decode()
    assert client_for(w.other).post(f'/_/tasks/checklist/{item}', {}, **htmx()).status_code == 404
    r = c.post(f'/_/tasks/{w.task_id}/comments', {'body': 'Half done'}, **htmx('tk-drawer'))
    assert 'Half done' in r.content.decode()
    assert NotificationOutbox.objects.filter(user_id=w.ram.id, template='tasks.comment').count() == 1
    r = c.post(f'/_/tasks/{w.task_id}/attachments', {'file_id': upload(w.sita)}, **htmx('tk-drawer'))
    assert r.status_code == 200 and 'count.pdf' in r.content.decode()
    att = str(TaskAttachment.objects.get(task_id=w.task_id).id)
    f = client_for(w.ram).get(f'/_/tasks/attachments/{att}/file')
    assert f.status_code == 200 and 'application/pdf' in f['Content-Type']
    assert client_for(w.other).get(f'/_/tasks/attachments/{att}/file').status_code == 404
    full = tk.get_task(w.sita, w.task_id)['task']
    assert (full['checklist_total'], full['checklist_done'], full['comments'], full['attachments']) == (1, 1, 1, 1)
    # The board card shows the counts.
    board = c.get(f'/_/tasks/boards/{w.board_id}/columns', **htmx('tk-board')).content.decode()
    assert '1/1' in board
    r = c.post(f'/_/tasks/checklist/{item}/delete', **htmx('tk-drawer'))
    assert r.status_code == 200 and 'Shelf A' not in r.content.decode()
    r = client_for(w.hr).post(f'/_/tasks/attachments/{att}/delete', **htmx('tk-drawer'))
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only the person who added it or the board owner can remove it'
    r = c.post(f'/_/tasks/attachments/{att}/delete', **htmx('tk-drawer'))
    assert r.status_code == 200 and 'count.pdf' not in r.content.decode()


def test_board_settings_columns_members_and_archive(w):
    team_board(w)
    c = client_for(w.ram)
    form = c.get(f'/_/tasks/boards/{w.board_id}/settings', **htmx()).content.decode()
    assert 'Board settings' in form and 'People on this board' in form and w.sita.name in form
    assert client_for(w.sita).get(f'/_/tasks/boards/{w.board_id}/settings', **htmx()).status_code == 403
    tk.update_task(w.sita, w.task_id, {'columnId': 'doing'})
    body = {'name': 'Ops', 'col_id': ['todo', 'done'], 'col_name': ['Backlog', 'Finished'], 'col_done': ['done'],
            'member_ids': [w.sita.id]}
    r = c.post(f'/_/tasks/boards/{w.board_id}/settings', body, **htmx())
    assert r.status_code == 409
    assert toasts(r)[0]['message'] == '1 task(s) are in the columns you removed; choose a column to move them to'
    r = c.post(f'/_/tasks/boards/{w.board_id}/settings', {**body, 'col_done': []}, **htmx())
    assert r.status_code == 400
    r = c.post(f'/_/tasks/boards/{w.board_id}/settings', {**body, 'move_to': 'todo'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Board saved.'
    assert r['HX-Redirect'] == f'/tasks/{w.board_id}'
    assert Task.objects.get(pk=w.task_id).column_id == 'todo'
    assert [x['name'] for x in tk.get_board(w.ram, w.board_id)['board']['columns']] == ['Backlog', 'Finished']
    # Taking Sita off the board unassigns her and hides the board from her.
    r = c.post(f'/_/tasks/boards/{w.board_id}/settings', {**body, 'member_ids': []}, **htmx())
    assert r.status_code == 204
    assert client_for(w.sita).get(f'/tasks/{w.board_id}').status_code == 404
    assert not TaskAssignee.objects.filter(task_id=w.task_id).exists()
    r = c.post(f'/_/tasks/boards/{w.board_id}/archive', **htmx())
    assert r['HX-Redirect'] == '/tasks' and toasts(r)[0]['message'] == 'Board archived.'
    assert 'Ops' not in c.get('/tasks').content.decode()


def test_personal_board_settings_have_no_members_or_archive(w):
    board_id = tk.personal_board(w.sita)['id']
    c = client_for(w.sita)
    form = c.get(f'/_/tasks/boards/{board_id}/settings', **htmx()).content.decode()
    assert 'People on this board' not in form and 'Archive board' not in form
    r = c.post(f'/_/tasks/boards/{board_id}/settings', {'name': 'Home jobs', 'col_id': ['todo', 'done'],
                                                        'col_name': ['To do', 'Done'], 'col_done': ['done']}, **htmx())
    assert r.status_code == 204
    assert tk.personal_board(w.sita)['name'] == 'Home jobs'
    assert c.post(f'/_/tasks/boards/{board_id}/archive', **htmx()).status_code == 400


def test_report_view_and_hr_access(w):
    team_board(w)
    Task.objects.filter(pk=w.task_id).update(due_date=plus(-1))
    c = client_for(w.ram)
    html = c.get(f'/tasks/{w.board_id}?view=report').content.decode()
    for text in ('Open', 'Overdue', 'Done this week', 'Unassigned', 'By person', 'Overdue tasks', 'Count stock', w.sita.name):
        assert text in html
    assert 'To do: 1' in html
    assert client_for(w.other).get(f'/_/tasks/boards/{w.board_id}/report', **htmx()).status_code == 404
    # HR can open a team board, but it is not theirs to change.
    hr = client_for(w.hr)
    page = hr.get(f'/tasks/{w.board_id}').content.decode()
    assert 'Count stock' in page and 'Board settings' not in page and 'Team board' in page


def test_only_creator_or_owner_removes_a_task(w):
    team_board(w)
    r = client_for(w.sita).post(f'/_/tasks/{w.task_id}/delete', **htmx())
    assert r.status_code == 403
    assert toasts(r)[0]['message'] == 'only the person who created the task or the board owner can remove it'
    html = client_for(w.sita).get(f'/_/tasks/{w.task_id}', **htmx()).content.decode()
    assert 'Remove task' not in html
    r = client_for(w.ram).post(f'/_/tasks/{w.task_id}/delete', **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Task removed.'
    assert {'hj:close-modal', 'hj:refresh-tk-board'} <= set(hx_triggers(r))
    assert 'Count stock' not in client_for(w.ram).get(f'/tasks/{w.board_id}').content.decode()


def test_nav_lists_tasks_and_page_open_task(w):
    board_id = tk.personal_board(w.sita)['id']
    task = tk.create_task(w.sita, board_id, {'title': 'Call bank', 'dueDate': today()})
    html = client_for(w.sita).get(f"/tasks?task={task['id']}").content.decode()
    assert f"/_/tasks/{task['id']}" in html and 'href="/tasks"' in html


def test_drawer_due_date_saves_only_the_picked_date_and_cards_show_it_in_bs(w):
    from hajiri.web.templatetags.hj import fmt
    team_board(w)
    c = client_for(w.ram)
    html = c.get(f'/_/tasks/{w.task_id}', **htmx('hj-drawer-body')).content.decode()
    # The picker's year list and typed-date box fire change too; only the hidden due_date input saves.
    assert "hx-trigger=\"change[target.name=='due_date']\"" in html
    due = plus(3)
    r = c.post(f'/_/tasks/{w.task_id}/update', {'due_date': due}, **htmx('tk-drawer'))
    assert r.status_code == 200
    assert Task.objects.get(pk=w.task_id).due_date.isoformat() == due
    board = c.get(f'/_/tasks/boards/{w.board_id}/columns', **htmx('tk-board')).content.decode()
    assert fmt.fmt_date_short(due) in board


def test_a_nul_character_in_a_page_post_is_a_400_not_a_crash(w):
    c = client_for(w.sita)
    board_id = tk.personal_board(w.sita)['id']
    r = c.post(f'/_/tasks/boards/{board_id}/tasks', {'title': 'bad\x00title'}, **htmx('tk-board'))
    assert r.status_code == 400
    assert not Task.objects.filter(board_id=board_id).exists()
