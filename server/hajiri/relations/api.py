"""
Employee relations JSON API: the TS routes under /api/v1/relations (apps/api/src/modules/relations/routes.ts), which
the React app calls with the cookie session (frontend/src/api/relations.js). Same paths, methods, bodies, JSON shapes
and status codes. DRF views with SameOriginAuthentication (the session cookie checked against the Origin, as the TS
sameOriginGuard did, or a Bearer token), IsSignedIn, the TS error shape and JsonRenderer. The TS module had no feature
switch. Routes the TS module guarded with requireRole('hr') add HasRole('hr') (the same 'requires role hr' answer the
services give). Rules and validation stay in services.py.
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.files.store import stored_file_response

from . import serializers as s
from . import services

AUTH = [SameOriginAuthentication]
HR = HasRole('hr')


class _View(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn]


class _HrView(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, HR]


def _body(request) -> dict:
    """The JSON body; a POST without one (React's post(path)) reads as {} like express.json() left it."""
    data = request.data
    return data if isinstance(data, dict) else {}


def _query(request) -> dict:
    return request.query_params.dict()


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


class _AnyAccept(DefaultContentNegotiation):
    """The file answer is not rendered, so whatever the browser accepts must not end in 406."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


# ================================================================ policy and overview

class PolicyView(_HrView):
    """GET /relations/policy -> {policy}; PUT -> {policy} (HR)."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.get_policy(request.user))

    @extend_schema(request=s.PolicyIn, responses=dict)
    def put(self, request):
        return Response(services.save_policy(request.user, _body(request)))


class SummaryView(_HrView):
    """GET /relations/summary -> {openGrievances, openCases, pendingDevices, openKye, isHr} (HR)."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.summary(request.user))


# ================================================================ grievances

class CommitteeView(_View):
    """GET /relations/grievances/committee -> {members, isMember}; PUT (HR) -> {ok, members}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.committee(request.user))

    @extend_schema(request=s.CommitteeIn, responses=dict)
    def put(self, request):
        return Response(services.set_committee(request.user, _body(request)))


class GrievancesView(_View):
    """GET /relations/grievances?scope=mine|handle&status= -> {grievances}; POST -> 201 {id, number}."""

    @extend_schema(operation_id='api_v1_relations_grievances_list', parameters=[s.GrievanceQueryIn], responses=dict)
    def get(self, request):
        return Response(services.list_grievances(request.user, _query(request)))

    @extend_schema(request=s.GrievanceIn, responses={201: dict})
    def post(self, request):
        return _created(services.file_grievance(request.user, _body(request)))


class GrievanceView(_View):
    """GET /relations/grievances/:id -> {grievance, notes, files, canAppeal, appealUntil}; PATCH -> {ok}."""

    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(services.grievance_detail(request.user, id))

    @extend_schema(request=s.GrievanceUpdateIn, responses=dict)
    def patch(self, request, id):
        return Response(services.update_grievance(request.user, id, _body(request)))


class GrievanceFileView(_View):
    """GET /relations/grievances/:id/files/:fileId[?download=1]: an attachment of a case the user may see."""
    content_negotiation_class = _AnyAccept

    @extend_schema(parameters=[OpenApiParameter('download', str, required=False)],
                   responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, id, file_id):
        f = services.grievance_file(request.user, id, file_id)
        return stored_file_response(f, download=request.query_params.get('download') == '1')


class GrievanceNotesView(_View):
    @extend_schema(request=s.NoteIn, responses={201: dict})
    def post(self, request, id):
        return _created(services.add_note(request.user, id, _body(request)))


class GrievanceDecisionView(_View):
    @extend_schema(request=s.GrievanceDecisionIn, responses=dict)
    def post(self, request, id):
        return Response(services.decide_grievance(request.user, id, _body(request)))


class GrievanceAppealView(_View):
    @extend_schema(request=s.AppealIn, responses=dict)
    def post(self, request, id):
        return Response(services.appeal_grievance(request.user, id, _body(request)))


class GrievanceAppealDecisionView(_HrView):
    @extend_schema(request=s.AppealDecisionIn, responses=dict)
    def post(self, request, id):
        return Response(services.decide_appeal(request.user, id, _body(request)))


class GrievanceWithdrawView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.withdraw_grievance(request.user, id))


class GrievanceCloseView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.close_grievance(request.user, id))


# ================================================================ disciplinary action

