from django.urls import path

from . import api, views

urlpatterns = [
    # pages (React routes)
    path('me/leave', views.my_leave, name='mine'),
    path('leave', views.calendar, name='calendar'),
    path('leave/balances', views.balances, name='balances'),
    path('leave/policies', views.policies, name='policies'),
    # addresses from the earlier layout (App.tsx LEGACY)
    # fragments and form posts
    path('_/leave/apply', views.apply, name='apply'),
    path('_/leave/preview', views.preview, name='preview'),
    path('_/leave/balances/mine', views.my_balances, name='my_balances'),
    path('_/leave/requests/<uuid:id>', views.request_detail, name='request_detail'),
    path('_/leave/requests/<uuid:id>/cancel', views.cancel, name='cancel'),
    path('_/leave/adjust', views.adjust, name='adjust'),
    path('_/leave/accrual/run', views.accrual_run, name='accrual_run'),
    path('_/leave/types/new', views.type_new, name='type_new'),
    path('_/leave/types/<uuid:id>/edit', views.type_edit, name='type_edit'),
    path('_/leave/types/<uuid:id>/move', views.type_move, name='type_move'),
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
