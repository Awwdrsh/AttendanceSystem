"""Group administration, JSON under /api/v1/group/... ."""
from django.urls import path

from . import api

app_name = 'group_admin'

urlpatterns = [
    path('api/v1/group/companies', api.CompaniesView.as_view(), name='api_companies'),
    path('api/v1/group/companies/attachable', api.AttachableView.as_view(), name='api_companies_attachable'),
    path('api/v1/group/companies/attach', api.AttachView.as_view(), name='api_company_attach'),
    path('api/v1/group/companies/<uuid:company_id>', api.CompanyView.as_view(), name='api_company'),
    path('api/v1/group/access', api.AccessView.as_view(), name='api_access'),
    path('api/v1/group/access/employees', api.AccessEmployeesView.as_view(), name='api_access_employees'),
    path('api/v1/group/access/invite', api.InviteView.as_view(), name='api_access_invite'),
    path('api/v1/group/access/<uuid:user_id>/<uuid:company_id>', api.AccessRevokeView.as_view(), name='api_access_revoke'),
    path('api/v1/group/transfers', api.TransfersView.as_view(), name='api_transfers'),
    path('api/v1/group/transfers/targets', api.TransferTargetsView.as_view(), name='api_transfer_targets'),
    path('api/v1/group/transfers/targets/<uuid:company_id>', api.TransferTargetView.as_view(), name='api_transfer_target'),
    path('api/v1/group/transfer-rules', api.TransferRulesView.as_view(), name='api_transfer_rules'),
    path('api/v1/group/policies/sections', api.PolicySectionsView.as_view(), name='api_policies_sections'),
    path('api/v1/group/policies/preview', api.PolicyPreviewView.as_view(), name='api_policies_preview'),
    path('api/v1/group/policies/apply', api.PolicyApplyView.as_view(), name='api_policies_apply'),
]
