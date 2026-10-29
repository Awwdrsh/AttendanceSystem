"""The TS /api/v1/settings routes (apps/api/src/modules/settings/routes.ts) for the React app and Bearer-token clients:
the header profile for everyone signed in; settings, the policy preview, branches and org units for HR and admin of
the ACTIVE company. The rules live in service.py; cookie requests are checked by Origin like the TS API."""
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication

from . import service


class _Api(APIView):
    authentication_classes = [SameOriginAuthentication]


class CompanyView(_Api):
    @extend_schema(operation_id='settings_company', responses=OpenApiTypes.OBJECT, summary='Company name, logo and today (AD and BS)')
    def get(self, request):
        return Response(service.company_header(request.user))


class SettingsView(_Api):
    @extend_schema(operation_id='settings_list', responses=OpenApiTypes.OBJECT, summary='Every company setting (HR)')
    def get(self, request):
        return Response(service.list_settings(request.user))


class SettingView(_Api):
    @extend_schema(operation_id='settings_put', request=OpenApiTypes.OBJECT, responses=OpenApiTypes.OBJECT, summary='Save one setting (HR)')
    def put(self, request, key):
        return Response(service.put_setting(request.user, key, request.data))


class PreviewAttendanceView(_Api):
    @extend_schema(operation_id='settings_preview_attendance', request=service.PreviewIn, responses=OpenApiTypes.OBJECT,
                   summary='What a proposed attendance policy would change (HR, no writes)')
    def post(self, request):
        return Response(service.preview_attendance(request.user, request.data))


class BranchesView(_Api):
    @extend_schema(operation_id='settings_branches_list', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'branches': service.list_branches(request.user)})

    @extend_schema(operation_id='settings_branches_create', request=service.BranchIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        return Response({'branch': service.create_branch(request.user, request.data)}, status=status.HTTP_201_CREATED)


class BranchView(_Api):
    @extend_schema(operation_id='settings_branches_update', request=service.BranchPatchIn, responses=OpenApiTypes.OBJECT)
    def patch(self, request, id):
        return Response({'branch': service.update_branch(request.user, id, request.data)})


class OrgUnitsView(_Api):
    @extend_schema(operation_id='settings_org_units_list', responses=OpenApiTypes.OBJECT)
    def get(self, request):
        return Response({'orgUnits': service.list_org_units(request.user)})

    @extend_schema(operation_id='settings_org_units_create', request=service.OrgUnitIn, responses={201: OpenApiTypes.OBJECT})
    def post(self, request):
        return Response({'orgUnit': service.create_org_unit(request.user, request.data)}, status=status.HTTP_201_CREATED)


class OrgUnitView(_Api):
    @extend_schema(operation_id='settings_org_units_update', request=service.OrgUnitPatchIn, responses=OpenApiTypes.OBJECT)
    def patch(self, request, id):
        return Response({'orgUnit': service.update_org_unit(request.user, id, request.data)})
