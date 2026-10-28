"""
Time tracking JSON API for Bearer-token clients (the TS routes under /api/v1/time, featureGate 'time'). DRF views with
the project defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn, the TS error shape and JsonRenderer. The
views are thin: rules, roles (requireModule('attendance') for the catalogue and settings) and validation live in
services.py, and everything works in the caller's active company.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, IsSignedIn
from hajiri.web.csv import csv_response

from . import services
from .serializers import (
    ActivityIn, ActivityPatchIn, ActivityQueryIn, ClientIn, ClientPatchIn, EntriesQueryIn, EntryIn, ProjectIn,
    ReportQueryIn, StopIn, SubmitIn, TeamWeekQueryIn, TimePolicyIn, TimerIn, TimesheetsQueryIn, WeekQueryIn,
)


def _query(request) -> dict:
    return request.query_params.dict()


def _all(request) -> bool:
    return request.query_params.get('all') == '1'


class TimeView(APIView):
    permission_classes = [IsSignedIn, FeatureEnabled('time')]


class SettingsView(TimeView):
    """GET /api/v1/time/settings (everyone reads the policy), PUT (the attendance area)."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.get_settings(request.user))

    @extend_schema(request=TimePolicyIn, responses=dict)
    def put(self, request):
        return Response(services.save_settings(request.user, request.data))


class ClientsView(TimeView):
    @extend_schema(operation_id='time_clients_list', responses=dict)
    def get(self, request):
        return Response({'clients': services.list_clients(request.user, _all(request))})

    @extend_schema(request=ClientIn, responses={201: dict})
    def post(self, request):
        return Response({'client': services.create_client(request.user, request.data)}, status=status.HTTP_201_CREATED)


class ClientView(TimeView):
    @extend_schema(request=ClientPatchIn, responses=dict)
    def patch(self, request, pk):
        return Response({'client': services.update_client(request.user, pk, request.data)})


class ProjectsView(TimeView):
    @extend_schema(operation_id='time_projects_list', responses=dict)
    def get(self, request):
        return Response({'projects': services.list_projects(request.user, _all(request))})

    @extend_schema(request=ProjectIn, responses={201: dict})
    def post(self, request):
        return Response({'id': services.create_project(request.user, request.data)}, status=status.HTTP_201_CREATED)


class ProjectView(TimeView):
    @extend_schema(request=ProjectIn, responses=dict)
    def patch(self, request, pk):
        services.update_project(request.user, pk, request.data)
        return Response({'ok': True})


class ActivitiesView(TimeView):
    @extend_schema(operation_id='time_activities_list', parameters=[ActivityQueryIn], responses=dict)
    def get(self, request):
        return Response({'activities': services.list_activities(request.user, _query(request))})

    @extend_schema(request=ActivityIn, responses={201: dict})
    def post(self, request):
        return Response({'activity': services.create_activity(request.user, request.data)}, status=status.HTTP_201_CREATED)


class ActivityView(TimeView):
    @extend_schema(request=ActivityPatchIn, responses=dict)
    def patch(self, request, pk):
        return Response({'activity': services.update_activity(request.user, pk, request.data)})


class TimerView(TimeView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.timer_state(request.user))


class TimerStartView(TimeView):
    @extend_schema(request=TimerIn, responses={201: dict})
    def post(self, request):
        return Response({'running': services.start_timer(request.user, request.data)}, status=status.HTTP_201_CREATED)


class TimerResumeView(TimeView):
    @extend_schema(request=None, responses={201: dict})
    def post(self, request):
        return Response({'running': services.resume_timer(request.user)}, status=status.HTTP_201_CREATED)


class TimerStopView(TimeView):
    @extend_schema(request=StopIn, responses=dict)
    def post(self, request):
        return Response({'entry': services.stop_timer(request.user, request.data)})


class EntriesView(TimeView):
    @extend_schema(operation_id='time_entries_list', parameters=[EntriesQueryIn], responses=dict)
    def get(self, request):
        return Response({'entries': services.list_entries(request.user, _query(request))})

    @extend_schema(request=EntryIn, responses={201: dict})
    def post(self, request):
        return Response({'entry': services.add_entry(request.user, request.data)}, status=status.HTTP_201_CREATED)


class EntryView(TimeView):
    @extend_schema(request=EntryIn, responses=dict)
    def patch(self, request, pk):
        return Response({'entry': services.update_entry(request.user, pk, request.data)})

    @extend_schema(responses=dict)
    def delete(self, request, pk):
        services.delete_entry(request.user, pk)
        return Response({'ok': True})


class WeekView(TimeView):
    @extend_schema(parameters=[WeekQueryIn], responses=dict)
    def get(self, request):
        return Response(services.week(request.user, _query(request)))


class TimesheetsView(TimeView):
    @extend_schema(operation_id='time_timesheets_list', parameters=[TimesheetsQueryIn], responses=dict)
    def get(self, request):
        return Response({'timesheets': services.list_timesheets(request.user, _query(request))})

    @extend_schema(request=SubmitIn, responses={201: dict})
    def post(self, request):
        return Response({'id': services.submit_timesheet(request.user, request.data)}, status=status.HTTP_201_CREATED)


class TimesheetView(TimeView):
    @extend_schema(responses=dict)
    def get(self, request, pk):
        return Response(services.timesheet_detail(request.user, pk))


class TimesheetWithdrawView(TimeView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, pk):
        services.withdraw_timesheet(request.user, pk)
        return Response({'ok': True})


class TeamWeekView(TimeView):
    @extend_schema(parameters=[TeamWeekQueryIn], responses=dict)
    def get(self, request):
        return Response(services.team_week(request.user, _query(request)))


class ReportView(TimeView):
    """Reports answer JSON, or a CSV download with ?format=csv (the TS query parameter, which DRF would otherwise read
    as a renderer choice and refuse)."""

    def perform_content_negotiation(self, request, force=False):
        return super().perform_content_negotiation(request, force=True)


class ReportSummaryView(ReportView):
    @extend_schema(parameters=[ReportQueryIn], responses=dict)
    def get(self, request):
        r = services.report_summary(request.user, _query(request))
        if r['format'] == 'csv':
            return csv_response(r['filename'], r['columns'], r['rows'])
        return Response({k: r[k] for k in ('columns', 'rows', 'totals', 'rounding')})


class ReportEntriesView(ReportView):
    @extend_schema(parameters=[ReportQueryIn], responses=dict)
    def get(self, request):
        r = services.report_entries(request.user, _query(request))
        if r['format'] == 'csv':
            return csv_response(r['filename'], r['columns'], r['rows'])
        return Response({'columns': r['columns'], 'rows': r['rows']})
