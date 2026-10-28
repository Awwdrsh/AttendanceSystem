"""
Approvals JSON API for Bearer-token clients (the TS routes under /api/v1/workflow): one-tap approvals from the mobile
app and the chain preview. DRF views with the project defaults (hajiri.core.api): HajiriAuthentication, IsSignedIn,
the TS error shape and JsonRenderer. Rules and scoping stay in service.py / flow_admin.py.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.validation import parse_uuid, plain, validate

from . import flow_admin, service
from .serializers import BulkApproveIn, BulkOut, DecisionIn, InboxOut, PreviewQueryIn, StateOut


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
