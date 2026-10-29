"""
Expenses and travel JSON API (the TS routes under /api/v1/expenses) for the React app and Bearer-token clients. DRF
views with SameOriginAuthentication (the session cookie the React app sends with fetch(), checked against the Origin
as the TS sameOriginGuard did, or a Bearer token), IsSignedIn, the TS error shape and JsonRenderer. Each view adds
the feature switch and, where the TS route did, the HR role. Rules and validation stay in services.py, so the answers
are the TS route's.
"""
from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.response import Response
from rest_framework.views import APIView

from hajiri.core.api import FeatureEnabled, HasRole, IsSignedIn, SameOriginAuthentication
from hajiri.core.validation import validate
from hajiri.files.store import stored_file_response
from hajiri.web.csv import csv_response

from . import serializers as s
from . import services as ex

FEATURE = FeatureEnabled('expenses')
HR = HasRole('hr')
AUTH = [SameOriginAuthentication]


class _View(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, FEATURE]


class _HrView(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, FEATURE, HR]


def _created(data) -> Response:
    return Response(data, status=status.HTTP_201_CREATED)


class ReportNegotiation(DefaultContentNegotiation):
    """The reports read ?format=json|csv themselves (sendReport), so DRF must not treat `format` as a renderer choice."""

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class _ReportView(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn, FEATURE, HR]
    content_negotiation_class = ReportNegotiation


def _report_format(request) -> str | None:
    """The TS routes parsed ?format with z.enum(['json', 'csv']).optional(): anything else is a 400 validation."""
    return validate(s.ReportFormatIn, request.query_params.dict()).get('format')


def _report(request, r: dict):
    """?format=csv downloads the report; otherwise {columns, rows} (sendReport in reports/csv.ts)."""
    if request.query_params.get('format') == 'csv':
        return csv_response(r['filename'], r['columns'], r['rows'])
    return Response({'columns': r['columns'], 'rows': r['rows']})


# ---------------------------------------------------------------- policy

class PolicyView(_View):
    @extend_schema(responses=dict)
    def get(self, request):
        return Response(ex.get_policy(request.user))


class PolicySettingsView(_HrView):
    @extend_schema(request=s.SettingsIn, responses=dict)
    def put(self, request):
        return Response(ex.save_settings(request.user, request.data))


class CategoriesView(_HrView):
    @extend_schema(request=s.CategoryIn, responses={201: dict})
    def post(self, request):
        return _created(ex.create_category(request.user, request.data))


class CategoryView(_HrView):
    @extend_schema(request=s.CategoryPatchIn, responses=dict)
    def patch(self, request, id):
        return Response(ex.update_category(request.user, id, request.data))


class CategoryLimitsView(_HrView):
    @extend_schema(request=s.LimitsIn, responses=dict)
    def put(self, request, id):
        return Response(ex.set_limits(request.user, id, request.data))


class TadaRatesView(_HrView):
    @extend_schema(request=s.TadaRatesIn, responses=dict)
    def put(self, request):
        return Response(ex.set_tada_rates(request.user, request.data))


# ---------------------------------------------------------------- claims

class ClaimCheckView(_View):
    @extend_schema(request=s.CheckIn, responses=dict)
    def post(self, request):
        return Response(ex.check_items(request.user, request.data))


class ClaimsView(_View):
    @extend_schema(operation_id='api_v1_expenses_claims_list', parameters=[s.ClaimsQueryIn], responses=dict)
    def get(self, request):
        return Response(ex.list_claims(request.user, request.query_params.dict()))

    @extend_schema(request=s.ClaimIn, responses={201: dict})
    def post(self, request):
        return _created(ex.create_claim(request.user, request.data))


class ClaimView(_View):
    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(ex.claim_detail(request.user, id))

    @extend_schema(request=s.ClaimIn, responses=dict)
    def put(self, request, id):
        return Response(ex.update_claim(request.user, id, request.data))


class ClaimSubmitView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(ex.submit_claim(request.user, id))


class ClaimCancelView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(ex.cancel_claim(request.user, id))


class ClaimReviewView(_View):
    @extend_schema(request=s.ClaimReviewIn, responses=dict)
    def post(self, request, id):
        return Response(ex.review_claim(request.user, id, request.data))


class ClaimPayOutsideView(_HrView):
    @extend_schema(request=s.NoteIn, responses=dict)
    def post(self, request, id):
        return Response(ex.pay_claim_outside(request.user, id, request.data))


class ClaimReceiptView(_View):
    @extend_schema(responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, id, item_id):
        return stored_file_response(ex.claim_receipt(request.user, id, item_id),
                                    download=request.query_params.get('download') == '1')


# ---------------------------------------------------------------- travel

class TravelQuoteView(_View):
    @extend_schema(request=s.QuoteIn, responses=dict)
    def post(self, request):
        return Response(ex.travel_quote(request.user, request.data))


class TripsView(_View):
    @extend_schema(operation_id='api_v1_expenses_travel_list', parameters=[s.TripsQueryIn], responses=dict)
    def get(self, request):
        return Response(ex.list_trips(request.user, request.query_params.dict()))

    @extend_schema(request=s.TravelIn, responses={201: dict})
    def post(self, request):
        return _created(ex.request_travel(request.user, request.data))


class TripView(_View):
    @extend_schema(responses=dict)
    def get(self, request, id):
        return Response(ex.trip_detail(request.user, id))


class TripCancelView(_View):
    @extend_schema(request=None, responses=dict)
    def post(self, request, id):
        return Response(ex.cancel_trip(request.user, id))


class TripReviewView(_View):
    @extend_schema(request=s.TripReviewIn, responses=dict)
    def post(self, request, id):
        return Response(ex.review_trip(request.user, id, request.data))


class TripAdvanceOutsideView(_HrView):
    @extend_schema(request=s.NoteIn, responses=dict)
    def post(self, request, id):
        return Response(ex.pay_advance_outside(request.user, id, request.data))


class TripSettleView(_View):
    @extend_schema(request=s.SettleIn, responses={201: dict})
    def post(self, request, id):
        return _created(ex.settle_trip(request.user, id, request.data))


class SettlementReviewView(_View):
    @extend_schema(request=s.SettlementReviewIn, responses=dict)
    def post(self, request, id):
        return Response(ex.review_settlement(request.user, id, request.data))


class BillFileView(_View):
    @extend_schema(responses={(200, 'application/octet-stream'): bytes})
    def get(self, request, id, bill_id):
        return stored_file_response(ex.bill_file(request.user, id, bill_id), download=request.query_params.get('download') == '1')


# ---------------------------------------------------------------- reports (HR)

class SpendReportView(_ReportView):
    @extend_schema(parameters=[s.SpendQueryIn], responses=dict)
    def get(self, request):
        return _report(request, ex.report_spend(request.user, request.query_params.dict()))


class AdvancesReportView(_ReportView):
    @extend_schema(responses=dict)
    def get(self, request):
        _report_format(request)
        return _report(request, ex.report_advances(request.user))


class PendingPaymentReportView(_ReportView):
    @extend_schema(responses=dict)
    def get(self, request):
        _report_format(request)
        return _report(request, ex.report_pending_payment(request.user))
