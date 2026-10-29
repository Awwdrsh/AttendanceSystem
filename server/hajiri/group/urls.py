"""Group-wide JSON (dashboard, reports, the company switcher). Mounted before hajiri.web in config/urls.py, so the
paths it shares with hajiri.web.group are answered here."""
from django.urls import path

from . import api

app_name = 'group'

urlpatterns = [
    # JSON for the React app. /api/v1/me/companies(/switch) and /api/v1/group/roles take over hajiri.web.group's
    # (same fields) with the Origin check the React app's cookie requests need instead of a CSRF token.
    path('api/v1/me/companies', api.MyCompaniesView.as_view(), name='api_my_companies'),
    path('api/v1/me/companies/switch', api.SwitchCompanyView.as_view(), name='api_switch_company'),
    path('api/v1/group/roles', api.GroupRolesView.as_view(), name='api_group_roles'),
    path('api/v1/group/roles/<uuid:user_id>', api.GroupRoleRevokeView.as_view(), name='api_group_role_revoke'),
    path('api/v1/group/pay-policy', api.PayPolicyView.as_view(), name='api_pay_policy'),
    path('api/v1/group/dashboard', api.DashboardView.as_view(), name='api_dashboard'),
    path('api/v1/group/people', api.PeopleView.as_view(), name='api_people'),
    path('api/v1/group/reports', api.ReportCatalogView.as_view(), name='api_reports'),
    path('api/v1/group/reports/<str:kind>', api.ReportView.as_view(), name='api_report'),
]
