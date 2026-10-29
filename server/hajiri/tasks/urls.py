from django.urls import path

# JSON API for the React app (cookie session) and Bearer-token clients (TS: /api/v1/tasks/...). Path ids
# are <str:> so a malformed id answers 400 "Invalid UUID" like the zod z.uuid(); the fixed paths (boards, people,
# my/todo) stay before /api/v1/tasks/<id>.
from . import api

urlpatterns = [
    path('api/v1/tasks/boards', api.BoardsView.as_view(), name='api_boards'),
    path('api/v1/tasks/boards/<str:board_id>', api.BoardView.as_view(), name='api_board'),
    path('api/v1/tasks/boards/<str:board_id>/members', api.BoardMembersView.as_view(), name='api_board_members'),
    path('api/v1/tasks/boards/<str:board_id>/tasks', api.BoardTasksView.as_view(), name='api_board_tasks'),
    path('api/v1/tasks/reports/boards/<str:board_id>', api.BoardReportView.as_view(), name='api_board_report'),
    path('api/v1/tasks/people', api.PeopleView.as_view(), name='api_people'),
    path('api/v1/tasks/my/todo', api.MyTodoView.as_view(), name='api_my_todo'),
    path('api/v1/tasks/checklist/<str:item_id>', api.ChecklistItemView.as_view(), name='api_checklist_item'),
    path('api/v1/tasks/attachments/<str:att_id>', api.AttachmentView.as_view(), name='api_attachment'),
    path('api/v1/tasks/attachments/<str:att_id>/file', api.AttachmentFileView.as_view(), name='api_attachment_file'),
    path('api/v1/tasks/<str:task_id>', api.TaskView.as_view(), name='api_task'),
    path('api/v1/tasks/<str:task_id>/checklist', api.TaskChecklistView.as_view(), name='api_task_checklist'),
    path('api/v1/tasks/<str:task_id>/comments', api.TaskCommentsView.as_view(), name='api_task_comments'),
    path('api/v1/tasks/<str:task_id>/attachments', api.TaskAttachmentsView.as_view(), name='api_task_attachments'),
]
