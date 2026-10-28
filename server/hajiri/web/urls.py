from django.urls import path, re_path
from django.views.generic import RedirectView

from hajiri.group import views as group_views

from . import api_docs, group, views

# Addresses from the earlier layout keep working (App.tsx LEGACY).
LEGACY = [
    ('hr/dashboard', '/overview'), ('hr/employees', '/employees'), ('manager/approvals', '/approvals'),
    ('manager/team', '/attendance'), ('hr/exceptions', '/attendance/exceptions'), ('hr/ot-slips', '/attendance/overtime'),
    ('manager/fix-punch', '/attendance/fix'), ('manager/calendar', '/leave'), ('hr/requests', '/leave/history'),
    ('hr/leave-adjust', '/leave/balances'), ('hr/leave-types', '/leave/policies'), ('hr/reports', '/reports'),
    ('hr/settings', '/settings'), ('hr/shifts', '/settings/shifts'), ('hr/holidays', '/settings/holidays'),
    ('hr/devices', '/settings/devices'), ('hr/users', '/settings/users'), ('hr/setup', '/settings/setup'),
    ('me/data', '/me/profile/privacy'),
]

urlpatterns = [
    path('login', views.login_view, name='login'),
    path('login/2fa', views.login_two_factor, name='login_2fa'),
    path('logout', views.logout_view, name='logout'),
    path('change-password', views.change_password_view, name='change_password'),
    path('setup-two-step', views.two_step_setup_page, name='setup_two_step'),
    path('me/profile/security', views.security_page, name='security'),

    path('_/web/me', views.me_view, name='me'),

    # Groups of companies: switcher, group overview and group administration.
    # /group itself is hajiri.group's overview (mounted first); the name stays for {% url 'web:group' %}.
    path('group', group_views.overview, name='group'),
    path('_/web/company/switch', group.switch_company, name='switch_company'),
    path('_/web/group/companies/new', group.group_company_new, name='group_company_new'),
    path('_/web/group/access/new', group.group_access_new, name='group_access_new'),
    path('_/web/group/access', group.group_members, name='group_members'),
    path('_/web/group/access/<uuid:user_id>/revoke', group.group_access_revoke, name='group_access_revoke'),
    path('_/web/company/default', group.default_company, name='default_company'),
    path('_/web/group/roles/new', group.group_role_new, name='group_role_new'),
    path('_/web/group/roles', group.group_roles, name='group_roles'),
    path('_/web/group/roles/<uuid:user_id>/revoke', group.group_role_revoke, name='group_role_revoke'),
    path('api/v1/group/roles', group.api_group_roles, name='api_group_roles'),
    path('api/v1/group/roles/<uuid:user_id>', group.api_group_role_revoke, name='api_group_role_revoke'),
    path('api/v1/me/companies', group.api_my_companies, name='api_my_companies'),
    path('api/v1/me/companies/switch', group.api_switch_company, name='api_switch_company'),
    path('api/v1/group/overview', group.api_group_overview, name='api_group_overview'),

    # OpenAPI schema and docs of the JSON API (admins).
    path('api/schema', api_docs.SchemaView.as_view(), name='api_schema'),
    path('api/docs', api_docs.DocsView.as_view(url_name='web:api_schema'), name='api_docs'),
    path('_/web/2fa/setup', views.two_factor_setup, name='2fa_setup'),
    path('_/web/2fa/enable', views.two_factor_enable, name='2fa_enable'),
    path('_/web/2fa/recovery-codes', views.two_factor_recovery_codes, name='2fa_recovery_codes'),
    path('_/web/2fa/disable', views.two_factor_disable, name='2fa_disable'),
    path('_/web/sessions/revoke-others', views.sessions_revoke_others, name='sessions_revoke_others'),
    path('_/web/sessions/<str:session_id>/revoke', views.session_revoke, name='session_revoke'),
    path('_/web/employees/search', views.employee_search, name='employee_search'),
    path('_/web/bs-calendar.js', views.bs_calendar_js, name='bs_calendar_js'),
    path('_/web/bs-grid', views.bs_grid_partial, name='bs_grid'),
    path('_/web/components', views.components_demo, name='components'),
    path('_/web/components/action', views.components_demo_action, name='components_action'),

    re_path(r'^(?P<name>manifest\.webmanifest|sw\.js|favicon\.svg|icon-192\.png|icon-512\.png|apple-touch-icon\.png)$',
            views.root_file, name='root_file'),
    re_path(r'^hr/employees/(?P<id>[^/]+)$', RedirectView.as_view(url='/employees/%(id)s', permanent=False)),
    *[path(src, RedirectView.as_view(url=dst, permanent=False, query_string=True)) for src, dst in LEGACY],
]
