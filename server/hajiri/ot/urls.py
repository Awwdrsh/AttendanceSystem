from django.urls import path

from . import api, views

urlpatterns = [
    # pages (React routes); /hr/ot-slips redirects here from hajiri.web
    path('attendance/overtime', views.overtime, name='overtime'),
    # fragments and form posts
    path('_/ot/register', views.register_csv, name='register_csv'),
    path('_/ot/slips/<uuid:id>/approve', views.slip_approve, name='slip_approve'),
    path('_/ot/slips/<uuid:id>/reject', views.slip_reject, name='slip_reject'),
    path('_/ot/slips/<uuid:id>/comp-off', views.slip_comp_off, name='slip_comp_off'),
    path('_/ot/requests', views.requests_list, name='requests'),
    path('_/ot/requests/new', views.request_new, name='request_new'),
    path('_/ot/requests/<uuid:id>/cancel', views.request_cancel, name='request_cancel'),
    # JSON API (TS paths; DRF views in api.py)
    path('api/v1/ot/requests', api.RequestsView.as_view(), name='api_requests'),
    path('api/v1/ot/requests/<str:id>/cancel', api.RequestCancelView.as_view(), name='api_request_cancel'),
    path('api/v1/ot/slips', api.SlipsView.as_view(), name='api_slips'),
    path('api/v1/ot/slips/<str:id>/approve', api.SlipApproveView.as_view(), name='api_slip_approve'),
    path('api/v1/ot/slips/<str:id>/reject', api.SlipRejectView.as_view(), name='api_slip_reject'),
    path('api/v1/ot/slips/<str:id>/comp-off', api.SlipCompOffView.as_view(), name='api_slip_comp_off'),
    path('api/v1/ot/register', api.RegisterView.as_view(), name='api_register'),
]
