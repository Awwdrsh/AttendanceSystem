from django.urls import path

from . import views

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
    # JSON API (TS paths)
    path('api/v1/ot/requests', views.api_requests, name='api_requests'),
    path('api/v1/ot/requests/<str:id>/cancel', views.api_request_cancel, name='api_request_cancel'),
    path('api/v1/ot/slips', views.api_slips, name='api_slips'),
    path('api/v1/ot/slips/<str:id>/approve', views.api_slip_approve, name='api_slip_approve'),
    path('api/v1/ot/slips/<str:id>/reject', views.api_slip_reject, name='api_slip_reject'),
    path('api/v1/ot/slips/<str:id>/comp-off', views.api_slip_comp_off, name='api_slip_comp_off'),
    path('api/v1/ot/register', views.api_register, name='api_register'),
]
