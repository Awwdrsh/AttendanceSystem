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