class StagesView(_View):
    """GET /relations/disciplinary/stages -> {stages} (seeded on first read); PUT (HR) -> {ok}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.list_stages(request.user))

    @extend_schema(request=s.StagesIn, responses=dict)
    def put(self, request):
        return Response(services.save_stages(request.user, _body(request)))


class CasesView(_HrView):
    """GET /relations/disciplinary?employeeId=&status= -> {cases}; POST -> 201 {id, number} (HR)."""

    @extend_schema(operation_id='api_v1_relations_disciplinary_list', parameters=[s.CasesQueryIn], responses=dict)
    def get(self, request):
        return Response(services.list_cases(request.user, _query(request)))

    @extend_schema(request=s.CaseIn, responses={201: dict})
    def post(self, request):
        return _created(services.open_case(request.user, _body(request)))


class MyCasesView(_View):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.my_cases(request.user))


class CaseView(_View):
    """GET /relations/disciplinary/:id -> {case}: HR, or the employee once asked to explain."""

    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(services.case_detail(request.user, id))


class CaseShowCauseView(_HrView):
    @extend_schema(request=s.ShowCauseIn, responses=dict)
    def post(self, request, id):
        return Response(services.show_cause(request.user, id, _body(request)))


class CaseRespondView(_View):
    @extend_schema(request=s.CaseResponseIn, responses=dict)
    def post(self, request, id):
        return Response(services.respond_case(request.user, id, _body(request)))


class CaseDecideView(_HrView):
    @extend_schema(request=s.CaseDecisionIn, responses=dict)
    def post(self, request, id):
        return Response(services.decide_case(request.user, id, _body(request)))


class CaseLetterView(_HrView):
    @extend_schema(request=s.LetterLinkIn, responses=dict)
    def post(self, request, id):
        return Response(services.link_letter(request.user, id, _body(request)))


class CaseWithdrawView(_HrView):
    @extend_schema(request=s.ReasonIn, responses=dict)
    def post(self, request, id):
        return Response(services.withdraw_case(request.user, id, _body(request)))


# ================================================================ milestones and contracts

class MilestonesView(_HrView):
    """GET /relations/milestones?days= -> {milestones, policy} (HR)."""

    @extend_schema(parameters=[s.MilestonesQueryIn], responses=dict)
    def get(self, request):
        return Response(services.list_milestones(request.user, _query(request)))


class SeparatingView(_HrView):
    """GET /relations/separating -> {people}: exits, contract ends and retirements in the next 180 days (HR)."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.separating(request.user))


class ContractView(_HrView):
    """GET /relations/employees/:id/contract -> {contractEnd, retirementDate, retirementAge}; PUT -> {ok} (HR)."""

    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(services.get_contract(request.user, id))

    @extend_schema(request=s.ContractIn, responses=dict)
    def put(self, request, id):
        return Response(services.set_contract(request.user, id, _body(request)))


# ================================================================ acting and deputation

class AssignmentsView(_View):
    """GET /relations/assignments?employeeId=&status= -> {assignments} (HR, or one's own); POST (HR) -> 201 {id}."""

    @extend_schema(parameters=[s.AssignmentsQueryIn], responses=dict)
    def get(self, request):
        return Response(services.list_assignments(request.user, _query(request)))

    @extend_schema(request=s.AssignmentIn, responses={201: dict})
    def post(self, request):
        return _created(services.add_assignment(request.user, _body(request)))


class AssignmentEndView(_HrView):
    @extend_schema(request=s.AssignmentEndIn, responses=dict)
    def post(self, request, id):
        return Response(services.end_assignment(request.user, id, _body(request)))


# ================================================================ Know Your Employee

class CampaignsView(_HrView):
    """GET /relations/kye/campaigns -> {campaigns}; POST -> 201 {id, employees} (HR)."""

    @extend_schema(operation_id='api_v1_relations_kye_campaigns_list', responses=dict)
    def get(self, request):
        return Response(services.list_campaigns(request.user))

    @extend_schema(request=s.CampaignIn, responses={201: dict})
    def post(self, request):
        return _created(services.launch_campaign(request.user, _body(request)))


class CampaignView(_HrView):
    """GET /relations/kye/campaigns/:id -> {campaign, responses} (HR)."""

    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(services.campaign_detail(request.user, id))


class CampaignNudgeView(_HrView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.nudge_campaign(request.user, id))


class CampaignCloseView(_HrView):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(services.close_campaign(request.user, id))


class CampaignRespondView(_View):
    @extend_schema(request=s.KyeResponseIn, responses=dict)
    def post(self, request, id):
        return Response(services.respond_kye(request.user, id, _body(request)))


class MyKyeView(_View):
    """GET /relations/kye/mine -> {campaigns, completeness, sectionLabels}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.my_kye(request.user))


class CompletenessView(_HrView):
    """GET /relations/completeness?employeeId= -> {employees, sectionLabels} (HR)."""

    @extend_schema(parameters=[s.EmployeeQueryIn], responses=dict)
    def get(self, request):
        return Response(services.completeness_report(request.user, _query(request)))


# ================================================================ sign-in pop-ups

class PopupsView(_View):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.popups(request.user))


class PopupDismissView(_View):
    @extend_schema(request=s.DismissIn, responses=dict)
    def post(self, request, id):
        return Response(services.dismiss_popup(request.user, id, _body(request)))


# ================================================================ registered devices

class MyDevicesView(_View):
    """GET /relations/devices/mine -> {devices, required}."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.my_devices(request.user))


class MyDeviceView(_View):
    """DELETE /relations/devices/mine/:id -> {ok}."""

    @extend_schema(responses=dict)
    def delete(self, request, id):
        return Response(services.remove_my_device(request.user, id))


class RegisterDeviceView(_View):
    """POST /relations/devices/register {token, label} -> 201 {id, status: 'pending'}."""

    @extend_schema(request=s.DeviceIn, responses={201: dict})
    def post(self, request):
        return _created(services.register_device(request.user, _body(request), request.headers.get('User-Agent')))


class DevicesView(_HrView):
    """GET /relations/devices?status= -> {devices} (HR)."""

    @extend_schema(parameters=[s.DevicesQueryIn], responses=dict)
    def get(self, request):
        return Response(services.list_devices(request.user, _query(request)))


class DeviceDecisionView(_HrView):
    """POST /relations/devices/:id/approve|reject|revoke -> {ok} (HR)."""

    @extend_schema(request=None, responses=dict)
    def post(self, request, id, action):
        return Response(services.decide_device(request.user, id, action))
