from django.urls import path

# JSON API for Bearer-token clients (TS: /api/v1/time/...).
from . import api

_API = 'api/v1/time/'
urlpatterns = [
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
