"""Group pages. Mounted before hajiri.web in config/urls.py, so /group here takes over from the foundation's overview
in hajiri.web.group (whose group administration fragments and JSON endpoints stay there)."""
from django.urls import path

from . import api, views

app_name = 'group'

urlpatterns = [
    path('group', views.overview, name='overview'),
    path('group/people', views.people, name='people'),
    path('group/reports', views.reports, name='reports'),
    path('_/group/pay-policy', views.pay_policy, name='pay_policy'),
    path('api/v1/group/dashboard', api.DashboardView.as_view(), name='api_dashboard'),
    path('api/v1/group/reports/<str:kind>', api.ReportView.as_view(), name='api_report'),
]
