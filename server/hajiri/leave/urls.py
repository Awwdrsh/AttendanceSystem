from django.urls import path

from . import api

urlpatterns = [
    # JSON API (docs/api.md; TS paths), DRF views
    path('api/v1/leave/types', api.TypesView.as_view(), name='api_types'),
    path('api/v1/leave/types/order', api.TypesOrderView.as_view(), name='api_types_order'),
    path('api/v1/leave/types/<str:id>', api.TypeView.as_view(), name='api_type'),
    path('api/v1/leave/balances', api.BalancesView.as_view(), name='api_balances'),
    path('api/v1/leave/preview', api.PreviewView.as_view(), name='api_preview'),
    path('api/v1/leave/requests', api.RequestsView.as_view(), name='api_requests'),
    path('api/v1/leave/requests/<str:id>/cancel', api.CancelView.as_view(), name='api_cancel'),
    path('api/v1/leave/accrual/run', api.AccrualRunView.as_view(), name='api_accrual_run'),
    path('api/v1/leave/adjust', api.AdjustView.as_view(), name='api_adjust'),
    path('api/v1/leave/ledger', api.LedgerView.as_view(), name='api_ledger'),
    path('api/v1/leave/team-calendar', api.TeamCalendarView.as_view(), name='api_team_calendar'),
]
