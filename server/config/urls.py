"""URLs of the web process. Every app mounts its JSON endpoints at the site root with the TS paths (/api/v1/...).
Every other address is the React app (frontend/, built into server/frontend_build/): hajiri.web.views.spa answers
its index.html and React Router shows the page, as the TS API served apps/web/dist. WhiteNoise serves the build's
files (/assets/*, sw.js, manifest.webmanifest, icons) and /static/ before any of this runs. The device gateway has
its own URLconf (config/gateway_urls.py)."""
from django.urls import include, path, re_path

from hajiri.web.views import spa

urlpatterns = [
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
    # The React app: every address outside /api/ and /static/ (TS: app.get(/^\/(?!api\/|health).*/, index.html)).
    # A clock-in device pointed at the web port by mistake gets a 404, not the app's HTML (its routes live on the
    # gateway process, config/gateway_urls.py).
    re_path(r'^(?!api/|static/|health$|(?i:iclock|hikvision)/).*$', spa, name='spa'),
]

handler400 = 'config.errors.bad_request'
handler403 = 'config.errors.forbidden'
handler404 = 'config.errors.not_found'
handler500 = 'config.errors.server_error'
