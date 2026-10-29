"""
Performance JSON API: the TS routes under /api/v1/performance (apps/api/src/modules/performance/{goals,reviews,
conversations}.ts at 65a9079), which the React app calls (frontend/src/api/performance.js). Same paths, methods,
bodies, JSON shapes and status codes.

DRF views with the project defaults (hajiri.core.api): IsSignedIn, the TS error shape and JsonRenderer. They
authenticate with SameOriginAuthentication (the session cookie with the TS Origin check, never a CSRF token, as the
React client sends it; or a Bearer token). The feature switch is checked before any role (featureGate was mounted
ahead of the routes' requireRole). Rules and validation stay in services.py; every view works in the active company.
"""
from __future__ import annotations

from django.http import HttpResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.core.validation import validate

from . import serializers as s
from . import services

OK = {'ok': True}


HR = HasRole('hr')


class _View(APIView):
    """Signed in with the Performance feature on; the methods in `hr_methods` also need HR (requireRole('hr'))."""
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn, FeatureEnabled('performance')]
    hr_methods: tuple[str, ...] = ()

    def get_permissions(self):
        perms = super().get_permissions()
        if self.request.method in self.hr_methods:
            perms.append(HR())
        return perms


class _HrView(_View):
    hr_methods = ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


def _query(request) -> dict:
    return request.query_params.dict()


def _html(html: str) -> HttpResponse:
    res = HttpResponse(html, content_type='text/html; charset=utf-8')
    res['Cache-Control'] = 'private, no-store'
    return res


# ---------------------------------------------------------------- pickers

class DepartmentsView(_View):
    @extend_schema(operation_id='performance_departments', responses=dict)
    def get(self, request):
        return Response({'departments': services.departments(request.user)})


class ColleaguesView(_View):
    """Colleagues for pickers (feedback, peer reviewers): name, designation and department only."""

    @extend_schema(operation_id='performance_colleagues', responses=dict)
    def get(self, request):
        return Response({'colleagues': services.colleagues(request.user, request.query_params.get('q'))})


# ---------------------------------------------------------------- goals and key results

class GoalsView(_View):
    @extend_schema(operation_id='performance_goals_list', responses=dict)
    def get(self, request):
        return Response({'goals': services.list_goals(request.user, _query(request))})

    @extend_schema(operation_id='performance_goals_create', request=s.GoalIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_goal(request.user, request.data)})


class GoalView(_View):
    @extend_schema(operation_id='performance_goal_get', responses=dict)
    def get(self, request, goal_id):
        return Response(services.goal_detail(request.user, goal_id))

    @extend_schema(operation_id='performance_goal_patch', request=s.GoalPatch, responses=dict)
    def patch(self, request, goal_id):
        services.update_goal(request.user, goal_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_goal_cancel', responses=dict)
    def delete(self, request, goal_id):
        services.cancel_goal(request.user, goal_id)
        return Response(OK)


class GoalKeyResultsView(_View):
    @extend_schema(operation_id='performance_key_result_add', request=s.KeyResultIn, responses={201: dict})
    def post(self, request, goal_id):
        services.add_key_result(request.user, goal_id, request.data)
        return _created(OK)


class KeyResultView(_View):
    @extend_schema(operation_id='performance_key_result_patch', request=s.KeyResultIn, responses=dict)
    def patch(self, request, kr_id):
        services.update_key_result(request.user, kr_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_key_result_delete', responses=dict)
    def delete(self, request, kr_id):
        services.delete_key_result(request.user, kr_id)
        return Response(OK)


class GoalCheckinsView(_View):
    @extend_schema(operation_id='performance_goal_checkin', request=s.CheckinIn, responses={201: dict})
    def post(self, request, goal_id):
        services.checkin(request.user, goal_id, request.data)
        return _created(OK)


# ---------------------------------------------------------------- KPI library

class KpisView(_View):
    hr_methods = ('POST',)

    @extend_schema(operation_id='performance_kpis_list', responses=dict)
    def get(self, request):
        return Response({'kpis': services.list_kpis(request.user, request.query_params.get('designation'))})

    @extend_schema(operation_id='performance_kpis_create', request=s.KpiIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_kpi(request.user, request.data)})


class KpiView(_HrView):
    @extend_schema(operation_id='performance_kpi_patch', request=s.KpiIn, responses=dict)
    def patch(self, request, kpi_id):
        services.update_kpi(request.user, kpi_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_kpi_delete', responses=dict)
    def delete(self, request, kpi_id):
        services.delete_kpi(request.user, kpi_id)
        return Response(OK)


class KpiApplyView(_View):
    """Turn a library KPI into an individual goal with one key result."""

    @extend_schema(operation_id='performance_kpi_apply', request=s.ApplyKpiIn, responses={201: dict})
    def post(self, request, kpi_id):
        return _created({'id': services.apply_kpi(request.user, kpi_id, request.data)})


# ---------------------------------------------------------------- competency framework

class CompetenciesView(_View):
    hr_methods = ('POST',)

    @extend_schema(operation_id='performance_competencies_list', responses=dict)
    def get(self, request):
        q = validate(s.CompetencyQueryIn, _query(request))
        return Response({'competencies': services.list_competencies(
            request.user, q.get('designation') or None, q.get('grade') or None, all_=q.get('all') == '1')})

    @extend_schema(operation_id='performance_competencies_create', request=s.CompetencyIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_competency(request.user, request.data)})


class CompetencyView(_HrView):
    @extend_schema(operation_id='performance_competency_patch', request=s.CompetencyIn, responses=dict)
    def patch(self, request, comp_id):
        services.update_competency(request.user, comp_id, request.data)
        return Response(OK)


# ---------------------------------------------------------------- review cycles

class CyclesView(_View):
    hr_methods = ('POST',)

    @extend_schema(operation_id='performance_cycles_list', responses=dict)
    def get(self, request):
        return Response({'cycles': services.list_cycles(request.user)})

    @extend_schema(operation_id='performance_cycles_create', request=s.CycleIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_cycle(request.user, request.data)})


