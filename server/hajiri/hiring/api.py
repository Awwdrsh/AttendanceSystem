"""
Hiring JSON API for the React app and Bearer-token clients: the TS routes under /api/v1/hiring (routes.ts), with the
same paths, bodies, answers and status codes. DRF views with the project defaults (hajiri.core.api:
HajiriAuthentication, IsSignedIn, the TS error shape, JsonRenderer) plus the hiring feature switch (the TS
featureGate). The services check roles, company and manager scope themselves (with the TS messages) and validate the
bodies, so the views stay thin. Cookie requests are checked against the Origin (SameOriginAuthentication), not a CSRF
token, because the React app never sends one. Ids are any path segment: the services answer a malformed one with 400 'Invalid UUID'.
"""
from __future__ import annotations

from django.http import HttpResponse, JsonResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import ApiEncoder, FeatureEnabled, IsSignedIn, SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import services

OK = {'ok': True}


def _query(request) -> dict:
    return request.query_params.dict()


class HiringView(APIView):
    # The React app sends the session cookie with fetch() and no CSRF token (as against the TS API): the cookie is
    # checked against the Origin like the TS sameOriginGuard; Bearer clients need no check.
    authentication_classes = [SameOriginAuthentication]
    permission_classes = [IsSignedIn, FeatureEnabled('hiring')]


# ---------------------------------------------------------------- requisitions

class RequisitionsView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_requisitions_list', responses=dict)
    def get(self, request):
        return Response({'requisitions': services.list_requisitions(request.user)})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response({'id': services.create_requisition(request.user, request.data)}, status=status.HTTP_201_CREATED)


class RequisitionCancelView(HiringView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        services.cancel_requisition(request.user, id)
        return Response(OK)


# ---------------------------------------------------------------- jobs

class JobsView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_jobs_list', responses=dict)
    def get(self, request):
        return Response(services.list_jobs(request.user, request.query_params.get('status')))

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request):
        return Response({'id': services.create_job(request.user, request.data)}, status=status.HTTP_201_CREATED)


class JobView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_job_retrieve', responses=dict)
    def get(self, request, id):
        return Response(services.get_job(request.user, id))

    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        services.update_job(request.user, id, request.data)
        return Response(OK)


class JobPublishView(HiringView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        services.publish_job(request.user, id)
        return Response(OK)


class JobCloseView(HiringView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        services.close_job(request.user, id)
        return Response(OK)


class JobApplicationsView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_job_applications_list', responses=dict)
    def get(self, request, id):
        d = services.board(request.user, id, request.query_params.get('include'))
        return Response({'applications': d['applications'], 'stages': d['stages']})

    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        return Response({'id': services.add_candidate(request.user, id, request.data)}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- applications

class ApplicationView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_application_retrieve', responses=dict)
    def get(self, request, id):
        return Response(services.application_detail(request.user, id))

    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        services.update_application(request.user, id, request.data)
        return Response(OK)


class ApplicationCvView(HiringView):
    @extend_schema(responses={(200, 'application/pdf'): bytes})
    def get(self, request, id):
        return stored_file_response(services.cv_file(request.user, id), download=request.query_params.get('download') == '1')


class ApplicationMoveView(HiringView):
    @extend_schema(request=dict, responses=dict)
    def post(self, request, id):
        services.move_application(request.user, id, request.data)
        return Response(OK)


class ApplicationRejectView(HiringView):
    @extend_schema(request=dict, responses=dict)
    def post(self, request, id):
        return Response({'ok': True, 'emailed': bool(services.reject_application(request.user, id, request.data))})


class ApplicationReopenView(HiringView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        services.reopen_application(request.user, id)
        return Response(OK)


class ApplicationNotesView(HiringView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        services.add_note(request.user, id, request.data)
        return Response(OK, status=status.HTTP_201_CREATED)


class ApplicationExportView(HiringView):
    @extend_schema(responses=dict)
    def get(self, request, id) -> HttpResponse:
        data = services.export_candidate(request.user, id)
        cand_id = data.pop('_candidate_id')
        res = JsonResponse(data, encoder=ApiEncoder, json_dumps_params={'ensure_ascii': False},
                           content_type='application/json; charset=utf-8')
        res['Content-Disposition'] = f'attachment; filename="candidate-{cand_id}.json"'
        return res


class ApplicationInterviewsView(HiringView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        return Response({'id': services.schedule_interview(request.user, id, request.data)}, status=status.HTTP_201_CREATED)


class ApplicationOffersView(HiringView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        return Response({'id': services.create_offer(request.user, id, request.data)}, status=status.HTTP_201_CREATED)


class ApplicationHireView(HiringView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        return Response({'employeeId': services.hire(request.user, id, request.data)}, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- interviews

class InterviewsView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_interviews_list', responses=dict)
    def get(self, request):
        return Response({'interviews': services.list_interviews(request.user, _query(request))})


class InterviewView(HiringView):
    @extend_schema(request=dict, responses=dict)
    def patch(self, request, id):
        services.update_interview(request.user, id, request.data)
        return Response(OK)


class InterviewScorecardView(HiringView):
    @extend_schema(request=dict, responses={201: dict})
    def post(self, request, id):
        services.submit_scorecard(request.user, id, request.data)
        return Response(OK, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------- offers

class OfferLetterView(HiringView):
    @extend_schema(responses={(200, 'text/html'): str})
    def get(self, request, id) -> HttpResponse:
        return HttpResponse(services.offer_letter(request.user, id), content_type='text/html; charset=utf-8')


class OfferRespondView(HiringView):
    @extend_schema(request=dict, responses=dict)
    def post(self, request, id):
        services.respond_offer(request.user, id, request.data)
        return Response(OK)


class OfferWithdrawView(HiringView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        services.withdraw_offer(request.user, id)
        return Response(OK)


# ---------------------------------------------------------------- lookups, reports, policy

class LookupsView(HiringView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.lookups(request.user))


class ReportsView(HiringView):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.reports(request.user, _query(request)))


class PolicyView(HiringView):
    @extend_schema(operation_id='api_v1_hiring_policy_retrieve', responses=dict)
    def get(self, request):
        return Response({'policy': services.policy(request.user)})

    @extend_schema(request=dict, responses=dict)
    def put(self, request):
        return Response({'policy': services.set_policy(request.user, request.data)})
