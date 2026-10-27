from django.urls import path

from . import views

urlpatterns = [
    # pages (React routes, hubs.tsx MyClaimsHub and ExpensesHub)
    path('me/claims', views.my_claims, name='my_claims'),
    path('me/claims/travel', views.my_travel, name='my_travel'),
    path('expenses', views.claims_review, name='claims'),
    path('expenses/travel', views.travel_review, name='travel'),
    path('expenses/policies', views.policies, name='policies'),
    # claims
    path('_/expenses/claims/new', views.claim_new, name='claim_new'),
    path('_/expenses/claims/line', views.claim_line, name='claim_line'),
    path('_/expenses/claims/check', views.claim_check, name='claim_check'),
    path('_/expenses/claims/list', views.claims_list, name='claims_list'),
    path('_/expenses/claims/<uuid:id>', views.claim_detail, name='claim_detail'),
    path('_/expenses/claims/<uuid:id>/edit', views.claim_edit, name='claim_edit'),
    path('_/expenses/claims/<uuid:id>/submit', views.claim_submit, name='claim_submit'),
    path('_/expenses/claims/<uuid:id>/cancel', views.claim_cancel, name='claim_cancel'),
    path('_/expenses/claims/<uuid:id>/review', views.claim_review, name='claim_review'),
    path('_/expenses/claims/<uuid:id>/pay-outside', views.claim_pay_outside, name='claim_pay_outside'),
    path('_/expenses/claims/<uuid:id>/items/<uuid:item_id>/receipt', views.claim_receipt, name='claim_receipt'),
    # travel and settlements
    path('_/expenses/travel/new', views.travel_new, name='travel_new'),
    path('_/expenses/travel/quote', views.travel_quote, name='travel_quote'),
    path('_/expenses/travel/list', views.trips_list, name='trips_list'),
    path('_/expenses/travel/<uuid:id>', views.trip_detail, name='trip_detail'),
    path('_/expenses/travel/<uuid:id>/cancel', views.trip_cancel, name='trip_cancel'),
    path('_/expenses/travel/<uuid:id>/review', views.trip_review, name='trip_review'),
    path('_/expenses/travel/<uuid:id>/advance-outside', views.trip_advance_outside, name='trip_advance_outside'),
    path('_/expenses/travel/<uuid:id>/settlement', views.trip_settle, name='trip_settle'),
    path('_/expenses/travel/<uuid:id>/settlement/preview', views.settle_preview, name='settle_preview'),
    path('_/expenses/travel/bill-row', views.bill_row, name='bill_row'),
    path('_/expenses/settlements/<uuid:id>/review', views.settlement_review, name='settlement_review'),
    path('_/expenses/settlements/<uuid:id>/bills/<uuid:bill_id>/file', views.bill_file, name='bill_file'),
    # policies (HR)
    path('_/expenses/policy/settings', views.policy_settings, name='policy_settings'),
    path('_/expenses/categories', views.categories_partial, name='categories'),
    path('_/expenses/categories/new', views.category_form, name='category_new'),
    path('_/expenses/categories/<uuid:id>/edit', views.category_form, name='category_edit'),
    path('_/expenses/categories/limit-row', views.limit_row, name='limit_row'),
    path('_/expenses/tada', views.tada_rates, name='tada'),
    path('_/expenses/tada/grade-row', views.tada_grade_row, name='tada_grade_row'),
    # reports (HR): the tables on the pages, and CSV downloads
    path('_/expenses/reports/spend', views.report_spend, name='report_spend'),
    path('_/expenses/reports/advances.csv', views.report_advances_csv, name='report_advances_csv'),
    path('_/expenses/reports/pending-payment.csv', views.report_pending_csv, name='report_pending_csv'),
]
