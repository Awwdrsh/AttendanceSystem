"""TS /api/v1/reports (React app, Bearer clients). The People and builder paths come before /reports/<name>. Ids are
<str:...> so a malformed one is the TS 400 "Invalid UUID"."""
from django.urls import path

from . import api

urlpatterns = [
    path('api/v1/reports/people/<str:name>', api.PeopleReportView.as_view(), name='api_people'),
    path('api/v1/reports/adhoc/datasets', api.DatasetsView.as_view(), name='api_adhoc_datasets'),
    path('api/v1/reports/adhoc/run', api.AdhocRunView.as_view(), name='api_adhoc_run'),
    path('api/v1/reports/adhoc/saved', api.SavedReportsView.as_view(), name='api_saved'),
    path('api/v1/reports/adhoc/saved/<str:id>', api.SavedReportView.as_view(), name='api_saved_one'),
    path('api/v1/reports/adhoc/saved/<str:id>/run', api.SavedReportRunView.as_view(), name='api_saved_run'),
    path('api/v1/reports/<str:name>', api.AttendanceReportView.as_view(), name='api_attendance'),
]
