"""Port of apps/api/test/tasks.test.ts at the service level (same steps, same assertions)."""
import pytest

from hajiri.db.models import (
    AuditLog, NotificationOutbox, StoredFile, Task, TaskAssignee, TaskAttachment, TaskBoard, TaskChecklistItem,
)
from hajiri.core.errors import ValidationError
from hajiri.tasks import services as tk
from hajiri.worker.registry import daily_jobs

from .world import build, plus, status, team_board, today, upload


@pytest.fixture
def w(db):
    return build()


@pytest.fixture
def t(w):
    return team_board(w)


# ---------------------------------------------------------------- personal boards

def test_every_user_gets_a_private_personal_board(w):
    r = tk.list_boards(w.sita)
    assert r['canCreateTeamBoard'] is False
    b = next(x for x in r['boards'] if x['kind'] == 'personal')
    assert [c['name'] for c in b['columns']] == ['To do', 'In progress', 'Done']
    # Asking again does not make a second one.
    again = tk.list_boards(w.sita)
    assert len([x for x in again['boards'] if x['kind'] == 'personal']) == 1
    assert TaskBoard.objects.filter(owner_user_id=w.sita.id).count() == 1


def test_nobody_else_can_open_it_not_even_hr(w):
    board_id = tk.personal_board(w.sita)['id']
    assert status(tk.get_board, w.hr, board_id) == 404
    assert status(tk.get_board, w.ram, board_id) == 404


def test_personal_tasks_go_to_the_owner_done_completes_and_my_todo(w):
    board_id = tk.personal_board(w.sita)['id']
    task = tk.create_task(w.sita, board_id, {'title': 'Renew passport', 'dueDate': plus(1), 'priority': 'high', 'labels': ['personal']})
    assert status(tk.create_task, w.sita, board_id, {'title': 'x', 'columnId': 'nope'}) == 400
    assert [str(u) for u in TaskAssignee.objects.filter(task_id=task['id']).values_list('user_id', flat=True)] == [w.sita.id]
    todo = tk.my_todo(w.sita)
    assert task['id'] in [x['id'] for x in todo['tasks']]
    done = tk.update_task(w.sita, task['id'], {'columnId': 'done'})
    assert done['task']['completed_at'] is not None
    assert task['id'] not in [x['id'] for x in tk.my_todo(w.sita)['tasks']]
    back = tk.update_task(w.sita, task['id'], {'columnId': 'todo'})
    assert back['task']['completed_at'] is None


def test_employees_cannot_set_up_team_boards(w):
    assert status(tk.create_board, w.sita, {'name': 'Mine'}) == 403


def test_personal_board_tasks_cannot_be_assigned_to_others_and_board_cannot_be_archived(w):
    board_id = tk.personal_board(w.sita)['id']
    # The owner is always the assignee on a personal board, whatever was asked.
    task = tk.create_task(w.sita, board_id, {'title': 'Mine', 'assigneeIds': [w.ram.id]})
    assert [str(u) for u in TaskAssignee.objects.filter(task_id=task['id']).values_list('user_id', flat=True)] == [w.sita.id]
    with pytest.raises(Exception) as e:
        tk.update_board(w.sita, board_id, {'archived': True})
    assert e.value.message == 'your personal board cannot be archived'
    assert status(tk.set_members, w.sita, board_id, {'userIds': []}) == 400


# ---------------------------------------------------------------- team boards

def test_manager_sets_up_board_with_reports_non_report_refused(w):
    with pytest.raises(Exception) as e:
        tk.create_board(w.ram, {'name': 'Ops', 'memberUserIds': [w.other.id]})
    assert e.value.status == 403 and e.value.message == 'managers can add only their direct reports to a team board'
    board = tk.create_board(w.ram, {'name': 'Ops', 'addMyReports': True})
    b = tk.get_board(w.ram, board['id'])
    assert b['access'] == 'owner'
    assert [m['user_id'] for m in b['members']] == [w.sita.id]
    assert 'create' in AuditLog.objects.filter(row_id=board['id']).values_list('action', flat=True)


