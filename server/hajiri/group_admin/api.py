"""
JSON endpoints of group administration for Bearer clients (DRF, the project's authentication, permission and
exception classes). The services check the group roles themselves; the views only route.

    GET/POST   /api/v1/group/companies                 list / add a company
    GET/PATCH  /api/v1/group/companies/<id>            one company / change names and profile
    POST       /api/v1/group/companies/attach          {companyId}: bring a standalone company I administer in
    GET/POST   /api/v1/group/access[?companyId=]       who can work where / give or change access
    POST       /api/v1/group/access/invite             a new account whose home is a company of the group
    DELETE     /api/v1/group/access/<userId>/<companyId>
    GET/POST   /api/v1/group/transfers                 recent transfers / transfer an employee of my active company
    GET/PUT    /api/v1/group/transfer-rules            per company: carry leave balances of people transferred in
    POST       /api/v1/group/policies/preview|apply    copy policy templates between companies

and what the React forms offer:

    GET  /api/v1/group/companies/attachable            standalone companies I administer (to attach)
    GET  /api/v1/group/access/employees?companyId=&keep=   employees of a group company no account works as yet
    GET  /api/v1/group/transfers/targets               sister companies the active company's employees can move to
    GET  /api/v1/group/transfers/targets/<id>          that company's branches, departments, managers and shifts
    GET  /api/v1/group/policies/sections               the policy sections that can be copied

Every view authenticates like the React app's other JSON endpoints: the session cookie with the Origin check (no
CSRF token), or a Bearer token.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import IsSignedIn, SameOriginAuthentication
from hajiri.core.errors import BadRequestError
from hajiri.core.scoping import is_uuid

from . import serializers, services


class _View(APIView):
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn]


class CompaniesView(_View):

    @extend_schema(operation_id='group_companies_list', responses=dict)
    def get(self, request):
        return Response({'items': services.list_companies(request.user)})

    @extend_schema(request=serializers.CompanyIn, responses=dict)
    def post(self, request):
        return Response(services.create_company(request.user, request.data), status=201)


class CompanyView(_View):

    @extend_schema(operation_id='group_company_retrieve', responses=dict)
    def get(self, request, company_id):
        return Response(services.get_company(request.user, company_id))

    @extend_schema(request=serializers.CompanyPatchIn, responses=dict)
    def patch(self, request, company_id):
        return Response(services.update_company(request.user, company_id, request.data))


class AttachView(_View):

    @extend_schema(request=serializers.AttachIn, responses=dict)
    def post(self, request):
        return Response(services.attach_company(request.user, request.data))


class AccessView(_View):

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'items': services.list_access(request.user, request.query_params.get('companyId') or None)})

    @extend_schema(request=serializers.AccessIn, responses=dict)
    def post(self, request):
        return Response(services.grant_access(request.user, request.data))


class InviteView(_View):

    @extend_schema(request=serializers.InviteIn, responses=dict)
    def post(self, request):
        return Response(services.invite_user(request.user, request.data), status=201)


class AccessRevokeView(_View):

    @extend_schema(responses={204: None})
    def delete(self, request, user_id, company_id):
        services.revoke_access(request.user, user_id, company_id)
        return Response(status=204)


class TransfersView(_View):

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'items': services.list_transfers(request.user)})

    @extend_schema(request=serializers.TransferIn, responses=dict)
    def post(self, request):
        return Response(services.transfer_employee(request.user, request.data), status=201)


class TransferRulesView(_View):

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'items': services.transfer_rules(request.user)})

    @extend_schema(request=serializers.TransferRuleIn, responses=dict)
    def put(self, request):
        return Response(services.set_transfer_rule(request.user, request.data))


class PolicyPreviewView(_View):

    @extend_schema(request=serializers.PolicyCopyIn, responses=dict)
    def post(self, request):
        return Response(services.preview_policies(request.user, request.data))


class PolicyApplyView(_View):

    @extend_schema(request=serializers.PolicyCopyIn, responses=dict)
    def post(self, request):
        return Response(services.copy_policies(request.user, request.data))


class AttachableView(_View):
    @extend_schema(operation_id='group_companies_attachable', responses=dict)
    def get(self, request):
        return Response({'items': services.attachable_companies(request.user)})


class AccessEmployeesView(_View):
    @extend_schema(operation_id='group_access_employees', responses=dict)
    def get(self, request):
        cid = request.query_params.get('companyId') or ''
        if not cid:
            raise BadRequestError('companyId is required')
        keep = request.query_params.get('keep') or None
        if keep and not is_uuid(keep):
            keep = None
        return Response({'items': services.unlinked_employees(request.user, cid, keep)})


class TransferTargetsView(_View):
    @extend_schema(operation_id='group_transfer_targets', responses=dict)
    def get(self, request):
        return Response({'items': services.target_companies(request.user)})


class TransferTargetView(_View):
    @extend_schema(operation_id='group_transfer_target', responses=dict)
    def get(self, request, company_id):
        return Response(services.target_options(request.user, company_id))


class PolicySectionsView(_View):
    @extend_schema(operation_id='group_policy_sections', responses=dict)
    def get(self, request):
        services.group_scope(request.user)
        return Response({'items': [{'key': k, 'label': label} for k, label in services.SECTION_CHOICES]})
