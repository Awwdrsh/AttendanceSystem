"""
Lifecycle JSON API (the routes of apps/api/src/modules/lifecycle/routes.ts under /api/v1/lifecycle), used by the React
app and Bearer-token clients: settings, checklist templates, onboarding and clearance cases and their tasks, probation,
resignations and exits, the exit interview and the asset register. Thin DRF views over the route functions in
hajiri.lifecycle.api (which hold the roles, validation, scoping and the TS messages); the response bodies are the TS
ones. As the TS router's middleware, every request first seeds the company's lifecycle defaults.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, IsSignedIn, SameOriginAuthentication

from . import api as lc
from .serializers import (AddTaskIn, AssetIn, AssetQuery, AssetTypeIn, AssetUpdateIn, AssignAssetIn, CancelExitIn, CommentIn,
                          CompleteExitIn, ConfirmIn, InterviewIn, PolicyIn, ProbationQuery, QuestionsIn, RecordExitIn, ResignIn,
                          ReturnAssetIn, ReviewIn, StartCaseIn, TemplateIn, UpdateExitIn, UpdateTaskIn)

OK = {'ok': True}


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


def _q(request, key: str) -> str | None:
    return request.query_params.get(key) or None


class LifecycleView(APIView):
    """The session cookie with the TS Origin check (the React app sends no CSRF token) or a Bearer token; the module
    answers 404 {"error": "feature off", "feature": "lifecycle"} when the company switched it off (TS featureGate)."""
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn, FeatureEnabled('lifecycle')]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)        # authentication and permissions first
        lc.ensure_defaults(request.user)


# ------------------------------------------------------------ settings

class SettingsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_settings_get', responses=dict)
    def get(self, request):
        return Response({'policy': lc.get_settings(request.user)})

    @extend_schema(operation_id='lifecycle_settings_put', request=PolicyIn, responses=dict)
    def put(self, request):
        return Response({'policy': lc.save_settings(request.user, request.data)})


# ------------------------------------------------------------ templates

class TemplatesView(LifecycleView):
    @extend_schema(operation_id='lifecycle_templates', parameters=[OpenApiParameter('kind', str, required=False)], responses=dict)
    def get(self, request):
        return Response({'templates': lc.templates(request.user, _q(request, 'kind'))})

    @extend_schema(operation_id='lifecycle_template_create', request=TemplateIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.save_template(request.user, None, request.data)})


class TemplateView(LifecycleView):
    @extend_schema(operation_id='lifecycle_template_update', request=TemplateIn, responses=dict)
    def put(self, request, id):
        return Response({'id': lc.save_template(request.user, id, request.data)})


# ------------------------------------------------------------ cases and tasks

class CasesView(LifecycleView):
    @extend_schema(operation_id='lifecycle_cases', parameters=[OpenApiParameter('kind', str, required=False),
                                                               OpenApiParameter('status', str, required=False)], responses=dict)
    def get(self, request):
        return Response({'cases': lc.cases(request.user, _q(request, 'kind'), _q(request, 'status'))})

    @extend_schema(operation_id='lifecycle_case_start', request=StartCaseIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.start_onboarding(request.user, request.data)})


class CaseView(LifecycleView):
    @extend_schema(operation_id='lifecycle_case', responses=dict)
    def get(self, request, id):
        return Response(lc.case_detail(request.user, id))


class CaseCancelView(LifecycleView):
    @extend_schema(operation_id='lifecycle_case_cancel', request=None, responses=dict)
    def post(self, request, id):
        lc.cancel_case(request.user, id)
        return Response(OK)


class CaseTasksView(LifecycleView):
    @extend_schema(operation_id='lifecycle_case_task_add', request=AddTaskIn, responses={201: dict})
    def post(self, request, id):
        return _created({'task': lc.add_task(request.user, id, request.data)})


class TaskView(LifecycleView):
    @extend_schema(operation_id='lifecycle_task_update', request=UpdateTaskIn, responses=dict)
    def patch(self, request, id):
        return Response(lc.update_task(request.user, id, request.data))


class TaskCommentsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_task_comments', responses=dict)
    def get(self, request, id):
        return Response({'comments': lc.comments(request.user, id)})

    @extend_schema(operation_id='lifecycle_task_comment_add', request=CommentIn, responses={201: dict})
    def post(self, request, id):
        return _created({'comment': lc.add_comment(request.user, id, request.data)})


class MyTasksView(LifecycleView):
    @extend_schema(operation_id='lifecycle_my_tasks', responses=dict)
    def get(self, request):
        return Response({'tasks': lc.my_tasks(request.user)})


class EmployeeSummaryView(LifecycleView):
    @extend_schema(operation_id='lifecycle_employee_summary', responses=dict)
    def get(self, request, id):
        return Response(lc.employee_summary(request.user, id))


# ------------------------------------------------------------ probation

class ProbationView(LifecycleView):
    @extend_schema(operation_id='lifecycle_probation', parameters=[ProbationQuery], responses=dict)
    def get(self, request):
        return Response({'employees': lc.probation(request.user, _q(request, 'within'))})


class ProbationReviewsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_probation_review', request=ReviewIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.submit_review(request.user, request.data)})


class ProbationConfirmView(LifecycleView):
    @extend_schema(operation_id='lifecycle_probation_confirm', request=ConfirmIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.confirm(request.user, request.data)})


# ------------------------------------------------------------ exits

class ExitsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exits', parameters=[OpenApiParameter('status', str, required=False)], responses=dict)
    def get(self, request):
        return Response({'exits': lc.exits(request.user, _q(request, 'status'))})

    @extend_schema(operation_id='lifecycle_exit_record', request=RecordExitIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.create_exit(request.user, request.data)})


class MyExitsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_my_exits', responses=dict)
    def get(self, request):
        return Response(lc.my_exits(request.user))


class ResignView(LifecycleView):
    @extend_schema(operation_id='lifecycle_resign', request=ResignIn, responses={201: dict})
    def post(self, request):
        return _created(lc.resign(request.user, request.data))


class ExitView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit', responses=dict)
    def get(self, request, id):
        return Response(lc.exit_detail(request.user, id))

    @extend_schema(operation_id='lifecycle_exit_update', request=UpdateExitIn, responses=dict)
    def patch(self, request, id):
        lc.update_exit(request.user, id, request.data)
        return Response(OK)


class ExitWithdrawView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit_withdraw', request=None, responses=dict)
    def post(self, request, id):
        lc.withdraw_exit(request.user, id)
        return Response(OK)


class ExitCancelView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit_cancel', request=CancelExitIn, responses=dict)
    def post(self, request, id):
        lc.cancel_exit(request.user, id, request.data)
        return Response(OK)


class ExitCompleteView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit_complete', request=CompleteExitIn, responses=dict)
    def post(self, request, id):
        lc.complete(request.user, id, request.data or {})
        return Response(OK)


class ExitQuestionsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit_questions', responses=dict)
    def get(self, request):
        return Response({'questions': lc.questions(request.user)})

    @extend_schema(operation_id='lifecycle_exit_questions_save', request=QuestionsIn, responses=dict)
    def put(self, request):
        lc.save_questions(request.user, request.data)
        return Response(OK)


class ExitInterviewView(LifecycleView):
    @extend_schema(operation_id='lifecycle_exit_interview', responses=dict)
    def get(self, request, id):
        return Response({'interview': lc.interview(request.user, id)})

    @extend_schema(operation_id='lifecycle_exit_interview_record', request=InterviewIn, responses={201: dict})
    def post(self, request, id):
        lc.record_interview(request.user, id, request.data)
        return _created(OK)


# ------------------------------------------------------------ assets

class AssetTypesView(LifecycleView):
    @extend_schema(operation_id='lifecycle_asset_types', responses=dict)
    def get(self, request):
        return Response({'types': lc.asset_types(request.user)})

    @extend_schema(operation_id='lifecycle_asset_type_add', request=AssetTypeIn, responses={201: dict})
    def post(self, request):
        return _created({'type': lc.add_asset_type(request.user, request.data)})


class AssetsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_assets', parameters=[AssetQuery], responses=dict)
    def get(self, request):
        query = {k: v for k, v in request.query_params.dict().items() if v != ''}
        return Response({'assets': lc.assets(request.user, query)})

    @extend_schema(operation_id='lifecycle_asset_create', request=AssetIn, responses={201: dict})
    def post(self, request):
        return _created({'id': lc.create_asset(request.user, request.data)})


class MyAssetsView(LifecycleView):
    @extend_schema(operation_id='lifecycle_my_assets', responses=dict)
    def get(self, request):
        return Response({'assets': lc.my_assets(request.user)})


class AssetView(LifecycleView):
    @extend_schema(operation_id='lifecycle_asset_update', request=AssetUpdateIn, responses=dict)
    def patch(self, request, id):
        lc.update_asset(request.user, id, request.data)
        return Response(OK)


class AssetHistoryView(LifecycleView):
    @extend_schema(operation_id='lifecycle_asset_history', responses=dict)
    def get(self, request, id):
        return Response({'history': lc.asset_history(request.user, id)})


class AssetAssignView(LifecycleView):
    @extend_schema(operation_id='lifecycle_asset_assign', request=AssignAssetIn, responses=dict)
    def post(self, request, id):
        lc.assign_asset(request.user, id, request.data)
        return Response(OK)


class AssetReturnView(LifecycleView):
    @extend_schema(operation_id='lifecycle_asset_return', request=ReturnAssetIn, responses=dict)
    def post(self, request, id):
        lc.return_asset(request.user, id, request.data)
        return Response(OK)
