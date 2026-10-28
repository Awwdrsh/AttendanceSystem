"""
Task boards JSON API for Bearer-token clients (the TS routes under /api/v1/tasks). DRF views with the project
defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn, the TS error shape and JsonRenderer, plus the Tasks
feature switch. Rules and validation stay in services.py; every view works in the caller's active company.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, IsSignedIn
from hajiri.files.store import stored_file_response

from . import services
from .serializers import (
    AttachmentIn, BoardIn, BoardPatchIn, ChecklistIn, ChecklistPatchIn, CommentIn, MembersIn, TaskIn, TaskPatchIn,
)


class _TasksView(APIView):
    permission_classes = [IsSignedIn, FeatureEnabled('tasks')]


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


class BoardsView(_TasksView):
    """GET /api/v1/tasks/boards (?all=1: HR sees every team board); POST sets up a team board."""

    @extend_schema(operation_id='tasks_boards_get', parameters=[OpenApiParameter('all', str, required=False)],
                   responses=dict)
    def get(self, request):
        return Response(services.list_boards(request.user, all_=request.query_params.get('all') == '1'))

    @extend_schema(operation_id='tasks_boards_post', request=BoardIn, responses={201: dict})
    def post(self, request):
        return _created({'board': services.create_board(request.user, request.data)})


class BoardView(_TasksView):
    @extend_schema(operation_id='tasks_board_get', responses=dict)
    def get(self, request, board_id):
        return Response(services.get_board(request.user, board_id))

    @extend_schema(operation_id='tasks_board_patch', request=BoardPatchIn, responses=dict)
    def patch(self, request, board_id):
        return Response(services.update_board(request.user, board_id, request.data))


class BoardMembersView(_TasksView):
    @extend_schema(operation_id='tasks_board_members_put', request=MembersIn, responses=dict)
    def put(self, request, board_id):
        return Response(services.set_members(request.user, board_id, request.data))


class BoardTasksView(_TasksView):
    @extend_schema(operation_id='tasks_board_tasks_post', request=TaskIn, responses={201: dict})
    def post(self, request, board_id):
        return _created({'task': services.create_task(request.user, board_id, request.data)})


class BoardReportView(_TasksView):
    @extend_schema(operation_id='tasks_board_report_get', responses=dict)
    def get(self, request, board_id):
        return Response(services.board_report(request.user, board_id))


class PeopleView(_TasksView):
    @extend_schema(operation_id='tasks_people_get', responses=dict)
    def get(self, request):
        return Response(services.people(request.user))


class MyTodoView(_TasksView):
    @extend_schema(operation_id='tasks_my_todo_get', responses=dict)
    def get(self, request):
        return Response(services.my_todo(request.user))


class TaskView(_TasksView):
    @extend_schema(operation_id='tasks_task_get', responses=dict)
    def get(self, request, task_id):
        return Response(services.get_task(request.user, task_id))

    @extend_schema(operation_id='tasks_task_patch', request=TaskPatchIn, responses=dict)
    def patch(self, request, task_id):
        return Response(services.update_task(request.user, task_id, request.data))

    @extend_schema(operation_id='tasks_task_delete', responses=dict)
    def delete(self, request, task_id):
        return Response(services.delete_task(request.user, task_id))


class TaskChecklistView(_TasksView):
    @extend_schema(operation_id='tasks_task_checklist_post', request=ChecklistIn, responses={201: dict})
    def post(self, request, task_id):
        return _created({'item': services.add_checklist(request.user, task_id, request.data)})


class ChecklistItemView(_TasksView):
    @extend_schema(operation_id='tasks_checklist_item_patch', request=ChecklistPatchIn, responses=dict)
    def patch(self, request, item_id):
        return Response(services.update_checklist(request.user, item_id, request.data))

    @extend_schema(operation_id='tasks_checklist_item_delete', responses=dict)
    def delete(self, request, item_id):
        return Response(services.delete_checklist(request.user, item_id))


class TaskCommentsView(_TasksView):
    @extend_schema(operation_id='tasks_task_comments_post', request=CommentIn, responses={201: dict})
    def post(self, request, task_id):
        return _created({'comment': services.add_comment(request.user, task_id, request.data)})


class TaskAttachmentsView(_TasksView):
    @extend_schema(operation_id='tasks_task_attachments_post', request=AttachmentIn, responses={201: dict})
    def post(self, request, task_id):
        return _created(services.add_attachment(request.user, task_id, request.data))


class AttachmentView(_TasksView):
    @extend_schema(operation_id='tasks_attachment_delete', responses=dict)
    def delete(self, request, att_id):
        return Response(services.delete_attachment(request.user, att_id))


class AttachmentFileView(_TasksView):
    @extend_schema(operation_id='tasks_attachment_file_get',
                   parameters=[OpenApiParameter('download', str, required=False)], responses={(200, '*/*'): bytes})
    def get(self, request, att_id):
        f = services.attachment_file(request.user, att_id)
        return stored_file_response(f, download=request.query_params.get('download') == '1')
