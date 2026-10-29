"""
People JSON API (the routes of apps/api/src/modules/people/routes.ts under /api/v1/people), used by the React app and
Bearer-token clients: directory, summary, org chart and structure for everyone; structure edits, policy, profile
edits, career events and change requests for HR; the employee's own change requests and photo. DRF views with the
project defaults (hajiri.core.api); every rule, role check and validation stays in service.py, so the pages and the
API answer the same way. Ids are parsed by the service (a malformed id is the TS 400 "Invalid UUID"); ids of another
company are not found.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import service as svc
from .serializers import AvatarIn, BranchIn, ChangeIn, DesignationIn, DirectoryQuery, PolicyIn, UnitIn

OK = {'ok': True}
_REVEAL = OpenApiParameter('reveal', str, required=False, description="'1' shows encrypted numbers in full (HR, audited)")


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


class ApiView(APIView):
    """The session cookie with the TS Origin check (the React app sends no CSRF token) or a Bearer token."""
    authentication_classes = [SameOriginAuthentication]


# ---------------------------------------------------------------- everyone

class DirectoryView(ApiView):
    @extend_schema(operation_id='people_directory', parameters=[DirectoryQuery], responses=dict)
    def get(self, request):
        """Active people: ?search=&orgUnitId=&branchId=."""
        return Response({'people': svc.directory(request.user, request.query_params.dict())})


class SummaryView(ApiView):
    @extend_schema(operation_id='people_summary', responses=dict)
    def get(self, request):
        return Response(svc.summary(request.user))


class OrgChartView(ApiView):
    @extend_schema(operation_id='people_org_chart', responses=dict)
    def get(self, request):
        return Response({'nodes': svc.org_chart(request.user)})


class StructureView(ApiView):
    @extend_schema(operation_id='people_structure', responses=dict)
    def get(self, request):
        return Response(svc.structure(request.user))


# ---------------------------------------------------------------- org structure (HR)

class BranchesView(ApiView):
    @extend_schema(operation_id='people_branch_create', request=BranchIn, responses={201: dict})
    def post(self, request):
        return _created({'branch': svc.create_branch(request.user, request.data)})


class BranchView(ApiView):
    @extend_schema(operation_id='people_branch_update', request=BranchIn, responses=dict)
    def patch(self, request, id):
        return Response({'branch': svc.update_branch(request.user, id, request.data)})

    @extend_schema(operation_id='people_branch_delete', responses=dict)
    def delete(self, request, id):
        svc.delete_branch(request.user, id)
        return Response(OK)


class UnitsView(ApiView):
    @extend_schema(operation_id='people_unit_create', request=UnitIn, responses={201: dict})
    def post(self, request):
        return _created({'unit': svc.create_unit(request.user, request.data)})


class UnitView(ApiView):
    @extend_schema(operation_id='people_unit_update', request=UnitIn, responses=dict)
    def patch(self, request, id):
        return Response({'unit': svc.update_unit(request.user, id, request.data)})

    @extend_schema(operation_id='people_unit_delete', responses=dict)
    def delete(self, request, id):
        svc.delete_unit(request.user, id)
        return Response(OK)


class DesignationsView(ApiView):
    @extend_schema(operation_id='people_designation_create', request=DesignationIn, responses={201: dict})
    def post(self, request):
        return _created({'designation': svc.create_designation(request.user, request.data)})


class DesignationView(ApiView):
    @extend_schema(operation_id='people_designation_update', request=DesignationIn, responses=dict)
    def patch(self, request, id):
        return Response({'designation': svc.update_designation(request.user, id, request.data)})

    @extend_schema(operation_id='people_designation_delete', responses=dict)
    def delete(self, request, id):
        svc.delete_designation(request.user, id)
        return Response(OK)


class PolicyView(ApiView):
    @extend_schema(operation_id='people_policy_get', responses=dict)
    def get(self, request):
        return Response({'policy': svc.get_policy(request.user)})

    @extend_schema(operation_id='people_policy_put', request=PolicyIn, responses=dict)
    def put(self, request):
        return Response({'policy': svc.set_policy(request.user, request.data)})


# ---------------------------------------------------------------- my change requests and photo

class MyChangeRequestsView(ApiView):
    @extend_schema(operation_id='people_my_requests', responses=dict)
    def get(self, request):
        return Response({'requests': svc.my_requests(request.user)})

    @extend_schema(operation_id='people_request_change', request=ChangeIn, responses={201: dict})
    def post(self, request):
        return _created({'request': svc.request_change(request.user, request.data)})


class MyChangeRequestCancelView(ApiView):
    @extend_schema(operation_id='people_my_request_cancel', request=None, responses=dict)
    def post(self, request, id):
        svc.cancel_my_request(request.user, id)
        return Response(OK)


class MyAvatarView(ApiView):
    @extend_schema(operation_id='people_my_avatar_set', request=AvatarIn, responses=dict)
    def post(self, request):
        svc.set_my_avatar(request.user, request.data)
        return Response(OK)

    @extend_schema(operation_id='people_my_avatar_remove', responses=dict)
    def delete(self, request):
        svc.set_my_avatar(request.user, None)
        return Response(OK)


# ---------------------------------------------------------------- change requests (HR; the employee may read their own)

class ChangeRequestsView(ApiView):
    @extend_schema(operation_id='people_requests', parameters=[OpenApiParameter('status', str, required=False)], responses=dict)
    def get(self, request):
        """?status=pending|approved|rejected|cancelled|all (default pending)."""
        return Response({'requests': svc.change_requests(request.user, request.query_params.get('status') or 'pending')})


class ChangeRequestView(ApiView):
    @extend_schema(operation_id='people_request', parameters=[_REVEAL], responses=dict)
    def get(self, request, id):
        return Response(svc.request_detail(request.user, id, reveal=request.query_params.get('reveal') == '1'))


class ChangeRequestProofView(ApiView):
    @extend_schema(operation_id='people_request_proof', parameters=[OpenApiParameter('download', str, required=False)],
                   responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, id):
        return stored_file_response(svc.request_proof(request.user, id), download=request.query_params.get('download') == '1')


# ---------------------------------------------------------------- one person

class CardView(ApiView):
    @extend_schema(operation_id='people_card', responses=dict)
    def get(self, request, id):
        return Response(svc.card(request.user, id))


class AvatarView(ApiView):
    @extend_schema(operation_id='people_avatar', responses={(200, 'image/*'): bytes})
    def get(self, request, id):
        resp = stored_file_response(svc.avatar_file(request.user, id))
        resp['Cache-Control'] = 'private, max-age=300'
        return resp

    @extend_schema(operation_id='people_avatar_set', request=AvatarIn, responses=dict)
    def post(self, request, id):
        svc.set_employee_avatar(request.user, id, request.data)
        return Response(OK)

    @extend_schema(operation_id='people_avatar_remove', responses=dict)
    def delete(self, request, id):
        svc.set_employee_avatar(request.user, id, None)
        return Response(OK)


class ProfileView(ApiView):
    @extend_schema(operation_id='people_profile', parameters=[_REVEAL], responses=dict)
    def get(self, request, id):
        return Response(svc.profile(request.user, id, reveal=request.query_params.get('reveal') == '1'))


class ProfileSectionView(ApiView):
    @extend_schema(operation_id='people_profile_section', request=dict, responses={200: dict, 202: dict})
    def put(self, request, id, section):
        """HR edits one section; with maker-checker on, bank and ID edits wait for a second HR person (202)."""
        out = svc.save_section(request.user, id, section, request.data)
        return Response(out, status=status.HTTP_202_ACCEPTED if out.get('pending') else status.HTTP_200_OK)


class HistoryView(ApiView):
    @extend_schema(operation_id='people_history', responses=dict)
    def get(self, request, id):
        # employee_field_history.id is a bigint, which the TS API (node-postgres) sent as a string.
        return Response({'history': [{**h, 'id': str(h['id'])} for h in svc.history(request.user, id)]})


class CareerView(ApiView):
    @extend_schema(operation_id='people_career', responses=dict)
    def get(self, request, id):
        return Response({'events': svc.career(request.user, id)})

    @extend_schema(operation_id='people_career_record', request=dict, responses={201: dict})
    def post(self, request, id):
        return _created({'event': svc.record_career(request.user, id, request.data)})


class CareerCancelView(ApiView):
    @extend_schema(operation_id='people_career_cancel', request=None, responses=dict)
    def post(self, request, id, event_id):
        svc.cancel_career(request.user, id, event_id)
        return Response(OK)
