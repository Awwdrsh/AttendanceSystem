from django.urls import path

from . import views

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
    # JSON API (docs/api.md; TS paths)
    path('api/v1/leave/types', views.api_types, name='api_types'),
    path('api/v1/leave/types/order', views.api_types_order, name='api_types_order'),
    path('api/v1/leave/types/<str:id>', views.api_type, name='api_type'),
    path('api/v1/leave/balances', views.api_balances, name='api_balances'),
    path('api/v1/leave/preview', views.api_preview, name='api_preview'),
    path('api/v1/leave/requests', views.api_requests, name='api_requests'),
    path('api/v1/leave/requests/<str:id>/cancel', views.api_cancel, name='api_cancel'),
    path('api/v1/leave/accrual/run', views.api_accrual_run, name='api_accrual_run'),
    path('api/v1/leave/adjust', views.api_adjust, name='api_adjust'),
    path('api/v1/leave/ledger', views.api_ledger, name='api_ledger'),
    path('api/v1/leave/team-calendar', views.api_team_calendar, name='api_team_calendar'),
]