def test_tasks_assigned_to_members_only_and_assignee_notified(w):
    board_id = tk.create_board(w.ram, {'name': 'Ops', 'addMyReports': True})['id']
    assert status(tk.create_task, w.ram, board_id, {'title': 'Count stock', 'assigneeIds': [w.other.id]}) == 400
    task = tk.create_task(w.ram, board_id, {'title': 'Count stock', 'assigneeIds': [w.sita.id], 'dueDate': today()})
    assert task['id']
    n = list(NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.assigned').values('body'))
    assert len(n) == 1
    assert n[0]['body'].startswith('Ram Bahadur assigned you “Count stock” on Ops, due ')


def test_members_see_board_outsiders_404_hr_opens_team_boards(t):
    w = t
    assert tk.get_board(w.sita, w.board_id)['access'] == 'member'
    assert status(tk.get_task, w.other, w.task_id) == 404
    assert status(tk.get_board, w.other, w.board_id) == 404
    assert tk.get_board(w.hr, w.board_id)['access'] == 'hr'
    assert w.board_id in [x['id'] for x in tk.list_boards(w.sita)['boards']]
    # HR lists every team board only when asking for all of them.
    assert w.board_id not in [x['id'] for x in tk.list_boards(w.hr)['boards']]
    assert w.board_id in [x['id'] for x in tk.list_boards(w.hr, all_=True)['boards']]


def test_checklist_comments_and_attachments(t):
    w = t
    item = tk.add_checklist(w.sita, w.task_id, {'text': 'Shelf A'})
    assert item['text'] == 'Shelf A' and item['done'] is False
    assert status(tk.update_checklist, w.sita, item['id'], {'done': True}) == 200
    assert status(tk.update_checklist, w.other, item['id'], {'done': False}) == 404
    cm = tk.add_comment(w.sita, w.task_id, {'body': 'Half done'})
    assert cm['name'] == w.sita.name
    n = list(NotificationOutbox.objects.filter(user_id=w.ram.id, template='tasks.comment').values('body'))
    assert len(n) == 1 and n[0]['body'] == f'{w.sita.name} commented on “Count stock”.'
    file_id = upload(w.sita)
    at = tk.add_attachment(w.sita, w.task_id, {'fileId': file_id})
    f = tk.attachment_file(w.ram, at['id'])
    assert f['mime'] == 'application/pdf'
    assert status(tk.attachment_file, w.other, at['id']) == 404
    full = tk.get_task(w.sita, w.task_id)
    assert {k: full['task'][k] for k in ('checklist_total', 'checklist_done', 'comments', 'attachments')} == {
        'checklist_total': 1, 'checklist_done': 1, 'comments': 1, 'attachments': 1}
    assert StoredFile.objects.get(pk=file_id).attached_to == f"task_attachment:{at['id']}"


def test_only_owner_changes_columns_and_members_removed_column_needs_target(t):
    w = t
    cols = [{'id': 'todo', 'name': 'Backlog', 'done': False}, {'id': 'done', 'name': 'Finished', 'done': True}]
    assert status(tk.update_board, w.sita, w.board_id, {'columns': cols}) == 403
    tk.update_task(w.sita, w.task_id, {'columnId': 'doing'})
    with pytest.raises(Exception) as e:
        tk.update_board(w.ram, w.board_id, {'columns': cols})
    assert e.value.status == 409
    assert e.value.message == '1 task(s) are in the columns you removed; choose a column to move them to'
    with pytest.raises(ValidationError) as v:
        tk.update_board(w.ram, w.board_id, {'columns': [{'id': 'todo', 'name': 'A', 'done': False}]})
    assert v.value.status == 400 and v.value.issues[0]['message'] == 'mark at least one column as done'
    assert status(tk.update_board, w.ram, w.board_id, {'columns': cols, 'moveTo': 'todo'}) == 200
    assert tk.get_task(w.sita, w.task_id)['task']['column_id'] == 'todo'
    assert status(tk.set_members, w.sita, w.board_id, {'userIds': []}) == 403


def test_column_rules(t):
    w = t
    dup = [{'id': 'a', 'name': 'A', 'done': True}, {'id': 'a', 'name': 'B'}]
    with pytest.raises(ValidationError) as v:
        tk.update_board(w.ram, w.board_id, {'columns': dup})
    assert 'column ids must be unique' in [i['message'] for i in v.value.issues]
    with pytest.raises(ValidationError) as v:
        tk.update_board(w.ram, w.board_id, {'columns': [{'id': 'Bad Id', 'name': 'A', 'done': True}]})
    assert v.value.issues[0]['message'] == 'column ids are short lowercase words'
    # Marking a column done completes the tasks in it, unmarking reopens them.
    tk.update_board(w.ram, w.board_id, {'columns': [{'id': 'todo', 'name': 'To do', 'done': True},
                                                    {'id': 'doing', 'name': 'Doing'}, {'id': 'done', 'name': 'Done', 'done': True}]})
    assert tk.get_task(w.sita, w.task_id)['task']['completed_at'] is not None
    tk.update_board(w.ram, w.board_id, {'columns': tk.DEFAULT_COLUMNS})
    assert tk.get_task(w.sita, w.task_id)['task']['completed_at'] is None


def test_reports_count_open_overdue_and_per_assignee_daily_job_reminds_once(t):
    w = t
    Task.objects.filter(pk=w.task_id).update(due_date=plus(-1))
    r = tk.board_report(w.ram, w.board_id)
    assert {k: r['totals'][k] for k in ('open', 'overdue')} == {'open': 1, 'overdue': 1}
    a = r['assignees'][0]
    assert (a['user_id'], a['open'], a['overdue']) == (w.sita.id, 1, 1)
    assert [x['id'] for x in r['overdueTasks']] == [w.task_id]
    assert [(c['id'], c['tasks']) for c in r['columns']] == [('todo', 1), ('doing', 0), ('done', 0)]
    first = tk.task_reminders()
    assert first['overdue'] == 1
    second = tk.task_reminders()
    assert second['overdue'] == 0
    n = list(NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.overdue').values('title', 'body'))
    assert n == [{'title': 'Task overdue', 'body': '“Count stock” on Ops was due yesterday and is still open.'}]


def test_daily_job_reminds_due_today_for_personal_unassigned_owner(w):
    board_id = tk.personal_board(w.sita)['id']
    tk.create_task(w.sita, board_id, {'title': 'Pay rent', 'dueDate': today()})
    assert tk.task_reminders() == {'dueToday': 1, 'overdue': 0}
    n = list(NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.due_today').values('title', 'body'))
    assert n == [{'title': 'Task due today', 'body': '“Pay rent” on My tasks is due today.'}]
    # The worker runs it among the daily module jobs.
    job = next(j for j in daily_jobs() if j.name == 'taskReminders')
    assert job.fn(None) == {'dueToday': 0, 'overdue': 0}


def test_removing_a_member_unassigns_only_creator_or_owner_removes_task(t):
    w = t
    with pytest.raises(Exception) as e:
        tk.delete_task(w.sita, w.task_id)
    assert e.value.status == 403 and e.value.message == 'only the person who created the task or the board owner can remove it'
    m = tk.set_members(w.ram, w.board_id, {'userIds': []})
    assert m['members'] == []
    assert status(tk.get_task, w.sita, w.task_id) == 404
    assert not TaskAssignee.objects.filter(task_id=w.task_id).exists()
    assert status(tk.delete_task, w.ram, w.task_id) == 200
    assert 'archive' in AuditLog.objects.filter(row_id=w.task_id).values_list('action', flat=True)
    assert status(tk.get_task, w.ram, w.task_id) == 200  # archived, still readable by id
    assert w.task_id not in [x['id'] for x in tk.get_board(w.ram, w.board_id)['tasks']]


# ---------------------------------------------------------------- more rules from the routes

def test_members_reassign_and_new_assignees_are_notified(t):
    w = t
    tk.set_members(w.ram, w.board_id, {'userIds': [w.sita.id]})
    out = tk.update_task(w.sita, w.task_id, {'assigneeIds': [w.sita.id, w.ram.id]})
    assert sorted(a['userId'] for a in out['task']['assignees']) == sorted([w.sita.id, w.ram.id])
    assert NotificationOutbox.objects.filter(user_id=w.ram.id, template='tasks.assigned').count() == 1  # sita assigned ram; sita is already on it, so no new notice for her
    assert NotificationOutbox.objects.filter(user_id=w.sita.id, template='tasks.assigned').count() == 1
    assert status(tk.update_task, w.sita, w.task_id, {'assigneeIds': [w.other.id]}) == 400


def test_patch_touches_only_the_fields_sent(t):
    w = t
    tk.update_task(w.ram, w.task_id, {'priority': 'urgent', 'labels': ['stock'], 'description': 'All shelves'})
    out = tk.update_task(w.ram, w.task_id, {'title': '  Count all stock  '})['task']
    assert (out['title'], out['priority'], out['labels'], out['description']) == ('Count all stock', 'urgent', ['stock'], 'All shelves')
    assert len(out['assignees']) == 1
    out = tk.update_task(w.ram, w.task_id, {'dueDate': None, 'description': None})['task']
    assert out['due_date'] is None and out['description'] is None
    assert status(tk.update_task, w.ram, w.task_id, {'columnId': 'nope'}) == 400
    assert status(tk.update_task, w.ram, w.task_id, {'priority': 'asap'}) == 400


def test_limits_and_attachment_removal(t):
    w = t
    TaskChecklistItem.objects.bulk_create([TaskChecklistItem(task_id=w.task_id, text=f'Item {i}', position=i) for i in range(50)])
    with pytest.raises(Exception) as e:
        tk.add_checklist(w.sita, w.task_id, {'text': 'One more'})
    assert e.value.message == 'a task can have at most 50 checklist items'
    item = str(TaskChecklistItem.objects.filter(task_id=w.task_id).values_list('id', flat=True).first())
    assert status(tk.delete_checklist, w.sita, item) == 200
    assert status(tk.delete_checklist, w.sita, item) == 404
    at = tk.add_attachment(w.sita, w.task_id, {'fileId': upload(w.sita)})
    # Someone else's upload cannot be attached.
    assert status(tk.add_attachment, w.sita, w.task_id, {'fileId': upload(w.ram)}) == 403
    with pytest.raises(Exception) as e:
        tk.delete_attachment(w.hr, at['id'])
    assert e.value.status == 403 and e.value.message == 'only the person who added it or the board owner can remove it'
    assert status(tk.delete_attachment, w.ram, at['id']) == 200
    assert tk.get_task(w.sita, w.task_id)['attachments'] == []
    assert not TaskAttachment.objects.filter(pk=at['id']).exists()
    assert StoredFile.objects.get(uploaded_by=w.sita.id, attached_to__isnull=False).deleted_at is not None


def test_people_hr_everyone_manager_reports_employee_nobody(w):
    hr_people = [p['user_id'] for p in tk.people(w.hr)['people']]
    assert {w.sita.id, w.ram.id, w.other.id, w.hr.id} <= set(hr_people)
    assert [p['user_id'] for p in tk.people(w.ram)['people']] == [w.sita.id]
    assert tk.people(w.sita) == {'people': []}


def test_owner_renames_and_archives_a_team_board(t):
    w = t
    tk.update_board(w.ram, w.board_id, {'name': 'Ops team'})
    assert tk.get_board(w.sita, w.board_id)['board']['name'] == 'Ops team'
    tk.update_board(w.ram, w.board_id, {'archived': True})
    assert w.board_id not in [x['id'] for x in tk.list_boards(w.ram)['boards']]
    assert w.task_id not in [x['id'] for x in tk.my_todo(w.sita)['tasks']]
