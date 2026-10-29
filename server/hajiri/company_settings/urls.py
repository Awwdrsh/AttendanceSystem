"""TS /api/v1/settings (React app, Bearer clients). /api/v1/settings/features is the features app's, mounted before
this one in config/urls.py; the named paths come before /settings/<key>. Ids are <str:...> so a malformed one is the
TS 400 "Invalid UUID"."""
from django.urls import path

from . import api

urlpatterns = [
    path('api/v1/settings', api.SettingsView.as_view(), name='api_settings'),
    path('api/v1/settings/company', api.CompanyView.as_view(), name='api_company'),
    path('api/v1/settings/preview/attendance', api.PreviewAttendanceView.as_view(), name='api_preview_attendance'),
    path('api/v1/settings/branches', api.BranchesView.as_view(), name='api_branches'),
    path('api/v1/settings/branches/<str:id>', api.BranchView.as_view(), name='api_branch'),
    path('api/v1/settings/org-units', api.OrgUnitsView.as_view(), name='api_org_units'),
    path('api/v1/settings/org-units/<str:id>', api.OrgUnitView.as_view(), name='api_org_unit'),
    path('api/v1/settings/<str:key>', api.SettingView.as_view(), name='api_setting'),
]
