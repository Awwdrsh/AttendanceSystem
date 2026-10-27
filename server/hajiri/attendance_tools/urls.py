from django.urls import path

from . import api, views

urlpatterns = [
    # Attendance hub pages (hubs.tsx AttendanceHub) and My attendance
    path('attendance', views.today, name='today'),
    path('attendance/exceptions', views.exceptions, name='exceptions'),
    path('attendance/issues', views.issues, name='issues'),
    path('attendance/penalties', views.penalties, name='penalties'),
    path('attendance/upload', views.upload, name='upload'),
    path('me/attendance', views.my_attendance, name='my_attendance'),
    # Fragments and form posts
    path('_/attendance_tools/recompute', views.recompute, name='recompute'),
    path('_/attendance_tools/nudge', views.nudge, name='nudge'),
    path('_/attendance_tools/policy', views.save_policy, name='policy'),
    path('_/attendance_tools/penalties/apply', views.apply_penalties, name='apply'),
    path('_/attendance_tools/penalties/<uuid:id>/reverse', views.reverse_penalty, name='reverse'),
    path('_/attendance_tools/upload/template', views.upload_template, name='upload_template'),
    path('_/attendance_tools/upload/check', views.upload_check, name='upload_check'),
    path('_/attendance_tools/upload/import', views.upload_import, name='upload_import'),
    path('_/attendance_tools/day', views.my_day, name='my_day'),
    path('_/attendance_tools/whos-out', views.whos_out, name='whos_out'),
    path('_/attendance_tools/late-note', views.late_note, name='late_note'),
    path('_/attendance_tools/late-blocked', views.late_blocked, name='late_blocked'),
    path('_/attendance_tools/payroll-check', views.payroll_check, name='payroll_check'),
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
