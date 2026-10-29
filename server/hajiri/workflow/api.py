"""
Approvals JSON API (the TS routes under /api/v1/workflow) for the React app (cookie session) and Bearer clients:
the inbox and one-tap decisions, the chain preview, and the HR set-up of flows, user groups, delegations and the
verify report (flow-routes.ts). DRF views with the project defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn,
the TS error shape and JsonRenderer. Rules and scoping stay in service.py / flow_admin.py.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.validation import parse_uuid, plain, validate

from . import flow_admin, service
from .serializers import (BulkApproveIn, BulkOut, DecisionIn, DefaultStepsIn, DelegateIn, FlowIn, FlowPatchIn, GroupIn,
                          GroupPatchIn, InboxOut, PreviewQueryIn, StateOut)


def _body(request):
    """The JSON body as the services take it (TS `req.body`, {} when absent)."""
    return request.data if request.data is not None else {}


class InboxView(APIView):
    """GET /api/v1/workflow/inbox: everything the caller can decide now, in the active company."""

    @extend_schema(responses=InboxOut)
    def get(self, request):
        return Response({'items': service.inbox_for(request.user)})


class PreviewView(APIView):
    """GET /api/v1/workflow/preview?objectType=&employeeId=&amount=: who will approve, step by step."""

    @extend_schema(parameters=[PreviewQueryIn], responses=dict)
    def get(self, request):
        return Response(flow_admin.preview(request.user, request.query_params.dict()))


class BulkApproveView(APIView):
    """POST /api/v1/workflow/bulk-approve {ids}: each id commits or fails on its own."""

    @extend_schema(request=BulkApproveIn, responses=BulkOut)
    def post(self, request):
        b = plain(validate(BulkApproveIn, request.data))
        return Response({'results': service.bulk_approve(request.user, b['ids'])})


class DetailView(APIView):
    """GET /api/v1/workflow/<id>: the approval, its summary and the signed timeline (only for people who may read it)."""

    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(service.detail(request.user, parse_uuid(id)))


class DecideView(APIView):
    """POST /api/v1/workflow/<id>/approve|reject {reason?, approvedMin?, onBehalfOf?}."""
    decision = 'approve'

    @extend_schema(request=DecisionIn, responses=StateOut)
    def post(self, request, id):
        iid = parse_uuid(id)
        b = plain(validate(DecisionIn, request.data or {}))
        return Response({'state': service.decide(request.user, iid, self.decision, reason=b.get('reason'),
                                                 on_behalf_of=b.get('onBehalfOf'), approved_min=b.get('approvedMin'))})


class ApproveView(DecideView):
    decision = 'approve'


class RejectView(DecideView):
    decision = 'reject'


# ---------------------------------------------------------------- flow set-up (HR; the services check the role)

class FlowsConfigView(APIView):
    """GET /api/v1/workflow/flows/config: every request type with its default chain and flows, and the user groups."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(flow_admin.flows_config(request.user))


class FlowDefaultView(APIView):
    """PUT /api/v1/workflow/flows/defaults/<type> {steps} -> {objectType, steps}."""

    @extend_schema(request=DefaultStepsIn, responses=dict)
    def put(self, request, object_type):
        return Response(flow_admin.set_default(request.user, object_type, _body(request)))


class FlowsView(APIView):
    """POST /api/v1/workflow/flows {objectType, name, conditions?, steps, priority?, active?} -> 201 {id}."""

    @extend_schema(request=FlowIn, responses={201: dict})
    def post(self, request):
        return Response(flow_admin.create_flow(request.user, _body(request)), status=201)


class FlowView(APIView):
    """PATCH | DELETE /api/v1/workflow/flows/<id> -> {ok: true}."""

    @extend_schema(request=FlowPatchIn, responses=dict)
    def patch(self, request, id):
        return Response(flow_admin.update_flow(request.user, id, _body(request)))

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(flow_admin.delete_flow(request.user, id))


class GroupsView(APIView):
    """POST /api/v1/workflow/groups {name, description?, userIds?} -> 201 {id}."""

    @extend_schema(request=GroupIn, responses={201: dict})
    def post(self, request):
        return Response(flow_admin.create_group(request.user, _body(request)), status=201)


class GroupView(APIView):
    """PATCH | DELETE /api/v1/workflow/groups/<id> -> {ok: true}."""

    @extend_schema(request=GroupPatchIn, responses=dict)
    def patch(self, request, id):
        return Response(flow_admin.update_group(request.user, id, _body(request)))

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(flow_admin.delete_group(request.user, id))


class VerifyView(APIView):
    """GET /api/v1/workflow/verify?objectType=: employees whose chain reaches nobody, with a fix -> {checked, problems}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(flow_admin.verify(request.user, request.query_params.dict()))


# ---------------------------------------------------------------- delegation (anyone for themselves, HR for others)

class DelegatesView(APIView):
    """GET /api/v1/workflow/delegates?all=1 -> {delegates}; POST {userId?, delegateId, from, to, note?} -> 201 {id}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'delegates': flow_admin.list_delegates(request.user, request.query_params.get('all') == '1')})

    @extend_schema(request=DelegateIn, responses={201: dict})
    def post(self, request):
        return Response(flow_admin.add_delegate(request.user, _body(request)), status=201)


class DelegateCandidatesView(APIView):
    """GET /api/v1/workflow/delegates/candidates -> {users: [{id, name}]}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response({'users': flow_admin.delegate_candidates(request.user)})


class DelegateView(APIView):
    """DELETE /api/v1/workflow/delegates/<id>: cancel (the owner or HR; anyone else gets 404) -> {ok: true}."""

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(flow_admin.cancel_delegate(request.user, id))
