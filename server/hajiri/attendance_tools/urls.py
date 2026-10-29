from django.urls import path

from . import api

urlpatterns = [
    # JSON (TS paths)
    path('api/v1/attendance-tools/policy', api.policy, name='api_policy'),
    path('api/v1/attendance-tools/penalties', api.penalties, name='api_penalties'),
    path('api/v1/attendance-tools/penalties/me', api.penalties_me, name='api_penalties_me'),
    path('api/v1/attendance-tools/penalties/apply', api.penalties_apply, name='api_penalties_apply'),
    path('api/v1/attendance-tools/penalties/<str:id>/reverse', api.penalty_reverse, name='api_penalty_reverse'),
    path('api/v1/attendance-tools/issues', api.issues, name='api_issues'),
    path('api/v1/attendance-tools/issues/summary', api.issues_summary, name='api_issues_summary'),
    path('api/v1/attendance-tools/issues/nudge', api.nudge, name='api_nudge'),
    path('api/v1/attendance-tools/upload/template', api.upload_template, name='api_upload_template'),
    path('api/v1/attendance-tools/upload/check', api.upload_check, name='api_upload_check'),
    path('api/v1/attendance-tools/upload/import', api.upload_import, name='api_upload_import'),
    path('api/v1/attendance-tools/uploads', api.uploads, name='api_uploads'),
    path('api/v1/attendance-tools/whos-out', api.whos_out, name='api_whos_out'),
]