class CycleView(_View):
    hr_methods = ('PATCH', 'DELETE')

    @extend_schema(operation_id='performance_cycle_get', responses=dict)
    def get(self, request, cycle_id):
        return Response(services.cycle_detail(request.user, cycle_id))

    @extend_schema(operation_id='performance_cycle_patch', request=s.CycleIn, responses=dict)
    def patch(self, request, cycle_id):
        services.update_cycle(request.user, cycle_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_cycle_delete', responses=dict)
    def delete(self, request, cycle_id):
        services.delete_cycle(request.user, cycle_id)
        return Response(OK)


class CycleLaunchView(_HrView):
    @extend_schema(operation_id='performance_cycle_launch', request=None, responses=dict)
    def post(self, request, cycle_id):
        return Response({'ok': True, 'participants': services.launch_cycle(request.user, cycle_id)})


class CycleAdvanceView(_HrView):
    @extend_schema(operation_id='performance_cycle_advance', request=None, responses=dict)
    def post(self, request, cycle_id):
        return Response({'ok': True, 'status': services.advance_cycle(request.user, cycle_id)})


class CycleCalibrationView(_HrView):
    @extend_schema(operation_id='performance_cycle_calibration', responses=dict)
    def get(self, request, cycle_id):
        return Response(services.calibration(request.user, cycle_id))


# ---------------------------------------------------------------- reviews

class ReviewsView(_View):
    @extend_schema(operation_id='performance_reviews_list', responses=dict)
    def get(self, request):
        return Response({'reviews': services.list_reviews(request.user, request.query_params.get('scope') or 'mine')})


class ReviewView(_View):
    @extend_schema(operation_id='performance_review_get', responses=dict)
    def get(self, request, review_id):
        return Response(services.review_detail(request.user, review_id))


class ReviewSelfView(_View):
    @extend_schema(operation_id='performance_review_self', request=s.SelfReviewIn, responses=dict)
    def put(self, request, review_id):
        services.save_self(request.user, review_id, request.data)
        return Response(OK)


class ReviewManagerView(_View):
    @extend_schema(operation_id='performance_review_manager', request=s.ManagerReviewIn, responses=dict)
    def put(self, request, review_id):
        services.save_manager(request.user, review_id, request.data)
        return Response(OK)


class ReviewCalibrateView(_HrView):
    @extend_schema(operation_id='performance_review_calibrate', request=s.CalibrateIn, responses=dict)
    def post(self, request, review_id):
        services.calibrate(request.user, review_id, request.data)
        return Response(OK)


class ReviewAcknowledgeView(_View):
    @extend_schema(operation_id='performance_review_acknowledge', request=s.AcknowledgeIn, responses=dict)
    def post(self, request, review_id):
        services.acknowledge(request.user, review_id, request.data)
        return Response(OK)


class ReviewPeersView(_View):
    @extend_schema(operation_id='performance_review_peers', request=s.NominatePeersIn, responses={201: dict})
    def post(self, request, review_id):
        return _created({'ok': True, 'added': services.nominate_peers(request.user, review_id, request.data)})


class ReviewPrintView(_View):
    """A printable summary (HTML); the employee's copy leaves out the recommendation."""

    @extend_schema(operation_id='performance_review_print', responses={(200, 'text/html'): str})
    def get(self, request, review_id):
        return _html(services.review_print_html(request.user, review_id))


# ---------------------------------------------------------------- peers (360)

class PeerDecisionView(_View):
    @extend_schema(operation_id='performance_peer_decision', request=s.DecidePeerIn, responses=dict)
    def post(self, request, peer_id):
        services.decide_peer(request.user, peer_id, request.data)
        return Response(OK)


class PeerView(_View):
    @extend_schema(operation_id='performance_peer_submit', request=s.PeerResponseIn, responses=dict)
    def put(self, request, peer_id):
        services.submit_peer(request.user, peer_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_peer_remove', responses=dict)
    def delete(self, request, peer_id):
        services.remove_peer(request.user, peer_id)
        return Response(OK)


class PeerRequestsView(_View):
    @extend_schema(operation_id='performance_peer_requests', responses=dict)
    def get(self, request):
        return Response({'requests': services.peer_requests(request.user)})


# ---------------------------------------------------------------- manager's journal

class JournalView(_View):
    @extend_schema(operation_id='performance_journal_list', responses=dict)
    def get(self, request):
        return Response({'entries': services.journal(request.user, _query(request))})

    @extend_schema(operation_id='performance_journal_add', request=s.JournalIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.add_journal(request.user, request.data)})


class JournalEntryView(_View):
    @extend_schema(operation_id='performance_journal_delete', responses=dict)
    def delete(self, request, entry_id):
        services.delete_journal(request.user, entry_id)
        return Response(OK)


# ---------------------------------------------------------------- continuous feedback

class FeedbackView(_View):
    @extend_schema(operation_id='performance_feedback_list', responses=dict)
    def get(self, request):
        return Response({'feedback': services.feedback(request.user, _query(request))})

    @extend_schema(operation_id='performance_feedback_give', request=s.GiveFeedbackIn, responses={201: dict})
    def post(self, request):
        services.give_feedback(request.user, request.data)
        return _created(OK)


class FeedbackRequestsView(_View):
    @extend_schema(operation_id='performance_feedback_request', request=s.RequestFeedbackIn, responses={201: dict})
    def post(self, request):
        services.request_feedback(request.user, request.data)
        return _created(OK)


class FeedbackItemView(_View):
    @extend_schema(operation_id='performance_feedback_answer', request=s.AnswerFeedbackIn, responses=dict)
    def put(self, request, feedback_id):
        services.answer_feedback(request.user, feedback_id, request.data)
        return Response(OK)


class FeedbackDeclineView(_View):
    @extend_schema(operation_id='performance_feedback_decline', request=None, responses=dict)
    def post(self, request, feedback_id):
        services.decline_feedback(request.user, feedback_id)
        return Response(OK)


# ---------------------------------------------------------------- 1-on-1s

class MeetingsView(_View):
    @extend_schema(operation_id='performance_meetings_list', responses=dict)
    def get(self, request):
        return Response(services.meetings(request.user, request.query_params.get('with') or None))

    @extend_schema(operation_id='performance_meetings_create', request=s.MeetingIn, responses={201: dict})
    def post(self, request):
        return _created({'id': services.create_meeting(request.user, request.data)})


class MeetingView(_View):
    @extend_schema(operation_id='performance_meeting_get', responses=dict)
    def get(self, request, meeting_id):
        return Response(services.meeting_detail(request.user, meeting_id))

    @extend_schema(operation_id='performance_meeting_patch', request=s.MeetingPatch, responses=dict)
    def patch(self, request, meeting_id):
        return Response({'ok': True, 'nextId': services.update_meeting(request.user, meeting_id, request.data)})


class MeetingItemsView(_View):
    @extend_schema(operation_id='performance_meeting_item_add', request=s.MeetingItemIn, responses={201: dict})
    def post(self, request, meeting_id):
        return _created({'id': services.add_meeting_item(request.user, meeting_id, request.data)})


class MeetingItemView(_View):
    @extend_schema(operation_id='performance_meeting_item_patch', request=s.MeetingItemPatch, responses=dict)
    def patch(self, request, item_id):
        services.update_meeting_item(request.user, item_id, request.data)
        return Response(OK)

    @extend_schema(operation_id='performance_meeting_item_delete', responses=dict)
    def delete(self, request, item_id):
        services.delete_meeting_item(request.user, item_id)
        return Response(OK)
