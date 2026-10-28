from django.urls import path

from . import views

urlpatterns = [
    # Pages (App.tsx: /tasks and /tasks/:boardId).
    path('tasks', views.page, name='board'),
    path('tasks/<uuid:board_id>', views.page, name='board_detail'),
    # Boards.
    path('_/tasks/boards/new', views.board_new, name='board_new'),
    path('_/tasks/boards/<uuid:board_id>/columns', views.board_columns, name='board_columns'),
    path('_/tasks/boards/<uuid:board_id>/report', views.board_report, name='board_report'),
    path('_/tasks/boards/<uuid:board_id>/settings', views.board_settings, name='board_settings'),
    path('_/tasks/boards/<uuid:board_id>/archive', views.board_archive, name='board_archive'),
    path('_/tasks/boards/<uuid:board_id>/tasks', views.task_new, name='task_new'),
    path('_/tasks/boards/<uuid:board_id>/move', views.task_move, name='task_move'),
    # My to-do (the Home card).
    path('_/tasks/todo', views.todo, name='todo'),
    path('_/tasks/todo/<uuid:task_id>/done', views.todo_done, name='todo_done'),
    # One task (the drawer).
    path('_/tasks/<uuid:task_id>', views.task, name='task'),
    path('_/tasks/<uuid:task_id>/update', views.task_update, name='task_update'),
    path('_/tasks/<uuid:task_id>/delete', views.task_delete, name='task_delete'),
    path('_/tasks/<uuid:task_id>/checklist', views.checklist_add, name='checklist_add'),
    path('_/tasks/checklist/<uuid:item_id>', views.checklist_update, name='checklist_update'),
    path('_/tasks/checklist/<uuid:item_id>/delete', views.checklist_delete, name='checklist_delete'),
    path('_/tasks/<uuid:task_id>/comments', views.comment_add, name='comment_add'),
    path('_/tasks/<uuid:task_id>/attachments', views.attachment_add, name='attachment_add'),
    path('_/tasks/attachments/<uuid:att_id>/file', views.attachment_file, name='attachment_file'),
    path('_/tasks/attachments/<uuid:att_id>/delete', views.attachment_delete, name='attachment_delete'),
]

# JSON API for Bearer-token clients (TS: /api/v1/tasks/...).
from . import api  # noqa: E402

urlpatterns += [
    path('api/v1/tasks/boards', api.BoardsView.as_view(), name='api_boards'),
    path('api/v1/tasks/boards/<uuid:board_id>', api.BoardView.as_view(), name='api_board'),
    path('api/v1/tasks/boards/<uuid:board_id>/members', api.BoardMembersView.as_view(), name='api_board_members'),
    path('api/v1/tasks/boards/<uuid:board_id>/tasks', api.BoardTasksView.as_view(), name='api_board_tasks'),
    path('api/v1/tasks/reports/boards/<uuid:board_id>', api.BoardReportView.as_view(), name='api_board_report'),
    path('api/v1/tasks/people', api.PeopleView.as_view(), name='api_people'),
    path('api/v1/tasks/my/todo', api.MyTodoView.as_view(), name='api_my_todo'),
    path('api/v1/tasks/checklist/<uuid:item_id>', api.ChecklistItemView.as_view(), name='api_checklist_item'),
    path('api/v1/tasks/attachments/<uuid:att_id>', api.AttachmentView.as_view(), name='api_attachment'),
    path('api/v1/tasks/attachments/<uuid:att_id>/file', api.AttachmentFileView.as_view(), name='api_attachment_file'),
    path('api/v1/tasks/<uuid:task_id>', api.TaskView.as_view(), name='api_task'),
    path('api/v1/tasks/<uuid:task_id>/checklist', api.TaskChecklistView.as_view(), name='api_task_checklist'),
    path('api/v1/tasks/<uuid:task_id>/comments', api.TaskCommentsView.as_view(), name='api_task_comments'),
    path('api/v1/tasks/<uuid:task_id>/attachments', api.TaskAttachmentsView.as_view(), name='api_task_attachments'),
]
