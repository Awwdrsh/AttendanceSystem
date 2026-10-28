"""Group administration: pages under /group/..., fragments under /_/group-admin/..., JSON under /api/v1/group/...."""
from django.urls import path

from . import api, views

app_name = 'group_admin'

urlpatterns = [
    # Pages (tabs of the Group hub)
    path('group/companies', views.companies, name='companies'),
    path('group/access', views.access, name='access'),
    path('group/transfers', views.transfers, name='transfers'),
    path('group/policies', views.policies, name='policies'),
    # Fragments and form posts
    path('_/group-admin/companies', views.companies_table, name='companies_table'),
    path('_/group-admin/companies/new', views.company_new, name='company_new'),
    path('_/group-admin/companies/attach', views.company_attach, name='company_attach'),
    path('_/group-admin/companies/<uuid:company_id>/edit', views.company_edit, name='company_edit'),
    path('_/group-admin/access', views.access_table, name='access_table'),
    path('_/group-admin/access/new', views.access_new, name='access_new'),
    path('_/group-admin/access/invite', views.access_invite, name='access_invite'),
    path('_/group-admin/access/employees', views.access_employees, name='access_employees'),
    path('_/group-admin/access/<uuid:user_id>/<uuid:company_id>/revoke', views.access_revoke, name='access_revoke'),
    path('_/group-admin/transfers/target', views.transfer_target, name='transfer_target'),
    path('_/group-admin/transfers/new', views.transfer_new, name='transfer_new'),
    path('_/group-admin/transfers/rules', views.transfer_rule, name='transfer_rule'),
    path('_/group-admin/policies/preview', views.policies_preview, name='policies_preview'),
    path('_/group-admin/policies/apply', views.policies_apply, name='policies_apply'),
    # JSON
    path('api/v1/group/companies', api.CompaniesView.as_view(), name='api_companies'),
    path('api/v1/group/companies/attach', api.AttachView.as_view(), name='api_company_attach'),
    path('api/v1/group/companies/<uuid:company_id>', api.CompanyView.as_view(), name='api_company'),
    path('api/v1/group/access', api.AccessView.as_view(), name='api_access'),
    path('api/v1/group/access/invite', api.InviteView.as_view(), name='api_access_invite'),
    path('api/v1/group/access/<uuid:user_id>/<uuid:company_id>', api.AccessRevokeView.as_view(), name='api_access_revoke'),
    path('api/v1/group/transfers', api.TransfersView.as_view(), name='api_transfers'),
    path('api/v1/group/transfer-rules', api.TransferRulesView.as_view(), name='api_transfer_rules'),
    path('api/v1/group/policies/preview', api.PolicyPreviewView.as_view(), name='api_policies_preview'),
    path('api/v1/group/policies/apply', api.PolicyApplyView.as_view(), name='api_policies_apply'),
]
