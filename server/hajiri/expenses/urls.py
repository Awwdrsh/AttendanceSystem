from django.urls import path

from . import api

# JSON API for the React app (session cookie) and Bearer-token clients (TS: /api/v1/expenses/...). Ids are plain path
# segments so a malformed one is refused with the TS 400 'Invalid UUID' rather than a 404.
API = 'api/v1/expenses/'
urlpatterns = [
    path(API + 'policy', api.PolicyView.as_view(), name='api_policy'),
    path(API + 'policy/settings', api.PolicySettingsView.as_view(), name='api_policy_settings'),
    path(API + 'categories', api.CategoriesView.as_view(), name='api_categories'),
    path(API + 'categories/<str:id>', api.CategoryView.as_view(), name='api_category'),
    path(API + 'categories/<str:id>/limits', api.CategoryLimitsView.as_view(), name='api_category_limits'),
    path(API + 'tada-rates', api.TadaRatesView.as_view(), name='api_tada_rates'),
    path(API + 'claims/check', api.ClaimCheckView.as_view(), name='api_claim_check'),
    path(API + 'claims', api.ClaimsView.as_view(), name='api_claims'),
    path(API + 'claims/<str:id>', api.ClaimView.as_view(), name='api_claim'),
    path(API + 'claims/<str:id>/submit', api.ClaimSubmitView.as_view(), name='api_claim_submit'),
    path(API + 'claims/<str:id>/cancel', api.ClaimCancelView.as_view(), name='api_claim_cancel'),
    path(API + 'claims/<str:id>/review', api.ClaimReviewView.as_view(), name='api_claim_review'),
    path(API + 'claims/<str:id>/pay-outside', api.ClaimPayOutsideView.as_view(), name='api_claim_pay_outside'),
    path(API + 'claims/<str:id>/items/<str:item_id>/receipt', api.ClaimReceiptView.as_view(), name='api_claim_receipt'),
    path(API + 'travel/quote', api.TravelQuoteView.as_view(), name='api_travel_quote'),
    path(API + 'travel', api.TripsView.as_view(), name='api_trips'),
    path(API + 'travel/<str:id>', api.TripView.as_view(), name='api_trip'),
    path(API + 'travel/<str:id>/cancel', api.TripCancelView.as_view(), name='api_trip_cancel'),
    path(API + 'travel/<str:id>/review', api.TripReviewView.as_view(), name='api_trip_review'),
    path(API + 'travel/<str:id>/advance-outside', api.TripAdvanceOutsideView.as_view(), name='api_trip_advance_outside'),
    path(API + 'travel/<str:id>/settlement', api.TripSettleView.as_view(), name='api_trip_settle'),
    path(API + 'settlements/<str:id>/review', api.SettlementReviewView.as_view(), name='api_settlement_review'),
    path(API + 'settlements/<str:id>/bills/<str:bill_id>/file', api.BillFileView.as_view(), name='api_bill_file'),
    path(API + 'reports/spend', api.SpendReportView.as_view(), name='api_report_spend'),
    path(API + 'reports/advances', api.AdvancesReportView.as_view(), name='api_report_advances'),
    path(API + 'reports/pending-payment', api.PendingPaymentReportView.as_view(), name='api_report_pending_payment'),
]
