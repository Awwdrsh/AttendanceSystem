"""Every app mounts at the site root using the same paths as the React app, so links and bookmarks keep working.
Partial (HTMX) endpoints live under /_/<app>/. The device gateway has its own URLconf (config/gateway_urls.py)."""
from django.urls import include, path

urlpatterns = [
    # Groups of companies: mounted first so their pages take over /group from hajiri.web.group once they exist.
    path('', include('hajiri.group.urls')),
    path('', include('hajiri.group_admin.urls')),
    path('', include(('hajiri.web.urls', 'web'))),
    path('', include(('hajiri.files.urls', 'files'))),
    path('', include(('hajiri.notifications.urls', 'notifications'))),
    path('', include(('hajiri.features.urls', 'features'))),
    path('', include(('hajiri.company_settings.urls', 'company_settings'))),
    path('', include(('hajiri.reports.urls', 'reports'))),
    path('', include(('hajiri.attendance.urls', 'attendance'))),
    path('', include(('hajiri.worker.urls', 'worker'))),
    path('', include(('hajiri.integrations.urls', 'integrations'))),
    path('', include(('hajiri.workflow.urls', 'workflow'))),
    path('', include(('hajiri.devices.urls', 'devices'))),
    path('', include(('hajiri.dashboard.urls', 'dashboard'))),
    path('', include(('hajiri.leave.urls', 'leave'))),
    path('', include(('hajiri.people.urls', 'people'))),
    path('', include(('hajiri.employees.urls', 'employees'))),
    path('', include(('hajiri.lifecycle.urls', 'lifecycle'))),
    path('', include(('hajiri.shifts.urls', 'shifts'))),
    path('', include(('hajiri.regularisation.urls', 'regularisation'))),
    path('', include(('hajiri.documents.urls', 'documents'))),
    path('', include(('hajiri.hiring.urls', 'hiring'))),
    path('', include(('hajiri.careers.urls', 'careers'))),
    path('', include(('hajiri.tasks.urls', 'tasks'))),
    path('', include(('hajiri.timesheets.urls', 'timesheets'))),
    path('', include(('hajiri.helpdesk.urls', 'helpdesk'))),
    path('', include(('hajiri.engage.urls', 'engage'))),
    path('', include(('hajiri.expenses.urls', 'expenses'))),
    path('', include(('hajiri.performance.urls', 'performance'))),
    path('', include(('hajiri.learning.urls', 'learning'))),
    path('', include(('hajiri.insights.urls', 'insights'))),
    path('', include(('hajiri.relations.urls', 'relations'))),
    path('', include(('hajiri.ot.urls', 'ot'))),
    path('', include(('hajiri.attendance_tools.urls', 'attendance_tools'))),
    path('', include(('hajiri.punch.urls', 'punch'))),
    path('', include(('hajiri.field.urls', 'field'))),
    path('', include(('hajiri.kiosk.urls', 'kiosk'))),
    path('', include(('hajiri.payroll.urls', 'payroll'))),
    path('', include(('hajiri.home.urls', 'home'))),
]

handler404 = 'config.errors.not_found'
