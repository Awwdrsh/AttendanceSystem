"""
Shifts JSON API for Bearer-token clients and integrations: the TS routes under /api/v1/shifts (routes.ts), with the
same paths, bodies, answers and status codes. DRF views with the project defaults (hajiri.core.api:
HajiriAuthentication, IsSignedIn, the TS error shape, JsonRenderer). The services check roles, company and manager
scope themselves (with the TS messages) and validate the bodies, so the views stay thin.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from hajiri.core.api import IsSignedIn

from . import services
from .serializers import (
    AssignIn, CalendarQueryIn, CopyWeekIn, HolidayIn, HolidayQueryIn, ImportDefaultIn, LocationIn, PublishIn,
    RosterDeleteIn, RosterPutIn, RosterQueryIn, ShiftIn,
)

UUID = '[0-9a-fA-F-]{36}'


def _query(request) -> dict:
    return request.query_params.dict()


class ShiftViewSet(viewsets.ViewSet):
    """/api/v1/shifts[/<id>]: everyone reads the company's shifts; attendance admins and HR change them."""
    permission_classes = [IsSignedIn]
    lookup_value_regex = UUID

    @extend_schema(operation_id='api_v1_shifts_list', responses=dict)
    def list(self, request):
        return Response({'shifts': services.shifts(request.user)})

    @extend_schema(request=ShiftIn, responses={201: dict})
    def create(self, request):
        return Response(services.create_shift(request.user, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(responses=dict)
    def retrieve(self, request, pk=None):
        return Response({'shift': services.get_shift(request.user, pk)})

    @extend_schema(request=ShiftIn(partial=True), responses=dict)
    def partial_update(self, request, pk=None):
        return Response(services.update_shift(request.user, pk, request.data))

    @extend_schema(request=None, responses=dict)
    def destroy(self, request, pk=None):
        return Response(services.delete_shift(request.user, pk))

    @extend_schema(request=AssignIn, responses={201: dict})
    @action(detail=False, methods=['post'])
    def assign(self, request):
        return Response(services.assign_shift(request.user, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(parameters=[CalendarQueryIn], responses=dict)
    @action(detail=False)
    def calendar(self, request):
        return Response(services.calendar(request.user, _query(request)))


class HolidayViewSet(viewsets.ViewSet):
    """/api/v1/shifts/holidays[/<id>|/import-default]."""
    permission_classes = [IsSignedIn]
    lookup_value_regex = UUID

    @extend_schema(parameters=[HolidayQueryIn], responses=dict)
    def list(self, request):
        return Response({'holidays': services.holidays(request.user, _query(request))})

    @extend_schema(request=HolidayIn, responses={201: dict})
    def create(self, request):
        return Response(services.create_holiday(request.user, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(request=HolidayIn(partial=True), responses=dict)
    def partial_update(self, request, pk=None):
        return Response(services.update_holiday(request.user, pk, request.data))

    @extend_schema(request=None, responses=dict)
    def destroy(self, request, pk=None):
        return Response(services.delete_holiday(request.user, pk))

    @extend_schema(request=ImportDefaultIn, responses=dict)
    @action(detail=False, methods=['post'], url_path='import-default')
    def import_default(self, request):
        return Response(services.import_default_holidays(request.user, request.data))


class LocationViewSet(viewsets.ViewSet):
    """/api/v1/shifts/locations[/<id>]: the mobile clock-in geofences."""
    permission_classes = [IsSignedIn]
    lookup_value_regex = UUID

    @extend_schema(responses=dict)
    def list(self, request):
        return Response({'locations': services.locations(request.user)})

    @extend_schema(request=LocationIn, responses={201: dict})
    def create(self, request):
        return Response(services.create_location(request.user, request.data), status=status.HTTP_201_CREATED)

    @extend_schema(request=LocationIn(partial=True), responses=dict)
    def partial_update(self, request, pk=None):
        return Response(services.update_location(request.user, pk, request.data))

    @extend_schema(request=None, responses=dict)
    def destroy(self, request, pk=None):
        return Response(services.delete_location(request.user, pk))


class RosterViewSet(viewsets.ViewSet):
    """/api/v1/shifts/roster: GET the grid, PUT cells, DELETE a cell; /copy-week and /publish."""
    permission_classes = [IsSignedIn]

    @extend_schema(parameters=[RosterQueryIn], responses=dict)
    def list(self, request):
        return Response(services.roster(request.user, _query(request)))

    @extend_schema(request=RosterPutIn, responses=dict)
    def save(self, request):
        return Response(services.save_roster(request.user, request.data))

    @extend_schema(request=RosterDeleteIn, responses=dict)
    def clear(self, request):
        return Response(services.clear_roster(request.user, request.data))

    @extend_schema(request=CopyWeekIn, responses=dict)
    @action(detail=False, methods=['post'], url_path='copy-week')
    def copy_week(self, request):
        return Response(services.copy_week(request.user, request.data))

    @extend_schema(request=PublishIn, responses=dict)
    @action(detail=False, methods=['post'])
    def publish(self, request):
        return Response(services.publish_roster(request.user, request.data))
