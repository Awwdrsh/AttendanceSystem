from django.urls import path

from . import views

urlpatterns = [
    # pages (React routes)
    path('me/timesheet', views.my_timesheet, name='mine'),
    path('attendance/timesheets', views.review, name='review'),
    path('attendance/projects', views.projects, name='projects'),
    # timer (TimerCard) and the Home timer
    path('_/timesheets/timer', views.timer, name='timer'),
    path('_/timesheets/timer/start', views.timer_start, name='timer_start'),
    path('_/timesheets/timer/stop', views.timer_stop, name='timer_stop'),
    path('_/timesheets/timer/resume', views.timer_resume, name='timer_resume'),
    path('_/timesheets/home-timer', views.home_timer, name='home_timer'),
    path('_/timesheets/activity-options', views.activity_options, name='activity_options'),
    # my week
    path('_/timesheets/week', views.week_partial, name='week'),
    path('_/timesheets/entries/new', views.entry_new, name='entry_new'),
    path('_/timesheets/entries/<uuid:id>/edit', views.entry_edit, name='entry_edit'),
    path('_/timesheets/entries/<uuid:id>/delete', views.entry_delete, name='entry_delete'),
    path('_/timesheets/submit', views.submit, name='submit'),
    path('_/timesheets/<uuid:id>/withdraw', views.withdraw, name='withdraw'),
    # review
    path('_/timesheets/report.csv', views.report_csv, name='report_csv'),
    path('_/timesheets/<uuid:id>', views.detail, name='detail'),
    path('_/timesheets/<uuid:id>/decide', views.decide, name='decide'),
    # catalogue
    path('_/timesheets/projects/new', views.project_new, name='project_new'),
    path('_/timesheets/projects/<uuid:id>/edit', views.project_edit, name='project_edit'),
    path('_/timesheets/clients/new', views.client_new, name='client_new'),
    path('_/timesheets/clients/<uuid:id>/archive', views.client_archive, name='client_archive'),
    path('_/timesheets/activities/new', views.activity_new, name='activity_new'),
    path('_/timesheets/activities/<uuid:id>/archive', views.activity_archive, name='activity_archive'),
    # Settings, Time tracking tab (embedded by the settings page)
    path('_/timesheets/settings', views.settings_card, name='settings'),
]

# JSON API for Bearer-token clients (TS: /api/v1/time/...).
from . import api  # noqa: E402

_API = 'api/v1/time/'
urlpatterns += [
    path(f'{_API}settings', api.SettingsView.as_view(), name='api_settings'),
    path(f'{_API}clients', api.ClientsView.as_view(), name='api_clients'),
    path(f'{_API}clients/<str:pk>', api.ClientView.as_view(), name='api_client'),
    path(f'{_API}projects', api.ProjectsView.as_view(), name='api_projects'),
    path(f'{_API}projects/<str:pk>', api.ProjectView.as_view(), name='api_project'),
    path(f'{_API}activities', api.ActivitiesView.as_view(), name='api_activities'),
    path(f'{_API}activities/<str:pk>', api.ActivityView.as_view(), name='api_activity'),
    path(f'{_API}timer', api.TimerView.as_view(), name='api_timer'),
    path(f'{_API}timer/start', api.TimerStartView.as_view(), name='api_timer_start'),
    path(f'{_API}timer/resume', api.TimerResumeView.as_view(), name='api_timer_resume'),
    path(f'{_API}timer/stop', api.TimerStopView.as_view(), name='api_timer_stop'),
    path(f'{_API}entries', api.EntriesView.as_view(), name='api_entries'),
    path(f'{_API}entries/<str:pk>', api.EntryView.as_view(), name='api_entry'),
    path(f'{_API}week', api.WeekView.as_view(), name='api_week'),
    path(f'{_API}timesheets', api.TimesheetsView.as_view(), name='api_timesheets'),
    path(f'{_API}timesheets/<str:pk>', api.TimesheetView.as_view(), name='api_timesheet'),
    path(f'{_API}timesheets/<str:pk>/withdraw', api.TimesheetWithdrawView.as_view(), name='api_timesheet_withdraw'),
    path(f'{_API}team-week', api.TeamWeekView.as_view(), name='api_team_week'),
    path(f'{_API}reports/summary', api.ReportSummaryView.as_view(), name='api_report_summary'),
    path(f'{_API}reports/entries', api.ReportEntriesView.as_view(), name='api_report_entries'),
]
