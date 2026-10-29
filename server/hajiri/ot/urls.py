from django.urls import path

from . import api

urlpatterns = [
    # JSON API (TS paths; DRF views in api.py)
    path('api/v1/ot/requests', api.RequestsView.as_view(), name='api_requests'),
    path('api/v1/ot/requests/<str:id>/cancel', api.RequestCancelView.as_view(), name='api_request_cancel'),
    path('api/v1/ot/slips', api.SlipsView.as_view(), name='api_slips'),
    path('api/v1/ot/slips/<str:id>/approve', api.SlipApproveView.as_view(), name='api_slip_approve'),
    path('api/v1/ot/slips/<str:id>/reject', api.SlipRejectView.as_view(), name='api_slip_reject'),
    path('api/v1/ot/slips/<str:id>/comp-off', api.SlipCompOffView.as_view(), name='api_slip_comp_off'),
    path('api/v1/ot/register', api.RegisterView.as_view(), name='api_register'),
]
